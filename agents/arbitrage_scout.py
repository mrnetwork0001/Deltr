"""agents/arbitrage_scout.py — Subsystem 1: the Arbitrage Scout.

Per market tick the scout turns a :class:`~deltr.models.MarketState` into a
horizon-based :class:`~deltr.models.EdgeBreakdown` and an
:class:`~deltr.models.ArbOpportunity`, appends a :class:`~deltr.models.SpreadPoint`
to the rolling history and emits a ``scan`` event.

Conventions (see DESIGN_FINAL.md §3.4 / §4.7)
* ``net(H) = basis_entry + funding(H) − roundtrip − basis_exit_assumed`` computed by
  :func:`deltr.edge.net_edge` (perp ref = unslipped mark; slippage lives in the round trip).
* Funding sign discipline: a negative ``lastFundingRate`` means the short PAYS, so the
  funding component is negative and shows as a cost.
* Actionable checks are ordered: freshness → price sanity → DEX impact → funding window →
  ``net_edge ≥ min_edge``.  The first failing check is the reason string.
* Funding window rule: never actionable within 15 min before a settlement whose
  ``lastFundingRate < 0`` (reason ``"funding_window"``).
* The runtime min-edge knob is clamped to ``[floor, 50]`` where the floor is 0 in PAPER and
  the measured round-trip cost in TESTNET (``Settings.min_edge_floor_bps``).

The scout never touches a venue client and never writes to stdout.  ``State`` is
duck-typed (only ``market``, ``edge``, ``opportunity``, ``history``, ``min_edge_bps`` and
``emit`` are used) so this module loads whether or not ``deltr.state`` exists.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from deltr import edge as edge_math
from deltr.config import Settings
from deltr.edge import HedgeSizing, size_for_capital
from deltr.models import (
    ArbOpportunity,
    EdgeBreakdown,
    Freshness,
    FundingSnapshot,
    MarketState,
    SpreadPoint,
    SymbolFilters,
)

log = logging.getLogger("deltr.scout")

MIN_EDGE_CEILING_BPS = 50.0
FUNDING_WINDOW_MINUTES = 15.0
DEFAULT_ROUNDTRIP_BPS = 20.0  # gate default when nothing has been measured yet (RiskLimits.default_roundtrip_cost_bps)


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def _now_ms(now_ms: Optional[int] = None) -> int:
    return int(now_ms) if now_ms is not None else int(time.time() * 1000)


def compute_edge(
    ms: MarketState, notional_usd: float, horizon_h: float, cfg: Settings, now_ms: Optional[int] = None
) -> EdgeBreakdown:
    """Thin wrapper over :func:`deltr.edge.net_edge` (the single edge convention)."""
    return edge_math.net_edge(ms, notional_usd, horizon_h, cfg, now_ms=now_ms)


def ms_to_next_settlement(funding: FundingSnapshot, now_ms: int) -> int:
    """Milliseconds until the next funding settlement, rolling a stale timestamp forward."""
    interval_ms = int(max(funding.interval_h, 1) * 3_600_000)
    t = int(funding.next_funding_time_ms)
    while t <= now_ms:
        t += interval_ms
    return t - now_ms


def funding_window_block(
    funding: Optional[FundingSnapshot], now_ms: Optional[int] = None, window_minutes: float = FUNDING_WINDOW_MINUTES
) -> bool:
    """True when we are within ``window_minutes`` BEFORE a settlement whose rate is negative.

    Opening a short perp right before paying funding is a certain cost, so the scout refuses
    to mark such a tick actionable.  A zero or positive rate never blocks.
    """
    if funding is None or funding.last_funding_rate >= 0:
        return False
    remaining_ms = ms_to_next_settlement(funding, _now_ms(now_ms))
    return remaining_ms <= window_minutes * 60_000


def _stale_reason(freshness: Freshness) -> str:
    reason = freshness.reason or "unknown"
    if reason == "dex_stale":
        return f"stale: dex_age {freshness.dex_age_ms}ms"
    if reason == "cex_stale":
        return f"stale: cex_age {freshness.cex_age_ms}ms"
    return f"stale: {reason}"


def actionable_reason(
    edge: EdgeBreakdown,
    freshness: Freshness,
    min_edge_bps: float,
    price_sanity_bps: float,
    max_impact_bps: float,
    funding_window_block: bool,
) -> str:
    """Return ``"ok"`` or the FIRST failing reason, in this fixed order:

    1. freshness      → ``"stale: dex_age 9000ms"`` / ``"stale: cex_age 6000ms"`` / ``"stale: feed_stale(stress)"``
    2. price sanity   → ``"price_sanity 140.0 bps"`` (|perp_ref − dex_exec| relative to dex_exec)
    3. DEX impact     → ``"impact 6.2 > 5.0 bps"``
    4. funding window → ``"funding_window"``
    5. net edge       → ``"net_edge 1.2 < 3.0 bps"``
    """
    if not freshness.ok:
        return _stale_reason(freshness)
    divergence = abs(edge.basis_entry_bps)
    if divergence > price_sanity_bps:
        return f"price_sanity {divergence:.1f} bps"
    if edge.dex_impact_bps > max_impact_bps:
        return f"impact {edge.dex_impact_bps:.1f} > {max_impact_bps:.1f} bps"
    if funding_window_block:
        return "funding_window"
    if edge.net_edge_bps < min_edge_bps:
        return f"net_edge {edge.net_edge_bps:.1f} < {min_edge_bps:.1f} bps"
    return "ok"


def build_opportunity(
    ms: MarketState,
    size_base: float,
    notional_usd: float,
    cfg: Settings,
    min_edge_bps: float,
    horizon_h: float,
    now_ms: Optional[int] = None,
) -> ArbOpportunity:
    """Edge + actionable verdict for one tick at a given size.

    Raises ``ValueError`` when the MarketState lacks a DEX quote or funding snapshot.
    """
    if ms.dex is None or ms.funding is None:
        raise ValueError("MarketState needs dex and funding to build an opportunity")
    now = _now_ms(now_ms)
    e = compute_edge(ms, notional_usd, horizon_h, cfg, now_ms=now)
    perp_ref = ms.perp_ref_price if ms.perp_ref_price else ms.funding.mark_price
    reason = actionable_reason(
        e,
        ms.freshness,
        min_edge_bps=min_edge_bps,
        price_sanity_bps=cfg.price_sanity_bps,
        max_impact_bps=cfg.max_dex_impact_bps,
        funding_window_block=funding_window_block(ms.funding, now),
    )
    return ArbOpportunity(
        symbol=ms.symbol,
        dex_price=ms.dex.exec_price_buy,
        perp_price=perp_ref,
        edge=e,
        size_base=float(size_base),
        notional_usd=float(notional_usd),
        horizon_h=horizon_h,
        is_actionable=reason == "ok",
        reason=reason,
        min_edge_bps_used=float(min_edge_bps),
        freshness=ms.freshness,
        ts=ms.ts,
    )


def spread_point(ms: MarketState, opp: ArbOpportunity) -> SpreadPoint:
    """One chart point for the rolling history (DEX exec vs perp ref, net edge, funding)."""
    return SpreadPoint(
        ts=ms.ts,
        dex_exec=opp.dex_price,
        perp_ref=opp.perp_price,
        basis_bps=opp.edge.basis_entry_bps,
        net_edge_bps=opp.edge.net_edge_bps,
        funding_rate=opp.edge.funding_rate_last,
        actionable=opp.is_actionable,
        source=ms.source,
    )


# --------------------------------------------------------------------------- #
# Scout                                                                        #
# --------------------------------------------------------------------------- #
class ArbitrageScout:
    """Stateful per-tick scanner.  ``state`` is duck-typed (see module docstring)."""

    def __init__(self, state: Any, settings: Settings, filters: SymbolFilters) -> None:
        self.state = state
        self.settings = settings
        self.filters = filters
        if getattr(state, "min_edge_bps", None) is None:
            state.min_edge_bps = float(settings.min_edge_bps)

    # -- knobs ------------------------------------------------------------------
    @property
    def min_edge_bps(self) -> float:
        v = getattr(self.state, "min_edge_bps", None)
        return float(v) if v is not None else float(self.settings.min_edge_bps)

    def measured_roundtrip_bps(self) -> float:
        """Latest measured round-trip cost (gate default until the first tick)."""
        e = getattr(self.state, "edge", None)
        return float(e.roundtrip_cost_bps) if e is not None else DEFAULT_ROUNDTRIP_BPS

    def min_edge_floor(self) -> float:
        """0 in PAPER; the measured round trip in TESTNET (a real-order run can never target a loss)."""
        return float(self.settings.min_edge_floor_bps(self.measured_roundtrip_bps()))

    def set_min_edge(self, bps: float) -> float:
        """Clamp to ``[floor, 50]``, store on State, return the effective value."""
        floor = self.min_edge_floor()
        effective = min(max(float(bps), floor), MIN_EDGE_CEILING_BPS)
        self.state.min_edge_bps = effective
        if effective != float(bps):
            log.info("min_edge clamped: requested %.2f → %.2f bps (floor %.2f, ceiling %.1f)", bps, effective, floor, MIN_EDGE_CEILING_BPS)
        self._emit("scan", f"min edge set to {effective:.2f} bps", data={"requested": float(bps), "effective": effective, "floor": floor})
        return effective

    # -- ticks ------------------------------------------------------------------
    def _default_size(self, ms: MarketState) -> tuple[float, float]:
        """(size_base, notional) the tick edge is measured at: the size the DEX was quoted for."""
        assert ms.dex is not None
        size = float(ms.dex.size_base) if ms.dex.size_base > 0 else 0.0
        notional = float(ms.dex.amount_in_usdt) if ms.dex.amount_in_usdt > 0 else size * ms.dex.exec_price_buy
        return size, notional

    async def on_tick(self, ms: MarketState) -> ArbOpportunity:
        """Store ``state.market/edge/opportunity``, append a SpreadPoint, emit ``scan``.

        When the tick lacks a DEX quote or funding (venue down), the previous opportunity is
        re-issued as non-actionable; if there is none yet, ``ValueError`` propagates.
        """
        self.state.market = ms
        if ms.dex is None or ms.funding is None:
            prev = getattr(self.state, "opportunity", None)
            missing = "dex missing" if ms.dex is None else "funding missing"
            if prev is None:
                raise ValueError(f"cannot scan: {missing}")
            opp = prev.model_copy(update={"is_actionable": False, "reason": f"stale: {missing}", "freshness": ms.freshness, "ts": ms.ts})
            self.state.opportunity = opp
            self._emit("scan", f"scan skipped: {missing}", level="warn", data={"reason": opp.reason})
            return opp
        size, notional = self._default_size(ms)
        opp = build_opportunity(ms, size, notional, self.settings, self.min_edge_bps, self.settings.funding_horizon_hours)
        self.state.edge = opp.edge
        self.state.opportunity = opp
        hist = getattr(self.state, "history", None)
        if hist is not None:
            hist.append(spread_point(ms, opp))
        self._emit(
            "scan",
            f"net edge {opp.edge.net_edge_bps:+.2f} bps ({'actionable' if opp.is_actionable else opp.reason})",
            data={
                "opportunity_id": opp.id, "net_edge_bps": round(opp.edge.net_edge_bps, 4),
                "basis_bps": round(opp.edge.basis_entry_bps, 4), "roundtrip_bps": round(opp.edge.roundtrip_cost_bps, 4),
                "funding_bps": round(opp.edge.funding_bps_horizon, 4), "actionable": opp.is_actionable,
                "reason": opp.reason, "min_edge_bps": opp.min_edge_bps_used,
            },
        )
        return opp

    def scan(
        self, notional_usd: Optional[float] = None, horizon_h: Optional[float] = None, min_edge_bps: Optional[float] = None
    ) -> ArbOpportunity:
        """Re-evaluate the latest MarketState with optional overrides (does not mutate history)."""
        ms = getattr(self.state, "market", None)
        if ms is None:
            raise ValueError("no market data yet")
        if ms.dex is None or ms.funding is None:
            raise ValueError("market incomplete: dex or funding missing")
        size, notional = self._default_size(ms)
        if notional_usd is not None and notional_usd > 0:
            notional = float(notional_usd)
            size = notional / ms.dex.exec_price_buy
        opp = build_opportunity(
            ms, size, notional, self.settings,
            self.min_edge_bps if min_edge_bps is None else float(min_edge_bps),
            self.settings.funding_horizon_hours if horizon_h is None else float(horizon_h),
        )
        if notional_usd is None and horizon_h is None and min_edge_bps is None:
            self.state.edge = opp.edge
            self.state.opportunity = opp
        return opp

    def explain(self, capital_usd: float, leverage: float, horizon_h: float) -> tuple[EdgeBreakdown, HedgeSizing]:
        """Edge at the size ``capital_usd`` @ ``leverage`` would deploy (``N = C/(1+1/L)``, step-aligned)."""
        ms = getattr(self.state, "market", None)
        if ms is None or ms.dex is None or ms.funding is None:
            raise ValueError("no market data yet")
        sizing = size_for_capital(
            float(capital_usd), float(leverage), ms.dex.exec_price_buy, self.filters,
            self.settings.max_notional_usd, self.settings.max_dex_impact_bps,
        )
        notional = sizing.notional_usd if sizing.notional_usd > 0 else self._default_size(ms)[1]
        e = compute_edge(ms, notional, float(horizon_h), self.settings)
        return e, sizing

    def history(self, n: int = 600) -> list[SpreadPoint]:
        hist = getattr(self.state, "history", None) or []
        items = list(hist)
        return items[-n:] if n > 0 else []

    # -- events -----------------------------------------------------------------
    def _emit(self, topic: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        emit = getattr(self.state, "emit", None)
        if callable(emit):
            try:
                emit(topic, message, level, data or {})
                return
            except Exception:  # never let telemetry break the tick
                log.exception("state.emit failed")
        log.log(logging.WARNING if level in ("warn", "error") else logging.DEBUG, "[%s] %s", topic, message)


__all__ = [
    "MIN_EDGE_CEILING_BPS", "FUNDING_WINDOW_MINUTES", "compute_edge", "ms_to_next_settlement", "funding_window_block",
    "actionable_reason", "build_opportunity", "spread_point", "ArbitrageScout",
]
