"""StressController: scenarios mutate the portfolio only, SIMULATED badge set/cleared, refused in
TESTNET with open real orders, dex_leg_fail exercises reverse-on-failure, feed_stale → STALE_QUOTE."""
from __future__ import annotations

import pytest

from deltr.models import Side, StressKind, StressScenario, TraceSource, Venue
from deltr.stress import StressController, StressRefused
from tests._fakes_d import FakeRouter, make_plan, make_stack, open_via_execute

DEX, PERP = Venue.PANCAKESWAP_V3, Venue.BINANCE_FUTURES


def _ctl(s):
    return StressController(s.state, s.portfolio, s.executor, s.hub, s.settings)


async def test_basis_shock_moves_mark_to_close_only_and_badges(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    pos = s.portfolio.get(r.position_id)
    feed_before = s.hub.snapshot().model_dump(mode="json")
    market_before = s.state.market.model_dump(mode="json")
    unreal_before = pos.unrealized_pnl_usd
    ctl = _ctl(s)
    res = await ctl.apply(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=50.0))
    assert s.hub.snapshot().model_dump(mode="json") == feed_before and s.state.market.model_dump(mode="json") == market_before
    assert pos.unrealized_pnl_usd < unreal_before
    assert pos.unrealized_pnl_usd - unreal_before == pytest.approx(-pos.dex_qty * s.ms.dex.exec_price_sell * 50 / 1e4, rel=1e-3)
    assert res.active_label and "SIMULATED" in res.active_label and "basis_shock" in res.active_label
    assert ctl.active() == res.active_label and s.state.stress_active == res.active_label
    assert pos.stress_applied == res.active_label and res.positions_affected == 1 and res.stops_fired == []
    # receipts produced while the badge is on carry it
    plan2, r2 = await open_via_execute(s, qty=1.0)
    assert r2.stress_active == res.active_label
    reset = await ctl.reset()
    assert reset.active_label is None and ctl.active() is None and s.state.stress_active is None
    assert pos.unrealized_pnl_usd == pytest.approx(unreal_before) and pos.stress_applied is None


async def test_equity_shock_halts_and_reset_keeps_halt_sticky(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    ctl = _ctl(s)
    res = await ctl.apply(StressScenario(kind=StressKind.EQUITY_SHOCK, magnitude=3.5))
    assert res.halted and res.dd_state == "HALTED" and res.drawdown_pct >= 3.0 and s.gate.halted
    assert res.equity_after == pytest.approx(res.equity_before * (1 - 0.035), rel=1e-9)
    plan2, r2 = await open_via_execute(s, qty=1.0)
    assert r2.status == "vetoed" and r2.decision.code == "HALTED_DRAWDOWN"
    reset = await ctl.reset()
    assert reset.equity_after == pytest.approx(res.equity_before, rel=1e-9)
    assert s.gate.halted  # sticky until the operator's reset_halt (never re-based by stress)


async def test_basis_shock_that_breaches_the_stop_unwinds_through_the_executor(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    ctl = _ctl(s)
    res = await ctl.apply(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=200.0))
    assert res.stops_fired == [r.position_id]
    assert s.portfolio.positions() == [] and s.gate.positions == {}
    last = s.receipts.recent(1)[0]
    assert last.status == "unwound" and last.source == TraceSource.AUTO
    assert next(st for st in last.steps if st.step == "unwind").data["reason"] == "position_stop"
    assert last.stress_active and "SIMULATED" in last.stress_active


async def test_dex_leg_fail_exercises_reverse_on_failure(tmp_path):
    s = make_stack(tmp_path, DELTR_LEG_ORDER="cex_first")
    ctl = _ctl(s)
    res = await ctl.apply(StressScenario(kind=StressKind.DEX_LEG_FAIL))
    assert s.portfolio.stress_flags.dex_leg_fail is True and "dex_leg_fail" in res.active_label
    plan, r = await open_via_execute(s)
    assert r.status == "unwound" and r.realized_cost_usd > 0
    assert [c[:3] for c in s.router.calls] == [("fill", PERP, Side.SELL), ("fill", DEX, Side.BUY), ("reverse", PERP, Side.BUY)]
    assert s.portfolio.stress_flags.dex_leg_fail is False  # consumed once
    plan2, r2 = await open_via_execute(s)
    assert r2.status == "filled"


async def test_dex_leg_fail_under_dex_first_fails_cleanly(tmp_path):
    s = make_stack(tmp_path)  # default dex_first: the DEX leg is the first leg, nothing to reverse
    await _ctl(s).apply(StressScenario(kind=StressKind.DEX_LEG_FAIL))
    plan, r = await open_via_execute(s)
    assert r.status == "failed" and r.fills == [] and s.portfolio.cash_usd == pytest.approx(10_000)


async def test_feed_stale_vetoes_and_reset_clears(tmp_path):
    s = make_stack(tmp_path)
    ctl = _ctl(s)
    await ctl.apply(StressScenario(kind=StressKind.FEED_STALE))
    assert s.hub.stale is True and s.portfolio.stress_flags.feed_stale
    plan, r = await open_via_execute(s)
    assert r.status == "vetoed" and r.decision.code == "STALE_QUOTE"
    await ctl.reset()
    assert s.hub.stale is False
    plan2, r2 = await open_via_execute(s)
    assert r2.status == "filled"


async def test_funding_flip_scenario_labels_and_overrides(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    res = await _ctl(s).apply(StressScenario(kind=StressKind.FUNDING_FLIP, magnitude=-0.0003))
    assert "funding_flip" in res.active_label and s.portfolio.stress_flags.funding_rate_override == -0.0003


async def test_refused_in_testnet_with_open_real_orders(tmp_path):
    s = make_stack(tmp_path, mode="testnet")
    ctl = _ctl(s)
    res = await ctl.apply(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=50.0))  # flat book: allowed
    assert res.active_label
    await ctl.reset()
    plan, r = await open_via_execute(s)
    assert r.status == "filled"
    with pytest.raises(StressRefused):
        await ctl.apply(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=50.0))
    with pytest.raises(StressRefused):
        await ctl.apply(StressScenario(kind=StressKind.DEX_LEG_FAIL))
    assert s.state.stress_active is None and s.portfolio.stress_flags.active() is False


async def test_reset_kind_routes_to_reset(tmp_path):
    s = make_stack(tmp_path)
    ctl = _ctl(s)
    await ctl.apply(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=10.0))
    res = await ctl.apply(StressScenario(kind=StressKind.RESET))
    assert res.active_label is None and ctl.active() is None
