# Deltr — Win Plan (Binance Agent OS Mini Hackathon, Track A)

**Written:** 2026-09-03 · **Deadline:** 2026-09-08 23:59 UTC · **Working days left:** 5 (Sept 8 is buffer, not a work day)
**Author:** lead engineer, after three scout reports and three judge reviews.

---

## 1. Verdict

**Will this win Track A? Not as it stands — and the reason has nothing to do with the code.** `github.com/mrnetwork0001/Deltr` returns 404 (confirmed again this morning: `gh repo view` reports `"visibility":"PRIVATE"`, last push 2026-09-02T02:30Z). `git ls-files` returns 11 files against 154 stageable; `deltr/`, `agents/`, `tests/`, `docs/`, `skills/`, `ui/` and `scripts/` are all untracked. There is no video. All three judges scored submission-completeness at 1, 2 and 3 out of 10 and all three said the same thing in different words: today the correct judged score is zero, because there is nothing to open. The 404 also silently breaks four other surfaces — `README.md:61`, the `npx skills add` line at `README.md:209`, the landing page's GitHub button, and the `git clone` inside `skills/deltr-binance/SKILL.md`'s openclaw install block. Everything below this paragraph is worthless until `git push` and the public flip are done.

**Conditional on shipping, the picture is genuinely strong but not a lock.** The judges' predictions: the Binance PM said push + video alone gets top 5 with a 20–25% shot at first, and push + video + one verifiable Agent OS touch gets top 2 at 45–55%. The hackathon veteran was the most bullish — 1st or 2nd if the repo and video land by Sept 6 — but discounted to a realistic 3rd–4th purely on observed shipping risk (three source files edited *during* the audit window). The quant was the most bearish on the top spot: ~60% it places, under 25% it wins outright, top 3 of an effective field of 12–18 substantive projects. **Where they disagreed matters more than where they agreed.** The PM's binding constraint is authenticity — Track A is literally named "build an agent *with Agent OS*", and Deltr's only contact is a bridge that prints `official upstream unavailable → shim` while three rivals (likeMdl, charter, Sentinel-One) show live OAuth against `agent.binance.com`; he scored that criterion 4/10 and called it the difference between top-5 and first. The quant's binding constraint is different: even after shipping, the only watchable demo is a simulated fill on a recorded file with the profit floor manually dropped to −50 bps, which a skeptical judge reads as a dashboard over nothing — a risk that *survives* the push. The vet's binding constraint is neither: it is that the operator keeps polishing instead of shipping. They also split on Onchain coverage (PM and quant say partial, vet says strong — the vet is right, hand-encoded `slot0` + QuoterV2 with gas priced into the round trip is more on-chain rigour than anyone else in the field) and on the single highest-ROI addition (PM: make Agent OS load-bearing; quant: surface the breakeven horizon; vet: just record the video). **My adjudication: the vet is right about sequencing, the PM is right about the ceiling, the quant is right about what the video must say.** Ship first, then spend the remaining days on exactly two things — Agent OS authenticity and the breakeven-horizon reframe — because those are the only two that move a judge who has already decided the engineering is good. And one high-value discovery the judges did not have: `deltr/mcp/binance_shim_server.py:93` already ships `resolve_tool_names()`, which renames the shim's 9 tools from a fixture at `tests/fixtures/binance_mcp_tools.json` that `scripts/probe_binance_mcp.py` is built to write. The fixture is simply absent. The "half-day rewrite" all three judges recommended is a one-file drop plus a hint tweak — call it 90 minutes. That single fact is why I think the 45–55% branch is reachable inside this week.

---

## 2. Blocking to submit

Nothing in section 3 counts unless every row here is green. Ordered by execution sequence.

