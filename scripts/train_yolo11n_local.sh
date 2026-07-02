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
    YOLO_TRAIN_VENV \
    YOLO_MODEL \
    YOLO_DATA_YAML \
    YOLO_IMG_SIZE \
    YOLO_EPOCHS \
    YOLO_BATCH \
    YOLO_WORKERS \
    YOLO_DEVICE \
    YOLO_CACHE \
    YOLO_PATIENCE \
    YOLO_PROJECT \
    YOLO_RUN_NAME \
    YOLO_FRACTION
else
  echo "Missing config: $CONFIG_FILE" >&2
  echo "Create it with: cp $EXAMPLE_CONFIG $CONFIG_FILE" >&2
  exit 2
fi

VENV_DIR="$REPO_ROOT/${YOLO_TRAIN_VENV:-.venv-yolo}"
YOLO_BIN="$VENV_DIR/bin/yolo"
PYTHON_BIN="$VENV_DIR/bin/python"
DATA_YAML="$REPO_ROOT/${YOLO_DATA_YAML:-data/fridge-food-images/data.yaml}"
DEVICE="${YOLO_DEVICE:-auto}"

if [ ! -x "$YOLO_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "Missing training venv. Run: scripts/setup_yolo_training_local.sh" >&2
  exit 3
fi

if [ ! -f "$DATA_YAML" ]; then
  echo "Missing dataset YAML: $DATA_YAML" >&2
  echo "Run: scripts/download_roboflow_dataset.sh" >&2
  exit 4
fi

if [ "$DEVICE" = "auto" ]; then
  DEVICE="$("$PYTHON_BIN" - <<'PY'
import torch
print("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu")
PY
)"
fi

args=(
  detect train
  "model=${YOLO_MODEL:-yolo11n.pt}"
  "data=$DATA_YAML"
  "imgsz=${YOLO_IMG_SIZE:-640}"
  "epochs=${YOLO_EPOCHS:-80}"
  "batch=${YOLO_BATCH:-8}"
  "workers=${YOLO_WORKERS:-4}"
  "device=$DEVICE"
  "cache=${YOLO_CACHE:-False}"
  "patience=${YOLO_PATIENCE:-20}"
  "project=$REPO_ROOT/${YOLO_PROJECT:-runs/fridge-yolo11n}"
  "name=${YOLO_RUN_NAME:-public-fridge-food-images}"
  exist_ok=True
)

if [ -n "${YOLO_FRACTION:-}" ]; then
  args+=("fraction=$YOLO_FRACTION")
fi

"$YOLO_BIN" "${args[@]}"

WEIGHTS="$REPO_ROOT/${YOLO_PROJECT:-runs/fridge-yolo11n}/${YOLO_RUN_NAME:-public-fridge-food-images}/weights/best.pt"
if [ -f "$WEIGHTS" ]; then
  echo "best_weights=$WEIGHTS"
else
  echo "Training finished but expected best weights were not found: $WEIGHTS" >&2
  exit 5
fi
