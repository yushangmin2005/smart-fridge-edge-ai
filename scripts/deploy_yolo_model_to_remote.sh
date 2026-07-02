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
    YOLO_REMOTE_HOST \
    YOLO_REMOTE_DIR \
    YOLO_REMOTE_MODEL_NAME \
    YOLO_EXPORT_MODEL \
    YOLO_EXPORT_LABELS
else
  echo "Missing config: $CONFIG_FILE" >&2
  echo "Create it with: cp $EXAMPLE_CONFIG $CONFIG_FILE" >&2
  exit 2
fi

HOST="${1:-${YOLO_REMOTE_HOST:-firecar-pi}}"
REMOTE_DIR_RAW="${YOLO_REMOTE_DIR:-~/yolo-inference}"
REMOTE_MODEL_NAME="${YOLO_REMOTE_MODEL_NAME:-fridge-yolo11n.onnx}"
LOCAL_ONNX_VALUE="${YOLO_EXPORT_MODEL:-models/fridge-yolo11n.onnx}"
LOCAL_LABELS_VALUE="${YOLO_EXPORT_LABELS:-models/fridge-yolo11n.classes.txt}"

if [ "$REMOTE_DIR_RAW" = "$HOME" ]; then
  REMOTE_DIR_RAW="~"
elif [ "${REMOTE_DIR_RAW#"$HOME"/}" != "$REMOTE_DIR_RAW" ]; then
  REMOTE_DIR_RAW="~/${REMOTE_DIR_RAW#"$HOME"/}"
fi

resolve_local_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$REPO_ROOT" "$1" ;;
  esac
}

LOCAL_ONNX="$(resolve_local_path "$LOCAL_ONNX_VALUE")"
LOCAL_LABELS="$(resolve_local_path "$LOCAL_LABELS_VALUE")"

if [ ! -f "$LOCAL_ONNX" ]; then
  echo "Missing local ONNX model: $LOCAL_ONNX" >&2
  echo "Run: scripts/export_yolo11n_onnx_local.sh" >&2
  exit 3
fi

if [ ! -f "$LOCAL_LABELS" ]; then
  echo "Missing local labels file: $LOCAL_LABELS" >&2
  echo "Run: scripts/export_yolo11n_onnx_local.sh" >&2
  exit 4
fi

REMOTE_DIR="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "REMOTE_DIR_RAW='$REMOTE_DIR_RAW' bash -s" <<'REMOTE'
set -euo pipefail

raw="${REMOTE_DIR_RAW:-~/yolo-inference}"
if [ "$raw" = "~" ]; then
  remote_dir="$HOME"
elif [ "${raw#\~/}" != "$raw" ]; then
  remote_dir="$HOME/${raw#\~/}"
elif [ "${raw#/}" != "$raw" ]; then
  remote_dir="$raw"
else
  remote_dir="$HOME/$raw"
fi

mkdir -p "$remote_dir/models" "$remote_dir/config"
printf '%s\n' "$remote_dir"
REMOTE
)"

scp -q "$LOCAL_ONNX" "$HOST:$REMOTE_DIR/models/$REMOTE_MODEL_NAME"
scp -q "$LOCAL_LABELS" "$HOST:$REMOTE_DIR/config/classes.txt"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "REMOTE_DIR='$REMOTE_DIR' REMOTE_MODEL_NAME='$REMOTE_MODEL_NAME' bash -s" <<'REMOTE'
set -euo pipefail

cat > "$REMOTE_DIR/config/yolo.env" <<EOF
YOLO_MODEL_PATH=$REMOTE_DIR/models/$REMOTE_MODEL_NAME
YOLO_LABELS=$REMOTE_DIR/config/classes.txt
YOLO_IMG_SIZE=640
YOLO_CONF=0.25
YOLO_IOU=0.45
YOLO_TOPK=300
YOLO_OUTPUT_DIR=$REMOTE_DIR/outputs
EOF

"$REMOTE_DIR/bin/yolo_check.sh"
REMOTE

echo "remote_yolo_model=$HOST:$REMOTE_DIR/models/$REMOTE_MODEL_NAME"
