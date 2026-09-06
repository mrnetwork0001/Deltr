"""deltr/market_data.py — market data hubs.

* ``MarketHub`` — the Protocol every consumer (scout, executor, stress, engine)
  codes against.
* ``MarketDataHub`` — REST polling: futures ``premiumIndex`` + ``bookTicker``
  and the spot-mirror ``bookTicker`` every ``poll_interval_cex_s``; the DEX
  quote every ``poll_interval_dex_s``.  Market data comes from the optional
  ``futures_data`` client (the keyless MAINNET ``fapi.binance.com`` feed in
  every mode); ``futures`` stays the order-routing client and is never
  re-pointed here.  Provenance is DERIVED from the host that answers
  (``data_source_for``), so a testnet number can never be tagged mainnet, and a
  DNS failure is reported with the resolver to check rather than hidden.  Computes ``Freshness`` from the
  configured ``cex_stale_ms`` / ``dex_stale_ms``, records ``VenueHealth`` into
  ``State.venues`` and NEVER raises from ``run()``: a failing venue keeps its
  last quote (whose age then grows past the threshold) and is marked unhealthy.
  ``perp_ref_price`` is always ``funding.mark_price`` (decision 8).
* ``ReplayHub`` — same interface, driven by the JSONL fixture written by
  ``scripts/record_replay.py`` (line 1 = ``{"header": true, "schema":
  "MarketState/v1", ...}``, then one ``MarketState`` JSON per line).  Every
  emitted row gets ``source = REPLAY``, its timestamps are shifted onto the
  replay clock so freshness ages are the RECORDED ages (rows are never stale
  merely because the recording is old), rows are emitted in file order, and the
  hub loops by default.
* ``market_state_to_jsonl`` / ``market_state_from_jsonl`` — the codecs.

The venue clients are duck-typed (``FuturesLike`` / ``SpotLike`` / ``DexLike``
Protocols) so this module imports without ``deltr.venues.*``.  All logging goes
to stderr through ``logging``; nothing prints to stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from deltr.config import REPO_ROOT, Settings
from deltr.funding_history import FUTURES_MAINNET_REST, data_source_for, dns_hint_for
from deltr.models import (
    DataSource,
    DexQuote,
    Freshness,
    FundingSnapshot,
    MarketState,
    Quote,
    SymbolFilters,
    VenueHealth,
    utcnow,
)
from deltr.state import State

log = logging.getLogger("deltr.market_data")

REPLAY_FIXTURE_DEFAULT = "tests/fixtures/replay.jsonl"
REPLAY_SCHEMA = "MarketState/v1"

VENUE_FUTURES = "binance_futures_testnet"
VENUE_FUTURES_MAINNET = "binance_futures_mainnet"   # the keyless mainnet market-data feed (never an order venue)
VENUE_SPOT = "binance_spot_mirror"
VENUE_DEX = "pancakeswap_v3"
VENUE_ORDER = (VENUE_DEX, VENUE_FUTURES, VENUE_SPOT)

# The health-slot name and provenance tag that go with a futures data feed.
FUTURES_VENUE_BY_SOURCE: dict[DataSource, str] = {
    DataSource.BINANCE_FUTURES_MAINNET: VENUE_FUTURES_MAINNET,
    DataSource.BINANCE_FUTURES_TESTNET: VENUE_FUTURES,
}

MISSING_AGE_MS = 2**31 - 1          # "never received" sentinel (fits any int32 consumer)
MAX_REPLAY_GAP_S = 5.0              # cap on inter-row sleep so a recorder hiccup never stalls the demo
FEED_STALE_REASON = "feed_stale(stress)"

TickCallback = Callable[[MarketState], Awaitable[None]]
Clock = Callable[[], datetime]

_SECRET_RE = re.compile(r"(?i)(signature|api[_-]?key|secret[_-]?key|secret|token|key)=([^&\s\"']+)")


def _redact(text: str) -> str:
    """Strip key/secret/token/signature VALUES from any text that may land in a health record or log."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _age_ms(now: datetime, ts: Optional[datetime]) -> int:
    if ts is None:
        return MISSING_AGE_MS
    return max(0, int((_aware(now) - _aware(ts)).total_seconds() * 1000))


