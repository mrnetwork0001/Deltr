import { Composition } from "remotion";
import { Demo } from "./Demo";
import { FPS, H, W, totalFrames } from "./timeline";

export const Root = () => (
  <Composition id="Demo" component={Demo} durationInFrames={totalFrames()} fps={FPS} width={W} height={H} />
);
