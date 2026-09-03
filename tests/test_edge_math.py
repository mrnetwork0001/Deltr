"""Golden numbers for deltr/edge.py (derived from live probes on 2026-09-02)."""

from __future__ import annotations

import math

import pytest

from deltr import edge


def test_floor_to_step_is_exact():
    assert edge.floor_to_step(4.8577, 0.01) == 4.85
    assert edge.floor_to_step(4.85, 0.01) == 4.85
    assert edge.floor_to_step(0.019999, 0.01) == 0.01
    assert edge.floor_to_step(1.1, 0.1) == 1.1  # no float drift (1.1/0.1 = 11.000000000000002)
    with pytest.raises(ValueError):
        edge.floor_to_step(1.0, 0)


def test_fmt_qty_is_exchange_safe():
    assert edge.fmt_qty(4.85, 2) == "4.85"
    assert edge.fmt_qty(4.857, 2) == "4.85"
    assert edge.fmt_qty(0.00001, 2) == "0.00"
    assert edge.fmt_qty(12345.0, 2) == "12345.00"


def test_sizing_golden_5000_at_2x():
    s = edge.size_from_capital(5_000.0, 2.0, 686.19, 0.01)
    assert math.isclose(s.unlevered_notional_usd, 3_333.3333, rel_tol=1e-6)
    assert s.base_qty == 4.85
    assert math.isclose(s.notional_usd, 3_328.0215, rel_tol=1e-6)
    assert math.isclose(s.margin_usd, 1_664.01075, rel_tol=1e-6)
    assert math.isclose(s.capital_required_usd, 4_992.03225, rel_tol=1e-6)
    assert s.capital_required_usd <= 5_000.0


def test_sizing_at_3x_uses_more_notional():
    s2 = edge.size_from_capital(10_000.0, 2.0, 686.19, 0.01)
    s3 = edge.size_from_capital(10_000.0, 3.0, 686.19, 0.01)
    assert s3.notional_usd > s2.notional_usd
    assert math.isclose(s3.unlevered_notional_usd, 7_500.0)


def test_gas_is_negligible_on_bsc():
    # 150k gas at 0.05 gwei with BNB at 686 → $0.0051
    g = edge.gas_usd(150_000, 50_000_000, 686.0)
    assert math.isclose(g, 0.005145, rel_tol=1e-6)
    assert edge.gas_bps(150_000, 50_000_000, 686.0, 3_333.0) < 0.02


def test_impact_excludes_pool_fee():
    # exec 686.191 vs mid 686.10 = 1.326 bps total premium; fee tier 100 = 1 bps → impact 0.326 bps
    imp = edge.impact_bps(686.191, 686.10, 100)
    assert math.isclose(imp, (686.191 - 686.10) / 686.10 * 1e4 - 1.0)
    assert edge.impact_bps(686.10, 686.10, 100) == 0.0  # never negative


def test_funding_sign_aware_and_settlement_count():
    n, fb = edge.funding_bps(0.0001, 24.0)  # 0.01 %/8h over 24h = 3 settlements
    assert n == 3 and math.isclose(fb, 3.0)
    n72, neg = edge.funding_bps(-0.0002, 72.0)  # short PAYS
    assert n72 == 9 and math.isclose(neg, -18.0)
    assert edge.settlements_in(7.9) == 0
    assert math.isclose(edge.annualized_funding_pct(0.0001), 10.95)


def test_perp_reference_half_spread():
    sell, buy = edge.perp_reference(686.339, 2.0)
    assert math.isclose(sell, 686.339 * (1 - 0.0002))
    assert math.isclose(buy, 686.339 * (1 + 0.0002))


