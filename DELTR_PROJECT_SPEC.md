# ⚡ DELTR — CEX ↔ DEX Delta-Neutral Basis/Funding Arbitrage Agent on Binance Agent OS + MCP

> **Binance Agent OS Mini Hackathon Master Blueprint ($60,000 USDC Prize Pool)**  
> **Host:** Binance (`@Binance`)  
> **Target:** Track A (judged, $20,000 USDC pool) + complete the Track B task (connect to Binance MCP and trade; $40,000 USDC pool paid as 4 USDC to the first 10,000 completions)  
> **Submission Deadline:** September 8, 2026 @ 23:59 UTC  
> **Primary Tracks:** Track B (Connect your MCPs and trade) & Track A (Build an AI agent with Agent OS)  
> **Core Tech Stack:** Binance Agent OS (hosted Binance MCP Server `agent.binance.com/mcp/agentic` + Skills Hub) + Deltr's own MCP server + BNB Chain PancakeSwap V3 (read-only quotes) + Binance USDⓈ-M Futures testnet API + Python 3.11 + Next.js 14  
> **License:** Apache 2.0 Open Source  
> **Author:** Ifeanyichukwu Onwo (`mrnetwork`)  

---

## 📌 Executive Summary & Core Value Proposition

Most trading agents fail because they execute basic 1-pair spot orders without market hedging or trade with unconstrained model authority that risks large drawdowns during market volatility.

**DELTR** is a **CEX ↔ DEX delta-neutral basis/funding arbitrage agent** built on Binance Agent OS (the hosted Binance MCP server + Skills Hub; there is no Agent OS SDK) and the Model Context Protocol (MCP). Deltr is itself an MCP server and ships as a Skills Hub skill.

Deltr monitors the basis between **PancakeSwap V3 on BNB Chain** (read-only mainnet quotes) and the **Binance USDⓈ-M Futures mark price** (testnet), plus the funding rate, and prices the full round trip. When the net edge clears the threshold and a prompt or MCP client asks for it, Deltr proposes and (on a second, explicit call) executes a **Delta-Neutral Hedge** (Long on PancakeSwap, simulated; Short on Binance Futures testnet), targeting basis plus funding carry with **no directional market exposure by construction** (the two legs offset). Funding figures are testnet-derived and indicative; the live net edge is often negative and the gate then says no. All trades pass through a **zero-LLM deterministic Python risk gate** before orders reach Binance. Deltr trades PAPER (simulated fills on live prices) or Binance Futures TESTNET only; there is deliberately no production trading mode.

---

## 🏗️ 4-Agent Architecture & Execution Flow

```
   ┌────────────────────────────────────────────────────────┐
   │             NATURAL LANGUAGE TRADING PROMPT            │
   │   ("Rebalance $5,000 USDC into delta-neutral arbitrage")│
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │      MCP HOST (Claude Code / Claude Desktop / Codex)    │
   │   (LLM turns the prompt into Deltr MCP tool calls)     │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │   DELTR MCP SERVER  +  BINANCE MCP SERVER (Agent OS)   │
   │ (18 Deltr tools; bridge reports official / shim / none)│
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │     HARDENED DETERMINISTIC RISK GATE (ZERO-LLM)        │
   │  (Vetoes orders if leverage > 3x or risk > 2% capital) │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │   PAPER ROUTER  |  TESTNET ROUTER (Binance Futures)    │
   │ (paired fills, receipts with sha256, mark-to-close PnL)│
   └───────────────────────────┬────────────────────────────┘
```

---

## 🌟 4 Key Subsystems

### 1. CEX ↔ DEX Arbitrage Scout (`agents/arbitrage_scout.py`)
- Polls PancakeSwap V3 (QuoterV2, BSC mainnet) and Binance Futures testnet mark/funding (spot mirror as a reference) and computes the horizon-based net edge after every cost; flags an opportunity actionable only when it clears the minimum edge.

### 2. Delta-Neutral Hedging Engine (`agents/hedger.py`)
- Sizes paired, step-aligned orders (`notional = capital / (1 + 1/L)`: buy BNB on the DEX, short the same quantity on the perp) to target basis + funding carry, priced on a full round-trip cost model; plans are single-use and re-priced at execution.

### 3. Binance Agent OS & MCP Integration Bridge (`agents/agent_os_bridge.ts`)
- MCP client that connects to Deltr's MCP server and discovers the Binance MCP upstream (official hosted endpoint, then the local testnet-backed shim, then none) and reports which one it reached; exposes a JSON-RPC 2.0 facade for order routing through Deltr's gate. The official endpoint was never exercised from the build machine: its default resolver refuses the hostname and the endpoint needs OAuth through a supported host. Nothing claims otherwise.

### 4. Zero-LLM Deterministic Risk Gate (`risk_gate.py`)
- Deterministic, stdlib-only Python gate with 19 ordered checks (measured median 1.5 to 2.5 µs across runs, quoted at startup) enforcing hard safety bounds:
  - Max leverage: 3x limit.
  - Max risk per trade: 2% of total capital.
  - Automatic stop-loss at 3% max drawdown.

---

## 📋 Required Submission Package Checklist

- [ ] Follow `@Binance` & repost hackathon announcement on X.
- [ ] Public GitHub repository (`mrnetwork0001/Deltr`, make public before submitting).
- [ ] 3-Minute Video Presentation & Demo walkthrough (docs/DEMO_SCRIPT.md).
- [ ] Submission quote repost on X with GitHub link & video.
- [ ] Complete Binance survey entry form.

---

## 📄 License
Apache 2.0 Open Source
