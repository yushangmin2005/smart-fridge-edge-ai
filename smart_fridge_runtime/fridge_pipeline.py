#!/usr/bin/env python3
"""End-to-end smart-fridge pipeline: capture, YOLO diff, VLM crop analysis, SQLite write."""

import argparse
import base64
import json
import math
import mimetypes
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fridge_sensor import read_sensor_state, sensor_ai_context


DEFAULT_PROMPT = """Return exactly one JSON object for the visible refrigerator crop.
Required keys: is_food, food_name, category, composition, freshness, freshness_score,
visible_state, storage_advice, risk_level, confidence, notes.
YOLO only located a possible scene change. Its label is not identity evidence."""

CLOUD_ADVICE_PROMPT = """你是智能冰箱的云端综合建议模型。你会收到当前仍然活跃的食物对象、
本轮新增/移除/未变信息、基础运行状态和 ESP32-S3 环境传感器快照。请综合温度、湿度、
门状态和数据时效性进行判断；探头温度标记为估算值时只能作为趋势参考。门状态已经纠正为
实际物理状态，不要再次取反。请只输出一个 JSON 对象，不要输出 Markdown。
字段要求：
- summary: 一句话总结当前冰箱状态。
- risk_level: normal、attention、danger 或 unknown。
- action_items: 字符串数组，给出 1 到 5 条可执行建议。
- item_suggestions: 数组，每项包含 food_id、name、suggestion、priority。
- next_check: 一句话说明下次应重点观察什么。
如果没有活跃对象，也要给出简短说明。"""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def env_bool(name, default=False):
    value = env(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def read_json(path, default=None):
    if not path or not Path(path).exists():
        return default
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(text), encoding="utf-8")


def run_command(command, timeout=None, capture_output=True):
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or "no output"
        raise RuntimeError("command failed ({0}): {1}".format(result.returncode, detail))
    return result


def detect_camera_device(configured):
    if configured and configured != "auto":
        return configured

    candidates = []
    try:
        output = run_command(["v4l2-ctl", "--list-devices"], timeout=8).stdout or ""
        current_name = ""
        for line in output.splitlines():
            if line.strip() and not line.startswith((" ", "\t")):
                current_name = line.strip().lower()
                continue
            device = line.strip()
            if not device.startswith("/dev/video"):
                continue
            score = 50
            if "web camera" in current_name or "usb" in current_name:
                score = 0
            elif "camera" in current_name:
                score = 10
            candidates.append((score, device))
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass

    for fallback in ("/dev/video-camera0", "/dev/video10", "/dev/video0"):
        if Path(fallback).exists():
            candidates.append((100, fallback))

    if not candidates:
        raise RuntimeError("no video capture device found")
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def capture_image(output_path):
    device = detect_camera_device(env("SMART_FRIDGE_CAMERA_DEVICE", "auto"))
    width = env_int("SMART_FRIDGE_CAPTURE_WIDTH", 640)
    height = env_int("SMART_FRIDGE_CAPTURE_HEIGHT", 360)
    input_format = env("SMART_FRIDGE_CAPTURE_FORMAT", "mjpeg")
    timeout = env_int("SMART_FRIDGE_CAPTURE_TIMEOUT", 25)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    attempts = [
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "v4l2",
            "-input_format",
            input_format,
            "-video_size",
            "{0}x{1}".format(width, height),
            "-i",
            device,
            "-frames:v",
            "1",
            str(output),
        ],
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "v4l2",
            "-video_size",
            "{0}x{1}".format(width, height),
            "-i",
            device,
            "-frames:v",
            "1",
            str(output),
        ],
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "v4l2",
            "-i",
            device,
            "-frames:v",
            "1",
            str(output),
        ],
    ]

    errors = []
    for command in attempts:
        try:
            run_command(command, timeout=timeout)
            if output.exists() and output.stat().st_size > 0:
                return str(output), device
            errors.append("empty output from ffmpeg")
        except Exception as exc:  # capture fallback intentionally broad
            errors.append(str(exc))
    raise RuntimeError("camera capture failed on {0}: {1}".format(device, " | ".join(errors[-3:])))


def prune_files(directory, patterns, keep):
    if keep <= 0:
        return []
    root = Path(directory)
    if not root.exists():
        return []
    files = []
    for pattern in patterns:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    files = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)
    deleted = []
    for path in files[keep:]:
        try:
            path.unlink()
            deleted.append(str(path))
        except FileNotFoundError:
            pass
    return deleted


