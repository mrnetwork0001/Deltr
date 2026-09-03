/**
 * Deltr — Subsystem 3: Binance Agent OS ↔ MCP bridge.
 *
 * A thin TypeScript MCP *client* that sits between an agent runtime and two MCP servers:
 *
 *   1. Deltr's own MCP server (streamable HTTP, `DELTR_MCP_URL`, default
 *      `http://127.0.0.1:8000/mcp`) — the ONLY path through which anything trades.
 *   2. An optional upstream Binance MCP server, discovered in this order and reported
 *      honestly, never claimed as exercised:
 *        official  — `BINANCE_MCP_URL` (streamable HTTP, optional `BINANCE_MCP_TOKEN` bearer;
 *                    the hosted endpoint is OAuth-gated, so a bare initialize yields 401 →
 *                    `authorized: false`)
 *        shim      — the local `deltr.mcp.binance_shim_server` spawned over stdio
 *        none      — with the reason string
 *
 * It exposes a tiny JSON-RPC 2.0 façade on `POST /rpc` (`DELTR_BRIDGE_PORT`, default 8788)
 * and a CLI (`npx tsx agents/agent_os_bridge.ts <cmd>`).
 *
 * Safety invariants:
 *   - `upstream/call` forwards ONLY read-only tools: a `get_*` name, or one of the official
 *     read-only Binance names in `OFFICIAL_TOOL_ALIASES`. Anything trade-shaped is refused with
 *     -32601 (trade calls are routed through Deltr's gate, `deltr.route_order`); anything else
 *     off the allowlist is refused too. Read calls attempt the official dotted name first and
 *     log every attempt, so what was tried is visible on stderr.
 *   - `routeOrder` is two-phase: `deltr_propose_hedge` → (approved && confirm) →
 *     `deltr_execute_hedge(plan_id, confirm)`. Nothing executes without both.
 *   - Leverage > 3 is rejected before it ever reaches a server (-32602).
 *   - Tokens are never logged; all human-readable output goes to stderr, JSON to stdout.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// ─────────────────────────────────────────────────────────────────────────────
// Public types (section 5.4 of DESIGN_FINAL.md)
// ─────────────────────────────────────────────────────────────────────────────

export type BridgeConfig = {
  /** Deltr MCP endpoint (`DELTR_MCP_URL`, default `http://127.0.0.1:8000/mcp`). */
  deltrUrl: string;
  /** Official Binance MCP endpoint (`BINANCE_MCP_URL`); unset ⇒ official upstream skipped. */
  binanceMcpUrl?: string;
  /** Bearer token for the official endpoint (`BINANCE_MCP_TOKEN`). Never logged. */
  binanceMcpToken?: string;
  /**
   * Command that starts the local shim over stdio. Default:
   * `[<repo>/.venv/bin/python, "-m", "deltr.mcp.binance_shim_server"]`.
   * An EMPTY array disables the shim attempt (useful in tests).
   */
  shimCommand?: string[];
  /** JSON-RPC façade port (`DELTR_BRIDGE_PORT`, default 8788; 0 ⇒ ephemeral). */
  port: number;
  /** Per-request timeout for upstream discovery / calls (`DELTR_BRIDGE_TIMEOUT_MS`, default 8000). */
  timeoutMs?: number;
};

export type UpstreamKind = "official" | "shim" | "none";

export type UpstreamStatus = {
  kind: UpstreamKind;
  /** URL of the official endpoint, or `stdio:<command>` for the shim. */
  url?: string;
  /** True only when the selected upstream completed `initialize` + `tools/list`. */
  authorized: boolean;
  toolsDiscovered: string[];
  /** Why the selected kind is not `official` (or why nothing connected). */
  error?: string;
  /** ISO-8601 timestamp of the probe. */
  ts: string;
};

export type OrderIntent = {
  symbol: string;
  capital_usd: number;
  leverage?: number;
  confirm?: boolean;
  reason?: string;
};

export type RouteReceipt = {
  ok: boolean;
  plan_id?: string;
  precheck?: unknown;
  receipt?: unknown;
  error?: { code: string; message: string };
  /** Human-readable one-liner describing what happened (proposal-only vs executed). */
  message?: string;
};

export type JsonRpcRequest = {
  jsonrpc: "2.0";
  id: string | number | null;
  method: string;
  params?: unknown;
};

export type JsonRpcResponse = {
  jsonrpc: "2.0";
  id: string | number | null;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
};

/** Handle returned to `ServeOptions.onListening` so embedders/tests can stop the server. */
export type BridgeServer = {
  port: number;
  close: () => Promise<void>;
};

/** Optional dependency injection for `serve` (tests pass an in-memory Deltr client). */
export type ServeOptions = {
  deltr?: Client;
  upstream?: { client: Client | null; status: UpstreamStatus };
  onListening?: (server: BridgeServer) => void;
  /** Install SIGINT/SIGTERM handlers that close the server (CLI only). */
  installSignalHandlers?: boolean;
};

