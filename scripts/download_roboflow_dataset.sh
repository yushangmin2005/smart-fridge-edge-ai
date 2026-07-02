#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="${YOLO_TRAIN_CONFIG:-$REPO_ROOT/config/yolo_public_dataset.env}"
EXAMPLE_CONFIG="$REPO_ROOT/config/yolo_public_dataset.env.example"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/lib_config.sh"
  load_config_with_overrides "$CONFIG_FILE" \
    ROBOFLOW_DATASET_ID \
    ROBOFLOW_DATASET_URL \
    ROBOFLOW_FORMAT \
    ROBOFLOW_API_KEY \
    YOLO_DATASET_DIR \
    YOLO_FORCE_DOWNLOAD \
    YOLO_TRAIN_VENV
else
  echo "Missing config: $CONFIG_FILE" >&2
  echo "Create it with: cp $EXAMPLE_CONFIG $CONFIG_FILE" >&2
  exit 2
fi

VENV_DIR="$REPO_ROOT/${YOLO_TRAIN_VENV:-.venv-yolo}"
PYTHON_BIN="$VENV_DIR/bin/python"
DATASET_REF="${ROBOFLOW_DATASET_ID:-${ROBOFLOW_DATASET_URL:-}}"
DATASET_DIR="$REPO_ROOT/${YOLO_DATASET_DIR:-data/fridge-food-images}"
FORMAT="${ROBOFLOW_FORMAT:-yolov11}"

if [ -z "$DATASET_REF" ]; then
  echo "ROBOFLOW_DATASET_ID is required." >&2
  exit 3
fi

DATASET_REF="${DATASET_REF#https://universe.roboflow.com/}"
DATASET_REF="${DATASET_REF#http://universe.roboflow.com/}"
DATASET_REF="${DATASET_REF%\?*}"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Missing training venv. Run: scripts/setup_yolo_training_local.sh" >&2
  exit 4
fi

if [ -f "$DATASET_DIR/data.yaml" ] && [ "${YOLO_FORCE_DOWNLOAD:-0}" != "1" ]; then
  echo "Dataset already exists: $DATASET_DIR/data.yaml"
  echo "Set YOLO_FORCE_DOWNLOAD=1 to re-download."
  exit 0
fi

mkdir -p "$(dirname "$DATASET_DIR")"

if [ -z "${ROBOFLOW_API_KEY:-}" ]; then
  auth_status="$("$VENV_DIR/bin/roboflow" auth status --json 2>&1 || true)"
  if printf '%s\n' "$auth_status" | grep -q '"error"'; then
    echo "Roboflow download requires an API key or CLI login." >&2
    echo "Set ROBOFLOW_API_KEY in $CONFIG_FILE, or run: $VENV_DIR/bin/roboflow auth login" >&2
    echo "Manual fallback: export YOLO11/YOLOv8 from Roboflow and unzip it to $DATASET_DIR with data.yaml at the top level." >&2
    exit 7
  fi
fi

download_with_format() {
  local format="$1"
  local cmd=("$VENV_DIR/bin/roboflow")

  if [ -n "${ROBOFLOW_API_KEY:-}" ]; then
    cmd+=(--api-key "$ROBOFLOW_API_KEY")
  fi

  cmd+=(download -f "$format" -l "$DATASET_DIR" "$DATASET_REF")

  rm -rf "$DATASET_DIR"
  "${cmd[@]}"
}

if ! download_with_format "$FORMAT"; then
  if [ "$FORMAT" = "yolov11" ]; then
    echo "Roboflow yolov11 export failed; retrying with yolov8." >&2
    download_with_format "yolov8"
  else
    exit 5
  fi
fi

if [ ! -f "$DATASET_DIR/data.yaml" ]; then
  echo "Download finished but data.yaml was not found under $DATASET_DIR." >&2
  exit 6
fi

"$PYTHON_BIN" - <<PY
from pathlib import Path
import yaml

path = Path("$DATASET_DIR/data.yaml")
data = yaml.safe_load(path.read_text())
names = data.get("names", [])
if isinstance(names, dict):
    names = [names[k] for k in sorted(names, key=lambda x: int(x))]
print(f"dataset_yaml={path}")
print(f"class_count={len(names)}")
print("classes=" + ",".join(map(str, names[:20])) + ("..." if len(names) > 20 else ""))
PY
