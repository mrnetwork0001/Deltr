"""Offline fakes for the portfolio / executor / receipts / stress tests (agent D).

Kept out of conftest.py on purpose (the integrator owns that file).  Everything
here is deterministic and network-free.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from risk_gate import BinanceRiskGate

from deltr.config import Mode, Settings
from deltr.executor import DEX_LEG, PERP_LEG, Executor, LegError, client_order_id
from deltr.models import (
    AgentEvent,
    DataSource,
    DexQuote,
    Fill,
    Freshness,
    FundingSnapshot,
    HedgePlan,
    MarketState,
    OrderLeg,
    Quote,
    Side,
    SymbolFilters,
    TraceSource,
    Venue,
    utcnow,
)
from deltr.portfolio import Portfolio
from deltr.receipts import ReceiptStore

SYMBOL = "BNBUSDT"
FILTERS = SymbolFilters(symbol=SYMBOL, step_size=0.01, min_qty=0.01, tick_size=0.01, min_notional=5.0, price_precision=3, qty_precision=2)
POOL = "0x172fcD41E0913e95784454622d1c3724f546f849"


# --------------------------------------------------------------------------- settings
def make_settings(tmp_path: Any, mode: str = "paper", **over: Any) -> Settings:
    kw: Dict[str, Any] = {
        "DELTR_MODE": mode,
        "DELTR_STATE_DIR": str(tmp_path),
        "DELTR_MIN_EDGE_BPS": 0.0,
        "DELTR_CAPITAL_USD": 10_000.0,
    }
    if mode == "testnet":
        kw.update(BINANCE_API_KEY="fake-testnet-key", BINANCE_SECRET_KEY="fake-testnet-secret")
    kw.update(over)
    return Settings(**kw)


# --------------------------------------------------------------------------- market
def make_market(
    *,
    dex_buy: float = 686.19,
    dex_sell: float = 686.05,
    mid: float = 686.10,
    mark: float = 686.34,
    rate: float = 0.0001,
    next_funding_ms: Optional[int] = None,
    ts: Optional[datetime] = None,
    ages: Tuple[int, int, int] = (120, 800, 150),
    ok: bool = True,
    reason: Optional[str] = None,
    bid: float = 686.30,
    ask: float = 686.40,
    size: float = 4.85,
) -> MarketState:
    ts = ts or utcnow()
    if next_funding_ms is None:
        next_funding_ms = int(ts.timestamp() * 1000) + 3_600_000
    gas_usd = 150_000 * 50_000_000 / 1e18 * mid
    dex = DexQuote(
        pool=POOL, fee_tier=100, fee_bps=1.0, sqrt_price_x96=3024721431835227127309627620, tick=-9000, mid_price=mid,
        size_base=size, exec_price_buy=dex_buy, amount_in_usdt=dex_buy * size, exec_price_sell=dex_sell,
        amount_out_usdt=dex_sell * size, impact_bps=max(0.0, (dex_buy / mid - 1) * 1e4 - 1.0), gas_units=150_000,
        gas_price_wei=50_000_000, gas_usd=gas_usd, block=60_000_000, ts=ts,
    )
    funding = FundingSnapshot(
        symbol=SYMBOL, mark_price=mark, index_price=mark - 0.2, last_funding_rate=rate, next_funding_time_ms=next_funding_ms,
        interval_h=8, annualized_pct=rate * 3 * 365 * 100, ts=ts,
    )
    book = Quote(venue=Venue.BINANCE_FUTURES, symbol=SYMBOL, bid=bid, ask=ask, ts=ts, source=DataSource.BINANCE_FUTURES_TESTNET)
    fresh = Freshness(cex_age_ms=ages[0], dex_age_ms=ages[1], spot_age_ms=ages[2], ok=ok, reason=reason)
    return MarketState(symbol=SYMBOL, dex=dex, cex_perp_book=book, cex_spot_ref=None, funding=funding, perp_ref_price=mark, freshness=fresh, ts=ts)


def make_plan(ms: MarketState, *, qty: float = 4.85, leverage: float = 2.0, expected_edge: float = 5.0, ttl_s: float = 60.0,
              source: TraceSource = TraceSource.API, **over: Any) -> HedgePlan:
    dex_px = ms.dex.exec_price_buy
    mark = ms.perp_ref_price or ms.funding.mark_price
    notional = qty * dex_px
    margin = notional / leverage
    rt = 16.6
    body = dict(
        symbol=SYMBOL,
        legs=[
            OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=SYMBOL, side=Side.BUY, qty=qty, price_hint=dex_px),
            OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=SYMBOL, side=Side.SELL, qty=qty, price_hint=mark, leverage=leverage),
        ],
        qty=qty, notional_usd=notional, leverage=leverage, margin_usd=margin, cash_required_usd=notional + margin,
        allocated_risk_usd=notional * (rt + 100.0) / 1e4, expected_edge_bps=expected_edge, roundtrip_cost_bps=rt,
        ref_dex_price=dex_px, ref_perp_price=mark, source=source, expires_at=utcnow() + timedelta(seconds=ttl_s),
    )
    body.update(over)
    return HedgePlan(**body)


# --------------------------------------------------------------------------- state / hub
class FakeState:
    """Minimal State (section 4.2 surface used by agent D's modules)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = settings.mode
        self.replay = False
        self.started_at = utcnow()
        self.market: Optional[MarketState] = None
        self.plans: Dict[str, HedgePlan] = {}
        self.positions: Dict[str, Any] = {}
        self.portfolio: Any = None
        self.decisions: deque = deque(maxlen=100)
        self.receipts: deque = deque(maxlen=10)
        self.events: List[AgentEvent] = []
        self.stress_active: Optional[str] = None

    def put_plan(self, plan: HedgePlan) -> None:
        self.plans[plan.id] = plan

    def take_plan(self, plan_id: str, now: Optional[datetime] = None) -> Optional[HedgePlan]:
        p = self.plans.get(plan_id)
        if p is None or p.expires_at <= (now or utcnow()):
            return None
        return self.plans.pop(plan_id)

    def peek_plan(self, plan_id: str) -> Optional[HedgePlan]:
        return self.plans.get(plan_id)

    def prune_plans(self, now: Optional[datetime] = None) -> int:
        now = now or utcnow()
        dead = [k for k, p in self.plans.items() if p.expires_at <= now]
        for k in dead:
            self.plans.pop(k)
        return len(dead)

    def record_decision(self, d: Any) -> None:
        self.decisions.append(d)

    def record_receipt(self, r: Any) -> None:
        self.receipts.append(r)

    def emit(self, topic: str, message: str, level: str = "info", data: Optional[dict] = None) -> AgentEvent:
        ev = AgentEvent(topic=topic, message=message, level=level, data=data or {})
        self.events.append(ev)
        return ev


class FakeHub:
    def __init__(self, ms: Optional[MarketState]) -> None:
        self.ms = ms
        self.stale = False

    def snapshot(self) -> Optional[MarketState]:
        return self.ms

    def set(self, ms: MarketState) -> None:
        self.ms = ms

    def freshness(self) -> Freshness:
        return self.ms.freshness

    def health(self) -> list:
        return []

    def set_feed_stale(self, on: bool) -> None:
        self.stale = on

    async def tick_once(self) -> MarketState:
        return self.ms


class FakeDex:
    """PancakeV3Client stand-in: `dex_quote(size)` returns the template quote scaled to `size`."""

    def __init__(self, template: DexQuote, drift: float = 1.0, fail: bool = False) -> None:
        self.template = template
        self.drift = drift
        self.fail = fail
        self.calls: List[float] = []

    async def dex_quote(self, size_base: float) -> DexQuote:
        self.calls.append(size_base)
        if self.fail:
            raise RuntimeError("rpc down")
        t = self.template
        return t.model_copy(update={
            "size_base": size_base, "exec_price_buy": t.exec_price_buy * self.drift, "exec_price_sell": t.exec_price_sell * self.drift,
            "amount_in_usdt": t.exec_price_buy * self.drift * size_base, "amount_out_usdt": t.exec_price_sell * self.drift * size_base,
        })


# --------------------------------------------------------------------------- router
class FakeRouter:
    """Scripted router: records the exact call sequence; fails / partially fills on demand."""

    mode = Mode.PAPER

    def __init__(self, *, fail: Tuple[Tuple[Venue, Side], ...] = (), partial: Optional[Dict[Venue, float]] = None,
                 fail_reverse: bool = False, delay_s: float = 0.0, requote: Optional[DexQuote] = None) -> None:
        self.fail = set(fail)
        self.partial = partial or {}
        self.fail_reverse = fail_reverse
        self.delay_s = delay_s
        self.requote_quote = requote
        self.calls: List[Tuple[str, Venue, Side, float]] = []
        self.stress: Any = None
        self.active = 0
        self.max_active = 0

    async def requote(self, qty: float, ms: MarketState) -> DexQuote:
        return self.requote_quote or ms.dex

    def _price(self, venue: Venue, side: Side, ms: MarketState) -> float:
        if venue == Venue.PANCAKESWAP_V3:
            return ms.dex.exec_price_buy if side == Side.BUY else ms.dex.exec_price_sell
        mark = ms.perp_ref_price
        return mark * (1 - 0.0002) if side == Side.SELL else mark * (1 + 0.0002)

    def _fill(self, venue: Venue, side: Side, q: float, ms: MarketState, plan_id: str, attempt: int, ref: str) -> Fill:
        price = self._price(venue, side, ms)
        idx = DEX_LEG if venue == Venue.PANCAKESWAP_V3 else PERP_LEG
        fee = ms.dex.gas_usd if idx == DEX_LEG else q * price * 5.0 / 1e4
        return Fill(leg_index=idx, venue=venue, symbol=SYMBOL, side=side, qty=q, price=price, fee_usd=fee, ref=ref, simulated=True,
                    source=DataSource.PAPER, client_id=client_order_id(plan_id, idx, attempt) if idx == PERP_LEG else None, attempt=attempt)

    async def _slow(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
        finally:
            self.active -= 1

    async def fill(self, leg: OrderLeg, ms: MarketState, qty: Optional[float] = None, plan_id: str = "", attempt: int = 1) -> Fill:
        q = float(qty if qty is not None else leg.qty)
        self.calls.append(("fill", leg.venue, leg.side, q))
        await self._slow()
        if (leg.venue, leg.side) in self.fail:
            raise LegError("scripted failure", leg_index=DEX_LEG if leg.venue == Venue.PANCAKESWAP_V3 else PERP_LEG, venue=leg.venue)
        if leg.venue == Venue.PANCAKESWAP_V3 and self.stress is not None and getattr(self.stress, "dex_leg_fail", False):
            self.stress.dex_leg_fail = False
            raise LegError("sim:dex_leg_fail(stress)", leg_index=DEX_LEG, venue=leg.venue)
        if leg.venue in self.partial:
            q = min(q, self.partial[leg.venue])
        return self._fill(leg.venue, leg.side, q, ms, plan_id, attempt, "fake")

    async def reverse(self, fill: Fill, ms: MarketState, plan_id: str) -> Fill:
        side = Side.SELL if fill.side == Side.BUY else Side.BUY
        self.calls.append(("reverse", fill.venue, side, fill.qty))
        if self.fail_reverse:
            raise LegError("scripted reverse failure", venue=fill.venue)
        return self._fill(fill.venue, side, fill.qty, ms, plan_id, fill.attempt + 1, "fake:reverse")

    async def reverse_partial(self, leg: OrderLeg, residual_qty: float, ms: MarketState, plan_id: str) -> Fill:
        side = Side.SELL if leg.side == Side.BUY else Side.BUY
        self.calls.append(("reverse_partial", leg.venue, side, residual_qty))
        return self._fill(leg.venue, side, residual_qty, ms, plan_id, 2, "fake:reverse_partial")


# --------------------------------------------------------------------------- futures (testnet router)
@dataclass(frozen=True)
class FakeOrderResult:
    order_id: int
    client_id: str
    status: str
    executed_qty: float
    avg_price: float
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FakeAccountPrep:
    dual_side: bool
    position_side: str
    margin_type: str
    leverage: int
    usdt_balance: float


class FakeFuturesError(Exception):
    def __init__(self, code: int, msg: str, retryable: bool = False) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.retryable = retryable


class FakeFutures:
    """FuturesClient stand-in.  `script` is a list of outcomes consumed per
    place_limit_ioc call: ("fill", qty) | ("zero",) | ("error", code) | ("timeout", known_qty|None)."""

    def __init__(self, script: Optional[List[tuple]] = None, base_url: str = "https://testnet.binancefuture.com", dual: bool = False) -> None:
        self.base_url = base_url
        self.script = list(script or [])
        self.orders: List[dict] = []
        self.lookups: List[str] = []
        self.known: Dict[str, FakeOrderResult] = {}
        self.synced = 0
        self.prep_calls = 0
        self.dual = dual

    @property
    def has_credentials(self) -> bool:
        return True

    async def sync_time(self) -> int:
        self.synced += 1
        return 74

    async def prepare_account(self, symbol: str, leverage: int, isolated: bool = True) -> FakeAccountPrep:
        self.prep_calls += 1
        return FakeAccountPrep(dual_side=self.dual, position_side="SHORT" if self.dual else "BOTH", margin_type="ISOLATED", leverage=leverage, usdt_balance=15_000.0)

    async def place_limit_ioc(self, symbol: str, side: Side, qty: float, price: float, client_id: str, reduce_only: bool = False, position_side: str = "BOTH") -> FakeOrderResult:
        self.orders.append({"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "IOC", "qty": qty, "price": price,
                            "client_id": client_id, "reduce_only": reduce_only, "position_side": position_side})
        outcome = self.script.pop(0) if self.script else ("fill", qty)
        oid = 1000 + len(self.orders)
        kind = outcome[0]
        if kind == "fill":
            return FakeOrderResult(oid, client_id, "FILLED", float(outcome[1]), price)
        if kind == "zero":
            return FakeOrderResult(oid, client_id, "EXPIRED", 0.0, 0.0)
        if kind == "error":
            raise FakeFuturesError(int(outcome[1]), "scripted", retryable=outcome[1] in (-1021, -4061))
        if kind == "timeout":
            if outcome[1] is not None:
                self.known[client_id] = FakeOrderResult(oid, client_id, "FILLED", float(outcome[1]), price)
            raise TimeoutError("scripted timeout")
        raise AssertionError(f"unknown script outcome {outcome}")

    async def get_order_by_client_id(self, symbol: str, client_id: str) -> Optional[FakeOrderResult]:
        self.lookups.append(client_id)
        return self.known.get(client_id)


# --------------------------------------------------------------------------- stack
def make_gate(settings: Settings) -> BinanceRiskGate:
    return BinanceRiskGate(capital_usd=settings.capital_usd, limits=settings.risk_limits(), mode=settings.mode.value)


def make_stack(tmp_path: Any, *, mode: str = "paper", ms: Optional[MarketState] = None, router: Any = None, persist: bool = False, **over: Any) -> SimpleNamespace:
    settings = make_settings(tmp_path, mode, **over)
    ms = ms or make_market()
    state = FakeState(settings)
    state.market = ms
    gate = make_gate(settings)
    portfolio = Portfolio(state, gate, settings, settings.portfolio_state_path if persist else None)
    hub = FakeHub(ms)
    receipts = ReceiptStore(state, settings.receipts_path if persist else None)
    router = router or FakeRouter()
    executor = Executor(state, gate, portfolio, router, hub, None, settings, receipts, FILTERS)
    portfolio.mark(ms)
    return SimpleNamespace(settings=settings, state=state, gate=gate, portfolio=portfolio, hub=hub, receipts=receipts, router=router, executor=executor, ms=ms)


async def open_via_execute(stack: SimpleNamespace, **plan_over: Any) -> Tuple[Any, Any]:
    plan = make_plan(stack.ms, **plan_over)
    stack.state.put_plan(plan)
    receipt = await stack.executor.execute(plan.id, True, TraceSource.API, "pytest")
    return plan, receipt


__all__ = [n for n in dir() if not n.startswith("_") or n == "_fakes"]
