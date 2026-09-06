"use client";
// Section 2: the four subsystems as a switcher. Top: numbered tabs that auto-advance
// (a progress bar runs under the active one; it pauses while you hover and stops for
// good once you pick a tab yourself). Panel: the subsystem's summary and bullets on
// the left, one representative call rendered as a trace on the right. Below: the
// life of one hedge as a four-step strip with a connector that draws itself in.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Blocks, Radar, Scale, ShieldCheck, type LucideIcon } from "lucide-react";
import { LIFECYCLE, SUBSYSTEMS, SUBSYSTEM_TRACES, type TraceTone } from "@/lib/landing";
import Reveal from "./Reveal";
import { Mono, Section, STATUS, nth } from "./Section";

const ICONS: Record<string, LucideIcon> = {
  "agents/arbitrage_scout.py": Radar,
  "agents/hedger.py": Scale,
  "risk_gate.py": ShieldCheck,
  "agents/agent_os_bridge.ts": Blocks,
};
const DWELL_MS = 7000;
const TONE: Record<TraceTone, { className?: string; color?: string }> = {
  cmd: { className: "text-gray-100" },
  ok: { color: STATUS.good },
  veto: { color: STATUS.critical },
  gold: { className: "text-bnb" },
  dim: { className: "text-gray-500" },
  text: { className: "text-gray-300" },
};

