#!/bin/sh
# Install spa-control into a Proxmox LXC. Run as root, inside the container,
# from a copy of this repository. The Proxmox host has to create the CT and
# pass /dev/net/tun in first; see the README.
set -eu

if [ "$(uname -s)" != "Linux" ]; then
  echo "run this inside the Proxmox LXC, not on the workstation" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root inside the container" >&2
  exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3
fi
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || {
    echo "Python 3.11 or newer is required" >&2
    exit 1
  }

if ! id spa >/dev/null 2>&1; then
  useradd --system --home /var/lib/spa-control --shell /usr/sbin/nologin spa
fi
mkdir -p /opt/spa-control /var/lib/spa-control

# Reinstalling from this tree replaces the code, not the saved module address.
rm -rf /opt/spa-control/app /opt/spa-control/static
cp -a "$ROOT/app" "$ROOT/static" /opt/spa-control/
find /opt/spa-control -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
install -m 0644 "$ROOT/spa-control.service" /etc/systemd/system/spa-control.service
chown -R spa:spa /opt/spa-control /var/lib/spa-control

# Keep an existing env file. A reinstall must not replace the module address.
if [ ! -f /etc/spa-control.env ]; then
  if [ -f "$ROOT/.env" ]; then
    src="$ROOT/.env"
  else
    src="$ROOT/.env.example"
  fi
  install -m 0600 -o root -g root "$src" /etc/spa-control.env
  echo "Wrote /etc/spa-control.env from $(basename "$src"). Set SPA_HOST before relying on it."
fi

if [ -n "${SPA_TZ:-}" ]; then
  if [ ! -e "/usr/share/zoneinfo/$SPA_TZ" ]; then
    echo "unknown timezone: $SPA_TZ" >&2
    exit 1
  fi
  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "$SPA_TZ"
  else
    ln -sfn "/usr/share/zoneinfo/$SPA_TZ" /etc/localtime
    printf '%s\n' "$SPA_TZ" > /etc/timezone
  fi
fi

systemctl daemon-reload
systemctl enable spa-control
systemctl restart spa-control

echo
echo "spa-control is running. The page is http://<this-container>:8080"
echo "Settings live in /var/lib/spa-control/config.json after the first save."
if [ -z "${SPA_TZ:-}" ]; then
  echo "Set the container timezone before using \"Set spa clock\" (example: SPA_TZ=America/Los_Angeles $0)."
fi
echo "Tailscale, from this container, after the host has passed in /dev/net/tun:"
echo "  curl -fsSL https://tailscale.com/install.sh | sh && tailscale up"
