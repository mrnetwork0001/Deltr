"""tests/test_binance_shim.py — the local Binance MCP shim against an httpx MockTransport.

Proves: market tools are keyless and shaped as promised; signed tools return
NO_CREDENTIALS without keys and sign correctly with keys; leverage > 3 is refused
before any request; only LIMIT IOC orders exist and default to /order/test;
production is refused at build time; fixture-driven tool naming is honoured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from deltr.config import HOSTS, Mode, load_settings
from deltr.mcp.activity import ActivityLog
from deltr.mcp.binance_shim_server import DEFAULT_TOOL_NAMES, build_shim, resolve_tool_names
from tests.helpers_mcp import FakeState, make_settings

FUT = HOSTS[Mode.PAPER]["futures_rest"]
SPOT = HOSTS[Mode.PAPER]["spot_rest"]
KEY, SECRET = "test-key-not-real", "test-secret-not-real"


class Recorder:
    """Deterministic fake of the testnet + spot mirror; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.path
        q = parse_qs(request.url.query.decode())
        if request.url.host == urlparse(SPOT).hostname and p == "/api/v3/ticker/bookTicker":
            return httpx.Response(200, json={"symbol": "BNBUSDT", "bidPrice": "686.30", "bidQty": "5", "askPrice": "686.31", "askQty": "7"})
        if p == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(200, json={"symbol": "BNBUSDT", "bidPrice": "686.03", "bidQty": "1", "askPrice": "686.34", "askQty": "2", "time": 1})
        if p == "/fapi/v1/depth":
            n = int(q.get("limit", ["20"])[0])
            return httpx.Response(200, json={"lastUpdateId": 1, "bids": [["686.03", "1"]] * n, "asks": [["686.34", "1"]] * n})
        if p == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BNBUSDT", "markPrice": "686.339", "indexPrice": "686.129", "lastFundingRate": "0.0001", "nextFundingTime": 1756800000000, "time": 1})
        if p == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[{"symbol": "BNBUSDT", "fundingRate": "0.00002763", "fundingTime": 1, "markPrice": "686"}] * 5)
        if p == "/fapi/v1/fundingInfo":
            return httpx.Response(200, json=[{"symbol": "BNBUSDT", "fundingIntervalHours": 8, "adjustedFundingRateCap": "0.00375"}])
        if p == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "BNBUSDT", "pricePrecision": 3, "quantityPrecision": 2, "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"}, {"filterType": "PRICE_FILTER", "tickSize": "0.010"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]})
        # ---- signed
        if request.headers.get("X-MBX-APIKEY") != KEY:
            return httpx.Response(401, json={"code": -2014, "msg": "API-key format invalid."})
        if p == "/fapi/v2/balance":
            return httpx.Response(200, json=[{"asset": "USDT", "balance": "15000.0", "availableBalance": "15000.0"}])
        if p == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": q["symbol"][0], "positionAmt": "0.000", "leverage": "2"}])
        if p == "/fapi/v1/leverage":
            return httpx.Response(200, json={"symbol": q["symbol"][0], "leverage": int(q["leverage"][0]), "maxNotionalValue": "1000000"})
        if p == "/fapi/v1/order/test":
            return httpx.Response(200, json={})
        if p == "/fapi/v1/order":
            return httpx.Response(200, json={"orderId": 123, "clientOrderId": q.get("newClientOrderId", ["x"])[0], "status": "EXPIRED", "executedQty": "0"})
        return httpx.Response(404, json={"code": -1, "msg": f"no route {p}"})


# These tests exercise the shim under its DEFAULT tool names, so they build it against a
# fixture path that does not exist.  The shipped fixture (and the official names it maps
# onto) is covered by tests/test_shim_tool_names.py.
NO_FIXTURE = Path(__file__).parent / "fixtures" / "no_such_binance_mcp_tools.json"


def shim(with_keys: bool = False, fixture_path: Path = NO_FIXTURE):
    rec = Recorder()
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    extra = {"BINANCE_API_KEY": KEY, "BINANCE_SECRET_KEY": SECRET} if with_keys else {}
    activity = ActivityLog(FakeState())
    return build_shim(make_settings(**extra), http=http, activity=activity, fixture_path=fixture_path), rec, activity


async def call(mcp, name: str, args: dict | None = None) -> dict:
    _, structured = await mcp.call_tool(name, args or {})
    return structured


# --------------------------------------------------------------------------- surface
async def test_tool_names_are_the_defaults_without_a_fixture():
    assert not NO_FIXTURE.exists()
    mcp, _, _ = shim()
    assert sorted(t.name for t in await mcp.list_tools()) == sorted(DEFAULT_TOOL_NAMES.values())
    assert mcp.name == "binance-shim"


