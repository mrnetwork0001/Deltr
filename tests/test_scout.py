"""Arbitrage Scout — golden edge numbers, actionable-reason ordering, min-edge floor, funding window, history."""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone

import pytest

from agents.arbitrage_scout import (
    MIN_EDGE_CEILING_BPS,
    ArbitrageScout,
    actionable_reason,
    build_opportunity,
    compute_edge,
    funding_window_block,
)
from deltr.config import Settings
from deltr.models import DataSource, DexQuote, Freshness, FundingSnapshot, MarketState, SymbolFilters

NOW_MS = 1_788_316_513_000  # probe instant 2026-09-02
NEXT_FUNDING_MS = 1_788_336_000_000  # ~5.4 h later


# --------------------------------------------------------------------------- fixtures / fakes
class FakeState:
    """Only the attributes the scout touches (agent B's deltr/state.py may land later)."""

    def __init__(self, min_edge_bps: float = 3.0) -> None:
        self.market = None
        self.edge = None
        self.opportunity = None
        self.history = deque(maxlen=600)
        self.min_edge_bps = min_edge_bps
        self.events: list[tuple[str, str, str, dict]] = []

    def emit(self, topic, message, level="info", data=None):
        self.events.append((topic, message, level, data or {}))


def make_ms(
    exec_buy: float = 686.191, mid: float = 686.1015, mark: float = 686.339, rate: float = 0.0,
    fresh: bool = True, fresh_reason: str | None = None, dex_age_ms: int = 10, cex_age_ms: int = 10,
    next_funding_ms: int = NEXT_FUNDING_MS, size: float = 4.86, gas_units: int = 162_878,
) -> MarketState:
    ts = datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc)
    dex = DexQuote(
        pool="0x172fcD41E0913e95784454622d1c3724f546f849", fee_tier=100, fee_bps=1.0, sqrt_price_x96=1, tick=0,
        mid_price=mid, size_base=size, exec_price_buy=exec_buy, amount_in_usdt=exec_buy * size,
        exec_price_sell=mid - 0.09, amount_out_usdt=(mid - 0.09) * size,
        impact_bps=max(0.0, (exec_buy / mid - 1) * 1e4 - 1.0), gas_units=gas_units, gas_price_wei=50_000_000,
        gas_usd=0.0056, block=1, ts=ts,
    )
    fund = FundingSnapshot(
        symbol="BNBUSDT", mark_price=mark, index_price=mark, last_funding_rate=rate,
        next_funding_time_ms=next_funding_ms, interval_h=8, annualized_pct=rate * 3 * 365 * 100, ts=ts,
    )
    return MarketState(
        symbol="BNBUSDT", dex=dex, funding=fund, perp_ref_price=mark,
        freshness=Freshness(cex_age_ms=cex_age_ms, dex_age_ms=dex_age_ms, spot_age_ms=10, ok=fresh, reason=fresh_reason),
        ts=ts, source=DataSource.REPLAY,
    )


@pytest.fixture
def paper() -> Settings:
    return Settings(DELTR_MODE="paper", _env_file=None)


@pytest.fixture
def testnet() -> Settings:
    return Settings(DELTR_MODE="testnet", BINANCE_API_KEY="x", BINANCE_SECRET_KEY="y", _env_file=None)


@pytest.fixture
def filters() -> SymbolFilters:
    return SymbolFilters(symbol="BNBUSDT")


# --------------------------------------------------------------------------- compute_edge golden (§3.4)
def test_compute_edge_probe_golden(paper):
    e = compute_edge(make_ms(), 3_334.89, 24.0, paper, now_ms=NOW_MS)
    assert math.isclose(e.basis_entry_bps, (686.339 - 686.191) / 686.191 * 1e4, rel_tol=1e-6)  # +2.16 bps
    assert math.isclose(e.dex_fee_bps, 1.0)
    assert math.isclose(e.dex_impact_bps, (686.191 / 686.1015 - 1) * 1e4 - 1.0, rel_tol=1e-6)  # 0.304
    assert 16.5 < e.roundtrip_cost_bps < 16.8  # 16.64
    assert e.settlements == 3 and e.funding_bps_horizon == 0.0
    assert -15.0 < e.net_edge_bps < -14.0  # the honest headline: −14.5 bps
    assert math.isclose(e.allocated_risk_usd, 3_334.89 * (e.roundtrip_cost_bps + 100) / 1e4)


