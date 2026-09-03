# Risk model — the 19 ordered checks

`risk_gate.py` is stdlib-only, deterministic and runs before every order (pre-check at propose time,
again at execute time on re-quoted prices). Checks run cheapest-and-most-decisive first and
short-circuit on the first failure; every veto carries the observed value, the limit and the unit, so a
client sees "leverage 10.0 > 3.0 x" rather than a bare code. The gate **owns** equity, peak, drawdown
state, the kill switch and the open-position registry: a caller cannot loosen a limit or claim a
balance, and `portfolio_balance` / `open_positions` supplied in a proposal are ignored.

A VETO is final for the same inputs. Change capital or leverage (or ask `deltr_explain_edge`) instead of
retrying. The full version with the test that proves each row is `docs/SAFETY_MODEL.md`.

| # | Code | Passes when | Default threshold |
|---|---|---|---|
| 1 | `KILL_SWITCH` | switch off, or a verified reduce-only unwind | operator flag |
| 2 | `HALTED_DRAWDOWN` | state != HALTED, or a verified unwind | drawdown >= 3 % halts (sticky) |
| 3 | `MALFORMED` | leverage, notional, allocated risk finite and > 0 (a missing leverage is a veto, never a default) | fail-closed |
| 4 | `REDUCE_ONLY_UNVERIFIED` | `reduce_only` binds a registered position with reversed sides and qty <= open qty | registry |
| 5 | `NOT_DELTA_NEUTRAL` | legs are DEX BUY / perp SELL (re-derived from the legs, not trusted from a flag) | structural |
| 6 | `HEDGE_MISMATCH` | \|dex_qty - perp_qty\| <= max(1 lot step, 0.5 %) | step 0.01 |
| 7 | `LEVERAGE` | leverage <= 3 | 3.0x |
| 8 | `MIN_NOTIONAL` | notional >= 5 USDT | exchange filter |
| 9 | `MAX_NOTIONAL` | notional <= per-mode cap | paper 50 000 / testnet 5 000 |
| 10 | `CAPITAL_RISK` | max(proposer risk, notional * (round trip + 100 bps)) <= 2 % of equity | $200 on $10 000 |
| 11 | `AGGREGATE_RISK` | open risk + new <= (3 % - drawdown) * equity | $300 budget at dd 0 |
| 12 | `CAPITAL_CAPACITY` | sum notional * (1 + 1/L) over open + new <= 90 % of equity | $9 000 on $10 000 |
| 13 | `AGGREGATE_NOTIONAL` | open notional + new <= 3 * equity | $30 000 on $10 000 |
| 14 | `SYMBOL_NOT_ALLOWED` | symbol in the configured whitelist | `DELTR_SYMBOLS` |
| 15 | `MAX_POSITIONS` | registered positions < max | 3 |
| 16 | `STALE_QUOTE` | quote age <= 5 000 ms (a fresh DEX quote up to 9 s is normalised to the limit) | 5 s |
| 17 | `PRICE_SANITY` | \|perp ref - DEX ref\| <= 100 bps | 100 bps |
| 18 | `PRICE_DRIFT` | \|re-quote - plan price\| <= 20 bps | 20 bps |
| 19 | `NEGATIVE_EDGE` | expected edge **at the re-quoted price** >= minimum | 3 bps default; PAPER may set a labelled override (>= -50) |

The three spec invariants (3x, 2 %, 3 %) are clamped in code: they can be tightened, never loosened.

## Measured latency

The gate is benchmarked on every start (10 000 evaluations) and the banner, the status bar and
`deltr_status.gate_median_us` quote **that measurement**, never a target. On the machine this skill was
packaged on (Apple M-series laptop, Python 3.11, 2026-09-02) `risk_gate.benchmark()` returned a
median of 1.5 to 2.5 µs across runs (p99 2.0 to 2.9 µs, amortised 1.8 to 2.1 µs). Reproduce it with:

```bash
.venv/bin/python -c "import risk_gate; print(risk_gate.benchmark())"   # (median_us, p99_us, amortised_us)
.venv/bin/python -m pytest -q -s tests/test_risk_gate.py                # prints the same numbers
```

Quote the number your machine prints.

## Drawdown state machine

`NORMAL` -> `WARN` at 2 % -> `HALTED` at 3 % (sticky). While HALTED only verified reduce-only unwinds
pass, so the book can always be flattened. `deltr_reset_halt(reason)` works only when the book is flat and
drawdown is back below 3 %; the peak is never re-based silently (`rebase_peak` is a separate, logged
operator action). Gate and portfolio state persist per mode under `state/<mode>/`, so a restart never
evades a halt. Every `drawdown_pct` a client sees is in **percent**.

## Execution discipline

* One lock around gate -> reserve -> execute: API, MCP, auto mode and the stop monitor cannot double-spend.
* Default leg order is DEX first; the second leg is sized from the first fill; a failed second leg reverses
  the first. A failed reversal books the naked leg as a one-legged position, engages the kill switch and
  keeps it stop-monitored until an operator unwinds it — a loss is booked, never hidden.
* Positions are marked **to close** (net of the estimated exit round trip); a per-position stop at
  `-allocated_risk_usd` auto-unwinds.
* Plans expire after 60 s, are single-use and are re-priced and re-gated at execution.
* Testnet orders are `LIMIT` + `IOC` only, with deterministic client order ids and query-before-retry, so a
  retry can never duplicate a fill. The DEX leg is simulated in both shipped modes.
* Stress scenarios mutate only the paper book, carry a persistent SIMULATED badge until `reset`, and are
  refused in TESTNET when they could touch a real order.

## What is deliberately not shipped

No production (live) mode, no mode-changing tool, no withdrawal or transfer code path, no free-text
execution. Funding figures are read from the Binance Futures testnet and are indicative only.
