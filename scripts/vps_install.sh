#!/usr/bin/env bash
# Run ON the VPS (Debian/Ubuntu) after the tree is at /opt/deltr (git clone, or deploy_vps.sh):
#
#   cd /opt/deltr && PORT=8000 bash scripts/vps_install.sh                 # the PAPER showcase (unit: deltr)
#   cd /opt/deltr && INSTANCE=live PORT=8001 bash scripts/vps_install.sh   # the LIVE showcase  (unit: deltr-live)
#
# Options (environment):
#   INSTANCE=paper|live             which engine to install (default paper)
#   PORT=8000                       the port this instance listens on (aborts if another service uses it)
#   CORS=https://usedeltrapp.vercel.app   allowed browser origin for the API (and Host on /mcp)
#   DOMAIN=deltr.example.com        add an nginx server block + certbot for this name (nginx must already exist)
#
# Designed for a box that ALREADY runs other services. It never touches: existing nginx sites, the
# default site, port 80/443 (unless DOMAIN is set, and then only its own server block), the system
# DNS, apt sources / PPAs, the system python, or any unit other than its own. Everything it creates is
# namespaced per instance: user `deltr`, /opt/deltr/.venv, /var/lib/<name>, /etc/<name>.env, <name>.service.
#
# Both instances are PUBLIC READ-ONLY: anyone can read, mutations need the DELTR_API_TOKEN printed once.
# The LIVE instance is installed disarmed: it does not start until you put the mainnet keys, the two
# acknowledgements and a signed-in wallet session in place (the script tells you how). Re-run after `git pull`.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd /opt/deltr
INSTANCE="${INSTANCE:-paper}"
case "$INSTANCE" in paper) NAME=deltr; DEF_PORT=8000;; live) NAME=deltr-live; DEF_PORT=8001;; *) echo "INSTANCE must be paper or live"; exit 1;; esac
PORT="${PORT:-$DEF_PORT}"
CORS="${CORS:-https://usedeltrapp.vercel.app}"
DOMAIN="${DOMAIN:-}"
ENVF="/etc/${NAME}.env"
STATE="/var/lib/${NAME}"

echo "==> preflight (${NAME})"
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
  if ! systemctl is-active --quiet "$NAME" 2>/dev/null; then
    echo "    port ${PORT} is already in use by another service on this box:"; ss -ltnp 2>/dev/null | grep -E "[:.]${PORT} " || true
    echo "    re-run with a free port, e.g.:  INSTANCE=${INSTANCE} PORT=8010 bash scripts/vps_install.sh"; exit 1
  fi
  echo "    port ${PORT} is held by the existing ${NAME} service (update run)"
fi
if [ -f "$ENVF" ]; then
  OLD_PORT="$(grep -E '^DELTR_API_PORT=' "$ENVF" | cut -d= -f2 || true)"
  if [ -n "$OLD_PORT" ] && [ "$OLD_PORT" != "$PORT" ]; then echo "    ${ENVF} has DELTR_API_PORT=$OLD_PORT; using that (edit the file to change it)"; PORT="$OLD_PORT"; fi
fi
echo "    port ${PORT} ok"
IP="$(curl -s -4 --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
MCP_HOSTS="${IP}:${PORT}"; [ -n "$DOMAIN" ] && MCP_HOSTS="${MCP_HOSTS},${DOMAIN}:*"
# the Vercel site proxies /api and /mcp here and forwards its own hostname as Host
CORS_HOST="$(echo "$CORS" | sed -E 's#^https?://##; s#/.*$##')"; [ -n "$CORS_HOST" ] && MCP_HOSTS="${MCP_HOSTS},${CORS_HOST}:*,${CORS_HOST}"

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
mkdir -p "$STATE" && chown -R deltr:deltr "$STATE" /opt/deltr
# the tree is owned by `deltr`, so root's git needs this once for `git pull` to work
git config --global --add safe.directory /opt/deltr 2>/dev/null || true

echo "==> ${ENVF}"
if [ ! -f "$ENVF" ]; then
  TOKEN="$(openssl rand -hex 24)"
  if [ "$INSTANCE" = paper ]; then
    cat > "$ENVF" <<ENV
