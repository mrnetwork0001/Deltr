"use client";
// Edge waterfall. The rows come from /api/market `components` (EdgeBreakdown.components()
// on the backend) when it is reachable; edgeComponents() below is the offline/mock fallback.
// Thin horizontal bars, gain = good, cost = critical (polarity is a status),
// net drawn bold in text colour. Text block below carries the funding story.
import { useEffect, useState } from "react";
import type { EdgeBreakdown, EdgeComponent, MarketState } from "@/lib/types";
import { SERIES, STATUS, SURFACE, bps, countdown, pct, rate, usd } from "@/lib/format";
import { fetchMarket } from "@/lib/api";
import { Panel, SourceBadge } from "@/components/StatusBar";
import { useMeasure } from "@/components/SpreadChart";

/** Offline/mock fallback only: mirrors EdgeBreakdown.components() in deltr/models.py. */
export function edgeComponents(e: EdgeBreakdown): EdgeComponent[] {
  const c: EdgeComponent[] = [
    { label: "Entry basis (perp ref - DEX exec)", bps: e.basis_entry_bps, kind: e.basis_entry_bps >= 0 ? "gain" : "cost" },
    { label: "DEX pool fee x2", bps: -2 * e.dex_fee_bps, kind: "cost" },
    { label: "DEX price impact x2", bps: -2 * e.dex_impact_bps, kind: "cost" },
    { label: "Perp slippage x2", bps: -2 * e.perp_slip_bps, kind: "cost" },
    { label: "Perp taker fee x2", bps: -2 * e.cex_taker_bps, kind: "cost" },
    { label: "BSC gas x2", bps: -2 * e.gas_bps_leg, kind: "cost" },
    {
      label: `Funding over ${e.horizon_h} h (${e.settlements} settlements)`,
      bps: e.funding_bps_horizon,
      kind: e.funding_bps_horizon >= 0 ? "gain" : "cost",
    },
  ];
  if (e.basis_exit_assumed_bps) c.push({ label: "Assumed exit basis", bps: -e.basis_exit_assumed_bps, kind: "cost" });
  c.push({ label: "Net edge", bps: e.net_edge_bps, kind: "net" });
  return c;
}

const ROW = 18;
const LABEL_W = 190;
const VAL_W = 60;

// --------------------------------------------------------------------------- horizon view
// Mirrors deltr/horizon.py.  The backend is the source of truth (GET /api/market returns
// `horizon`); the local versions below are the offline/mock fallback only, exactly like
// edgeComponents() above.
export interface CurvePoint {
  horizon_h: number;
  horizon_days: number;
  settlements: number;
  funding_bps: number;
  net_edge_bps: number;
}

export interface EdgeCurve {
  rate_basis: "measured" | "assumed";
  rate_per_interval: number;
  interval_h: number;
  is_assumption: boolean;
  assumption_label: string | null;
  horizon_h: number;
  max_horizon_h: number;
  zero_cross_h: number | null;
  zero_cross_days: number | null;
  note: string;
  points: CurvePoint[];
}

export interface BreakevenView {
  rate_basis: "measured" | "assumed";
  rate_per_interval: number;
  interval_h: number;
  annualized_pct: number;
  is_assumption: boolean;
  assumption_label: string | null;
  cost_to_recover_bps: number;
  settlements: number | null;
  hours: number | null;
  days: number | null;
  horizon_h: number;
  horizon_days: number;
  horizon_settlements: number;
  reached_within_horizon: boolean;
  verdict: string;
}

export interface HorizonAnalysis {
  symbol: string | null;
  notional_usd: number;
  net_edge_bps: number;
  basis_entry_bps: number;
  roundtrip_cost_bps: number;
  basis_exit_assumed_bps: number;
  cost_to_recover_bps: number;
  interval_h: number;
  horizon_h: number;
  horizon_days: number;
  horizon_settlements: number;
  funding_bps_horizon: number;
  measured_rate: number;
  measured_annualized_pct: number;
  measured_rate_source: string;
  measured_rate_note: string;
  assumed_rate: number | null;
  assumption_label: string | null;
  assumption_note: string | null;
  breakeven: BreakevenView;
  breakeven_assumed: BreakevenView | null;
  curve: EdgeCurve;
  curve_assumed: EdgeCurve | null;
  verdict: string;
}

