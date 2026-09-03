"""tests/test_api.py — the FastAPI surface through Starlette's TestClient (ASGI, no sockets).

Snapshot shape == ``Snapshot.model_json_schema()``; prompt -> propose -> execute ->
position; unwind; kill; reset_halt 409 while halted; stress; error envelope + status
codes; the /mcp mount answers; WS first frames are ``hello`` + ``snapshot`` and ping -> pong.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from deltr.api.app import create_app
from deltr.engine import build_engine
from deltr.mcp.server import build_mcp
from deltr.models import Snapshot
from tests.conftest import REPLAY_FIXTURE, make_replay_settings, refusing_http

UI = {"X-Deltr-Source": "ui", "User-Agent": "pytest-ui/1.0"}


@pytest.fixture
def api_engine(tmp_path: Path):
    settings = make_replay_settings(tmp_path / "state")
    eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http())
    asyncio.run(eng.start(loops=False, benchmark_iterations=500))
    yield eng
    asyncio.run(eng.stop())


@pytest.fixture
def client(api_engine, tmp_path: Path) -> Iterator[TestClient]:
    mcp = build_mcp(api_engine, api_engine.activity)
    app = create_app(api_engine, mcp, api_engine.activity, ui_dir=tmp_path / "no-ui")
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def test_health_and_snapshot_shape(client: TestClient):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["mode"] == "paper" and h["version"] and h["uptime_s"] >= 0
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == set(Snapshot.model_json_schema()["properties"])
    snap = Snapshot.model_validate(body)
    assert snap.status.replay is True and snap.market is not None and snap.edge is not None and snap.history
    assert snap.status.check_order[0] == "KILL_SWITCH" and len(snap.status.check_order) == 19
    st = client.get("/api/status").json()
    assert st["mode"] == "paper" and st["gate_median_us"] > 0


def test_read_routes(client: TestClient):
    m = client.get("/api/market").json()
    assert m["market"]["symbol"] == "BNBUSDT" and m["components"][-1]["kind"] == "net"
    m24 = client.get("/api/market", params={"horizon_h": 24}).json()
    assert m24["edge"]["horizon_h"] == 24
    assert client.get("/api/market", params={"symbol": "DOGEUSDT"}).status_code == 400
    # the breakeven holding period and the edge-versus-horizon curve ride along (win-plan A2)
    h = m["horizon"]
    assert h["verdict"] and h["curve"]["points"] and h["breakeven"]["is_assumption"] is False
    assert h["horizon_h"] == m["edge"]["horizon_h"] and h["interval_h"] == m["market"]["funding"]["interval_h"]
    assert h["measured_rate"] == m["edge"]["funding_rate_last"]
    assert m24["horizon"]["horizon_h"] == 24
    # an assumed rate is always labelled as one, with the measured rate still on the page
    ha = client.get("/api/market", params={"assumed_funding_rate": 0.0001}).json()["horizon"]
    assert ha["assumed_rate"] == 0.0001 and ha["assumption_label"] == "assumption, not a measurement"
    assert ha["curve_assumed"]["is_assumption"] is True and ha["breakeven_assumed"]["rate_basis"] == "assumed"
    assert ha["curve"]["is_assumption"] is False and ha["measured_rate"] == h["measured_rate"]
    o = client.get("/api/opportunities", params={"n": 5}).json()
    assert "is_actionable" in o["opportunity"] and len(o["history"]) <= 5
    p = client.get("/api/positions").json()
    assert p["positions"] == [] and p["portfolio"]["open_positions"] == 0
    assert client.get("/api/risk/decisions").json() == []
    assert client.get("/api/receipts").json() == []
    assert client.get("/api/receipts/rcpt_nope").status_code == 404
    assert client.get("/api/mcp/activity").json() == []
    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "paper" and cfg["secrets_present"] is False and "claude_code" in cfg["mcp_client_snippets"]
    assert cfg["risk_limits"]["max_leverage"] == 3.0 and cfg["hosts"]["futures_order_rest"] == ""


def test_prompt_propose_execute_unwind_flow(client: TestClient, api_engine):
    pr = client.post("/api/prompt", json={"text": "Rebalance $5,000 USDC into delta-neutral BNB arbitrage"}, headers=UI)
    assert pr.status_code == 200
    body = pr.json()
    assert body["executes"] is False and body["plan_id"] and body["precheck"]["approved"] is True
    assert body["intent"]["stablecoin_note"] and body["plan"]["source"] == "ui" and body["plan"]["client"] == "pytest-ui/1.0"
    assert api_engine.portfolio.positions("open") == []

    ex = client.post("/api/hedge/execute", json={"plan_id": body["plan_id"], "confirm": True}, headers=UI)
    assert ex.status_code == 200, ex.text
    rc = ex.json()
    assert rc["status"] == "filled" and len(rc["fills"]) == 2 and rc["sha256"] and rc["position_id"]
    assert client.get(f"/api/receipts/{rc['id']}").json()["id"] == rc["id"]
    assert len(client.get("/api/receipts").json()) == 1

    pos = client.get("/api/positions").json()
    open_positions = [p for p in pos["positions"] if p["status"] == "open"]
    assert len(open_positions) == 1 and abs(open_positions[0]["delta_base"]) <= 0.01
    assert pos["portfolio"]["open_positions"] == 1

    again = client.post("/api/hedge/execute", json={"plan_id": body["plan_id"], "confirm": True})
    assert again.status_code == 404 and again.json()["error"]["code"] == "PLAN_NOT_FOUND" and "receipt" in again.json()

    decisions = client.get("/api/risk/decisions", params={"limit": 5}).json()
    assert decisions[0]["code"] == "PLAN_NOT_FOUND" and decisions[0]["approved"] is False  # newest first: the replayed plan id
    executed = next(d for d in decisions if d["code"] == "OK" and d["dry_run"] is False)
    assert executed["plan_id"] == body["plan_id"] and len(executed["checks"]) == 19
    vetoes = client.get("/api/risk/decisions", params={"only_vetoes": "true"}).json()
    assert all(d["approved"] is False for d in vetoes) and vetoes

    un = client.post("/api/hedge/unwind", json={"position_id": "all", "reason": "test"})
    assert un.status_code == 200 and un.json()[0]["status"] == "unwound"
    assert client.get("/api/positions").json()["portfolio"]["open_positions"] == 0
    missing = client.post("/api/hedge/unwind", json={"position_id": "pos_nope"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "POSITION_NOT_FOUND"


def test_propose_and_evaluate_routes(client: TestClient):
    r = client.post("/api/hedge/propose", json={"capital_usd": 5000, "leverage": 2}).json()
    assert r["plan_id"] == r["plan"]["id"] and r["precheck"]["approved"] is True and r["proposal"]["is_delta_neutral"] is True
    assert "expires_at" in r
    veto = client.post("/api/risk/evaluate", json={"capital_usd": 5000, "leverage": 10}).json()
    assert veto["approved"] is False and veto["code"] == "LEVERAGE" and veto["dry_run"] is True
    bad = client.post("/api/hedge/propose", json={"capital_usd": -5})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"
    bad_sym = client.post("/api/hedge/propose", json={"capital_usd": 5000, "symbol": "DOGEUSDT"})
    assert bad_sym.status_code == 400
    assert client.post("/api/prompt", json={"text": ""}).status_code == 400


def test_kill_stress_and_reset_halt(client: TestClient, api_engine):
    ks = client.post("/api/kill", json={"on": True, "reason": "drill"}).json()
    assert ks["kill_switch"] is True
    veto = client.post("/api/risk/evaluate", json={"capital_usd": 5000, "leverage": 2}).json()
    assert veto["code"] == "KILL_SWITCH"
    assert client.post("/api/kill", json={"on": False}).json()["kill_switch"] is False

    # open a position, then shock equity by 3.5 % -> HALTED (sticky)
    plan = client.post("/api/hedge/propose", json={"capital_usd": 5000, "leverage": 2}).json()
    assert client.post("/api/hedge/execute", json={"plan_id": plan["plan_id"], "confirm": True}).status_code == 200
    sr = client.post("/api/stress", json={"kind": "equity_shock", "magnitude": 3.5}).json()
    assert sr["halted"] is True and sr["dd_state"] == "HALTED" and sr["active_label"].startswith("SIMULATED")
    assert client.get("/api/status").json()["stress_active"] == sr["active_label"]
    rh = client.post("/api/risk/reset_halt", json={"reason": "operator review"})
    assert rh.status_code == 409 and rh.json()["error"]["code"] == "HALT_NOT_CLEARABLE"
    vetoed = client.post("/api/hedge/propose", json={"capital_usd": 1000, "leverage": 2}).json()
    assert vetoed["precheck"]["code"] == "HALTED_DRAWDOWN"
    # unwinds are allowed while halted (verified reduce-only), then reset the stress and clear the halt
    un = client.post("/api/hedge/unwind", json={"position_id": "all", "reason": "flatten"}).json()
    assert un[0]["status"] == "unwound"
    rs = client.post("/api/stress", json={"kind": "reset"}).json()
    assert rs["active_label"] is None
    ok = client.post("/api/risk/reset_halt", json={"reason": "operator review"})
    assert ok.status_code == 200 and ok.json()["halted"] is False and ok.json()["dd_state"] in ("NORMAL", "WARN")
    me = client.post("/api/scout/min_edge", json={"min_edge_bps": -10}).json()
    assert me == {"min_edge_bps": -10.0, "floor_bps": 0.0, "mode": "paper"}
    assert client.post("/api/scout/min_edge", json={"min_edge_bps": 99}).status_code == 400


def test_mcp_is_mounted_at_slash_mcp(client: TestClient):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    r = client.post("/mcp", json=init, headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "deltr"


def test_ws_stream_sends_hello_then_snapshot_and_answers_ping(client: TestClient):
    with client.websocket_connect("/ws/stream") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["data"]["mode"] == "paper" and hello["data"]["poll_fallback_ms"] == 1000
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and set(snap["data"]) == set(Snapshot.model_json_schema()["properties"])
        ws.send_json({"type": "ping"})
        for _ in range(10):
            frame = ws.receive_json()
            if frame["type"] == "pong":
                break
            assert frame["type"] in ("snapshot", "event")
        else:
            pytest.fail("no pong")


# --------------------------------------------------------------------------- review fixes
def test_drawdown_pct_has_one_unit_across_the_snapshot(client: TestClient):
    """SystemStatus / PortfolioSnapshot / StressResult all render PERCENT (the gate reports a fraction)."""
    plan = client.post("/api/hedge/propose", json={"capital_usd": 5000, "leverage": 2}).json()
    assert client.post("/api/hedge/execute", json={"plan_id": plan["plan_id"], "confirm": True}).status_code == 200
    sr = client.post("/api/stress", json={"kind": "equity_shock", "magnitude": 2.5}).json()
    snap = Snapshot.model_validate(client.get("/api/snapshot").json())
    assert snap.status.drawdown_pct == pytest.approx(snap.portfolio.drawdown_pct)
    assert snap.status.drawdown_pct == pytest.approx(sr["drawdown_pct"], abs=1e-6)
    assert 2.0 <= snap.status.drawdown_pct < 3.0 and snap.status.dd_state == "WARN"
    assert client.post("/api/stress", json={"kind": "reset"}).status_code == 200
    client.post("/api/hedge/unwind", json={"position_id": "all", "reason": "cleanup"})


def test_expired_plan_stays_410_after_a_later_proposal(client: TestClient, api_engine):
    from datetime import timedelta

    a = client.post("/api/hedge/propose", json={"capital_usd": 5000, "leverage": 2}).json()["plan_id"]
    plan_a = api_engine.state.peek_plan(a)
    api_engine.state.plans[a] = plan_a.model_copy(update={"expires_at": plan_a.expires_at - timedelta(minutes=5)})
    assert api_engine.state.plan_status(a) == "expired"
    client.post("/api/hedge/propose", json={"capital_usd": 4000, "leverage": 2})  # prunes A
    assert a not in api_engine.state.plans and api_engine.state.plan_status(a) == "expired"
    ex = client.post("/api/hedge/execute", json={"plan_id": a, "confirm": True})
    assert ex.status_code == 410 and ex.json()["error"]["code"] == "PLAN_EXPIRED", ex.text


def test_upstream_status_route_feeds_the_status_badge(client: TestClient):
    body = {"kind": "shim", "url": "stdio:python -m deltr.mcp.binance_shim_server", "authorized": True,
            "tools_discovered": ["get_ticker", "get_funding_rate"], "error": "official upstream not configured (BINANCE_MCP_URL unset)"}
    r = client.post("/api/upstream", json=body)
    assert r.status_code == 200 and r.json()["kind"] == "shim"
    st = client.get("/api/status").json()
    assert st["upstream"]["kind"] == "shim" and st["upstream"]["tools_discovered"] == ["get_ticker", "get_funding_rate"]
    assert client.post("/api/upstream", json={"kind": "bogus"}).status_code == 400


def test_stress_magnitudes_are_bounded_and_rpc_urls_redacted(client: TestClient):
    bad = client.post("/api/stress", json={"kind": "equity_shock", "magnitude": 150})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert client.post("/api/stress", json={"kind": "equity_shock", "magnitude": -1}).status_code == 400
    cfg = client.get("/api/config").json()
    for u in cfg["bsc_rpc_urls"]:
        assert u.startswith("https://") and u.count("/") == 2 or u.endswith("/…"), u


def test_bare_mcp_path_answers_even_with_the_static_ui_mounted(api_engine, tmp_path: Path):
    ui = tmp_path / "ui-out"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Deltr</title>", encoding="utf-8")
    mcp = build_mcp(api_engine, api_engine.activity)
    app = create_app(api_engine, mcp, api_engine.activity, ui_dir=ui)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/").status_code == 200 and "Deltr" in c.get("/").text
        r = c.post("/mcp", json=init, headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}, follow_redirects=False)
        assert r.status_code == 200, r.text
        assert r.json()["result"]["serverInfo"]["name"] == "deltr"
