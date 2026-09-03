"""tests/test_funding_history.py — the funding-history fetch, analysis and product surface.

Everything here is offline: the venue is an ``httpx.MockTransport`` serving
``tests/fixtures/funding_history.json``, which holds verbatim mainnet
``/fapi/v1/fundingRate`` rows captured on 2026-09-03. The analysis is therefore
reproducible without a network, which is the point: the strategy claim in
``docs/STRATEGY_EVIDENCE.md`` has to be regenerable rather than a static document.

The single live test (``@pytest.mark.live``) hits the real endpoint and is skipped
unless ``DELTR_LIVE_TESTS=1``.  No test in this file can place an order: the module
under test has no signed endpoint, no key handling and no order path at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from deltr.funding_history import (
    DEFAULT_MAKER_ROUNDTRIP_BPS,
    DEFAULT_TAKER_ROUNDTRIP_BPS,
    FUNDING_RATE_PATH,
    FUTURES_MAINNET_REST,
    FUTURES_TESTNET_REST,
    POSTED_MODEL,
    SPOT_MIRROR_REST,
    TAKEN_MODEL,
    CostModel,
    FundingHistoryError,
    FundingHistoryService,
    FundingPoint,
    analyse_funding,
    annualise_pct,
    data_source_for,
    dns_hint_for,
    fetch_funding_history,
    measure_interval_h,
    parse_funding_rows,
    render_funding_report,
    resolver_hint,
    rolling_carry_bps,
)
from deltr.models import DataSource

LIVE = os.environ.get("DELTR_LIVE_TESTS") == "1"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "funding_history.json"
PAGE = 150  # the fake venue answers with fewer rows than asked for, exactly like the real one


# --------------------------------------------------------------------------- fixture venue
def fixture_rows(symbol: str) -> list[dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(data["symbols"][symbol.upper()])


class FakeFapi:
    """``/fapi/v1/fundingRate`` served from the captured rows, with real endTime paging."""

    def __init__(self, page_size: int = PAGE, fail_with: Optional[Exception] = None) -> None:
        self.page_size = page_size
        self.fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            raise self.fail_with
        url = urlsplit(str(request.url))
        assert url.path == FUNDING_RATE_PATH, f"unexpected path {url.path}"
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append(q)
        rows = fixture_rows(q["symbol"])
        end = int(q["endTime"]) if "endTime" in q else None
        if end is not None:
            rows = [r for r in rows if r["fundingTime"] <= end]
        return httpx.Response(200, json=rows[-self.page_size:])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=5.0)


@pytest.fixture
def bnb_points() -> list[FundingPoint]:
    return parse_funding_rows(fixture_rows("BNBUSDT"), symbol="BNBUSDT")


# --------------------------------------------------------------------------- provenance
def test_data_source_is_derived_from_the_host_not_asserted():
    assert data_source_for(FUTURES_MAINNET_REST) is DataSource.BINANCE_FUTURES_MAINNET
    assert data_source_for("https://fapi2.binance.com") is DataSource.BINANCE_FUTURES_MAINNET
    assert data_source_for(FUTURES_TESTNET_REST) is DataSource.BINANCE_FUTURES_TESTNET
    assert data_source_for(SPOT_MIRROR_REST) is DataSource.BINANCE_SPOT_MIRROR
    # unknown hosts get no tag at all rather than an invented one
    assert data_source_for("https://example.invalid") is None
    assert data_source_for("") is None


def test_a_testnet_host_can_never_be_tagged_mainnet():
    for url in (FUTURES_TESTNET_REST, "https://testnet.binancefuture.com/fapi", "https://TESTNET.binancefuture.com"):
        assert data_source_for(url) is not DataSource.BINANCE_FUTURES_MAINNET


def test_resolver_hints_name_the_hostname_and_a_public_resolver():
    hint = resolver_hint(FUTURES_MAINNET_REST)
    assert "fapi.binance.com" in hint and "dig +short" in hint and "@1.1.1.1" in hint
    dns = dns_hint_for(httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"), FUTURES_MAINNET_REST)
    assert dns is not None and "fapi.binance.com" in dns and len(dns) <= 200
    assert dns_hint_for(httpx.ReadTimeout("slow"), FUTURES_MAINNET_REST) is None


# --------------------------------------------------------------------------- parsing
def test_parse_rows_are_ascending_deduplicated_and_tagged(bnb_points: list[FundingPoint]):
    assert len(bnb_points) == 400
    assert all(p.source is DataSource.BINANCE_FUTURES_MAINNET for p in bnb_points)
    times = [p.funding_time_ms for p in bnb_points]
    assert times == sorted(times) and len(set(times)) == len(times)
    dupes = parse_funding_rows(fixture_rows("BNBUSDT") * 2, symbol="BNBUSDT")
    assert len(dupes) == len(bnb_points)


def test_parse_rejects_a_row_without_the_fields_it_needs():
    with pytest.raises(FundingHistoryError):
        parse_funding_rows([{"symbol": "BNBUSDT", "markPrice": "1"}], symbol="BNBUSDT")
    with pytest.raises(FundingHistoryError):
        parse_funding_rows(["not-a-row"], symbol="BNBUSDT")  # type: ignore[list-item]


def test_interval_is_measured_from_the_data(bnb_points: list[FundingPoint]):
    assert measure_interval_h(bnb_points) == 8.0
    four_hourly = [
        FundingPoint("X", 1_700_000_000_000 + i * 4 * 3_600_000, 0.0001) for i in range(20)
    ]
    assert measure_interval_h(four_hourly) == 4.0
    assert measure_interval_h(four_hourly[:1]) == 8.0  # too few points -> the documented default


def test_annualise_matches_the_evidence_formula():
    # the evidence document annualises as rate * 3 * 365
    assert annualise_pct(0.0001, 8.0) == pytest.approx(0.0001 * 3 * 365 * 100)
    assert annualise_pct(0.0001, 4.0) == pytest.approx(0.0001 * 6 * 365 * 100)
    with pytest.raises(ValueError):
        annualise_pct(0.0001, 0.0)


# --------------------------------------------------------------------------- analysis
def test_rolling_carry_is_the_sum_of_the_window_in_bps():
    pts = [FundingPoint("X", 1_000 + i, r) for i, r in enumerate([0.0001, 0.0002, -0.0001, 0.0003])]
    assert rolling_carry_bps(pts, 2) == pytest.approx([3.0, 1.0, 2.0])
    assert rolling_carry_bps(pts, 4) == pytest.approx([5.0])
    assert rolling_carry_bps(pts, 9) == []
    with pytest.raises(ValueError):
        rolling_carry_bps(pts, 0)


def test_analysis_reports_carry_short_paid_share_and_both_cost_models(bnb_points: list[FundingPoint]):
    a = analyse_funding(bnb_points, holds_days=(3.0, 7.0, 14.0, 30.0))
    assert a.symbol == "BNBUSDT" and a.samples == 400
    assert a.interval_h == 8.0 and a.interval_measured is True
    assert a.source is DataSource.BINANCE_FUTURES_MAINNET and a.base_url == FUTURES_MAINNET_REST
    assert a.span_days == pytest.approx(400 / 3.0, abs=0.5)

    # short-paid share is exactly the count of positive settlements
    positive = sum(1 for p in bnb_points if p.rate > 0)
    assert a.short_paid_count == positive
    assert a.short_paid_pct == pytest.approx(100.0 * positive / 400)
    assert a.min_annualised_pct <= a.median_annualised_pct <= a.max_annualised_pct

    names = {m.name for m in a.cost_models}
    assert names == {TAKEN_MODEL, POSTED_MODEL}
    taken = a.window(TAKEN_MODEL, 7.0)
    posted = a.window(POSTED_MODEL, 7.0)
    assert taken is not None and posted is not None
    assert taken.settlements_per_window == 21 and taken.windows == 400 - 21 + 1
    assert taken.roundtrip_bps == DEFAULT_TAKER_ROUNDTRIP_BPS
    assert posted.roundtrip_bps == DEFAULT_MAKER_ROUNDTRIP_BPS
    # the cheaper round trip can only be cleared at least as often as the dearer one
    assert posted.cleared >= taken.cleared
    assert 0.0 <= taken.share_pct <= 100.0 and 0.0 <= posted.share_pct <= 100.0


def test_the_posted_model_clears_more_often_than_the_taken_one_at_every_hold(bnb_points: list[FundingPoint]):
    """The measured finding the strategy rests on: execution style, not signal."""
    a = analyse_funding(bnb_points)
    for hold in (3.0, 7.0, 14.0, 30.0):
        t = a.window(TAKEN_MODEL, hold)
        p = a.window(POSTED_MODEL, hold)
        assert t is not None and p is not None
        assert p.share_pct >= t.share_pct, f"posted must clear at least as often at {hold} days"


def test_a_four_hour_symbol_is_not_annualised_as_if_it_were_eight():
    pts = [FundingPoint("CAKEUSDT", 1_700_000_000_000 + i * 4 * 3_600_000, 0.0001) for i in range(60)]
    measured = analyse_funding(pts)
    forced = analyse_funding(pts, interval_h=8.0)
    assert measured.interval_h == 4.0 and measured.interval_measured is True
    assert forced.interval_h == 8.0 and forced.interval_measured is False
    assert measured.mean_annualised_pct == pytest.approx(2 * forced.mean_annualised_pct)
    # windows are counted in settlements, so a 4 h symbol gets six per day
    assert measured.window(TAKEN_MODEL, 3.0).settlements_per_window == 18       # type: ignore[union-attr]
    assert forced.window(TAKEN_MODEL, 3.0).settlements_per_window == 9          # type: ignore[union-attr]


def test_custom_cost_models_change_only_the_thresholds(bnb_points: list[FundingPoint]):
    free = analyse_funding(bnb_points, taker_roundtrip_bps=0.0, maker_roundtrip_bps=0.0, holds_days=(7.0,))
    dear = analyse_funding(bnb_points, taker_roundtrip_bps=500.0, maker_roundtrip_bps=500.0, holds_days=(7.0,))
    assert free.window(TAKEN_MODEL, 7.0).cleared >= dear.window(TAKEN_MODEL, 7.0).cleared  # type: ignore[union-attr]
    assert dear.window(TAKEN_MODEL, 7.0).cleared == 0                                      # type: ignore[union-attr]


def test_analysis_refuses_an_empty_series():
    with pytest.raises(FundingHistoryError):
        analyse_funding([])


def test_as_dict_is_json_safe_and_labels_the_posted_column_as_a_model(bnb_points: list[FundingPoint]):
    d = analyse_funding(bnb_points).as_dict()
    json.dumps(d)  # must not raise
    assert d["source"] == "binance-futures-mainnet"
    assert d["symbol"] == "BNBUSDT" and d["samples"] == 400
    assert "model, not a measurement" in d["labels"]["posted_model"]
    assert "not a forecast" in d["labels"]["not_advice"]
    assert {w["model"] for w in d["windows"]} == {TAKEN_MODEL, POSTED_MODEL}


def test_report_renders_the_numbers_without_a_profit_claim(bnb_points: list[FundingPoint]):
    md = render_funding_report(analyse_funding(bnb_points))
    assert "# Funding carry: BNBUSDT" in md and "binance-futures-mainnet" in md
    assert "Settlements where the short is paid" in md and "TAKEN" in md and "POSTED" in md
    lowered = md.lower()
    for banned in ("profitable", "risk-free", "risk free", "guaranteed", "recommended"):
        assert banned not in lowered
    assert "—" not in md  # no em-dashes in user-facing copy


def test_cost_model_serialises():
    assert CostModel("taken", 16.6, "note").as_dict() == {"name": "taken", "roundtrip_bps": 16.6, "note": "note"}


# --------------------------------------------------------------------------- fetch
async def test_fetch_pages_backwards_with_endtime_until_the_lookback_is_covered():
    fake = FakeFapi(page_size=PAGE)
    async with fake.client() as http:
        pts = await fetch_funding_history(http, "bnbusdt", base_url=FUTURES_MAINNET_REST, lookback_days=60)
    assert len(fake.calls) >= 2, "one page cannot cover 60 days at 150 rows a page"
    assert all(c["symbol"] == "BNBUSDT" for c in fake.calls)
    # every page after the first ends strictly before the previous page's oldest row
    ends = [int(c["endTime"]) for c in fake.calls]
    assert ends == sorted(ends, reverse=True) and len(set(ends)) == len(ends)
    assert all(p.source is DataSource.BINANCE_FUTURES_MAINNET for p in pts)
    times = [p.funding_time_ms for p in pts]
    assert times == sorted(times) and len(set(times)) == len(times)
    span_days = (times[-1] - times[0]) / 86_400_000.0
    assert 55 <= span_days <= 62


async def test_fetch_stops_at_max_rows_and_honours_the_lookback_floor():
    fake = FakeFapi(page_size=PAGE)
    async with fake.client() as http:
        pts = await fetch_funding_history(
            http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=500, max_rows=200
        )
    assert len(pts) == 200


async def test_fetch_refuses_to_tag_an_unknown_host():
    fake = FakeFapi()
    async with fake.client() as http:
        with pytest.raises(FundingHistoryError) as exc:
            await fetch_funding_history(http, "BNBUSDT", base_url="https://example.invalid")
    assert "unrecognised host" in str(exc.value) and FUTURES_MAINNET_REST in str(exc.value)


async def test_fetch_names_the_resolver_when_dns_fails():
    fake = FakeFapi(fail_with=httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"))
    async with fake.client() as http:
        with pytest.raises(FundingHistoryError) as exc:
            await fetch_funding_history(http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=7)
    msg = str(exc.value)
    assert "fapi.binance.com" in msg and "dig +short" in msg and "@1.1.1.1" in msg
    assert "will not substitute another venue or simulated data" in msg


async def test_fetch_surfaces_a_binance_error_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(FundingHistoryError) as exc:
            await fetch_funding_history(http, "NOTAPAIR", base_url=FUTURES_MAINNET_REST, lookback_days=7)
    assert exc.value.code == -1121 and "Invalid symbol" in str(exc.value)


async def test_fetch_explains_a_403_by_naming_the_host_that_does_work():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(FundingHistoryError) as exc:
            await fetch_funding_history(http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=7)
    assert "HTTP 403" in str(exc.value) and "needs no key" in str(exc.value)


async def test_fetch_rejects_a_payload_that_is_not_a_funding_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(FundingHistoryError) as exc:
            await fetch_funding_history(http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=7)
    assert "expected a list" in str(exc.value)


async def test_fetch_raises_when_the_window_holds_no_settlement():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(FundingHistoryError) as exc:
            await fetch_funding_history(http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=7)
    assert "no funding settlements" in str(exc.value)


async def test_fetch_validates_its_arguments():
    fake = FakeFapi()
    async with fake.client() as http:
        with pytest.raises(ValueError):
            await fetch_funding_history(http, "", base_url=FUTURES_MAINNET_REST)
        with pytest.raises(ValueError):
            await fetch_funding_history(http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=0)


# --------------------------------------------------------------------------- service + cache
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_service_caches_until_the_ttl_expires():
    fake = FakeFapi()
    clock = FakeClock()
    async with fake.client() as http:
        svc = FundingHistoryService(http, ttl_s=3600.0, lookback_days=30, monotonic=clock)
        a1 = await svc.analyse("BNBUSDT")
        first_calls = len(fake.calls)
        a2 = await svc.analyse("BNBUSDT")
        assert len(fake.calls) == first_calls and svc.hits == 1 and svc.misses == 1
        assert a1.samples == a2.samples

        clock.t += 3601.0
        await svc.analyse("BNBUSDT")
        assert len(fake.calls) > first_calls and svc.misses == 2


async def test_service_refresh_and_invalidate_bypass_the_cache():
    fake = FakeFapi()
    async with fake.client() as http:
        svc = FundingHistoryService(http, lookback_days=30, monotonic=FakeClock())
        await svc.analyse("BNBUSDT")
        n = len(fake.calls)
        await svc.analyse("BNBUSDT", refresh=True)
        assert len(fake.calls) > n
        assert svc.cached_at_age_s("BNBUSDT", 30) == 0.0
        svc.invalidate("BNBUSDT")
        assert svc.cached_at_age_s("BNBUSDT", 30) is None


async def test_service_caches_per_symbol_and_per_lookback():
    fake = FakeFapi()
    async with fake.client() as http:
        svc = FundingHistoryService(http, lookback_days=30, monotonic=FakeClock())
        await svc.analyse("BNBUSDT", lookback_days=30)
        await svc.analyse("BNBUSDT", lookback_days=60)
        await svc.analyse("BTCUSDT", lookback_days=30)
        assert svc.misses == 3 and svc.hits == 0
        await svc.analyse("BTCUSDT", lookback_days=30)
        assert svc.hits == 1


def test_service_refuses_a_non_mainnet_host():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    for url in (FUTURES_TESTNET_REST, SPOT_MIRROR_REST, "https://example.invalid"):
        with pytest.raises(ValueError, match="mainnet futures host"):
            FundingHistoryService(http, base_url=url)


async def test_service_analysis_carries_the_mainnet_tag_and_both_models():
    fake = FakeFapi()
    async with fake.client() as http:
        svc = FundingHistoryService(http, lookback_days=90, monotonic=FakeClock())
        a = await svc.analyse("BTCUSDT", holds_days=(7.0,))
    assert a.symbol == "BTCUSDT" and a.source is DataSource.BINANCE_FUTURES_MAINNET
    assert a.window(TAKEN_MODEL, 7.0) is not None and a.window(POSTED_MODEL, 7.0) is not None
    assert a.window(TAKEN_MODEL, 99.0) is None


# --------------------------------------------------------------------------- no order path
def test_the_module_has_no_signing_or_order_surface():
    """The evidence surface must stay read-only: no key handling, no order endpoint."""
    src = (Path(__file__).resolve().parents[1] / "deltr" / "funding_history.py").read_text(encoding="utf-8")
    for banned in ("import hmac", "import hashlib", "hexdigest", "X-MBX-APIKEY", "/fapi/v1/order",
                   "api_key", "secret_key", "private_key", ".post(", ".put(", ".delete("):
        assert banned not in src, f"funding_history.py must not contain {banned!r}"


# --------------------------------------------------------------------------- live
@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="live endpoint; set DELTR_LIVE_TESTS=1")
async def test_live_mainnet_funding_history_is_real_and_tagged_mainnet():
    """Hits the real keyless mainnet endpoint. Read-only: it cannot place an order."""
    async with httpx.AsyncClient(timeout=20.0) as http:
        pts = await fetch_funding_history(http, "BNBUSDT", base_url=FUTURES_MAINNET_REST, lookback_days=30)
    assert len(pts) >= 60
    assert all(p.symbol == "BNBUSDT" and p.source is DataSource.BINANCE_FUTURES_MAINNET for p in pts)
    a = analyse_funding(pts, holds_days=(7.0,))
    assert a.interval_h in (4.0, 8.0)
    assert a.window(TAKEN_MODEL, 7.0) is not None and a.window(POSTED_MODEL, 7.0) is not None
    assert 0.0 <= a.short_paid_pct <= 100.0
