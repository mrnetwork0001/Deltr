"""tests/test_shim_tool_names.py — the shipped official tool-name fixture and the mapping it drives.

Proves three things that the honesty pitch depends on:

1. ``tests/fixtures/binance_mcp_tools.json`` maps the shim's read-only tools onto the
   official dotted names (``spot.ticker24hr``, ``futures_usds.premiumIndexKlineData``, …)
   and the shim actually serves under them.
2. The shim's WRITE tools are never aliased onto a read-only official name.  The published
   inventory contains no write-capable trade tool, so ``place_futures_order`` and
   ``set_leverage`` must keep their Deltr-local names.
3. The fixture carries a provenance block that says where the names came from.  As long as
   the fixture is the transcription, that block must say the names were **not captured
   from our own session** — so the claim cannot silently rot into "verified with Binance".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deltr.mcp.binance_shim_server import (
    DEFAULT_TOOL_NAMES,
    FIXTURE_PATH,
    build_shim,
    resolve_tool_names,
)
from tests.helpers_mcp import make_settings

READ_ONLY_CATEGORIES = {"MARKET_DATA", "ACCOUNT_READ", "ANALYSIS", "GATEWAY"}

# What the shipped fixture is expected to bind.  Every value must exist in the fixture.
EXPECTED_OFFICIAL: dict[str, str] = {
    "get_ticker": "spot.ticker24hr",
    "get_order_book": "spot.depth",
    "get_mark_price": "futures_usds.premiumIndexKlineData",
    "get_exchange_filters": "futures_usds.exchangeInformation",
    "get_account": "futures_usds.futuresAccountBalanceV3",
    "get_positions": "futures_usds.positionInformationV2",
}

# Shim tools with no counterpart in the published inventory: both write tools (it exposes
# none) and the funding-rate read (it exposes none).  These keep their local names.
EXPECTED_LOCAL = ("place_futures_order", "set_leverage", "get_funding_rate")


@pytest.fixture(scope="module")
def fixture_doc() -> dict:
    assert FIXTURE_PATH.exists(), f"missing official tool-name fixture at {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def official_names(fixture_doc: dict) -> list[str]:
    return [t["name"] for t in fixture_doc["tools"]]


# --------------------------------------------------------------------------- provenance
def test_provenance_block_is_present_and_states_where_the_names_came_from(fixture_doc: dict):
    prov = fixture_doc.get("provenance")
    assert isinstance(prov, dict), "the fixture must carry a machine-readable provenance block"
    statement = prov["statement"]

    if prov["captured_from_our_own_session"] is False:
        # The shipped state: transcribed from a third-party published inventory.
        assert prov["method"] == "transcribed_from_third_party_published_inventory"
        assert prov["official_endpoint_reached_from_this_machine"] is False
        assert "not captured from our own session" in statement
        assert "third-party" in statement and prov["source_dated"] == "2026-09-02"
        assert "likeMdl/binance-ai-risk-trader" in statement
        assert prov["source_repo"] == "likeMdl/binance-ai-risk-trader"
        assert "captured from our own OAuth session" in prov["not_claimed"]
        assert "verified against Binance" in prov["not_claimed"]
    else:
        # A first-party capture (scripts/probe_binance_mcp.py inside a real OAuth session)
        # must say so explicitly and date itself; it may not keep the transcription wording.
        assert prov["captured_from_our_own_session"] is True
        assert prov["official_endpoint_reached_from_this_machine"] is True
        assert prov["method"] == "captured_from_our_own_oauth_session"
        assert "not captured from our own session" not in statement
        assert prov["captured_on"]


def test_provenance_records_zero_write_capable_trade_tools(fixture_doc: dict):
    counts = fixture_doc["provenance"]["source_reported_counts"]
    assert counts["write_capable_trade_tools"] == 0
    assert counts["transfer_tools"] == 0
    assert any("0 write-capable trade tools" in o for o in fixture_doc["provenance"]["observations"])


def test_fixture_holds_the_published_inventory_and_no_write_tools(fixture_doc: dict, official_names: list[str]):
    counts = fixture_doc["provenance"]["source_reported_counts"]
    assert len(official_names) == counts["tools_total"] == 50
    assert len(set(official_names)) == len(official_names), "duplicate official tool name"
    by_category: dict[str, int] = {}
    for tool in fixture_doc["tools"]:
        assert tool["category"] in READ_ONLY_CATEGORIES, f"{tool['name']} is not a read-only category"
        by_category[tool["category"]] = by_category.get(tool["category"], 0) + 1
    assert by_category["MARKET_DATA"] == counts["market_data"] == 24
    assert by_category["ACCOUNT_READ"] == counts["account_read"] == 23
    # Rows the source did not enumerate verbatim have to admit it.
    for tool in fixture_doc["tools"]:
        assert tool["transcription"] in {"verbatim", "mirrored_from_futures_usds"}
        if tool["name"].startswith("futures_coin."):
            assert tool["transcription"] == "mirrored_from_futures_usds"


# --------------------------------------------------------------------------- mapping
def test_shipped_fixture_maps_the_shim_onto_the_official_names(official_names: list[str]):
    names = resolve_tool_names(FIXTURE_PATH)
    for ours, official in EXPECTED_OFFICIAL.items():
        assert names[ours] == official, f"{ours} should serve as {official}, got {names[ours]}"
        assert official in official_names
    assert len(set(names.values())) == len(names), "two shim tools claimed the same official name"


def test_write_tools_are_never_aliased_onto_a_read_only_official_name(official_names: list[str]):
    names = resolve_tool_names(FIXTURE_PATH)
    for ours in EXPECTED_LOCAL:
        assert names[ours] == DEFAULT_TOOL_NAMES[ours], f"{ours} must keep its Deltr-local name"
        assert names[ours] not in official_names
    # The near-miss that motivates this test: a read-only query tool exists in the
    # inventory, and the shim's order-placing tool must not borrow its name.
    assert "futures_usds.queryOrder" in official_names
    assert names["place_futures_order"] != "futures_usds.queryOrder"
    assert not any(names["place_futures_order"] == o or names["set_leverage"] == o for o in official_names)


def test_a_write_shaped_official_name_would_still_not_be_claimed_by_a_read_tool(tmp_path: Path):
    """Guard the hint table itself: read tools must not drift onto an order-placing name."""
    fx = tmp_path / "binance_mcp_tools.json"
    tools = [{"name": "futures_usds.queryOrder"}, {"name": "futures_usds.newOrder"}]
    fx.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    names = resolve_tool_names(fx)
    assert names["place_futures_order"] == "futures_usds.newOrder"
    assert names["get_positions"] == "get_positions" and names["get_account"] == "get_account"


# --------------------------------------------------------------------------- served surface
async def test_shim_serves_the_official_names_from_the_shipped_fixture():
    mcp = build_shim(make_settings(), fixture_path=FIXTURE_PATH)
    served = sorted(t.name for t in await mcp.list_tools())
    assert served == sorted({**DEFAULT_TOOL_NAMES, **EXPECTED_OFFICIAL}.values())
    assert "futures_usds.premiumIndexKlineData" in served and "spot.depth" in served
    assert "place_futures_order" in served and "futures_usds.queryOrder" not in served