def box_iou(left, right):
    lx1, ly1, lx2, ly2 = [float(left[key]) for key in ("x1", "y1", "x2", "y2")]
    rx1, ry1, rx2, ry2 = [float(right[key]) for key in ("x1", "y1", "x2", "y2")]
    ix1, iy1 = max(lx1, rx1), max(ly1, ry1)
    ix2, iy2 = min(lx2, rx2), min(ly2, ry2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - inter
    if union <= 0:
        return 0.0
    return inter / union


def detection_crop_box(detection, width, height, pad_ratio=0.0):
    box = detection.get("box") or {}
    x1 = float(box.get("x1", 0))
    y1 = float(box.get("y1", 0))
    x2 = float(box.get("x2", width))
    y2 = float(box.get("y2", height))
    pad_x = max(0.0, x2 - x1) * pad_ratio
    pad_y = max(0.0, y2 - y1) * pad_ratio
    crop_box = (
        max(0, int(math.floor(x1 - pad_x))),
        max(0, int(math.floor(y1 - pad_y))),
        min(width, int(math.ceil(x2 + pad_x))),
        min(height, int(math.ceil(y2 + pad_y))),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return None
    return crop_box


def image_difference_hash(image):
    from PIL import Image

    resampling = getattr(Image, "Resampling", Image)
    resized = image.convert("L").resize((9, 8), resampling.BILINEAR)
    if hasattr(resized, "get_flattened_data"):
        pixels = list(resized.get_flattened_data())
    else:
        pixels = list(resized.getdata())
    value = 0
    for row in range(8):
        row_offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[row_offset + column] > pixels[row_offset + column + 1]
            )
    return "{0:016x}".format(value)


def add_change_fingerprints(image_path, detections):
    from PIL import Image

    with Image.open(image_path) as source:
        image = source.convert("RGB")
        width, height = image.size
        enriched = []
        for detection in detections:
            item = dict(detection)
            crop_box = detection_crop_box(item, width, height)
            if crop_box is not None:
                item["change_fingerprint"] = image_difference_hash(image.crop(crop_box))
            enriched.append(item)
    return enriched


def fingerprint_distance(left, right):
    left_value = (left or {}).get("change_fingerprint")
    right_value = (right or {}).get("change_fingerprint")
    if not left_value or not right_value:
        return None
    try:
        return bin(int(left_value, 16) ^ int(right_value, 16)).count("1")
    except (TypeError, ValueError):
        return None


def match_detections(previous_objects, detections, threshold, max_hash_distance=16):
    matches = {}
    used_previous = set()
    for index, detection in enumerate(detections):
        box = detection.get("box") or {}
        best_prev = None
        best_score = 0.0
        best_hash_distance = None
        for prev_index, previous in enumerate(previous_objects):
            if prev_index in used_previous:
                continue
            previous_box = previous.get("box") or {}
            score = box_iou(box, previous_box)
            if score < threshold:
                continue
            hash_distance = fingerprint_distance(previous, detection)
            if hash_distance is not None and hash_distance > max_hash_distance:
                continue
            if score > best_score:
                best_score = score
                best_prev = prev_index
                best_hash_distance = hash_distance
        if best_prev is not None:
            matches[index] = (best_prev, best_score, best_hash_distance)
            used_previous.add(best_prev)
    removed = [index for index in range(len(previous_objects)) if index not in used_previous]
    added = [index for index in range(len(detections)) if index not in matches]
    return matches, added, removed


def select_yolo_change_candidates(payload):
    result = dict(payload or {})
    raw_detections = result.get("raw_detections")
    if raw_detections is None:
        raw_detections = result.get("detections") or []
    legacy_threshold = env_float("SMART_FRIDGE_YOLO_MIN_CONFIDENCE", 0.45)
    min_confidence = env_float(
        "SMART_FRIDGE_YOLO_CHANGE_MIN_CONFIDENCE", legacy_threshold
    )
    candidates = []
    for detection in raw_detections:
        try:
            confidence = float(detection.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence >= min_confidence:
            candidates.append(detection)
    result.update(
        {
            "raw_detections": raw_detections,
            "detections": candidates,
            "pipeline_role": "change_candidates",
            "pipeline_semantic_authority": "vlm",
            "pipeline_change_min_confidence": min_confidence,
            "pipeline_candidate_count": len(candidates),
            "pipeline_filtered_count": len(raw_detections) - len(candidates),
        }
    )
    return result, candidates


def run_yolo(image_path, output_json_path):
    mock_yolo = env("SMART_FRIDGE_YOLO_MOCK_JSON")
    if mock_yolo:
        if mock_yolo.strip().startswith("{"):
            payload = json.loads(mock_yolo)
        else:
            payload = read_json(mock_yolo, {})
        payload = dict(payload or {})
        payload.setdefault("image", image_path)
    else:
        yolo_bin = shlex.split(
            env(
                "SMART_FRIDGE_YOLO_BIN",
                "/home/pi/yolo-inference/bin/yolo_detect.sh",
            )
        )
        command = yolo_bin + [
            "--image",
            image_path,
            "--output-json",
            output_json_path,
        ]
        run_command(command, timeout=env_int("SMART_FRIDGE_YOLO_TIMEOUT", 300))
        payload = read_json(output_json_path, {})

    payload, detections = select_yolo_change_candidates(payload)
    write_json(output_json_path, payload)
    return payload, detections


def crop_detection(image_path, detection, crop_path):
    from PIL import Image

    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    pad_ratio = env_float("SMART_FRIDGE_CROP_PADDING", 0.08)
    crop_box = detection_crop_box(detection, width, height, pad_ratio)
    if crop_box is None:
        raise ValueError("YOLO change candidate has an empty crop")
    crop = image.crop(crop_box)
    target = Path(crop_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    crop.save(target, format="JPEG", quality=92)
    return str(target), {"x1": crop_box[0], "y1": crop_box[1], "x2": crop_box[2], "y2": crop_box[3]}


def load_prompt():
    prompt_path = env("SMART_FRIDGE_VLM_PROMPT_PATH")
    if prompt_path and Path(prompt_path).exists():
        return Path(prompt_path).read_text(encoding="utf-8")
    local_prompt = Path(__file__).with_name("vlm_food_prompt.txt")
    if local_prompt.exists():
        return local_prompt.read_text(encoding="utf-8")
    return DEFAULT_PROMPT


def data_url_for_image(image_path):
    data = Path(image_path).read_bytes()
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return "data:{0};base64,{1}".format(mime, encoded)


def models_url_from_chat_url(chat_url):
    if chat_url.endswith("/v1/chat/completions"):
        return chat_url[: -len("/chat/completions")] + "/models"
    return chat_url.rstrip("/") + "/models"


def resolve_vlm_model(chat_url):
    configured = env("SMART_FRIDGE_VLM_MODEL")
    if configured:
        return configured
    try:
        request = urllib.request.Request(models_url_from_chat_url(chat_url), method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") or payload.get("models") or []
        if data:
            first = data[0]
            return first.get("id") or first.get("name") or "local-model"
    except Exception:
        pass
    return "local-model"


def extract_json_object(text):
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start < 0:
        raise ValueError("VLM response did not contain a JSON object")
    depth = 0
    in_string = False
    escape = False
    for pos in range(start, len(stripped)):
        char = stripped[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stripped[start : pos + 1])
    raise ValueError("VLM response JSON object was incomplete")


def extract_chat_content(response_payload):
    message = response_payload["choices"][0]["message"]["content"]
    if isinstance(message, list):
        return "\n".join(part.get("text", "") for part in message if isinstance(part, dict))
    return str(message)


def load_deepseek_api_key():
    for name in ("SMART_FRIDGE_CLOUD_ADVICE_API_KEY", "DEEPSEEK_API_KEY"):
        value = env(name)
        if value:
            return value
    auth_path = Path(env("SMART_FRIDGE_CLOUD_ADVICE_AUTH_PATH", str(Path.home() / ".pi" / "agent" / "auth.json"))).expanduser()
    try:
        auth = read_json(auth_path, {}) or {}
    except Exception:
        return ""
    credential = auth.get("deepseek") or {}
    if credential.get("type") == "api_key":
        return credential.get("key") or ""
    return ""


def compact_active_object(item):
    vlm = item.get("vlm") or {}
    return {
        "food_id": item.get("food_id"),
        "yolo_label": item.get("yolo_label"),
        "confidence": item.get("confidence"),
        "first_seen_at": item.get("first_seen_at"),
        "last_seen_at": item.get("last_seen_at"),
        "food_name": vlm.get("food_name"),
        "category": vlm.get("category"),
        "composition": vlm.get("composition") or [],
        "freshness": vlm.get("freshness"),
        "freshness_score": vlm.get("freshness_score"),
        "visible_state": vlm.get("visible_state"),
        "storage_advice": vlm.get("storage_advice"),
        "risk_level": vlm.get("risk_level"),
        "notes": vlm.get("notes"),
    }


def load_sensor_context():
    root = Path(env("SMART_FRIDGE_ROOT", str(Path(__file__).resolve().parents[1])))
    state_path = env("SMART_FRIDGE_SENSOR_STATE_PATH", str(root / "data" / "sensor_state.json"))
    stale_seconds = env_int("SMART_FRIDGE_SENSOR_STALE_SECONDS", 10)
    return sensor_ai_context(read_sensor_state(state_path, stale_seconds))


def build_cloud_advice_user_payload(active_objects, cycle_summary, sensor_context):
    return {
        "captured_at": cycle_summary.get("captured_at"),
        "completed_at": cycle_summary.get("completed_at"),
        "next_scheduled_at": cycle_summary.get("next_scheduled_at"),
        "detections": cycle_summary.get("detections"),
        "active_count": cycle_summary.get("active_count"),
        "added": cycle_summary.get("added") or [],
        "unchanged": cycle_summary.get("unchanged") or [],
        "removed": cycle_summary.get("removed") or [],
        "active_objects": [compact_active_object(item) for item in active_objects],
        "sensor_snapshot": sensor_context or load_sensor_context(),
    }


def normalize_cloud_advice(payload):
    result = dict(payload or {})
    result.setdefault("summary", "暂无云端建议。")
    result.setdefault("risk_level", "unknown")
    result.setdefault("action_items", [])
    result.setdefault("item_suggestions", [])
    result.setdefault("next_check", "")
    if not isinstance(result.get("action_items"), list):
        result["action_items"] = [str(result["action_items"])]
    if not isinstance(result.get("item_suggestions"), list):
        result["item_suggestions"] = []
    normalized_items = []
    for item in result["item_suggestions"]:
        if not isinstance(item, dict):
            continue
        normalized_items.append(
            {
                "food_id": item.get("food_id"),
                "name": item.get("name") or item.get("food_name") or "",
                "suggestion": item.get("suggestion") or "",
                "priority": item.get("priority") or "normal",
            }
        )
    result["item_suggestions"] = normalized_items
    return result


def load_mock_cloud_advice(mock_value):
    if not mock_value:
        return None
    if mock_value.strip().startswith("{"):
        return normalize_cloud_advice(json.loads(mock_value))
    path = Path(mock_value)
    if path.exists():
        return normalize_cloud_advice(read_json(path, {}))
    raise RuntimeError("SMART_FRIDGE_CLOUD_ADVICE_MOCK_JSON does not point to a JSON object or file")


def request_cloud_advice(active_objects, cycle_summary, sensor_context=None):
    generated_at = utc_now()
    model = env("SMART_FRIDGE_CLOUD_ADVICE_MODEL", "deepseek-v4-flash")
    provider = env("SMART_FRIDGE_CLOUD_ADVICE_PROVIDER", "deepseek")
    base = {
        "ok": False,
        "provider": provider,
        "model": model,
        "generated_at": generated_at,
        "active_object_count": len(active_objects),
    }
    if not env_bool("SMART_FRIDGE_CLOUD_ADVICE_ENABLED", True):
        return dict(base, skipped=True, reason="disabled")

    mock = load_mock_cloud_advice(env("SMART_FRIDGE_CLOUD_ADVICE_MOCK_JSON"))
    if mock is not None:
        result = dict(base)
        result.update(mock)
        result["ok"] = True
        result["mock"] = True
        return result

    api_key = load_deepseek_api_key()
    if not api_key:
        return dict(base, skipped=True, reason="missing_deepseek_api_key")

    user_payload = build_cloud_advice_user_payload(active_objects, cycle_summary, sensor_context)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLOUD_ADVICE_PROMPT},
            {
                "role": "user",
                "content": "请根据以下智能冰箱活跃对象给出综合建议：\n"
                + json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "temperature": env_float("SMART_FRIDGE_CLOUD_ADVICE_TEMPERATURE", 0.2),
        "max_tokens": env_int("SMART_FRIDGE_CLOUD_ADVICE_MAX_TOKENS", 600),
    }
    if env_bool("SMART_FRIDGE_CLOUD_ADVICE_USE_RESPONSE_FORMAT", True):
        payload["response_format"] = {"type": "json_object"}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        env("SMART_FRIDGE_CLOUD_ADVICE_URL", "https://api.deepseek.com/chat/completions"),
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer {0}".format(api_key)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=env_int("SMART_FRIDGE_CLOUD_ADVICE_TIMEOUT", 120)) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
        content = extract_chat_content(response_payload)
        advice = normalize_cloud_advice(extract_json_object(content))
        result = dict(base)
        result.update(advice)
        result["ok"] = True
        return result
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return dict(base, error="HTTP {0}: {1}".format(exc.code, detail[:500]))
    except Exception as exc:
        return dict(base, error=str(exc))


def normalize_vlm_result(payload):
    result = dict(payload or {})
    result.setdefault("is_food", True)
    result.setdefault("food_name", result.get("name") or "unknown_food")
    result.setdefault("category", "unknown")
    result.setdefault("composition", [])
    result.setdefault("freshness", result.get("state") or "unknown")
    result.setdefault("freshness_score", 0.0)
    result.setdefault("visible_state", result.get("description") or "")
    result.setdefault("storage_advice", "")
    result.setdefault("risk_level", result.get("advice_label") or "unknown")
    result.setdefault("confidence", 0.0)
    result.setdefault("notes", "")
    if not isinstance(result.get("composition"), list):
        result["composition"] = [str(result["composition"])]
    for key in ("freshness_score", "confidence"):
        try:
            result[key] = max(0.0, min(1.0, float(result[key])))
        except (TypeError, ValueError):
            result[key] = 0.0
    return result


def load_mock_vlm_result(mock_value):
    if not mock_value:
        return None
    if mock_value.strip().startswith("{"):
        return normalize_vlm_result(json.loads(mock_value))
    path = Path(mock_value)
    if path.exists():
        return normalize_vlm_result(read_json(path, {}))
    raise RuntimeError("SMART_FRIDGE_VLM_MOCK_JSON does not point to a JSON object or file")


def build_vlm_user_text(yolo_detection, sensor_context):
    yolo_hint = {
        "yolo_label": yolo_detection.get("class_name"),
        "yolo_confidence": yolo_detection.get("confidence"),
        "yolo_box": yolo_detection.get("box"),
    }
    return (
        "Analyze this cropped refrigerator image. YOLO only located a possible "
        "scene change; its class label and confidence are routing metadata, not "
        "food identity evidence. Decide whether this is food and identify it "
        "independently from the pixels. YOLO change candidate:\n"
        + json.dumps(yolo_hint, ensure_ascii=False)
        + "\nRefrigerator sensor snapshot (door_state and door_open are corrected physical values):\n"
        + json.dumps(sensor_context or load_sensor_context(), ensure_ascii=False)
        + "\nUse the sensor values as freshness and storage context, but never infer food identity from sensors alone. "
        + "reported_door_open_count is the device-reported counter before physical-state correction. "
        + "Return only the required JSON object."
    )


def call_vlm(crop_path, yolo_detection, raw_response_path=None, raw_text_path=None, sensor_context=None):
    mock = load_mock_vlm_result(env("SMART_FRIDGE_VLM_MOCK_JSON"))
    if mock is not None:
        if raw_response_path:
            write_json(raw_response_path, {"mock": True, "result": mock})
        if raw_text_path:
            write_text(raw_text_path, json.dumps(mock, ensure_ascii=False, separators=(",", ":")))
        return mock

    chat_url = env("SMART_FRIDGE_VLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
    timeout = env_int("SMART_FRIDGE_VLM_TIMEOUT", 3600)
    model = resolve_vlm_model(chat_url)
    prompt = load_prompt()
    user_text = build_vlm_user_text(yolo_detection, sensor_context)
    base_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url_for_image(crop_path)}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": env_int("SMART_FRIDGE_VLM_MAX_TOKENS", 512),
    }
    if env_bool("SMART_FRIDGE_VLM_USE_RESPONSE_FORMAT", False):
        attempts = [dict(base_payload, response_format={"type": "json_object"}), base_payload]
    else:
        attempts = [base_payload]
    last_error = None
    for payload in attempts:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            chat_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            message = extract_chat_content(response_payload)
            if raw_response_path:
                write_json(raw_response_path, response_payload)
            if raw_text_path:
                write_text(raw_text_path, message)
            return normalize_vlm_result(extract_json_object(str(message)))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = "HTTP {0}: {1}".format(exc.code, detail[:500])
            if exc.code in (400, 422):
                continue
        except Exception as exc:
            last_error = str(exc)
            break
    raise RuntimeError("VLM request failed: {0}".format(last_error))


def fallback_vlm_result(detection, error):
    routing_label = detection.get("class_name") or "unknown"
    return {
        "is_food": True,
        "food_name": "unknown_food",
        "category": "unknown",
        "composition": [],
        "freshness": "unknown",
        "freshness_score": 0.0,
        "visible_state": "VLM 分析失败，当前候选尚未完成内容识别。",
        "storage_advice": "需要重新拍照或人工确认。",
        "risk_level": "unknown",
        "confidence": 0.0,
        "identification_status": "pending_vlm",
        "notes": "VLM error: {0}; YOLO routing label: {1}".format(error, routing_label),
    }


def db_command(args):
    db_bin = shlex.split(env("SMART_FRIDGE_DB_BIN", "/home/pi/smart-fridge/bin/fridge_db.sh"))
    result = run_command(db_bin + args, timeout=env_int("SMART_FRIDGE_DB_TIMEOUT", 60))
    text = result.stdout.strip() if result.stdout else "{}"
    return json.loads(text)


def ingest_added_detection(image_path, yolo_json_path, detection_index, detection, crop_path, vlm_json_path, vlm_result):
    write_json(vlm_json_path, vlm_result)
    command = [
        "ingest",
        "--source",
        "smart-fridge-pipeline",
        "--force-new-food",
        "--image-ref",
        image_path,
        "--yolo-json",
        yolo_json_path,
        "--yolo-detection-index",
        str(detection_index),
        "--vlm-json",
        vlm_json_path,
        "--canonical-name",
        str(vlm_result.get("food_name") or "unknown_food"),
        "--vlm-name",
        str(vlm_result.get("food_name") or ""),
        "--vlm-state",
        str(vlm_result.get("freshness") or "unknown"),
        "--vlm-confidence",
        str(vlm_result.get("confidence") if vlm_result.get("confidence") is not None else 0.0),
        "--vlm-description",
        str(vlm_result.get("visible_state") or vlm_result.get("notes") or ""),
        "--advice-label",
        str(vlm_result.get("risk_level") or "unknown"),
    ]
    storage_location = env("SMART_FRIDGE_STORAGE_LOCATION")
    if storage_location:
        command.extend(["--storage-location", storage_location])
    payload = db_command(command)
    payload["crop_ref"] = crop_path
    payload["vlm_json"] = vlm_json_path
    return payload


def mark_removed(previous_object, captured_at):
    food_id = previous_object.get("food_id")
    if not food_id:
        return {"ok": False, "reason": "missing_food_id"}
    payload = {
        "last_yolo_label": previous_object.get("yolo_label"),
        "last_image_ref": previous_object.get("image_ref"),
        "removed_at": captured_at,
    }
    return db_command(
        [
            "add-event",
            "--food-id",
            food_id,
            "--event-type",
            "food.removed",
            "--event-at",
            captured_at,
            "--source",
            "smart-fridge-pipeline",
            "--payload-json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def build_paths(root, timestamp):
    tmp_dir = Path(env("SMART_FRIDGE_TMP_DIR", str(Path(root) / "tmp")))
    capture_dir = Path(env("SMART_FRIDGE_CAPTURE_DIR", str(tmp_dir / "captures")))
    crop_dir = Path(env("SMART_FRIDGE_CROP_DIR", str(tmp_dir / "crops")))
    yolo_dir = Path(env("SMART_FRIDGE_YOLO_OUTPUT_DIR", str(tmp_dir / "yolo")))
    vlm_dir = Path(env("SMART_FRIDGE_VLM_OUTPUT_DIR", str(tmp_dir / "vlm")))
    for directory in (capture_dir, crop_dir, yolo_dir, vlm_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "capture": capture_dir / "{0}.jpg".format(timestamp),
        "crop_dir": crop_dir,
        "yolo_json": yolo_dir / "{0}.json".format(timestamp),
        "vlm_dir": vlm_dir,
    }


def suppress_known_rejections(
    previous_rejections,
    detections,
    candidate_indexes,
    iou_threshold,
    max_hash_distance,
    now,
    image_path,
):
    candidates = [detections[index] for index in candidate_indexes]
    matches, new_candidate_indexes, _ = match_detections(
        previous_rejections,
        candidates,
        iou_threshold,
        max_hash_distance,
    )
    current_rejections = []
    suppressed = []
    for candidate_index, (
        previous_index,
        score,
        hash_distance,
    ) in sorted(matches.items()):
        detection_index = candidate_indexes[candidate_index]
        detection = detections[detection_index]
        previous = dict(previous_rejections[previous_index])
        previous.update(
            {
                "yolo_label": detection.get("class_name"),
                "confidence": detection.get("confidence"),
                "box": detection.get("box"),
                "change_fingerprint": detection.get("change_fingerprint"),
                "last_seen_at": now,
                "image_ref": image_path,
                "match_iou": round(score, 6),
                "fingerprint_distance": hash_distance,
            }
        )
        current_rejections.append(previous)
        suppressed.append(
            {
                "detection_index": detection_index,
                "reason": "known_vlm_non_food",
                "yolo_label": detection.get("class_name"),
                "match_iou": round(score, 6),
                "fingerprint_distance": hash_distance,
            }
        )
    new_indexes = [
        candidate_indexes[candidate_index]
        for candidate_index in new_candidate_indexes
    ]
    return current_rejections, suppressed, new_indexes


def rejected_candidate_from_detection(
    detection,
    detection_index,
    now,
    image_path,
    vlm_json_path,
    vlm_response_path,
    vlm_raw_text_path,
):
    return {
        "detection_index": detection_index,
        "reason": "vlm_is_food_false",
        "yolo_label": detection.get("class_name"),
        "confidence": detection.get("confidence"),
        "box": detection.get("box"),
        "change_fingerprint": detection.get("change_fingerprint"),
        "first_seen_at": now,
        "last_seen_at": now,
        "image_ref": image_path,
        "vlm_json": str(vlm_json_path),
        "vlm_response_json": vlm_response_path,
        "vlm_raw_text": vlm_raw_text_path,
    }


def run_once(args):
    root = Path(env("SMART_FRIDGE_ROOT", str(Path(__file__).resolve().parents[1])))
    state_path = Path(env("SMART_FRIDGE_STATE_PATH", str(root / "data" / "pipeline_state.json")))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = build_paths(root, timestamp)

    if args.image:
        image_path = str(Path(args.image).expanduser().resolve())
        camera_device = "provided-image"
    else:
        image_path, camera_device = capture_image(paths["capture"])

    keep = env_int("SMART_FRIDGE_CAPTURE_KEEP", 24)
    deleted_captures = prune_files(Path(paths["capture"]).parent, ["*.jpg", "*.jpeg", "*.png"], keep)
    prune_files(paths["crop_dir"], ["*.jpg", "*.jpeg", "*.png"], keep * env_int("SMART_FRIDGE_MAX_CROPS_PER_IMAGE", 8))

    yolo_payload, detections = run_yolo(image_path, str(paths["yolo_json"]))
    detections = add_change_fingerprints(image_path, detections)
    yolo_payload["detections"] = detections
    write_json(paths["yolo_json"], yolo_payload)

    previous_state = read_json(state_path, {"active_objects": []}) or {"active_objects": []}
    previous_objects = previous_state.get("active_objects") or []
    previous_rejections = previous_state.get("rejected_candidates") or []
    match_iou = env_float("SMART_FRIDGE_MATCH_IOU", 0.35)
    max_hash_distance = env_int("SMART_FRIDGE_CHANGE_HASH_MAX_DISTANCE", 16)
    matches, added_indexes, removed_indexes = match_detections(
        previous_objects,
        detections,
        match_iou,
        max_hash_distance,
    )

    now = utc_now()
    current_rejections, suppressed, added_indexes = suppress_known_rejections(
        previous_rejections,
        detections,
        added_indexes,
        match_iou,
        max_hash_distance,
        now,
        image_path,
    )
    current_objects = []
    unchanged = []
    added = []
    removed = []
    errors = []

    for detection_index, (
        previous_index,
        score,
        hash_distance,
    ) in sorted(matches.items()):
        detection = detections[detection_index]
        previous = dict(previous_objects[previous_index])
        previous.update(
            {
                "yolo_label": detection.get("class_name"),
                "confidence": detection.get("confidence"),
                "box": detection.get("box"),
                "change_fingerprint": detection.get("change_fingerprint"),
                "last_seen_at": now,
                "image_ref": image_path,
                "match_iou": round(score, 6),
                "fingerprint_distance": hash_distance,
            }
        )
        current_objects.append(previous)
        unchanged.append(
            {
                "food_id": previous.get("food_id"),
                "food_name": (previous.get("vlm") or {}).get("food_name"),
                "yolo_label": previous.get("yolo_label"),
                "match_iou": round(score, 6),
                "fingerprint_distance": hash_distance,
            }
        )

    for detection_index in added_indexes:
        detection = detections[detection_index]
        crop_path = paths["crop_dir"] / "{0}_det{1}.jpg".format(timestamp, detection_index)
        vlm_json_path = paths["vlm_dir"] / "{0}_det{1}.json".format(timestamp, detection_index)
        vlm_response_path = "{0}.response.json".format(vlm_json_path)
        vlm_raw_text_path = "{0}.raw.txt".format(vlm_json_path)
        crop_path, crop_box = crop_detection(image_path, detection, crop_path)
        vlm_sensor_context = load_sensor_context()
        try:
            vlm_result = call_vlm(
                crop_path,
                detection,
                vlm_response_path,
                vlm_raw_text_path,
                sensor_context=vlm_sensor_context,
            )
        except Exception as exc:
            if env_bool("SMART_FRIDGE_WRITE_FALLBACK_ON_VLM_ERROR", False):
                vlm_result = fallback_vlm_result(detection, str(exc))
                write_json(vlm_json_path, vlm_result)
                errors.append(
                    {
                        "stage": "vlm",
                        "detection_index": detection_index,
                        "error": str(exc),
                        "fallback": True,
                        "vlm_json": str(vlm_json_path),
                        "vlm_response_json": vlm_response_path,
                        "vlm_raw_text": vlm_raw_text_path,
                    }
                )
            else:
                errors.append(
                    {
                        "stage": "vlm",
                        "detection_index": detection_index,
                        "error": str(exc),
                        "fallback": False,
                        "vlm_response_json": vlm_response_path,
                        "vlm_raw_text": vlm_raw_text_path,
                    }
                )
                continue
        write_json(vlm_json_path, vlm_result)
        if vlm_result.get("is_food") is False:
            current_rejections.append(
                rejected_candidate_from_detection(
                    detection,
                    detection_index,
                    now,
                    image_path,
                    vlm_json_path,
                    vlm_response_path,
                    vlm_raw_text_path,
                )
            )
            added.append(
                {
                    "detection_index": detection_index,
                    "skipped": True,
                    "reason": "vlm_is_food_false",
                    "yolo_label": detection.get("class_name"),
                    "vlm_json": str(vlm_json_path),
                    "vlm_response_json": vlm_response_path,
                    "vlm_raw_text": vlm_raw_text_path,
                }
            )
            continue
        db_result = ingest_added_detection(
            image_path,
            str(paths["yolo_json"]),
            detection_index,
            detection,
            crop_path,
            str(vlm_json_path),
            vlm_result,
        )
        food_id = db_result.get("food_id")
        current_object = {
            "food_id": food_id,
            "yolo_label": detection.get("class_name"),
            "confidence": detection.get("confidence"),
            "box": detection.get("box"),
            "change_fingerprint": detection.get("change_fingerprint"),
            "crop_box": crop_box,
            "image_ref": image_path,
            "crop_ref": crop_path,
            "vlm_json": str(vlm_json_path),
            "first_seen_at": now,
            "last_seen_at": now,
            "vlm": vlm_result,
            "sensor_snapshot": vlm_sensor_context,
        }
        current_objects.append(current_object)
        added.append(
            {
                "food_id": food_id,
                "detection_index": detection_index,
                "yolo_label": detection.get("class_name"),
                "food_name": vlm_result.get("food_name"),
                "freshness": vlm_result.get("freshness"),
                "risk_level": vlm_result.get("risk_level"),
            }
        )

    for previous_index in removed_indexes:
        previous = previous_objects[previous_index]
        try:
            result = mark_removed(previous, now)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            errors.append({"stage": "remove", "food_id": previous.get("food_id"), "error": str(exc)})
        removed.append({"food_id": previous.get("food_id"), "yolo_label": previous.get("yolo_label"), "db_result": result})

    completed_at = datetime.now(timezone.utc).replace(microsecond=0)
    interval = env_int("SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS", 3600)
    summary = {
        "ok": not any(not item.get("fallback", True) for item in errors),
        "captured_at": now,
        "completed_at": utc_iso(completed_at),
        "next_scheduled_at": utc_iso(completed_at + timedelta(seconds=interval)),
        "camera_device": camera_device,
        "image_ref": image_path,
        "yolo_json": str(paths["yolo_json"]),
        "detections": len(detections),
        "yolo_role": "change_candidates",
        "unchanged": unchanged,
        "added": added,
        "removed": removed,
        "suppressed_candidates": suppressed,
        "rejected_candidate_count": len(current_rejections),
        "active_count": len(current_objects),
        "deleted_captures": deleted_captures,
        "state_path": str(state_path),
        "errors": errors,
        "sensor_snapshot": load_sensor_context(),
    }
    state = {
        "updated_at": now,
        "last_image_ref": image_path,
        "last_yolo_json": str(paths["yolo_json"]),
        "active_objects": current_objects,
        "rejected_candidates": current_rejections,
        "last_cycle": summary,
    }
    write_json(state_path, state)
    cloud_advice = request_cloud_advice(current_objects, summary, summary["sensor_snapshot"])
    summary["cloud_advice"] = cloud_advice
    state["cloud_advice"] = cloud_advice
    state["last_cycle"] = summary
    write_json(state_path, state)
    return summary


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Run one smart-fridge capture -> YOLO -> VLM -> SQLite cycle.")
    parser.add_argument("--once", action="store_true", help="Run one cycle. This is the default behavior.")
    parser.add_argument("--image", help="Use an existing image instead of capturing from camera.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    summary = run_once(args)
    print_json(summary)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
