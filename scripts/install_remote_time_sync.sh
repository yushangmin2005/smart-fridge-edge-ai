#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"
REMOTE_SCRIPT="/tmp/smart-fridge-install-time-sync.sh"
LOCAL_SCRIPT="$(mktemp)"
trap 'rm -f "$LOCAL_SCRIPT"' EXIT

cat > "$LOCAL_SCRIPT" <<'REMOTE'
#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "This installer must run as root." >&2
  exit 1
fi

install -d -m 0755 /etc/systemd/timesyncd.conf.d
cat > /etc/systemd/timesyncd.conf.d/smart-fridge.conf <<'EOF_TIMESYNCD'
[Time]
NTP=162.159.200.1 162.159.200.123 216.239.35.0 216.239.35.1 216.239.35.2 216.239.35.3 203.107.6.88
FallbackNTP=ntp.ubuntu.com
RootDistanceMaxSec=10
PollIntervalMinSec=32
PollIntervalMaxSec=2048
EOF_TIMESYNCD

cat > /usr/local/sbin/smart-fridge-http-time-sync <<'EOF_SYNC'
#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C
STATE_DIR="/var/lib/smart-fridge"
STATE_FILE="$STATE_DIR/time-sync-state"
mkdir -p "$STATE_DIR"

SOURCES=(
  "https://www.baidu.com"
  "https://www.cloudflare.com"
  "http://www.baidu.com"
)

write_state() {
  local ok="$1"
  local source="$2"
  local server_time="$3"
  local skew="$4"
  local adjusted="$5"
  local message="$6"
  local tmp
  tmp="$(mktemp "$STATE_DIR/time-sync-state.XXXXXX")"
  {
    printf 'ok=%s\n' "$ok"
    printf 'source=%s\n' "$source"
    printf 'server_time_utc=%s\n' "$server_time"
    printf 'skew_seconds=%s\n' "$skew"
    printf 'adjusted=%s\n' "$adjusted"
    printf 'checked_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'message=%s\n' "$message"
  } > "$tmp"
  mv "$tmp" "$STATE_FILE"
  chmod 0644 "$STATE_FILE"
}

for url in "${SOURCES[@]}"; do
  headers="$(curl -k -fsSI --connect-timeout 5 --max-time 10 "$url" 2>/dev/null || true)"
  date_header="$(
    printf '%s\n' "$headers" |
      tr -d '\r' |
      awk '{ line=$0; if (tolower(line) ~ /^date:/) { sub(/^[^:]+:[[:space:]]*/, "", line); print line; exit } }'
  )"
  if [ -z "$date_header" ]; then
    continue
  fi

  if ! server_epoch="$(date -u -d "$date_header" +%s 2>/dev/null)"; then
    continue
  fi

  now_epoch="$(date -u +%s)"
  skew="$((server_epoch - now_epoch))"
  abs_skew="${skew#-}"
  server_iso="$(date -u -d "@$server_epoch" +%Y-%m-%dT%H:%M:%SZ)"
  adjusted="0"

  if [ "$abs_skew" -gt 2 ]; then
    date -u -s "@$server_epoch" >/dev/null
    adjusted="1"
  fi

  hwclock --systohc --utc >/dev/null 2>&1 || true
  write_state "1" "$url" "$server_iso" "$skew" "$adjusted" "synced"
  echo "time_sync_ok source=$url server_time=$server_iso skew_seconds=$skew adjusted=$adjusted"
  exit 0
done

write_state "0" "none" "" "" "0" "no usable Date header"
echo "time_sync_failed no usable Date header" >&2
exit 1
EOF_SYNC
chmod 0755 /usr/local/sbin/smart-fridge-http-time-sync

cat > /etc/systemd/system/smart-fridge-http-time-sync.service <<'EOF_SERVICE'
[Unit]
Description=Smart Fridge HTTPS time sync fallback
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/smart-fridge-http-time-sync
EOF_SERVICE

cat > /etc/systemd/system/smart-fridge-http-time-sync.timer <<'EOF_TIMER'
[Unit]
Description=Run Smart Fridge HTTPS time sync fallback

[Timer]
OnBootSec=45s
OnUnitActiveSec=30min
AccuracySec=1min
Persistent=true
Unit=smart-fridge-http-time-sync.service

[Install]
WantedBy=timers.target
EOF_TIMER

timedatectl set-timezone Asia/Shanghai
timedatectl set-ntp true || true
systemctl daemon-reload
systemctl restart systemd-timesyncd || true
systemctl enable --now smart-fridge-http-time-sync.timer
systemctl start smart-fridge-http-time-sync.service

echo "timedatectl:"
timedatectl
echo "http_time_state:"
cat /var/lib/smart-fridge/time-sync-state
echo "http_time_timer:"
systemctl list-timers --all --no-pager | grep smart-fridge-http-time-sync || true
REMOTE

scp -q -o ConnectTimeout=8 "$LOCAL_SCRIPT" "$HOST:$REMOTE_SCRIPT"
ssh -tt -o ConnectTimeout=8 "$HOST" "chmod 700 '$REMOTE_SCRIPT' && sudo -S -p '[sudo] password: ' bash '$REMOTE_SCRIPT'; status=\$?; rm -f '$REMOTE_SCRIPT'; exit \$status"
