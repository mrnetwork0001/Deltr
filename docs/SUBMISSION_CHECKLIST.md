# Deltr - Submission Checklist (Binance Agent OS Mini Hackathon)

Source of truth: the official announcement post by @Binance on X (September 1, 2026, 15:30 UTC):
<https://x.com/binance/status/2094810011557838988>

| Item | Detail |
|---|---|
| Prize pool | $60,000 USDC in total |
| Track A (judged) | *Build an AI agent with Agent OS* - 20K USDC, judged. **This is Deltr's submission.** |
| "Connect your MCPs and trade" | **Not a judged prize track.** A first-come task reward: 4 USDC each to the first 10,000 users who complete a spot trade, a futures trade and a margin-or-convert trade through the Binance MCP server (40K USDC distributed this way). Do not claim it as a 40K prize anywhere |
| Deadline | **September 8, 2026, 23:59 UTC** |
| Eligibility | Not available in the US, UK, EEA, Hong Kong, Singapore, or jurisdictions on Binance's prohibited list |

## Entry steps (verbatim from the announcement)

1. Follow **@Binance** and **repost** the announcement post.
2. **Reply or quote-repost** the announcement with your submission.
   Track A: include **video/demo + GitHub** link ("if applicable"). Lead with the strategy, not the risk gate.
3. **Complete the survey:** <https://app.binance.com/uni-qr/user-survey/2913aa200aac462c89a737779393f3d4> (requires a Binance login).

## Pre-flight (repository)

- [ ] **Make `mrnetwork0001/Deltr` public** on GitHub (it is private as of 2026-09-02). Judges cannot open a private repo.
- [ ] `LICENSE` = Apache 2.0 (present). Add the `license` badge/line to `README.md`.
- [ ] `python main.py` boots the whole stack with **zero secrets** (paper mode on live Binance + PancakeSwap data).
- [ ] `pytest -q` is green; `pytest -q -s tests/test_risk_gate.py` (or the `python main.py` banner) prints the measured gate median (quote that number, never the target).
- [ ] `npm run typecheck` is green; `npm run bridge:list` prints Deltr's MCP tools.
- [ ] `README.md` has: 60-second quickstart, architecture diagram, MCP connection snippets (Claude Desktop / Claude Code / Cursor / Codex), Agent OS Skills-Hub install command, safety model, demo video link, and states exactly which Binance MCP upstream was exercised (official / shim).
- [ ] `ui/out` (static export: landing page at `/`, dashboard at `/app/`) is committed so `python main.py` needs Python only.
- [ ] Fresh-clone acceptance: clone to a new directory, `pip install -r requirements.txt`, `python main.py --once --json` prints real DEX/perp/funding/edge/plan/gate.
- [ ] `skills/deltr-binance/SKILL.md` frontmatter matches the Skills Hub format (`name`, `description`, `metadata.version`, `metadata.author`, `license`).
- [ ] `.env.example` documents `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, `BINANCE_API_ENV` (same names as `binance-cli`).

## Demo video (Track A)

- [ ] ≤ 3 minutes, screen recording + voice-over, following `docs/DEMO_SCRIPT.md`.
- [ ] Show: natural-language prompt → MCP tool calls → risk gate verdicts (one veto, one approval) → paired DEX/CEX fills → live delta ≈ 0 and funding accrual (testnet-derived, indicative) → Claude/Codex connected to Deltr's MCP server → Deltr's bridge reporting its upstream truthfully (official / shim / none; it reports `shim` from this build machine, and the official endpoint has never been exercised here).
- [ ] Upload (YouTube unlisted or X native video) and paste the link into `README.md` and the X submission.

## The X submission post (Track A only)

Template (quote-repost of the announcement). It leads with the strategy: a scan of the public
hackathon repos on 2026-09-02 found no other entry running a two-venue delta-neutral basis and
funding trade, while at least fifteen shipped a policy or risk layer. Do **not** mention a 40K
"Track B" prize; that item is a 4 USDC task reward and claiming it burns the scarcest real estate
in the submission.

> ⚡ **Deltr** - a CEX / DEX delta-neutral basis and funding agent. Long PancakeSwap V3 on BNB Chain, short the Binance USDⓈ-M perp, delta-neutral by construction. Paper mode, testnet perp, zero secrets.
> Every order clears a deterministic zero-LLM gate before it exists. On live data right now it **declines to trade** - net edge about -14 bps - and shows you the arithmetic.
> 🎥 \<video\> · 💻 github.com/mrnetwork0001/Deltr · 🌐 mrnetwork0001.github.io/Deltr
> Track A - Trading + Onchain + Data & Analysis workflows. @binance

- [ ] Follow @Binance ✔ · Repost ✔ · Quote-repost with links ✔ · Survey ✔
- [ ] The post makes no claim about a judged "Track B" prize.

## The separate task reward (optional errand, not part of the judged entry)

- [ ] In a supported host (ChatGPT, Claude, Claude Code, Codex, Cursor, VS Code), OAuth to
      `https://agent.binance.com/mcp/agentic` and complete one spot, one futures and one
      margin-or-convert trade on your own Agentic sub-account. 4 USDC to the first 10,000 users.
- [ ] Only from a network and jurisdiction where you are legitimately eligible. Do not attempt to
      route around a geoblock.

## After submitting

- [ ] Tag the release: `git tag -a v1.0.0-hackathon -m "Binance Agent OS Mini Hackathon submission"` and push tags.
- [ ] Keep `main` frozen until results; hotfix on a branch.
