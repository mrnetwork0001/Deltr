// Landing-page copy and constants. Every number here is quoted from docs/
// (ARCHITECTURE, SAFETY_MODEL, MCP_SETUP, DEMO_SCRIPT, SUBMISSION_CHECKLIST)
// or from the 2026-09-02 probe recorded in the design plan. Nothing is invented.

export const REPO_URL = "https://github.com/mrnetwork0001/Deltr";
export const MCP_URL = "http://127.0.0.1:8000/mcp";
export const DASHBOARD_PATH = "/app/";
export const BINANCE_MCP_URL = "https://agent.binance.com/mcp/agentic";

export const NAV_LINKS: { id: string; label: string }[] = [
  { id: "how", label: "How it works" },
  { id: "architecture", label: "Architecture" },
  { id: "edge", label: "The edge" },
  { id: "safety", label: "Safety" },
  { id: "connect", label: "Connect" },
  { id: "agent-os", label: "Agent OS" },
  { id: "demo", label: "Demo" },
  { id: "hackathon", label: "Hackathon" },
  { id: "faq", label: "FAQ" },
];

export const HERO_CLAIMS: { title: string; body: string }[] = [
  {
    title: "One trade, two real venues",
    body: "Long BNB on PancakeSwap V3, short the same quantity of the Binance USDⓈ-M perp. The pairing is re-derived from the legs, never trusted from a flag, so the book carries no directional view.",
  },
  {
    title: "The whole round trip, priced",
    body: "net(H) = entry basis + funding over the horizon − round trip − assumed exit basis. Pool fee, price impact, BSC gas, perp slippage and taker fee, both legs, entry and exit.",
  },
  {
    title: "Real data, zero secrets",
    body: "PancakeSwap V3 quotes from BNB Chain mainnet (slot0 + QuoterV2 via eth_call) and Binance USDⓈ-M Futures testnet mark, book and funding. PAPER mode needs no API key.",
  },
  {
    title: "MCP-native both ways",
    body: "Deltr is an MCP server with 18 tools for Claude, Codex, Cursor and VS Code, and its TypeScript bridge discovers the hosted Binance MCP upstream and reports which one it reached.",
  },
];

export interface Subsystem {
  name: string;
  path: string;
  summary: string;
  bullets: string[];
}

export const SUBSYSTEMS: Subsystem[] = [
  {
    name: "Arbitrage Scout",
    path: "agents/arbitrage_scout.py",
    summary: "Polls the three venues and prices the full round trip, not just the spread.",
    bullets: [
      "MarketDataHub polls CEX every 1 s and DEX every 3 s with per-feed ages and a Freshness verdict",
      "Horizon-based edge: entry basis + funding over the horizon minus round-trip cost minus assumed exit basis",
      "Marks each opportunity actionable or not, with the reason",
    ],
  },
  {
    name: "Delta-Neutral Hedger",
    path: "agents/hedger.py",
    summary: "Sizes a paired Long-DEX / Short-Perp plan and hands it to the gate as a pre-check.",
    bullets: [
      "N = capital / (1 + 1/L), floored to the 0.01 lot step",
      "Returns a single-use plan_id that expires in 60 s",
      "Execution re-quotes the DEX leg and re-runs the gate before any leg is placed",
    ],
  },
  {
    name: "Agent OS ↔ MCP Bridge",
    path: "agents/agent_os_bridge.ts",
    summary: "MCP client, JSON-RPC 2.0 router and upstream discovery for the Binance MCP server.",
    bullets: [
      "Reports the upstream it could reach, in order: official, shim, none",
      "Lists Deltr's 18 tools and routes a JSON-RPC request into propose + precheck",
      "Never claims more than it exercised; unauthenticated 401s are reported as authorized: false",
    ],
  },
  {
    name: "Deterministic Risk Gate",
    path: "risk_gate.py",
    summary: "19 ordered checks, stdlib only, owns equity, drawdown, kill switch and the position registry.",
    bullets: [
      "Cheapest and most decisive checks run first and short-circuit on the first failure",
      "Every veto carries the observed value, the limit and the unit",
      "Halt and kill state persist per mode under state/<mode>/ so a restart cannot evade them",
    ],
  },
];

