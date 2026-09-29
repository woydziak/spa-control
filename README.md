# Spa Control

A small local web service that talks to a Balboa / Master Spa Wi-Fi module on **TCP 4257** and serves a phone-friendly UI over **Tailscale**.

It does **not** use Balboa cloud and it does **not** depend on UDP discovery the way the official BWA app does. After the first save it reconnects to the configured `host:port` every time — including when you are off the home LAN and on Tailscale.

The module address is not in the source tree. Copy `.env.example` to `.env` (or `/etc/spa-control.env` on the container) and set `SPA_HOST`. The Balboa control port is `4257`.

## Why this exists

Balboa now charges for BWA cloud connection. Remote control from outside the house used to be included with the Wi-Fi module; it is a paid subscription, and without it the official app only connects on the same network as the spa. This service is the workaround. A machine on the home LAN holds the connection to the module, and Tailscale is how the phone reaches the page when you are away. No Balboa account.

Local Connect has a separate failure. It broadcasts UDP `255.255.255.255:30303` on every launch and treats the reply source IP as the spa. A VPN (Proton on iOS especially) eats that broadcast, so the stock app reports “not found” even when the module answers ping and TCP 4257.

The service keeps a single TCP session to the module on port 4257 and serves the page on the container’s Tailscale address. The phone only needs Tailscale, not LAN broadcasts.

## What you can do from the phone

- Live water temperature and set point
- Raise / lower set temperature
- Toggle pumps. A two-speed pump steps off → speed 1 → speed 2 → off; a one-speed pump is on or off. Pump 1 defaults to two-speed.
- Lights, blower, heat mode (Ready / Rest), high/low range, hold
- Spa clock, as reported by the panel
- Push the spa clock to the container’s local time
- Change the persisted module IP without rebuilding

## Run on a Proxmox LXC

This is the supported install: one unprivileged container on the Proxmox node, bridged onto the same LAN as the spa module, with Tailscale inside the container for access away from home. Docker is optional and not used on the node.

The container needs to open TCP to the module on port 4257, so give it a vNIC on the LAN bridge (usually `vmbr0`), not a NAT-only network. One core, 512 MB RAM, and 4 GB of disk are enough. Debian 12 or 13 is plenty; Python 3.11+ comes from the distro and there are no pip packages.

On the Proxmox host, after the CT exists and before Tailscale will come up, pass in the tunnel device and start the container:

```bash
pct set <CTID> --dev0 /dev/net/tun
pct set <CTID> --features nesting=1,keyctl=1
pct start <CTID>
```

Copy this directory into the container (a shared mount, `pct push`, or `scp`), then inside the CT:

```bash
# SPA_TZ is the clock "Set spa clock" writes. Use the zone the tub lives in.
SPA_TZ=America/Los_Angeles sh scripts/install-lxc.sh
```

The script creates the `spa` user, installs the unit, and restarts `spa-control`. On first install it copies `.env` if that file was shipped with the tree, otherwise `.env.example`, to `/etc/spa-control.env` (mode `0600`, root only). systemd reads that file before the process drops to the `spa` user. Re-run the script to update the code. It does not replace an existing env file or `/var/lib/spa-control/config.json`.

Then join the tailnet from inside the CT (`curl -fsSL https://tailscale.com/install.sh | sh && tailscale up`). Open:

```
http://<container-lan-or-tailscale-ip>:8080
```

Add a smartphone bookmark / “Add to Home Screen”. No app store, no Balboa account.

The unit binds `0.0.0.0:8080`, so the LAN address and the Tailscale address both work. Do not publish 8080 on the public internet. Tailscale plus the home LAN is the access control. A PIN is not enforced yet.

### Network checklist

- Container can `ping` the module address from `SPA_HOST`
- Container can `nc -vz <module> 4257`
- Phone can reach the container over Tailscale when you are away
- Give the module a DHCP reservation so the saved IP stays valid
- Leave official BWA alone if you still want it on the home Wi-Fi; this service does not take exclusive ownership of 4257, but two simultaneous TCP clients can confuse some modules. Prefer one controller at a time.

## Docker (optional)

If the LXC is just a Docker host:

```bash
docker build -t spa-control .
docker run -d --name spa-control --restart unless-stopped \
  --network host \
  --env-file .env \
  -v spa-control-data:/var/lib/spa-control \
  spa-control
```

`--network host` is the simple path so the container can open TCP 4257 on the LAN and serve :8080 on Tailscale.

## Configuration

Copy `.env.example` to `.env`. A systemd install reads `/etc/spa-control.env` instead. Variables already exported in the shell win over the file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPA_HOST` | unset (required) | Wi-Fi module address |
| `SPA_PORT` | `4257` | Balboa control port |
| `SPA_MAC` | empty | Expected module MAC. Optional |
| `SPA_LABEL` | `Spa` | Name shown in the page header |
| `SPA_BIND` | `0.0.0.0` | HTTP bind |
| `SPA_HTTP_PORT` | `8080` | HTTP port |
| `SPA_CONFIG` | `/var/lib/spa-control/config.json` | Persisted settings |
| `SPA_PIN` | empty | Stored only. Not checked yet |
| `SPA_MOCK` | empty | `1` to run the UI without hardware |
| `SPA_ENV` | `.env`, else `/etc/spa-control.env` | Override which env file is read |

The web UI Settings panel writes the same JSON. Saving always reconnects by **configured IP**. The Scan button is an optional UDP 30303 fallback and will not overwrite the saved address by itself.

## Protocol notes

- Discover (optional): UDP broadcast `255.255.255.255:30303`, reply `BWGSPA` + MAC, OUI `00:15:27`
- Control: TCP `host:4257`
- Frame: `0x7E` … CRC-8 … `0x7E`
- Status: type `FF AF 13`, about once a second
- Commands: `0A BF 11` toggle, `0A BF 20` set temperature

This client never depends on a LAN hostname and never requires a successful broadcast to operate.

## Local try-out (no spa)

```bash
cp .env.example .env
SPA_MOCK=1 SPA_CONFIG=./data/config.json SPA_HTTP_PORT=8080 python3 -m app.main
```

Open http://127.0.0.1:8080 — pumps, lights, and set-temp work against the in-process mock.

## Publishing

The code is under the MIT License. `.env.example` is the file to commit. `.env`, `data/`, and the container's `config.json` hold the module address and any PIN, and `.gitignore` keeps the first two out of git. Do not publish port 8080 to the internet. Nothing in the page checks the PIN yet.

## Out of scope

- Balboa cloud / subscription
- Teaching official BWA a static IP (it will not remember one)
- 5 GHz or guest Wi-Fi for the module
- Acting as a 30303 proxy so stock BWA works across VLANs — use sailorfrag/balboa-proxy for that if you still need the official app