# --------------------------------------------------------------------------- codecs
def market_state_to_jsonl(ms: MarketState) -> str:
    """One JSON line (no trailing newline) — the exact format ``scripts/record_replay.py`` writes."""
    return ms.model_dump_json()


def market_state_from_jsonl(line: str) -> MarketState:
    """Parse one fixture row (computed fields such as ``Quote.mid`` are ignored on input)."""
    return MarketState.model_validate_json(line)


def compute_freshness(
    now: datetime,
    *,
    cex_ts: Optional[datetime],
    dex_ts: Optional[datetime],
    spot_ts: Optional[datetime],
    cex_stale_ms: int,
    dex_stale_ms: int,
    frozen: bool = False,
) -> Freshness:
    """Ages relative to ``now`` and the ok/reason verdict (cex checked before dex; spot never blocks)."""
    return freshness_from_ages(
        cex_age_ms=_age_ms(now, cex_ts),
        dex_age_ms=_age_ms(now, dex_ts),
        spot_age_ms=_age_ms(now, spot_ts),
        cex_stale_ms=cex_stale_ms,
        dex_stale_ms=dex_stale_ms,
        frozen=frozen,
    )


def freshness_from_ages(
    *, cex_age_ms: int, dex_age_ms: int, spot_age_ms: int, cex_stale_ms: int, dex_stale_ms: int, frozen: bool = False
) -> Freshness:
    """Verdict from already-known ages.  ``frozen`` (stress ``feed_stale``) forces ``ok=False``."""
    reason: Optional[str] = None
    if frozen:
        reason = FEED_STALE_REASON
    elif cex_age_ms > cex_stale_ms:
        reason = "cex_stale"
    elif dex_age_ms > dex_stale_ms:
        reason = "dex_stale"
    return Freshness(cex_age_ms=cex_age_ms, dex_age_ms=dex_age_ms, spot_age_ms=spot_age_ms, ok=reason is None, reason=reason)


# --------------------------------------------------------------------------- protocols
class FuturesLike(Protocol):
    async def premium_index(self, symbol: str) -> FundingSnapshot: ...
    async def book_ticker(self, symbol: str) -> Quote: ...


class SpotLike(Protocol):
    async def book_ticker(self, symbol: str) -> Quote: ...


class DexLike(Protocol):
    async def dex_quote(self, size_base: float) -> DexQuote: ...


@runtime_checkable
class MarketHub(Protocol):
    """What every consumer of market data depends on."""

    def on_tick(self, cb: TickCallback) -> None: ...
    async def tick_once(self) -> MarketState: ...
    async def run(self, stop: asyncio.Event) -> None: ...
    def snapshot(self) -> Optional[MarketState]: ...
    def health(self) -> list[VenueHealth]: ...
    def freshness(self) -> Freshness: ...


