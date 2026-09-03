"""tests/test_live_mode.py — LIVE mode, maker execution and the agentic-wallet on-chain leg.

Nothing in this file places a real order, broadcasts a transaction, or invokes ``baw``.  The
perp venue is a scripted fake and the wallet is a fake CLI object; the one thing that would
touch a real venue is marked ``@pytest.mark.live`` and skipped by default.

What is asserted:

* the maker path posts ``timeInForce=GTX`` at the touch on its own side and NEVER crosses;
* a post-only rejection re-prices instead of crossing;
* a maker order that does not fill is a **failure** that the Executor reverses, never a
  silent one-legged trade and never a fallback to a taker order;
* the on-chain leg goes through the wallet, produces a Fill whose ``ref`` is the transaction
  hash and whose ``source`` says the wallet routed it, and refuses an unconfirmed swap;
* reverse-on-failure covers a REAL on-chain leg, and a failed reversal books a naked leg and
  engages the kill switch;
* the risk gate is in front of LIVE exactly as it is in front of every other mode;
* LIVE refuses to start when any single requirement is missing, naming it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from deltr.config import LIVE_ACK_PHRASE, ONCHAIN_ACK_PHRASE, ExecutionStyle, LegOrder, Mode, Settings
from deltr.executor import Executor, LegError, LiveRouter
from deltr.maker import MakerError, MakerExecution, maker_price, walked_away
from deltr.models import DataSource, Quote, Side, TraceSource, Venue, utcnow
from deltr.onchain_leg import OnchainLegError, WalletDexLeg, tokens_for
from deltr.portfolio import Portfolio
from deltr.receipts import ReceiptStore
from deltr.venues.agentic_wallet import AgenticWalletError, SwapQuote, SwapResult
from deltr.venues.binance_futures import FuturesError
from tests._fakes_d import FILTERS, FakeHub, FakeState, make_gate, make_market, make_plan

MAINNET = "https://fapi.binance.com"

LIVE_ENV = dict(
    BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real", BINANCE_API_ENV="mainnet",
    DELTR_LIVE_ACK=LIVE_ACK_PHRASE, DELTR_ONCHAIN_MODE="live", DELTR_ONCHAIN_ACK=ONCHAIN_ACK_PHRASE,
)


def live_settings(tmp_path, **over: Any) -> Settings:
    kw: dict[str, Any] = {
        "_env_file": None, "DELTR_MODE": "live", "DELTR_STATE_DIR": str(tmp_path),
        "DELTR_MIN_EDGE_BPS": -50.0, "DELTR_CAPITAL_USD": 10_000.0,
        "DELTR_MAKER_WAIT_MS": 50, "DELTR_MAKER_POLL_MS": 1,
    }
    kw.update(LIVE_ENV)
    kw.update(over)
    return Settings(**kw)  # type: ignore[arg-type]


def book(bid: float = 686.30, ask: float = 686.40) -> Quote:
    return Quote(venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", bid=bid, ask=ask, bid_qty=50.0, ask_qty=50.0,
                 ts=utcnow(), source=DataSource.BINANCE_FUTURES_MAINNET)


# LIVE caps notional at $250 per trade, so plans in these tests are deliberately small.
SMALL_QTY = 0.3


# --------------------------------------------------------------------------- fakes
@dataclass
class FakeMakerOrder:
    order_id: int
    client_id: str
    status: str
    executed_qty: float
    avg_price: float
    raw: dict = field(default_factory=dict)


class FakeMainnetFutures:
    """A mainnet futures venue that only understands POST-ONLY orders.

    ``script`` is consumed per ``place_limit_maker`` call:
    ("fill", qty) | ("rest", qty_after_poll) | ("reject",) | ("error", code).
    A "rest" order fills ``qty_after_poll`` when it is next looked up (0.0 means it never fills).
    """

    base_url = MAINNET
    has_credentials = True

    def __init__(self, script: Optional[list[tuple]] = None, dual: bool = False) -> None:
        self.script = list(script or [])
        self.orders: list[dict] = []
        self.cancels: list[str] = []
        self.lookups: list[str] = []
        self.ioc_orders: list[dict] = []
        self._resting: dict[str, FakeMakerOrder] = {}
        self.dual = dual

    async def prepare_account(self, symbol: str, leverage: int, isolated: bool = True) -> Any:
        from tests._fakes_d import FakeAccountPrep

        return FakeAccountPrep(dual_side=self.dual, position_side="SHORT" if self.dual else "BOTH",
                               margin_type="ISOLATED", leverage=leverage, usdt_balance=5_000.0)

    async def book_ticker(self, symbol: str) -> Quote:
        return book()

    async def place_limit_maker(self, symbol, side, qty, price, client_id, reduce_only=False, position_side="BOTH"):
        self.orders.append({"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "GTX", "qty": qty,
                            "price": price, "client_id": client_id, "reduce_only": reduce_only,
                            "position_side": position_side})
        outcome = self.script.pop(0) if self.script else ("fill", qty)
        oid = 9000 + len(self.orders)
        if outcome[0] == "fill":
            return FakeMakerOrder(oid, client_id, "FILLED", float(outcome[1]), price)
        if outcome[0] == "reject":
            raise FuturesError(-5022, "Post Only order will be rejected", False)
        if outcome[0] == "error":
            raise FuturesError(int(outcome[1]), "scripted", False)
        rested = FakeMakerOrder(oid, client_id, "NEW", float(outcome[1]), price if outcome[1] else 0.0)
        self._resting[client_id] = rested
        return FakeMakerOrder(oid, client_id, "NEW", 0.0, 0.0)

    async def place_limit_ioc(self, symbol, side, qty, price, client_id, reduce_only=False, position_side="BOTH"):
        self.ioc_orders.append({"symbol": symbol, "side": side, "timeInForce": "IOC", "qty": qty, "price": price,
                                "client_id": client_id, "reduce_only": reduce_only})
        return FakeMakerOrder(8000 + len(self.ioc_orders), client_id, "FILLED", qty, price)

    async def get_order_by_client_id(self, symbol, client_id):
        self.lookups.append(client_id)
        r = self._resting.get(client_id)
        if r is not None and r.executed_qty > 0:
            # a partially filled order that the venue then cancelled underneath us
            return FakeMakerOrder(r.order_id, client_id, "CANCELED", r.executed_qty, r.avg_price)
        return r

    async def cancel_order_by_client_id(self, symbol, client_id):
        self.cancels.append(client_id)
        r = self._resting.get(client_id)
        return None if r is None else FakeMakerOrder(r.order_id, client_id, "CANCELED", r.executed_qty, r.avg_price)


class FakeWallet:
    """Stand-in for AgenticWalletClient.  Holds no key, signs nothing, runs no subprocess."""

    def __init__(self, *, available: bool = True, signed_in: bool = True, outcome: str = "ok") -> None:
        self._available = available
        self._signed_in = signed_in
        self.outcome = outcome
        self.swaps: list[dict] = []

    def is_available(self) -> bool:
        return self._available

    def resolved_binary(self) -> Optional[str]:
        return "/fake/baw" if self._available else None

    def require_available(self) -> None:
        if not self._available:
            raise AgenticWalletError("BAW_NOT_INSTALLED", "the wallet CLI is not installed")

    async def require_signed_in(self) -> Any:
        if not self._signed_in:
            raise AgenticWalletError("BAW_NOT_SIGNED_IN", "the wallet is not signed in")
        return True

    async def swap(self, from_token, to_token, amount, **kw) -> SwapResult:
        self.swaps.append({"from": from_token, "to": to_token, "amount": amount, **kw})
        q = SwapQuote(chain_id=56, from_token=from_token, to_token=to_token, from_amount=amount,
                      to_amount=amount, min_receive=None, price_impact_pct=0.1, slippage="0.5")
        if self.outcome == "declined":
            raise AgenticWalletError("SWAP_PREVIEW_REJECTED", "preview breached the bound; nothing was submitted and nothing was signed.")
        if self.outcome == "unconfirmed":
            return SwapResult(confirmed=False, status="PENDING", order_id="ord-1", tx_hash=None, chain_id=56,
                              from_token=from_token, to_token=to_token, from_amount=amount, received_amount=None, quote=q)
        # a BUY spends USDT and receives base; a SELL sends base and receives USDT
        received = amount / 686.0 if amount > 1000 else amount * 686.0
        return SwapResult(confirmed=True, status="FINISHED", order_id="ord-1", tx_hash="0xdeadbeef", chain_id=56,
                          from_token=from_token, to_token=to_token, from_amount=amount, received_amount=received, quote=q)


class FakeQuoter:
    async def dex_quote(self, qty: float):
        return make_market().dex


def make_live_router(tmp_path, futures=None, wallet=None, **over):
    s = live_settings(tmp_path, **over)
    fut = futures or FakeMainnetFutures()
    leg = WalletDexLeg(wallet or FakeWallet(), s, quoter=FakeQuoter(), chain_id=56)
    return LiveRouter(fut, leg, s, FILTERS, book_ticker=fut.book_ticker), s, fut, leg


# --------------------------------------------------------------------------- maker pricing
def test_maker_price_rests_on_its_own_side_and_never_crosses():
    b = book(bid=686.30, ask=686.40)
    assert maker_price(Side.SELL, b, 0.01) == pytest.approx(686.40)   # joins the ask
    assert maker_price(Side.BUY, b, 0.01) == pytest.approx(686.30)    # joins the bid
    # improving by ticks stays on our side of the touch
    assert maker_price(Side.SELL, b, 0.01, offset_ticks=3) == pytest.approx(686.37)
    assert maker_price(Side.BUY, b, 0.01, offset_ticks=3) == pytest.approx(686.33)
    # an absurd offset is clamped one tick short of crossing, in both directions
    assert maker_price(Side.SELL, b, 0.01, offset_ticks=10_000) == pytest.approx(686.31)
    assert maker_price(Side.BUY, b, 0.01, offset_ticks=10_000) == pytest.approx(686.39)


def test_maker_price_refuses_a_book_it_cannot_trust():
    with pytest.raises(MakerError):
        maker_price(Side.SELL, book(bid=0.0, ask=686.4), 0.01)
    with pytest.raises(MakerError):
        maker_price(Side.SELL, book(bid=686.5, ask=686.4), 0.01)   # crossed book
    with pytest.raises(MakerError):
        maker_price(Side.SELL, book(), 0.0)                        # no tick size


def test_walked_away_only_fires_in_the_unfavourable_direction():
    # a resting SELL at 686.40: the ask rising is GOOD (we are the best offer), falling is bad
    assert walked_away(Side.SELL, 686.40, book(bid=686.9, ask=687.0), 0.01) is False
    assert walked_away(Side.SELL, 686.40, book(bid=685.9, ask=686.0), 0.01) is True
    assert walked_away(Side.BUY, 686.30, book(bid=685.9, ask=686.0), 0.01) is False
    assert walked_away(Side.BUY, 686.30, book(bid=686.9, ask=687.0), 0.01) is True


# --------------------------------------------------------------------------- maker execution
async def test_maker_posts_gtx_at_the_touch_and_never_sends_ioc(tmp_path):
    r, s, fut, _ = make_live_router(tmp_path)
    ms = make_market()
    plan = make_plan(ms)
    perp = next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES)
    fill = await r.fill(perp, ms, None, plan.id, 1)
    assert len(fut.orders) == 1 and not fut.ioc_orders, "LIVE maker must not send a taker order"
    o = fut.orders[0]
    assert o["timeInForce"] == "GTX" and o["side"] == Side.SELL
    assert o["price"] == pytest.approx(686.40)          # the ask, not a mark-derived crossing price
    assert fill.simulated is False and fill.source == DataSource.BINANCE_FUTURES_MAINNET
    assert fill.fee_usd == pytest.approx(fill.qty * fill.price * s.perp_maker_fee_bps / 1e4)
    assert fill.fee_usd < fill.qty * fill.price * s.perp_taker_fee_bps / 1e4


async def test_a_post_only_rejection_reprices_instead_of_crossing(tmp_path):
    fut = FakeMainnetFutures(script=[("reject",), ("fill", 4.85)])
    r, _, fut, _ = make_live_router(tmp_path, futures=fut)
    ms = make_market()
    plan = make_plan(ms)
    fill = await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert len(fut.orders) == 2 and all(o["timeInForce"] == "GTX" for o in fut.orders)
    assert not fut.ioc_orders, "a post-only rejection must never fall back to crossing the spread"
    assert fill.qty == pytest.approx(4.85)


async def test_an_unfilled_maker_order_is_a_failure_not_a_silent_skip(tmp_path):
    """The order rests, never fills, is cancelled at the budget, and the leg raises."""
    fut = FakeMainnetFutures(script=[("rest", 0.0)] * 5)
    r, _, fut, _ = make_live_router(tmp_path, futures=fut)
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(LegError) as exc:
        await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert "did not fill" in str(exc.value) and "never crossed" in str(exc.value)
    assert fut.cancels, "an order that did not fill must be cancelled, never left working"
    assert not fut.ioc_orders, "an unfilled maker order must not be rescued by crossing the spread"


async def test_a_partially_filled_maker_order_keeps_what_the_venue_reported(tmp_path):
    fut = FakeMainnetFutures(script=[("rest", 2.0), ("fill", 2.85)])
    r, _, fut, _ = make_live_router(tmp_path, futures=fut)
    ms = make_market()
    plan = make_plan(ms)
    fill = await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert fill.qty == pytest.approx(4.85)              # 2.0 partial + 2.85 on the re-post
    assert fut.orders[1]["qty"] == pytest.approx(2.85)  # only the remainder is re-posted


async def test_maker_refuses_to_price_off_the_mark_when_there_is_no_book(tmp_path):
    class NoBook(FakeMainnetFutures):
        async def book_ticker(self, symbol):
            raise RuntimeError("book unavailable")

    fut = NoBook()
    r, _, fut, _ = make_live_router(tmp_path, futures=fut)
    ms = make_market().model_copy(update={"cex_perp_book": None})
    plan = make_plan(ms)
    with pytest.raises(LegError, match="will not cross the spread"):
        await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert not fut.orders and not fut.ioc_orders


async def test_taker_style_is_available_but_must_be_chosen(tmp_path):
    fut = FakeMainnetFutures()
    r, s, fut, _ = make_live_router(tmp_path, futures=fut, DELTR_EXECUTION_STYLE="taker")
    assert s.execution_style is ExecutionStyle.TAKER and not r.is_maker
    ms = make_market()
    plan = make_plan(ms)
    await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert fut.ioc_orders and not fut.orders


# --------------------------------------------------------------------------- on-chain leg
def test_token_mapping_refuses_to_guess():
    assert tokens_for("BNBUSDT")[0] == "WBNB"
    with pytest.raises(OnchainLegError, match="refusing to guess"):
        tokens_for("DOGEUSDT")


async def test_the_onchain_leg_reports_the_transaction_hash_and_the_wallet_as_its_source(tmp_path):
    wallet = FakeWallet()
    r, _, _, _ = make_live_router(tmp_path, wallet=wallet)
    ms = make_market()
    plan = make_plan(ms)
    dex = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
    fill = await r.fill(dex, ms, None, plan.id, 1)
    assert fill.ref == "0xdeadbeef" and fill.simulated is False
    assert fill.source == DataSource.BINANCE_AGENTIC_WALLET   # the wallet routed it
    assert fill.venue == Venue.PANCAKESWAP_V3                 # the liquidity venue the plan speaks in
    assert wallet.swaps and wallet.swaps[0]["chain_id"] == 56


async def test_an_unconfirmed_swap_is_never_reported_as_a_fill(tmp_path):
    r, _, _, _ = make_live_router(tmp_path, wallet=FakeWallet(outcome="unconfirmed"))
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(LegError, match="not confirmed"):
        await r.fill(next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3), ms, None, plan.id, 1)


async def test_a_declined_swap_says_nothing_was_signed(tmp_path):
    r, _, _, _ = make_live_router(tmp_path, wallet=FakeWallet(outcome="declined"))
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(LegError, match="nothing was signed"):
        await r.fill(next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3), ms, None, plan.id, 1)


async def test_a_signed_out_wallet_refuses_before_anything_is_requested(tmp_path):
    wallet = FakeWallet(signed_in=False)
    r, _, _, _ = make_live_router(tmp_path, wallet=wallet)
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(LegError, match="not signed in"):
        await r.fill(next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3), ms, None, plan.id, 1)
    assert not wallet.swaps


async def test_the_onchain_guard_runs_before_the_wallet_is_asked_for_anything(tmp_path):
    """A refusal provably left nothing on chain: the wallet was never called."""
    wallet = FakeWallet()
    s = live_settings(tmp_path)
    refused: list[float] = []

    def guard(notional: float) -> None:
        refused.append(notional)
        raise OnchainLegError("REFUSED: over the on-chain cap", code="MAX_NOTIONAL")

    leg = WalletDexLeg(wallet, s, quoter=FakeQuoter(), guard=guard, chain_id=56)
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(OnchainLegError):
        await leg.fill(next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3), ms, None, plan.id, 1)
    assert refused and not wallet.swaps


async def test_the_onchain_leg_is_reversed_with_a_real_opposite_swap(tmp_path):
    wallet = FakeWallet()
    r, _, _, _ = make_live_router(tmp_path, wallet=wallet)
    ms = make_market()
    plan = make_plan(ms)
    dex = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
    fill = await r.fill(dex, ms, None, plan.id, 1)
    rev = await r.reverse(fill, ms, plan.id)
    assert rev.side != fill.side and len(wallet.swaps) == 2
    assert wallet.swaps[0]["from"] == wallet.swaps[1]["to"], "the reversal must swap back the other way"


# --------------------------------------------------------------------------- through the Executor
def live_stack(tmp_path, futures=None, wallet=None, **over):
    s = live_settings(tmp_path, **over)
    ms = make_market()
    state = FakeState(s)
    state.market = ms
    gate = make_gate(s)
    portfolio = Portfolio(state, gate, s, None)
    hub = FakeHub(ms)
    receipts = ReceiptStore(state, None)
    fut = futures or FakeMainnetFutures()
    leg = WalletDexLeg(wallet or FakeWallet(), s, quoter=FakeQuoter(), chain_id=56)
    router = LiveRouter(fut, leg, s, FILTERS, book_ticker=fut.book_ticker)
    ex = Executor(state, gate, portfolio, router, hub, None, s, receipts, FILTERS)
    portfolio.mark(ms)
    from types import SimpleNamespace

    return SimpleNamespace(settings=s, state=state, gate=gate, portfolio=portfolio, executor=ex, ms=ms,
                           futures=fut, router=router)


async def test_live_execution_requires_confirm(tmp_path):
    st = live_stack(tmp_path)
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, False, TraceSource.MCP, "pytest")
    assert r.status == "vetoed" and r.decision.code == "CONFIRM_REQUIRED"
    assert "REAL MONEY" in r.decision.reason
    assert not st.futures.orders, "nothing may reach the venue without confirm"


async def test_the_gate_still_sits_in_front_of_every_live_order(tmp_path):
    st = live_stack(tmp_path)
    st.gate.set_kill_switch(True)
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "vetoed" and not r.decision.approved
    assert not st.futures.orders and not st.futures.ioc_orders


async def test_the_live_aggregate_cap_can_only_subtract_from_what_the_gate_approved(tmp_path):
    """The gate approves the plan on its own limits; the LIVE ceiling then vetoes it."""
    st = live_stack(tmp_path, DELTR_LIVE_MAX_AGGREGATE_USD=10.0)
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "vetoed" and r.decision.code == "AGGREGATE_NOTIONAL"
    assert "DELTR_LIVE_MAX_AGGREGATE_USD" in r.decision.reason
    assert not st.futures.orders


async def test_a_live_hedge_fills_both_real_legs_and_opens_a_position(tmp_path):
    st = live_stack(tmp_path)
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "filled", r.decision.reason
    sources = {f.source for f in r.fills}
    assert DataSource.BINANCE_AGENTIC_WALLET in sources and DataSource.BINANCE_FUTURES_MAINNET in sources
    assert all(f.simulated is False for f in r.fills), "nothing in LIVE is simulated"


async def test_an_unfilled_maker_leg_costs_nothing_because_no_swap_was_made(tmp_path):
    """Under MAKER the perp leg goes FIRST, because it is the leg that may not fill.

    A post-only order that misses its budget is ordinary, not exceptional.  Sending it first
    means the ordinary outcome costs zero: an order that never filled pays no fee and there is
    nothing on chain to undo.  Doing the swap first would pay two pool fees, two lots of gas
    and two lots of price impact every time the post missed, against an edge measured in
    single-digit bps.
    """
    wallet = FakeWallet()
    st = live_stack(tmp_path, futures=FakeMainnetFutures(script=[("rest", 0.0)] * 6), wallet=wallet)
    assert st.settings.effective_leg_order is LegOrder.CEX_FIRST
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "failed"
    assert not wallet.swaps, "the on-chain leg must not have been touched: the perp never filled"
    assert not st.futures.ioc_orders, "an unfilled maker order must not be rescued by crossing the spread"
    assert not st.portfolio.positions("open")
    assert st.gate.snapshot()["kill_switch"] is False, "nothing was submitted on chain; this is a clean no-trade"


class DeadIocFutures(FakeMainnetFutures):
    """A venue whose IOC orders never fill, so the perp leg fails after the DEX leg is real."""

    async def place_limit_ioc(self, symbol, side, qty, price, client_id, reduce_only=False, position_side="BOTH"):
        self.ioc_orders.append({"symbol": symbol, "side": side, "qty": qty, "price": price, "client_id": client_id})
        return FakeMakerOrder(8000 + len(self.ioc_orders), client_id, "EXPIRED", 0.0, 0.0)


async def test_a_failed_perp_leg_reverses_the_real_onchain_leg(tmp_path):
    """TAKER style keeps DEX first, so this is the case where a real swap must be swapped back."""
    wallet = FakeWallet()
    st = live_stack(tmp_path, futures=DeadIocFutures(), wallet=wallet, DELTR_EXECUTION_STYLE="taker")
    st.router.backoff_ms = (0, 0, 0)
    assert st.settings.effective_leg_order is LegOrder.DEX_FIRST
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "unwound"
    assert len(wallet.swaps) == 2, "the on-chain leg must be reversed by a real opposite swap"
    assert wallet.swaps[0]["from"] == wallet.swaps[1]["to"], "the reversal must swap back the other way"
    assert not st.portfolio.positions("open")


async def test_a_failed_reversal_books_a_naked_leg_and_engages_the_kill_switch(tmp_path):
    class RefusingWallet(FakeWallet):
        async def swap(self, *a, **kw):
            if self.swaps:                       # the reversal, not the opening swap
                self.swaps.append({"refused": True})
                raise AgenticWalletError("SWAP_FAILED", "the wallet reported the swap failed")
            return await super().swap(*a, **kw)

    st = live_stack(tmp_path, futures=DeadIocFutures(), wallet=RefusingWallet(), DELTR_EXECUTION_STYLE="taker")
    st.router.backoff_ms = (0, 0, 0)
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "failed"
    assert st.gate.snapshot()["kill_switch"] is True
    assert st.portfolio.positions("open"), "the unhedged leg must be booked, never hidden"


# --------------------------------------------------------------------------- preflight
async def test_live_preflight_refuses_a_missing_wallet_cli_and_a_signed_out_wallet(tmp_path):
    from deltr.engine import Engine
    from tests.conftest import refusing_http

    eng = Engine(live_settings(tmp_path), http=refusing_http())
    eng.wallet = FakeWallet(available=False)  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="CLI is not available"):
        await eng.live_preflight()

    class SignedOut(FakeWallet):
        async def auth_status(self):
            from deltr.venues.agentic_wallet import AuthStatus

            return AuthStatus(signed_in=False, status="UNCONNECTED")

    eng.wallet = SignedOut()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="not signed in"):
        await eng.live_preflight()
    assert eng.status().real_funds_armed is False, "a failed preflight must never arm the status"


async def test_live_preflight_passes_and_arms_the_status_with_a_public_address_only(tmp_path):
    from deltr.engine import Engine
    from deltr.venues.agentic_wallet import AuthStatus
    from tests.conftest import refusing_http

    class SignedIn(FakeWallet):
        async def auth_status(self):
            return AuthStatus(signed_in=True, status="CONNECTED", address="0xPUBLIC")

        async def wallet_address(self):
            return {"bsc": "0xPUBLIC"}

    eng = Engine(live_settings(tmp_path), http=refusing_http())
    eng.wallet = SignedIn()  # type: ignore[assignment]
    facts = await eng.live_preflight()
    assert facts["real_funds_armed"] is True and facts["wallet_signed_in"] is True
    st = eng.status()
    assert st.real_funds_armed is True and st.wallet_address == "0xPUBLIC"
    assert st.execution_style == "maker" and st.max_notional_usd == 250.0 and st.max_aggregate_usd == 1000.0
    blob = str(facts) + st.model_dump_json()
    assert "k-not-real" not in blob and "s-not-real" not in blob, "no secret may reach the banner or the status"


async def test_live_refuses_replay_and_auto_execute(tmp_path):
    from deltr.engine import build_engine
    from tests.conftest import REPLAY_FIXTURE, refusing_http

    with pytest.raises(ValueError, match="replay"):
        build_engine(live_settings(tmp_path), replay_path=str(REPLAY_FIXTURE), http=refusing_http())
    with pytest.raises(ValueError, match="auto"):
        build_engine(live_settings(tmp_path, DELTR_AUTO_EXECUTE=True), http=refusing_http())


def test_a_live_engine_keeps_market_data_on_a_keyless_client(tmp_path):
    from deltr.engine import build_engine
    from tests.conftest import refusing_http

    eng = build_engine(live_settings(tmp_path), http=refusing_http())
    assert eng.futures.has_credentials is True                 # the order client is credentialed
    assert eng.futures_data.has_credentials is False           # market data never is
    assert eng.futures_data is not eng.futures
    assert type(eng.router).__name__ == "LiveRouter"
    assert eng.onchain_leg is not None


# --------------------------------------------------------------------------- live venue (opt-in)
@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DELTR_LIVE_TESTS") != "1", reason="live tests are opt-in: set DELTR_LIVE_TESTS=1")
async def test_mainnet_book_ticker_is_reachable_and_keyless():
    """Read-only public market data from the real mainnet host. Places nothing, signs nothing."""
    import httpx

    from deltr.venues.binance_futures import FuturesClient

    async with httpx.AsyncClient(timeout=15.0) as http:
        c = FuturesClient(http, "https://fapi.binance.com")
        assert c.has_credentials is False
        q = await c.book_ticker("BNBUSDT")
    assert q.bid > 0 and q.ask > q.bid


# --------------------------------------------------------------------------- unconfirmed cancel
class StuckCancelFutures(FakeMainnetFutures):
    """A venue whose DELETE never lands: the cancel raises and the order stays NEW.

    This is the ordinary transport blip (a 5xx, a timeout, a dropped connection) on the one call
    that is supposed to take Deltr's order off a real book.
    """

    async def cancel_order_by_client_id(self, symbol, client_id):
        self.cancels.append(client_id)
        raise FuturesError(-1001, "internal error / disconnected", True)

    async def get_order_by_client_id(self, symbol, client_id):
        self.lookups.append(client_id)
        r = self._resting.get(client_id)
        if r is None:
            return None
        # still working: the venue keeps reporting NEW because the cancel never arrived
        return FakeMakerOrder(r.order_id, client_id, "NEW", 0.0, 0.0)


@pytest.mark.asyncio
async def test_an_unconfirmed_cancel_stops_instead_of_posting_a_second_order(tmp_path):
    """A cancel the venue never confirmed must NOT be followed by another post.

    Failure sequence without this: the budget expires, ``_cancel`` raises, the code assumed the
    order was gone and re-posted the full remainder.  Both orders rest on a real mainnet book,
    both fill, and the perp leg ends up 2x (up to 3x, once per re-price attempt) the size the
    risk gate approved and the DEX leg hedges — a levered directional position on real money,
    reported to the operator as a single filled hedge.
    """
    fut = StuckCancelFutures(script=[("rest", 0.0), ("rest", 0.0), ("rest", 0.0)])
    router, s, fut, _leg = make_live_router(tmp_path, futures=fut)
    out = await router.maker.work("BNBUSDT", Side.SELL, SMALL_QTY, book=book(),
                                  client_ids=["a-1", "a-2", "a-3"])
    assert len(fut.orders) == 1, f"posted {len(fut.orders)} orders on top of an unconfirmed cancel"
    assert out.left_working == ["a-1"], out.left_working
    assert out.filled_qty == 0.0
    assert "may still be resting" in out.reason
    assert fut.ioc_orders == [], "never crosses the spread to rescue a stuck order"


@pytest.mark.asyncio
async def test_a_stuck_maker_order_is_a_submitted_leg_failure_not_a_clean_fill(tmp_path):
    """The router must surface it as ``submitted=True`` so the Executor books it and engages the
    kill switch, rather than reporting a fill quantity that can still grow."""
    from deltr.models import OrderLeg

    fut = StuckCancelFutures(script=[("rest", 0.0)])
    router, s, fut, _leg = make_live_router(tmp_path, futures=fut)
    leg = OrderLeg(venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.SELL, qty=SMALL_QTY, price_hint=686.0)
    with pytest.raises(LegError) as ei:
        await router.fill(leg, make_market(), SMALL_QTY, "plan_stuck", 1)
    assert ei.value.submitted is True
    assert "UNCONFIRMED cancel" in ei.value.reason and "a-1" not in ei.value.reason
    assert "Cancel the order at the venue by hand" in ei.value.reason
    assert fut.ioc_orders == []


@pytest.mark.asyncio
async def test_a_confirmed_cancel_is_not_reported_as_left_working(tmp_path):
    """The guard must not fire on the ordinary path: when the venue DOES confirm the cancel,
    the outcome is the plain "budget expired" one and nothing is flagged for an operator."""
    fut = FakeMainnetFutures(script=[("rest", 0.0)])
    router, s, fut, _leg = make_live_router(tmp_path, futures=fut)
    out = await router.maker.work("BNBUSDT", Side.SELL, SMALL_QTY, book=book(),
                                  client_ids=["b-1", "b-2", "b-3"])
    assert out.left_working == []
    assert fut.cancels == ["b-1"], "the order was cancelled and the venue said so"
    assert "budget" in out.reason and "may still be resting" not in out.reason


@pytest.mark.asyncio
async def test_a_partial_fill_the_venue_confirms_is_still_re_posted(tmp_path):
    """A venue-confirmed terminal state is not a stuck cancel: the remainder is worked again."""
    fut = FakeMainnetFutures(script=[("rest", 0.1), ("fill", 0.2)])
    router, s, fut, _leg = make_live_router(tmp_path, futures=fut)
    out = await router.maker.work("BNBUSDT", Side.SELL, SMALL_QTY, book=book(),
                                  client_ids=["c-1", "c-2", "c-3"])
    assert out.left_working == []
    assert len(fut.orders) == 2, "a confirmed terminal order is re-posted, not stopped"
    assert out.filled_qty == pytest.approx(SMALL_QTY)


# --------------------------------------------------------------------------- audit regressions
class UnknowableFutures(FakeMainnetFutures):
    """A venue that times out on the POST and then will not answer the follow-up lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.lookup_calls = 0

    async def place_limit_ioc(self, symbol, side, qty, price, client_id, reduce_only=False, position_side="BOTH"):
        self.ioc_orders.append({"client_id": client_id, "qty": qty, "price": price})
        raise FuturesError(-1000, "read timeout", True)

    async def get_order_by_client_id(self, symbol, client_id):
        self.lookup_calls += 1
        raise FuturesError(-1003, "too many requests", True)


