"""deltr/engine.py — the Engine facade (section 4.14).

The ONLY object the API, the MCP server and ``main.py`` talk to.  ``build_engine``
wires every subsystem together:

* market data: ``MarketDataHub`` (REST polling: futures testnet + spot mirror +
  PancakeSwap V3 on BSC mainnet) or ``ReplayHub`` (``--replay`` JSONL fixture);
* ``ArbitrageScout`` (edge per tick) and ``Hedger`` (capital + leverage -> plan);
* ``BinanceRiskGate`` (owns equity / peak / halt / kill / position registry) fed by
  the ``Portfolio`` (mark-to-close, funding, stops);
* ``Executor`` — the single execution choke point — with ``PaperRouter`` (PAPER,
  keyless, simulated fills on live prices) or ``TestnetRouter`` (TESTNET, real
  USDS-M testnet perp leg, simulated DEX leg);
* ``ReceiptStore``, ``StressController``, ``ActivityLog``.

Mode is fixed at construction (PAPER | TESTNET).  There is no LIVE mode and no
way to change mode at runtime.  Nothing here writes to stdout: every diagnostic
goes through ``logging`` (stderr) because the stdio MCP transport owns stdout.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

import risk_gate
from agents.arbitrage_scout import ArbitrageScout
from agents.hedger import Hedger, SizingError
from deltr.bus import EventBus
from deltr.config import Mode, Settings
from deltr.edge import HedgeSizing, size_for_capital
from deltr.funding_history import (
    DEFAULT_HOLDS_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    FUTURES_MAINNET_REST,
    FundingAnalysis,
    FundingHistoryService,
    data_source_for,
)
from deltr.horizon import HorizonAnalysis, horizon_analysis, render_edge_report
from deltr.executor import Executor, LiveRouter, PaperRouter, PositionNotFound, TestnetRouter
from deltr.onchain_leg import WalletDexLeg
from deltr.market_data import MarketDataHub, MarketHub, ReplayHub, resolve_fixture_path
from deltr.mcp.activity import ActivityLog
from deltr.models import (
    ArbOpportunity,
    DexQuote,
    EdgeBreakdown,
    EdgeComponent,
    ExecutionReceipt,
    HedgePlan,
    MarketState,
    McpActivity,
    PromptResult,
    RiskDecisionRecord,
    Snapshot,
    StressResult,
    StressScenario,
    SymbolFilters,
    SystemStatus,
    TraceSource,
    TraceStep,
    TradeProposal,
    VenueHealth,
    utcnow,
)
from deltr.nl_intent import parse_intent
from deltr.payments import (
    PaymentError,
    X402Buyer,
    X402Seller,
    amount_from_atomic,
    artifact_from_edge_report,
    parse_payment_required,
)
from deltr.portfolio import Portfolio
from deltr.receipts import ReceiptStore
from deltr.state import State
from deltr.stress import StressController, StressRefused
from deltr.venues.agentic_wallet import SIGNIN_REMEDY, AgenticWalletClient, AgenticWalletError
from deltr.venues.binance_futures import FuturesClient
from deltr.venues.binance_spot import SpotMirror
from deltr.venues.pancake_constants import addresses_for_chain
from deltr.venues.pancakeswap_v3 import PancakeV3Client

log = logging.getLogger("deltr.engine")

PROBE_TIMEOUT_S = 3.0
FILTERS_TIMEOUT_S = 5.0
PREPARE_TIMEOUT_S = 10.0
GATE_BENCHMARK_ITERATIONS = 10_000
AUTO_COOLDOWN_S = 5.0
MIN_EDGE_OVERRIDE_RANGE = (-50.0, 50.0)
BOOTSTRAP_QUOTE_SIZE_BASE = 4.85  # the size the fixture and the worked example are quoted at ($5,000 @ 2x)

# Failure codes that PROVE the wallet was never asked to sign and nothing was broadcast, so the
# on-chain aggregate charge taken before the call can be given back.  Every other failure keeps
# its charge: a swap whose outcome is unknown is a swap that may already be on chain.
NOT_SUBMITTED_CODES = frozenset({
    "BAW_NOT_INSTALLED", "BAW_NOT_SIGNED_IN", "SWAP_PREVIEW_REJECTED",
    "X402_PREVIEW_FAILED", "X402_NO_OPTIONS", "X402_BAD_INDEX", "X402_NO_ACCEPTABLE_OPTION",
    "X402_NETWORK_NOT_ALLOWED", "X402_AMOUNT_OVER_CAP", "X402_AMOUNT_UNREADABLE", "X402_SIGN_FAILED",
})

# the share of DELTR_CAPITAL_USD one automatic / one-shot proposal deploys: both legs of a
# hedge need notional·(1 + 1/L) of cash, and the gate caps utilisation at 90 % of equity,
# so a proposal that "deploys all capital" is always vetoed (CAPITAL_CAPACITY).  Half the
# capital at 2x is the worked example: $10,000 -> $5,000 -> 4.85 BNB.
TRADE_CAPITAL_FRACTION = 0.5


class OnchainRefused(Exception):
    """A value-moving on-chain request was refused before anything reached the wallet.

    ``code`` is the gate's own code where the gate decided (``KILL_SWITCH``, ``HALTED_DRAWDOWN``,
    ``MAX_NOTIONAL``, ``AGGREGATE_NOTIONAL``) or a Deltr code (``CONFIRM_REQUIRED``,
    ``ONCHAIN_NOT_ARMED``, ``CHAIN_NOT_ALLOWED``).
    """

    def __init__(self, code: str, reason: str, *, decision: Any = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.decision = decision


class HaltNotClearable(Exception):
    """``reset_halt`` refused: positions are open or drawdown is still >= the stop-loss."""


class ReplayDexQuoter:
    """DEX re-quotes for a replayed feed: the last replayed quote rescaled to ``size_base``.

    Replay runs are deterministic and offline, so the hedger / PaperRouter must
    never reach BSC for a re-quote; exec prices are those of the replayed row.
    """

    def __init__(self, hub: MarketHub) -> None:
        self.hub = hub
        self.calls: int = 0

    async def dex_quote(self, size_base: float) -> DexQuote:
        ms = self.hub.snapshot()
        if ms is None or ms.dex is None:
            raise RuntimeError("replay: no DEX quote yet")
        self.calls += 1
        q = ms.dex
        return q.model_copy(
            update={
                "size_base": float(size_base),
                "amount_in_usdt": q.exec_price_buy * float(size_base),
                "amount_out_usdt": q.exec_price_sell * float(size_base),
            }
        )

    async def probe(self) -> VenueHealth:  # parity with PancakeV3Client (never called in replay)
        from deltr.models import DataSource

        return VenueHealth(name="pancakeswap_v3", ok=True, age_ms=0, source=DataSource.REPLAY, detail="replay")


def _apply_overrides(settings: Settings, replay_path: Optional[str], min_edge_override: Optional[float]) -> Settings:
    """A frozen Settings copy carrying the launch overrides (replay path, min-edge)."""
    update: dict[str, Any] = {}
    if replay_path:
        update["replay_path"] = str(replay_path)
    if min_edge_override is not None:
        x = float(min_edge_override)
        lo, hi = MIN_EDGE_OVERRIDE_RANGE
        if settings.mode == Mode.PAPER or settings.live_test_override:
            if not (lo <= x <= hi):
                raise ValueError(f"--min-edge-bps {x:g} outside the override range [{lo:g}, {hi:g}]")
        elif x < 0:
            raise ValueError(
                "--min-edge-bps must be >= 0 in TESTNET/LIVE (never a knowingly negative target). "
                "A tiny real-money test needs DELTR_LIVE_TEST_ACK and a per-trade cap <= $25."
            )
        update["min_edge_bps"] = x
    return settings.model_copy(update=update) if update else settings


class Engine:
    """Engine facade (section 4.14).  Construct with :func:`build_engine`."""

    def __init__(
        self,
        settings: Settings,
        *,
        replay_path: Optional[str] = None,
        min_edge_override: Optional[float] = None,
        http: Optional[httpx.AsyncClient] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        # ONE source of truth for "replay": the CLI flag or DELTR_REPLAY_PATH (settings) — both pick the ReplayHub
        replay_path = replay_path or settings.replay_path or None
        if replay_path and settings.real_orders:
            raise ValueError(
                f"--replay is refused in {settings.mode.value.upper()}: real orders must never be priced off a recording"
            )
        if settings.auto_execute and settings.real_orders:
            raise ValueError(
                f"--auto is PAPER-only: {settings.mode.value.upper()} executions require an explicit confirm"
            )
        self.settings: Settings = _apply_overrides(settings, replay_path, min_edge_override)
        self.replay_path: Optional[str] = str(replay_path) if replay_path else None
        self.min_edge_override: Optional[float] = None if min_edge_override is None else float(min_edge_override)
        self.clock: Callable[[], datetime] = clock or utcnow
        self.trade_capital_usd: float = self.settings.capital_usd * TRADE_CAPITAL_FRACTION

        s = self.settings
        self.bus = EventBus()
        self.state = State(s, self.bus)
        self.state.replay = bool(self.replay_path)
        self.activity = ActivityLog(self.state)
        self.filters = SymbolFilters(symbol=s.symbol)
        # persistence: replay runs never share a book with live runs of the same mode
        self.state_root: Path = Path(s.state_dir) / s.mode.value / ("replay" if self.replay_path else "")
        self.gate_state_path = str(self.state_root / "gate.json")
        self.portfolio_state_path = str(self.state_root / "portfolio.json")
        self.receipts_path = str(self.state_root / "receipts.jsonl")

        # ---- venue clients (no I/O at construction) ----------------------------------
        self._owns_http = http is None
        self.http: httpx.AsyncClient = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        keys = (s.binance_api_key, s.binance_secret_key) if s.real_orders else (None, None)
        # LIVE is the only path that may sign against mainnet, and FuturesClient still refuses
        # any mainnet host but fapi.binance.com even then.  PAPER never receives keys at all.
        self.futures = FuturesClient(
            self.http, s.hosts["futures_rest"], api_key=keys[0], secret_key=keys[1],
            allow_mainnet_orders=(s.mode == Mode.LIVE),
        )
        # Market data is REAL MAINNET in every mode.  This second client is KEYLESS by
        # construction (no credentials are ever passed to it) so it can only reach public
        # endpoints; order routing keeps using ``self.futures`` and is not touched here.
        data_url = str(s.hosts.get("futures_data_rest") or FUTURES_MAINNET_REST)
        self.futures_data_url = data_url
        # In LIVE both URLs are fapi.binance.com, but market data still gets its OWN keyless
        # client: a credentialed client is never used to poll public endpoints in any mode.
        self.futures_data = (
            self.futures
            if (data_url.rstrip("/") == str(s.hosts["futures_rest"]).rstrip("/") and not self.futures.has_credentials)
            else FuturesClient(self.http, data_url)
        )
        self.spot = SpotMirror(self.http, s.hosts["spot_rest"])
        self.funding_history_service = FundingHistoryService(self.http, base_url=FUTURES_MAINNET_REST)
        self.dex = PancakeV3Client(
            self.http, tuple(s.rpc_urls), chain=addresses_for_chain(s.bsc_chain_id),
            fee_tier=s.dex_fee_tier, cache_ttl_s=s.dex_quote_cache_s,
        )

        # ---- on-chain leg: the Binance Agentic Wallet CLI ------------------------------
        # Deltr holds no key and signs nothing.  The CLI custodies the key, applies Binance's own
        # daily limits and does the signing; nothing here starts a process at construction time.
        self.wallet = AgenticWalletClient(
            binary=s.baw_bin,
            chain_id=s.wallet_chain_id,
            timeout_s=s.baw_timeout_s,
            swap_timeout_s=s.baw_swap_timeout_s,
            confirm_timeout_s=s.baw_confirm_timeout_s,
            forbidden_values=tuple(v for v in (s.binance_api_key, s.binance_secret_key, s.binance_mcp_token) if v),
        )
        self.x402_buyer = X402Buyer(self.wallet, max_amount_units=s.x402_max_payment_units)
        self.x402_seller = X402Seller(pay_to=s.x402_pay_to, price_units=s.x402_price_units)
        self._onchain_notional_usd: float = 0.0

        # ---- market data ---------------------------------------------------------------
        self.hub: MarketHub
        if self.replay_path:
            self.hub = ReplayHub(self.state, str(resolve_fixture_path(self.replay_path)), speed=s.replay_speed, loop=True, clock=clock)
            self.dex_quoter: Any = ReplayDexQuoter(self.hub)
        else:
            self.hub = MarketDataHub(self.state, self.futures, self.spot, self.dex, s, self.filters,
                                     BOOTSTRAP_QUOTE_SIZE_BASE, clock=clock, futures_data=self.futures_data)
            self.dex_quoter = self.dex

        # ---- gate, book, agents, executor ---------------------------------------------
        self.gate = risk_gate.BinanceRiskGate(
            capital_usd=s.capital_usd, limits=s.risk_limits(), mode=s.mode.value, state_path=self.gate_state_path
        )
        self.scout = ArbitrageScout(self.state, s, self.filters)
        self.hedger = Hedger(self.state, s, self.filters, dex_quote_at=self._dex_quote_at, clock=self.clock)
        self.portfolio = Portfolio(self.state, self.gate, s, self.portfolio_state_path)
        self.portfolio.min_qty = self.filters.min_qty
        self.receipts = ReceiptStore(self.state, self.receipts_path)
        self.onchain_leg: Optional[WalletDexLeg] = None
        if s.mode == Mode.LIVE:
            # The DEX leg is REAL and goes through the Binance Agentic Wallet.  Price discovery
            # stays on the read-only PancakeSwap quoter; the wallet only executes.
            self.onchain_leg = WalletDexLeg(
                self.wallet, s, quoter=self.dex_quoter, guard=self._leg_guard, chain_id=s.wallet_chain_id
            )
            self.router: Any = LiveRouter(
                self.futures, self.onchain_leg, s, self.filters, book_ticker=self.futures_data.book_ticker
            )
        elif s.mode == Mode.TESTNET:
            self.router = TestnetRouter(self.futures, self.dex_quoter, s, self.filters)
        else:
            self.router = PaperRouter(self.dex_quoter, s, self.filters)
        self.executor = Executor(self.state, self.gate, self.portfolio, self.router, self.hub, self.hedger, s, self.receipts, self.filters)
        self.stress_controller = StressController(self.state, self.portfolio, self.executor, self.hub, s)

        # ---- lifecycle -------------------------------------------------------------------
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False
        self._stopped = False
        self._auto_last_mono: float = -1e9
        self._auto_lock = asyncio.Lock()
        self.last_auto_receipt: Optional[ExecutionReceipt] = None
        self.account_prep: Any = None
        self.start_errors: list[str] = []
        self.live_facts: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self, *, loops: bool = True, benchmark_iterations: int = GATE_BENCHMARK_ITERATIONS) -> None:
        """Probe venues, load filters and persisted state, prepare the account (TESTNET),
        take the first tick, benchmark the gate and (``loops``) start the tick loop + stop monitor."""
        s = self.settings
        if self._started:
            return
        self._started = True
        self.state.emit("log", f"engine starting: mode={s.mode.value} symbol={s.symbol} replay={bool(self.replay_path)}")

        # 0. LIVE preflight — BEFORE the first tick and before anything can place an order.
        #    Raises with the exact missing requirement; nothing below runs until it passes.
        if s.mode == Mode.LIVE:
            await self.live_preflight()

        # 1. venue probes (3 s each; failures recorded; fatal only for TESTNET futures)
        if self.replay_path:
            for h in self.hub.health():
                self.state.record_venue(h)
        else:
            order_venue = "binance_futures_mainnet" if s.mode == Mode.LIVE else "binance_futures_testnet"
            futures_ok = await self._probe(order_venue, self.futures)
            if self.futures_data is not self.futures:
                # the mainnet market-data feed is a venue of its own in the health table
                await self._probe(getattr(self.hub, "venue_futures", "binance_futures_mainnet"), self.futures_data)
            await self._probe("binance_spot_mirror", self.spot)
            await self._probe("pancakeswap_v3", self.dex)
            if s.real_orders and not futures_ok:
                raise RuntimeError(
                    f"Binance Futures order venue {s.hosts['futures_order_rest']} unreachable — refusing to start in "
                    f"{s.mode.value.upper()}. If this is a DNS failure, check the resolver: "
                    "`dig +short fapi.binance.com` against `dig +short @1.1.1.1 fapi.binance.com`. "
                    "Deltr does not substitute another venue or simulated data."
                )

            # 2. exchange filters (never hard-coded when the venue answers; defaults offline)
            if futures_ok:
                try:
                    self.filters = await asyncio.wait_for(self.futures.exchange_filters(s.symbol), FILTERS_TIMEOUT_S)
                    self._apply_filters(self.filters)
                except Exception as exc:  # noqa: BLE001 - keep defaults, say so
                    self.start_errors.append(f"exchange_filters: {exc}")
                    log.warning("exchange filters unavailable (%s); using defaults %s", exc, self.filters.model_dump())

        # 3. persisted gate + book (per mode)
        self.gate.load()
        restored = self.portfolio.restore()
        if restored:
            self.state.emit("log", f"restored {len(self.portfolio.positions('open'))} open position(s) from {self.portfolio_state_path}")

        # 4. account preparation — the REAL account leverage must match what the gate approves
        if s.real_orders:
            self.account_prep = await asyncio.wait_for(self.router.prepare(s.symbol, int(s.default_leverage)), PREPARE_TIMEOUT_S)
            self.state.emit("log", f"{s.mode.value} account prepared: {self.account_prep}")
            self._verify_account_leverage(self.account_prep)

        # 5. first tick
        self.hub.on_tick(self._on_tick)
        try:
            await self.hub.tick_once()
        except StopAsyncIteration as exc:
            raise RuntimeError(f"replay fixture produced no rows: {exc}") from exc

        # 6. min-edge override (after the first tick so the TESTNET floor is measured)
        if self.min_edge_override is not None:
            eff, floor = self.set_min_edge(self.min_edge_override)
            self.state.emit("log", f"MIN-EDGE OVERRIDE: {self.min_edge_override:g} bps -> effective {eff:g} bps (floor {floor:g})", level="warn")

        # 7. gate micro-benchmark: quote the measured median, never the target
        median_us, p99_us, amortised_us = risk_gate.benchmark(int(benchmark_iterations), capital_usd=s.capital_usd)
        self.state.gate_median_us = round(median_us, 3)
        self.state.emit("log", f"risk gate benchmark: median {median_us:.2f} µs, p99 {p99_us:.2f} µs, amortised {amortised_us:.2f} µs ({benchmark_iterations:,} calls)")

        # 8. loops
        if loops:
            self._tasks = [
                asyncio.create_task(self.hub.run(self._stop), name="deltr-tick-loop"),
                asyncio.create_task(self.executor.run_stop_monitor(self._stop), name="deltr-stop-monitor"),
            ]

    async def stop(self) -> None:
        """Persist gate / book / receipts and cancel the background tasks (idempotent)."""
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        try:
            self.gate.save()
            self.portfolio.persist()
        except Exception as exc:  # noqa: BLE001
            log.error("persist on stop failed: %s", exc)
        if self._owns_http:
            try:
                await self.http.aclose()
            except Exception:  # noqa: BLE001
                pass
        self.state.emit("log", "engine stopped")

    def _verify_account_leverage(self, prep: Any) -> None:
        """Refuse to start when the testnet account's effective leverage is unknown, above the gate's
        MAX_LEVERAGE or different from the configured default: real perp orders would otherwise run at
        a leverage the gate never approved (``plan.leverage`` is what the gate checks)."""
        lev = getattr(prep, "leverage", None)
        max_lev = float(self.gate.limits.max_leverage)
        want = int(self.settings.default_leverage)
        ok_num = isinstance(lev, (int, float)) and not isinstance(lev, bool) and math.isfinite(float(lev)) and float(lev) > 0
        if not ok_num:
            raise RuntimeError(f"{self.settings.mode.value} account leverage unknown after prepare_account — refusing to start")
        if float(lev) > max_lev or int(lev) != want:
            raise RuntimeError(
                f"{self.settings.mode.value} account leverage is {float(lev):g}x but Deltr is configured for {want}x (gate max {max_lev:g}x); "
                "flatten the account or set DELTR_DEFAULT_LEVERAGE to the account's leverage — refusing to start"
            )
        self.state.emit("log", f"{self.settings.mode.value} account leverage verified: {int(lev)}x (max {max_lev:g}x)")

    async def _probe(self, name: str, client: Any) -> bool:
        from deltr.models import DataSource

        # provenance derived from the host that will answer, with the slot name as the fallback
        src = data_source_for(str(getattr(client, "base_url", "") or "")) or {
            "binance_futures_testnet": DataSource.BINANCE_FUTURES_TESTNET,
            "binance_futures_mainnet": DataSource.BINANCE_FUTURES_MAINNET,
            "binance_spot_mirror": DataSource.BINANCE_SPOT_MIRROR,
            "pancakeswap_v3": DataSource.BSC_MAINNET_CHAIN,
        }[name]
        try:
            h: VenueHealth = await asyncio.wait_for(client.probe(), PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            h = VenueHealth(name=name, ok=False, age_ms=0, source=src, detail=f"probe timeout after {PROBE_TIMEOUT_S:g} s")
        except Exception as exc:  # noqa: BLE001
            h = VenueHealth(name=name, ok=False, age_ms=0, source=src, detail=f"probe failed: {type(exc).__name__}: {exc}"[:200])
        if h.name != name or h.source != src:
            # a client may hardcode its own label; the engine knows which slot and which host this is
            h = h.model_copy(update={"name": name, "source": src})
        self.state.record_venue(h)
        if not h.ok:
            self.start_errors.append(f"{name}: {h.detail}")
            detail = h.detail
            if any(m in detail.lower() for m in ("nodename nor servname", "getaddrinfo", "name or service not known", "name resolution")):
                host = str(getattr(client, "base_url", "") or "").replace("https://", "").replace("http://", "").strip("/") or "the venue host"
                detail = f"DNS: {host} does not resolve via this machine's resolver; set DNS to 1.1.1.1 (works) - no fallback venue"
                h = h.model_copy(update={"detail": detail[:200]})
                self.state.record_venue(h)
            self.state.emit("log", f"venue {name} unavailable: {detail}", level="warn", data={"venue": name})
        else:
            log.info("venue %s ok: %s", name, h.detail)
        return h.ok

    def _apply_filters(self, filters: SymbolFilters) -> None:
        self.filters = filters
        for obj in (self.scout, self.hedger, self.executor, self.router, self.hub):
            if hasattr(obj, "filters"):
                try:
                    obj.filters = filters
                except Exception:  # noqa: BLE001
                    pass
        paper = getattr(self.router, "_paper", None)
        if paper is not None:
            paper.filters = filters
        self.portfolio.min_qty = filters.min_qty

    async def _dex_quote_at(self, qty: float) -> DexQuote:
        return await self.dex_quoter.dex_quote(float(qty))

    # ------------------------------------------------------------------ tick
    async def _on_tick(self, ms: MarketState) -> None:
        opp: Optional[ArbOpportunity] = None
        try:
            opp = await self.scout.on_tick(ms)
        except ValueError as exc:
            log.warning("scan skipped: %s", exc)
        self.portfolio.mark(ms)
        for h in self.hub.health():
            self.state.record_venue(h)
        self.state.min_edge_floor_bps = self.scout.min_edge_floor()
        self._retarget_quote_size(ms)
        if opp is not None and opp.is_actionable and self.settings.auto_execute:
            await self._auto_trade(opp)

    def _retarget_quote_size(self, ms: MarketState) -> None:
        """Quote the DEX at the size the configured trade capital would deploy (once known)."""
        if ms.dex is None or self.replay_path:
            return
        try:
            sizing = size_for_capital(
                self.trade_capital_usd, self.settings.default_leverage, ms.dex.exec_price_buy, self.filters,
                self.settings.max_notional_usd, self.settings.max_dex_impact_bps,
            )
        except ValueError:
            return
        size = float(sizing.qty)
        current = float(getattr(self.hub, "quote_size", 0.0) or 0.0)
        if size > 0 and abs(size - current) >= self.filters.step_size:
            setter = getattr(self.hub, "set_quote_size", None)
            if callable(setter):
                setter(size)

    async def _auto_trade(self, opp: ArbOpportunity) -> None:
        """``--auto``: propose + execute an actionable opportunity (PAPER only; max one open
        position per symbol; 5 s cooldown; serialised)."""
        if self._auto_lock.locked() or self.executor.in_flight:
            return
        if any(p.symbol == opp.symbol for p in self.portfolio.positions("open")):
            return
        now = time.monotonic()
        if now - self._auto_last_mono < AUTO_COOLDOWN_S:
            return
        self._auto_last_mono = now
        async with self._auto_lock:
            try:
                plan, _proposal, precheck = await self.propose(
                    self.trade_capital_usd, self.settings.default_leverage, opp.symbol, TraceSource.AUTO, client="auto"
                )
            except (SizingError, ValueError) as exc:
                self.state.emit("plan", f"auto: cannot size a hedge: {exc}", level="warn")
                return
            if not precheck.approved:
                self.state.emit("gate", f"auto: precheck VETO {precheck.code}: {precheck.reason}", level="warn", data={"plan_id": plan.id})
                return
            receipt = await self.execute(plan.id, True, TraceSource.AUTO, client="auto")
            self.last_auto_receipt = receipt
            self.state.emit("log", f"auto: executed plan {plan.id} -> {receipt.status} ({receipt.decision.code})",
                            level="info" if receipt.status == "filled" else "warn", data={"receipt_id": receipt.id})

    # ------------------------------------------------------------------ read
    def snapshot(self) -> Snapshot:
        return self.state.snapshot(self.gate.snapshot())

    def status(self) -> SystemStatus:
        """System status, including whether REAL FUNDS are armed.

        ``real_funds_armed`` is True only in LIVE and only once ``live_preflight()`` has actually
        passed, so a status read before the preflight never claims the agent is armed.  The wallet
        address is the wallet's PUBLIC address; no secret of any kind is placed on this object.
        """
        st = self.state.system_status(self.gate.snapshot())
        facts = self.live_facts
        if self.settings.mode == Mode.LIVE and facts:
            addrs = facts.get("wallet_addresses") or {}
            addr = next((str(v) for v in addrs.values() if v), None) if isinstance(addrs, dict) else None
            st = st.model_copy(update={"real_funds_armed": True, "wallet_address": addr})
        return st

    def market(self) -> tuple[Optional[MarketState], Optional[EdgeBreakdown]]:
        return self.state.market, self.state.edge

    def scan(self, notional_usd: Optional[float] = None, horizon_h: Optional[float] = None, min_edge_bps: Optional[float] = None) -> ArbOpportunity:
        return self.scout.scan(notional_usd=notional_usd, horizon_h=horizon_h, min_edge_bps=min_edge_bps)

    def explain_edge(
        self, capital_usd: Optional[float], leverage: Optional[float], horizon_h: Optional[float],
        assumed_funding_rate: Optional[float] = None,
    ) -> tuple[EdgeBreakdown, HedgeSizing, list[EdgeComponent], HorizonAnalysis]:
        """Edge breakdown, sizing, waterfall rows and the horizon view (breakeven + curve).

        The fourth element answers the question the ``--min-edge-bps`` override dodges:
        at this funding rate, how long must the position be held before the carry pays
        for the round trip?  When the measured testnet rate can never repay it, the
        analysis adds a clearly labelled mainnet-typical ASSUMPTION beside the
        measured rate (never instead of it).
        """
        cap = float(capital_usd) if capital_usd is not None else self.trade_capital_usd
        lev = float(leverage) if leverage is not None else float(self.settings.default_leverage)
        hz = float(horizon_h) if horizon_h is not None else float(self.settings.funding_horizon_hours)
        if cap <= 0 or lev <= 0 or hz <= 0:
            raise ValueError("capital_usd, leverage and horizon_h must be > 0")
        edge, sizing = self.scout.explain(cap, lev, hz)
        return edge, sizing, edge.components(), self.edge_horizon(edge, assumed_funding_rate=assumed_funding_rate)

    async def funding_history(
        self,
        symbol: Optional[str] = None,
        *,
        lookback_days: Optional[float] = None,
        holds_days: Optional[list[float]] = None,
        refresh: bool = False,
    ) -> FundingAnalysis:
        """Real mainnet funding history for ``symbol``, analysed under both cost models.

        Read-only, keyless and cached: it hits public ``fapi.binance.com`` market data
        (never an order endpoint, in any mode) and reports annualised carry, the share
        of settlements where a short is paid, and the share of rolling windows whose
        carry clears the round trip with the perp leg TAKEN and with it POSTED.  The
        posted figures are a model, not a realised result.
        """
        sym = str(symbol or self.settings.symbol).upper().strip()
        if not sym.isalnum() or not (5 <= len(sym) <= 20):
            raise ValueError(f"symbol {symbol!r} is not a valid perpetual symbol, e.g. BNBUSDT")
        look = float(lookback_days) if lookback_days is not None else DEFAULT_LOOKBACK_DAYS
        if not (1.0 <= look <= 2000.0):
            raise ValueError("lookback_days must be between 1 and 2000")
        holds = [float(h) for h in (holds_days or DEFAULT_HOLDS_DAYS)]
        if any(h <= 0 or h > 365 for h in holds):
            raise ValueError("every hold in holds_days must be > 0 and <= 365 days")
        return await self.funding_history_service.analyse(
            sym, lookback_days=look, holds_days=holds, refresh=bool(refresh)
        )

    def edge_horizon(self, edge: EdgeBreakdown, assumed_funding_rate: Optional[float] = None) -> HorizonAnalysis:
        """Breakeven holding period + edge-versus-horizon curve for an existing breakdown."""
        ms = self.state.market
        funding = getattr(ms, "funding", None) if ms is not None else None
        source = getattr(getattr(funding, "source", None), "value", None)
        return horizon_analysis(
            edge,
            interval_h=float(getattr(funding, "interval_h", 0) or 0) or None,
            symbol=getattr(ms, "symbol", None) if ms is not None else self.settings.symbol,
            measured_source=source,
            assumed_rate=assumed_funding_rate,
        )

    def history(self, n: int = 600):
        return self.scout.history(n)

    # ------------------------------------------------------------------ two-phase trading
    def _check_symbol(self, symbol: Optional[str]) -> str:
        sym = (symbol or self.settings.symbol).upper()
        if sym not in self.settings.symbol_list:
            raise ValueError(f"symbol {sym!r} is not configured; allowed: {self.settings.symbol_list}")
        return sym

    async def propose(
        self,
        capital_usd: float,
        leverage: Optional[float],
        symbol: Optional[str],
        source: TraceSource,
        client: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> tuple[HedgePlan, TradeProposal, RiskDecisionRecord]:
        """Size + store a plan and pre-check it against the gate.  NOTHING executes here."""
        sym = self._check_symbol(symbol)
        if capital_usd is None or float(capital_usd) <= 0:
            raise ValueError("capital_usd must be > 0")
        if leverage is not None and float(leverage) <= 0:
            raise ValueError("leverage must be > 0")
        plan = await self.hedger.propose(float(capital_usd), leverage, sym, source, client=client, prompt=prompt)
        ms = self.hub.snapshot()
        proposal = self.executor.build_proposal(plan, ms, None)
        precheck = self.executor.precheck(plan)
        self.state.emit(
            "gate",
            f"precheck {'APPROVED' if precheck.approved else 'VETO ' + precheck.code} for plan {plan.id}: {precheck.reason}",
            level="info" if precheck.approved else "warn",
            data={"plan_id": plan.id, "code": precheck.code, "approved": precheck.approved, "latency_us": precheck.latency_us, "source": source.value, "client": client},
        )
        return plan, proposal, precheck

    def evaluate_risk(self, capital_usd: float, leverage: float, symbol: Optional[str]) -> RiskDecisionRecord:
        """Dry-run probe of the gate (leverage above 3x is vetoed, never clamped)."""
        sym = (symbol or self.settings.symbol).upper()
        return self.executor.dry_run(float(capital_usd), leverage, sym)

    async def execute(self, plan_id: str, confirm: bool, source: TraceSource, client: Optional[str] = None) -> ExecutionReceipt:
        return await self.executor.execute(plan_id, bool(confirm), source, client)

    async def unwind(
        self, position_id: str, reason: str, confirm: bool, source: TraceSource, client: Optional[str] = None
    ) -> list[ExecutionReceipt]:
        if position_id == "all":
            return await self.executor.unwind_all(reason or "user", source, client, bool(confirm))
        return [await self.executor.unwind(position_id, reason or "user", source, client, bool(confirm))]

    # ------------------------------------------------------------------ natural language (propose-only)
    async def prompt(self, text: str, source: TraceSource, client: Optional[str] = None) -> PromptResult:
        intent = parse_intent(text, default_symbol=self.settings.symbol, source=source)
        steps: list[TraceStep] = [
            TraceStep(step="intent", status="ok" if intent.action != "unknown" else "error",
                      summary=f"intent {intent.action} (confidence {intent.confidence:.2f})",
                      data={k: v for k, v in intent.model_dump(mode="json").items() if v not in (None, "")})
        ]
        opp = self.state.opportunity
        plan = proposal = precheck = None
        message: str
        act = intent.action
        if act in ("rebalance", "hedge"):
            capital = intent.capital_usd if intent.capital_usd else self.trade_capital_usd
            try:
                plan, proposal, precheck = await self.propose(capital, intent.leverage, intent.symbol, source, client=client, prompt=text)
            except (SizingError, ValueError) as exc:
                steps.append(TraceStep(step="error", status="error", summary=str(exc)))
                message = f"Could not build a hedge for {text!r}: {exc}"
            else:
                opp = self.state.opportunity
                if opp is not None:
                    steps.append(TraceStep(step="scan", status="ok" if opp.is_actionable else "skipped",
                                           summary=f"net edge {opp.edge.net_edge_bps:+.2f} bps ({opp.reason})",
                                           data={"opportunity_id": opp.id, "net_edge_bps": opp.edge.net_edge_bps, "reason": opp.reason}))
                steps.append(TraceStep(step="plan", status="ok",
                                       summary=f"plan {plan.id}: {plan.qty:g} {plan.symbol} notional ${plan.notional_usd:,.2f} at {plan.leverage:g}x (cash ${plan.cash_required_usd:,.2f})",
                                       data={"plan_id": plan.id, "qty": plan.qty, "notional_usd": plan.notional_usd, "leverage": plan.leverage, "expires_at": plan.expires_at.isoformat()}))
                steps.append(TraceStep(step="gate", status="ok" if precheck.approved else "veto", summary=precheck.reason,
                                       data={"code": precheck.code, "latency_us": precheck.latency_us, "dry_run": True}))
                verdict = "APPROVED" if precheck.approved else f"VETO {precheck.code}"
                message = (
                    f"Proposed plan {plan.id}: buy {plan.qty:g} {plan.symbol[:-4]} on PancakeSwap V3 and short {plan.qty:g} on "
                    f"Binance Futures ({self.settings.mode.value}) at {plan.leverage:g}x — notional ${plan.notional_usd:,.2f}, "
                    f"expected edge {plan.expected_edge_bps:+.2f} bps. Pre-check {verdict}: {precheck.reason} "
                    f"Nothing executed — execute with plan_id {plan.id} (expires {plan.expires_at.isoformat()})."
                )
                if not intent.capital_usd:
                    message += f" No amount given: sized {TRADE_CAPITAL_FRACTION:.0%} of configured capital (${capital:,.2f})."
        elif act == "explain":
            try:
                edge, sizing, _, _hz = self.explain_edge(intent.capital_usd, intent.leverage, intent.magnitude)
            except ValueError as exc:
                message = f"Cannot explain the edge yet: {exc}"
                steps.append(TraceStep(step="error", status="error", summary=str(exc)))
            else:
                steps.append(TraceStep(step="scan", status="ok", summary=f"net edge {edge.net_edge_bps:+.2f} bps over {edge.horizon_h:g} h",
                                       data={"net_edge_bps": edge.net_edge_bps, "basis_entry_bps": edge.basis_entry_bps,
                                             "roundtrip_cost_bps": edge.roundtrip_cost_bps, "funding_bps_horizon": edge.funding_bps_horizon,
                                             "qty": float(sizing.qty), "notional_usd": sizing.notional_usd}))
                message = (
                    f"Edge at {float(sizing.qty):g} {self.settings.symbol[:-4]} (${sizing.notional_usd:,.2f}) over {edge.horizon_h:g} h: "
                    f"basis {edge.basis_entry_bps:+.2f} + funding {edge.funding_bps_horizon:+.2f} ({edge.settlements} settlements) "
                    f"− round trip {edge.roundtrip_cost_bps:.2f} = net {edge.net_edge_bps:+.2f} bps (${edge.expected_edge_usd:+.2f}); "
                    f"allocated risk ${edge.allocated_risk_usd:.2f}."
                )
        elif act == "scan":
            try:
                opp = self.scan()
            except ValueError as exc:
                message = f"Cannot scan yet: {exc}"
                steps.append(TraceStep(step="error", status="error", summary=str(exc)))
            else:
                steps.append(TraceStep(step="scan", status="ok" if opp.is_actionable else "skipped",
                                       summary=f"net edge {opp.edge.net_edge_bps:+.2f} bps ({opp.reason})", data={"opportunity_id": opp.id}))
                message = (
                    f"{opp.symbol}: DEX exec {opp.dex_price:.3f} vs perp mark {opp.perp_price:.3f}; net edge "
                    f"{opp.edge.net_edge_bps:+.2f} bps over {opp.horizon_h:g} h against a {opp.min_edge_bps_used:g} bps minimum — "
                    f"{'ACTIONABLE' if opp.is_actionable else 'not actionable: ' + opp.reason}."
                )
        elif act == "status":
            st = self.status()
            message = (
                f"{st.mode.value.upper()} {st.symbol}: equity ${st.equity_usd:,.2f}, drawdown {st.drawdown_pct:.2f}% ({st.dd_state}), "
                f"{st.open_positions} open position(s), kill switch {'ON' if st.kill_switch else 'off'}, "
                f"{'HALTED' if st.halted else 'not halted'}, min edge {st.min_edge_bps:g} bps, gate median {st.gate_median_us:g} µs."
            )
        elif act == "unwind":
            target = intent.position_id or "all"
            message = (
                f"Unwind of {target} understood, but prompts never execute: call deltr_unwind(position_id=\"{target}\""
                f"{', confirm=true' if self.settings.real_orders else ''}) or POST /api/hedge/unwind."
            )
        elif act == "kill":
            message = ("Kill switch " + ("ON" if (intent.magnitude or 0) >= 1 else "OFF") +
                       " understood, but prompts never change state: call deltr_kill_switch / POST /api/kill.")
        elif act == "reset_halt":
            message = "Halt reset understood, but prompts never change state: call deltr_reset_halt(reason) / POST /api/risk/reset_halt."
        elif act == "stress":
            kind = intent.stress_kind.value if intent.stress_kind else "basis_shock"
            message = (f"Stress scenario {kind} (magnitude {intent.magnitude if intent.magnitude is not None else 0:g}) understood, "
                       "but prompts never mutate the book: call deltr_stress / POST /api/stress (badged SIMULATED).")
        elif act == "set_min_edge":
            message = (f"Min-edge {intent.min_edge_bps if intent.min_edge_bps is not None else '?'} bps understood, but prompts never change "
                       "settings: call deltr_set_min_edge / POST /api/scout/min_edge.")
        else:
            message = (f"Could not parse {text!r}. Try: \"Rebalance $5,000 USDC into delta-neutral BNB arbitrage\", "
                       "\"What is the edge right now?\", \"Scan for opportunities\", \"Status\".")
        if intent.stablecoin_note:
            message += f" Note: {intent.stablecoin_note}."
        res = PromptResult(intent=intent, opportunity=opp, plan=plan, plan_id=plan.id if plan is not None else None,
                           proposal=proposal, precheck=precheck, message=message, steps=steps)
        self.state.record_prompt(res)
        self.state.emit("plan" if plan is not None else "log", f"prompt ({source.value}): {intent.action} -> {'plan ' + plan.id if plan is not None else 'no plan'}",
                        data={"action": intent.action, "plan_id": res.plan_id, "client": client})
        return res

    # ------------------------------------------------------------------ on-chain leg (agentic wallet)
    def _wallet_decision(self, code: str, reason: str, approved: bool = False) -> RiskDecisionRecord:
        """Record a gate-shaped decision for a value-moving on-chain request.

        The 19-check paired evaluation is not applied here on purpose: an unpaired swap is not a
        delta-neutral trade, so ``gate.evaluate`` would always answer ``NOT_DELTA_NEUTRAL``.  The
        gate's *state* still governs the request (kill switch, drawdown halt) and its per-trade
        notional ceiling still bounds it; opening a position remains propose -> execute only.
        """
        g = self.gate.snapshot()
        rec = RiskDecisionRecord(
            plan_id=None, approved=approved, code=code, reason=reason, latency_ns=0,
            dd_state=g.get("state", "NORMAL"), drawdown_pct=float(g.get("drawdown_pct", 0.0)),
            halted=bool(g.get("halted", False)), kill_switch=bool(g.get("kill_switch", False)),
            mode=self.settings.mode, dry_run=False,
        )
        self.state.record_decision(rec)
        return rec

    def _onchain_guard(self, *, notional_usd: float, chain_id: int, confirm: bool,
                       reducing: bool = False) -> RiskDecisionRecord:
        """Deterministic pre-flight for anything that moves on-chain value.  Order is fixed.

        ``reducing=True`` marks a swap that CLOSES an on-chain exposure (a reverse-on-failure,
        a residual reversal, the DEX leg of an unwind).  Those skip the kill switch, the
        drawdown halt and the notional caps, for the same reason the frozen risk gate lets a
        verified reduce-only unwind through its own kill switch: every one of those checks
        exists to stop Deltr TAKING ON risk, and applying them to the swap that removes risk
        turns a stop-loss into a naked leg.  Arming and the chain allow-list still apply, and
        the wallet's own limits apply on top of everything.
        """
        s = self.settings
        g = self.gate.snapshot()
        if g.get("kill_switch") and not reducing:
            raise OnchainRefused(risk_gate.KILL_SWITCH, "REFUSED: kill switch engaged; the on-chain leg is closed.",
                                 decision=self._wallet_decision(risk_gate.KILL_SWITCH, "REFUSED: kill switch engaged; the on-chain leg is closed."))
        if g.get("halted") and not reducing:
            reason = f"REFUSED: gate HALTED at {float(g.get('drawdown_pct', 0.0)) * 100:.2f}% drawdown; the on-chain leg is closed until reset_halt()."
            raise OnchainRefused(risk_gate.HALTED_DRAWDOWN, reason, decision=self._wallet_decision(risk_gate.HALTED_DRAWDOWN, reason))
        arming = s.onchain_arming_error()
        if arming:
            reason = f"REFUSED: {arming}."
            raise OnchainRefused("ONCHAIN_NOT_ARMED", reason, decision=self._wallet_decision("ONCHAIN_NOT_ARMED", reason))
        if int(chain_id) != int(s.wallet_chain_id):
            reason = f"REFUSED: chain {chain_id} is not the configured chain {s.wallet_chain_id}."
            raise OnchainRefused("CHAIN_NOT_ALLOWED", reason, decision=self._wallet_decision("CHAIN_NOT_ALLOWED", reason))
        notional = float(notional_usd)
        if not (notional > 0):
            reason = f"REFUSED: on-chain notional ${notional:,.2f} is not a positive amount."
            raise OnchainRefused(risk_gate.MAX_NOTIONAL, reason, decision=self._wallet_decision(risk_gate.MAX_NOTIONAL, reason))
        if not reducing and notional > s.onchain_max_notional_usd:
            reason = (f"REFUSED: on-chain notional ${notional:,.2f} is outside the per-request cap "
                      f"${s.onchain_max_notional_usd:,.2f} (raise DELTR_ONCHAIN_MAX_NOTIONAL_USD deliberately).")
            raise OnchainRefused(risk_gate.MAX_NOTIONAL, reason, decision=self._wallet_decision(risk_gate.MAX_NOTIONAL, reason))
        if not reducing and self._onchain_notional_usd + notional > s.onchain_max_aggregate_usd:
            reason = (f"REFUSED: aggregate on-chain notional ${self._onchain_notional_usd + notional:,.2f} would exceed "
                      f"${s.onchain_max_aggregate_usd:,.2f} for this run.")
            raise OnchainRefused(risk_gate.AGGREGATE_NOTIONAL, reason, decision=self._wallet_decision(risk_gate.AGGREGATE_NOTIONAL, reason))
        if not confirm:
            reason = "REFUSED: this moves real funds through the Binance Agentic Wallet; call again with confirm=true."
            raise OnchainRefused("CONFIRM_REQUIRED", reason, decision=self._wallet_decision("CONFIRM_REQUIRED", reason))
        return self._wallet_decision(
            risk_gate.OK,
            f"APPROVED: on-chain request for ${notional:,.2f} on chain {chain_id}; the wallet's own daily limits apply on top.",
            approved=True,
        )

    def _charge_onchain(self, notional_usd: float) -> None:
        """Book on-chain notional against the run's aggregate ceiling.

        Called BEFORE the wallet is asked to sign, never after: a swap the CLI has broadcast but
        not yet confirmed, and a payment whose outcome is unknown, have both already moved value.
        Charging only what came back confirmed left the ceiling unenforced for exactly the cases
        it exists to bound.
        """
        self._onchain_notional_usd += max(0.0, float(notional_usd))

    def _refund_onchain(self, notional_usd: float, code: Any) -> None:
        """Give a charge back ONLY when the failure code proves nothing was signed or submitted.

        Anything else — a timeout, an unmapped CLI error, a broadcast the CLI never confirmed —
        keeps the charge, because an uncertain swap is a swap that may be on chain.
        """
        if str(code) in NOT_SUBMITTED_CODES:
            self._onchain_notional_usd = max(0.0, self._onchain_notional_usd - max(0.0, float(notional_usd)))

    def _leg_guard(self, notional_usd: float, *, reducing: bool = False) -> RiskDecisionRecord:
        """Pre-flight for the on-chain leg of a gated plan (LiveRouter -> WalletDexLeg).

        The deterministic 19-check gate has ALREADY approved the plan in the Executor; this is
        the on-chain-specific layer on top of it: kill switch, drawdown halt, the arming triple,
        the chain allow-list and the on-chain notional caps.  It runs before the wallet is asked
        for anything, so a refusal provably left nothing on chain.  ``confirm`` is True here
        because LIVE already required an explicit confirm to reach ``execute``.

        ``_onchain_notional_usd`` tracks OPEN on-chain exposure, not gross swap volume.  It used
        to only ever grow, so a $1,000 aggregate cap was consumed by four $250 swaps — two
        entries and their two reversals — and every leg after that was refused, INCLUDING the
        reversals and unwinds that exist to flatten the book.  A reducing leg now gives its
        notional back instead of taking more.
        """
        n = float(notional_usd)
        decision = self._onchain_guard(notional_usd=n, chain_id=self.settings.wallet_chain_id,
                                       confirm=True, reducing=reducing)
        if reducing:
            self._onchain_notional_usd = max(0.0, self._onchain_notional_usd - n)
        else:
            self._onchain_notional_usd += n
        return decision

    # ------------------------------------------------------------------ LIVE preflight
    async def live_preflight(self) -> dict[str, Any]:
        """Every LIVE requirement, checked BEFORE the first tick and before any order.

        Refuses with the FIRST missing requirement named exactly, in a fixed order:

        1. the mode was selected explicitly (``DELTR_MODE=live``);
        2. real mainnet credentials are present, and ``BINANCE_API_ENV=mainnet`` says so;
        3. the LIVE acknowledgement phrase is set;
        4. the on-chain opt-in is complete (mode + acknowledgement);
        5. the Binance Agentic Wallet CLI is installed and reachable;
        6. that CLI reports a signed-in wallet session.

        Deltr never signs anything into the wallet and never creates a session: 5 and 6 are read
        from the CLI, and a signed-out wallet is reported with the exact command to run.
        Returns the redacted facts the banner prints; raises RuntimeError to refuse.  No secret
        is read, logged or returned by any branch.
        """
        s = self.settings
        if s.mode != Mode.LIVE:
            raise RuntimeError(f"live_preflight is LIVE-only; mode is {s.mode.value.upper()}")
        arming = s.live_arming_error()
        if arming:
            raise RuntimeError(f"REFUSING TO START LIVE: {arming}")
        if not self.wallet.is_available():
            raise RuntimeError(
                "REFUSING TO START LIVE: the Binance Agentic Wallet CLI is not available. LIVE routes the "
                f"on-chain leg through it and Deltr has no other way to sign. Install it, or point "
                f"DELTR_BAW_BIN at it (currently {s.baw_bin!r}); check with `{s.baw_bin} --version`."
            )
        try:
            auth = await self.wallet.auth_status()
        except AgenticWalletError as exc:
            raise RuntimeError(
                f"REFUSING TO START LIVE: the wallet CLI did not answer `wallet status`: {exc}. {SIGNIN_REMEDY}"
            ) from exc
        if not auth.signed_in:
            raise RuntimeError(
                f"REFUSING TO START LIVE: the Binance Agentic Wallet is not signed in (status {auth.status}). "
                f"{SIGNIN_REMEDY} Deltr never signs in for you and never holds the key."
            )
        addresses: dict[str, str] = {}
        try:
            addresses = await self.wallet.wallet_address()
        except AgenticWalletError as exc:
            log.warning("live preflight: wallet address unavailable (%s)", exc)
        facts = {
            "mode": s.mode.value,
            "real_funds_armed": True,
            "execution_style": s.execution_style.value,
            "execution_style_label": s.execution_style_label,
            "data_source": s.data_source_label,
            "order_host": s.hosts["futures_order_rest"],
            "max_notional_usd": s.max_notional_usd,
            "max_aggregate_usd": s.max_aggregate_usd,
            "onchain_max_notional_usd": s.onchain_max_notional_usd,
            "onchain_max_aggregate_usd": s.onchain_max_aggregate_usd,
            "wallet_signed_in": True,
            "wallet_status": auth.status,
            "wallet_addresses": addresses or ({"address": auth.address} if auth.address else {}),
            "wallet_binary": self.wallet.resolved_binary(),
            "chain_id": s.wallet_chain_id,
            "custody": ("Binance's Agentic Wallet holds the key, enforces its own limits and performs the signing. "
                        "Deltr never holds, reads, stores or signs with a private key."),
        }
        self.live_facts = facts
        self.state.emit(
            "log",
            f"LIVE preflight passed: real funds armed, {s.execution_style.value} execution, per-trade cap "
            f"${s.max_notional_usd:,.0f}, aggregate cap ${s.max_aggregate_usd:,.0f}",
            level="warn",
        )
        return facts

    async def wallet_status(self) -> dict[str, Any]:
        """Read-only view of the Binance Agentic Wallet leg: CLI presence, session, quota, caps.

        Never moves value and never starts a sign-in.  When the CLI is missing or signed out the
        answer says so and names the command an operator should run; Deltr does not simulate.
        """
        s = self.settings
        gate_snapshot = self.gate.snapshot()
        out: dict[str, Any] = {
            "custody": ("Binance's Agentic Wallet holds the key, enforces its own daily limits and performs the "
                        "signing. Deltr never holds, reads, stores or signs with a private key."),
            "binary": s.baw_bin,
            "installed": self.wallet.is_available(),
            "resolved_path": self.wallet.resolved_binary(),
            "chain_id": s.wallet_chain_id,
            "armed": s.onchain_arming_error() is None,
            "arming_error": s.onchain_arming_error(),
            "caps": {
                "per_request_usd": s.onchain_max_notional_usd,
                "aggregate_usd": s.onchain_max_aggregate_usd,
                "aggregate_used_usd": self._onchain_notional_usd,
                "max_slippage_pct": s.onchain_max_slippage_pct,
                "max_price_impact_pct": s.onchain_max_price_impact_pct,
            },
            "gate": {k: gate_snapshot[k] for k in ("state", "halted", "kill_switch", "drawdown_pct")},
        }
        if not out["installed"]:
            out["signed_in"] = False
            out["error"] = AgenticWalletError(
                "BAW_NOT_INSTALLED",
                f"the Binance Agentic Wallet CLI ({s.baw_bin!r}) is not installed or not on PATH",
                remedy="install it so `baw` is on PATH, or set DELTR_BAW_BIN. Deltr does not fall back to a simulated on-chain leg.",
            ).as_dict()
            return out
        try:
            auth = await self.wallet.auth_status()
            out.update(auth.as_dict())
            if auth.signed_in:
                out["addresses"] = await self.wallet.wallet_address()
                out["wallet_daily_quota"] = await self.wallet.left_quota()
            else:
                out["error"] = {"code": "BAW_NOT_SIGNED_IN", "message": f"wallet status {auth.status}",
                                "remedy": SIGNIN_REMEDY}
        except AgenticWalletError as exc:
            out["signed_in"] = False
            out["error"] = exc.as_dict()
        return out

    async def onchain_swap(
        self,
        *,
        from_token: str,
        to_token: str,
        amount: float,
        notional_usd: float,
        confirm: bool = False,
        chain_id: Optional[int] = None,
        slippage: Optional[float] = None,
        min_receive: Optional[float] = None,
    ) -> dict[str, Any]:
        """Ask the Binance Agentic Wallet to swap on-chain, behind the gate pre-flight.

        Deltr requests; Binance's wallet decides whether to sign, applies its own daily limits on
        top of Deltr's caps, and broadcasts.  The result carries the transaction hash and the
        amount the CLI said was actually received, and is reported unconfirmed rather than
        assumed filled when the CLI has not finished the order.
        """
        s = self.settings
        chain = int(chain_id if chain_id is not None else s.wallet_chain_id)
        slip = float(slippage) if slippage is not None else float(s.onchain_max_slippage_pct)
        if slip > s.onchain_max_slippage_pct:
            raise OnchainRefused(
                "INVALID_ARGUMENT",
                f"REFUSED: slippage {slip:g}% exceeds the configured ceiling {s.onchain_max_slippage_pct:g}%.",
            )
        decision = self._onchain_guard(notional_usd=notional_usd, chain_id=chain, confirm=confirm)
        self.wallet.require_available()
        await self.wallet.require_signed_in()
        # charge the run's aggregate ceiling BEFORE the wallet is asked, and give it back only on a
        # code that proves nothing was submitted.  An unconfirmed swap has a transaction hash and
        # has moved value; charging only confirmed ones let unlimited PENDING swaps through the cap.
        self._charge_onchain(notional_usd)
        try:
            result = await self.wallet.swap(
                from_token, to_token, float(amount),
                chain_id=chain, slippage=f"{slip:g}",
                min_receive=min_receive, max_price_impact_pct=s.onchain_max_price_impact_pct,
            )
        except AgenticWalletError as exc:
            self._refund_onchain(notional_usd, exc.code)
            raise
        self.state.emit(
            "fill" if result.confirmed else "log",
            f"agentic wallet swap {result.status.lower()}" + (f" tx {result.tx_hash}" if result.tx_hash else ""),
            data={"order_id": result.order_id, "confirmed": result.confirmed, "chain_id": chain},
        )
        return {"decision": decision.model_dump(mode="json"), "swap": result.as_dict(),
                "cli_calls": list(self.wallet.calls[-4:])}

    # ------------------------------------------------------------------ x402 (B402) payments
    async def x402_pay(self, payment_required: Any, *, selected_index: Optional[int] = None, confirm: bool = False) -> dict[str, Any]:
        """Pay an HTTP 402 challenge through the wallet: preview, policy, sign.

        Value-moving, so it takes the same pre-flight as a swap.  The wallet signs; Deltr receives
        only the replay header value, which is returned by name and never echoed into a log.
        """
        s = self.settings
        parsed = parse_payment_required(payment_required)
        chosen = parsed.options[int(selected_index)] if selected_index is not None and 0 <= int(selected_index) < len(parsed.options) else parsed.options[0]
        units = amount_from_atomic(chosen.amount_atomic, self.x402_buyer.asset_decimals) or 0.0
        charged = max(units, 1e-9)
        decision = self._onchain_guard(notional_usd=charged, chain_id=s.wallet_chain_id, confirm=confirm)
        self.wallet.require_available()
        await self.wallet.require_signed_in()
        # a payment is value leaving the wallet exactly as a swap is, so it consumes the same
        # aggregate ceiling.  It used to consume nothing, which made the per-run cap unenforceable:
        # any number of payments under the per-request cap could be signed in one run.
        self._charge_onchain(charged)
        try:
            payment = await self.x402_buyer.pay(parsed, selected_index=selected_index)
        except (PaymentError, AgenticWalletError) as exc:
            self._refund_onchain(charged, getattr(exc, "code", ""))
            raise
        self.state.emit("log", f"x402 payment signed by the agentic wallet (option {payment.selected_index})",
                        data={"payment_id": payment.payment_id, "network": payment.option.network if payment.option else None})
        return {"decision": decision.model_dump(mode="json"), "payment": payment.as_dict(include_header_value=True),
                "challenge": parsed.as_dict()}

    def x402_edge_report_challenge(
        self,
        *,
        capital_usd: Optional[float] = None,
        leverage: float = 2.0,
        horizon_h: float = 24.0,
        resource: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build the 402 challenge whose paid artifact is Deltr's existing edge report.

        Read-only and local: it seals the report with the same sha256 Deltr seals receipts with
        and returns the challenge body and header.  No payment is requested, none is claimed to
        have settled, and no mainnet B402 application was made.
        """
        edge, sizing, components, analysis = self.explain_edge(capital_usd, leverage, horizon_h, None)
        ms, _ = self.market()
        symbol = getattr(ms, "symbol", None) or self.settings.symbol
        markdown = render_edge_report(
            edge=edge, analysis=analysis, components=components, market=ms, sizing=sizing,
            symbol=symbol, mode=self.settings.mode.value,
        )
        artifact = artifact_from_edge_report(
            {"title": f"Deltr edge report: {symbol}", "markdown": markdown}, symbol=symbol
        )
        challenge = self.x402_seller.challenge(artifact, resource=resource)
        out = challenge.as_dict()
        out["artifact_preview"] = markdown[:400]
        return out

    # ------------------------------------------------------------------ operator controls
    def kill_switch(self, on: bool, reason: str) -> SystemStatus:
        self.gate.set_kill_switch(bool(on))
        self.state.emit("gate", f"kill switch {'ENGAGED' if on else 'released'} ({reason or 'operator'})", level="warn" if on else "info",
                        data={"kill_switch": bool(on), "reason": reason})
        return self.status()

    def reset_halt(self, reason: str) -> SystemStatus:
        """Clear a sticky HALT only when the book is flat and drawdown < the stop-loss; never re-bases the peak."""
        if not reason or len(reason.strip()) < 3:
            raise ValueError("reset_halt needs a reason (>= 3 characters)")
        if self.gate.halted:
            open_positions = self.portfolio.positions("open")
            if open_positions:
                raise HaltNotClearable(f"{len(open_positions)} position(s) still open — unwind first")
            if self.gate.drawdown_pct >= self.gate.limits.max_drawdown_pct:
                raise HaltNotClearable(
                    f"drawdown {self.gate.drawdown_pct * 100:.2f}% still >= {self.gate.limits.max_drawdown_pct * 100:.1f}% stop-loss"
                )
            if not self.gate.reset_halt():
                raise HaltNotClearable("gate refused to clear the halt")
            self.state.emit("gate", f"halt cleared by operator: {reason}", level="warn", data={"reason": reason})
        else:
            self.state.emit("gate", f"reset_halt requested while not halted ({reason}); no-op", level="info")
        return self.status()

    async def stress(self, scenario: StressScenario) -> StressResult:
        return await self.stress_controller.apply(scenario)

    def set_min_edge(self, bps: float) -> tuple[float, float]:
        """Runtime min-edge knob for the scout AND the gate's NEGATIVE_EDGE limit.

        PAPER: the demo override range [-50, 50] bps (floor reported as 0; a negative value
        lets a currently-negative live edge through so the execution path can be shown).
        TESTNET/LIVE: the scout clamps to [0, 50] — a real-order run can never target a net loss.
        LIVE with the test override (DELTR_LIVE_TEST_ACK + per-trade cap <= $25): the PAPER
        range, so the whole real-money path can be exercised for cents on a negative-edge day.
        """
        lo, hi = MIN_EDGE_OVERRIDE_RANGE
        if self.settings.mode == Mode.PAPER or self.settings.live_test_override:
            effective = min(max(float(bps), lo), hi)
            floor = float(self.scout.min_edge_floor())
            self.state.min_edge_bps = effective
            self.state.emit("scan", f"min edge set to {effective:g} bps (PAPER range [{lo:g}, {hi:g}])",
                            data={"requested": float(bps), "effective": effective, "floor": floor})
        else:
            effective = float(self.scout.set_min_edge(float(bps)))
            floor = float(self.scout.min_edge_floor())
        self.gate.limits = risk_gate.RiskLimits(**{**self.gate.limits.as_dict(), "min_expected_edge_bps": effective})
        self.state.min_edge_bps = effective
        self.state.min_edge_floor_bps = floor
        return effective, floor

    def get_receipt(self, receipt_id: str) -> Optional[ExecutionReceipt]:
        return self.receipts.get(receipt_id)

    def activity_log(self, n: int = 30) -> list[McpActivity]:
        return self.activity.recent(n)

    # ------------------------------------------------------------------ one-shot
    async def run_once(self) -> dict[str, Any]:
        """tick + scan + explain + propose(trade capital, default leverage) + precheck (+ execute with --auto)."""
        s = self.settings
        try:
            ms = await self.hub.tick_once()
        except StopAsyncIteration:
            ms = self.hub.snapshot()
        out: dict[str, Any] = {
            "ok": False,
            "mode": s.mode.value,
            "replay": bool(self.replay_path),
            "replay_path": self.replay_path,
            "symbol": s.symbol,
            "version": s.version,
            "ts": utcnow().isoformat(),
            "capital_usd": self.trade_capital_usd,
            "portfolio_capital_usd": s.capital_usd,
            "leverage": s.default_leverage,
            "horizon_h": s.funding_horizon_hours,
            "min_edge_bps": self.state.min_edge_bps,
            "min_edge_override": self.min_edge_override,
            "gate_median_us": self.state.gate_median_us,
            "venues": [h.model_dump(mode="json") for h in self.state.venues.values()],
            "start_errors": list(self.start_errors),
            "market": None, "edge": None, "components": [], "horizon": None, "opportunity": None, "sizing": None,
            "plan": None, "proposal": None, "precheck": None, "receipt": None, "error": None,
        }
        if ms is None or ms.dex is None or ms.funding is None:
            out["error"] = "no market state: " + ("; ".join(self.start_errors) or "venues returned no data")
            out["status"] = self.status().model_dump(mode="json")
            return out
        out["market"] = ms.model_dump(mode="json")
        opp = self.state.opportunity
        if opp is None:
            try:
                opp = self.scan()
            except ValueError as exc:
                out["error"] = f"scan failed: {exc}"
                out["status"] = self.status().model_dump(mode="json")
                return out
        out["opportunity"] = opp.model_dump(mode="json")
        try:
            edge, sizing, comps, hz_analysis = self.explain_edge(self.trade_capital_usd, s.default_leverage, s.funding_horizon_hours)
            out["edge"] = edge.model_dump(mode="json")
            out["components"] = [c.model_dump(mode="json") for c in comps]
            out["horizon"] = hz_analysis.model_dump(mode="json")
            out["sizing"] = {
                "qty": float(sizing.qty), "notional_usd": sizing.notional_usd, "margin_usd": sizing.margin_usd,
                "cash_required_usd": sizing.cash_required_usd, "leverage": sizing.leverage, "capped_by": sizing.capped_by,
            }
            auto_receipt = self.last_auto_receipt if s.auto_execute else None
            if auto_receipt is not None:
                # --auto already traded during the tick: report THAT plan / decision / receipt instead of
                # proposing a second hedge against a book that now holds the auto position
                plan = auto_receipt.plan
                out["plan"] = plan.model_dump(mode="json")
                out["proposal"] = self.executor.build_proposal(plan, ms, None).model_dump(mode="json")
                out["precheck"] = auto_receipt.decision.model_dump(mode="json")
                out["receipt"] = auto_receipt.model_dump(mode="json")
                out["ok"] = True
            elif s.auto_execute and any(p.symbol == s.symbol for p in self.portfolio.positions("open")):
                out["note"] = "auto: a position is already open for this symbol (restored or opened earlier); no new proposal"
                out["ok"] = True
            else:
                plan, proposal, precheck = await self.propose(self.trade_capital_usd, s.default_leverage, s.symbol, TraceSource.CLI, client="cli")
                out["plan"] = plan.model_dump(mode="json")
                out["proposal"] = proposal.model_dump(mode="json")
                out["precheck"] = precheck.model_dump(mode="json")
                out["ok"] = True
                if s.auto_execute and precheck.approved:
                    receipt = await self.execute(plan.id, True, TraceSource.CLI, client="cli")
                    out["receipt"] = receipt.model_dump(mode="json")
        except (SizingError, ValueError) as exc:
            out["error"] = str(exc)
            out["ok"] = out["edge"] is not None
        out["status"] = self.status().model_dump(mode="json")
        return out


def build_engine(
    settings: Settings,
    *,
    replay_path: Optional[str] = None,
    min_edge_override: Optional[float] = None,
    http: Optional[httpx.AsyncClient] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> Engine:
    """Wire everything: ``MarketDataHub`` or ``ReplayHub``; ``PaperRouter`` or ``TestnetRouter``.

    ``http`` (an injected ``httpx.AsyncClient``, e.g. with a ``MockTransport``) and ``clock``
    (a deterministic ``datetime`` source for replay determinism tests) are optional extras.
    """
    return Engine(settings, replay_path=replay_path, min_edge_override=min_edge_override, http=http, clock=clock)


__all__ = [
    "Engine", "HaltNotClearable", "ReplayDexQuoter", "build_engine", "PositionNotFound", "SizingError", "StressRefused",
    "TRADE_CAPITAL_FRACTION", "AUTO_COOLDOWN_S",
]
