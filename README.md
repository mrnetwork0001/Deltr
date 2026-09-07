# Deltr: CEX / DEX delta-neutral basis and funding agent on Binance Agent OS + MCP

**Deltr runs one trade.** It buys BNB on **PancakeSwap V3 (BNB Chain mainnet, quoted on-chain)** and
sells the same quantity of the **Binance USDS-M perpetual**, so the book carries no directional view.
What is left is the *basis* between the two venues plus the *funding* the short leg collects or pays.
Deltr prices that carry over an explicit horizon, and it prices the **entire** round trip (pool fee,
price impact, BSC gas, perp slippage, taker fee, both legs, entry and exit) before it will call
anything actionable. On live data today the arithmetic comes out around -14 bps and the agent
declines. That is the finding, and the numbers are on screen.

A scan of the public hackathon repositories on 2026-09-02 found no other entry running a two-venue
delta-neutral basis and funding strategy. That survey is ours and it is not exhaustive.

What the strategy actually pays, measured over 500 days of real Binance mainnet funding history
(public, read-only market data): [docs/STRATEGY_EVIDENCE.md](docs/STRATEGY_EVIDENCE.md). The short
version is that the round trip is the whole game, and execution style decides it. Taken, the round
trip is about 16.6 bps and carry clears it in 3.8 % of 7-day windows on BNBUSDT; posted, about
8.6 bps and 36.3 %. That result is why LIVE posts the perp leg as a maker by default and treats an
unfilled post-only order as a failure rather than crossing the spread.

**Market data is real Binance mainnet in every mode** (keyless, read-only). Deltr also has a LIVE
mode that places real mainnet orders and executes a real on-chain leg through the Binance Agentic
Wallet, and **it has run for real**: on 2026-09-07 the public LIVE engine opened a 0.01 BNB hedge
with a maker short on Binance USDⓈ-M Futures (order 95385108461) and a PancakeSwap buy signed by
the Agentic Wallet (BSC tx `0xc715c1e001fc30ebe4bc757f27f749b5ec909751693d86fba4adbf16061f4441`),
5.5 s apart, under a $25 cap and the labelled LIVE test override. The receipt is below.

Built for the Binance Agent OS Mini Hackathon (Track A). Apache-2.0, skill folder MIT.

## Try it live

| What | Where |
|---|---|
| Landing page | <https://usedeltrapp.vercel.app/> |
| Dashboard, real mainnet data, **PAPER / LIVE** switch in the header | <https://usedeltrapp.vercel.app/app/> |
| MCP, PAPER engine (22 tools) | `claude mcp add deltr --transport http https://usedeltrapp.vercel.app/mcp` |
| MCP, LIVE engine | `claude mcp add deltr-live --transport http https://usedeltrapp.vercel.app/live/mcp` |

The public instances are **read-only**: every read tool and every panel works for anyone; the
mutating tools and `POST` routes answer `READ_ONLY` unless the request carries that engine's
`DELTR_API_TOKEN`. The header switch changes which engine the page *reads*; it cannot change a mode,
and neither can any tool. The two engines are separate processes on one VPS, each with its own opt-in
(`docs/VPS_RUNBOOK.md`). The LIVE engine shows `offline` until its keys, acknowledgements and wallet
session are in place on that box.

| # | Claim | What backs it |
|---|---|---|
| 1 | **A real strategy on a real two-venue path.** | Long DEX / short perp, delta-neutral by construction: the pairing is re-derived from the legs, never trusted from a flag, and \|dex_qty - perp_qty\| must be inside one lot step. `net(H) = basis_entry + funding(H) - round_trip - assumed_exit_basis` over an explicit horizon (72 h, 9 settlements, by default). The DEX leg is quoted on-chain with `slot0` + QuoterV2 through `eth_call`, with gas priced into the round trip. |
| 2 | **Real mainnet data; three modes; keys only where they are needed.** | Market data is real mainnet everywhere: PancakeSwap V3 quotes (QuoterV2 on BSC mainnet), `fapi.binance.com` mark/index/funding and the public spot mirror, all keyless and read-only, on a client that never carries credentials. PAPER simulates fills on those prices and badges them `paper`. TESTNET places real *testnet* perp orders (LIMIT IOC); its DEX leg is simulated. LIVE places real mainnet perp orders (post-only by default) and a real on-chain leg that the **Binance Agentic Wallet** signs. Deltr never holds, reads, stores or signs with a private key, in any mode. No tool changes the mode. |
| 2b | **LIVE cannot arm by accident.** | Six requirements, each refused on its own by name: `DELTR_MODE=live`, both mainnet credentials, `BINANCE_API_ENV=mainnet`, `DELTR_LIVE_ACK`, the on-chain opt-in (`DELTR_ONCHAIN_MODE` + `DELTR_ONCHAIN_ACK`), plus an installed and signed-in wallet CLI checked before the first tick. Caps start at **$250 per trade and $1,000 aggregate** and the per-trade ceiling cannot be raised past the table. |
| 3 | **MCP-native both ways.** | Deltr *is* an MCP server (22 tools, streamable HTTP at `/mcp`, stdio with `--mcp`). The Agent OS bridge is an MCP *client* that discovers the Binance MCP upstream (official endpoint, then local shim, then none) and reports truthfully which one it reached. |

