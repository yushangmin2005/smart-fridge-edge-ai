#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FIRMWARE_DIR="${ESP32_FIRMWARE_DIR:-$HOME/Documents/Embedded/ESP32-S3/smart_fridge_sensor_node}"
ARDUINO_CLI="${ARDUINO_CLI:-/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli}"
ARDUINO15_DIR="${ARDUINO15_DIR:-$HOME/Library/Arduino15}"
ESP32_CORE_VERSION="2.0.17"
ESP32_CORE_DIR="$ARDUINO15_DIR/packages/esp32/hardware/esp32/$ESP32_CORE_VERSION"
BOOT_APP0="$ESP32_CORE_DIR/tools/partitions/boot_app0.bin"
FQBN="esp32:esp32:esp32s3"
REMOTE_PROJECT="${SMART_FRIDGE_REMOTE_PROJECT:-smart-fridge}"
RELEASE_ID="esp32s3-$(date -u +%Y%m%dT%H%M%SZ)"
BUILD_DIR=""
SSH_OPTIONS=(
  -o BatchMode=yes
  -o ConnectTimeout=8
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=3
)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

cleanup() {
  if [ -n "$BUILD_DIR" ] && [ -d "$BUILD_DIR" ]; then
    find "$BUILD_DIR" -depth -delete
  fi
}
trap cleanup EXIT INT TERM

case "$REMOTE_PROJECT" in
  *[!A-Za-z0-9._/-]*) fail "Invalid remote project path: $REMOTE_PROJECT" ;;
esac

for command_name in scp ssh shasum; do
  command -v "$command_name" >/dev/null 2>&1 || fail "Missing command: $command_name"
done

[ -x "$ARDUINO_CLI" ] || fail "Arduino CLI not found: $ARDUINO_CLI"
[ -d "$FIRMWARE_DIR" ] || fail "Firmware directory not found: $FIRMWARE_DIR"
[ -d "$ESP32_CORE_DIR" ] || fail "ESP32 Arduino core $ESP32_CORE_VERSION is not installed"
[ -f "$BOOT_APP0" ] || fail "boot_app0.bin not found: $BOOT_APP0"

BUILD_DIR="$(mktemp -d /tmp/smart-fridge-esp32s3.XXXXXX)"
BUNDLE_DIR="$BUILD_DIR/bundle"
mkdir -p "$BUNDLE_DIR"

echo "Compiling $FIRMWARE_DIR with $FQBN and core $ESP32_CORE_VERSION"
"$ARDUINO_CLI" compile \
  --fqbn "$FQBN" \
  --output-dir "$BUILD_DIR/output" \
  "$FIRMWARE_DIR"

install -m 0644 \
  "$BUILD_DIR/output/smart_fridge_sensor_node.ino.bin" \
  "$BUILD_DIR/output/smart_fridge_sensor_node.ino.bootloader.bin" \
  "$BUILD_DIR/output/smart_fridge_sensor_node.ino.partitions.bin" \
  "$BOOT_APP0" \
  "$BUNDLE_DIR/"

(
  cd "$BUNDLE_DIR"
  shasum -a 256 \
    boot_app0.bin \
    smart_fridge_sensor_node.ino.bin \
    smart_fridge_sensor_node.ino.bootloader.bin \
    smart_fridge_sensor_node.ino.partitions.bin \
    > SHA256SUMS
)

REMOTE_RELEASE="$REMOTE_PROJECT/firmware/releases/$RELEASE_ID"
REMOTE_HELPER="$REMOTE_PROJECT/bin/rk3399_flash_esp32s3.sh"
REMOTE_BACKUP="$REMOTE_PROJECT/firmware/backups/$RELEASE_ID-full-flash.bin"

ssh "${SSH_OPTIONS[@]}" "$HOST" \
  "mkdir -p '$REMOTE_RELEASE' '$REMOTE_PROJECT/bin' '$REMOTE_PROJECT/firmware/backups'"
scp -q "${SSH_OPTIONS[@]}" "$SCRIPT_DIR/rk3399_flash_esp32s3.sh" "$HOST:$REMOTE_HELPER"
scp -q "${SSH_OPTIONS[@]}" "$BUNDLE_DIR/"* "$HOST:$REMOTE_RELEASE/"
ssh "${SSH_OPTIONS[@]}" "$HOST" "chmod 0755 '$REMOTE_HELPER'"

ssh "${SSH_OPTIONS[@]}" "$HOST" \
  "'$REMOTE_HELPER' '$REMOTE_RELEASE' '$RELEASE_ID'"

LOCAL_BACKUP_DIR="$REPO_ROOT/artifacts/esp32-backups"
mkdir -p "$LOCAL_BACKUP_DIR"
scp -q "${SSH_OPTIONS[@]}" "$HOST:$REMOTE_BACKUP" "$LOCAL_BACKUP_DIR/"
scp -q "${SSH_OPTIONS[@]}" "$HOST:$REMOTE_BACKUP.sha256" "$LOCAL_BACKUP_DIR/"

(
  cd "$LOCAL_BACKUP_DIR"
  shasum -a 256 -c "$RELEASE_ID-full-flash.bin.sha256"
)

printf 'LOCAL_BACKUP=%s\n' "$LOCAL_BACKUP_DIR/$RELEASE_ID-full-flash.bin"
printf 'RELEASE_ID=%s\n' "$RELEASE_ID"
