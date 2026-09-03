"""tests/test_horizon.py — the breakeven holding period, the edge-versus-horizon curve
and the rendered edge report (win-plan items A2 and A7).

Proves:

* ``breakeven_settlements`` is now reachable from a caller: the analysis expresses it in
  settlements, hours and days beside the configured horizon and produces a one-line verdict;
* the curve is the same arithmetic sampled over holding horizons, and its zero crossing
  agrees with the breakeven;
* **the honesty guardrail**: whenever an assumed (mainnet-typical) funding rate is used, the
  label "assumption, not a measurement" is present in the JSON, in the markdown report and in
  every derived view, the measured testnet rate is reported beside it, and the measured view is
  never marked as an assumption.  No assumed rate means no assumption label anywhere;
* the ``deltr_explain_edge`` payload and the new ``deltr_edge_report`` tool carry all of it.
"""
from __future__ import annotations

import json
import math

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from deltr import horizon as hz
from deltr.horizon import (
    ASSUMED_FUNDING_RATE_PER_INTERVAL,
    ASSUMPTION_LABEL,
    HorizonAnalysis,
    horizon_analysis,
    render_edge_report,
)
from deltr.models import EdgeBreakdown
from tests.helpers_mcp import build_fake_server

pytestmark = pytest.mark.anyio