/** Mainnet-typical USDS-M funding, 0.01 % per 8 h. Never measured by Deltr. */
const ASSUMED_RATE = 0.0001;
const ASSUMPTION_LABEL = "assumption, not a measurement";
const CURVE_NOTE =
  "Curve settlements are counted as floor(horizon / interval). The live breakdown counts settlement instants from the next funding time, so the two can differ by one settlement.";

function breakevenSettlements(costBps: number, rate: number): number | null {
  if (rate <= 0 || costBps <= 0) return null;
  const per = rate * 1e4;
  const n = Math.floor(costBps / per);
  return n * per >= costBps ? n : n + 1;
}

function buildBreakeven(costBps: number, rate: number, intervalH: number, horizonH: number, basis: "measured" | "assumed"): BreakevenView {
  const horizonDays = horizonH / 24;
  const n = costBps <= 0 ? 0 : breakevenSettlements(costBps, rate);
  const hours = n === null ? null : n * intervalH;
  const days = hours === null ? null : hours / 24;
  const reached = hours !== null && hours <= horizonH;
  const ratePct = `${(rate * 100).toFixed(4)} %/${intervalH} h`;
  let verdict: string;
  if (n === 0) verdict = `entry basis already covers the round trip at the ${basis} rate (${ratePct}); no holding period is needed.`;
  else if (n === null)
    verdict = `at the ${basis} funding rate (${ratePct}) the carry never repays the ${costBps.toFixed(1)} bps it costs to enter and exit, so no holding period turns this positive.`;
  else
    verdict = `breaks even in ${n} settlement${n === 1 ? "" : "s"} (${(days as number).toFixed(1)} days at the ${basis} rate ${ratePct}); horizon is ${horizonDays.toFixed(1)} days, so the answer is ${reached ? "yes" : "no"}.`;
  return {
    rate_basis: basis,
    rate_per_interval: rate,
    interval_h: intervalH,
    annualized_pct: rate * (24 / intervalH) * 365 * 100,
    is_assumption: basis === "assumed",
    assumption_label: basis === "assumed" ? ASSUMPTION_LABEL : null,
    cost_to_recover_bps: costBps,
    settlements: n,
    hours,
    days,
    horizon_h: horizonH,
    horizon_days: horizonDays,
    horizon_settlements: Math.floor(horizonH / intervalH),
    reached_within_horizon: reached,
    verdict,
  };
}

function buildCurve(
  basisEntry: number,
  fixedCost: number,
  rate: number,
  intervalH: number,
  horizonH: number,
  be: BreakevenView,
  basis: "measured" | "assumed",
): EdgeCurve {
  let span = Math.max(horizonH * 2, 24, horizonH);
  if (be.hours) span = Math.max(span, be.hours * 1.35);
  span = Math.min(span, 720);
  const steps = Math.max(1, Math.ceil(span / intervalH));
  const points: CurvePoint[] = [];
  let zero: number | null = null;
  for (let k = 0; k <= steps; k += 1) {
    const h = k * intervalH;
    const funding = rate * k * 1e4;
    const net = basisEntry + funding - fixedCost;
    if (zero === null && net >= 0) zero = h;
    points.push({ horizon_h: h, horizon_days: h / 24, settlements: k, funding_bps: funding, net_edge_bps: net });
  }
  return {
    rate_basis: basis,
    rate_per_interval: rate,
    interval_h: intervalH,
    is_assumption: basis === "assumed",
    assumption_label: basis === "assumed" ? ASSUMPTION_LABEL : null,
    horizon_h: horizonH,
    max_horizon_h: steps * intervalH,
    zero_cross_h: zero,
    zero_cross_days: zero === null ? null : zero / 24,
    note: CURVE_NOTE,
    points,
  };
}