# --------------------------------------------------------------------------- shared base
class _HubBase:
    """Callback registry, feed-freeze flag and the parts of the interface both hubs share."""

    def __init__(self, state: State, clock: Optional[Clock] = None) -> None:
        self.state = state
        self.settings: Settings = state.settings
        self._clock: Clock = clock or utcnow
        self._callbacks: list[TickCallback] = []
        self._last: Optional[MarketState] = None
        self._feed_frozen = False
        self.ticks: int = 0
        self.callback_errors: int = 0

    # interface
    def on_tick(self, cb: TickCallback) -> None:
        """Register a coroutine callback; callbacks are awaited in registration order after every tick."""
        self._callbacks.append(cb)

    def snapshot(self) -> Optional[MarketState]:
        return self._last

    def freshness(self) -> Freshness:
        if self._last is None:
            return freshness_from_ages(
                cex_age_ms=MISSING_AGE_MS, dex_age_ms=MISSING_AGE_MS, spot_age_ms=MISSING_AGE_MS,
                cex_stale_ms=self.settings.cex_stale_ms, dex_stale_ms=self.settings.dex_stale_ms,
            )
        return self._last.freshness

    # stress hook
    def set_feed_frozen(self, frozen: bool) -> None:
        """Stress ``feed_stale``: while frozen no new data is applied, so ages grow and ``Freshness.ok`` is False."""
        self._feed_frozen = bool(frozen)

    @property
    def feed_frozen(self) -> bool:
        active = self.state.stress_active or ""
        return self._feed_frozen or "feed_stale" in active

    # helpers
    def now(self) -> datetime:
        return _aware(self._clock())

    async def _dispatch(self, ms: MarketState) -> None:
        """Set ``state.market`` then await callbacks in order; a failing callback never stops the others."""
        self._last = ms
        self.state.market = ms
        self.ticks += 1
        for cb in list(self._callbacks):
            try:
                await cb(ms)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - isolate consumer bugs from the feed
                self.callback_errors += 1
                log.exception("on_tick callback %r failed", getattr(cb, "__qualname__", cb))

    def _record_health(self, name: str, ok: bool, age_ms: int, source: DataSource, detail: str = "") -> VenueHealth:
        prev = self.state.venues.get(name)
        if not ok and any(m in str(detail).lower() for m in ("nodename nor servname", "getaddrinfo", "name or service not known", "name resolution")):
            # A DNS failure is the one outage the operator can fix in 30 seconds; say so on the health line.
            host = getattr(getattr(self, "futures_data", None), "base_url", "") if "futures" in name else ""
            host = host.replace("https://", "").replace("http://", "").strip("/") or "the venue host"
            detail = f"DNS: {host} does not resolve via this machine's resolver; set DNS to 1.1.1.1 (works) - no fallback venue"
        h = VenueHealth(name=name, ok=ok, age_ms=age_ms, source=source, detail=_redact(detail)[:200])
        self.state.record_venue(h)
        if prev is not None and prev.ok != ok:
            level = "info" if ok else "warn"
            self.state.emit("log", f"venue {name} {'recovered' if ok else 'unhealthy'}: {h.detail or 'ok'}", level=level,
                            data={"venue": name, "ok": ok, "age_ms": age_ms})
        return h


