#!/usr/bin/env bash
# scripts/deltr.sh — the Deltr skill wrapper (Binance Agent OS / Skills Hub).
#
#   bash scripts/deltr.sh status                  # mode, venues, equity, drawdown, gate median (JSON)
#   bash scripts/deltr.sh once [--json]           # one tick + scan + explain + propose + gate pre-check (nothing executes)
#   bash scripts/deltr.sh scan [n]                # latest opportunity + last n spread points
#   bash scripts/deltr.sh explain [horizon_h]     # market state + edge waterfall for the horizon
#   bash scripts/deltr.sh propose <capital> [lev] # phase 1: size + gate pre-check -> plan_id (nothing executes)
#   bash scripts/deltr.sh execute <plan_id> [--confirm]      # phase 2: single-use plan, re-priced, re-gated
#   bash scripts/deltr.sh unwind <position_id|all> [--confirm]
#   bash scripts/deltr.sh evaluate <capital> <lev>           # dry-run the gate (nothing is stored)
#   bash scripts/deltr.sh mcp                     # stdio MCP transport (+ dashboard) in one process
#   bash scripts/deltr.sh serve [args...]         # python main.py (dashboard :8000, MCP at /mcp)
#
# Resolution: DELTR_HOME, else the checkout this script lives in, else ~/.deltr.  Uses the repo's
# .venv when present.  Read-only commands (status/scan/explain/propose/evaluate) talk to a RUNNING
# instance over the REST API (DELTR_API, default http://127.0.0.1:8000) and fall back to a one-shot
# engine when nothing is up.  execute/unwind need the running instance that holds the plan/position.
# Every output is one JSON document on stdout; logs go to stderr.  Never prints secrets; there is no
# production mode to select (BINANCE_API_ENV=prod is refused by main.py).
set -euo pipefail

resolve_home() {
  if [ -n "${DELTR_HOME:-}" ] && [ -f "${DELTR_HOME}/main.py" ]; then echo "${DELTR_HOME}"; return; fi
  local d
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  while [ "$d" != "/" ]; do
    if [ -f "$d/main.py" ] && [ -f "$d/risk_gate.py" ]; then echo "$d"; return; fi
    d="$(dirname "$d")"
  done
  if [ -f "$HOME/.deltr/main.py" ]; then echo "$HOME/.deltr"; return; fi
  echo "deltr.sh: cannot find the Deltr checkout (set DELTR_HOME)" >&2
  exit 2
}

HOME_DIR="$(resolve_home)"
PY="${HOME_DIR}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
API="${DELTR_API:-http://127.0.0.1:${DELTR_API_PORT:-8000}}"
cd "$HOME_DIR"

api_up() { command -v curl >/dev/null 2>&1 && curl -sf -m 2 "$API/api/health" >/dev/null 2>&1; }
post() { curl -sS -m 30 -H 'content-type: application/json' -H 'X-Deltr-Source: cli' -X POST "$API$1" -d "$2"; echo; }
get()  { curl -sS -m 30 -H 'X-Deltr-Source: cli' "$API$1"; echo; }
need_api() { api_up || { echo "deltr.sh: no running Deltr at $API — start one with: bash scripts/deltr.sh serve" >&2; exit 3; }; }
num() { [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "deltr.sh: '$1' is not a number" >&2; exit 2; }; }