**Why the numbers are believable.** Every order is assembled by exactly one module and cleared by a
deterministic, zero-LLM Python risk gate: **19 ordered checks, stdlib only, 1.5 to 2.5 µs measured
across runs** (the startup banner prints the median measured on *your* machine, never a target). The
gate, not the caller, owns equity, peak, drawdown state, the kill switch and the open-position
registry, so a prompt cannot claim a balance or an empty book; an AST test in
`tests/test_gate_inputs.py` proves only the Executor can construct a gate input. Two runs over the
same recorded feed produce a byte-identical decision log. The gate is table stakes in this field.
It is here so the strategy above is auditable line by line.

## Architecture

```
 Claude / Claude Code / Codex / Cursor / VS Code            Binance Agent OS
 ───────────────────────────────────────────            ────────────────────────────
        │ MCP (streamable HTTP :8000/mcp or stdio)         hosted Binance MCP server
        ▼                                                  agent.binance.com/mcp/agentic
 ┌──────────────────┐   ┌──────────────────┐                    ▲ (OAuth, via the host)
 │ Deltr MCP server │   │ FastAPI  /api    │◀── dashboard ──┐   │
 │ 22 tools         │   │ + /ws/stream     │   (ui/out)     │   │  agents/agent_os_bridge.ts
 └────────┬─────────┘   └────────┬─────────┘                │   │  upstream: official → shim → none
          └──────────┬───────────┘                          │   ▼
                     ▼                                      │  deltr/mcp/binance_shim_server.py
              ┌─────────────┐                               │  (testnet-backed local twin)
              │   Engine    │  scan · explain · propose ·   │
              │ (facade)    │  evaluate · execute · unwind  │
              └──────┬──────┘                               │
     ┌───────────────┼──────────────────┐                   │
     ▼               ▼                  ▼                   │
 ArbitrageScout    Hedger            Executor ── asyncio.Lock
 (edge, history)   (sizing, plans)     │
                                       ▼
                              ╔════════════════════╗
                              ║  BinanceRiskGate   ║  19 ordered checks, stdlib only,
                              ║  (risk_gate.py)    ║  1.5-2.5 µs, owns equity/dd/registry
                              ╚═════════╤══════════╝
                                        ▼
          PaperRouter │ TestnetRouter (LIMIT IOC) │ LiveRouter (GTX post-only + Agentic Wallet)
                                        │
        ┌───────────────────────────────┼──────────────────────────────┐
        ▼                               ▼                              ▼
 PancakeSwap V3 (BSC mainnet)   Binance USDS-M Futures            Spot data mirror
 slot0 + QuoterV2 via eth_call  mainnet data (keyless) in every   bookTicker (reference)
 LIVE leg: Binance Agentic      mode; orders: testnet or, in      source: binance-spot-mirror
 Wallet CLI signs the swap      LIVE, fapi.binance.com
 source: bsc-mainnet-chain      source: binance-futures-mainnet
   / binance-agentic-wallet             / binance-futures-testnet
```

Provenance: every price, fill and history point carries a `DataSource` tag (`bsc-mainnet-chain`,
`binance-futures-mainnet`, `binance-futures-testnet`, `binance-spot-mirror`, `binance-agentic-wallet`,
`paper`, `replay`, `simulated`); the dashboard shows feed ages.
Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 60-second quickstart (Python only)

```bash
git clone https://github.com/mrnetwork0001/Deltr.git && cd Deltr
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python main.py
```

`requirements.lock` is the pinned set the suite is green on; `requirements.txt` holds the loose
ranges. Then open <http://127.0.0.1:8000> (landing page) and click **Launch App**; the dashboard
lives at <http://127.0.0.1:8000/app/> and the MCP endpoint at `http://127.0.0.1:8000/mcp`. The banner
prints the mode, the hosts, `secrets: absent`, the measured gate median and the MCP client
one-liners. If :8000 is taken, add `--port 8765` (3000/3001 are reserved for `npm run dev`).

| Command | What it does |
|---|---|
| `.venv/bin/python main.py --once --json` | one tick on live feeds: market, edge waterfall, breakeven horizon, sizing, plan, gate pre-check. Nothing executes |
| `.venv/bin/python main.py --replay tests/fixtures/replay.jsonl --min-edge-bps -20` | deterministic replay of the recorded feed (REPLAY badge, PAPER-only edge override shown on screen) |
| `.venv/bin/python main.py --mcp` | adds the stdio MCP transport to the same process (logs go to stderr) |
| `.venv/bin/python main.py --mode testnet` | real testnet perp leg; needs `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` |
| `.venv/bin/python main.py --mode live` | **REAL MONEY.** Refuses to start until every requirement below is present, naming the first one missing |
| `.venv/bin/python main.py --execution-style taker` | LIVE/TESTNET: cross the spread instead of posting. The measured economics say do not |
| `.venv/bin/python main.py --auto` | auto-execute gate-approved actionable opportunities (PAPER freely; TESTNET/LIVE need `DELTR_LIVE_AUTO_ACK` and run only at min edge >= 0) |
| `.venv/bin/python main.py --host 0.0.0.0 --port 8000` | bind all interfaces (the VPS showcase); default is loopback only |
| `.venv/bin/python -m pytest -q` | 684 offline, deterministic tests in about 8 s |

