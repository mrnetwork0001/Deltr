# Deltr MCP tools (22)

Generated from the server's `tools/list` by `scripts/dump_mcp_tools.py` (checked by
`tests/test_skill_manifest.py`; regenerate with `--write`). Server name `deltr`; streamable HTTP at
`http://127.0.0.1:8000/mcp` (or stdio via `python main.py --mcp`). `\*` marks a required argument.

Every tool returns JSON. Errors come back inside the result as `{"error": {"code", "message"}}`,
never as a transport failure. A VETO is deterministic and **final for the same inputs**: change
capital or leverage, or ask `deltr_explain_edge`, instead of retrying.

| Tool | Arguments | Use it for | Trades? |
|---|---|---|---|
| `deltr_status` | — | mode, venues (with feed ages), equity, drawdown state, kill/halt, measured gate latency | no |
| `deltr_market` | `symbol` string="BNBUSDT" | current DEX / perp / funding state and the edge breakdown for a symbol | no |
| `deltr_scan` | `notional_usd` number, `horizon_h` number, `min_edge_bps` number, `history_points` integer=60 | the latest opportunity (horizon-based net edge) + spread history | no |
| `deltr_explain_edge` | `capital_usd` number, `leverage` number=2.0, `horizon_h` number=24.0, `assumed_funding_rate` number | dry sizing and the full cost/carry math for a capital + leverage (+ horizon); no side effects | no |
| `deltr_propose_hedge` | `capital_usd`\* number, `leverage` number=2.0, `symbol` string="BNBUSDT" | **phase 1**: size a plan, run the gate pre-check, get a `plan_id` (nothing executes) | no |
| `deltr_evaluate_risk` | `capital_usd`\* number, `leverage`\* number, `symbol` string="BNBUSDT" | dry-run the gate (e.g. leverage 10 -> `LEVERAGE` veto with observed/limit); nothing is clamped | no |
| `deltr_execute_hedge` | `plan_id`\* string, `confirm` boolean=False | **phase 2**: execute a `plan_id` (re-priced, re-gated, single-use; TESTNET needs `confirm: true`) | **yes** |
| `deltr_unwind` | `position_id`\* string, `reason` string="user", `confirm` boolean=False | reduce-only unwind of a position or `"all"` (TESTNET needs `confirm: true`) | **yes** |
| `deltr_positions` | — | open positions + portfolio (mark-to-close PnL split, stop distance) | no |
| `deltr_risk_log` | `limit` integer=20, `only_vetoes` boolean=False | recent gate decisions, newest first, with every check's observed/limit | no |
| `deltr_receipt` | `receipt_id`\* string | one receipt by id (trace steps, fills with provenance, SHA-256) | no |
| `deltr_activity` | `limit` integer=30 | the inbound MCP call log (which client called what, redacted args, latency) | no |
| `deltr_prompt` | `text`\* string | natural language -> typed intent -> plan -> pre-check (propose-only; execute needs the `plan_id`) | no |
| `deltr_kill_switch` | `on`\* boolean, `reason` string="operator" | operator kill switch on/off (only verified reduce-only unwinds pass while on) | no |
| `deltr_reset_halt` | `reason`\* string | clear a sticky drawdown halt once the book is flat and drawdown < 3 % | no |
| `deltr_set_min_edge` | `min_edge_bps`\* number | runtime minimum net edge (PAPER may go negative, labelled on screen; TESTNET >= floor) | no |
| `deltr_stress` | `kind`\* string (basis_shock\|equity_shock\|dex_leg_fail\|funding_flip\|feed_stale\|reset), `magnitude` number=0.0 | labelled SIMULATED scenarios on the paper book only; `kind: "reset"` clears | no (paper book only) |
| `deltr_edge_report` | `capital_usd` number, `leverage` number=2.0, `horizon_h` number=24.0, `assumed_funding_rate` number | the same decomposition rendered as a titled markdown report: waterfall, funding, breakeven verdict, source tags and feed ages, honesty labels | no |
| `deltr_funding_history` | `symbol` string="BNBUSDT", `lookback_days` number=500.0, `holds_days` array, `refresh` boolean=False | REAL MAINNET funding history for a perpetual, and what it says about the carry trade. | see description |
| `deltr_wallet_status` | — | the on-chain leg's status: is the Binance Agentic Wallet CLI installed and signed in, its addresses and Binance's own remaining daily quota, Deltr's arming state and caps | no |
| `deltr_onchain_swap` | `from_token`\* string, `to_token`\* string, `amount`\* number, `notional_usd`\* number, `confirm` boolean=False, `chain_id` integer, `slippage_pct` number, `min_receive` number | request an on-chain swap through the Binance Agentic Wallet (Deltr holds no key); previewed, gated and confirmed, `confirm: true` required | **yes** |
| `deltr_x402_pay` | `payment_required`\* string, `selected_index` integer, `confirm` boolean=False | pay an HTTP 402 (x402 / B402) challenge on BNB Smart Chain through the wallet: preview, policy, sign; `confirm: true` required | **yes** |

## Two-phase execution

1. `deltr_propose_hedge(capital_usd, leverage)` (or `deltr_prompt(text)`) returns a `plan_id`, the plan, the
   flat gate input and the pre-check decision. Nothing executes. Plans expire after 60 s and are single-use.
2. `deltr_execute_hedge(plan_id, confirm)` re-prices the plan against live quotes, runs the full gate again
   (`PRICE_DRIFT` 20 bps, `NEGATIVE_EDGE` at the re-quoted price) and only then places both legs, reversing
   the first if the second fails. In TESTNET `confirm: true` is mandatory; without it the plan is kept and the
   result is `CONFIRM_REQUIRED`.

There is no tool that changes the mode, touches production hosts or withdraws funds.

## Resources (read-only JSON)

| URI | Contents |
|---|---|
| `deltr://status` | the same `SystemStatus` document as `deltr_status` |
| `deltr://risk-limits` | hard limits, `CHECK_ORDER` (19 codes) and the measured gate latency |
| `deltr://config` | `settings.redacted()` — never a key value, only `secrets_present` |

## Error codes

`BAD_PAYMENT_REQUIRED`, `BAW_BAD_JSON`, `BAW_CLI_ERROR`, `BAW_NOT_INSTALLED`, `BAW_NOT_SIGNED_IN`, `BAW_TIMEOUT`, `CHAIN_NOT_ALLOWED`, `CONFIRM_REQUIRED`, `EXECUTION_IN_FLIGHT`, `HALT_NOT_CLEARABLE`, `INTERNAL_ERROR`, `INVALID_ARGUMENT`, `ONCHAIN_NOT_ARMED`, `PLAN_EXPIRED`, `PLAN_NOT_FOUND`, `POSITION_NOT_FOUND`, `RECEIPT_NOT_FOUND`, `SECRET_IN_ARGV`, `STRESS_REFUSED`, `SWAP_PREVIEW_REJECTED`, `SWAP_UNCONFIRMED`, `X402_AMOUNT_OVER_CAP`, `X402_NETWORK_NOT_ALLOWED`, `X402_NO_ACCEPTABLE_OPTION`, `X402_NO_PAY_TO`, `X402_SIGN_FAILED`.
