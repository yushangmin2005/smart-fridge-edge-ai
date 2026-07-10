#!/usr/bin/env python3
"""Read ESP32-S3 JSON Lines sensor frames and publish a normalized snapshot."""

import argparse
import glob
import json
import os
import select
import sys
import termios
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DEVICE = "auto"
DEFAULT_BAUD_RATE = 115200
DEFAULT_STALE_SECONDS = 10
DEFAULT_RETRY_SECONDS = 5
DEFAULT_READ_TIMEOUT_SECONDS = 10
DEFAULT_MAX_LINE_BYTES = 16384


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def env(name, default=None):
    return os.environ.get(name, default)


def env_int(name, default):
    value = env(name)
    if value in (None, ""):
        return default
    return int(value)


def env_bool(name, default=False):
    value = env(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def read_json(path, default=None):
    target = Path(path).expanduser()
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path, payload):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".{0}.{1}.tmp".format(target.name, os.getpid()))
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(target))


def parse_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_sensor_frame(frame, received_at=None, device=None, baud_rate=DEFAULT_BAUD_RATE, door_inverted=True):
    if not isinstance(frame, dict):
        raise ValueError("sensor frame must be a JSON object")
    if frame.get("v") != 2:
        raise ValueError("unsupported sensor protocol version: {0}".format(frame.get("v")))

    received_at = received_at or utc_now()
    raw = dict(frame)
    data = dict(frame)
    data["protocol_version"] = data.pop("v")

    reported_open = data.pop("door_open", None)
    reported_state = data.pop("door_state", None)
    reported_count = data.pop("door_open_count", None)
    actual_open = None
    if isinstance(reported_open, bool):
        actual_open = not reported_open if door_inverted else reported_open
    elif reported_state in ("open", "closed"):
        reported_state_open = reported_state == "open"
        actual_open = not reported_state_open if door_inverted else reported_state_open

    data.update(
        {
            "door_open": actual_open,
            "door_state": "open" if actual_open is True else "closed" if actual_open is False else "unknown",
            "door_mapping_corrected": bool(door_inverted),
            "reported_door_open": reported_open,
            "reported_door_state": reported_state,
            "reported_door_open_count": reported_count,
        }
    )

    health_values = [data.get(name) for name in ("aht_ok", "ntc_ok", "door_sensor_ok")]
    health_ok = all(value is True for value in health_values)
    return {
        "ok": health_ok,
        "connected": True,
        "source": "esp32-s3",
        "device": device,
        "baud_rate": baud_rate,
        "received_at": received_at,
        "updated_at": received_at,
        "door_mapping": "inverted" if door_inverted else "direct",
        "data": data,
        "raw": raw,
    }


