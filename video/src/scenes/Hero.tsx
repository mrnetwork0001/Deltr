import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { INK, MONO, SANS, SERIES, STATUS, TEXT } from "../theme";
import { Eyebrow, Grid, Sfx } from "../ui";

/** Kinetic statement of the trade: two legs slide in from each side and cancel to delta zero. */
export const Hero: React.FC<{ frames: number }> = ({ frames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;
  const s1 = spring({ frame: frame - fps * 0.3, fps, config: { damping: 16, stiffness: 120 } });
  const s2 = spring({ frame: frame - fps * 2.4, fps, config: { damping: 16, stiffness: 120 } });
  const meet = spring({ frame: frame - fps * 5.6, fps, config: { damping: 18, stiffness: 90 } });
  const zero = spring({ frame: frame - fps * 6.6, fps, config: { damping: 12, stiffness: 160 } });
  const carry = interpolate(frame, [fps * 9.2, fps * 10.0], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const barW = 520;
  return (
    <AbsoluteFill>
      <Grid />
      <AbsoluteFill style={{ padding: "110px 140px" }}>
        <Eyebrow>One trade, two venues</Eyebrow>
        <div style={{ marginTop: 40, fontFamily: SANS, fontWeight: 800, fontSize: 96, lineHeight: 1.05, color: TEXT.hi }}>
          <div style={{ opacity: s1, transform: `translateX(${(1 - s1) * -120}px)` }}>
            <span style={{ color: SERIES.dex }}>Long</span> BNB on PancakeSwap V3.
          </div>
          <div style={{ opacity: s2, transform: `translateX(${(1 - s2) * 120}px)` }}>
            <span style={{ color: SERIES.perp }}>Short</span> the Binance perp.
          </div>
        </div>

        {/* the two legs as bars that meet in the middle */}
        <div style={{ position: "relative", height: 120, marginTop: 70 }}>
          <div style={{ position: "absolute", left: 0, top: 40, width: barW * s1, height: 34, borderRadius: 8, background: SERIES.dex, transform: `translateX(${meet * 260}px)` }} />
          <div style={{ position: "absolute", right: 0, top: 40, width: barW * s2, height: 34, borderRadius: 8, background: SERIES.perp, transform: `translateX(${-meet * 260}px)` }} />
          <div style={{ position: "absolute", left: "50%", top: 0, transform: `translateX(-50%) scale(${zero})`, opacity: zero, fontFamily: MONO, fontSize: 64, fontWeight: 700, color: STATUS.good, background: INK[900], border: `2px solid ${STATUS.good}66`, borderRadius: 16, padding: "10px 34px" }}>
            Δ = 0.00 BNB
          </div>
        </div>

        <div style={{ marginTop: 80, opacity: carry, transform: `translateY(${(1 - carry) * 18}px)`, fontFamily: SANS, fontSize: 40, color: TEXT.mid }}>
          What is left: <span style={{ color: TEXT.hi, fontWeight: 700 }}>basis</span> between the venues
          <span style={{ color: TEXT.lo }}> + </span>
          <span style={{ color: SERIES.funding, fontWeight: 700 }}>funding</span> the short leg collects.
        </div>
      </AbsoluteFill>
      <Sfx id="whoosh" at={Math.round(fps * 0.3)} volume={0.45} />
      <Sfx id="whoosh" at={Math.round(fps * 2.4)} volume={0.45} />
      <Sfx id="ding" at={Math.round(fps * 6.6)} volume={0.5} />
    </AbsoluteFill>
  );
};