Node is only needed for the bridge (`npx tsx agents/agent_os_bridge.ts ...`) and `npm run dev`; the
dashboard is a static export in `ui/out/` served by FastAPI. Python 3.11+ (verified on 3.11, 3.12 and 3.14).

## Edge math: why the gate often says no

`net(H) = basis_entry + funding(H) - round_trip - assumed_exit_basis`, over a 72 h horizon
(9 funding settlements) by default. Worked example from the live probe on 2026-09-02 (pool mid 686.10,
buying 4.86 WBNB cost 3,334.89 USDT, testnet mark 686.34, `lastFundingRate` 0.0):

| Component | bps | Note |
|---|---|---|
| Entry basis (perp mark 686.34 vs DEX exec 686.19) | **+2.2** | the "spread" a naive bot sees |
| DEX pool fee x 2 | -2.0 | fee tier 100 |
| DEX price impact x 2 | -0.6 | QuoterV2 exact-output quote |
| Perp slippage x 2 | -4.0 | assumed 2 bps per leg |
| Perp taker fee x 2 | -10.0 | 5 bps per leg |
| BSC gas x 2 | -0.03 | about $0.0056 per swap at 0.05 gwei |
| Round trip | **-16.6** | everything above, both legs, entry and exit |
| Funding over 72 h | 0.0 | rate was 0.0 at the probe (testnet-derived, indicative) |
| **Net edge** | **about -14** | **not actionable** |

Sizing: both legs need cash, so `notional = capital / (1 + 1/L)`; "$5,000 at 2x" is $3,333 notional,
4.85 BNB at 686.19 (lot step 0.01, Decimal rounding). Ask `deltr_explain_edge(horizon_h=24)` for the
conservative what-if. Testnet funding is structurally near zero, so any figure drawn at a
mainnet-typical rate is an assumption and is labelled as one. For the video the PAPER-only
`--min-edge-bps` override is shown on screen so the mechanics are visible; prices are never faked.

## Connect your MCP client

| Client | Setup |
|---|---|
| Claude Code | `claude mcp add deltr --transport http http://127.0.0.1:8000/mcp` (the repo ships `.mcp.json` with `deltr` and `binance-mcp-server` side by side) |
| Claude Desktop | Settings > Developer > Edit Config, paste the block below (`claude_desktop_config.example.json` has it plus a stdio alternative) |
| Cursor | `.cursor/mcp.json`: `{ "mcpServers": { "deltr": { "url": "http://127.0.0.1:8000/mcp" } } }` |
| Codex CLI | `codex mcp add deltr --url http://127.0.0.1:8000/mcp` |
| VS Code | Chat > MCP Servers > Add > HTTP > `http://127.0.0.1:8000/mcp`, name `deltr` |
| Inspector | `npx @modelcontextprotocol/inspector --transport http --server-url http://127.0.0.1:8000/mcp` then `tools/list` shows 22 tools |

```json
{ "mcpServers": { "deltr": { "command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"] } } }
```

Keyless check without any client:

```bash
curl -s -X POST http://127.0.0.1:8000/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

| Tool | Use it for | Trades? |
|---|---|---|
| `deltr_status`, `deltr_market`, `deltr_scan`, `deltr_explain_edge` | status, market state + edge breakdown, latest opportunity, dry sizing plus the cost/carry math, the breakeven holding period and the edge-versus-horizon curve | no |
| `deltr_edge_report` | the same decomposition rendered as a titled markdown report: cost waterfall, funding over the horizon, breakeven verdict, every source tag and feed age, honesty labels | no |
| `deltr_propose_hedge(capital_usd, leverage)` | phase 1: size a plan, run the gate pre-check, get a `plan_id` (single-use, 60 s TTL) | no |
| `deltr_evaluate_risk(capital_usd, leverage)` | dry-run the 19-check gate; returns the full check matrix | no |
| `deltr_execute_hedge(plan_id, confirm)` | phase 2: re-price, re-gate, place both legs, return the receipt | yes |
| `deltr_unwind(position_id \| "all")` | reduce-only unwind (allowed while HALTED or with the kill switch on) | yes |
| `deltr_prompt(text)` | natural language to intent to plan to pre-check; propose-only, never executes | no |
| `deltr_positions`, `deltr_risk_log`, `deltr_receipt`, `deltr_activity` | book, gate decisions, one receipt by id, who called what | no |
| `deltr_kill_switch`, `deltr_reset_halt`, `deltr_set_min_edge`, `deltr_stress` | operator controls; stress mutates only the paper book and is badged SIMULATED | no |
| `deltr_funding_history(symbol, lookback_days)` | real mainnet funding history (up to 2,000 days) and the share of windows in which the carry beats the round trip at taker and at maker cost | no |
| `deltr_wallet_status` | the on-chain leg's state through the Binance Agentic Wallet CLI: installed, signed in, addresses, Binance's remaining daily quota, Deltr's arming state and caps | no |
| `deltr_onchain_swap(from_token, to_token, amount)` | request a swap through the Binance Agentic Wallet; the wallet holds the key, decides, applies its own limits on top of Deltr's caps, and broadcasts | yes (LIVE + on-chain opt-in only) |
| `deltr_x402_pay(payment_required)` | pay an HTTP 402 (x402 / B402) challenge on BNB Smart Chain through the wallet, inside Deltr's network allow-list and per-payment ceiling | yes (on-chain opt-in only) |

Resources: `deltr://status`, `deltr://risk-limits`, `deltr://config` (redacted). Errors come back as
`{"error": {"code", "message"}}`; a VETO is final for the same inputs. Full setup, including the
Binance upstream: [docs/MCP_SETUP.md](docs/MCP_SETUP.md).

