/**
 * Offline, deterministic tests for agents/agent_os_bridge.ts.
 *
 * Run:  npx tsx --test agents/bridge.test.ts
 *   or: node --import tsx --test agents/bridge.test.ts
 *
 * No network: Deltr is a fake McpServer over InMemoryTransport; the "official" upstream is a
 * local node:http server answering 401 like the real OAuth-gated endpoint; the shim path spawns
 * a tiny stdio MCP server written to a temp file.
 */

import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { z } from "zod";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

import {
  JSONRPC_INVALID_PARAMS,
  JSONRPC_METHOD_NOT_FOUND,
  JSONRPC_PARSE_ERROR,
  JSONRPC_UPSTREAM_UNAVAILABLE,
  JsonRpcError,
  OFFICIAL_TOOL_ALIASES,
  READ_ONLY_ALLOWLIST_REFUSED_MESSAGE,
  TRADE_CALL_REFUSED_MESSAGE,
  isForwardableReadOnly,
  officialCandidates,
  officialNameReport,
  type BridgeConfig,
  type BridgeServer,
  type JsonRpcResponse,
  type RouteReceipt,
  configFromEnv,
  connectUpstream,
  describeConfig,
  listTools,
  redactSecrets,
  routeOrder,
  serve,
  unwrapToolResult,
  validateOrderIntent,
} from "./agent_os_bridge.js";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// ─────────────────────────────────────────────────────────────────────────────
// Fake Deltr MCP server (in-memory)
// ─────────────────────────────────────────────────────────────────────────────

type FakeCall = { name: string; args: Record<string, unknown> };

function jsonResult(obj: Record<string, unknown>) {
  return { content: [{ type: "text" as const, text: JSON.stringify(obj) }], structuredContent: obj };
}

