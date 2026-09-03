"""deltr/executor.py — THE execution choke point (sections 3.6 / 3.7 / 3.8 / 4.10).

Every order Deltr places — from the API, the MCP server, the CLI or the automatic
stop monitor — goes through ``Executor.execute`` / ``Executor.unwind``:

* one ``asyncio.Lock`` ⇒ one execution in flight, no double-spend across transports;
* plans are single-use (``state.take_plan``) with a TTL, re-priced at execution
  (``price_drift_bps``) and re-gated: ``build_proposal`` is the ONLY constructor of
  ``TradeProposal`` (tests/test_gate_inputs.py checks this by AST);
* leg sequencing per ``settings.leg_order``; the second leg is sized from the first
  fill; if the second leg fails the first is reversed (symmetric, both orders);
* an unwind is perp reduce-only BUY first (verified against the gate registry, so it
  passes even when HALTED / kill switch), then the DEX sell; a half-closed book is
  never left silently (receipt ``failed`` + error event);
* every outcome is a sealed ``ExecutionReceipt`` with ordered ``steps[]``.

Routers: ``PaperRouter`` (no credentials, simulated fills on live prices) and
``TestnetRouter`` (real USDⓈ-M testnet perp leg via LIMIT IOC; DEX leg simulated).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any, List, Optional, Protocol, Tuple

from deltr.config import LegOrder, Mode, Settings
from deltr.edge import floor_to_step, net_edge, perp_reference, round_to_tick, size_for_capital
from deltr.models import (
    DataSource,
    DexQuote,
    ExecutionReceipt,
    Fill,
    HedgePlan,
    MarketState,
    OrderLeg,
    Position,
    ReceiptStatus,
    RiskDecisionRecord,
    Side,
    StepName,
    StepStatus,
    SymbolFilters,
    TradeProposal,
    TraceSource,
    TraceStep,
    Venue,
    utcnow,
)
from deltr.receipts import make_receipt

log = logging.getLogger("deltr.executor")

DEX_LEG = 0
PERP_LEG = 1
REVERSE_LEG_OFFSET = 2  # client ids of reversal orders use leg digit 2 (DEX) / 3 (perp)

# FuturesError codes (section 3.8): fatal → no retry
_FATAL_FUTURES_CODES = {-1111, -2019, -4164, -4028, -2022}


class LegError(Exception):
    """A leg could not be filled (zero fill after retries, venue error, simulated failure)."""

    def __init__(self, reason: str, *, leg_index: int = -1, venue: Optional[Venue] = None, retryable: bool = False, code: Optional[int] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.leg_index = leg_index
        self.venue = venue
        self.retryable = retryable
        self.code = code


class PositionNotFound(KeyError):
    """unwind() was asked for a position id that is not open."""


class Router(Protocol):
    mode: Mode

    async def fill(self, leg: OrderLeg, ms: MarketState, qty: Optional[float] = None, plan_id: str = "", attempt: int = 1) -> Fill: ...

    async def reverse(self, fill: Fill, ms: MarketState, plan_id: str) -> Fill: ...

    async def reverse_partial(self, leg: OrderLeg, residual_qty: float, ms: MarketState, plan_id: str) -> Fill: ...


# --------------------------------------------------------------------------- helpers
def client_order_id(plan_id: str, leg: int, attempt: int) -> str:
    """``DLTR{plan_id[-8:]}{leg}{attempt}`` — matches Binance ``^[.A-Z:/a-z0-9_-]{1,36}$``."""
    return f"DLTR{plan_id[-8:]}{leg}{attempt}"


def legging_window_ms(first: Fill, second: Fill) -> int:
    return int(abs((second.ts - first.ts).total_seconds() * 1000))


def leg_index_of(leg: OrderLeg) -> int:
    return DEX_LEG if leg.venue == Venue.PANCAKESWAP_V3 else PERP_LEG


def perp_mark(ms: Optional[MarketState]) -> Optional[float]:
    if ms is None:
        return None
    if ms.perp_ref_price:
        return float(ms.perp_ref_price)
    if ms.funding is not None:
        return float(ms.funding.mark_price)
    return None


def _opposite(side: Side) -> Side:
    return Side.SELL if side == Side.BUY else Side.BUY


def _reversal_cost(first: Fill, reversal: Fill) -> float:
    """USD cost (positive = loss) of a leg that was filled and then reversed."""
    q = min(first.qty, reversal.qty)
    pnl = (reversal.price - first.price) * q if first.side == Side.BUY else (first.price - reversal.price) * q
    return -pnl + first.fee_usd + reversal.fee_usd


class _Trace:
    """Ordered TraceStep recorder with per-step latency."""

    def __init__(self) -> None:
        self.steps: List[TraceStep] = []
        self._t = time.perf_counter()

    def add(self, step: StepName, status: StepStatus, summary: str, **data: Any) -> TraceStep:
        now = time.perf_counter()
        ts = TraceStep(step=step, status=status, summary=summary, data=data, latency_ms=int((now - self._t) * 1000))
        self._t = now
        self.steps.append(ts)
        return ts


# --------------------------------------------------------------------------- PaperRouter
class PaperRouter:
    """Simulated fills on live prices.  NO credential arguments (test_mode_isolation).

    DEX BUY  = exact-output quote price × (1 + paper_dex_extra_slippage_bps), fee = gas
    DEX SELL = exact-input quote price × (1 − paper_dex_extra_slippage_bps), fee = gas
    Perp SELL = mark × (1 − perp_slippage_bps), min with the testnet bid when the bid is
    within price_sanity_bps of mark; BUY mirrored; fee = qty·price·taker_bps.
    Honours the ``dex_leg_fail`` stress flag (consumed once) by raising LegError.
    """

    mode = Mode.PAPER

    def __init__(self, dex: Any, settings: Settings, filters: SymbolFilters) -> None:
        self.dex = dex
        self.settings = settings
        self.filters = filters
        self.stress: Any = None  # Portfolio.StressFlags, attached by the Executor

    async def requote(self, qty: float, ms: Optional[MarketState]) -> Optional[DexQuote]:
        """Fresh exact-output/-input quote for `qty`; falls back to the hub's last quote."""
        fn = getattr(self.dex, "dex_quote", None)
        if callable(fn) and qty > 0:
            try:
                return await fn(qty)
            except Exception as exc:
                log.warning("paper: DEX re-quote failed (%s); using the hub's last quote", exc)
        return ms.dex if ms is not None else None

    async def fill(self, leg: OrderLeg, ms: MarketState, qty: Optional[float] = None, plan_id: str = "", attempt: int = 1) -> Fill:
        q = float(qty if qty is not None else leg.qty)
        if q <= 0:
            raise LegError("qty must be > 0", leg_index=leg_index_of(leg), venue=leg.venue)
        t0 = time.perf_counter()
        if leg.venue == Venue.PANCAKESWAP_V3:
            return await self._fill_dex(leg, ms, q, attempt, t0)
        return self._fill_perp(leg, ms, q, plan_id, attempt, t0)

    async def _fill_dex(self, leg: OrderLeg, ms: MarketState, q: float, attempt: int, t0: float) -> Fill:
        if self.stress is not None and getattr(self.stress, "dex_leg_fail", False):
            self.stress.dex_leg_fail = False  # consumed once
            raise LegError("sim:dex_leg_fail(stress)", leg_index=DEX_LEG, venue=leg.venue, retryable=False)
        quote = await self.requote(q, ms)
        if quote is None:
            raise LegError("no DEX quote available", leg_index=DEX_LEG, venue=leg.venue, retryable=True)
        extra = self.settings.paper_dex_extra_slippage_bps / 1e4
        price = quote.exec_price_buy * (1.0 + extra) if leg.side == Side.BUY else quote.exec_price_sell * (1.0 - extra)
        div = 0.0 if quote.mid_price <= 0 else 1e4 * (price - quote.mid_price) / quote.mid_price
        return Fill(
            leg_index=DEX_LEG, venue=leg.venue, symbol=leg.symbol, side=leg.side, qty=q, price=price, fee_usd=quote.gas_usd,
            ref="paper", simulated=True, source=DataSource.PAPER, attempt=attempt, reference_divergence_bps=div,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    def _fill_perp(self, leg: OrderLeg, ms: MarketState, q: float, plan_id: str, attempt: int, t0: float) -> Fill:
        mark = perp_mark(ms)
        if mark is None or mark <= 0:
            raise LegError("no perp mark price", leg_index=PERP_LEG, venue=leg.venue, retryable=True)
        sell_ref, buy_ref = perp_reference(mark, self.settings.perp_slippage_bps)
        sanity = self.settings.price_sanity_bps
        book = ms.cex_perp_book
        if leg.side == Side.SELL:
            price = sell_ref
            if book is not None and book.bid > 0 and abs(book.bid - mark) / mark * 1e4 <= sanity:
                price = min(book.bid, price)
        else:
            price = buy_ref
            if book is not None and book.ask > 0 and abs(book.ask - mark) / mark * 1e4 <= sanity:
                price = max(book.ask, price)
        fee = q * price * self.settings.perp_taker_fee_bps / 1e4
        return Fill(
            leg_index=PERP_LEG, venue=leg.venue, symbol=leg.symbol, side=leg.side, qty=q, price=price, fee_usd=fee,
            ref="paper", simulated=True, source=DataSource.PAPER, client_id=client_order_id(plan_id, PERP_LEG, attempt),
            attempt=attempt, reference_divergence_bps=1e4 * (price - mark) / mark, latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    async def reverse(self, fill: Fill, ms: MarketState, plan_id: str) -> Fill:
        leg = OrderLeg(venue=fill.venue, symbol=fill.symbol, side=_opposite(fill.side), qty=fill.qty, price_hint=fill.price,
                       reduce_only=fill.venue == Venue.BINANCE_FUTURES)
        f = await self.fill(leg, ms, qty=fill.qty, plan_id=plan_id, attempt=fill.attempt + 1)
        return f.model_copy(update={"ref": "paper:reverse"})

    async def reverse_partial(self, leg: OrderLeg, residual_qty: float, ms: MarketState, plan_id: str) -> Fill:
        rev = OrderLeg(venue=leg.venue, symbol=leg.symbol, side=_opposite(leg.side), qty=residual_qty, price_hint=leg.price_hint,
                       reduce_only=leg.venue == Venue.BINANCE_FUTURES)
        f = await self.fill(rev, ms, qty=residual_qty, plan_id=plan_id, attempt=2)
        return f.model_copy(update={"ref": "paper:reverse_partial"})


# --------------------------------------------------------------------------- TestnetRouter
class TestnetRouter:
    """Real USDⓈ-M futures **testnet** perp leg (LIMIT + IOC only); DEX leg simulated.

    Requires a FuturesClient whose ``base_url`` contains "testnet" (checked at
    construction — never a production host).  Perp SELL is placed at
    ``round_down(mark·(1 − 10 bps), tick)``; a zero fill is retried once at 25 bps;
    a partial fill returns ``executedQty`` (the Executor handles the residual).
    Unknown outcomes (timeouts) are resolved with ``GET order?origClientOrderId``
    **before** any retry.  Attempts back off 200/400/800 ms.
    """

    mode = Mode.TESTNET
    __test__ = False  # keep pytest from collecting this class (name starts with "Test")

    def __init__(self, futures: Any, dex: Any, settings: Settings, filters: SymbolFilters) -> None:
        base = str(getattr(futures, "base_url", "") or "")
        if "testnet" not in base.lower():
            raise AssertionError(f"TestnetRouter refuses a non-testnet futures host: {base!r}")
        self.futures = futures
        self.settings = settings
        self.filters = filters
        self._paper = PaperRouter(dex, settings, filters)
        self._prep: Any = None
        self.backoff_ms: Tuple[int, int, int] = (200, 400, 800)
        self.max_attempts = 3

    # stress flags are shared with the simulated DEX leg
    @property
    def stress(self) -> Any:
        return self._paper.stress

    @stress.setter
    def stress(self, flags: Any) -> None:
        self._paper.stress = flags

    async def prepare(self, symbol: str, leverage: int) -> Any:
        self._prep = await self.futures.prepare_account(symbol, leverage)
        return self._prep

    async def requote(self, qty: float, ms: Optional[MarketState]) -> Optional[DexQuote]:
        return await self._paper.requote(qty, ms)

    def _side_params(self, leg: OrderLeg) -> Tuple[bool, str]:
        """(reduce_only, position_side) for the account's position mode."""
        prep = self._prep
        if prep is not None and getattr(prep, "dual_side", False):
            return False, "SHORT"  # hedge mode: reduceOnly is rejected; SHORT on open SELL and closing BUY
        return bool(leg.reduce_only), "BOTH"

    def _limit_price(self, side: Side, mark: float, band_bps: float) -> float:
        tick = self.filters.tick_size
        if side == Side.SELL:
            return round_to_tick(mark * (1.0 - band_bps / 1e4), tick)
        return round_to_tick(mark * (1.0 + band_bps / 1e4), tick) + tick

    async def fill(self, leg: OrderLeg, ms: MarketState, qty: Optional[float] = None, plan_id: str = "", attempt: int = 1) -> Fill:
        if leg.venue == Venue.PANCAKESWAP_V3:
            return await self._paper.fill(leg, ms, qty=qty, plan_id=plan_id, attempt=attempt)
        return await self._fill_perp(leg, ms, qty, plan_id, attempt, leg_code=PERP_LEG)

    async def _fill_perp(self, leg: OrderLeg, ms: MarketState, qty: Optional[float], plan_id: str, attempt: int, leg_code: int) -> Fill:
        mark = perp_mark(ms)
        if mark is None or mark <= 0:
            raise LegError("no perp mark price", leg_index=PERP_LEG, venue=leg.venue, retryable=True)
        q = floor_to_step(float(qty if qty is not None else leg.qty), self.filters.step_size)
        if q < self.filters.min_qty:
            raise LegError(f"qty {q} below min_qty {self.filters.min_qty}", leg_index=PERP_LEG, venue=leg.venue)
        reduce_only, position_side = self._side_params(leg)
        bands = (self.settings.testnet_ioc_band_bps, self.settings.testnet_ioc_retry_band_bps)
        attempt_no = attempt
        last_reason = "zero fill"
        t0 = time.perf_counter()
        for i in range(self.max_attempts):
            band = bands[min(i, 1)]
            price = self._limit_price(leg.side, mark, band)
            cid = client_order_id(plan_id, leg_code, attempt_no)
            res = None
            try:
                res = await self.futures.place_limit_ioc(leg.symbol, leg.side, q, price, cid, reduce_only=reduce_only, position_side=position_side)
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code in _FATAL_FUTURES_CODES:
                    raise LegError(f"futures error {code}: {exc}", leg_index=PERP_LEG, venue=leg.venue, retryable=False, code=code) from exc
                if code == -1021:
                    log.warning("testnet: timestamp out of recvWindow; resyncing time")
                    await self._maybe(self.futures.sync_time)
                elif code == -4061:
                    log.warning("testnet: positionSide mismatch; re-reading position mode")
                    await self._maybe(self.futures.prepare_account, leg.symbol, int(leg.leverage) or 1, _store=True)
                    reduce_only, position_side = self._side_params(leg)
                else:
                    # timeout / unknown → query by client id BEFORE any retry
                    res = await self._lookup(leg.symbol, cid)
                    if res is None:
                        log.warning("testnet: attempt %d (%s) unknown: %s", attempt_no, cid, exc)
                last_reason = f"{code or type(exc).__name__}: {exc}"
            if res is not None and float(getattr(res, "executed_qty", 0.0)) > 0:
                executed = floor_to_step(float(res.executed_qty), self.filters.step_size)
                avg = float(res.avg_price) if float(getattr(res, "avg_price", 0.0)) > 0 else price
                return Fill(
                    leg_index=PERP_LEG, venue=leg.venue, symbol=leg.symbol, side=leg.side, qty=executed, price=avg,
                    fee_usd=executed * avg * self.settings.perp_taker_fee_bps / 1e4, ref=str(res.order_id), simulated=False,
                    source=DataSource.BINANCE_FUTURES_TESTNET, client_id=cid, attempt=attempt_no,
                    reference_divergence_bps=1e4 * (avg - mark) / mark, latency_ms=int((time.perf_counter() - t0) * 1000),
                )
            attempt_no += 1
            if i < self.max_attempts - 1:
                await asyncio.sleep(self.backoff_ms[min(i, 2)] / 1000.0)
        raise LegError(f"perp leg unfilled after {self.max_attempts} attempts ({last_reason})", leg_index=PERP_LEG, venue=leg.venue, retryable=False)

    async def _lookup(self, symbol: str, cid: str) -> Any:
        fn = getattr(self.futures, "get_order_by_client_id", None)
        if not callable(fn):
            return None
        try:
            return await fn(symbol, cid)
        except Exception as exc:
            log.warning("testnet: order lookup %s failed: %s", cid, exc)
            return None

    async def _maybe(self, fn: Any, *args: Any, _store: bool = False) -> None:
        if not callable(fn):
            return
        try:
            out = await fn(*args)
            if _store:
                self._prep = out
        except Exception as exc:
            log.warning("testnet: %s failed: %s", getattr(fn, "__name__", fn), exc)

    async def reverse(self, fill: Fill, ms: MarketState, plan_id: str) -> Fill:
        if fill.venue == Venue.PANCAKESWAP_V3:
            return await self._paper.reverse(fill, ms, plan_id)
        leg = OrderLeg(venue=fill.venue, symbol=fill.symbol, side=_opposite(fill.side), qty=fill.qty, price_hint=fill.price, reduce_only=True)
        return await self._fill_perp(leg, ms, fill.qty, plan_id, 1, leg_code=PERP_LEG + REVERSE_LEG_OFFSET)

    async def reverse_partial(self, leg: OrderLeg, residual_qty: float, ms: MarketState, plan_id: str) -> Fill:
        if leg.venue == Venue.PANCAKESWAP_V3:
            return await self._paper.reverse_partial(leg, residual_qty, ms, plan_id)
        rev = OrderLeg(venue=leg.venue, symbol=leg.symbol, side=_opposite(leg.side), qty=residual_qty, price_hint=leg.price_hint, reduce_only=True)
        return await self._fill_perp(rev, ms, residual_qty, plan_id, 1, leg_code=PERP_LEG + REVERSE_LEG_OFFSET)


# --------------------------------------------------------------------------- Executor
class Executor:
    """Single choke point: lock → single-use plan → re-quote → gate → legs → receipt."""

    def __init__(self, state: Any, gate: Any, portfolio: Any, router: Any, hub: Any, hedger: Any, settings: Settings, receipts: Any, filters: SymbolFilters) -> None:
        self.state = state
        self.gate = gate
        self.portfolio = portfolio
        self.router = router
        self.hub = hub
        self.hedger = hedger
        self.settings = settings
        self.receipts = receipts
        self.filters = filters
        self._lock = asyncio.Lock()
        self._consumed: set[str] = set()
        if hasattr(router, "stress"):
            router.stress = portfolio.stress_flags

    @property
    def in_flight(self) -> bool:
        return self._lock.locked()

    # ------------------------------------------------------------------ state helpers
    def _emit(self, topic: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        emit = getattr(self.state, "emit", None)
        if callable(emit):
            try:
                emit(topic, message, level, data or {})
                return
            except Exception:  # pragma: no cover
                pass
        log.log(logging.ERROR if level == "error" else logging.INFO, "%s: %s", topic, message)

    def _record_decision(self, rec: RiskDecisionRecord) -> None:
        fn = getattr(self.state, "record_decision", None)
        if callable(fn):
            try:
                fn(rec)
            except Exception:  # pragma: no cover
                pass

    def _stress_label(self) -> Optional[str]:
        return getattr(self.state, "stress_active", None)

    def _gate_snapshot(self) -> dict:
        return self.gate.snapshot()

    def _peek(self, plan_id: str) -> Optional[HedgePlan]:
        fn = getattr(self.state, "peek_plan", None)
        if callable(fn):
            try:
                return fn(plan_id)
            except Exception:
                return None
        plans = getattr(self.state, "plans", None)
        return plans.get(plan_id) if isinstance(plans, dict) else None

    def _drop(self, plan_id: str) -> None:
        plans = getattr(self.state, "plans", None)
        if isinstance(plans, dict):
            plans.pop(plan_id, None)

    @staticmethod
    def legs_of(plan: HedgePlan) -> Tuple[OrderLeg, OrderLeg]:
        dex = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
        perp = next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES)
        return dex, perp

    # ------------------------------------------------------------------ proposal / gate
    def build_proposal(self, plan: HedgePlan, ms: Optional[MarketState], requote: Optional[DexQuote]) -> TradeProposal:
        """The ONLY constructor of gate inputs: plan + live market + portfolio flags."""
        dex_leg, perp_leg = self.legs_of(plan)
        if requote is not None:
            new_dex = requote.exec_price_sell if plan.reduce_only else requote.exec_price_buy
        elif ms is not None and ms.dex is not None:
            new_dex = ms.dex.exec_price_sell if plan.reduce_only else ms.dex.exec_price_buy
        else:
            new_dex = plan.ref_dex_price
        drift = 0.0 if plan.ref_dex_price <= 0 else 1e4 * abs(new_dex - plan.ref_dex_price) / plan.ref_dex_price
        perp_ref = perp_mark(ms) or plan.ref_perp_price
        cex_limit = float(self.settings.cex_stale_ms)
        stale_floor = cex_limit + 1.0
        if ms is None:
            age = stale_floor
        else:
            age = float(max(ms.freshness.cex_age_ms, ms.freshness.dex_age_ms))
            reason = ms.freshness.reason or ""
            if not ms.freshness.ok and (not reason or "stale" in reason):
                age = max(age, stale_floor)
            elif ms.freshness.ok and age > cex_limit:
                # the DEX is polled every 3 s and stays fresh up to dex_stale_ms (9 s); the gate has ONE
                # limit (cex_stale_ms): normalise a fresh-but-older DEX quote to that limit instead of
                # letting the scout say "fresh" while the gate says STALE_QUOTE
                age = cex_limit
        if getattr(self.portfolio.stress_flags, "feed_stale", False):
            age = max(age, stale_floor)
        # the edge the gate judges is the edge at THIS price, not the propose-time one: an adverse move on
        # either leg inside the PRICE_DRIFT band still lowers the expected edge one-for-one (bps of notional)
        expected_edge = float(plan.expected_edge_bps)
        if not plan.reduce_only:
            if plan.ref_dex_price > 0:
                expected_edge -= 1e4 * (new_dex - plan.ref_dex_price) / plan.ref_dex_price
            if plan.ref_perp_price > 0 and perp_ref:
                expected_edge += 1e4 * (perp_ref - plan.ref_perp_price) / plan.ref_perp_price
        return TradeProposal(
            plan_id=plan.id,
            symbol=plan.symbol,
            dex_side=dex_leg.side,
            perp_side=perp_leg.side,
            dex_qty=dex_leg.qty,
            perp_qty=perp_leg.qty,
            qty_step=self.filters.step_size,
            leverage=plan.leverage,
            notional=plan.notional_usd,
            allocated_risk=plan.allocated_risk_usd,
            roundtrip_cost_bps=plan.roundtrip_cost_bps,
            expected_edge_bps=expected_edge,
            quote_age_ms=age,
            price_drift_bps=drift,
            dex_ref_price=new_dex,
            perp_ref_price=perp_ref,
            reduce_only=plan.reduce_only,
            position_id=plan.position_id,
        )

    def _decide(self, gate_input: dict, plan_id: Optional[str], dry_run: bool) -> RiskDecisionRecord:
        d = self.gate.explain(gate_input)
        rec = RiskDecisionRecord.from_gate(d, plan_id=plan_id, mode=self.settings.mode, gate_snapshot=self._gate_snapshot(), dry_run=dry_run)
        self._record_decision(rec)
        return rec

    def _synthetic(self, code: str, reason: str, plan_id: Optional[str], dry_run: bool = False) -> RiskDecisionRecord:
        g = self._gate_snapshot()
        rec = RiskDecisionRecord(
            plan_id=plan_id, approved=False, code=code, reason=reason, latency_ns=0, dd_state=g.get("state", "NORMAL"),
            drawdown_pct=float(g.get("drawdown_pct", 0.0)), halted=bool(g.get("halted", False)), kill_switch=bool(g.get("kill_switch", False)),
            mode=self.settings.mode, dry_run=dry_run,
        )
        self._record_decision(rec)
        return rec

    def precheck(self, plan: HedgePlan) -> RiskDecisionRecord:
        """Dry-run gate.explain on the current market for a stored plan (nothing executes)."""
        ms = self.hub.snapshot()
        proposal = self.build_proposal(plan, ms, None)
        return self._decide(proposal.as_gate_input(), plan.id, dry_run=True)

    def dry_run(self, capital_usd: float, leverage: float, symbol: str) -> RiskDecisionRecord:
        """Size + explain without storing a plan (deltr_evaluate_risk).  Fail-closed:
        a missing leverage or market reaches the gate as MALFORMED, never as 1x."""
        ms = self.hub.snapshot()
        lev_ok = isinstance(leverage, (int, float)) and not isinstance(leverage, bool) and math.isfinite(leverage) and leverage > 0
        if not lev_ok or ms is None or ms.dex is None or ms.funding is None or capital_usd <= 0:
            probe = {"symbol": symbol, "leverage": leverage if lev_ok else None, "notional": None, "allocated_risk": 0.0,
                     "is_delta_neutral": True, "dex_side": "BUY", "perp_side": "SELL"}
            return self._decide(probe, None, dry_run=True)
        sizing = size_for_capital(capital_usd, float(leverage), ms.dex.exec_price_buy, self.filters, self.settings.max_notional_usd, self.settings.max_dex_impact_bps)
        if sizing.notional_usd <= 0:
            probe = {"symbol": symbol, "leverage": float(leverage), "notional": 0.0, "allocated_risk": 0.0, "is_delta_neutral": True,
                     "dex_side": "BUY", "perp_side": "SELL", "dex_qty": 0.0, "perp_qty": 0.0}
            return self._decide(probe, None, dry_run=True)
        edge = net_edge(ms, sizing.notional_usd, self.settings.funding_horizon_hours, self.settings)
        plan = self._draft_plan(symbol, ms, sizing, edge, leverage=float(leverage))
        proposal = self.build_proposal(plan, ms, None)
        return self._decide(proposal.as_gate_input(), None, dry_run=True)

    def _draft_plan(self, symbol: str, ms: MarketState, sizing: Any, edge: Any, leverage: float) -> HedgePlan:
        qty = float(sizing.base_qty)
        mark = perp_mark(ms) or 0.0
        dex_px = ms.dex.exec_price_buy if ms.dex is not None else 0.0
        return HedgePlan(
            symbol=symbol,
            legs=[
                OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=symbol, side=Side.BUY, qty=qty, price_hint=dex_px),
                OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=symbol, side=Side.SELL, qty=qty, price_hint=mark, leverage=leverage),
            ],
            qty=qty, notional_usd=sizing.notional_usd, leverage=leverage, margin_usd=sizing.margin_usd,
            cash_required_usd=sizing.cash_required_usd, allocated_risk_usd=edge.allocated_risk_usd,
            expected_edge_bps=edge.net_edge_bps, roundtrip_cost_bps=edge.roundtrip_cost_bps,
            ref_dex_price=dex_px, ref_perp_price=mark, source=TraceSource.API,
            expires_at=utcnow() + timedelta(seconds=self.settings.plan_ttl_seconds),
        )

    def _placeholder_plan(self, plan_id: str) -> HedgePlan:
        sym = self.settings.symbol_list[0]
        return HedgePlan(
            id=plan_id, symbol=sym,
            legs=[OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=sym, side=Side.BUY, qty=0.0, price_hint=0.0),
                  OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=sym, side=Side.SELL, qty=0.0, price_hint=0.0)],
            qty=0.0, notional_usd=0.0, leverage=self.settings.default_leverage, margin_usd=0.0, cash_required_usd=0.0,
            allocated_risk_usd=0.0, expected_edge_bps=0.0, roundtrip_cost_bps=0.0, ref_dex_price=0.0, ref_perp_price=0.0,
            expires_at=utcnow(),
        )

    async def _requote(self, qty: float, ms: Optional[MarketState]) -> Optional[DexQuote]:
        fn = getattr(self.router, "requote", None)
        if callable(fn):
            try:
                return await fn(qty, ms)
            except Exception as exc:
                log.warning("executor: re-quote failed: %s", exc)
        return ms.dex if ms is not None else None

    # ------------------------------------------------------------------ receipts
    def _finish(self, plan: HedgePlan, decision: RiskDecisionRecord, fills: List[Fill], status: ReceiptStatus, tr: _Trace,
                source: TraceSource, client: Optional[str], *, position_id: Optional[str] = None, residual: float = 0.0,
                realized_cost: float = 0.0, legging: Optional[int] = None, level: str = "info", message: str = "") -> ExecutionReceipt:
        tr.add("receipt", "ok" if status in ("filled", "unwound") else ("veto" if status == "vetoed" else "error"),
               message or f"receipt {status}", receipt_status=status, fills=len(fills))
        r = make_receipt(plan, decision, fills, status, tr.steps, self.settings.mode, source, client, position_id, residual,
                         realized_cost, legging, self._stress_label())
        self.receipts.put(r)
        self._emit("fill" if status in ("filled", "unwound") else "gate", f"{status}: {message or decision.reason}", level,
                   {"receipt_id": r.id, "plan_id": plan.id, "status": status, "code": decision.code, "position_id": position_id})
        return r

    # ------------------------------------------------------------------ execute
    async def execute(self, plan_id: str, confirm: bool, source: TraceSource, client: Optional[str] = None) -> ExecutionReceipt:
        async with self._lock:
            return await self._execute_locked(plan_id, confirm, source, client)

    async def _execute_locked(self, plan_id: str, confirm: bool, source: TraceSource, client: Optional[str]) -> ExecutionReceipt:
        tr = _Trace()
        mode = self.settings.mode
        peeked = self._peek(plan_id)
        if mode == Mode.TESTNET and not confirm:
            plan = peeked or self._placeholder_plan(plan_id)
            dec = self._synthetic("CONFIRM_REQUIRED", "VETO: TESTNET execution requires confirm=true (plan kept; call again with confirm).", plan_id)
            tr.add("gate", "veto", dec.reason, code=dec.code)
            return self._finish(plan, dec, [], "vetoed", tr, source, client, level="warn", message=dec.reason)
        status_fn = getattr(self.state, "plan_status", None)
        pre_status = status_fn(plan_id) if callable(status_fn) else None
        plan = self.state.take_plan(plan_id)
        if plan is None:
            if plan_id in self._consumed:
                code, reason = "PLAN_NOT_FOUND", f"plan {plan_id} was already executed (plans are single-use); propose again."
            elif pre_status == "expired" or (pre_status is None and peeked is not None):
                self._drop(plan_id)
                code, reason = "PLAN_EXPIRED", f"plan {plan_id} expired (TTL {self.settings.plan_ttl_seconds:g} s); propose again."
            else:
                code, reason = "PLAN_NOT_FOUND", f"plan {plan_id} not found; propose first."
            dec = self._synthetic(code, reason, plan_id)
            tr.add("plan", "veto", reason, code=code)
            return self._finish(peeked or self._placeholder_plan(plan_id), dec, [], "expired", tr, source, client, level="warn", message=reason)
        self._consumed.add(plan_id)
        tr.add("plan", "ok", f"plan {plan.id} taken (single-use): {plan.qty:g} {plan.symbol} notional ${plan.notional_usd:,.2f} at {plan.leverage:g}x",
               qty=plan.qty, notional_usd=plan.notional_usd, leverage=plan.leverage, plan_hash=plan.plan_hash)
        if plan.reduce_only and plan.position_id:
            pos = self.portfolio.get(plan.position_id)
            if pos is None:
                dec = self._synthetic("POSITION_NOT_FOUND", f"position {plan.position_id} is not open", plan.id)
                tr.add("gate", "veto", dec.reason, code=dec.code)
                return self._finish(plan, dec, [], "vetoed", tr, source, client, level="warn", message=dec.reason)
            return await self._unwind_with_plan(pos, plan, plan.prompt or "unwind", source, client, tr)

        ms = self.hub.snapshot()
        if ms is None:
            dec = self._synthetic("NO_MARKET_DATA", "VETO: no market snapshot yet; cannot re-price the plan.", plan.id)
            tr.add("scan", "error", dec.reason, code=dec.code)
            return self._finish(plan, dec, [], "failed", tr, source, client, level="error", message=dec.reason)
        requote = await self._requote(plan.qty, ms)
        proposal = self.build_proposal(plan, ms, requote)
        tr.add("scan", "ok", f"re-quoted DEX {plan.qty:g} @ {proposal.dex_ref_price:.4f} (drift {proposal.price_drift_bps:.2f} bps); perp mark {proposal.perp_ref_price:.4f}",
               dex_ref_price=proposal.dex_ref_price, perp_ref_price=proposal.perp_ref_price, price_drift_bps=proposal.price_drift_bps, quote_age_ms=proposal.quote_age_ms)
        decision = self._decide(proposal.as_gate_input(), plan.id, dry_run=False)
        tr.add("gate", "ok" if decision.approved else "veto", decision.reason, code=decision.code, latency_us=decision.latency_us,
               observed=decision.observed, limit=decision.limit, checks=[c.model_dump(mode="json") for c in decision.checks])
        if not decision.approved:
            return self._finish(plan, decision, [], "vetoed", tr, source, client, level="warn", message=decision.reason)
        try:
            self.portfolio.reserve(plan.cash_required_usd)
        except ValueError as exc:
            dec = self._synthetic("INSUFFICIENT_CASH", f"VETO: {exc}", plan.id)
            tr.add("error", "error", dec.reason, code=dec.code)
            return self._finish(plan, dec, [], "failed", tr, source, client, level="error", message=dec.reason)

        dex_leg, perp_leg = self.legs_of(plan)
        order = [dex_leg, perp_leg] if self.settings.leg_order == LegOrder.DEX_FIRST else [perp_leg, dex_leg]
        fills: List[Fill] = []
        # ---- leg 1
        try:
            fill1 = await self.router.fill(order[0], ms, None, plan.id, 1)
        except LegError as exc:
            self.portfolio.release(plan.cash_required_usd)
            tr.add("error", "error", f"first leg ({order[0].venue.value} {order[0].side.value}) failed: {exc.reason}; nothing to reverse", leg=order[0].venue.value, reason=exc.reason)
            return self._finish(plan, decision, [], "failed", tr, source, client, level="error", message=f"first leg failed: {exc.reason}")
        fills.append(fill1)
        tr.add(self._fill_step(fill1), "ok", self._fill_summary(fill1), **self._fill_data(fill1))
        # ---- leg 2 sized from the first fill
        try:
            fill2 = await self.router.fill(order[1], ms, fill1.qty, plan.id, 1)
        except LegError as exc:
            tr.add("error", "error", f"second leg ({order[1].venue.value} {order[1].side.value}) failed: {exc.reason}; reversing first leg", leg=order[1].venue.value, reason=exc.reason)
            return await self._reverse_first(plan, decision, fill1, ms, tr, source, client)
        fills.append(fill2)
        tr.add(self._fill_step(fill2), "ok", self._fill_summary(fill2), **self._fill_data(fill2))
        # ---- residual handling
        eff1 = fill1
        residual = float(Decimal(str(fill1.qty)) - Decimal(str(fill2.qty)))  # never float subtraction on lot sizes
        if residual > self.filters.step_size - 1e-12:
            try:
                rev = await self.router.reverse_partial(order[0], floor_to_step(residual, self.filters.step_size), ms, plan.id)
                fills.append(rev)
                eff1 = fill1.model_copy(update={"qty": fill1.qty - rev.qty})
                self.portfolio.book_realized(-(_reversal_cost(fill1.model_copy(update={"qty": rev.qty}), rev)), rev.fee_usd, reason=f"residual {rev.qty:g} reversed on {plan.id}")
                tr.add(self._fill_step(rev), "ok", f"residual {rev.qty:g} reversed on {rev.venue.value} @ {rev.price:.4f}", **self._fill_data(rev))
            except LegError as exc:
                tr.add("error", "error", f"residual {residual:g} could not be reversed: {exc.reason}", reason=exc.reason)
        fill_dex, fill_perp = (eff1, fill2) if order[0].venue == Venue.PANCAKESWAP_V3 else (fill2, eff1)
        pos = self.portfolio.open_position(plan, [fill_dex, fill_perp])
        tr.add("position", "ok", f"position {pos.id} open: {pos.perp_qty:g} {pos.symbol} delta {pos.delta_base:+.4f}, stop distance ${pos.stop_distance_usd:.2f}",
               position_id=pos.id, delta_base=pos.delta_base, unrealized_pnl_usd=pos.unrealized_pnl_usd, stop_distance_usd=pos.stop_distance_usd)
        window = legging_window_ms(fill1, fill2)
        return self._finish(plan, decision, fills, "filled", tr, source, client, position_id=pos.id, residual=pos.delta_base,
                            realized_cost=sum(f.fee_usd for f in fills), legging=window, message=f"filled {pos.perp_qty:g} {pos.symbol}; legging window {window} ms")

    async def _reverse_first(self, plan: HedgePlan, decision: RiskDecisionRecord, fill1: Fill, ms: MarketState, tr: _Trace,
                             source: TraceSource, client: Optional[str]) -> ExecutionReceipt:
        try:
            rev = await self.router.reverse(fill1, ms, plan.id)
        except LegError as exc:
            # the naked leg is booked (one-legged position: marked, stop-monitored, registered with the gate),
            # the reservation stays locked in it and the kill switch stops every new entry until an operator
            # unwinds it — never a flat book hiding an open order
            pos = self.portfolio.open_naked_leg(plan, fill1)
            self.gate.set_kill_switch(True)
            tr.add("error", "error", f"REVERSAL FAILED: {fill1.qty:g} {fill1.symbol} {fill1.side.value} on {fill1.venue.value} is still open — booked as naked leg {pos.id}; kill switch ENGAGED; manual action required ({exc.reason})", reason=exc.reason, position_id=pos.id)
            self._emit("fill", f"unhedged leg left open on {fill1.venue.value}: {fill1.qty:g} {fill1.symbol} (naked position {pos.id}; kill switch engaged)", "error", {"plan_id": plan.id, "position_id": pos.id})
            self._emit("gate", "kill switch ENGAGED: naked leg after a failed reversal", "error", {"kill_switch": True, "position_id": pos.id})
            return self._finish(plan, decision, [fill1], "failed", tr, source, client, position_id=pos.id, residual=pos.delta_base,
                                level="error", message="second leg failed and the first leg could NOT be reversed")
        cost = _reversal_cost(fill1, rev)
        self.portfolio.book_realized(-cost, fill1.fee_usd + rev.fee_usd, reason=f"reverse-on-failure {plan.id}")
        self.portfolio.release(plan.cash_required_usd)
        tr.add(self._fill_step(rev), "ok", f"reversed {rev.qty:g} on {rev.venue.value} @ {rev.price:.4f}; realized cost ${cost:.4f}", **self._fill_data(rev))
        return self._finish(plan, decision, [fill1, rev], "unwound", tr, source, client, realized_cost=cost, level="warn",
                            message=f"second leg failed; first leg reversed at a cost of ${cost:.4f}")

    # ------------------------------------------------------------------ unwind
    async def unwind(self, position_id: str, reason: str, source: TraceSource, client: Optional[str] = None, confirm: bool = False) -> ExecutionReceipt:
        async with self._lock:
            return await self._unwind_locked(position_id, reason, source, client, confirm)

    async def _unwind_locked(self, position_id: str, reason: str, source: TraceSource, client: Optional[str], confirm: bool) -> ExecutionReceipt:
        pos = self.portfolio.get(position_id)
        if pos is None:
            raise PositionNotFound(position_id)
        tr = _Trace()
        ms = self.hub.snapshot()
        plan = self._unwind_plan(pos, ms, source, client, reason)
        tr.add("plan", "ok", f"unwind plan for {pos.id}: perp reduce-only BUY {pos.perp_qty:g} then DEX SELL {pos.dex_qty:g} ({reason})",
               position_id=pos.id, reason=reason, plan_hash=plan.plan_hash)
        if self.settings.mode == Mode.TESTNET and not confirm and source != TraceSource.AUTO:
            dec = self._synthetic("CONFIRM_REQUIRED", "VETO: TESTNET unwind requires confirm=true.", plan.id)
            tr.add("gate", "veto", dec.reason, code=dec.code)
            return self._finish(plan, dec, [], "vetoed", tr, source, client, position_id=pos.id, level="warn", message=dec.reason)
        return await self._unwind_with_plan(pos, plan, reason, source, client, tr)

    async def _unwind_with_plan(self, pos: Position, plan: HedgePlan, reason: str, source: TraceSource, client: Optional[str], tr: _Trace) -> ExecutionReceipt:
        ms = self.hub.snapshot()
        requote = await self._requote(pos.dex_qty, ms) if ms is not None and pos.dex_qty > 0 else None
        proposal = self.build_proposal(plan, ms, requote)
        gate_input = proposal.as_gate_input()
        one_legged = pos.perp_qty <= 0.0 or pos.dex_qty <= 0.0
        if one_legged:
            # a naked leg has no paired legs: the gate verifies the reduce-only binding against its own
            # registry (position_id, qty <= registered qty) instead of the leg pair
            gate_input.pop("dex_qty", None)
            gate_input.pop("perp_qty", None)
        decision = self._decide(gate_input, plan.id, dry_run=False)
        tr.add("gate", "ok" if decision.approved else "veto", decision.reason, code=decision.code, latency_us=decision.latency_us,
               checks=[c.model_dump(mode="json") for c in decision.checks])
        if not decision.approved:
            return self._finish(plan, decision, [], "vetoed", tr, source, client, position_id=pos.id, level="warn", message=decision.reason)
        if ms is None:
            dec = self._synthetic("NO_MARKET_DATA", "unwind needs a market snapshot", plan.id)
            tr.add("error", "error", dec.reason)
            return self._finish(plan, dec, [], "failed", tr, source, client, position_id=pos.id, level="error", message=dec.reason)
        dex_leg, perp_leg = self.legs_of(plan)
        # ---- perp reduce-only BUY first (certain, cheap); skipped when the perp leg is already gone
        perp_fill: Optional[Fill] = None
        if pos.perp_qty > 0.0:
            try:
                perp_fill = await self.router.fill(perp_leg, ms, pos.perp_qty, plan.id, 1)
            except LegError as exc:
                tr.add("error", "error", f"perp reduce-only leg failed: {exc.reason}; position {pos.id} stays OPEN", reason=exc.reason)
                self._emit("position", f"unwind of {pos.id} failed on the perp leg: {exc.reason}", "error", {"position_id": pos.id})
                return self._finish(plan, decision, [], "failed", tr, source, client, position_id=pos.id, level="error", message=f"perp leg failed: {exc.reason}")
            tr.add("cex_fill", "ok", self._fill_summary(perp_fill), **self._fill_data(perp_fill))
        # ---- DEX sell sized from the perp fill; skipped when the DEX leg is already gone
        dex_fill: Optional[Fill] = None
        if pos.dex_qty > 0.0:
            dex_qty = min(perp_fill.qty, pos.dex_qty) if perp_fill is not None else pos.dex_qty
            try:
                dex_fill = await self.router.fill(dex_leg, ms, dex_qty, plan.id, 1)
            except LegError as exc:
                if perp_fill is None:
                    tr.add("error", "error", f"DEX leg failed: {exc.reason}; naked DEX leg {pos.id} stays OPEN", reason=exc.reason)
                    self._emit("position", f"unwind of naked leg {pos.id} failed on the DEX leg: {exc.reason}", "error", {"position_id": pos.id})
                    return self._finish(plan, decision, [], "failed", tr, source, client, position_id=pos.id, level="error", message=f"DEX leg failed: {exc.reason}")
                tr.add("error", "error", f"DEX leg failed: {exc.reason}; re-establishing the perp short", reason=exc.reason)
                try:
                    rev = await self.router.reverse(perp_fill, ms, plan.id)
                except LegError as exc2:
                    # the perp IS closed: book that leg so the position becomes a naked DEX long (marked,
                    # stop-monitored, still registered) and stop new entries until an operator resolves it
                    half = self.portfolio.close_position(pos.id, [perp_fill], f"{reason}:perp_leg_only")
                    self.gate.set_kill_switch(True)
                    self._emit("position", f"HALF-CLOSED BOOK on {pos.id}: perp closed, DEX still long {half.dex_qty:g} — kill switch ENGAGED, manual action required ({exc2.reason})", "error", {"position_id": pos.id})
                    self._emit("gate", "kill switch ENGAGED: half-closed book after a failed unwind", "error", {"kill_switch": True, "position_id": pos.id})
                    tr.add("error", "error", f"perp re-short failed too ({exc2.reason}); book is half-closed: naked DEX long {half.dex_qty:g} booked, kill switch ENGAGED — manual action required", reason=exc2.reason)
                    return self._finish(plan, decision, [perp_fill], "failed", tr, source, client, position_id=pos.id, residual=half.delta_base, level="error",
                                        message="DEX leg failed and the perp could not be re-shorted")
                cost = _reversal_cost(perp_fill, rev)
                self.portfolio.book_realized(-cost, perp_fill.fee_usd + rev.fee_usd, reason=f"unwind reversal {pos.id}")
                tr.add("cex_fill", "ok", f"perp re-shorted {rev.qty:g} @ {rev.price:.4f}; realized cost ${cost:.4f}; position stays OPEN", **self._fill_data(rev))
                self._emit("position", f"unwind of {pos.id} failed on the DEX leg; hedge restored at a cost of ${cost:.4f}", "error", {"position_id": pos.id})
                return self._finish(plan, decision, [perp_fill, rev], "failed", tr, source, client, position_id=pos.id, realized_cost=cost, level="error",
                                    message=f"DEX leg failed: {exc.reason}; hedge restored")
            tr.add("dex_fill", "ok", self._fill_summary(dex_fill), **self._fill_data(dex_fill))
        fills: List[Fill] = [f for f in (perp_fill, dex_fill) if f is not None]
        closed = self.portfolio.close_position(pos.id, fills, reason)
        tr.add("unwind", "ok", f"position {pos.id} {'closed' if closed.status == 'closed' else 'reduced'} ({reason}); realized ${closed.realized_pnl_usd:.4f}",
               position_id=pos.id, realized_pnl_usd=closed.realized_pnl_usd, position_status=closed.status, reason=reason)
        window = legging_window_ms(perp_fill, dex_fill) if perp_fill is not None and dex_fill is not None else 0
        return self._finish(plan, decision, fills, "unwound", tr, source, client, position_id=pos.id,
                            residual=closed.delta_base, realized_cost=sum(f.fee_usd for f in fills), legging=window,
                            message=f"unwound {pos.id} ({reason}); realized ${closed.realized_pnl_usd:.4f}")

    def _unwind_plan(self, pos: Position, ms: Optional[MarketState], source: TraceSource, client: Optional[str], reason: str) -> HedgePlan:
        fn = getattr(self.hedger, "unwind_plan", None)
        if fn is None:
            try:  # the Hedger object may not expose it; the module function is the contract (section 4.8)
                from agents.hedger import unwind_plan as fn  # type: ignore[no-redef]
            except Exception:
                fn = None
        one_legged = pos.perp_qty <= 0.0 or pos.dex_qty <= 0.0
        if callable(fn) and ms is not None and not one_legged:
            try:
                return fn(pos, ms, self.settings, source, client, reason)
            except Exception as exc:
                log.warning("executor: hedger.unwind_plan failed (%s); building a local unwind plan", exc)
        dex_sell = ms.dex.exec_price_sell if ms is not None and ms.dex is not None else pos.dex_entry
        mark = perp_mark(ms) or pos.perp_entry
        _, perp_buy = perp_reference(mark, self.settings.perp_slippage_bps)
        rt = 2.0 * (self.settings.perp_taker_fee_bps + self.settings.perp_slippage_bps + self.settings.dex_fee_tier / 100.0)
        # a naked leg is sized from the leg that is still open (the hedger sizes from the perp leg, which may be 0)
        live_qty = max(pos.dex_qty, pos.perp_qty)
        live_px = dex_sell if pos.dex_qty > 0.0 else mark
        notional = max(pos.notional_usd, live_qty * live_px) if one_legged else pos.notional_usd
        return HedgePlan(
            symbol=pos.symbol,
            legs=[
                OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=pos.symbol, side=Side.SELL, qty=pos.dex_qty, price_hint=dex_sell),
                OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=pos.symbol, side=Side.BUY, qty=pos.perp_qty, price_hint=perp_buy, reduce_only=True, leverage=pos.leverage),
            ],
            qty=live_qty, notional_usd=notional, leverage=pos.leverage, margin_usd=pos.margin_usd, cash_required_usd=0.0,
            allocated_risk_usd=pos.allocated_risk_usd, expected_edge_bps=0.0, roundtrip_cost_bps=rt, ref_dex_price=dex_sell,
            ref_perp_price=mark, reduce_only=True, position_id=pos.id, source=source, client=client, prompt=f"unwind:{reason}",
            expires_at=utcnow() + timedelta(seconds=self.settings.plan_ttl_seconds),
        )

    async def unwind_all(self, reason: str, source: TraceSource, client: Optional[str] = None, confirm: bool = False) -> List[ExecutionReceipt]:
        out: List[ExecutionReceipt] = []
        for pos in list(self.portfolio.positions("open")):
            out.append(await self.unwind(pos.id, reason, source, client, confirm))
        return out

    # ------------------------------------------------------------------ stop monitor
    async def check_stops_once(self, source: TraceSource = TraceSource.AUTO) -> List[ExecutionReceipt]:
        """Unwind every open position whose stop / funding-flip rule fires."""
        out: List[ExecutionReceipt] = []
        for pos in list(self.portfolio.positions("open")):
            trigger = self.portfolio.stop_breached(pos)
            if trigger:
                self._emit("position", f"{trigger} on {pos.id}: stop distance ${pos.stop_distance_usd:.2f}", "warn", {"position_id": pos.id})
                try:
                    out.append(await self.unwind(pos.id, trigger, source, None, confirm=True))
                except PositionNotFound:
                    continue
        return out

    async def run_stop_monitor(self, stop: asyncio.Event) -> None:
        interval = float(self.settings.poll_interval_cex_s)
        while not stop.is_set():
            try:
                await self.check_stops_once()
            except Exception as exc:  # never let the monitor die silently
                log.error("stop monitor: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------ formatting
    @staticmethod
    def _fill_step(f: Fill) -> StepName:
        return "dex_fill" if f.venue == Venue.PANCAKESWAP_V3 else "cex_fill"

    @staticmethod
    def _fill_summary(f: Fill) -> str:
        tag = "simulated" if f.simulated else f"order {f.ref}"
        div = f" (vs ref {f.reference_divergence_bps:+.2f} bps)" if f.reference_divergence_bps is not None else ""
        return f"{f.venue.value} {f.side.value} {f.qty:g} @ {f.price:.4f} fee ${f.fee_usd:.4f} [{tag}]{div}"

    @staticmethod
    def _fill_data(f: Fill) -> dict:
        return {"venue": f.venue.value, "side": f.side.value, "qty": f.qty, "price": f.price, "fee_usd": f.fee_usd, "ref": f.ref,
                "simulated": f.simulated, "source": f.source.value, "client_id": f.client_id, "attempt": f.attempt,
                "reference_divergence_bps": f.reference_divergence_bps, "latency_ms": f.latency_ms}


__all__ = [
    "LegError", "PositionNotFound", "Router", "PaperRouter", "TestnetRouter", "Executor",
    "client_order_id", "legging_window_ms", "leg_index_of", "perp_mark",
]
