"""deltr/funding_history.py — real mainnet funding history and the carry analysis built on it.

Deltr's strategy claim is a measurement, not a slogan, so it has to be regenerable.
This module fetches ``GET /fapi/v1/fundingRate`` from **mainnet** ``fapi.binance.com``
(public, keyless, read-only: there is no signed endpoint and no order path here) and
turns it into the numbers ``docs/STRATEGY_EVIDENCE.md`` reports:

* annualised carry (mean / median / min / max) for the fetched window;
* the share of settlements where a **short** is paid (``fundingRate > 0``);
* the share of rolling holding windows where funding carry alone clears a round trip,
  under **two cost models** — the perp leg TAKEN (crossing the spread) and the perp
  leg POSTED (maker). The posted column is a *model*: it recomputes the same window
  against a maker round trip and assumes the limit order fills, which a real maker
  order may not.

Design notes:

* **Pagination** walks backwards with ``endTime``: ask for ``FUNDING_PAGE_LIMIT`` rows,
  then re-ask ending one millisecond before the oldest row returned. The endpoint
  currently answers with fewer rows than requested, so paging is driven by timestamps
  and never by a row count.
* **The settlement interval is measured, not assumed** (BNBUSDT settles every 8 h,
  CAKEUSDT every 4 h): it comes from the median gap between consecutive settlements,
  with an explicit override for callers that know better.
* **Provenance is derived from the host that actually answered** (:func:`data_source_for`).
  A testnet number can never be tagged mainnet, because the tag is a function of the
  base URL rather than a caller-supplied label.
* **A DNS failure is named, never papered over**: :class:`FundingHistoryError` says
  which hostname failed and which resolver to try. Nothing here falls back to another
  venue, to the testnet, or to simulated data.
* Caching: funding only changes once per settlement, so :class:`FundingHistoryService`
  keeps a small in-memory TTL cache keyed by (symbol, lookback).

Nothing in this module writes to stdout; all logging goes to stderr via ``logging``.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import httpx

from deltr.models import DataSource

log = logging.getLogger("deltr.funding_history")

# --------------------------------------------------------------------------- hosts
FUTURES_MAINNET_REST = "https://fapi.binance.com"
FUTURES_TESTNET_REST = "https://testnet.binancefuture.com"
SPOT_MIRROR_REST = "https://data-api.binance.vision"

# Mainnet USDS-M futures market-data hosts (fapi.binance.com plus its numbered aliases).
_MAINNET_FUTURES_HOSTS = frozenset(
    {"fapi.binance.com", "fapi1.binance.com", "fapi2.binance.com", "fapi3.binance.com", "fapi4.binance.com"}
)
_SPOT_MIRROR_HOSTS = frozenset({"data-api.binance.vision", "data.binance.vision"})

FUNDING_RATE_PATH = "/fapi/v1/fundingRate"
FUNDING_PAGE_LIMIT = 1000              # documented maximum; the venue may answer with fewer
MAX_PAGES = 12                         # hard stop so a misbehaving endpoint cannot loop forever

# Cost models measured in docs/STRATEGY_EVIDENCE.md (round trip, both legs, bps).
DEFAULT_TAKER_ROUNDTRIP_BPS = 16.6     # perp leg crossed: 10 bps of it is the Binance taker fee (2 x 5 bps)
DEFAULT_MAKER_ROUNDTRIP_BPS = 8.6      # perp leg posted: the same round trip with a maker fee
DEFAULT_HOLDS_DAYS: tuple[float, ...] = (3.0, 7.0, 14.0, 30.0)
DEFAULT_LOOKBACK_DAYS = 500.0
DEFAULT_INTERVAL_H = 8.0
FUNDING_CACHE_TTL_S = 3600.0           # funding settles every 4-8 h; an hour of cache is conservative

BPS = 10_000.0
HOURS_PER_YEAR = 24.0 * 365.0

TAKEN_MODEL = "taken"                  # perp leg crosses the spread (taker fee)
POSTED_MODEL = "posted"                # perp leg is posted (maker fee) — a model, not a realised result

_DNS_MARKERS = ("getaddrinfo", "name or service not known", "nodename nor servname", "temporary failure in name resolution",
                "name does not resolve", "no address associated with hostname")


class FundingHistoryError(Exception):
    """A funding-history fetch failed. ``hint`` carries the operator-actionable next step."""

    def __init__(self, message: str, *, hint: str = "", code: Optional[int] = None) -> None:
        super().__init__(f"{message}. {hint}" if hint else message)
        self.message = message
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------------- provenance
def data_source_for(base_url: str) -> Optional[DataSource]:
    """The :class:`DataSource` tag the host in ``base_url`` genuinely warrants.

    Returns ``None`` for a host we cannot identify, so callers keep whatever tag the
    payload already carried instead of inventing a provenance claim.
    """
    try:
        host = (urlsplit(str(base_url)).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host in _MAINNET_FUTURES_HOSTS:
        return DataSource.BINANCE_FUTURES_MAINNET
    if host in _SPOT_MIRROR_HOSTS:
        return DataSource.BINANCE_SPOT_MIRROR
    if "testnet" in host:
        return DataSource.BINANCE_FUTURES_TESTNET
    return None


def resolver_hint(base_url: str) -> str:
    """The message shown when ``base_url``'s hostname will not resolve."""
    host = (urlsplit(str(base_url)).hostname or str(base_url)).lower()
    return (
        f"{host} did not resolve through this machine's default DNS resolver. "
        f"Check it with 'dig +short {host}' and compare against a public resolver "
        f"('dig +short @1.1.1.1 {host}'); if only the public resolver answers, the LAN resolver is the fault. "
        "Deltr will not substitute another venue or simulated data for mainnet market data."
    )


