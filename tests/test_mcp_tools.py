"""tests/test_mcp_tools.py — the Deltr MCP server through an in-memory MCP client.

Proves: tools/list is exactly ``TOOL_NAMES`` with the promised schema constraints
and VETO docstrings; the veto path; the two-phase propose → execute path with
single-use plans; error envelopes (never raised through the transport); activity
rows carrying the client's ``clientInfo``; readable resources; and that the
server writes nothing but JSON-RPC to stdout under the stdio transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from deltr.mcp.activity import ActivityLog, redact
from deltr.mcp.server import TOOL_NAMES, VETO_SENTENCE, build_mcp
from tests.helpers_mcp import FakeEngine, FakeState, build_fake_server, make_settings

REPO = Path(__file__).resolve().parent.parent
CLIENT = Implementation(name="test-client", version="9.9")


def _payload(result) -> dict:
    """structuredContent when present, else the JSON text block."""
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def _session(mcp):
    return create_connected_server_and_client_session(mcp._mcp_server, client_info=CLIENT)


async def test_status_reports_mode_data_source_execution_style_and_whether_funds_are_armed():
    """deltr_status must let a caller tell a simulation from real money without guessing."""
    built = build_fake_server()
    mcp = next(b for b in built if hasattr(b, "_mcp_server"))
    async with await _session(mcp) as c:
        d = _payload(await c.call_tool("deltr_status", {}))
    assert d["mode"] == "paper"
    assert d["real_funds_armed"] is False          # never true outside an armed LIVE engine
    assert d["execution_style"] is None            # PAPER reaches no venue
    assert "simulated" in d["execution_style_label"]
    assert "binance-futures-mainnet" in d["data_source"]   # data is real mainnet in every mode
    assert d["onchain_armed"] is False and d["wallet_address"] is None
    assert d["max_notional_usd"] == 50_000.0
    assert d["secrets_present"] in (True, False)
    assert "secret_key" not in json.dumps(d).lower()


# --------------------------------------------------------------------------- surface
async def test_tools_list_is_exactly_the_named_tools():
    _, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        listed = await c.list_tools()
    names = [t.name for t in listed.tools]
    assert sorted(names) == sorted(TOOL_NAMES)
    assert len(names) == len(TOOL_NAMES) and len(set(names)) == len(TOOL_NAMES)
    assert not any("mode" in n for n in names), "no mode-changing tool may exist"


async def test_every_tool_docstring_ends_with_the_veto_sentence():
    _, _, mcp = build_fake_server()
    for t in await mcp.list_tools():
        assert t.description and t.description.strip().endswith(VETO_SENTENCE), t.name


async def test_schema_constraints_match_the_plan():
    _, _, mcp = build_fake_server()
    schemas = {t.name: t.inputSchema for t in await mcp.list_tools()}
    p = schemas["deltr_propose_hedge"]["properties"]
    assert p["capital_usd"]["minimum"] == 10 and "capital_usd" in schemas["deltr_propose_hedge"]["required"]
    assert p["leverage"]["exclusiveMinimum"] == 0 and p["leverage"]["maximum"] == 3 and p["leverage"]["default"] == 2
    assert p["symbol"]["default"] == "BNBUSDT"
    ev = schemas["deltr_evaluate_risk"]
    assert set(ev["required"]) == {"capital_usd", "leverage"} and "maximum" not in ev["properties"]["leverage"]
    ex = schemas["deltr_execute_hedge"]
    assert ex["required"] == ["plan_id"] and ex["properties"]["confirm"]["default"] is False
    st = schemas["deltr_stress"]["properties"]["kind"]
    assert st["enum"] == ["basis_shock", "equity_shock", "dex_leg_fail", "funding_flip", "feed_stale", "reset"]
    me = schemas["deltr_set_min_edge"]["properties"]["min_edge_bps"]
    assert me["minimum"] == -50 and me["maximum"] == 50
    assert schemas["deltr_risk_log"]["properties"]["limit"]["maximum"] == 100
    assert schemas["deltr_activity"]["properties"]["limit"]["maximum"] == 50
    assert schemas["deltr_prompt"]["properties"]["text"]["minLength"] == 1
    assert schemas["deltr_reset_halt"]["properties"]["reason"]["minLength"] == 3
    assert schemas["deltr_status"]["properties"] == {}
    sc = schemas["deltr_scan"]["properties"]
    assert sc["history_points"]["default"] == 60 and sc["history_points"]["maximum"] == 600
    assert "required" not in schemas["deltr_scan"] or schemas["deltr_scan"]["required"] == []


# --------------------------------------------------------------------------- veto path
async def test_evaluate_risk_leverage_10_is_a_leverage_veto():
    _, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        res = await c.call_tool("deltr_evaluate_risk", {"capital_usd": 5000, "leverage": 10})
    assert not res.isError
    d = _payload(res)
    assert d["approved"] is False and d["code"] == "LEVERAGE" and d["dry_run"] is True
    assert d["observed"] == 10 and d["limit"] == 3 and d["unit"] == "x"
    lev = next(ch for ch in d["checks"] if ch["name"] == "LEVERAGE")
    assert lev["passed"] is False and lev["observed"] == 10 and lev["limit"] == 3
    assert d["latency_us"] > 0


async def test_evaluate_risk_compliant_hedge_is_approved():
    _, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        d = _payload(await c.call_tool("deltr_evaluate_risk", {"capital_usd": 5000, "leverage": 2}))
    assert d["approved"] is True and d["code"] == "OK" and len(d["checks"]) == 19


# --------------------------------------------------------------------------- two-phase path
async def test_propose_then_execute_then_plan_is_single_use():
    engine, activity, mcp = build_fake_server()
    async with await _session(mcp) as c:
        prop = _payload(await c.call_tool("deltr_propose_hedge", {"capital_usd": 5000, "leverage": 2}))
        assert prop["plan_id"].startswith("plan_") and prop["precheck"]["approved"] is True
        assert prop["precheck"]["dry_run"] is True and "expires_at" in prop and "Nothing executed" in prop["message"]
        assert len(prop["plan"]["legs"]) == 2 and prop["plan"]["client"] == "test-client/9.9"
        assert prop["plan"]["qty"] == pytest.approx(4.85)  # $5,000 at 2x -> 4.85 BNB at 686.19
        assert engine.state.positions == {}, "propose must not execute"

        rc = _payload(await c.call_tool("deltr_execute_hedge", {"plan_id": prop["plan_id"], "confirm": True}))
        assert "error" not in rc
        assert rc["status"] == "filled" and rc["id"].startswith("rcpt_") and len(rc["fills"]) == 2
        assert rc["sha256"] and rc["decision"]["approved"] is True and rc["client"] == "test-client/9.9"
        assert [s["step"] for s in rc["steps"]][:2] == ["plan", "gate"]
        assert len(engine.state.positions) == 1

        again = _payload(await c.call_tool("deltr_execute_hedge", {"plan_id": prop["plan_id"], "confirm": True}))
        assert again["error"]["code"] == "PLAN_NOT_FOUND"

        got = _payload(await c.call_tool("deltr_receipt", {"receipt_id": rc["id"]}))
        assert got["id"] == rc["id"] and got["sha256"] == rc["sha256"]

        pos = _payload(await c.call_tool("deltr_positions", {}))
        assert len(pos["positions"]) == 1 and pos["portfolio"]["open_positions"] == 1
        unw = _payload(await c.call_tool("deltr_unwind", {"position_id": "all", "reason": "test"}))
        assert unw["count"] == 1 and unw["receipts"][0]["status"] == "unwound"
        assert engine.state.positions == {}


async def test_unknown_plan_and_bad_receipt_are_error_values():
    _, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        res = await c.call_tool("deltr_execute_hedge", {"plan_id": "plan_nope"})
        assert not res.isError, "errors are values, never transport errors"
        assert _payload(res)["error"]["code"] == "PLAN_NOT_FOUND"
        assert _payload(await c.call_tool("deltr_receipt", {"receipt_id": "rcpt_nope"}))["error"]["code"] == "RECEIPT_NOT_FOUND"
        assert _payload(await c.call_tool("deltr_unwind", {"position_id": "pos_nope"}))["error"]["code"] == "POSITION_NOT_FOUND"


async def test_testnet_execute_requires_confirm():
    engine, activity, mcp = build_fake_server("testnet")
    async with await _session(mcp) as c:
        prop = _payload(await c.call_tool("deltr_propose_hedge", {"capital_usd": 2000, "leverage": 2}))
        res = _payload(await c.call_tool("deltr_execute_hedge", {"plan_id": prop["plan_id"]}))
    assert res["error"]["code"] == "CONFIRM_REQUIRED"


async def test_halt_and_stress_refusals_map_by_class_name():
    _, _, mcp = build_fake_server(halted=True, refuse_stress=True)
    async with await _session(mcp) as c:
        rh = _payload(await c.call_tool("deltr_reset_halt", {"reason": "operator review"}))
        st = _payload(await c.call_tool("deltr_stress", {"kind": "basis_shock", "magnitude": 150}))
    assert rh["error"]["code"] == "HALT_NOT_CLEARABLE"
    assert st["error"]["code"] == "STRESS_REFUSED"


async def test_invalid_arguments_are_transport_validation_errors_not_trades():
    engine, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        res = await c.call_tool("deltr_propose_hedge", {"capital_usd": 5000, "leverage": 10})
    assert res.isError and "leverage" in res.content[0].text
    assert engine.calls == [] and engine.state.plans == {}


async def test_prompt_is_propose_only():
    engine, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        pr = _payload(await c.call_tool("deltr_prompt", {"text": "Rebalance $5,000 USDC into delta-neutral BNB arbitrage"}))
    assert pr["executes"] is False and pr["plan_id"] in engine.state.plans
    assert pr["intent"]["capital_usd"] == 5000 and pr["intent"]["stablecoin_note"]
    assert engine.state.positions == {}


async def test_read_only_tools_and_operator_controls():
    engine, _, mcp = build_fake_server()
    async with await _session(mcp) as c:
        st = _payload(await c.call_tool("deltr_status", {}))
        assert st["mode"] == "paper" and st["check_order"][0] == "KILL_SWITCH" and len(st["check_order"]) == 19
        mk = _payload(await c.call_tool("deltr_market", {}))
        assert mk["market"]["dex"]["exec_price_buy"] == pytest.approx(686.19) and mk["components"][-1]["kind"] == "net"
        sc = _payload(await c.call_tool("deltr_scan", {"history_points": 3}))
        assert len(sc["history"]) == 3 and "is_actionable" in sc["opportunity"]
        ex = _payload(await c.call_tool("deltr_explain_edge", {"capital_usd": 5000, "leverage": 2, "horizon_h": 24}))
        assert ex["sizing"]["base_qty"] == pytest.approx(4.85) and ex["edge"]["horizon_h"] == 24 and "net_edge_bps" in ex["formulas"]
        bad = _payload(await c.call_tool("deltr_market", {"symbol": "DOGEUSDT"}))
        assert bad["error"]["code"] == "INVALID_ARGUMENT"
        ks = _payload(await c.call_tool("deltr_kill_switch", {"on": True, "reason": "drill"}))
        assert ks["kill_switch"] is True
        veto = _payload(await c.call_tool("deltr_evaluate_risk", {"capital_usd": 5000, "leverage": 2}))
        assert veto["code"] == "KILL_SWITCH"
        _payload(await c.call_tool("deltr_kill_switch", {"on": False}))
        me = _payload(await c.call_tool("deltr_set_min_edge", {"min_edge_bps": -10}))
        assert me == {"min_edge_bps": -10.0, "floor_bps": 0.0, "mode": "paper"}
        log = _payload(await c.call_tool("deltr_risk_log", {"limit": 5, "only_vetoes": True}))
        assert log["count"] == 1 and log["decisions"][0]["code"] == "KILL_SWITCH"
        sr = _payload(await c.call_tool("deltr_stress", {"kind": "equity_shock", "magnitude": 1.0}))
        assert sr["active_label"].startswith("SIMULATED") and sr["equity_after"] < sr["equity_before"]
        rs = _payload(await c.call_tool("deltr_stress", {"kind": "reset"}))
        assert rs["active_label"] is None


# --------------------------------------------------------------------------- activity
async def test_activity_rows_carry_client_info_and_trace_id():
    engine, activity, mcp = build_fake_server()
    async with await _session(mcp) as c:
        prop = _payload(await c.call_tool("deltr_propose_hedge", {"capital_usd": 5000, "leverage": 2}))
        _payload(await c.call_tool("deltr_execute_hedge", {"plan_id": "plan_missing"}))
        act = _payload(await c.call_tool("deltr_activity", {"limit": 10}))
    rows = activity.recent(10)
    assert [r.tool for r in rows] == ["deltr_activity", "deltr_execute_hedge", "deltr_propose_hedge"]
    assert all(r.client == "test-client/9.9" and r.direction == "inbound" and r.server == "deltr" for r in rows)
    proposed = rows[-1]
    assert proposed.ok and proposed.trace_id == prop["plan_id"] and proposed.args == {"capital_usd": 5000.0, "leverage": 2.0, "symbol": "BNBUSDT"}
    assert "ctx" not in proposed.args
    failed = rows[1]
    assert failed.ok is False and failed.result_summary.startswith("error PLAN_NOT_FOUND")
    assert len(engine.state.activity) == 3, "rows are mirrored into State.activity"
    assert engine.state.events and engine.state.events[-1].topic == "mcp"
    assert act["count"] == 2 and act["activity"][0]["tool"] == "deltr_execute_hedge"


def test_redact_drops_secrets_and_truncates():
    out = redact({"api_key": "x", "BINANCE_SECRET_KEY": "y", "token": "z", "Password": "p", "capital_usd": 5, "text": "a" * 500, "nested": {"secret": 1, "ok": [1, "b"]}})
    assert set(out) == {"capital_usd", "text", "nested"}
    assert out["text"].startswith("a" * 200) and "+300 chars" in out["text"]
    assert out["nested"] == {"ok": [1, "b"]}


async def test_in_process_call_without_request_context_uses_default_client_name():
    engine, activity, mcp = build_fake_server()
    await mcp.call_tool("deltr_status", {})
    assert activity.recent(1)[0].client == "mcp-client"


def test_instrumented_works_on_sync_and_async_functions():
    from deltr.mcp.activity import instrumented

    log = ActivityLog(FakeState())
    wrap = instrumented(log, "deltr")

    @wrap
    def sync_tool(x: int) -> dict:
        return {"plan_id": "plan_abc", "x": x}

    @wrap
    async def async_tool(x: int) -> dict:
        return {"error": {"code": "INVALID_ARGUMENT", "message": "nope"}}

    assert sync_tool(1) == {"plan_id": "plan_abc", "x": 1}
    assert asyncio.run(async_tool(2))["error"]["code"] == "INVALID_ARGUMENT"
    rows = log.recent()
    assert rows[1].tool == "sync_tool" and rows[1].trace_id == "plan_abc" and rows[1].ok
    assert rows[0].tool == "async_tool" and rows[0].ok is False


# --------------------------------------------------------------------------- resources
async def test_resources_are_readable_json_and_never_leak_secrets():
    engine, _, mcp = build_fake_server("testnet")
    async with await _session(mcp) as c:
        listed = await c.list_resources()
        uris = {str(r.uri) for r in listed.resources}
        assert uris == {"deltr://status", "deltr://risk-limits", "deltr://config"}
        status = json.loads((await c.read_resource("deltr://status")).contents[0].text)
        limits = json.loads((await c.read_resource("deltr://risk-limits")).contents[0].text)
        config = json.loads((await c.read_resource("deltr://config")).contents[0].text)
    assert status["mode"] == "testnet" and status["secrets_present"] is True
    assert limits["check_order"] == status["check_order"] and limits["limits"]["max_leverage"] == 3.0 and limits["gate_median_us"] > 0
    assert config["secrets_present"] is True and config["mode"] == "testnet"
    blob = json.dumps(config) + json.dumps(status) + json.dumps(limits)
    assert "test-secret-not-real" not in blob and "test-key-not-real" not in blob


# --------------------------------------------------------------------------- stdout cleanliness
def test_import_and_build_write_nothing_to_stdout():
    code = (
        "import deltr.mcp.server, deltr.mcp.activity, deltr.mcp.binance_shim_server as s\n"
        "from tests.helpers_mcp import build_fake_server, make_settings\n"
        "build_fake_server(); s.build_shim(make_settings())\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(REPO)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


async def test_stdout_clean_under_stdio_transport():
    params = StdioServerParameters(command=sys.executable, args=["-m", "tests.helpers_mcp"], cwd=str(REPO),
                                   env={**os.environ, "PYTHONPATH": str(REPO)})

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w, client_info=CLIENT) as s:
                await s.initialize()
                tools = await s.list_tools()
                st = await s.call_tool("deltr_status", {})
                return [t.name for t in tools.tools], _payload(st)

    names, status = await asyncio.wait_for(run(), timeout=60)
    assert sorted(names) == sorted(TOOL_NAMES)
    assert status["mode"] == "paper"


# --------------------------------------------------------------------------- client config files
def test_repo_client_configs_point_at_the_mounted_http_endpoint():
    mcp_json = json.loads((REPO / ".mcp.json").read_text())
    assert mcp_json["mcpServers"]["deltr"]["url"] == "http://127.0.0.1:8000/mcp"
    assert mcp_json["mcpServers"]["binance-mcp-server"]["url"] == "https://agent.binance.com/mcp/agentic"
    desktop = json.loads((REPO / "claude_desktop_config.example.json").read_text())
    assert desktop["mcpServers"]["deltr"]["args"][-1] == "http://127.0.0.1:8000/mcp"
    stdio = desktop["mcpServers"]["deltr-stdio-alternative"]
    assert stdio["command"].endswith("/.venv/bin/python") and stdio["args"][-1] == "--mcp"


def test_fake_engine_settings_helper_is_offline():
    s = make_settings()
    assert s.mode.value == "paper" and s.hosts["futures_order_rest"] == "" and FakeEngine(s).settings is s
