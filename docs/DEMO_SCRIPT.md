# Deltr — 3:00 Demo Script (Track A video)

Shoot from this script with a stopwatch. Two takes. Nothing in the video is faked: prices are live, fills are labelled `paper` (or carry a testnet order id), stress is labelled `SIMULATED`, and any min-edge override is on screen.

## Window layout

* Left 40 %: Claude Desktop (or Claude Code in a terminal) connected to `deltr` (and, if authorised, `binance-mcp-server`).
* Right 60 %: the Deltr dashboard at <http://127.0.0.1:8000/app/> (the landing page with the Launch App button is at <http://127.0.0.1:8000/>).
* A terminal overlay for beats 2 and 6.

## Pre-flight (10 minutes before)

- [ ] `state/` directory is clean (`rm -rf state/paper`), so equity starts at $10 000 and drawdown at 0 %.
- [ ] `python main.py --min-edge-bps -20` running in PAPER (the amber **MIN-EDGE OVERRIDE −20 bps (PAPER)** chip is visible). Drop the override if the live net edge is ≥ 0 at recording time.
- [ ] `npx @modelcontextprotocol/inspector --transport http --server-url http://127.0.0.1:8000/mcp` → `tools/list` shows 22 tools; close it.
- [ ] Claude connected: `claude mcp add deltr --transport http http://127.0.0.1:8000/mcp` (or the Desktop `mcp-remote` block). Ask "Use deltr_status" once to warm the connection.
- [ ] Stress is reset (no red SIMULATED chip). Venue dots green with ages < 5 s.
- [ ] Prompts on a sticky note (below). Font zoom 125 % on the dashboard.
- [ ] Secrets: `secrets: absent` is fine for the paper take; for the testnet take, `secrets: present` and `BINANCE_API_ENV=testnet`.

## Beats

