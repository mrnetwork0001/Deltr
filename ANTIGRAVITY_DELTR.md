# ⚡ ANTIGRAVITY_DELTR — Persistent Project Context Directive

> **Project Name:** DELTR  
> **Target Event:** Binance Agent OS Mini Hackathon ($60,000 USDC Prize Pool)  
> **Host:** Binance (`@Binance`)  
> **Submission Deadline:** September 8, 2026 @ 23:59 UTC  
> **Target:** Track A (judged) + Track B task completion  
> **Primary Tracks:** Track B (Connect your MCPs and trade) & Track A (Build an AI agent with Agent OS)  
> **Core Stack:** Binance Agent OS (hosted Binance MCP Server + Skills Hub) + Deltr MCP server + PancakeSwap V3 quotes + Binance Futures testnet + Python 3.11 + Next.js 14  

---

## 📌 Core Directives for Deltr Development

1. **Master Spec Source of Truth:**  
   Always consult [DELTR_PROJECT_SPEC.md](file:///Users/mrnetwork/Deltr/DELTR_PROJECT_SPEC.md).

2. **Technical Architecture Guidelines:**
   - **Binance Agent OS:** There is no Agent OS SDK. Agent OS is the hosted Binance MCP server (`https://agent.binance.com/mcp/agentic`, OAuth via a supported host), the Skills Hub, the Agentic Wallet and the APIs. `agents/agent_os_bridge.ts` is an MCP client that connects to Deltr's own MCP server and discovers the Binance MCP upstream (official, then local shim, then none) and reports truthfully which one it reached.
   - **Binance MCP Server:** the bridge discovers tool names at runtime (`tools/list`) once a host has authorised it, and attempts the official Agent OS names first. Those names come from `tests/fixtures/binance_mcp_tools.json`, transcribed from a third-party published inventory dated 2026-09-02 and not captured from our own session. The official endpoint was never exercised from the build machine: its default resolver refuses the hostname and the endpoint needs OAuth through a supported host. Deltr exposes its own 18 MCP tools for Claude Code / Claude Desktop / Codex / Cursor.
   - **Deterministic Risk Gate:** Enforce leverage and risk bounds in `risk_gate.py` (19 ordered checks, zero LLM, measured median quoted at startup). Every order passes it. Modes are PAPER and Binance Futures TESTNET only; there is no live mode.
   - **Honesty rules:** funding figures are testnet-derived and indicative; latency is quoted as measured; USDC in prompts is treated as USDT-equivalent for sizing; no "risk-free" or "guaranteed" wording anywhere.

3. **Submission Requirements Checklist:**
   - Public GitHub repository under Apache 2.0 / MIT License.
   - 3-Minute Video Presentation & Demo walkthrough.
   - Public X quote repost tagging `@Binance`.

4. **Repository Key Files:**
   - Master Spec: `DELTR_PROJECT_SPEC.md`
   - Directives: `ANTIGRAVITY_DELTR.md`
   - Skill Instructions: `.agents/skills/deltr-binance/SKILL.md`
