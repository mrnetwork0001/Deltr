"""tests/test_funding_surface.py — the funding evidence as a product surface.

``deltr_funding_history`` (MCP) and ``GET /api/funding/history`` (REST) must return the
same regenerable analysis, tagged mainnet, labelled honestly, and never raising through
the transport.  Both run against the captured fixture through ``FakeEngine``, so no
network is touched and no order can be placed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from deltr.api.app import create_app
from deltr.funding_history import POSTED_MODEL, TAKEN_MODEL
from deltr.mcp.server import build_mcp
from tests.helpers_mcp import FakeEngine, build_fake_server

CLIENT = Implementation(name="test-client", version="9.9")


def _payload(result) -> dict:
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    from deltr.mcp.activity import ActivityLog

    engine = FakeEngine()
    activity = ActivityLog(engine.state)
    app = create_app(engine, build_mcp(engine, activity), activity, ui_dir=tmp_path / "no-ui")
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


# --------------------------------------------------------------------------- MCP tool
async def test_mcp_tool_returns_the_mainnet_analysis_with_both_cost_models():
    _engine, _activity, mcp = build_fake_server()
    async with create_connected_server_and_client_session(mcp._mcp_server, client_info=CLIENT) as c:
        out = _payload(await c.call_tool("deltr_funding_history", {"symbol": "BNBUSDT"}))
    assert "error" not in out
    assert out["symbol"] == "BNBUSDT" and out["source"] == "binance-futures-mainnet"
    assert out["samples"] == 400 and out["interval_h"] == 8.0 and out["interval_measured"] is True
    models = {w["model"] for w in out["windows"]}
    assert models == {TAKEN_MODEL, POSTED_MODEL}
    assert 0.0 <= out["short_paid_pct"] <= 100.0
    assert "model, not a measurement" in out["labels"]["posted_model"]
    assert out["format"] == "markdown" and out["title"] == "Funding carry: BNBUSDT"
    assert "Settlements where the short is paid" in out["markdown"]


async def test_mcp_tool_honours_custom_holds_and_symbol():
    _engine, _activity, mcp = build_fake_server()
    async with create_connected_server_and_client_session(mcp._mcp_server, client_info=CLIENT) as c:
        out = _payload(await c.call_tool("deltr_funding_history", {"symbol": "BTCUSDT", "holds_days": [7.0]}))
    assert out["symbol"] == "BTCUSDT"
    assert sorted({w["hold_days"] for w in out["windows"]}) == [7.0]
    taken = next(w for w in out["windows"] if w["model"] == TAKEN_MODEL)
    posted = next(w for w in out["windows"] if w["model"] == POSTED_MODEL)
    assert taken["roundtrip_bps"] == 16.6 and posted["roundtrip_bps"] == 8.6
    assert posted["cleared"] >= taken["cleared"]


async def test_mcp_tool_returns_an_error_value_never_a_transport_failure():
    _engine, _activity, mcp = build_fake_server()
    async with create_connected_server_and_client_session(mcp._mcp_server, client_info=CLIENT) as c:
        bad_symbol = _payload(await c.call_tool("deltr_funding_history", {"symbol": "NOTAPAIR"}))
        malformed = _payload(await c.call_tool("deltr_funding_history", {"symbol": "BNB-USDT"}))
    assert bad_symbol["error"]["code"] == "INVALID_ARGUMENT"
    assert malformed["error"]["code"] == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------- REST route
def test_rest_route_returns_the_same_analysis(client: TestClient):
    r = client.get("/api/funding/history", params={"symbol": "BNBUSDT"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "BNBUSDT" and body["source"] == "binance-futures-mainnet"
    assert body["samples"] == 400 and body["cost_models"][0]["roundtrip_bps"] == 16.6
    assert "markdown" not in body


def test_rest_route_renders_markdown_on_request(client: TestClient):
    body = client.get("/api/funding/history", params={"symbol": "BTCUSDT", "markdown": "true"}).json()
    assert body["format"] == "markdown"
    assert body["markdown"].startswith("# Funding carry: BTCUSDT")
    assert "model, not a measurement" in body["markdown"]


def test_rest_route_parses_holds_days(client: TestClient):
    body = client.get("/api/funding/history", params={"symbol": "BNBUSDT", "holds_days": "7,30"}).json()
    assert sorted({w["hold_days"] for w in body["windows"]}) == [7.0, 30.0]


def test_rest_route_rejects_bad_input_with_the_error_envelope(client: TestClient):
    r = client.get("/api/funding/history", params={"symbol": "BNBUSDT", "holds_days": "seven"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_ARGUMENT"
    r = client.get("/api/funding/history", params={"symbol": "BNB-USDT"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_ARGUMENT"
    r = client.get("/api/funding/history", params={"symbol": "BNBUSDT", "lookback_days": 0})
    assert r.status_code == 400


def test_rest_route_reports_an_upstream_failure_as_502(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from deltr.funding_history import FundingHistoryError

    async def boom(*a, **k):
        raise FundingHistoryError("GET /fapi/v1/fundingRate: hostname did not resolve", hint="dig +short @1.1.1.1 ...")

    monkeypatch.setattr(FakeEngine, "funding_history", boom, raising=True)
    r = client.get("/api/funding/history", params={"symbol": "BNBUSDT"})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert "dig +short" in r.json()["error"]["message"]