# Deltr public showcase (PAPER). No exchange keys on this box. Mutations need the token.
DELTR_MODE=paper
DELTR_PUBLIC_READONLY=1
DELTR_API_TOKEN=$TOKEN
DELTR_API_PORT=$PORT
DELTR_STATE_DIR=$STATE
DELTR_CORS_ORIGINS=$CORS
# Host headers the MCP endpoint answers on (loopback is always allowed)
DELTR_MCP_ALLOWED_HOSTS=$MCP_HOSTS
ENV
  else
    BAW="$(command -v baw 2>/dev/null || echo baw)"
    cat > "$ENVF" <<ENV
# Deltr public showcase (LIVE). REAL MONEY once armed. Read-only to the public: mutations need the token.
# The service stays stopped until every line marked FILL/UNCOMMENT is done; the engine refuses to start
# otherwise and names the first thing missing (journalctl -u ${NAME}).
DELTR_MODE=live
DELTR_PUBLIC_READONLY=1
DELTR_API_TOKEN=$TOKEN
DELTR_API_PORT=$PORT
DELTR_STATE_DIR=$STATE
DELTR_CORS_ORIGINS=$CORS
DELTR_MCP_ALLOWED_HOSTS=$MCP_HOSTS

# 1. FILL both: Binance mainnet API key with Reading + Futures permission, no withdrawals, IP-restricted to this box
BINANCE_API_KEY=        # FILL
BINANCE_SECRET_KEY=     # FILL
BINANCE_API_ENV=mainnet

# 2. UNCOMMENT: the acknowledgement that this places real orders with real money
# DELTR_LIVE_ACK=i-understand-this-trades-real-money

# 3. UNCOMMENT: the on-chain opt-in (the DEX leg runs through the Binance Agentic Wallet on this box)
DELTR_ONCHAIN_MODE=live
# DELTR_ONCHAIN_ACK=i-understand-this-moves-real-funds
DELTR_BAW_BIN=$BAW

# caps (small on purpose; the per-trade cap can only be lowered)
DELTR_LIVE_MAX_NOTIONAL_USD=250
DELTR_LIVE_MAX_AGGREGATE_USD=1000
DELTR_ONCHAIN_MAX_NOTIONAL_USD=250
DELTR_ONCHAIN_MAX_AGGREGATE_USD=1000
DELTR_EXECUTION_STYLE=maker
ENV
  fi
  chown root:deltr "$ENVF" && chmod 0640 "$ENVF"
  echo
  echo "    ############################################################"
  echo "    #  DELTR_API_TOKEN for ${NAME} (shown once, save it):"
  echo "    #  $TOKEN"
  echo "    ############################################################"
  echo
else
  grep -q '^DELTR_CORS_ORIGINS=' "$ENVF" || echo "DELTR_CORS_ORIGINS=$CORS" >> "$ENVF"
  grep -q '^DELTR_API_PORT=' "$ENVF" || echo "DELTR_API_PORT=$PORT" >> "$ENVF"
  if grep -q '^DELTR_MCP_ALLOWED_HOSTS=' "$ENVF"; then
    CUR="$(grep -E '^DELTR_MCP_ALLOWED_HOSTS=' "$ENVF" | cut -d= -f2-)"
    for h in $(echo "$MCP_HOSTS" | tr ',' ' '); do case ",$CUR," in *",$h,"*) ;; *) CUR="${CUR:+$CUR,}$h";; esac; done
    sed -i "s|^DELTR_MCP_ALLOWED_HOSTS=.*|DELTR_MCP_ALLOWED_HOSTS=$CUR|" "$ENVF"
  else
    echo "DELTR_MCP_ALLOWED_HOSTS=$MCP_HOSTS" >> "$ENVF"
  fi
  echo "    kept existing ${ENVF}"
fi

echo "==> DNS check (report only; this script never changes the box's resolver)"
if getent hosts fapi.binance.com >/dev/null; then echo "    fapi.binance.com resolves: ok"; else
  echo "    WARNING: fapi.binance.com does not resolve on this box. Deltr will start but the perp feed"
  echo "    will read 'DNS ... does not resolve'. Fix the resolver yourself (e.g. add 'DNS=1.1.1.1' in"
  echo "    /etc/systemd/resolved.conf) only if that is safe for the other services here."; fi

