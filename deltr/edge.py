"""
Deltr edge / fee / funding / sizing math.  Pure functions, Decimal rounding,
golden-tested in tests/test_edge_math.py.

Conventions
* All rates in **bps of notional** unless stated.  1 bps = 1e-4.
* Funding is a fraction per settlement (Binance USDⓈ-M: every 8 h).  A
  positive rate means longs pay shorts, i.e. Deltr's short perp **receives**.
* Horizon-based edge (judges' consensus — entry-only math shows phantom profit):

      net(H) = basis_entry + funding(H) − roundtrip_cost − basis_exit_assumed
      roundtrip_cost = 2·(dex_fee + dex_impact + perp_slip + cex_taker + gas_leg)

* The QuoterV2 executable price already INCLUDES the pool fee, so
  ``impact = (exec/mid − 1)·1e4 − fee`` (never counted twice).
* Sizing: the DEX leg is unlevered, the perp leg needs notional/L margin, so
  for capital C at leverage L the deployable notional is N = C / (1 + 1/L).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Tuple

from deltr.models import EdgeBreakdown

FUNDING_INTERVAL_HOURS_DEFAULT = 8


# --------------------------------------------------------------------------- #
# Rounding / sizing                                                           #
# --------------------------------------------------------------------------- #
def floor_to_step(qty: float, step: float) -> float:
    """Floor `qty` to an exchange LOT_SIZE step using Decimal (no float drift)."""
    if step <= 0:
        raise ValueError("step must be > 0")
    q = Decimal(str(qty))
    s = Decimal(str(step))
    return float((q / s).to_integral_value(rounding=ROUND_DOWN) * s)


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be > 0")
    p = Decimal(str(price))
    t = Decimal(str(tick))
    return float((p / t).to_integral_value(rounding=ROUND_DOWN) * t)


def fmt_qty(qty: float, precision: int) -> str:
    """Exchange-safe string for order quantities (no scientific notation)."""
    return f"{Decimal(str(qty)).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN):f}"


@dataclass(frozen=True, slots=True)
class Sizing:
    capital_usd: float
    leverage: float
    price: float
    step: float
    base_qty: float  # step-aligned
    notional_usd: float  # base_qty · price
    margin_usd: float  # notional / leverage
    capital_required_usd: float  # notional · (1 + 1/L)
    unlevered_notional_usd: float  # capital / (1 + 1/L) before step rounding


def size_from_capital(capital_usd: float, leverage: float, price: float, step: float, utilization: float = 1.0) -> Sizing:
    """Deployable notional for a delta-neutral pair funded from `capital_usd`.

    N = capital · utilization / (1 + 1/L);  qty = floor(N / price, step).
    Golden: $5,000 at 2x, price 686.19, step 0.01 → N = 3,333.33 → 4.85 BNB →
    notional 3,328.02, margin 1,664.01, capital required 4,992.03.
    """
    if capital_usd <= 0 or leverage <= 0 or price <= 0:
        raise ValueError("capital_usd, leverage and price must be > 0")
    n_unlevered = capital_usd * utilization / (1.0 + 1.0 / leverage)
    qty = floor_to_step(n_unlevered / price, step)
    notional = qty * price
    margin = notional / leverage
    return Sizing(
        capital_usd=capital_usd,
        leverage=leverage,
        price=price,
        step=step,
        base_qty=qty,
        notional_usd=notional,
        margin_usd=margin,
        capital_required_usd=notional * (1.0 + 1.0 / leverage),
        unlevered_notional_usd=n_unlevered,
    )


# --------------------------------------------------------------------------- #
# Components                                                                  #
# --------------------------------------------------------------------------- #
def bps(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator * 1e4


def dex_fee_bps(fee_tier: int) -> float:
    """PancakeSwap fee units are 1e-6 (100 = 0.01 %) → bps."""
    return fee_tier / 100.0


def impact_bps(exec_price: float, mid: float, fee_tier: int) -> float:
    """Pure price impact of an executable BUY quote: total premium over mid
    minus the pool fee the quote already contains.  Clamped at 0."""
    return max(0.0, bps(exec_price - mid, mid) - dex_fee_bps(fee_tier))


def gas_usd(gas_units: int, gas_price_wei: int, bnb_price_usd: float) -> float:
    return gas_units * gas_price_wei / 1e18 * bnb_price_usd


def gas_bps(gas_units: int, gas_price_wei: int, bnb_price_usd: float, notional_usd: float) -> float:
    """Gas cost of ONE swap expressed in bps of notional."""
    if notional_usd <= 0:
        return 0.0
    return gas_usd(gas_units, gas_price_wei, bnb_price_usd) / notional_usd * 1e4


def settlements_in(horizon_hours: float, interval_hours: int = FUNDING_INTERVAL_HOURS_DEFAULT) -> int:
    if interval_hours <= 0:
        raise ValueError("interval_hours must be > 0")
    return int(horizon_hours // interval_hours)


def funding_bps(rate_per_period: float, horizon_hours: float, interval_hours: int = FUNDING_INTERVAL_HOURS_DEFAULT) -> Tuple[int, float]:
    """(settlements, bps received by the SHORT over the horizon).  Sign-aware:
    negative rates mean the short PAYS and the returned bps is negative."""
    n = settlements_in(horizon_hours, interval_hours)
    return n, rate_per_period * n * 1e4


def annualized_funding_pct(rate_per_period: float, interval_hours: int = FUNDING_INTERVAL_HOURS_DEFAULT) -> float:
    return rate_per_period * (24.0 / interval_hours) * 365.0 * 100.0


def perp_reference(mark_price: float, half_spread_bps: float) -> Tuple[float, float]:
    """(sell_ref, buy_ref): the price Deltr assumes it SELLS/BUYS the perp at,
    i.e. mark ∓ a modelled half-spread.  Testnet order books are thin, so the
    mark (which tracks the mainnet index) is the honest reference."""
    hs = half_spread_bps / 1e4
    return mark_price * (1.0 - hs), mark_price * (1.0 + hs)


def roundtrip_cost_bps(dex_fee: float, dex_impact: float, perp_slip: float, cex_taker: float, gas_leg: float) -> float:
    return 2.0 * (dex_fee + dex_impact + perp_slip + cex_taker + gas_leg)


def allocated_risk_usd(notional_usd: float, roundtrip_bps: float, basis_shock_bps: float = 100.0) -> float:
    """Capital at risk for the gate: round-trip costs plus an adverse basis move."""
    return notional_usd * (roundtrip_bps + basis_shock_bps) / 1e4


# --------------------------------------------------------------------------- #
# Edge                                                                        #
# --------------------------------------------------------------------------- #
def compute_edge(
    *,
    notional_usd: float,
    dex_exec_buy: float,
    dex_mid: float,
    perp_sell_ref: float,
    funding_rate: float,
    horizon_hours: float,
    dex_fee_tier: int,
    perp_slip_bps: float,
    cex_taker_bps: float,
    gas_bps_leg: float,
    basis_exit_bps: float = 0.0,
    basis_shock_bps: float = 100.0,
    interval_hours: int = FUNDING_INTERVAL_HOURS_DEFAULT,
) -> EdgeBreakdown:
    """Horizon-based net edge of Long-DEX / Short-Perp in bps of notional."""
    if dex_exec_buy <= 0 or dex_mid <= 0 or perp_sell_ref <= 0:
        raise ValueError("prices must be > 0")
    basis_entry = bps(perp_sell_ref - dex_exec_buy, dex_exec_buy)
    fee = dex_fee_bps(dex_fee_tier)
    imp = impact_bps(dex_exec_buy, dex_mid, dex_fee_tier)
    n, fund = funding_bps(funding_rate, horizon_hours, interval_hours)
    rt = roundtrip_cost_bps(fee, imp, perp_slip_bps, cex_taker_bps, gas_bps_leg)
    net = basis_entry + fund - rt - basis_exit_bps
    return EdgeBreakdown(
        notional_usd=notional_usd,
        basis_entry_bps=basis_entry,
        dex_fee_bps=fee,
        dex_impact_bps=imp,
        perp_slip_bps=perp_slip_bps,
        cex_taker_bps=cex_taker_bps,
        gas_bps_leg=gas_bps_leg,
        roundtrip_cost_bps=rt,
        funding_rate_last=funding_rate,
        horizon_h=horizon_hours,
        settlements=n,
        funding_bps_horizon=fund,
        basis_exit_assumed_bps=basis_exit_bps,
        basis_shock_bps=basis_shock_bps,
        net_edge_bps=net,
        expected_edge_usd=net / 1e4 * notional_usd,
        allocated_risk_usd=allocated_risk_usd(notional_usd, rt, basis_shock_bps),
    )


# --------------------------------------------------------------------------- #
# Mark-to-close                                                               #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MarkToClose:
    spread_pnl_usd: float  # DEX leg + perp leg price PnL at closing prices
    funding_usd: float
    entry_fees_usd: float
    exit_cost_usd: float  # estimated fees + gas to unwind now
    net_pnl_usd: float  # spread + funding − entry fees − exit cost
    delta_base: float


def mark_to_close(
    *,
    dex_qty: float,
    perp_qty: float,
    dex_entry: float,
    perp_entry: float,
    dex_sell_exec: float,
    perp_buy_ref: float,
    funding_accrued_usd: float,
    entry_fees_usd: float,
    dex_fee_tier: int,
    cex_taker_bps: float,
    gas_usd_per_swap: float,
) -> MarkToClose:
    """PnL if the position were closed NOW (never shows a green position that
    would be red after paying the exit)."""
    spread = (dex_sell_exec - dex_entry) * dex_qty + (perp_entry - perp_buy_ref) * perp_qty
    exit_cost = (
        dex_sell_exec * dex_qty * dex_fee_bps(dex_fee_tier) / 1e4
        + perp_buy_ref * perp_qty * cex_taker_bps / 1e4
        + gas_usd_per_swap
    )
    net = spread + funding_accrued_usd - entry_fees_usd - exit_cost
    return MarkToClose(
        spread_pnl_usd=spread,
        funding_usd=funding_accrued_usd,
        entry_fees_usd=entry_fees_usd,
        exit_cost_usd=exit_cost,
        net_pnl_usd=net,
        delta_base=dex_qty - perp_qty,
    )


def liquidation_price_est(perp_entry: float, leverage: float, maintenance_rate: float = 0.004) -> float:
    """Rough USDⓈ-M isolated-short liquidation estimate: entry·(1+1/L)/(1+mmr)."""
    return perp_entry * (1.0 + 1.0 / leverage) / (1.0 + maintenance_rate)


__all__ = [
    "floor_to_step", "round_to_tick", "fmt_qty", "Sizing", "size_from_capital",
    "bps", "dex_fee_bps", "impact_bps", "gas_bps", "gas_usd", "settlements_in", "funding_bps", "annualized_funding_pct",
    "perp_reference", "roundtrip_cost_bps", "allocated_risk_usd", "compute_edge",
    "MarkToClose", "mark_to_close", "liquidation_price_est",
]


# --------------------------------------------------------------------------- #
# Convenience layer used by the scout / hedger (names from the build plan)    #
# --------------------------------------------------------------------------- #
from typing import Callable, Optional  # noqa: E402

from deltr.models import MarketState, SymbolFilters  # noqa: E402
from deltr.venues.pancake_constants import Q192  # noqa: E402


def sqrt_price_to_bnb_usdt(sqrt_price_x96: int) -> float:
    """Pool mid for the WBNB/USDT pools (token0 = USDT, token1 = WBNB, 18/18 dec)."""
    return float(Q192 / (sqrt_price_x96 * sqrt_price_x96))


def round_step(qty: float | Decimal, step: float | str) -> Decimal:
    """Floor to a LOT_SIZE step, returned as Decimal (never float modulo)."""
    q = Decimal(str(qty))
    s = Decimal(str(step))
    if s <= 0:
        raise ValueError("step must be > 0")
    return (q / s).to_integral_value(rounding=ROUND_DOWN) * s


def dex_cost_split(mid: float, exec_buy: float, fee_bps: float) -> Tuple[float, float]:
    """(fee_bps, impact_bps): 1e4·(exec/mid − 1) = fee + impact, impact clamped ≥ 0."""
    total = bps(exec_buy - mid, mid)
    return fee_bps, max(0.0, total - fee_bps)


gas_bps_per_leg = gas_bps
liq_price_short = liquidation_price_est


def settlements_between(now_ms: int, next_funding_ms: int, interval_h: float, horizon_h: float) -> int:
    """Number of funding settlement instants in (now, now + horizon]."""
    if interval_h <= 0 or horizon_h <= 0:
        return 0
    interval_ms = int(interval_h * 3_600_000)
    end_ms = now_ms + int(horizon_h * 3_600_000)
    t = next_funding_ms
    # roll forward if the recorded next settlement is already in the past
    while t <= now_ms:
        t += interval_ms
    if t > end_ms:
        return 0
    return 1 + (end_ms - t) // interval_ms


def funding_bps_for(rate: float, settlements: int, short: bool = True) -> float:
    """bps earned (+) or paid (−) by the SHORT over `settlements` settlements."""
    v = rate * settlements * 1e4
    return v if short else -v


def breakeven_settlements(entry_cost_bps: float, rate: float) -> Optional[int]:
    """Settlements needed for funding to pay back the round-trip cost (None if rate ≤ 0)."""
    if rate <= 0 or entry_cost_bps <= 0:
        return None
    per = rate * 1e4
    n = int(entry_cost_bps // per)
    return n if n * per >= entry_cost_bps else n + 1


def net_edge(ms: MarketState, notional_usd: float, horizon_h: float, cfg, now_ms: Optional[int] = None) -> EdgeBreakdown:
    """Horizon-based net edge from a MarketState + Settings (perp ref = mark; the
    modelled perp slippage is charged inside the round trip, not in the basis)."""
    if ms.dex is None or ms.funding is None:
        raise ValueError("MarketState needs dex and funding")
    import time as _t

    now = now_ms if now_ms is not None else int(_t.time() * 1000)
    n = settlements_between(now, ms.funding.next_funding_time_ms, ms.funding.interval_h, horizon_h)
    perp_ref = ms.perp_ref_price if ms.perp_ref_price else ms.funding.mark_price
    fee_bps_v, imp = dex_cost_split(ms.dex.mid_price, ms.dex.exec_price_buy, ms.dex.fee_bps)
    g = gas_bps(ms.dex.gas_units, ms.dex.gas_price_wei, ms.dex.mid_price, notional_usd)
    rt = roundtrip_cost_bps(fee_bps_v, imp, cfg.perp_slippage_bps, cfg.perp_taker_fee_bps, g)
    basis_entry = bps(perp_ref - ms.dex.exec_price_buy, ms.dex.exec_price_buy)
    fund = funding_bps_for(ms.funding.last_funding_rate, n)
    net = basis_entry + fund - rt - cfg.basis_exit_bps
    return EdgeBreakdown(
        notional_usd=notional_usd,
        basis_entry_bps=basis_entry,
        dex_fee_bps=fee_bps_v,
        dex_impact_bps=imp,
        perp_slip_bps=cfg.perp_slippage_bps,
        cex_taker_bps=cfg.perp_taker_fee_bps,
        gas_bps_leg=g,
        roundtrip_cost_bps=rt,
        funding_rate_last=ms.funding.last_funding_rate,
        horizon_h=horizon_h,
        settlements=n,
        funding_bps_horizon=fund,
        basis_exit_assumed_bps=cfg.basis_exit_bps,
        basis_shock_bps=cfg.basis_shock_bps,
        net_edge_bps=net,
        expected_edge_usd=net / 1e4 * notional_usd,
        allocated_risk_usd=allocated_risk_usd(notional_usd, rt, cfg.basis_shock_bps),
    )


@dataclass(frozen=True, slots=True)
class HedgeSizing:
    qty: Decimal  # step-aligned base quantity
    notional_usd: float
    margin_usd: float
    cash_required_usd: float  # notional + margin (DEX leg is unlevered)
    leverage: float
    capped_by: str  # "capital" | "max_notional" | "impact" | "min_qty"

    @property
    def base_qty(self) -> float:
        return float(self.qty)


def size_for_capital(
    capital_usd: float,
    leverage: float,
    dex_exec_price: float,
    filters: SymbolFilters,
    max_notional_usd: float,
    max_impact_bps: float,
    impact_at: Optional[Callable[[float], float]] = None,
) -> HedgeSizing:
    """N = min(capital/(1+1/L), max_notional); qty = round_step(N / dex_exec);
    shrink qty by 10 % while impact_at(qty) > max_impact_bps (max 20 rounds);
    qty < min_qty ⇒ qty = 0 with capped_by = "min_qty"."""
    if capital_usd <= 0 or leverage <= 0 or dex_exec_price <= 0:
        raise ValueError("capital_usd, leverage and dex_exec_price must be > 0")
    n_cap = capital_usd / (1.0 + 1.0 / leverage)
    capped_by = "capital"
    n = n_cap
    if max_notional_usd > 0 and n > max_notional_usd:
        n = max_notional_usd
        capped_by = "max_notional"
    qty = round_step(n / dex_exec_price, filters.step_size)
    if impact_at is not None:
        for _ in range(20):
            if qty <= 0 or impact_at(float(qty)) <= max_impact_bps:
                break
            qty = round_step(float(qty) * 0.9, filters.step_size)
            capped_by = "impact"
    if float(qty) < filters.min_qty:
        qty = Decimal(0)
        capped_by = "min_qty"
    notional = float(qty) * dex_exec_price
    margin = notional / leverage
    return HedgeSizing(qty=qty, notional_usd=notional, margin_usd=margin, cash_required_usd=notional + margin, leverage=leverage, capped_by=capped_by)


__all__ += [
    "sqrt_price_to_bnb_usdt", "round_step", "dex_cost_split", "gas_bps_per_leg", "liq_price_short",
    "settlements_between", "funding_bps_for", "breakeven_settlements", "net_edge", "HedgeSizing", "size_for_capital",
]
