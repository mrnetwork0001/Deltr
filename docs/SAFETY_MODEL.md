# Deltr Safety Model

> "Your rules. Your agents." Every order Deltr routes to Binance passes a **deterministic, zero-LLM Python risk gate** (`risk_gate.py`) before it leaves the process. This page lists every check, its threshold, who is allowed to supply its input, and the test that proves it.

## What Deltr never does

| Never | How it is enforced |
|---|---|
| Place a mainnet order by accident | Only `Mode.LIVE` carries a mainnet order host in the frozen `HOSTS` table, and LIVE refuses to build or start unless **all six** requirements below are present. `FuturesClient` refuses to sign against any non-testnet host unless the caller passes `allow_mainnet_orders=True`, which only the LIVE path does, and even then only for `fapi.binance.com`. `BINANCE_API_ENV=prod` stays refused in every mode. (`tests/test_mode_isolation.py`, `tests/test_live_mode.py`.) **As of this writing Deltr has never placed a mainnet order.** |
| Hold, read, store or sign with a private key | The on-chain leg is delegated to the Binance Agentic Wallet CLI, which custodies the key and does the signing. There is no signer, no key parameter and no raw-transaction path in the codebase; AST tests refuse the import of any signing library and any parameter or field named `private_key` / `mnemonic` / `seed_phrase` / `keystore` (`tests/test_agentic_wallet.py`). `assert_no_secret_in_argv` screens every CLI argument before exec. |
| Cross the spread by default | The perp leg is `timeInForce=GTX` (post-only) in LIVE. The measured economics make this a correctness requirement: about 16.6 bps taken against about 8.6 bps posted. An unfilled post-only order is a **failure** that reverses the other leg, never a silent skip and never a fallback to a taker order (`tests/test_live_mode.py`). |
| Take a directional bet | Only a paired Long-DEX / Short-Perp is accepted (`NOT_DELTA_NEUTRAL`, re-derived from the legs, not trusted from a flag). |
| Let a caller loosen a limit | `RiskLimits` clamps the three spec invariants (3x, 2 %, 3 %) so they can be tightened but never loosened. Equity, peak, drawdown and the open-position registry live **inside the gate**; `portfolio_balance` / `open_positions` in a proposal are ignored (`test_caller_cannot_rescale_limits_via_proposal`, `test_max_open_positions_is_gate_owned`). |
| Trust "reduce only" | An unwind earns the kill-switch / halt bypass only when it references a **registered** position and its quantity is at most the open quantity (`REDUCE_ONLY_UNVERIFIED`). |
| Let an LLM execute from free text | `deltr_prompt` is propose-only. Execution needs a `plan_id` from `deltr_propose_hedge`; plans are single-use, expire after 60 s, and are re-priced and re-gated at execution. There is no mode-changing tool. |
| Withdraw or move funds | Deltr has no withdrawal or transfer code; the Binance MCP server itself has no withdrawal scope. |
| Hide a loss | Positions are marked **to close** (net of the estimated exit round trip), so a position is never green if closing it would be red. |
| Evade a halt by restarting | Gate and portfolio state persist per mode under `state/<mode>/`; a halt survives a restart, `reset_halt()` only works once drawdown is back under 3 %, and the peak is never silently re-based. |

## Arming LIVE

LIVE is opt-in six times over. Each requirement is refused on its own, and the message names it.
Nothing arms from a default.

| # | Requirement | Where it is refused |
|---|---|---|
| 1 | `DELTR_MODE=live` chosen explicitly | mode selection; no tool changes the mode at runtime |
| 2 | `BINANCE_API_KEY` **and** `BINANCE_SECRET_KEY` present | `Settings` validation |
| 3 | `BINANCE_API_ENV=mainnet` | `Settings` validation (`prod` is refused in every mode) |
| 4 | `DELTR_LIVE_ACK=i-understand-this-trades-real-money` | `Settings` validation |
| 5 | `DELTR_ONCHAIN_MODE=live` and `DELTR_ONCHAIN_ACK=i-understand-this-moves-real-funds` | `Settings` validation |
| 6 | the wallet CLI installed **and** reporting a signed-in session | `Engine.live_preflight()`, **before the first tick** |

Requirement 6 is read from the CLI at startup and again at call time. Deltr never signs in for the
operator, never creates a wallet and never stores a session.

When the preflight passes, the banner and `deltr_status.real_funds_armed` say so unmistakably, with
the caps and the wallet's **public** address. `real_funds_armed` is set by the Engine only after the
preflight actually passed, so a misconfigured run can never present itself as armed.

### LIVE caps