echo "==> systemd unit ${NAME} (listens on 0.0.0.0:${PORT})"
sed -e "s|/etc/deltr.env|${ENVF}|g" -e "s|/var/lib/deltr|${STATE}|g" \
    -e "s|^Description=.*|Description=Deltr ${INSTANCE^^} showcase (public read-only) on :${PORT}|" \
    -e "s|^Environment=PYTHONUNBUFFERED=1|Environment=PYTHONUNBUFFERED=1\nEnvironment=HOME=${STATE}|" \
    -e "s|^StateDirectory=.*|StateDirectory=${NAME}|" \
    deploy/deltr.service > "/etc/systemd/system/${NAME}.service"
systemctl daemon-reload

ARMED=1
if [ "$INSTANCE" = live ]; then
  grep -qE '^BINANCE_SECRET_KEY=[A-Za-z0-9]{16,}' "$ENVF" && grep -qE '^BINANCE_API_KEY=[A-Za-z0-9]{16,}' "$ENVF" \
    && grep -qE '^DELTR_LIVE_ACK=' "$ENVF" && grep -qE '^DELTR_ONCHAIN_ACK=' "$ENVF" || ARMED=0
fi
if [ "$ARMED" = 1 ]; then
  systemctl enable --now "$NAME"
  systemctl restart "$NAME"
else
  systemctl disable --now "$NAME" >/dev/null 2>&1 || true
  echo "    ${NAME} installed but NOT started: ${ENVF} is not armed yet (keys / acknowledgements)."
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null && echo "==> ufw: allowed ${PORT}/tcp"
fi

if [ -n "$DOMAIN" ]; then
  echo "==> nginx server block for ${DOMAIN} (existing sites untouched)"
  command -v nginx >/dev/null 2>&1 || { echo "    nginx is not installed; skipping. Point ${DOMAIN} at port ${PORT} with the web server you already run."; DOMAIN=""; }
fi
if [ -n "$DOMAIN" ]; then
  sed -e "s/server_name _;/server_name ${DOMAIN};/" -e "s#127.0.0.1:8000#127.0.0.1:${PORT}#g" deploy/nginx.conf > "/etc/nginx/sites-available/${NAME}"
  mkdir -p /etc/nginx/sites-enabled && ln -sf "/etc/nginx/sites-available/${NAME}" "/etc/nginx/sites-enabled/${NAME}"
  if nginx -t; then systemctl reload nginx; else echo "    nginx config test failed; removing the ${NAME} site"; rm -f "/etc/nginx/sites-enabled/${NAME}"; fi
  if [ -e "/etc/nginx/sites-enabled/${NAME}" ]; then
    command -v certbot >/dev/null 2>&1 || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || echo "    certbot failed; site stays on http"
  fi
fi

if [ "$ARMED" = 1 ]; then
  echo "==> verify"
  sleep 4
  echo -n "    health: "; curl -s "http://127.0.0.1:${PORT}/api/health" || echo "(no answer yet; check: journalctl -u ${NAME} -n 50)"; echo
  echo -n "    token-less mutation (expect 403): "; curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1:${PORT}/api/kill" -H 'content-type: application/json' -d '{"on":true}'
  systemctl --no-pager --lines=3 status "$NAME" | tail -4
  echo
  echo -n "    MCP initialize over the public host (expect 200): "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 -X POST "http://${IP}:${PORT}/mcp" -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
  echo "==> open  http://${IP}:${PORT}/app/   (dashboard)   http://${IP}:${PORT}/mcp   (MCP)   logs: journalctl -u ${NAME} -f"
else
  cat <<STEPS

==> to arm ${NAME} (REAL MONEY), on this box:
    1. nano ${ENVF}      fill BINANCE_API_KEY / BINANCE_SECRET_KEY, uncomment DELTR_LIVE_ACK and DELTR_ONCHAIN_ACK
    2. wallet CLI:        command -v baw || npm install -g @binance/agentic-wallet
                          sudo -u deltr HOME=${STATE} baw auth signin      (scan the QR in the Binance app)
                          sudo -u deltr HOME=${STATE} baw wallet status --json   -> a session, not UNCONNECTED
    3. INSTANCE=live PORT=${PORT} bash scripts/vps_install.sh     (re-run: it starts the service once armed)
    The engine's preflight re-checks all of it before the first tick and refuses to start otherwise.
STEPS
fi