def test_compute_edge_72h_counts_nine_settlements(paper):
    e = compute_edge(make_ms(rate=0.0001), 3_334.89, 72.0, paper, now_ms=NOW_MS)
    assert e.settlements == 9
    assert math.isclose(e.funding_bps_horizon, 9.0)


def test_negative_funding_shows_as_cost(paper):
    """Funding sign discipline: a negative rate means the short PAYS → negative component, lower net."""
    pos = compute_edge(make_ms(rate=0.0002), 3_334.89, 24.0, paper, now_ms=NOW_MS)
    neg = compute_edge(make_ms(rate=-0.0002), 3_334.89, 24.0, paper, now_ms=NOW_MS)
    assert math.isclose(neg.funding_bps_horizon, -6.0) and math.isclose(pos.funding_bps_horizon, 6.0)
    assert neg.net_edge_bps < pos.net_edge_bps
    comp = {c.label: c for c in neg.components()}
    fund = next(c for label, c in comp.items() if label.startswith("Funding"))
    assert fund.kind == "cost" and fund.bps < 0


# --------------------------------------------------------------------------- actionable_reason ordering
def _edge(paper, **kw):
    return compute_edge(make_ms(**kw), 3_334.89, 72.0, paper, now_ms=NOW_MS)


def test_actionable_reason_order_freshness_first(paper):
    e = _edge(paper, exec_buy=600.0, mid=600.0)  # absurd basis → sanity AND impact would fail too
    fr = Freshness(cex_age_ms=10, dex_age_ms=9000, spot_age_ms=10, ok=False, reason="dex_stale")
    assert actionable_reason(e, fr, 3.0, 100.0, 5.0, True) == "stale: dex_age 9000ms"
    fr2 = Freshness(cex_age_ms=6000, dex_age_ms=10, spot_age_ms=10, ok=False, reason="cex_stale")
    assert actionable_reason(e, fr2, 3.0, 100.0, 5.0, True) == "stale: cex_age 6000ms"
    fr3 = Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=False, reason="feed_stale(stress)")
    assert actionable_reason(e, fr3, 3.0, 100.0, 5.0, False) == "stale: feed_stale(stress)"


def test_actionable_reason_price_sanity_before_impact(paper):
    e = _edge(paper, exec_buy=600.0, mid=590.0)  # basis +1439 bps, impact ~168 bps
    fresh = Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=True)
    r = actionable_reason(e, fresh, 3.0, 100.0, 5.0, True)
    assert r.startswith("price_sanity ") and r.endswith(" bps")


def test_actionable_reason_impact_before_funding_window(paper):
    e = _edge(paper, exec_buy=686.8, mid=686.1)  # impact ≈ 9.2 bps > 5, basis −6.7 bps (sane)
    fresh = Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=True)
    r = actionable_reason(e, fresh, 3.0, 100.0, 5.0, True)
    assert r.startswith("impact ") and "> 5.0 bps" in r


def test_actionable_reason_funding_window_before_min_edge(paper):
    e = _edge(paper)
    fresh = Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=True)
    assert actionable_reason(e, fresh, 3.0, 100.0, 5.0, True) == "funding_window"
    assert actionable_reason(e, fresh, 3.0, 100.0, 5.0, False).startswith("net_edge ")


def test_actionable_reason_net_edge_and_ok(paper):
    e = _edge(paper)  # ≈ −14.5 bps
    fresh = Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=True)
    r = actionable_reason(e, fresh, 3.0, 100.0, 5.0, False)
    assert r == f"net_edge {e.net_edge_bps:.1f} < 3.0 bps"
    assert actionable_reason(e, fresh, -50.0, 100.0, 5.0, False) == "ok"


