# Deltr Architecture

One process, one engine, two transports. Everything an agent can do goes through the same `Engine`, and every order goes through the same gate.

```
 Claude / Claude Code / Codex / Cursor / VS Code            Binance Agent OS
 ───────────────────────────────────────────            ────────────────────────────
        │ MCP (streamable HTTP :8000/mcp or stdio)         hosted Binance MCP server
        ▼                                                  agent.binance.com/mcp/agentic
 ┌──────────────────┐   ┌──────────────────┐                    ▲ (OAuth, via the host)
 │ Deltr MCP server │   │ FastAPI  /api    │◀── dashboard ──┐   │
 │ 18 tools         │   │ + /ws/stream     │   (ui/out)     │   │  agents/agent_os_bridge.ts
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
                              ║  (risk_gate.py)    ║  1.5-2.5 µs measured, owns equity/dd/registry
                              ╚═════════╤══════════╝
                                        ▼
                         PaperRouter │ TestnetRouter (LIMIT IOC, signed)
                                        │
        ┌───────────────────────────────┼──────────────────────────────┐
        ▼                               ▼                              ▼
 PancakeSwap V3 (BSC mainnet)   Binance USDⓈ-M Futures testnet   Spot data mirror
 slot0 + QuoterV2 via eth_call  premiumIndex, bookTicker, orders  bookTicker (reference)
 source: bsc-mainnet-chain      source: binance-futures-testnet   source: binance-spot-mirror
```

## Data flow (one tick)

1. `MarketDataHub` polls the three venues (CEX every 1 s, DEX every 3 s) and builds a `MarketState` with per-feed ages and a `Freshness` verdict. `ReplayHub` replays a recorded fixture through the same interface (badge `REPLAY`).
2. `ArbitrageScout.on_tick` computes the horizon-based edge (`deltr/edge.py`): entry basis + funding over the horizon − full round-trip cost − assumed exit basis, and marks the opportunity actionable or not with a reason.
3. `Portfolio.mark` marks open positions **to close**, accrues funding at settlement crossings, and feeds equity into the gate (`update_equity`).
4. Optional: `--auto` in PAPER proposes and executes gate-approved opportunities; otherwise a human or an LLM drives `propose` → `execute` through MCP, the API or the prompt console.

## Two-phase execution

`propose` sizes a plan (`N = capital / (1 + 1/L)`, floored to the lot step), runs the gate as a **pre-check**, and returns a single-use `plan_id` that expires in 60 s. `execute(plan_id)` re-quotes the DEX leg, rebuilds the gate input from live state (the executor is the only module allowed to do that), re-runs the gate, and only then places the legs: DEX first by default, the perp sized from the actual DEX fill, reverse-on-failure if the second leg fails. Every receipt embeds the ordered trace and a SHA-256.

## Provenance

Every price, funding rate, fill and history point carries a `DataSource` tag. The dashboard shows a legend with feed ages; the README states which Binance MCP upstream (official or shim) was actually exercised.

## Modes

Market DATA is real Binance mainnet in every mode: `fapi.binance.com` for mark, index and funding,
`data-api.binance.vision` for the spot reference, and BSC mainnet for the PancakeSwap V3 quote. All
of it is keyless and read-only, on a client that never carries credentials. `api.binance.com`
(mainnet spot REST) is deliberately not used: it answers 403 from many networks.

| Mode | Perp leg | DEX leg | Secrets | Per-trade cap |
|---|---|---|---|---|
| `paper` (default) | simulated at quoted prices (labelled `paper`) | simulated | none | $50,000 |
| `testnet` | real USDⓈ-M **testnet** LIMIT IOC orders | simulated | testnet keys | $5,000 |
| `live` | real **mainnet** orders, post-only (GTX) by default | **real**, signed by the Binance Agentic Wallet | mainnet keys + acknowledgements | **$250** |

LIVE is opt-in six times over and refuses to start unless every part is present, naming the first
one that is not; see `docs/SAFETY_MODEL.md`. **No mainnet order has been placed.**

## Execution style

`DELTR_EXECUTION_STYLE` is `maker` by default. Maker means `timeInForce=GTX` (post-only): the
exchange rejects the order rather than letting it take liquidity, so Deltr never has to trust its
own price arithmetic to stay a maker. This is an economics requirement, not a preference: measured
over 500 days of real mainnet funding, the round trip is about 16.6 bps taken (10 bps of which is
the taker fee) and clears in 3.8 % of 7-day windows on BNBUSDT, against about 8.6 bps posted and
36.3 %. `deltr/maker.py` posts at the touch on its own side, re-prices when the market walks away,
cancels at the end of the budget, and keeps whatever the venue reported filled. **An unfilled maker
order is a failure**, handled by the Executor's reverse-on-failure path; there is no fallback that
crosses the spread. The one exception is a *reversal*, which closes an exposure that already exists
and therefore crosses; receipts record that.

Note that the gate's cost model still quotes the TAKER round trip. That is deliberate: it makes the
gate demand more edge than a maker fill actually costs.

## Custody: the on-chain leg

The LIVE DEX leg is delegated to the **Binance Agentic Wallet CLI** (`baw`). Binance's wallet
custodies the private key, applies its own spending limits and performs the signing; Deltr shells
out to the CLI and reads its JSON. **Deltr never holds, reads, stores or signs with a private key**,
there is no signer and no key parameter anywhere in the codebase, and AST tests in
`tests/test_agentic_wallet.py` keep it that way.

`deltr/onchain_leg.py` keeps the two jobs apart on purpose: price discovery for the gate stays on
the read-only PancakeSwap V3 quoter over a public BSC RPC, and only *execution* goes to the wallet.
The resulting `Fill` carries the transaction hash as its `ref` and `binance-agentic-wallet` as its
`source`, so a receipt can always be pointed at a real transaction. A swap the CLI has not confirmed
is never recorded as a fill.

## Repository map

```
main.py                     one-command launcher (API + dashboard + optional stdio MCP)
risk_gate.py                deterministic gate (subsystem 4)
agents/arbitrage_scout.py   subsystem 1 — edge + opportunities
agents/hedger.py            subsystem 2 — sizing + plans
agents/agent_os_bridge.ts   subsystem 3 — MCP client / JSON-RPC router / upstream discovery
deltr/models.py             pydantic contracts (frozen)
deltr/config.py             settings, frozen host table, per-mode caps
deltr/edge.py               edge / fee / funding / sizing math
deltr/market_data.py        polling hub + replay hub
deltr/portfolio.py          positions, mark-to-close, funding, persistence
deltr/executor.py           the choke point: gate → legs → receipts (PaperRouter / TestnetRouter / LiveRouter)
deltr/engine.py             the facade everything talks to
deltr/maker.py              post-only (GTX) perp execution: post, re-price, cancel, report
deltr/onchain_leg.py        the LIVE DEX leg, executed by the Binance Agentic Wallet (no key here)
deltr/funding_history.py    real mainnet funding history + the carry analysis behind the evidence
deltr/venues/agentic_wallet.py    the `baw` CLI wrapper (custody stays with Binance)
deltr/mcp/server.py         Deltr MCP server (22 tools)
deltr/mcp/binance_shim_server.py  local twin of the Binance MCP scopes
deltr/api/                  FastAPI routes + WebSocket
deltr/venues/               PancakeSwap V3 (eth_call), Binance Futures testnet, spot mirror
ui/                         Next.js 14 landing page (/) + dashboard (/app/), static export served by FastAPI
skills/deltr-binance/       Binance Skills Hub packaging
tests/                      offline, deterministic; replay fixture recorded live
```
