#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-firecar-pi}"

ssh -tt -o ConnectTimeout=8 "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail

sudo -v
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y gpiod libgpiod-dev i2c-tools

for group in gpio i2c; do
  if ! getent group "$group" >/dev/null 2>&1; then
    sudo groupadd -r "$group"
  fi
done
sudo usermod -aG gpio,i2c pi

sudo tee /etc/udev/rules.d/90-smart-fridge-peripherals.rules >/dev/null <<'EOF_RULES'
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"
EOF_RULES

sudo udevadm control --reload-rules || true
sudo udevadm trigger --subsystem-match=gpio || true
sudo udevadm trigger --subsystem-match=i2c-dev || true
sudo chgrp gpio /dev/gpiochip* 2>/dev/null || true
sudo chmod 0660 /dev/gpiochip* 2>/dev/null || true
sudo chgrp i2c /dev/i2c-* 2>/dev/null || true
sudo chmod 0660 /dev/i2c-* 2>/dev/null || true

echo "installed_tools:"
command -v gpioinfo
command -v gpioget
command -v gpioset
command -v i2cdetect
command -v i2cget
command -v i2cset
echo "pi_groups:"
id pi
echo "device_permissions:"
ls -l /dev/gpiochip* /dev/i2c-* 2>/dev/null | sed -n '1,80p'
REMOTE
