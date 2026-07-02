#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
YOLO_REMOTE_DIR="${YOLO_REMOTE_DIR:-~/yolo-inference}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "$YOLO_REMOTE_DIR/bin/yolo_check.sh"
