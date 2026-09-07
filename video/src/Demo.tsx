// The cut. Each beat is a scene with its narration; beats cross-fade with a whoosh; a soft pad runs
// underneath at low volume. Every timing comes from timeline.ts, which derives it from the manifest.
import React from "react";
import { AbsoluteFill, Audio, Loop, Sequence, staticFile, useVideoConfig } from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { BEATS, FPS, TRANSITION_S, beatStartFrame, manifest } from "./timeline";
import { Narration, Sfx } from "./ui";
import { Intro } from "./scenes/Intro";
import { Hero } from "./scenes/Hero";
import { Edge } from "./scenes/Edge";
import { Footage } from "./scenes/Footage";
import { Close } from "./scenes/Close";

const Scene: React.FC<{ i: number }> = ({ i }) => {
  const b = BEATS[i];
  const frames = Math.round(b.seconds * FPS);
  switch (b.kind) {
    case "intro": return <Intro frames={frames} />;
    case "hero": return <Hero frames={frames} />;
    case "edge": return <Edge frames={frames} />;
    case "footage": return <Footage beat={b} frames={frames} />;
    case "close": return <Close frames={frames} />;
  }
};

export const Demo: React.FC = () => {
  const { fps, durationInFrames } = useVideoConfig();
  const pad = manifest.sfx.pad;
  const padFrames = Math.max(1, Math.round(pad.seconds * fps));
  return (
    <AbsoluteFill style={{ background: "#07090f" }}>
      <TransitionSeries>
        {BEATS.map((b, i) => (
          <React.Fragment key={b.id}>
            {i > 0 ? (
              <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: Math.round(TRANSITION_S * fps) })} />
            ) : null}
            <TransitionSeries.Sequence durationInFrames={Math.round(b.seconds * fps)}>
              <Scene i={i} />
            </TransitionSeries.Sequence>
          </React.Fragment>
        ))}
      </TransitionSeries>

      {/* narration, placed on the global timeline so cross-fades never clip a sentence */}
      {BEATS.map((b, i) => (
        <Narration key={b.id} id={b.narration} at={beatStartFrame(i) + Math.round(fps * 0.35)} />
      ))}
      {/* the launch beat carries two sentences: "press Launch App" then the paper-mode line */}
      <Narration id="paper" at={beatStartFrame(3) + Math.round(fps * 0.35) + Math.ceil(manifest.narration.launch.seconds * fps) + Math.round(fps * 0.5)} />
      {/* a whoosh on every cut */}
      {BEATS.slice(1).map((b, i) => (
        <Sfx key={b.id} id="whoosh" at={beatStartFrame(i + 1) - Math.round(TRANSITION_S * fps * 0.5)} volume={0.35} />
      ))}
      {/* music bed */}
      <Sequence from={0} durationInFrames={durationInFrames} layout="none">
        <Loop durationInFrames={padFrames} times={Math.ceil(durationInFrames / padFrames)} layout="none">
          <Audio src={staticFile(pad.file)} volume={0.11} />
        </Loop>
      </Sequence>
    </AbsoluteFill>
  );
};
