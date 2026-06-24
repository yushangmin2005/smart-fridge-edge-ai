#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
LLAMA_CPP_TAG="${LLAMA_CPP_TAG:-b9773}"
VLM_REMOTE_DIR="${VLM_REMOTE_DIR:-}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "LLAMA_CPP_TAG='$LLAMA_CPP_TAG' VLM_REMOTE_DIR='$VLM_REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail

if [ -z "${VLM_REMOTE_DIR:-}" ]; then
  VLM_REMOTE_DIR="$HOME/vlm-inference"
fi

arch="$(uname -m)"
case "$arch" in
  aarch64|arm64)
    asset="llama-${LLAMA_CPP_TAG}-bin-ubuntu-arm64.tar.gz"
    ;;
  x86_64|amd64)
    asset="llama-${LLAMA_CPP_TAG}-bin-ubuntu-x64.tar.gz"
    ;;
  *)
    echo "Unsupported architecture for prebuilt llama.cpp package: $arch" >&2
    exit 2
    ;;
esac

available_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
if [ "$available_kb" -lt 524288 ]; then
  echo "Not enough free disk under $HOME; need at least 512 MiB for runtime install." >&2
  exit 3
fi

url="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_CPP_TAG}/${asset}"
mkdir -p "$VLM_REMOTE_DIR"/{bin,config,logs,models,run,runtime,tmp}

archive="$VLM_REMOTE_DIR/tmp/$asset"
echo "Downloading $url"
curl -fL --retry 3 --connect-timeout 20 -o "$archive" "$url"

extract_root="$VLM_REMOTE_DIR/runtime"
rm -rf "$extract_root/llama-${LLAMA_CPP_TAG}"
tar -xzf "$archive" -C "$extract_root"
rm -f "$archive"
runtime_dir="$extract_root/llama-${LLAMA_CPP_TAG}"

if ! LD_LIBRARY_PATH="$runtime_dir:${LD_LIBRARY_PATH:-}" "$runtime_dir/llama-server" --version >/tmp/llama-prebuilt-check.log 2>&1; then
  echo "Prebuilt llama.cpp package is not usable on this host:"
  cat /tmp/llama-prebuilt-check.log
  echo "Falling back to source build with local system libraries."

  src_archive="$VLM_REMOTE_DIR/tmp/llama.cpp-${LLAMA_CPP_TAG}.tar.gz"
  src_dir="$VLM_REMOTE_DIR/tmp/llama.cpp-${LLAMA_CPP_TAG}"
  build_dir="$VLM_REMOTE_DIR/tmp/llama.cpp-${LLAMA_CPP_TAG}-build"
  runtime_dir="$extract_root/llama-${LLAMA_CPP_TAG}-source"

  rm -rf "$src_dir" "$build_dir" "$runtime_dir"
  curl -fL --retry 3 --connect-timeout 20 \
    -o "$src_archive" \
    "https://github.com/ggml-org/llama.cpp/archive/refs/tags/${LLAMA_CPP_TAG}.tar.gz"
  mkdir -p "$src_dir"
  tar -xzf "$src_archive" -C "$src_dir" --strip-components=1
  rm -f "$src_archive"

  # firecar-pi ships CMake 3.16. The server UI asset script only needs the
  # newer CMake path when building/downloading the web UI, which is disabled
  # for this headless deployment.
  sed -i 's/cmake_minimum_required(VERSION 3.18)/cmake_minimum_required(VERSION 3.16)/' \
    "$src_dir/scripts/ui-assets.cmake"

  cmake -S "$src_dir" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_TOOLS=ON \
    -DLLAMA_BUILD_APP=OFF \
    -DLLAMA_BUILD_UI=OFF \
    -DLLAMA_USE_PREBUILT_UI=OFF \
    -DGGML_NATIVE=OFF
  cmake --build "$build_dir" --config Release --target llama-server llama-mtmd-cli -j "${VLM_BUILD_JOBS:-1}"

  mkdir -p "$runtime_dir"
  cp "$build_dir/bin/llama-server" "$runtime_dir/"
  cp "$build_dir/bin/llama-mtmd-cli" "$runtime_dir/"
  find "$build_dir" -type f \( -name '*.so' -o -name '*.so.*' \) -exec cp -P {} "$runtime_dir/" \; 2>/dev/null || true
  [ -f "$src_dir/LICENSE" ] && cp "$src_dir/LICENSE" "$runtime_dir/LICENSE"
  rm -rf "$src_dir" "$build_dir"
  rm -rf "$extract_root/llama-${LLAMA_CPP_TAG}"
fi

ln -sfn "$runtime_dir" "$extract_root/current"

cat > "$VLM_REMOTE_DIR/config/vlm.env.example" <<'EOF'
VLM_HOST=0.0.0.0
VLM_PORT=8080
VLM_THREADS=4
VLM_CTX_SIZE=2048
VLM_PARALLEL=1

# Set exactly one model source before starting the service.
# Example: VLM_MODEL_HF=ggml-org/SmolVLM-256M-Instruct-GGUF
VLM_MODEL_HF=
VLM_MODEL_PATH=

# Required for some local GGUF VLMs when the projector is stored separately.
VLM_MMPROJ_PATH=

# Keep GPU projector offload disabled for CPU-only deployment.
VLM_NO_MMPROJ_OFFLOAD=1

