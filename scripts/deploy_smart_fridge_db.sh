#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
SMART_FRIDGE_REMOTE_DIR="${SMART_FRIDGE_REMOTE_DIR:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$REPO_ROOT/smart_fridge_runtime/fridge_db.py"

if [ ! -f "$RUNNER" ]; then
  echo "Missing local runtime: $RUNNER" >&2
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
EOF

if [ ! -f "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env" ]; then
  cp "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env.example" "$SMART_FRIDGE_REMOTE_DIR/config/smart_fridge.env"
fi

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_env.sh" <<'EOF'
#!/usr/bin/env bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMART_FRIDGE_DB_PATH_OVERRIDE="${SMART_FRIDGE_DB_PATH-}"
SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES_OVERRIDE="${SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES-}"
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
export SMART_FRIDGE_ROOT="$ROOT"
: "${SMART_FRIDGE_DB_PATH:=$ROOT/data/fridge.sqlite3}"
: "${SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES:=120}"
export SMART_FRIDGE_DB_PATH SMART_FRIDGE_DUPLICATE_WINDOW_MINUTES
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

cat > "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_check.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$ROOT/bin/fridge_db.sh" --help >/dev/null
"$ROOT/bin/fridge_db.sh" init >/dev/null
"$ROOT/bin/fridge_db.sh" health
EOF
chmod +x "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_check.sh"
REMOTE

scp -q "$RUNNER" "$HOST:$REMOTE_PATH/runtime/fridge_db.py"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" \
  "chmod +x $REMOTE_PATH/runtime/fridge_db.py && $REMOTE_PATH/bin/fridge_db_check.sh"
