"""Executor: leg sequencing (both orders), reverse-on-failure both ways, lock, plan single-use /
TTL / PRICE_DRIFT, receipts + sha256, verified unwind while HALTED, stop monitor, dry runs."""
from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest

from deltr.models import Side, StressKind, StressScenario, TraceSource, Venue, sha256_of, utcnow
from deltr.executor import Executor, PositionNotFound, client_order_id, legging_window_ms
from tests._fakes_d import FakeRouter, make_market, make_plan, make_stack, open_via_execute

DEX, PERP = Venue.PANCAKESWAP_V3, Venue.BINANCE_FUTURES


# --------------------------------------------------------------------------- happy path
async def test_execute_dex_first_fills_registers_and_seals(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    assert r.status == "filled" and r.decision.approved and r.decision.code == "OK"
    assert [c[:3] for c in s.router.calls] == [("fill", DEX, Side.BUY), ("fill", PERP, Side.SELL)]
    assert s.router.calls[1][3] == pytest.approx(4.85)  # second leg sized from the first fill
    assert [st.step for st in r.steps] == ["plan", "scan", "gate", "dex_fill", "cex_fill", "position", "receipt"]
    assert all(st.status == "ok" for st in r.steps)
    assert r.legging_window_ms is not None and r.legging_window_ms >= 0
    assert re.fullmatch(r"[0-9a-f]{64}", r.sha256) and r.sha256 == sha256_of(r.digest_body())
    assert r.fills[1].client_id == client_order_id(plan.id, 1, 1)
    assert r.position_id in s.gate.positions and s.gate.positions[r.position_id].base_qty == pytest.approx(4.85)
    pos = s.portfolio.get(r.position_id)
    assert pos.delta_base == pytest.approx(0.0) and r.residual_delta_base == pytest.approx(0.0)
    assert s.receipts.get(r.id) is r and s.state.receipts[-1] is r
    assert s.state.decisions[-1].plan_id == plan.id and s.state.decisions[-1].dry_run is False
    assert plan.id not in s.state.plans and s.portfolio.reserved_cash_usd == 0.0
    assert any(e.topic == "fill" for e in s.state.events)


async def test_execute_cex_first_orders_perp_then_dex(tmp_path):
    s = make_stack(tmp_path, DELTR_LEG_ORDER="cex_first")
    plan, r = await open_via_execute(s)
    assert r.status == "filled"
    assert [c[:3] for c in s.router.calls] == [("fill", PERP, Side.SELL), ("fill", DEX, Side.BUY)]
    assert [st.step for st in r.steps] == ["plan", "scan", "gate", "cex_fill", "dex_fill", "position", "receipt"]
    assert s.portfolio.get(r.position_id).delta_base == pytest.approx(0.0)


async def test_veto_fills_nothing_and_keeps_cash(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s, leverage=10.0)
    assert r.status == "vetoed" and r.decision.code == "LEVERAGE" and r.decision.observed == 10.0 and r.decision.limit == 3.0
    assert r.fills == [] and s.router.calls == []
    assert s.portfolio.cash_usd == pytest.approx(10_000) and s.portfolio.reserved_cash_usd == 0.0
    assert [st.step for st in r.steps] == ["plan", "scan", "gate", "receipt"] and r.steps[2].status == "veto"
    checks = r.steps[2].data["checks"]
    assert any(c["name"] == "LEVERAGE" and not c["passed"] for c in checks)


# --------------------------------------------------------------------------- reverse-on-failure
async def test_second_leg_failure_reverses_first_dex_first(tmp_path):
    s = make_stack(tmp_path, router=FakeRouter(fail=((PERP, Side.SELL),)))
    plan, r = await open_via_execute(s)
    assert r.status == "unwound"
    assert [c[:3] for c in s.router.calls] == [("fill", DEX, Side.BUY), ("fill", PERP, Side.SELL), ("reverse", DEX, Side.SELL)]
    assert s.router.calls[2][3] == pytest.approx(4.85)
    assert r.realized_cost_usd > 0 and len(r.fills) == 2 and r.position_id is None
    assert s.portfolio.positions() == [] and s.gate.positions == {}
    assert s.portfolio.reserved_cash_usd == 0.0
    assert s.portfolio.cash_usd == pytest.approx(10_000 - r.realized_cost_usd)
    assert [st.step for st in r.steps] == ["plan", "scan", "gate", "dex_fill", "error", "dex_fill", "receipt"]


async def test_second_leg_failure_reverses_first_cex_first(tmp_path):
    s = make_stack(tmp_path, router=FakeRouter(fail=((DEX, Side.BUY),)), DELTR_LEG_ORDER="cex_first")
    plan, r = await open_via_execute(s)
    assert r.status == "unwound"
    assert [c[:3] for c in s.router.calls] == [("fill", PERP, Side.SELL), ("fill", DEX, Side.BUY), ("reverse", PERP, Side.BUY)]
    assert r.realized_cost_usd > 0 and s.portfolio.positions() == []
    assert s.portfolio.cash_usd == pytest.approx(10_000 - r.realized_cost_usd)


async def test_first_leg_failure_has_nothing_to_reverse(tmp_path):
    s = make_stack(tmp_path, router=FakeRouter(fail=((DEX, Side.BUY),)))
    plan, r = await open_via_execute(s)
    assert r.status == "failed" and r.fills == []
    assert [c[0] for c in s.router.calls] == ["fill"]
    assert s.portfolio.cash_usd == pytest.approx(10_000) and s.portfolio.reserved_cash_usd == 0.0
    assert r.steps[-2].step == "error"


async def test_reverse_failure_is_loud_never_silent(tmp_path):
    s = make_stack(tmp_path, router=FakeRouter(fail=((PERP, Side.SELL),), fail_reverse=True))
    plan, r = await open_via_execute(s)
    assert r.status == "failed" and len(r.fills) == 1
    assert r.residual_delta_base == pytest.approx(4.85)  # unhedged DEX long reported, not hidden
    assert any(e.level == "error" for e in s.state.events)


async def test_partial_second_fill_reverses_residual(tmp_path):
    s = make_stack(tmp_path, router=FakeRouter(partial={PERP: 4.0}))
    plan, r = await open_via_execute(s)
    assert r.status == "filled"
    assert s.router.calls[2][:3] == ("reverse_partial", DEX, Side.SELL) and s.router.calls[2][3] == pytest.approx(0.85)
    pos = s.portfolio.get(r.position_id)
    assert pos.dex_qty == pytest.approx(4.0) and pos.perp_qty == pytest.approx(4.0) and pos.delta_base == pytest.approx(0.0)
    assert s.gate.positions[pos.id].base_qty == pytest.approx(4.0)
    assert len(r.fills) == 3


# --------------------------------------------------------------------------- plan lifecycle
async def test_plan_is_single_use(tmp_path):
    s = make_stack(tmp_path)
    plan, r1 = await open_via_execute(s)
    r2 = await s.executor.execute(plan.id, True, TraceSource.MCP, "claude")
    assert r1.status == "filled" and r2.status == "expired" and r2.decision.code == "PLAN_NOT_FOUND"
    assert r2.fills == [] and len(s.portfolio.positions()) == 1
    r3 = await s.executor.execute("plan_doesnotexist", True, TraceSource.MCP)
    assert r3.status == "expired" and r3.decision.code == "PLAN_NOT_FOUND"


async def test_expired_plan_is_refused(tmp_path):
    s = make_stack(tmp_path)
    plan = make_plan(s.ms, expires_at=utcnow() - timedelta(seconds=1))
    s.state.put_plan(plan)
    r = await s.executor.execute(plan.id, True, TraceSource.API)
    assert r.status == "expired" and r.decision.code == "PLAN_EXPIRED"
    assert s.router.calls == [] and plan.id not in s.state.plans


async def test_price_drift_veto_on_requote(tmp_path):
    ms = make_market()
    drifted = ms.dex.model_copy(update={"exec_price_buy": ms.dex.exec_price_buy * 1.0025})  # +25 bps
    s = make_stack(tmp_path, ms=ms, router=FakeRouter(requote=drifted))
    plan, r = await open_via_execute(s)
    assert r.status == "vetoed" and r.decision.code == "PRICE_DRIFT"
    assert r.decision.observed == pytest.approx(25.0, abs=0.01) and r.decision.limit == 20.0
    assert s.router.calls == []


async def test_stale_feed_flag_vetoes(tmp_path):
    ms = make_market(ages=(7_000, 800, 150), ok=False, reason="cex_stale")
    s = make_stack(tmp_path, ms=ms)
    plan, r = await open_via_execute(s)
    assert r.status == "vetoed" and r.decision.code == "STALE_QUOTE"


# --------------------------------------------------------------------------- lock
async def test_concurrent_executes_serialize_and_second_sees_reservation(tmp_path):
    s = make_stack(tmp_path, router=FakeRouter(delay_s=0.02))
    p1, p2 = make_plan(s.ms), make_plan(s.ms)
    s.state.put_plan(p1)
    s.state.put_plan(p2)
    seen = []

    async def run(pid):
        seen.append(("start", pid, s.executor.in_flight))
        return await s.executor.execute(pid, True, TraceSource.API)

    r1, r2 = await asyncio.gather(run(p1.id), run(p2.id))
    assert s.router.max_active == 1  # never two legs in flight at once
    statuses = sorted([r1.status, r2.status])
    assert statuses == ["filled", "vetoed"]
    veto = r1 if r1.status == "vetoed" else r2
    assert veto.decision.code in ("CAPITAL_CAPACITY", "AGGREGATE_RISK")  # gate-owned registry saw the first fill
    assert len(s.portfolio.positions()) == 1 and s.executor.in_flight is False


# --------------------------------------------------------------------------- TESTNET confirm convention
async def test_testnet_requires_confirm_and_keeps_plan(tmp_path):
    s = make_stack(tmp_path, mode="testnet")
    plan = make_plan(s.ms)
    s.state.put_plan(plan)
    r = await s.executor.execute(plan.id, False, TraceSource.MCP, "claude")
    assert r.status == "vetoed" and r.decision.code == "CONFIRM_REQUIRED" and s.router.calls == []
    assert plan.id in s.state.plans  # not consumed: the caller can confirm
    r2 = await s.executor.execute(plan.id, True, TraceSource.MCP, "claude")
    assert r2.status == "filled" and r2.mode.value == "testnet"


# --------------------------------------------------------------------------- unwind
async def test_verified_unwind_passes_while_halted(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    s.gate.update_equity(9_600.0)
    assert s.gate.halted
    s.router.calls.clear()
    u = await s.executor.unwind(r.position_id, "operator", TraceSource.MCP, "claude")
    assert u.status == "unwound" and u.decision.approved and u.decision.code == "OK"
    assert [c[:3] for c in s.router.calls] == [("fill", PERP, Side.BUY), ("fill", DEX, Side.SELL)]  # perp reduce-only first
    assert u.plan.reduce_only and u.plan.position_id == r.position_id
    assert [st.step for st in u.steps] == ["plan", "gate", "cex_fill", "dex_fill", "unwind", "receipt"]
    assert s.portfolio.positions() == [] and s.gate.positions == {} and s.portfolio.delta_base() == 0.0
    assert s.portfolio.positions("closed")[0].close_reason == "operator"
    # a NEW entry is still halted
    plan2, r2 = await open_via_execute(s)
    assert r2.status == "vetoed" and r2.decision.code == "HALTED_DRAWDOWN"


async def test_unwind_unknown_position_raises(tmp_path):
    s = make_stack(tmp_path)
    with pytest.raises(PositionNotFound):
        await s.executor.unwind("pos_nope", "x", TraceSource.API)


async def test_unwind_perp_leg_failure_keeps_position_open(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    s.router.fail.add((PERP, Side.BUY))
    u = await s.executor.unwind(r.position_id, "operator", TraceSource.API)
    assert u.status == "failed" and u.fills == []
    assert s.portfolio.get(r.position_id) is not None and r.position_id in s.gate.positions
    assert any(e.level == "error" and e.topic == "position" for e in s.state.events)


async def test_unwind_dex_leg_failure_restores_hedge(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    s.router.fail.add((DEX, Side.SELL))
    s.router.calls.clear()
    u = await s.executor.unwind(r.position_id, "operator", TraceSource.API)
    assert u.status == "failed"
    assert [c[:3] for c in s.router.calls] == [("fill", PERP, Side.BUY), ("fill", DEX, Side.SELL), ("reverse", PERP, Side.SELL)]
    assert s.portfolio.get(r.position_id) is not None  # still hedged and open
    assert u.realized_cost_usd > 0


async def test_unwind_all_and_stop_monitor(tmp_path):
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s, qty=2.0)
    plan2, r2 = await open_via_execute(s, qty=2.0)
    assert len(s.portfolio.positions()) == 2
    assert await s.executor.check_stops_once() == []  # nothing breached
    s.portfolio.apply_stress(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=200.0))
    receipts = await s.executor.check_stops_once()
    assert len(receipts) == 2 and all(x.status == "unwound" and x.source == TraceSource.AUTO for x in receipts)
    assert all(next(st for st in x.steps if st.step == "unwind").data["reason"] == "position_stop" for x in receipts)
    assert all(x.plan.prompt == "unwind:position_stop" for x in receipts)
    assert s.portfolio.positions() == []
    plan3, r3 = await open_via_execute(s, qty=2.0)
    assert r3.status == "filled"
    out = await s.executor.unwind_all("kill", TraceSource.CLI)
    assert len(out) == 1 and out[0].status == "unwound" and s.portfolio.positions() == []


async def test_run_stop_monitor_stops_on_event(tmp_path):
    s = make_stack(tmp_path, DELTR_POLL_CEX_S=0.01)
    plan, r = await open_via_execute(s)
    stop = asyncio.Event()
    task = asyncio.create_task(s.executor.run_stop_monitor(stop))
    await asyncio.sleep(0.03)
    s.portfolio.apply_stress(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=200.0))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not s.portfolio.positions():
            break
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert s.portfolio.positions() == []


# --------------------------------------------------------------------------- dry runs
def test_precheck_is_dry_run_and_keeps_plan(tmp_path):
    s = make_stack(tmp_path)
    plan = make_plan(s.ms)
    s.state.put_plan(plan)
    d = s.executor.precheck(plan)
    assert d.approved and d.dry_run and d.plan_id == plan.id and plan.id in s.state.plans
    assert [c.name for c in d.checks][:3] == ["KILL_SWITCH", "HALTED_DRAWDOWN", "MALFORMED"]


def test_dry_run_fail_closed_and_sizing(tmp_path):
    s = make_stack(tmp_path, ms=make_market(rate=0.0003))
    assert s.executor.dry_run(5_000.0, None, "BNBUSDT").code == "MALFORMED"  # never "assume 1x"
    d = s.executor.dry_run(5_000.0, 10.0, "BNBUSDT")
    assert d.code == "LEVERAGE" and d.observed == 10.0 and d.limit == 3.0 and d.dry_run
    d = s.executor.dry_run(5_000.0, 2.0, "BNBUSDT")
    assert d.approved and d.code == "OK"
    notional = next(c for c in d.checks if c.name == "MAX_NOTIONAL").observed
    assert notional == pytest.approx(4.85 * s.ms.dex.exec_price_buy, rel=1e-6)  # $5,000 @ 2x → 4.85 BNB
    assert s.state.plans == {}  # nothing stored
    s.hub.set(None)
    assert s.executor.dry_run(5_000.0, 2.0, "BNBUSDT").code == "MALFORMED"


def test_build_proposal_is_flat_and_uses_requote(tmp_path):
    s = make_stack(tmp_path)
    plan = make_plan(s.ms)
    q = s.ms.dex.model_copy(update={"exec_price_buy": s.ms.dex.exec_price_buy * 1.001})
    p = s.executor.build_proposal(plan, s.ms, q)
    assert p.is_delta_neutral and p.dex_side == Side.BUY and p.perp_side == Side.SELL
    assert p.price_drift_bps == pytest.approx(10.0, abs=1e-6) and p.dex_ref_price == q.exec_price_buy
    assert p.perp_ref_price == s.ms.perp_ref_price and p.quote_age_ms == 800 and p.qty_step == 0.01
    gi = p.as_gate_input()
    assert gi["dex_side"] == "BUY" and gi["is_delta_neutral"] is True and gi["leverage"] == 2.0


def test_helpers():
    assert client_order_id("plan_0123456789ab", 1, 2) == "DLTR456789ab12"
    assert re.fullmatch(r"^[\.A-Z\:/a-z0-9_-]{1,36}$", client_order_id("plan_0123456789ab", 1, 2))
    from deltr.models import DataSource, Fill
    a = Fill(leg_index=0, venue=DEX, symbol="BNBUSDT", side=Side.BUY, qty=1, price=1, fee_usd=0, ref="x", simulated=True, source=DataSource.PAPER, ts=utcnow())
    b = a.model_copy(update={"ts": a.ts + timedelta(milliseconds=37)})
    assert legging_window_ms(a, b) == 37
