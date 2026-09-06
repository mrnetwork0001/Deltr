"use client";
// Hero: two columns on a faint grid. Left: the strategy in one breath, the prompt a
// user actually says, two outlined actions and a mono caption. Right: a continuously
// animating screen (HeroScene). Below: a four-tile stats strip with honest numbers.
import Link from "next/link";
import { DASHBOARD_PATH } from "@/lib/landing";
import HeroScene from "./HeroScene";

// Every number here is measured or counted in the repo, not aspirational.
const STATS: { value: string; label: string }[] = [
  { value: "19", label: "risk checks" },
  { value: "670", label: "tests passing" },
  { value: "22", label: "MCP tools" },
  { value: "500", label: "days of funding data" },
];

export default function Hero() {
  return (
    <header id="top" className="relative overflow-hidden hero-grid">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(55% 55% at 15% 0%, rgba(240,185,11,0.10), transparent 60%), radial-gradient(45% 45% at 92% 8%, rgba(31,199,212,0.10), transparent 60%)",
        }}
      />

      <div className="relative mx-auto max-w-[calc(50vw+36rem)] px-4 pb-14 pt-14 sm:px-6 sm:pb-20 sm:pt-20">
        <div className="grid items-center gap-12 lg:grid-cols-[1.2fr_1fr] lg:gap-10">
          {/* left column */}
          <div>
            <h1 className="hero-in text-4xl font-bold leading-[1.06] tracking-tight text-gray-50 sm:text-[2.6rem] xl:text-5xl 2xl:text-[3.4rem]">
              Long the DEX. Short the perp.
              <br />
              <span className="bg-gradient-to-r from-bnb to-cake bg-clip-text text-transparent">
                Delta-neutral by construction.
              </span>
            </h1>

            <p className="hero-in mt-6 max-w-xl text-lg leading-relaxed text-gray-400">
              Deltr is a CEX / DEX basis and funding agent on Binance Agent OS. Say{" "}
              <strong className="font-semibold text-gray-100">“rebalance $5,000 into a delta-neutral BNB hedge”</strong>, and it prices the
              full round trip, sizes both legs to the lot step, and clears a deterministic 19-check risk gate before anything reaches Binance.
            </p>

            <div className="hero-in mt-8 flex flex-col gap-3 sm:flex-row sm:items-center">
              <Link
                href={DASHBOARD_PATH}
                className="inline-flex items-center justify-center gap-2 rounded-lg bg-bnb px-6 py-3.5 text-base font-bold text-ink-950 shadow-[0_0_0_1px_rgba(240,185,11,0.5),0_8px_30px_rgba(240,185,11,0.18)] transition hover:bg-bnb-dim"
              >
                Launch App <span aria-hidden>→</span>
              </Link>
              <a
                href="#safety"
                className="inline-flex items-center justify-center rounded-lg border border-ink-700 bg-ink-900 px-5 py-3.5 text-base font-semibold text-gray-200 transition hover:border-gray-500"
              >
                Read the safety model
              </a>
            </div>

            <p className="hero-in mt-5 font-mono text-[11px] uppercase tracking-[0.2em] text-gray-400">
              Runs in this browser · paper mode · real mainnet data · no keys needed
            </p>
          </div>

          {/* right column */}
          <div className="hero-in min-w-0">
            <HeroScene />
          </div>
        </div>
      </div>

      {/* stats strip */}
      <div className="relative border-y border-ink-700/80 bg-ink-950/60">
        <ul className="mx-auto grid max-w-[calc(50vw+36rem)] grid-cols-2 divide-ink-700/80 sm:grid-cols-4 sm:divide-x">
          {STATS.map((s) => (
            <li key={s.label} className="px-4 py-6 text-center">
              <p className="font-mono text-2xl font-bold tabular-nums text-gray-50 sm:text-3xl">{s.value}</p>
              <p className="mt-1.5 font-mono text-[10px] uppercase tracking-[0.22em] text-bnb/90">{s.label}</p>
            </li>
          ))}
        </ul>
      </div>
    </header>
  );
}