async def test_an_order_whose_state_is_unknown_is_never_retried_into_a_duplicate(tmp_path):
    """A retry after an unresolvable timeout is how one intended order becomes two real ones."""
    fut = UnknowableFutures()
    r, _, fut, _ = make_live_router(tmp_path, futures=fut, DELTR_EXECUTION_STYLE="taker")
    r.backoff_ms = (0, 0, 0)
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(LegError) as exc:
        await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert len(fut.ioc_orders) == 1, "an order Deltr could not resolve must not be sent a second time"
    assert exc.value.submitted is True
    assert "duplicate" in str(exc.value) and "UNKNOWN" in str(exc.value)


async def test_a_maker_partial_survives_a_fatal_venue_error(tmp_path):
    """2.0 filled then the venue dies: that 2.0 is a real position and must not be forgotten."""
    fut = FakeMainnetFutures(script=[("rest", 2.0), ("error", -1111)])
    r, _, fut, _ = make_live_router(tmp_path, futures=fut)
    ms = make_market()
    plan = make_plan(ms)
    fill = await r.fill(next(l for l in plan.legs if l.venue == Venue.BINANCE_FUTURES), ms, None, plan.id, 1)
    assert fill.qty == pytest.approx(2.0), "the venue-confirmed partial is the position; it is reported, not dropped"
    assert not fut.ioc_orders