export interface EdgeRow {
  label: string;
  perLeg: number | null;
  roundTrip: number;
  note: string;
}

// Worked example from the 2026-09-02 probe: pool fee100, mark 686.34 vs DEX exec 686.19.
export const EDGE_EXAMPLE = {
  basisBps: 2.2,
  fundingBps: 0.0,
  roundTripBps: 16.6,
  netBps: -14.4,
  netLabel: "≈ −14 bps",
  horizonH: 72,
  settlements: 9,
  dexExec: 686.19,
  perpMark: 686.34,
  rows: [
    { label: "PancakeSwap pool fee (fee100)", perLeg: 1.0, roundTrip: 2.0, note: "0.01 % tier" },
    { label: "DEX price impact", perLeg: 0.3, roundTrip: 0.6, note: "QuoterV2 exec vs slot0 mid, net of fee" },
    { label: "Perp slippage vs mark", perLeg: 2.0, roundTrip: 4.0, note: "testnet book is thin" },
    { label: "Binance taker fee", perLeg: 5.0, roundTrip: 10.0, note: "0.05 %" },
    { label: "Gas per swap", perLeg: 0.02, roundTrip: 0.03, note: "≈ $0.0056 at 0.05 gwei on $3.3k" },
  ] as EdgeRow[],
};

export const SIZING_EXAMPLE = {
  capitalUsd: 5000,
  leverage: 2,
  qtyBnb: 4.85,
  notionalUsd: 3328,
  marginUsd: 1664,
  cashUsd: 4992,
  price: 686.19,
};

export interface GateCheck {
  n: number;
  code: string;
  threshold: string;
  owner: string;
}

export const GATE_CHECKS: GateCheck[] = [
  { n: 1, code: "KILL_SWITCH", threshold: "operator flag (verified unwinds pass)", owner: "gate" },
  { n: 2, code: "HALTED_DRAWDOWN", threshold: "dd ≥ 3 % (sticky)", owner: "gate (update_equity)" },
  { n: 3, code: "MALFORMED", threshold: "fail-closed; a missing leverage is a veto", owner: "executor" },
  { n: 4, code: "REDUCE_ONLY_UNVERIFIED", threshold: "registered position_id and qty ≤ open qty", owner: "gate + executor" },
  { n: 5, code: "NOT_DELTA_NEUTRAL", threshold: "legs must be DEX BUY / perp SELL", owner: "executor (from plan legs)" },
  { n: 6, code: "HEDGE_MISMATCH", threshold: "|dex_qty − perp_qty| ≤ max(1 lot step, 0.5 %)", owner: "executor" },
  { n: 7, code: "LEVERAGE", threshold: "≤ 3.0x", owner: "caller (validated)" },
  { n: 8, code: "MIN_NOTIONAL", threshold: "≥ 5 USDT", owner: "executor" },
  { n: 9, code: "MAX_NOTIONAL", threshold: "PAPER 50 000 / TESTNET 5 000", owner: "config" },
  { n: 10, code: "CAPITAL_RISK", threshold: "≤ 2 % of equity ($200 on $10 000)", owner: "executor + gate floor" },
  { n: 11, code: "AGGREGATE_RISK", threshold: "Σ open risk + new ≤ (3 % − dd) · equity", owner: "gate registry" },
  { n: 12, code: "CAPITAL_CAPACITY", threshold: "Σ notional · (1 + 1/L) ≤ 90 % · equity", owner: "gate registry" },
  { n: 13, code: "AGGREGATE_NOTIONAL", threshold: "Σ notional ≤ 3 · equity", owner: "gate registry" },
  { n: 14, code: "SYMBOL_NOT_ALLOWED", threshold: "DELTR_SYMBOLS whitelist", owner: "config" },
  { n: 15, code: "MAX_POSITIONS", threshold: "registered positions < 3", owner: "gate registry" },
  { n: 16, code: "STALE_QUOTE", threshold: "oldest quote age ≤ 5 000 ms", owner: "executor (freshness)" },
  { n: 17, code: "PRICE_SANITY", threshold: "|perp ref − DEX ref| ≤ 100 bps", owner: "executor (market state)" },
  { n: 18, code: "PRICE_DRIFT", threshold: "|re-quote − plan price| ≤ 20 bps", owner: "executor (re-quote at execute)" },
  { n: 19, code: "NEGATIVE_EDGE", threshold: "expected edge ≥ 0 bps (PAPER may set a labelled override)", owner: "scout via executor" },
];

