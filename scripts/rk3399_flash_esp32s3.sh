#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR="${1:-}"
RELEASE_ID="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERIAL_PORT="${ESP32_SERIAL_PORT:-auto}"
SENSOR_SERVICE="${ESP32_SENSOR_SERVICE:-smart-fridge-sensor.service}"
ESPTOOL_VERSION="4.5.1"
ESPTOOL_ROOT="$HOME/.local/share/smart-fridge-esptool"
ESPTOOL_PYTHON="$ESPTOOL_ROOT/venv-$ESPTOOL_VERSION/bin/python"
BACKUP_DIR="$PROJECT_ROOT/firmware/backups"
BACKUP_PATH="$BACKUP_DIR/$RELEASE_ID-full-flash.bin"
FLASH_BYTES="0x800000"
FLASH_BAUD="${ESP32_FLASH_BAUD:-460800}"

usage() {
  echo "Usage: $0 BUNDLE_DIR RELEASE_ID" >&2
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [ -z "$BUNDLE_DIR" ] || [ -z "$RELEASE_ID" ]; then
  usage
  exit 2
fi

case "$RELEASE_ID" in
  *[!A-Za-z0-9._-]*) fail "Invalid release id: $RELEASE_ID" ;;
esac

BUNDLE_DIR="$(cd "$BUNDLE_DIR" 2>/dev/null && pwd)" || fail "Bundle directory not found"

for command_name in flock python3 sha256sum systemctl; do
  command -v "$command_name" >/dev/null 2>&1 || fail "Missing command: $command_name"
done

for file_name in \
  SHA256SUMS \
  boot_app0.bin \
  smart_fridge_sensor_node.ino.bin \
  smart_fridge_sensor_node.ino.bootloader.bin \
  smart_fridge_sensor_node.ino.partitions.bin; do
  [ -f "$BUNDLE_DIR/$file_name" ] || fail "Missing bundle file: $file_name"
done

if [ "$SERIAL_PORT" = "auto" ]; then
  serial_candidates=(/dev/serial/by-id/*CP2102N*)
  if [ "${#serial_candidates[@]}" -ne 1 ] || [ ! -e "${serial_candidates[0]}" ]; then
    fail "Expected exactly one CP2102N serial device; set ESP32_SERIAL_PORT explicitly"
  fi
  SERIAL_PORT="${serial_candidates[0]}"
fi

if [ ! -x "$ESPTOOL_PYTHON" ]; then
  mkdir -p "$ESPTOOL_ROOT"
  python3 -m venv "$ESPTOOL_ROOT/venv-$ESPTOOL_VERSION" || {
    fail "Cannot create Python venv; install python3-venv first"
  }
  "$ESPTOOL_PYTHON" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    "esptool==$ESPTOOL_VERSION"
fi

"$ESPTOOL_PYTHON" -m esptool version | grep -F "$ESPTOOL_VERSION" >/dev/null || {
  fail "Unexpected esptool version"
}

(
  cd "$BUNDLE_DIR"
  sha256sum -c SHA256SUMS
)

[ -e "$SERIAL_PORT" ] || fail "Serial device not found: $SERIAL_PORT"
mkdir -p "$BACKUP_DIR" "$HOME/.cache"

exec 9>"$HOME/.cache/smart-fridge-esp32-flash.lock"
flock -n 9 || fail "Another ESP32 flash operation is running"

service_was_active=0
if systemctl --user is-active --quiet "$SENSOR_SERVICE"; then
  service_was_active=1
fi

restore_sensor_service() {
  if [ "$service_was_active" -eq 1 ]; then
    systemctl --user start "$SENSOR_SERVICE" || true
  else
    systemctl --user stop "$SENSOR_SERVICE" || true
  fi
}
trap restore_sensor_service EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

systemctl --user stop "$SENSOR_SERVICE" || true
sleep 1

probe_output="$({
  "$ESPTOOL_PYTHON" -m esptool \
    --chip esp32s3 \
    --port "$SERIAL_PORT" \
    --baud 115200 \
    flash_id
} 2>&1)"
printf '%s\n' "$probe_output"
printf '%s\n' "$probe_output" | grep -F "Chip is ESP32-S3" >/dev/null || {
  fail "Connected target is not an ESP32-S3"
}
printf '%s\n' "$probe_output" | grep -F "Detected flash size: 8MB" >/dev/null || {
  fail "Expected an 8MB flash chip"
}

backup_partial="$BACKUP_PATH.partial"
"$ESPTOOL_PYTHON" -m esptool \
  --chip esp32s3 \
  --port "$SERIAL_PORT" \
  --baud "$FLASH_BAUD" \
  --before default_reset \
  --after hard_reset \
  read_flash 0 "$FLASH_BYTES" "$backup_partial"
mv "$backup_partial" "$BACKUP_PATH"
(
  cd "$BACKUP_DIR"
  sha256sum "$(basename "$BACKUP_PATH")" > "$(basename "$BACKUP_PATH").sha256"
)

"$ESPTOOL_PYTHON" -m esptool \
  --chip esp32s3 \
  --port "$SERIAL_PORT" \
  --baud "$FLASH_BAUD" \
  --before default_reset \
  --after hard_reset \
  write_flash -z \
  --flash_mode dio \
  --flash_freq 80m \
  --flash_size detect \
  0x0 "$BUNDLE_DIR/smart_fridge_sensor_node.ino.bootloader.bin" \
  0x8000 "$BUNDLE_DIR/smart_fridge_sensor_node.ino.partitions.bin" \
  0xe000 "$BUNDLE_DIR/boot_app0.bin" \
  0x10000 "$BUNDLE_DIR/smart_fridge_sensor_node.ino.bin"

systemctl --user start "$SENSOR_SERVICE"
python3 - "$PROJECT_ROOT/data/sensor_state.json" <<'PY'
import json
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
deadline = time.monotonic() + 30
last_seq = None

while time.monotonic() < deadline:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        raw = state.get("raw") or {}
        seq = raw.get("seq")
        if state.get("connected") and raw.get("v") == 2 and isinstance(seq, int):
            if last_seq is not None and seq > last_seq:
                result = {
                    "connected": True,
                    "protocol_version": raw["v"],
                    "seq": seq,
                    "uptime_ms": raw.get("uptime_ms"),
                    "reported_door_state": raw.get("door_state"),
                    "corrected_door_state": (state.get("data") or {}).get("door_state"),
                    "door_mapping": state.get("door_mapping"),
                }
                print("SENSOR_CHECK=" + json.dumps(result, ensure_ascii=False))
                break
            last_seq = seq
    except (OSError, ValueError):
        pass
    time.sleep(1)
else:
    raise SystemExit("Sensor service did not produce two increasing v2 frames within 30 seconds")
PY

if [ "$service_was_active" -eq 0 ]; then
  systemctl --user stop "$SENSOR_SERVICE"
fi

ln -sfn "$BUNDLE_DIR" "$PROJECT_ROOT/firmware/current"
trap - EXIT INT TERM

printf 'FLASH_STATUS=ok\n'
printf 'SERIAL_PORT=%s\n' "$SERIAL_PORT"
printf 'RELEASE_REMOTE=%s\n' "$BUNDLE_DIR"
printf 'BACKUP_REMOTE=%s\n' "$BACKUP_PATH"
printf 'SERVICE_RESTORED=%s\n' "$service_was_active"
