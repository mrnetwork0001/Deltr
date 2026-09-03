# CLI surface

One process does everything: `python main.py` = engine + REST API + dashboard + MCP at `/mcp`.
The skill wrapper `scripts/deltr.sh` (also shipped as `skills/deltr-binance/scripts/deltr.sh`) resolves
`DELTR_HOME` (else the checkout it lives in, else `~/.deltr`), uses `.venv/bin/python` when present and
forwards to `main.py` or the REST API. Every wrapper command prints one JSON document on stdout; logs go
to stderr; no command prints a secret.

## `main.py` flags

| Flag | Meaning |
|---|---|
| `--mode paper\|testnet` | overrides `DELTR_MODE` (default `paper`; `testnet` needs `BINANCE_API_KEY` / `BINANCE_SECRET_KEY`; there is no live mode) |
| `--symbol BNBUSDT` | overrides `DELTR_SYMBOLS` |
| `--capital 10000` | portfolio capital in USD (`DELTR_CAPITAL_USD`) |
| `--leverage 2` | default perp leverage, max 3 (`DELTR_DEFAULT_LEVERAGE`) |
| `--port 8000` | API / dashboard / MCP port (3000 and 3001 are refused; a port already in use refuses a second engine) |
| `--no-ui` / `--dev-ui` | do not serve the dashboard / spawn `npm run dev` instead of the static `ui/out` |
| `--mcp` | add the stdio MCP transport to this process (banner and logs go to stderr; stdout is JSON-RPC only) |
| `--auto` | PAPER only: auto-execute gate-approved actionable opportunities |
| `--once [--json]` | one tick + scan + explain + propose + gate pre-check, then exit (`--json`: one JSON document on stdout, nothing else) |
| `--replay PATH` | deterministic replay of a `MarketState/v1` JSONL fixture (REPLAY badge; state under `state/<mode>/replay/`) |
| `--replay-speed 1.0` | replay speed multiplier |
| `--min-edge-bps N` | PAPER: [-50, 50] labelled override of the minimum net edge; TESTNET: must be >= the measured round trip |
| `--state-dir DIR` | persistence root (default `<repo>/state`, per-mode subfolders) |
| `--version` | print `deltr <version>` |

Exit codes: `0` ok · `2` invalid configuration (e.g. `BINANCE_API_ENV=prod`, missing testnet keys, port taken) ·
`3` (`--once`) no market state could be built — the document's `error` says why.

## `scripts/deltr.sh` commands

| Command | Runs | Needs a running instance? |
|---|---|---|
| `status` | `GET /api/status`, else `main.py --once --json` reduced to its `status` block | no |
| `once [--json]` | `main.py --once --json` (plain `--once` pretty-prints) | no |
| `scan [n]` | `GET /api/opportunities?n=` else a one-shot engine: `{"opportunity", "history"}` | no |
| `explain [horizon_h]` | `GET /api/market?horizon_h=` else one-shot: `{"market", "edge", "components", "sizing"}` | no |
| `propose <capital> [lev]` | `POST /api/hedge/propose` else one-shot pre-check (the plan then dies with the process) | no |
| `evaluate <capital> <lev>` | `POST /api/risk/evaluate` else one-shot `RiskDecisionRecord` (`dry_run: true`) | no |
| `execute <plan_id> [--confirm]` | `POST /api/hedge/execute` | **yes** (the plan lives in it) |
| `unwind <position_id\|all> [--confirm]` | `POST /api/hedge/unwind` | **yes** |
| `mcp` | `main.py --mcp` | — |
| `serve [flags]` | `main.py [flags]` | — |

`DELTR_API` (default `http://127.0.0.1:8000`) points the wrapper at another port. REST calls carry
`X-Deltr-Source: cli`, so the dashboard's trace shows `cli` as the origin.

## `--once --json` document

Top-level keys, in order (values are the pydantic models serialised with `model_dump(mode="json")`):

