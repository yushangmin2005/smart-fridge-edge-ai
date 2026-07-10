#!/usr/bin/env python3
"""Maintenance tasks for the smart-fridge runtime: cleanup and alert checks."""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fridge_sensor import read_sensor_state


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def env(name, default=None):
    return os.environ.get(name, default)


def env_int(name, default):
    value = env(name)
    if value in (None, ""):
        return default
    return int(value)


def env_float(name, default):
    value = env(name)
    if value in (None, ""):
        return default
    return float(value)


def read_json(path, default=None):
    target = Path(path).expanduser()
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, payload):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_json_line(path, payload):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def root_dir():
    return Path(env("SMART_FRIDGE_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser()


def list_files(directory, patterns):
    root = Path(directory).expanduser()
    if not root.exists():
        return []
    files = []
    for pattern in patterns:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)


def remove_file(path, dry_run):
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if not dry_run:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return {"path": str(path), "size": size}


def prune_by_count(directory, patterns, keep, dry_run):
    files = list_files(directory, patterns)
    deleted = []
    for path in files[max(0, keep) :]:
        item = remove_file(path, dry_run)
        if item:
            deleted.append(item)
    return {"directory": str(Path(directory).expanduser()), "kept": min(len(files), max(0, keep)), "deleted": deleted}


def truncate_log(path, max_bytes, dry_run):
    target = Path(path).expanduser()
    if max_bytes <= 0 or not target.exists() or not target.is_file():
        return {"path": str(target), "truncated": False}
    size = target.stat().st_size
    if size <= max_bytes:
        return {"path": str(target), "truncated": False, "size": size}
    if not dry_run:
        with target.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            tail = handle.read()
        target.write_bytes(tail)
    return {"path": str(target), "truncated": True, "before": size, "after": min(size, max_bytes)}


def cleanup(args):
    root = root_dir()
    tmp_dir = Path(env("SMART_FRIDGE_TMP_DIR", str(root / "tmp"))).expanduser()
    log_dir = root / "logs"
    dry_run = args.dry_run
    result = {
        "ok": True,
        "generated_at": utc_now(),
        "dry_run": dry_run,
        "cleanup": [],
        "logs": [],
    }
    result["cleanup"].append(
        prune_by_count(
            env("SMART_FRIDGE_CAPTURE_DIR", str(tmp_dir / "captures")),
            ["*.jpg", "*.jpeg", "*.png"],
            env_int("SMART_FRIDGE_CLEANUP_CAPTURE_KEEP", env_int("SMART_FRIDGE_CAPTURE_KEEP", 24)),
            dry_run,
        )
    )
    result["cleanup"].append(
        prune_by_count(
            env("SMART_FRIDGE_YOLO_OUTPUT_DIR", str(tmp_dir / "yolo")),
            ["*.json", "*.jpg", "*.jpeg", "*.png"],
            env_int("SMART_FRIDGE_CLEANUP_YOLO_KEEP", 96),
            dry_run,
        )
    )
    result["cleanup"].append(
        prune_by_count(
            env("SMART_FRIDGE_VLM_OUTPUT_DIR", str(tmp_dir / "vlm")),
            ["*.json", "*.txt"],
            env_int("SMART_FRIDGE_CLEANUP_VLM_KEEP", 96),
            dry_run,
        )
    )
    result["cleanup"].append(
        prune_by_count(
            env("SMART_FRIDGE_CROP_DIR", str(tmp_dir / "crops")),
            ["*.jpg", "*.jpeg", "*.png"],
            env_int("SMART_FRIDGE_CLEANUP_CROP_KEEP", 48),
            dry_run,
        )
    )
    result["cleanup"].append(
        prune_by_count(
            env("SMART_FRIDGE_PI_BOARD_CAPTURE_DIR", "/tmp/pi-board-tools/captures"),
            ["*.jpg", "*.jpeg", "*.png"],
            env_int("SMART_FRIDGE_CLEANUP_BOARD_CAPTURE_KEEP", 12),
            dry_run,
        )
    )
    max_log_bytes = env_int("SMART_FRIDGE_CLEANUP_LOG_MAX_BYTES", 5 * 1024 * 1024)
    for log_path in (
        log_dir / "fridge-pipeline.log",
        log_dir / "fridge-web.log",
        log_dir / "fridge-sensor.log",
        log_dir / "fridge-alerts.log",
        Path.home() / "vlm-inference" / "logs" / "vlm-server.log",
    ):
        result["logs"].append(truncate_log(log_path, max_log_bytes, dry_run))
    result["deleted_count"] = sum(len(item["deleted"]) for item in result["cleanup"])
    result["deleted_bytes"] = sum(file["size"] for item in result["cleanup"] for file in item["deleted"])
    return result


def add_alert(alerts, severity, alert_id, title, detail, data=None):
    item = {"severity": severity, "id": alert_id, "title": title, "detail": detail}
    if data is not None:
        item["data"] = data
    alerts.append(item)


def pid_running(pid_file):
    target = Path(pid_file).expanduser()
    if not target.exists():
        return {"running": False, "pid": None, "reason": "pid_file_missing"}
    try:
        pid = int(target.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return {"running": False, "pid": None, "reason": "invalid_pid_file"}
    try:
        os.kill(pid, 0)
        return {"running": True, "pid": pid}
    except OSError:
        return {"running": False, "pid": pid, "reason": "process_not_running"}


def latest_file_age_minutes(directory, patterns):
    files = list_files(directory, patterns)
    if not files:
        return None, None
    latest = files[0]
    modified = datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc)
    age = (datetime.now(timezone.utc) - modified).total_seconds() / 60.0
    return latest, age


def http_check(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read(256)
        return {"ok": True, "status": response.status, "sample": body.decode("utf-8", errors="replace")}


def command_available(command):
    return shutil.which(command) is not None


def db_health(db_path):
    path = Path(db_path).expanduser()
    if not path.exists():
        return {"ok": False, "error": "db_missing", "path": str(path)}
    conn = sqlite3.connect(str(path))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for table in ("foods", "food_observations", "food_events"):
            counts[table] = conn.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]
        return {"ok": integrity == "ok", "integrity_check": integrity, "counts": counts}
    finally:
        conn.close()


def monitor(args):
    root = root_dir()
    tmp_dir = Path(env("SMART_FRIDGE_TMP_DIR", str(root / "tmp"))).expanduser()
    alerts = []
    checks = {}

    disk = shutil.disk_usage(str(root))
    free_mb = disk.free / (1024 * 1024)
    used_percent = 100.0 * (disk.used / disk.total) if disk.total else 0.0
    checks["disk"] = {"total_mb": round(disk.total / (1024 * 1024), 1), "free_mb": round(free_mb, 1), "used_percent": round(used_percent, 1)}
    min_free_mb = env_float("SMART_FRIDGE_DISK_MIN_FREE_MB", 512)
    max_used_percent = env_float("SMART_FRIDGE_DISK_MAX_USED_PERCENT", 90)
    if free_mb < min_free_mb or used_percent > max_used_percent:
        severity = "critical" if free_mb < min_free_mb / 2 or used_percent > 95 else "warning"
        add_alert(alerts, severity, "disk_space", "磁盘空间偏低", "剩余 {0:.0f} MB，使用率 {1:.1f}%".format(free_mb, used_percent), checks["disk"])

    pid_files = {
        "pipeline": env("SMART_FRIDGE_PIPELINE_PID", str(root / "run" / "fridge-pipeline.pid")),
        "web": env("SMART_FRIDGE_WEB_PID", str(root / "run" / "fridge-web.pid")),
        "vlm": env("SMART_FRIDGE_VLM_PID", str(Path.home() / "vlm-inference" / "run" / "vlm.pid")),
        "sensor": env("SMART_FRIDGE_SENSOR_PID", str(root / "run" / "fridge-sensor.pid")),
    }
    checks["services"] = {name: pid_running(path) for name, path in pid_files.items()}
    for name, status in checks["services"].items():
        if not status["running"]:
            add_alert(alerts, "critical", "{0}_stopped".format(name), "{0} 服务未运行".format(name), status.get("reason", "not running"), status)

    capture_dir = env("SMART_FRIDGE_CAPTURE_DIR", str(tmp_dir / "captures"))
    latest_capture, capture_age = latest_file_age_minutes(capture_dir, ["*.jpg", "*.jpeg", "*.png"])
    stale_minutes = env_float("SMART_FRIDGE_PIPELINE_STALE_MINUTES", 90)
    checks["latest_capture"] = {
        "path": str(latest_capture) if latest_capture else None,
        "age_minutes": round(capture_age, 1) if capture_age is not None else None,
    }
    if latest_capture is None:
        add_alert(alerts, "warning", "capture_missing", "还没有拍照结果", "未找到最近照片")
    elif capture_age is not None and capture_age > stale_minutes:
        add_alert(alerts, "warning", "capture_stale", "自动识别可能停滞", "最近照片已经 {0:.1f} 分钟未更新".format(capture_age), checks["latest_capture"])

    sensor_state_path = env("SMART_FRIDGE_SENSOR_STATE_PATH", str(root / "data" / "sensor_state.json"))
    checks["sensor"] = read_sensor_state(
        sensor_state_path,
        env_int("SMART_FRIDGE_SENSOR_STALE_SECONDS", 10),
    )
    if not checks["sensor"].get("available"):
        add_alert(alerts, "warning", "sensor_missing", "环境数据尚未接入", "没有收到 ESP32-S3 传感器数据", checks["sensor"])
    elif not checks["sensor"].get("fresh"):
        add_alert(
            alerts,
            "warning",
            "sensor_stale",
            "环境数据已经中断",
            "最近传感器数据距今 {0} 秒".format(checks["sensor"].get("age_seconds")),
            checks["sensor"],
        )
    elif not checks["sensor"].get("ok"):
        add_alert(alerts, "warning", "sensor_health", "部分环境传感器异常", "请检查温湿度、温度探头和门磁", checks["sensor"])

    db_path = env("SMART_FRIDGE_DB_PATH", str(root / "data" / "fridge.sqlite3"))
    checks["db"] = db_health(db_path)
    if not checks["db"].get("ok"):
        add_alert(alerts, "critical", "db_health", "数据库异常", checks["db"].get("error") or checks["db"].get("integrity_check") or "unknown", checks["db"])

    web_url = "http://127.0.0.1:{0}/api/overview".format(env_int("SMART_FRIDGE_WEB_PORT", 8090))
    try:
        checks["web_api"] = http_check(web_url, env_int("SMART_FRIDGE_MONITOR_HTTP_TIMEOUT", 5))
    except Exception as exc:
        checks["web_api"] = {"ok": False, "error": str(exc)}
        add_alert(alerts, "critical", "web_api", "Web 状态接口不可用", str(exc))

    vlm_url = env("SMART_FRIDGE_VLM_MODELS_URL", "http://127.0.0.1:8080/v1/models")
    try:
        checks["vlm_api"] = http_check(vlm_url, env_int("SMART_FRIDGE_MONITOR_HTTP_TIMEOUT", 5))
    except Exception as exc:
        checks["vlm_api"] = {"ok": False, "error": str(exc)}
        add_alert(alerts, "warning", "vlm_api", "主识别服务接口不可用", str(exc))

    tool_commands = ["gpioinfo", "gpioget", "gpioset", "i2cdetect", "i2cget", "i2cset"]
    checks["board_tools"] = {name: command_available(name) for name in tool_commands}
    missing_tools = [name for name, ok in checks["board_tools"].items() if not ok]
    if missing_tools:
        add_alert(alerts, "warning", "board_tools_missing", "GPIO/I2C 命令缺失", "缺少: " + ", ".join(missing_tools), checks["board_tools"])

    result = {
        "ok": not any(item["severity"] == "critical" for item in alerts),
        "generated_at": utc_now(),
        "alert_count": len(alerts),
        "alerts": alerts,
        "checks": checks,
    }
    alerts_path = env("SMART_FRIDGE_ALERTS_PATH", str(root / "data" / "alerts.json"))
    alert_log = env("SMART_FRIDGE_ALERT_LOG", str(root / "logs" / "fridge-alerts.log"))
    write_json(alerts_path, result)
    append_json_line(alert_log, result)
    return result


def print_result(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Run smart-fridge cleanup and monitoring tasks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cleanup_parser = subparsers.add_parser("cleanup", help="Prune temporary images, JSON outputs, and oversized logs.")
    cleanup_parser.add_argument("--dry-run", action="store_true")
    cleanup_parser.set_defaults(func=cleanup)

    monitor_parser = subparsers.add_parser("monitor", help="Write alert status for disk, services, database, and APIs.")
    monitor_parser.set_defaults(func=monitor)

    run_all_parser = subparsers.add_parser("run-all", help="Run cleanup then monitor.")
    run_all_parser.add_argument("--dry-run", action="store_true")
    run_all_parser.set_defaults(func=lambda args: {"cleanup": cleanup(args), "monitor": monitor(args)})

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    result = args.func(args)
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
