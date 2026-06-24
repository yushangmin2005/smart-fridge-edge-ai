#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
VLM_REMOTE_DIR="${VLM_REMOTE_DIR:-~/vlm-inference}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "$VLM_REMOTE_DIR/bin/runtime_check.sh"
