#!/usr/bin/env bash
# Run ON the VPS (Debian/Ubuntu) after the tree is at /opt/deltr (git clone, or deploy_vps.sh):
#
#   cd /opt/deltr && sudo bash scripts/vps_install.sh
#
# Options (environment):
#   PORT=8000                       the port Deltr listens on (aborts if something else already uses it)
#   CORS=https://usedeltrapp.vercel.app   allowed browser origin for the API
#   DOMAIN=deltr.example.com        add an nginx server block + certbot for this name (nginx must already exist)
#
# Designed for a box that ALREADY runs other services. It never touches: existing nginx sites, the
# default site, port 80/443 (unless DOMAIN is set, and then only its own server block), the system
# DNS, apt sources / PPAs, the system python, or any unit other than `deltr`. Everything it creates is
# namespaced: user `deltr`, /opt/deltr/.venv, /var/lib/deltr, /etc/deltr.env, deltr.service.
#
# Installs a PUBLIC READ-ONLY showcase: paper mode, no exchange keys, real mainnet data, mutations
# only with the generated DELTR_API_TOKEN (printed once). Re-run after `git pull`.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd /opt/deltr
PORT="${PORT:-8000}"
CORS="${CORS:-https://usedeltrapp.vercel.app}"
DOMAIN="${DOMAIN:-}"

echo "==> preflight"
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
  if ! systemctl is-active --quiet deltr 2>/dev/null; then
    echo "    port ${PORT} is already in use by another service on this box:"; ss -ltnp 2>/dev/null | grep -E "[:.]${PORT} " || true
    echo "    re-run with a free port, e.g.:  sudo PORT=8010 bash scripts/vps_install.sh"; exit 1
  fi
  echo "    port ${PORT} is held by the existing deltr service (update run)"
fi
if [ -f /etc/deltr.env ]; then
  OLD_PORT="$(grep -E '^DELTR_API_PORT=' /etc/deltr.env | cut -d= -f2 || true)"
  if [ -n "$OLD_PORT" ] && [ "$OLD_PORT" != "$PORT" ]; then echo "    /etc/deltr.env has DELTR_API_PORT=$OLD_PORT; using that (edit the file to change it)"; PORT="$OLD_PORT"; fi
fi
echo "    port ${PORT} ok"
IP="$(curl -s -4 --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
MCP_HOSTS="${IP}:${PORT}"; [ -n "$DOMAIN" ] && MCP_HOSTS="${MCP_HOSTS},${DOMAIN}:*"

echo "==> python 3.11+ (system python is left alone; a venv is created under /opt/deltr)"
PY=""
for c in python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "    no python >= 3.11 found; trying the distro package (no PPA is added)"
  apt-get update -qq && apt-get install -y -qq python3.11 python3.11-venv >/dev/null 2>&1 && PY=python3.11 || {
    echo "    python3.11 is not in this distro's repositories. Install it yourself (e.g. from deadsnakes or pyenv) and re-run."; exit 1; }
fi
"$PY" -c 'import venv, ensurepip' 2>/dev/null || { apt-get update -qq && apt-get install -y -qq "${PY}-venv" >/dev/null 2>&1 || true; }
echo "    using $PY ($($PY --version))"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.lock 2>/dev/null || .venv/bin/pip install -q -r requirements.txt

echo "==> service user + state dir"
id -u deltr >/dev/null 2>&1 || useradd --system --home /opt/deltr --shell /usr/sbin/nologin deltr
mkdir -p /var/lib/deltr && chown -R deltr:deltr /var/lib/deltr /opt/deltr

echo "==> /etc/deltr.env"
if [ ! -f /etc/deltr.env ]; then
  TOKEN="$(openssl rand -hex 24)"
  cat > /etc/deltr.env <<ENV
