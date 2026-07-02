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
    YOLO_DATA_YAML \
    YOLO_WEIGHTS_PATH \
    YOLO_PROJECT \
    YOLO_RUN_NAME \
    YOLO_EXPORT_MODEL \
    YOLO_EXPORT_LABELS \
    YOLO_IMG_SIZE \
    YOLO_ONNX_OPSET
else
  echo "Missing config: $CONFIG_FILE" >&2
  echo "Create it with: cp $EXAMPLE_CONFIG $CONFIG_FILE" >&2
  exit 2
fi

VENV_DIR="$REPO_ROOT/${YOLO_TRAIN_VENV:-.venv-yolo}"
YOLO_BIN="$VENV_DIR/bin/yolo"
PYTHON_BIN="$VENV_DIR/bin/python"
DATA_YAML="$REPO_ROOT/${YOLO_DATA_YAML:-data/fridge-food-images/data.yaml}"
WEIGHTS="${YOLO_WEIGHTS_PATH:-$REPO_ROOT/${YOLO_PROJECT:-runs/fridge-yolo11n}/${YOLO_RUN_NAME:-public-fridge-food-images}/weights/best.pt}"
EXPORT_MODEL="$REPO_ROOT/${YOLO_EXPORT_MODEL:-models/fridge-yolo11n.onnx}"
EXPORT_LABELS="$REPO_ROOT/${YOLO_EXPORT_LABELS:-models/fridge-yolo11n.classes.txt}"

if [ ! -x "$YOLO_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "Missing training venv. Run: scripts/setup_yolo_training_local.sh" >&2
  exit 3
fi

if [ ! -f "$WEIGHTS" ]; then
  echo "Missing trained weights: $WEIGHTS" >&2
  echo "Run: scripts/train_yolo11n_local.sh" >&2
  exit 4
fi

if [ ! -f "$DATA_YAML" ]; then
  echo "Missing dataset YAML for class export: $DATA_YAML" >&2
  exit 5
fi

mkdir -p "$(dirname "$EXPORT_MODEL")" "$(dirname "$EXPORT_LABELS")"

"$YOLO_BIN" export \
  "model=$WEIGHTS" \
  format=onnx \
  "imgsz=${YOLO_IMG_SIZE:-640}" \
  "opset=${YOLO_ONNX_OPSET:-19}" \
  simplify=True \
  dynamic=False \
  nms=False

RAW_ONNX="${WEIGHTS%.pt}.onnx"
if [ ! -f "$RAW_ONNX" ]; then
  echo "Ultralytics export finished but ONNX file was not found: $RAW_ONNX" >&2
  exit 6
fi

cp "$RAW_ONNX" "$EXPORT_MODEL"

"$PYTHON_BIN" - <<PY
from pathlib import Path
import yaml

data = yaml.safe_load(Path("$DATA_YAML").read_text())
names = data.get("names", [])
if isinstance(names, dict):
    names = [names[k] for k in sorted(names, key=lambda x: int(x))]
Path("$EXPORT_LABELS").write_text("\\n".join(map(str, names)) + "\\n")
print(f"export_model=$EXPORT_MODEL")
print(f"export_labels=$EXPORT_LABELS")
print(f"class_count={len(names)}")
PY