| # | Item | Owner | Est. | Exact command / link |
|---|---|---|---|---|
| B1 | Commit everything on a clean tree | agent | 15 min | `git add -A && git status --short \| wc -l` (expect ~154) then `git commit -m "feat: Deltr v1 — CEX/DEX delta-neutral basis agent, MCP server, risk gate, UI"` |
| B2 | Verify no secrets and no runtime state got staged | agent | 10 min | `git ls-files \| grep -E '\.env$\|state/\|\.key$\|\.pem$'` must return **nothing**; `git ls-files ui/out \| wc -l` must return **25** (confirmed `ui/out/` is un-ignored by the `!ui/out/` rule) |
| B3 | Push to `origin/main` | agent | 2 min | `git push origin main` |
| B4 | **Flip the repo public** | user | 2 min | `gh repo edit mrnetwork0001/Deltr --visibility public --accept-visibility-change-consequences` — then verify: `curl -s -o /dev/null -w '%{http_code}\n' https://github.com/mrnetwork0001/Deltr` must be **200** |
| B5 | Verify the four surfaces the 404 was breaking | agent | 10 min | `README.md:61` clone line, `README.md:209` `npx skills add`, landing-page GitHub button, and the `git clone` inside `skills/deltr-binance/SKILL.md` openclaw block. Run `npx skills add https://github.com/mrnetwork0001/Deltr` once to confirm it resolves `skills/deltr-binance/` |
| B6 | Freeze `main`, work on a branch | agent | 2 min | `git tag -a v1.0.0-hackathon -m "Binance Agent OS Mini Hackathon submission" && git push --tags && git checkout -b polish` |
| B7 | Publish the landing page to a clickable URL | agent | 15 min | GitHub Pages from `/docs` won't work (that's the docs dir). Use a `gh-pages` branch: `git subtree push --prefix ui/out origin gh-pages` then `gh api -X POST repos/mrnetwork0001/Deltr/pages -f 'source[branch]=gh-pages' -f 'source[path]=/'`. Verify 200 at `https://mrnetwork0001.github.io/Deltr/` |
| B8 | Consistency fixes — **must precede the camera** | agent | 45 min | Pick one min-edge value: `README.md:74`, `README.md:181` say `-50`; `ui/out/index.html` and `docs/DEMO_SCRIPT.md:14` say `-20`. **Standardise on `-20`.** Change `README.md:16`, `:42`, `:264` from `1.5 µs` to `1.6–2.5 µs measured; the banner prints your machine's median`. Change both `<title>` tags off the old yield-flavoured product name and onto `Deltr - CEX / DEX delta-neutral arbitrage agent`. Point `README.md:62` install at `requirements.lock`. Suppress the bridge's stray `[bridge] upstream status not reported to Deltr (404)` line |
| B9 | Record the demo video (2:30–3:00, screen + voice) | user | 2–3 h | Script already exists at `docs/DEMO_SCRIPT.md`. Shot order in §5, Sept 5. Upload unlisted YouTube **and** keep the file for X native upload |
| B10 | Rewrite the X post — **Track A only** | agent drafts, user posts | 20 min | `docs/SUBMISSION_CHECKLIST.md:10,40,46` currently claims Track B as a judged 40K prize. It is not. Delete that line. New draft in §5, Sept 5 |
| B11 | Follow @Binance + repost the announcement | user | 2 min | https://x.com/binance/status/2094810011557838988 |
| B12 | Quote-repost with video + repo + live URL | user | 5 min | Same URL. Must contain: the video, `https://github.com/mrnetwork0001/Deltr`, and the Pages URL from B7 |
| B13 | Complete the survey | user | 5 min | https://app.binance.com/uni-qr/user-survey/2913aa200aac462c89a737779393f3d4 (needs a Binance login) |
| B14 | Fix `docs/SUBMISSION_CHECKLIST.md` Track B row | agent | 5 min | Line 10: Track B is *first 10,000 users get 4 USDC each for completing a spot + futures + margin/convert trade* — a task reward, not a judged track |
| B15 | Track B task (separate errand, real 4 USDC) | user | 30–60 min | OAuth `https://agent.binance.com/mcp/agentic` in a supported host (ChatGPT, Claude, Claude Code, Codex, Cursor, VS Code — setup at https://developers.binance.com/en/docs/agent-native/mcp-server), then one spot + one futures + one margin-or-convert trade. **Only from a network and jurisdiction where you are legitimately eligible** — US/UK/EEA/HK/SG are excluded. Do not attempt to route around a geoblock |

**Eligibility check before spending another day (user, 10 min):** the build machine gets `getaddrinfo ENOTFOUND agent.binance.com` and `dev.binance.vision` returns HTTP 451. If the operator's actual jurisdiction is on the excluded list, the prize cannot be paid and the correct plan is different. Confirm this first.

---

## 3. Highest-ROI additions

Ranked by (judge impact ÷ hours × probability of landing). Every item gets a verdict.

### A1 — Official tool-name fixture: `tests/fixtures/binance_mcp_tools.json` — **BUILD**
- **What:** the published 50-tool official inventory (dotted names: `spot.ticker24hr`, `spot.depth`, `futures_usds.premiumIndexKlineData`, `futures_usds.markPriceKlineCandlestickData`, `futures_usds.accountInformationV3`, `futures_usds.positionInformationV2`, `futures_usds.exchangeInformation`, …) written as the fixture that `resolve_tool_names()` at `deltr/mcp/binance_shim_server.py:93` already reads. The shim's 9 tools then *serve under the real Binance names*, and the bridge attempts those names first.
- **Category:** Trading (and it is the Track A criterion itself).
- **Why a judge cares:** this is the PM's 4/10 criterion — "what part of this needed Agent OS at all?" The answer changes from "the skill file" to "our shim is name-compatible with the official 50-tool surface, and here is the bridge attempting `futures_usds.premiumIndexKlineData` live." Verifiable on camera without ever reaching `agent.binance.com`. The PM said this alone moves his first criterion from 4 to 7–8.
- **Cost: 1.5 h.** The machinery already exists — this is the single biggest mispricing in all six reports, which assumed a half-day rewrite. Required tweaks to `_FIXTURE_HINTS` (lines 68–78): remove the bare `"order"` from `place_futures_order` (it would wrongly alias a write tool onto read-only `futures_usds.queryOrder`), add `"position"` to `get_positions` (the official name is `positionInformationV2`, which matches neither existing hint), and relax the `futures_ok` guard at line 114 so `spot.depth` can map to `get_order_book`. Add one test asserting the mapping.
- **Risk: low.** `resolve_tool_names()` is written never to raise and falls back to default names on a bad fixture. Worst case is a partial mapping, which is honest.
- **Honesty condition (non-negotiable):** the fixture must carry a provenance header — these names were **transcribed from a third-party published inventory** (`likeMdl/binance-ai-risk-trader/docs/binance-agent-os-tools.md`, dated 2026-09-02), **not captured from Deltr's own OAuth session**. Say that in the fixture, in the README and on camera. Without it this becomes the one claim that could sink the honesty pitch.

### A2 — Breakeven holding period + edge-vs-horizon curve — **BUILD**
- **What:** promote `edge.breakeven_settlements()` (`deltr/edge.py:320`, unit-tested at `tests/test_edge_math.py:168`, **called by nothing**) into a first-class output of `deltr_explain_edge`, the `--once --json` payload, and the dashboard.
- **Category:** Data & Analysis (converts it from partial to real) and Trading.
- **Why a judge cares:** it kills the quant's main objection — the 72 h default horizon is arbitrary and is the *only* reason the headline edge is negative. More importantly it retires the `--min-edge-bps` override, which is the single artifact that makes the demo look staged. The line changes from "I refuse" to **"at today's funding this pays for itself in 4.9 days; my horizon is 3, so no."** No competitor can copy that, because none of them has a carry strategy to compute it from.
- **Cost: 2–3 h** (the function and its test exist; this is plumbing plus one dashboard panel).
- **Risk: low**, additive. One trap: if the curve is drawn at mainnet-typical funding (0.01%/8h) rather than the ~0 testnet rate Deltr actually reads, every such number must be labelled an **assumption**, not a measurement.

### A3 — GitHub Pages "judge console" URL — **BUILD**
- **What:** the existing `ui/out` static export served at a public URL, linked from the X post and the README's first line.
- **Category:** presentation, all four.
- **Why a judge cares:** `0xCaptain888/binance-agentguard`'s live Judge Console is, per the scouts, the smartest presentation move in the field. Deltr's landing page (9 anchored sections, SVG cost waterfall, the full 19-row check table with an "input owner" column) beats it on substance and is currently reachable by nobody. A judge with five minutes clicks one link.
- **Cost: 15 min.** **Risk: none.** (Covered as B7 because it is that cheap.)

### A4 — `binance-cli --env testnet` demonstration (not a rewrite) — **BUILD**
- **What:** a `scripts/agentos_cli_check.sh` that runs the official `binance-cli` against `--env testnet` for `futures-usds basis` and `get-funding-rate-history` and prints its output **side by side with Deltr's own numbers for the same instant**. 15 seconds of screen time.
- **Category:** Trading / Agent OS authenticity.
- **Why a judge cares:** `binance-cli` is an official Agent OS surface that works while `agent.binance.com` is DNS-blocked, and it exposes exactly Deltr's two strategy inputs. It converts "built on Agent OS" from a claim into a screenshot. It also cross-validates Deltr's basis and funding math against Binance's own tool — a credibility beat nobody else has.
- **Cost: 1.5–2 h** including install (it is not on this machine: `which binance-cli` → not found).
- **Risk: medium-low.** Some endpoints need keys; the two Deltr needs are public. If install fails, abort at 2 h and drop the shot — nothing else depends on it.
- **Explicitly SKIP the rewrite:** do **not** re-route the engine's perp reads through the CLI. That is a subprocess dependency in the hot path, it breaks the "boots with Python only" claim, and it risks the 431-test suite four days before a deadline.

### A5 — x402 / B402 payment leg on BSC testnet, **seller side first** — **BUILD (hard 5 h timebox, Sept 6, abort clean)**
- **What:** put `deltr_explain_edge` behind an HTTP 402. Deltr returns a proper `paymentRequirements` challenge; on settle it returns the gate's existing sha256 receipt as the paid artifact. **Seller side only in the base scope** — that half is entirely within Deltr's control and needs no Binance credentials, no funded wallet and no facilitator round-trip to demonstrate the 402 → signed payload → verified → artifact shape. Buyer side (`baw x402-payment preview` → EIP-712 sign → facilitator `/verify` + `/settle`) is a stretch goal only if the first three hours went clean.
- **Category:** **Payment — the one Deltr scores 0 on, and the one the entire field scores 0 on.** Grep for `x402|b402|EIP-712|facilitator` across the tree returns nothing today; the only two payment-named rival repos are 0-byte shells with no README.
- **Why a judge cares:** with no published rubric, breadth across Binance's own four advertised cards is one of the very few legible proxies available. This makes Deltr **the only entry in the hackathon touching all four** — a sentence literally nobody else can write. All three judges independently named it.
- **Cost: 4–6 h** for seller-side; buyer-side adds 2–3 h and depends on `baw` (not installed: `which baw` → not found) plus a funded BSC testnet wallet.
- **Risk: medium — this is the one item that can eat a day.** Mitigation is the seller-first split: the guaranteed-deliverable half ships in 3 h, and the buyer half is a bonus. Hard rule: if it is not demonstrable by 17:00 on Sept 6, `git stash` it and ship without. It is ranked below A1/A2 for exactly this reason.
- **Honesty condition:** label it BSC **testnet** everywhere. B402 mainnet requires an application; do not apply, do not imply.

### A6 — Twentieth risk check: `LIQ_BUFFER` / margin solvency — **BUILD (conditional on Sept 3–4 landing clean)**
- **What:** a check that vetoes when the perp's mark-to-liquidation distance is inside a configured buffer, plus a `stop_breached()` branch that unwinds on margin distance and not only on P&L. `liq_price_est` is already computed and stored on every `Position` (`deltr/portfolio.py:194,243`, `deltr/models.py:460`) and **used by nothing**.
- **Category:** Trading.
- **Why a judge cares:** it is the one substantive hole a quant finds in ten minutes. The book is long spot on-chain and short an isolated perp at up to 3x — a +33% BNB move liquidates the short while the hedge collateral sits in a BSC wallet that cannot post margin, leaving you naked long at the worst moment. All 19 existing checks are pre-trade sizing/limit checks; for a delta-neutral book, margin solvency *is* the risk. It also gives the video a beat no rival can match: a risk gate that understands the strategy it guards rather than a generic policy filter.
- **Cost: 2–3 h. Risk: medium** — it touches `risk_gate.py`, which is frozen, ordered and heavily tested. Additive-only, new check appended as #20, no renumbering. Abort at 3 h if the suite goes red.

### A7 — Data & Analysis deliverable: a rendered `explain_edge` report — **BUILD**
- **What:** wrap the existing edge decomposition into a titled, readable artifact (markdown or HTML) with the round-trip waterfall, funding over 9 settlements, breakeven horizon from A2, and every `DataSource` tag and feed age. One new MCP tool, zero new analysis.
- **Category:** Data & Analysis — moves it from "internal plumbing" to "an output a judge can read and quote."
- **Why a judge cares:** the card's own examples are Reports / Market Analysis / Portfolio Insights, and Deltr currently produces none of those as a *deliverable*. It also becomes the paid artifact behind A5's 402.
- **Cost: 2 h. Risk: low.** Do **not** turn this into a market-intelligence agent — six rivals are attacking that category head-on and Deltr would lose on their turf.

### A8 — Live OAuth session against `agent.binance.com`, screen-recorded — **BUILD (user, only if legitimately reachable)**
- **What:** complete OAuth in Claude Code (an officially listed client) from a network and jurisdiction where the operator is genuinely eligible, run one market-data call, record 20 seconds. Then run `scripts/probe_binance_mcp.py --out tests/fixtures/binance_mcp_tools.json` — which replaces A1's third-party fixture with a **first-party capture**, upgrading the provenance line from "transcribed" to "captured from our own session."
- **Category:** the Track A criterion itself.
- **Why a judge cares:** it erases weakness #1 outright and neutralises the three rivals who can show OAuth. The PM called it the highest-value half-hour available anywhere in this project.
- **Cost: 30–60 min. Risk: low technically, but gated on eligibility** — if the operator is in an excluded jurisdiction this is both impossible and irrelevant, since the prize could not be paid. **Do not circumvent a geoblock.** If it cannot be done legitimately, A1's fixture with its honest provenance is the correct and defensible fallback, and Deltr's three-state bridge reporting stays a deliberate feature rather than an apology.
- Free rider: this is the same OAuth session as B15, so it costs nothing extra once Track B is being done.

### A9 — Skills Hub PR + skill polish — **BUILD (Sept 7, low priority)**
- **What:** add `skills/deltr-binance/README.md` documenting `scripts/deltr.sh` (CONTRIBUTING explicitly requires this for any skill shipping a script — it is a named, currently-open gap); rewrite the SKILL.md `description` in the house style (enumerated trigger phrases + an explicit `Do NOT use for` clause, the way `p2p` and `payment` do); open the PR with every template section filled.
- **Why a judge cares:** free, visible to Binance staff, and signals the author read the repo. Frame the skill around what Binance does **not** ship — delta-neutral sizing — never around futures data, which is a textbook duplicate of the flagship skill and has been closed for exactly that reason (PR #263, #230).
- **Cost: 1 h. Risk: none.**
- **Guardrail:** zero external PRs have ever been merged in that repo's history (78 merged, all three internal authors; 45 open, oldest six months). Word every claim as **"submitted a skill PR"** — never shipped, listed or published.

### A10 — Chinese-language README summary — **BUILD (Sept 7, if and only if everything above is green)**
- **What:** a 200-word 中文 section at the top of the README.
- **Why a judge cares:** six rivals in a competent Chinese-language cohort already have bilingual docs; Deltr does not. If Binance's judging includes that team it is a cheap tiebreaker.
- **Cost: 30–45 min. Risk: none.** Lowest priority item on this list; drop it without regret.

### A11 — Multi-symbol scanning so the edge is sometimes positive — **SKIP**
- **Why:** it will not work. ~10 bps of Binance taker fee dominates a ~3 bps basis on every liquid BNB-chain pair; adding symbols multiplies the scan surface without changing the arithmetic, and the honest outcome is five negative edges instead of one. It needs per-symbol Pancake pool maps, new market-data paths, config and tests — a full day minimum — and it is a *worse* answer to the same problem that A2 solves in two hours. The demo problem is not "the edge is negative", it is "the video has no way to explain why negative is the correct answer." A2 explains it.

### A12 — A real mainnet PancakeSwap swap to prove two-legged execution — **SKIP**
- **Why:** it requires a private key in a project whose headline claim is "zero secrets", five days before a deadline, with one bad transaction capable of destroying the honesty story that is Deltr's core asset. A BSC-testnet Pancake leg is no better — testnet V3 liquidity is negligible, so the "execution" would be less real than the current mainnet quote. Keep the current posture and **say it out loud**: the DEX leg is quoted live on mainnet with gas priced in, and is always simulated. Disclosed, that is a strength; discovered, it would be fatal.

---

## 4. Do not do

1. **~~Do not chase `agent.binance.com` from this laptop.~~ CORRECTED 2026-09-03 by direct measurement.** The premise was wrong. `agent.binance.com` resolves fine on a public resolver (`dig @1.1.1.1` returns `d3ees4ixkj4fsa.cloudfront.net`, 108.156.221.22) and a `POST /mcp/agentic` reaches it and returns **HTTP 401** — the documented "needs OAuth" challenge, not a block. `dev.binance.vision` now returns **200**, not the 451 the scouts saw. The only fault is the LAN router's resolver at `192.168.1.1` failing to resolve that one hostname; the machine's own DNS setting is the fix. This is ordinary network configuration, not circumvention, and the operator's jurisdiction is not on the excluded list. **A8 and B15 are therefore both achievable, which makes them the highest-value hour in this plan** (they erase weakness #1 and upgrade A1's fixture from a third-party transcription to a first-party capture).
2. **Do not re-route the engine through `binance-cli`.** Demonstrate it (A4); do not depend on it.
3. **Do not refactor `risk_gate.py`.** It is frozen, ordered and AST-enforced. The only permitted change is the additive check #20 (A6). No renumbering, no "while I'm in here."
4. **Do not scrub `0x172fcD41E0913e95784454622d1c3724f546f849`.** Two scouts flagged it as a possible EOA and recommended replacing all 311 occurrences. I checked: `deltr/venues/pancake_constants.py:75` shows it is the **PancakeSwap V3 WBNB/USDT 0.01% pool contract**, sitting in a `pools_wbnb_usdt` map beside three other fee-tier pools. The quant judge is right and the scouts are wrong. Binance's own `x402-payment.md` ships contract addresses. A find-and-replace across a 305-line replay fixture four days out is pure downside.
5. **Do not add tests to raise the count.** 431 already beats sentinel-os's 64 and eikarna's 14. New tests only where A1/A2/A6 introduce new behaviour.
6. **Do not wait on the Skills Hub PR.** It will not merge. Open it and move on.
7. **Do not claim Track B as a judged prize in the X post.** It is 4 USDC to the first 10,000 users. Claiming it burns the scarcest real estate in the submission on a task reward.
8. **Do not build a market-intelligence / reports agent.** Six rivals are already there; A7's report deliverable is the right-sized version.
9. **Do not spend more hours on competitor recon.** Every X proxy is dead (nitter offline, xcancel served a cease-and-desist, r.jina.ai 403, DDG CAPTCHA). The GitHub-derived picture is sufficient: ~55 repos, ~12–18 substantive, exactly one video in the field.
10. **Do not rebuild or redesign the landing page.** It is already the strongest asset in the project. It needs a URL, not edits.
11. **Do not apply for B402 mainnet.** Testnet is the correct and honest scope this week.
12. **Do not edit anything on `main` after B6.** Branch, then merge deliberately.

---

## 5. Day-by-day

### **Sept 3 — Ship. Nothing else.**
Target state at end of day: *a stranger can clone Deltr and click a live URL.*
- B1 → B7 in order: commit, verify no secrets, push, **flip public**, verify the four broken surfaces, tag `v1.0.0-hackathon`, publish Pages.
- B8 consistency pass: min-edge standardised to `-20`; the µs claim becomes a measured range; both `<title>` tags drop the old product name; README install points at `requirements.lock`; the bridge's stray 404 line suppressed.
- B14: fix the Track B row in `docs/SUBMISSION_CHECKLIST.md`.
- Add the two-sentence non-advice disclaimer (mirroring the Skills Hub's own language) to `README.md` and `SKILL.md`.
- **Invert the README's first screen and the landing-page hero.** Claim #1 becomes the strategy: *the only CEX ↔ DEX delta-neutral basis and funding agent in the field, with a real two-venue path.* The gate moves to the credibility paragraph. Fifteen rivals are selling a risk firewall; zero are selling a basis trade.
- Checkout `polish`. Freeze `main`.
- **User, in parallel:** confirm jurisdiction eligibility (10 min). This gates A8/B15 and, honestly, the whole week.

### **Sept 4 — The two things that change what the video says.**
- **A1** official tool-name fixture + `_FIXTURE_HINTS` tweaks + mapping test (1.5 h). Bridge attempts the real dotted names first and prints the attempt.
- **A2** breakeven horizon surfaced in `deltr_explain_edge`, `--once --json` and the dashboard (2–3 h).
- **A4** `binance-cli --env testnet` side-by-side check script (2 h, hard abort at 2 h).
- Full suite green, merge `polish` → `main`, push.
- Dry-run the demo end to end once with the screen recorder off. Confirm the override flag is now unnecessary.

### **Sept 5 — Record and submit.**
Shot order (≤ 3:00), narrated strategy-first:
1. **0:00** Startup banner — mode, hosts, `secrets: absent`, measured gate median, copy-paste MCP config. Best first-60-seconds artifact in the field.
2. **0:25** The hook: *"Everyone here built a guard for an agent that has no strategy. We built the strategy — long PancakeSwap V3 on BNB Chain, short the Binance perp, delta-neutral by construction."*
3. **0:45** The money shot — the EdgeWaterfall ending at −14 bps, `not actionable`. *"A naive bot sees +2.2 bps and trades. Deltr prices the full 16.6 bps round trip and declines."* Then the new line: **"and at today's funding it breaks even in 4.9 days — my horizon is 3, so the answer is no."** No override flag needed.
4. **1:20** Agent OS: the bridge attempting `futures_usds.premiumIndexKlineData` against the official endpoint, the truthful three-state result, and the `binance-cli --env testnet` cross-check agreeing with Deltr's basis figure. State the fixture's provenance out loud.
5. **1:50** `deltr_evaluate_risk(capital_usd=50000, leverage=10)` → the typed veto with `code / reason / observed / limit / unit`. Beats every prose BLOCK/MODIFY/APPROVE in the field.
6. **2:15** Two-venue architecture + the replay execution (clearly badged REPLAY) → paired fills → delta ≈ 0 → sha256 receipt.
7. **2:40** One sentence of disclaimer, the repo URL and the live Pages URL on screen.
- Upload unlisted YouTube; keep the file for native X upload.
- **B10–B13: submit today.** Draft post:

> ⚡ **Deltr** — the only CEX ↔ DEX delta-neutral basis + funding agent in this hackathon. Long PancakeSwap V3 on BNB Chain, short the Binance USDS-M perp, delta-neutral by construction. Paper mode, testnet perp, zero secrets.
> Every order clears a deterministic zero-LLM gate before it exists. On live data right now it **declines to trade** — net edge −14 bps — and shows you the arithmetic.
> 🎥 \<video\> · 💻 github.com/mrnetwork0001/Deltr · 🌐 mrnetwork0001.github.io/Deltr
> Track A — Trading + Onchain + Data & Analysis workflows. @binance

- Follow, repost, quote-repost, survey. **The entry is now complete with three days of margin.**

### **Sept 6 — Additions, on a branch, with a hard stop.**
- **A5** x402/B402 seller side (timebox 5 h; `git stash` and walk at 17:00 if not demonstrable).
- **A7** rendered `explain_edge` report — becomes A5's paid artifact.
- **A6** `LIQ_BUFFER` check #20, if and only if Sept 3–5 landed clean (abort at 3 h on any red).
- If A5 lands, post **one** follow-up reply in the same X thread: *"Update: Deltr now covers all four Track A workflow categories — added an x402/B402 payment leg on BSC testnet."* Plus a 30-second addendum clip. Do not re-record the main video.

### **Sept 7 — Errands and slack.**
- **B15 / A8**: the Track B task in a supported host (4 USDC, uncontested by every serious builder) — and, in the same session, `scripts/probe_binance_mcp.py --out tests/fixtures/binance_mcp_tools.json` to upgrade A1's fixture to a first-party capture, plus 20 seconds of screen recording. If that recording exists, it is worth one more thread reply.
- **A9** Skills Hub PR + skill README + description rewrite.
- **A10** Chinese summary, if everything else is green.
- Final read-through of README and landing page against §6.
- Merge `polish` → `main`, re-tag, push. **Freeze.**

### **Sept 8 — Buffer only. No code.**
Verify the repo is public and the clone works from a clean directory. Verify the video link resolves. Verify the X post is live and the survey is submitted. That is the whole day. If something above slipped, this is where it lands — which is exactly why nothing is scheduled here.

---

## 6. Honesty guardrails

These claims must stay true in the video, the README, the landing page, the X post and the Skills Hub PR. The honesty is not decoration — all three judges scored it 8–9 and it is the reason every *other* claim is believed. One overclaim discovered by a cross-checking judge discounts everything else.

1. **Agent OS contact.** Never say "connected to the official Binance MCP server" unless an OAuth session actually happened. Until then the truthful sentence is: *"the official endpoint has never been exercised from our build machine; our bridge reports official / shim / none truthfully and currently reports shim."* Keep the three-state output on screen.
2. **Tool-name provenance.** If A8 has not happened, the fixture's names are **transcribed from a third-party published inventory dated 2026-09-02, not captured from our own session.** That sentence goes in the fixture file, the README and the narration. The claim is "name-compatible with the published official surface" — never "verified against Binance."
3. **Skills Hub.** "Submitted a skill PR." Never shipped, listed, accepted or published. No external PR has ever merged there.
4. **No yield, no profit, no safety.** Delete the old yield-flavoured product name everywhere. Never call the strategy profitable, safe, guaranteed or recommended — Skills Hub CONTRIBUTING forbids exactly this, and it is the likeliest disqualifying objection for an agent that visibly proposes trades. Carry the non-advice disclaimer.
5. **Funding is testnet-derived and indicative.** Testnet funding is structurally ~0. Any breakeven or edge-vs-horizon figure drawn at mainnet-typical rates is an **assumption**, labelled as such on the same screen. Never present an assumed rate as a measurement.
6. **The DEX leg is quoted live and always simulated.** It has never sent a BNB Chain transaction, in any mode. Say it.
7. **The agent has never traded on its own signal on live data.** Every observed scan is −10 to −15 bps and declines. That is the finding, not the failure — but it must be stated as fact, not implied away.
8. **Replay and override badges stay visible.** If any override is used on camera, the amber chip stays on screen and the narrator says why. Stress scenarios keep the SIMULATED badge.
9. **Gate latency is a measured range.** 1.6–2.5 µs on the machines we measured; the banner prints the viewer's own median. Never a single flattering number in a headline.
10. **Test count is whatever the suite prints**, with the command and the date beside it. Note that a funding-accrual bug was caught by the calendar rolling past a hardcoded test date — if any date-coupled test goes red on a judge's machine, that is a bug, not a mystery, and the README should say the suite is deterministic and offline.
11. **x402/B402 is BSC testnet.** No mainnet claim, no implied application.
12. **Track B is a first-come 4 USDC task reward**, not a judged prize track — in the checklist, the README and the post.
13. **Paper mode by default, zero secrets, no production venue, no withdrawal code path.** These are true today. They must stay true; do not add a config flag this week that makes any of them conditional.

---

### One-line summary

Deltr is the best-engineered entry in a shallow field and the only one with a real strategy, and it is currently invisible. Push it today, spend two days making Agent OS visibly load-bearing and the negative edge visibly *reasoned*, record three minutes, and submit on Sept 5 with three days to spare. Everything else is optional.
