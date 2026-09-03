"""Binance USDⓈ-M Futures testnet client + Spot mirror: signing, IOC params, error map,
query-before-retry, filter/premiumIndex parsing, testnet-only credentials — all offline
via httpx.MockTransport.  Nothing here touches the network unless DELTR_LIVE_TESTS=1."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from typing import Any, Callable
from urllib.parse import parse_qsl

import httpx
import pytest

from deltr.models import DataSource, FundingSnapshot, Quote, Side, SymbolFilters, Venue
from deltr.venues.binance_futures import (
    CLIENT_ID_RE,
    ERROR_MAP,
    AccountPrep,
    FuturesClient,
    FuturesError,
    FuturesTimeout,
    OrderResult,
    fmt_decimal,
    parse_order,
    parse_premium_index,
    parse_symbol_filters,
    redact,
)
from deltr.venues.binance_spot import SpotMirror, SpotMirrorError

TESTNET = "https://testnet.binancefuture.com"
SPOT = "https://data-api.binance.vision"
API_KEY = "fake-testnet-api-key-for-unit-tests"
SECRET = "fake-testnet-secret-for-unit-tests"

# --------------------------------------------------------------------------- canned payloads (probe 2026-09-02)
EXCHANGE_INFO = {
    "symbols": [
        {"symbol": "BTCUSDT", "pricePrecision": 2, "quantityPrecision": 3, "filters": []},
        {
            "symbol": "BNBUSDT", "pricePrecision": 2, "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.010", "minPrice": "0.100", "maxPrice": "100000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        },
    ]
}
FUNDING_INFO = [{"symbol": "BNBUSDT", "fundingIntervalHours": 8, "adjustedFundingRateCap": "0.00375", "adjustedFundingRateFloor": "-0.00375"}]
PREMIUM_INDEX = {
    "symbol": "BNBUSDT", "markPrice": "686.33900000", "indexPrice": "686.12900000", "estimatedSettlePrice": "686.2",
    "lastFundingRate": "0.00002763", "interestRate": "0.00010000", "nextFundingTime": 1788336000000, "time": 1788316985000,
}
BOOK_TICKER = {"symbol": "BNBUSDT", "bidPrice": "686.03", "bidQty": "12.5", "askPrice": "686.34", "askQty": "3.1", "time": 1788316985000}
DEPTH = {"lastUpdateId": 1, "bids": [["686.03", "12.5"], ["686.00", "4"]], "asks": [["686.34", "3.1"], ["686.40", "9"]]}
ORDER_FILLED = {
    "orderId": 4_000_000_001, "symbol": "BNBUSDT", "status": "FILLED", "clientOrderId": "DLTRabcdef12P1",
    "price": "685.65", "avgPrice": "686.03000", "origQty": "4.85", "executedQty": "4.85", "cumQuote": "3327.2455",
    "timeInForce": "IOC", "type": "LIMIT", "reduceOnly": False, "side": "SELL", "positionSide": "BOTH", "updateTime": 1788316990000,
}
ORDER_EXPIRED = {**ORDER_FILLED, "status": "EXPIRED", "executedQty": "0", "avgPrice": "0.00000", "cumQuote": "0"}


class FakeFutures:
    """Stateful Binance Futures testnet behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.dual_side = False
        self.position_amt = "0"
        self.order_response: dict | Callable[[], Any] = ORDER_FILLED
        self.order_lookup: dict | None = None
        self.margin_type_error: int | None = -4046
        self.leverage_error: int | None = None
        self.server_offset_ms = 74

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        q = dict(parse_qsl(request.url.query.decode()))
        signed_paths = ("/fapi/v1/positionSide/dual", "/fapi/v1/marginType", "/fapi/v1/leverage", "/fapi/v1/order",
                        "/fapi/v2/positionRisk", "/fapi/v2/balance")
        if path in signed_paths:
            if request.headers.get("X-MBX-APIKEY") != API_KEY:
                return self._err(401, -2014, "API-key format invalid.")
            if not self._signature_ok(request):
                return self._err(400, -1022, "Signature for this request is not valid.")
        if path == "/fapi/v1/ping":
            return httpx.Response(200, json={})
        if path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000) + self.server_offset_ms})
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=EXCHANGE_INFO)
        if path == "/fapi/v1/fundingInfo":
            return httpx.Response(200, json=FUNDING_INFO)
        if path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json=PREMIUM_INDEX)
        if path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(200, json=BOOK_TICKER)
        if path == "/fapi/v1/depth":
            return httpx.Response(200, json=DEPTH)
        if path == "/fapi/v1/positionSide/dual" and request.method == "GET":
            return httpx.Response(200, json={"dualSidePosition": self.dual_side})
        if path == "/fapi/v1/marginType":
            if self.margin_type_error:
                return self._err(400, self.margin_type_error, "No need to change margin type.")
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        if path == "/fapi/v1/leverage":
            if self.leverage_error:
                return self._err(400, self.leverage_error, "Leverage is not valid")
            return httpx.Response(200, json={"leverage": int(q["leverage"]), "maxNotionalValue": "1000000", "symbol": q["symbol"]})
        if path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": "BNBUSDT", "positionAmt": self.position_amt, "leverage": "5", "marginType": "isolated", "entryPrice": "0.0"}])
        if path == "/fapi/v2/balance":
            return httpx.Response(200, json=[{"asset": "BNB", "balance": "0", "availableBalance": "0"}, {"asset": "USDT", "balance": "15000.5", "availableBalance": "14990.25"}])
        if path == "/fapi/v1/order" and request.method == "POST":
            out = self.order_response() if callable(self.order_response) else self.order_response
            if isinstance(out, Exception):
                raise out
            if isinstance(out, httpx.Response):
                return out
            return httpx.Response(200, json={**out, "clientOrderId": q.get("newClientOrderId", out.get("clientOrderId"))})
        if path == "/fapi/v1/order" and request.method == "GET":
            if self.order_lookup is None:
                return self._err(400, -2013, "Order does not exist.")
            return httpx.Response(200, json={**self.order_lookup, "clientOrderId": q.get("origClientOrderId")})
        return httpx.Response(404, json={"code": -5000, "msg": f"path {path} not found"})

    @staticmethod
    def _signature_ok(request: httpx.Request) -> bool:
        raw = request.url.query.decode()
        payload, _, sig = raw.rpartition("&signature=")
        expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    @staticmethod
    def _err(status: int, code: int, msg: str) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "msg": msg})


