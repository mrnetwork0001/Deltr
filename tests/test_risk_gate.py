"""Risk gate invariants + micro-benchmark.

Every check in `risk_gate.CHECK_ORDER` has at least one test that proves it
vetoes, plus happy-path tests that prove a compliant delta-neutral hedge is
approved.  The benchmark asserts the hot path stays in the microsecond range.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time

import pytest

from risk_gate import (
    CHECK_ORDER,
    MAX_CAPITAL_RISK_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_LEVERAGE,
    BinanceRiskGate,
    RiskLimits,
)


def good(**over):
    base = {
        "symbol": "BNBUSDT",
        "is_delta_neutral": True,
        "dex_side": "BUY",
        "perp_side": "SELL",
        "leverage": 2.0,
        "allocated_risk": 60.0,
        "roundtrip_cost_bps": 20.0,
        "notional": 3_000.0,
        "dex_qty": 4.38,
        "perp_qty": 4.38,
        "qty_step": 0.01,
        "quote_age_ms": 120.0,
        "price_drift_bps": 1.5,
        "expected_edge_bps": 6.5,
        "dex_ref_price": 686.19,
        "perp_ref_price": 686.34,
    }
    base.update(over)
    return base


@pytest.fixture
def gate():
    return BinanceRiskGate(capital_usd=10_000.0)


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #
def test_compliant_hedge_is_approved(gate):
    d = gate.evaluate(good())
    assert d.approved and d.code == "OK"
    assert d.latency_ns > 0
    assert gate.decisions == 1 and gate.vetoes == 0


def test_explain_lists_every_check_true_on_approval(gate):
    d = gate.explain(good())
    assert d.approved
    assert [c.name for c in d.checks] == list(CHECK_ORDER)
    assert all(c.passed for c in d.checks)
    lev = next(c for c in d.checks if c.name == "LEVERAGE")
    assert lev.observed == 2.0 and lev.limit == MAX_LEVERAGE and lev.unit == "x"


def test_legacy_signature_still_works(gate):
    ok, reason = gate.evaluate_arbitrage_trade(
        {"pair": "BNB/USDT", "is_delta_neutral": True, "leverage": 2.0, "allocated_risk": 150.0}
    )
    assert ok and reason.startswith("APPROVED")


def test_legacy_constructor_alias():
    g = BinanceRiskGate(portfolio_balance=5_000.0)
    assert g.equity == 5_000.0 and g.portfolio_balance == 5_000.0


# --------------------------------------------------------------------------- #
# Spec invariants                                                             #
# --------------------------------------------------------------------------- #
def test_leverage_above_3x_is_vetoed(gate):
    d = gate.evaluate(good(leverage=3.01))
    assert not d.approved and d.code == "LEVERAGE"
    assert d.observed == 3.01 and d.limit == 3.0


def test_leverage_exactly_3x_is_allowed(gate):
    d = gate.evaluate(good(leverage=3.0, notional=1_000.0, dex_qty=1.46, perp_qty=1.46))
    assert d.approved


def test_missing_leverage_fails_closed(gate):
    p = good()
    del p["leverage"]
    d = gate.evaluate(p)
    assert not d.approved and d.code == "MALFORMED"


def test_risk_above_2pct_is_vetoed(gate):
    d = gate.evaluate(good(allocated_risk=200.01))
    assert not d.approved and d.code == "CAPITAL_RISK"
    assert "2%" in d.reason


def test_risk_exactly_2pct_is_allowed(gate):
    d = gate.evaluate(good(allocated_risk=10_000.0 * MAX_CAPITAL_RISK_PCT))
    assert d.approved


def test_risk_floor_overrides_optimistic_proposer(gate):
    # proposer claims $1 of risk on $16k notional; floor = 16k * (20+100)bps = $192 -> ok
    assert gate.evaluate(good(allocated_risk=1.0, notional=16_000.0, leverage=3.0, dex_qty=23.36, perp_qty=23.36)).code in ("OK", "CAPITAL_CAPACITY")
    # 17k * 120 bps = $204 > $200 -> CAPITAL_RISK regardless of the proposer's claim
    d = gate.evaluate(good(allocated_risk=1.0, notional=17_000.0, leverage=3.0, dex_qty=24.8, perp_qty=24.8))
    assert d.code == "CAPITAL_RISK"


def test_drawdown_stop_loss_halts_new_risk(gate):
    gate.update_equity(10_000.0)
    gate.update_equity(9_800.0)  # -2 % -> WARN, still trades
    assert gate.state == "WARN" and gate.evaluate(good()).approved
    dd = gate.update_equity(9_700.0)  # -3 % -> HALTED
    assert math.isclose(dd, 0.03, rel_tol=1e-9)
    assert gate.halted and gate.state == "HALTED"
    d = gate.evaluate(good())
    assert not d.approved and d.code == "HALTED_DRAWDOWN"


def test_halt_cannot_be_reset_until_recovered(gate):
    gate.update_equity(9_600.0)
    assert gate.halted
    assert gate.reset_halt() is False  # still 4 % under water
    assert gate.halted
    gate.update_equity(9_800.0)  # recovered to -2 %; halt remains sticky
    assert gate.halted
    assert gate.reset_halt() is True
    assert gate.state == "WARN" and gate.evaluate(good()).approved
    assert gate.peak_equity == 10_000.0  # peak never silently re-based


def test_rebase_peak_requires_reason(gate):
    with pytest.raises(ValueError):
        gate.rebase_peak(12_000.0, reason="")
    gate.rebase_peak(12_000.0, reason="capital top-up")
    assert gate.peak_equity == 12_000.0 and gate.state == "NORMAL"


def test_limits_cannot_be_loosened_beyond_spec():
    lim = RiskLimits(max_leverage=10.0, max_capital_risk_pct=0.5, max_drawdown_pct=0.5)
    assert lim.max_leverage == MAX_LEVERAGE
    assert lim.max_capital_risk_pct == MAX_CAPITAL_RISK_PCT
    assert lim.max_drawdown_pct == MAX_DRAWDOWN_PCT


def test_limits_can_be_tightened():
    g = BinanceRiskGate(10_000.0, limits=RiskLimits(max_leverage=1.5))
    assert not g.evaluate(good(leverage=2.0)).approved
    assert g.evaluate(good(leverage=1.5, notional=2_000.0, dex_qty=2.92, perp_qty=2.92)).approved


# --------------------------------------------------------------------------- #
# Reduce-only / unwind semantics                                              #
# --------------------------------------------------------------------------- #
def test_unverified_reduce_only_is_vetoed(gate):
    d = gate.evaluate(good(reduce_only=True, is_delta_neutral=False))
    assert d.code == "REDUCE_ONLY_UNVERIFIED"


def test_verified_unwind_passes_kill_switch_and_halt(gate):
    gate.register_position("pos-1", "BNBUSDT", 4.38, 3_000.0, 60.0, 2.0)
    gate.set_kill_switch(True)
    gate.update_equity(9_000.0)  # halted
    unwind = good(reduce_only=True, position_id="pos-1", is_delta_neutral=False, dex_side="SELL", perp_side="BUY")
    assert gate.evaluate(unwind).approved
    # an oversized or unknown unwind is NOT verified, so the kill switch/halt still block it
    assert not gate.evaluate({**unwind, "perp_qty": 5.0, "dex_qty": 5.0}).approved
    assert not gate.evaluate({**unwind, "position_id": "nope"}).approved
    gate.set_kill_switch(False)
    gate.update_equity(10_000.0)
    gate.reset_halt()
    # with the gate open again the specific veto code is surfaced
    assert gate.evaluate({**unwind, "perp_qty": 5.0, "dex_qty": 5.0}).code == "REDUCE_ONLY_UNVERIFIED"
    assert gate.evaluate({**unwind, "position_id": "nope"}).code == "REDUCE_ONLY_UNVERIFIED"


def test_kill_switch_blocks_new_risk(gate):
    gate.set_kill_switch(True)
    assert gate.evaluate(good()).code == "KILL_SWITCH"
    gate.set_kill_switch(False)
    assert gate.evaluate(good()).approved


# --------------------------------------------------------------------------- #
# Hardening checks                                                            #
# --------------------------------------------------------------------------- #
def test_non_delta_neutral_is_vetoed(gate):
    assert gate.evaluate(good(is_delta_neutral=False)).code == "NOT_DELTA_NEUTRAL"
    # the flag alone is not enough: legs must be BUY dex / SELL perp
    assert gate.evaluate(good(dex_side="SELL")).code == "NOT_DELTA_NEUTRAL"
    assert gate.evaluate(good(perp_side="BUY")).code == "NOT_DELTA_NEUTRAL"


@pytest.mark.parametrize(
    "bad",
    [
        {"leverage": "2"},
        {"leverage": float("nan")},
        {"leverage": 0},
        {"leverage": -1},
        {"leverage": True},
        {"allocated_risk": None},
        {"allocated_risk": -5},
        {"notional": None},
        {"notional": 0},
        {"notional": float("inf")},
        {"dex_qty": 0},
        {"perp_qty": "4"},
    ],
)
def test_malformed_fields_fail_closed(gate, bad):
    d = gate.evaluate(good(**bad))
    assert not d.approved and d.code == "MALFORMED"


def test_missing_fields_fail_closed(gate):
    d = gate.evaluate({"symbol": "BNBUSDT", "is_delta_neutral": True})
    assert not d.approved and d.code == "MALFORMED"


def test_hedge_leg_mismatch_is_vetoed(gate):
    d = gate.evaluate(good(dex_qty=4.38, perp_qty=4.20))
    assert d.code == "HEDGE_MISMATCH"
    # one LOT_SIZE step of rounding is always tolerated
    assert gate.evaluate(good(dex_qty=4.389, perp_qty=4.38)).approved
    # 0.5 % tolerance on large sizes
    assert gate.evaluate(good(dex_qty=100.4, perp_qty=100.0, notional=3_000.0)).approved


def test_capital_capacity_for_both_legs(gate):
    # notional 6000 at 2x needs 6000 + 3000 = 9000 = 90 % of equity -> boundary OK
    assert gate.evaluate(good(notional=6_000.0, allocated_risk=100.0, dex_qty=8.76, perp_qty=8.76)).approved
    d = gate.evaluate(good(notional=6_001.0, allocated_risk=100.0, dex_qty=8.76, perp_qty=8.76))
    assert d.code == "CAPITAL_CAPACITY"


def test_capital_capacity_counts_open_positions(gate):
    gate.register_position("p1", "BNBUSDT", 4.38, 3_000.0, 60.0, 2.0)  # uses 4500
    assert gate.evaluate(good(notional=3_000.0)).approved  # 4500 + 4500 = 9000 OK
    d = gate.evaluate(good(notional=3_001.0))
    assert d.code == "CAPITAL_CAPACITY"


def test_aggregate_risk_budget(gate):
    # budget = 3 % of 10k = $300 with zero drawdown; each trade risks ≥ $36 (3k * 120 bps)
    gate.register_position("p1", "BNBUSDT", 4.38, 3_000.0, 150.0, 2.0)
    gate.register_position("p2", "BNBUSDT", 1.0, 700.0, 140.0, 2.0)
    d = gate.evaluate(good(notional=1_000.0, allocated_risk=12.0, dex_qty=1.46, perp_qty=1.46))
    assert d.code == "AGGREGATE_RISK"
    gate.release_position("p2")
    assert gate.evaluate(good(notional=1_000.0, allocated_risk=12.0, dex_qty=1.46, perp_qty=1.46)).approved


def test_aggregate_risk_budget_shrinks_with_drawdown(gate):
    gate.update_equity(9_800.0)  # 2 % dd -> remaining budget 1 % of 9800 = $98
    d = gate.evaluate(good(notional=3_000.0, allocated_risk=99.0))
    assert d.code == "AGGREGATE_RISK"


def test_aggregate_notional_cap():
    g = BinanceRiskGate(10_000.0, limits=RiskLimits(max_capital_utilization=5.0, max_notional_usd=100_000.0))
    g.register_position("p1", "BNBUSDT", 40.0, 28_000.0, 10.0, 3.0)
    d = g.evaluate(good(notional=3_000.0, leverage=3.0, allocated_risk=10.0))
    assert d.code == "AGGREGATE_NOTIONAL"


def test_min_and_max_notional(gate):
    assert gate.evaluate(good(notional=4.99, allocated_risk=0.1, dex_qty=0.007, perp_qty=0.007)).code == "MIN_NOTIONAL"
    g = BinanceRiskGate(1_000_000.0, limits=RiskLimits(max_notional_usd=5_000.0))
    assert g.evaluate(good(notional=5_001.0, allocated_risk=100.0, dex_qty=7.3, perp_qty=7.3)).code == "MAX_NOTIONAL"


def test_symbol_whitelist():
    g = BinanceRiskGate(10_000.0, allowed_symbols=["BNBUSDT"])
    assert g.evaluate(good()).approved
    assert g.evaluate(good(symbol="DOGEUSDT")).code == "SYMBOL_NOT_ALLOWED"
    assert g.evaluate(good(symbol="bnbusdt")).approved  # case-insensitive


def test_max_open_positions_is_gate_owned(gate):
    lim = RiskLimits(max_open_positions=2, max_capital_utilization=5.0)
    g = BinanceRiskGate(100_000.0, limits=lim)
    g.register_position("a", "BNBUSDT", 1.0, 700.0, 10.0, 2.0)
    g.register_position("b", "BNBUSDT", 1.0, 700.0, 10.0, 2.0)
    d = g.evaluate(good(open_positions=0))  # caller-supplied count is ignored
    assert d.code == "MAX_POSITIONS"


def test_caller_cannot_rescale_limits_via_proposal(gate):
    # portfolio_balance / open_positions in the proposal are ignored
    d = gate.evaluate(good(allocated_risk=250.0, portfolio_balance=1_000_000.0))
    assert d.code == "CAPITAL_RISK"


def test_stale_quote_is_vetoed(gate):
    assert gate.evaluate(good(quote_age_ms=5_001)).code == "STALE_QUOTE"
    assert gate.evaluate(good(quote_age_ms=4_999)).approved


def test_price_drift_is_vetoed(gate):
    assert gate.evaluate(good(price_drift_bps=20.1)).code == "PRICE_DRIFT"
    assert gate.evaluate(good(price_drift_bps=-20.1)).code == "PRICE_DRIFT"
    assert gate.evaluate(good(price_drift_bps=19.9)).approved


def test_negative_edge_is_vetoed(gate):
    assert gate.evaluate(good(expected_edge_bps=-0.1)).code == "NEGATIVE_EDGE"
    assert gate.evaluate(good(expected_edge_bps=0.0)).approved


def test_explain_marks_failing_check_false_and_stops(gate):
    d = gate.explain(good(leverage=5.0))
    names = [c.name for c in d.checks]
    assert names[-1] == "LEVERAGE" and d.checks[-1].passed is False
    assert d.checks[-1].observed == 5.0 and d.checks[-1].limit == 3.0
    assert all(c.passed for c in d.checks[:-1])
    assert "CAPITAL_RISK" not in names  # evaluation short-circuits


def test_snapshot_shape(gate):
    s = gate.snapshot()
    for k in ("equity", "drawdown_pct", "state", "halted", "kill_switch", "limits", "check_order", "positions"):
        assert k in s
    assert s["check_order"] == list(CHECK_ORDER)


def test_invalid_constructor_args():
    with pytest.raises(ValueError):
        BinanceRiskGate(0)
    with pytest.raises(ValueError):
        BinanceRiskGate(float("nan"))


def test_persistence_survives_restart(tmp_path):
    path = str(tmp_path / "gate.json")
    g = BinanceRiskGate(10_000.0, mode="paper", state_path=path)
    g.register_position("p1", "BNBUSDT", 4.38, 3_000.0, 60.0, 2.0)
    g.update_equity(9_600.0)  # halted
    g.set_kill_switch(True)
    # "restart"
    g2 = BinanceRiskGate(10_000.0, mode="paper", state_path=path)
    assert g2.halted and g2.kill_switch and g2.peak_equity == 10_000.0 and g2.equity == 9_600.0
    assert "p1" in g2.positions
    assert g2.evaluate(good()).code == "KILL_SWITCH"
    # a different mode never inherits paper state
    g3 = BinanceRiskGate(10_000.0, mode="testnet", state_path=path)
    assert not g3.halted and not g3.kill_switch and g3.positions == {}


def test_determinism_hash(gate):
    proposals = [good(), good(leverage=5.0), good(allocated_risk=500.0), good(quote_age_ms=9_000)]
    def run():
        g = BinanceRiskGate(10_000.0)
        out = [(d.code, d.reason) for d in (g.evaluate(p) for p in proposals)]
        return hashlib.sha256(json.dumps(out).encode()).hexdigest()
    assert run() == run()


# --------------------------------------------------------------------------- #
# Micro-benchmark: the "microsecond gate"                                     #
# --------------------------------------------------------------------------- #
def test_hot_path_latency_is_microseconds(gate):
    p = good()
    gate.evaluate(p)  # warm-up
    n = 50_000
    t0 = time.perf_counter_ns()
    for _ in range(n):
        gate.evaluate(p)
    per_call_us = (time.perf_counter_ns() - t0) / n / 1_000
    samples = sorted(gate.evaluate(p).latency_ns for _ in range(10_000))
    median_us = statistics.median(samples) / 1_000
    print(f"\nrisk gate: {per_call_us:.2f} µs/call amortised, {median_us:.2f} µs median self-reported")
    # Target ≈ 1.5 µs on Apple Silicon; keep CI green on slower runners.
    assert per_call_us < 5.0, f"risk gate too slow: {per_call_us:.2f} µs"


def test_price_sanity_vetoes_junk_gaps(gate):
    # 686 vs 700 = 204 bps gap -> junk data, not edge
    d = gate.evaluate(good(dex_ref_price=686.0, perp_ref_price=700.0))
    assert d.code == "PRICE_SANITY" and d.unit == "bps"
    assert gate.evaluate(good(dex_ref_price=686.0, perp_ref_price=692.0)).approved  # 87 bps ok
    assert gate.evaluate(good(dex_ref_price=686.0, perp_ref_price=672.0)).code == "PRICE_SANITY"  # symmetric


# --------------------------------------------------------------------------- #
# Review fixes (2026-09-02): fail-closed inputs, reversed-leg unwinds, wiped book #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", ["quote_age_ms", "price_drift_bps", "expected_edge_bps", "dex_ref_price", "perp_ref_price"])
def test_missing_freshness_drift_edge_or_refs_fail_closed(gate, missing):
    p = good()
    del p[missing]
    d = gate.evaluate(p)
    assert not d.approved and d.code == "MALFORMED"
    p2 = good(**{missing: None})
    assert gate.evaluate(p2).code == "MALFORMED"


def test_unwind_must_reverse_legs_and_match_symbol(gate):
    gate.register_position("pos-1", "BNBUSDT", 4.38, 3_000.0, 60.0, 2.0)
    gate.set_kill_switch(True)
    base = good(reduce_only=True, position_id="pos-1", is_delta_neutral=False)
    # correct unwind: DEX SELL / perp BUY on the same symbol passes the kill switch
    assert gate.evaluate({**base, "dex_side": "SELL", "perp_side": "BUY"}).approved
    # same sides as the entry (would ADD exposure) is not an unwind
    assert not gate.evaluate({**base, "dex_side": "BUY", "perp_side": "SELL"}).approved
    # a different symbol cannot borrow the registry entry
    assert not gate.evaluate({**base, "dex_side": "SELL", "perp_side": "BUY", "symbol": "ETHUSDT"}).approved


def test_negative_equity_halts(gate):
    dd = gate.update_equity(-250.0)
    assert dd == 1.0 and gate.halted and gate.equity == 0.0


def test_legacy_adapter_supplies_neutral_inputs(gate):
    ok, reason = gate.evaluate_arbitrage_trade({"is_delta_neutral": True, "leverage": 2.0, "allocated_risk": 150.0})
    assert ok, reason
