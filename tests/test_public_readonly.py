"""Public read-only mode: a VPS showcase must not let strangers move the book.

Every mutating /api route is refused without DELTR_API_TOKEN, every mutating MCP tool
answers READ_ONLY, and all read surfaces keep working. Off by default.
"""

from __future__ import annotations

import pytest

from deltr.config import Settings
from deltr.mcp.server import MUTATING_TOOLS, TOOL_NAMES, apply_public_readonly


def _settings(**over):
    base = dict(_env_file=None, DELTR_MODE="paper", DELTR_PUBLIC_READONLY="1", DELTR_API_TOKEN="judge-token-123")
    base.update(over)
    return Settings(**base)  # type: ignore[call-arg]


def test_readonly_is_off_by_default():
    s = Settings(_env_file=None, DELTR_MODE="paper")  # type: ignore[call-arg]
    assert s.public_readonly is False and s.api_token is None


def test_redacted_never_shows_the_token():
    s = _settings()
    r = s.redacted()
    assert r["public_readonly"] is True
    assert r["api_token_present"] is True
    assert "judge-token-123" not in str(r)


def test_mutating_tool_list_is_the_documented_set():
    for name in MUTATING_TOOLS:
        assert name in TOOL_NAMES, name
    # read tools are never in the mutating set
    for name in ("deltr_status", "deltr_market", "deltr_scan", "deltr_positions", "deltr_funding_history"):
        assert name not in MUTATING_TOOLS



async def test_mcp_mutating_tools_answer_read_only(monkeypatch):
    from tests.helpers_mcp import build_fake_server

    engine, activity, mcp = build_fake_server("paper")
    object.__setattr__(engine, "settings", _settings())
    disabled = apply_public_readonly(mcp, engine)
    assert set(disabled) == set(MUTATING_TOOLS) & set(TOOL_NAMES)
    res = await mcp.call_tool("deltr_kill_switch", {"on": True})
    text = res[0].text if isinstance(res, list) else str(res)
    assert "READ_ONLY" in text
    # a read tool still works
    res = await mcp.call_tool("deltr_status", {})
    text = res[0].text if isinstance(res, list) else str(res)
    assert "READ_ONLY" not in text


def test_apply_is_a_no_op_when_not_readonly():
    from tests.helpers_mcp import build_fake_server

    engine, activity, mcp = build_fake_server("paper")
    object.__setattr__(engine, "settings", Settings(_env_file=None, DELTR_MODE="paper"))  # type: ignore[call-arg]
    assert apply_public_readonly(mcp, engine) == []



async def test_api_mutations_need_the_token():
    from httpx import ASGITransport, AsyncClient

    from deltr.api.app import create_app
    from tests.helpers_mcp import build_fake_server

    engine, activity, mcp = build_fake_server("paper")
    object.__setattr__(engine, "settings", _settings())
    app = create_app(engine, mcp, activity)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # GETs are never gated (the fake engine may not implement every read route; 403 is the only wrong answer)
        assert (await c.get("/api/health")).status_code != 403
        r = await c.post("/api/kill", json={"on": True})
        assert r.status_code == 403 and r.json()["error"]["code"] == "READ_ONLY"
        r = await c.post("/api/kill", json={"on": True}, headers={"X-Deltr-Token": "wrong"})
        assert r.status_code == 403
        r = await c.post("/api/kill", json={"on": True}, headers={"X-Deltr-Token": "judge-token-123"})
        assert r.status_code != 403
