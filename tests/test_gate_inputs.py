"""tests/test_gate_inputs.py — the gate owns its truth.

* ``Executor.build_proposal`` is the ONLY constructor of ``TradeProposal`` (AST scan of
  deltr/, agents/ and main.py).
* Caller-supplied ``open_positions`` / ``portfolio_balance`` are ignored: a proposal that
  claims 0 open positions while 3 are registered is vetoed by ``MAX_POSITIONS``; one that
  claims a huge balance still trips ``CAPITAL_RISK`` against the gate's own equity.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import risk_gate
from risk_gate import BinanceRiskGate, RiskLimits

ROOT = Path(__file__).resolve().parents[1]
SCAN = sorted(list((ROOT / "deltr").rglob("*.py")) + list((ROOT / "agents").glob("*.py")) + [ROOT / "main.py"])


def _trade_proposal_calls() -> list[tuple[str, str]]:
    """(file, enclosing function) for every ``TradeProposal(...)`` call outside models.py."""
    out: list[tuple[str, str]] = []
    for path in SCAN:
        if path.name == "models.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                    if name == "TradeProposal":
                        out.append((str(path.relative_to(ROOT)), func.name))
    return out


def test_only_executor_build_proposal_constructs_trade_proposals():
    calls = _trade_proposal_calls()
    assert calls == [("deltr/executor.py", "build_proposal")], calls


def _proposal(**over):
    p = {
        "symbol": "BNBUSDT", "is_delta_neutral": True, "dex_side": "BUY", "perp_side": "SELL",
        "leverage": 2.0, "notional": 300.0, "allocated_risk": 3.5, "roundtrip_cost_bps": 16.6,
        "dex_qty": 0.44, "perp_qty": 0.44, "qty_step": 0.01, "quote_age_ms": 120.0, "price_drift_bps": 0.5,
        "dex_ref_price": 686.19, "perp_ref_price": 686.34, "expected_edge_bps": 5.0,
    }
    p.update(over)
    return p


def test_caller_supplied_open_positions_is_ignored_max_positions_vetoes():
    gate = BinanceRiskGate(capital_usd=10_000.0, limits=RiskLimits(max_open_positions=3), mode="paper")
    for i in range(3):
        gate.register_position(f"pos_{i}", "BNBUSDT", 0.44, 300.0, 3.5, 2.0)
    assert gate.open_positions == 3
    d = gate.explain(_proposal(open_positions=0, portfolio_balance=1e9))
    assert not d.approved and d.code == "MAX_POSITIONS" and d.observed == 3 and d.limit == 3
    mp = next(c for c in d.checks if c.name == "MAX_POSITIONS")
    assert mp.passed is False and mp.observed == 3
    gate.release_position("pos_0")
    assert gate.explain(_proposal(open_positions=0)).approved


def test_caller_supplied_portfolio_balance_is_ignored_capital_risk_vetoes():
    gate = BinanceRiskGate(capital_usd=10_000.0, mode="paper")
    d = gate.explain(_proposal(notional=25_000.0, allocated_risk=250.0, dex_qty=36.4, perp_qty=36.4, portfolio_balance=1e9))
    assert not d.approved and d.code == "CAPITAL_RISK"
    assert d.limit == pytest.approx(200.0)  # 2 % of the gate's OWN equity, not of the claimed balance
    assert gate.portfolio_balance == 10_000.0


def test_missing_leverage_is_malformed_never_assumed_1x():
    gate = BinanceRiskGate(capital_usd=10_000.0, mode="paper")
    for bad in ({"leverage": None}, {"leverage": float("nan")}, {"leverage": 0}):
        d = gate.explain(_proposal(**bad))
        assert not d.approved and d.code == "MALFORMED"


def test_gate_recomputes_delta_neutrality_from_legs():
    gate = BinanceRiskGate(capital_usd=10_000.0, mode="paper")
    d = gate.explain(_proposal(dex_side="SELL", perp_side="SELL", is_delta_neutral=True))
    assert d.code == "NOT_DELTA_NEUTRAL"
    d = gate.explain(_proposal(perp_qty=0.60))
    assert d.code == "HEDGE_MISMATCH"


def test_reduce_only_needs_a_registered_binding_before_any_bypass():
    gate = BinanceRiskGate(capital_usd=10_000.0, mode="paper")
    gate.set_kill_switch(True)
    unverified = gate.explain(_proposal(reduce_only=True, position_id="pos_ghost", dex_side="SELL", perp_side="BUY"))
    assert not unverified.approved and unverified.code == "KILL_SWITCH"
    gate.register_position("pos_real", "BNBUSDT", 0.44, 300.0, 3.5, 2.0)
    verified = gate.explain(_proposal(reduce_only=True, position_id="pos_real", dex_side="SELL", perp_side="BUY"))
    assert verified.approved and verified.code == "OK"
    too_big = gate.explain(_proposal(reduce_only=True, position_id="pos_real", dex_side="SELL", perp_side="BUY", dex_qty=0.9, perp_qty=0.9))
    assert not too_big.approved


def test_check_order_has_19_checks_and_the_engine_reports_it(tmp_path):
    assert len(risk_gate.CHECK_ORDER) == 19 and "PRICE_SANITY" in risk_gate.CHECK_ORDER
    from deltr.engine import build_engine
    from tests.conftest import make_replay_settings, refusing_http

    eng = build_engine(make_replay_settings(tmp_path), replay_path=str(make_replay_settings(tmp_path).replay_path), http=refusing_http())
    assert eng.status().check_order == list(risk_gate.CHECK_ORDER)


# --------------------------------------------------------------------------- review fixes (executor -> gate inputs)
def test_fresh_dex_age_is_normalised_to_the_gate_limit(tmp_path):
    """The DEX is fresh up to 9 s; the gate has one 5 s limit: a fresh 6.5 s DEX quote must not be STALE_QUOTE."""
    from tests._fakes_d import make_market, make_plan, make_stack

    fresh = make_stack(tmp_path, ms=make_market(ages=(100, 6_500, 150), ok=True))
    p = fresh.executor.build_proposal(make_plan(fresh.ms), fresh.ms, None)
    assert p.quote_age_ms == 5_000.0 and fresh.gate.explain(p.as_gate_input()).approved
    within = make_stack(tmp_path / "b", ms=make_market(ages=(100, 800, 150), ok=True))
    assert within.executor.build_proposal(make_plan(within.ms), within.ms, None).quote_age_ms == 800.0
    stale = make_stack(tmp_path / "c", ms=make_market(ages=(100, 9_500, 150), ok=False, reason="dex_stale"))
    d = stale.gate.explain(stale.executor.build_proposal(make_plan(stale.ms), stale.ms, None).as_gate_input())
    assert not d.approved and d.code == "STALE_QUOTE"


async def test_edge_is_re_priced_at_execution_not_copied_from_the_plan(tmp_path):
    """An adverse DEX move inside the PRICE_DRIFT band still lowers the edge the gate judges."""
    from tests._fakes_d import FakeRouter, make_market, make_plan, make_stack

    ms = make_market()
    worse = ms.dex.model_copy(update={"exec_price_buy": ms.dex.exec_price_buy * (1 + 15 / 1e4)})  # +15 bps < 20 bps drift
    s = make_stack(tmp_path, ms=ms, router=FakeRouter(requote=worse))
    plan = make_plan(ms, expected_edge=5.0)
    p = s.executor.build_proposal(plan, ms, worse)
    assert p.price_drift_bps == pytest.approx(15.0, abs=1e-6) and p.expected_edge_bps == pytest.approx(-10.0, abs=1e-6)
    s.state.put_plan(plan)
    from deltr.models import TraceSource

    r = await s.executor.execute(plan.id, True, TraceSource.API)
    assert r.status == "vetoed" and r.decision.code == "NEGATIVE_EDGE" and s.router.calls == []
    # a favourable perp move raises it symmetrically
    better = make_market(mark=ms.perp_ref_price * (1 + 10 / 1e4))
    assert s.executor.build_proposal(plan, better, ms.dex).expected_edge_bps == pytest.approx(15.0, abs=1e-6)


async def test_failed_reversal_books_a_naked_leg_and_engages_the_kill_switch(tmp_path):
    from deltr.models import Side, TraceSource, Venue
    from tests._fakes_d import FakeRouter, make_stack, open_via_execute

    DEX, PERP = Venue.PANCAKESWAP_V3, Venue.BINANCE_FUTURES
    s = make_stack(tmp_path, router=FakeRouter(fail=((PERP, Side.SELL),), fail_reverse=True))
    plan, r = await open_via_execute(s)
    assert r.status == "failed" and len(r.fills) == 1 and r.position_id
    pos = s.portfolio.get(r.position_id)
    assert pos is not None and pos.dex_qty == pytest.approx(4.85) and pos.perp_qty == 0.0 and pos.delta_base == pytest.approx(4.85)
    assert r.position_id in s.gate.positions and s.gate.kill_switch is True
    assert s.portfolio.reserved_cash_usd == 0.0 and s.portfolio.cash_usd < 10_000 - 3_000  # the leg's notional stays locked
    assert s.portfolio.equity() == pytest.approx(10_000, abs=25)
    assert s.state.portfolio.open_positions == 1
    # new entries are blocked until an operator resolves it ...
    plan2, r2 = await open_via_execute(s, qty=1.0)
    assert r2.status == "vetoed" and r2.decision.code == "KILL_SWITCH"
    # ... and the naked leg unwinds through the normal (verified reduce-only) path even under the kill switch
    s.router.fail.clear()
    s.router.fail_reverse = False
    s.router.calls.clear()
    u = await s.executor.unwind(pos.id, "operator", TraceSource.API)
    assert u.status == "unwound" and [c[:3] for c in s.router.calls] == [("fill", DEX, Side.SELL)]
    assert s.portfolio.positions("open") == [] and s.gate.positions == {} and s.portfolio.reserved_cash_usd == 0.0
    assert s.portfolio.cash_usd == pytest.approx(10_000, abs=25)


async def test_half_closed_unwind_books_the_perp_leg_and_keeps_the_dex_leg(tmp_path):
    from deltr.models import Side, TraceSource, Venue
    from tests._fakes_d import make_stack, open_via_execute

    DEX, PERP = Venue.PANCAKESWAP_V3, Venue.BINANCE_FUTURES
    s = make_stack(tmp_path)
    plan, r = await open_via_execute(s)
    s.router.fail.add((DEX, Side.SELL))
    s.router.fail_reverse = True
    u = await s.executor.unwind(r.position_id, "operator", TraceSource.API)
    assert u.status == "failed" and len(u.fills) == 1 and u.fills[0].venue == PERP
    pos = s.portfolio.get(r.position_id)
    assert pos is not None and pos.perp_qty == 0.0 and pos.dex_qty == pytest.approx(4.85) and pos.margin_usd == 0.0
    assert pos.delta_base == pytest.approx(4.85) and s.gate.positions[pos.id].base_qty == pytest.approx(4.85) and s.gate.kill_switch
    assert s.portfolio.equity() == pytest.approx(10_000, abs=25)
    s.router.fail.clear()
    u2 = await s.executor.unwind(pos.id, "operator", TraceSource.API)
    assert u2.status == "unwound" and s.portfolio.positions("open") == [] and s.gate.positions == {}
