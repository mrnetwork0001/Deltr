"""deltr/onchain_leg.py — the DEX leg of a LIVE hedge, executed by the Binance Agentic Wallet.

Custody, stated once and true everywhere below: **Deltr never holds, reads, stores or signs
with a private key.**  It shells out to the Binance Agentic Wallet CLI (``baw``), which
custodies the key, enforces Binance's own spending limits and performs the signing, and reads
the JSON that comes back.  There is no signer in this file, no key parameter, and no raw
transaction anywhere in Deltr.  If the wallet declines, the CLI's own message is what surfaces.

Two clients, two jobs, deliberately not the same one:

* **price discovery** stays on the read-only PancakeSwap V3 quoter over a public BSC RPC, which
  is what the gate re-prices against;
* **execution** goes to the wallet CLI, and the resulting :class:`~deltr.models.Fill` carries
  ``source=binance-agentic-wallet`` with the transaction hash as its ``ref``, so a receipt can
  always be pointed at a real transaction.

The Fill is tagged ``venue=PANCAKESWAP_V3`` because that is the venue the liquidity came from
and the venue the plan, the position book and the gate's leg pairing all speak in; the wallet
is the *route*, and it is recorded in ``source`` and in the trace, never hidden.

A swap the CLI has not confirmed is never returned as a fill: ``fill_from_swap`` refuses it.
"""
from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Optional

from deltr.config import Settings
from deltr.models import DexQuote, Fill, MarketState, OrderLeg, Side, Venue
from deltr.venues.agentic_wallet import AgenticWalletError, fill_from_swap
from deltr.venues.pancake_constants import MAINNET_TOKENS, SYMBOL_MAP

log = logging.getLogger("deltr.onchain")