/** Offline/mock fallback only: mirrors deltr.horizon.horizon_analysis(). */
export function horizonFallback(e: EdgeBreakdown, intervalH = 8, source = "binance-futures-testnet"): HorizonAnalysis {
  const iv = intervalH > 0 ? intervalH : 8;
  const fixedCost = e.roundtrip_cost_bps + e.basis_exit_assumed_bps;
  const cost = fixedCost - e.basis_entry_bps;
  const measuredBe = buildBreakeven(cost, e.funding_rate_last, iv, e.horizon_h, "measured");
  const measuredCurve = buildCurve(e.basis_entry_bps, fixedCost, e.funding_rate_last, iv, e.horizon_h, measuredBe, "measured");
  const useAssumed = measuredBe.settlements === null && e.funding_rate_last <= 1e-9;
  const assumedBe = useAssumed ? buildBreakeven(cost, ASSUMED_RATE, iv, e.horizon_h, "assumed") : null;
  return {
    symbol: null,
    notional_usd: e.notional_usd,
    net_edge_bps: e.net_edge_bps,
    basis_entry_bps: e.basis_entry_bps,
    roundtrip_cost_bps: e.roundtrip_cost_bps,
    basis_exit_assumed_bps: e.basis_exit_assumed_bps,
    cost_to_recover_bps: cost,
    interval_h: iv,
    horizon_h: e.horizon_h,
    horizon_days: e.horizon_h / 24,
    horizon_settlements: e.settlements,
    funding_bps_horizon: e.funding_bps_horizon,
    measured_rate: e.funding_rate_last,
    measured_annualized_pct: e.funding_rate_last * (24 / iv) * 365 * 100,
    measured_rate_source: source,
    measured_rate_note: "Measured from the Binance USDS-M Futures testnet premiumIndex feed. Testnet funding is structurally near zero and is indicative only.",
    assumed_rate: useAssumed ? ASSUMED_RATE : null,
    assumption_label: useAssumed ? ASSUMPTION_LABEL : null,
    assumption_note: useAssumed
      ? "Binance Futures testnet funding is structurally near zero, so the measured carry never repays the round trip. The rate used for this view is a mainnet-typical figure supplied by Deltr, not a rate Deltr measured. The measured testnet rate is reported beside it."
      : null,
    breakeven: measuredBe,
    breakeven_assumed: assumedBe,
    curve: measuredCurve,
    curve_assumed: assumedBe ? buildCurve(e.basis_entry_bps, fixedCost, ASSUMED_RATE, iv, e.horizon_h, assumedBe, "assumed") : null,
    verdict: measuredBe.verdict,
  };
}

