#!/usr/bin/env bash
# Deploy Deltr to a VPS as a PUBLIC READ-ONLY showcase (paper mode, no exchange keys).
#
#   scripts/deploy_vps.sh user@host            # first run and every update
#   DOMAIN=deltr.example.com scripts/deploy_vps.sh user@host   # also configures TLS via certbot
#
# What it does on the box: rsyncs this tree to /opt/deltr (no .env, no state, no git),
# installs Python 3.11+ venv + deps, creates the `deltr` user, writes /etc/deltr.env with a
# freshly generated DELTR_API_TOKEN (printed ONCE, keep it), installs the systemd unit and
# nginx site, starts the service and verifies /api/health.
#
# It never copies your local .env and never enables testnet or live trading.
set -euo pipefail

TARGET="${1:?usage: deploy_vps.sh user@host}"
DOMAIN="${DOMAIN:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> rsync tree to $TARGET:/opt/deltr (excluding secrets, state, venv, git)"
ssh "$TARGET" 'sudo mkdir -p /opt/deltr && sudo chown "$USER" /opt/deltr'
rsync -az --delete \
  --exclude '.env' --exclude '.git' --exclude '.venv' --exclude 'node_modules' \
  --exclude 'state' --exclude '.next' --exclude '__pycache__' --exclude '.pytest_cache' \
  "$HERE/" "$TARGET:/opt/deltr/"

ssh "$TARGET" DOMAIN="$DOMAIN" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/deltr

# --- python 3.11+ --------------------------------------------------------------------
PY=""
for c in python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "no python >= 3.11 found; installing python3.11 (Debian/Ubuntu)"
  sudo apt-get update -qq && sudo apt-get install -y -qq python3.11 python3.11-venv >/dev/null
  PY=python3.11
fi
echo "==> using $PY ($($PY --version))"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.lock 2>/dev/null || .venv/bin/pip install -q -r requirements.txt

# --- service user, state dir, env file ------------------------------------------------
id -u deltr >/dev/null 2>&1 || sudo useradd --system --home /opt/deltr --shell /usr/sbin/nologin deltr
sudo mkdir -p /var/lib/deltr && sudo chown -R deltr:deltr /var/lib/deltr /opt/deltr
if [ ! -f /etc/deltr.env ]; then
  TOKEN="$(openssl rand -hex 24)"
  sudo tee /etc/deltr.env >/dev/null <<EOF
# Deltr public showcase. PAPER mode, no exchange keys on this box, mutations need the token.
DELTR_MODE=paper
DELTR_PUBLIC_READONLY=1
DELTR_API_TOKEN=$TOKEN
DELTR_API_PORT=8000
DELTR_STATE_DIR=/var/lib/deltr
EOF
  sudo chown root:deltr /etc/deltr.env && sudo chmod 0640 /etc/deltr.env
  echo "==> generated DELTR_API_TOKEN (shown once, keep it to drive the demo remotely): $TOKEN"
fi

# --- systemd + nginx -----------------------------------------------------------------
sudo cp deploy/deltr.service /etc/systemd/system/deltr.service
sudo systemctl daemon-reload
sudo systemctl enable --now deltr
sudo systemctl restart deltr

if ! command -v nginx >/dev/null 2>&1; then sudo apt-get install -y -qq nginx >/dev/null; fi
sudo cp deploy/nginx.conf /etc/nginx/sites-available/deltr
sudo ln -sf /etc/nginx/sites-available/deltr /etc/nginx/sites-enabled/deltr
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

if [ -n "${DOMAIN:-}" ]; then
  command -v certbot >/dev/null 2>&1 || sudo apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || echo "certbot failed; site stays on http"
fi

# --- verify --------------------------------------------------------------------------
sleep 4
echo "==> health:"; curl -s http://127.0.0.1:8000/api/health || true; echo
echo "==> DNS check from this box:"; getent hosts fapi.binance.com | head -1 || echo "  fapi.binance.com does not resolve here: set DNS to 1.1.1.1 in /etc/resolv.conf"
echo "==> mutation refused without token (expect 403):"; curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/kill -H 'content-type: application/json' -d '{"on":true}'
sudo systemctl --no-pager --lines=5 status deltr | tail -6
REMOTE

echo
echo "==> done. Open http://$(echo "$TARGET" | cut -d@ -f2)/  (landing)  and /app/ (dashboard)"