# --------------------------------------------------------------------------- live polling hub
class MarketDataHub(_HubBase):
    """REST polling hub (PAPER and TESTNET).  Never raises from ``run()``."""

    def __init__(
        self,
        state: State,
        futures: FuturesLike,
        spot: SpotLike,
        dex: DexLike,
        settings: Settings,
        filters: SymbolFilters,
        quote_size_base: float,
        clock: Optional[Clock] = None,
        futures_data: Optional[FuturesLike] = None,
    ) -> None:
        super().__init__(state, clock)
        self.settings = settings
        self.futures = futures
        # Market data comes from ``futures_data`` when one is supplied (the keyless mainnet
        # feed); ``futures`` stays the order-routing client and is never re-pointed here.
        self.futures_data: FuturesLike = futures_data if futures_data is not None else futures
        self.spot = spot
        self.dex = dex
        self.filters = filters
        self.symbol = filters.symbol or settings.symbol
        # Provenance is DERIVED from the host that will actually answer, never asserted by a
        # caller, so a testnet number can never be tagged mainnet.  ``None`` (a duck-typed or
        # unrecognised client) means "keep whatever tag the payload already carried".
        self.futures_data_url: str = str(getattr(self.futures_data, "base_url", "") or "")
        self.futures_source: Optional[DataSource] = data_source_for(self.futures_data_url)
        self.venue_futures: str = FUTURES_VENUE_BY_SOURCE.get(self.futures_source or DataSource.BINANCE_FUTURES_TESTNET, VENUE_FUTURES)
        self.venue_order: tuple[str, ...] = (VENUE_DEX, self.venue_futures, VENUE_SPOT)
        self._quote_size = float(quote_size_base)
        self._funding: Optional[FundingSnapshot] = None
        self._perp_book: Optional[Quote] = None
        self._spot: Optional[Quote] = None
        self._dex: Optional[DexQuote] = None
        self._dex_fetched_mono: Optional[float] = None
        self._errors: dict[str, str] = {}

    # knobs
    def set_quote_size(self, size_base: float) -> None:
        """Change the DEX quote size (BNB); the next tick re-quotes immediately."""
        if size_base <= 0:
            raise ValueError("quote size must be > 0")
        if abs(size_base - self._quote_size) > 1e-12:
            self._quote_size = float(size_base)
            self._dex_fetched_mono = None

    @property
    def quote_size(self) -> float:
        return self._quote_size

    # interface
    @property
    def futures_tag(self) -> DataSource:
        """The provenance tag for the futures feed (the derived one, or the historical default)."""
        return self.futures_source or DataSource.BINANCE_FUTURES_TESTNET

    def health(self) -> list[VenueHealth]:
        now = self.now()
        out: list[VenueHealth] = []
        for name in self.venue_order:
            h = self.state.venues.get(name)
            if h is None:
                src = {VENUE_DEX: DataSource.BSC_MAINNET_CHAIN, self.venue_futures: self.futures_tag,
                       VENUE_SPOT: DataSource.BINANCE_SPOT_MIRROR}[name]
                h = VenueHealth(name=name, ok=False, age_ms=MISSING_AGE_MS, source=src, detail="no data yet")
            else:
                ts = {VENUE_DEX: self._dex, self.venue_futures: self._funding, VENUE_SPOT: self._spot}[name]
                h = h.model_copy(update={"age_ms": _age_ms(now, getattr(ts, "ts", None))})
            out.append(h)
        return out

    async def tick_once(self, *, force_dex: bool = False) -> MarketState:
        """Poll (unless frozen), rebuild the MarketState, record health, dispatch callbacks.  Never raises."""
        if not self.feed_frozen:
            await self._poll(force_dex=force_dex)
        ms = self._build()
        await self._dispatch(ms)
        return ms

    async def run(self, stop: asyncio.Event) -> None:
        """Tick every ``poll_interval_cex_s`` until ``stop`` is set.  Cancellation-safe; swallows everything else."""
        interval = float(self.settings.poll_interval_cex_s)
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the feed loop must survive anything
                log.exception("tick failed; continuing")
            delay = max(0.0, interval - (time.monotonic() - t0))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # internals
    def _retag(self, obj: Any) -> Any:
        """Stamp the provenance the answering host warrants; leave it alone when it is unknown."""
        src = self.futures_source
        if src is None or getattr(obj, "source", None) is src:
            return obj
        return obj.model_copy(update={"source": src})

    @staticmethod
    def _venue_error(label: str, exc: Any, base_url: str) -> str:
        """One health-record line for a failed venue call, naming the resolver on a DNS failure."""
        base = f"{label}: {type(exc).__name__}: {exc}"
        hint = dns_hint_for(exc, base_url) if isinstance(exc, BaseException) and base_url else None
        return f"{label}: {hint}" if hint else base

    def _dex_due(self, force: bool) -> bool:
        if force or self._dex_fetched_mono is None:
            return True
        return (time.monotonic() - self._dex_fetched_mono) >= float(self.settings.poll_interval_dex_s)

    async def _poll(self, *, force_dex: bool) -> None:
        dex_due = self._dex_due(force_dex)
        coros: list[Awaitable[Any]] = [
            self.futures_data.premium_index(self.symbol),
            self.futures_data.book_ticker(self.symbol),
            self.spot.book_ticker(self.symbol),
        ]
        if dex_due:
            coros.append(self.dex.dex_quote(self._quote_size))
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, Exception):
                raise r  # never swallow CancelledError / KeyboardInterrupt
        funding, book, spot = results[0], results[1], results[2]
        dexq = results[3] if dex_due else None

        # futures (premiumIndex is the reference; bookTicker is for fill simulation only)
        fut_errs: list[str] = []
        if isinstance(funding, FundingSnapshot):
            self._funding = self._retag(funding)
        else:
            fut_errs.append(self._venue_error("premiumIndex", funding, self.futures_data_url))
        if isinstance(book, Quote):
            self._perp_book = self._retag(book)
        else:
            fut_errs.append(self._venue_error("bookTicker", book, self.futures_data_url))
        now = self.now()
        self._record_health(self.venue_futures, not fut_errs, _age_ms(now, getattr(self._funding, "ts", None)),
                            self.futures_tag, "; ".join(fut_errs))

        # spot mirror
        spot_url = str(getattr(self.spot, "base_url", "") or "")
        if isinstance(spot, Quote):
            self._spot = spot
            self._record_health(VENUE_SPOT, True, _age_ms(now, spot.ts), DataSource.BINANCE_SPOT_MIRROR)
        else:
            self._record_health(VENUE_SPOT, False, _age_ms(now, getattr(self._spot, "ts", None)),
                                DataSource.BINANCE_SPOT_MIRROR, self._venue_error("bookTicker", spot, spot_url))

        # dex
        if dex_due:
            if isinstance(dexq, DexQuote):
                self._dex = dexq
                self._dex_fetched_mono = time.monotonic()
                self._record_health(VENUE_DEX, True, _age_ms(now, dexq.ts), DataSource.BSC_MAINNET_CHAIN)
            else:
                # leave _dex_fetched_mono as-is so the next tick retries immediately if we never had a quote
                self._record_health(VENUE_DEX, False, _age_ms(now, getattr(self._dex, "ts", None)),
                                    DataSource.BSC_MAINNET_CHAIN, f"{type(dexq).__name__}: {dexq}")

    def _build(self) -> MarketState:
        now = self.now()
        cex_ts = None
        if self._funding is not None:
            cex_ts = self._funding.ts
            if self._perp_book is not None and _aware(self._perp_book.ts) < _aware(cex_ts):
                cex_ts = self._perp_book.ts  # oldest of the two CEX inputs drives the age
        fr = compute_freshness(
            now,
            cex_ts=cex_ts,
            dex_ts=getattr(self._dex, "ts", None),
            spot_ts=getattr(self._spot, "ts", None),
            cex_stale_ms=self.settings.cex_stale_ms,
            dex_stale_ms=self.settings.dex_stale_ms,
            frozen=self.feed_frozen,
        )
        return MarketState(
            symbol=self.symbol,
            dex=self._dex,
            cex_perp_book=self._perp_book,
            cex_spot_ref=self._spot,
            funding=self._funding,
            perp_ref_price=self._funding.mark_price if self._funding is not None else None,
            freshness=fr,
            ts=now,
            source=self.futures_tag,
        )


