# Connect your MCP client to Deltr (and Deltr to Binance)

Deltr is an MCP server. Start it once, then point any MCP host at it. The same process also serves the dashboard and the REST API, so what your agent does is what the dashboard shows.

```bash
python main.py            # landing http://127.0.0.1:8000  ·  dashboard /app/  ·  MCP http://127.0.0.1:8000/mcp
```

## 1. Deltr as an MCP server (Streamable HTTP, recommended)

| Client | Setup |
|---|---|
| **Claude Code** | `claude mcp add deltr --transport http http://127.0.0.1:8000/mcp` then `/mcp` → `deltr` |
| **Claude Desktop** | Settings → Developer → Edit Config, add the `mcp-remote` block below |
| **Cursor** | `.cursor/mcp.json` → `{ "mcpServers": { "deltr": { "url": "http://127.0.0.1:8000/mcp" } } }` |
| **Codex CLI** | `codex mcp add deltr --url http://127.0.0.1:8000/mcp` |
| **VS Code** | Chat → MCP Servers → + → HTTP → `http://127.0.0.1:8000/mcp`, name `deltr` |
| **Any client (inspector)** | `npx @modelcontextprotocol/inspector --transport http --server-url http://127.0.0.1:8000/mcp` |

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "deltr": { "command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"] }
  }
}
```

The repo ships `.mcp.json` (Claude Code project scope) and `claude_desktop_config.example.json`.

### stdio alternative

`python main.py --mcp` adds a stdio MCP transport to the **same** process (it still serves the dashboard on :8000). Use it when a host only speaks stdio:

```json
{
  "mcpServers": {
    "deltr": { "command": "/ABSOLUTE/PATH/Deltr/.venv/bin/python", "args": ["/ABSOLUTE/PATH/Deltr/main.py", "--mcp"] }
  }
}
```

Do not run a second `python main.py` next to the stdio one; two engines would mean two books.

## 2. The 22 Deltr tools

| Tool | Use it for |
|---|---|
| `deltr_status` | mode, venues, equity, drawdown state, kill/halt, measured gate latency |
| `deltr_market` | current DEX / perp / funding state and the edge breakdown |
| `deltr_scan` | the latest opportunity + spread history |
| `deltr_explain_edge` | dry sizing, the full cost/carry math, the breakeven holding period and the edge-versus-horizon curve |
| `deltr_propose_hedge` | **phase 1**: size a plan, run the gate pre-check, get a `plan_id` (nothing executes) |
| `deltr_evaluate_risk` | dry-run the gate (leverage 10 → `LEVERAGE` veto with observed/limit) |
| `deltr_execute_hedge` | **phase 2**: execute a `plan_id` (re-priced, re-gated, single-use; TESTNET needs `confirm: true`) |
| `deltr_unwind` | reduce-only unwind of a position or `"all"` |
| `deltr_positions` | open positions + portfolio (mark-to-close) |
| `deltr_risk_log` | recent gate decisions with every check's observed/limit |
| `deltr_receipt` | a receipt by id (trace steps + SHA-256) |
| `deltr_activity` | the inbound MCP call log (which client called what) |
| `deltr_prompt` | natural language → intent → plan → pre-check (propose-only) |
| `deltr_kill_switch` | operator kill switch on/off |
| `deltr_reset_halt` | clear a drawdown halt once the book has recovered |
| `deltr_set_min_edge` | runtime min-edge (PAPER may go negative, labelled on screen) |
| `deltr_stress` | labelled SIMULATED scenarios on the paper book; `reset` clears |
| `deltr_edge_report` | the same decomposition as a titled markdown report: waterfall, funding, breakeven verdict, source tags with feed ages, honesty labels |
| `deltr_funding_history` | real mainnet funding history for a perpetual (up to 2 000 days) and what it says about the carry: share of profitable windows at taker and maker cost |
| `deltr_wallet_status` | read-only state of the on-chain leg through the Binance Agentic Wallet CLI: installed, signed in, addresses, Binance's remaining daily quota, Deltr's arming state and caps |
| `deltr_onchain_swap` | request a swap through the Binance Agentic Wallet; the wallet holds the key, decides, applies its own limits on top of Deltr's caps, and broadcasts (LIVE + opt-in only) |
| `deltr_x402_pay` | pay an HTTP 402 (x402 / B402) challenge on BNB Smart Chain through the wallet: preview the options, apply Deltr's allow-list and per-payment ceiling, then sign |

Resources: `deltr://status`, `deltr://risk-limits`, `deltr://config`.

A VETO is final for the same inputs. Change capital or leverage, or ask `deltr_explain_edge`; do not retry the same call.

## 3. Deltr → Binance MCP (the upstream)

Binance Agent OS publishes one hosted MCP endpoint:

```
https://agent.binance.com/mcp/agentic
```

It uses **OAuth** through a supported host (Claude Code, Claude Desktop, Codex, ChatGPT, VS Code), trades inside a dedicated Agentic sub-account, and has no withdrawal scope. Connect it next to Deltr in the same host:

```bash
claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
```

