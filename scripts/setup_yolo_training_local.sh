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
    YOLO_PYTHON_VERSION
else
  echo "Missing config: $CONFIG_FILE" >&2
  echo "Create it with: cp $EXAMPLE_CONFIG $CONFIG_FILE" >&2
  exit 2
fi

VENV_DIR="$REPO_ROOT/${YOLO_TRAIN_VENV:-.venv-yolo}"
PYTHON_VERSION="${YOLO_PYTHON_VERSION:-3.12}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Missing uv. Install it first or provide a compatible venv manually." >&2
  exit 3
fi

if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
  rm -rf "$VENV_DIR"
  uv python install "$PYTHON_VERSION"
  uv venv --seed --python "$PYTHON_VERSION" "$VENV_DIR"
fi

pip_install() {
  local attempt
  for attempt in 1 2 3; do
    if "$VENV_DIR/bin/python" -m pip install --retries 10 --timeout 120 --upgrade "$@"; then
      return 0
    fi
    echo "pip install failed; retrying ($attempt/3)..." >&2
    sleep 5
  done
  return 1
}

pip_install pip setuptools wheel
pip_install "torch>=2.3.0" "torchvision>=0.18.0"
pip_install \
  "ultralytics>=8.3.0" \
  "roboflow>=1.1.0" \
  "onnx>=1.16.0" \
  "onnxruntime>=1.18.0" \
  "onnxslim>=0.1.40"

"$VENV_DIR/bin/python" - <<'PY'
import platform
import torch
import ultralytics

mps = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
print("python=" + platform.python_version())
print("torch=" + torch.__version__)
print("ultralytics=" + ultralytics.__version__)
print("mps_available=" + str(mps).lower())
PY

echo "yolo_training_env=ready"
