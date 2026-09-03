// Section 8: the 3:00 storyboard as a timeline and the seven sticky-note prompts.
import { DEMO_BEATS, DEMO_PROMPTS } from "@/lib/landing";
import { Card, Section } from "./Section";

export default function DemoTimeline() {
  return (
    <Section
      id="demo"
      eyebrow="Demo"
      title="Three minutes, nothing faked"
      lead="Prices are live, fills are labelled paper or carry a testnet order id, stress is labelled SIMULATED, and any min-edge override is on screen. Claude on the left, the dashboard on the right, a terminal overlay for two beats."
    >
      <div className="grid gap-6 lg:grid-cols-5">
        <ol className="relative min-w-0 space-y-6 border-l border-ink-700 pl-6 lg:col-span-3">
          {DEMO_BEATS.map((b) => (
            <li key={b.time} className="relative">
              <span className="absolute -left-[31px] top-1 flex h-3 w-3 items-center justify-center rounded-full border-2 border-bnb bg-ink-950" aria-hidden />
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-mono text-xs tabular-nums text-bnb">{b.time}</span>
                <h3 className="text-base font-semibold text-gray-100">{b.title}</h3>
              </div>
              <p className="mt-1 text-sm text-gray-300">{b.onScreen}</p>
              <p className="mt-1 text-sm italic text-gray-500">&ldquo;{b.voice}&rdquo;</p>
            </li>
          ))}
        </ol>

        <div className="min-w-0 lg:col-span-2">
          <Card className="bg-ink-900">
            <h3 className="text-base font-semibold text-gray-100">The sticky note</h3>
            <p className="mt-1 text-sm text-gray-400">Prompts, verbatim, in order.</p>
            <ol className="mt-3 space-y-2">
              {DEMO_PROMPTS.map((p, i) => (
                <li
                  key={p}
                  className="flex gap-3 rounded-md border border-bnb/25 bg-bnb/5 px-3 py-2 text-[15px] text-gray-100"
                  style={{ borderLeftWidth: 3, borderLeftColor: "#F0B90B" }}
                >
                  <span className="font-mono text-xs tabular-nums text-gray-500">{i + 1}.</span>
                  <span>{p}</span>
                </li>
              ))}
            </ol>
            <ul className="mt-4 space-y-1.5 text-sm text-gray-400">
              <li>Say &ldquo;paper&rdquo; whenever a fill is simulated; say the order id when it is a testnet fill.</li>
              <li>Say &ldquo;simulated&rdquo; whenever the red chip is on; say that the feed was untouched.</li>
              <li>Quote the measured gate latency shown on the status bar, never a target.</li>
              <li>Funding numbers are testnet-derived and indicative; do not call them yield.</li>
            </ul>
          </Card>
        </div>
      </div>
    </Section>
  );
}