class OnchainLegError(RuntimeError):
    """The on-chain leg could not be executed.  ``submitted`` says whether anything was sent.

    ``submitted=False`` means the refusal happened before the wallet was asked to sign, so
    there is provably nothing on chain to unwind.
    """

    def __init__(self, reason: str, *, code: str = "ONCHAIN_LEG_FAILED", submitted: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.submitted = submitted


def tokens_for(symbol: str) -> tuple[str, str, str, str]:
    """``(base_symbol, base_address, quote_symbol, quote_address)`` for a perp symbol."""
    pair = SYMBOL_MAP.get(symbol.upper())
    if pair is None:
        raise OnchainLegError(f"no PancakeSwap token pair is mapped for {symbol}; refusing to guess token addresses")
    base_sym, quote_sym = pair
    try:
        base_addr = MAINNET_TOKENS[base_sym][0]
        quote_addr = MAINNET_TOKENS[quote_sym][0]
    except KeyError as exc:  # pragma: no cover - SYMBOL_MAP and MAINNET_TOKENS are kept in step
        raise OnchainLegError(f"token address missing for {exc}") from exc
    return base_sym, base_addr, quote_sym, quote_addr


class WalletDexLeg:
    """Executes the DEX leg of a plan through the wallet CLI, and reverses it the same way.

    ``guard`` is the engine's LIVE pre-flight for a value-moving on-chain request (arming,
    caps, kill switch, drawdown halt).  It is called with the USD notional BEFORE the wallet is
    asked for anything and raises to refuse.  It is additional to, never instead of, the
    deterministic risk gate that already approved the plan in the Executor.
    """

    def __init__(
        self,
        wallet: Any,
        settings: Settings,
        *,
        quoter: Any = None,
        guard: Optional[Callable[[float], Any]] = None,
        chain_id: Optional[int] = None,
    ) -> None:
        self.wallet = wallet
        self.settings = settings
        self.quoter = quoter
        self.guard = guard
        self.chain_id = int(chain_id if chain_id is not None else settings.wallet_chain_id)
        # A guard that understands ``reducing`` can tell an opening swap from one that CLOSES an
        # exposure; one that does not is called the old way and simply sees every leg as opening.
        self._last_quote: Optional[tuple[DexQuote, float]] = None
        self._guard_takes_reducing = False
        if guard is not None:
            try:
                self._guard_takes_reducing = "reducing" in inspect.signature(guard).parameters
            except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
                self._guard_takes_reducing = False

    # ------------------------------------------------------------------ price discovery
    async def requote(self, qty: float, ms: Optional[MarketState]) -> Optional[DexQuote]:
        """Read-only re-quote for the gate, from the public RPC.  Never the wallet.

        The result is kept because it is the exact price the gate approved the plan against,
        moments before the swap is sized.  See :meth:`_price_for`.
        """
        fn = getattr(self.quoter, "dex_quote", None)
        if callable(fn) and qty > 0:
            try:
                q = await fn(qty)
                if q is not None:
                    self._last_quote = (q, time.monotonic())
                return q
            except Exception as exc:  # noqa: BLE001
                log.warning("onchain: DEX re-quote failed (%s); using the hub's last quote", exc)
        return ms.dex if ms is not None else None

    # ------------------------------------------------------------------ execution
    async def fill(self, leg: OrderLeg, ms: Optional[MarketState], qty: Optional[float] = None,
                   plan_id: str = "", attempt: int = 1, *, ref_note: str = "") -> Fill:
        q = float(qty if qty is not None else leg.qty)
        if q <= 0:
            raise OnchainLegError(f"on-chain leg qty must be > 0 (got {q})")
        price_hint = self._price_for(ms, leg.side, float(leg.price_hint or 0.0))
        if price_hint <= 0:
            raise OnchainLegError("no reference price for the on-chain leg; refusing to size a swap blind")
        return await self._swap(leg.symbol, leg.side, q, price_hint, ms,
                                ref_note=ref_note, reducing=bool(leg.reduce_only))

    async def reverse(self, fill: Fill, ms: Optional[MarketState], plan_id: str) -> Fill:
        """Undo a filled on-chain leg with the opposite swap.  Real leg, real reversal.

        The reversal is priced off the CURRENT quote, never off the price the entry filled at.
        Pricing a sell-back at the buy price makes ``min_receive`` demand back the pool fee and
        the spread that were paid on the way in, so the wallet's preview check refuses a
        perfectly normal reversal and the leg is left naked.  A reversal exists to remove an
        exposure; refusing it is always worse than executing it at the going rate, and the
        slippage bound still applies against that rate.
        """
        side = Side.SELL if fill.side == Side.BUY else Side.BUY
        price = self._price_for(ms, side, float(fill.price or 0.0))
        return await self._swap(fill.symbol, side, fill.qty, price, ms, ref_note="reverse", reducing=True)

    async def reverse_partial(self, leg: OrderLeg, residual_qty: float, ms: Optional[MarketState], plan_id: str) -> Fill:
        side = Side.SELL if leg.side == Side.BUY else Side.BUY
        price_hint = self._price_for(ms, side, float(leg.price_hint or 0.0))
        return await self._swap(leg.symbol, side, float(residual_qty), price_hint, ms,
                                ref_note="reverse_partial", reducing=True)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _hint_from(ms: Optional[MarketState], side: Side) -> float:
        if ms is None or ms.dex is None:
            return 0.0
        return float(ms.dex.exec_price_buy if side == Side.BUY else ms.dex.exec_price_sell)

    def _price_for(self, ms: Optional[MarketState], side: Side, fallback: float) -> float:
        """The reference price used to SIZE a real swap: the live quote first, the caller's
        hint only when there is no live quote.

        A plan's ``price_hint`` is the price at PROPOSE time and may be a whole plan TTL old.
        Sizing a real swap off it is how a stale quote turns into either a swap the wallet
        refuses (spend too little, receive under ``min_receive``) or one that buys more base
        than the gate approved (spend too much).  The gate has already re-priced and passed
        PRICE_DRIFT by the time this runs, so the live quote is both fresher and the number the
        approval was actually made against.
        """
        gated = self._gate_price(side)
        if gated > 0:
            return gated
        live = self._hint_from(ms, side)
        return live if live > 0 else float(fallback or 0.0)

    def _gate_price(self, side: Side) -> float:
        """The price from the re-quote the gate just judged, while it is still fresh.

        ``Executor._execute_locked`` calls ``requote()`` and then hands the gate a proposal
        priced off it.  Using that same number to size the swap keeps approval and execution on
        one price instead of two; anything older than ``DELTR_DEX_STALE_MS`` is discarded rather
        than trusted.
        """
        if self._last_quote is None:
            return 0.0
        quote, at = self._last_quote
        if (time.monotonic() - at) * 1000.0 > float(self.settings.dex_stale_ms):
            return 0.0
        return float(quote.exec_price_buy if side == Side.BUY else quote.exec_price_sell)

    def _gas_usd(self, ms: Optional[MarketState]) -> float:
        if ms is not None and ms.dex is not None:
            return float(ms.dex.gas_usd or 0.0)
        return 0.0

    def _call_guard(self, notional: float, reducing: bool) -> None:
        """Run the engine's on-chain pre-flight, and never let a foreign exception escape.

        The engine raises ``OnchainRefused``, which is not an ``OnchainLegError``.  Left
        unmapped it travelled straight out of the router and out of ``Executor.execute``: the
        plan was consumed, the cash stayed reserved, no receipt was written, and on the second
        leg of an unwind the perp was already closed while the position book still said the
        trade was hedged.  Everything here refuses BEFORE the wallet is asked for anything, so
        ``submitted=False`` is provable.
        """
        if self.guard is None:
            return
        try:
            if self._guard_takes_reducing:
                self.guard(notional, reducing=reducing)
            else:
                self.guard(notional)
        except OnchainLegError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped, never swallowed
            raise OnchainLegError(
                f"the on-chain pre-flight refused this leg: {exc}",
                code=str(getattr(exc, "code", "ONCHAIN_REFUSED")), submitted=False,
            ) from exc

    async def _swap(self, symbol: str, side: Side, qty: float, price_hint: float,
                    ms: Optional[MarketState], *, ref_note: str, reducing: bool = False) -> Fill:
        s = self.settings
        base_sym, base_addr, quote_sym, quote_addr = tokens_for(symbol)
        notional = abs(qty * price_hint)
        slip = float(s.onchain_max_slippage_pct)

        if side == Side.BUY:
            # spend quote to receive base: the swap amount is denominated in the quote token
            from_token, to_token = quote_addr, base_addr
            amount = notional
            min_receive = qty * (1.0 - slip / 100.0)
        else:
            from_token, to_token = base_addr, quote_addr
            amount = qty
            min_receive = notional * (1.0 - slip / 100.0)

        # refuses BEFORE the wallet is asked for anything: nothing is submitted, nothing signed
        self._call_guard(notional, reducing)

        # An unavailable or signed-out wallet is a refusal in the same shape as any other leg
        # failure, so the Executor's reverse-on-failure path handles it instead of an unmapped
        # exception escaping the router.  Nothing has been submitted at this point.
        try:
            self.wallet.require_available()
            await self.wallet.require_signed_in()
        except AgenticWalletError as exc:
            raise OnchainLegError(f"the agentic wallet is not usable: {exc}", code=str(exc.code), submitted=False) from exc
        t0 = time.perf_counter()
        try:
            result = await self.wallet.swap(
                from_token, to_token, amount,
                chain_id=self.chain_id,
                slippage=f"{slip:g}",
                min_receive=min_receive,
                max_price_impact_pct=s.onchain_max_price_impact_pct,
                wait_for_confirmation=True,
            )
        except AgenticWalletError as exc:
            submitted = exc.code not in {"SWAP_PREVIEW_REJECTED", "BAW_NOT_INSTALLED", "BAW_NOT_SIGNED_IN"}
            raise OnchainLegError(
                f"the agentic wallet did not complete the {side.value} of {qty:g} {base_sym}: {exc}",
                code=str(exc.code), submitted=submitted,
            ) from exc

        try:
            fill = fill_from_swap(
                result,
                leg_index=0,
                symbol=symbol,
                side=side,
                fee_usd=self._gas_usd(ms),
                venue=Venue.PANCAKESWAP_V3,   # the liquidity venue; the wallet is the route (see source)
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        except AgenticWalletError as exc:
            # confirmed=False: the swap may or may not be on chain.  Say so; never claim a fill,
            # and never claim it is safe to assume nothing happened.
            raise OnchainLegError(
                f"the wallet has not confirmed the swap (order {result.order_id or '<unknown>'}, "
                f"status {result.status}): {exc}",
                code="SWAP_UNCONFIRMED", submitted=True,
            ) from exc
        if ref_note:
            fill = fill.model_copy(update={"client_id": ref_note})
        return fill


__all__ = ["OnchainLegError", "WalletDexLeg", "tokens_for"]