def read_sensor_state(path, stale_after_seconds=DEFAULT_STALE_SECONDS, now=None):
    state = read_json(path, None)
    if not isinstance(state, dict):
        return {
            "ok": False,
            "connected": False,
            "available": False,
            "fresh": False,
            "age_seconds": None,
            "received_at": None,
            "data": {},
            "error": "sensor_state_missing",
        }

    result = dict(state)
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
    result["data"] = data
    received_at = parse_utc(result.get("received_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = None
    if received_at is not None:
        age_seconds = max(0.0, (current.astimezone(timezone.utc) - received_at).total_seconds())
    result["available"] = bool(data)
    result["age_seconds"] = round(age_seconds, 1) if age_seconds is not None else None
    result["fresh"] = bool(
        result.get("connected")
        and age_seconds is not None
        and age_seconds <= max(0, stale_after_seconds)
    )
    return result


def sensor_ai_context(state):
    data = dict((state or {}).get("data") or {})
    context = {
        "available": bool((state or {}).get("available")),
        "connected": bool((state or {}).get("connected")),
        "fresh": bool((state or {}).get("fresh")),
        "health_ok": bool((state or {}).get("ok")),
        "received_at": (state or {}).get("received_at"),
        "age_seconds": (state or {}).get("age_seconds"),
        "source": (state or {}).get("source") or "esp32-s3",
    }
    context.update(data)
    return context


def resolve_device(configured=DEFAULT_DEVICE):
    if configured and configured != "auto":
        target = Path(configured).expanduser()
        if target.exists():
            return str(target)
        raise FileNotFoundError("configured sensor device not found: {0}".format(target))

    candidates = []
    candidates.extend(sorted(glob.glob("/dev/serial/by-id/*")))
    candidates.extend(sorted(glob.glob("/dev/ttyUSB*")))
    candidates.extend(sorted(glob.glob("/dev/ttyACM*")))
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("no USB serial sensor device found")


def baud_constant(baud_rate):
    name = "B{0}".format(baud_rate)
    if not hasattr(termios, name):
        raise ValueError("unsupported baud rate: {0}".format(baud_rate))
    return getattr(termios, name)


def configure_serial(fd, baud_rate):
    speed = baud_constant(baud_rate)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)


def publish_error(state_path, error, device=None):
    state = read_json(state_path, {}) or {}
    if not isinstance(state, dict):
        state = {}
    state.update(
        {
            "ok": False,
            "connected": False,
            "updated_at": utc_now(),
            "device": device or state.get("device"),
            "error": str(error),
        }
    )
    state.setdefault("data", {})
    write_json_atomic(state_path, state)


def read_device(device, baud_rate, state_path, door_inverted, once, read_timeout_seconds, max_line_bytes):
    fd = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        configure_serial(fd, baud_rate)
        started = time.monotonic()
        buffer = b""
        while True:
            remaining = max(0.1, read_timeout_seconds - (time.monotonic() - started)) if once else read_timeout_seconds
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                if once:
                    raise TimeoutError("no valid sensor frame received within {0}s".format(read_timeout_seconds))
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                raise OSError("sensor serial device returned EOF")
            buffer += chunk
            if len(buffer) > max_line_bytes and b"\n" not in buffer:
                buffer = b""
                continue
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line or len(line) > max_line_bytes:
                    continue
                try:
                    frame = json.loads(line.decode("utf-8"))
                    state = normalize_sensor_frame(
                        frame,
                        device=device,
                        baud_rate=baud_rate,
                        door_inverted=door_inverted,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    continue
                write_json_atomic(state_path, state)
                if once:
                    print(json.dumps(read_sensor_state(state_path), ensure_ascii=False, indent=2, sort_keys=True))
                    return
    finally:
        os.close(fd)


def run_reader(args):
    while True:
        device = None
        try:
            device = resolve_device(args.device)
            read_device(
                device,
                args.baud_rate,
                args.state_path,
                bool(args.door_inverted),
                args.once,
                args.read_timeout_seconds,
                args.max_line_bytes,
            )
            if args.once:
                return 0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            publish_error(args.state_path, exc, device=device)
            if args.once:
                print(str(exc), file=sys.stderr)
                return 1
            time.sleep(max(1, args.retry_seconds))


def parse_args(argv):
    root = Path(env("SMART_FRIDGE_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser()
    parser = argparse.ArgumentParser(description="Read and normalize ESP32-S3 smart-fridge sensor frames.")
    parser.add_argument("--device", default=env("SMART_FRIDGE_SENSOR_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--baud-rate", type=int, default=env_int("SMART_FRIDGE_SENSOR_BAUD_RATE", DEFAULT_BAUD_RATE))
    parser.add_argument("--state-path", default=env("SMART_FRIDGE_SENSOR_STATE_PATH", str(root / "data" / "sensor_state.json")))
    parser.add_argument("--stale-seconds", type=int, default=env_int("SMART_FRIDGE_SENSOR_STALE_SECONDS", DEFAULT_STALE_SECONDS))
    parser.add_argument("--retry-seconds", type=int, default=env_int("SMART_FRIDGE_SENSOR_RETRY_SECONDS", DEFAULT_RETRY_SECONDS))
    parser.add_argument(
        "--read-timeout-seconds",
        type=int,
        default=env_int("SMART_FRIDGE_SENSOR_READ_TIMEOUT_SECONDS", DEFAULT_READ_TIMEOUT_SECONDS),
    )
    parser.add_argument("--max-line-bytes", type=int, default=env_int("SMART_FRIDGE_SENSOR_MAX_LINE_BYTES", DEFAULT_MAX_LINE_BYTES))
    parser.add_argument(
        "--door-inverted",
        type=int,
        choices=(0, 1),
        default=1 if env_bool("SMART_FRIDGE_SENSOR_DOOR_INVERTED", True) else 0,
        help="Set to 1 when reported open/closed values are physically reversed.",
    )
    parser.add_argument("--once", action="store_true", help="Exit after publishing one valid frame.")
    parser.add_argument("--check", action="store_true", help="Print the current state and exit non-zero if it is stale or unhealthy.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.check:
        state = read_sensor_state(args.state_path, args.stale_seconds)
        print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if state.get("available") and state.get("fresh") and state.get("ok") else 1
    return run_reader(args)


if __name__ == "__main__":
    raise SystemExit(main())