# One-shot engine for read-only commands when no instance is running: tick once, answer, persist
# nothing new (a plan proposed here lives only in this process and cannot be executed later).
offline() {
  "$PY" - "$@" <<'PY'
import asyncio, json, logging, sys
from deltr.config import load_settings
from deltr.engine import build_engine
from deltr.models import TraceSource

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
cmd, *rest = sys.argv[1:]

def j(x):
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x

async def run() -> int:
    s = load_settings()
    eng = build_engine(s)
    await eng.start(loops=False)
    try:
        ms, edge = eng.market()
        if ms is None or ms.dex is None or ms.funding is None:
            print(json.dumps({"error": {"code": "NO_MARKET_STATE", "message": "; ".join(eng.start_errors) or "venues returned no data"}}))
            return 3
        if cmd == "scan":
            n = int(rest[0]) if rest else 20
            out = {"opportunity": j(eng.scan()), "history": [j(p) for p in eng.history(n)]}
        elif cmd == "explain":
            h = float(rest[0]) if rest else float(s.funding_horizon_hours)
            edge, sizing, comps = eng.explain_edge(None, None, h)
            out = {"market": j(ms), "edge": j(edge), "components": [j(c) for c in comps],
                   "sizing": {"qty": float(sizing.qty), "notional_usd": sizing.notional_usd, "margin_usd": sizing.margin_usd,
                              "cash_required_usd": sizing.cash_required_usd, "leverage": sizing.leverage, "capped_by": sizing.capped_by}}
        elif cmd == "propose":
            cap = float(rest[0]); lev = float(rest[1]) if len(rest) > 1 else None
            plan, proposal, precheck = await eng.propose(cap, lev, s.symbol, TraceSource.CLI, client="cli")
            out = {"plan_id": plan.id, "plan": j(plan), "proposal": j(proposal), "precheck": j(precheck), "expires_at": j(plan)["expires_at"],
                   "message": "offline pre-check only: no instance is running, so this plan cannot be executed; start `deltr.sh serve` first"}
        elif cmd == "evaluate":
            out = j(eng.evaluate_risk(float(rest[0]), float(rest[1]), s.symbol))
        else:
            raise SystemExit(f"offline: unknown command {cmd}")
        print(json.dumps(out, default=str))
        return 0
    finally:
        await eng.stop()

raise SystemExit(asyncio.run(run()))
PY
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  status)
    if api_up; then get /api/status
    else "$PY" main.py --once --json "$@" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get("status") or {"error": d.get("error")}, default=str))'; fi ;;
  once)
    if [ "${1:-}" = "--json" ] || [ $# -eq 0 ]; then exec "$PY" main.py --once --json "$@"; else exec "$PY" main.py --once "$@"; fi ;;
  scan)
    if api_up; then get "/api/opportunities?n=${1:-20}"; else offline scan "$@"; fi ;;
  explain)
    [ -n "${1:-}" ] && num "$1"
    if api_up; then get "/api/market${1:+?horizon_h=$1}"; else offline explain "$@"; fi ;;
  propose)
    cap="${1:?usage: propose <capital_usd> [leverage]}"; lev="${2:-}"; num "$cap"; [ -n "$lev" ] && num "$lev"
    if api_up; then
      body="{\"capital_usd\": $cap"; [ -n "$lev" ] && body="$body, \"leverage\": $lev"; body="$body}"
      post /api/hedge/propose "$body"
    else offline propose "$cap" ${lev:+"$lev"}; fi ;;
  evaluate)
    cap="${1:?usage: evaluate <capital_usd> <leverage>}"; lev="${2:?usage: evaluate <capital_usd> <leverage>}"; num "$cap"; num "$lev"
    if api_up; then post /api/risk/evaluate "{\"capital_usd\": $cap, \"leverage\": $lev}"; else offline evaluate "$cap" "$lev"; fi ;;
  execute)
    need_api; pid="${1:?usage: execute <plan_id> [--confirm]}"; confirm=false; [ "${2:-}" = "--confirm" ] && confirm=true
    post /api/hedge/execute "{\"plan_id\": \"$pid\", \"confirm\": $confirm}" ;;
  unwind)
    need_api; pos="${1:?usage: unwind <position_id|all> [--confirm]}"; confirm=false; [ "${2:-}" = "--confirm" ] && confirm=true
    post /api/hedge/unwind "{\"position_id\": \"$pos\", \"reason\": \"cli\", \"confirm\": $confirm}" ;;
  mcp)     exec "$PY" main.py --mcp "$@" ;;
  serve)   exec "$PY" main.py "$@" ;;
  help|-h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) echo "deltr.sh: unknown command '$cmd' (try: help)" >&2; exit 2 ;;
esac