def make_edge(*, rate: float, horizon_h: float = 72.0, basis: float = 2.16, notional: float = 3_334.89) -> EdgeBreakdown:
    """The probe numbers from the design plan: 16.64 bps round trip, +2.16 bps entry basis."""
    roundtrip = 2 * (1.0 + 0.304 + 2.0 + 5.0 + 0.017)
    settlements = int(horizon_h // 8)
    funding = rate * settlements * 1e4
    net = basis + funding - roundtrip
    return EdgeBreakdown(
        notional_usd=notional, basis_entry_bps=basis, dex_fee_bps=1.0, dex_impact_bps=0.304, perp_slip_bps=2.0,
        cex_taker_bps=5.0, gas_bps_leg=0.017, roundtrip_cost_bps=roundtrip, funding_rate_last=rate,
        horizon_h=horizon_h, settlements=settlements, funding_bps_horizon=funding, net_edge_bps=net,
        expected_edge_usd=net / 1e4 * notional, allocated_risk_usd=notional * (roundtrip + 100) / 1e4,
    )


# --------------------------------------------------------------------------- breakeven
def test_breakeven_is_expressed_in_settlements_hours_and_days_with_a_verdict():
    a = horizon_analysis(make_edge(rate=0.0001, horizon_h=72.0), interval_h=8)
    be = a.breakeven
    # 16.64 round trip - 2.16 entry basis = 14.48 bps to repay at 1 bps per settlement
    assert math.isclose(a.cost_to_recover_bps, 14.48, abs_tol=0.01)
    assert be.settlements == 15 and be.hours == 120.0 and math.isclose(be.days, 5.0)
    assert be.rate_basis == "measured" and be.is_assumption is False and be.assumption_label is None
    assert be.horizon_days == 3.0 and be.reached_within_horizon is False
    assert "breaks even in 15 settlements" in be.verdict and "5.0 days" in be.verdict
    assert "horizon is 3.0 days, so the answer is no" in be.verdict
    assert a.verdict == be.verdict


def test_a_horizon_long_enough_to_pay_for_the_round_trip_answers_yes():
    a = horizon_analysis(make_edge(rate=0.0001, horizon_h=240.0), interval_h=8)
    assert a.breakeven.settlements == 15 and a.breakeven.reached_within_horizon is True
    assert "so the answer is yes" in a.verdict


def test_zero_testnet_funding_never_repays_the_round_trip():
    a = horizon_analysis(make_edge(rate=0.0), interval_h=8, auto_assume=False)
    assert a.breakeven.settlements is None and a.breakeven.hours is None and a.breakeven.days is None
    assert "never repays" in a.verdict and a.curve.zero_cross_h is None
    assert a.assumed_rate is None and a.assumption_label is None and a.curve_assumed is None


def test_entry_basis_that_already_covers_the_round_trip_needs_no_holding_period():
    a = horizon_analysis(make_edge(rate=0.0, basis=40.0), interval_h=8)
    assert a.breakeven.settlements == 0 and a.breakeven.reached_within_horizon is True
    assert "no holding period is needed" in a.verdict


# --------------------------------------------------------------------------- curve
def test_curve_is_the_same_arithmetic_and_crosses_zero_where_the_breakeven_says():
    a = horizon_analysis(make_edge(rate=0.0001, horizon_h=72.0), interval_h=8)
    pts = a.curve.points
    assert pts[0].settlements == 0 and math.isclose(pts[0].net_edge_bps, -a.cost_to_recover_bps)
    for p in pts:
        assert math.isclose(p.funding_bps, a.measured_rate * p.settlements * 1e4, abs_tol=1e-9)
        assert math.isclose(p.net_edge_bps, a.basis_entry_bps + p.funding_bps - a.roundtrip_cost_bps - a.basis_exit_assumed_bps, abs_tol=1e-9)
        assert math.isclose(p.horizon_days, p.horizon_h / 24.0)
    assert a.curve.zero_cross_h == a.breakeven.settlements * a.curve.interval_h
    crossing = [p for p in pts if p.horizon_h == a.curve.zero_cross_h][0]
    assert crossing.net_edge_bps >= 0
    assert a.curve.horizon_h == 72.0 and a.curve.max_horizon_h >= a.curve.zero_cross_h
    assert "floor(horizon / interval)" in a.curve.note


def test_curve_point_count_is_bounded():
    a = horizon_analysis(make_edge(rate=1e-7, horizon_h=72.0), interval_h=8, max_horizon_h=720.0, max_points=16)
    assert 2 <= len(a.curve.points) <= 20


# --------------------------------------------------------------------------- honesty guardrail
def _assumption_is_labelled(a: HorizonAnalysis) -> None:
    """Guardrail 5: an assumed rate is labelled as one, everywhere, beside the measured rate."""
    assert a.assumed_rate is not None
    assert a.assumption_label == ASSUMPTION_LABEL
    assert a.assumption_note and "not a rate Deltr measured" in a.assumption_note
    # the measured rate is still reported, and the measured views stay measured
    assert a.measured_rate == a.breakeven.rate_per_interval == a.curve.rate_per_interval
    assert a.breakeven.rate_basis == "measured" and a.breakeven.is_assumption is False
    assert a.breakeven.assumption_label is None
    assert a.curve.rate_basis == "measured" and a.curve.is_assumption is False
    for view in (a.breakeven_assumed, a.curve_assumed):
        assert view is not None
        assert view.rate_basis == "assumed" and view.is_assumption is True
        assert view.assumption_label == ASSUMPTION_LABEL
        assert view.rate_per_interval == a.assumed_rate
    assert "assumed rate" in a.breakeven_assumed.verdict


def test_near_zero_measured_rate_adds_a_labelled_mainnet_typical_assumption():
    a = horizon_analysis(make_edge(rate=0.0), interval_h=8)
    assert a.assumed_rate == ASSUMED_FUNDING_RATE_PER_INTERVAL
    _assumption_is_labelled(a)
    assert a.measured_rate == 0.0 and a.measured_rate_source == "binance-futures-testnet"
    assert "testnet" in a.measured_rate_note.lower()
    assert a.breakeven_assumed.settlements == 15 and math.isclose(a.breakeven_assumed.days, 5.0)
    assert a.curve_assumed.zero_cross_h == 120.0


def test_an_explicit_assumed_rate_is_always_labelled_even_when_funding_is_measurable():
    a = horizon_analysis(make_edge(rate=0.0003), interval_h=8, assumed_rate=0.0001)
    assert a.measured_rate == 0.0003 and a.breakeven.settlements == 5  # measured view unchanged
    _assumption_is_labelled(a)


def test_the_assumption_label_survives_json_serialisation():
    payload = json.loads(horizon_analysis(make_edge(rate=0.0), interval_h=8).model_dump_json())
    assert payload["assumed_rate"] == ASSUMED_FUNDING_RATE_PER_INTERVAL
    assert payload["assumption_label"] == ASSUMPTION_LABEL
    assert payload["breakeven_assumed"]["assumption_label"] == ASSUMPTION_LABEL
    assert payload["curve_assumed"]["assumption_label"] == ASSUMPTION_LABEL
    assert payload["curve_assumed"]["is_assumption"] is True
    assert payload["measured_rate"] == 0.0 and payload["curve"]["is_assumption"] is False


def test_no_assumed_rate_means_no_assumption_label_anywhere():
    a = horizon_analysis(make_edge(rate=0.0003), interval_h=8)
    assert a.assumed_rate is None and a.assumption_label is None and a.assumption_note is None
    assert a.breakeven_assumed is None and a.curve_assumed is None
    body = a.model_dump_json()
    assert ASSUMPTION_LABEL not in body
    report = render_edge_report(edge=make_edge(rate=0.0003), analysis=a)
    assert ASSUMPTION_LABEL not in report
    assert "No assumed funding rate was used in this report" in report


# --------------------------------------------------------------------------- report (A7)
def test_report_is_a_titled_readable_artifact_of_numbers_that_already_exist():
    edge = make_edge(rate=0.0)
    a = horizon_analysis(edge, interval_h=8, symbol="BNBUSDT")
    md = render_edge_report(edge=edge, analysis=a, symbol="BNBUSDT", mode="paper")
    assert md.startswith("# Deltr edge report: BNBUSDT")
    for section in (
        "## Round-trip cost waterfall",
        "## Funding over the horizon",
        "## Breakeven holding period",
        "## Edge versus holding horizon",
        "## Data sources and feed age",
        "## Labels",
    ):
        assert section in md, section
    # every waterfall row is present
    for row in edge.components():
        assert row.label in md
    assert f"{edge.roundtrip_cost_bps:.2f} bps of round-trip cost" in md
    assert a.breakeven.verdict in md and a.breakeven_assumed.verdict in md
    # honesty labels
    assert ASSUMPTION_LABEL in md
    assert "always simulated" in md
    assert "not advice" in md
    assert "structurally near zero" in md


def test_report_carries_every_source_tag_and_feed_age():
    from tests.helpers_mcp import make_market

    market = make_market("BNBUSDT")
    edge = make_edge(rate=0.0)
    md = render_edge_report(edge=edge, analysis=horizon_analysis(edge, interval_h=8), market=market, symbol="BNBUSDT")
    assert "`bsc-mainnet-chain`" in md and "`binance-futures-testnet`" in md and "`binance-spot-mirror`" in md
    assert f"{market.freshness.dex_age_ms} ms" in md and f"{market.freshness.cex_age_ms} ms" in md
    assert f"{market.freshness.spot_age_ms} ms" in md


def test_report_never_uses_the_scrubbed_wording():
    edge = make_edge(rate=0.0)
    md = render_edge_report(edge=edge, analysis=horizon_analysis(edge, interval_h=8)).lower()
    for banned in ("risk-free", "risk free", "guaranteed", "profitable", "yield", " safe"):
        assert banned not in md, banned


# --------------------------------------------------------------------------- MCP surface
def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def test_explain_edge_surfaces_the_breakeven_and_the_curve():
    _, _, mcp = build_fake_server()
    async with connect(mcp._mcp_server) as c:
        out = _payload(await c.call_tool("deltr_explain_edge", {"capital_usd": 5000, "leverage": 2, "horizon_h": 24}))
    h = out["horizon"]
    assert out["breakeven_verdict"] == h["verdict"] and "breaks even" in h["verdict"]
    assert h["breakeven"]["settlements"] is not None and h["breakeven"]["days"] is not None
    assert h["horizon_h"] == 24 and h["horizon_days"] == 1.0
    assert h["curve"]["points"] and h["curve"]["zero_cross_h"] is not None
    assert out["formulas"]["breakeven_settlements"].startswith("smallest n")
    assert h["assumed_rate"] is None and h["assumption_label"] is None


async def test_explain_edge_labels_an_assumed_rate_it_was_asked_for():
    _, _, mcp = build_fake_server()
    async with connect(mcp._mcp_server) as c:
        out = _payload(await c.call_tool(
            "deltr_explain_edge",
            {"capital_usd": 5000, "leverage": 2, "horizon_h": 24, "assumed_funding_rate": 0.0001},
        ))
    h = out["horizon"]
    assert h["assumed_rate"] == 0.0001 and h["assumption_label"] == ASSUMPTION_LABEL
    assert h["breakeven_assumed"]["is_assumption"] is True and h["curve_assumed"]["is_assumption"] is True
    assert h["breakeven"]["is_assumption"] is False and h["measured_rate"] == 0.0003


async def test_edge_report_tool_renders_the_markdown_artifact():
    _, _, mcp = build_fake_server()
    async with connect(mcp._mcp_server) as c:
        out = _payload(await c.call_tool("deltr_edge_report", {"capital_usd": 5000, "leverage": 2, "horizon_h": 24}))
    assert out["format"] == "markdown" and out["title"] == "Deltr edge report: BNBUSDT"
    md = out["markdown"]
    assert md.startswith("# Deltr edge report: BNBUSDT")
    assert "## Breakeven holding period" in md and out["breakeven_verdict"] in md
    assert "`bsc-mainnet-chain`" in md and "`binance-futures-testnet`" in md
    assert out["assumed_rate"] is None and out["assumption_label"] is None
    assert ASSUMPTION_LABEL not in md
    assert out["measured_rate"] == 0.0003 and out["horizon"]["curve"]["points"]


async def test_edge_report_tool_labels_an_assumed_rate_in_the_markdown():
    _, _, mcp = build_fake_server()
    async with connect(mcp._mcp_server) as c:
        out = _payload(await c.call_tool(
            "deltr_edge_report",
            {"capital_usd": 5000, "leverage": 2, "horizon_h": 24, "assumed_funding_rate": 0.0001},
        ))
    md = out["markdown"]
    assert out["assumption_label"] == ASSUMPTION_LABEL and out["assumed_rate"] == 0.0001
    # the label and the measured rate appear on the same page, never one without the other
    assert ASSUMPTION_LABEL in md
    assert "measured testnet rate" in md
    assert f"`{out['measured_rate']:.6f}`" in md


async def test_edge_report_is_read_only_and_leaves_no_plan_behind():
    engine, _, mcp = build_fake_server()
    async with connect(mcp._mcp_server) as c:
        _payload(await c.call_tool("deltr_edge_report", {"capital_usd": 5000}))
    assert engine.state.plans == {} and list(engine.state.receipts) == []
    assert [row.tool for row in engine.activity_log(5)] == ["deltr_edge_report"]


def test_module_never_prints():
    import inspect

    src = inspect.getsource(hz)
    assert "print(" not in src, "library code never writes to stdout"