# --------------------------------------------------------------------------- replay hub
class ReplayHub(_HubBase):
    """Deterministic feed from a ``MarketState/v1`` JSONL fixture (same interface as ``MarketDataHub``)."""

    def __init__(
        self,
        state: State,
        path: str,
        speed: float = 1.0,
        loop: bool = True,
        clock: Optional[Clock] = None,
        restamp: bool = True,
    ) -> None:
        super().__init__(state, clock)
        if speed <= 0:
            raise ValueError("replay speed must be > 0")
        self.path = str(path)
        self.speed = float(speed)
        self.loop = bool(loop)
        self.restamp = bool(restamp)
        self.header, self.rows = load_replay_fixture(self.path)
        self.symbol = str(self.header.get("symbol") or (self.rows[0].symbol if self.rows else state.settings.symbol))
        self.index = 0            # next row to emit
        self.cycles = 0           # completed passes over the file
        self.state.replay = True

    # knobs (duck-typed parity with MarketDataHub; replay size is whatever was recorded)
    def set_quote_size(self, size_base: float) -> None:  # noqa: D401 - intentional no-op
        """No-op: the recorded DEX quote size is fixed by the fixture header."""
        return None

    @property
    def quote_size(self) -> float:
        return float(self.header.get("size_base") or (self.rows[0].dex.size_base if self.rows and self.rows[0].dex else 0.0))

    @property
    def exhausted(self) -> bool:
        return not self.loop and self.index >= len(self.rows)

    # interface
    def health(self) -> list[VenueHealth]:
        fr = self.freshness()
        ages = {VENUE_DEX: fr.dex_age_ms, VENUE_FUTURES: fr.cex_age_ms, VENUE_SPOT: fr.spot_age_ms}
        return [VenueHealth(name=n, ok=self._last is not None, age_ms=ages[n], source=DataSource.REPLAY,
                            detail=f"replay {Path(self.path).name}") for n in VENUE_ORDER]

    async def tick_once(self) -> MarketState:
        """Emit the next row (wrapping when ``loop``).  Raises ``StopAsyncIteration`` once a non-looping replay ends."""
        if not self.rows:
            raise StopAsyncIteration("replay fixture has no rows")
        if self.index >= len(self.rows):
            if not self.loop:
                raise StopAsyncIteration("replay exhausted")
            self.index = 0
            self.cycles += 1
        row = self.rows[self.index]
        self.index += 1
        ms = self._emit_row(row)
        await self._dispatch(ms)
        return ms

    async def run(self, stop: asyncio.Event) -> None:
        """Emit rows on the recorded cadence divided by ``speed``; returns when stopped or (non-loop) exhausted."""
        while not stop.is_set():
            try:
                await self.tick_once()
            except StopAsyncIteration:
                log.info("replay finished: %d rows, %d cycles", len(self.rows), self.cycles)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("replay tick failed; continuing")
            delay = self._delay_after(self.index - 1)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # internals
    def _delay_after(self, i: int) -> float:
        """Seconds to wait after emitting row ``i`` (recorded gap, capped, divided by speed)."""
        if not self.rows:
            return 0.0
        j = (i + 1) % len(self.rows)
        if j == 0:
            gap = float(self.settings.poll_interval_cex_s)
        else:
            gap = (_aware(self.rows[j].ts) - _aware(self.rows[i].ts)).total_seconds()
        gap = min(max(gap, 0.0), MAX_REPLAY_GAP_S)
        return gap / self.speed

    def _emit_row(self, row: MarketState) -> MarketState:
        """Copy the row onto the replay clock: source=REPLAY, timestamps shifted, freshness re-verdicted."""
        now = self.now()
        recorded = row.freshness
        if not self.restamp:
            fr = freshness_from_ages(
                cex_age_ms=recorded.cex_age_ms, dex_age_ms=recorded.dex_age_ms, spot_age_ms=recorded.spot_age_ms,
                cex_stale_ms=self.settings.cex_stale_ms, dex_stale_ms=self.settings.dex_stale_ms, frozen=self.feed_frozen,
            )
            return row.model_copy(update={"freshness": fr, "source": DataSource.REPLAY,
                                          "perp_ref_price": row.funding.mark_price if row.funding else row.perp_ref_price})
        delta: timedelta = now - _aware(row.ts)
        delta_ms = int(delta.total_seconds() * 1000)

        def shift(q: Any) -> Any:
            if q is None:
                return None
            upd: dict[str, Any] = {"ts": _aware(q.ts) + delta}
            if isinstance(q, FundingSnapshot):
                upd["next_funding_time_ms"] = q.next_funding_time_ms + delta_ms
            return q.model_copy(update=upd)

        dex = shift(row.dex)
        book = shift(row.cex_perp_book)
        spot = shift(row.cex_spot_ref)
        funding = shift(row.funding)
        cex_ts = None
        if funding is not None:
            cex_ts = funding.ts
            if book is not None and _aware(book.ts) < _aware(cex_ts):
                cex_ts = book.ts
        fr = compute_freshness(
            now, cex_ts=cex_ts, dex_ts=getattr(dex, "ts", None), spot_ts=getattr(spot, "ts", None),
            cex_stale_ms=self.settings.cex_stale_ms, dex_stale_ms=self.settings.dex_stale_ms, frozen=self.feed_frozen,
        )
        return MarketState(
            symbol=row.symbol, dex=dex, cex_perp_book=book, cex_spot_ref=spot, funding=funding,
            perp_ref_price=funding.mark_price if funding is not None else row.perp_ref_price,
            freshness=fr, ts=now, source=DataSource.REPLAY,
        )


