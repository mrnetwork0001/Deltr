---
name: deltr-binance
description: Architecture, guidelines, Binance Agent OS, Binance MCP Server Suite, and risk gate rules for Deltr built for the Binance Agent OS Mini Hackathon.
---

# ⚡ Deltr — Binance Agent OS Mini Hackathon Skill & Execution Guide

Use this skill whenever working on, reviewing, or developing **Deltr** — the Autonomous CEX ↔ DEX Cross-Venue Arbitrage & Delta-Neutral Yield OS on Binance.

## 📌 Project Overview & Target
- **Target Event:** Binance Agent OS Mini Hackathon
- **Prize Target:** 1st Place (Track B: $40,000 USDC Pool & Track A: $20,000 USDC Pool)
- **Primary Tracks:** Track B (Connect your MCPs and trade) & Track A (Build an AI agent with Agent OS)
- **Binance Stack Integration:** Binance Agent OS + Binance MCP Server Suite (`binance-mcp-server`) + Binance API
- **Core Tech Stack:** TypeScript + Python 3.11 + Next.js 14 + Web3.js / Ethers.js

## 🏗️ Technical Architecture Rules

### 1. Binance Agent OS Core Integration (`agents/agent_os_bridge.ts`)
- Use Binance Agent OS as the primary agentic runtime for multi-agent reasoning and workflow orchestration.

### 2. Binance MCP Server Suite (`binance-mcp-server`)
- Connect MCP tools for orderbook inspection, ticker subscription, and structured JSON-RPC order routing.

### 3. CEX ↔ DEX Arbitrage & Delta-Neutral Hedging (`agents/hedger.py`)
- Execute paired orders: Long spot on PancakeSwap (BNB Chain) while Short perpetual futures on Binance CEX.

### 4. Zero-LLM Deterministic Risk Gate (`risk_gate.py`)
- Enforce max 3x leverage, max 2% portfolio capital risk, and mandatory 3% drawdown stop-loss before any order reaches Binance.

## 🚨 Submission Checklist
- Public GitHub repo under OSI-approved license (Apache 2.0 / MIT).
- Follow `@Binance` & repost hackathon announcement on X.
- 3-Minute Video Presentation & Demo walkthrough.
- Quote repost on X with GitHub link & video.
