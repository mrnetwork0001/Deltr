# ⚡ Deltr — Autonomous CEX ↔ DEX Cross-Venue Arbitrage & Delta-Neutral Yield OS on Binance

> Built for **Binance Agent OS Mini Hackathon** (`@Binance`) — $60,000 USDC Prize Pool  
> **Target:** 1st Place (Track B: $40,000 USDC Pool & Track A: $20,000 USDC Pool)  
> **Submission Deadline:** September 8, 2026 @ 23:59 UTC  
> **Core Tech Stack:** Binance Agent OS + Binance MCP Server Suite + PancakeSwap DEX + Python 3.11  
> **License:** Apache 2.0 Open Source  

---

## 📌 Overview

**Deltr** is an **Autonomous Cross-Venue Arbitrage & Delta-Neutral Yield OS** built natively on Binance Agent OS and Model Context Protocol (MCP) tools.

- **Binance Agent OS Integration (`agents/agent_os_bridge.ts`):** Uses Binance Agent OS for multi-agent reasoning and workflow orchestration.
- **Binance MCP Server Suite (`binance-mcp-server`):** Connects MCP tools for orderbook inspection and structured JSON-RPC order execution.
- **CEX ↔ DEX Arbitrage Engine (`agents/arbitrage_scout.py`):** Monitors price spreads between PancakeSwap (BNB Chain) and Binance CEX (Spot/Futures).
- **Delta-Neutral Hedger (`agents/hedger.py`):** Executes paired trades (DEX Long + CEX Short) to earn risk-free yield with zero market directional risk.
- **Hardened Risk Gate (`risk_gate.py`):** 1.5 µs zero-LLM deterministic Python risk controller enforcing max 3x leverage and 2% risk limits.

---

## 🚀 Quickstart & Setup Instructions

### 1. Prerequisites
- Node.js 18+
- Python 3.11+
- Binance API Key & Secret Key

### 2. Installation
```bash
git clone https://github.com/mrnetwork/Deltr.git
cd Deltr
npm install
pip install -r requirements.txt
```

### 3. Run Risk Gate & Binance Agent OS Bridge
```bash
python risk_gate.py
python main.py
```

---

## 📄 License
Apache 2.0 Open Source