def dns_hint_for(exc: BaseException, base_url: str) -> Optional[str]:
    """A short, health-record-sized resolver hint when ``exc`` is a DNS failure, else ``None``.

    :func:`resolver_hint` is the long form for exceptions; this one fits a
    ``VenueHealth.detail`` field without being truncated into uselessness.
    """
    if not _is_dns_failure(exc):
        return None
    host = (urlsplit(str(base_url)).hostname or str(base_url)).lower()
    return (
        f"DNS: {host} did not resolve; compare 'dig +short {host}' with "
        f"'dig +short @1.1.1.1 {host}'. No fallback venue."
    )


def _is_dns_failure(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        text += f" {type(cause).__name__}: {cause}".lower()
    return any(marker in text for marker in _DNS_MARKERS)


# --------------------------------------------------------------------------- records
@dataclass(frozen=True)
class FundingPoint:
    """One settled funding payment, exactly as the venue reported it."""

    symbol: str
    funding_time_ms: int
    rate: float                 # per settlement, as a fraction (0.0001 = 1 bp)
    mark_price: Optional[float] = None
    source: DataSource = DataSource.BINANCE_FUTURES_MAINNET

    @property
    def ts(self) -> datetime:
        return datetime.fromtimestamp(self.funding_time_ms / 1000.0, tz=timezone.utc)

    @property
    def rate_bps(self) -> float:
        return self.rate * BPS

    def annualised_pct(self, interval_h: float = DEFAULT_INTERVAL_H) -> float:
        """The settlement rate expressed as an annualised percentage."""
        return annualise_pct(self.rate, interval_h)


def annualise_pct(rate: float, interval_h: float = DEFAULT_INTERVAL_H) -> float:
    """``rate`` per settlement -> annualised percent (8 h interval => rate * 3 * 365 * 100)."""
    if interval_h <= 0:
        raise ValueError("interval_h must be > 0")
    return float(rate) * (HOURS_PER_YEAR / float(interval_h)) * 100.0


def parse_funding_rows(rows: Iterable[Mapping[str, Any]], *, symbol: Optional[str] = None,
                       source: DataSource = DataSource.BINANCE_FUTURES_MAINNET) -> list[FundingPoint]:
    """Parse raw ``/fapi/v1/fundingRate`` rows into ascending, de-duplicated :class:`FundingPoint`s."""
    by_time: dict[int, FundingPoint] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise FundingHistoryError(f"fundingRate row is not an object: {type(raw).__name__}")
        try:
            t = int(raw["fundingTime"])
            rate = float(raw["fundingRate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FundingHistoryError(f"fundingRate row missing fundingTime/fundingRate: {exc}") from exc
        sym = str(raw.get("symbol") or symbol or "")
        if symbol and sym and sym.upper() != symbol.upper():
            continue
        mark = raw.get("markPrice")
        by_time[t] = FundingPoint(
            symbol=sym or (symbol or ""),
            funding_time_ms=t,
            rate=rate,
            mark_price=float(mark) if mark not in (None, "") else None,
            source=source,
        )
    return [by_time[k] for k in sorted(by_time)]


def measure_interval_h(points: Sequence[FundingPoint], default: float = DEFAULT_INTERVAL_H) -> float:
    """The settlement interval MEASURED from the data (median gap), not assumed.

    BNBUSDT settles every 8 h and CAKEUSDT every 4 h; annualising both at 8 h would
    misstate CAKEUSDT's carry by a factor of two, so the interval is derived.
    """
    if len(points) < 3:
        return float(default)
    gaps = [
        (points[i + 1].funding_time_ms - points[i].funding_time_ms) / 3_600_000.0
        for i in range(len(points) - 1)
        if points[i + 1].funding_time_ms > points[i].funding_time_ms
    ]
    if not gaps:
        return float(default)
    median_h = statistics.median(gaps)
    if not (0.25 <= median_h <= 48.0):
        log.warning("implausible measured funding interval %.3f h; falling back to %.1f h", median_h, default)
        return float(default)
    # snap to the nearest supported cadence so float noise never leaks into the annualisation
    for candidate in (1.0, 2.0, 4.0, 8.0, 12.0, 24.0):
        if abs(median_h - candidate) <= 0.25:
            return candidate
    return round(median_h, 3)


# --------------------------------------------------------------------------- analysis
@dataclass(frozen=True)
class CostModel:
    """A named round-trip cost, in bps, applied to both legs."""

    name: str
    roundtrip_bps: float
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "roundtrip_bps": round(self.roundtrip_bps, 4), "note": self.note}


@dataclass(frozen=True)
class WindowStat:
    """Share of rolling holding windows whose carry alone clears one cost model's round trip."""

    model: str
    hold_days: float
    roundtrip_bps: float
    settlements_per_window: int
    windows: int
    cleared: int
    share_pct: float
    median_carry_bps: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "hold_days": self.hold_days,
            "roundtrip_bps": round(self.roundtrip_bps, 4),
            "settlements_per_window": self.settlements_per_window,
            "windows": self.windows,
            "cleared": self.cleared,
            "share_pct": round(self.share_pct, 4),
            "median_carry_bps": round(self.median_carry_bps, 4),
        }


@dataclass(frozen=True)
class FundingAnalysis:
    """The regenerable form of the strategy evidence, for one symbol and one window."""

    symbol: str
    source: DataSource
    base_url: str
    samples: int
    interval_h: float
    interval_measured: bool
    first_ts: Optional[datetime]
    last_ts: Optional[datetime]
    span_days: float
    mean_annualised_pct: float
    median_annualised_pct: float
    min_annualised_pct: float
    max_annualised_pct: float
    short_paid_count: int
    short_paid_pct: float
    total_carry_bps: float
    cost_models: tuple[CostModel, ...] = ()
    windows: tuple[WindowStat, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def window(self, model: str, hold_days: float) -> Optional[WindowStat]:
        for w in self.windows:
            if w.model == model and abs(w.hold_days - float(hold_days)) < 1e-9:
                return w
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "source": self.source.value,
            "base_url": self.base_url,
            "samples": self.samples,
            "interval_h": self.interval_h,
            "interval_measured": self.interval_measured,
            "first_settlement": self.first_ts.isoformat() if self.first_ts else None,
            "last_settlement": self.last_ts.isoformat() if self.last_ts else None,
            "span_days": round(self.span_days, 3),
            "mean_annualised_pct": round(self.mean_annualised_pct, 4),
            "median_annualised_pct": round(self.median_annualised_pct, 4),
            "min_annualised_pct": round(self.min_annualised_pct, 4),
            "max_annualised_pct": round(self.max_annualised_pct, 4),
            "short_paid_count": self.short_paid_count,
            "short_paid_pct": round(self.short_paid_pct, 4),
            "total_carry_bps": round(self.total_carry_bps, 4),
            "cost_models": [m.as_dict() for m in self.cost_models],
            "windows": [w.as_dict() for w in self.windows],
            "generated_at": self.generated_at.isoformat(),
            "labels": {
                "data": "real mainnet funding history, read-only and keyless",
                "posted_model": (
                    "The posted column is a model, not a measurement: it recomputes the same window with a "
                    "maker round trip and assumes the limit order fills, which a real maker order may not."
                ),
                "not_advice": "Historical carry is not a forecast and says nothing about what the next window pays.",
            },
        }


def rolling_carry_bps(points: Sequence[FundingPoint], settlements: int) -> list[float]:
    """Carry (bps) of every consecutive window of ``settlements`` settlements, in order."""
    if settlements <= 0:
        raise ValueError("settlements must be > 0")
    n = len(points)
    if n < settlements:
        return []
    out: list[float] = []
    running = sum(p.rate for p in points[:settlements])
    out.append(running * BPS)
    for i in range(settlements, n):
        running += points[i].rate - points[i - settlements].rate
        out.append(running * BPS)
    return out


def analyse_funding(
    points: Sequence[FundingPoint],
    *,
    symbol: Optional[str] = None,
    interval_h: Optional[float] = None,
    holds_days: Sequence[float] = DEFAULT_HOLDS_DAYS,
    taker_roundtrip_bps: float = DEFAULT_TAKER_ROUNDTRIP_BPS,
    maker_roundtrip_bps: float = DEFAULT_MAKER_ROUNDTRIP_BPS,
    source: DataSource = DataSource.BINANCE_FUTURES_MAINNET,
    base_url: str = FUTURES_MAINNET_REST,
    now: Optional[datetime] = None,
) -> FundingAnalysis:
    """Compute the carry statistics and both cost models' window-clearing shares.

    ``holds_days`` are holding periods; each becomes ``round(hold_days * 24 / interval_h)``
    consecutive settlements, and a window "clears" when its summed carry in bps is at
    least the model's round trip.
    """
    pts = sorted(points, key=lambda p: p.funding_time_ms)
    if not pts:
        raise FundingHistoryError(
            "no funding settlements to analyse",
            hint="widen the lookback or check that the symbol has a USDS-M perpetual",
        )
    sym = symbol or pts[0].symbol
    measured = interval_h is None
    ivl = float(interval_h) if interval_h is not None else measure_interval_h(pts)
    if ivl <= 0:
        raise ValueError("interval_h must be > 0")

    rates = [p.rate for p in pts]
    ann = [annualise_pct(r, ivl) for r in rates]
    short_paid = sum(1 for r in rates if r > 0.0)
    span_days = (pts[-1].funding_time_ms - pts[0].funding_time_ms) / 86_400_000.0

    models = (
        CostModel(TAKEN_MODEL, float(taker_roundtrip_bps),
                  "perp leg crosses the spread; the Binance taker fee is 2 x 5 bps of this"),
        CostModel(POSTED_MODEL, float(maker_roundtrip_bps),
                  "perp leg posted as a maker; modelled, and it assumes the limit order fills"),
    )

    windows: list[WindowStat] = []
    per_day = 24.0 / ivl
    for hold in holds_days:
        n = int(round(float(hold) * per_day))
        if n <= 0 or n > len(pts):
            continue
        carries = rolling_carry_bps(pts, n)
        if not carries:
            continue
        med = statistics.median(carries)
        for model in models:
            cleared = sum(1 for c in carries if c >= model.roundtrip_bps)
            windows.append(
                WindowStat(
                    model=model.name,
                    hold_days=float(hold),
                    roundtrip_bps=model.roundtrip_bps,
                    settlements_per_window=n,
                    windows=len(carries),
                    cleared=cleared,
                    share_pct=100.0 * cleared / len(carries),
                    median_carry_bps=med,
                )
            )

    return FundingAnalysis(
        symbol=sym,
        source=source,
        base_url=base_url,
        samples=len(pts),
        interval_h=ivl,
        interval_measured=measured,
        first_ts=pts[0].ts,
        last_ts=pts[-1].ts,
        span_days=span_days,
        mean_annualised_pct=statistics.fmean(ann),
        median_annualised_pct=statistics.median(ann),
        min_annualised_pct=min(ann),
        max_annualised_pct=max(ann),
        short_paid_count=short_paid,
        short_paid_pct=100.0 * short_paid / len(pts),
        total_carry_bps=sum(rates) * BPS,
        cost_models=models,
        windows=tuple(windows),
        generated_at=now or datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- fetch
async def fetch_funding_history(
    http: httpx.AsyncClient,
    symbol: str,
    *,
    base_url: str = FUTURES_MAINNET_REST,
    lookback_days: float = DEFAULT_LOOKBACK_DAYS,
    max_rows: Optional[int] = None,
    end_time_ms: Optional[int] = None,
    page_limit: int = FUNDING_PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
    now: Optional[datetime] = None,
) -> list[FundingPoint]:
    """Page ``GET /fapi/v1/fundingRate`` backwards until ``lookback_days`` is covered.

    Public, keyless and read-only: no signature, no API key header, no order path.
    Pagination is driven by the oldest ``fundingTime`` seen (the venue may return fewer
    rows than ``page_limit``), and stops on an empty page, on a page that does not move
    the cursor, at ``max_pages``, or once ``max_rows`` is collected.
    """
    sym = str(symbol).upper()
    if not sym:
        raise ValueError("symbol is required")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be > 0")
    source = data_source_for(base_url)
    if source is None:
        raise FundingHistoryError(
            f"refusing to tag funding history from an unrecognised host {base_url!r}",
            hint=f"use {FUTURES_MAINNET_REST} for real mainnet funding history",
        )
    if source is not DataSource.BINANCE_FUTURES_MAINNET:
        log.warning("funding history requested from %s (%s), not mainnet", base_url, source.value)

    end_dt = now or datetime.now(timezone.utc)
    cursor = int(end_time_ms) if end_time_ms is not None else int(end_dt.timestamp() * 1000)
    floor_ms = int((end_dt - timedelta(days=float(lookback_days))).timestamp() * 1000)

    collected: dict[int, FundingPoint] = {}
    root = str(base_url).rstrip("/")
    for _page in range(max(1, int(max_pages))):
        params = {"symbol": sym, "limit": int(page_limit), "endTime": cursor}
        rows = await _get_json(http, f"{root}{FUNDING_RATE_PATH}", params, base_url=root)
        if not isinstance(rows, list):
            raise FundingHistoryError(
                f"GET {FUNDING_RATE_PATH} returned {type(rows).__name__}, expected a list",
                hint="the host answered, but not with a fundingRate payload; check the base URL",
            )
        page = parse_funding_rows(rows, symbol=sym, source=source)
        if not page:
            break
        before = len(collected)
        for p in page:
            collected[p.funding_time_ms] = p
        oldest = page[0].funding_time_ms
        if len(collected) == before or oldest <= floor_ms:
            break
        if max_rows is not None and len(collected) >= int(max_rows):
            break
        cursor = oldest - 1

    points = [collected[k] for k in sorted(collected) if collected[k].funding_time_ms >= floor_ms]
    if max_rows is not None and len(points) > int(max_rows):
        points = points[-int(max_rows):]
    if not points:
        raise FundingHistoryError(
            f"no funding settlements for {sym} in the last {lookback_days:g} day(s)",
            hint="check the symbol has a USDS-M perpetual, or widen the lookback",
        )
    log.info("funding history %s: %d settlements from %s", sym, len(points), root)
    return points


async def _get_json(http: httpx.AsyncClient, url: str, params: Mapping[str, Any], *, base_url: str) -> Any:
    """One keyless GET with typed, actionable failures (DNS named; venue error codes surfaced)."""
    try:
        r = await http.get(url, params=dict(params))
    except httpx.HTTPError as exc:
        if _is_dns_failure(exc):
            raise FundingHistoryError(
                f"GET {url}: hostname did not resolve", hint=resolver_hint(base_url)
            ) from exc
        raise FundingHistoryError(
            f"GET {url}: {type(exc).__name__}: {exc}",
            hint="mainnet market data is required here; Deltr will not fall back to another venue or to simulated data",
        ) from exc
    try:
        payload: Any = r.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping) and isinstance(payload.get("code"), int) and int(payload["code"]) < 0:
        raise FundingHistoryError(
            f"GET {url}: Binance error {payload.get('code')}: {payload.get('msg', '')}",
            code=int(payload["code"]),
        )
    if r.status_code >= 400:
        hint = ""
        if r.status_code in (401, 403):
            hint = (
                f"{urlsplit(base_url).hostname} refused this request; mainnet futures market data lives on "
                f"{FUTURES_MAINNET_REST} and needs no key"
            )
        raise FundingHistoryError(f"GET {url}: HTTP {r.status_code}", hint=hint)
    if payload is None:
        raise FundingHistoryError(f"GET {url}: non-JSON body (HTTP {r.status_code})")
    return payload


# --------------------------------------------------------------------------- service
@dataclass
class _CacheEntry:
    points: list[FundingPoint]
    fetched_mono: float


class FundingHistoryService:
    """Cached access to real mainnet funding history plus its analysis.

    Funding only settles every 4-8 hours, so a fetched window is cached for
    ``ttl_s`` (default one hour) per (symbol, lookback). ``refresh=True`` bypasses it.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str = FUTURES_MAINNET_REST,
        ttl_s: float = FUNDING_CACHE_TTL_S,
        lookback_days: float = DEFAULT_LOOKBACK_DAYS,
        taker_roundtrip_bps: float = DEFAULT_TAKER_ROUNDTRIP_BPS,
        maker_roundtrip_bps: float = DEFAULT_MAKER_ROUNDTRIP_BPS,
        monotonic: Any = time.monotonic,
    ) -> None:
        source = data_source_for(base_url)
        if source is not DataSource.BINANCE_FUTURES_MAINNET:
            raise ValueError(
                f"FundingHistoryService needs a mainnet futures host (got {base_url!r}); "
                f"the evidence claim is only honest against {FUTURES_MAINNET_REST}"
            )
        self.http = http
        self.base_url = str(base_url).rstrip("/")
        self.source = source
        self.ttl_s = float(ttl_s)
        self.lookback_days = float(lookback_days)
        self.taker_roundtrip_bps = float(taker_roundtrip_bps)
        self.maker_roundtrip_bps = float(maker_roundtrip_bps)
        self._monotonic = monotonic
        self._cache: dict[tuple[str, float], _CacheEntry] = {}
        self._locks: dict[tuple[str, float], asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0

    def cached_at_age_s(self, symbol: str, lookback_days: Optional[float] = None) -> Optional[float]:
        entry = self._cache.get((str(symbol).upper(), float(lookback_days or self.lookback_days)))
        return None if entry is None else max(0.0, self._monotonic() - entry.fetched_mono)

    def invalidate(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._cache.clear()
            return
        sym = str(symbol).upper()
        for key in [k for k in self._cache if k[0] == sym]:
            self._cache.pop(key, None)

    async def points(self, symbol: str, *, lookback_days: Optional[float] = None, refresh: bool = False) -> list[FundingPoint]:
        key = (str(symbol).upper(), float(lookback_days or self.lookback_days))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._cache.get(key)
            if entry is not None and not refresh and (self._monotonic() - entry.fetched_mono) < self.ttl_s:
                self.hits += 1
                return list(entry.points)
            self.misses += 1
            pts = await fetch_funding_history(self.http, key[0], base_url=self.base_url, lookback_days=key[1])
            self._cache[key] = _CacheEntry(points=list(pts), fetched_mono=self._monotonic())
            return list(pts)

    async def analyse(
        self,
        symbol: str,
        *,
        lookback_days: Optional[float] = None,
        holds_days: Sequence[float] = DEFAULT_HOLDS_DAYS,
        interval_h: Optional[float] = None,
        refresh: bool = False,
    ) -> FundingAnalysis:
        pts = await self.points(symbol, lookback_days=lookback_days, refresh=refresh)
        return analyse_funding(
            pts,
            symbol=str(symbol).upper(),
            interval_h=interval_h,
            holds_days=holds_days,
            taker_roundtrip_bps=self.taker_roundtrip_bps,
            maker_roundtrip_bps=self.maker_roundtrip_bps,
            source=self.source,
            base_url=self.base_url,
        )


# --------------------------------------------------------------------------- presentation
def render_funding_report(analysis: FundingAnalysis) -> str:
    """A readable markdown summary of one analysis (pure presentation, no new numbers)."""
    a = analysis
    lines = [
        f"# Funding carry: {a.symbol}",
        "",
        f"Source: `{a.source.value}` via `{a.base_url}{FUNDING_RATE_PATH}` (public, keyless, read-only).",
        f"{a.samples} settlements, {a.interval_h:g} h apart "
        f"({'measured from the data' if a.interval_measured else 'caller supplied'}), "
        f"{a.span_days:.1f} days"
        + (f" ({a.first_ts:%Y-%m-%d} to {a.last_ts:%Y-%m-%d})." if a.first_ts and a.last_ts else "."),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Mean carry | {a.mean_annualised_pct:+.2f}% annualised |",
        f"| Median carry | {a.median_annualised_pct:+.2f}% annualised |",
        f"| Range | {a.min_annualised_pct:+.2f}% to {a.max_annualised_pct:+.2f}% annualised |",
        f"| Settlements where the short is paid | {a.short_paid_count} / {a.samples} = {a.short_paid_pct:.1f}% |",
        "",
    ]
    holds = sorted({w.hold_days for w in a.windows})
    if holds:
        taken = next((m for m in a.cost_models if m.name == TAKEN_MODEL), None)
        posted = next((m for m in a.cost_models if m.name == POSTED_MODEL), None)
        lines += [
            "Share of rolling windows where funding carry alone clears the round trip:",
            "",
            f"| Hold | Perp leg TAKEN ({taken.roundtrip_bps:g} bps) | Perp leg POSTED ({posted.roundtrip_bps:g} bps) |"
            if taken and posted else "| Hold | Taken | Posted |",
            "|---|---|---|",
        ]
        for hold in holds:
            wt = a.window(TAKEN_MODEL, hold)
            wp = a.window(POSTED_MODEL, hold)
            lines.append(
                f"| {hold:g} days | {wt.share_pct:.1f}% ({wt.cleared}/{wt.windows}) |"
                f" {wp.share_pct:.1f}% ({wp.cleared}/{wp.windows}) |"
                if wt and wp else f"| {hold:g} days | - | - |"
            )
        lines.append("")
    lines += [
        "The POSTED column is a model, not a measurement: it recomputes the same windows against a maker "
        "round trip and assumes the limit order fills, which a real maker order may not. Historical carry "
        "is not a forecast.",
    ]
    return "\n".join(lines)


__all__ = [
    "FundingHistoryError",
    "FundingPoint",
    "FundingAnalysis",
    "FundingHistoryService",
    "CostModel",
    "WindowStat",
    "analyse_funding",
    "annualise_pct",
    "data_source_for",
    "fetch_funding_history",
    "measure_interval_h",
    "parse_funding_rows",
    "render_funding_report",
    "resolver_hint",
    "dns_hint_for",
    "rolling_carry_bps",
    "FUTURES_MAINNET_REST",
    "FUTURES_TESTNET_REST",
    "SPOT_MIRROR_REST",
    "FUNDING_RATE_PATH",
    "FUNDING_PAGE_LIMIT",
    "FUNDING_CACHE_TTL_S",
    "DEFAULT_TAKER_ROUNDTRIP_BPS",
    "DEFAULT_MAKER_ROUNDTRIP_BPS",
    "DEFAULT_HOLDS_DAYS",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_INTERVAL_H",
    "TAKEN_MODEL",
    "POSTED_MODEL",
]