export const NEVER_DOES: { never: string; how: string }[] = [
  { never: "Place a production order", how: "Only paper and testnet modes exist; the frozen HOSTS table has no production trading host and BINANCE_API_ENV=prod is refused at startup." },
  { never: "Take a directional bet", how: "Only a paired Long-DEX / Short-Perp is accepted, re-derived from the legs rather than trusted from a flag." },
  { never: "Let a caller loosen a limit", how: "RiskLimits clamps 3x, 2 % and 3 %; equity, peak, drawdown and the open-position registry live inside the gate." },
  { never: "Trust \"reduce only\"", how: "An unwind earns the kill-switch / halt bypass only for a registered position with qty at most the open quantity." },
  { never: "Let an LLM execute from free text", how: "deltr_prompt is propose-only. Execution needs a plan_id; plans are single-use, expire after 60 s, and are re-priced and re-gated." },
  { never: "Withdraw or move funds", how: "Deltr has no withdrawal or transfer code; the Binance MCP server itself has no withdrawal scope." },
  { never: "Hide a loss", how: "Positions are marked to close, net of the estimated exit round trip." },
  { never: "Evade a halt by restarting", how: "Gate and portfolio state persist per mode; a halt survives a restart and the peak is never silently re-based." },
];

export const HARD_INVARIANTS: { name: string; value: string; constant: string }[] = [
  { name: "Max futures leverage", value: "3.0x", constant: "MAX_LEVERAGE" },
  { name: "Max capital at risk per trade", value: "2 % of equity", constant: "MAX_CAPITAL_RISK_PCT" },
  { name: "Drawdown stop-loss (halt)", value: "3 % from peak", constant: "MAX_DRAWDOWN_PCT" },
  { name: "Drawdown warning", value: "2 %", constant: "WARN_DRAWDOWN_PCT" },
];

export interface ClientSnippet {
  id: string;
  label: string;
  lang: "bash" | "json";
  code: string;
  hint: string;
}

export const CLIENT_SNIPPETS: ClientSnippet[] = [
  {
    id: "claude-code",
    label: "Claude Code",
    lang: "bash",
    code: `claude mcp add deltr --transport http ${MCP_URL}`,
    hint: "Then /mcp and pick deltr. The repo also ships .mcp.json at project scope.",
  },
  {
    id: "claude-desktop",
    label: "Claude Desktop",
    lang: "json",
    code: `{
  "mcpServers": {
    "deltr": { "command": "npx", "args": ["-y", "mcp-remote", "${MCP_URL}"] }
  }
}`,
    hint: "Settings, Developer, Edit Config (claude_desktop_config.json).",
  },
  {
    id: "cursor",
    label: "Cursor",
    lang: "json",
    code: `{ "mcpServers": { "deltr": { "url": "${MCP_URL}" } } }`,
    hint: ".cursor/mcp.json in the project.",
  },
  {
    id: "codex",
    label: "Codex CLI",
    lang: "bash",
    code: `codex mcp add deltr --url ${MCP_URL}`,
    hint: "VS Code: Chat, MCP Servers, +, HTTP, same URL, name deltr.",
  },
  {
    id: "inspector",
    label: "Inspector",
    lang: "bash",
    code: `npx @modelcontextprotocol/inspector --transport http --server-url ${MCP_URL}
# tools/list -> 18 tools; call deltr_status -> mode "paper", venues ok`,
    hint: "Any client. The sanity check before recording.",
  },
];