# --------------------------------------------------------------------------- fixture loader
def resolve_fixture_path(path: Optional[str] = None) -> Path:
    """Absolute path of a fixture: absolute paths pass through; relative ones resolve against the repo root."""
    p = Path(path or REPLAY_FIXTURE_DEFAULT)
    return p if p.is_absolute() else (REPO_ROOT / p)


def load_replay_fixture(path: str) -> tuple[dict[str, Any], list[MarketState]]:
    """Parse ``scripts/record_replay.py`` output.  Fails loudly on a missing/legacy header or a bad row."""
    p = resolve_fixture_path(path)
    if not p.exists():
        raise FileNotFoundError(f"replay fixture not found: {p}")
    rows: list[MarketState] = []
    header: dict[str, Any] = {}
    with p.open("r", encoding="utf-8") as fh:
        first = fh.readline()
        if not first.strip():
            raise ValueError(f"replay fixture is empty: {p}")
        try:
            header = json.loads(first)
        except json.JSONDecodeError as e:
            raise ValueError(f"replay fixture line 1 is not JSON: {p}: {e}") from e
        if not isinstance(header, dict) or header.get("header") is not True:
            raise ValueError(f"replay fixture line 1 must be a header object with \"header\": true: {p}")
        schema = header.get("schema")
        if schema != REPLAY_SCHEMA:
            raise ValueError(f"replay fixture schema {schema!r} unsupported (need {REPLAY_SCHEMA!r}); "
                             f"convert with scripts/record_replay.py --convert: {p}")
        for lineno, line in enumerate(fh, start=2):
            if not line.strip():
                continue
            try:
                rows.append(market_state_from_jsonl(line))
            except Exception as e:  # noqa: BLE001 - re-raise with the line number
                raise ValueError(f"replay fixture row invalid at line {lineno}: {type(e).__name__}: {e}") from e
    if not rows:
        raise ValueError(f"replay fixture has a header but no rows: {p}")
    return header, rows


__all__ = [
    "MarketHub",
    "MarketDataHub",
    "ReplayHub",
    "FuturesLike",
    "SpotLike",
    "DexLike",
    "market_state_to_jsonl",
    "market_state_from_jsonl",
    "compute_freshness",
    "freshness_from_ages",
    "load_replay_fixture",
    "resolve_fixture_path",
    "REPLAY_FIXTURE_DEFAULT",
    "REPLAY_SCHEMA",
    "MISSING_AGE_MS",
    "FEED_STALE_REASON",
    "VENUE_FUTURES",
    "VENUE_FUTURES_MAINNET",
    "FUTURES_VENUE_BY_SOURCE",
    "FUTURES_MAINNET_REST",
    "VENUE_SPOT",
    "VENUE_DEX",
    "VENUE_ORDER",
]
