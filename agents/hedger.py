"""agents/hedger.py — Subsystem 2: the Hedger.

Turns ``capital_usd @ leverage`` into a step-aligned, two-leg
:class:`~deltr.models.HedgePlan` (``[DEX BUY, PERP SELL]``) and builds the symmetric
reduce-only unwind plan for an open :class:`~deltr.models.Position`.

Sizing (DESIGN_FINAL.md §3.5, decision 6): the DEX leg is unlevered and the perp leg needs
``notional / L`` margin, so ``N = capital / (1 + 1/L)``; the quantity is floored to the
exchange step with Decimal arithmetic by :func:`deltr.edge.size_for_capital`, capped by the
per-mode notional cap and shrunk while the DEX impact exceeds ``max_dex_impact_bps``.

Golden: "$5,000 at 2x" at a DEX exec of 686.191 → 4.85 BNB, notional ≈ $3,328, margin ≈ $1,664,
cash required ≈ $4,992.

The hedger never talks to a venue; a DEX re-quote callback may be injected by the engine.
It never builds a :class:`~deltr.models.TradeProposal` (only the Executor does).
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Union

from deltr.config import Settings
from deltr.edge import HedgeSizing, size_for_capital
from deltr.models import (
    ArbOpportunity,
    DexQuote,
    EdgeBreakdown,
    HedgePlan,
    MarketState,
    OrderLeg,
    Position,
    Side,
    SymbolFilters,
    TraceSource,
    Venue,
    utcnow,
)

from agents.arbitrage_scout import compute_edge

log = logging.getLogger("deltr.hedger")

DexQuoteFn = Callable[[float], Union[DexQuote, Awaitable[DexQuote]]]


class SizingError(ValueError):
    """Capital cannot be turned into a tradable quantity (below the exchange minimum)."""


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def _dex_exec_price(opp_or_ms: Union[ArbOpportunity, MarketState]) -> float:
    if isinstance(opp_or_ms, ArbOpportunity):
        return float(opp_or_ms.dex_price)
    if opp_or_ms.dex is None:
        raise ValueError("MarketState has no DEX quote")
    return float(opp_or_ms.dex.exec_price_buy)


def _perp_ref_price(ms: MarketState) -> float:
    if ms.perp_ref_price:
        return float(ms.perp_ref_price)
    if ms.funding is not None:
        return float(ms.funding.mark_price)
    if ms.cex_perp_book is not None:
        return float(ms.cex_perp_book.mid)
    raise ValueError("MarketState has no perp reference price")


def size_hedge(
    opp_or_ms: Union[ArbOpportunity, MarketState],
    capital_usd: float,
    leverage: float,
    filters: SymbolFilters,
    cfg: Settings,
    dex_quote_at: Optional[Callable[[float], DexQuote]] = None,
) -> HedgeSizing:
    """Step-aligned sizing for ``capital_usd @ leverage`` at the current DEX exec price.

    ``dex_quote_at`` (synchronous ``qty → DexQuote``) drives the impact-cap shrink loop when
    given; without it the quoted size's impact is assumed to hold.
    """
    price = _dex_exec_price(opp_or_ms)
    impact_at = (lambda q: float(dex_quote_at(q).impact_bps)) if dex_quote_at is not None else None
    return size_for_capital(
        float(capital_usd), float(leverage), price, filters,
        max_notional_usd=cfg.max_notional_usd, max_impact_bps=cfg.max_dex_impact_bps, impact_at=impact_at,
    )


def build_plan(
    ms: MarketState,
    sizing: HedgeSizing,
    edge: EdgeBreakdown,
    cfg: Settings,
    symbol: str,
    source: TraceSource,
    client: Optional[str],
    prompt: Optional[str],
    opportunity_id: Optional[str],
    now: Optional[datetime] = None,
) -> HedgePlan:
    """Two-leg entry plan ``[DEX BUY, PERP SELL]``; ``expires_at = now + plan_ttl_seconds``."""
    created = now or utcnow()
    qty = float(sizing.qty)
    dex_px = _dex_exec_price(ms)
    perp_px = _perp_ref_price(ms)
    legs = [
        OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=symbol, side=Side.BUY, qty=qty, price_hint=dex_px, leverage=1.0),
        OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=symbol, side=Side.SELL, qty=qty, price_hint=perp_px, leverage=float(sizing.leverage)),
    ]
    return HedgePlan(
        opportunity_id=opportunity_id,
        symbol=symbol,
        legs=legs,
        qty=qty,
        notional_usd=float(sizing.notional_usd),
        leverage=float(sizing.leverage),
        margin_usd=float(sizing.margin_usd),
        cash_required_usd=float(sizing.cash_required_usd),
        allocated_risk_usd=float(edge.allocated_risk_usd),
        expected_edge_bps=float(edge.net_edge_bps),
        roundtrip_cost_bps=float(edge.roundtrip_cost_bps),
        ref_dex_price=dex_px,
        ref_perp_price=perp_px,
        reduce_only=False,
        source=source,
        client=client,
        prompt=prompt,
        created_at=created,
        expires_at=created + timedelta(seconds=float(cfg.plan_ttl_seconds)),
    )


def unwind_plan(
    pos: Position,
    ms: MarketState,
    cfg: Settings,
    source: TraceSource,
    client: Optional[str],
    reason: str,
    now: Optional[datetime] = None,
) -> HedgePlan:
    """Reduce-only plan ``[DEX SELL, PERP BUY]`` bound to ``pos.id`` with the position's quantities."""
    created = now or utcnow()
    dex_px = float(ms.dex.exec_price_sell) if ms.dex is not None and ms.dex.exec_price_sell > 0 else float(pos.dex_entry)
    try:
        perp_px = _perp_ref_price(ms)
    except ValueError:
        perp_px = float(pos.perp_entry)
    qty = float(pos.perp_qty)
    notional = qty * perp_px
    if ms.dex is not None and ms.funding is not None and notional > 0:
        roundtrip = compute_edge(ms, notional, cfg.funding_horizon_hours, cfg).roundtrip_cost_bps
    else:
        roundtrip = 20.0
    legs = [
        OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=pos.symbol, side=Side.SELL, qty=float(pos.dex_qty), price_hint=dex_px, reduce_only=True, leverage=1.0),
        OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=pos.symbol, side=Side.BUY, qty=qty, price_hint=perp_px, reduce_only=True, leverage=float(pos.leverage)),
    ]
    return HedgePlan(
        opportunity_id=None,
        symbol=pos.symbol,
        legs=legs,
        qty=qty,
        notional_usd=notional,
        leverage=float(pos.leverage),
        margin_usd=float(pos.margin_usd),
        cash_required_usd=0.0,
        allocated_risk_usd=float(pos.allocated_risk_usd),
        expected_edge_bps=0.0,
        roundtrip_cost_bps=float(roundtrip),
        ref_dex_price=dex_px,
        ref_perp_price=perp_px,
        reduce_only=True,
        position_id=pos.id,
        source=source,
        client=client,
        prompt=f"unwind:{reason}",
        created_at=created,
        expires_at=created + timedelta(seconds=float(cfg.plan_ttl_seconds)),
    )