@pytest.fixture
def fake() -> FakeFutures:
    return FakeFutures()


@pytest.fixture
async def http(fake: FakeFutures):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
        yield c


@pytest.fixture
def client(http: httpx.AsyncClient) -> FuturesClient:
    return FuturesClient(http, TESTNET, api_key=API_KEY, secret_key=SECRET)


@pytest.fixture
def keyless(http: httpx.AsyncClient) -> FuturesClient:
    return FuturesClient(http, TESTNET)


# --------------------------------------------------------------------------- safety: testnet only
def test_credentials_require_testnet_host(http: httpx.AsyncClient):
    with pytest.raises(ValueError, match="testnet"):
        FuturesClient(http, "https://fapi.binance.com", api_key=API_KEY, secret_key=SECRET)
    with pytest.raises(ValueError, match="testnet"):
        FuturesClient(http, "https://demo-fapi.binance.com", api_key=API_KEY, secret_key=SECRET)
    with pytest.raises(ValueError):
        FuturesClient(http, TESTNET, api_key=API_KEY)  # half a credential pair
    assert FuturesClient(http, "https://fapi.binance.com").has_credentials is False  # keyless public data is fine
    c = FuturesClient(http, TESTNET, api_key=API_KEY, secret_key=SECRET)
    assert c.has_credentials and API_KEY not in repr(c) and SECRET not in repr(c)


async def test_signed_endpoint_without_credentials_fails_closed(keyless: FuturesClient, fake: FakeFutures):
    with pytest.raises(FuturesError) as ei:
        await keyless.usdt_balance()
    assert ei.value.code == -2015 and ei.value.retryable is False
    assert fake.requests == []  # never even sent


# --------------------------------------------------------------------------- signing
def test_sign_matches_hmac_sha256_over_urlencoded_query(client: FuturesClient):
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": 1, "price": 9000, "timeInForce": "GTC"}
    signed = client._sign(dict(params), timestamp_ms=1591702613943)
    keys = list(signed)
    assert keys == ["symbol", "side", "type", "quantity", "price", "timeInForce", "recvWindow", "timestamp", "signature"]
    query = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
    expected = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert signed["signature"] == expected and len(expected) == 64
    assert signed["recvWindow"] == 5000 and signed["timestamp"] == 1591702613943
    # a different secret must yield a different signature (proves the secret is actually used)
    other = FuturesClient(client.http, TESTNET, api_key=API_KEY, secret_key=SECRET + "x")._sign(dict(params), timestamp_ms=1591702613943)
    assert other["signature"] != expected


