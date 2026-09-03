"""MarketDataHub with fake venue clients; ReplayHub on an inline fixture and on the shipped recording."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deltr.bus import EventBus
from deltr.config import REPO_ROOT, Settings
from deltr.market_data import (
    FEED_STALE_REASON,
    MISSING_AGE_MS,
    REPLAY_FIXTURE_DEFAULT,
    REPLAY_SCHEMA,
    VENUE_DEX,
    VENUE_FUTURES,
    VENUE_ORDER,
    VENUE_SPOT,
    MarketDataHub,
    MarketHub,
    ReplayHub,
    load_replay_fixture,
    market_state_from_jsonl,
    market_state_to_jsonl,
    resolve_fixture_path,
)
from deltr.models import DataSource, DexQuote, Freshness, FundingSnapshot, MarketState, Quote, SymbolFilters, Venue
from deltr.state import State

SYMBOL = "BNBUSDT"
T0 = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- fakes
class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_settings(**over) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


def make_funding(ts: datetime, mark: float = 686.34) -> FundingSnapshot:
    return FundingSnapshot(symbol=SYMBOL, mark_price=mark, index_price=mark - 0.2, last_funding_rate=0.0001,
                           next_funding_time_ms=int(ts.timestamp() * 1000) + 3_600_000, interval_h=8,
                           annualized_pct=0.0001 * 3 * 365 * 100, ts=ts)


def make_quote(ts: datetime, venue: Venue, mid: float, source: DataSource) -> Quote:
    return Quote(venue=venue, symbol=SYMBOL, bid=mid - 0.01, ask=mid + 0.01, bid_qty=10.0, ask_qty=10.0, ts=ts, source=source)


def make_dex(ts: datetime, mid: float = 686.10, size: float = 4.85) -> DexQuote:
    exec_buy = mid * (1 + 1.3e-4)
    exec_sell = mid * (1 - 1.2e-4)
    return DexQuote(pool="0x172fcD41E0913e95784454622d1c3724f546f849", fee_tier=100, fee_bps=1.0,
                    sqrt_price_x96=3024721431835227127309627620, tick=-65330, mid_price=mid, size_base=size,
                    exec_price_buy=exec_buy, amount_in_usdt=exec_buy * size, exec_price_sell=exec_sell,
                    amount_out_usdt=exec_sell * size, impact_bps=0.3, gas_units=162_878, gas_price_wei=50_000_000,
                    gas_usd=0.0056, block=119_463_660, ts=ts)


class FakeFutures:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.fail_premium: Exception | None = None
        self.fail_book: Exception | None = None
        self.calls = {"premium_index": 0, "book_ticker": 0}
        self.mark = 686.34

    async def premium_index(self, symbol: str) -> FundingSnapshot:
        self.calls["premium_index"] += 1
        if self.fail_premium:
            raise self.fail_premium
        return make_funding(self.clock(), self.mark)

    async def book_ticker(self, symbol: str) -> Quote:
        self.calls["book_ticker"] += 1
        if self.fail_book:
            raise self.fail_book
        return make_quote(self.clock(), Venue.BINANCE_FUTURES, 686.18, DataSource.BINANCE_FUTURES_TESTNET)


class FakeSpot:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.fail: Exception | None = None
        self.calls = 0

    async def book_ticker(self, symbol: str) -> Quote:
        self.calls += 1
        if self.fail:
            raise self.fail
        return make_quote(self.clock(), Venue.BINANCE_SPOT, 686.30, DataSource.BINANCE_SPOT_MIRROR)


class FakeDex:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.fail: Exception | None = None
        self.calls: list[float] = []

    async def dex_quote(self, size_base: float) -> DexQuote:
        self.calls.append(size_base)
        if self.fail:
            raise self.fail
        return make_dex(self.clock(), size=size_base)


def build_hub(**settings_over):
    clock = FakeClock()
    settings = make_settings(**settings_over)
    state = State(settings, EventBus())
    fut, spot, dex = FakeFutures(clock), FakeSpot(clock), FakeDex(clock)
    hub = MarketDataHub(state, fut, spot, dex, settings, SymbolFilters(symbol=SYMBOL), quote_size_base=4.85, clock=clock)
    return hub, state, clock, fut, spot, dex


# --------------------------------------------------------------------------- MarketDataHub
async def test_tick_builds_state_with_perp_ref_from_mark_and_fresh_ok():
    hub, state, clock, fut, spot, dex = build_hub()
    assert isinstance(hub, MarketHub)
    assert hub.snapshot() is None and hub.freshness().ok is False and hub.freshness().reason == "cex_stale"
    ms = await hub.tick_once()
    assert state.market is ms and hub.snapshot() is ms
    assert ms.perp_ref_price == ms.funding.mark_price == 686.34
    assert ms.cex_perp_book.mid != ms.perp_ref_price  # bookTicker is NOT the reference
    assert ms.freshness.ok and ms.freshness.reason is None
    assert ms.freshness.cex_age_ms == 0 and ms.freshness.dex_age_ms == 0 and ms.freshness.spot_age_ms == 0
    assert ms.source == DataSource.BINANCE_FUTURES_TESTNET and ms.symbol == SYMBOL
    assert dex.calls == [4.85] and fut.calls == {"premium_index": 1, "book_ticker": 1} and spot.calls == 1
    health = {h.name: h for h in hub.health()}
    assert [h.name for h in hub.health()] == list(VENUE_ORDER)
    assert all(health[n].ok for n in VENUE_ORDER)
    assert health[VENUE_DEX].source == DataSource.BSC_MAINNET_CHAIN
    assert health[VENUE_FUTURES].source == DataSource.BINANCE_FUTURES_TESTNET
    assert health[VENUE_SPOT].source == DataSource.BINANCE_SPOT_MIRROR
    assert set(state.venues) == set(VENUE_ORDER)


async def test_dex_polled_on_its_own_slower_interval():
    hub, state, clock, fut, spot, dex = build_hub()
    await hub.tick_once()
    await hub.tick_once()
    assert fut.calls["premium_index"] == 2 and len(dex.calls) == 1  # within poll_interval_dex_s -> no re-quote
    hub._dex_fetched_mono -= 10.0  # pretend 10 s of wall time passed
    await hub.tick_once()
    assert len(dex.calls) == 2
    await hub.tick_once(force_dex=True)
    assert len(dex.calls) == 3
    hub.set_quote_size(2.0)
    await hub.tick_once()
    assert dex.calls[-1] == 2.0 and hub.quote_size == 2.0
    with pytest.raises(ValueError):
        hub.set_quote_size(0)


async def test_cex_failure_keeps_last_quote_then_goes_stale():
    hub, state, clock, fut, spot, dex = build_hub()
    ms1 = await hub.tick_once()
    fut.fail_premium = RuntimeError("boom 503")
    clock.advance(2.0)
    ms2 = await hub.tick_once()
    assert ms2.funding is ms1.funding  # last good value kept
    assert ms2.freshness.cex_age_ms == 2000 and ms2.freshness.ok  # 2 s < 5 s threshold
    fh = state.venues[VENUE_FUTURES]
    assert fh.ok is False and "boom 503" in fh.detail and fh.age_ms == 2000
    clock.advance(3.5)
    ms3 = await hub.tick_once()
    assert ms3.freshness.cex_age_ms == 5500 and ms3.freshness.ok is False and ms3.freshness.reason == "cex_stale"
    fut.fail_premium = None
    ms4 = await hub.tick_once()
    assert ms4.freshness.ok and state.venues[VENUE_FUTURES].ok
    # health transitions were announced on the bus (unhealthy, recovered)
    msgs = [e.message for e in state.bus.history()]
    assert any("unhealthy" in m for m in msgs) and any("recovered" in m for m in msgs)


async def test_dex_failure_yields_dex_stale_after_threshold():
    hub, state, clock, fut, spot, dex = build_hub()
    await hub.tick_once()
    dex.fail = ConnectionError("rpc down")
    clock.advance(9.5)
    hub._dex_fetched_mono -= 10.0
    ms = await hub.tick_once()
    assert ms.freshness.cex_age_ms == 0 and ms.freshness.dex_age_ms == 9500
    assert ms.freshness.ok is False and ms.freshness.reason == "dex_stale"
    assert state.venues[VENUE_DEX].ok is False and "rpc down" in state.venues[VENUE_DEX].detail
    assert ms.dex is not None  # last good DEX quote retained for display


async def test_spot_failure_never_blocks_actionability():
    hub, state, clock, fut, spot, dex = build_hub()
    spot.fail = TimeoutError("slow")
    ms = await hub.tick_once()
    assert ms.cex_spot_ref is None and ms.freshness.spot_age_ms == MISSING_AGE_MS
    assert ms.freshness.ok is True
    assert state.venues[VENUE_SPOT].ok is False and state.venues[VENUE_SPOT].age_ms == MISSING_AGE_MS


async def test_everything_down_is_not_fresh_and_never_raises():
    hub, state, clock, fut, spot, dex = build_hub()
    fut.fail_premium = fut.fail_book = spot.fail = dex.fail = RuntimeError("all down")
    ms = await hub.tick_once()
    assert ms.perp_ref_price is None and ms.dex is None and ms.freshness.ok is False
    assert ms.freshness.reason == "cex_stale" and ms.freshness.cex_age_ms == MISSING_AGE_MS
    assert all(not h.ok for h in hub.health())
    ms.model_dump(mode="json")


async def test_health_detail_redacts_secrets():
    hub, state, clock, fut, spot, dex = build_hub()
    fut.fail_book = RuntimeError("GET /fapi/v1/order?symbol=BNBUSDT&timestamp=1&signature=deadbeefcafe api_key=XYZ")
    await hub.tick_once()
    detail = state.venues[VENUE_FUTURES].detail
    assert "deadbeefcafe" not in detail and "XYZ" not in detail and "<redacted>" in detail


async def test_on_tick_callbacks_in_order_with_error_isolation():
    hub, state, clock, fut, spot, dex = build_hub()
    seen: list[str] = []

    async def first(ms: MarketState) -> None:
        seen.append("first")
        raise ValueError("scout bug")

    async def second(ms: MarketState) -> None:
        await asyncio.sleep(0)
        seen.append("second")

    hub.on_tick(first)
    hub.on_tick(second)
    ms = await hub.tick_once()
    assert seen == ["first", "second"] and hub.callback_errors == 1 and hub.ticks == 1
    assert state.market is ms


async def test_feed_frozen_stress_marks_feed_stale_and_stops_polling():
    hub, state, clock, fut, spot, dex = build_hub()
    await hub.tick_once()
    hub.set_feed_frozen(True)
    clock.advance(1.0)
    ms = await hub.tick_once()
    assert fut.calls["premium_index"] == 1  # no polling while frozen
    assert ms.freshness.ok is False and ms.freshness.reason == FEED_STALE_REASON
    assert ms.freshness.cex_age_ms == 1000  # ages keep growing against the frozen quotes
    hub.set_feed_frozen(False)
    state.stress_active = "SIMULATED: feed_stale"  # the controller's label alone also freezes the feed
    ms2 = await hub.tick_once()
    assert ms2.freshness.reason == FEED_STALE_REASON and fut.calls["premium_index"] == 1
    state.stress_active = None
    ms3 = await hub.tick_once()
    assert ms3.freshness.ok and fut.calls["premium_index"] == 2


async def test_run_loops_until_stop_and_survives_exceptions():
    hub, state, clock, fut, spot, dex = build_hub(DELTR_POLL_CEX_S=0.01)
    stop = asyncio.Event()
    ticks: list[int] = []

    async def cb(ms: MarketState) -> None:
        ticks.append(hub.ticks)
        if hub.ticks == 1:
            fut.fail_premium = RuntimeError("transient")
        if hub.ticks >= 3:
            stop.set()

    hub.on_tick(cb)
    await asyncio.wait_for(hub.run(stop), timeout=2.0)
    assert ticks == [1, 2, 3]


async def test_run_is_cancellation_safe():
    hub, *_ = build_hub(DELTR_POLL_CEX_S=5.0)
    task = asyncio.create_task(hub.run(asyncio.Event()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- codecs
def test_jsonl_round_trip_ignores_computed_fields():
    ms = MarketState(symbol=SYMBOL, dex=make_dex(T0), cex_perp_book=make_quote(T0, Venue.BINANCE_FUTURES, 686.2, DataSource.BINANCE_FUTURES_TESTNET),
                     cex_spot_ref=make_quote(T0, Venue.BINANCE_SPOT, 686.3, DataSource.BINANCE_SPOT_MIRROR), funding=make_funding(T0),
                     perp_ref_price=686.34, freshness=Freshness(cex_age_ms=0, dex_age_ms=0, spot_age_ms=0, ok=True), ts=T0)
    line = market_state_to_jsonl(ms)
    assert "\n" not in line and json.loads(line)["cex_perp_book"]["mid"] == pytest.approx(686.2)
    back = market_state_from_jsonl(line)
    assert back == ms


# --------------------------------------------------------------------------- ReplayHub (inline fixture)
def write_fixture(path: Path, rows: list[MarketState], schema: str = REPLAY_SCHEMA, header: bool = True) -> Path:
    lines = []
    if header:
        lines.append(json.dumps({"header": True, "schema": schema, "symbol": SYMBOL, "size_base": 4.85, "fee_tier": 100,
                                 "recorded_at": T0.isoformat(), "note": "inline test fixture"}))
    lines += [market_state_to_jsonl(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_rows(n: int = 3, start: datetime = T0, gap_s: float = 1.0) -> list[MarketState]:
    rows = []
    for i in range(n):
        ts = start + timedelta(seconds=gap_s * i)
        dex_ts = ts - timedelta(seconds=2)  # recorded DEX age 2 s
        fr = Freshness(cex_age_ms=0, dex_age_ms=2000, spot_age_ms=0, ok=True)
        rows.append(MarketState(
            symbol=SYMBOL, dex=make_dex(dex_ts, mid=686.0 + i), funding=make_funding(ts, mark=690.0 + i),
            cex_perp_book=make_quote(ts, Venue.BINANCE_FUTURES, 690.0 + i, DataSource.BINANCE_FUTURES_TESTNET),
            cex_spot_ref=make_quote(ts, Venue.BINANCE_SPOT, 690.0 + i, DataSource.BINANCE_SPOT_MIRROR),
            perp_ref_price=690.0 + i, freshness=fr, ts=ts, source=DataSource.BINANCE_FUTURES_TESTNET,
        ))
    return rows


@pytest.fixture
def inline_fixture(tmp_path: Path) -> Path:
    return write_fixture(tmp_path / "replay3.jsonl", make_rows(3))


async def test_replay_emits_rows_in_order_restamped_and_looping(inline_fixture: Path):
    clock = FakeClock(datetime(2026, 9, 7, 9, 0, 0, tzinfo=timezone.utc))  # days after the recording
    state = State(make_settings(), EventBus())
    hub = ReplayHub(state, str(inline_fixture), clock=clock)
    assert isinstance(hub, MarketHub) and state.replay is True and hub.symbol == SYMBOL and hub.quote_size == 4.85
    seen: list[float] = []

    async def cb(ms: MarketState) -> None:
        seen.append(ms.perp_ref_price)

    hub.on_tick(cb)
    out = []
    for _ in range(3):
        out.append(await hub.tick_once())
        clock.advance(1.0)
    assert [m.perp_ref_price for m in out] == [690.0, 691.0, 692.0] == seen
    for m in out:
        assert m.source == DataSource.REPLAY
        assert m.freshness.ok and m.freshness.reason is None
        assert m.freshness.dex_age_ms == 2000 and m.freshness.cex_age_ms == 0  # recorded ages, not wall-clock ages
        assert m.perp_ref_price == m.funding.mark_price
        assert m.funding.ts == m.ts and m.dex.ts == m.ts - timedelta(seconds=2)  # shifted onto the replay clock
        assert m.dex.source == DataSource.BSC_MAINNET_CHAIN  # nested provenance is preserved
    assert out[0].ts == datetime(2026, 9, 7, 9, 0, 0, tzinfo=timezone.utc)
    # next_funding_time is shifted by the same delta so settlement counting still works
    orig = hub.rows[0].funding.next_funding_time_ms
    assert out[0].funding.next_funding_time_ms - orig == int((out[0].ts - hub.rows[0].ts).total_seconds() * 1000)
    # wraps around
    m4 = await hub.tick_once()
    assert m4.perp_ref_price == 690.0 and hub.cycles == 1 and hub.ticks == 4
    assert state.market is m4 and hub.snapshot() is m4
    health = hub.health()
    assert [h.name for h in health] == list(VENUE_ORDER) and all(h.ok and h.source == DataSource.REPLAY for h in health)
    assert health[0].age_ms == 2000  # dex


async def test_replay_no_loop_stops_and_run_returns(inline_fixture: Path):
    state = State(make_settings(DELTR_POLL_CEX_S=0.01), EventBus())
    hub = ReplayHub(state, str(inline_fixture), speed=1000.0, loop=False)
    for _ in range(3):
        await hub.tick_once()
    assert hub.exhausted
    with pytest.raises(StopAsyncIteration):
        await hub.tick_once()
    hub2 = ReplayHub(state, str(inline_fixture), speed=1000.0, loop=False)
    n = 0

    async def cb(ms: MarketState) -> None:
        nonlocal n
        n += 1

    hub2.on_tick(cb)
    await asyncio.wait_for(hub2.run(asyncio.Event()), timeout=2.0)
    assert n == 3


async def test_replay_run_honours_stop_event(inline_fixture: Path):
    state = State(make_settings(), EventBus())
    hub = ReplayHub(state, str(inline_fixture), speed=100.0, loop=True)
    stop = asyncio.Event()

    async def cb(ms: MarketState) -> None:
        if hub.ticks >= 7:
            stop.set()

    hub.on_tick(cb)
    await asyncio.wait_for(hub.run(stop), timeout=2.0)
    assert hub.ticks == 7 and hub.cycles == 2


def test_replay_delay_uses_recorded_gap_capped_and_scaled(tmp_path: Path):
    rows = make_rows(3, gap_s=1.0)
    rows[2] = rows[2].model_copy(update={"ts": rows[1].ts + timedelta(seconds=60)})  # recorder hiccup
    p = write_fixture(tmp_path / "gap.jsonl", rows)
    state = State(make_settings(), EventBus())
    hub = ReplayHub(state, str(p), speed=2.0)
    assert hub._delay_after(0) == pytest.approx(0.5)      # 1 s / 2
    assert hub._delay_after(1) == pytest.approx(2.5)      # 60 s capped to 5 s / 2
    assert hub._delay_after(2) == pytest.approx(0.5)      # wrap uses the CEX poll interval
    with pytest.raises(ValueError):
        ReplayHub(state, str(p), speed=0)


async def test_replay_feed_frozen_and_restamp_off(inline_fixture: Path):
    state = State(make_settings(), EventBus())
    hub = ReplayHub(state, str(inline_fixture), restamp=False)
    ms = await hub.tick_once()
    assert ms.source == DataSource.REPLAY and ms.ts == T0 and ms.freshness.dex_age_ms == 2000 and ms.freshness.ok
    hub.set_feed_frozen(True)
    ms2 = await hub.tick_once()
    assert ms2.freshness.ok is False and ms2.freshness.reason == FEED_STALE_REASON


def test_replay_rejects_legacy_or_broken_fixtures(tmp_path: Path):
    state = State(make_settings(), EventBus())
    legacy = write_fixture(tmp_path / "legacy.jsonl", make_rows(1), schema="legacy")
    with pytest.raises(ValueError, match="schema"):
        ReplayHub(state, str(legacy))
    headerless = write_fixture(tmp_path / "noheader.jsonl", make_rows(1), header=False)
    with pytest.raises(ValueError, match="header"):
        ReplayHub(state, str(headerless))
    bad = tmp_path / "bad.jsonl"
    bad.write_text(write_fixture(tmp_path / "ok.jsonl", make_rows(1)).read_text() + '{"symbol": "BNBUSDT"}\n')
    with pytest.raises(ValueError, match="line 3"):
        ReplayHub(state, str(bad))
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(ValueError, match="empty"):
        ReplayHub(state, str(empty))
    with pytest.raises(FileNotFoundError):
        ReplayHub(state, str(tmp_path / "missing.jsonl"))
    only_header = tmp_path / "only_header.jsonl"
    only_header.write_text(json.dumps({"header": True, "schema": REPLAY_SCHEMA}) + "\n")
    with pytest.raises(ValueError, match="no rows"):
        ReplayHub(state, str(only_header))


# --------------------------------------------------------------------------- ReplayHub (shipped fixture)
def test_shipped_fixture_path_constant():
    assert REPLAY_FIXTURE_DEFAULT == "tests/fixtures/replay.jsonl"
    assert resolve_fixture_path() == REPO_ROOT / REPLAY_FIXTURE_DEFAULT
    assert resolve_fixture_path(REPLAY_FIXTURE_DEFAULT).exists()
    assert resolve_fixture_path("/abs/x.jsonl") == Path("/abs/x.jsonl")


async def test_shipped_fixture_parses_and_replays_as_replay_source():
    header, rows = load_replay_fixture(REPLAY_FIXTURE_DEFAULT)
    assert header["schema"] == REPLAY_SCHEMA and header["symbol"] == SYMBOL
    assert len(rows) >= 100  # ≥ 305 recorded live on 2026-09-02
    assert all(r.dex is not None and r.funding is not None and r.perp_ref_price == r.funding.mark_price for r in rows)
    assert rows == sorted(rows, key=lambda r: r.ts)  # deterministic, time-ordered
    state = State(make_settings(), EventBus())
    hub = ReplayHub(state, REPLAY_FIXTURE_DEFAULT, loop=False)
    emitted = []
    while True:
        try:
            emitted.append(await hub.tick_once())
        except StopAsyncIteration:
            break
    assert len(emitted) == len(rows)
    assert all(m.source == DataSource.REPLAY for m in emitted)
    assert all(m.freshness.ok for m in emitted), "recorded rows must never be stale merely because the recording is old"
    assert all(abs(m.ts - datetime.now(timezone.utc)).total_seconds() < 5 for m in emitted)
    last = emitted[-1]
    last.model_dump(mode="json")
    assert 600.0 < last.perp_ref_price < 800.0 and 600.0 < last.dex.exec_price_buy < 800.0