# --------------------------------------------------------------------------- #
# Hedger                                                                       #
# --------------------------------------------------------------------------- #
class Hedger:
    """Sizes and stores entry plans on the latest MarketState.  ``state`` is duck-typed
    (``market``, ``opportunity``, ``put_plan``, ``emit``)."""

    def __init__(
        self,
        state: Any,
        settings: Settings,
        filters: SymbolFilters,
        dex_quote_at: Optional[DexQuoteFn] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.state = state
        self.settings = settings
        self.filters = filters
        self.dex_quote_at: Optional[DexQuoteFn] = dex_quote_at  # engine injects the (cached) PancakeSwap quoter
        self.clock: Callable[[], datetime] = clock or utcnow  # injectable for replay determinism (plan_hash covers created_at)

    async def _requote(self, qty: float) -> Optional[DexQuote]:
        if self.dex_quote_at is None or qty <= 0:
            return None
        try:
            res = self.dex_quote_at(qty)
            if inspect.isawaitable(res):
                res = await res
            return res  # type: ignore[return-value]
        except Exception as exc:  # a failed re-quote falls back to the tick quote (plan still re-priced at execute)
            log.warning("DEX re-quote at %.4f failed: %s", qty, exc)
            return None

    async def propose(
        self,
        capital_usd: float,
        leverage: Optional[float],
        symbol: Optional[str],
        source: TraceSource,
        client: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> HedgePlan:
        """Size on the latest MarketState, re-quote the DEX at the final qty, store the plan in State."""
        ms: Optional[MarketState] = getattr(self.state, "market", None)
        if ms is None or ms.dex is None or ms.funding is None:
            raise ValueError("no market data yet: cannot size a hedge")
        lev = float(leverage) if leverage is not None else float(self.settings.default_leverage)
        sym = (symbol or self.settings.symbol).upper()
        if capital_usd <= 0:
            raise SizingError("capital_usd must be > 0")

        sync_quote = self.dex_quote_at if (self.dex_quote_at is not None and not inspect.iscoroutinefunction(self.dex_quote_at)) else None
        sizing = size_hedge(ms, capital_usd, lev, self.filters, self.settings, dex_quote_at=sync_quote)  # type: ignore[arg-type]
        if float(sizing.qty) <= 0:
            raise SizingError(
                f"${capital_usd:,.2f} at {lev:g}x sizes below the exchange minimum "
                f"({self.filters.min_qty} {sym[:-4] or sym}); increase capital."
            )
        quote = await self._requote(float(sizing.qty))
        if quote is not None and quote.exec_price_buy > 0:
            ms = ms.model_copy(update={"dex": quote})
            notional = float(sizing.qty) * quote.exec_price_buy
            sizing = HedgeSizing(
                qty=sizing.qty, notional_usd=notional, margin_usd=notional / lev,
                cash_required_usd=notional + notional / lev, leverage=lev, capped_by=sizing.capped_by,
            )
        e = compute_edge(ms, sizing.notional_usd, self.settings.funding_horizon_hours, self.settings)
        opp = getattr(self.state, "opportunity", None)
        plan = build_plan(ms, sizing, e, self.settings, sym, source, client, prompt, opp.id if opp is not None else None, now=self.clock())
        put = getattr(self.state, "put_plan", None)
        if callable(put):
            put(plan)
        else:
            plans = getattr(self.state, "plans", None)
            if isinstance(plans, dict):
                plans[plan.id] = plan
        self._emit(
            "plan",
            f"plan {plan.id}: {plan.qty:g} {sym} notional ${plan.notional_usd:,.2f} @ {lev:g}x (capped_by={sizing.capped_by})",
            data={
                "plan_id": plan.id, "qty": plan.qty, "notional_usd": round(plan.notional_usd, 2),
                "margin_usd": round(plan.margin_usd, 2), "cash_required_usd": round(plan.cash_required_usd, 2),
                "capped_by": sizing.capped_by, "expected_edge_bps": round(plan.expected_edge_bps, 3),
                "source": source.value, "client": client, "expires_at": plan.expires_at.isoformat(),
            },
        )
        return plan

    def _emit(self, topic: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        emit = getattr(self.state, "emit", None)
        if callable(emit):
            try:
                emit(topic, message, level, data or {})
                return
            except Exception:
                log.exception("state.emit failed")
        log.debug("[%s] %s", topic, message)


__all__ = ["SizingError", "size_hedge", "build_plan", "unwind_plan", "Hedger"]
