import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { BNB, MONO, SANS, TEXT } from "../theme";
import { Eyebrow, Grid, Mark, Sfx } from "../ui";

const LETTERS = ["D", "E", "L", "T", "R"];

export const Intro: React.FC<{ frames: number }> = ({ frames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const letterAt = (i: number) => Math.round(fps * (1.0 + i * 0.16));
  const tag = interpolate(frame, [fps * 2.1, fps * 2.7], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const sub = interpolate(frame, [fps * 2.9, fps * 3.5], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <AbsoluteFill>
      <Grid />
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", flexDirection: "row", gap: 56 }}>
        <Mark size={190} />
        <div>
          <div style={{ display: "flex", gap: 30 }}>
            {LETTERS.map((ch, i) => {
              const s = spring({ frame: frame - letterAt(i), fps, config: { damping: 14, stiffness: 160 } });
              return (
                <span key={ch} style={{ fontFamily: SANS, fontWeight: 800, fontSize: 168, color: TEXT.hi, letterSpacing: "0.02em", display: "inline-block", opacity: s, transform: `translateY(${(1 - s) * 40}px)` }}>
                  {ch}
                </span>
              );
            })}
          </div>
          <div style={{ fontFamily: MONO, fontSize: 30, letterSpacing: "0.32em", color: BNB, opacity: tag, marginTop: 6 }}>DELTA-NEUTRAL BY CONSTRUCTION.</div>
          <div style={{ fontFamily: SANS, fontSize: 26, color: TEXT.mid, opacity: sub, marginTop: 26 }}>A CEX / DEX basis and funding agent on Binance Agent OS</div>
        </div>
      </AbsoluteFill>
      <div style={{ position: "absolute", bottom: 60, left: 0, right: 0, textAlign: "center", opacity: sub }}>
        <Eyebrow color={TEXT.lo}>Binance Agent OS Mini Hackathon · Track A · Trading workflows</Eyebrow>
      </div>
      {LETTERS.map((_, i) => (
        <Sfx key={i} id="key" at={letterAt(i)} volume={0.7} />
      ))}
      <Sfx id="ding" at={Math.round(fps * 2.1)} volume={0.35} />
    </AbsoluteFill>
  );
};