## The demo prompts

| # | Prompt | What happens |
|---|---|---|
| 1 | *Rebalance $5,000 USDC into delta-neutral BNB arbitrage.* | `deltr_prompt`: intent `rebalance 5000`; USDC is treated as USDT-equivalent for sizing with no conversion leg (the note is on the result); plan about 4.85 BNB at 2x; gate pre-check with all 19 checks. Nothing executes. |
| 2 | *Execute that plan.* | `deltr_execute_hedge(plan_id)`: re-priced, re-gated, both legs, receipt with sha256. |
| 3 | *Do it again with $50,000 at 10x.* | VETO `LEVERAGE` "10.0 > 3.0 x". |
| 4 | *Fine, $50,000 at 3x.* | VETO `CAPITAL_RISK` "$437 > $200 (2 % of $10,000)". |
| 5 | *Kill switch on.* | the next proposal is vetoed `KILL_SWITCH`. |
| 6 | *Hedge $2,000.* (after a SIMULATED equity shock) | VETO `HALTED_DRAWDOWN` "350 > 300 bps"; *Reset the halt* is refused (`HALT_NOT_CLEARABLE`) until the book recovers. |

Second-by-second script: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).
Public read-only VPS showcase, step by step: [docs/VPS_RUNBOOK.md](docs/VPS_RUNBOOK.md).

## Safety model (short version)

| # | Check | Threshold |
|---|---|---|
| 1-2 | `KILL_SWITCH`, `HALTED_DRAWDOWN` | operator flag; drawdown >= 3 % halts (sticky, WARN at 2 %) |
| 3-4 | `MALFORMED`, `REDUCE_ONLY_UNVERIFIED` | fail-closed (a missing leverage is a veto); unwinds must bind a registered position |
| 5-6 | `NOT_DELTA_NEUTRAL`, `HEDGE_MISMATCH` | re-derived from the legs; \|dex_qty - perp_qty\| <= 1 lot step |
| 7-9 | `LEVERAGE`, `MIN_NOTIONAL`, `MAX_NOTIONAL` | <= 3x; >= 5 USDT; PAPER 50k / TESTNET 5k / **LIVE 250** |
| 10-13 | `CAPITAL_RISK`, `AGGREGATE_RISK`, `CAPITAL_CAPACITY`, `AGGREGATE_NOTIONAL` | <= 2 % of equity per trade (floored at round trip + 100 bps); <= (3 % - dd) in total; both legs' cash <= 90 %; <= 3x equity |
| 14-15 | `SYMBOL_NOT_ALLOWED`, `MAX_POSITIONS` | whitelist; 3 |
| 16-19 | `STALE_QUOTE`, `PRICE_SANITY`, `PRICE_DRIFT`, `NEGATIVE_EDGE` | 5 s; 100 bps; 20 bps re-quote at execute; edge re-priced at execution >= minimum |

| Property | How |
|---|---|
| Gate-owned inputs | The gate owns equity, peak, drawdown state, the kill switch and the open-position registry; a caller cannot claim a balance or an empty book. Only the Executor builds gate inputs (checked by AST in `tests/test_gate_inputs.py`). |
| LIVE is opt-in six times over | `DELTR_MODE=live`, both mainnet credentials, `BINANCE_API_ENV=mainnet`, `DELTR_LIVE_ACK`, the on-chain opt-in, and a wallet CLI that is installed **and** signed in. The first five are refused at settings load; the last two by a preflight that runs before the first tick. `BINANCE_API_ENV=prod` stays refused in every mode. Only `Mode.LIVE` carries a mainnet order host, and `FuturesClient` refuses to sign against mainnet without an explicit `allow_mainnet_orders` that only the LIVE path passes, and then only for `fapi.binance.com`. |
| Custody: Deltr holds no key | The on-chain leg is delegated to the Binance Agentic Wallet CLI. Binance's wallet custodies the key, applies its own spending limits and performs the signing; Deltr shells out and reads JSON. There is no signer, no key parameter and no raw transaction path in the codebase, and AST tests keep it that way. |
| Maker by default | LIVE posts the perp leg `timeInForce=GTX` (post-only). GTX is the exchange's guarantee, not Deltr's arithmetic: a crossing order is rejected, not filled. An unfilled maker order is a **failure** that reverses the other leg; it is never a silent one-legged trade and never falls back to crossing. A reversal, which closes an exposure that already exists, does cross, and the receipt says so. |
| LIVE caps | `MAX_NOTIONAL` $250 per trade (a table ceiling: `DELTR_LIVE_MAX_NOTIONAL_USD` can only lower it), $1,000 aggregate open notional, and the separate on-chain caps of $250 per request / $1,000 per run. |
| CONFIRM is a convention | TESTNET and LIVE `confirm: true` mirrors the Binance CLI habit; the controls are the gate and the caps, not the flag. |
| SIMULATED labelling | Stress scenarios mutate only the paper book, never the feed, and paint a persistent SIMULATED badge until reset. |
| Failed reversal | A naked leg is booked as a one-legged position under the stop monitor and the kill switch engages; a flat book never hides an open order. |
| Restart | Gate and portfolio state persist per mode under `state/<mode>/`; a halt survives a restart. |

