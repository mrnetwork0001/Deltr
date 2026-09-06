"""The LIVE test override: a negative min edge in LIVE only with its own phrase and a tiny cap.

Outside PAPER the floor is 0 bps (net edge is already net of costs). The override exists so the
whole real-money path can be exercised for cents on a day the edge is negative, and it is refused
the moment the per-trade cap is larger than the test ceiling.
"""

from __future__ import annotations

import pytest

from deltr.config import LIVE_ACK_PHRASE, LIVE_TEST_ACK_PHRASE, LIVE_TEST_MAX_NOTIONAL_USD, ONCHAIN_ACK_PHRASE, Mode, Settings
from deltr.engine import _apply_overrides

LIVE_ENV = dict(
    BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real", BINANCE_API_ENV="mainnet",
    DELTR_LIVE_ACK=LIVE_ACK_PHRASE, DELTR_ONCHAIN_MODE="live", DELTR_ONCHAIN_ACK=ONCHAIN_ACK_PHRASE,
)


def live(**over):
    kw = {"_env_file": None, "DELTR_MODE": "live", **LIVE_ENV, **over}
    return Settings(**kw)  # type: ignore[arg-type]


def test_floor_is_zero_outside_paper():
    for mode in ("paper", "testnet"):
        s = Settings(_env_file=None, DELTR_MODE=mode, **({} if mode == "paper" else {"BINANCE_API_KEY": "k", "BINANCE_SECRET_KEY": "s"}))  # type: ignore[arg-type]
        assert s.min_edge_floor_bps(16.6) == 0.0
    assert live().min_edge_floor_bps(16.6) == 0.0


def test_override_needs_phrase_and_tiny_cap():
    assert live().live_test_override is False
    assert live(DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=25).live_test_override is True
    assert live(DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=LIVE_TEST_MAX_NOTIONAL_USD + 1).live_test_override is False
    assert live(DELTR_LIVE_TEST_ACK="yes", DELTR_LIVE_MAX_NOTIONAL_USD=10).live_test_override is False
    # the phrase alone never arms it in another mode
    s = Settings(_env_file=None, DELTR_MODE="paper", DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=10)  # type: ignore[arg-type]
    assert s.mode == Mode.PAPER and s.live_test_override is False


def test_override_is_visible_in_redacted_and_never_leaks_the_phrase():
    r = live(DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=20).redacted()
    assert r["live_test_override"] is True
    assert "s-not-real" not in str(r)


def test_startup_min_edge_override_respects_the_rule():
    with pytest.raises(ValueError, match="never a knowingly negative"):
        _apply_overrides(live(), None, -20.0)
    ok = _apply_overrides(live(DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=25), None, -20.0)
    assert ok.min_edge_bps == -20.0
    with pytest.raises(ValueError, match="never a knowingly negative"):
        _apply_overrides(live(DELTR_LIVE_TEST_ACK=LIVE_TEST_ACK_PHRASE, DELTR_LIVE_MAX_NOTIONAL_USD=100), None, -20.0)
    assert _apply_overrides(live(), None, 5.0).min_edge_bps == 5.0
