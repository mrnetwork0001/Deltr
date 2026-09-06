// Section 4: the round-trip cost model as a waterfall (inline SVG) plus the
// worked example table and the sizing example. Numbers come from lib/landing.ts.
import { EDGE_EXAMPLE, SIZING_EXAMPLE } from "@/lib/landing";
import Reveal from "./Reveal";
import { Card, Mono, Section, SERIES, nth } from "./Section";

interface Step {
  label: string;
  value: number;
  color: string;
  kind: "delta" | "total";
}

function fmt(x: number, digits = 1): string {
  const s = x > 0 ? "+" : x < 0 ? "−" : "";
  return `${s}${Math.abs(x).toFixed(digits)}`;
}

// A bar with 4 px rounded corners on its data end only (the end that carries
// the value), square on the baseline side.
function barPath(x: number, y: number, w: number, h: number, roundedEnd: "top" | "bottom", r = 4): string {
  const rr = Math.min(r, h / 2, w / 2);
  if (roundedEnd === "top") {
    return `M ${x} ${y + h} V ${y + rr} Q ${x} ${y} ${x + rr} ${y} H ${x + w - rr} Q ${x + w} ${y} ${x + w} ${y + rr} V ${y + h} Z`;
  }
  return `M ${x} ${y} V ${y + h - rr} Q ${x} ${y + h} ${x + rr} ${y + h} H ${x + w - rr} Q ${x + w} ${y + h} ${x + w} ${y + h - rr} V ${y} Z`;
}