| Cap | Default | Raised by |
|---|---|---|
| Per-trade notional | **$250** | `DELTR_LIVE_MAX_NOTIONAL_USD`, which can only *lower* it; `MAX_NOTIONAL_BY_MODE[LIVE]` is a hard ceiling |
| Aggregate open notional | **$1,000** | `DELTR_LIVE_MAX_AGGREGATE_USD` |
| On-chain per request | $250 | `DELTR_ONCHAIN_MAX_NOTIONAL_USD` |
| On-chain per run | $1,000 | `DELTR_ONCHAIN_MAX_AGGREGATE_USD` |
| On-chain slippage / price impact | 0.5 % / 1.0 % | `DELTR_ONCHAIN_MAX_SLIPPAGE_PCT`, `DELTR_ONCHAIN_MAX_PRICE_IMPACT_PCT` |

The gate runs first and unchanged; the LIVE aggregate ceiling sits **after** the gate's decision and
can only subtract from what the gate approved. It never approves anything the gate vetoed.

The on-chain per-run ceiling counts **everything the wallet was asked to sign**: hedge legs, standalone
`deltr_onchain_swap` calls and `deltr_x402_pay` payments, and swaps the CLI returns as `PENDING` as well
as confirmed ones. A charge is taken before the wallet is called and given back only on a failure code
that proves nothing was submitted (`tests/test_blast_radius.py`). It counts **open** on-chain exposure
rather than gross volume: a reversal or an unwind gives its notional back, and an exposure-reducing swap
is exempt from the caps and from the kill switch for the same reason the frozen gate exempts a verified
reduce-only unwind — a cap that refuses the swap which removes an exposure turns a stop into a naked leg.

> **The on-chain leg is armed independently of the mode.** `DELTR_ONCHAIN_MODE=live` plus
> `DELTR_ONCHAIN_ACK` arms `deltr_onchain_swap` and `deltr_x402_pay` in **PAPER and TESTNET too**: those
> two tools spend real BNB Smart Chain funds in any mode. The perp side is fully isolated (only LIVE
> carries a mainnet order host, and `FuturesClient` refuses mainnet credentials outside it), but "PAPER"
> alone does not mean "no funds at risk". The startup banner says so in as many words whenever the
> on-chain opt-in is complete, and `deltr_status.onchain_armed` reports it on every surface. Leave
> `DELTR_ONCHAIN_MODE` unset to close that path entirely.

## Custody of the on-chain leg

| Property | How |
|---|---|
| Who holds the key | Binance's Agentic Wallet. Deltr shells out to the `baw` CLI and reads its JSON |
| Who signs | Binance's wallet, under its own spending limits, on top of Deltr's caps |
| What Deltr sees | public data only: an address, a quota, an order id, a transaction hash |
| Preview before execute | `market-order swap` has no preview/execute pair, so the wrapper previews with `market-order quote`, checks `min_receive` and price impact against that preview, and refuses (`SWAP_PREVIEW_REJECTED`, "nothing was submitted and nothing was signed") before `swap` runs |
| Unconfirmed swap | never recorded as a fill. `fill_from_swap` refuses a result without both a transaction hash and a CLI-reported received amount, and derives the price only from reported amounts |
| Reversal | real, in both directions. A filled on-chain leg is reversed by the opposite swap; a reversal that fails books a naked one-legged position and engages the kill switch, exactly as on the perp side |
| No fallback | a missing CLI or a signed-out wallet is a refusal that names the command to run. Deltr never falls back to a simulated on-chain leg |

## Market data provenance

Market data is real Binance mainnet in **every** mode, and always keyless: the engine builds a
separate credential-free `FuturesClient` for it, so a credentialed client never polls a public
endpoint even in LIVE where both point at the same host. Provenance is derived from the host that
answered, never asserted by a caller.

If a hostname does not resolve, Deltr raises an actionable error naming the resolver and both `dig`
commands to compare, and the feed simply reads stale so the gate blocks. It never substitutes a
different venue or simulated data.

## Hard invariants (from the spec, clamped in code)

| Invariant | Value | Constant |
|---|---|---|
| Max futures leverage | 3.0x | `MAX_LEVERAGE` |
| Max capital at risk per trade | 2 % of equity | `MAX_CAPITAL_RISK_PCT` |
| Drawdown stop-loss (halt) | 3 % from peak equity | `MAX_DRAWDOWN_PCT` |
| Drawdown warning | 2 % | `WARN_DRAWDOWN_PCT` |

## The 19 ordered checks

Checks run cheapest-and-most-decisive first and short-circuit on the first failure. Every veto carries the **observed value, the limit and the unit**, so the dashboard renders "leverage 10.0 > 3.0 x" rather than a bare code.

