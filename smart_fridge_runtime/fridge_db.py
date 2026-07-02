#!/usr/bin/env python3
"""SQLite runtime for the smart-fridge inventory and observation database."""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_DUPLICATE_WINDOW_MINUTES = 120


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS foods (
  food_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  storage_location TEXT,
  status_current TEXT,
  advice_current TEXT,
  confidence_current REAL,
  source_current TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_foods_normalized_name
  ON foods(normalized_name);

CREATE INDEX IF NOT EXISTS idx_foods_last_seen_at
  ON foods(last_seen_at);

CREATE TABLE IF NOT EXISTS food_observations (
  observation_id TEXT PRIMARY KEY,
  food_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  image_ref TEXT,
  yolo_label TEXT,
  yolo_confidence REAL,
  yolo_bbox_json TEXT,
  yolo_detection_json TEXT,
  vlm_name TEXT,
  vlm_state TEXT,
  vlm_confidence REAL,
  vlm_description TEXT,
  advice_label TEXT,
  raw_yolo_json TEXT,
  raw_vlm_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(food_id) REFERENCES foods(food_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_observations_food_captured
  ON food_observations(food_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_observations_captured_at
  ON food_observations(captured_at DESC);

CREATE TABLE IF NOT EXISTS food_events (
  event_id TEXT PRIMARY KEY,
  food_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_at TEXT NOT NULL,
  source TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(food_id) REFERENCES foods(food_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_food_event_at
  ON food_events(food_id, event_at DESC);
"""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_name(value):
    return " ".join((value or "").strip().casefold().split())


def compact_json(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def load_json_file(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def first_present(mapping, keys):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def load_yolo_result(path, detection_index):
    payload = load_json_file(path)
    if not payload:
        return {}, {}
    detections = payload.get("detections") or []
    detection = {}
    if detections and detection_index < len(detections):
        detection = detections[detection_index] or {}
    return payload, detection


def load_vlm_result(path):
    payload = load_json_file(path)
    if not payload:
        return {}, {}

    candidate = payload
    if isinstance(payload.get("result"), dict):
        candidate = payload["result"]
    elif isinstance(payload.get("food"), dict):
        candidate = payload["food"]

    result = {
        "name": first_present(candidate, ["food_name", "canonical_name", "name", "vlm_name", "label"]),
        "state": first_present(candidate, ["food_state", "state", "status", "freshness", "vlm_state"]),
        "confidence": first_present(candidate, ["confidence", "vlm_confidence", "score"]),
        "description": first_present(
            candidate,
            ["description", "vlm_description", "observation", "visual_description", "reason"],
        ),
        "advice": first_present(candidate, ["advice_label", "advice", "risk_level", "recommendation"]),
    }
    return payload, result


def coerce_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def derive_advice(state, explicit_advice):
    if explicit_advice:
        return str(explicit_advice)
    text = (state or "").casefold()
    danger_words = ("danger", "unsafe", "spoiled", "expired", "变质", "腐烂", "过期", "霉")
    attention_words = ("attention", "warning", "soon", "mild", "轻微", "尽快", "临期", "注意")
    if any(word in text for word in danger_words):
        return "danger"
    if any(word in text for word in attention_words):
        return "attention"
    if text:
        return "normal"
    return None


def connect(db_path):
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    now = utc_now()
    with conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
            (SCHEMA_VERSION, now),
        )


def row_to_dict(row):
    result = dict(row)
    for key in ("metadata_json", "payload_json", "yolo_bbox_json", "yolo_detection_json"):
        if key in result and result[key]:
            try:
                result[key] = json.loads(result[key])
            except json.JSONDecodeError:
                pass
    return result


def prefixed_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def find_food(conn, food_id, normalized_name, captured_at, duplicate_window_minutes):
    if food_id:
        row = conn.execute("SELECT * FROM foods WHERE food_id = ?", (food_id,)).fetchone()
        return row, bool(row)

    if not normalized_name:
        return None, False

    row = conn.execute(
        """
        SELECT * FROM foods
        WHERE normalized_name = ?
        ORDER BY last_seen_at DESC
        LIMIT 1
        """,
        (normalized_name,),
    ).fetchone()
    if not row:
        return None, False

    last_seen = parse_time(row["last_seen_at"])
    captured = parse_time(captured_at)
    if not last_seen or not captured:
        return row, True

    minutes = abs((captured - last_seen).total_seconds()) / 60.0
    return row, minutes <= duplicate_window_minutes


def command_init(args):
    with connect(args.db) as conn:
        init_schema(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print_json({"ok": True, "db": str(Path(args.db).expanduser()), "schema_version": SCHEMA_VERSION, "journal_mode": mode, "tables": tables})


def build_ingest_values(args):
    yolo_payload, yolo_detection = load_yolo_result(args.yolo_json, args.yolo_detection_index)
    vlm_payload, vlm_result = load_vlm_result(args.vlm_json)

    vlm_name = args.vlm_name or vlm_result.get("name")
    vlm_state = args.vlm_state or vlm_result.get("state")
    vlm_confidence = coerce_float(args.vlm_confidence if args.vlm_confidence is not None else vlm_result.get("confidence"))
    vlm_description = args.vlm_description or vlm_result.get("description")
    advice_label = derive_advice(vlm_state, args.advice_label or vlm_result.get("advice"))

    yolo_label = args.yolo_label or yolo_detection.get("class_name")
    yolo_confidence = coerce_float(
        args.yolo_confidence if args.yolo_confidence is not None else yolo_detection.get("confidence")
    )
    yolo_bbox = yolo_detection.get("box") if isinstance(yolo_detection, dict) else None

    canonical_name = args.canonical_name or vlm_name or yolo_label or "unknown_food"
    image_ref = args.image_ref or yolo_payload.get("image") or None
    captured_at = args.captured_at or utc_now()

    return {
        "captured_at": captured_at,
        "image_ref": image_ref,
        "canonical_name": str(canonical_name),
        "normalized_name": normalize_name(str(canonical_name)),
        "storage_location": args.storage_location,
        "yolo_label": yolo_label,
        "yolo_confidence": yolo_confidence,
        "yolo_bbox": yolo_bbox,
        "yolo_detection": yolo_detection,
        "yolo_payload": yolo_payload,
        "vlm_name": vlm_name,
        "vlm_state": vlm_state,
        "vlm_confidence": vlm_confidence,
        "vlm_description": vlm_description,
        "advice_label": advice_label,
        "vlm_payload": vlm_payload,
    }


def command_ingest(args):
    values = build_ingest_values(args)
    now = utc_now()
    duplicate_window = args.duplicate_window_minutes
    observation_id = prefixed_id("obs")
    event_id = prefixed_id("evt")

    with connect(args.db) as conn:
        init_schema(conn)
        with conn:
            if args.force_new_food:
                food_row, duplicate_candidate = None, False
            else:
                food_row, duplicate_candidate = find_food(
                    conn,
                    args.food_id,
                    values["normalized_name"],
                    values["captured_at"],
                    duplicate_window,
                )
            if food_row and duplicate_candidate:
                food_id = food_row["food_id"]
                event_type = "food.updated"
                conn.execute(
                    """
                    UPDATE foods
                    SET canonical_name = ?,
                        normalized_name = ?,
                        last_seen_at = ?,
                        storage_location = COALESCE(?, storage_location),
                        status_current = COALESCE(?, status_current),
                        advice_current = COALESCE(?, advice_current),
                        confidence_current = COALESCE(?, confidence_current),
                        source_current = ?
                    WHERE food_id = ?
                    """,
                    (
                        values["canonical_name"],
                        values["normalized_name"],
                        values["captured_at"],
                        values["storage_location"],
                        values["vlm_state"],
                        values["advice_label"],
                        values["vlm_confidence"] if values["vlm_confidence"] is not None else values["yolo_confidence"],
                        args.source,
                        food_id,
                    ),
                )
            else:
                food_id = prefixed_id("food")
                event_type = "food.created"
                duplicate_candidate = False
                conn.execute(
                    """
                    INSERT INTO foods(
                      food_id, canonical_name, normalized_name, first_seen_at, last_seen_at,
                      storage_location, status_current, advice_current, confidence_current,
                      source_current, metadata_json
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        food_id,
                        values["canonical_name"],
                        values["normalized_name"],
                        values["captured_at"],
                        values["captured_at"],
                        values["storage_location"],
                        values["vlm_state"],
                        values["advice_label"],
                        values["vlm_confidence"] if values["vlm_confidence"] is not None else values["yolo_confidence"],
                        args.source,
                        compact_json({}),
                    ),
                )

            conn.execute(
                """
                INSERT INTO food_observations(
                  observation_id, food_id, captured_at, image_ref,
                  yolo_label, yolo_confidence, yolo_bbox_json, yolo_detection_json,
                  vlm_name, vlm_state, vlm_confidence, vlm_description, advice_label,
                  raw_yolo_json, raw_vlm_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    food_id,
                    values["captured_at"],
                    values["image_ref"],
                    values["yolo_label"],
                    values["yolo_confidence"],
                    compact_json(values["yolo_bbox"]) if values["yolo_bbox"] else None,
                    compact_json(values["yolo_detection"]) if values["yolo_detection"] else None,
                    values["vlm_name"],
                    values["vlm_state"],
                    values["vlm_confidence"],
                    values["vlm_description"],
                    values["advice_label"],
                    compact_json(values["yolo_payload"]) if values["yolo_payload"] else None,
                    compact_json(values["vlm_payload"]) if values["vlm_payload"] else None,
                    now,
                ),
            )

            event_payload = {
                "observation_id": observation_id,
                "duplicate_candidate": duplicate_candidate,
                "canonical_name": values["canonical_name"],
                "status_current": values["vlm_state"],
                "advice_current": values["advice_label"],
                "yolo_label": values["yolo_label"],
            }
            conn.execute(
                """
                INSERT INTO food_events(event_id, food_id, event_type, event_at, source, payload_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (event_id, food_id, event_type, values["captured_at"], args.source, compact_json(event_payload)),
            )

    print_json(
        {
            "ok": True,
            "food_id": food_id,
            "observation_id": observation_id,
            "event_id": event_id,
            "event_type": event_type,
            "duplicate_candidate": duplicate_candidate,
            "canonical_name": values["canonical_name"],
            "status_current": values["vlm_state"],
            "advice_current": values["advice_label"],
        }
    )


def command_list_foods(args):
    with connect(args.db) as conn:
        init_schema(conn)
        params = []
        where = []
        if args.status:
            where.append("status_current = ?")
            params.append(args.status)
        sql = "SELECT * FROM foods"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(args.limit)
        rows = [row_to_dict(row) for row in conn.execute(sql, params)]
    if args.format == "json":
        print_json({"foods": rows})
    else:
        for row in rows:
            print(
                "{food_id}\t{canonical_name}\t{status}\t{advice}\t{last_seen}".format(
                    food_id=row["food_id"],
                    canonical_name=row["canonical_name"],
                    status=row.get("status_current") or "-",
                    advice=row.get("advice_current") or "-",
                    last_seen=row["last_seen_at"],
                )
            )


def command_show_food(args):
    with connect(args.db) as conn:
        init_schema(conn)
        food = conn.execute("SELECT * FROM foods WHERE food_id = ?", (args.food_id,)).fetchone()
        if not food:
            print_json({"ok": False, "error": "food_not_found", "food_id": args.food_id})
            return 1
        observations = [
            row_to_dict(row)
            for row in conn.execute(
                """
                SELECT * FROM food_observations
                WHERE food_id = ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (args.food_id, args.limit),
            )
        ]
        events = [
            row_to_dict(row)
            for row in conn.execute(
                """
                SELECT * FROM food_events
                WHERE food_id = ?
                ORDER BY event_at DESC
                LIMIT ?
                """,
                (args.food_id, args.limit),
            )
        ]
    print_json({"ok": True, "food": row_to_dict(food), "observations": observations, "events": events})
    return 0


def command_add_event(args):
    payload = {}
    if args.payload_json:
        try:
            payload = json.loads(args.payload_json)
        except json.JSONDecodeError as exc:
            raise SystemExit("Invalid --payload-json: {0}".format(exc))
    elif args.payload_file:
        payload = load_json_file(args.payload_file) or {}

    event_id = prefixed_id("evt")
    event_at = args.event_at or utc_now()
    with connect(args.db) as conn:
        init_schema(conn)
        with conn:
            food = conn.execute("SELECT food_id FROM foods WHERE food_id = ?", (args.food_id,)).fetchone()
            if not food:
                print_json({"ok": False, "error": "food_not_found", "food_id": args.food_id})
                return 1
            conn.execute(
                """
                INSERT INTO food_events(event_id, food_id, event_type, event_at, source, payload_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (event_id, args.food_id, args.event_type, event_at, args.source, compact_json(payload)),
            )
            if args.event_type in ("food.removed", "food.consumed", "food.discarded"):
                conn.execute(
                    """
                    UPDATE foods
                    SET status_current = ?, advice_current = ?, last_seen_at = ?, source_current = ?
                    WHERE food_id = ?
                    """,
                    (args.event_type, "closed", event_at, args.source, args.food_id),
                )
    print_json({"ok": True, "event_id": event_id, "food_id": args.food_id, "event_type": args.event_type})
    return 0


def command_health(args):
    with connect(args.db) as conn:
        init_schema(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        counts = {
            "foods": conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0],
            "food_observations": conn.execute("SELECT COUNT(*) FROM food_observations").fetchone()[0],
            "food_events": conn.execute("SELECT COUNT(*) FROM food_events").fetchone()[0],
        }
    print_json(
        {
            "ok": integrity == "ok",
            "db": str(Path(args.db).expanduser()),
            "integrity_check": integrity,
            "journal_mode": journal_mode,
            "schema_version": SCHEMA_VERSION,
            "counts": counts,
        }
    )
    return 0 if integrity == "ok" else 1


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def parse_args(argv):
    default_db = os.environ.get("SMART_FRIDGE_DB_PATH", "data/smart_fridge.sqlite3")
    default_duplicate_window = int(
        os.environ.get("SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES", DEFAULT_DUPLICATE_WINDOW_MINUTES)
    )
    parser = argparse.ArgumentParser(description="Manage the smart-fridge SQLite inventory database.")
    parser.add_argument("--db", default=default_db, help="SQLite database path.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or migrate the SQLite schema.")
    init_parser.set_defaults(func=command_init)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest one YOLO/VLM observation into the database.")
    ingest_parser.add_argument("--source", default="smart-fridge-runtime")
    ingest_parser.add_argument("--captured-at")
    ingest_parser.add_argument("--image-ref")
    ingest_parser.add_argument("--food-id")
    ingest_parser.add_argument("--force-new-food", action="store_true")
    ingest_parser.add_argument("--canonical-name")
    ingest_parser.add_argument("--storage-location")
    ingest_parser.add_argument("--duplicate-window-minutes", type=int, default=default_duplicate_window)
    ingest_parser.add_argument("--yolo-json")
    ingest_parser.add_argument("--yolo-detection-index", type=int, default=0)
    ingest_parser.add_argument("--yolo-label")
    ingest_parser.add_argument("--yolo-confidence", type=float)
    ingest_parser.add_argument("--vlm-json")
    ingest_parser.add_argument("--vlm-name")
    ingest_parser.add_argument("--vlm-state")
    ingest_parser.add_argument("--vlm-confidence", type=float)
    ingest_parser.add_argument("--vlm-description")
    ingest_parser.add_argument("--advice-label")
    ingest_parser.set_defaults(func=command_ingest)

    list_parser = subparsers.add_parser("list-foods", help="List current food records.")
    list_parser.add_argument("--status")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--format", choices=("json", "text"), default="text")
    list_parser.set_defaults(func=command_list_foods)

    show_parser = subparsers.add_parser("show-food", help="Show one food record with observations and events.")
    show_parser.add_argument("--food-id", required=True)
    show_parser.add_argument("--limit", type=int, default=20)
    show_parser.set_defaults(func=command_show_food)

    event_parser = subparsers.add_parser("add-event", help="Append a manual or system event for one food.")
    event_parser.add_argument("--food-id", required=True)
    event_parser.add_argument("--event-type", required=True)
    event_parser.add_argument("--event-at")
    event_parser.add_argument("--source", default="manual")
    event_parser.add_argument("--payload-json")
    event_parser.add_argument("--payload-file")
    event_parser.set_defaults(func=command_add_event)

    health_parser = subparsers.add_parser("health", help="Run integrity and schema checks.")
    health_parser.set_defaults(func=command_health)

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    result = args.func(args)
    return 0 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main())