// ─────────────────────────────────────────────────────────────────────────────
// Constants, logging, helpers
// ─────────────────────────────────────────────────────────────────────────────

export const BRIDGE_VERSION = "1.0.0";
export const DEFAULT_DELTR_MCP_URL = "http://127.0.0.1:8000/mcp";
export const DEFAULT_BRIDGE_PORT = 8788;
export const DEFAULT_TIMEOUT_MS = 8000;
export const MAX_LEVERAGE = 3;
export const MIN_CAPITAL_USD = 10;
export const TRADE_CALL_REFUSED_MESSAGE = "trade calls are routed through Deltr's gate (deltr.route_order)";
export const READ_ONLY_ALLOWLIST_REFUSED_MESSAGE = "the bridge forwards only read-only tools and this name is not on its read-only allowlist";

/**
 * Official Binance Agent OS tool names for Deltr's read-only inputs, most-preferred first.
 * The bridge attempts these BEFORE the shim's own `get_*` names, and logs every attempt.
 *
 * PROVENANCE: transcribed on 2026-09-03 from a third-party published inventory dated
 * 2026-09-02 (`likeMdl/binance-ai-risk-trader`, `docs/binance-agent-os-tools.md`) — the same
 * source as `tests/fixtures/binance_mcp_tools.json`, which carries the full provenance block.
 * They were NOT captured from our own OAuth session and are not verified against Binance;
 * `agent.binance.com` has never been reached from this machine. The claim is name
 * compatibility with a published inventory, nothing more.
 *
 * That inventory contains no write-capable trade tool and no transfer tool, so no trade name
 * appears here: `get_funding_rate` has no published counterpart either and keeps its local name.
 */
export const OFFICIAL_TOOL_ALIASES: Readonly<Record<string, readonly string[]>> = {
  get_ticker: ["spot.ticker24hr"],
  get_order_book: ["spot.depth"],
  get_mark_price: ["futures_usds.premiumIndexKlineData", "futures_usds.markPriceKlineCandlestickData"],
  get_exchange_filters: ["futures_usds.exchangeInformation", "spot.exchangeInfo"],
  get_account: ["futures_usds.futuresAccountBalanceV3", "futures_usds.accountInformationV3"],
  get_positions: ["futures_usds.positionInformationV2"],
  get_funding_rate: [], // the published inventory exposes no funding-rate tool
};

/** Official names the bridge will forward (every entry above is read-only by construction). */
export const OFFICIAL_READ_ONLY_TOOLS: ReadonlySet<string> = new Set(Object.values(OFFICIAL_TOOL_ALIASES).flat());

/** Names that look like a write: refused with `TRADE_CALL_REFUSED_MESSAGE`, never forwarded. */
const TRADE_SHAPED = /(^|[._-])(place|new|create|cancel|amend|replace|close|set|transfer|withdraw|deposit|borrow|repay|redeem|execute|submit|send)/i;

/** True when the bridge may forward this tool name upstream (default-deny). */
export function isForwardableReadOnly(name: string): boolean {
  if (TRADE_SHAPED.test(name)) return false;
  return /^get_[a-z0-9_]+$/i.test(name) || OFFICIAL_READ_ONLY_TOOLS.has(name);
}

/**
 * Names to try for `name`, official first. When the upstream published a tool list, only the
 * candidates it actually advertises are attempted; otherwise every candidate is tried in order.
 */
export function officialCandidates(name: string, discovered: readonly string[] = []): string[] {
  const ordered = [...(OFFICIAL_TOOL_ALIASES[name] ?? []), name].filter((n, i, a) => a.indexOf(n) === i);
  const advertised = discovered.length > 0 ? ordered.filter((n) => discovered.includes(n)) : [];
  return advertised.length > 0 ? advertised : ordered;
}

/** Which official names the bridge attempts, and which of them this upstream actually serves. */
export function officialNameReport(discovered: readonly string[]): { attempted: string[]; matched: string[]; missing: string[] } {
  const attempted = [...OFFICIAL_READ_ONLY_TOOLS];
  const matched = attempted.filter((n) => discovered.includes(n));
  return { attempted, matched, missing: attempted.filter((n) => !matched.includes(n)) };
}

/** Print the official-name attempt on stderr so an operator can see what was tried. */
function logOfficialNameCheck(discovered: readonly string[]): { attempted: string[]; matched: string[]; missing: string[] } {
  const report = officialNameReport(discovered);
  log(`attempting official Binance tool names: ${report.attempted.join(", ")}`);
  log(
    `official names served by this upstream: ${report.matched.length}/${report.attempted.length}` +
      (report.matched.length > 0 ? ` (${report.matched.join(", ")})` : "") +
      "  [names transcribed from a published inventory, not captured from our own session]",
  );
  return report;
}

