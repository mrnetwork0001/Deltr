"use client";
// The animated "screen" on the right of the hero. Everything is inline SVG + CSS
// keyframes (no assets, no data fetch): two price legs scroll continuously with the
// basis band between them, the gate checks light up in sequence, a receipt hash types
// itself out, and the caption cycles through the real demo prompts. Loops forever.
import { useEffect, useState } from "react";
import { DEMO_PROMPTS } from "@/lib/landing";

const W = 600;
const H = 300;
const CHART_TOP = 44;
const CHART_BOTTOM = 196;

// Deterministic, periodic walk so the scroll loops seamlessly (second half == first half).
function walk(seed: number, base: number, amp: number, n = 40): number[] {
  const pts: number[] = [];
  let v = 0;
  let s = seed;
  for (let i = 0; i < n; i++) {
    s = (s * 9301 + 49297) % 233280;
    const r = s / 233280 - 0.5;
    v = v * 0.82 + r * amp;
    pts.push(base + v);
  }
  // ease the tail back toward the head so the wrap has no seam
  for (let i = 0; i < 6; i++) pts[n - 1 - i] = pts[n - 1 - i] * (i / 6) + pts[0] * (1 - i / 6);
  return pts;
}

function path(pts: number[], xStep: number, offset = 0): string {
  return pts.map((y, i) => `${i === 0 ? "M" : "L"}${(i * xStep + offset).toFixed(1)},${y.toFixed(1)}`).join(" ");
}

const DEX = walk(7, 128, 26);
const PERP = DEX.map((y, i) => y - 14 - Math.sin(i / 3) * 6); // the perp sits above spot: that gap is the basis
const STEP = W / (DEX.length - 1);
const dexPath = path(DEX, STEP) + " " + path(DEX, STEP, W).replace(/^M/, "L");
const perpPath = path(PERP, STEP) + " " + path(PERP, STEP, W).replace(/^M/, "L");
const bandPath = (() => {
  const top = path(PERP, STEP) + " " + path(PERP, STEP, W).replace(/^M/, "L");
  const bottomPts = [...DEX, ...DEX].map((y, i) => `L${((i % DEX.length) * STEP + (i >= DEX.length ? W : 0)).toFixed(1)},${y.toFixed(1)}`).reverse();
  return `${top} ${bottomPts.join(" ")} Z`;
})();

const CHECKS = ["KILL SWITCH", "DRAWDOWN", "DELTA = 0", "LEV ≤ 3x", "RISK ≤ 2%", "SANITY", "EDGE"];

export default function HeroScene() {
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setI((k) => (k + 1) % DEMO_PROMPTS.length), 3800);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="relative rounded-xl border border-ink-700 bg-ink-900/70 p-4 shadow-[0_30px_80px_rgba(0,0,0,0.45)] sm:p-5">
      {/* status line */}
      <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.2em] text-gray-400">
        <span className="relative inline-flex h-3.5 w-3.5 items-center justify-center rounded-full border border-cake/50">
          <span className="h-1.5 w-1.5 rounded-full bg-cake hero-pulse" />
        </span>
        <span className="text-cake">Deltr</span> · scanning
        <span className="ml-auto text-gray-500">BNBUSDT · mainnet data</span>
      </div>

      {/* the scene */}
      <div className="mt-3 overflow-hidden rounded-lg border border-ink-700/80 bg-ink-950">
        <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label="Animated view of the DEX and perp legs, the basis between them, the risk gate checks and a sealed receipt">
          <defs>
            <clipPath id="chart"><rect x="0" y={CHART_TOP} width={W} height={CHART_BOTTOM - CHART_TOP} /></clipPath>
            <linearGradient id="band" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#3987e5" stopOpacity="0.28" />
              <stop offset="1" stopColor="#d95926" stopOpacity="0.16" />
            </linearGradient>
            <pattern id="grid" width="30" height="30" patternUnits="userSpaceOnUse">
              <path d="M30 0H0V30" fill="none" stroke="#1f2937" strokeWidth="0.6" />
            </pattern>
          </defs>

          <rect width={W} height={H} fill="url(#grid)" />

          {/* legend */}
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace" fontSize="11">
            <circle cx="16" cy="22" r="3.5" fill="#3987e5" />
            <text x="26" y="26" fill="#9ca3af">Binance perp (short)</text>
            <circle cx="176" cy="22" r="3.5" fill="#d95926" />
            <text x="186" y="26" fill="#9ca3af">PancakeSwap V3 (long)</text>
            <text x={W - 16} y="26" fill="#199e70" textAnchor="end">basis + funding</text>
          </g>

          {/* scrolling price legs */}
          <g clipPath="url(#chart)">
            <g className="hero-scroll">
              <path d={bandPath} fill="url(#band)" />
              <path d={perpPath} fill="none" stroke="#3987e5" strokeWidth="2" strokeLinejoin="round" />
              <path d={dexPath} fill="none" stroke="#d95926" strokeWidth="2" strokeLinejoin="round" />
            </g>
            {/* live marker at the right edge */}
            <line x1={W - 60} x2={W - 60} y1={CHART_TOP} y2={CHART_BOTTOM} stroke="#374151" strokeDasharray="2 4" />
            <circle cx={W - 60} cy={PERP[Math.round((DEX.length - 1) * 0.9)]} r="4" fill="#3987e5" className="hero-pulse" />
            <circle cx={W - 60} cy={DEX[Math.round((DEX.length - 1) * 0.9)]} r="4" fill="#d95926" className="hero-pulse" />
          </g>

          {/* gate checks: light up in sequence, then reset */}
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace" fontSize="10.5" transform={`translate(16 ${CHART_BOTTOM + 18})`}>
            <text x="0" y="0" fill="#6b7280" letterSpacing="1.5">RISK GATE · 19 CHECKS · 1.5 µs</text>
            {CHECKS.map((c, k) => (
              <g key={c} transform={`translate(${k * 82} 14)`} className="hero-check" style={{ animationDelay: `${k * 0.45}s` }}>
                <rect x="0" y="0" width="78" height="20" rx="4" fill="#0ca30c" fillOpacity="0.12" stroke="#0ca30c" strokeOpacity="0.35" />
                <text x="39" y="14" fill="#9ca3af" textAnchor="middle" fontSize="9.5">{c}</text>
              </g>
            ))}
          </g>

          {/* receipt hash that types itself */}
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace" fontSize="10.5" transform={`translate(16 ${H - 16})`}>
            <text x="0" y="0" fill="#6b7280">receipt</text>
            <text x="54" y="0" fill="#199e70" className="hero-type">sha256 d4e39509…c216fda5 · delta 0.00 BNB · paper</text>
          </g>
        </svg>
      </div>

      {/* cycling prompt */}
      <p className="mt-3 text-center font-mono text-sm text-cake sm:truncate" key={i}>
        <span className="hero-fade">“{DEMO_PROMPTS[i]}”</span>
      </p>
    </div>
  );
}
