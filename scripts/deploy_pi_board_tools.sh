#!/usr/bin/env bash
set -euo pipefail

REMOTE="${1:-firecar-pi}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCAL_EXTENSION="${ROOT_DIR}/pi_extensions/firecar_board_tools.ts"
REMOTE_EXTENSION_DIR="${PI_BOARD_TOOLS_REMOTE_DIR:-~/.pi/agent/extensions}"
REMOTE_EXTENSION_PATH="${REMOTE_EXTENSION_DIR}/firecar-board-tools.ts"

if [[ ! -f "${LOCAL_EXTENSION}" ]]; then
  echo "Missing extension: ${LOCAL_EXTENSION}" >&2
  exit 1
fi

ssh "${REMOTE}" "mkdir -p ${REMOTE_EXTENSION_DIR}"
scp "${LOCAL_EXTENSION}" "${REMOTE}:${REMOTE_EXTENSION_PATH}"

ssh "${REMOTE}" "bash -lc '
set -euo pipefail
test -s ~/.pi/agent/extensions/firecar-board-tools.ts
pi --version >/dev/null
pi --extension ~/.pi/agent/extensions/firecar-board-tools.ts --offline --help >/dev/null
printf \"deployed=%s\n\" ~/.pi/agent/extensions/firecar-board-tools.ts
'"
