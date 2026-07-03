#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
SMART_FRIDGE_REMOTE_DIR="${SMART_FRIDGE_REMOTE_DIR:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$REPO_ROOT/smart_fridge_runtime/fridge_db.py"
PIPELINE="$REPO_ROOT/smart_fridge_runtime/fridge_pipeline.py"
WEB="$REPO_ROOT/smart_fridge_runtime/fridge_web.py"
PROMPT="$REPO_ROOT/smart_fridge_runtime/vlm_food_prompt.txt"

if [ ! -f "$RUNNER" ]; then
  echo "Missing local runtime: $RUNNER" >&2
  exit 2
fi
if [ ! -f "$PIPELINE" ]; then
  echo "Missing local runtime: $PIPELINE" >&2
  exit 2
fi
if [ ! -f "$WEB" ]; then
  echo "Missing local runtime: $WEB" >&2
  exit 2
fi
if [ ! -f "$PROMPT" ]; then
  echo "Missing local prompt: $PROMPT" >&2
  exit 2
fi

if [ -z "$SMART_FRIDGE_REMOTE_DIR" ]; then
  REMOTE_PATH="~/smart-fridge"
else
  REMOTE_PATH="$SMART_FRIDGE_REMOTE_DIR"
fi

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "SMART_FRIDGE_REMOTE_DIR='$SMART_FRIDGE_REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail

if [ -z "${SMART_FRIDGE_REMOTE_DIR:-}" ]; then
  SMART_FRIDGE_REMOTE_DIR="$HOME/smart-fridge"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Missing required command: python3" >&2
  exit 2
fi

mkdir -p "$SMART_FRIDGE_REMOTE_DIR"/{bin,config,data,runtime,tmp}

cat > "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env.example" <<EOF
SMART_FRIDGE_DB_PATH=$SMART_FRIDGE_REMOTE_DIR/data/fridge.sqlite3
SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES=120
SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS=3600
SMART_FRIDGE_CAPTURE_KEEP=24
SMART_FRIDGE_CAMERA_DEVICE=auto
SMART_FRIDGE_CAPTURE_WIDTH=640
SMART_FRIDGE_CAPTURE_HEIGHT=360
SMART_FRIDGE_CAPTURE_FORMAT=mjpeg
SMART_FRIDGE_CAPTURE_TIMEOUT=25
SMART_FRIDGE_MATCH_IOU=0.35
SMART_FRIDGE_CROP_PADDING=0.08
SMART_FRIDGE_WRITE_FALLBACK_ON_VLM_ERROR=1
SMART_FRIDGE_YOLO_BIN=/home/pi/yolo-inference/bin/yolo_detect.sh
SMART_FRIDGE_YOLO_TIMEOUT=300
SMART_FRIDGE_VLM_URL=http://127.0.0.1:8080/v1/chat/completions
SMART_FRIDGE_VLM_TIMEOUT=3600
SMART_FRIDGE_VLM_MAX_TOKENS=160
SMART_FRIDGE_VLM_USE_RESPONSE_FORMAT=0
SMART_FRIDGE_VLM_PROMPT_PATH=$SMART_FRIDGE_REMOTE_DIR/runtime/vlm_food_prompt.txt
SMART_FRIDGE_WEB_HOST=0.0.0.0
SMART_FRIDGE_WEB_PORT=8090
SMART_FRIDGE_WEB_REFRESH_SECONDS=30
EOF

if [ ! -f "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env" ]; then
  cp "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env.example" "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env"
else
  while IFS='=' read -r key value; do
    case "$key" in
      ""|\#*) continue ;;
    esac
    if ! grep -q "^${key}=" "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env"; then
      printf '%s=%s\n' "$key" "$value" >> "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env"
    fi
  done < "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env.example"
fi

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_env.sh" <<'EOF'
#!/usr/bin/env bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMART_FRIDGE_DB_PATH_OVERRIDE="${SMART_FRIDGE_DB_PATH-}"
SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES_OVERRIDE="${SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES-}"
SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS_OVERRIDE="${SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS-}"
ENV_FILE="${SMART_FRIDGE_ENV_FILE:-$ROOT/config/smart_fridge.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
if [ -n "$SMART_FRIDGE_DB_PATH_OVERRIDE" ]; then
  SMART_FRIDGE_DB_PATH="$SMART_FRIDGE_DB_PATH_OVERRIDE"
fi
if [ -n "$SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES_OVERRIDE" ]; then
  SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES="$SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES_OVERRIDE"
fi
if [ -n "$SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS_OVERRIDE" ]; then
  SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS="$SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS_OVERRIDE"
fi
export SMART_FRIDGE_ROOT="$ROOT"
: "${SMART_FRIDGE_DB_PATH:=$ROOT/data/fridge.sqlite3}"
: "${SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES:=120}"
: "${SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS:=3600}"
: "${SMART_FRIDGE_CAPTURE_KEEP:=24}"
: "${SMART_FRIDGE_TMP_DIR:=$ROOT/tmp}"
: "${SMART_FRIDGE_STATE_PATH:=$ROOT/data/pipeline_state.json}"
: "${SMART_FRIDGE_VLM_PROMPT_PATH:=$ROOT/runtime/vlm_food_prompt.txt}"
: "${SMART_FRIDGE_WEB_HOST:=0.0.0.0}"
: "${SMART_FRIDGE_WEB_PORT:=8090}"
: "${SMART_FRIDGE_WEB_REFRESH_SECONDS:=30}"
export SMART_FRIDGE_DB_PATH SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES
export SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS SMART_FRIDGE_CAPTURE_KEEP
export SMART_FRIDGE_TMP_DIR SMART_FRIDGE_STATE_PATH SMART_FRIDGE_VLM_PROMPT_PATH
export SMART_FRIDGE_WEB_HOST SMART_FRIDGE_WEB_PORT SMART_FRIDGE_WEB_REFRESH_SECONDS
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_env.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/fridge_db_env.sh"