def test_compute_edge_probe_snapshot_is_negative_when_costs_dominate():
    # Probe: dex exec buy 686.191 vs mid 686.10; mark 686.339; funding 0
    sell_ref, _ = edge.perp_reference(686.339, 2.0)
    e = edge.compute_edge(
        notional_usd=3_328.0, dex_exec_buy=686.191, dex_mid=686.10, perp_sell_ref=sell_ref,
        funding_rate=0.0, horizon_hours=72.0, dex_fee_tier=100,
        perp_slip_bps=2.0, cex_taker_bps=5.0, gas_bps_leg=0.017, basis_exit_bps=0.0,
    )
    assert math.isclose(e.basis_entry_bps, (sell_ref - 686.191) / 686.191 * 1e4)
    assert math.isclose(e.dex_fee_bps, 1.0)
    assert math.isclose(e.dex_impact_bps, (686.191 - 686.10) / 686.10 * 1e4 - 1.0)
    assert math.isclose(e.roundtrip_cost_bps, 2 * (1.0 + e.dex_impact_bps + 2.0 + 5.0 + 0.017))
    assert e.settlements == 9 and e.funding_bps_horizon == 0.0
    assert e.net_edge_bps < 0  # honest: no trade at this snapshot
    assert math.isclose(e.expected_edge_usd, e.net_edge_bps / 1e4 * 3_328.0)
    assert math.isclose(e.allocated_risk_usd, 3_328.0 * (e.roundtrip_cost_bps + 100.0) / 1e4)
    comps = e.components()
    assert comps[-1].kind == "net" and math.isclose(comps[-1].bps, e.net_edge_bps)
    assert math.isclose(sum(c.bps for c in comps[:-1]), e.net_edge_bps, abs_tol=1e-9)


def test_compute_edge_positive_when_basis_and_funding_cover_costs():
    e = edge.compute_edge(
        notional_usd=3_400.0, dex_exec_buy=680.0, dex_mid=680.0, perp_sell_ref=682.0,
        funding_rate=0.0003, horizon_hours=72.0, dex_fee_tier=100,
        perp_slip_bps=2.0, cex_taker_bps=5.0, gas_bps_leg=0.02,
    )
    # basis 29.41 bps + funding 27 bps − roundtrip 16.04 = +40.4 bps
    assert math.isclose(e.basis_entry_bps, 2.0 / 680.0 * 1e4)
    assert math.isclose(e.funding_bps_horizon, 27.0)
    assert math.isclose(e.roundtrip_cost_bps, 2 * (1.0 + 0.0 + 2.0 + 5.0 + 0.02))
    assert e.net_edge_bps > 40.0


def test_allocated_risk_floor():
    assert math.isclose(edge.allocated_risk_usd(3_333.0, 20.0), 3_333.0 * 0.012)


def test_mark_to_close_never_hides_exit_cost():
    m = edge.mark_to_close(
        dex_qty=4.85, perp_qty=4.85, dex_entry=686.19, perp_entry=686.20, dex_sell_exec=686.19, perp_buy_ref=686.20,
        funding_accrued_usd=0.0, entry_fees_usd=2.0, dex_fee_tier=100, cex_taker_bps=5.0, gas_usd_per_swap=0.005,
    )
    assert m.spread_pnl_usd == 0.0
    assert m.exit_cost_usd > 0
    assert m.net_pnl_usd < -2.0  # flat prices still lose entry + exit costs
    assert m.delta_base == 0.0


def test_mark_to_close_with_basis_convergence_and_funding():
    # bought DEX at 680, shorted perp at 682; now both at 681 → +1·q on each leg
    m = edge.mark_to_close(
        dex_qty=10.0, perp_qty=10.0, dex_entry=680.0, perp_entry=682.0, dex_sell_exec=681.0, perp_buy_ref=681.0,
        funding_accrued_usd=5.0, entry_fees_usd=1.0, dex_fee_tier=100, cex_taker_bps=5.0, gas_usd_per_swap=0.005,
    )
    assert math.isclose(m.spread_pnl_usd, 20.0)
    expected_exit = 681.0 * 10.0 * 1e-4 + 681.0 * 10.0 * 5e-4 + 0.005  # dex fee + perp taker + gas
    assert math.isclose(m.exit_cost_usd, expected_exit)
    assert math.isclose(m.net_pnl_usd, 20.0 + 5.0 - 1.0 - expected_exit)
    assert 19.0 < m.net_pnl_usd < 20.0  # exit cost is never hidden


def test_liquidation_estimate():
    liq = edge.liquidation_price_est(686.0, 2.0)
    assert 1_020.0 < liq < 1_030.0  # short at 2x liquidates ~+50 %


# --------------------------------------------------------------------------- #
# Convenience layer                                                           #
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone  # noqa: E402

from deltr.config import load_settings  # noqa: E402
from deltr.models import DataSource, DexQuote, Freshness, FundingSnapshot, MarketState, SymbolFilters  # noqa: E402


def test_sqrt_price_and_round_step():
    assert math.isclose(edge.sqrt_price_to_bnb_usdt(3026198540864043993837911404), 685.4319, rel_tol=1e-6)
    assert str(edge.round_step(4.8577, "0.01")) == "4.85"
    assert edge.round_step(1.1, 0.1) == edge.round_step("1.1", "0.1")