Full table with tests and the drawdown state machine: [docs/SAFETY_MODEL.md](docs/SAFETY_MODEL.md).

One real receipt, produced here on 2026-09-03 by
`.venv/bin/python main.py --once --json --auto --replay tests/fixtures/replay.jsonl --min-edge-bps -20`
(paper fills on the recorded feed; the 19-row `checks` array is collapsed to its names, everything
else verbatim; ids and hashes differ per run):

```json
{"id": "rcpt_f2a8a1d4dfc4", "plan_id": "plan_877d941777e1", "source": "auto", "mode": "paper", "status": "filled",
 "decision": {"approved": true, "code": "OK", "latency_us": 12.459, "dd_state": "NORMAL", "drawdown_pct": 0.0,
   "checks": ["KILL_SWITCH", "HALTED_DRAWDOWN", "MALFORMED", "REDUCE_ONLY_UNVERIFIED", "NOT_DELTA_NEUTRAL",
              "HEDGE_MISMATCH", "LEVERAGE", "MIN_NOTIONAL", "MAX_NOTIONAL", "CAPITAL_RISK", "AGGREGATE_RISK",
              "CAPITAL_CAPACITY", "AGGREGATE_NOTIONAL", "SYMBOL_NOT_ALLOWED", "MAX_POSITIONS", "STALE_QUOTE",
              "PRICE_SANITY", "PRICE_DRIFT", "NEGATIVE_EDGE"]},
 "plan": {"qty": 4.84, "notional_usd": 3331.62, "leverage": 2.0, "expected_edge_bps": -15.48,
   "roundtrip_cost_bps": 16.75, "plan_hash": "d2f49153bfabbd4f9181c76fd56f35a87e85b3a34a987bdcaa3911c33b6fb91e"},
 "fills": [
   {"venue": "pancakeswap_v3", "side": "BUY", "qty": 4.84, "price": 688.420916, "fee_usd": 0.005,
    "simulated": true, "source": "paper", "reference_divergence_bps": 2.36},
   {"venue": "binance_futures", "side": "SELL", "qty": 4.84, "price": 688.302312, "fee_usd": 1.6657,
    "simulated": true, "source": "paper", "client_id": "DLTR941777e111", "reference_divergence_bps": -2.0}],
 "position_id": "pos_6427b1c93369", "residual_delta_base": 0.0, "realized_cost_usd": 1.6707, "legging_window_ms": 0,
 "steps": ["plan", "scan", "gate", "dex_fill", "cex_fill", "position", "receipt"],
 "sha256": "d4e395097ba10f9c13cc348a9f0e164195d8a4acc56c02c5018c27c6c216fda5", "ts": "2026-09-03T04:13:03.496714Z"}
```

The `NEGATIVE_EDGE` row passed only because the PAPER override lowered the minimum to -20 bps; at the
default minimum the same replay returns `VETO NEGATIVE_EDGE: -15.28 bps < 3.00 bps` and no receipt
(from a clean `state/paper/replay/`; if you ran the receipt command first, the paper position it
opened makes the next default run veto `CAPITAL_CAPACITY` instead, which is the aggregate-capacity
check doing its job). The agent has never traded on its own signal on live data: every live scan so
far has come out between -10 and -15 bps and declined. The one mainnet hedge it holds was opened
under the labelled LIVE test override (below), not on a positive edge.

## Agent OS skill (Track A)

```bash
npx skills add https://github.com/mrnetwork0001/Deltr      # installs skills/deltr-binance
bash scripts/deltr.sh status                                 # JSON status (runs --once when nothing is up)
bash scripts/deltr.sh once --json                            # edge, plan, gate pre-check; nothing executes
bash scripts/deltr.sh propose 5000 2                         # phase 1 against the running instance -> plan_id
bash scripts/deltr.sh execute <plan_id>                      # phase 2 (add --confirm in TESTNET)
```

