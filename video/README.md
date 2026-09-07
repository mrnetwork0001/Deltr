# Deltr demo video

A Remotion composition that mixes motion graphics with recorded footage of the live app, narrated
through ElevenLabs. Nothing in it is mocked: the footage is the public site and a local paper engine
on live mainnet data, the numbers in the graphics are the README's.

```bash
npm install
npm run capture     # records public/footage/*.webm with a scripted browser (needs the app up; see scripts/capture.js)
#  ffmpeg -i landing.webm ... landing.mp4   (h264; see the transcode line in the session notes)
npm run narrate     # ElevenLabs voice-over + sound effects, cached, quota-checked (key from the Syntura env file)
npm run manifest    # public/manifest.json: real durations + captured event times -> the cut derives from this
npm run stills      # one PNG per scene to eyeball
npm run render      # out/deltr-demo.mp4, 1920x1080, 30 fps
```

Scenes (src/scenes): Intro (mark draws, wordmark types), Hero (two legs meet at delta zero), Edge
(the round-trip waterfall from the probe), Footage x4 (landing -> Launch App, paper prompt/execute,
two vetoes, LIVE), Close (measured numbers). Keystrokes, clicks, verdicts and vetoes in the footage
carry sounds at the times the capture logged them. `src/timeline.ts` is the cut.
