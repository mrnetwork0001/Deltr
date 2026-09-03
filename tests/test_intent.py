"""Deterministic NL intent grammar — the 10 canonical prompts from DESIGN_FINAL.md §4.13 and edge cases."""

from __future__ import annotations

import pytest

from deltr.models import Intent, StressKind, TraceSource
from deltr.nl_intent import STABLECOIN_NOTE, VERB_TABLE, parse_amount, parse_intent, parse_leverage


# --------------------------------------------------------------------------- canonical prompts
def test_rebalance_usdc_sets_note():
    i = parse_intent("Rebalance $5,000 USDC into delta-neutral BNB arbitrage")
    assert i.action == "rebalance"
    assert i.capital_usd == 5000.0
    assert i.stablecoin_note == STABLECOIN_NOTE
    assert i.symbol == "BNBUSDT"
    assert i.confidence == 1.0


def test_hedge_2k_at_3x():
    i = parse_intent("Hedge 2k at 3x")
    assert (i.action, i.capital_usd, i.leverage) == ("hedge", 2000.0, 3.0)
    assert i.stablecoin_note is None


def test_explain():
    assert parse_intent("What is the edge right now?").action == "explain"


def test_scan():
    assert parse_intent("Scan for opportunities").action == "scan"


@pytest.mark.parametrize("text", ["Unwind everything", "Close all positions"])
def test_unwind_all(text):
    i = parse_intent(text)
    assert i.action == "unwind" and i.position_id == "all"


def test_status():
    assert parse_intent("Status").action == "status"


def test_kill_switch_on():
    i = parse_intent("Kill switch on")
    assert i.action == "kill" and i.magnitude == 1.0


def test_reset_halt():
    assert parse_intent("Reset halt").action == "reset_halt"


def test_stress_basis_shock():
    i = parse_intent("Simulate a 150 bps basis shock")
    assert i.action == "stress"
    assert i.stress_kind == StressKind.BASIS_SHOCK
    assert i.magnitude == 150.0
    assert i.capital_usd is None  # "150" is a magnitude, never money


def test_set_min_edge():
    i = parse_intent("Set min edge to 1 bps")
    assert i.action == "set_min_edge" and i.min_edge_bps == 1.0


# --------------------------------------------------------------------------- amounts / leverage
@pytest.mark.parametrize(
    "text,usd,note",
    [
        ("$5,000", 5000.0, None),
        ("5k", 5000.0, None),
        ("2.5k", 2500.0, None),
        ("$5k", 5000.0, None),
        ("5000 USDC", 5000.0, STABLECOIN_NOTE),
        ("5000 USDT", 5000.0, None),
        ("1m", 1_000_000.0, None),
        ("hedge at 3x", None, None),           # leverage is not an amount
        ("150 bps basis shock", None, None),   # bps figure is not an amount
        ("Unwind pos_1234", None, None),       # id fragment is not an amount
        ("Hedge 5000", 5000.0, None),
    ],
)
def test_parse_amount(text, usd, note):
    assert parse_amount(text) == (usd, note)


@pytest.mark.parametrize(
    "text,lev",
    [("2x", 2.0), ("3 x", 3.0), ("leverage 2", 2.0), ("at 2.5x", 2.5), ("2x leverage", 2.0), ("leverage of 3", 3.0), ("no leverage here", None), ("$5,000", None)],
)
def test_parse_leverage(text, lev):
    assert parse_leverage(text) == lev


# --------------------------------------------------------------------------- other behaviours
def test_unknown_text_has_zero_confidence():
    i = parse_intent("make me a sandwich")
    assert i.action == "unknown" and i.confidence == 0.0
    assert i.raw == "make me a sandwich"


def test_empty_text_is_unknown():
    assert parse_intent("").action == "unknown"


def test_sizing_only_prompt_infers_hedge_with_lower_confidence():
    i = parse_intent("$5,000 at 2x")
    assert i.action == "hedge" and i.capital_usd == 5000.0 and i.leverage == 2.0
    assert i.confidence < 1.0


def test_unwind_specific_position():
    assert parse_intent("Unwind pos_ab12cd").position_id == "pos_ab12cd"


def test_stress_variants():
    e = parse_intent("Simulate a 3.5% equity shock")
    assert e.stress_kind == StressKind.EQUITY_SHOCK and e.magnitude == 3.5
    f = parse_intent("flip funding to -0.0003")
    assert f.action == "stress" and f.stress_kind == StressKind.FUNDING_FLIP and f.magnitude == -0.0003
    f2 = parse_intent("Simulate funding flipping to -3 bps")
    assert f2.stress_kind == StressKind.FUNDING_FLIP and f2.magnitude == pytest.approx(-0.0003)
    d = parse_intent("Simulate a DEX leg failure")
    assert d.stress_kind == StressKind.DEX_LEG_FAIL
    s = parse_intent("Simulate a stale feed")
    assert s.stress_kind == StressKind.FEED_STALE
    r = parse_intent("Reset the stress scenario")
    assert r.action == "stress" and r.stress_kind == StressKind.RESET


def test_kill_switch_off():
    assert parse_intent("kill switch off").magnitude == 0.0


def test_symbol_detection_and_default():
    assert parse_intent("Hedge 2k of ETH at 2x").symbol == "ETHUSDT"
    assert parse_intent("Hedge 2k at 2x", default_symbol="BTCUSDT").symbol == "BTCUSDT"


def test_source_is_carried():
    assert parse_intent("Status", source=TraceSource.MCP).source == TraceSource.MCP


def test_explain_with_horizon_and_size():
    i = parse_intent("explain the edge over 24h for $5000 at 2x")
    assert i.action == "explain" and i.magnitude == 24.0 and i.capital_usd == 5000.0 and i.leverage == 2.0


def test_never_yields_a_mode_change():
    """Every action the grammar can produce is a member of Intent.action; there is no mode verb."""
    actions = {a for a, _ in VERB_TABLE}
    allowed = set(Intent.model_fields["action"].annotation.__args__)
    assert actions <= allowed
    assert not {"set_mode", "live", "go_live"} & actions
    for text in ("switch to live mode", "go live", "set mode live", "trade with real money"):
        i = parse_intent(text)
        assert i.action in allowed and i.action not in ("hedge", "rebalance")


def test_determinism():
    a = parse_intent("Hedge 2k at 3x").model_dump()
    b = parse_intent("Hedge 2k at 3x").model_dump()
    assert a == b