async def test_a_closing_perp_leg_crosses_the_spread_even_under_maker(tmp_path):
    """A stop that cannot execute is not a discipline, it is an open loss.  reduce_only takes."""
    from deltr.models import OrderLeg

    fut = FakeMainnetFutures()
    r, s, fut, _ = make_live_router(tmp_path, futures=fut)
    assert r.is_maker
    ms = make_market()
    leg = OrderLeg(venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.BUY, qty=SMALL_QTY,
                   price_hint=686.0, reduce_only=True, leverage=2.0)
    await r.fill(leg, ms, SMALL_QTY, "plan-close", 1)
    assert fut.ioc_orders and not fut.orders, "a reduce-only leg must not be posted behind the touch"


async def test_a_foreign_guard_refusal_cannot_escape_the_executor(tmp_path):
    """The engine's on-chain refusal is not an OnchainLegError; unmapped it left no receipt."""

    class EngineStyleRefusal(Exception):
        code = "ONCHAIN_NOT_ARMED"

    def guard(notional: float, *, reducing: bool = False) -> None:
        raise EngineStyleRefusal("REFUSED: the on-chain leg is not armed")

    wallet = FakeWallet()
    s = live_settings(tmp_path)
    leg = WalletDexLeg(wallet, s, quoter=FakeQuoter(), guard=guard, chain_id=56)
    ms = make_market()
    plan = make_plan(ms)
    with pytest.raises(OnchainLegError) as exc:
        await leg.fill(next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3), ms, None, plan.id, 1)
    assert exc.value.submitted is False and not wallet.swaps


