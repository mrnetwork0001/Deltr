import React from "react";
import { AbsoluteFill, OffthreadVideo, interpolate, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { INK } from "../theme";
import type { Beat } from "../timeline";
import { manifest } from "../timeline";
import { Caption, Grid, Sfx } from "../ui";

/** A recorded clip in a framed viewport, with the sounds the capture logged at their real times. */
export const Footage: React.FC<{ beat: Beat; frames: number }> = ({ beat, frames }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const clip = manifest.footage[beat.clip!];
  const from = beat.from ?? 0;
  const play = Math.min(beat.play ?? clip.seconds, clip.seconds - from);
  const seconds = frames / fps;
  // shorter footage than narration: slow it a little (never below 0.75x); longer: cut it
  const rate = play < seconds ? Math.max(0.75, play / seconds) : 1;
  const enter = interpolate(frame, [0, 14], [0.965, 1], { extrapolateRight: "clamp" });
  const toFrame = (t: number) => Math.round(((t - from) / rate) * fps);
  const inWindow = (t: number) => t >= from && t <= from + play;
  const events = clip.events.filter((e) => inWindow(e.t));
  return (
    <AbsoluteFill>
      <Grid glow={false} />
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center" }}>
        <div style={{ width: 1800, height: 1012, borderRadius: 18, overflow: "hidden", border: `1px solid ${INK[700]}`, boxShadow: "0 40px 120px rgba(0,0,0,0.6)", transform: `scale(${enter})`, background: INK[950] }}>
          <OffthreadVideo src={staticFile(clip.file)} startFrom={Math.round(from * fps)} endAt={Math.round((from + play) * fps) + 1} playbackRate={rate} muted style={{ width: 1800, height: 1012, objectFit: "cover" }} />
        </div>
      </AbsoluteFill>
      {beat.caption ? <Caption text={beat.caption} total={frames} /> : null}
      {events.map((e, i) => {
        const at = toFrame(e.t);
        if (e.kind === "key") return <Sfx key={i} id="key" at={at} volume={0.55} />;
        if (e.kind === "click") return <Sfx key={i} id="click" at={at} volume={0.7} />;
        if (e.kind === "result") return <Sfx key={i} id="ding" at={at} volume={0.45} />;
        if (e.kind === "veto") return <Sfx key={i} id="veto" at={at} volume={0.6} />;
        if (e.kind === "scroll" && e.note) return <Sfx key={i} id="whoosh" at={at} volume={0.18} />;
        return null;
      })}
    </AbsoluteFill>
  );
};