def test_sign_applies_clock_offset_and_recv_window(http: httpx.AsyncClient):
    c = FuturesClient(http, TESTNET, api_key=API_KEY, secret_key=SECRET, recv_window_ms=7000)
    c.time_offset_ms = 5_000
    before = int(time.time() * 1000)
    signed = c._sign({"symbol": "BNBUSDT"})
    assert signed["recvWindow"] == 7000
    assert before + 5_000 <= signed["timestamp"] <= before + 5_000 + 1_000
    assert "None" not in json.dumps({k: v for k, v in c._sign({"a": 1, "b": None}).items()})  # None dropped


async def test_signed_request_sends_header_and_valid_signature(client: FuturesClient, fake: FakeFutures):
    assert await client.usdt_balance() == 14990.25
    req = fake.requests[-1]
    assert req.headers["X-MBX-APIKEY"] == API_KEY
    assert req.url.path == "/fapi/v2/balance"
    q = dict(parse_qsl(req.url.query.decode()))
    assert {"timestamp", "recvWindow", "signature"} <= set(q) and q["recvWindow"] == "5000"
    assert SECRET not in req.url.query.decode()


async def test_sync_time_stores_offset(client: FuturesClient, fake: FakeFutures):
    fake.server_offset_ms = 250
    off = await client.sync_time()
    assert 200 <= off <= 300 and client.time_offset_ms == off


# --------------------------------------------------------------------------- public parsing
async def test_exchange_filters_parsed_not_hardcoded(keyless: FuturesClient, fake: FakeFutures):
    f = await keyless.exchange_filters("BNBUSDT")
    assert isinstance(f, SymbolFilters)
    assert (f.step_size, f.min_qty, f.tick_size, f.min_notional) == (0.01, 0.01, 0.01, 5.0)
    assert (f.price_precision, f.qty_precision, f.funding_interval_h) == (2, 3, 8)
    assert f.source == DataSource.BINANCE_FUTURES_TESTNET
    assert [r.url.path for r in fake.requests] == ["/fapi/v1/exchangeInfo", "/fapi/v1/fundingInfo"]
    with pytest.raises(FuturesError):
        parse_symbol_filters(EXCHANGE_INFO, "DOGEUSDT")


async def test_funding_interval_defaults_to_8h_when_symbol_absent(keyless: FuturesClient):
    assert await keyless.funding_interval_h("ETHUSDT") == 8
    assert await keyless.funding_interval_h("BNBUSDT") == 8


async def test_premium_index_parsing(keyless: FuturesClient):
    fs = await keyless.premium_index("BNBUSDT")
    assert isinstance(fs, FundingSnapshot)
    assert fs.mark_price == 686.339 and fs.index_price == 686.129
    assert fs.last_funding_rate == 0.00002763 and fs.next_funding_time_ms == 1788336000000
    assert fs.interval_h == 8 and math.isclose(fs.annualized_pct, 0.00002763 * 3 * 365 * 100)
    assert fs.source == DataSource.BINANCE_FUTURES_TESTNET
    alt = parse_premium_index({**PREMIUM_INDEX, "lastFundingRate": "-0.0001"}, interval_h=4)
    assert alt.interval_h == 4 and math.isclose(alt.annualized_pct, -0.0001 * 6 * 365 * 100)


async def test_book_ticker_and_depth(keyless: FuturesClient):
    q = await keyless.book_ticker("BNBUSDT")
    assert isinstance(q, Quote) and q.venue == Venue.BINANCE_FUTURES and q.source == DataSource.BINANCE_FUTURES_TESTNET
    assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (686.03, 686.34, 12.5, 3.1) and math.isclose(q.mid, 686.185)
    bids, asks = await keyless.depth("BNBUSDT", limit=5)
    assert bids[0] == (686.03, 12.5) and asks[1] == (686.40, 9.0)


