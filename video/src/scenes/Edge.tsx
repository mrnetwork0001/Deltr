import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { INK, MONO, SANS, SERIES, STATUS, TEXT } from "../theme";
import { Eyebrow, Grid, Sfx } from "../ui";

// The probe's own numbers (README "Edge math"): every bar is a measured component.
const ROWS = [
  { label: "Entry basis", bps: 2.2, color: SERIES.edge },
  { label: "DEX pool fee ×2", bps: -2.0, color: SERIES.dex },
  { label: "DEX price impact ×2", bps: -0.6, color: SERIES.dex },
  { label: "BSC gas ×2", bps: -0.03, color: SERIES.dex },
  { label: "Perp slippage ×2", bps: -4.0, color: SERIES.perp },
  { label: "Perp taker fee ×2", bps: -10.0, color: SERIES.perp },
  { label: "Funding over 72 h", bps: 0.0, color: SERIES.funding },
];
const NET = -14.4;

export const Edge: React.FC<{ frames: number }> = ({ frames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rowAt = (i: number) => Math.round(fps * (2.2 + i * 1.05));
  const netAt = Math.round(fps * 10.2);
  const stampAt = Math.round(fps * 12.0);
  const scale = 34; // px per bps
  const zeroX = 1000;
  let run = 0;
  const bars = ROWS.map((r) => { const from = run; run += r.bps; return { ...r, from, to: run }; });
  const net = spring({ frame: frame - netAt, fps, config: { damping: 14, stiffness: 110 } });
  const stamp = spring({ frame: frame - stampAt, fps, config: { damping: 10, stiffness: 220 } });
  const title = interpolate(frame, [fps * 0.2, fps * 0.9], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <AbsoluteFill>
      <Grid />
      <AbsoluteFill style={{ padding: "90px 140px" }}>
        <Eyebrow>The whole round trip, priced</Eyebrow>
        <div style={{ marginTop: 16, opacity: title, fontFamily: SANS, fontWeight: 800, fontSize: 60, color: TEXT.hi }}>
          A naive bot sees a spread. Deltr prices what it costs to get in and out.
        </div>
        <div style={{ marginTop: 44, position: "relative" }}>
          <div style={{ position: "absolute", left: zeroX, top: 0, bottom: 0, width: 2, background: INK[700] }} />
          {bars.map((b, i) => {
            const p = spring({ frame: frame - rowAt(i), fps, config: { damping: 16, stiffness: 120 } });
            const x0 = zeroX + Math.min(b.from, b.to) * scale;
            const w = Math.max(3, Math.abs(b.to - b.from) * scale) * p;
            const shown = (b.bps * p);
            return (
              <div key={b.label} style={{ display: "flex", alignItems: "center", height: 58, opacity: Math.min(1, p * 2) }}>
                <div style={{ width: 420, fontFamily: SANS, fontSize: 28, color: TEXT.mid }}>{b.label}</div>
                <div style={{ position: "relative", flex: 1, height: 26 }}>
                  <div style={{ position: "absolute", left: x0 - 420, width: w, height: 26, borderRadius: 6, background: b.color, opacity: 0.9 }} />
                </div>
                <div style={{ width: 170, textAlign: "right", fontFamily: MONO, fontSize: 30, color: TEXT.hi }}>
                  {shown > 0 ? "+" : ""}{shown.toFixed(1)}
                </div>
              </div>
            );
          })}
          <div style={{ display: "flex", alignItems: "center", height: 74, marginTop: 10, borderTop: `1px solid ${INK[700]}`, paddingTop: 10, opacity: net }}>
            <div style={{ width: 420, fontFamily: SANS, fontSize: 32, fontWeight: 700, color: TEXT.hi }}>Net edge · 72 h</div>
            <div style={{ position: "relative", flex: 1, height: 30 }}>
              <div style={{ position: "absolute", left: zeroX - 420 + NET * scale * net, width: Math.abs(NET) * scale * net, height: 30, borderRadius: 6, background: TEXT.hi }} />
            </div>
            <div style={{ width: 170, textAlign: "right", fontFamily: MONO, fontSize: 40, fontWeight: 700, color: STATUS.critical }}>{(NET * net).toFixed(1)}</div>
          </div>
        </div>
        <div style={{ position: "absolute", right: 150, bottom: 110, transform: `rotate(-6deg) scale(${stamp})`, opacity: stamp, fontFamily: MONO, fontSize: 54, fontWeight: 800, color: STATUS.critical, border: `5px solid ${STATUS.critical}`, borderRadius: 14, padding: "10px 28px", letterSpacing: "0.08em" }}>
          NOT ACTIONABLE
        </div>
      </AbsoluteFill>
      {ROWS.map((_, i) => (
        <Sfx key={i} id="click" at={rowAt(i)} volume={0.6} />
      ))}
      <Sfx id="veto" at={stampAt} volume={0.55} />
    </AbsoluteFill>
  );
};