export const STDIO_SNIPPET = `{
  "mcpServers": {
    "deltr": { "command": "/ABSOLUTE/PATH/Deltr/.venv/bin/python", "args": ["/ABSOLUTE/PATH/Deltr/main.py", "--mcp"] }
  }
}`;

export interface ToolInfo {
  name: string;
  use: string;
  phase?: "read" | "propose" | "execute" | "operator";
}

export const TOOLS: ToolInfo[] = [
  { name: "deltr_status", use: "mode, venues, equity, drawdown state, kill/halt, measured gate latency", phase: "read" },
  { name: "deltr_market", use: "current DEX / perp / funding state and the edge breakdown", phase: "read" },
  { name: "deltr_scan", use: "the latest opportunity + spread history", phase: "read" },
  { name: "deltr_explain_edge", use: "dry sizing and the full cost/carry math for a capital + leverage", phase: "read" },
  { name: "deltr_edge_report", use: "the same decomposition rendered as a readable markdown report: waterfall, funding, breakeven verdict, source tags and feed ages", phase: "read" },
  { name: "deltr_propose_hedge", use: "phase 1: size a plan, run the gate pre-check, get a plan_id (nothing executes)", phase: "propose" },
  { name: "deltr_evaluate_risk", use: "dry-run the gate (leverage 10 gives a LEVERAGE veto with observed/limit)", phase: "read" },
  { name: "deltr_execute_hedge", use: "phase 2: execute a plan_id (re-priced, re-gated, single-use; TESTNET needs confirm: true)", phase: "execute" },
  { name: "deltr_unwind", use: "reduce-only unwind of a position or \"all\"", phase: "execute" },
  { name: "deltr_positions", use: "open positions + portfolio (mark-to-close)", phase: "read" },
  { name: "deltr_risk_log", use: "recent gate decisions with every check's observed/limit", phase: "read" },
  { name: "deltr_receipt", use: "a receipt by id (trace steps + SHA-256)", phase: "read" },
  { name: "deltr_activity", use: "the inbound MCP call log (which client called what)", phase: "read" },
  { name: "deltr_prompt", use: "natural language to intent to plan to pre-check (propose-only)", phase: "propose" },
  { name: "deltr_kill_switch", use: "operator kill switch on/off", phase: "operator" },
  { name: "deltr_reset_halt", use: "clear a drawdown halt once the book has recovered", phase: "operator" },
  { name: "deltr_set_min_edge", use: "runtime min-edge (PAPER may go negative, labelled on screen)", phase: "operator" },
  { name: "deltr_stress", use: "labelled SIMULATED scenarios on the paper book; reset clears", phase: "operator" },
];

export const MCP_RESOURCES = ["deltr://status", "deltr://risk-limits", "deltr://config"];

// Skills Hub packaging status, stated as it is in the checkout: SKILL.md with
// the Skills Hub frontmatter, the scripts/deltr.sh wrapper, references/ and an
// MIT LICENSE for the skill folder are committed.
export const SKILL_INSTALL = `npx skills add ${REPO_URL}`;
export const SKILL_RUN = "bash scripts/deltr.sh once --json";
export const SKILL_STATUS = {
  folder: "skills/deltr-binance/",
  state: "packaged",
  note: "SKILL.md carries the Skills Hub frontmatter (name, description, version, license: MIT, metadata.version/author/openclaw), the command routing table and references/{tools,cli,risk-model}.md; scripts/deltr.sh wraps the REST API and the one-shot engine. The skill folder is MIT, the code Apache-2.0.",
};

export const BRIDGE_COMMANDS: { cmd: string; what: string }[] = [
  { cmd: "npx tsx agents/agent_os_bridge.ts upstream-status", what: "official, then shim, then none" },
  { cmd: "npx tsx agents/agent_os_bridge.ts list", what: "Deltr's 18 tools" },
  { cmd: `npx tsx agents/agent_os_bridge.ts route '{"symbol":"BNBUSDT","capital_usd":2000}'`, what: "JSON-RPC receipt with plan_id + precheck" },
  { cmd: "npx tsx agents/agent_os_bridge.ts serve", what: "JSON-RPC 2.0 on :8788" },
];