async def test_a_reversal_is_priced_off_the_current_quote_not_the_entry_fill(tmp_path):
    """min_receive built from the entry price demands the round trip back and refuses the undo."""

    class RecordingWallet(FakeWallet):
        async def swap(self, from_token, to_token, amount, **kw):
            return await super().swap(from_token, to_token, amount, **kw)

    wallet = RecordingWallet()
    s = live_settings(tmp_path)
    leg = WalletDexLeg(wallet, s, quoter=FakeQuoter(), chain_id=56)
    ms = make_market()
    plan = make_plan(ms, qty=SMALL_QTY)
    dex_leg = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
    fill = await leg.fill(dex_leg, ms, SMALL_QTY, plan.id, 1)
    # the entry filled expensively; the market is unchanged
    dear = fill.model_copy(update={"price": fill.price * 1.05})
    await leg.reverse(dear, ms, plan.id)
    sell = wallet.swaps[-1]
    expected = fill.qty * ms.dex.exec_price_sell * (1.0 - s.onchain_max_slippage_pct / 100.0)
    assert sell["min_receive"] == pytest.approx(expected, rel=1e-6), (
        "the reversal must ask for what the market pays now, not for the entry price back"
    )


async def test_a_reducing_leg_tells_the_guard_so(tmp_path):
    seen: list[tuple[float, bool]] = []

    def guard(notional: float, *, reducing: bool = False) -> None:
        seen.append((notional, reducing))

    wallet = FakeWallet()
    s = live_settings(tmp_path)
    leg = WalletDexLeg(wallet, s, quoter=FakeQuoter(), guard=guard, chain_id=56)
    ms = make_market()
    plan = make_plan(ms, qty=SMALL_QTY)
    dex_leg = next(l for l in plan.legs if l.venue == Venue.PANCAKESWAP_V3)
    fill = await leg.fill(dex_leg, ms, SMALL_QTY, plan.id, 1)
    await leg.reverse(fill, ms, plan.id)
    assert [r for _, r in seen] == [False, True], "an opening swap and a closing swap are not the same request"