`skills/deltr-binance/SKILL.md` (mirrored to `.agents/skills/deltr-binance/`) carries the Skills Hub
frontmatter (`name`, `description`, `version`, `license: MIT`, `metadata.version/author/openclaw`), the
command routing table and `references/{tools,cli,risk-model}.md`; `tests/test_skill_manifest.py` keeps
the tool table in sync with the server. `scripts/deltr.sh` talks to a running instance over the REST
API (`DELTR_API`, default `http://127.0.0.1:8000`) and never prints secrets.

## What Deltr connects to, and what it does not

| Item | Status |
|---|---|
| Deltr MCP server (22 tools) | exercised end to end by the tests, the inspector, `curl` and the bridge |
| Official Binance MCP (`https://agent.binance.com/mcp/agentic`) | **never exercised from this build machine**: this machine's default resolver refuses the hostname (a public resolver does resolve it) and the endpoint requires OAuth through a supported host, which has not been completed here. With `BINANCE_MCP_URL` set the bridge reports `official upstream unavailable: fetch failed (getaddrinfo ENOTFOUND agent.binance.com)` and falls back. The bridge's three-state result stays on screen |
| Local shim (`deltr/mcp/binance_shim_server.py`) | stdio, 9 tools shaped like the market / account / trade scopes, backed by the Binance Futures testnet and the public spot mirror; refuses `prod`. `upstream-status` reports `kind: shim` with the official failure reason attached |
| Binance tool names | the shim serves 6 of its 9 tools under official Agent OS names (`spot.ticker24hr`, `spot.depth`, `futures_usds.premiumIndexKlineData`, `futures_usds.exchangeInformation`, `futures_usds.futuresAccountBalanceV3`, `futures_usds.positionInformationV2`) read from `tests/fixtures/binance_mcp_tools.json`. Those names were **transcribed from a third-party published inventory dated 2026-09-02, not captured from our own session**, and are not claimed to have been verified against Binance; the fixture's `provenance` block says so and `tests/test_shim_tool_names.py` enforces it. That inventory lists no write-capable trade tool, so `place_futures_order`, `set_leverage` and `get_funding_rate` keep Deltr-local names. See `docs/MCP_SETUP.md` § Tool-name compatibility |
| Order path | the Python executor never routes an order through an upstream MCP. Testnet orders go straight to the Binance Futures testnet REST API, signed locally, behind the gate; LIVE perp orders go the same way to `fapi.binance.com`. The LIVE on-chain leg goes to the Binance Agentic Wallet CLI, which signs it. The bridge forwards only `get_*` upstream calls; trade-shaped calls are refused and routed through Deltr's gate |
| Mainnet orders | **placed, on the public LIVE engine (2026-09-07).** One 0.01 BNB hedge: Binance USDⓈ-M maker short, order 95385108461, and a PancakeSwap V3 buy signed by the Agentic Wallet, BSC tx `0xc715c1e0…f4441`. Three earlier attempts failed safely and each taught the wrapper a real CLI field (see "First mainnet hedge") |
| `fapi.binance.com` from this machine | reachable and answering 200 **only through a public resolver**: this build machine's default resolver returns nothing for the hostname. Deltr raises an actionable DNS error naming both `dig` commands rather than substituting another venue or simulated data. The public showcase VPS resolves it and runs on live mainnet data |

```bash
npx tsx agents/agent_os_bridge.ts upstream-status     # official -> shim -> none, with the reason
npx tsx agents/agent_os_bridge.ts list                # 22 Deltr tools (needs `python main.py` running; set DELTR_MCP_URL if not on :8000)
npx tsx agents/agent_os_bridge.ts route '{"symbol":"BNBUSDT","capital_usd":2000}'   # JSON-RPC receipt: plan_id + precheck
npx tsx agents/agent_os_bridge.ts serve               # JSON-RPC 2.0 facade on :8788
```

The hackathon's second item ("connect your MCPs and trade") is a first-come task reward of 4 USDC to
the first 10,000 users who complete a spot, a futures and a margin-or-convert trade through the
Binance MCP server. It is not a judged prize track, and it is completed by you, in a supported host,
on your own Agentic sub-account. `.mcp.json` lists `binance-mcp-server` next to `deltr` so both sit in
the same Claude Code session; Deltr does not perform that task on your behalf and never claims a
`tools/call` against the official endpoint succeeded.

## Testnet mode

| Setting | Value |
|---|---|
| Env | `BINANCE_API_ENV=testnet`, `BINANCE_API_KEY`, `BINANCE_SECRET_KEY` (same names as the official `binance` skill; values are never printed; `prod` is refused) |
| Startup | `prepare_account` syncs time, reads the exchange filters, sets isolated margin and the configured leverage; Deltr refuses to start if the account's real leverage differs from the configured one or exceeds 3x |
| Orders | `LIMIT` + `IOC` only, deterministic client ids, query-before-retry; every fill records its divergence from mark |
| Caps | `MAX_NOTIONAL` 5,000 USDT; `deltr_execute_hedge` / `deltr_unwind` need `confirm: true` |
| DEX leg | always simulated at the live QuoterV2 price and badged `simulated` in the receipt |

## LIVE mode (real money)

LIVE has run on the public showcase VPS with about $20 of real funds; the first mainnet hedge and
the three failed attempts before it are documented at the end of this section.

