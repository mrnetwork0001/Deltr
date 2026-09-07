// The cut: one entry per narrated beat. Durations come from public/manifest.json (real audio and
// footage lengths); a beat lasts as long as its narration plus a tail, and footage beats never end
// before the footage they need.
import manifest from "../public/manifest.json";

export const FPS = 30;
export const W = 1920;
export const H = 1080;
export const TRANSITION_S = 0.5;

type Kind = "intro" | "hero" | "edge" | "footage" | "close";
export interface Beat {
  id: string;
  kind: Kind;
  narration: keyof typeof manifest.narration;
  seconds: number;
  /** footage beats: which clip, where to start, how long to play (seconds) */
  clip?: keyof typeof manifest.footage;
  from?: number;
  play?: number;
  caption?: string;
}

const N = manifest.narration;
const F = manifest.footage;
const ev = (clip: keyof typeof F, kind: string, note?: string) =>
  F[clip].events.find((e) => e.kind === kind && (note === undefined || e.note === note))?.t ?? 0;
const keys = (clip: keyof typeof F) => F[clip].events.filter((e) => e.kind === "key").map((e) => e.t);

// landing: play from 4 s before the Launch App click to the end of the clip (the public dashboard)
const launchClick = ev("landing", "click", "launch");
const landingFrom = Math.max(0, launchClick - 4.5);
const landingPlay = F.landing.seconds - landingFrom;
// paper: prompt beat = clip start -> after the trace is shown; veto beat = second typing -> end
const paperKeys = keys("paper");
const secondTyping = paperKeys.find((t) => t > ev("paper", "result", "filled")) ?? 0;
const promptPlay = Math.max(secondTyping - 0.6, 1);
const vetoFrom = Math.max(0, secondTyping - 0.8);
const vetoPlay = F.paper.seconds - vetoFrom;

const tail = 0.9;
export const BEATS: Beat[] = [
  { id: "intro", kind: "intro", narration: "intro", seconds: Math.max(N.intro.seconds + 1.4, 4.5) },
  { id: "hero", kind: "hero", narration: "hero", seconds: N.hero.seconds + tail },
  { id: "edge", kind: "edge", narration: "edge", seconds: N.edge.seconds + tail },
  { id: "launch", kind: "footage", narration: "launch", clip: "landing", from: landingFrom, play: landingPlay,
    seconds: Math.max(N.launch.seconds + N.paper.seconds + tail, 8), caption: "usedeltrapp.vercel.app · real mainnet data" },
  { id: "prompt", kind: "footage", narration: "prompt", clip: "paper", from: 0, play: promptPlay,
    seconds: Math.max(N.prompt.seconds + tail, promptPlay), caption: "Paper mode · prompt → plan → 19-check gate → execute" },
  { id: "veto", kind: "footage", narration: "veto", clip: "paper", from: vetoFrom, play: vetoPlay,
    seconds: Math.max(N.veto.seconds + tail, vetoPlay), caption: "Every veto carries the observed value, the limit and the unit" },
  { id: "live", kind: "footage", narration: "live", clip: "live", from: 0, play: F.live.seconds,
    seconds: Math.max(N.live.seconds + tail, F.live.seconds), caption: "LIVE · real funds · Agentic Wallet custody · delta 0" },
  { id: "close", kind: "close", narration: "close", seconds: N.close.seconds + 2.2 },
];

export const totalFrames = () =>
  Math.round((BEATS.reduce((s, b) => s + b.seconds, 0) - TRANSITION_S * (BEATS.length - 1)) * FPS);
export const beatStartFrame = (i: number) =>
  Math.round((BEATS.slice(0, i).reduce((s, b) => s + b.seconds, 0) - TRANSITION_S * i) * FPS);
export { manifest, keys as clipKeys, ev as clipEvent };