# --------------------------------------------------------------------------- funding window rule
def test_funding_window_blocks_only_negative_rate_within_15_min():
    soon = NOW_MS + 10 * 60_000
    later = NOW_MS + 20 * 60_000
    assert funding_window_block(make_ms(rate=-0.0001, next_funding_ms=soon).funding, NOW_MS) is True
    assert funding_window_block(make_ms(rate=-0.0001, next_funding_ms=later).funding, NOW_MS) is False
    assert funding_window_block(make_ms(rate=0.0001, next_funding_ms=soon).funding, NOW_MS) is False
    assert funding_window_block(make_ms(rate=0.0, next_funding_ms=soon).funding, NOW_MS) is False
    assert funding_window_block(None, NOW_MS) is False
    # stale next_funding rolls forward by the interval (8 h) → not within the window
    assert funding_window_block(make_ms(rate=-0.0001, next_funding_ms=NOW_MS - 1000).funding, NOW_MS) is False


def test_build_opportunity_funding_window_never_actionable_even_with_huge_edge(paper):
    ms = make_ms(exec_buy=680.0, mid=680.0, mark=682.0, rate=-0.0001, next_funding_ms=NOW_MS + 5 * 60_000)
    opp = build_opportunity(ms, 4.86, 3_334.89, paper, min_edge_bps=-50.0, horizon_h=72.0, now_ms=NOW_MS)
    assert opp.is_actionable is False and opp.reason == "funding_window"
    # same tick with the settlement 20 min away is fine
    ms2 = make_ms(exec_buy=680.0, mid=680.0, mark=682.0, rate=-0.0001, next_funding_ms=NOW_MS + 20 * 60_000)
    opp2 = build_opportunity(ms2, 4.86, 3_334.89, paper, min_edge_bps=-50.0, horizon_h=72.0, now_ms=NOW_MS)
    assert opp2.is_actionable is True and opp2.reason == "ok"


def test_build_opportunity_requires_dex_and_funding(paper):
    ms = make_ms()
    bare = MarketState(symbol="BNBUSDT", freshness=ms.freshness, ts=ms.ts)
    with pytest.raises(ValueError):
        build_opportunity(bare, 1.0, 686.0, paper, 3.0, 72.0)


# --------------------------------------------------------------------------- ArbitrageScout
async def test_on_tick_stores_edge_opportunity_history_and_emits(paper, filters):
    st = FakeState()
    scout = ArbitrageScout(st, paper, filters)
    ms = make_ms()
    opp = await scout.on_tick(ms)
    assert st.market is ms and st.edge is opp.edge and st.opportunity is opp
    assert opp.size_base == 4.86 and math.isclose(opp.notional_usd, 686.191 * 4.86)
    assert opp.horizon_h == paper.funding_horizon_hours == 72.0
    assert opp.min_edge_bps_used == 3.0
    assert opp.is_actionable is False and opp.reason.startswith("net_edge ")
    assert len(st.history) == 1
    sp = st.history[0]
    assert sp.dex_exec == 686.191 and sp.perp_ref == 686.339 and sp.source == DataSource.REPLAY
    assert math.isclose(sp.net_edge_bps, opp.edge.net_edge_bps) and sp.actionable is False
    topics = [e[0] for e in st.events]
    assert topics == ["scan"]
    assert st.events[0][3]["actionable"] is False


async def test_history_is_bounded_and_history_n(paper, filters):
    st = FakeState()
    scout = ArbitrageScout(st, paper, filters)
    for _ in range(650):
        await scout.on_tick(make_ms())
    assert len(st.history) == 600
    assert len(scout.history(10)) == 10
    assert len(scout.history()) == 600


