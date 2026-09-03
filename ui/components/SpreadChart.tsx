"use client";
// Last 10 minutes: two small single-axis charts (never dual-axis):
//   1. DEX executable buy vs perp reference (mark) - price
//   2. Net edge bps vs the min-edge threshold, actionable ticks highlighted
// Inline SVG, 2 px lines, hairline grid, crosshair + tooltip, latest value direct-labelled.
import { useEffect, useMemo, useRef, useState } from "react";
import type { MarketState, SpreadPoint } from "@/lib/types";
import { SERIES, STATUS, SURFACE, bps, clock, px } from "@/lib/format";
import { Panel, SourceBadge } from "@/components/StatusBar";

export function useMeasure<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setW(Math.floor(e.contentRect.width));
    });
    ro.observe(el);
    setW(Math.floor(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const M = { l: 8, r: 58, t: 10, b: 20 };

function extent(vals: number[], pad = 0.15): [number, number] {
  if (!vals.length) return [0, 1];
  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  if (hi - lo < 1e-9) {
    lo -= Math.abs(lo) * 0.001 || 0.5;
    hi += Math.abs(hi) * 0.001 || 0.5;
  }
  const p = (hi - lo) * pad;
  return [lo - p, hi + p];
}

function niceTicks(lo: number, hi: number, n = 4): number[] {
  const span = hi - lo;
  if (span <= 0) return [lo];
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toFixed(6)));
  return out;
}

interface LineSeries {
  key: string;
  label: string;
  color: string;
  values: number[];
  fmt: (v: number) => string;
}

function LineChart({
  width,
  height,
  xs,
  series,
  threshold,
  highlight,
  ariaLabel,
}: {
  width: number;
  height: number;
  xs: number[]; // epoch ms
  series: LineSeries[];
  threshold?: { value: number; label: string } | null;
  highlight?: boolean[];
  ariaLabel: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const plotW = Math.max(10, width - M.l - M.r);
  const plotH = Math.max(10, height - M.t - M.b);
  const n = xs.length;
  const all = series.flatMap((s) => s.values).concat(threshold ? [threshold.value] : []);
  const [lo, hi] = extent(all);
  const x = (i: number) => M.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v: number) => M.t + plotH - ((v - lo) / (hi - lo)) * plotH;
  const ticks = niceTicks(lo, hi, 3);
  const path = (vals: number[]) => vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  if (n === 0 || width < 40) {
    return (
      <div className="flex items-center justify-center text-xs text-gray-500" style={{ height }}>
        no ticks yet
      </div>
    );
  }
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - r.left - M.l) / plotW;
    setHover(Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1)))));
  };
  const last = n - 1;
  // Latest value direct-labelled at the line end (in the series colour); labels nudged apart when two lines end close together.
  const endLabels = series
    .map((s) => ({ key: s.key, color: s.color, cy: y(s.values[last]), ly: y(s.values[last]), text: s.fmt(s.values[last]) }))
    .sort((a, b) => a.ly - b.ly);
  for (let i = 1; i < endLabels.length; i++) {
    if (endLabels[i].ly - endLabels[i - 1].ly < 12) endLabels[i].ly = endLabels[i - 1].ly + 12;
  }
  for (let i = endLabels.length - 1; i >= 0; i--) {
    const maxY = M.t + plotH - (endLabels.length - 1 - i) * 12;
    if (endLabels[i].ly > maxY) endLabels[i].ly = maxY;
  }
  const tipX = hover !== null ? x(hover) : 0;
  const tipRight = hover !== null && tipX > width * 0.55;

  return (
    <svg
      width={width}
      height={height}
      role="img"
      aria-label={ariaLabel}
      className="select-none overflow-visible"
      onPointerMove={onMove}
      onPointerLeave={() => setHover(null)}
    >
      {ticks.map((t) => (
        <g key={t}>
          <line x1={M.l} x2={M.l + plotW} y1={y(t)} y2={y(t)} stroke={SURFACE.grid} strokeWidth={1} />
          <text x={M.l + plotW + 4} y={y(t) + 3} fontSize={10} fill={SURFACE.muted} className="font-mono tabular-nums">
            {series[0]?.fmt(t) ?? t}
          </text>
        </g>
      ))}
      {[0, Math.floor(last / 2), last].map((i) => (
        <text key={i} x={x(i)} y={height - 5} fontSize={10} fill={SURFACE.muted} textAnchor={i === 0 ? "start" : i === last ? "end" : "middle"} className="font-mono">
          {clock(new Date(xs[i]).toISOString())}
        </text>
      ))}
      {threshold ? (
        <g>
          <line x1={M.l} x2={M.l + plotW} y1={y(threshold.value)} y2={y(threshold.value)} stroke={SURFACE.text2} strokeWidth={1} />
          <text x={M.l + 2} y={y(threshold.value) - 3} fontSize={10} fill={SURFACE.text2}>
            {threshold.label}
          </text>
        </g>
      ) : null}
      {highlight
        ? highlight.map((h, i) =>
            h ? <rect key={i} x={x(i) - 1} y={M.t + plotH - 3} width={2} height={3} fill={SERIES.edge} opacity={0.9} /> : null,
          )
        : null}
      {series.map((s) => (
        <path key={s.key} d={path(s.values)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      ))}
      {endLabels.map((l) => (
        <g key={l.key + "-end"}>
          <circle cx={x(last)} cy={l.cy} r={4} fill={l.color} stroke={SURFACE.card} strokeWidth={2} />
          <rect x={M.l + plotW + 2} y={l.ly - 7} width={M.r - 4} height={13} rx={2} fill={SURFACE.card} />
          <text x={M.l + plotW + 4} y={l.ly + 3} fontSize={10} fontWeight={600} fill={l.color} className="font-mono tabular-nums">
            {l.text}
          </text>
        </g>
      ))}
      {hover !== null ? (
        <g>
          <line x1={tipX} x2={tipX} y1={M.t} y2={M.t + plotH} stroke={SURFACE.text2} strokeWidth={1} />
          {series.map((s) => (
            <circle key={s.key} cx={tipX} cy={y(s.values[hover])} r={4} fill={s.color} stroke={SURFACE.card} strokeWidth={2} />
          ))}
          <foreignObject x={tipRight ? tipX - 160 : tipX + 8} y={M.t} width={152} height={20 + series.length * 16 + (highlight ? 16 : 0)}>
            <div className="rounded border border-ink-700 bg-ink-800/95 px-2 py-1 text-[11px] text-gray-300 shadow">
              <div className="font-mono text-gray-500">{clock(new Date(xs[hover]).toISOString())}</div>
              {series.map((s) => (
                <div key={s.key} className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1">
                    <span className="inline-block h-[2px] w-3" style={{ background: s.color }} />
                    {s.label}
                  </span>
                  <span className="font-mono font-semibold tabular-nums text-gray-100">{s.fmt(s.values[hover])}</span>
                </div>
              ))}
              {highlight ? <div className="text-gray-500">{highlight[hover] ? "actionable" : "not actionable"}</div> : null}
            </div>
          </foreignObject>
        </g>
      ) : null}
    </svg>
  );
}