| # | Code | Passes when | Threshold (default) | Input owner | Test |
|---|---|---|---|---|---|
| 1 | `KILL_SWITCH` | switch off, or a verified unwind | operator flag | gate | `test_kill_switch_blocks_new_risk` |
| 2 | `HALTED_DRAWDOWN` | state ≠ HALTED, or a verified unwind | dd ≥ 3 % (sticky) | gate (`update_equity`) | `test_drawdown_stop_loss_halts_new_risk`, `test_halt_cannot_be_reset_until_recovered` |
| 3 | `MALFORMED` | leverage, notional, allocated_risk finite and valid; legs finite and > 0 | fail-closed; a **missing leverage is a veto** | executor | `test_malformed_fields_fail_closed`, `test_missing_leverage_fails_closed` |
| 4 | `REDUCE_ONLY_UNVERIFIED` | reduce_only ⇒ registered `position_id` and qty ≤ open qty | registry | gate + executor | `test_unverified_reduce_only_is_vetoed`, `test_verified_unwind_passes_kill_switch_and_halt` |
| 5 | `NOT_DELTA_NEUTRAL` | flag set **and** legs are DEX BUY / perp SELL | structural | executor (from plan legs) | `test_non_delta_neutral_is_vetoed` |
| 6 | `HEDGE_MISMATCH` | \|dex_qty − perp_qty\| ≤ max(1 lot step, 0.5 %) | step 0.01 | executor | `test_hedge_leg_mismatch_is_vetoed` |
| 7 | `LEVERAGE` | leverage ≤ 3.0 | 3.0x | caller (validated) | `test_leverage_above_3x_is_vetoed` |
| 8 | `MIN_NOTIONAL` | notional ≥ 5 USDT | exchange filter | executor | `test_min_and_max_notional` |
| 9 | `MAX_NOTIONAL` | notional ≤ per-mode cap | PAPER 50 000 / TESTNET 5 000 | config | `test_min_and_max_notional` |
| 10 | `CAPITAL_RISK` | max(proposer risk, notional·(round-trip + 100 bps)/1e4) ≤ 2 % · equity | $200 on $10 000 | executor + gate floor | `test_risk_above_2pct_is_vetoed`, `test_risk_floor_overrides_optimistic_proposer` |
| 11 | `AGGREGATE_RISK` | Σ open risk + new ≤ (3 % − dd) · equity | $300 budget at dd 0 | gate registry | `test_aggregate_risk_budget`, `test_aggregate_risk_budget_shrinks_with_drawdown` |
| 12 | `CAPITAL_CAPACITY` | Σ notional·(1 + 1/L) over open + new ≤ 90 % · equity | $9 000 on $10 000 | gate registry | `test_capital_capacity_for_both_legs`, `test_capital_capacity_counts_open_positions` |
| 13 | `AGGREGATE_NOTIONAL` | Σ open notional + new ≤ 3 · equity | $30 000 on $10 000 | gate registry | `test_aggregate_notional_cap` |
| 14 | `SYMBOL_NOT_ALLOWED` | symbol in the configured whitelist | `DELTR_SYMBOLS` | config | `test_symbol_whitelist` |
| 15 | `MAX_POSITIONS` | registered positions < max | 3 (from settings) | gate registry | `test_max_open_positions_is_gate_owned` |
| 16 | `STALE_QUOTE` | quote age ≤ 5 000 ms (a DEX quote the hub still calls fresh, up to 9 s, is normalised to the 5 s limit so scout and gate agree) | 5 s | executor (freshness) | `test_stale_quote_is_vetoed`, `test_fresh_dex_age_is_normalised_to_the_gate_limit` |
| 17 | `PRICE_SANITY` | \|perp ref − DEX ref\| ≤ 100 bps | 100 bps | executor (market state) | `test_price_sanity_vetoes_junk_gaps` |
| 18 | `PRICE_DRIFT` | \|re-quote − plan price\| ≤ 20 bps | 20 bps | executor (re-quote at execute) | `test_price_drift_is_vetoed` |
| 19 | `NEGATIVE_EDGE` | expected edge **at the re-quoted price** ≥ minimum (an adverse move inside the drift band lowers it one-for-one) | 0 bps (PAPER may set a labelled override) | scout via executor (re-priced at execute) | `test_negative_edge_is_vetoed`, `test_edge_is_re_priced_at_execution_not_copied_from_the_plan` |

Why the risk floor in check 10 matters: the proposer's own estimate is only a lower bound. The gate assumes the full round trip is paid **and** a 100 bps adverse basis move, so an optimistic caller cannot talk its way past the 2 % rule.

## Drawdown state machine

```
NORMAL ──dd ≥ 2 %──▶ WARN ──dd ≥ 3 %──▶ HALTED (sticky)
   ▲                    ▲                    │
   └──── reset_halt() only when dd < 3 % ────┘   (peak is never re-based here)
rebase_peak(new_equity, reason)  — separate, logged operator action (capital top-up)
```

