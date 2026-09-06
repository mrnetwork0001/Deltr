# VPS runbook: the public read-only showcases (PAPER and LIVE)

Goal: judges open <https://usedeltrapp.vercel.app>, press **Launch App**, and get a live dashboard
with a **PAPER / LIVE** switch in the header. Each engine is its own process on the VPS, both public
read-only: anyone can read, every mutation needs that engine's `DELTR_API_TOKEN` (printed once).
Vercel proxies `/api`, `/mcp` to the PAPER engine (port 8000) and `/live/api`, `/live/mcp` to the LIVE
engine (port 8001); the switch only changes which engine the page reads. Nothing on the page, and no
MCP tool, can change a mode.

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

## The LIVE instance (real money, read-only to the public)

Install it disarmed, then arm it yourself on the box:

```bash
cd /opt/deltr && INSTANCE=live PORT=8001 bash scripts/vps_install.sh
```

The installer writes `/etc/deltr-live.env` with `DELTR_MODE=live` and blank credentials, installs
`deltr-live.service`, and does **not** start it. To arm:

1. `nano /etc/deltr-live.env`: fill `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` (mainnet key, Reading +
   Futures, no withdrawals, IP-restricted to the VPS), uncomment `DELTR_LIVE_ACK` and `DELTR_ONCHAIN_ACK`.
2. Wallet CLI on the box, signed in **as the service user** so the session lives in its state dir:
   ```bash
   command -v baw || npm install -g @binance/agentic-wallet
   sudo -u deltr HOME=/var/lib/deltr-live baw auth signin        # scan the QR in the Binance app
   sudo -u deltr HOME=/var/lib/deltr-live baw wallet status --json
   ```
3. Re-run `INSTANCE=live PORT=8001 bash scripts/vps_install.sh`; it starts the service once the env
   file is armed. The engine's preflight re-checks keys, acknowledgements and the wallet session before
   the first tick and refuses to start otherwise (`journalctl -u deltr-live -n 50` names what is missing).

Caps: $250 per trade, $1,000 aggregate, on-chain $250 per request. Fund the Futures wallet with about
$90 USDT margin and the Agentic Wallet with a little BNB and USDT for one $200 trade at 2x.

## What the installer does (and does not touch)

Built for a box that already runs other services. It creates only namespaced things: user `deltr`,
`/opt/deltr/.venv`, and per instance `/var/lib/<name>`, `/etc/<name>.env`, `<name>.service` (`deltr`, `deltr-live`). It never touches existing
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

## How Vercel reaches the engines

`vercel.json` rewrites `/api/*`, `/mcp`, `/ws/*` to `http://38.49.213.208:8000` and `/live/api/*`,
`/live/mcp`, `/live/ws/*` to port 8001, so the browser only ever talks to `usedeltrapp.vercel.app`
over https (an https page cannot call an http address directly). WebSockets do not pass the rewrite,
so the dashboard runs on its 1 Hz poll fallback there. `next.config.js` sets
`NEXT_PUBLIC_ENGINES=paper=|live=/live` on Vercel, which is what renders the header switch; the copy
FastAPI serves on the VPS itself has one engine and no switch.

## Wire the Vercel landing page to it (only without the rewrites)

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

PAPER: `claude mcp add deltr --transport http https://usedeltrapp.vercel.app/mcp`
LIVE: `claude mcp add deltr-live --transport http https://usedeltrapp.vercel.app/live/mcp`
(or the raw `http://38.49.213.208:8000/mcp` and `:8001/mcp`). Read tools work for everyone; the mutating
tools answer `READ_ONLY` unless the `X-Deltr-Token` header carries that engine's token.
