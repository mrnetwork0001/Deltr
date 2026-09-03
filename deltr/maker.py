"""deltr/maker.py — post-only (maker) execution for the perp leg.

Why this module exists at all is an economics result, not a preference.  Measured over 500
days of real mainnet funding (docs/STRATEGY_EVIDENCE.md), the round trip with the perp leg
TAKEN is about 16.6 bps, of which 10 bps is the Binance taker fee (2 x 5 bps).  Carry clears
that in only 3.8% of 7-day windows on BNBUSDT.  POSTING the perp leg takes the round trip to
about 8.6 bps, which 36.3% of the same windows clear.  Crossing the spread by default would
lose money on this strategy, so LIVE posts.

The discipline, in order:

* every order is ``timeInForce=GTX`` (post-only).  GTX is the **exchange's** guarantee: if the
  order would take liquidity, the matching engine rejects it (-5021 / -5022) instead of
  filling it.  Deltr never has to trust its own price arithmetic to stay a maker;
* the price is the touch on our own side of the book (a SELL rests at the best ask, a BUY at
  the best bid), optionally improved by ``DELTR_MAKER_OFFSET_TICKS`` but never past the point
  where it would cross;
* there is no book, there is no order: a maker price guessed off the mark is a taker order
  waiting to happen, so a missing book is a refusal;
* a resting order is polled; when the market walks away from it, it is CANCELLED and re-posted
  (up to ``DELTR_MAKER_REPRICE_ATTEMPTS``) inside one total time budget
  (``DELTR_MAKER_WAIT_MS``).  Partial fills are kept and the remainder is re-posted;
* when the budget runs out, the order is cancelled and whatever filled is reported.  **A zero
  fill is a failure, not a silent skip**: the caller raises and the Executor's reverse-on-failure
  path handles the other leg.  Nothing here ever falls back to crossing the spread.

This module places orders but makes no risk decision: the gate has already approved the plan
before the Executor calls a router, and the router calls this.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional, Sequence

from deltr.config import Settings
from deltr.edge import floor_to_step, round_to_tick
from deltr.models import Quote, Side, SymbolFilters
from deltr.venues.binance_futures import POST_ONLY_REJECT_CODES, FuturesError

log = logging.getLogger("deltr.maker")

# Order states the venue reports for an order that is no longer working.
_TERMINAL = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
# A cancel is worth retrying: the alternative to a confirmed cancel is an order Deltr cannot
# account for, so a transport blip must not be allowed to strand one on a real book.
_CANCEL_ATTEMPTS = 3
_CANCEL_RETRY_S = 0.2


class MakerError(RuntimeError):
    """The maker leg could not be worked (no book, fatal venue error, nothing filled)."""

    def __init__(self, reason: str, *, code: Optional[int] = None, filled_qty: float = 0.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.filled_qty = float(filled_qty)


@dataclass
class MakerOutcome:
    """What actually happened, in the venue's own numbers.

    ``filled_qty`` is the sum of the executed quantity the venue reported across every posted
    order.  ``avg_price`` is quantity-weighted over those same fills.  Nothing here is inferred
    from the price Deltr asked for.
    """

    filled_qty: float
    avg_price: float
    requested_qty: float
    attempts: int
    order_ids: list[str] = field(default_factory=list)
    client_ids: list[str] = field(default_factory=list)
    posted_prices: list[float] = field(default_factory=list)
    elapsed_ms: int = 0
    reason: str = ""
    crossed_spread: bool = False  # always False: this path posts, it never takes
    #: client ids the venue never confirmed as cancelled or terminal.  Non-empty means an order
    #: of Deltr's MAY still be resting on the book, so no further order was posted and an
    #: operator has to look.  This is the one state where the maker path stops early on purpose.
    left_working: list[str] = field(default_factory=list)

    @property
    def filled_any(self) -> bool:
        return self.filled_qty > 0.0

    @property
    def complete(self) -> bool:
        return self.filled_qty >= self.requested_qty - 1e-12

    def as_dict(self) -> dict[str, Any]:
        return {
            "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "requested_qty": self.requested_qty,
            "attempts": self.attempts,
            "order_ids": list(self.order_ids),
            "posted_prices": list(self.posted_prices),
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "crossed_spread": self.crossed_spread,
            "left_working": list(self.left_working),
            "style": "maker: post-only (GTX)",
        }


def maker_price(side: Side, book: Quote, tick: float, offset_ticks: int = 0) -> float:
    """The post-only price for ``side``: the touch on our own side, improved by ``offset_ticks``
    but never to a price that would cross.

    A SELL rests at the best ask (or ``offset_ticks`` below it, floored at one tick above the
    best bid).  A BUY rests at the best bid (or ``offset_ticks`` above it, capped at one tick
    below the best ask).  The clamp is belt and braces: GTX already refuses a crossing order.
    """
    if tick <= 0:
        raise MakerError("tick size must be > 0 to price a maker order")
    if book.bid <= 0 or book.ask <= 0 or book.ask <= book.bid:
        raise MakerError(f"order book is not two-sided (bid {book.bid}, ask {book.ask}); refusing to guess a maker price")
    # Decimal, not float: 686.30 + 0.03 is 686.3299999... in binary and would floor to the wrong
    # tick, quietly moving the order a tick away from where the caller asked for it.
    d_tick = Decimal(str(tick))
    off = Decimal(max(0, int(offset_ticks))) * d_tick
    bid, ask = Decimal(str(book.bid)), Decimal(str(book.ask))
    if side == Side.SELL:
        price = round_to_tick(float(ask - off), tick)
        floor = round_to_tick(float(bid + d_tick), tick)
        return max(price, floor)
    price = round_to_tick(float(bid + off), tick)
    ceiling = round_to_tick(float(ask - d_tick), tick)
    return min(price, ceiling)


def walked_away(side: Side, resting_price: float, book: Quote, tick: float) -> bool:
    """True when the market has moved past our resting order and it should be re-posted.

    Only the unfavourable direction counts.  A SELL resting at 700.0 while the ask climbs to
    701.0 is now the best offer and closer to filling, so it is left alone; the same SELL while
    the ask FALLS to 699.0 is stranded above the book and gets re-posted.
    """
    if tick <= 0 or book.bid <= 0 or book.ask <= 0:
        return False
    if side == Side.SELL:
        return book.ask < resting_price - tick / 2.0
    return book.bid > resting_price + tick / 2.0


class MakerExecution:
    """Works one perp leg as a maker against a real venue.

    ``futures`` must expose ``place_limit_maker``, ``get_order_by_client_id`` and
    ``cancel_order_by_client_id``.  ``book_ticker`` is an async callable returning a fresh
    :class:`~deltr.models.Quote`; it is kept separate from the order client so the keyless
    mainnet market-data client can serve the book while the credentialed client places orders.
    """

    def __init__(
        self,
        futures: Any,
        settings: Settings,
        filters: SymbolFilters,
        *,
        book_ticker: Optional[Callable[[str], Awaitable[Quote]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.futures = futures
        self.settings = settings
        self.filters = filters
        self._book_ticker = book_ticker or getattr(futures, "book_ticker", None)
        self._sleep = sleep
        self._clock = clock

    async def _book(self, symbol: str, fallback: Optional[Quote]) -> Optional[Quote]:
        if callable(self._book_ticker):
            try:
                return await self._book_ticker(symbol)
            except Exception as exc:  # noqa: BLE001 - a stale-but-present book beats no book
                log.warning("maker: book refresh for %s failed (%s); using the last snapshot", symbol, exc)
        return fallback

    async def work(
        self,
        symbol: str,
        side: Side,
        qty: float,
        *,
        book: Optional[Quote],
        client_ids: Sequence[str],
        reduce_only: bool = False,
        position_side: str = "BOTH",
    ) -> MakerOutcome:
        """Post, watch, re-post; return what the venue filled.

        ``client_ids`` supplies one id per allowed attempt, so every posted order is
        idempotently identifiable and a timeout can be resolved by lookup rather than by
        placing a second order.
        """
        step = self.filters.step_size
        tick = self.filters.tick_size
        requested = floor_to_step(float(qty), step)
        if requested <= 0:
            raise MakerError(f"qty {qty} rounds to zero at step {step}")
        started = self._clock()
        budget_s = self.settings.maker_wait_ms / 1000.0
        poll_s = self.settings.maker_poll_ms / 1000.0
        max_attempts = min(int(self.settings.maker_reprice_attempts), len(client_ids)) or 1

        filled = 0.0
        notional = 0.0
        out = MakerOutcome(filled_qty=0.0, avg_price=0.0, requested_qty=requested, attempts=0)
        reason = "budget exhausted before the order filled"

        for attempt in range(max_attempts):
            # lot sizes are never subtracted as floats (the Executor keeps the same discipline)
            remaining = floor_to_step(float(Decimal(str(requested)) - Decimal(str(filled))), step)
            if remaining < self.filters.min_qty:
                reason = "filled" if filled >= requested - 1e-12 else "remainder below min_qty"
                break
            if self._clock() - started >= budget_s:
                reason = f"maker time budget of {self.settings.maker_wait_ms} ms expired"
                break
            live_book = await self._book(symbol, book)
            if live_book is None:
                msg = ("no order book available for the perp leg; a maker price cannot be derived from the mark "
                       "and Deltr will not cross the spread instead")
                if filled > 0:
                    # Something already filled.  That is a REAL position; raising here would throw
                    # the quantity away and the Executor would hedge/reverse against a size that
                    # does not match the book.  Stop working the order and report the partial.
                    reason = f"{msg} (stopped with {filled:g} already filled)"
                    break
                raise MakerError(msg, filled_qty=filled)
            book = live_book
            price = maker_price(side, live_book, tick, self.settings.maker_offset_ticks)
            cid = client_ids[attempt]
            out.attempts = attempt + 1
            try:
                res = await self.futures.place_limit_maker(
                    symbol, side, remaining, price, cid, reduce_only=reduce_only, position_side=position_side
                )
            except FuturesError as exc:
                if exc.code in POST_ONLY_REJECT_CODES:
                    # the book moved between the read and the POST: re-price, never cross
                    reason = f"post-only rejected at {price:g} (the book moved); re-pricing"
                    log.info("maker: %s", reason)
                    await self._sleep(min(poll_s, 0.2))
                    continue
                # a timeout may still have rested an order: look it up before doing anything else
                found = await self._lookup(symbol, cid)
                if found is None:
                    if filled > 0:
                        # see the note above: a venue-confirmed partial is never discarded
                        reason = f"maker order {cid} failed: {exc} (stopped with {filled:g} already filled)"
                        break
                    raise MakerError(f"maker order {cid} failed: {exc}", code=exc.code, filled_qty=filled) from exc
                res = found
            out.order_ids.append(str(getattr(res, "order_id", "")))
            out.client_ids.append(cid)
            out.posted_prices.append(price)

            got, px, status = await self._watch(symbol, cid, side, price, tick, started, budget_s, poll_s, res)
            if got > 0:
                filled += got
                notional += got * px
            if status == "FILLED" and filled >= requested - 1e-12:
                reason = "filled"
                break
            if status == "STUCK":
                # The venue never confirmed the cancel, so order ``cid`` may STILL be resting.
                # Posting the remainder now is how one intended position becomes two: both
                # orders rest, both fill, and the perp leg ends up a multiple of the size the
                # gate approved and the DEX leg hedges.  Stop here and say so.
                out.left_working.append(cid)
                reason = (f"cancel of {cid} was not confirmed by the venue; it may still be resting. "
                          f"No further order was posted. Cancel it by hand before trading this symbol again")
                log.error("maker: %s", reason)
                break
            if status == "BUDGET":
                reason = f"maker time budget of {self.settings.maker_wait_ms} ms expired"
                break

        out.filled_qty = floor_to_step(filled, step) if filled > 0 else 0.0
        out.avg_price = (notional / filled) if filled > 0 else 0.0
        out.elapsed_ms = int((self._clock() - started) * 1000)
        out.reason = reason
        return out

    async def _watch(
        self, symbol: str, cid: str, side: Side, price: float, tick: float,
        started: float, budget_s: float, poll_s: float, placed: Any,
    ) -> tuple[float, float, str]:
        """Poll one resting order until it fills, the market walks away, or the budget ends.

        Returns ``(executed_qty, avg_price, status)`` where status is FILLED, REPRICE, BUDGET or
        STUCK.  The order is always cancelled before this returns anything but FILLED; STUCK is
        the case where the venue did not CONFIRM the cancel, and the caller must not post
        another order on top of one that may still be working.
        """
        executed = float(getattr(placed, "executed_qty", 0.0) or 0.0)
        avg = float(getattr(placed, "avg_price", 0.0) or 0.0) or price
        if str(getattr(placed, "status", "")).upper() == "FILLED" and executed > 0:
            return executed, avg, "FILLED"

        while True:
            over_budget = self._clock() - started >= budget_s
            await self._sleep(0.0 if over_budget else poll_s)
            cur = await self._lookup(symbol, cid)
            if cur is not None:
                executed = float(cur.executed_qty or 0.0)
                avg = float(cur.avg_price or 0.0) or price
                status = str(cur.status or "").upper()
                if status == "FILLED":
                    return executed, avg, "FILLED"
                if status in _TERMINAL:
                    # cancelled or expired underneath us: keep the partial, re-price the rest
                    return executed, avg, "REPRICE"
            if over_budget:
                got, px, gone = await self._cancel(symbol, cid, executed, avg, price)
                return got, px, ("BUDGET" if gone else "STUCK")
            fresh = await self._book(symbol, None)
            if fresh is not None and walked_away(side, price, fresh, tick):
                got, px, gone = await self._cancel(symbol, cid, executed, avg, price)
                log.info("maker: %s walked away from %g; cancelled with %g filled", symbol, price, got)
                return got, px, ("REPRICE" if gone else "STUCK")

    async def _cancel(self, symbol: str, cid: str, executed: float, avg: float,
                      price: float) -> tuple[float, float, bool]:
        """Cancel, and report ``(executed, price, gone)`` where ``gone`` is the venue's OWN
        confirmation that the order is no longer working.

        ``gone`` is only True when the venue said so: it accepted the DELETE, or it answered
        "unknown order" (-2011 / -2013, i.e. already gone), or a lookup came back in a terminal
        state.  A cancel that merely raised, and a lookup that still shows the order NEW or
        PARTIALLY_FILLED, both leave ``gone=False`` — the order may still be on the book, and
        the caller must not post another one on top of it.  Deltr never reports a quantity the
        venue did not report, and never assumes an order it could not confirm is dead.
        """
        gone = False
        res: Any = None
        for i in range(_CANCEL_ATTEMPTS):
            try:
                res = await self.futures.cancel_order_by_client_id(symbol, cid)
                gone = True  # the venue accepted the cancel (None = it was already gone)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("maker: cancel of %s failed (%s); reading the order instead", cid, exc)
                res = await self._lookup(symbol, cid)
                if res is not None and str(getattr(res, "status", "") or "").upper() in _TERMINAL:
                    gone = True
                    break
                if i < _CANCEL_ATTEMPTS - 1:
                    await self._sleep(_CANCEL_RETRY_S)
        if res is None:
            found = await self._lookup(symbol, cid)
            if found is not None:
                res = found
                if str(getattr(found, "status", "") or "").upper() not in _TERMINAL:
                    gone = False  # it is still working despite an accepted DELETE
        if res is not None:
            executed = float(res.executed_qty or 0.0)
            avg = float(res.avg_price or 0.0) or avg
        return executed, (avg or price), gone

    async def _lookup(self, symbol: str, cid: str) -> Any:
        fn = getattr(self.futures, "get_order_by_client_id", None)
        if not callable(fn):
            return None
        try:
            return await fn(symbol, cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("maker: lookup of %s failed: %s", cid, exc)
            return None


__all__ = ["MakerError", "MakerExecution", "MakerOutcome", "maker_price", "walked_away"]
