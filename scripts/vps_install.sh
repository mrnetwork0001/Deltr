#!/usr/bin/env bash
# Run ON the VPS (Debian/Ubuntu) after cloning the repo, e.g. from Termius:
#
#   sudo apt-get update && sudo apt-get install -y git
#   sudo git clone https://github.com/mrnetwork0001/Deltr.git /opt/deltr
#   cd /opt/deltr && sudo bash scripts/vps_install.sh
#
# Optional:  DOMAIN=deltr.example.com  CORS=https://deltrapp.vercel.app  sudo -E bash scripts/vps_install.sh
#
# Installs a PUBLIC READ-ONLY showcase: paper mode, no exchange keys, real mainnet data,
# mutations only with the generated DELTR_API_TOKEN (printed once). Re-run after `git pull`.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd /opt/deltr
DOMAIN="${DOMAIN:-}"
CORS="${CORS:-https://deltrapp.vercel.app}"

echo "==> python 3.11+"
PY=""
for c in python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  apt-get update -qq && apt-get install -y -qq python3.11 python3.11-venv >/dev/null || {
    apt-get install -y -qq software-properties-common >/dev/null
    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null && apt-get update -qq && apt-get install -y -qq python3.11 python3.11-venv >/dev/null; }
  PY=python3.11
fi
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
  cat > /etc/deltr.env <<EOF
# Deltr public showcase. PAPER mode, no exchange keys on this box. Mutations need the token.
DELTR_MODE=paper
DELTR_PUBLIC_READONLY=1
DELTR_API_TOKEN=$TOKEN
DELTR_API_PORT=8000
DELTR_STATE_DIR=/var/lib/deltr
DELTR_CORS_ORIGINS=$CORS
EOF
  chown root:deltr /etc/deltr.env && chmod 0640 /etc/deltr.env
  echo
  echo "    ############################################################"
  echo "    #  DELTR_API_TOKEN (shown once, save it):                  #"
  echo "    #  $TOKEN"
  echo "    ############################################################"
  echo
else
  grep -q '^DELTR_CORS_ORIGINS=' /etc/deltr.env || echo "DELTR_CORS_ORIGINS=$CORS" >> /etc/deltr.env
  echo "    kept existing /etc/deltr.env"
fi

echo "==> DNS check (Binance hosts must resolve on this box)"
if ! getent hosts fapi.binance.com >/dev/null; then
  echo "    fapi.binance.com does not resolve; pointing systemd-resolved at 1.1.1.1"
  mkdir -p /etc/systemd/resolved.conf.d
  printf '[Resolve]\nDNS=1.1.1.1 8.8.8.8\n' > /etc/systemd/resolved.conf.d/deltr.conf
  systemctl restart systemd-resolved 2>/dev/null || true
  getent hosts fapi.binance.com >/dev/null && echo "    resolved now" || echo "    STILL unresolved: fix /etc/resolv.conf manually"
else
  echo "    ok"
fi

echo "==> systemd"
cp deploy/deltr.service /etc/systemd/system/deltr.service
systemctl daemon-reload
systemctl enable --now deltr
systemctl restart deltr

echo "==> nginx"
command -v nginx >/dev/null 2>&1 || apt-get install -y -qq nginx >/dev/null
cp deploy/nginx.conf /etc/nginx/sites-available/deltr
ln -sf /etc/nginx/sites-available/deltr /etc/nginx/sites-enabled/deltr
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

if [ -n "$DOMAIN" ]; then
  command -v certbot >/dev/null 2>&1 || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || echo "    certbot failed; site stays on http"
fi

echo "==> verify"
sleep 4
echo -n "    health: "; curl -s http://127.0.0.1:8000/api/health || echo "(no answer yet; check: journalctl -u deltr -n 50)"; echo
echo -n "    token-less mutation (expect 403): "; curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/kill -H 'content-type: application/json' -d '{"on":true}'
systemctl --no-pager --lines=3 status deltr | tail -4
echo
echo "==> open  http://$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/   (landing)   and /app/ (dashboard)"
echo "    logs: journalctl -u deltr -f"