| Time | Beat | Do / say |
|---|---|---|
| 0:00–0:15 | **Hook** | Dashboard already live. Point at the header (mode `PAPER`, connection dot) and the KPI strip: equity $10 000, drawdown 0 % with the warn/halt marks, net edge with its verdict, the gate tile with "median (measured on this machine; 1.5 to 2.5 µs across our runs)", and the data-feeds tile with three green venues and their ages. *"Deltr runs one trade: long BNB on PancakeSwap V3, short the same quantity of the Binance perp. Delta-neutral by construction. Fifteen agents in this hackathon built a policy layer; this one built the strategy underneath it."* |
| 0:15–0:35 | **One command** | Terminal: `python main.py`. Banner: mode, hosts, `secrets: absent`, dashboard + MCP URLs, the Claude Code one-liner. Landing page renders in under 5 s; click **Launch App** (or open /app/ directly) for the dashboard. SpreadChart ticks; EdgeWaterfall shows today's honest numbers (basis ≈ +2 bps, round trip ≈ −16.6 bps, funding, net ≈ −14 bps, **not actionable**). *"Real PancakeSwap and Binance data, zero secrets. A naive bot sees a spread; Deltr prices the full round trip, and no order exists until a deterministic zero-LLM gate has cleared it."* Then the breakeven line from `deltr_explain_edge` / `deltr_edge_report`: *"at the measured testnet rate the carry never repays the round trip; at a mainnet-typical rate, which the screen labels an assumption and not a measurement, it breaks even in about 5.3 days, and my horizon is 3, so the answer is no."* |
| 0:35–1:15 | **Prompt → hedge (hero)** | Claude: **"Rebalance $5,000 USDC into delta-neutral BNB arbitrage."** Claude calls `deltr_prompt` → intent `rebalance 5000` with the USDC→USDT note → plan 4.85 BNB at 2x, cash $4 992, precheck **APPROVED** (19 green rows, microseconds). Claude: **"Execute that plan."** → `deltr_execute_hedge(plan_id)`. McpActivity shows `claude-desktop → deltr_prompt`, `→ deltr_execute_hedge`. TradeTrace: intent → scan → plan → gate → dex_fill (QuoterV2 price, `paper`) → cex_fill (mark − 2 bps, `paper` or testnet order id) → position → receipt sha256. PositionsTable: delta **0.00 BNB**, mark-to-close slightly negative with "breakeven in N settlements". If the override chip is on, say it: *"min-edge override −20 bps in paper mode so you can see the mechanics; otherwise the gate would have vetoed on NEGATIVE_EDGE."* |
| 1:15–1:45 | **The gate says no** | Claude: **"Do it again with $50,000 at 10x."** → VETO `LEVERAGE` "10.0 > 3.0 x" (red row, observed/limit, µs). **"Fine, $50,000 at 3x."** → VETO `CAPITAL_RISK` "$437 > $200 (2 % of $10 000)". **"Kill switch on."** → next propose VETO `KILL_SWITCH`. **"Kill switch off."** *"Pure Python, no model in the loop, every veto has a number."* |
| 1:45–2:20 | **Stress, halt, unwind** | PromptConsole stress row: **basis shock 130 bps** (SIMULATED chip appears) → position stop fires → auto-unwind receipt (perp reduce-only BUY, then DEX sell). **Equity shock 3.5 %** → drawdown bar crosses 3 %, status **HALTED** + SIMULATED. Claude: **"Hedge $2,000."** → VETO `HALTED_DRAWDOWN` "350 > 300 bps". **"Reset the halt."** → refused, `HALT_NOT_CLEARABLE` (still under water). Stress **RESET** → `deltr_reset_halt("demo reset")` → NORMAL, peak unchanged. *"The badge says simulated because the market feed was never touched."* |
| 2:20–2:45 | **Agent OS / two MCPs** | Terminal: `npx skills add https://github.com/mrnetwork0001/Deltr` (installs `skills/deltr-binance`) → `bash scripts/deltr.sh once --json` (edge, plan, precheck as JSON; nothing executes) → `npx tsx agents/agent_os_bridge.ts upstream-status` (*official: unreachable/unauthorised → shim: 9 tools*) → `list` (22 Deltr tools) → `route '{"symbol":"BNBUSDT","capital_usd":2000}'` (JSON-RPC receipt with plan_id + precheck). Flash `skills/deltr-binance/SKILL.md` (frontmatter: name, description, version, license, metadata.openclaw). *"Deltr is itself an MCP server, discovers the Binance MCP upstream, and ships as a Skills-Hub skill."* Say the provenance out loud: *"the shim serves six of its nine tools under official Agent OS names. Those names were transcribed from a third-party published inventory dated 2026-09-02, not captured from our own session, and the official endpoint has never been exercised from this machine."* |
| 2:45–3:00 | **Close** | `pytest -q -s` green with the printed gate benchmark; README hero (quickstart, MCP snippets, safety model with a real receipt JSON); repo URL. *"Delta-neutral by construction, deterministic by design."* |

## Sticky note (prompts, verbatim)

1. Rebalance $5,000 USDC into delta-neutral BNB arbitrage.
2. Execute that plan.
3. Do it again with $50,000 at 10x.
4. Fine, $50,000 at 3x.
5. Kill switch on. / Kill switch off.
6. Hedge $2,000.
7. Reset the halt.

## Honesty rules for the voice-over

* Say "paper" whenever a fill is simulated; say the order id when it is a testnet fill.
* Say "simulated" whenever the red chip is on; say that the feed was untouched.
* Say the override value if the amber chip is on.
* Quote the *measured* gate latency shown on the status bar, never a target, and never a single flattering number: the range across our runs is 1.5 to 2.5 µs.
* Funding numbers are testnet-derived and indicative; do not call them yield.
* If a mainnet-typical funding rate is on screen, say "assumption, not a measurement" and read the measured testnet rate beside it. Never present an assumed rate as measured.
* On the Agent OS beat, state the tool-name provenance: transcribed from a third-party published inventory dated 2026-09-02, not captured from our own session, and never verified against Binance. Never say Deltr connected to the official Binance MCP server.

## After recording

Upload (YouTube unlisted or X native), paste the link into `README.md` and the quote-repost, and run `docs/SUBMISSION_CHECKLIST.md` top to bottom.
