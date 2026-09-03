"""deltr/horizon.py — breakeven holding period, edge-versus-horizon curve and the
rendered edge report.

``deltr/edge.py`` is frozen, so this module is the presentation layer that sits on
top of it: it calls :func:`deltr.edge.breakeven_settlements` (previously called by
nothing) and re-expresses an existing :class:`~deltr.models.EdgeBreakdown` as

* a **breakeven holding period** in settlements, hours and days, next to the
  configured horizon, with a one-line verdict;
* an **edge-versus-horizon curve** (net edge in bps as a function of holding
  horizon) so the zero crossing is visible;
* a **markdown report** that renders the numbers that already exist.

Zero new analysis: every figure here is either read off the ``EdgeBreakdown`` or
derived from it with the arithmetic already stated in ``deltr/edge.py``.

Honesty rule (WIN_PLAN section 6, guardrail 5).  Binance Futures testnet funding is
structurally near zero, which is why the measured carry never repays the round
trip.  Any breakeven or curve drawn at a mainnet-typical rate is an **assumption**:
whenever ``assumed_rate`` is populated, ``assumption_label`` and
``assumption_note`` are populated too, every derived view carries
``rate_basis="assumed"`` and ``is_assumption=True``, and the measured testnet rate
is reported beside it.  Nothing in this module presents an assumed rate as a
measurement.

Nothing here writes to stdout and nothing here performs I/O.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal, Optional

from deltr.edge import breakeven_settlements, settlements_in
from deltr.models import DeltrModel, EdgeBreakdown, utcnow

# --------------------------------------------------------------------------- constants
FUNDING_INTERVAL_H_DEFAULT = 8.0

#: Mainnet-typical USDS-M funding: 0.01 % per 8 h settlement.  Deltr never measures
#: this; it is supplied so a judge can see where the curve would cross zero.
ASSUMED_FUNDING_RATE_PER_INTERVAL = 0.0001

ASSUMPTION_LABEL = "assumption, not a measurement"

ASSUMPTION_NOTE = (
    "Binance Futures testnet funding is structurally near zero, so the measured carry never repays the "
    "round trip. The rate used for this view is a mainnet-typical figure supplied by Deltr, not a rate "
    "Deltr measured. The measured testnet rate is reported beside it."
)

MEASURED_NOTE = (
    "Measured from the Binance USDS-M Futures testnet premiumIndex feed. Testnet funding is structurally "
    "near zero and is indicative only."
)

#: Rates at or below this magnitude are treated as "no carry" when deciding whether
#: an assumed rate is worth showing.
NEAR_ZERO_RATE = 1e-9

MAX_CURVE_HORIZON_H = 720.0
MAX_CURVE_POINTS = 64

CURVE_NOTE = (
    "Curve settlements are counted as floor(horizon / interval). The live breakdown counts settlement "
    "instants from the next funding time, so the two can differ by one settlement."
)

RateBasis = Literal["measured", "assumed"]


def _annualized_pct(rate: float, interval_h: float) -> float:
    if interval_h <= 0:
        return 0.0
    return rate * (24.0 / interval_h) * 365.0 * 100.0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --------------------------------------------------------------------------- models
class BreakevenView(DeltrModel):
    """How long the carry needs to repay what entry costs, at one funding rate."""

    rate_basis: RateBasis
    rate_per_interval: float
    interval_h: float
    annualized_pct: float
    is_assumption: bool
    assumption_label: Optional[str] = None
    #: bps the carry must repay: roundtrip + assumed exit basis - entry basis.
    cost_to_recover_bps: float
    settlements: Optional[int] = None
    hours: Optional[float] = None
    days: Optional[float] = None
    horizon_h: float
    horizon_days: float
    horizon_settlements: int
    reached_within_horizon: bool
    verdict: str


class CurvePoint(DeltrModel):
    horizon_h: float
    horizon_days: float
    settlements: int
    funding_bps: float
    net_edge_bps: float


class EdgeCurve(DeltrModel):
    """Net edge in bps as a function of holding horizon, at one funding rate."""

    rate_basis: RateBasis
    rate_per_interval: float
    interval_h: float
    is_assumption: bool
    assumption_label: Optional[str] = None
    horizon_h: float
    max_horizon_h: float
    #: First horizon (hours) at which net edge reaches zero; None when it never does.
    zero_cross_h: Optional[float] = None
    zero_cross_days: Optional[float] = None
    note: str = CURVE_NOTE
    points: list[CurvePoint]


class HorizonAnalysis(DeltrModel):
    """First-class breakeven + curve view of an existing ``EdgeBreakdown``."""

    symbol: Optional[str] = None
    notional_usd: float
    net_edge_bps: float
    basis_entry_bps: float
    roundtrip_cost_bps: float
    basis_exit_assumed_bps: float
    cost_to_recover_bps: float
    interval_h: float
    horizon_h: float
    horizon_days: float
    horizon_settlements: int
    funding_bps_horizon: float

    measured_rate: float
    measured_annualized_pct: float
    measured_rate_source: str
    measured_rate_note: str = MEASURED_NOTE

    #: Populated only when a mainnet-typical rate is used; never a measured value.
    assumed_rate: Optional[float] = None
    assumption_label: Optional[str] = None
    assumption_note: Optional[str] = None

    breakeven: BreakevenView
    breakeven_assumed: Optional[BreakevenView] = None
    curve: EdgeCurve
    curve_assumed: Optional[EdgeCurve] = None
    verdict: str


# --------------------------------------------------------------------------- builders
def _breakeven_view(
    *,
    cost_to_recover_bps: float,
    rate: float,
    interval_h: float,
    horizon_h: float,
    basis: RateBasis,
) -> BreakevenView:
    horizon_days = horizon_h / 24.0
    horizon_settlements = settlements_in(horizon_h, int(interval_h)) if interval_h >= 1 else 0
    is_assumption = basis == "assumed"
    n: Optional[int]
    if cost_to_recover_bps <= 0:
        n = 0
    else:
        n = breakeven_settlements(cost_to_recover_bps, rate)
    hours = None if n is None else n * interval_h
    days = None if hours is None else hours / 24.0
    reached = n is not None and hours is not None and hours <= horizon_h
    rate_pct = f"{rate * 100:.4f} %/{interval_h:g} h"
    if n == 0:
        verdict = (
            f"entry basis already covers the round trip at the {basis} rate ({rate_pct}); "
            f"no holding period is needed."
        )
    elif n is None:
        verdict = (
            f"at the {basis} funding rate ({rate_pct}) the carry never repays the "
            f"{cost_to_recover_bps:.1f} bps it costs to enter and exit, so no holding period turns this positive."
        )
    else:
        verdict = (
            f"breaks even in {_plural(n, 'settlement')} ({days:.1f} days at the {basis} rate {rate_pct}); "
            f"horizon is {horizon_days:.1f} days, so the answer is {'yes' if reached else 'no'}."
        )
    return BreakevenView(
        rate_basis=basis,
        rate_per_interval=rate,
        interval_h=interval_h,
        annualized_pct=_annualized_pct(rate, interval_h),
        is_assumption=is_assumption,
        assumption_label=ASSUMPTION_LABEL if is_assumption else None,
        cost_to_recover_bps=cost_to_recover_bps,
        settlements=n,
        hours=hours,
        days=days,
        horizon_h=horizon_h,
        horizon_days=horizon_days,
        horizon_settlements=horizon_settlements,
        reached_within_horizon=reached,
        verdict=verdict,
    )


def _curve(
    *,
    basis_entry_bps: float,
    fixed_cost_bps: float,
    rate: float,
    interval_h: float,
    horizon_h: float,
    breakeven: BreakevenView,
    basis: RateBasis,
    max_horizon_h: Optional[float] = None,
    max_points: int = MAX_CURVE_POINTS,
) -> EdgeCurve:
    """Net edge over a range of holding horizons.  net(H) = basis + rate*settlements(H)*1e4 - fixed_cost."""
    span = max(horizon_h * 2.0, 24.0)
    if breakeven.hours:
        span = max(span, breakeven.hours * 1.35)
    span = min(float(max_horizon_h or span), MAX_CURVE_HORIZON_H)
    span = max(span, interval_h, horizon_h)
    steps = max(1, int(math.ceil(span / interval_h)))
    stride = max(1, int(math.ceil((steps + 1) / max(2, max_points))))
    points: list[CurvePoint] = []
    zero_cross_h: Optional[float] = None
    for k in range(0, steps + 1):
        h = k * interval_h
        funding = rate * k * 1e4
        net = basis_entry_bps + funding - fixed_cost_bps
        if zero_cross_h is None and net >= 0:
            zero_cross_h = h
        if k % stride == 0 or k == steps:
            points.append(
                CurvePoint(
                    horizon_h=h,
                    horizon_days=h / 24.0,
                    settlements=k,
                    funding_bps=funding,
                    net_edge_bps=net,
                )
            )
    is_assumption = basis == "assumed"
    return EdgeCurve(
        rate_basis=basis,
        rate_per_interval=rate,
        interval_h=interval_h,
        is_assumption=is_assumption,
        assumption_label=ASSUMPTION_LABEL if is_assumption else None,
        horizon_h=horizon_h,
        max_horizon_h=steps * interval_h,
        zero_cross_h=zero_cross_h,
        zero_cross_days=None if zero_cross_h is None else zero_cross_h / 24.0,
        points=points,
    )


def horizon_analysis(
    edge: EdgeBreakdown,
    *,
    interval_h: Optional[float] = None,
    symbol: Optional[str] = None,
    measured_source: Optional[str] = None,
    assumed_rate: Optional[float] = None,
    auto_assume: bool = True,
    max_horizon_h: Optional[float] = None,
    max_points: int = MAX_CURVE_POINTS,
) -> HorizonAnalysis:
    """Breakeven holding period and edge-versus-horizon curve for ``edge``.

    ``assumed_rate`` (or ``auto_assume`` when the measured carry can never repay the
    round trip) adds a second, clearly labelled view at a mainnet-typical rate.  The
    measured rate is always reported; the assumed one is never presented as measured.
    """
    iv = float(interval_h) if interval_h and interval_h > 0 else FUNDING_INTERVAL_H_DEFAULT
    horizon_h = float(edge.horizon_h)
    fixed_cost = float(edge.roundtrip_cost_bps) + float(edge.basis_exit_assumed_bps)
    cost_to_recover = fixed_cost - float(edge.basis_entry_bps)
    measured_rate = float(edge.funding_rate_last)

    measured_be = _breakeven_view(
        cost_to_recover_bps=cost_to_recover, rate=measured_rate, interval_h=iv, horizon_h=horizon_h, basis="measured"
    )
    measured_curve = _curve(
        basis_entry_bps=float(edge.basis_entry_bps), fixed_cost_bps=fixed_cost, rate=measured_rate, interval_h=iv,
        horizon_h=horizon_h, breakeven=measured_be, basis="measured", max_horizon_h=max_horizon_h, max_points=max_points,
    )

    use_assumed: Optional[float] = None
    if assumed_rate is not None:
        use_assumed = float(assumed_rate)
    elif auto_assume and measured_be.settlements is None and measured_rate <= NEAR_ZERO_RATE:
        use_assumed = ASSUMED_FUNDING_RATE_PER_INTERVAL

    assumed_be: Optional[BreakevenView] = None
    assumed_curve: Optional[EdgeCurve] = None
    if use_assumed is not None:
        assumed_be = _breakeven_view(
            cost_to_recover_bps=cost_to_recover, rate=use_assumed, interval_h=iv, horizon_h=horizon_h, basis="assumed"
        )
        assumed_curve = _curve(
            basis_entry_bps=float(edge.basis_entry_bps), fixed_cost_bps=fixed_cost, rate=use_assumed, interval_h=iv,
            horizon_h=horizon_h, breakeven=assumed_be, basis="assumed", max_horizon_h=max_horizon_h, max_points=max_points,
        )

    return HorizonAnalysis(
        symbol=symbol,
        notional_usd=float(edge.notional_usd),
        net_edge_bps=float(edge.net_edge_bps),
        basis_entry_bps=float(edge.basis_entry_bps),
        roundtrip_cost_bps=float(edge.roundtrip_cost_bps),
        basis_exit_assumed_bps=float(edge.basis_exit_assumed_bps),
        cost_to_recover_bps=cost_to_recover,
        interval_h=iv,
        horizon_h=horizon_h,
        horizon_days=horizon_h / 24.0,
        horizon_settlements=int(edge.settlements),
        funding_bps_horizon=float(edge.funding_bps_horizon),
        measured_rate=measured_rate,
        measured_annualized_pct=_annualized_pct(measured_rate, iv),
        measured_rate_source=measured_source or "binance-futures-testnet",
        assumed_rate=use_assumed,
        assumption_label=ASSUMPTION_LABEL if use_assumed is not None else None,
        assumption_note=ASSUMPTION_NOTE if use_assumed is not None else None,
        breakeven=measured_be,
        breakeven_assumed=assumed_be,
        curve=measured_curve,
        curve_assumed=assumed_curve,
        verdict=measured_be.verdict,
    )


# --------------------------------------------------------------------------- report
def _fmt_bps(v: float) -> str:
    return f"{v:+.2f} bps"


def _age_row(label: str, source: Any, age_ms: Optional[int]) -> str:
    src = getattr(source, "value", source)
    age = "n/a" if age_ms is None else f"{int(age_ms)} ms"
    return f"| {label} | `{src}` | {age} |"


def render_edge_report(
    *,
    edge: EdgeBreakdown,
    analysis: HorizonAnalysis,
    components: Optional[list[Any]] = None,
    market: Optional[Any] = None,
    sizing: Optional[Any] = None,
    symbol: Optional[str] = None,
    mode: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    curve_rows: int = 8,
) -> str:
    """Render the existing decomposition as a titled markdown artifact.

    Pure presentation: every number comes from ``edge``, ``analysis``, ``components``,
    ``sizing`` or ``market``.  No new analysis is performed here.
    """
    sym = symbol or analysis.symbol or getattr(market, "symbol", None) or "the configured pair"
    ts = generated_at or utcnow()
    rows = list(components) if components else list(edge.components())

    out: list[str] = []
    out.append(f"# Deltr edge report: {sym}")
    out.append("")
    meta = [
        f"Generated {ts.isoformat()}",
        f"mode {mode or 'paper'}",
        f"notional ${edge.notional_usd:,.2f}",
        f"horizon {edge.horizon_h:g} h ({analysis.horizon_days:.1f} days)",
    ]
    out.append(" · ".join(meta))
    out.append("")
    out.append(
        f"**Net edge {_fmt_bps(edge.net_edge_bps)}** over the configured horizon "
        f"(${edge.expected_edge_usd:,.2f} on ${edge.notional_usd:,.2f}), against "
        f"{edge.roundtrip_cost_bps:.2f} bps of round-trip cost."
    )
    out.append("")

    # ---- waterfall
    out.append("## Round-trip cost waterfall")
    out.append("")
    out.append("| Component | bps | Kind |")
    out.append("|---|---:|---|")
    for c in rows:
        label = getattr(c, "label", "")
        value = float(getattr(c, "bps", 0.0))
        kind = getattr(c, "kind", "")
        out.append(f"| {label} | {value:+.2f} | {kind} |")
    out.append("")
    out.append(
        "Formula: `net = basis_entry + funding(H) - roundtrip - basis_exit_assumed`, "
        "`roundtrip = 2 * (dex_fee + dex_impact + perp_slip + cex_taker + gas_leg)`."
    )
    out.append("")

    # ---- sizing
    if sizing is not None:
        qty = getattr(sizing, "base_qty", None)
        if qty is None:
            qty = getattr(sizing, "qty", None)
        out.append("## Sizing")
        out.append("")
        out.append(
            f"- quantity: `{float(qty) if qty is not None else 0.0:g}` base "
            f"(notional ${float(getattr(sizing, 'notional_usd', 0.0)):,.2f}, "
            f"margin ${float(getattr(sizing, 'margin_usd', 0.0)):,.2f}, "
            f"leverage {float(getattr(sizing, 'leverage', 0.0)):g}x)"
        )
        capped = getattr(sizing, "capped_by", None)
        if capped:
            out.append(f"- capped by: `{capped}`")
        out.append("")

    # ---- funding
    out.append("## Funding over the horizon")
    out.append("")
    out.append("| Measure | Value |")
    out.append("|---|---|")
    out.append(f"| Measured funding rate | `{analysis.measured_rate:.6f}` per {analysis.interval_h:g} h |")
    out.append(f"| Measured, annualized | {analysis.measured_annualized_pct:.2f} % |")
    out.append(f"| Settlements in the horizon | {edge.settlements} |")
    out.append(f"| Funding over the horizon | {_fmt_bps(edge.funding_bps_horizon)} |")
    out.append(f"| Rate source | `{analysis.measured_rate_source}` |")
    out.append("")
    out.append(f"{analysis.measured_rate_note}")
    out.append("")

    # ---- breakeven
    out.append("## Breakeven holding period")
    out.append("")
    out.append(
        f"The carry has to repay {analysis.cost_to_recover_bps:.2f} bps "
        "(round trip plus assumed exit basis, less the entry basis)."
    )
    out.append("")
    be = analysis.breakeven
    out.append(f"- **Measured rate:** {be.verdict}")
    if analysis.breakeven_assumed is not None:
        ab = analysis.breakeven_assumed
        out.append(
            f"- **Assumed rate ({ASSUMPTION_LABEL}):** {ab.verdict} "
            f"Assumed rate `{ab.rate_per_interval:.6f}` per {ab.interval_h:g} h; "
            f"measured testnet rate `{analysis.measured_rate:.6f}` per {analysis.interval_h:g} h."
        )
    out.append("")

    # ---- curve
    out.append("## Edge versus holding horizon")
    out.append("")
    curves = [("measured", analysis.curve)]
    if analysis.curve_assumed is not None:
        curves.append(("assumed", analysis.curve_assumed))
    for basis, cur in curves:
        tag = f" ({ASSUMPTION_LABEL})" if cur.is_assumption else ""
        out.append(f"**{basis.capitalize()} rate `{cur.rate_per_interval:.6f}` per {cur.interval_h:g} h**{tag}")
        out.append("")
        out.append("| Horizon (h) | Days | Settlements | Funding (bps) | Net edge (bps) |")
        out.append("|---:|---:|---:|---:|---:|")
        pts = list(cur.points)
        if curve_rows > 0 and len(pts) > curve_rows:
            step = max(1, len(pts) // curve_rows)
            kept = pts[::step][:curve_rows]
            if kept and kept[-1].horizon_h != pts[-1].horizon_h:
                kept.append(pts[-1])  # always show where the range ends
            pts = kept
        for p in pts:
            out.append(
                f"| {p.horizon_h:g} | {p.horizon_days:.2f} | {p.settlements} | {p.funding_bps:+.2f} | {p.net_edge_bps:+.2f} |"
            )
        out.append("")
        if cur.zero_cross_h is None:
            out.append("Net edge does not reach zero at any horizon in this range at this rate.")
        else:
            out.append(f"Zero crossing at {cur.zero_cross_h:g} h ({cur.zero_cross_days:.1f} days).")
        out.append("")
    out.append(analysis.curve.note)
    out.append("")

    # ---- provenance
    out.append("## Data sources and feed age")
    out.append("")
    out.append("| Feed | Source tag | Age |")
    out.append("|---|---|---|")
    if market is not None:
        fresh = getattr(market, "freshness", None)
        dex = getattr(market, "dex", None)
        fund = getattr(market, "funding", None)
        spot = getattr(market, "cex_spot_ref", None)
        perp = getattr(market, "cex_perp_book", None)
        if dex is not None:
            out.append(_age_row("PancakeSwap V3 quote", getattr(dex, "source", None), getattr(fresh, "dex_age_ms", None)))
        if fund is not None:
            out.append(_age_row("Perp mark / funding", getattr(fund, "source", None), getattr(fresh, "cex_age_ms", None)))
        if perp is not None:
            out.append(_age_row("Perp book ticker", getattr(perp, "source", None), getattr(fresh, "cex_age_ms", None)))
        if spot is not None:
            out.append(_age_row("Spot mirror reference", getattr(spot, "source", None), getattr(fresh, "spot_age_ms", None)))
        out.append(_age_row("Market state", getattr(market, "source", None), None))
        if fresh is not None:
            reason = getattr(fresh, "reason", None)
            out.append("")
            out.append(f"Freshness ok: `{bool(getattr(fresh, 'ok', False))}`" + (f" (`{reason}`)" if reason else "") + ".")
    else:
        out.append(_age_row("Perp mark / funding", analysis.measured_rate_source, None))
        out.append("")
        out.append("No market snapshot was attached to this report, so only the funding source tag is available.")
    out.append("")

    # ---- honesty
    out.append("## Labels")
    out.append("")
    out.append("- Funding is read from the Binance USDS-M Futures **testnet**, where it is structurally near zero, and is indicative.")
    if analysis.assumed_rate is not None:
        out.append(
            f"- The mainnet-typical rate `{analysis.assumed_rate:.6f}` per {analysis.interval_h:g} h used above is an "
            f"**{ASSUMPTION_LABEL}**. {ASSUMPTION_NOTE}"
        )
    else:
        out.append("- No assumed funding rate was used in this report: every rate above is the measured one.")
    out.append("- The PancakeSwap V3 leg is quoted live on BSC mainnet with gas priced into the round trip, and is always simulated.")
    out.append("- The exit basis is an assumption set by configuration, not a measurement.")
    out.append("- This report is a description of Deltr's arithmetic. It is not advice and not a recommendation.")
    out.append("")
    return "\n".join(out)


__all__ = [
    "ASSUMED_FUNDING_RATE_PER_INTERVAL",
    "ASSUMPTION_LABEL",
    "ASSUMPTION_NOTE",
    "MEASURED_NOTE",
    "CURVE_NOTE",
    "FUNDING_INTERVAL_H_DEFAULT",
    "NEAR_ZERO_RATE",
    "BreakevenView",
    "CurvePoint",
    "EdgeCurve",
    "HorizonAnalysis",
    "horizon_analysis",
    "render_edge_report",
]
