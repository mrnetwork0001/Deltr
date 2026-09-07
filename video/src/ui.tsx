// Small shared pieces: the grid background, the brand mark, eyebrow captions, sound cues.
import React from "react";
import { AbsoluteFill, Audio, Sequence, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { BNB, INK, MONO, SANS, TEXT } from "./theme";
import { manifest } from "./timeline";

export const Grid: React.FC<{ glow?: boolean }> = ({ glow = true }) => (
  <AbsoluteFill
    style={{
      background: INK[950],
      backgroundImage:
        "linear-gradient(to right, rgba(255,255,255,0.035) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,0.035) 1px, transparent 1px)",
      backgroundSize: "44px 44px",
    }}
  >
    {glow ? (
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(55% 55% at 15% 0%, rgba(240,185,11,0.12), transparent 60%), radial-gradient(45% 45% at 92% 8%, rgba(31,199,212,0.10), transparent 60%)",
        }}
      />
    ) : null}
  </AbsoluteFill>
);

/** The square mark: outer stroke draws itself, inner square springs in. */
export const Mark: React.FC<{ size?: number; delay?: number }> = ({ size = 160, delay = 0 }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const f = Math.max(0, frame - delay);
  const draw = interpolate(f, [0, fps * 0.9], [0, 1], { extrapolateRight: "clamp" });
  const inner = spring({ frame: f - fps * 0.5, fps, config: { damping: 12, stiffness: 140 } });
  const s = size;
  const sw = s * 0.11;
  const perim = 4 * (s - sw);
  return (
    <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`}>
      <rect x={sw / 2} y={sw / 2} width={s - sw} height={s - sw} fill="none" stroke={BNB} strokeWidth={sw} strokeDasharray={perim} strokeDashoffset={perim * (1 - draw)} />
      <rect x={s * 0.36} y={s * 0.36} width={s * 0.28} height={s * 0.28} fill={BNB} transform={`translate(${s / 2} ${s / 2}) scale(${inner}) translate(${-s / 2} ${-s / 2})`} />
    </svg>
  );
};

export const Eyebrow: React.FC<{ children: React.ReactNode; color?: string; style?: React.CSSProperties }> = ({ children, color = BNB, style }) => (
  <div style={{ fontFamily: MONO, fontSize: 22, letterSpacing: "0.22em", textTransform: "uppercase", color, ...style }}>{children}</div>
);

/** Lower-third caption that fades in after `delay` frames and out before the end. */
export const Caption: React.FC<{ text: string; delay?: number; total: number }> = ({ text, delay = 10, total }) => {
  const frame = useCurrentFrame();
  const o = interpolate(frame, [delay, delay + 12, total - 18, total - 4], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const y = interpolate(frame, [delay, delay + 12], [16, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <div style={{ position: "absolute", left: 72, bottom: 56, opacity: o, transform: `translateY(${y}px)` }}>
      <div style={{ display: "inline-flex", alignItems: "center", gap: 14, padding: "12px 18px", borderRadius: 10, background: "rgba(7,9,15,0.82)", border: `1px solid ${INK[700]}`, backdropFilter: "blur(6px)" }}>
        <span style={{ width: 10, height: 10, background: BNB, borderRadius: 2 }} />
        <span style={{ fontFamily: SANS, fontSize: 24, color: TEXT.hi, fontWeight: 600 }}>{text}</span>
      </div>
    </div>
  );
};

type SfxId = keyof typeof manifest.sfx;
/** One sound at one frame. */
export const Sfx: React.FC<{ id: SfxId; at: number; volume?: number }> = ({ id, at, volume = 0.5 }) => {
  const { fps } = useVideoConfig();
  const s = manifest.sfx[id];
  const frames = Math.max(2, Math.ceil(s.seconds * fps));
  if (at < 0) return null;
  return (
    <Sequence from={Math.round(at)} durationInFrames={frames} layout="none">
      <Audio src={staticFile(s.file)} volume={volume} />
    </Sequence>
  );
};

export const Narration: React.FC<{ id: keyof typeof manifest.narration; at?: number }> = ({ id, at = 0 }) => {
  const { fps } = useVideoConfig();
  const n = manifest.narration[id];
  return (
    <Sequence from={at} durationInFrames={Math.ceil(n.seconds * fps) + 2} layout="none">
      <Audio src={staticFile(n.file)} volume={1} />
    </Sequence>
  );
};