Then `/mcp` → `binance-mcp-server` → authenticate on the "Binance Agentic Account Access" consent screen. Binance does not publish the tool names in its docs; discover them with `tools/list` after authorising. A third-party inventory of that surface is transcribed in `tests/fixtures/binance_mcp_tools.json` (see **Tool-name compatibility** below).

Deltr's TypeScript bridge (`agents/agent_os_bridge.ts`) reports which upstream it could reach, in this order, and never claims more than it exercised:

```bash
npx tsx agents/agent_os_bridge.ts upstream-status   # official → shim → none   (works standalone)
npx tsx agents/agent_os_bridge.ts list              # Deltr's 22 tools (needs `python main.py` running; DELTR_MCP_URL overrides :8000)
npx tsx agents/agent_os_bridge.ts route '{"symbol":"BNBUSDT","capital_usd":2000}'
npx tsx agents/agent_os_bridge.ts serve             # JSON-RPC 2.0 on :8788
```

* **official** — `BINANCE_MCP_URL` answered `initialize` (with `BINANCE_MCP_TOKEN` if you have a bearer). Unauthenticated calls get `401` + `www-authenticate`, which the bridge reports as `authorized: false`.
* **shim** — the local `deltr/mcp/binance_shim_server.py` (stdio) mirroring the market / account / trade scopes against the Binance Futures **testnet** and the public spot data mirror.
* **none** — with the reason.

### Tool-name compatibility

The shim serves its 9 tools under the **official Binance Agent OS names** wherever the published
inventory has an equivalent, so a client written against Agent OS can call the shim without
renaming anything. `deltr/mcp/binance_shim_server.py` reads the mapping from
`tests/fixtures/binance_mcp_tools.json` at startup, and falls back to its own names when that file
is missing or unreadable.

| Shim tool | Served as | Source category |
|---|---|---|
| `get_ticker` | `spot.ticker24hr` | MARKET_DATA |
| `get_order_book` | `spot.depth` | MARKET_DATA |
| `get_mark_price` | `futures_usds.premiumIndexKlineData` | MARKET_DATA |
| `get_exchange_filters` | `futures_usds.exchangeInformation` | MARKET_DATA |
| `get_account` | `futures_usds.futuresAccountBalanceV3` | ACCOUNT_READ |
| `get_positions` | `futures_usds.positionInformationV2` | ACCOUNT_READ |
| `get_funding_rate` | `get_funding_rate` (local name) | no equivalent published |
| `set_leverage` | `set_leverage` (local name) | no equivalent published |
| `place_futures_order` | `place_futures_order` (local name) | no equivalent published |

The bridge attempts the official name first and prints every attempt on stderr, so what was tried
is visible rather than asserted:

```bash
npx tsx agents/agent_os_bridge.ts upstream-list
# [bridge] attempting official Binance tool names: spot.ticker24hr, spot.depth, futures_usds.premiumIndexKlineData, ...
# [bridge] official names served by this upstream: 6/9 (...)  [names transcribed from a published inventory, not captured from our own session]
# JSON on stdout carries status + tools + officialNames { attempted, matched, missing }
```

**What is claimed:** the shim's names are compatible with a published inventory of the official
surface, and the bridge asks for those names first.

**What is not claimed:** the names were **transcribed on 2026-09-03 from a third-party published
inventory dated 2026-09-02** (`likeMdl/binance-ai-risk-trader`, `docs/binance-agent-os-tools.md`,
which states it captured them from a live OAuth session). They were **not captured from our own
session**, and they are not verified with Binance: `agent.binance.com` has never been reached from
this build machine. The fixture's `provenance` block says exactly this in machine-readable form,
`tests/test_shim_tool_names.py` fails if that statement is removed or contradicted, and
`scripts/probe_binance_mcp.py --out tests/fixtures/binance_mcp_tools.json`, run inside a completed
OAuth session, replaces the file with a first-party capture and rewrites the provenance block.

Three further honesty notes:

* The published inventory the fixture transcribes contains **0 write-capable trade tools and 0
  transfer tools** (24 MARKET_DATA + 23 ACCOUNT_READ + an AI report tool + 2 generic gateways).
  That is why the shim's two write tools and its funding read keep Deltr-local names: there is
  nothing published to be compatible with, and aliasing a write tool onto a read-only official
  name such as `futures_usds.queryOrder` would be a false claim.
* A name says what to call, not where the data comes from. Each tool's own description states its
  source, and they do not always agree with the official name's product: `spot.depth` on the shim
  returns the **futures testnet** book, and `get_ticker` combines the public spot mirror with the
  testnet perp book.
* The bridge forwards only read-only tools: a `get_*` name or one of the official read-only names
  above. Trade-shaped names are refused and routed through Deltr's gate; any other name is refused
  as off the allowlist.

Deltr's own executor never routes orders through the bridge or the shim; it signs testnet REST orders directly, so the deterministic gate always sits in front of the exchange.

## 4. Sanity check before recording

```bash
npx @modelcontextprotocol/inspector --transport http --server-url http://127.0.0.1:8000/mcp
# tools/list → 22 tools; call deltr_status → mode "paper", venues ok
```
