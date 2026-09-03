"""Portfolio: mark-to-close, funding accrual sign, stops, funding-flip, equity → gate, stress, persistence."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deltr.edge import mark_to_close, perp_reference
from deltr.models import DataSource, Fill, Side, StressKind, StressScenario, Venue
from deltr.portfolio import Portfolio
from tests._fakes_d import FILTERS, FakeState, make_gate, make_market, make_plan, make_settings

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def _fills(ms, qty=4.85):
    dex_px = ms.dex.exec_price_buy * (1 + 1e-4)
    perp_px = ms.perp_ref_price * (1 - 2e-4)
    return [
        Fill(leg_index=0, venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.BUY, qty=qty, price=dex_px, fee_usd=ms.dex.gas_usd, ref="paper", simulated=True, source=DataSource.PAPER),
        Fill(leg_index=1, venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.SELL, qty=qty, price=perp_px, fee_usd=qty * perp_px * 5e-4, ref="paper", simulated=True, source=DataSource.PAPER),
    ]


def _book(tmp_path, ms=None, persist=False, **over):
    settings = make_settings(tmp_path, **over)
    ms = ms or make_market(ts=NOW, next_funding_ms=NOW_MS + 3_600_000)
    state = FakeState(settings)
    gate = make_gate(settings)
    pf = Portfolio(state, gate, settings, settings.portfolio_state_path if persist else None)
    pf.mark(ms, now=NOW)
    return settings, state, gate, pf, ms


def _open(pf, ms, **plan_over):
    plan = make_plan(ms, **plan_over)
    pf.reserve(plan.cash_required_usd)
    return pf.open_position(plan, _fills(ms, plan.qty)), plan


# --------------------------------------------------------------------------- cash
def test_reserve_release_roundtrip(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pf.reserve(4_992.0)
    assert pf.cash_usd == pytest.approx(10_000 - 4_992) and pf.reserved_cash_usd == pytest.approx(4_992)
    assert pf.equity() == pytest.approx(10_000)  # reserving is not a loss
    pf.release(4_992.0)
    assert pf.cash_usd == pytest.approx(10_000) and pf.reserved_cash_usd == 0.0
    with pytest.raises(ValueError):
        pf.reserve(50_000.0)


# --------------------------------------------------------------------------- mark-to-close
def test_open_position_is_delta_neutral_and_registered(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    assert pos.status == "open" and pos.delta_base == pytest.approx(0.0)
    assert pf.delta_base() == pytest.approx(0.0)
    assert pos.id in gate.positions and gate.positions[pos.id].base_qty == pytest.approx(4.85)
    assert pf.reserved_cash_usd == pytest.approx(0.0)
    assert state.positions[pos.id] is pos and state.portfolio.open_positions == 1
    assert pos.liq_price_est > pos.perp_entry  # short liquidates above entry


def test_mark_to_close_never_hides_exit_cost(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    snap = pf.mark(ms, now=NOW)
    dex_sell = ms.dex.exec_price_sell
    _, perp_buy = perp_reference(ms.perp_ref_price, settings.perp_slippage_bps)
    entry_fees = sum(f.fee_usd for f in _fills(ms))
    mtc = mark_to_close(dex_qty=pos.dex_qty, perp_qty=pos.perp_qty, dex_entry=pos.dex_entry, perp_entry=pos.perp_entry,
                        dex_sell_exec=dex_sell, perp_buy_ref=perp_buy, funding_accrued_usd=0.0, entry_fees_usd=entry_fees,
                        dex_fee_tier=settings.dex_fee_tier, cex_taker_bps=settings.perp_taker_fee_bps, gas_usd_per_swap=ms.dex.gas_usd)
    assert mtc.exit_cost_usd > 0
    assert pos.est_exit_cost_usd == pytest.approx(mtc.exit_cost_usd)
    assert pos.unrealized_pnl_usd == pytest.approx(mtc.net_pnl_usd)  # funding is 0 here
    assert pos.unrealized_pnl_usd < mtc.spread_pnl_usd  # exit cost + entry fees always subtracted
    locked = pos.notional_usd + pos.margin_usd
    assert snap.equity_usd == pytest.approx(pf.cash_usd + locked + pos.unrealized_pnl_usd)
    assert snap.equity_usd < settings.capital_usd  # entering costs money; equity says so immediately
    assert pos.stop_distance_usd == pytest.approx(pos.allocated_risk_usd + pos.unrealized_pnl_usd)


# --------------------------------------------------------------------------- funding
@pytest.mark.parametrize("rate, sign", [(0.0001, 1), (-0.0001, -1)])
def test_funding_accrues_with_correct_sign_at_settlement_crossing(tmp_path, rate, sign):
    ms = make_market(ts=NOW, rate=rate, next_funding_ms=NOW_MS + 3_600_000)
    settings, state, gate, pf, _ = _book(tmp_path, ms=ms)
    pos, plan = _open(pf, ms)
    pf.mark(ms, now=NOW + timedelta(minutes=30))
    assert pos.funding_accrued_usd == 0.0  # no settlement crossed yet
    pf.mark(ms, now=NOW + timedelta(hours=1, seconds=1))
    expected = rate * pos.perp_qty * ms.funding.mark_price
    assert pos.funding_accrued_usd == pytest.approx(expected)
    assert (pos.funding_accrued_usd > 0) == (sign > 0)
    pf.mark(ms, now=NOW + timedelta(hours=2))
    assert pos.funding_accrued_usd == pytest.approx(expected)  # same settlement is never double-counted
    pf.mark(ms, now=NOW + timedelta(hours=9, seconds=1))  # second settlement 8 h later
    assert pos.funding_accrued_usd == pytest.approx(2 * expected)
    snap = pf.snapshot()
    assert snap.funding_pnl_usd == pytest.approx(2 * expected)


def test_funding_flip_stress_overrides_the_rate(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    pf.apply_stress(StressScenario(kind=StressKind.FUNDING_FLIP, magnitude=-0.0003))
    pf.mark(ms, now=NOW + timedelta(hours=1, seconds=1))
    assert pos.funding_accrued_usd == pytest.approx(-0.0003 * pos.perp_qty * ms.funding.mark_price)
    assert pos.stress_applied and "SIMULATED" in pos.stress_applied


# --------------------------------------------------------------------------- stops
def test_position_stop_fires_when_loss_eats_allocated_risk(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    assert pf.stop_breached(pos) is None
    res = pf.apply_stress(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=200.0))
    assert pos.stop_distance_usd <= 0.0
    assert pf.stop_breached(pos) == "position_stop"
    assert res.stops_fired == [pos.id] and res.positions_affected == 1
    assert res.equity_after < res.equity_before


def test_funding_flip_rule_when_carry_cannot_pay_for_holding(tmp_path):
    # settlement an HOUR away: a negative rate never unwinds a fresh position on its own (no churn)
    ms = make_market(ts=NOW, rate=-0.0005, next_funding_ms=NOW_MS + 3_600_000)
    settings, state, gate, pf, _ = _book(tmp_path, ms=ms)
    pos, plan = _open(pf, ms)
    pf.mark(ms, now=NOW)
    assert pos.stop_distance_usd > 0  # not a hard stop ...
    assert pf.stop_breached(pos) is None
    # ... but INSIDE the 15-minute pre-settlement window, paying to hold with no basis to earn unwinds
    ms2 = make_market(ts=NOW, rate=-0.0005, next_funding_ms=NOW_MS + 10 * 60_000)
    pf.mark(ms2, now=NOW)
    assert pf.stop_breached(pos) == "funding_flip"


# --------------------------------------------------------------------------- equity → gate
def test_equity_feeds_gate_drawdown_states(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    r = pf.apply_stress(StressScenario(kind=StressKind.EQUITY_SHOCK, magnitude=2.5))
    assert gate.state == "WARN" and r.dd_state == "WARN" and not r.halted
    r = pf.apply_stress(StressScenario(kind=StressKind.EQUITY_SHOCK, magnitude=3.5))
    assert gate.halted and r.halted and r.dd_state == "HALTED"
    r = pf.clear_stress()
    assert r.active_label is None and r.equity_after > r.equity_before
    assert gate.halted  # sticky: only reset_halt clears it


def test_stress_mutates_portfolio_only(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    before = ms.model_dump(mode="json")
    eq0 = pf.equity()
    pf.apply_stress(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=50.0))
    assert ms.model_dump(mode="json") == before  # feed untouched
    assert pf.equity() < eq0 and pos.stress_applied.startswith("SIMULATED")
    pf.clear_stress()
    assert pf.equity() == pytest.approx(eq0) and pos.stress_applied is None


# --------------------------------------------------------------------------- close
def test_close_position_realizes_and_releases_gate(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    exit_fills = [
        Fill(leg_index=0, venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.SELL, qty=pos.dex_qty, price=ms.dex.exec_price_sell, fee_usd=ms.dex.gas_usd, ref="paper", simulated=True, source=DataSource.PAPER),
        Fill(leg_index=1, venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.BUY, qty=pos.perp_qty, price=ms.perp_ref_price * (1 + 2e-4), fee_usd=pos.perp_qty * ms.perp_ref_price * 5e-4, ref="paper", simulated=True, source=DataSource.PAPER),
    ]
    entry_fees = sum(f.fee_usd for f in _fills(ms))
    closed = pf.close_position(pos.id, exit_fills, "test")
    spread = (exit_fills[0].price - pos.dex_entry) * pos.dex_qty + (pos.perp_entry - exit_fills[1].price) * pos.perp_qty
    realized = spread - entry_fees - sum(f.fee_usd for f in exit_fills)
    assert closed.status == "closed" and closed.close_reason == "test"
    assert closed.realized_pnl_usd == pytest.approx(realized)
    assert pf.cash_usd == pytest.approx(10_000 + realized)
    assert pf.positions() == [] and pf.positions("closed") == [closed]
    assert pos.id not in gate.positions
    assert pf.snapshot().daily_realized_usd == pytest.approx(realized)
    with pytest.raises(KeyError):
        pf.close_position(pos.id, exit_fills, "again")


def test_partial_close_keeps_remainder_registered(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, plan = _open(pf, ms)
    half = 2.0
    exit_fills = [
        Fill(leg_index=0, venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.SELL, qty=half, price=ms.dex.exec_price_sell, fee_usd=ms.dex.gas_usd, ref="paper", simulated=True, source=DataSource.PAPER),
        Fill(leg_index=1, venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.BUY, qty=half, price=ms.perp_ref_price, fee_usd=half * ms.perp_ref_price * 5e-4, ref="paper", simulated=True, source=DataSource.PAPER),
    ]
    p = pf.close_position(pos.id, exit_fills, "partial")
    assert p.status == "open" and p.perp_qty == pytest.approx(2.85) and p.dex_qty == pytest.approx(2.85)
    assert gate.positions[pos.id].base_qty == pytest.approx(2.85)


# --------------------------------------------------------------------------- persistence
def test_persistence_round_trip(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path, persist=True)
    pos, plan = _open(pf, ms)
    pf.mark(ms, now=NOW + timedelta(hours=1, seconds=1))  # one funding settlement accrued
    pf.persist()
    raw = json.loads(open(settings.portfolio_state_path).read())
    assert raw["mode"] == "paper" and len(raw["positions"]) == 1
    # fresh process: new gate (empty registry), new portfolio
    gate2 = make_gate(settings)
    state2 = FakeState(settings)
    pf2 = Portfolio(state2, gate2, settings, settings.portfolio_state_path)
    assert pf2.restore() is True
    p2 = pf2.get(pos.id)
    assert p2 is not None and p2.model_dump(mode="json") == pos.model_dump(mode="json")
    assert pf2.cash_usd == pytest.approx(pf.cash_usd)
    assert pos.id in gate2.positions  # registry re-established
    assert pf2.stress_flags.active() is False
    pf2.mark(ms, now=NOW + timedelta(hours=2))
    assert p2.funding_accrued_usd == pytest.approx(pos.funding_accrued_usd)  # marker restored: no double accrual
    assert pf2.equity() == pytest.approx(pf.equity(), rel=1e-9)


def test_restore_refuses_other_mode_state(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path, persist=True)
    pf.persist()
    raw = json.loads(open(settings.portfolio_state_path).read())
    raw["mode"] = "testnet"
    open(settings.portfolio_state_path, "w").write(json.dumps(raw))
    pf2 = Portfolio(FakeState(settings), make_gate(settings), settings, settings.portfolio_state_path)
    assert pf2.restore() is False and pf2.cash_usd == settings.capital_usd


def test_equity_curve_is_bounded(tmp_path):
    settings, state, gate, pf, ms = _book(tmp_path)
    for i in range(700):
        pf.mark(ms, now=NOW + timedelta(seconds=i))
    assert len(pf.snapshot().equity_curve) == 600


# --------------------------------------------------------------------------- clock discipline
def test_funding_accrual_is_independent_of_the_wall_clock(tmp_path):
    """Regression: the book must run on the market state's clock, not the machine's.

    Seeding the funding schedule from `utcnow()` made these tests pass on the day
    they were written and fail the next, and made replay non-deterministic.
    """
    far_past = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
    past_ms = int(far_past.timestamp() * 1000)
    ms = make_market(ts=far_past, rate=0.0001, next_funding_ms=past_ms + 3_600_000)
    settings, state, gate, pf, _ = _book(tmp_path, ms=ms)
    pf.mark(ms, now=far_past)
    pos, _ = _open(pf, ms)
    pf.mark(ms, now=far_past + timedelta(minutes=30))
    assert pos.funding_accrued_usd == 0.0
    pf.mark(ms, now=far_past + timedelta(hours=1, seconds=1))
    assert pos.funding_accrued_usd == pytest.approx(0.0001 * pos.perp_qty * ms.funding.mark_price)


def test_applying_stress_does_not_advance_the_book_clock(tmp_path):
    """Stress changes prices, never time: it must accrue no funding of its own."""
    settings, state, gate, pf, ms = _book(tmp_path)
    pos, _ = _open(pf, ms)
    pf.mark(ms, now=NOW)
    assert pos.funding_accrued_usd == 0.0
    pf.apply_stress(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=50.0))
    assert pos.funding_accrued_usd == 0.0, "stress accrued a phantom settlement"
    pf.clear_stress()
    assert pos.funding_accrued_usd == 0.0