export interface SpreadChartProps {
  history: SpreadPoint[];
  market: MarketState | null;
  minEdgeBps: number | null;
}

export default function SpreadChart({ history, market, minEdgeBps }: SpreadChartProps) {
  const [ref, width] = useMeasure<HTMLDivElement>();
  const pts = useMemo(() => {
    const cutoff = history.length ? new Date(history[history.length - 1].ts).getTime() - 10 * 60_000 : 0;
    const sorted = [...history].sort((a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime());
    return sorted.filter((p) => new Date(p.ts).getTime() >= cutoff);
  }, [history]);
  const xs = pts.map((p) => new Date(p.ts).getTime());
  const last = pts[pts.length - 1];
  const srcHist = last?.source ?? null;
  const dexSrc = market?.dex?.source ?? "bsc-mainnet-chain";
  const perpSrc = market?.funding?.source ?? market?.source ?? "binance-futures-testnet";
  const actionableCount = pts.filter((p) => p.actionable).length;

  return (
    <Panel
      title="Spread · last 10 min"
      right={
        <>
          <span className="font-mono tabular-nums">{pts.length} ticks</span>
          {srcHist ? <SourceBadge source={srcHist} /> : null}
        </>
      }
    >
      <div ref={ref} className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-300">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-[2px] w-4" style={{ background: SERIES.dex }} />
            DEX exec buy
            <span className="font-mono font-semibold tabular-nums text-gray-100">{px(last?.dex_exec ?? market?.dex?.exec_price_buy)}</span>
            <SourceBadge source={dexSrc} />
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-[2px] w-4" style={{ background: SERIES.perp }} />
            perp ref (mark)
            <span className="font-mono font-semibold tabular-nums text-gray-100">{px(last?.perp_ref ?? market?.perp_ref_price)}</span>
            <SourceBadge source={perpSrc} />
          </span>
        </div>
        {pts.length ? (
          <LineChart
            width={width}
            height={132}
            xs={xs}
            ariaLabel="DEX executable buy price vs perp reference price, last 10 minutes"
            series={[
              { key: "dex", label: "DEX exec", color: SERIES.dex, values: pts.map((p) => p.dex_exec), fmt: (v) => px(v) },
              { key: "perp", label: "perp ref", color: SERIES.perp, values: pts.map((p) => p.perp_ref), fmt: (v) => px(v) },
            ]}
          />
        ) : (
          <div className="flex h-[132px] items-center justify-center rounded border border-dashed border-ink-700 text-xs text-gray-500">
            {market ? "collecting ticks" : "feeds warming up: no market state yet"}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-300">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-[2px] w-4" style={{ background: SERIES.edge }} />
            net edge
            <span className="font-mono font-semibold tabular-nums text-gray-100">{bps(last?.net_edge_bps)}</span>
          </span>
          <span className="flex items-center gap-1.5 text-gray-400">
            <span className="inline-block h-px w-4 bg-gray-400" />
            min edge {bps(minEdgeBps)}
          </span>
          <span className="flex items-center gap-1.5 text-gray-400">
            <span className="inline-block h-[3px] w-[2px]" style={{ background: SERIES.edge }} />
            actionable ticks
            <span className="font-mono tabular-nums">
              {actionableCount}/{pts.length}
            </span>
          </span>
          {last && last.net_edge_bps < 0 ? (
            <span className="ml-auto text-gray-500" style={{ color: STATUS.warning }}>
              edge negative: full round trip costs more than the basis
            </span>
          ) : null}
        </div>
        {pts.length ? (
          <LineChart
            width={width}
            height={96}
            xs={xs}
            ariaLabel="Net edge in basis points vs the min-edge threshold"
            series={[{ key: "edge", label: "net edge", color: SERIES.edge, values: pts.map((p) => p.net_edge_bps), fmt: (v) => bps(v, 1, false) }]}
            threshold={minEdgeBps !== null && Number.isFinite(minEdgeBps) ? { value: minEdgeBps, label: `min edge ${bps(minEdgeBps)}` } : null}
            highlight={pts.map((p) => p.actionable)}
          />
        ) : null}
      </div>
    </Panel>
  );
}
