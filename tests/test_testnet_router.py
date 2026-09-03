"""TestnetRouter against a scripted FakeFutures: LIMIT IOC only, client-id scheme, query-before-retry,
zero-fill retry band, partial fills, error map, reference divergence, host assertion, simulated DEX leg."""
from __future__ import annotations

import re

import pytest

from deltr.edge import round_to_tick
from deltr.executor import LegError, PaperRouter, TestnetRouter, client_order_id
from deltr.models import DataSource, Fill, OrderLeg, Side, Venue
from deltr.portfolio import StressFlags
from tests._fakes_d import FILTERS, FakeDex, FakeFutures, make_market, make_plan, make_settings

CID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


def _router(tmp_path, futures, dex=None):
    settings = make_settings(tmp_path, "testnet")
    r = TestnetRouter(futures, dex, settings, FILTERS)
    r.backoff_ms = (0, 0, 0)
    return r, settings


def _perp_leg(plan):
    return next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES)


def test_constructor_refuses_non_testnet_host(tmp_path):
    with pytest.raises(AssertionError):
        _router(tmp_path, FakeFutures(base_url="https://fapi.binance.com"))
    r, _ = _router(tmp_path, FakeFutures())
    assert r.mode.value == "testnet"


def test_paper_router_has_no_credential_parameters():
    import inspect
    params = set(inspect.signature(PaperRouter.__init__).parameters)
    assert params == {"self", "dex", "settings", "filters"}
    assert not any("key" in p or "secret" in p for p in params)


async def test_limit_ioc_only_with_client_id_and_band(tmp_path):
    fut = FakeFutures()
    r, settings = _router(tmp_path, fut)
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert len(fut.orders) == 1
    o = fut.orders[0]
    assert o["type"] == "LIMIT" and o["timeInForce"] == "IOC" and o["side"] == Side.SELL and o["qty"] == pytest.approx(4.85)
    assert o["price"] == pytest.approx(round_to_tick(ms.perp_ref_price * (1 - 10 / 1e4), 0.01))
    assert o["client_id"] == client_order_id(plan.id, 1, 1) and CID_RE.fullmatch(o["client_id"])
    assert o["reduce_only"] is False and o["position_side"] == "BOTH"
    assert f.simulated is False and f.source == DataSource.BINANCE_FUTURES_TESTNET and f.ref == "1001"
    assert f.client_id == o["client_id"] and f.attempt == 1 and f.qty == pytest.approx(4.85)
    assert f.reference_divergence_bps == pytest.approx(1e4 * (f.price - ms.perp_ref_price) / ms.perp_ref_price)
    assert f.reference_divergence_bps < 0  # sold below mark
    assert f.fee_usd == pytest.approx(4.85 * f.price * settings.perp_taker_fee_bps / 1e4)


async def test_zero_fill_retries_once_at_wider_band(tmp_path):
    fut = FakeFutures(script=[("zero",), ("fill", 4.85)])
    r, settings = _router(tmp_path, fut)
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert len(fut.orders) == 2
    assert fut.orders[0]["price"] == pytest.approx(round_to_tick(ms.perp_ref_price * (1 - 10 / 1e4), 0.01))
    assert fut.orders[1]["price"] == pytest.approx(round_to_tick(ms.perp_ref_price * (1 - 25 / 1e4), 0.01))
    assert fut.orders[0]["client_id"] == client_order_id(plan.id, 1, 1)
    assert fut.orders[1]["client_id"] == client_order_id(plan.id, 1, 2)
    assert f.attempt == 2 and f.client_id == fut.orders[1]["client_id"]
    assert fut.lookups == []  # a clean zero fill needs no lookup


async def test_all_zero_fills_raise_leg_error(tmp_path):
    fut = FakeFutures(script=[("zero",), ("zero",), ("zero",)])
    r, _ = _router(tmp_path, fut)
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(LegError) as ei:
        await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert len(fut.orders) == 3 and ei.value.leg_index == 1 and ei.value.retryable is False


async def test_timeout_queries_by_client_id_before_retry(tmp_path):
    fut = FakeFutures(script=[("timeout", 4.85)])  # order actually filled on the exchange
    r, _ = _router(tmp_path, fut)
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    cid = client_order_id(plan.id, 1, 1)
    assert fut.lookups == [cid] and len(fut.orders) == 1  # NO blind retry
    assert f.qty == pytest.approx(4.85) and f.client_id == cid and f.ref == "1001"


async def test_timeout_unknown_then_retry_with_new_client_id(tmp_path):
    fut = FakeFutures(script=[("timeout", None), ("fill", 4.85)])
    r, _ = _router(tmp_path, fut)
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert fut.lookups == [client_order_id(plan.id, 1, 1)]
    assert [o["client_id"] for o in fut.orders] == [client_order_id(plan.id, 1, 1), client_order_id(plan.id, 1, 2)]
    assert f.attempt == 2


async def test_partial_fill_returns_executed_qty(tmp_path):
    fut = FakeFutures(script=[("fill", 4.0)])
    r, _ = _router(tmp_path, fut)
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert f.qty == pytest.approx(4.0) and len(fut.orders) == 1