function Waterfall({ width, comps }: { width: number; comps: EdgeComponent[] }) {
  const plotW = Math.max(40, width - LABEL_W - VAL_W - 8);
  // running totals
  let run = 0;
  const bars = comps.map((c) => {
    if (c.kind === "net") return { c, from: 0, to: c.bps };
    const from = run;
    run += c.bps;
    return { c, from, to: run };
  });
  const lo = Math.min(0, ...bars.map((b) => Math.min(b.from, b.to)));
  const hi = Math.max(0, ...bars.map((b) => Math.max(b.from, b.to)));
  const span = hi - lo || 1;
  const x = (v: number) => LABEL_W + ((v - lo) / span) * plotW;
  const [hover, setHover] = useState<number | null>(null);
  const height = bars.length * ROW + 6;
  return (
    <svg width={width} height={height} role="img" aria-label="Edge waterfall in basis points" className="overflow-visible">
      <line x1={x(0)} x2={x(0)} y1={0} y2={height - 4} stroke={SURFACE.hairline} strokeWidth={1} />
      {bars.map((b, i) => {
        const isNet = b.c.kind === "net";
        const color = isNet ? SURFACE.text : b.c.kind === "gain" ? STATUS.good : STATUS.critical;
        const x0 = Math.min(x(b.from), x(b.to));
        const w = Math.max(2, Math.abs(x(b.to) - x(b.from)));
        const y = i * ROW + 3;
        const h = isNet ? 12 : 10;
        const rightEnd = b.to >= b.from;
        return (
          <g key={i} onPointerEnter={() => setHover(i)} onPointerLeave={() => setHover(null)}>
            <rect x={0} y={i * ROW} width={width} height={ROW} fill={hover === i ? SURFACE.raised : "transparent"} />
            <text x={0} y={y + h - 2} fontSize={11} fill={isNet ? SURFACE.text : SURFACE.text2} fontWeight={isNet ? 700 : 400}>
              {b.c.label.length > 34 ? b.c.label.slice(0, 33) + "…" : b.c.label}
            </text>
            <rect
              x={x0}
              y={y + (isNet ? 0 : 1)}
              width={w}
              height={h}
              fill={color}
              opacity={hover === i ? 1 : 0.9}
              rx={2}
              clipPath={undefined}
              style={{ clipPath: rightEnd ? "inset(0 0 0 0 round 0 4px 4px 0)" : "inset(0 0 0 0 round 4px 0 0 4px)" }}
            />
            <text
              x={LABEL_W + plotW + 6}
              y={y + h - 2}
              fontSize={11}
              fontWeight={isNet ? 700 : 500}
              fill={SURFACE.text}
              className="font-mono tabular-nums"
            >
              {bps(b.c.bps, 1, false)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

const CURVE_M = { l: 6, r: 46, t: 12, b: 18 };
const CURVE_H = 132;

/** Net edge (bps) against holding horizon (days). One x axis; the zero line is the y reference. */
function HorizonCurve({ width, hz }: { width: number; hz: HorizonAnalysis }) {
  const w = Math.max(220, width);
  const curves: EdgeCurve[] = [hz.curve, ...(hz.curve_assumed ? [hz.curve_assumed] : [])];
  const pts = curves.flatMap((c) => c.points);
  if (pts.length < 2) return null;
  const maxD = Math.max(...pts.map((p) => p.horizon_days), hz.horizon_days, 0.5);
  const lo = Math.min(0, ...pts.map((p) => p.net_edge_bps));
  const hi = Math.max(0, ...pts.map((p) => p.net_edge_bps));
  const pad = (hi - lo || 1) * 0.12;
  const y0 = lo - pad;
  const y1 = hi + pad;
  const px = (d: number) => CURVE_M.l + (d / maxD) * (w - CURVE_M.l - CURVE_M.r);
  const py = (v: number) => CURVE_M.t + (1 - (v - y0) / (y1 - y0)) * (CURVE_H - CURVE_M.t - CURVE_M.b);
  const path = (c: EdgeCurve) => c.points.map((p, i) => `${i ? "L" : "M"}${px(p.horizon_days).toFixed(1)},${py(p.net_edge_bps).toFixed(1)}`).join("");
  const ticks = [0, maxD / 2, maxD];
  return (
    <svg width={w} height={CURVE_H} role="img" aria-label="Net edge in basis points against holding horizon in days">
      {/* zero line: where the carry has paid for the round trip */}
      <line x1={CURVE_M.l} x2={w - CURVE_M.r} y1={py(0)} y2={py(0)} stroke={SURFACE.hairline} strokeWidth={1} />
      <text x={w - CURVE_M.r + 4} y={py(0) + 3} fontSize={10} fill={SURFACE.muted} className="font-mono tabular-nums">
        0
      </text>
      {/* configured horizon marker */}
      <line
        x1={px(hz.horizon_days)}
        x2={px(hz.horizon_days)}
        y1={CURVE_M.t - 4}
        y2={CURVE_H - CURVE_M.b}
        stroke={SURFACE.muted}
        strokeWidth={1}
        strokeDasharray="2 3"
      />
      <text x={px(hz.horizon_days) + 3} y={CURVE_M.t + 4} fontSize={10} fill={SURFACE.text2}>
        horizon {hz.horizon_days.toFixed(1)} d
      </text>
      {curves.map((c) => (
        <g key={c.rate_basis}>
          <path
            d={path(c)}
            fill="none"
            stroke={SERIES.edge}
            strokeWidth={c.is_assumption ? 1.5 : 2}
            strokeDasharray={c.is_assumption ? "4 3" : undefined}
            opacity={c.is_assumption ? 0.75 : 1}
          />
          {c.zero_cross_days !== null ? (
            <g>
              <circle cx={px(c.zero_cross_days)} cy={py(0)} r={3.5} fill={SERIES.edge} opacity={c.is_assumption ? 0.75 : 1} />
              <text
                x={px(c.zero_cross_days) + 6}
                y={py(0) - 6}
                fontSize={10}
                fill={SERIES.edge}
                className="font-mono tabular-nums"
              >
                {c.zero_cross_days.toFixed(1)} d
              </text>
            </g>
          ) : null}
        </g>
      ))}
      {/* x axis: holding horizon in days */}
      <line x1={CURVE_M.l} x2={w - CURVE_M.r} y1={CURVE_H - CURVE_M.b} y2={CURVE_H - CURVE_M.b} stroke={SURFACE.hairline} strokeWidth={1} />
      {ticks.map((d) => (
        <text
          key={d}
          x={px(d)}
          y={CURVE_H - 5}
          fontSize={10}
          fill={SURFACE.muted}
          textAnchor={d === 0 ? "start" : d === maxD ? "end" : "middle"}
          className="font-mono tabular-nums"
        >
          {d.toFixed(1)}
        </text>
      ))}
      <text x={(CURVE_M.l + w - CURVE_M.r) / 2} y={CURVE_M.t - 3} fontSize={10} fill={SURFACE.muted} textAnchor="middle">
        holding horizon (days) -&gt;
      </text>
    </svg>
  );
}

export interface EdgeWaterfallProps {
  edge: EdgeBreakdown | null;
  market: MarketState | null;
  actionable: boolean | null;
  reason: string | null;
  mock: boolean;
}

export default function EdgeWaterfall({ edge, market, actionable, reason, mock }: EdgeWaterfallProps) {
  const [ref, width] = useMeasure<HTMLDivElement>();
  const [horizon, setHorizon] = useState<24 | 72>(72);
  const [alt, setAlt] = useState<{ edge: EdgeBreakdown; components: EdgeComponent[] | null; horizon: HorizonAnalysis | null } | null>(null);
  const [altErr, setAltErr] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    let live = true;
    if (mock || !edge) {
      setAlt(null);
      setAltErr(null);
      return;
    }
    fetchMarket(horizon)
      .then((r) => {
        const withHorizon = r as typeof r & { horizon?: HorizonAnalysis | null };
        if (live)
          setAlt(
            r.edge
              ? { edge: r.edge, components: r.components?.length ? r.components : null, horizon: withHorizon.horizon ?? null }
              : null,
          );
      })
      .catch((e) => live && setAltErr(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [horizon, mock, edge?.horizon_h, edge]);

  // Backend view for the selected horizon (its own component rows) when reachable; else the snapshot edge + local fallback rows.
  const fromApi = alt && alt.edge.horizon_h === horizon ? alt : null;
  const e = fromApi?.edge ?? edge;
  const fund = market?.funding ?? null;
  const comps = e ? (fromApi?.components && fromApi.edge === e ? fromApi.components : edgeComponents(e)) : [];
  const net = e?.net_edge_bps ?? null;
  // Backend horizon view when reachable, else the local mirror (mock / API down).
  const hz = e ? (fromApi?.horizon && fromApi.edge === e ? fromApi.horizon : horizonFallback(e, fund?.interval_h ?? 8, fund?.source ?? undefined)) : null;

  return (
    <Panel
      title="Edge waterfall"
      right={
        <>
          <span>horizon</span>
          {[24, 72].map((h) => (
            <button
              key={h}
              onClick={() => setHorizon(h as 24 | 72)}
              className={`rounded border px-1.5 py-[1px] font-mono ${horizon === h ? "border-bnb text-bnb" : "border-ink-700 text-gray-400 hover:text-gray-200"}`}
            >
              {h} h
            </button>
          ))}
        </>
      }
    >
      <div ref={ref} className="flex flex-col gap-2">
        {e ? (
          <>
            <div className="flex items-center gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-wide text-gray-500">net edge · {e.horizon_h} h</div>
                <div className="text-2xl font-semibold" style={{ color: net !== null && net >= 0 ? STATUS.good : STATUS.critical }}>
                  {bps(net)}
                </div>
              </div>
              <div className="text-xs text-gray-400">
                <div>
                  notional <span className="font-mono tabular-nums text-gray-200">{usd(e.notional_usd, 0)}</span>
                </div>
                <div>
                  expected <span className="font-mono tabular-nums text-gray-200">{usd(e.expected_edge_usd)}</span> · allocated risk{" "}
                  <span className="font-mono tabular-nums text-gray-200">{usd(e.allocated_risk_usd)}</span>
                </div>
              </div>
              <div className="ml-auto text-right text-xs">
                {actionable === null ? null : actionable ? (
                  <span style={{ color: STATUS.good }}>actionable</span>
                ) : (
                  <span style={{ color: STATUS.warning }}>not actionable</span>
                )}
                {reason && reason !== "ok" ? <div className="font-mono text-[11px] text-gray-500">{reason}</div> : null}
              </div>
            </div>
            <Waterfall width={width} comps={comps} />
            {altErr ? <div className="text-[11px] text-gray-500">{horizon} h view unavailable ({altErr}); showing {edge?.horizon_h} h</div> : null}
            {hz ? (
              <div className="flex flex-col gap-1 border-t border-ink-700 pt-2">
                <div className="flex flex-wrap items-baseline gap-x-2 text-[11px]">
                  <span className="uppercase tracking-wide text-gray-500">breakeven</span>
                  <span style={{ color: hz.breakeven.reached_within_horizon ? STATUS.good : STATUS.warning }}>{hz.breakeven.verdict}</span>
                </div>
                <div className="text-[11px] text-gray-400">
                  the carry has to repay{" "}
                  <span className="font-mono tabular-nums text-gray-200">{bps(hz.cost_to_recover_bps)}</span> (round trip plus assumed
                  exit basis, less the entry basis) · measured rate{" "}
                  <span className="font-mono tabular-nums text-gray-200">{rate(hz.measured_rate)}</span> / {hz.interval_h} h
                  <SourceBadge source={hz.measured_rate_source} />
                </div>
                {hz.breakeven_assumed ? (
                  <div className="flex flex-col gap-0.5 rounded-sm border px-2 py-1" style={{ borderColor: STATUS.warning + "55", background: STATUS.warning + "10" }}>
                    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                      <span
                        className="inline-flex items-center rounded-sm border px-1 font-mono text-[10px] leading-[14px] uppercase tracking-tight"
                        style={{ color: STATUS.warning, borderColor: STATUS.warning + "80", background: STATUS.warning + "14" }}
                      >
                        {hz.assumption_label}
                      </span>
                      <span className="text-gray-300">
                        at <span className="font-mono tabular-nums">{rate(hz.assumed_rate)}</span> / {hz.interval_h} h (mainnet typical,
                        supplied by Deltr, never measured here)
                      </span>
                    </div>
                    <div className="text-[11px] text-gray-400">{hz.breakeven_assumed.verdict}</div>
                    <div className="text-[11px] text-gray-500">
                      measured Binance Futures testnet rate beside it:{" "}
                      <span className="font-mono tabular-nums text-gray-300">{rate(hz.measured_rate)}</span> / {hz.interval_h} h ·
                      testnet funding is structurally near zero
                    </div>
                  </div>
                ) : null}
                <HorizonCurve width={width} hz={hz} />
                <div className="flex flex-wrap items-center gap-x-3 text-[11px] text-gray-500">
                  <span className="flex items-center gap-1">
                    <svg width={16} height={6} aria-hidden="true">
                      <line x1={0} x2={16} y1={3} y2={3} stroke={SERIES.edge} strokeWidth={2} />
                    </svg>
                    net edge at the measured rate
                  </span>
                  {hz.curve_assumed ? (
                    <span className="flex items-center gap-1">
                      <svg width={16} height={6} aria-hidden="true">
                        <line x1={0} x2={16} y1={3} y2={3} stroke={SERIES.edge} strokeWidth={1.5} strokeDasharray="4 3" opacity={0.75} />
                      </svg>
                      net edge at the assumed rate ({hz.assumption_label})
                    </span>
                  ) : null}
                  <span title={hz.curve.note}>settlements counted as floor(horizon / interval)</span>
                </div>
              </div>
            ) : null}
            <div className="grid grid-cols-3 gap-x-3 gap-y-1 border-t border-ink-700 pt-2 text-[11px] text-gray-400">
              <div>
                basis <span className="font-mono tabular-nums text-gray-200">{bps(e.basis_entry_bps)}</span>
              </div>
              <div>
                round trip <span className="font-mono tabular-nums text-gray-200">{bps(-e.roundtrip_cost_bps)}</span>
              </div>
              <div>
                shock assumed <span className="font-mono tabular-nums text-gray-200">{bps(e.basis_shock_bps, 0)}</span>
              </div>
              <div className="col-span-3 flex flex-wrap items-center gap-x-2">
                funding
                <span className="font-mono tabular-nums text-gray-200">{rate(fund?.last_funding_rate ?? e.funding_rate_last)}</span>
                / {fund?.interval_h ?? 8} h ·
                <span className="font-mono tabular-nums text-gray-200">{pct(fund?.annualized_pct ?? e.funding_rate_last * (24 / (fund?.interval_h ?? 8)) * 365 * 100)}</span> annualized
                {fund ? (
                  <>
                    · next settlement in <span className="font-mono tabular-nums text-gray-200">{countdown(fund.next_funding_time_ms, now)}</span>
                  </>
                ) : null}
                <SourceBadge source={fund?.source ?? "binance-futures-testnet"} />
                <span className="text-gray-500">Binance Futures testnet: indicative</span>
              </div>
            </div>
          </>
        ) : (
          <div className="flex h-40 items-center justify-center text-xs text-gray-500">no edge yet: waiting for DEX and perp quotes</div>
        )}
      </div>
    </Panel>
  );
}
