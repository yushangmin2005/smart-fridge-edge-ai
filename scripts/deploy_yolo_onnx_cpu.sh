#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
YOLO_REMOTE_DIR="${YOLO_REMOTE_DIR:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$REPO_ROOT/yolo_runtime/yolo_detect.py"

if [ ! -f "$RUNNER" ]; then
  echo "Missing local runner: $RUNNER" >&2
  exit 2
fi

if [ -z "$YOLO_REMOTE_DIR" ]; then
  REMOTE_PATH="~/yolo-inference"
else
  REMOTE_PATH="$YOLO_REMOTE_DIR"
fi

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "YOLO_REMOTE_DIR='$YOLO_REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail

if [ -z "${YOLO_REMOTE_DIR:-}" ]; then
  YOLO_REMOTE_DIR="$HOME/yolo-inference"
fi

for c in python3; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "Missing required command: $c" >&2
    exit 2
  fi
done

if ! python3 -m pip --version >/dev/null 2>&1; then
  echo "Missing python3 pip on remote host." >&2
  exit 3
fi

available_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
if [ "$available_kb" -lt 786432 ]; then
  echo "Not enough free disk under $HOME; need at least 768 MiB for YOLO runtime." >&2
  exit 4
fi

mkdir -p "$YOLO_REMOTE_DIR"/{bin,config,models,samples,outputs,runtime,tmp}

deps_dir="$YOLO_REMOTE_DIR/runtime/python-packages"
tmp_deps="$YOLO_REMOTE_DIR/tmp/python-packages.$$"
rm -rf "$tmp_deps"
mkdir -p "$tmp_deps"
python3 -m pip install --no-cache-dir --upgrade --target "$tmp_deps" \
  "numpy==1.24.4" \
  "Pillow==10.4.0" \
  "onnxruntime==1.16.3"
rm -rf "$deps_dir"
mv "$tmp_deps" "$deps_dir"

cat > "$YOLO_REMOTE_DIR/config/yolo.env.example" <<EOF
# Put an exported YOLO ONNX model here.
YOLO_MODEL_PATH=$YOLO_REMOTE_DIR/models/model.onnx

# Optional labels file, one class name per line.
YOLO_LABELS=$YOLO_REMOTE_DIR/config/classes.txt

YOLO_IMG_SIZE=640
YOLO_CONF=0.25
YOLO_IOU=0.45
YOLO_TOPK=300
YOLO_OUTPUT_DIR=$YOLO_REMOTE_DIR/outputs
EOF

if [ ! -f "$YOLO_REMOTE_DIR/config/yolo.env" ]; then
  cp "$YOLO_REMOTE_DIR/config/yolo.env.example" "$YOLO_REMOTE_DIR/config/yolo.env"
fi

cat > "$YOLO_REMOTE_DIR/bin/yolo_env.sh" <<'EOF'
#!/usr/bin/env bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export YOLO_ROOT="$ROOT"
export PYTHONPATH="$ROOT/runtime/python-packages:${PYTHONPATH:-}"
EOF
chmod +x "$YOLO_REMOTE_DIR/bin/yolo_env.sh"

cat > "$YOLO_REMOTE_DIR/bin/yolo_detect.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/yolo_env.sh"

ENV_FILE="${YOLO_ENV_FILE:-$ROOT/config/yolo.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

args=()
[ -n "${YOLO_MODEL_PATH:-}" ] && args+=(--model "$YOLO_MODEL_PATH")
[ -n "${YOLO_LABELS:-}" ] && [ -f "$YOLO_LABELS" ] && args+=(--labels "$YOLO_LABELS")
[ -n "${YOLO_IMG_SIZE:-}" ] && args+=(--img-size "$YOLO_IMG_SIZE")
[ -n "${YOLO_CONF:-}" ] && args+=(--conf "$YOLO_CONF")
[ -n "${YOLO_IOU:-}" ] && args+=(--iou "$YOLO_IOU")
[ -n "${YOLO_TOPK:-}" ] && args+=(--topk "$YOLO_TOPK")

exec python3 "$ROOT/runtime/yolo_detect.py" "${args[@]}" "$@"
EOF
chmod +x "$YOLO_REMOTE_DIR/bin/yolo_detect.sh"

cat > "$YOLO_REMOTE_DIR/bin/yolo_check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/yolo_env.sh"

python3 - <<'PY'
import numpy as np
from PIL import Image
import onnxruntime as ort

print("numpy=" + np.__version__)
print("pillow=" + Image.__version__)
print("onnxruntime=" + ort.__version__)
print("providers=" + ",".join(ort.get_available_providers()))
PY

test -f "$ROOT/runtime/yolo_detect.py"
python3 "$ROOT/runtime/yolo_detect.py" --help >/dev/null
echo "yolo_runtime_check=pass"
EOF
chmod +x "$YOLO_REMOTE_DIR/bin/yolo_check.sh"
REMOTE

scp -q "$RUNNER" "$HOST:$REMOTE_PATH/runtime/yolo_detect.py"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "chmod +x $REMOTE_PATH/runtime/yolo_detect.py && $REMOTE_PATH/bin/yolo_check.sh"