export const JSONRPC_PARSE_ERROR = -32700;
export const JSONRPC_INVALID_REQUEST = -32600;
export const JSONRPC_METHOD_NOT_FOUND = -32601;
export const JSONRPC_INVALID_PARAMS = -32602;
export const JSONRPC_UPSTREAM_UNAVAILABLE = -32000;

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** Typed JSON-RPC error; thrown by validators and turned into a response by `serve`. */
export class JsonRpcError extends Error {
  readonly code: number;
  readonly data?: unknown;
  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "JsonRpcError";
    this.code = code;
    this.data = data;
  }
}

/** Log a human-readable line to stderr (stdout is reserved for JSON results). */
export function log(message: string): void {
  process.stderr.write(`[bridge ${new Date().toISOString()}] ${message}\n`);
}

/** Scrub anything that looks like a bearer token / key / secret from a string. */
export function redactSecrets(text: string, ...secrets: Array<string | undefined>): string {
  let out = text;
  for (const s of secrets) {
    if (s && s.length > 0) out = out.split(s).join("<redacted>");
  }
  out = out.replace(/Bearer\s+[A-Za-z0-9._~+/=-]{8,}/g, "Bearer <redacted>");
  out = out.replace(/((?:api[_-]?key|secret|token|password)["']?\s*[:=]\s*["']?)[^\s"',}]+/gi, "$1<redacted>");
  return out;
}

function errorMessage(e: unknown): string {
  if (e instanceof JsonRpcError) return e.message;
  if (e instanceof Error) {
    const code = (e as { code?: unknown }).code;
    const cause = (e as { cause?: unknown }).cause;
    const causeMsg = cause instanceof Error ? ` (${cause.message})` : "";
    return `${e.message}${code !== undefined && !String(e.message).includes(String(code)) ? ` [${String(code)}]` : ""}${causeMsg}`;
  }
  return String(e);
}