exec python3 "$ROOT/runtime/fridge_db.py" --db "$SMART_FRIDGE_DB_PATH" "$@"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_pipeline.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/fridge_db_env.sh"

YOLO_PACKAGES="/home/pi/yolo-inference/runtime/python-packages"
if [ -d "$YOLO_PACKAGES" ]; then
  export PYTHONPATH="$ROOT/runtime:$YOLO_PACKAGES:${PYTHONPATH:-}"
else
  export PYTHONPATH="$ROOT/runtime:${PYTHONPATH:-}"
fi

exec python3 "$ROOT/runtime/fridge_pipeline.py" "$@"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_pipeline.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_web.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/fridge_db_env.sh"

exec python3 "$ROOT/runtime/fridge_web.py" "$@"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_web.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_pipeline_loop.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/fridge_db_env.sh"

interval="${SMART_FRIDGE_CAPTURE_INTERVAL_SECONDS:-3600}"
while true; do
  date -u '+pipeline_cycle_start=%Y-%m-%dT%H:%M:%SZ'
  "$ROOT/bin/fridge_pipeline.sh" --once || true
  date -u '+pipeline_cycle_end=%Y-%m-%dT%H:%M:%SZ'
  sleep "$interval"
done
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_pipeline_loop.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/start_pipeline.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/fridge-pipeline.pid"
LOG_FILE="$ROOT/logs/fridge-pipeline.log"
mkdir -p "$ROOT/run" "$ROOT/logs" "$ROOT/tmp"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "Smart-fridge pipeline already running with PID $(cat "$PID_FILE")"
  exit 0
fi

nohup "$ROOT/bin/fridge_pipeline_loop.sh" >> "$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" > "$PID_FILE"
echo "Started smart-fridge pipeline PID $pid; log: $LOG_FILE"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/start_pipeline.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/stop_pipeline.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/fridge-pipeline.pid"
if [ ! -f "$PID_FILE" ]; then
  echo "Smart-fridge pipeline is not running."
  exit 0
fi
pid="$(cat "$PID_FILE")"
if kill -0 "$pid" >/dev/null 2>&1; then
  kill "$pid"
  echo "Stopped smart-fridge pipeline PID $pid"
fi
rm -f "$PID_FILE"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/stop_pipeline.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/status_pipeline.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/fridge-pipeline.pid"
LOG_FILE="$ROOT/logs/fridge-pipeline.log"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "running PID $(cat "$PID_FILE")"
else
  echo "not running"
fi
if [ -f "$LOG_FILE" ]; then
  echo "--- last log lines ---"
  tail -n 40 "$LOG_FILE"
fi
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/status_pipeline.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/start_web.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/fridge_db_env.sh"

PID_FILE="$ROOT/run/fridge-web.pid"
LOG_FILE="$ROOT/logs/fridge-web.log"
mkdir -p "$ROOT/run" "$ROOT/logs"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "Smart-fridge web already running with PID $(cat "$PID_FILE")"
  exit 0
fi

nohup "$ROOT/bin/fridge_web.sh" --host "$SMART_FRIDGE_WEB_HOST" --port "$SMART_FRIDGE_WEB_PORT" >> "$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" > "$PID_FILE"
echo "Started smart-fridge web PID $pid; url: http://$(hostname -I | awk '{print $1}'):$SMART_FRIDGE_WEB_PORT/; log: $LOG_FILE"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/start_web.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/stop_web.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/run/fridge-web.pid"
if [ ! -f "$PID_FILE" ]; then
  echo "Smart-fridge web is not running."
  exit 0
fi
pid="$(cat "$PID_FILE")"
if kill -0 "$pid" >/dev/null 2>&1; then
  kill "$pid"
  echo "Stopped smart-fridge web PID $pid"
fi
rm -f "$PID_FILE"
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/stop_web.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/status_web.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$ROOT/bin/fridge_db_env.sh"

PID_FILE="$ROOT/run/fridge-web.pid"
LOG_FILE="$ROOT/logs/fridge-web.log"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "running PID $(cat "$PID_FILE")"
  echo "url http://$(hostname -I | awk '{print $1}'):$SMART_FRIDGE_WEB_PORT/"
else
  echo "not running"
fi
if [ -f "$LOG_FILE" ]; then
  echo "--- last log lines ---"
  tail -n 30 "$LOG_FILE"
fi
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/status_web.sh"

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$ROOT/bin/fridge_db.sh" --help >/dev/null
"$ROOT/bin/fridge_pipeline.sh" --help >/dev/null
"$ROOT/bin/fridge_web.sh" --help >/dev/null
"$ROOT/bin/fridge_db.sh" init >/dev/null
"$ROOT/bin/fridge_db.sh" health
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_check.sh"
REMOTE

scp -q "$RUNNER" "$HOST:$REMOTE_PATH/runtime/fridge_db.py"
scp -q "$PIPELINE" "$HOST:$REMOTE_PATH/runtime/fridge_pipeline.py"
scp -q "$WEB" "$HOST:$REMOTE_PATH/runtime/fridge_web.py"
scp -q "$PROMPT" "$HOST:$REMOTE_PATH/runtime/vlm_food_prompt.txt"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "chmod +x $REMOTE_PATH/runtime/fridge_db.py $REMOTE_PATH/runtime/fridge_pipeline.py $REMOTE_PATH/runtime/fridge_web.py && $REMOTE_PATH/bin/fridge_db_check.sh"