def test_settlements_between_counts_instants_in_window():
    now = 1_788_316_513_000  # probe time
    nxt = 1_788_336_000_000  # next funding (~5.4 h later)
    assert edge.settlements_between(now, nxt, 8, 24) == 3  # 5.4h, 13.4h, 21.4h
    assert edge.settlements_between(now, nxt, 8, 72) == 9
    assert edge.settlements_between(now, nxt, 8, 5) == 0
    assert edge.settlements_between(now, now - 1000, 8, 8) == 1  # stale next_funding rolls forward


def test_funding_bps_for_and_breakeven():
    assert math.isclose(edge.funding_bps_for(0.0001, 3), 3.0)
    assert math.isclose(edge.funding_bps_for(-0.0001, 3), -3.0)
    assert edge.breakeven_settlements(16.64 - 2.16, 0.0001) == 15
    assert edge.breakeven_settlements(10.0, 0.0) is None


def _ms(exec_buy=686.191, mid=686.1015, mark=686.339, rate=0.0):
    ts = datetime.now(timezone.utc)
    dex = DexQuote(pool="0x172fcD41E0913e95784454622d1c3724f546f849", fee_tier=100, fee_bps=1.0, sqrt_price_x96=1, tick=0,
                   mid_price=mid, size_base=4.86, exec_price_buy=exec_buy, amount_in_usdt=exec_buy * 4.86,
                   exec_price_sell=mid - 0.09, amount_out_usdt=(mid - 0.09) * 4.86, impact_bps=0.304,
                   gas_units=162_878, gas_price_wei=50_000_000, gas_usd=0.0056, block=1, ts=ts)
    fund = FundingSnapshot(symbol="BNBUSDT", mark_price=mark, index_price=mark, last_funding_rate=rate,
                           next_funding_time_ms=int(ts.timestamp() * 1000) + 3_600_000, interval_h=8,
                           annualized_pct=rate * 3 * 365 * 100, ts=ts)
    return MarketState(symbol="BNBUSDT", dex=dex, funding=fund, perp_ref_price=mark,
                       freshness=Freshness(cex_age_ms=10, dex_age_ms=10, spot_age_ms=10, ok=True), ts=ts)


def test_net_edge_golden_from_design_plan():
    cfg = load_settings()
    e = edge.net_edge(_ms(), 3_334.89, 24.0, cfg)
    assert math.isclose(e.basis_entry_bps, (686.339 - 686.191) / 686.191 * 1e4, rel_tol=1e-6)  # +2.16 bps
    assert math.isclose(e.dex_fee_bps, 1.0)
    assert math.isclose(e.dex_impact_bps, (686.191 / 686.1015 - 1) * 1e4 - 1.0, rel_tol=1e-6)  # 0.304 bps
    assert 16.5 < e.roundtrip_cost_bps < 16.8  # 2·(1 + 0.304 + 2 + 5 + 0.017) = 16.64
    assert e.settlements == 3 and e.funding_bps_horizon == 0.0
    assert -15.0 < e.net_edge_bps < -14.0  # −14.5 bps: the honest headline
    assert math.isclose(e.allocated_risk_usd, 3_334.89 * (e.roundtrip_cost_bps + 100) / 1e4)


def test_net_edge_requires_market_data():
    cfg = load_settings()
    ms = _ms()
    with pytest.raises(ValueError):
        edge.net_edge(MarketState(symbol="BNBUSDT", freshness=ms.freshness, ts=ms.ts), 1000, 24, cfg)


def test_size_for_capital_caps():
    f = SymbolFilters(symbol="BNBUSDT")
    s = edge.size_for_capital(5_000.0, 2.0, 686.191, f, 50_000.0, 5.0)
    assert str(s.qty) == "4.85" and s.capped_by == "capital"
    assert math.isclose(s.cash_required_usd, s.notional_usd * 1.5)
    s2 = edge.size_for_capital(50_000.0, 3.0, 686.191, f, 5_000.0, 5.0)  # TESTNET cap
    assert s2.capped_by == "max_notional" and s2.notional_usd <= 5_000.0
    s3 = edge.size_for_capital(5_000.0, 2.0, 686.191, f, 50_000.0, 5.0, impact_at=lambda q: 10.0 if q > 3.0 else 1.0)
    assert s3.capped_by == "impact" and float(s3.qty) <= 3.0
    s4 = edge.size_for_capital(10.0, 2.0, 686.191, f, 50_000.0, 5.0)
    assert s4.capped_by == "min_qty" and s4.qty == 0