async def test_probe_public(keyless: FuturesClient):
    h = await keyless.probe()
    assert h.ok and h.name == "binance_futures_testnet" and h.source == DataSource.BINANCE_FUTURES_TESTNET
    assert "credentials=no" in h.detail


# --------------------------------------------------------------------------- IOC orders
async def test_ioc_params_are_limit_ioc_only(client: FuturesClient):
    await client.exchange_filters("BNBUSDT")
    p = client.ioc_params("BNBUSDT", Side.SELL, 4.85, 685.652, "DLTRabcdef12P1")
    assert p["type"] == "LIMIT" and p["timeInForce"] == "IOC" and p["newOrderRespType"] == "RESULT"
    assert p["quantity"] == "4.85" and p["price"] == "685.65"  # floored to precisions, no sci-notation
    assert p["newClientOrderId"] == "DLTRabcdef12P1" and CLIENT_ID_RE.match(p["newClientOrderId"])
    assert "reduceOnly" not in p and "positionSide" not in p
    assert "MARKET" not in json.dumps(p)


def test_ioc_params_reduce_only_vs_hedge_mode(client: FuturesClient):
    one_way = client.ioc_params("BNBUSDT", Side.BUY, 4.85, 690.0, "DLTRabcdef12P1", reduce_only=True)
    assert one_way["reduceOnly"] == "true" and "positionSide" not in one_way
    hedge = client.ioc_params("BNBUSDT", Side.BUY, 4.85, 690.0, "DLTRabcdef12P1", reduce_only=True, position_side="SHORT")
    assert hedge["positionSide"] == "SHORT" and "reduceOnly" not in hedge  # reduceOnly is rejected in hedge mode
    for bad in ("", "x" * 37, "has space", "DLTR#1"):
        with pytest.raises(ValueError):
            client.ioc_params("BNBUSDT", Side.SELL, 1, 1, bad)
    with pytest.raises(ValueError):
        client.ioc_params("BNBUSDT", Side.SELL, 0, 1, "ok")
    with pytest.raises(ValueError):
        client.ioc_params("BNBUSDT", Side.SELL, 1, 1, "ok", position_side="UP")