export const BINANCE_MCP_ADD = `claude mcp add binance-mcp-server --transport http ${BINANCE_MCP_URL}`;

export const UPSTREAM_KINDS: { kind: string; meaning: string }[] = [
  { kind: "official", meaning: "BINANCE_MCP_URL answered initialize (with BINANCE_MCP_TOKEN if you have a bearer). Unauthenticated calls get 401 + www-authenticate, reported as authorized: false." },
  { kind: "shim", meaning: "The local deltr/mcp/binance_shim_server.py (stdio) mirroring the market / account / trade scopes against the Binance Futures testnet and the public spot data mirror." },
  { kind: "none", meaning: "Neither upstream could be reached; the reason is reported." },
];

export interface DemoBeat {
  time: string;
  title: string;
  onScreen: string;
  voice: string;
}

export const DEMO_BEATS: DemoBeat[] = [
  {
    time: "0:00",
    title: "Hook",
    onScreen: "Dashboard live: mode PAPER, three venue dots with source labels, equity $10 000, drawdown 0 %, gate median (measured).",
    voice: "Deltr never takes a directional bet, never talks to production, and no order reaches Binance without a deterministic, zero-LLM risk gate.",
  },
  {
    time: "0:15",
    title: "One command",
    onScreen: "python main.py. Banner with mode, hosts, secrets: absent, dashboard + MCP URLs. EdgeWaterfall shows the honest numbers: basis, round trip, funding, net, not actionable.",
    voice: "Real PancakeSwap and Binance data, zero secrets. A naive bot sees a spread; Deltr prices the full round trip.",
  },
  {
    time: "0:35",
    title: "Prompt to hedge",
    onScreen: "deltr_prompt sizes 4.85 BNB at 2x, precheck APPROVED with 19 green rows. deltr_execute_hedge(plan_id). TradeTrace: intent, scan, plan, gate, dex_fill, cex_fill, position, receipt sha256. Delta 0.00 BNB.",
    voice: "If the min-edge override chip is on, say so: otherwise the gate would have vetoed on NEGATIVE_EDGE.",
  },
  {
    time: "1:15",
    title: "The gate says no",
    onScreen: "$50,000 at 10x: VETO LEVERAGE 10.0 > 3.0 x. $50,000 at 3x: VETO CAPITAL_RISK $437 > $200. Kill switch on: VETO KILL_SWITCH.",
    voice: "Pure Python, no model in the loop, every veto has a number.",
  },
  {
    time: "1:45",
    title: "Stress, halt, unwind",
    onScreen: "Basis shock 130 bps (SIMULATED chip): stop fires, auto-unwind receipt. Equity shock 3.5 %: HALTED. Hedge $2,000: VETO HALTED_DRAWDOWN. Reset refused until recovered.",
    voice: "The badge says simulated because the market feed was never touched.",
  },
  {
    time: "2:20",
    title: "Agent OS, two MCPs",
    onScreen: "npx skills add, then bash scripts/deltr.sh once --json (edge, plan, precheck JSON). bridge upstream-status (official / shim / none), list (18 tools), route (JSON-RPC receipt). SKILL.md frontmatter.",
    voice: "Deltr is itself an MCP server, discovers the Binance MCP upstream, and ships as a Skills Hub skill.",
  },
  {
    time: "2:45",
    title: "Close",
    onScreen: "pytest -q -s green with the printed gate benchmark; README; repo URL.",
    voice: "Delta-neutral by construction, deterministic by design.",
  },
];

export const DEMO_PROMPTS: string[] = [
  "Rebalance $5,000 USDC into delta-neutral BNB arbitrage.",
  "Execute that plan.",
  "Do it again with $50,000 at 10x.",
  "Fine, $50,000 at 3x.",
  "Kill switch on. / Kill switch off.",
  "Hedge $2,000.",
  "Reset the halt.",
];