* `update_equity()` is called on every tick with mark-to-close equity.
* While HALTED, only verified reduce-only unwinds pass, so the book can always be flattened.
* State (equity, peak, state, kill switch, registry) is persisted per mode and reloaded on start. Paper state never leaks into testnet state and vice versa.

## Who may build a gate input

Only the **Executor** assembles `TradeProposal` (from the plan, the live market state and the portfolio). MCP and API callers may supply exactly: `capital_usd`, `leverage`, `symbol`, `plan_id`, `confirm`, `position_id`, `reason`. `tests/test_gate_inputs.py` checks by AST that no other module constructs a proposal.

## Execution discipline

* One `asyncio.Lock` around gate → reserve → execute, so API, MCP and auto mode cannot double-spend.
* Default leg order is DEX first; the second leg is sized from the first fill; if the second leg fails the first is reversed and the receipt says `unwound` with the realised cost. If the reversal fails too, the naked leg is **booked as a one-legged position** (marked, stop-monitored, registered with the gate, cash still locked) and the kill switch is engaged until an operator unwinds it (`test_failed_reversal_books_a_naked_leg_and_engages_the_kill_switch`).
* Unwinds go perp reduce-only BUY first, then DEX sell. A half-closed book is never left silently: the perp leg that did close is booked, the position becomes a naked DEX long under the stop monitor, the kill switch engages, the receipt says `failed` and an error event is emitted (`test_half_closed_unwind_books_the_perp_leg_and_keeps_the_dex_leg`).
* The funding-flip rule only fires inside the 15-minute window before a settlement whose rate is negative (and only when basis + remaining carry cannot pay the exit); a negative rate hours before settlement never churns a fresh position.
* Replay runs (`--replay`) persist under `state/<mode>/replay/`, so a rehearsal on fixture prices can never leak positions into a live run of the same mode.
* Every `drawdown_pct` a client sees (status, portfolio, stress result) is in **percent**; the gate's internal fraction never reaches a client.
* Every receipt embeds the ordered trace steps and a SHA-256 over the canonical plan + decision + fills.
* Testnet orders are `LIMIT` + `IOC` only, with deterministic client order ids and query-before-retry, so a retry can never duplicate a fill.
* LIVE perp orders are `LIMIT` + `GTX` (post-only) by default, with the same deterministic client-id scheme (one id per posting attempt, so a timeout is resolved by lookup rather than by a second order). A resting order is always cancelled before the leg returns, so no order of Deltr's is left working after the leg is done with it. `taker` style is available but has to be chosen deliberately.
* TESTNET and LIVE execution and unwind both require `confirm: true`; the LIVE refusal says in as many words that a real mainnet order and a real on-chain swap are what is being confirmed.

## Stress scenarios are labelled

Stress (`basis_shock`, `equity_shock`, `dex_leg_fail`, `funding_flip`, `feed_stale`) mutates only the paper portfolio or leg state, never the market feed, and paints a persistent **SIMULATED** badge on the status bar, the affected positions and receipts until `reset`. `basis_shock` and `funding_flip` (book arithmetic only) are refused in TESTNET while real orders are open; `dex_leg_fail`, `feed_stale` and `equity_shock` are refused in TESTNET outright, because a later real execution would consume the leg-failure flag, price off a frozen feed or start from a persisted simulated HALT. `equity_shock` magnitudes are bounded to 0–100 %.

## Latency

The gate is stdlib-only and allocation-light. `risk_gate.benchmark()` runs 10 000 evaluations at startup and the banner, the status bar and the README quote **that measured median** (1.5 to 2.5 µs across repeated runs on an Apple M-series laptop), never a target figure. `tests/test_risk_gate.py::test_hot_path_latency_is_microseconds` prints the number under `pytest -q -s` and fails above 5 µs so slower CI machines stay green.

## Determinism

Two runs of the engine over the same replay fixture produce a byte-identical decision log (`tests/test_replay_determinism.py`). That is the cheapest proof that no model sits in the loop.

## Secrets

Environment names mirror the official Binance skill (`BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, `BINANCE_API_ENV`). The banner, `/api/config`, the MCP activity log and every receipt expose only `secrets_present: true/false`; `tests/test_secrets_hygiene.py` greps the repo for key-shaped literals. `.env` is git-ignored.

The LIVE surfaces keep the same rule. The startup banner, `live_preflight()`'s returned facts and
`deltr_status` carry the wallet's **public** address, the caps and the arming state, and never a
key, a secret, a token or a session. `assert_no_secret_in_argv` screens every argument passed to the
wallet CLI and refuses credential-named flags, 32-byte-hex values and BIP-39-shaped values, with the
engine's own credentials passed in as forbidden substrings.
