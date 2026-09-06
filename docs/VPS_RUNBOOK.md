# VPS runbook: the public read-only showcase

Goal: judges open the landing page on Vercel, press **Launch App**, and land on a live Deltr
dashboard served from the VPS in PAPER mode with real mainnet data. No exchange keys on the box;
every mutation needs the `DELTR_API_TOKEN` the installer prints once.

Requirements: Ubuntu 22.04/24.04 or Debian 12, 1 vCPU, 1 GB RAM, port 22 plus the chosen app port open
(default 8000). Commands are typed on the VPS in Termius unless marked "on the Mac".

## Path A: the repo is public (simplest)

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/mrnetwork0001/Deltr.git /opt/deltr
cd /opt/deltr && sudo bash scripts/vps_install.sh
```

## Path B: the repo is still private (rsync from the Mac, no token on the box)

On the Mac, once SSH to the box works (`ssh user@VPS_IP`):

```bash
scripts/deploy_vps.sh user@VPS_IP
```

It rsyncs the tree to `/opt/deltr` (never `.env`, never `state/`) and runs the same installer.

## What the installer does (and does not touch)

Built for a box that already runs other services. It creates only namespaced things: user `deltr`,
`/opt/deltr/.venv`, `/var/lib/deltr`, `/etc/deltr.env`, `deltr.service`. It never touches existing
nginx sites, port 80/443, the system DNS, apt sources, the system python or any other unit.

* Preflight: aborts if the chosen `PORT` (default 8000) is already in use; pick another with
  `sudo PORT=8010 bash scripts/vps_install.sh`.
* Python 3.11+ from what the box already has (a venv under `/opt/deltr`), else the distro package.
* `/etc/deltr.env` with `DELTR_MODE=paper`, `DELTR_PUBLIC_READONLY=1`, a random `DELTR_API_TOKEN`
  (printed ONCE, save it), the port, and `DELTR_CORS_ORIGINS=https://usedeltrapp.vercel.app`.
* DNS check for `fapi.binance.com`: report only. If it fails, fix the resolver yourself.
* systemd unit `deltr` bound to `0.0.0.0:PORT` (restart always, hardened); `ufw allow PORT` if ufw is active.
* Verifies `/api/health` and that a token-less `POST /api/kill` gets 403.

Re-run the installer after every `git pull` (or every `deploy_vps.sh`); it keeps `/etc/deltr.env`.

## Wire the Vercel landing page to it

The Vercel site is static and has no backend, so its **Launch App** button must point at the VPS.
On Vercel: Project → Settings → Environment Variables → add

```
NEXT_PUBLIC_APP_URL = http://38.49.213.208:PORT/app/
```

then Deployments → Redeploy. With a domain and TLS (below) use `https://your.domain/app/`.

Do NOT set `NEXT_PUBLIC_API` to a plain `http://` address: the Vercel page is `https`, and browsers
block mixed-content API calls. `NEXT_PUBLIC_API` only makes sense once the VPS has TLS.

## Optional: domain + TLS

Only if nginx is already the web server on this box. Point an A record at the VPS, then:

```bash
cd /opt/deltr && sudo DOMAIN=your.domain bash scripts/vps_install.sh
```

It adds one server block for that name only (existing sites untouched, config test before reload)
and runs certbot for it. After that `NEXT_PUBLIC_API=https://your.domain` on Vercel makes the Vercel
dashboard itself live. If the box runs Apache or Caddy instead, add the reverse proxy to port `PORT` there.

## Useful commands on the box

```bash
sudo systemctl status deltr          # running?
journalctl -u deltr -f               # live log (banner, ticks, gate decisions)
curl -s http://127.0.0.1:8000/api/health
sudo nano /etc/deltr.env && sudo systemctl restart deltr
```

## Judges' MCP access

`claude mcp add deltr --transport http http://38.49.213.208:PORT/mcp` (or the https URL). Read tools work for
everyone; the mutating tools answer `READ_ONLY` unless the `X-Deltr-Token` header carries the token.