export default function HowItWorks() {
  const [active, setActive] = useState(0);
  const [auto, setAuto] = useState(true); // false once the reader picks a tab
  const [hover, setHover] = useState(false);
  const [visible, setVisible] = useState(false);
  const [cycle, setCycle] = useState(0); // bumping it restarts the progress bar
  const root = useRef<HTMLDivElement | null>(null);
  const reduced = useRef(false);
  const paused = hover || !visible;

  useEffect(() => {
    reduced.current = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const el = root.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver((es) => setVisible(es.some((e) => e.isIntersecting)), { threshold: 0.25 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!auto || paused || reduced.current) return;
    const t = setTimeout(() => setActive((a) => (a + 1) % SUBSYSTEMS.length), DWELL_MS);
    return () => clearTimeout(t);
  }, [auto, paused, active, cycle]);

  function pick(k: number) {
    setActive(k);
    setAuto(false);
  }
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "ArrowRight") pick((active + 1) % SUBSYSTEMS.length);
    if (e.key === "ArrowLeft") pick((active - 1 + SUBSYSTEMS.length) % SUBSYSTEMS.length);
  }
  function leave() {
    setHover(false);
    setCycle((c) => c + 1); // restart the bar in step with the restarted timer
  }

  const s = SUBSYSTEMS[active];
  const trace = SUBSYSTEM_TRACES[s.path];

  return (
    <Section
      id="how"
      eyebrow="How it works"
      title="Four subsystems, one engine, one gate"
      lead={
        <>
          Everything an agent can do goes through the same <Mono>Engine</Mono> facade: scan, explain, propose, evaluate, execute, unwind.
          Every order goes through the same gate. Two transports (MCP and REST) share one process, so what your agent does is what the
          dashboard shows.
        </>
      }
    >
      <Reveal>
        <div ref={root} onMouseEnter={() => setHover(true)} onMouseLeave={leave} onFocus={() => setHover(true)} onBlur={leave}>
          {/* tabs */}
          <div role="tablist" aria-label="Subsystem" onKeyDown={onKey} className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {SUBSYSTEMS.map((sub, k) => {
              const Icon = ICONS[sub.path] ?? Blocks;
              const on = k === active;
              return (
                <button
                  key={sub.path}
                  id={`how-tab-${k}`}
                  role="tab"
                  aria-selected={on}
                  aria-controls="how-panel"
                  tabIndex={on ? 0 : -1}
                  onClick={() => pick(k)}
                  className={`group relative overflow-hidden rounded-lg border p-4 text-left transition-colors duration-300 ${
                    on ? "border-bnb/60 bg-bnb/[0.06]" : "border-ink-700 bg-ink-900 hover:border-gray-600"
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <span
                      className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-md border transition-colors duration-300 ${
                        on ? "border-bnb/50 bg-bnb/10 text-bnb" : "border-ink-700 bg-ink-800 text-gray-400 group-hover:text-gray-200"
                      }`}
                    >
                      <Icon size={18} />
                    </span>
                    <span className="min-w-0">
                      <span className="block font-mono text-[10px] uppercase tracking-[0.22em] text-gray-500">0{k + 1}</span>
                      <span className={`block truncate text-[15px] font-semibold ${on ? "text-gray-50" : "text-gray-300"}`}>{sub.name}</span>
                    </span>
                  </div>
                  <span aria-hidden className="absolute inset-x-0 bottom-0 h-0.5 bg-ink-700/60">
                    {on && auto ? (
                      <span key={`${active}-${cycle}`} className={`how-progress block h-full bg-bnb ${paused ? "paused" : ""}`} />
                    ) : on ? (
                      <span className="block h-full bg-bnb" />
                    ) : null}
                  </span>
                </button>
              );
            })}
          </div>

          {/* panel */}
          <div
            role="tabpanel"
            id="how-panel"
            aria-labelledby={`how-tab-${active}`}
            className="mt-3 grid overflow-hidden rounded-lg border border-ink-700 bg-ink-900 lg:grid-cols-[1fr_1.15fr]"
          >
            <div key={`d-${active}`} className="how-panel p-5 sm:p-6">
              <p className="font-mono text-xs text-gray-500">{s.path}</p>
              <h3 className="mt-2 text-xl font-semibold text-gray-50">{s.name}</h3>
              <p className="mt-2 text-[15px] leading-relaxed text-gray-300">{s.summary}</p>
              <ul className="mt-4 space-y-2.5">
                {s.bullets.map((b, j) => (
                  <li key={b} className="how-line flex gap-3 text-sm leading-relaxed text-gray-400" style={{ animationDelay: `${120 + j * 90}ms` }}>
                    <span aria-hidden className="mt-[8px] h-1.5 w-1.5 shrink-0 rounded-full bg-bnb/70" />
                    <span>{b}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div key={`t-${active}`} className="min-w-0 border-t border-ink-700 bg-ink-950 lg:border-l lg:border-t-0">
              <div className="flex items-center gap-2 border-b border-ink-700/80 px-4 py-2.5 font-mono text-[11px] uppercase tracking-[0.18em] text-gray-500">
                <span className="flex gap-1.5" aria-hidden>
                  <i className="h-2 w-2 rounded-full bg-ink-700" />
                  <i className="h-2 w-2 rounded-full bg-ink-700" />
                  <i className="h-2 w-2 rounded-full bg-ink-700" />
                </span>
                <span className="ml-1">trace · {trace.title}</span>
                <span className="ml-auto hidden text-gray-600 sm:inline">paper · mainnet data</span>
              </div>
              <div className="overflow-x-auto p-4 font-mono text-[12.5px] leading-6 sm:p-5">
                {trace.lines.map((l, j) => {
                  const tone = TONE[l.tone ?? "text"];
                  return (
                    <div key={j} className={`how-line whitespace-pre ${tone.className ?? ""}`} style={{ animationDelay: `${150 + j * 170}ms`, color: tone.color }}>
                      {l.tone === "cmd" ? <span className="text-cake">$ </span> : null}
                      {l.text}
                    </div>
                  );
                })}
                <span aria-hidden className="how-cursor inline-block h-3.5 w-[7px] translate-y-[3px] bg-gray-500" style={{ animationDelay: `${150 + trace.lines.length * 170}ms` }} />
              </div>
            </div>
          </div>
        </div>
      </Reveal>

      {/* the life of one hedge */}
      <Reveal i={2} className="mt-10">
        <p className="font-mono text-xs uppercase tracking-[0.18em] text-gray-500">The life of one hedge</p>
        <div className="relative mt-5">
          <span aria-hidden className="draw-x absolute left-0 right-0 top-[22px] hidden h-px bg-gradient-to-r from-bnb/70 via-ink-700 to-cake/70 lg:block" />
          <ol className="stagger grid gap-6 sm:grid-cols-2 lg:grid-cols-4 lg:gap-4">
            {LIFECYCLE.map((st, j) => (
              <li key={st.step} className="relative" style={nth(j)}>
                <div className="inline-flex h-11 w-11 items-center justify-center rounded-full border border-bnb/50 bg-ink-950 font-mono text-sm font-bold text-bnb">
                  {st.step}
                </div>
                <h4 className="mt-3 text-base font-semibold text-gray-100">{st.title}</h4>
                <p className="mt-1.5 text-sm leading-relaxed text-gray-400">{st.body}</p>
              </li>
            ))}
          </ol>
        </div>
      </Reveal>
    </Section>
  );
}
