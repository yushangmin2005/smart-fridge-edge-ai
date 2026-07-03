#!/usr/bin/env python3
"""Minimal web dashboard for the smart-fridge runtime."""

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


DEFAULT_LIMIT = 24


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def env(name, default=None):
    return os.environ.get(name, default)


def env_int(name, default):
    value = env(name)
    if value in (None, ""):
        return default
    return int(value)


def read_json(path, default=None):
    if not path:
        return default
    target = Path(path)
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def compact_json_loads(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def row_to_dict(row):
    result = dict(row)
    for key in ("metadata_json", "payload_json", "yolo_bbox_json", "yolo_detection_json", "raw_yolo_json", "raw_vlm_json"):
        if key in result:
            parsed = compact_json_loads(result[key])
            if parsed is not None:
                result[key] = parsed
    return result


def file_info(path):
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return None
    return {
        "path": str(target),
        "name": target.name,
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def list_files(directory, patterns, limit):
    root = Path(directory).expanduser()
    if not root.exists():
        return []
    files = []
    for pattern in patterns:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    files = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)
    return [item for item in (file_info(path) for path in files[:limit]) if item]


def check_pid(pid_file):
    path = Path(pid_file)
    if not path.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return {"running": False, "pid": None}
    try:
        os.kill(pid, 0)
        return {"running": True, "pid": pid}
    except OSError:
        return {"running": False, "pid": pid}


def read_tail(path, lines=80):
    target = Path(path)
    if not target.exists():
        return []
    try:
        data = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return data[-lines:]


def parse_last_cycle(log_lines):
    decoder = json.JSONDecoder()
    text = "\n".join(log_lines)
    last = None
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            payload, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(payload, dict) and "captured_at" in payload and "detections" in payload:
            last = payload
        index = start + end
    return last


class SmartFridgeStore:
    def __init__(self):
        root = Path(env("SMART_FRIDGE_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser()
        self.root = root
        self.db_path = Path(env("SMART_FRIDGE_DB_PATH", str(root / "data" / "fridge.sqlite3"))).expanduser()
        self.tmp_dir = Path(env("SMART_FRIDGE_TMP_DIR", str(root / "tmp"))).expanduser()
        self.state_path = Path(env("SMART_FRIDGE_STATE_PATH", str(root / "data" / "pipeline_state.json"))).expanduser()
        self.capture_dir = Path(env("SMART_FRIDGE_CAPTURE_DIR", str(self.tmp_dir / "captures"))).expanduser()
        self.crop_dir = Path(env("SMART_FRIDGE_CROP_DIR", str(self.tmp_dir / "crops"))).expanduser()
        self.yolo_dir = Path(env("SMART_FRIDGE_YOLO_OUTPUT_DIR", str(self.tmp_dir / "yolo"))).expanduser()
        self.vlm_dir = Path(env("SMART_FRIDGE_VLM_OUTPUT_DIR", str(self.tmp_dir / "vlm"))).expanduser()
        self.pipeline_log = Path(env("SMART_FRIDGE_PIPELINE_LOG", str(root / "logs" / "fridge-pipeline.log"))).expanduser()
        self.pipeline_pid = Path(env("SMART_FRIDGE_PIPELINE_PID", str(root / "run" / "fridge-pipeline.pid"))).expanduser()
        self.web_pid = Path(env("SMART_FRIDGE_WEB_PID", str(root / "run" / "fridge-web.pid"))).expanduser()
        self.vlm_pid = Path(env("SMART_FRIDGE_VLM_PID", str(Path.home() / "vlm-inference" / "run" / "vlm.pid"))).expanduser()

    def connect(self):
        if not self.db_path.exists():
            return None
        uri = "file:{0}?mode=ro".format(quote(str(self.db_path), safe="/:"))
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def query_all(self, sql, params=()):
        conn = self.connect()
        if conn is None:
            return []
        try:
            with conn:
                return [row_to_dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    def db_counts(self):
        conn = self.connect()
        if conn is None:
            return {"foods": 0, "food_observations": 0, "food_events": 0}
        try:
            counts = {}
            for table in ("foods", "food_observations", "food_events"):
                counts[table] = conn.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]
            return counts
        finally:
            conn.close()

    def foods(self, limit=DEFAULT_LIMIT):
        return self.query_all(
            """
            SELECT *
            FROM foods
            ORDER BY
              CASE WHEN status_current = 'food.removed' THEN 1 ELSE 0 END,
              last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def recent_events(self, limit=DEFAULT_LIMIT):
        return self.query_all(
            """
            SELECT e.*, f.canonical_name, f.status_current, f.advice_current
            FROM food_events e
            LEFT JOIN foods f ON f.food_id = e.food_id
            ORDER BY e.event_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def observations(self, limit=DEFAULT_LIMIT):
        return self.query_all(
            """
            SELECT o.*, f.canonical_name
            FROM food_observations o
            LEFT JOIN foods f ON f.food_id = o.food_id
            ORDER BY o.captured_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def state(self):
        return read_json(self.state_path, {"active_objects": []}) or {"active_objects": []}

    def media_url(self, path):
        if not path:
            return None
        return "/media?path={0}".format(quote(str(path)))

    def decorate_image_items(self, items):
        result = []
        for item in items:
            copied = dict(item)
            copied["url"] = self.media_url(copied["path"])
            result.append(copied)
        return result

    def latest_yolo_payload(self):
        yolo_files = list_files(self.yolo_dir, ["*.json"], 1)
        if not yolo_files:
            return {}
        payload = read_json(yolo_files[0]["path"], {}) or {}
        payload["_file"] = yolo_files[0]
        return payload

    def overview(self):
        captures = self.decorate_image_items(list_files(self.capture_dir, ["*.jpg", "*.jpeg", "*.png"], DEFAULT_LIMIT))
        crops = self.decorate_image_items(list_files(self.crop_dir, ["*.jpg", "*.jpeg", "*.png"], DEFAULT_LIMIT))
        vlm_files = list_files(self.vlm_dir, ["*.json"], DEFAULT_LIMIT)
        yolo_files = list_files(self.yolo_dir, ["*.json"], DEFAULT_LIMIT)
        log_tail = read_tail(self.pipeline_log, 120)
        state = self.state()
        latest_capture = captures[0] if captures else None
        latest_yolo = self.latest_yolo_payload()
        active_objects = state.get("active_objects") or []
        active_food_ids = {item.get("food_id") for item in active_objects if item.get("food_id")}
        foods = self.foods(50)
        for food in foods:
            food["active"] = food.get("food_id") in active_food_ids and food.get("status_current") != "food.removed"
        return {
            "ok": True,
            "generated_at": utc_now(),
            "config": {
                "root": str(self.root),
                "db_path": str(self.db_path),
                "capture_interval_seconds": env_int("SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS", 3600),
                "capture_keep": env_int("SMART_FRIDGE_CAPTURE_KEEP", 24),
                "camera_device": env("SMART_FRIDGE_CAMERA_DEVICE", "auto"),
                "vlm_timeout_seconds": env_int("SMART_FRIDGE_VLM_TIMEOUT", 3600),
                "refresh_seconds": env_int("SMART_FRIDGE_WEB_REFRESH_SECONDS", 30),
            },
            "services": {
                "pipeline": check_pid(self.pipeline_pid),
                "web": check_pid(self.web_pid),
                "vlm": check_pid(self.vlm_pid),
            },
            "db_counts": self.db_counts(),
            "foods": foods,
            "events": self.recent_events(40),
            "observations": self.observations(20),
            "state": state,
            "active_objects": active_objects,
            "latest_capture": latest_capture,
            "captures": captures,
            "crops": crops,
            "yolo_files": yolo_files,
            "vlm_files": vlm_files,
            "latest_yolo": latest_yolo,
            "last_cycle": parse_last_cycle(log_tail),
            "log_tail": log_tail[-80:],
        }

    def allowed_media_path(self, raw_path):
        if not raw_path:
            return None
        try:
            candidate = Path(unquote(raw_path)).expanduser().resolve()
        except OSError:
            return None
        allowed_roots = [
            self.capture_dir.resolve(),
            self.crop_dir.resolve(),
        ]
        if candidate.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            return None
        for root in allowed_roots:
            try:
                candidate.relative_to(root)
                return candidate if candidate.is_file() else None
            except ValueError:
                continue
        return None


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>智能冰箱状态面板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #687385;
      --line: #dce1e8;
      --ok: #20865a;
      --warn: #a66505;
      --bad: #b42318;
      --info: #245f9f;
      --soft: #eef2f6;
      --shadow: 0 1px 2px rgba(24, 39, 75, 0.06);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(255, 255, 255, 0.94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
    }
    .topbar {
      max-width: 1320px;
      margin: 0 auto;
      padding: 14px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 720; letter-spacing: 0; }
    .subline { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 0 12px;
      cursor: pointer;
      box-shadow: var(--shadow);
    }
    button:hover { border-color: #b8c1cd; }
    main {
      max-width: 1320px;
      margin: 0 auto;
      padding: 18px 20px 36px;
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
      gap: 16px;
    }
    .band {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .section-head {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    h2 { margin: 0; font-size: 15px; font-weight: 700; letter-spacing: 0; }
    .grid-metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .metric { padding: 12px; min-height: 76px; }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 24px; font-weight: 760; margin-top: 4px; }
    .metric .note { color: var(--muted); font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .content { padding: 12px 14px; }
    .split { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 14px; }
    .photo-wrap {
      min-height: 310px;
      background: #101820;
      border-radius: 6px;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .photo-wrap img { width: 100%; height: 100%; max-height: 440px; object-fit: contain; display: block; }
    .photo-meta { display: grid; gap: 8px; align-content: start; }
    .kv {
      display: grid;
      grid-template-columns: 96px minmax(0, 1fr);
      gap: 8px;
      padding: 7px 0;
      border-bottom: 1px solid var(--soft);
    }
    .kv span:first-child { color: var(--muted); }
    .kv span:last-child { overflow-wrap: anywhere; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 760px; }
    th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--soft); vertical-align: top; }
    th { color: var(--muted); font-weight: 650; font-size: 12px; background: #fbfcfd; }
    tr:last-child td { border-bottom: 0; }
    .badge {
      display: inline-flex;
      align-items: center;
      height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      font-size: 12px;
      border: 1px solid transparent;
      white-space: nowrap;
    }
    .badge.ok { color: var(--ok); background: #e9f6ef; border-color: #c6ead8; }
    .badge.warn { color: var(--warn); background: #fff4de; border-color: #f5d69a; }
    .badge.bad { color: var(--bad); background: #fdecec; border-color: #f6c7c7; }
    .badge.info { color: var(--info); background: #ebf3fc; border-color: #cbdff5; }
    .badge.off { color: var(--muted); background: #f0f2f5; border-color: #dce1e8; }
    .timeline { display: grid; gap: 10px; }
    .event {
      border-left: 3px solid #9db5cf;
      padding: 2px 0 2px 10px;
    }
    .event strong { display: block; font-size: 13px; }
    .event p { margin: 3px 0 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .thumbs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .thumb {
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
      background: var(--soft);
      min-height: 72px;
    }
    .thumb img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; }
    .thumb div { padding: 5px 6px; font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    pre {
      margin: 0;
      max-height: 270px;
      overflow: auto;
      background: #101820;
      color: #d9e4f2;
      padding: 10px;
      border-radius: 6px;
      font-size: 12px;
      line-height: 1.35;
    }
    .stack { display: grid; gap: 16px; align-content: start; }
    .muted { color: var(--muted); }
    .empty { color: var(--muted); padding: 18px 0; text-align: center; }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .grid-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .split { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .topbar { align-items: flex-start; flex-direction: column; }
      .grid-metrics { grid-template-columns: 1fr; }
      main { padding: 12px; }
      .thumbs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>智能冰箱状态面板</h1>
        <div class="subline" id="subtitle">等待数据</div>
      </div>
      <div class="actions">
        <span class="badge off" id="pipelineStatus">pipeline</span>
        <span class="badge off" id="vlmStatus">vlm</span>
        <button id="refreshBtn" type="button">刷新</button>
      </div>
    </div>
  </header>
  <main>
    <div class="stack">
      <section class="grid-metrics">
        <div class="band metric"><div class="label">当前活跃</div><div class="value" id="activeCount">-</div><div class="note">state active objects</div></div>
        <div class="band metric"><div class="label">食物记录</div><div class="value" id="foodCount">-</div><div class="note">SQLite foods</div></div>
        <div class="band metric"><div class="label">观察记录</div><div class="value" id="obsCount">-</div><div class="note">YOLO/VLM observations</div></div>
        <div class="band metric"><div class="label">事件记录</div><div class="value" id="eventCount">-</div><div class="note">created / removed</div></div>
      </section>

      <section class="band">
        <div class="section-head"><h2>最新画面</h2><span class="muted" id="captureTime">-</span></div>
        <div class="content split">
          <div class="photo-wrap" id="photoBox"><span class="muted">暂无照片</span></div>
          <div class="photo-meta">
            <div class="kv"><span>摄像头</span><span id="cameraDevice">-</span></div>
            <div class="kv"><span>最新检测</span><span id="latestDetections">-</span></div>
            <div class="kv"><span>当前周期</span><span id="lastCycle">-</span></div>
            <div class="kv"><span>照片数量</span><span id="captureCount">-</span></div>
            <div class="kv"><span>VLM 超时</span><span id="vlmTimeout">-</span></div>
          </div>
        </div>
      </section>

      <section class="band">
        <div class="section-head"><h2>当前库存与状态</h2><span class="muted" id="foodSummary">-</span></div>
        <div class="table-wrap"><table><thead><tr><th>状态</th><th>名称</th><th>新鲜度</th><th>建议</th><th>置信度</th><th>最近出现</th><th>ID</th></tr></thead><tbody id="foodsBody"></tbody></table></div>
      </section>

      <section class="band">
        <div class="section-head"><h2>识别变化</h2><span class="muted">最近事件</span></div>
        <div class="content"><div class="timeline" id="events"></div></div>
      </section>
    </div>

    <aside class="stack">
      <section class="band">
        <div class="section-head"><h2>最近裁剪</h2><span class="muted">新增目标</span></div>
        <div class="content"><div class="thumbs" id="crops"></div></div>
      </section>
      <section class="band">
        <div class="section-head"><h2>最近照片</h2><span class="muted">最多 24 张</span></div>
        <div class="content"><div class="thumbs" id="captures"></div></div>
      </section>
      <section class="band">
        <div class="section-head"><h2>活跃目标</h2><span class="muted">pipeline state</span></div>
        <div class="content"><div class="timeline" id="activeObjects"></div></div>
      </section>
      <section class="band">
        <div class="section-head"><h2>运行日志</h2><span class="muted">tail</span></div>
        <div class="content"><pre id="logs">等待数据</pre></div>
      </section>
    </aside>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
    const fmtTime = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-";
    const badgeClass = (value) => {
      const text = String(value || "").toLowerCase();
      if (text.includes("danger") || text.includes("removed") || text.includes("失败")) return "bad";
      if (text.includes("attention") || text.includes("warning") || text.includes("unknown")) return "warn";
      if (text.includes("normal") || text.includes("running") || text === "ok") return "ok";
      return "info";
    };
    const badge = (text) => `<span class="badge ${badgeClass(text)}">${escapeHtml(text || "-")}</span>`;
    const media = (item) => item && item.url ? item.url : "";

    async function loadData() {
      const res = await fetch("/api/overview", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }

    function renderThumbs(id, items) {
      const root = $(id);
      if (!items || !items.length) {
        root.innerHTML = `<div class="empty">暂无图片</div>`;
        return;
      }
      root.innerHTML = items.slice(0, 9).map((item) => `
        <a class="thumb" href="${media(item)}" target="_blank" rel="noreferrer">
          <img src="${media(item)}" alt="${escapeHtml(item.name)}">
          <div title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
        </a>
      `).join("");
    }

    function renderFoods(data) {
      const rows = (data.foods || []).map((food) => {
        const active = food.active ? "active" : (food.status_current === "food.removed" ? "removed" : "inactive");
        return `<tr>
          <td>${badge(active)}</td>
          <td>${escapeHtml(food.canonical_name)}</td>
          <td>${badge(food.status_current || "-")}</td>
          <td>${badge(food.advice_current || "-")}</td>
          <td>${food.confidence_current ?? "-"}</td>
          <td>${fmtTime(food.last_seen_at)}</td>
          <td><code>${escapeHtml(food.food_id)}</code></td>
        </tr>`;
      }).join("");
      $("foodsBody").innerHTML = rows || `<tr><td colspan="7" class="empty">暂无库存记录</td></tr>`;
      $("foodSummary").textContent = `${data.foods?.length || 0} 条`;
    }

    function renderEvents(data) {
      const events = (data.events || []).slice(0, 12);
      $("events").innerHTML = events.length ? events.map((event) => {
        const payload = event.payload_json || {};
        const detail = [
          event.event_at && fmtTime(event.event_at),
          payload.yolo_label && `YOLO ${payload.yolo_label}`,
          payload.status_current && `状态 ${payload.status_current}`,
        ].filter(Boolean).join(" · ");
        return `<div class="event">
          <strong>${escapeHtml(event.event_type)} · ${escapeHtml(event.canonical_name || event.food_id)}</strong>
          <p>${escapeHtml(detail || event.food_id)}</p>
        </div>`;
      }).join("") : `<div class="empty">暂无变化事件</div>`;
    }

    function renderActiveObjects(data) {
      const objects = data.active_objects || [];
      $("activeObjects").innerHTML = objects.length ? objects.map((item) => `
        <div class="event">
          <strong>${escapeHtml(item.yolo_label || "object")} · ${escapeHtml(item.food_id || "-")}</strong>
          <p>${escapeHtml(item.vlm?.food_name || "")} ${escapeHtml(item.vlm?.freshness || "")} ${escapeHtml(item.vlm?.risk_level || "")}</p>
        </div>
      `).join("") : `<div class="empty">当前没有 active object</div>`;
    }

    function render(data) {
      const counts = data.db_counts || {};
      const lastCycle = data.last_cycle || {};
      $("subtitle").textContent = `更新时间 ${fmtTime(data.generated_at)} · DB ${data.config?.db_path || "-"}`;
      $("activeCount").textContent = (data.active_objects || []).length;
      $("foodCount").textContent = counts.foods ?? 0;
      $("obsCount").textContent = counts.food_observations ?? 0;
      $("eventCount").textContent = counts.food_events ?? 0;
      $("pipelineStatus").className = `badge ${data.services?.pipeline?.running ? "ok" : "bad"}`;
      $("pipelineStatus").textContent = data.services?.pipeline?.running ? `pipeline ${data.services.pipeline.pid}` : "pipeline stopped";
      $("vlmStatus").className = `badge ${data.services?.vlm?.running ? "ok" : "warn"}`;
      $("vlmStatus").textContent = data.services?.vlm?.running ? `vlm ${data.services.vlm.pid}` : "vlm unknown";
      $("cameraDevice").textContent = data.config?.camera_device || "-";
      $("latestDetections").textContent = `${lastCycle.detections ?? data.latest_yolo?.detections?.length ?? 0}`;
      $("lastCycle").textContent = lastCycle.captured_at ? `${fmtTime(lastCycle.captured_at)} / ${lastCycle.ok ? "ok" : "error"}` : "-";
      $("captureCount").textContent = `${data.captures?.length || 0} / ${data.config?.capture_keep || 24}`;
      $("vlmTimeout").textContent = `${data.config?.vlm_timeout_seconds || 0}s`;
      $("captureTime").textContent = data.latest_capture ? fmtTime(data.latest_capture.modified_at) : "-";
      if (data.latest_capture) {
        $("photoBox").innerHTML = `<img src="${media(data.latest_capture)}" alt="latest capture">`;
      } else {
        $("photoBox").innerHTML = `<span class="muted">暂无照片</span>`;
      }
      renderFoods(data);
      renderEvents(data);
      renderActiveObjects(data);
      renderThumbs("crops", data.crops || []);
      renderThumbs("captures", data.captures || []);
      $("logs").textContent = (data.log_tail || []).join("\n") || "暂无日志";
    }

    let refreshTimer = null;

    async function refresh() {
      $("refreshBtn").disabled = true;
      try {
        const data = await loadData();
        render(data);
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refresh, Math.max(5, data.config?.refresh_seconds || 30) * 1000);
      } catch (err) {
        $("subtitle").textContent = `加载失败：${err.message}`;
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refresh, 30000);
      } finally {
        $("refreshBtn").disabled = false;
      }
    }

    $("refreshBtn").addEventListener("click", refresh);
    refresh();
  </script>
</body>
</html>
"""


class SmartFridgeHandler(BaseHTTPRequestHandler):
    server_version = "SmartFridgeWeb/0.1"

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send_bytes(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        response_headers = {"Cache-Control": "no-store"}
        if headers:
            response_headers.update(headers)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_error_json(self, status, message):
        self.send_json({"ok": False, "error": message, "status": status}, status=status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/overview":
            self.send_json(self.store.overview())
            return
        if parsed.path == "/api/foods":
            limit = env_int("SMART_FRIDGE_WEB_API_LIMIT", DEFAULT_LIMIT)
            self.send_json({"ok": True, "foods": self.store.foods(limit)})
            return
        if parsed.path == "/api/events":
            limit = env_int("SMART_FRIDGE_WEB_API_LIMIT", DEFAULT_LIMIT)
            self.send_json({"ok": True, "events": self.store.recent_events(limit)})
            return
        if parsed.path == "/api/captures":
            captures = self.store.decorate_image_items(list_files(self.store.capture_dir, ["*.jpg", "*.jpeg", "*.png"], DEFAULT_LIMIT))
            self.send_json({"ok": True, "captures": captures})
            return
        if parsed.path == "/api/state":
            self.send_json({"ok": True, "state": self.store.state()})
            return
        if parsed.path == "/media":
            query = parse_qs(parsed.query)
            media_path = self.store.allowed_media_path((query.get("path") or [""])[0])
            if not media_path:
                self.send_error_json(404, "media_not_found")
                return
            content_type = mimetypes.guess_type(str(media_path))[0] or "application/octet-stream"
            try:
                self.send_bytes(200, media_path.read_bytes(), content_type, {"Cache-Control": "public, max-age=30"})
            except OSError as exc:
                self.send_error_json(500, str(exc))
            return
        self.send_error_json(404, "not_found")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Serve the smart-fridge web dashboard.")
    parser.add_argument("--host", default=env("SMART_FRIDGE_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=env_int("SMART_FRIDGE_WEB_PORT", 8090))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    server = ThreadingHTTPServer((args.host, args.port), SmartFridgeHandler)
    server.store = SmartFridgeStore()
    print("smart_fridge_web=http://{0}:{1}".format(args.host, args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
