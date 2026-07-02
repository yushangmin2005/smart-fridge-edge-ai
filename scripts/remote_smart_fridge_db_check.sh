#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
SMART_FRIDGE_REMOTE_DIR="${SMART_FRIDGE_REMOTE_DIR:-~/smart-fridge}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "$SMART_FRIDGE_REMOTE_DIR/bin/fridge_db_check.sh"
