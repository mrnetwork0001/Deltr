"""tests/test_mainnet_market_data.py — real mainnet market data, and the provenance that goes with it.

The claim this file defends is narrow and load-bearing: **every quote, funding snapshot
and market state carries the source it genuinely came from**, derived from the host that
answered rather than asserted by a caller. A testnet number tagged mainnet would make the
whole "no mocked data" claim worthless, so it is tested from both directions.

Offline throughout (fake clients and ``httpx.MockTransport``); one ``@pytest.mark.live``
test hits the real keyless endpoints and is skipped unless ``DELTR_LIVE_TESTS=1``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from deltr.bus import EventBus
from deltr.config import Settings
from deltr.funding_history import FUTURES_MAINNET_REST, FUTURES_TESTNET_REST, SPOT_MIRROR_REST
from deltr.market_data import (
    VENUE_DEX,
    VENUE_FUTURES,
    VENUE_FUTURES_MAINNET,
    VENUE_SPOT,
    MarketDataHub,
)
from deltr.models import DataSource, DexQuote, FundingSnapshot, Quote, SymbolFilters, Venue
from deltr.state import State
from deltr.venues.binance_spot import SpotMirror, SpotMirrorError

LIVE = os.environ.get("DELTR_LIVE_TESTS") == "1"
SYMBOL = "BNBUSDT"
T0 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
DNS_ERROR = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")


# --------------------------------------------------------------------------- fakes
class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeFutures:
    """A futures client identified only by its ``base_url``, exactly like the real one."""

    def __init__(self, clock: Clock, base_url: str, *, claimed: DataSource = DataSource.BINANCE_FUTURES_TESTNET) -> None:
        self.clock = clock
        self.base_url = base_url
        self.claimed = claimed
        self.fail: Exception | None = None
        self.calls = 0

    async def premium_index(self, symbol: str) -> FundingSnapshot:
        self.calls += 1
        if self.fail:
            raise self.fail
        ts = self.clock()
        return FundingSnapshot(
            symbol=symbol, mark_price=686.34, index_price=686.14, last_funding_rate=0.0001,
            next_funding_time_ms=int(ts.timestamp() * 1000) + 3_600_000, interval_h=8,
            annualized_pct=0.0001 * 3 * 365 * 100, ts=ts, source=self.claimed,
        )

    async def book_ticker(self, symbol: str) -> Quote:
        if self.fail:
            raise self.fail
        ts = self.clock()
        return Quote(venue=Venue.BINANCE_FUTURES, symbol=symbol, bid=686.17, ask=686.19,
                     bid_qty=10.0, ask_qty=10.0, ts=ts, source=self.claimed)


class FakeSpot:
    def __init__(self, clock: Clock, base_url: str = SPOT_MIRROR_REST) -> None:
        self.clock = clock
        self.base_url = base_url
        self.fail: Exception | None = None

    async def book_ticker(self, symbol: str) -> Quote:
        if self.fail:
            raise self.fail
        return Quote(venue=Venue.BINANCE_SPOT, symbol=symbol, bid=686.29, ask=686.31, bid_qty=5.0, ask_qty=5.0,
                     ts=self.clock(), source=DataSource.BINANCE_SPOT_MIRROR)


class FakeDex:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def dex_quote(self, size_base: float) -> DexQuote:
        ts = self.clock()
        mid = 686.10
        return DexQuote(pool="0x172fcD41E0913e95784454622d1c3724f546f849", fee_tier=100, fee_bps=1.0,
                        sqrt_price_x96=3024721431835227127309627620, tick=-65330, mid_price=mid,
                        size_base=size_base, exec_price_buy=mid * 1.00013, amount_in_usdt=mid * 1.00013 * size_base,
                        exec_price_sell=mid * 0.99988, amount_out_usdt=mid * 0.99988 * size_base, impact_bps=0.3,
                        gas_units=162_878, gas_price_wei=50_000_000, gas_usd=0.0056, block=119_463_660, ts=ts)


def build_hub(*, data_url: str = FUTURES_MAINNET_REST, claimed: DataSource = DataSource.BINANCE_FUTURES_TESTNET):
    clock = Clock()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    state = State(settings, EventBus())
    order_client = FakeFutures(clock, FUTURES_TESTNET_REST, claimed=DataSource.BINANCE_FUTURES_TESTNET)
    data_client = FakeFutures(clock, data_url, claimed=claimed)
    spot, dex = FakeSpot(clock), FakeDex(clock)
    hub = MarketDataHub(state, order_client, spot, dex, settings, SymbolFilters(symbol=SYMBOL),
                        quote_size_base=4.85, clock=clock, futures_data=data_client)
    return hub, state, clock, order_client, data_client, spot


# --------------------------------------------------------------------------- the hub reads mainnet
async def test_hub_reads_mark_funding_and_book_from_the_mainnet_feed_not_the_order_client():
    hub, _state, _clock, order_client, data_client, _spot = build_hub()
    ms = await hub.tick_once()
    assert order_client.calls == 0, "the order-routing client must not be polled for market data"
    assert data_client is not None and data_client.calls == 1
    assert hub.futures is order_client and hub.futures_data is data_client
    assert ms.perp_ref_price == pytest.approx(686.34)


async def test_every_number_from_the_mainnet_feed_is_tagged_mainnet():
    hub, state, _clock, _order, _data, _spot = build_hub()
    ms = await hub.tick_once()
    assert hub.futures_source is DataSource.BINANCE_FUTURES_MAINNET
    assert ms.source is DataSource.BINANCE_FUTURES_MAINNET
    assert ms.funding is not None and ms.funding.source is DataSource.BINANCE_FUTURES_MAINNET
    assert ms.cex_perp_book is not None and ms.cex_perp_book.source is DataSource.BINANCE_FUTURES_MAINNET
    # the spot mirror keeps its own honest tag
    assert ms.cex_spot_ref is not None and ms.cex_spot_ref.source is DataSource.BINANCE_SPOT_MIRROR
    assert ms.dex is not None and ms.dex.source is DataSource.BSC_MAINNET_CHAIN
    health = {h.name: h for h in hub.health()}
    assert VENUE_FUTURES_MAINNET in health and VENUE_FUTURES not in health
    assert health[VENUE_FUTURES_MAINNET].source is DataSource.BINANCE_FUTURES_MAINNET
    assert set(state.venues) == {VENUE_DEX, VENUE_FUTURES_MAINNET, VENUE_SPOT}


async def test_a_testnet_feed_is_never_tagged_mainnet_even_if_the_payload_claims_it():
    """The tag follows the host that answered, so a mislabelled payload is corrected downwards."""
    hub, _state, _clock, _order, data_client, _spot = build_hub(
        data_url=FUTURES_TESTNET_REST, claimed=DataSource.BINANCE_FUTURES_MAINNET
    )
    assert data_client is not None
    ms = await hub.tick_once()
    assert hub.futures_source is DataSource.BINANCE_FUTURES_TESTNET
    assert ms.source is DataSource.BINANCE_FUTURES_TESTNET
    assert ms.funding is not None and ms.funding.source is DataSource.BINANCE_FUTURES_TESTNET
    assert ms.cex_perp_book is not None and ms.cex_perp_book.source is DataSource.BINANCE_FUTURES_TESTNET
    assert [h.name for h in hub.health()] == [VENUE_DEX, VENUE_FUTURES, VENUE_SPOT]


async def test_an_unidentifiable_client_keeps_the_tag_it_shipped_with():
    """No base_url means no derivation, so the hub never invents a provenance claim."""
    clock = Clock()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    state = State(settings, EventBus())
    anonymous = FakeFutures(clock, "")  # a duck-typed client that names no host
    hub = MarketDataHub(state, anonymous, FakeSpot(clock), FakeDex(clock), settings,
                        SymbolFilters(symbol=SYMBOL), quote_size_base=4.85, clock=clock)
    ms = await hub.tick_once()
    assert hub.futures_source is None
    assert ms.source is DataSource.BINANCE_FUTURES_TESTNET
    assert ms.funding is not None and ms.funding.source is DataSource.BINANCE_FUTURES_TESTNET
    assert [h.name for h in hub.health()] == [VENUE_DEX, VENUE_FUTURES, VENUE_SPOT]


async def test_a_dns_failure_on_the_mainnet_feed_names_the_resolver_and_does_not_fall_back():
    hub, state, _clock, _order, data_client, _spot = build_hub()
    assert data_client is not None
    data_client.fail = DNS_ERROR
    ms = await hub.tick_once()
    health = state.venues[VENUE_FUTURES_MAINNET]
    assert health.ok is False
    assert "fapi.binance.com" in health.detail and "dig +short" in health.detail and "@1.1.1.1" in health.detail
    # no substitute data appears: there is simply no funding yet, so the feed reads stale
    assert ms.funding is None and ms.perp_ref_price is None
    assert ms.freshness.ok is False and ms.freshness.reason == "cex_stale"


async def test_a_non_dns_failure_still_reports_the_underlying_error():
    hub, state, _clock, _order, data_client, _spot = build_hub()
    assert data_client is not None
    data_client.fail = RuntimeError("boom")
    await hub.tick_once()
    detail = state.venues[VENUE_FUTURES_MAINNET].detail
    assert "RuntimeError" in detail and "boom" in detail


async def test_a_spot_dns_failure_also_names_the_resolver():
    hub, state, _clock, _order, _data, spot = build_hub()
    spot.fail = DNS_ERROR
    await hub.tick_once()
    detail = state.venues[VENUE_SPOT].detail
    assert "data-api.binance.vision" in detail and "dig +short" in detail


# --------------------------------------------------------------------------- the spot mirror
def test_spot_mirror_derives_its_tag_and_refuses_an_unknown_host():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    mirror = SpotMirror(http, SPOT_MIRROR_REST)
    assert mirror.source is DataSource.BINANCE_SPOT_MIRROR
    assert "data-api.binance.vision" in repr(mirror) and "binance-spot-mirror" in repr(mirror)
    for bad in ("https://api.binance.com", FUTURES_TESTNET_REST, "https://example.invalid"):
        with pytest.raises(ValueError, match="unrecognised market-data host"):
            SpotMirror(http, bad)
    with pytest.raises(ValueError):
        SpotMirror(http, "")


async def test_spot_mirror_quotes_carry_the_mirror_tag():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"symbol": SYMBOL, "bidPrice": "686.29", "askPrice": "686.31",
                                         "bidQty": "5", "askQty": "5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        q = await SpotMirror(http, SPOT_MIRROR_REST).book_ticker(SYMBOL)
    assert q.source is DataSource.BINANCE_SPOT_MIRROR and q.venue is Venue.BINANCE_SPOT
    assert q.mid == pytest.approx(686.30)


async def test_spot_mirror_dns_failure_is_actionable_in_the_error_and_in_the_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        raise DNS_ERROR

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mirror = SpotMirror(http, SPOT_MIRROR_REST)
        with pytest.raises(SpotMirrorError) as exc:
            await mirror.book_ticker(SYMBOL)
        assert "dig +short" in str(exc.value) and "data-api.binance.vision" in str(exc.value)
        health = await mirror.probe()
    assert health.ok is False and health.source is DataSource.BINANCE_SPOT_MIRROR
    assert "dig +short" in health.detail


async def test_spot_mirror_probe_reports_the_mainnet_mirror_when_it_answers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        health = await SpotMirror(http, SPOT_MIRROR_REST).probe()
    assert health.ok and "keyless mainnet spot market-data mirror" in health.detail


# --------------------------------------------------------------------------- engine wiring
def _engine_transport() -> httpx.AsyncClient:
    """Answers the public market-data calls the engine makes at construction time only."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2.0)