# Deltr public showcase. PAPER mode, no exchange keys on this box. Mutations need the token.
DELTR_MODE=paper
DELTR_PUBLIC_READONLY=1
DELTR_API_TOKEN=$TOKEN
DELTR_API_PORT=$PORT
DELTR_STATE_DIR=/var/lib/deltr
DELTR_CORS_ORIGINS=$CORS
# Host headers the MCP endpoint answers on (loopback is always allowed)
DELTR_MCP_ALLOWED_HOSTS=$MCP_HOSTS
ENV
  chown root:deltr /etc/deltr.env && chmod 0640 /etc/deltr.env
  echo
  echo "    ############################################################"
  echo "    #  DELTR_API_TOKEN (shown once, save it):                  #"
  echo "    #  $TOKEN"
  echo "    ############################################################"
  echo
else
  grep -q '^DELTR_CORS_ORIGINS=' /etc/deltr.env || echo "DELTR_CORS_ORIGINS=$CORS" >> /etc/deltr.env
  grep -q '^DELTR_API_PORT=' /etc/deltr.env || echo "DELTR_API_PORT=$PORT" >> /etc/deltr.env
  grep -q '^DELTR_MCP_ALLOWED_HOSTS=' /etc/deltr.env || echo "DELTR_MCP_ALLOWED_HOSTS=$MCP_HOSTS" >> /etc/deltr.env
  echo "    kept existing /etc/deltr.env"
fi

echo "==> DNS check (report only; this script never changes the box's resolver)"
if getent hosts fapi.binance.com >/dev/null; then echo "    fapi.binance.com resolves: ok"; else
  echo "    WARNING: fapi.binance.com does not resolve on this box. Deltr will start but the perp feed"
  echo "    will read 'DNS ... does not resolve'. Fix the resolver yourself (e.g. add 'DNS=1.1.1.1' in"
  echo "    /etc/systemd/resolved.conf) only if that is safe for the other services here."; fi

echo "==> systemd unit deltr (listens on 0.0.0.0:${PORT})"
cp deploy/deltr.service /etc/systemd/system/deltr.service
systemctl daemon-reload
systemctl enable --now deltr
systemctl restart deltr

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null && echo "==> ufw: allowed ${PORT}/tcp"
fi

if [ -n "$DOMAIN" ]; then
  echo "==> nginx server block for ${DOMAIN} (existing sites untouched)"
  command -v nginx >/dev/null 2>&1 || { echo "    nginx is not installed; skipping. Point ${DOMAIN} at port ${PORT} with the web server you already run."; DOMAIN=""; }
fi
if [ -n "$DOMAIN" ]; then
  sed -e "s/server_name _;/server_name ${DOMAIN};/" -e "s#127.0.0.1:8000#127.0.0.1:${PORT}#g" deploy/nginx.conf > /etc/nginx/sites-available/deltr
  mkdir -p /etc/nginx/sites-enabled && ln -sf /etc/nginx/sites-available/deltr /etc/nginx/sites-enabled/deltr
  if nginx -t; then systemctl reload nginx; else echo "    nginx config test failed; removing the deltr site"; rm -f /etc/nginx/sites-enabled/deltr; fi
  if [ -e /etc/nginx/sites-enabled/deltr ]; then
    command -v certbot >/dev/null 2>&1 || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || echo "    certbot failed; site stays on http"
  fi
fi

echo "==> verify"
sleep 4
echo -n "    health: "; curl -s "http://127.0.0.1:${PORT}/api/health" || echo "(no answer yet; check: journalctl -u deltr -n 50)"; echo
echo -n "    token-less mutation (expect 403): "; curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1:${PORT}/api/kill" -H 'content-type: application/json' -d '{"on":true}'
systemctl --no-pager --lines=3 status deltr | tail -4
echo
echo -n "    MCP initialize over the public host (expect 200): "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 -X POST "http://${IP}:${PORT}/mcp" -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
echo "==> open  http://${IP}:${PORT}/   (landing)   http://${IP}:${PORT}/app/   (dashboard)   http://${IP}:${PORT}/mcp   (MCP)"
echo "    Vercel: set NEXT_PUBLIC_APP_URL=http://${IP}:${PORT}/app/ and redeploy"
echo "    logs: journalctl -u deltr -f"
