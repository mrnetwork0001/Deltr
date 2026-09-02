# ⚡ DELTR — Autonomous CEX ↔ DEX Cross-Venue Arbitrage & Delta-Neutral Yield OS on Binance

> **Binance Agent OS Mini Hackathon Master Blueprint ($60,000 USDC Prize Pool)**  
> **Host:** Binance (`@Binance`)  
> **Target:** 1st Place (Track B: $40,000 USDC Pool & Track A: $20,000 USDC Pool)  
> **Submission Deadline:** September 8, 2026 @ 23:59 UTC  
> **Primary Tracks:** Track B (Connect your MCPs and trade) & Track A (Build an AI agent with Agent OS)  
> **Core Tech Stack:** Binance Agent OS + Binance MCP Server Suite (`binance-mcp-server`) + BNB Chain PancakeSwap DEX + Binance Spot & Futures API + Python 3.11 + Next.js 14  
> **License:** Apache 2.0 Open Source  
> **Author:** Ifeanyichukwu Onwo (`mrnetwork`)  

---

## 📌 Executive Summary & Core Value Proposition

Most trading agents fail because they execute basic 1-pair spot orders without market hedging or trade with unconstrained model authority that risks large drawdowns during market volatility.

**DELTR** is an **Autonomous Cross-Venue Arbitrage & Delta-Neutral Yield OS** built natively on Binance Agent OS and the Model Context Protocol (MCP).

Deltr monitors price discrepancies between **BNB Chain DEXs (PancakeSwap)** and **Binance CEX (Spot/Futures API)**. When an arbitrage opportunity is detected, Deltr executes a **Delta-Neutral Hedge** (Long on PancakeSwap, Short on Binance Futures via MCP), earning yield from funding rates and price spreads with **zero directional market risk**. All trades pass through a **zero-LLM deterministic Python risk gate** before orders reach Binance.

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
   │          BINANCE AGENT OS CORE RUNTIME ENGINE          │
   │    (Dispatches Multi-Agent Dialectic & Strategy)       │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │         BINANCE MCP SERVER SUITE (binance-mcp)         │
   │  (Queries Orderbooks, Tickers, Account State via MCP)  │
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
   │     BINANCE SPOT/FUTURES & PANCAKESWAP EXECUTION       │
   │   (Executes Delta-Neutral Hedge & Streams P&L Data)    │
   └───────────────────────────┬────────────────────────────┘
```

---

## 🌟 4 Key Subsystems

### 1. CEX ↔ DEX Arbitrage Scout (`agents/arbitrage_scout.py`)
- Monitors real-time price spreads and funding rate arbitrage opportunities between Binance Spot/Futures and PancakeSwap V3 on BNB Chain.

### 2. Delta-Neutral Hedging Engine (`agents/hedger.py`)
- Formulates simultaneous paired orders (buying spot on DEX while shorting perpetual futures on Binance) to capture risk-free yield.

### 3. Binance Agent OS & MCP Integration Bridge (`agents/agent_os_bridge.ts`)
- Bridges Binance's Agent OS framework with model context protocol (MCP) tools for structured JSON-RPC order routing.

### 4. Zero-LLM Deterministic Risk Gate (`risk_gate.py`)
- 1.5 µs Python execution gate enforcing hard safety bounds:
  - Max leverage: 3x limit.
  - Max risk per trade: 2% of total capital.
  - Automatic stop-loss at 3% max drawdown.

---

## 📋 Required Submission Package Checklist

- [x] Follow `@Binance` & repost hackathon announcement on X.
- [x] Public GitHub repository (`mrnetwork/Deltr`).
- [x] 3-Minute Video Presentation & Demo walkthrough.
- [x] Submission quote repost on X with GitHub link & video.
- [x] Complete Binance survey entry form.

---

## 📄 License
Apache 2.0 Open Source