async def test_on_tick_incomplete_market_reissues_non_actionable(paper, filters):
    st = FakeState()
    scout = ArbitrageScout(st, paper, filters)
    ms = make_ms()
    bare = MarketState(symbol="BNBUSDT", freshness=ms.freshness, ts=ms.ts)
    with pytest.raises(ValueError):
        await scout.on_tick(bare)  # nothing to fall back on yet
    await scout.on_tick(make_ms(exec_buy=680.0, mid=680.0, mark=682.0, rate=0.0003))
    assert st.opportunity.is_actionable is True
    opp = await scout.on_tick(bare)
    assert opp.is_actionable is False and opp.reason.startswith("stale:")
    assert len(st.history) == 1  # no chart point for a broken tick


async def test_scan_overrides_do_not_mutate_state(paper, filters):
    st = FakeState()
    scout = ArbitrageScout(st, paper, filters)
    await scout.on_tick(make_ms())
    base = st.opportunity
    o = scout.scan(notional_usd=1_000.0, horizon_h=24.0, min_edge_bps=-50.0)
    assert o.notional_usd == 1_000.0 and o.horizon_h == 24.0 and o.min_edge_bps_used == -50.0
    assert o.edge.settlements == 3 and o.is_actionable is True
    assert st.opportunity is base  # overrides never replace the tick opportunity


def test_scan_without_market_raises(paper, filters):
    with pytest.raises(ValueError):
        ArbitrageScout(FakeState(), paper, filters).scan()


async def test_explain_uses_capital_sizing(paper, filters):
    st = FakeState()
    scout = ArbitrageScout(st, paper, filters)
    await scout.on_tick(make_ms())
    e, sizing = scout.explain(5_000.0, 2.0, 24.0)
    assert str(sizing.qty) == "4.85" and sizing.capped_by == "capital"
    assert math.isclose(sizing.notional_usd, 4.85 * 686.191)
    assert math.isclose(e.notional_usd, sizing.notional_usd) and e.horizon_h == 24.0 and e.settlements == 3


# --------------------------------------------------------------------------- min-edge knob
async def test_set_min_edge_floor_is_zero_in_paper(paper, filters):
    st = FakeState()
    scout = ArbitrageScout(st, paper, filters)
    await scout.on_tick(make_ms())
    assert scout.min_edge_floor() == 0.0
    assert scout.set_min_edge(-10.0) == 0.0  # clamped at the PAPER floor
    assert st.min_edge_bps == 0.0
    assert scout.set_min_edge(7.5) == 7.5
    assert scout.set_min_edge(500.0) == MIN_EDGE_CEILING_BPS == 50.0
    opp = await scout.on_tick(make_ms())
    assert opp.min_edge_bps_used == 50.0


async def test_min_edge_floor_outside_paper(testnet, filters):
    """TESTNET: the floor is the measured round trip (≈ 16.64 bps); a real-order run can never target a loss."""
    st = FakeState()
    scout = ArbitrageScout(st, testnet, filters)
    assert scout.min_edge_floor() == 20.0  # gate default before the first measurement
    await scout.on_tick(make_ms())
    floor = scout.min_edge_floor()
    assert math.isclose(floor, st.edge.roundtrip_cost_bps) and 16.5 < floor < 16.8
    assert scout.set_min_edge(1.0) == floor
    assert scout.set_min_edge(0.0) == floor
    assert scout.set_min_edge(30.0) == 30.0
    assert testnet.risk_limits().min_expected_edge_bps >= 0.0


def test_scout_defaults_min_edge_from_settings_when_state_has_none(paper, filters):
    st = FakeState()
    st.min_edge_bps = None
    scout = ArbitrageScout(st, paper, filters)
    assert scout.min_edge_bps == paper.min_edge_bps == 3.0


@pytest.mark.live
@pytest.mark.skipif(__import__("os").environ.get("DELTR_LIVE_TESTS") != "1", reason="set DELTR_LIVE_TESTS=1")
def test_live_premium_index_reachable():
    import httpx

    r = httpx.get("https://testnet.binancefuture.com/fapi/v1/premiumIndex", params={"symbol": "BNBUSDT"}, timeout=10)
    assert r.status_code == 200 and "lastFundingRate" in r.json()