function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolvePromise, reject) => {
    const t = setTimeout(() => reject(new Error(`${label} timed out after ${ms} ms`)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolvePromise(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

/** Default shim command: repo venv python if present, else `python3` on PATH. */
export function defaultShimCommand(): string[] {
  const venvPython = resolve(REPO_ROOT, ".venv", "bin", "python");
  const python = existsSync(venvPython) ? venvPython : "python3";
  return [python, "-m", "deltr.mcp.binance_shim_server"];
}

/** Build a `BridgeConfig` from environment variables. */
export function configFromEnv(env: NodeJS.ProcessEnv = process.env): BridgeConfig {
  const port = Number.parseInt(env.DELTR_BRIDGE_PORT ?? "", 10);
  const timeout = Number.parseInt(env.DELTR_BRIDGE_TIMEOUT_MS ?? "", 10);
  const cfg: BridgeConfig = {
    deltrUrl: env.DELTR_MCP_URL?.trim() || DEFAULT_DELTR_MCP_URL,
    port: Number.isFinite(port) && port >= 0 ? port : DEFAULT_BRIDGE_PORT,
    timeoutMs: Number.isFinite(timeout) && timeout > 0 ? timeout : DEFAULT_TIMEOUT_MS,
  };
  if (env.BINANCE_MCP_URL?.trim()) cfg.binanceMcpUrl = env.BINANCE_MCP_URL.trim();
  if (env.BINANCE_MCP_TOKEN?.trim()) cfg.binanceMcpToken = env.BINANCE_MCP_TOKEN.trim();
  return cfg;
}

/** Config view that is safe to log (token presence only, never its value). */
export function describeConfig(cfg: BridgeConfig): Record<string, unknown> {
  return {
    deltrUrl: cfg.deltrUrl,
    binanceMcpUrl: cfg.binanceMcpUrl ?? null,
    binanceMcpToken: cfg.binanceMcpToken ? "<set>" : "<unset>",
    shimCommand: (cfg.shimCommand ?? defaultShimCommand()).join(" ") || "<disabled>",
    port: cfg.port,
    timeoutMs: cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// MCP tool-result unwrapping
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Turn an MCP `tools/call` result into plain JSON.
 * Prefers `structuredContent`; otherwise parses the first text content block as JSON;
 * otherwise returns `{ text }`. Tool-level errors (`isError`) become `{ error: {...} }`.
 */
export function unwrapToolResult(res: unknown): Record<string, unknown> {
  if (!isRecord(res)) return { value: res };
  const content = Array.isArray(res.content) ? (res.content as Array<Record<string, unknown>>) : [];
  const firstText = content.find((c) => isRecord(c) && c.type === "text" && typeof c.text === "string");
  const text = firstText ? String(firstText.text) : "";
  if (res.isError === true) {
    return { error: { code: "TOOL_ERROR", message: text || "tool reported an error" } };
  }
  if (isRecord(res.structuredContent)) return res.structuredContent;
  if (text) {
    try {
      const parsed: unknown = JSON.parse(text);
      return isRecord(parsed) ? parsed : { value: parsed };
    } catch {
      return { text };
    }
  }
  return {};
}

async function callToolJson(client: Client, name: string, args: Record<string, unknown>, timeoutMs: number): Promise<Record<string, unknown>> {
  const res = await withTimeout(client.callTool({ name, arguments: args }), timeoutMs, `tools/call ${name}`);
  return unwrapToolResult(res);
}

// ─────────────────────────────────────────────────────────────────────────────
// Connections
// ─────────────────────────────────────────────────────────────────────────────

function newClient(name: string): Client {
  return new Client({ name, version: BRIDGE_VERSION }, { capabilities: {} });
}

/** Connect to Deltr's MCP server over streamable HTTP. Throws if unreachable. */
export async function connectDeltr(cfg: BridgeConfig): Promise<Client> {
  const client = newClient("deltr-agent-os-bridge");
  const transport = new StreamableHTTPClientTransport(new URL(cfg.deltrUrl));
  await withTimeout(client.connect(transport), cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS, `connect ${cfg.deltrUrl}`);
  return client;
}

/** `tools/list` → tool names. */
export async function listTools(client: Client): Promise<string[]> {
  const res = await client.listTools();
  return res.tools.map((t) => t.name);
}

/**
 * Best-effort: tell the running Deltr which upstream the bridge reached (POST /api/upstream), so the
 * dashboard's upstream badge says the same thing as the terminal.  Never throws, never blocks a command.
 */
export async function reportUpstream(cfg: BridgeConfig, status: UpstreamStatus): Promise<boolean> {
  try {
    const api = new URL(cfg.deltrUrl);
    api.pathname = "/api/upstream";
    api.search = "";
    const body = {
      kind: status.kind,
      url: status.url ?? null,
      authorized: status.authorized,
      tools_discovered: status.toolsDiscovered ?? [],
      error: status.error ?? null,
    };
    const res = await fetch(api, {
      method: "POST",
      headers: { "content-type": "application/json", "X-Deltr-Source": "bridge" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(Math.min(cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS, 3000)),
    });
    // 404 just means no Deltr dashboard is listening on that URL, which is the normal case for
    // a standalone `upstream-status` run: do not print a scary line the operator cannot act on.
    if (!res.ok && res.status !== 404) log(`upstream status not reported to Deltr (${res.status})`);
    return res.ok;
  } catch (e) {
    log(`upstream status not reported to Deltr (${errorMessage(e)})`);
    return false;
  }
}

/**
 * Discover the upstream Binance MCP server: official (HTTP, optional bearer) → shim (stdio) → none.
 * Never throws; the returned status explains what happened.
 */
export async function connectUpstream(cfg: BridgeConfig): Promise<{ client: Client | null; status: UpstreamStatus }> {
  const timeoutMs = cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const reasons: string[] = [];
  const ts = () => new Date().toISOString();

  // 1. Official endpoint.
  if (cfg.binanceMcpUrl) {
    const client = newClient("deltr-agent-os-bridge");
    try {
      const headers: Record<string, string> = {};
      if (cfg.binanceMcpToken) headers.Authorization = `Bearer ${cfg.binanceMcpToken}`;
      const transport = new StreamableHTTPClientTransport(new URL(cfg.binanceMcpUrl), { requestInit: { headers } });
      await withTimeout(client.connect(transport), timeoutMs, "official initialize");
      const tools = await withTimeout(listTools(client), timeoutMs, "official tools/list");
      log(`upstream=official url=${cfg.binanceMcpUrl} tools=${tools.length}`);
      logOfficialNameCheck(tools);
      return { client, status: { kind: "official", url: cfg.binanceMcpUrl, authorized: true, toolsDiscovered: tools, ts: ts() } };
    } catch (e) {
      const msg = redactSecrets(errorMessage(e), cfg.binanceMcpToken);
      const gated = /\b401\b|unauthori[sz]ed/i.test(msg);
      reasons.push(`official upstream unavailable: ${gated ? "OAuth/bearer required (401)" : msg}${gated ? ` — ${msg}` : ""}`);
      await client.close().catch(() => undefined);
    }
  } else {
    reasons.push("official upstream not configured (BINANCE_MCP_URL unset)");
  }

  // 2. Local shim over stdio.
  const shimCmd = cfg.shimCommand ?? defaultShimCommand();
  if (shimCmd.length === 0) {
    reasons.push("shim disabled (empty shimCommand)");
  } else {
    const client = newClient("deltr-agent-os-bridge");
    const url = `stdio:${shimCmd.join(" ")}`;
    try {
      const transport = new StdioClientTransport({ command: shimCmd[0], args: shimCmd.slice(1), cwd: REPO_ROOT, stderr: "inherit" });
      await withTimeout(client.connect(transport), timeoutMs, "shim initialize");
      const tools = await withTimeout(listTools(client), timeoutMs, "shim tools/list");
      log(`upstream=shim cmd="${shimCmd.join(" ")}" tools=${tools.length}`);
      logOfficialNameCheck(tools);
      return {
        client,
        status: { kind: "shim", url, authorized: true, toolsDiscovered: tools, error: reasons.join("; ") || undefined, ts: ts() },
      };
    } catch (e) {
      reasons.push(`shim unavailable: ${redactSecrets(errorMessage(e), cfg.binanceMcpToken)}`);
      await client.close().catch(() => undefined);
    }
  }

  const error = reasons.join("; ");
  log(`upstream=none (${error})`);
  return { client: null, status: { kind: "none", authorized: false, toolsDiscovered: [], error, ts: ts() } };
}

// ─────────────────────────────────────────────────────────────────────────────
// Order intent validation and two-phase routing
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Hand-written validation of an order intent. Throws `JsonRpcError(-32602)` on any problem.
 * Leverage is pre-checked here (≤ 3) so an over-levered request never reaches a server;
 * Deltr's gate re-checks everything anyway.
 */
export function validateOrderIntent(x: unknown): OrderIntent {
  const bad = (msg: string): never => {
    throw new JsonRpcError(JSONRPC_INVALID_PARAMS, `invalid order intent: ${msg}`);
  };
  if (!isRecord(x)) return bad("expected an object");

  const symbolRaw = x.symbol ?? "BNBUSDT";
  if (typeof symbolRaw !== "string" || !/^[A-Z0-9]{5,20}$/.test(symbolRaw.toUpperCase())) return bad("symbol must be a string like BNBUSDT");
  const symbol = symbolRaw.toUpperCase();

  const capital = x.capital_usd;
  if (capital === undefined || capital === null) return bad("capital_usd is required");
  if (typeof capital !== "number" || !Number.isFinite(capital)) return bad("capital_usd must be a finite number");
  if (capital < MIN_CAPITAL_USD) return bad(`capital_usd must be >= ${MIN_CAPITAL_USD}`);

  let leverage: number | undefined;
  if (x.leverage !== undefined && x.leverage !== null) {
    if (typeof x.leverage !== "number" || !Number.isFinite(x.leverage)) return bad("leverage must be a finite number");
    if (x.leverage <= 0) return bad("leverage must be > 0");
    if (x.leverage > MAX_LEVERAGE) return bad(`leverage ${x.leverage} exceeds the hard cap of ${MAX_LEVERAGE}x`);
    leverage = x.leverage;
  }

  let confirm: boolean | undefined;
  if (x.confirm !== undefined && x.confirm !== null) {
    if (typeof x.confirm !== "boolean") return bad("confirm must be a boolean");
    confirm = x.confirm;
  }

  let reason: string | undefined;
  if (x.reason !== undefined && x.reason !== null) {
    if (typeof x.reason !== "string") return bad("reason must be a string");
    reason = x.reason.slice(0, 200);
  }

  const out: OrderIntent = { symbol, capital_usd: capital };
  if (leverage !== undefined) out.leverage = leverage;
  if (confirm !== undefined) out.confirm = confirm;
  if (reason !== undefined) out.reason = reason;
  return out;
}

function pickError(obj: Record<string, unknown>): { code: string; message: string } | undefined {
  if (!isRecord(obj.error)) return undefined;
  const e = obj.error;
  return { code: String(e.code ?? "ERROR"), message: String(e.message ?? "unknown error") };
}

/**
 * Two-phase routing through Deltr's gate:
 *   deltr_propose_hedge(capital_usd, leverage, symbol) → plan_id + precheck
 *   if precheck.approved && intent.confirm → deltr_execute_hedge(plan_id, confirm: true)
 * Never calls any upstream Binance tool.
 */
export async function routeOrder(deltr: Client, intent: OrderIntent, timeoutMs: number = DEFAULT_TIMEOUT_MS): Promise<RouteReceipt> {
  const proposeArgs: Record<string, unknown> = { capital_usd: intent.capital_usd, symbol: intent.symbol };
  if (intent.leverage !== undefined) proposeArgs.leverage = intent.leverage;

  const proposal = await callToolJson(deltr, "deltr_propose_hedge", proposeArgs, timeoutMs);
  const proposeError = pickError(proposal);
  if (proposeError) return { ok: false, error: proposeError, message: `propose failed: ${proposeError.message}` };

  const plan_id = typeof proposal.plan_id === "string" ? proposal.plan_id : undefined;
  const precheck = proposal.precheck;
  const pre = isRecord(precheck) ? precheck : {};
  const approved = pre.approved === true;

  if (!approved) {
    const code = typeof pre.code === "string" && pre.code ? pre.code : "VETO";
    const reason = typeof pre.reason === "string" && pre.reason ? pre.reason : "risk gate vetoed the proposal";
    return { ok: false, plan_id, precheck, error: { code, message: reason }, message: `vetoed by risk gate: ${code}` };
  }
  if (!plan_id) {
    return { ok: false, precheck, error: { code: "NO_PLAN_ID", message: "propose returned no plan_id" } };
  }
  if (!intent.confirm) {
    return { ok: true, plan_id, precheck, message: "proposal approved by precheck; not executed (confirm=false)" };
  }

  const receipt = await callToolJson(deltr, "deltr_execute_hedge", { plan_id, confirm: true }, timeoutMs);
  const execError = pickError(receipt);
  if (execError) return { ok: false, plan_id, precheck, error: execError, message: `execute failed: ${execError.message}` };
  return { ok: true, plan_id, precheck, receipt, message: "executed through Deltr's gate" };
}

// ─────────────────────────────────────────────────────────────────────────────
// JSON-RPC 2.0 façade
// ─────────────────────────────────────────────────────────────────────────────

export type BridgeState = {
  cfg: BridgeConfig;
  deltr: Client | null;
  deltrOwned: boolean;
  upstream: { client: Client | null; status: UpstreamStatus };
  upstreamOwned: boolean;
};

async function getDeltr(state: BridgeState): Promise<Client> {
  if (state.deltr) return state.deltr;
  try {
    state.deltr = await connectDeltr(state.cfg);
    state.deltrOwned = true;
    log(`connected to Deltr at ${state.cfg.deltrUrl}`);
    return state.deltr;
  } catch (e) {
    throw new JsonRpcError(JSONRPC_UPSTREAM_UNAVAILABLE, "deltr unavailable", { reason: redactSecrets(errorMessage(e), state.cfg.binanceMcpToken) });
  }
}

function paramsRecord(params: unknown): Record<string, unknown> {
  if (params === undefined || params === null) return {};
  if (!isRecord(params)) throw new JsonRpcError(JSONRPC_INVALID_PARAMS, "params must be an object");
  return params;
}

function toolCallParams(params: unknown): { name: string; args: Record<string, unknown> } {
  const p = paramsRecord(params);
  if (typeof p.name !== "string" || !p.name) throw new JsonRpcError(JSONRPC_INVALID_PARAMS, "params.name (tool name) is required");
  const args = p.arguments ?? {};
  if (!isRecord(args)) throw new JsonRpcError(JSONRPC_INVALID_PARAMS, "params.arguments must be an object");
  return { name: p.name, args };
}

/** Dispatch one JSON-RPC method. Throws `JsonRpcError`; other errors are wrapped by the caller. */
export async function dispatchRpc(state: BridgeState, method: string, params: unknown): Promise<unknown> {
  const timeoutMs = state.cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  switch (method) {
    case "tools/list": {
      const deltr = await getDeltr(state);
      const res = await withTimeout(deltr.listTools(), timeoutMs, "tools/list");
      return { tools: res.tools };
    }
    case "tools/call": {
      const { name, args } = toolCallParams(params);
      const deltr = await getDeltr(state);
      return callToolJson(deltr, name, args, timeoutMs);
    }
    case "deltr.route_order": {
      const p = paramsRecord(params);
      const intent = validateOrderIntent(p.intent !== undefined ? p.intent : p);
      const deltr = await getDeltr(state);
      const receipt = await routeOrder(deltr, intent, timeoutMs);
      log(`route_order symbol=${intent.symbol} capital=${intent.capital_usd} confirm=${intent.confirm === true} → ok=${receipt.ok} plan=${receipt.plan_id ?? "-"}`);
      return receipt;
    }
    case "upstream/status":
      return state.upstream.status;
    case "upstream/list":
      return {
        status: state.upstream.status,
        tools: state.upstream.status.toolsDiscovered,
        officialNames: officialNameReport(state.upstream.status.toolsDiscovered),
      };
    case "upstream/call": {
      const { name, args } = toolCallParams(params);
      if (TRADE_SHAPED.test(name)) throw new JsonRpcError(JSONRPC_METHOD_NOT_FOUND, TRADE_CALL_REFUSED_MESSAGE, { name });
      if (!isForwardableReadOnly(name)) throw new JsonRpcError(JSONRPC_METHOD_NOT_FOUND, READ_ONLY_ALLOWLIST_REFUSED_MESSAGE, { name });
      if (!state.upstream.client) {
        throw new JsonRpcError(JSONRPC_UPSTREAM_UNAVAILABLE, "upstream unavailable", { reason: state.upstream.status.error ?? "no upstream connected" });
      }
      // Official dotted names first, then the local `get_*` name; every attempt is logged.
      const candidates = officialCandidates(name, state.upstream.status.toolsDiscovered);
      let lastError: unknown;
      for (const candidate of candidates) {
        log(`upstream/call ${name} → attempting ${candidate}${candidate === name ? "" : " (official name)"}`);
        try {
          return await callToolJson(state.upstream.client, candidate, args, timeoutMs);
        } catch (e) {
          lastError = e;
          log(`  ${candidate} did not answer: ${redactSecrets(errorMessage(e), state.cfg.binanceMcpToken)}`);
        }
      }
      throw lastError ?? new JsonRpcError(JSONRPC_UPSTREAM_UNAVAILABLE, "upstream call failed", { name });
    }
    default:
      throw new JsonRpcError(JSONRPC_METHOD_NOT_FOUND, `method not found: ${method}`);
  }
}

function rpcError(id: string | number | null, code: number, message: string, data?: unknown): JsonRpcResponse {
  const err: JsonRpcResponse["error"] = { code, message };
  if (data !== undefined) err.data = data;
  return { jsonrpc: "2.0", id, error: err };
}

/** Handle a raw request body and return the JSON-RPC response object (exported for tests/embedding). */
export async function handleRpcBody(state: BridgeState, body: string): Promise<JsonRpcResponse> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return rpcError(null, JSONRPC_PARSE_ERROR, "parse error");
  }
  if (!isRecord(parsed) || parsed.jsonrpc !== "2.0" || typeof parsed.method !== "string") {
    const id = isRecord(parsed) && (typeof parsed.id === "string" || typeof parsed.id === "number") ? parsed.id : null;
    return rpcError(id, JSONRPC_INVALID_REQUEST, "invalid request (expected {jsonrpc:'2.0', method, id, params?})");
  }
  const id: string | number | null = typeof parsed.id === "string" || typeof parsed.id === "number" ? parsed.id : null;
  try {
    const result = await dispatchRpc(state, parsed.method, parsed.params);
    return { jsonrpc: "2.0", id, result };
  } catch (e) {
    if (e instanceof JsonRpcError) return rpcError(id, e.code, e.message, e.data);
    const msg = redactSecrets(errorMessage(e), state.cfg.binanceMcpToken);
    log(`rpc ${parsed.method} failed: ${msg}`);
    return rpcError(id, JSONRPC_UPSTREAM_UNAVAILABLE, "upstream call failed", { reason: msg });
  }
}

function readBody(req: IncomingMessage, limit = 1_000_000): Promise<string> {
  return new Promise((resolvePromise, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > limit) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolvePromise(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(body) });
  res.end(body);
}

/**
 * Serve the JSON-RPC 2.0 façade on `cfg.port` (`POST /rpc`, plus `GET /health`).
 * Resolves once listening; the server keeps the event loop alive until closed via
 * `onListening(server).close()` or a signal (when `installSignalHandlers` is set).
 */
export async function serve(cfg: BridgeConfig, opts: ServeOptions = {}): Promise<void> {
  const state: BridgeState = {
    cfg,
    deltr: opts.deltr ?? null,
    deltrOwned: false,
    upstream: opts.upstream ?? (await connectUpstream(cfg)),
    upstreamOwned: !opts.upstream,
  };
  void reportUpstream(cfg, state.upstream.status);

  const server: Server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? "/", "http://localhost");
      if (req.method === "GET" && (url.pathname === "/health" || url.pathname === "/")) {
        sendJson(res, 200, { ok: true, service: "deltr-agent-os-bridge", version: BRIDGE_VERSION, deltrUrl: cfg.deltrUrl, upstream: state.upstream.status });
        return;
      }
      if (url.pathname !== "/rpc") {
        sendJson(res, 404, rpcError(null, JSONRPC_METHOD_NOT_FOUND, "not found: use POST /rpc"));
        return;
      }
      if (req.method !== "POST") {
        sendJson(res, 405, rpcError(null, JSONRPC_INVALID_REQUEST, "use POST /rpc with a JSON-RPC 2.0 body"));
        return;
      }
      const body = await readBody(req);
      sendJson(res, 200, await handleRpcBody(state, body));
    } catch (e) {
      sendJson(res, 200, rpcError(null, JSONRPC_INVALID_REQUEST, redactSecrets(errorMessage(e), cfg.binanceMcpToken)));
    }
  });

  const close = async (): Promise<void> => {
    await new Promise<void>((resolvePromise) => server.close(() => resolvePromise()));
    if (state.deltrOwned && state.deltr) await state.deltr.close().catch(() => undefined);
    if (state.upstreamOwned && state.upstream.client) await state.upstream.client.close().catch(() => undefined);
  };

  await new Promise<void>((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(cfg.port, "127.0.0.1", () => {
      server.off("error", reject);
      resolvePromise();
    });
  });
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : cfg.port;
  log(`JSON-RPC façade listening on http://127.0.0.1:${port}/rpc (deltr=${cfg.deltrUrl}, upstream=${state.upstream.status.kind})`);

  if (opts.installSignalHandlers) {
    const stop = () => {
      log("shutting down");
      void close().finally(() => process.exit(0));
    };
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
  }
  opts.onListening?.({ port, close });
}

// ─────────────────────────────────────────────────────────────────────────────
// CLI
// ─────────────────────────────────────────────────────────────────────────────

const USAGE = `usage: npx tsx agents/agent_os_bridge.ts <command>

commands:
  list                       list Deltr MCP tools (JSON array on stdout)
  call <tool> '<json>'       call a Deltr tool with JSON arguments
  route '<order-json>'       two-phase route: {"symbol","capital_usd","leverage?","confirm?"}
  upstream-list              upstream Binance MCP status + discovered tool names
  upstream-status            upstream Binance MCP status only
  serve                      JSON-RPC 2.0 façade on POST /rpc (DELTR_BRIDGE_PORT, default 8788)

env: DELTR_MCP_URL (default ${DEFAULT_DELTR_MCP_URL}), BINANCE_MCP_URL, BINANCE_MCP_TOKEN (never logged),
     DELTR_BRIDGE_PORT (default ${DEFAULT_BRIDGE_PORT}), DELTR_BRIDGE_TIMEOUT_MS (default ${DEFAULT_TIMEOUT_MS})
`;

function emit(result: unknown): void {
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

function parseJsonArg(raw: string | undefined, what: string): Record<string, unknown> {
  if (raw === undefined || raw.trim() === "") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new JsonRpcError(JSONRPC_PARSE_ERROR, `${what} is not valid JSON: ${errorMessage(e)}`);
  }
  if (!isRecord(parsed)) throw new JsonRpcError(JSONRPC_INVALID_PARAMS, `${what} must be a JSON object`);
  return parsed;
}

/** CLI entry point. Returns the process exit code (0 ok, 1 failure, 2 usage). */
export async function main(argv: string[]): Promise<number> {
  const cfg = configFromEnv();
  const [cmd, ...rest] = argv;
  const timeoutMs = cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  if (!cmd || cmd === "-h" || cmd === "--help" || cmd === "help") {
    process.stderr.write(USAGE);
    return cmd ? 0 : 2;
  }

  let deltr: Client | null = null;
  let upstream: { client: Client | null; status: UpstreamStatus } | null = null;
  try {
    switch (cmd) {
      case "list": {
        deltr = await connectDeltr(cfg);
        emit(await listTools(deltr));
        return 0;
      }
      case "call": {
        const tool = rest[0];
        if (!tool) {
          process.stderr.write("call: missing <tool>\n" + USAGE);
          return 2;
        }
        const args = parseJsonArg(rest[1], "arguments");
        deltr = await connectDeltr(cfg);
        const result = await callToolJson(deltr, tool, args, timeoutMs);
        emit(result);
        return pickError(result) ? 1 : 0;
      }
      case "route": {
        const intent = validateOrderIntent(parseJsonArg(rest[0], "order"));
        deltr = await connectDeltr(cfg);
        const receipt = await routeOrder(deltr, intent, timeoutMs);
        emit(receipt);
        return receipt.ok ? 0 : 1;
      }
      case "upstream-status": {
        upstream = await connectUpstream(cfg);
        await reportUpstream(cfg, upstream.status);
        emit(upstream.status);
        return 0;
      }
      case "upstream-list": {
        upstream = await connectUpstream(cfg);
        await reportUpstream(cfg, upstream.status);
        emit({ status: upstream.status, tools: upstream.status.toolsDiscovered, officialNames: officialNameReport(upstream.status.toolsDiscovered) });
        return 0;
      }
      case "serve": {
        log(`config: ${JSON.stringify(describeConfig(cfg))}`);
        await serve(cfg, { installSignalHandlers: true });
        return 0;
      }
      default:
        process.stderr.write(`unknown command: ${cmd}\n${USAGE}`);
        return 2;
    }
  } catch (e) {
    const msg = redactSecrets(errorMessage(e), cfg.binanceMcpToken);
    if (e instanceof JsonRpcError) {
      log(`error ${e.code}: ${msg}`);
      emit({ error: { code: e.code, message: msg, data: e.data } });
      return e.code === JSONRPC_INVALID_PARAMS || e.code === JSONRPC_PARSE_ERROR ? 2 : 1;
    }
    log(`error: ${msg}`);
    emit({ error: { code: JSONRPC_UPSTREAM_UNAVAILABLE, message: "bridge call failed", data: { reason: msg } } });
    return 1;
  } finally {
    if (cmd !== "serve") {
      if (deltr) await deltr.close().catch(() => undefined);
      if (upstream?.client) await upstream.client.close().catch(() => undefined);
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then(
    (code) => {
      if (code !== 0 || process.argv[2] !== "serve") process.exitCode = code;
    },
    (e) => {
      log(`fatal: ${errorMessage(e)}`);
      process.exitCode = 1;
    },
  );
}
