"""tests/test_integration_slice.py — the wave-1 definition of done.

ReplayHub engine: tick -> scan -> propose($5,000, 2x) -> gate APPROVED (test
settings use the PAPER min-edge override of -50 bps) -> execute -> snapshot has
exactly one position with |delta_base| <= 0.01 (one lot step), the receipt carries a
sha256, and prompts / MCP activity are populated.  Plus the two one-command paths
(``main.py --once --json --replay`` and ``main.py --mcp --replay`` over stdio).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Implementation

from deltr.mcp.activity import ActivityLog
from deltr.mcp.server import TOOL_NAMES, build_mcp
from deltr.models import Snapshot, TraceSource
from tests.conftest import REPLAY_FIXTURE, REPO_ROOT, free_port

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


async def test_replay_engine_tick_scan_propose_execute_snapshot(engine):
    # tick happened in start(); the scout scanned it
    ms, edge = engine.market()
    assert ms is not None and ms.dex is not None and ms.funding is not None and edge is not None
    assert engine.state.replay is True and engine.status().replay is True
    assert engine.state.gate_median_us > 0
    opp = engine.scan()
    assert opp.symbol == "BNBUSDT" and opp.is_actionable, opp.reason  # -50 bps override makes the replayed tick actionable
    assert engine.state.min_edge_bps == -50.0

    # propose $5,000 at 2x -> ~4.85 BNB, gate APPROVED in the pre-check
    plan, proposal, precheck = await engine.propose(5_000.0, 2.0, "BNBUSDT", TraceSource.CLI, client="pytest")
    assert 4.7 <= plan.qty <= 4.9 and abs(plan.notional_usd - plan.qty * plan.ref_dex_price) < 1e-6
    assert plan.cash_required_usd == pytest.approx(plan.notional_usd * 1.5, rel=1e-9)
    assert proposal.plan_id == plan.id and proposal.is_delta_neutral
    assert precheck.approved and precheck.code == "OK" and precheck.dry_run and len(precheck.checks) == 19
    assert plan.id in engine.state.plans

    # execute -> filled, position registered with the gate, plan consumed
    receipt = await engine.execute(plan.id, True, TraceSource.CLI, client="pytest")
    assert receipt.status == "filled", receipt.decision.reason
    assert SHA_RE.match(receipt.sha256) and receipt.position_id
    assert [s.step for s in receipt.steps] == ["plan", "scan", "gate", "dex_fill", "cex_fill", "position", "receipt"]
    assert receipt.legging_window_ms is not None and receipt.legging_window_ms >= 0
    assert plan.id not in engine.state.plans and receipt.position_id in engine.gate.positions
    assert engine.get_receipt(receipt.id) is receipt

    snap = engine.snapshot()
    assert isinstance(snap, Snapshot)
    open_positions = [p for p in snap.positions if p.status == "open"]
    assert len(open_positions) == 1 and snap.portfolio.open_positions == 1 and snap.status.open_positions == 1
    assert abs(open_positions[0].delta_base) <= 0.01
    assert open_positions[0].dex_qty == pytest.approx(open_positions[0].perp_qty)
    assert snap.receipts and snap.receipts[-1].id == receipt.id
    assert snap.portfolio.equity_usd == pytest.approx(10_000.0, abs=25.0)  # entry costs only
    assert any(e.topic == "fill" for e in snap.events)

    # the plan is single-use
    again = await engine.execute(plan.id, True, TraceSource.CLI)
    assert again.status == "expired" and again.decision.code == "PLAN_NOT_FOUND"

    # unwind everything -> flat, registry empty
    receipts = await engine.unwind("all", "test", True, TraceSource.CLI)
    assert len(receipts) == 1 and receipts[0].status == "unwound"
    assert engine.gate.positions == {} and engine.portfolio.positions("open") == []
    assert engine.status().open_positions == 0


async def test_prompt_is_propose_only_and_populates_prompts(engine):
    res = await engine.prompt("Rebalance $5,000 USDC into delta-neutral BNB arbitrage", TraceSource.UI, client="pytest")
    assert res.executes is False and res.plan_id and res.plan is not None and res.precheck is not None
    assert res.intent.action == "rebalance" and res.intent.capital_usd == 5000 and res.intent.stablecoin_note
    assert "USDC treated as USDT-equivalent" in res.message and res.plan_id in res.message
    assert [s.step for s in res.steps] == ["intent", "scan", "plan", "gate"]
    assert engine.portfolio.positions("open") == [], "prompts never execute"
    assert list(engine.state.prompts)[-1] is res
    # the plan it stored is executable through the normal path
    r = await engine.execute(res.plan_id, True, TraceSource.UI)
    assert r.status == "filled"
    await engine.unwind("all", "cleanup", True, TraceSource.UI)
    # non-trade intents never mutate anything
    for text, action in [("What is the edge right now?", "explain"), ("Scan for opportunities", "scan"), ("Status", "status"),
                         ("Kill switch on", "kill"), ("Unwind everything", "unwind"), ("Simulate a 150 bps basis shock", "stress")]:
        out = await engine.prompt(text, TraceSource.API)
        assert out.intent.action == action and out.plan_id is None and out.executes is False
    assert engine.gate.kill_switch is False and engine.state.stress_active is None


async def test_mcp_tools_over_the_real_engine_populate_activity(engine):
    mcp = build_mcp(engine, engine.activity)
    assert isinstance(engine.activity, ActivityLog)
    status = await mcp.call_tool("deltr_status", {})
    prop = await mcp.call_tool("deltr_propose_hedge", {"capital_usd": 5000, "leverage": 2})
    rows = engine.activity_log(10)
    assert [r.tool for r in rows] == ["deltr_propose_hedge", "deltr_status"] and all(r.ok for r in rows)
    assert len(engine.state.activity) == 2 and engine.snapshot().activity[-1].tool == "deltr_propose_hedge"
    assert rows[0].trace_id and rows[0].trace_id.startswith("plan_")
    assert status is not None and prop is not None


async def test_run_once_returns_plain_json(engine):
    out = await engine.run_once()
    assert out["ok"] is True and out["error"] is None and out["replay"] is True
    assert out["precheck"]["approved"] is True and out["plan"]["qty"] > 0 and out["edge"]["net_edge_bps"] < 0
    assert out["receipt"] is None, "run_once executes only with --auto"
    # the breakeven holding period + curve are first-class in the --once --json payload (A2)
    h = out["horizon"]
    assert h["verdict"] and h["curve"]["points"] and h["horizon_h"] == out["edge"]["horizon_h"]
    assert h["measured_rate"] == out["edge"]["funding_rate_last"]
    if h["assumed_rate"] is not None:
        assert h["assumption_label"] == "assumption, not a measurement"
        assert h["curve_assumed"]["is_assumption"] is True and h["curve"]["is_assumption"] is False
    json.dumps(out, default=str)  # plain JSON


async def test_evaluate_risk_and_operator_controls(engine):
    veto = engine.evaluate_risk(5000, 10, None)
    assert not veto.approved and veto.code == "LEVERAGE" and veto.observed == 10 and veto.limit == 3 and veto.dry_run
    ok = engine.evaluate_risk(5000, 2, None)
    assert ok.approved and ok.code == "OK"
    st = engine.kill_switch(True, "drill")
    assert st.kill_switch is True and engine.evaluate_risk(5000, 2, None).code == "KILL_SWITCH"
    engine.kill_switch(False, "drill over")
    eff, floor = engine.set_min_edge(-10)
    assert (eff, floor) == (-10.0, 0.0) and engine.gate.limits.min_expected_edge_bps == -10.0
    eff, _ = engine.set_min_edge(80)
    assert eff == 50.0
    engine.set_min_edge(-50)
    assert engine.reset_halt("not halted, no-op").halted is False


# --------------------------------------------------------------------------- one-command paths
def _env(tmp_path: Path) -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if not (k.startswith("DELTR_") or k.startswith("BINANCE_"))},
            "PYTHONPATH": str(REPO_ROOT), "DELTR_STATE_DIR": str(tmp_path / "state")}


def test_main_once_json_replay_prints_one_json_document(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--once", "--json", "--replay", str(REPLAY_FIXTURE), "--min-edge-bps", "-50"],
        cwd=REPO_ROOT, env=_env(tmp_path), capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    doc = json.loads(proc.stdout)  # exactly one JSON document, nothing else
    assert proc.stdout.count("\n") == 1
    assert doc["ok"] is True and doc["mode"] == "paper" and doc["replay"] is True
    assert doc["precheck"]["approved"] is True and doc["plan"]["legs"][0]["venue"] == "pancakeswap_v3"
    assert doc["market"]["source"] == "replay" and doc["gate_median_us"] > 0


async def test_main_mcp_stdio_is_clean_json_rpc(tmp_path: Path):
    port = free_port(8020)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "main.py"), "--mcp", "--no-ui", "--port", str(port), "--replay", str(REPLAY_FIXTURE), "--min-edge-bps", "-50"],
        cwd=str(REPO_ROOT), env=_env(tmp_path),
    )

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w, client_info=Implementation(name="deltr-test", version="1.0")) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                st = await s.call_tool("deltr_status", {})
                return init.serverInfo.name, [t.name for t in tools.tools], st

    name, names, st = await asyncio.wait_for(run(), timeout=60)
    assert name == "deltr" and sorted(names) == sorted(TOOL_NAMES) and len(names) == len(TOOL_NAMES)
    payload = st.structuredContent or json.loads(st.content[0].text)
    assert payload["mode"] == "paper" and payload["replay"] is True


# --------------------------------------------------------------------------- review fixes
async def test_run_once_with_auto_reports_the_auto_receipt(tmp_path: Path):
    from deltr.engine import build_engine
    from tests.conftest import make_replay_settings, refusing_http

    settings = make_replay_settings(tmp_path / "state", DELTR_AUTO_EXECUTE=True)
    eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http())
    await eng.start(loops=False, benchmark_iterations=500)
    try:
        out = await eng.run_once()
        assert out["ok"] is True and out["error"] is None
        assert out["receipt"] is not None and out["receipt"]["status"] == "filled" and out["receipt"]["source"] == "auto"
        assert out["plan"]["id"] == out["receipt"]["plan"]["id"] and out["precheck"]["approved"] is True and out["precheck"]["dry_run"] is False
        assert out["status"]["open_positions"] == 1 and len(eng.receipts.all()) == 1  # no second, vetoed proposal
        json.dumps(out, default=str)
    finally:
        await eng.stop()


async def test_replay_book_never_leaks_into_a_live_run(tmp_path: Path):
    """A replay --auto rehearsal persists under state/<mode>/replay/, not where the live PAPER book lives."""
    from deltr.engine import build_engine
    from deltr.market_data import MarketDataHub
    from tests.conftest import make_replay_settings, refusing_http

    state_dir = tmp_path / "state"
    settings = make_replay_settings(state_dir, DELTR_AUTO_EXECUTE=True)
    eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http())
    await eng.start(loops=False, benchmark_iterations=500)
    assert len(eng.portfolio.positions("open")) == 1
    await eng.stop()
    assert (state_dir / "paper" / "replay" / "portfolio.json").exists() and (state_dir / "paper" / "replay" / "gate.json").exists()
    assert not (state_dir / "paper" / "portfolio.json").exists() and not (state_dir / "paper" / "gate.json").exists()

    from deltr.config import Settings
    live = Settings(_env_file=None, DELTR_MODE="paper", DELTR_STATE_DIR=str(state_dir), DELTR_CAPITAL_USD=10_000.0)  # type: ignore[call-arg]
    eng2 = build_engine(live, http=refusing_http())
    assert isinstance(eng2.hub, MarketDataHub) and eng2.replay_path is None and eng2.status().replay is False
    assert eng2.portfolio.restore() is False and eng2.gate.load() is False
    assert eng2.portfolio.positions("open") == [] and eng2.gate.positions == {} and eng2.portfolio.cash_usd == 10_000.0
    await eng2.stop()


async def test_settings_only_replay_path_selects_the_replay_hub(tmp_path: Path):
    """DELTR_REPLAY_PATH without --replay must not run the live hub under a REPLAY badge."""
    from deltr.engine import build_engine
    from deltr.market_data import ReplayHub
    from tests.conftest import make_replay_settings, refusing_http

    eng = build_engine(make_replay_settings(tmp_path / "state"), http=refusing_http())  # no replay_path argument
    assert isinstance(eng.hub, ReplayHub) and eng.replay_path == str(REPLAY_FIXTURE) and eng.status().replay is True
    await eng.start(loops=False, benchmark_iterations=200)
    try:
        out = await eng.run_once()
        assert out["replay"] is True and out["status"]["replay"] is True and out["market"]["source"] == "replay"
    finally:
        await eng.stop()


async def test_feed_stale_with_custom_label_freezes_the_hub(engine):
    from deltr.models import StressKind, StressScenario

    res = await engine.stress(StressScenario(kind=StressKind.FEED_STALE, label="frozen for the demo"))
    assert res.active_label == "SIMULATED · frozen for the demo" and "feed_stale" not in res.active_label
    assert engine.hub.feed_frozen is True and engine.hub._feed_frozen is True
    plan, _proposal, precheck = await engine.propose(5_000.0, 2.0, "BNBUSDT", TraceSource.CLI)
    assert precheck.code == "STALE_QUOTE"
    await engine.stress(StressScenario(kind=StressKind.RESET))
    assert engine.hub.feed_frozen is False and engine.state.stress_active is None


async def test_negative_funding_hours_before_settlement_does_not_unwind(engine):
    from deltr.models import StressKind, StressScenario

    plan, _p, precheck = await engine.propose(5_000.0, 2.0, "BNBUSDT", TraceSource.CLI)
    assert precheck.approved
    receipt = await engine.execute(plan.id, True, TraceSource.CLI)
    assert receipt.status == "filled"
    ms = engine.hub.snapshot()
    remaining_h = (ms.funding.next_funding_time_ms - int(ms.ts.timestamp() * 1000)) / 3_600_000
    assert remaining_h > 0.25 or remaining_h < 0, "fixture settlement must be outside the 15-minute window for this test"
    res = await engine.stress(StressScenario(kind=StressKind.FUNDING_FLIP, magnitude=-0.0001))
    assert res.stops_fired == [] and len(engine.portfolio.positions("open")) == 1
    assert await engine.executor.check_stops_once() == []
    await engine.stress(StressScenario(kind=StressKind.RESET))
    await engine.unwind("all", "cleanup", True, TraceSource.CLI)