```
{
  "ok": true,                      // false when nothing could be priced; see "error"
  "mode": "paper", "replay": false, "replay_path": null, "symbol": "BNBUSDT", "version": "1.0.0",
  "ts": "2026-09-02T08:01:25.873978+00:00",
  "capital_usd": 5000.0,           // trade capital = 50 % of the portfolio
  "portfolio_capital_usd": 10000.0, "leverage": 2.0, "horizon_h": 72.0,
  "min_edge_bps": 3.0, "min_edge_override": null,
  "gate_median_us": 1.583,         // measured on this machine at startup (10 000 evaluations)
  "venues": [ {"name", "ok", "age_ms", "source", "detail"} ],          // 3 entries
  "start_errors": [],
  "market": {
    "symbol", "dex": {"pool", "fee_tier", "fee_bps", "mid_price", "size_base", "exec_price_buy", "amount_in_usdt",
                      "exec_price_sell", "amount_out_usdt", "impact_bps", "gas_units", "gas_price_wei", "gas_usd", "block", "ts", "source"},
    "cex_perp_book": {"venue", "symbol", "bid", "ask", "bid_qty", "ask_qty", "ts", "source", "mid"},
    "cex_spot_ref":  { ...same shape (public spot data mirror, displayed sanity reference)... },
    "funding": {"symbol", "mark_price", "index_price", "last_funding_rate", "next_funding_time_ms", "interval_h", "annualized_pct", "ts", "source"},
    "perp_ref_price", "freshness": {"cex_age_ms", "dex_age_ms", "spot_age_ms", "ok", "reason"}, "ts", "source"
  },
  "edge": {"notional_usd", "basis_entry_bps", "dex_fee_bps", "dex_impact_bps", "perp_slip_bps", "cex_taker_bps", "gas_bps_leg",
           "roundtrip_cost_bps", "funding_rate_last", "horizon_h", "settlements", "funding_bps_horizon", "basis_exit_assumed_bps",
           "basis_shock_bps", "net_edge_bps", "expected_edge_usd", "allocated_risk_usd"},
  "components": [ {"label", "bps", "kind"} ],                          // the waterfall, 8 rows
  "opportunity": {"id", "symbol", "direction", "dex_price", "perp_price", "edge": {...}, "size_base", "notional_usd",
                  "horizon_h", "is_actionable", "reason", "min_edge_bps_used", "freshness": {...}, "ts"},
  "sizing": {"qty", "notional_usd", "margin_usd", "cash_required_usd", "leverage", "capped_by"},
  "plan": {"id", "opportunity_id", "symbol", "legs": [ {"venue", "symbol", "side", "qty", "price_hint", "reduce_only", "leverage",
           "client_id", "position_side"} ], "qty", "notional_usd", "leverage", "margin_usd", "cash_required_usd", "allocated_risk_usd",
           "expected_edge_bps", "roundtrip_cost_bps", "ref_dex_price", "ref_perp_price", "reduce_only", "position_id", "source",
           "client", "prompt", "created_at", "expires_at", "plan_hash"},
  "proposal": { ...the flat gate input the executor built from the plan... },
  "precheck": {"id", "plan_id", "approved", "code", "reason", "observed", "limit", "unit",
               "checks": [ {"name", "passed", "observed", "limit", "unit"} ],   // all 19, in CHECK_ORDER
               "latency_ns", "latency_us", "dd_state", "drawdown_pct", "halted", "kill_switch", "mode", "dry_run", "ts"},
  "receipt": null,                 // only --auto fills it (the receipt of the tick's auto-execution)
  "error": null,
  "status": { ...SystemStatus, the same document as `deltr.sh status` / deltr_status... }
}
```

Funding figures (`funding.*`, `funding_bps_horizon`) come from the Binance Futures **testnet** and are
indicative. `drawdown_pct` is in percent. Nothing executes under `--once` unless `--auto` is set (PAPER only).

## `status` document (what `deltr.sh status` prints)

```
{"mode": "paper", "symbol": "BNBUSDT", "version": "1.0.0", "uptime_s": 10, "started_at": "...",
 "halted": false, "kill_switch": false, "dd_state": "NORMAL", "equity_usd": 10000.0, "drawdown_pct": 0.0,
 "open_positions": 0,
 "venues": [{"name": "binance_futures_testnet", "ok": true, "age_ms": 0,    "source": "binance-futures-testnet", "detail": ""},
            {"name": "binance_spot_mirror",     "ok": true, "age_ms": 819,  "source": "binance-spot-mirror",     "detail": ""},
            {"name": "pancakeswap_v3",          "ok": true, "age_ms": 1423, "source": "bsc-mainnet-chain",       "detail": ""}],
 "upstream": null, "secrets_present": false, "binance_api_env": "testnet", "replay": false, "stress_active": null,
 "gate_median_us": 1.916, "min_edge_bps": 3.0, "min_edge_floor_bps": 0.0, "leg_order": "dex_first",
 "limits": {"max_leverage": 3.0, "max_capital_risk_pct": 0.02, "max_drawdown_pct": 0.03, "warn_drawdown_pct": 0.02,
            "max_capital_utilization": 0.9, "max_notional_usd": 50000.0, "min_notional_usd": 5.0, "hedge_qty_tolerance": 0.005,
            "max_open_positions": 3, "max_quote_age_ms": 5000.0, "max_price_drift_bps": 20.0, "price_sanity_bps": 100.0,
            "min_expected_edge_bps": 3.0, "basis_shock_bps": 100.0, "default_roundtrip_cost_bps": 20.0, "allowed_symbols": ["BNBUSDT"]},
 "check_order": ["KILL_SWITCH", "...", "NEGATIVE_EDGE"]}
```

`secrets_present` is the only thing the CLI ever says about credentials.

## Two-phase documents

* `propose` → `{"plan_id", "plan", "proposal", "precheck", "expires_at", "message"}` — nothing executes.
* `execute` → an `ExecutionReceipt`: `{"id", "plan_id", "status", "decision", "plan", "fills": [...], "steps": [...],
  "position_id", "residual_delta_base", "legging_window_ms", "sha256", ...}`. In TESTNET without `--confirm`
  the receipt carries `CONFIRM_REQUIRED` and the plan is kept.
* `unwind` → a list of receipts (reduce-only perp BUY first, then the DEX sell).

## REST (running instance)

`GET /api/health | /api/status | /api/snapshot | /api/market | /api/opportunities | /api/positions | /api/receipts | /api/receipts/{id} | /api/risk/decisions | /api/mcp/activity | /api/config`
`POST /api/prompt | /api/hedge/propose | /api/hedge/execute | /api/hedge/unwind | /api/risk/evaluate | /api/kill | /api/risk/reset_halt | /api/stress | /api/scout/min_edge | /api/upstream`

Error envelope `{"error": {"code", "message"}}` with HTTP 400 / 404 / 409 / 410 (`PLAN_EXPIRED`).
OpenAPI docs at `/api/docs`.