export const TRACK_A_CATEGORIES: { name: string; how: string }[] = [
  { name: "Trading workflows", how: "Natural-language prompt to a sized, gated, paired hedge with a hashed receipt; two-phase propose / execute through MCP." },
  { name: "Onchain workflows", how: "PancakeSwap V3 on BNB Chain quoted on-chain through slot0 + QuoterV2 eth_call, with gas priced into the round trip." },
  { name: "Data & Analysis", how: "Live DEX vs perp spread, funding countdown, and the horizon-based edge breakdown with provenance and feed ages on every number." },
];

export const TRACK_B = {
  title: "Connect your MCPs and trade (task reward, not a judged track)",
  body: "The second hackathon item is a first-come task reward of 4 USDC to the first 10,000 users who complete a spot, a futures and a margin-or-convert trade through the Binance MCP server; you complete it yourself in a supported host. Deltr's bridge attempts that upstream first and reports what it actually reached (official, shim or none); the official endpoint has never been exercised from our build machine, so the bridge reports the testnet-backed shim here. Deltr is itself an MCP server any host can route through, with the deterministic gate in front of every order.",
};

export const HACKATHON_META = {
  name: "Binance Agent OS Mini Hackathon",
  trackA: "Track A: Build an AI agent with Agent OS",
  trackB: "Connect your MCPs and trade (4 USDC task reward, first 10,000 users)",
  deadline: "September 8, 2026, 23:59 UTC",
};

export const FAQ: { q: string; a: string }[] = [
  {
    q: "Is this live trading?",
    a: "No. There are exactly two modes, paper (default, no secrets, simulated fills at live quoted prices) and testnet (real Binance USDⓈ-M Futures testnet LIMIT IOC orders, DEX leg simulated). There is no production mode by design, and BINANCE_API_ENV=prod is refused at startup.",
  },
  {
    q: "Why does the gate say no so often?",
    a: "Because the edge is priced honestly. At the probe the basis was about +2 bps while the full round trip cost about 16.6 bps, so the net was about −14 bps and NEGATIVE_EDGE vetoes. In PAPER mode a labelled min-edge override lets you watch the mechanics anyway.",
  },
  {
    q: "What does the SIMULATED badge mean?",
    a: "A stress scenario (basis_shock, equity_shock, dex_leg_fail, funding_flip, feed_stale) mutated the paper portfolio or leg state. The market feed is never touched. The badge stays on the status bar, the affected positions and receipts until reset, and stress is refused in TESTNET while real orders are open.",
  },
  {
    q: "The prompt says USDC but the perp is USDT-margined. What happens?",
    a: "USDC is accepted and treated as USDT-equivalent for sizing, with no conversion leg in this version. The intent records a stablecoin note and the prompt result says so.",
  },
  {
    q: "Can Deltr withdraw or move funds?",
    a: "No. Deltr has no withdrawal or transfer code, and the Binance MCP server itself has no withdrawal scope. Testnet mode trades inside the testnet account with LIMIT IOC orders only.",
  },
  {
    q: "How is the gate latency measured?",
    a: "risk_gate.benchmark() runs 10,000 evaluations at startup. The banner, the status bar and the README quote that measured median, never a target: across repeated runs on an Apple M-series laptop it lands between 1.5 and 2.5 µs. The test suite fails above 5 µs so slower CI machines stay green.",
  },
  {
    q: "Can an LLM talk its way past a limit?",
    a: "No. Callers may supply only capital_usd, leverage, symbol, plan_id, confirm, position_id and reason. Only the Executor assembles the gate input, equity and the position registry live inside the gate, and an AST test checks that no other module constructs a proposal.",
  },
];

export const FOOTER_LINKS: { label: string; href: string }[] = [
  { label: "GitHub", href: REPO_URL },
  { label: "Architecture", href: `${REPO_URL}/blob/main/docs/ARCHITECTURE.md` },
  { label: "Safety model", href: `${REPO_URL}/blob/main/docs/SAFETY_MODEL.md` },
  { label: "MCP setup", href: `${REPO_URL}/blob/main/docs/MCP_SETUP.md` },
  { label: "Demo script", href: `${REPO_URL}/blob/main/docs/DEMO_SCRIPT.md` },
  { label: "Dashboard", href: DASHBOARD_PATH },
];
