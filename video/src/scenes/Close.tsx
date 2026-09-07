import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { BNB, CAKE, INK, MONO, SANS, TEXT } from "../theme";
import { Eyebrow, Grid, Mark, Sfx } from "../ui";

const STATS = [
  { value: 22, label: "MCP tools" },
  { value: 19, label: "risk checks" },
  { value: 683, label: "tests passing" },
  { value: 500, label: "days of funding data" },
];

export const Close: React.FC<{ frames: number }> = ({ frames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const tileAt = (i: number) => Math.round(fps * (0.5 + i * 0.45));
  const brand = spring({ frame: frame - fps * 3.4, fps, config: { damping: 14, stiffness: 120 } });
  const links = interpolate(frame, [fps * 4.6, fps * 5.3], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <AbsoluteFill>
      <Grid />
      <AbsoluteFill style={{ padding: "100px 140px" }}>
        <Eyebrow>Measured, not aspirational</Eyebrow>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 26, marginTop: 36 }}>
          {STATS.map((s, i) => {
            const p = spring({ frame: frame - tileAt(i), fps, config: { damping: 18, stiffness: 90 } });
            return (
              <div key={s.label} style={{ opacity: p, transform: `translateY(${(1 - p) * 30}px)`, background: INK[900], border: `1px solid ${INK[700]}`, borderRadius: 16, padding: "34px 30px" }}>
                <div style={{ fontFamily: MONO, fontSize: 84, fontWeight: 700, color: TEXT.hi }}>{Math.round(s.value * Math.min(1, p))}</div>
                <div style={{ fontFamily: MONO, fontSize: 20, letterSpacing: "0.22em", textTransform: "uppercase", color: BNB, marginTop: 8 }}>{s.label}</div>
              </div>
            );
          })}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 40, marginTop: 90, opacity: brand, transform: `translateY(${(1 - brand) * 20}px)` }}>
          <Mark size={120} delay={Math.round(fps * 3.2)} />
          <div>
            <div style={{ fontFamily: SANS, fontWeight: 800, fontSize: 92, color: TEXT.hi, letterSpacing: "0.02em" }}>DELTR</div>
            <div style={{ fontFamily: MONO, fontSize: 24, letterSpacing: "0.3em", color: BNB }}>DELTA-NEUTRAL BY CONSTRUCTION.</div>
          </div>
        </div>
        <div style={{ marginTop: 50, opacity: links, fontFamily: MONO, fontSize: 30, color: TEXT.mid, display: "flex", gap: 60 }}>
          <span><span style={{ color: CAKE }}>▸</span> usedeltrapp.vercel.app</span>
          <span><span style={{ color: CAKE }}>▸</span> github.com/mrnetwork0001/Deltr</span>
        </div>
      </AbsoluteFill>
      {STATS.map((_, i) => (
        <Sfx key={i} id="click" at={tileAt(i)} volume={0.6} />
      ))}
      <Sfx id="ding" at={Math.round(fps * 3.4)} volume={0.5} />
    </AbsoluteFill>
  );
};
