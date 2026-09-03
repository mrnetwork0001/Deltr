"""deltr/portfolio.py — paper/testnet book: positions, mark-to-close, funding, stops, stress, persistence.

The Portfolio is the single owner of cash and open positions.  It feeds the gate
(``gate.update_equity`` on every mark, ``register_position`` / ``release_position``
on open / close) and never lets a position look green when closing it now would
be red: every unrealized number is **mark-to-close** via ``deltr.edge.mark_to_close``.

Stress scenarios (section 4.12) mutate *only* this object (and the leg-failure flag
the router consumes) — never the market feed.  Everything a scenario changes is
labelled ``SIMULATED`` and reversible via ``clear_stress``.

Cash model
    reserve(cash)            cash -> reserved (before the first leg)
    open_position(plan, ..)  reserved -> locked in the position (notional + margin)
    close_position(..)       locked + realized pnl -> cash
    equity = cash + reserved + Σ(locked + unrealized + funding) − equity_shock(SIMULATED)
    unrealized = spread − entry fees − estimated exit cost (funding shown separately)
    stop_distance = allocated_risk + unrealized + funding   (≤ 0 ⇒ "position_stop")
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from deltr.config import Settings
from deltr.edge import (
    MarkToClose,
    liquidation_price_est,
    mark_to_close,
    perp_reference,
    settlements_between,
)
from deltr.models import (
    Fill,
    FundingSnapshot,
    HedgePlan,
    MarketState,
    PortfolioSnapshot,
    Position,
    StressKind,
    StressResult,
    StressScenario,
    Venue,
    utcnow,
)

log = logging.getLogger("deltr.portfolio")

EQUITY_CURVE_LEN = 600
CLOSED_KEEP = 20
FUNDING_FLIP_WINDOW_MS = 15 * 60_000  # the scout's pre-settlement window (agents.arbitrage_scout.FUNDING_WINDOW_MINUTES)


@dataclass
class StressFlags:
    """Active SIMULATED overrides.  Mutated only by ``apply_stress`` / ``clear_stress``."""

    label: Optional[str] = None
    basis_shock_bps: float = 0.0
    equity_shock_usd: float = 0.0
    funding_rate_override: Optional[float] = None
    dex_leg_fail: bool = False  # consumed (cleared) by the router on the next DEX leg
    feed_stale: bool = False

    def active(self) -> bool:
        return self.label is not None


@dataclass
class _PosMeta:
    """Per-position bookkeeping that is not part of the public Position model."""

    entry_fees_usd: float = 0.0
    next_funding_ms: Optional[int] = None  # next settlement instant we have NOT yet accrued
    last_mark: Optional[MarkToClose] = None
    last_mark_price: float = 0.0
    last_rate: float = 0.0
    last_dex_sell: float = 0.0
    last_perp_buy: float = 0.0
    settlements_remaining: int = 0
    last_mark_ms: int = 0
    next_settlement_ms: Optional[int] = None  # live next settlement at the last mark (the funding-flip window)
    extra: Dict[str, Any] = field(default_factory=dict)


def _ms_now(now: Optional[datetime]) -> int:
    return int((now or utcnow()).timestamp() * 1000)


class Portfolio:
    """Positions + cash + mark-to-close + funding + stress + persistence (section 4.9)."""

    def __init__(self, state: Any, gate: Any, settings: Settings, persist_path: Optional[str]) -> None:
        self.state = state
        self.gate = gate
        self.settings = settings
        self.persist_path = persist_path
        self.min_qty: float = 0.01  # exchange LOT_SIZE minimum; the engine sets it from SymbolFilters
        self.cash_usd: float = float(settings.capital_usd)
        self.reserved_cash_usd: float = 0.0
        self._positions: Dict[str, Position] = {}
        self._meta: Dict[str, _PosMeta] = {}
        self._closed: Deque[Position] = deque(maxlen=CLOSED_KEEP)
        self.equity_curve: Deque[tuple[datetime, float]] = deque(maxlen=EQUITY_CURVE_LEN)
        self.daily_realized_usd: float = 0.0
        self._day: str = utcnow().date().isoformat()
        self.realized_total_usd: float = 0.0
        self.realized_spread_usd: float = 0.0
        self.realized_funding_usd: float = 0.0
        self.fees_paid_usd: float = 0.0
        self.stress_flags = StressFlags()
        self._last_ms: Optional[MarketState] = None
        self._last_snapshot: Optional[PortfolioSnapshot] = None
        self._sync_state()

    # ------------------------------------------------------------------ cash
    def reserve(self, cash_usd: float) -> None:
        """Move `cash_usd` from free cash to reserved (before the first leg)."""
        if cash_usd < 0:
            raise ValueError("reserve amount must be >= 0")
        if cash_usd > self.cash_usd + 1e-9:
            raise ValueError(f"insufficient cash: need {cash_usd:.2f}, have {self.cash_usd:.2f}")
        self.cash_usd -= cash_usd
        self.reserved_cash_usd += cash_usd
        self._sync_state()

    def release(self, cash_usd: float) -> None:
        """Return a reservation to free cash (failed / vetoed execution)."""
        amt = min(max(0.0, cash_usd), self.reserved_cash_usd)
        self.reserved_cash_usd -= amt
        self.cash_usd += amt
        self._sync_state()

    def book_realized(self, pnl_usd: float, fees_usd: float = 0.0, reason: str = "") -> None:
        """Book a realized amount outside a position (e.g. the cost of reversing a
        first leg after the second failed).  Cash moves by `pnl_usd`."""
        self._roll_day()
        self.cash_usd += pnl_usd
        self.realized_total_usd += pnl_usd
        self.daily_realized_usd += pnl_usd
        self.fees_paid_usd += fees_usd
        log.info("portfolio: booked realized %.4f USD (fees %.4f) %s", pnl_usd, fees_usd, reason)
        self._sync_state()

    # ------------------------------------------------------------------ positions
    @staticmethod
    def _split_fills(fills: List[Fill]) -> tuple[Fill, Fill]:
        dex = next((f for f in fills if f.venue == Venue.PANCAKESWAP_V3), None)
        perp = next((f for f in fills if f.venue == Venue.BINANCE_FUTURES), None)
        if dex is None or perp is None:
            raise ValueError("need one PancakeSwap fill and one Binance Futures fill")
        return dex, perp

    @staticmethod
    def _one_fill(fills: List[Fill]) -> Optional[Fill]:
        """The single fill of a one-legged close (``None`` when both legs are present)."""
        venues = {f.venue for f in fills}
        if len(fills) == 1 and len(venues) == 1:
            return fills[0]
        return None

    def open_position(self, plan: HedgePlan, fills: List[Fill]) -> Position:
        """Open from the executed fills (qty comes from the fills, never the plan)
        and register the position with the gate."""
        dex, perp = self._split_fills(fills)
        entry_fees = sum(f.fee_usd for f in fills)
        notional = dex.qty * dex.price
        margin = perp.qty * perp.price / plan.leverage if plan.leverage > 0 else 0.0
        locked = notional + margin
        # consume the reservation made for this plan; any difference goes back to cash
        reserved = min(plan.cash_required_usd, self.reserved_cash_usd)
        self.reserved_cash_usd -= reserved
        self.cash_usd += reserved - locked
        basis_entry = 0.0 if dex.price <= 0 else (perp.price - dex.price) / dex.price * 1e4
        pos = Position(
            plan_id=plan.id,
            symbol=plan.symbol,
            dex_qty=dex.qty,
            dex_entry=dex.price,
            perp_qty=perp.qty,
            perp_entry=perp.price,
            leverage=plan.leverage,
            margin_usd=margin,
            notional_usd=notional,
            allocated_risk_usd=plan.allocated_risk_usd,
            basis_entry_bps=basis_entry,
            opened_at=max(dex.ts, perp.ts),
            delta_base=dex.qty - perp.qty,
            liq_price_est=liquidation_price_est(perp.price, plan.leverage) if plan.leverage > 0 else 0.0,
            stress_applied=self.stress_flags.label,
        )
        meta = _PosMeta(entry_fees_usd=entry_fees)
        ms = self._last_ms
        if ms is not None and ms.funding is not None:
            # seed from the MARKET STATE's clock, never the wall clock: replay and tests
            # must advance on simulated time, and a stale wall clock would roll the
            # first settlement past the book's own timeline (funding would never accrue).
            meta.next_funding_ms = self._roll_forward(ms.funding.next_funding_time_ms, ms.funding.interval_h, _ms_now(ms.ts))
            meta.last_mark_price = ms.funding.mark_price
            meta.last_rate = ms.funding.last_funding_rate
        self._positions[pos.id] = pos
        self._meta[pos.id] = meta
        self.gate.register_position(pos.id, pos.symbol, perp.qty, notional, pos.allocated_risk_usd, plan.leverage)
        self._mark_position(pos, ms, self._book_now_ms())
        self._sync_state()
        self.persist()
        log.info("portfolio: opened %s qty %.4f notional %.2f (fees %.4f)", pos.id, perp.qty, notional, entry_fees)
        return pos

    def open_naked_leg(self, plan: HedgePlan, fill: Fill) -> Position:
        """Book a ONE-legged position after a second leg failed AND its reversal failed.

        The naked leg is never hidden: it is marked, stop-monitored, registered with the
        gate (so capacity / aggregate checks see it) and blocks ``--auto`` until an
        operator unwinds it.  The other leg's quantity is 0.
        """
        is_dex = fill.venue == Venue.PANCAKESWAP_V3
        notional = fill.qty * fill.price if is_dex else 0.0
        margin = 0.0 if is_dex else (fill.qty * fill.price / plan.leverage if plan.leverage > 0 else 0.0)
        locked = notional + margin
        reserved = min(plan.cash_required_usd, self.reserved_cash_usd)
        self.reserved_cash_usd -= reserved
        self.cash_usd += reserved - locked
        pos = Position(
            plan_id=plan.id,
            symbol=plan.symbol,
            dex_qty=fill.qty if is_dex else 0.0,
            dex_entry=fill.price if is_dex else 0.0,
            perp_qty=0.0 if is_dex else fill.qty,
            perp_entry=0.0 if is_dex else fill.price,
            leverage=plan.leverage,
            margin_usd=margin,
            notional_usd=notional,
            allocated_risk_usd=plan.allocated_risk_usd,
            basis_entry_bps=0.0,
            opened_at=fill.ts,
            delta_base=(fill.qty if is_dex else -fill.qty),
            liq_price_est=0.0 if is_dex else (liquidation_price_est(fill.price, plan.leverage) if plan.leverage > 0 else 0.0),
            stress_applied=self.stress_flags.label,
        )
        meta = _PosMeta(entry_fees_usd=fill.fee_usd)
        ms = self._last_ms
        if ms is not None and ms.funding is not None:
            # seed from the MARKET STATE's clock, never the wall clock: replay and tests
            # must advance on simulated time, and a stale wall clock would roll the
            # first settlement past the book's own timeline (funding would never accrue).
            meta.next_funding_ms = self._roll_forward(ms.funding.next_funding_time_ms, ms.funding.interval_h, _ms_now(ms.ts))
            meta.last_mark_price = ms.funding.mark_price
            meta.last_rate = ms.funding.last_funding_rate
        self._positions[pos.id] = pos
        self._meta[pos.id] = meta
        self.gate.register_position(pos.id, pos.symbol, fill.qty, notional if is_dex else fill.qty * fill.price, pos.allocated_risk_usd, plan.leverage)
        self._mark_position(pos, ms, self._book_now_ms())
        self._sync_state()
        self.persist()
        log.error("portfolio: NAKED LEG booked as %s: %s %s %.4f @ %.4f (manual action required)", pos.id, fill.venue.value, fill.side.value, fill.qty, fill.price)
        return pos

    def close_leg(self, position_id: str, fill: Fill, reason: str) -> Position:
        """Realise ONE leg of an open position (the other leg stays open as a naked leg,
        or the position closes when it was the last leg).  Used when an unwind closed
        the perp but the DEX sell failed, and to close a one-legged position."""
        pos = self._positions.get(position_id)
        if pos is None:
            raise KeyError(f"unknown open position {position_id}")
        meta = self._meta.get(position_id, _PosMeta())
        self._roll_day()
        is_dex = fill.venue == Venue.PANCAKESWAP_V3
        if is_dex:
            q = min(fill.qty, pos.dex_qty)
            spread = (fill.price - pos.dex_entry) * q
            frac = 0.0 if pos.dex_qty <= 0 else min(1.0, q / pos.dex_qty)
            locked_part = pos.notional_usd * frac
            pos.notional_usd -= locked_part
            pos.dex_qty -= q
        else:
            q = min(fill.qty, pos.perp_qty)
            spread = (pos.perp_entry - fill.price) * q
            frac = 0.0 if pos.perp_qty <= 0 else min(1.0, q / pos.perp_qty)
            locked_part = pos.margin_usd * frac
            pos.margin_usd -= locked_part
            pos.perp_qty -= q
        funding = pos.funding_accrued_usd * (frac if not is_dex else 0.0)  # funding belongs to the perp leg
        entry_fees_part = meta.entry_fees_usd * frac * 0.5
        realized = spread + funding - entry_fees_part - fill.fee_usd
        self.cash_usd += locked_part + realized
        self.realized_total_usd += realized
        self.realized_spread_usd += spread
        self.realized_funding_usd += funding
        self.fees_paid_usd += entry_fees_part + fill.fee_usd
        self.daily_realized_usd += realized
        pos.funding_accrued_usd -= funding
        pos.realized_pnl_usd += realized
        pos.allocated_risk_usd -= pos.allocated_risk_usd * frac * 0.5
        meta.entry_fees_usd -= entry_fees_part
        pos.delta_base = pos.dex_qty - pos.perp_qty
        min_qty = self.min_qty
        if pos.perp_qty < min_qty - 1e-12 and pos.dex_qty < min_qty - 1e-12:
            pos.status = "closed"
            pos.closed_at = utcnow()
            pos.close_reason = reason
            pos.unrealized_pnl_usd = 0.0
            pos.funding_accrued_usd = 0.0
            pos.stop_distance_usd = 0.0
            pos.delta_base = 0.0
            self._positions.pop(position_id, None)
            self._meta.pop(position_id, None)
            self._closed.append(pos)
            self.gate.release_position(position_id)
            log.info("portfolio: closed %s (%s) realized %.4f USD", position_id, reason, realized)
        else:
            live_qty = max(pos.dex_qty, pos.perp_qty)
            self.gate.register_position(pos.id, pos.symbol, live_qty, pos.notional_usd + pos.margin_usd * pos.leverage, pos.allocated_risk_usd, pos.leverage)
            self._mark_position(pos, self._last_ms, self._book_now_ms())
            log.error("portfolio: %s is now ONE-LEGGED after %s: dex %.4f perp %.4f (manual action required)", position_id, reason, pos.dex_qty, pos.perp_qty)
        self._sync_state()
        self.persist()
        return pos

    def close_position(self, position_id: str, fills: List[Fill], reason: str) -> Position:
        """Realise the exit fills (full or partial), return locked cash, release the
        gate registry (or re-register the remainder after a partial unwind).  A single
        fill closes just that leg (``close_leg``)."""
        pos = self._positions.get(position_id)
        if pos is None:
            raise KeyError(f"unknown open position {position_id}")
        single = self._one_fill(fills)
        if single is not None:
            return self.close_leg(position_id, single, reason)
        dex, perp = self._split_fills(fills)
        meta = self._meta.get(position_id, _PosMeta())
        self._roll_day()
        q_dex = min(dex.qty, pos.dex_qty)
        q_perp = min(perp.qty, pos.perp_qty)
        frac = 1.0 if pos.perp_qty <= 0 else min(1.0, q_perp / pos.perp_qty)
        exit_fees = sum(f.fee_usd for f in fills)
        entry_fees_part = meta.entry_fees_usd * frac
        spread = (dex.price - pos.dex_entry) * q_dex + (pos.perp_entry - perp.price) * q_perp
        funding = pos.funding_accrued_usd * frac
        realized = spread + funding - entry_fees_part - exit_fees
        locked_part = (pos.notional_usd + pos.margin_usd) * frac
        self.cash_usd += locked_part + realized
        self.realized_total_usd += realized
        self.realized_spread_usd += spread
        self.realized_funding_usd += funding
        self.fees_paid_usd += entry_fees_part + exit_fees
        self.daily_realized_usd += realized
        remaining_dex = pos.dex_qty - q_dex
        remaining_perp = pos.perp_qty - q_perp
        min_qty = self.min_qty
        if remaining_perp < min_qty - 1e-12 and remaining_dex < min_qty - 1e-12:
            pos.status = "closed"
            pos.closed_at = utcnow()
            pos.close_reason = reason
            pos.realized_pnl_usd = realized + pos.realized_pnl_usd
            pos.unrealized_pnl_usd = 0.0
            pos.funding_accrued_usd = 0.0
            pos.stop_distance_usd = 0.0
            pos.delta_base = 0.0
            self._positions.pop(position_id, None)
            self._meta.pop(position_id, None)
            self._closed.append(pos)
            self.gate.release_position(position_id)
            log.info("portfolio: closed %s (%s) realized %.4f USD", position_id, reason, realized)
        else:
            pos.realized_pnl_usd += realized
            pos.dex_qty = remaining_dex
            pos.perp_qty = remaining_perp
            pos.notional_usd -= pos.notional_usd * frac
            pos.margin_usd -= pos.margin_usd * frac
            pos.allocated_risk_usd -= pos.allocated_risk_usd * frac
            pos.funding_accrued_usd -= funding
            pos.delta_base = remaining_dex - remaining_perp
            meta.entry_fees_usd -= entry_fees_part
            self.gate.register_position(pos.id, pos.symbol, remaining_perp, pos.notional_usd, pos.allocated_risk_usd, pos.leverage)
            self._mark_position(pos, self._last_ms, self._book_now_ms())
            log.warning("portfolio: partial close %s (%s): %.4f remaining", position_id, reason, remaining_perp)
        self._sync_state()
        self.persist()
        return pos

    def get(self, position_id: str) -> Optional[Position]:
        return self._positions.get(position_id)

    def positions(self, status: str = "open") -> List[Position]:
        if status == "open":
            return list(self._positions.values())
        if status == "closed":
            return list(self._closed)
        return list(self._positions.values()) + list(self._closed)

    def delta_base(self) -> float:
        return sum(p.dex_qty - p.perp_qty for p in self._positions.values())

    # ------------------------------------------------------------------ marking
    @staticmethod
    def _roll_forward(next_ms: int, interval_h: float, now_ms: int) -> int:
        interval_ms = int(max(interval_h, 1e-9) * 3_600_000)
        t = int(next_ms)
        if interval_ms <= 0:
            return t
        while t <= now_ms:
            t += interval_ms
        return t

    def _closing_prices(self, pos: Position, ms: Optional[MarketState]) -> tuple[float, float, float, float]:
        """(dex_sell_exec, perp_buy_ref, mark, gas_usd) honouring the basis-shock stress."""
        meta = self._meta.get(pos.id, _PosMeta())
        dex_sell = meta.last_dex_sell or pos.dex_entry
        gas = 0.0
        mark = meta.last_mark_price or pos.perp_entry
        if ms is not None and ms.dex is not None:
            dex_sell = ms.dex.exec_price_sell
            gas = ms.dex.gas_usd
        if ms is not None and ms.funding is not None:
            mark = ms.perp_ref_price or ms.funding.mark_price
        _, perp_buy = perp_reference(mark, self.settings.perp_slippage_bps)
        shock = self.stress_flags.basis_shock_bps
        if shock:
            # adverse basis: the DEX leg sells lower relative to the perp we must buy back
            dex_sell = dex_sell * (1.0 - shock / 1e4)
        return dex_sell, perp_buy, mark, gas

    def _mark_position(self, pos: Position, ms: Optional[MarketState], now_ms: int) -> MarkToClose:
        meta = self._meta.setdefault(pos.id, _PosMeta())
        dex_sell, perp_buy, mark, gas = self._closing_prices(pos, ms)
        mtc = mark_to_close(
            dex_qty=pos.dex_qty,
            perp_qty=pos.perp_qty,
            dex_entry=pos.dex_entry,
            perp_entry=pos.perp_entry,
            dex_sell_exec=dex_sell,
            perp_buy_ref=perp_buy,
            funding_accrued_usd=pos.funding_accrued_usd,
            entry_fees_usd=meta.entry_fees_usd,
            dex_fee_tier=self.settings.dex_fee_tier,
            cex_taker_bps=self.settings.perp_taker_fee_bps,
            gas_usd_per_swap=gas,
        )
        pos.unrealized_pnl_usd = mtc.net_pnl_usd - mtc.funding_usd
        pos.est_exit_cost_usd = mtc.exit_cost_usd
        pos.delta_base = mtc.delta_base
        pos.stop_distance_usd = pos.allocated_risk_usd + pos.unrealized_pnl_usd + pos.funding_accrued_usd
        pos.stress_applied = self.stress_flags.label
        meta.last_mark = mtc
        meta.last_mark_price = mark
        meta.last_dex_sell = dex_sell
        meta.last_perp_buy = perp_buy
        meta.last_mark_ms = int(now_ms)
        if ms is not None and ms.funding is not None:
            meta.last_rate = self._effective_rate(ms.funding.last_funding_rate)
            meta.settlements_remaining = settlements_between(
                now_ms, ms.funding.next_funding_time_ms, ms.funding.interval_h, self.settings.funding_horizon_hours
            )
            meta.next_settlement_ms = self._roll_forward(ms.funding.next_funding_time_ms, ms.funding.interval_h, now_ms)
        return mtc

    def _effective_rate(self, rate: float) -> float:
        o = self.stress_flags.funding_rate_override
        return float(o) if o is not None else float(rate)

    def accrue_funding(self, funding: FundingSnapshot, now_ms: int) -> float:
        """Accrue one settlement per crossed `next_funding_time_ms` for every open
        short.  Sign-aware: the short RECEIVES when rate > 0 and PAYS when rate < 0.
        Returns the USD accrued by this call."""
        rate = self._effective_rate(funding.last_funding_rate)
        interval_ms = int(max(funding.interval_h, 1e-9) * 3_600_000)
        total = 0.0
        for pos in self._positions.values():
            meta = self._meta.setdefault(pos.id, _PosMeta())
            if meta.next_funding_ms is None:
                meta.next_funding_ms = self._roll_forward(funding.next_funding_time_ms, funding.interval_h, now_ms)
                continue
            while meta.next_funding_ms <= now_ms:
                usd = rate * pos.perp_qty * funding.mark_price
                pos.funding_accrued_usd += usd
                total += usd
                meta.next_funding_ms += interval_ms
                log.info("portfolio: funding accrued %.4f USD on %s at rate %.6f", usd, pos.id, rate)
        return total

    def mark(self, ms: MarketState, now: Optional[datetime] = None) -> PortfolioSnapshot:
        """Mark every open position to close, accrue funding at settlement crossings,
        feed the gate with equity and refresh the snapshot."""
        self._last_ms = ms
        now_dt = now or utcnow()
        now_ms = _ms_now(now_dt)
        self._roll_day(now_dt)
        if ms.funding is not None:
            self.accrue_funding(ms.funding, now_ms)
        for pos in self._positions.values():
            self._mark_position(pos, ms, now_ms)
        equity = self.equity()
        self.gate.update_equity(equity)
        self.equity_curve.append((now_dt, equity))
        snap = self.snapshot(now_dt)
        self._sync_state()
        return snap

    def equity(self) -> float:
        open_value = sum(
            p.notional_usd + p.margin_usd + p.unrealized_pnl_usd + p.funding_accrued_usd for p in self._positions.values()
        )
        return self.cash_usd + self.reserved_cash_usd + open_value - self.stress_flags.equity_shock_usd

    def stop_breached(self, pos: Position, now_ms: Optional[int] = None) -> Optional[str]:
        """"position_stop" when the mark-to-close loss has eaten the allocated risk;
        "funding_flip" when, INSIDE the 15-minute window before a settlement whose
        effective rate is negative, the remaining carry can no longer pay for holding
        (basis_now + remaining carry − exit cost < 0); else None.

        Outside that window a negative rate never unwinds on its own: right after entry
        ``spread − exit_cost`` is always negative (it is the round-trip cost), so a rule
        without the window would churn every fresh position."""
        if pos.stop_distance_usd <= 0.0:
            return "position_stop"
        meta = self._meta.get(pos.id)
        if meta is None or meta.last_mark is None:
            return None
        rate = meta.last_rate
        next_settlement = meta.next_settlement_ms if meta.next_settlement_ms is not None else meta.next_funding_ms
        if rate >= 0.0 or next_settlement is None or pos.perp_qty <= 0.0:
            return None
        now = int(now_ms) if now_ms is not None else (meta.last_mark_ms or _ms_now(None))
        remaining_ms = int(next_settlement) - now
        if remaining_ms < 0 or remaining_ms > FUNDING_FLIP_WINDOW_MS:
            return None
        remaining_carry = rate * pos.perp_qty * meta.last_mark_price * max(1, meta.settlements_remaining)  # < 0
        # basis still to be captured by holding: short perp above the DEX exit price
        basis_now_usd = (meta.last_mark_price - meta.last_dex_sell) * min(pos.perp_qty, pos.dex_qty)
        if basis_now_usd + remaining_carry - meta.last_mark.exit_cost_usd < 0.0:
            return "funding_flip"
        return None

    # ------------------------------------------------------------------ snapshot
    def _gate_view(self) -> Dict[str, Any]:
        try:
            g = self.gate.snapshot()
            return {"peak": float(g.get("peak_equity", 0.0)), "dd": float(g.get("drawdown_pct", 0.0)), "state": g.get("state", "NORMAL")}
        except Exception:  # pragma: no cover - defensive against a minimal fake gate
            return {"peak": self.equity(), "dd": 0.0, "state": "NORMAL"}

    def snapshot(self, now: Optional[datetime] = None) -> PortfolioSnapshot:
        eq = self.equity()
        g = self._gate_view()
        open_unreal = sum(p.unrealized_pnl_usd for p in self._positions.values())
        open_fund = sum(p.funding_accrued_usd for p in self._positions.values())
        open_spread = sum((m.last_mark.spread_pnl_usd if m.last_mark else 0.0) for m in self._meta.values())
        snap = PortfolioSnapshot(
            equity_usd=eq,
            cash_usd=self.cash_usd,
            reserved_cash_usd=self.reserved_cash_usd,
            peak_equity_usd=max(g["peak"], eq) if g["peak"] <= 0 else g["peak"],
            drawdown_pct=g["dd"] * 100.0,
            dd_state=g["state"],
            total_pnl_usd=self.realized_total_usd + open_unreal + open_fund,
            spread_pnl_usd=self.realized_spread_usd + open_spread,
            funding_pnl_usd=self.realized_funding_usd + open_fund,
            fees_paid_usd=self.fees_paid_usd + sum(m.entry_fees_usd for m in self._meta.values()),
            open_positions=len(self._positions),
            daily_realized_usd=self.daily_realized_usd,
            equity_curve=list(self.equity_curve),
            ts=now or utcnow(),
        )
        self._last_snapshot = snap
        return snap

    def _roll_day(self, now: Optional[datetime] = None) -> None:
        day = (now or utcnow()).date().isoformat()
        if day != self._day:
            self._day = day
            self.daily_realized_usd = 0.0

    def _sync_state(self) -> None:
        """Mirror positions/snapshot into the shared State (duck-typed; never raises)."""
        st = self.state
        if st is None:
            return
        try:
            positions = getattr(st, "positions", None)
            if isinstance(positions, dict):
                positions.clear()
                positions.update(self._positions)
                for p in self._closed:
                    positions[p.id] = p
            st.portfolio = self.snapshot()
        except Exception as exc:  # pragma: no cover
            log.debug("portfolio: state sync skipped: %s", exc)

    # ------------------------------------------------------------------ stress
    def apply_stress(self, scenario: StressScenario) -> StressResult:
        """Apply a labelled SIMULATED override to the paper book only (feed untouched)."""
        before = self.equity()
        kind = scenario.kind
        label = scenario.label or f"SIMULATED · {kind.value} {scenario.magnitude:g}"
        if kind == StressKind.RESET:
            return self.clear_stress()
        if "SIMULATED" not in label:
            label = f"SIMULATED · {label}"
        f = self.stress_flags
        affected = 0
        if kind == StressKind.BASIS_SHOCK:
            f.basis_shock_bps = float(scenario.magnitude)
            affected = len(self._positions)
        elif kind == StressKind.EQUITY_SHOCK:
            f.equity_shock_usd = before * float(scenario.magnitude) / 100.0
            affected = len(self._positions)
        elif kind == StressKind.FUNDING_FLIP:
            f.funding_rate_override = float(scenario.magnitude)
            affected = len(self._positions)
        elif kind == StressKind.DEX_LEG_FAIL:
            f.dex_leg_fail = True
        elif kind == StressKind.FEED_STALE:
            f.feed_stale = True
        f.label = label
        return self._stress_result(scenario, before, affected, label)

    def clear_stress(self) -> StressResult:
        before = self.equity()
        self.stress_flags = StressFlags()
        scenario = StressScenario(kind=StressKind.RESET, magnitude=0.0, label="reset")
        return self._stress_result(scenario, before, len(self._positions), None)

    def _book_now_ms(self) -> int:
        """The book's own clock: the timestamp of the last market state we marked on.

        Only `mark()` advances time. Every other operation (opening, closing,
        applying stress) must reuse this instant, otherwise a stale book jumps to
        the wall clock and accrues phantom funding settlements, and replay stops
        being deterministic.
        """
        ms = self._last_ms
        if ms is not None:
            return _ms_now(ms.ts)
        return _ms_now(None)

    def _stress_result(self, scenario: StressScenario, before: float, affected: int, label: Optional[str]) -> StressResult:
        if self._last_ms is not None:
            # re-mark on the book's own clock: stress changes prices, never the time
            self.mark(self._last_ms, now=self._last_ms.ts)
        else:
            now_ms = _ms_now(None)
            for pos in self._positions.values():
                self._mark_position(pos, None, now_ms)
            self.gate.update_equity(self.equity())
            self._sync_state()
        g = self._gate_view()
        stops = [p.id for p in self._positions.values() if self.stop_breached(p) is not None]
        return StressResult(
            scenario=scenario,
            equity_before=before,
            equity_after=self.equity(),
            drawdown_pct=g["dd"] * 100.0,
            dd_state=g["state"],
            halted=g["state"] == "HALTED",
            positions_affected=affected,
            stops_fired=stops,
            active_label=label,
        )

    # ------------------------------------------------------------------ persistence
    def _to_json(self) -> Dict[str, Any]:
        return {
            "mode": self.settings.mode.value,
            "cash_usd": self.cash_usd,
            "reserved_cash_usd": self.reserved_cash_usd,
            "positions": [p.model_dump(mode="json") for p in self._positions.values()],
            "closed": [p.model_dump(mode="json") for p in self._closed],
            "meta": {
                pid: {"entry_fees_usd": m.entry_fees_usd, "next_funding_ms": m.next_funding_ms}
                for pid, m in self._meta.items()
            },
            "equity_curve": [[t.isoformat(), e] for t, e in list(self.equity_curve)[-100:]],
            "daily_realized_usd": self.daily_realized_usd,
            "day": self._day,
            "realized_total_usd": self.realized_total_usd,
            "realized_spread_usd": self.realized_spread_usd,
            "realized_funding_usd": self.realized_funding_usd,
            "fees_paid_usd": self.fees_paid_usd,
            "saved_at": utcnow().isoformat(),
        }

    def persist(self) -> None:
        if not self.persist_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.persist_path)), exist_ok=True)
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._to_json(), fh, indent=1, sort_keys=True, default=str)
            os.replace(tmp, self.persist_path)
        except OSError as exc:
            log.error("portfolio: persist failed: %s", exc)

    def restore(self) -> bool:
        """Load state/<mode>/portfolio.json; stress is never restored; positions missing
        from the gate registry (gate.load() runs separately) are re-registered."""
        if not self.persist_path or not os.path.exists(self.persist_path):
            return False
        try:
            with open(self.persist_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.error("portfolio: restore failed: %s", exc)
            return False
        if data.get("mode") != self.settings.mode.value:
            log.warning("portfolio: ignoring %s state for mode %s", data.get("mode"), self.settings.mode.value)
            return False
        self.cash_usd = float(data.get("cash_usd", self.cash_usd))
        self.reserved_cash_usd = float(data.get("reserved_cash_usd", 0.0))
        self._positions = {}
        self._meta = {}
        for raw in data.get("positions", []):
            try:
                pos = Position.model_validate(raw)
            except Exception as exc:
                log.warning("portfolio: skipping bad position record: %s", exc)
                continue
            if pos.status != "open":
                continue
            self._positions[pos.id] = pos
            m = data.get("meta", {}).get(pos.id, {})
            self._meta[pos.id] = _PosMeta(entry_fees_usd=float(m.get("entry_fees_usd", 0.0)), next_funding_ms=m.get("next_funding_ms"))
            registry = getattr(self.gate, "positions", {})
            if pos.id not in registry:
                self.gate.register_position(pos.id, pos.symbol, pos.perp_qty, pos.notional_usd, pos.allocated_risk_usd, pos.leverage)
        self._closed = deque((Position.model_validate(r) for r in data.get("closed", [])), maxlen=CLOSED_KEEP)
        self.equity_curve = deque(
            ((datetime.fromisoformat(t), float(e)) for t, e in data.get("equity_curve", [])), maxlen=EQUITY_CURVE_LEN
        )
        self.daily_realized_usd = float(data.get("daily_realized_usd", 0.0))
        self._day = str(data.get("day", self._day))
        self.realized_total_usd = float(data.get("realized_total_usd", 0.0))
        self.realized_spread_usd = float(data.get("realized_spread_usd", 0.0))
        self.realized_funding_usd = float(data.get("realized_funding_usd", 0.0))
        self.fees_paid_usd = float(data.get("fees_paid_usd", 0.0))
        self.stress_flags = StressFlags()
        self._roll_day()
        self._sync_state()
        log.info("portfolio: restored %d open positions, cash %.2f", len(self._positions), self.cash_usd)
        return True


__all__ = ["Portfolio", "StressFlags", "EQUITY_CURVE_LEN", "FUNDING_FLIP_WINDOW_MS"]