# Optional raw llama-server arguments, split on spaces.
VLM_EXTRA_ARGS=
EOF

if [ ! -f "$VLM_REMOTE_DIR/config/vlm.env" ]; then
  cp "$VLM_REMOTE_DIR/config/vlm.env.example" "$VLM_REMOTE_DIR/config/vlm.env"
fi

cat > "$VLM_REMOTE_DIR/bin/start_vlm.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENSSL_ENV="$ROOT/bin/openssl3_env.sh"
[ -f "$OPENSSL_ENV" ] && . "$OPENSSL_ENV"
ENV_FILE="${VLM_ENV_FILE:-$ROOT/config/vlm.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

LLAMA_DIR="${LLAMA_DIR:-$ROOT/runtime/current}"
SERVER="$LLAMA_DIR/llama-server"
PID_FILE="$ROOT/run/vlm.pid"
LOG_FILE="$ROOT/logs/vlm-server.log"

if [ ! -x "$SERVER" ]; then
  echo "llama-server not found or not executable: $SERVER" >&2
  exit 2
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "VLM server already running with PID $(cat "$PID_FILE")"
  exit 0
fi

: "${VLM_HOST:=0.0.0.0}"
: "${VLM_PORT:=8080}"
: "${VLM_THREADS:=4}"
: "${VLM_CTX_SIZE:=2048}"
: "${VLM_PARALLEL:=1}"
: "${VLM_MODEL_HF:=}"
: "${VLM_MODEL_PATH:=}"
: "${VLM_MMPROJ_PATH:=}"
: "${VLM_NO_MMPROJ_OFFLOAD:=1}"
: "${VLM_EXTRA_ARGS:=}"

args=(
  "$SERVER"
  --host "$VLM_HOST"
  --port "$VLM_PORT"
  --threads "$VLM_THREADS"
  --ctx-size "$VLM_CTX_SIZE"
  --parallel "$VLM_PARALLEL"
)

if [ -n "$VLM_MODEL_PATH" ]; then
  args+=(-m "$VLM_MODEL_PATH")
elif [ -n "$VLM_MODEL_HF" ]; then
  args+=(-hf "$VLM_MODEL_HF")
else
  echo "No model configured. Set VLM_MODEL_HF or VLM_MODEL_PATH in $ENV_FILE." >&2
  exit 3
fi

if [ -n "$VLM_MMPROJ_PATH" ]; then
  args+=(--mmproj "$VLM_MMPROJ_PATH")
fi

if [ "$VLM_NO_MMPROJ_OFFLOAD" = "1" ]; then
  args+=(--no-mmproj-offload)
fi

if [ -n "$VLM_EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206
  extra_args=($VLM_EXTRA_ARGS)
  args+=("${extra_args[@]}")
fi

mkdir -p "$ROOT/logs" "$ROOT/run" "$ROOT/models"
export LD_LIBRARY_PATH="$LLAMA_DIR:${LD_LIBRARY_PATH:-}"
nohup "${args[@]}" > "$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" > "$PID_FILE"
echo "Started VLM server PID $pid; log: $LOG_FILE"
EOF

cat > "$VLM_REMOTE_DIR/bin/stop_vlm.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/vlm.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "VLM server is not running."
  exit 0
fi

pid="$(cat "$PID_FILE")"
if kill -0 "$pid" >/dev/null 2>&1; then
  kill "$pid"
  echo "Stopped VLM server PID $pid"
else
  echo "Stale PID file removed: $PID_FILE"
fi
rm -f "$PID_FILE"
EOF

cat > "$VLM_REMOTE_DIR/bin/status_vlm.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/vlm.pid"
LOG_FILE="$ROOT/logs/vlm-server.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "running PID $(cat "$PID_FILE")"
else
  echo "stopped"
fi

if [ -f "$LOG_FILE" ]; then
  echo "--- last log lines ---"
  tail -n 20 "$LOG_FILE"
fi
EOF

cat > "$VLM_REMOTE_DIR/bin/health_vlm.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${VLM_ENV_FILE:-$ROOT/config/vlm.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

: "${VLM_PORT:=8080}"
: "${VLM_HEALTH_HOST:=127.0.0.1}"
curl -fsS "http://${VLM_HEALTH_HOST}:${VLM_PORT}/v1/models"
echo
EOF

cat > "$VLM_REMOTE_DIR/bin/runtime_check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENSSL_ENV="$ROOT/bin/openssl3_env.sh"
[ -f "$OPENSSL_ENV" ] && . "$OPENSSL_ENV"
LLAMA_DIR="${LLAMA_DIR:-$ROOT/runtime/current}"
export LD_LIBRARY_PATH="$LLAMA_DIR:${LD_LIBRARY_PATH:-}"

"$LLAMA_DIR/llama-server" --version
"$LLAMA_DIR/llama-mtmd-cli" --help >/dev/null
echo "runtime_check=pass"
EOF

chmod +x "$VLM_REMOTE_DIR"/bin/*.sh

export LD_LIBRARY_PATH="$VLM_REMOTE_DIR/runtime/current:${LD_LIBRARY_PATH:-}"
"$VLM_REMOTE_DIR/runtime/current/llama-server" --version
"$VLM_REMOTE_DIR/runtime/current/llama-mtmd-cli" --help >/dev/null
echo "deploy=pass path=$VLM_REMOTE_DIR"
REMOTE