async def test_a_first_leg_that_was_submitted_is_never_reported_as_nothing_to_reverse(tmp_path):
    """The one sentence that must never be a guess: 'nothing to reverse'."""
    st = live_stack(tmp_path)

    class SubmittedThenFailed:
        mode = st.router.mode

        async def requote(self, qty, ms):
            return ms.dex if ms is not None else None

        async def fill(self, leg, ms, qty=None, plan_id="", attempt=1):
            raise LegError("the wallet timed out after broadcasting", leg_index=0, venue=leg.venue, submitted=True)

        async def reverse(self, fill, ms, plan_id):  # pragma: no cover - never reached
            raise AssertionError("nothing was confirmed, so there is nothing to reverse from")

        async def reverse_partial(self, leg, residual_qty, ms, plan_id):  # pragma: no cover
            raise AssertionError("unreachable")

    st.executor.router = SubmittedThenFailed()
    plan = make_plan(st.ms, qty=SMALL_QTY)
    st.state.put_plan(plan)
    r = await st.executor.execute(plan.id, True, TraceSource.MCP, "pytest")
    assert r.status == "failed"
    assert "nothing to reverse" not in " ".join(st.summary for st in r.steps)
    assert st.gate.snapshot()["kill_switch"] is True, "an unreconciled real exposure must stop new entries"
    assert any("reconciliation" in st.summary for st in r.steps)
