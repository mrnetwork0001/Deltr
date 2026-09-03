"""Hedger — golden sizing ($5,000 @ 2x → 4.85 BNB), plan shape/TTL/hash, impact cap, unwind plan."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from agents.hedger import Hedger, SizingError, build_plan, size_hedge, unwind_plan
from agents.arbitrage_scout import build_opportunity, compute_edge
from deltr.config import Settings
from deltr.models import DataSource, DexQuote, Freshness, FundingSnapshot, MarketState, Position, Side, SymbolFilters, TraceSource, Venue

NOW_MS = 1_788_316_513_000
NEXT_FUNDING_MS = 1_788_336_000_000
TS = datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc)


# --------------------------------------------------------------------------- fakes
class FakeState:
    def __init__(self) -> None:
        self.market = None
        self.edge = None
        self.opportunity = None
        self.plans: dict = {}
        self.events: list = []

    def put_plan(self, plan):
        self.plans[plan.id] = plan

    def emit(self, topic, message, level="info", data=None):
        self.events.append((topic, message, level, data or {}))


def make_dex(size: float, exec_buy: float = 686.191, mid: float = 686.1015) -> DexQuote:
    return DexQuote(
        pool="0x172fcD41E0913e95784454622d1c3724f546f849", fee_tier=100, fee_bps=1.0, sqrt_price_x96=1, tick=0,
        mid_price=mid, size_base=size, exec_price_buy=exec_buy, amount_in_usdt=exec_buy * size,
        exec_price_sell=686.053, amount_out_usdt=686.053 * size, impact_bps=max(0.0, (exec_buy / mid - 1) * 1e4 - 1.0),
        gas_units=162_878, gas_price_wei=50_000_000, gas_usd=0.0056, block=1, ts=TS,
    )


def make_ms(rate: float = 0.0, exec_buy: float = 686.191, mark: float = 686.339, size: float = 4.86) -> MarketState:
    fund = FundingSnapshot(symbol="BNBUSDT", mark_price=mark, index_price=mark, last_funding_rate=rate,
                           next_funding_time_ms=NEXT_FUNDING_MS, interval_h=8, annualized_pct=0.0, ts=TS)
    return MarketState(symbol="BNBUSDT", dex=make_dex(size, exec_buy), funding=fund, perp_ref_price=mark,
                       freshness=Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=True), ts=TS,
                       source=DataSource.REPLAY)


@pytest.fixture
def paper() -> Settings:
    return Settings(DELTR_MODE="paper", _env_file=None)


@pytest.fixture
def testnet() -> Settings:
    return Settings(DELTR_MODE="testnet", BINANCE_API_KEY="x", BINANCE_SECRET_KEY="y", _env_file=None)


@pytest.fixture
def filters() -> SymbolFilters:
    return SymbolFilters(symbol="BNBUSDT")


# --------------------------------------------------------------------------- sizing golden (§3.5)
def test_size_hedge_golden_5000_at_2x(paper, filters):
    s = size_hedge(make_ms(), 5_000.0, 2.0, filters, paper)
    assert str(s.qty) == "4.85" and s.base_qty == 4.85
    assert math.isclose(s.notional_usd, 3_328.03, abs_tol=0.01)
    assert math.isclose(s.margin_usd, 1_664.01, abs_tol=0.01)
    assert math.isclose(s.cash_required_usd, 4_992.04, abs_tol=0.01)
    assert s.cash_required_usd <= 5_000.0 and s.capped_by == "capital"


def test_size_hedge_accepts_opportunity(paper, filters):
    ms = make_ms()
    opp = build_opportunity(ms, 4.86, 686.191 * 4.86, paper, 3.0, 72.0, now_ms=NOW_MS)
    assert size_hedge(opp, 5_000.0, 2.0, filters, paper).qty == size_hedge(ms, 5_000.0, 2.0, filters, paper).qty


def test_size_hedge_caps_at_mode_notional(paper, testnet, filters):
    big = size_hedge(make_ms(), 50_000.0, 3.0, filters, testnet)  # N = 37,500 > 5,000 TESTNET cap
    assert big.capped_by == "max_notional" and big.notional_usd <= 5_000.0
    paper_big = size_hedge(make_ms(), 50_000.0, 3.0, filters, paper)
    assert paper_big.capped_by == "capital" and paper_big.notional_usd > 37_000.0


def test_size_hedge_impact_cap_shrinks_size(paper, filters):
    def quote_at(q: float) -> DexQuote:
        # 12 bps impact above 3 BNB, 1 bps below → the loop must shrink below 3
        return make_dex(q, exec_buy=686.1015 * (1 + (13.0 if q > 3.0 else 2.0) / 1e4))

    s = size_hedge(make_ms(), 5_000.0, 2.0, filters, paper, dex_quote_at=quote_at)
    assert s.capped_by == "impact" and float(s.qty) <= 3.0 and float(s.qty) > 0


def test_size_hedge_below_min_qty(paper, filters):
    s = size_hedge(make_ms(), 5.0, 2.0, filters, paper)
    assert s.qty == 0 and s.capped_by == "min_qty"


# --------------------------------------------------------------------------- build_plan
def _plan(cfg, filters, capital=5_000.0, lev=2.0, now=None, ms=None, **kw):
    ms = ms or make_ms()
    s = size_hedge(ms, capital, lev, filters, cfg)
    e = compute_edge(ms, s.notional_usd, cfg.funding_horizon_hours, cfg, now_ms=NOW_MS)
    return build_plan(ms, s, e, cfg, "BNBUSDT", kw.pop("source", TraceSource.MCP), kw.pop("client", "claude-desktop/0.12"),
                      kw.pop("prompt", "Hedge $5,000 at 2x"), kw.pop("opportunity_id", "opp_x"), now=now), s, e


def test_plan_legs_are_dex_buy_then_perp_sell_with_equal_step_qty(paper, filters):
    plan, s, e = _plan(paper, filters)
    assert len(plan.legs) == 2
    dex, perp = plan.legs
    assert dex.venue == Venue.PANCAKESWAP_V3 and dex.side == Side.BUY and dex.leverage == 1.0 and dex.reduce_only is False
    assert perp.venue == Venue.BINANCE_FUTURES and perp.side == Side.SELL and perp.leverage == 2.0 and perp.reduce_only is False
    assert dex.qty == perp.qty == plan.qty == 4.85
    assert dex.price_hint == 686.191 and perp.price_hint == 686.339
    assert plan.ref_dex_price == 686.191 and plan.ref_perp_price == 686.339
    assert plan.reduce_only is False and plan.position_id is None
    assert plan.source == TraceSource.MCP and plan.client == "claude-desktop/0.12" and plan.prompt == "Hedge $5,000 at 2x"
    assert plan.opportunity_id == "opp_x"


def test_plan_cash_and_risk_numbers(paper, filters):
    plan, s, e = _plan(paper, filters)
    assert math.isclose(plan.notional_usd, 3_328.03, abs_tol=0.01)
    assert math.isclose(plan.margin_usd, plan.notional_usd / 2.0)
    assert math.isclose(plan.cash_required_usd, plan.notional_usd + plan.margin_usd)
    assert math.isclose(plan.allocated_risk_usd, e.allocated_risk_usd)
    assert 38.0 < plan.allocated_risk_usd < 39.5  # ≈ $38.8 = 3,328 × (16.64 + 100) / 1e4
    assert math.isclose(plan.expected_edge_bps, e.net_edge_bps) and math.isclose(plan.roundtrip_cost_bps, e.roundtrip_cost_bps)


def test_plan_ttl_is_settings_plan_ttl(paper, filters):
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    plan, _, _ = _plan(paper, filters, now=now)
    assert plan.created_at == now
    assert plan.expires_at == now + timedelta(seconds=paper.plan_ttl_seconds) == now + timedelta(seconds=60)
    cfg30 = Settings(DELTR_MODE="paper", DELTR_PLAN_TTL_SECONDS=30, _env_file=None)
    plan30, _, _ = _plan(cfg30, filters, now=now)
    assert plan30.expires_at == now + timedelta(seconds=30)


def test_plan_hash_is_stable_and_content_bound(paper, filters):
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    a, _, _ = _plan(paper, filters, now=now)
    b, _, _ = _plan(paper, filters, now=now)
    assert a.plan_hash and a.plan_hash == b.plan_hash and len(a.plan_hash) == 64
    c, _, _ = _plan(paper, filters, capital=4_000.0, now=now)
    assert c.plan_hash != a.plan_hash
    assert a.id != b.id  # ids are unique even when the hash matches


# --------------------------------------------------------------------------- unwind_plan
def _position(plan) -> Position:
    return Position(
        plan_id=plan.id, symbol="BNBUSDT", dex_qty=plan.qty, dex_entry=686.191, perp_qty=plan.qty, perp_entry=686.339,
        leverage=plan.leverage, margin_usd=plan.margin_usd, notional_usd=plan.notional_usd,
        allocated_risk_usd=plan.allocated_risk_usd, basis_entry_bps=2.16, opened_at=TS,
    )


def test_unwind_plan_reverses_sides_reduce_only_with_position_id(paper, filters):
    plan, _, _ = _plan(paper, filters)
    pos = _position(plan)
    u = unwind_plan(pos, make_ms(), paper, TraceSource.MCP, "claude-desktop/0.12", "position_stop")
    assert u.reduce_only is True and u.position_id == pos.id
    dex, perp = u.legs
    assert dex.venue == Venue.PANCAKESWAP_V3 and dex.side == Side.SELL and dex.reduce_only is True and dex.qty == pos.dex_qty
    assert perp.venue == Venue.BINANCE_FUTURES and perp.side == Side.BUY and perp.reduce_only is True and perp.qty == pos.perp_qty
    assert perp.leverage == pos.leverage
    assert u.qty == pos.perp_qty and u.symbol == "BNBUSDT"
    assert u.cash_required_usd == 0.0 and u.margin_usd == pos.margin_usd and u.allocated_risk_usd == pos.allocated_risk_usd
    assert u.ref_dex_price == 686.053 and u.ref_perp_price == 686.339  # DEX sell exec, perp mark
    assert u.prompt == "unwind:position_stop" and u.source == TraceSource.MCP
    assert u.expires_at - u.created_at == timedelta(seconds=paper.plan_ttl_seconds)
    assert u.plan_hash and u.plan_hash != plan.plan_hash


def test_unwind_plan_without_market_data_falls_back_to_entries(paper, filters):
    plan, _, _ = _plan(paper, filters)
    pos = _position(plan)
    bare = MarketState(symbol="BNBUSDT", freshness=Freshness(cex_age_ms=0, dex_age_ms=0, spot_age_ms=0, ok=False, reason="dex_stale"), ts=TS)
    u = unwind_plan(pos, bare, paper, TraceSource.AUTO, None, "kill")
    assert u.ref_dex_price == pos.dex_entry and u.ref_perp_price == pos.perp_entry
    assert u.roundtrip_cost_bps == 20.0


# --------------------------------------------------------------------------- Hedger.propose
async def test_propose_golden_and_stores_plan(paper, filters):
    st = FakeState()
    st.market = make_ms()
    h = Hedger(st, paper, filters)
    plan = await h.propose(5_000.0, 2.0, None, TraceSource.MCP, client="claude-desktop/0.12", prompt="Hedge $5,000 at 2x")
    assert plan.qty == 4.85 and plan.symbol == "BNBUSDT" and plan.leverage == 2.0
    assert math.isclose(plan.notional_usd, 3_328.03, abs_tol=0.01)
    assert math.isclose(plan.margin_usd, 1_664.01, abs_tol=0.01)
    assert math.isclose(plan.cash_required_usd, 4_992.04, abs_tol=0.01)
    assert st.plans[plan.id] is plan
    assert [e[0] for e in st.events] == ["plan"]
    assert st.events[0][3]["capped_by"] == "capital" and st.events[0][3]["plan_id"] == plan.id


async def test_propose_defaults_leverage_and_symbol_and_links_opportunity(paper, filters):
    st = FakeState()
    st.market = make_ms()
    st.opportunity = build_opportunity(st.market, 4.86, 686.191 * 4.86, paper, 3.0, 72.0, now_ms=NOW_MS)
    plan = await Hedger(st, paper, filters).propose(5_000.0, None, None, TraceSource.UI)
    assert plan.leverage == paper.default_leverage == 2.0
    assert plan.symbol == paper.symbol
    assert plan.opportunity_id == st.opportunity.id
    assert plan.legs[1].leverage == 2.0


async def test_propose_requotes_dex_at_final_qty(paper, filters):
    st = FakeState()
    st.market = make_ms()
    seen: list[float] = []

    async def quote_at(q: float) -> DexQuote:
        seen.append(q)
        return make_dex(q, exec_buy=686.30)  # a slightly worse re-quote at the exact size

    plan = await Hedger(st, paper, filters, dex_quote_at=quote_at).propose(5_000.0, 2.0, "BNBUSDT", TraceSource.API)
    assert seen == [4.85]
    assert plan.ref_dex_price == 686.30 and plan.legs[0].price_hint == 686.30
    assert math.isclose(plan.notional_usd, 4.85 * 686.30)
    assert math.isclose(plan.cash_required_usd, plan.notional_usd * 1.5)


async def test_propose_sync_quote_drives_impact_cap(paper, filters):
    st = FakeState()
    st.market = make_ms()

    def quote_at(q: float) -> DexQuote:
        return make_dex(q, exec_buy=686.1015 * (1 + (13.0 if q > 3.0 else 2.0) / 1e4))

    plan = await Hedger(st, paper, filters, dex_quote_at=quote_at).propose(5_000.0, 2.0, None, TraceSource.CLI)
    assert plan.qty <= 3.0 and st.events[0][3]["capped_by"] == "impact"


async def test_propose_failed_requote_falls_back_to_tick_quote(paper, filters):
    st = FakeState()
    st.market = make_ms()

    async def boom(q: float) -> DexQuote:
        raise RuntimeError("rpc down")

    plan = await Hedger(st, paper, filters, dex_quote_at=boom).propose(5_000.0, 2.0, None, TraceSource.AUTO)
    assert plan.ref_dex_price == 686.191 and plan.qty == 4.85


async def test_propose_errors(paper, filters):
    st = FakeState()
    h = Hedger(st, paper, filters)
    with pytest.raises(ValueError):
        await h.propose(5_000.0, 2.0, None, TraceSource.UI)  # no market yet
    st.market = make_ms()
    with pytest.raises(SizingError):
        await h.propose(5.0, 2.0, None, TraceSource.UI)  # below min qty
    with pytest.raises(SizingError):
        await h.propose(0.0, 2.0, None, TraceSource.UI)


async def test_propose_testnet_cap_shows_in_plan(testnet, filters):
    st = FakeState()
    st.market = make_ms()
    plan = await Hedger(st, testnet, filters).propose(50_000.0, 3.0, None, TraceSource.MCP)
    assert plan.notional_usd <= 5_000.0 and st.events[0][3]["capped_by"] == "max_notional"


def test_agents_import_no_venue_or_router_symbols():
    """Choke-point rule: the scout/hedger never import venue clients or routers."""
    import ast
    from pathlib import Path

    for name in ("arbitrage_scout.py", "hedger.py"):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("agents", name).read_text())
        for node in ast.walk(tree):
            mods = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            for m in mods:
                assert not m.startswith("deltr.venues"), f"{name} imports {m}"
                assert m not in ("deltr.executor", "risk_gate"), f"{name} imports {m}"
