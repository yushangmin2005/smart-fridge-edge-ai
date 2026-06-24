#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"

ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" 'set -u
printf "HOSTNAME="; hostname
printf "UNAME="; uname -a
printf "ARCH="; uname -m
printf "OS="; if [ -r /etc/os-release ]; then . /etc/os-release; printf "%s %s\n" "${PRETTY_NAME:-unknown}" "${VERSION_ID:-}"; else printf "unknown\n"; fi
printf "CPU_CORES="; nproc
printf "MEMORY="; free -h | awk "/Mem:/ {print \$2}"
printf "DISK_HOME="; df -h "$HOME" | awk "NR==2 {print \$4 \" free of \" \$2}"
for c in git cmake make gcc g++ curl wget tar python3 systemctl sudo docker podman nvidia-smi; do
  printf "%s=" "$c"
  if command -v "$c" >/dev/null 2>&1; then command -v "$c"; else printf "not-found\n"; fi
done
printf "SUDO_NONINTERACTIVE="; sudo -n true >/dev/null 2>&1 && echo yes || echo no
'
