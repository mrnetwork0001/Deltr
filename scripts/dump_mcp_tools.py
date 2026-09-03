#!/usr/bin/env python
"""scripts/dump_mcp_tools.py — render ``skills/deltr-binance/references/tools.md`` from the REAL
``tools/list`` of the Deltr MCP server (``deltr/mcp/server.py``), so the skill reference can never
drift from what an MCP client sees.  No network: the engine is built but never started.

    .venv/bin/python scripts/dump_mcp_tools.py            # print the markdown
    .venv/bin/python scripts/dump_mcp_tools.py --write    # rewrite references/tools.md
    .venv/bin/python scripts/dump_mcp_tools.py --check    # exit 1 if references/tools.md is stale

``tests/test_skill_manifest.py`` runs the ``--check`` form.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "skills" / "deltr-binance" / "references" / "tools.md"

# Hand-maintained, per tool: (one-line purpose, "Trades?" column).  Names not listed here still
# render (with the server docstring's first sentence), so a new tool can never be silently omitted.
PURPOSE: dict[str, tuple[str, str]] = {
    "deltr_status": ("mode, venues (with feed ages), equity, drawdown state, kill/halt, measured gate latency", "no"),
    "deltr_market": ("current DEX / perp / funding state and the edge breakdown for a symbol", "no"),
    "deltr_scan": ("the latest opportunity (horizon-based net edge) + spread history", "no"),
    "deltr_explain_edge": ("dry sizing and the full cost/carry math for a capital + leverage (+ horizon); no side effects", "no"),
    "deltr_propose_hedge": ("**phase 1**: size a plan, run the gate pre-check, get a `plan_id` (nothing executes)", "no"),
    "deltr_evaluate_risk": ("dry-run the gate (e.g. leverage 10 -> `LEVERAGE` veto with observed/limit); nothing is clamped", "no"),
    "deltr_execute_hedge": ("**phase 2**: execute a `plan_id` (re-priced, re-gated, single-use; TESTNET needs `confirm: true`)", "**yes**"),
    "deltr_unwind": ("reduce-only unwind of a position or `\"all\"` (TESTNET needs `confirm: true`)", "**yes**"),
    "deltr_positions": ("open positions + portfolio (mark-to-close PnL split, stop distance)", "no"),
    "deltr_risk_log": ("recent gate decisions, newest first, with every check's observed/limit", "no"),
    "deltr_receipt": ("one receipt by id (trace steps, fills with provenance, SHA-256)", "no"),
    "deltr_activity": ("the inbound MCP call log (which client called what, redacted args, latency)", "no"),
    "deltr_prompt": ("natural language -> typed intent -> plan -> pre-check (propose-only; execute needs the `plan_id`)", "no"),
    "deltr_kill_switch": ("operator kill switch on/off (only verified reduce-only unwinds pass while on)", "no"),
    "deltr_reset_halt": ("clear a sticky drawdown halt once the book is flat and drawdown < 3 %", "no"),
    "deltr_set_min_edge": ("runtime minimum net edge (PAPER may go negative, labelled on screen; TESTNET >= floor)", "no"),
    "deltr_stress": ("labelled SIMULATED scenarios on the paper book only; `kind: \"reset\"` clears", "no (paper book only)"),
    "deltr_edge_report": ("the same decomposition rendered as a titled markdown report: waterfall, funding, breakeven verdict, source tags and feed ages, honesty labels", "no"),
}


def _schema_type(spec: dict[str, Any]) -> str:
    if "type" in spec:
        return str(spec["type"])
    if "enum" in spec:
        return "enum"
    for alt in spec.get("anyOf", []):
        if alt.get("type") not in (None, "null"):
            return str(alt["type"])
    return "any"


def _args(schema: dict[str, Any]) -> str:
    props: dict[str, Any] = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    parts = []
    for name, spec in props.items():
        piece = f"`{name}`" + ("\\*" if name in required else "")
        piece += f" {_schema_type(spec)}"
        if "enum" in spec:
            piece += " (" + "\\|".join(str(x) for x in spec["enum"]) + ")"
        if spec.get("default") not in (None, ""):
            piece += f"={spec['default']!r}".replace("'", '"')
        parts.append(piece)
    return ", ".join(parts) if parts else "—"


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    for stop in (". ", ".  "):
        if stop in text:
            return text.split(stop, 1)[0] + "."
    return text


async def _collect(mcp: Any) -> tuple[list[Any], list[Any]]:
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
    return list(tools), list(resources)


def render() -> str:
    from deltr.config import Settings
    from deltr.engine import build_engine
    from deltr.mcp.server import ERROR_CODES, TOOL_NAMES, build_mcp

    def refuse(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"dump_mcp_tools never touches the network: {request.method} {request.url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=1.0)
    settings = Settings(_env_file=None, DELTR_MODE="paper", DELTR_STATE_DIR=tempfile.mkdtemp(prefix="deltr-tools-"))  # type: ignore[call-arg]
    engine = build_engine(settings, http=http)
    mcp = build_mcp(engine, engine.activity)
    tools, resources = asyncio.run(_collect(mcp))

    by_name = {t.name: t for t in tools}
    ordered = [by_name[n] for n in TOOL_NAMES if n in by_name] + [t for t in tools if t.name not in TOOL_NAMES]

    lines = [
        f"# Deltr MCP tools ({len(ordered)})",
        "",
        "Generated from the server's `tools/list` by `scripts/dump_mcp_tools.py` (checked by",
        "`tests/test_skill_manifest.py`; regenerate with `--write`). Server name `deltr`; streamable HTTP at",
        "`http://127.0.0.1:8000/mcp` (or stdio via `python main.py --mcp`). `\\*` marks a required argument.",
        "",
        "Every tool returns JSON. Errors come back inside the result as `{\"error\": {\"code\", \"message\"}}`,",
        "never as a transport failure. A VETO is deterministic and **final for the same inputs**: change",
        "capital or leverage, or ask `deltr_explain_edge`, instead of retrying.",
        "",
        "| Tool | Arguments | Use it for | Trades? |",
        "|---|---|---|---|",
    ]
    for t in ordered:
        purpose, trades = PURPOSE.get(t.name, (_first_sentence(t.description or ""), "see description"))
        lines.append(f"| `{t.name}` | {_args(t.inputSchema or {})} | {purpose} | {trades} |")
    lines += [
        "",
        "## Two-phase execution",
        "",
        "1. `deltr_propose_hedge(capital_usd, leverage)` (or `deltr_prompt(text)`) returns a `plan_id`, the plan, the",
        "   flat gate input and the pre-check decision. Nothing executes. Plans expire after 60 s and are single-use.",
        "2. `deltr_execute_hedge(plan_id, confirm)` re-prices the plan against live quotes, runs the full gate again",
        "   (`PRICE_DRIFT` 20 bps, `NEGATIVE_EDGE` at the re-quoted price) and only then places both legs, reversing",
        "   the first if the second fails. In TESTNET `confirm: true` is mandatory; without it the plan is kept and the",
        "   result is `CONFIRM_REQUIRED`.",
        "",
        "There is no tool that changes the mode, touches production hosts or withdraws funds.",
        "",
        "## Resources (read-only JSON)",
        "",
        "| URI | Contents |",
        "|---|---|",
    ]
    res_desc = {
        "deltr://status": "the same `SystemStatus` document as `deltr_status`",
        "deltr://risk-limits": "hard limits, `CHECK_ORDER` (19 codes) and the measured gate latency",
        "deltr://config": "`settings.redacted()` — never a key value, only `secrets_present`",
    }
    for r in resources:
        uri = str(r.uri)
        lines.append(f"| `{uri}` | {res_desc.get(uri, r.description or r.name)} |")
    lines += [
        "",
        "## Error codes",
        "",
        ", ".join(f"`{c}`" for c in sorted(ERROR_CODES)) + ".",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    text = render()
    if "--write" in argv:
        TARGET.write_text(text, encoding="utf-8")
        print(f"wrote {TARGET.relative_to(ROOT)}", file=sys.stderr)
        return 0
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET.relative_to(ROOT)} is stale: run scripts/dump_mcp_tools.py --write", file=sys.stderr)
            return 1
        print("references/tools.md is up to date", file=sys.stderr)
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
