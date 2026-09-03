"""Freshness thresholds: stale feeds produce ``Freshness.ok=False`` with the reason the scout/gate act on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deltr.config import Settings
from deltr.market_data import FEED_STALE_REASON, MISSING_AGE_MS, compute_freshness, freshness_from_ages
from deltr.models import Freshness

T0 = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def make_settings(**over) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


def verdict(cex_ms: int, dex_ms: int, spot_ms: int = 0, **over) -> Freshness:
    s = make_settings(**over)
    return freshness_from_ages(cex_age_ms=cex_ms, dex_age_ms=dex_ms, spot_age_ms=spot_ms,
                               cex_stale_ms=s.cex_stale_ms, dex_stale_ms=s.dex_stale_ms)


def test_default_thresholds_are_5s_cex_and_9s_dex():
    s = make_settings()
    assert s.cex_stale_ms == 5_000 and s.dex_stale_ms == 9_000


@pytest.mark.parametrize(
    "cex_ms,dex_ms,ok,reason",
    [
        (0, 0, True, None),
        (5_000, 9_000, True, None),          # exactly at the threshold is still fresh
        (5_001, 0, False, "cex_stale"),
        (0, 9_001, False, "dex_stale"),
        (5_001, 9_001, False, "cex_stale"),  # cex reported first
        (MISSING_AGE_MS, 0, False, "cex_stale"),
        (0, MISSING_AGE_MS, False, "dex_stale"),
    ],
)
def test_threshold_verdicts(cex_ms, dex_ms, ok, reason):
    fr = verdict(cex_ms, dex_ms)
    assert fr.ok is ok and fr.reason == reason
    assert (fr.cex_age_ms, fr.dex_age_ms) == (cex_ms, dex_ms)


def test_spot_mirror_age_is_informational_only():
    fr = verdict(0, 0, spot_ms=60_000)
    assert fr.ok is True and fr.spot_age_ms == 60_000


def test_thresholds_come_from_settings():
    fr = verdict(1_500, 0, DELTR_CEX_STALE_MS=1_000)
    assert fr.ok is False and fr.reason == "cex_stale"
    fr2 = verdict(0, 4_000, DELTR_DEX_STALE_MS=3_000)
    assert fr2.ok is False and fr2.reason == "dex_stale"


def test_feed_stale_stress_overrides_even_fresh_ages():
    fr = freshness_from_ages(cex_age_ms=0, dex_age_ms=0, spot_age_ms=0, cex_stale_ms=5_000, dex_stale_ms=9_000, frozen=True)
    assert fr.ok is False and fr.reason == FEED_STALE_REASON == "feed_stale(stress)"


def test_compute_freshness_from_timestamps():
    now = T0
    fr = compute_freshness(now, cex_ts=now - timedelta(milliseconds=4_999), dex_ts=now - timedelta(seconds=9),
                           spot_ts=None, cex_stale_ms=5_000, dex_stale_ms=9_000)
    assert fr.ok and fr.cex_age_ms == 4_999 and fr.dex_age_ms == 9_000 and fr.spot_age_ms == MISSING_AGE_MS
    stale = compute_freshness(now, cex_ts=now - timedelta(seconds=6), dex_ts=now, spot_ts=now, cex_stale_ms=5_000, dex_stale_ms=9_000)
    assert stale.ok is False and stale.reason == "cex_stale" and stale.cex_age_ms == 6_000
    naive = compute_freshness(now, cex_ts=now.replace(tzinfo=None), dex_ts=now, spot_ts=now, cex_stale_ms=5_000, dex_stale_ms=9_000)
    assert naive.cex_age_ms == 0  # naive timestamps are treated as UTC
    future = compute_freshness(now, cex_ts=now + timedelta(seconds=3), dex_ts=now, spot_ts=now, cex_stale_ms=5_000, dex_stale_ms=9_000)
    assert future.cex_age_ms == 0  # clock skew never yields a negative age


def test_stale_freshness_is_the_not_actionable_input():
    """The scout's ``actionable_reason`` and the gate's STALE_QUOTE both key off these two fields."""
    fr = verdict(7_000, 0)
    assert fr.ok is False and fr.reason == "cex_stale"
    assert max(fr.cex_age_ms, fr.dex_age_ms) > make_settings().risk_limits().max_quote_age_ms