async def test_place_limit_ioc_filled(client: FuturesClient, fake: FakeFutures):
    res = await client.place_limit_ioc("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert isinstance(res, OrderResult)
    assert res.order_id == 4_000_000_001 and res.client_id == "DLTRabcdef12P1" and res.status == "FILLED"
    assert res.executed_qty == 4.85 and res.avg_price == 686.03 and res.filled_any
    req = fake.requests[-1]
    q = dict(parse_qsl(req.url.query.decode()))
    assert req.method == "POST" and q["type"] == "LIMIT" and q["timeInForce"] == "IOC" and q["side"] == "SELL"
    assert q["newClientOrderId"] == "DLTRabcdef12P1" and q["newOrderRespType"] == "RESULT"


async def test_place_limit_ioc_zero_fill_is_not_an_error(client: FuturesClient, fake: FakeFutures):
    fake.order_response = ORDER_EXPIRED
    res = await client.place_limit_ioc("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert res.status == "EXPIRED" and res.executed_qty == 0.0 and not res.filled_any


def test_parse_order_derives_avg_from_cum_quote():
    r = parse_order({"orderId": 7, "clientOrderId": "c", "status": "PARTIALLY_FILLED", "executedQty": "2", "cumQuote": "1372.0"})
    assert r.avg_price == 686.0 and r.executed_qty == 2.0


# --------------------------------------------------------------------------- error map
@pytest.mark.parametrize(
    "code,retryable",
    [(-1021, True), (-1111, False), (-2019, False), (-4061, True), (-4164, False), (-4028, False), (-2022, False), (-1003, True)],
)
async def test_error_map(client: FuturesClient, fake: FakeFutures, code: int, retryable: bool):
    fake.order_response = lambda: httpx.Response(400, json={"code": code, "msg": "boom"})
    with pytest.raises(FuturesError) as ei:
        await client.place_limit_ioc("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert ei.value.code == code and ei.value.retryable is retryable
    assert ERROR_MAP[code][0] is retryable


async def test_unmapped_code_is_not_retryable(client: FuturesClient, fake: FakeFutures):
    fake.order_response = lambda: httpx.Response(400, json={"code": -9999, "msg": "mystery"})
    with pytest.raises(FuturesError) as ei:
        await client.place_limit_ioc("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert ei.value.code == -9999 and ei.value.retryable is False and "mystery" in ei.value.msg


async def test_http_5xx_and_transport_errors(client: FuturesClient, fake: FakeFutures):
    fake.order_response = lambda: httpx.Response(503, text="<html>maintenance</html>")
    with pytest.raises(FuturesError) as ei:
        await client.place_limit_ioc("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert ei.value.code == -503 and ei.value.retryable
    fake.order_response = lambda: httpx.ConnectError("refused")
    with pytest.raises(FuturesError) as ei:
        await client.place_limit_ioc("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert ei.value.code == -1 and ei.value.retryable and not isinstance(ei.value, FuturesTimeout)


async def test_get_order_by_client_id(client: FuturesClient, fake: FakeFutures):
    assert await client.get_order_by_client_id("BNBUSDT", "DLTRnope") is None  # -2013 → None
    fake.order_lookup = ORDER_FILLED
    res = await client.get_order_by_client_id("BNBUSDT", "DLTRabcdef12P1")
    assert res is not None and res.client_id == "DLTRabcdef12P1" and res.executed_qty == 4.85
    q = dict(parse_qsl(fake.requests[-1].url.query.decode()))
    assert q["origClientOrderId"] == "DLTRabcdef12P1" and fake.requests[-1].method == "GET"


async def test_query_before_retry_on_timeout(client: FuturesClient, fake: FakeFutures):
    # 1) timeout, order actually reached the engine → recovered by client id, no duplicate order
    fake.order_response = lambda: httpx.ReadTimeout("slow")
    fake.order_lookup = ORDER_FILLED
    res = await client.place_limit_ioc_recovering("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P1")
    assert res.status == "FILLED" and res.client_id == "DLTRabcdef12P1"
    paths = [(r.method, r.url.path) for r in fake.requests]
    assert paths == [("POST", "/fapi/v1/order"), ("GET", "/fapi/v1/order")]
    # 2) timeout, order never arrived → original retryable timeout re-raised for the router
    fake.requests.clear()
    fake.order_lookup = None
    with pytest.raises(FuturesTimeout) as ei:
        await client.place_limit_ioc_recovering("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P2")
    assert ei.value.retryable and [(r.method, r.url.path) for r in fake.requests] == [("POST", "/fapi/v1/order"), ("GET", "/fapi/v1/order")]
    # 3) a deterministic rejection is NOT looked up, just raised
    fake.requests.clear()
    fake.order_response = lambda: httpx.Response(400, json={"code": -2019, "msg": "Margin is insufficient."})
    with pytest.raises(FuturesError) as ei:
        await client.place_limit_ioc_recovering("BNBUSDT", Side.SELL, 4.85, 685.65, "DLTRabcdef12P3")
    assert ei.value.code == -2019 and len(fake.requests) == 1


# --------------------------------------------------------------------------- prepare_account
async def test_prepare_account_one_way_flat(client: FuturesClient, fake: FakeFutures):
    prep = await client.prepare_account("BNBUSDT", leverage=2)
    assert isinstance(prep, AccountPrep)
    assert prep.dual_side is False and prep.position_side == "BOTH" and prep.margin_type == "ISOLATED"
    assert prep.leverage == 2 and prep.usdt_balance == 14990.25
    paths = [r.url.path for r in fake.requests]
    assert paths == [
        "/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/fundingInfo", "/fapi/v1/positionSide/dual",
        "/fapi/v1/marginType", "/fapi/v2/positionRisk", "/fapi/v1/leverage", "/fapi/v2/balance",
    ]
    assert client.time_offset_ms != 0  # synced


async def test_prepare_account_hedge_mode_and_not_flat(client: FuturesClient, fake: FakeFutures):
    fake.dual_side = True
    fake.position_amt = "-4.85"
    fake.margin_type_error = None
    prep = await client.prepare_account("BNBUSDT", leverage=2, isolated=False)
    assert prep.dual_side is True and prep.position_side == "SHORT" and prep.margin_type == "CROSSED"
    assert prep.leverage == 5  # not flat → leverage untouched, account value reported
    assert "/fapi/v1/leverage" not in [r.url.path for r in fake.requests]


async def test_prepare_account_ignores_4028_but_raises_others(client: FuturesClient, fake: FakeFutures):
    fake.leverage_error = -4028
    prep = await client.prepare_account("BNBUSDT", leverage=2)
    assert prep.leverage == 5  # the exchange refused the change: report what positionRisk says the account HAS, never the request
    fake.leverage_error = -2015
    with pytest.raises(FuturesError) as ei:
        await client.prepare_account("BNBUSDT", leverage=2)
    assert ei.value.code == -2015
    fake.leverage_error = None
    fake.margin_type_error = -1022
    with pytest.raises(FuturesError):
        await client.prepare_account("BNBUSDT", leverage=2)


async def test_position_risk(client: FuturesClient):
    rows = await client.position_risk("BNBUSDT")
    assert rows and rows[0]["symbol"] == "BNBUSDT" and rows[0]["positionAmt"] == "0"


# --------------------------------------------------------------------------- helpers
def test_redact_and_fmt_decimal():
    r = redact({"symbol": "BNBUSDT", "signature": "abc", "apiKey": "k", "secret_key": "s", "token": "t", "qty": 1})
    assert r == {"symbol": "BNBUSDT", "signature": "***", "apiKey": "***", "secret_key": "***", "token": "***", "qty": 1}
    assert fmt_decimal(4.85, 3) == "4.85" and fmt_decimal(686.199, 2) == "686.19" and fmt_decimal(0.00001) == "0.00001"
    assert fmt_decimal(5.0) == "5" and fmt_decimal(1e-7, 3) == "0"


# --------------------------------------------------------------------------- spot mirror
class FakeSpot:
    def __init__(self) -> None:
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError("offline")
        if request.url.path == "/api/v3/ping":
            return httpx.Response(200, json={})
        if request.url.path == "/api/v3/ticker/bookTicker":
            assert request.url.params["symbol"] == "BNBUSDT"
            return httpx.Response(200, json={"symbol": "BNBUSDT", "bidPrice": "686.30000000", "bidQty": "5.2", "askPrice": "686.31000000", "askQty": "1.1"})
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})


async def test_spot_mirror_book_ticker_and_probe():
    spot = FakeSpot()
    async with httpx.AsyncClient(transport=httpx.MockTransport(spot.handler)) as http:
        m = SpotMirror(http, SPOT)
        q = await m.book_ticker("BNBUSDT")
        assert q.venue == Venue.BINANCE_SPOT and q.source == DataSource.BINANCE_SPOT_MIRROR
        assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (686.30, 686.31, 5.2, 1.1)
        h = await m.probe()
        assert h.ok and h.name == "binance_spot_mirror" and h.source == DataSource.BINANCE_SPOT_MIRROR
        with pytest.raises(SpotMirrorError) as ei:
            await m._get("/api/v3/nope")
        assert ei.value.code == -1121
        spot.fail = True
        assert (await m.probe()).ok is False
        with pytest.raises(SpotMirrorError):
            await m.book_ticker("BNBUSDT")
        assert not any(name.startswith(("place", "order", "sign")) for name in dir(m))  # no trading surface at all


# --------------------------------------------------------------------------- live (opt-in)
@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DELTR_LIVE_TESTS") != "1", reason="set DELTR_LIVE_TESTS=1 to hit the Binance testnet")
async def test_live_futures_testnet_public():
    async with httpx.AsyncClient(timeout=20.0) as http:
        c = FuturesClient(http, TESTNET)
        off = await c.sync_time()
        assert abs(off) < 60_000
        f = await c.exchange_filters("BNBUSDT")
        assert f.step_size > 0 and f.tick_size > 0 and f.min_notional > 0
        fs = await c.premium_index("BNBUSDT")
        assert fs.mark_price > 0 and fs.next_funding_time_ms > 0
        q = await c.book_ticker("BNBUSDT")
        assert q.bid > 0 and q.ask >= q.bid
        assert (await c.probe()).ok


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DELTR_LIVE_TESTS") != "1", reason="set DELTR_LIVE_TESTS=1 to hit the spot data mirror")
async def test_live_spot_mirror():
    async with httpx.AsyncClient(timeout=20.0) as http:
        m = SpotMirror(http, SPOT)
        q = await m.book_ticker("BNBUSDT")
        assert q.bid > 0 and q.ask >= q.bid
        assert (await m.probe()).ok