def test_engine_points_market_data_at_mainnet_in_paper_mode(tmp_path):
    from deltr.engine import build_engine

    settings = Settings(_env_file=None, DELTR_MODE="paper", DELTR_STATE_DIR=str(tmp_path))  # type: ignore[call-arg]
    eng = build_engine(settings, http=_engine_transport())
    assert eng.futures_data is not eng.futures, "market data must not come from the order-routing client"
    assert eng.futures_data.base_url == FUTURES_MAINNET_REST
    assert eng.futures_data.has_credentials is False, "the market-data client must stay keyless"
    assert eng.futures.base_url == settings.hosts["futures_rest"]
    assert eng.hub.futures_source is DataSource.BINANCE_FUTURES_MAINNET
    assert eng.hub.venue_futures == VENUE_FUTURES_MAINNET
    assert eng.funding_history_service.base_url == FUTURES_MAINNET_REST


async def test_engine_funding_history_validates_its_arguments(tmp_path):
    from deltr.engine import build_engine

    settings = Settings(_env_file=None, DELTR_MODE="paper", DELTR_STATE_DIR=str(tmp_path))  # type: ignore[call-arg]
    eng = build_engine(settings, http=_engine_transport())
    for bad_symbol in ("BNB-USDT", "X", "BNB USDT"):
        with pytest.raises(ValueError):
            await eng.funding_history(bad_symbol)
    with pytest.raises(ValueError):
        await eng.funding_history("BNBUSDT", lookback_days=0)
    with pytest.raises(ValueError):
        await eng.funding_history("BNBUSDT", holds_days=[0.0])


# --------------------------------------------------------------------------- live
@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="live endpoints; set DELTR_LIVE_TESTS=1")
async def test_live_mainnet_feeds_answer_and_tag_themselves_correctly():
    """Read-only public market data. Nothing here can sign or place an order."""
    from deltr.venues.binance_futures import FuturesClient

    async with httpx.AsyncClient(timeout=20.0) as http:
        futures = FuturesClient(http, FUTURES_MAINNET_REST)
        funding = await futures.premium_index(SYMBOL)
        book = await futures.book_ticker(SYMBOL)
        spot = await SpotMirror(http, SPOT_MIRROR_REST).book_ticker(SYMBOL)
    assert funding.mark_price > 0 and book.bid > 0 and spot.bid > 0
    assert spot.source is DataSource.BINANCE_SPOT_MIRROR
    # the mainnet mark and the mainnet spot mid track each other closely
    assert abs(funding.mark_price - spot.mid) / spot.mid < 0.02