function Waterfall() {
  const e = EDGE_EXAMPLE;
  const NEUTRAL = "#9ca3af";
  const steps: Step[] = [
    { label: "basis", value: e.basisBps, color: NEUTRAL, kind: "delta" },
    { label: "pool fee ×2", value: -e.rows[0].roundTrip, color: SERIES.orange, kind: "delta" },
    { label: "impact ×2", value: -e.rows[1].roundTrip, color: SERIES.orange, kind: "delta" },
    { label: "gas ×2", value: -e.rows[4].roundTrip, color: SERIES.orange, kind: "delta" },
    { label: "perp slip ×2", value: -e.rows[2].roundTrip, color: SERIES.blue, kind: "delta" },
    { label: "taker ×2", value: -e.rows[3].roundTrip, color: SERIES.blue, kind: "delta" },
    { label: `funding ${e.horizonH} h`, value: e.fundingBps, color: SERIES.gold, kind: "delta" },
    { label: "net edge", value: e.netBps, color: SERIES.green, kind: "total" },
  ];
  // Label only the endpoint and the single largest cost; the table twin carries
  // the rest.
  const largestCost = steps.filter((st) => st.kind === "delta" && st.value < 0).reduce((a, b) => (b.value < a.value ? b : a));
  const labelled = new Set<string>(["net edge", largestCost.label]);

  const W = 640;
  const H = 300;
  const padL = 48;
  const padR = 16;
  const padT = 30;
  const padB = 44;
  const yMax = 4;
  const yMin = -16;
  const plotH = H - padT - padB;
  const y = (v: number) => padT + ((yMax - v) / (yMax - yMin)) * plotH;
  const n = steps.length;
  const slot = (W - padL - padR) / n;
  const barW = Math.min(48, slot * 0.62);

  let running = 0;
  const bars = steps.map((st, i) => {
    const x = padL + i * slot + (slot - barW) / 2;
    let from: number;
    let to: number;
    if (st.kind === "total") {
      from = 0;
      to = st.value;
    } else {
      from = running;
      to = running + st.value;
      running = to;
    }
    const top = Math.max(from, to);
    const bottom = Math.min(from, to);
    const h = Math.max(2, y(bottom) - y(top));
    return { ...st, x, yTop: y(top), h, from, to, i };
  });

  const gridVals = [4, 0, -4, -8, -12, -16];
  const MONO = "ui-monospace, Menlo, monospace";

  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby="wf-title" className="block h-auto w-full" style={{ minWidth: 600 }}>
      <title id="wf-title">Round-trip cost waterfall in basis points at the 2026-09-02 probe</title>
      {gridVals.map((g) => (
        <g key={g}>
          <line x1={padL} x2={W - padR} y1={y(g)} y2={y(g)} stroke={g === 0 ? "#4b5563" : "#141a26"} strokeWidth={1} />
          <text x={padL - 8} y={y(g) + 4} fill="#9ca3af" fontSize={13} textAnchor="end" fontFamily={MONO}>
            {g}
          </text>
        </g>
      ))}
      <text x={padL - 8} y={padT - 12} fill="#9ca3af" fontSize={13} textAnchor="end" fontFamily={MONO}>
        bps
      </text>
      {bars.map((b, i) => {
        const next = bars[i + 1];
        const connector =
          next && next.kind === "delta" ? <line x1={b.x + barW} x2={next.x} y1={y(b.to)} y2={y(b.to)} stroke="#4b5563" strokeDasharray="3 3" /> : null;
        const goesUp = b.to >= b.from;
        const labelY = goesUp ? b.yTop - 8 : b.yTop + b.h + 18;
        return (
          <g key={b.label}>
            <path
              d={barPath(b.x, b.yTop, barW, b.h, goesUp ? "top" : "bottom")}
              fill={b.color}
              opacity={b.kind === "total" ? 1 : 0.85}
              className="grow-y"
              style={{ ...nth(i), transformOrigin: `${b.x + barW / 2}px ${y(b.from)}px` }}
            />
            {connector}
            {labelled.has(b.label) ? (
              <text x={b.x + barW / 2} y={labelY} fill="#e5e7eb" fontSize={14} fontWeight={600} textAnchor="middle" fontFamily={MONO}>
                {fmt(b.value)}
              </text>
            ) : null}
            <text x={b.x + barW / 2} y={H - padB + 22} fill="#9ca3af" fontSize={13} textAnchor="middle">
              {b.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default function EdgeExplainer() {
  const e = EDGE_EXAMPLE;
  const s = SIZING_EXAMPLE;
  return (
    <Section
      id="edge"
      eyebrow="The edge, honestly"
      title="A naive bot sees a spread; Deltr prices the full round trip"
      lead={
        <>
          The horizon-based edge is <Mono>net(H) = basis_entry + funding(H) − roundtrip − basis_exit_assumed</Mono>, with{" "}
          <Mono>roundtrip = 2 · (dex_fee + dex_impact + perp_slip + cex_taker + gas_leg)</Mono>. Default horizon 72 h, nine funding
          settlements. This is why the gate often says no.
        </>
      }
    >
      <Reveal className="stagger grid gap-4 lg:grid-cols-5">
        <div className="min-w-0 lg:col-span-3" style={nth(0)}>
        <Card>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-base font-semibold text-gray-100">Worked example at the probe</h3>
            <span className="font-mono text-xs text-gray-500">BNBUSDT · mark {e.perpMark.toFixed(2)} vs DEX exec {e.dexExec.toFixed(2)}</span>
          </div>
          <div className="mt-3 overflow-x-auto">
            <Waterfall />
          </div>
          <p className="mt-1 text-sm text-gray-400">
            DEX costs in orange, perp costs in blue, funding in gold. Funding ≈ {e.fundingBps.toFixed(1)} bps at the probe (lastFundingRate 0.0,
            testnet, indicative). Values for the remaining bars are in the table.
          </p>
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {[
              { k: "basis", v: `+${e.basisBps.toFixed(1)} bps`, c: "#e5e7eb" },
              { k: "round trip", v: `−${e.roundTripBps.toFixed(1)} bps`, c: SERIES.orange },
              { k: `funding ${e.horizonH} h`, v: `≈ ${e.fundingBps.toFixed(1)} bps`, c: SERIES.gold },
              { k: "net", v: e.netLabel, c: SERIES.green },
            ].map((t) => (
              <div key={t.k} className="rounded-md border border-ink-700 bg-ink-950 px-3 py-2">
                <p className="text-xs text-gray-500">{t.k}</p>
                <p className="font-mono text-lg font-semibold" style={{ color: t.c }}>
                  {t.v}
                </p>
              </div>
            ))}
          </div>
          <p className="mt-3 text-sm text-gray-400">
            Not actionable: <Mono>NEGATIVE_EDGE</Mono> vetoes at 0 bps. In PAPER mode the demo runs with a labelled{" "}
            <Mono>--min-edge-bps -20</Mono> override so the mechanics are visible; the amber chip stays on screen the whole time.
          </p>
        </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-4 lg:col-span-2" style={nth(1)}>
          <Card>
            <h3 className="text-base font-semibold text-gray-100">Round-trip components</h3>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-gray-500">
                  <tr>
                    <th className="py-1 pr-2 font-medium">component</th>
                    <th className="py-1 pr-2 text-right font-medium">per leg</th>
                    <th className="py-1 text-right font-medium">×2</th>
                  </tr>
                </thead>
                <tbody className="font-mono tabular-nums text-gray-300">
                  {e.rows.map((r) => (
                    <tr key={r.label} className="border-t border-ink-700">
                      <td className="py-1.5 pr-2 font-sans">
                        {r.label}
                        <span className="block text-xs text-gray-500">{r.note}</span>
                      </td>
                      <td className="py-1.5 pr-2 text-right align-top">{r.perLeg === null ? "–" : r.perLeg.toFixed(2)}</td>
                      <td className="py-1.5 text-right align-top">{r.roundTrip.toFixed(2)}</td>
                    </tr>
                  ))}
                  <tr className="border-t border-ink-700 font-semibold text-gray-100">
                    <td className="py-1.5 pr-2 font-sans">round trip</td>
                    <td className="py-1.5 pr-2 text-right" />
                    <td className="py-1.5 text-right">≈ {e.roundTripBps.toFixed(1)} bps</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </Card>
          <Card>
            <h3 className="text-base font-semibold text-gray-100">Sizing: ${s.capitalUsd.toLocaleString("en-US")} at {s.leverage}x</h3>
            <p className="mt-1 text-sm text-gray-400">
              <Mono>N = capital / (1 + 1/L)</Mono>, floored to the 0.01 lot step. The DEX leg is unlevered, so both legs must fit in the
              capital.
            </p>
            <dl className="mt-3 grid grid-cols-2 gap-2 font-mono text-sm tabular-nums">
              {[
                ["quantity", `${s.qtyBnb.toFixed(2)} BNB`],
                ["notional", `$${s.notionalUsd.toLocaleString("en-US")}`],
                ["perp margin", `$${s.marginUsd.toLocaleString("en-US")}`],
                ["cash used", `$${s.cashUsd.toLocaleString("en-US")}`],
              ].map(([k, v]) => (
                <div key={k} className="rounded-md border border-ink-700 bg-ink-950 px-3 py-2">
                  <dt className="font-sans text-xs text-gray-500">{k}</dt>
                  <dd className="text-gray-100">{v}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-sm text-gray-400">At {s.price.toFixed(2)} per BNB. Positions are marked to close, net of the estimated exit round trip.</p>
          </Card>
        </div>
      </Reveal>
    </Section>
  );
}