/** A Deltr look-alike: approves capital < 100_000, vetoes above; execute needs confirm. */
async function makeFakeDeltr(): Promise<{ client: Client; calls: FakeCall[]; close: () => Promise<void> }> {
  const calls: FakeCall[] = [];
  const server = new McpServer({ name: "deltr-fake", version: "0.0.0" });

  server.registerTool("deltr_status", { description: "status" }, async () => jsonResult({ mode: "PAPER", halted: false, kill_switch: false }));

  server.registerTool(
    "deltr_propose_hedge",
    {
      description: "propose",
      inputSchema: { capital_usd: z.number(), leverage: z.number().optional(), symbol: z.string().optional() },
    },
    async (args) => {
      calls.push({ name: "deltr_propose_hedge", args });
      const approved = args.capital_usd < 100_000;
      return jsonResult({
        plan_id: "plan_fake_1",
        plan: { symbol: args.symbol ?? "BNBUSDT", leverage: args.leverage ?? 2, notional_usd: args.capital_usd / (1 + 1 / (args.leverage ?? 2)) },
        precheck: approved
          ? { approved: true, code: "APPROVED", reason: "all checks passed", dry_run: true }
          : { approved: false, code: "CAPITAL_CAPACITY", reason: "capital exceeds free equity", dry_run: true },
        expires_at: "2026-09-02T00:01:00Z",
        message: approved ? "approved" : "vetoed",
      });
    },
  );

  server.registerTool(
    "deltr_execute_hedge",
    { description: "execute", inputSchema: { plan_id: z.string(), confirm: z.boolean().optional() } },
    async (args) => {
      calls.push({ name: "deltr_execute_hedge", args });
      if (!args.confirm) return jsonResult({ error: { code: "CONFIRM_REQUIRED", message: "confirm=true required" } });
      return jsonResult({ id: "rcpt_fake_1", plan_id: args.plan_id, status: "FILLED", mode: "PAPER", fills: [] });
    },
  );

  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  const client = new Client({ name: "bridge-test", version: "0.0.0" }, { capabilities: {} });
  await client.connect(clientTransport);
  return {
    client,
    calls,
    close: async () => {
      await client.close();
      await server.close();
    },
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// (a) routeOrder + listTools against the in-memory Deltr
// ─────────────────────────────────────────────────────────────────────────────

describe("routeOrder / listTools (in-memory Deltr)", () => {
  let fake: Awaited<ReturnType<typeof makeFakeDeltr>>;
  before(async () => {
    fake = await makeFakeDeltr();
  });
  after(async () => {
    await fake.close();
  });

  test("listTools returns the fake Deltr tool names", async () => {
    const names = await listTools(fake.client);
    assert.deepEqual(names.sort(), ["deltr_execute_hedge", "deltr_propose_hedge", "deltr_status"]);
  });

  test("propose-only when confirm is not set: approved, no execute call", async () => {
    fake.calls.length = 0;
    const r = await routeOrder(fake.client, { symbol: "BNBUSDT", capital_usd: 5000, leverage: 2 });
    assert.equal(r.ok, true);
    assert.equal(r.plan_id, "plan_fake_1");
    assert.equal((r.precheck as { approved: boolean }).approved, true);
    assert.equal(r.receipt, undefined);
    assert.deepEqual(
      fake.calls.map((c) => c.name),
      ["deltr_propose_hedge"],
    );
    assert.equal(fake.calls[0].args.capital_usd, 5000);
    assert.equal(fake.calls[0].args.leverage, 2);
    assert.equal(fake.calls[0].args.symbol, "BNBUSDT");
  });

  test("two-phase: approved + confirm executes with confirm=true", async () => {
    fake.calls.length = 0;
    const r = await routeOrder(fake.client, { symbol: "BNBUSDT", capital_usd: 5000, leverage: 2, confirm: true });
    assert.equal(r.ok, true);
    assert.equal(r.plan_id, "plan_fake_1");
    assert.equal((r.receipt as { id: string }).id, "rcpt_fake_1");
    assert.deepEqual(
      fake.calls.map((c) => c.name),
      ["deltr_propose_hedge", "deltr_execute_hedge"],
    );
    assert.deepEqual(fake.calls[1].args, { plan_id: "plan_fake_1", confirm: true });
  });

  test("vetoed precheck never executes, even with confirm", async () => {
    fake.calls.length = 0;
    const r = await routeOrder(fake.client, { symbol: "BNBUSDT", capital_usd: 1_000_000, leverage: 2, confirm: true });
    assert.equal(r.ok, false);
    assert.equal(r.error?.code, "CAPITAL_CAPACITY");
    assert.deepEqual(
      fake.calls.map((c) => c.name),
      ["deltr_propose_hedge"],
    );
  });

  test("unwrapToolResult prefers structuredContent, falls back to JSON text, then raw text", () => {
    assert.deepEqual(unwrapToolResult({ content: [{ type: "text", text: "{\"a\":1}" }], structuredContent: { a: 2 } }), { a: 2 });
    assert.deepEqual(unwrapToolResult({ content: [{ type: "text", text: "{\"a\":1}" }] }), { a: 1 });
    assert.deepEqual(unwrapToolResult({ content: [{ type: "text", text: "not json" }] }), { text: "not json" });
    assert.equal(unwrapToolResult({ isError: true, content: [{ type: "text", text: "boom" }] }).error !== undefined, true);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// (b) validateOrderIntent
// ─────────────────────────────────────────────────────────────────────────────

describe("validateOrderIntent", () => {
  const rejects = (input: unknown, re: RegExp) => {
    assert.throws(
      () => validateOrderIntent(input),
      (e: unknown) => e instanceof JsonRpcError && e.code === JSONRPC_INVALID_PARAMS && re.test(e.message),
    );
  };

  test("accepts a canonical intent and normalises symbol", () => {
    const i = validateOrderIntent({ symbol: "bnbusdt", capital_usd: 5000, leverage: 2, confirm: false, reason: "demo" });
    assert.deepEqual(i, { symbol: "BNBUSDT", capital_usd: 5000, leverage: 2, confirm: false, reason: "demo" });
  });

  test("defaults symbol to BNBUSDT and leaves optional fields absent", () => {
    assert.deepEqual(validateOrderIntent({ capital_usd: 100 }), { symbol: "BNBUSDT", capital_usd: 100 });
  });

  test("rejects leverage 10 (hard cap 3x) with -32602", () => {
    rejects({ symbol: "BNBUSDT", capital_usd: 5000, leverage: 10 }, /leverage 10 exceeds the hard cap of 3x/);
  });

  test("rejects missing capital with -32602", () => {
    rejects({ symbol: "BNBUSDT", leverage: 2 }, /capital_usd is required/);
  });

  test("rejects non-object, bad capital, non-positive leverage, bad types", () => {
    rejects("nope", /expected an object/);
    rejects({ capital_usd: "5000" }, /finite number/);
    rejects({ capital_usd: 1 }, />= 10/);
    rejects({ capital_usd: 100, leverage: 0 }, /> 0/);
    rejects({ capital_usd: 100, confirm: "yes" }, /confirm must be a boolean/);
    rejects({ capital_usd: 100, symbol: 42 }, /symbol/);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// (c) serve(): JSON-RPC façade on an ephemeral port
// ─────────────────────────────────────────────────────────────────────────────

async function rpc(port: number, body: string): Promise<JsonRpcResponse> {
  const res = await fetch(`http://127.0.0.1:${port}/rpc`, { method: "POST", headers: { "content-type": "application/json" }, body });
  assert.equal(res.status, 200);
  return (await res.json()) as JsonRpcResponse;
}

function call(port: number, method: string, params?: unknown, id: string | number = 1): Promise<JsonRpcResponse> {
  return rpc(port, JSON.stringify({ jsonrpc: "2.0", id, method, params }));
}

describe("serve() JSON-RPC façade", () => {
  let fake: Awaited<ReturnType<typeof makeFakeDeltr>>;
  let bridge: BridgeServer;

  before(async () => {
    fake = await makeFakeDeltr();
    const cfg: BridgeConfig = { deltrUrl: "http://127.0.0.1:1/mcp", port: 0, shimCommand: [], timeoutMs: 2000 };
    await new Promise<void>((resolveListening) => {
      void serve(cfg, {
        deltr: fake.client,
        onListening: (s) => {
          bridge = s;
          resolveListening();
        },
      });
    });
  });
  after(async () => {
    await bridge.close();
    await fake.close();
  });

  test("tools/list forwards Deltr's tool list", async () => {
    const r = await call(bridge.port, "tools/list");
    assert.equal(r.error, undefined);
    const tools = (r.result as { tools: Array<{ name: string }> }).tools.map((t) => t.name).sort();
    assert.deepEqual(tools, ["deltr_execute_hedge", "deltr_propose_hedge", "deltr_status"]);
    assert.equal(r.id, 1);
  });

  test("tools/call forwards to Deltr and unwraps the JSON result", async () => {
    const r = await call(bridge.port, "tools/call", { name: "deltr_status", arguments: {} }, "abc");
    assert.equal(r.id, "abc");
    assert.deepEqual(r.result, { mode: "PAPER", halted: false, kill_switch: false });
  });

  test("deltr.route_order routes through propose → execute", async () => {
    fake.calls.length = 0;
    const r = await call(bridge.port, "deltr.route_order", { intent: { symbol: "BNBUSDT", capital_usd: 5000, leverage: 2, confirm: true } });
    assert.equal(r.error, undefined);
    const receipt = r.result as RouteReceipt;
    assert.equal(receipt.ok, true);
    assert.equal(receipt.plan_id, "plan_fake_1");
    assert.deepEqual(
      fake.calls.map((c) => c.name),
      ["deltr_propose_hedge", "deltr_execute_hedge"],
    );
  });

  test("deltr.route_order rejects leverage 10 with -32602 before touching Deltr", async () => {
    fake.calls.length = 0;
    const r = await call(bridge.port, "deltr.route_order", { intent: { symbol: "BNBUSDT", capital_usd: 5000, leverage: 10 } });
    assert.equal(r.error?.code, JSONRPC_INVALID_PARAMS);
    assert.equal(fake.calls.length, 0);
  });

  test("unknown method → -32601", async () => {
    const r = await call(bridge.port, "deltr.nope");
    assert.equal(r.error?.code, JSONRPC_METHOD_NOT_FOUND);
    assert.equal(r.id, 1);
  });

  test("parse error → -32700 with null id", async () => {
    const r = await rpc(bridge.port, "{this is not json");
    assert.equal(r.error?.code, JSONRPC_PARSE_ERROR);
    assert.equal(r.id, null);
  });

  test("invalid request (no method / wrong version) → -32600", async () => {
    const r = await rpc(bridge.port, JSON.stringify({ jsonrpc: "1.0", id: 7 }));
    assert.equal(r.error?.code, -32600);
    assert.equal(r.id, 7);
  });

  test("upstream/status reports kind none when nothing is configured", async () => {
    const r = await call(bridge.port, "upstream/status");
    const status = r.result as { kind: string; authorized: boolean; toolsDiscovered: string[]; error?: string };
    assert.equal(status.kind, "none");
    assert.equal(status.authorized, false);
    assert.deepEqual(status.toolsDiscovered, []);
    assert.match(status.error ?? "", /BINANCE_MCP_URL unset/);
    assert.match(status.error ?? "", /shim disabled/);
  });

  test("upstream/list mirrors status + tools", async () => {
    const r = await call(bridge.port, "upstream/list");
    const out = r.result as { status: { kind: string }; tools: string[] };
    assert.equal(out.status.kind, "none");
    assert.deepEqual(out.tools, []);
  });

  test("upstream/call refuses trade-shaped names with -32601 and the exact message", async () => {
    for (const name of ["place_futures_order", "set_leverage", "cancel_order", "deltr_execute_hedge"]) {
      const r = await call(bridge.port, "upstream/call", { name, arguments: {} });
      assert.equal(r.error?.code, JSONRPC_METHOD_NOT_FOUND, name);
      assert.equal(r.error?.message, TRADE_CALL_REFUSED_MESSAGE);
    }
  });

  test("upstream/call for a get_* tool with no upstream → -32000 with data.reason", async () => {
    const r = await call(bridge.port, "upstream/call", { name: "get_mark_price", arguments: { symbol: "BNBUSDT" } });
    assert.equal(r.error?.code, JSONRPC_UPSTREAM_UNAVAILABLE);
    assert.equal(typeof (r.error?.data as { reason: string }).reason, "string");
  });

  test("GET /health exposes the upstream status", async () => {
    const res = await fetch(`http://127.0.0.1:${bridge.port}/health`);
    assert.equal(res.status, 200);
    const body = (await res.json()) as { ok: boolean; upstream: { kind: string } };
    assert.equal(body.ok, true);
    assert.equal(body.upstream.kind, "none");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// connectUpstream: OAuth-gated official endpoint (401) and the stdio shim path
// ─────────────────────────────────────────────────────────────────────────────

function startFake401Server(): Promise<{ url: string; server: Server; hits: number }> {
  const state = { hits: 0 };
  const server = createServer((_req, res) => {
    state.hits += 1;
    res.writeHead(401, {
      "content-type": "application/json",
      "www-authenticate": 'Bearer resource_metadata="http://127.0.0.1/.well-known/oauth-protected-resource/gateway-mcp"',
    });
    res.end(JSON.stringify({ error: "unauthorized" }));
  });
  return new Promise((resolveServer) => {
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      resolveServer({ url: `http://127.0.0.1:${port}/mcp/agentic`, server, hits: state.hits });
    });
  });
}

/** Minimal stdio MCP server (a stand-in for deltr.mcp.binance_shim_server) written to a temp dir. */
function writeFakeShim(dir: string): string {
  const sdk = pathToFileURL(join(REPO_ROOT, "node_modules", "@modelcontextprotocol", "sdk", "dist", "esm")).href;
  const src = `
import { McpServer } from "${sdk}/server/mcp.js";
import { StdioServerTransport } from "${sdk}/server/stdio.js";
const server = new McpServer({ name: "binance-shim-fake", version: "0.0.0" });
const json = (o) => ({ content: [{ type: "text", text: JSON.stringify(o) }], structuredContent: o });
server.registerTool("get_mark_price", { description: "mark" }, async () => json({ symbol: "BNBUSDT", markPrice: "686.34", source: "fake" }));
server.registerTool("place_futures_order", { description: "trade (never reachable through the bridge)" }, async () => json({ error: { code: "NO_CREDENTIALS", message: "no keys" } }));
await server.connect(new StdioServerTransport());
`;
  const path = join(dir, "fake_shim.mjs");
  writeFileSync(path, src, "utf8");
  return path;
}

describe("connectUpstream discovery order", () => {
  let tmp: string;
  let shimPath: string;
  before(() => {
    tmp = mkdtempSync(join(tmpdir(), "deltr-bridge-"));
    shimPath = writeFakeShim(tmp);
  });
  after(() => {
    rmSync(tmp, { recursive: true, force: true });
  });

  test("official 401 → authorized:false, falls through to none when shim disabled", async () => {
    const fake = await startFake401Server();
    try {
      const { client, status } = await connectUpstream({ deltrUrl: "http://127.0.0.1:1/mcp", port: 0, binanceMcpUrl: fake.url, binanceMcpToken: "sekrit-token-value-123", shimCommand: [], timeoutMs: 3000 });
      assert.equal(client, null);
      assert.equal(status.kind, "none");
      assert.equal(status.authorized, false);
      assert.match(status.error ?? "", /official upstream unavailable: OAuth\/bearer required \(401\)/);
      assert.doesNotMatch(status.error ?? "", /sekrit-token-value-123/);
    } finally {
      await new Promise<void>((r) => fake.server.close(() => r()));
    }
  });

  test("official unavailable → shim over stdio → kind shim with discovered tools", async () => {
    const fake = await startFake401Server();
    try {
      const { client, status } = await connectUpstream({
        deltrUrl: "http://127.0.0.1:1/mcp",
        port: 0,
        binanceMcpUrl: fake.url,
        shimCommand: [process.execPath, shimPath],
        timeoutMs: 8000,
      });
      try {
        assert.equal(status.kind, "shim");
        assert.equal(status.authorized, true);
        assert.deepEqual(status.toolsDiscovered.sort(), ["get_mark_price", "place_futures_order"]);
        assert.match(status.error ?? "", /official upstream unavailable/);
        assert.ok(client);
        const res = unwrapToolResult(await client.callTool({ name: "get_mark_price", arguments: {} }));
        assert.equal(res.markPrice, "686.34");
      } finally {
        await client?.close();
      }
    } finally {
      await new Promise<void>((r) => fake.server.close(() => r()));
    }
  });

  test("broken shim command → none with the spawn reason", async () => {
    const { client, status } = await connectUpstream({ deltrUrl: "http://127.0.0.1:1/mcp", port: 0, shimCommand: ["/nonexistent/deltr-shim-binary"], timeoutMs: 3000 });
    assert.equal(client, null);
    assert.equal(status.kind, "none");
    assert.match(status.error ?? "", /BINANCE_MCP_URL unset/);
    assert.match(status.error ?? "", /shim unavailable: .*ENOENT/);
  });

  test("serve() gates upstream/call through a live shim: get_* forwarded, trade names refused", async () => {
    const upstream = await connectUpstream({ deltrUrl: "http://127.0.0.1:1/mcp", port: 0, shimCommand: [process.execPath, shimPath], timeoutMs: 8000 });
    assert.equal(upstream.status.kind, "shim");
    const deltr = await makeFakeDeltr();
    let bridge!: BridgeServer;
    await new Promise<void>((r) => {
      void serve({ deltrUrl: "http://127.0.0.1:1/mcp", port: 0, shimCommand: [] }, { deltr: deltr.client, upstream, onListening: (s) => { bridge = s; r(); } });
    });
    try {
      const ok = await call(bridge.port, "upstream/call", { name: "get_mark_price", arguments: { symbol: "BNBUSDT" } });
      assert.equal((ok.result as { markPrice: string }).markPrice, "686.34");
      const refused = await call(bridge.port, "upstream/call", { name: "place_futures_order", arguments: {} });
      assert.equal(refused.error?.code, JSONRPC_METHOD_NOT_FOUND);
      assert.equal(refused.error?.message, TRADE_CALL_REFUSED_MESSAGE);
      const st = await call(bridge.port, "upstream/status");
      assert.equal((st.result as { kind: string }).kind, "shim");
    } finally {
      await bridge.close();
      await upstream.client?.close();
      await deltr.close();
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Config / secrets hygiene
// ─────────────────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────────────────
// Official Binance tool names (transcribed inventory — see the fixture's provenance block)
// ─────────────────────────────────────────────────────────────────────────────

describe("official tool names", () => {
  test("read calls attempt the official dotted name before the local one", () => {
    assert.deepEqual(officialCandidates("get_mark_price"), [
      "futures_usds.premiumIndexKlineData",
      "futures_usds.markPriceKlineCandlestickData",
      "get_mark_price",
    ]);
    // Only what the upstream advertises is attempted, when it advertised anything.
    assert.deepEqual(officialCandidates("get_mark_price", ["get_mark_price"]), ["get_mark_price"]);
    assert.deepEqual(officialCandidates("get_mark_price", ["futures_usds.premiumIndexKlineData"]), ["futures_usds.premiumIndexKlineData"]);
    // An official name asks for itself; an unknown name is attempted verbatim.
    assert.deepEqual(officialCandidates("spot.depth"), ["spot.depth"]);
    assert.deepEqual(officialCandidates("get_funding_rate"), ["get_funding_rate"]);
  });

  test("no write-capable name is aliased, and only read-only names are forwardable", () => {
    for (const alias of Object.values(OFFICIAL_TOOL_ALIASES).flat()) assert.ok(isForwardableReadOnly(alias), alias);
    for (const name of ["place_futures_order", "set_leverage", "cancel_order", "futures_usds.newOrder", "deltr_execute_hedge"]) {
      assert.equal(isForwardableReadOnly(name), false, name);
    }
    // Read-only names outside the bridge's allowlist are refused too (default-deny).
    assert.equal(isForwardableReadOnly("margin.queryMaxBorrow"), false);
    assert.ok(READ_ONLY_ALLOWLIST_REFUSED_MESSAGE.includes("read-only"));
    const report = officialNameReport(["spot.depth", "get_funding_rate"]);
    assert.deepEqual(report.matched, ["spot.depth"]);
    assert.ok(report.missing.includes("futures_usds.premiumIndexKlineData"));
  });
});

describe("config and redaction", () => {
  test("configFromEnv applies defaults and env overrides", () => {
    const d = configFromEnv({});
    assert.equal(d.deltrUrl, "http://127.0.0.1:8000/mcp");
    assert.equal(d.port, 8788);
    assert.equal(d.binanceMcpUrl, undefined);
    const c = configFromEnv({ DELTR_MCP_URL: "http://localhost:9000/mcp", BINANCE_MCP_URL: "https://agent.binance.com/mcp/agentic", BINANCE_MCP_TOKEN: "tok", DELTR_BRIDGE_PORT: "0" });
    assert.equal(c.deltrUrl, "http://localhost:9000/mcp");
    assert.equal(c.binanceMcpUrl, "https://agent.binance.com/mcp/agentic");
    assert.equal(c.binanceMcpToken, "tok");
    assert.equal(c.port, 0);
  });

  test("describeConfig never exposes the token value", () => {
    const shown = JSON.stringify(describeConfig({ deltrUrl: "x", port: 1, binanceMcpToken: "super-secret-token-value" }));
    assert.doesNotMatch(shown, /super-secret-token-value/);
    assert.match(shown, /<set>/);
  });

  test("redactSecrets scrubs bearer tokens and key=value secrets", () => {
    const s = redactSecrets("Authorization: Bearer abcdefghijklmnop; api_key=ZZZ123 token: qqq", "qqq");
    assert.doesNotMatch(s, /abcdefghijklmnop/);
    assert.doesNotMatch(s, /ZZZ123/);
    assert.doesNotMatch(s, /qqq/);
  });
});
