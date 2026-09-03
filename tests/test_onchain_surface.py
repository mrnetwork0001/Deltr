"""tests/test_onchain_surface.py — the on-chain leg and x402 as MCP tools and REST routes.

Two layers:

* the REAL :class:`deltr.engine.Engine` pre-flight, driven with a FAKE CLI runner injected into
  its wallet client, so the refusal order (kill switch -> halt -> arming -> chain -> per-request
  cap -> aggregate cap -> confirm) is exercised against the real gate state;
* the MCP tools and the REST routes over the fake engine / a replay engine.

Nothing here invokes ``baw``, places an order, or moves funds.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from deltr.api.app import create_app
from deltr.config import ONCHAIN_ACK_PHRASE
from deltr.engine import OnchainRefused, build_engine
from deltr.mcp.server import ERROR_CODES, TOOL_NAMES, build_mcp
from deltr.payments import PAYMENT_REQUIRED_HEADER
from tests.conftest import REPLAY_FIXTURE, make_replay_settings, refusing_http
from tests.helpers_mcp import build_fake_server
from tests.test_agentic_wallet import FakeCli, ok

USDT = "0x55d398326f99059fF775485246999027B3197955"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

ARMED = {"DELTR_ONCHAIN_MODE": "live", "DELTR_ONCHAIN_ACK": ONCHAIN_ACK_PHRASE}
SWAP_SCRIPT = {
    "wallet status": ok({"status": "CONNECTED", "address": "0x" + "1" * 40}),
    "market-order quote": ok({"toTokenAmount": "0.5", "priceImpact": "0.1"}),
    "market-order swap": ok({"status": "FINISHED", "txHash": "0x" + "b" * 64, "toTokenAmount": "0.5", "orderId": "7"}),
}


# --------------------------------------------------------------------------- real-engine pre-flight
@pytest.fixture
def onchain_engine(tmp_path: Path):
    """A started PAPER replay engine whose wallet client runs a fake CLI."""

    def _make(script: dict[str, Any] | None = None, **extra: Any):
        settings = make_replay_settings(tmp_path / "state", **extra)
        eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http())
        eng.wallet.binary = "/bin/sh"                       # resolves, so require_available() passes
        eng.wallet._runner = FakeCli(dict(script or SWAP_SCRIPT))
        asyncio.run(eng.start(loops=False, benchmark_iterations=200))
        return eng

    made: list[Any] = []

    def factory(*a: Any, **kw: Any):
        eng = _make(*a, **kw)
        made.append(eng)
        return eng

    yield factory
    for eng in made:
        asyncio.run(eng.stop())


def refuse(eng: Any, **kw: Any) -> OnchainRefused:
    body = {"from_token": USDT, "to_token": WBNB, "amount": 1.0, "notional_usd": 100.0, "confirm": True}
    body.update(kw)
    with pytest.raises(OnchainRefused) as exc:
        asyncio.run(eng.onchain_swap(**body))
    return exc.value


def test_an_unarmed_engine_refuses_the_swap_and_names_the_missing_switch(onchain_engine):
    eng = onchain_engine()
    err = refuse(eng)
    assert err.code == "ONCHAIN_NOT_ARMED"
    assert "DELTR_ONCHAIN_MODE=live" in err.reason


def test_selecting_the_leg_without_the_acknowledgement_still_refuses(onchain_engine):
    eng = onchain_engine(DELTR_ONCHAIN_MODE="live")
    err = refuse(eng)
    assert err.code == "ONCHAIN_NOT_ARMED" and "DELTR_ONCHAIN_ACK" in err.reason


def test_an_armed_engine_still_requires_confirm(onchain_engine):
    eng = onchain_engine(**ARMED)
    err = refuse(eng, confirm=False)
    assert err.code == "CONFIRM_REQUIRED" and "confirm=true" in err.reason


def test_the_kill_switch_closes_the_on_chain_leg_first(onchain_engine):
    eng = onchain_engine(**ARMED)
    eng.gate.set_kill_switch(True)
    err = refuse(eng)
    assert err.code == "KILL_SWITCH"


def test_a_drawdown_halt_closes_the_on_chain_leg(onchain_engine):
    eng = onchain_engine(**ARMED)
    eng.gate.state = "HALTED"
    assert eng.gate.halted is True
    err = refuse(eng)
    assert err.code == "HALTED_DRAWDOWN"


def test_the_default_per_request_cap_is_small_and_enforced(onchain_engine):
    eng = onchain_engine(**ARMED)
    assert eng.settings.onchain_max_notional_usd == 250.0
    assert eng.settings.onchain_max_aggregate_usd == 1000.0
    err = refuse(eng, notional_usd=251.0)
    assert err.code == "MAX_NOTIONAL" and "DELTR_ONCHAIN_MAX_NOTIONAL_USD" in err.reason


def test_the_aggregate_cap_is_enforced_across_a_run(onchain_engine):
    eng = onchain_engine(**ARMED, DELTR_ONCHAIN_MAX_AGGREGATE_USD=300.0)
    asyncio.run(eng.onchain_swap(from_token=USDT, to_token=WBNB, amount=1.0, notional_usd=200.0, confirm=True))
    err = refuse(eng, notional_usd=200.0)
    assert err.code == "AGGREGATE_NOTIONAL"


def test_another_chain_is_refused(onchain_engine):
    eng = onchain_engine(**ARMED)
    err = refuse(eng, chain_id=1)
    assert err.code == "CHAIN_NOT_ALLOWED"


def test_slippage_above_the_configured_ceiling_is_refused(onchain_engine):
    eng = onchain_engine(**ARMED)
    err = refuse(eng, slippage=40.0)
    assert err.code == "INVALID_ARGUMENT" and "ceiling" in err.reason


def test_an_armed_confirmed_swap_previews_executes_and_records_a_decision(onchain_engine):
    eng = onchain_engine(**ARMED)
    before = len(eng.state.decisions)
    out = asyncio.run(eng.onchain_swap(from_token=USDT, to_token=WBNB, amount=343.0, notional_usd=200.0, confirm=True))
    keys = [FakeCli.key(a) for a in eng.wallet._runner.calls]
    assert keys == ["wallet status", "market-order quote", "market-order swap"], keys
    assert out["swap"]["confirmed"] is True and out["swap"]["tx_hash"].startswith("0x")
    assert out["decision"]["approved"] is True and out["decision"]["code"] == "OK"
    assert len(eng.state.decisions) > before, "every value-moving request is recorded in the risk log"
    assert "Deltr holds no key" in out["swap"]["custody"]


def test_a_refused_swap_is_recorded_in_the_risk_log_too(onchain_engine):
    eng = onchain_engine(**ARMED)
    before = len(eng.state.decisions)
    err = refuse(eng, confirm=False)
    assert err.decision is not None and err.decision.approved is False
    assert len(eng.state.decisions) > before


def test_a_signed_out_wallet_refuses_with_the_signin_command(onchain_engine):
    eng = onchain_engine({**SWAP_SCRIPT, "wallet status": ok({"status": "UNCONNECTED"})}, **ARMED)
    from deltr.venues.agentic_wallet import AgenticWalletError

    with pytest.raises(AgenticWalletError) as exc:
        asyncio.run(eng.onchain_swap(from_token=USDT, to_token=WBNB, amount=1.0, notional_usd=100.0, confirm=True))
    assert exc.value.code == "BAW_NOT_SIGNED_IN" and "baw auth signin" in (exc.value.remedy or "")
    assert [FakeCli.key(a) for a in eng.wallet._runner.calls] == ["wallet status"], "no swap may be attempted"


def test_wallet_status_is_read_only_and_never_starts_a_signin(onchain_engine):
    eng = onchain_engine(**ARMED)
    out = asyncio.run(eng.wallet_status())
    assert out["installed"] is True and out["signed_in"] is True
    assert "never holds, reads, stores or signs" in out["custody"]
    assert out["armed"] is True and out["caps"]["per_request_usd"] == 250.0
    assert all(FakeCli.key(a) not in ("auth signin", "auth verify") for a in eng.wallet._runner.calls)


def test_wallet_status_reports_a_missing_cli_without_simulating(onchain_engine):
    eng = onchain_engine(**ARMED)
    eng.wallet.binary = "definitely-not-installed-baw"
    out = asyncio.run(eng.wallet_status())
    assert out["installed"] is False and out["signed_in"] is False
    assert out["error"]["code"] == "BAW_NOT_INSTALLED"
    assert "does not fall back to a simulated" in out["error"]["remedy"]


def test_the_seller_challenge_is_refused_until_a_receiving_address_is_configured(onchain_engine):
    eng = onchain_engine(**ARMED)
    from deltr.payments import PaymentError

    with pytest.raises(PaymentError) as exc:
        eng.x402_edge_report_challenge()
    assert exc.value.code == "X402_NO_PAY_TO"


def test_the_seller_challenge_seals_the_real_edge_report(onchain_engine):
    eng = onchain_engine(**ARMED, DELTR_X402_PAY_TO="0x" + "9" * 40)
    out = eng.x402_edge_report_challenge(capital_usd=5_000.0, leverage=2.0, horizon_h=24.0)
    assert out["status_code"] == 402
    option = out["body"]["accepts"][0]
    assert option["network"] == "bsc" and option["payTo"] == "0x" + "9" * 40
    assert len(option["extra"]["artifact_sha256"]) == 64
    assert "Round-trip cost waterfall" in out["artifact_preview"] or out["artifact_preview"]
    assert "No mainnet B402 application was made" in json.dumps(out)


# --------------------------------------------------------------------------- MCP tools
def test_the_three_tools_are_registered():
    for name in ("deltr_wallet_status", "deltr_onchain_swap", "deltr_x402_pay"):
        assert name in TOOL_NAMES


def test_the_wallet_error_codes_are_declared():
    for code in ("ONCHAIN_NOT_ARMED", "CHAIN_NOT_ALLOWED", "BAW_NOT_INSTALLED", "BAW_NOT_SIGNED_IN", "BAW_TIMEOUT"):
        assert code in ERROR_CODES


async def call(mcp: Any, name: str, args: dict[str, Any]) -> Any:
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(mcp._mcp_server) as client:
        res = await client.call_tool(name, args)
    return json.loads(res.content[0].text)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mcp_wallet_status_is_read_only():
    _engine, _activity, mcp = build_fake_server()
    out = await call(mcp, "deltr_wallet_status", {})
    assert out["installed"] is True and "custody" in out
    assert "never holds, reads, stores or signs" in out["custody"]


@pytest.mark.asyncio
async def test_mcp_onchain_swap_refuses_without_arming_then_without_confirm():
    engine, _activity, mcp = build_fake_server()
    args = {"from_token": USDT, "to_token": WBNB, "amount": 1.0, "notional_usd": 100.0}
    out = await call(mcp, "deltr_onchain_swap", args)
    assert out["error"]["code"] == "ONCHAIN_NOT_ARMED"

    engine.settings = engine.settings.model_copy(update={"onchain_mode": "live", "onchain_ack": ONCHAIN_ACK_PHRASE})
    out = await call(mcp, "deltr_onchain_swap", args)
    assert out["error"]["code"] == "CONFIRM_REQUIRED"
    out = await call(mcp, "deltr_onchain_swap", {**args, "confirm": True})
    assert out["swap"]["confirmed"] is True and out["decision"]["approved"] is True


@pytest.mark.asyncio
async def test_mcp_x402_pay_requires_confirm():
    engine, _activity, mcp = build_fake_server()
    engine.settings = engine.settings.model_copy(update={"onchain_mode": "live", "onchain_ack": ONCHAIN_ACK_PHRASE})
    payload = json.dumps({"x402Version": 2, "accepts": [{"scheme": "exact", "network": "bsc"}]})
    out = await call(mcp, "deltr_x402_pay", {"payment_required": payload})
    assert out["error"]["code"] == "CONFIRM_REQUIRED"
    out = await call(mcp, "deltr_x402_pay", {"payment_required": payload, "confirm": True})
    assert out["payment"]["header_name"] == "X-PAYMENT"


@pytest.mark.asyncio
async def test_the_activity_log_never_records_a_token_argument():
    engine, activity, mcp = build_fake_server()
    await call(mcp, "deltr_onchain_swap", {"from_token": USDT, "to_token": WBNB, "amount": 1.0, "notional_usd": 100.0})
    row = activity.recent(1)[0]
    assert "from_token" not in row.args and "to_token" not in row.args, "key/token-named args are redacted"
    assert row.args["notional_usd"] == 100.0


# --------------------------------------------------------------------------- REST routes
@pytest.fixture
def api(tmp_path: Path) -> Iterator[TestClient]:
    settings = make_replay_settings(tmp_path / "state", DELTR_X402_PAY_TO="0x" + "9" * 40)
    eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http())
    eng.wallet.binary = "/bin/sh"
    eng.wallet._runner = FakeCli(dict(SWAP_SCRIPT))
    asyncio.run(eng.start(loops=False, benchmark_iterations=200))
    mcp = build_mcp(eng, eng.activity)
    app = create_app(eng, mcp, eng.activity, ui_dir=tmp_path / "no-ui")
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as c:
            yield c
    finally:
        asyncio.run(eng.stop())


def test_rest_wallet_status_is_read_only(api: TestClient):
    r = api.get("/api/wallet/status")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is True and body["armed"] is False
    assert "DELTR_ONCHAIN_MODE=live" in body["arming_error"]


def test_rest_onchain_swap_refuses_unarmed_with_409(api: TestClient):
    r = api.post("/api/onchain/swap", json={"from_token": USDT, "to_token": WBNB, "amount": 1.0, "notional_usd": 100.0, "confirm": True})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ONCHAIN_NOT_ARMED"


def test_rest_x402_pay_refuses_unarmed_with_409(api: TestClient):
    payload = json.dumps({"x402Version": 2, "accepts": [{"scheme": "exact", "network": "bsc", "maxAmountRequired": "1"}]})
    r = api.post("/api/x402/pay", json={"payment_required": payload, "confirm": True})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ONCHAIN_NOT_ARMED"


def test_rest_seller_answers_a_real_402_with_the_payment_required_header(api: TestClient):
    r = api.get("/api/x402/edge-report")
    assert r.status_code == 402
    header = r.headers.get(PAYMENT_REQUIRED_HEADER) or r.headers.get(PAYMENT_REQUIRED_HEADER.lower())
    assert header, r.headers
    decoded = json.loads(base64.b64decode(header))
    assert decoded == r.json()
    option = decoded["accepts"][0]
    assert option["network"] == "bsc" and option["mimeType"] == "text/markdown"
    assert len(option["extra"]["artifact_sha256"]) == 64
    assert "no settlement is claimed" in option["extra"]["scope"].lower()


def test_rest_seller_preview_returns_200_without_the_paywall(api: TestClient):
    body = api.get("/api/x402/edge-report", params={"preview": True}).json()
    assert body["status_code"] == 402 and body["network"] == "bsc"
    assert "markdown" not in body["artifact"], "the paid artifact is not given away in the challenge"
    assert body["artifact"]["bytes"] > 0 and len(body["artifact"]["sha256"]) == 64


def test_config_reports_the_on_chain_arming_state_without_any_secret(api: TestClient):
    cfg = api.get("/api/config").json()
    assert cfg["onchain_armed"] is False and cfg["onchain_mode"] == "off"
    assert cfg["onchain_max_notional_usd"] == 250.0
    assert cfg["x402_pay_to_present"] is True
    assert not any(k.lower().endswith(("_key", "secret", "token")) for k in cfg), sorted(cfg)