def test_fixture_driven_naming(tmp_path: Path):
    fx = tmp_path / "binance_mcp_tools.json"
    fx.write_text(json.dumps({"tools": [{"name": "spot.ticker24hr"}, {"name": "um.premiumIndex"}, {"name": "futures.fundingRate"}, {"name": "um.newOrder"}]}))
    names = resolve_tool_names(fx)
    assert names["get_ticker"] == "spot.ticker24hr" and names["get_mark_price"] == "um.premiumIndex"
    assert names["get_funding_rate"] == "futures.fundingRate" and names["place_futures_order"] == "um.newOrder"
    assert names["get_account"] == "get_account"  # unmatched keeps the default
    assert resolve_tool_names(tmp_path / "missing.json") == DEFAULT_TOOL_NAMES


# --------------------------------------------------------------------------- market tools (keyless)
async def test_get_ticker_combines_spot_mirror_and_testnet_perp():
    mcp, rec, activity = shim()
    out = await call(mcp, "get_ticker", {"symbol": "bnbusdt"})
    assert out["symbol"] == "BNBUSDT"
    assert out["spot_mirror"]["bidPrice"] == "686.30" and out["spot_mirror"]["source"] == "binance-spot-mirror"
    assert out["perp_testnet"]["askPrice"] == "686.34" and out["perp_testnet"]["source"] == "binance-futures-testnet"
    hosts = {r.url.host for r in rec.requests}
    assert hosts == {urlparse(SPOT).hostname, urlparse(FUT).hostname}
    assert all("X-MBX-APIKEY" not in r.headers for r in rec.requests)
    row = activity.recent(1)[0]
    assert row.server == "binance-shim" and row.tool == "get_ticker" and row.ok


async def test_order_book_mark_price_funding_and_filters():
    mcp, rec, _ = shim()
    book = await call(mcp, "get_order_book", {"symbol": "BNBUSDT", "limit": 5})
    assert len(book["bids"]) == 5 and book["source"] == "binance-futures-testnet"
    mark = await call(mcp, "get_mark_price", {"symbol": "BNBUSDT"})
    assert mark["mark_price"] == pytest.approx(686.339) and mark["index_price"] == pytest.approx(686.129)
    assert mark["last_funding_rate"] == pytest.approx(0.0001) and mark["next_funding_time_ms"] == 1756800000000
    fr = await call(mcp, "get_funding_rate", {"symbol": "BNBUSDT", "limit": 3})
    assert len(fr["history"]) == 3 and fr["interval_h"] == 8 and "testnet" in fr["label"]
    assert fr["annualized_pct"] == pytest.approx(0.00002763 * 3 * 365 * 100)
    filt = await call(mcp, "get_exchange_filters", {"symbol": "BNBUSDT"})
    assert filt["step_size"] == 0.01 and filt["tick_size"] == 0.01 and filt["min_notional"] == 5.0
    assert filt["price_precision"] == 3 and filt["qty_precision"] == 2
    missing = await call(mcp, "get_exchange_filters", {"symbol": "DOGEUSDT"})
    assert missing["error"]["code"] == "INVALID_ARGUMENT"


async def test_upstream_http_errors_become_error_values():
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": -1001, "msg": "down"})

    mcp = build_shim(make_settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(boom)), fixture_path=NO_FIXTURE)
    out = await call(mcp, "get_mark_price", {"symbol": "BNBUSDT"})
    assert out["error"]["code"] == "BINANCE_ERROR" and out["status"] == 503 and out["binance"]["code"] == -1001

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    mcp = build_shim(make_settings(), http=httpx.AsyncClient(transport=httpx.MockTransport(dead)), fixture_path=NO_FIXTURE)
    assert (await call(mcp, "get_mark_price", {}))["error"]["code"] == "UPSTREAM_UNREACHABLE"


# --------------------------------------------------------------------------- signed tools
async def test_signed_tools_return_no_credentials_without_keys_and_send_nothing():
    mcp, rec, activity = shim(with_keys=False)
    for name, args in [("get_account", {}), ("get_positions", {"symbol": "BNBUSDT"}), ("set_leverage", {"symbol": "BNBUSDT", "leverage": 2}),
                       ("place_futures_order", {"symbol": "BNBUSDT", "side": "SELL", "quantity": 0.01, "price": 700})]:
        out = await call(mcp, name, args)
        assert out["error"]["code"] == "NO_CREDENTIALS", name
    assert rec.requests == []
    assert all(not r.ok for r in activity.recent(4))


async def test_leverage_above_3_is_refused_before_any_request():
    mcp, rec, _ = shim(with_keys=True)
    out = await call(mcp, "set_leverage", {"symbol": "BNBUSDT", "leverage": 5})
    assert out["error"]["code"] == "LEVERAGE_LIMIT" and out["observed"] == 5 and out["limit"] == 3
    assert rec.requests == []
    ok = await call(mcp, "set_leverage", {"symbol": "BNBUSDT", "leverage": 3})
    assert ok["ack"]["leverage"] == 3 and rec.requests[-1].method == "POST" and rec.requests[-1].url.path == "/fapi/v1/leverage"


