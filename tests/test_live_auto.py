"""Unattended trading with real orders: only with its own phrase, and only at min edge >= 0.

PAPER may auto-trade freely (its labelled override is the demo). TESTNET/LIVE auto-execute needs
DELTR_LIVE_AUTO_ACK, and stands down the moment any override takes the threshold below zero, so an
unattended real-money loop can never chase a known loss.
"""

from __future__ import annotations

from deltr.config import LIVE_ACK_PHRASE, LIVE_AUTO_ACK_PHRASE, LIVE_TEST_ACK_PHRASE, ONCHAIN_ACK_PHRASE, Settings, auto_allowed

LIVE_ENV = dict(
    BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real", BINANCE_API_ENV="mainnet",
    DELTR_LIVE_ACK=LIVE_ACK_PHRASE, DELTR_ONCHAIN_MODE="live", DELTR_ONCHAIN_ACK=ONCHAIN_ACK_PHRASE,
)


def live(**over):
    return Settings(**{"_env_file": None, "DELTR_MODE": "live", **LIVE_ENV, **over})  # type: ignore[arg-type]


def paper(**over):
    return Settings(**{"_env_file": None, "DELTR_MODE": "paper", **over})  # type: ignore[arg-type]


def test_paper_auto_is_always_allowed_even_with_the_override():
    assert auto_allowed(paper(), -20.0) == (True, None)
    assert paper(DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE).live_auto_ok is False  # the phrase means nothing in PAPER


def test_live_auto_needs_the_phrase():
    ok, why = auto_allowed(live(), 3.0)
    assert ok is False and "DELTR_LIVE_AUTO_ACK" in (why or "")
    assert live(DELTR_LIVE_AUTO_ACK="yes").live_auto_ok is False
    assert live(DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE).live_auto_ok is True
    assert auto_allowed(live(DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE), 3.0) == (True, None)
    assert auto_allowed(live(DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE), 0.0) == (True, None)


def test_live_auto_stands_down_while_any_override_is_negative():
    s = live(DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE, DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=25)
    assert s.live_test_override is True and s.live_auto_ok is True
    ok, why = auto_allowed(s, -20.0)
    assert ok is False and ">= 0" in (why or "")


def test_engine_refuses_auto_with_real_orders_without_the_phrase(tmp_path):
    import pytest

    from deltr.engine import Engine
    from tests.conftest import refusing_http

    with pytest.raises(ValueError, match="DELTR_LIVE_AUTO_ACK"):
        Engine(live(DELTR_AUTO_EXECUTE="true", DELTR_STATE_DIR=str(tmp_path)), http=refusing_http())
    # with the phrase the same construction succeeds
    Engine(live(DELTR_AUTO_EXECUTE="true", DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE, DELTR_STATE_DIR=str(tmp_path)), http=refusing_http())


def test_status_reports_the_auto_state():
    s = live(DELTR_LIVE_AUTO_ACK=LIVE_AUTO_ACK_PHRASE, DELTR_AUTO_EXECUTE="true")
    r = s.redacted()
    assert r["live_auto_ok"] is True and r["auto_execute"] is True
    assert "s-not-real" not in str(r)