LIVE refuses to start unless **all six** of these are true, and the refusal names the first one that
is not:

| # | Requirement | Refused by |
|---|---|---|
| 1 | `DELTR_MODE=live` | mode selection; no tool can change it at runtime |
| 2 | `BINANCE_API_KEY` **and** `BINANCE_SECRET_KEY` | settings load |
| 3 | `BINANCE_API_ENV=mainnet` (the switch that says these are mainnet keys; `prod` stays refused) | settings load |
| 4 | `DELTR_LIVE_ACK=i-understand-this-trades-real-money` | settings load |
| 5 | `DELTR_ONCHAIN_MODE=live` **and** `DELTR_ONCHAIN_ACK=i-understand-this-moves-real-funds` | settings load |
| 6 | the Binance Agentic Wallet CLI installed **and** reporting a signed-in session | engine preflight, **before the first tick** |

```
$ .venv/bin/python main.py --mode live
deltr: invalid configuration: the LIVE acknowledgement is not set: set
DELTR_LIVE_ACK=i-understand-this-trades-real-money to confirm you understand this places real
orders with real money.

$ ... (everything above set, wallet signed out)
deltr: startup failed: RuntimeError: REFUSING TO START LIVE: the Binance Agentic Wallet is not
signed in (status UNCONNECTED). run `baw auth signin --json`, scan the QR code in the Binance
Wallet app, then `baw auth verify --qrCodeId <id> --json`. Deltr never receives, stores or signs
with the key: the wallet keeps custody and does the signing.
```

When it does start, the banner says so unmistakably, with the caps and the wallet's **public**
address and never a secret:

```
 *** LIVE: REAL FUNDS ARE ARMED. Orders from this process spend real money on Binance mainnet
     and on BNB Smart Chain. ***
 execution  : maker: post-only (GTX) perp leg, never crosses the spread
 data       : binance-futures-mainnet (keyless, read-only) + binance-spot-mirror + bsc-mainnet-chain
 caps       : per trade $250   aggregate $1,000   on-chain $250/request, $1,000/run
 wallet     : Binance Agentic Wallet, signed in, chainId 56   address bsc=0x...
              custody: Binance's wallet holds the key, applies its own limits and does the signing.
              Deltr never holds, reads, stores or signs with a private key.
```

| Aspect | LIVE |
|---|---|
| Perp leg | `fapi.binance.com`, HMAC-signed REST, `timeInForce=GTX` (post-only). Posted at the touch on our own side; a post-only rejection re-prices, never crosses. Re-posted up to 3 times inside one 8 s budget, cancelled at the end, partials kept |
| Unfilled maker order | a **failure**: the other leg is reversed by the Executor's existing reverse-on-failure path. Never a silent skip, never a fallback to a taker order |
| On-chain leg | a real PancakeSwap swap requested through `baw market-order quote` then `swap`, then confirmed by `market-order list --orderId`. An unconfirmed swap is never recorded as a fill. The Fill's `ref` is the transaction hash and its `source` is `binance-agentic-wallet` |
| Custody | Binance's Agentic Wallet holds the key, enforces its own daily limits and performs the signing. **Deltr never holds, reads, stores or signs with a private key**, and there is no code path that would accept one |
| Reversal | real, in both directions: a filled on-chain leg is reversed by the opposite swap. A reversal that fails books a naked one-legged position under the stop monitor and engages the kill switch |
| Gate | unchanged and in front of every order, exactly as in PAPER and TESTNET. The LIVE aggregate cap sits *after* the gate and can only subtract from what it approved |
| Caps | $250 per trade (a ceiling: `DELTR_LIVE_MAX_NOTIONAL_USD` can only lower it), $1,000 aggregate |
| Min edge | floor 0 bps outside PAPER: net edge is already net of the round trip, so a real-order run never targets a net loss but is not asked to clear the costs twice. Today's live edge is negative, so LIVE mostly declines |
| Test override | `DELTR_LIVE_TEST_ACK=i-accept-a-small-known-loss` lets the min edge go negative in LIVE **only while the per-trade cap is at most $25**, so one tiny real hedge can exercise the whole path on a negative-edge day for a loss of cents. The banner and the header say LIVE TEST OVERRIDE while it is on |
| Unattended | `--auto` (or `DELTR_AUTO_EXECUTE=true`) runs with real orders only with `DELTR_LIVE_AUTO_ACK=i-understand-this-trades-real-money-unattended`, and then only while the min edge is >= 0: any override switches unattended trading off instead of letting it chase a known loss. The engine opens at most one position per symbol, holds it, and re-arms after the stop monitor closes it. The header shows AUTO, or AUTO · STANDING DOWN with the reason |
| Market data | the same keyless mainnet client PAPER and TESTNET use. A credentialed client never polls a public endpoint in any mode |

## Public read-only deployment

The showcase above is `python main.py` with four settings, installed by `scripts/vps_install.sh` on a
box that already ran other services (it creates only its own user, venv, state dir, env file and
unit; `INSTANCE=live` installs the second, disarmed engine):