async def test_error_map(tmp_path):
    ms = make_market()
    plan = make_plan(ms)
    # -1021: resync time, then retry
    fut = FakeFutures(script=[("error", -1021), ("fill", 4.85)])
    r, _ = _router(tmp_path, fut)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert fut.synced == 1 and len(fut.orders) == 2 and f.qty == pytest.approx(4.85)
    # -4061: re-read position mode, then retry
    fut = FakeFutures(script=[("error", -4061), ("fill", 4.85)])
    r, _ = _router(tmp_path, fut)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert fut.prep_calls == 1 and len(fut.orders) == 2
    # fatal codes: fail immediately, one order, no retry
    for code in (-2019, -1111, -4164, -4028, -2022):
        fut = FakeFutures(script=[("error", code)])
        r, _ = _router(tmp_path, fut)
        with pytest.raises(LegError) as ei:
            await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
        assert len(fut.orders) == 1 and ei.value.code == code and ei.value.retryable is False


async def test_dual_side_mode_uses_position_side_short_no_reduce_only(tmp_path):
    fut = FakeFutures(dual=True)
    r, _ = _router(tmp_path, fut)
    prep = await r.prepare("BNBUSDT", 2)
    assert prep.dual_side is True
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    assert fut.orders[0]["position_side"] == "SHORT" and fut.orders[0]["reduce_only"] is False
    rev = await r.reverse(f, ms, plan.id)
    assert fut.orders[1]["side"] == Side.BUY and fut.orders[1]["position_side"] == "SHORT" and fut.orders[1]["reduce_only"] is False
    assert fut.orders[1]["client_id"] == client_order_id(plan.id, 3, 1)  # reversal leg digit
    assert rev.side == Side.BUY and rev.qty == pytest.approx(4.85)


async def test_one_way_mode_reverse_is_reduce_only_buy_above_mark(tmp_path):
    fut = FakeFutures()
    r, _ = _router(tmp_path, fut)
    await r.prepare("BNBUSDT", 2)
    ms = make_market()
    plan = make_plan(ms)
    f = await r.fill(_perp_leg(plan), ms, None, plan.id, 1)
    rev = await r.reverse(f, ms, plan.id)
    o = fut.orders[1]
    assert o["side"] == Side.BUY and o["reduce_only"] is True and o["position_side"] == "BOTH"
    assert o["price"] > ms.perp_ref_price and rev.reference_divergence_bps > 0
    part = await r.reverse_partial(_perp_leg(plan), 0.85, ms, plan.id)
    assert fut.orders[2]["qty"] == pytest.approx(0.85) and fut.orders[2]["reduce_only"] is True and part.qty == pytest.approx(0.85)


async def test_dex_leg_is_simulated_and_honours_stress_flag(tmp_path):
    ms = make_market()
    fut = FakeFutures()
    dex = FakeDex(ms.dex)
    r, settings = _router(tmp_path, fut, dex)
    plan = make_plan(ms)
    dex_leg = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
    f = await r.fill(dex_leg, ms, None, plan.id, 1)
    assert f.simulated is True and f.source == DataSource.PAPER and f.ref == "paper"
    assert f.price == pytest.approx(ms.dex.exec_price_buy * (1 + settings.paper_dex_extra_slippage_bps / 1e4))
    assert f.fee_usd == pytest.approx(ms.dex.gas_usd) and dex.calls == [4.85]
    assert fut.orders == []  # never touches the exchange
    flags = StressFlags(dex_leg_fail=True, label="SIMULATED · dex_leg_fail")
    r.stress = flags
    with pytest.raises(LegError) as ei:
        await r.fill(dex_leg, ms, None, plan.id, 1)
    assert "dex_leg_fail" in ei.value.reason and flags.dex_leg_fail is False  # consumed once
    f2 = await r.fill(dex_leg, ms, None, plan.id, 2)
    assert f2.simulated


async def test_paper_router_perp_fill_uses_book_bid_within_sanity(tmp_path):
    settings = make_settings(tmp_path)
    ms = make_market(bid=686.30, ask=686.40)
    pr = PaperRouter(FakeDex(ms.dex), settings, FILTERS)
    plan = make_plan(ms)
    f = await pr.fill(_perp_leg(plan), ms, None, plan.id, 1)
    model = ms.perp_ref_price * (1 - settings.perp_slippage_bps / 1e4)
    assert f.price == pytest.approx(min(686.30, model)) and f.simulated and f.source == DataSource.PAPER
    assert f.client_id == client_order_id(plan.id, 1, 1)
    junk = make_market(bid=600.0, ask=700.0)  # > 100 bps from mark: ignored
    f2 = await pr.fill(_perp_leg(plan), junk, None, plan.id, 1)
    assert f2.price == pytest.approx(model)
    # DEX sell (unwind) is symmetric via the exact-input price
    sell = OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.SELL, qty=4.85, price_hint=0)
    f3 = await pr.fill(sell, ms, None, plan.id, 1)
    assert f3.price == pytest.approx(ms.dex.exec_price_sell * (1 - settings.paper_dex_extra_slippage_bps / 1e4))


async def test_paper_router_falls_back_to_hub_quote_when_rpc_fails(tmp_path):
    settings = make_settings(tmp_path)
    ms = make_market()
    pr = PaperRouter(FakeDex(ms.dex, fail=True), settings, FILTERS)
    plan = make_plan(ms)
    dex_leg = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
    f = await pr.fill(dex_leg, ms, None, plan.id, 1)
    assert f.price == pytest.approx(ms.dex.exec_price_buy * (1 + 1e-4))