async def test_signed_requests_carry_a_valid_hmac_and_key_header():
    mcp, rec, activity = shim(with_keys=True)
    acct = await call(mcp, "get_account")
    assert acct["balances"][0]["asset"] == "USDT" and acct["env"] == "testnet"
    r = rec.requests[-1]
    assert r.headers["X-MBX-APIKEY"] == KEY and r.url.path == "/fapi/v2/balance"
    q = r.url.query.decode()
    payload, sig = q.rsplit("&signature=", 1)
    assert "recvWindow=5000" in payload and "timestamp=" in payload
    assert sig == hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    pos = await call(mcp, "get_positions", {"symbol": "bnbusdt"})
    assert pos["positions"][0]["symbol"] == "BNBUSDT"
    blob = json.dumps([a.model_dump(mode="json") for a in activity.recent(5)])
    assert SECRET not in blob and KEY not in blob


async def test_orders_are_limit_ioc_only_and_default_to_the_test_endpoint():
    mcp, rec, _ = shim(with_keys=True)
    out = await call(mcp, "place_futures_order", {"symbol": "BNBUSDT", "side": "SELL", "quantity": 0.05, "price": 700.0, "newClientOrderId": "DLTRabc123"})
    assert out["test"] is True and out["endpoint"] == "/fapi/v1/order/test"
    r = rec.requests[-1]
    q = parse_qs(r.url.query.decode())
    assert r.url.path == "/fapi/v1/order/test" and q["type"] == ["LIMIT"] and q["timeInForce"] == ["IOC"] and q["side"] == ["SELL"]
    assert q["newClientOrderId"] == ["DLTRabc123"] and "reduceOnly" not in q
    real = await call(mcp, "place_futures_order", {"symbol": "BNBUSDT", "side": "BUY", "quantity": 0.05, "price": 600.0, "reduceOnly": True, "test": False})
    assert real["endpoint"] == "/fapi/v1/order" and real["ack"]["orderId"] == 123
    assert parse_qs(rec.requests[-1].url.query.decode())["reduceOnly"] == ["true"]
    schema = next(t.inputSchema for t in await mcp.list_tools() if t.name == "place_futures_order")
    tif = schema["properties"]["timeInForce"]
    assert (tif.get("enum") == ["IOC"] or tif.get("const") == "IOC") and schema["properties"]["test"]["default"] is True
    assert "type" not in schema["properties"], "order type is not a parameter: LIMIT is hard-wired"
    _, structured = await mcp.call_tool("place_futures_order", {"symbol": "BNBUSDT", "side": "SELL", "quantity": 0.05, "price": 700.0, "timeInForce": "IOC"})
    assert "error" not in structured


async def test_gtc_orders_are_rejected_by_the_schema():
    from mcp.server.fastmcp.exceptions import ToolError

    mcp, rec, _ = shim(with_keys=True)
    with pytest.raises(ToolError):
        await mcp.call_tool("place_futures_order", {"symbol": "BNBUSDT", "side": "SELL", "quantity": 0.05, "price": 700.0, "timeInForce": "GTC"})
    with pytest.raises(ToolError):
        await mcp.call_tool("place_futures_order", {"symbol": "BNBUSDT", "side": "HOLD", "quantity": 0.05, "price": 700.0})
    assert rec.requests == []


# --------------------------------------------------------------------------- production refusal
def test_prod_env_is_refused():
    with pytest.raises(Exception, match="prod"):
        load_settings(BINANCE_API_ENV="prod", _env_file=None)

    class ProdLike:
        binance_api_env = "prod"
        hosts = HOSTS[Mode.TESTNET]
        binance_api_key = binance_secret_key = None

    with pytest.raises(RuntimeError, match="prod"):
        build_shim(ProdLike())

    class MainnetHost:
        binance_api_env = "testnet"
        hosts = {"futures_rest": "https://fapi.binance.com", "spot_rest": SPOT}
        binance_api_key = binance_secret_key = None

    with pytest.raises(RuntimeError, match="testnet"):
        build_shim(MainnetHost())


@pytest.mark.live
@pytest.mark.skipif(not __import__("os").environ.get("DELTR_LIVE_TESTS"), reason="set DELTR_LIVE_TESTS=1 to hit the testnet")
async def test_live_mark_price_from_testnet():
    mcp = build_shim(make_settings(), fixture_path=NO_FIXTURE)
    out = await call(mcp, "get_mark_price", {"symbol": "BNBUSDT"})
    assert "error" not in out and out["mark_price"] > 0