| Setting | Effect |
|---|---|
| `DELTR_PUBLIC_READONLY=1` | every mutating `POST` route and MCP tool answers `READ_ONLY` without the token |
| `DELTR_API_TOKEN` | the `X-Deltr-Token` header value that re-enables them for you |
| `DELTR_MCP_ALLOWED_HOSTS` | the public `ip:port` / hostnames the MCP transport accepts besides loopback (proxies may send the bare host; it is admitted too) |
| `DELTR_CORS_ORIGINS` | the browser origin allowed to call the API (the Vercel site) |

Vercel serves the static site and proxies `/api`, `/mcp` (PAPER) and `/live/api`, `/live/mcp` (LIVE)
to the two engines, so the browser only ever talks to one https origin. Step by step:
[docs/VPS_RUNBOOK.md](docs/VPS_RUNBOOK.md).

### First mainnet hedge (2026-09-07, public LIVE engine, $25 cap, LIVE test override on)

```json
{"id": "rcpt_56d15285fc33", "plan_id": "plan_342c864c6e63", "source": "api", "mode": "live", "status": "filled",
 "position_id": "pos_87fed9625f83", "residual_delta_base": 2.82e-06, "realized_cost_usd": 0.0076, "legging_window_ms": 5500,
 "decision": {"approved": true, "code": "OK", "latency_us": 54.201, "dd_state": "NORMAL", "checks": ["KILL_SWITCH", "…", "NEGATIVE_EDGE"]},
 "plan": {"qty": 0.01, "notional_usd": 7.49, "leverage": 2.0, "expected_edge_bps": -19.45, "roundtrip_cost_bps": 25.21},
 "fills": [
   {"venue": "binance_futures", "side": "SELL", "qty": 0.01, "price": 749.28, "fee_usd": 0.0015, "simulated": false,
    "source": "binance-futures-mainnet", "ref": "95385108461"},
   {"venue": "pancakeswap_v3", "side": "BUY", "qty": 0.0100028, "price": 748.886, "fee_usd": 0.0061, "simulated": false,
    "source": "binance-agentic-wallet", "ref": "0xc715c1e001fc30ebe4bc757f27f749b5ec909751693d86fba4adbf16061f4441"}],
 "steps": ["plan", "scan", "gate", "cex_fill", "dex_fill", "position", "receipt"],
 "sha256": "a4c2d82d9c0abefc75629f056bffafbdb38367ad3ed0de1944783350b54de490", "ts": "2026-09-07T03:19:55.810443Z"}
```

The BSC transaction reads back from a public RPC as status `0x1` in block 120421309: 7.49 USDT out of
the wallet, 0.010003 WBNB in. The expected edge was -19 bps; the trade exists to prove the path, and
it lost about a cent of fees as expected.

What the three attempts before it did, all with real money and all handled by the safety paths:

| Attempt | What happened | What it fixed |
|---|---|---|
| 1 | perp maker short filled; the wallet's swap preview read as "would receive 0"; Deltr refused the leg and reversed the perp (cost $0.013) | the quote parser did not know baw 1.9's `toCoinAmount` field |
| 2 | maker short posted for 8 s, never hit, cancelled; nothing to reverse | the maker window is a setting (`DELTR_MAKER_WAIT_MS`), raised to 30 s for the test |
| 3 | perp filled; the swap executed on chain in 2 s, but the swap command echoed a different order id than the wallet booked, the confirmation poll saw an empty page, Deltr called the leg unconfirmed, engaged the kill switch and reversed the perp; the WBNB was swapped back by hand | confirmation now matches the booked order on tokens, amount and time, and reads `toTokenActualQty` |

Every one of those receipts is on the LIVE engine's risk log and receipt list, unedited.

## Tests and the measured benchmark

```bash
.venv/bin/python -m pytest -q                                   # 684 passed, 9 skipped in about 8 s (offline)
.venv/bin/python -m pytest -q -s tests/test_risk_gate.py        # prints the gate median measured here
.venv/bin/python -c "import risk_gate; print(risk_gate.benchmark())"   # (median_us, p99_us, amortised_us)
npm run typecheck && npm run bridge:test                        # TypeScript bridge (Node only)
```

Counts are whatever the suite prints: 684 passed, 9 skipped on 2026-09-07 with the command above.
Across repeated runs on this Apple M-series laptop the gate median lands between **1.5 and 2.5 µs**
(p99 between 2.0 and 2.9 µs); the number in your banner is the one that counts, and the test asserts
only that it stays under 5 µs so slower machines stay green. The suite is offline and deterministic:
`conftest.py` blocks real network, and the replay fixture (`tests/fixtures/replay.jsonl`, 305 recorded
`MarketState/v1` rows) drives the integration, API, MCP and determinism tests.

## Video, license, disclaimer

Demo video: *link added at submission*. Code Apache-2.0 (`LICENSE`); `skills/deltr-binance` MIT.

Deltr is a technical demonstration and does not provide financial, investment, legal or tax advice.
Nothing it outputs is a recommendation to enter any position, and you are solely responsible for any
decision you make with it.

Funding figures are testnet-derived and indicative; edge estimates are model outputs, not fills;
nothing here trades on production venues.
