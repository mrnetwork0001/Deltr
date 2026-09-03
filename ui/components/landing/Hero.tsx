"use client";
// Hero: the strategy first, then the honest claims, Launch App, and a live badge
// that pings GET /api/health (1.5 s timeout) so the page never lies about whether
// an engine is behind it. The risk gate is deliberately below the fold of this
// block: it is the credibility line, not the pitch.
import Link from "next/link";
import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { ArrowRight, Github, ShieldCheck } from "lucide-react";
import { API_BASE } from "@/lib/api";
import { DASHBOARD_PATH, HERO_CLAIMS, REPO_URL } from "@/lib/landing";
import { STATUS } from "./Section";

type Health = { state: "checking" } | { state: "live"; mode: string; version?: string } | { state: "offline" };

function useHealth(): Health {
  const [h, setH] = useState<Health>({ state: "checking" });
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    fetch(`${API_BASE}/api/health`, { cache: "no-store", signal: ctrl.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as { ok?: boolean; mode?: string; version?: string };
        if (!cancelled) setH(body && body.ok ? { state: "live", mode: String(body.mode ?? "paper"), version: body.version } : { state: "offline" });
      })
      .catch(() => {
        if (!cancelled) setH({ state: "offline" });
      })
      .finally(() => clearTimeout(timer));
    return () => {
      cancelled = true;
      clearTimeout(timer);
      ctrl.abort();
    };
  }, []);
  return h;
}

function LiveBadge() {
  const h = useHealth();
  const color = h.state === "live" ? STATUS.good : h.state === "offline" ? "#6b7280" : STATUS.warning;
  const label =
    h.state === "live"
      ? `engine: live · ${h.mode}${h.version ? ` · v${h.version}` : ""}`
      : h.state === "offline"
        ? "engine: offline · dashboard shows mock data"
        : "engine: checking";
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-xs"
      style={{ color, borderColor: color + "66", background: color + "14" }}
      title="GET /api/health with a 1.5 s timeout"
    >
      <span className="relative inline-flex h-2 w-2">
        {h.state === "live" ? <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: color }} /> : null}
        <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: color }} />
      </span>
      {label}
    </span>
  );
}

const fade = (delay: number) => ({
  initial: { opacity: 0, y: 12 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.45, delay, ease: "easeOut" as const },
});

export default function Hero() {
  return (
    <header id="top" className="relative overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 50% at 20% 0%, rgba(240,185,11,0.10), transparent 60%), radial-gradient(50% 40% at 90% 10%, rgba(31,199,212,0.08), transparent 60%)",
        }}
      />
      <div className="relative mx-auto max-w-6xl px-4 pb-16 pt-14 sm:px-6 sm:pb-24 sm:pt-20">
        <motion.div {...fade(0)} className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-bnb/40 bg-bnb/10 px-3 py-1 font-mono text-xs text-bnb">Binance Agent OS Mini Hackathon</span>
          <span className="rounded-full border border-cake/40 bg-cake/10 px-3 py-1 font-mono text-xs text-cake">PancakeSwap V3 · BNB Chain</span>
          <LiveBadge />
        </motion.div>

        <motion.h1 {...fade(0.05)} className="mt-6 max-w-4xl text-4xl font-bold leading-[1.05] tracking-tight text-gray-50 sm:text-6xl">
          Long PancakeSwap V3. Short the Binance perp.
          <br />
          <span className="text-gray-400">Delta-neutral by construction.</span>
        </motion.h1>

        <motion.p {...fade(0.1)} className="mt-6 max-w-2xl text-lg leading-relaxed text-gray-300">
          Deltr is a CEX / DEX basis and funding arbitrage agent built on Binance Agent OS. It buys BNB on PancakeSwap V3 (BNB Chain
          mainnet, quoted on-chain) and sells the same quantity of the Binance USDⓈ-M perpetual, so the book carries no directional
          view. What is left is the basis between the two venues plus the funding the short leg collects or pays, priced over an
          explicit horizon and net of the entire round trip.
        </motion.p>

        <motion.p {...fade(0.12)} className="mt-4 max-w-2xl text-lg leading-relaxed text-gray-400">
          On live data today that arithmetic comes out around −14 bps, and the agent declines. The numbers are on screen either way.
        </motion.p>

        <motion.div {...fade(0.15)} className="mt-8 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
          <Link
            href={DASHBOARD_PATH}
            className="inline-flex items-center justify-center gap-2 rounded-lg bg-bnb px-6 py-3.5 text-base font-bold text-ink-950 shadow-[0_0_0_1px_rgba(240,185,11,0.5),0_8px_30px_rgba(240,185,11,0.18)] transition hover:bg-bnb-dim"
          >
            Launch App <ArrowRight size={18} />
          </Link>
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center justify-center gap-2 rounded-lg border border-ink-700 bg-ink-900 px-5 py-3.5 text-base font-semibold text-gray-200 transition hover:border-gray-500"
          >
            <Github size={18} /> GitHub
          </a>
          <a
            href="#safety"
            className="inline-flex items-center justify-center gap-2 rounded-lg border border-ink-700 bg-ink-900 px-5 py-3.5 text-base font-semibold text-gray-200 transition hover:border-gray-500"
          >
            <ShieldCheck size={18} /> Read the safety model
          </a>
        </motion.div>

        <motion.p {...fade(0.2)} className="mt-4 font-mono text-sm text-gray-400">
          PAPER mode by default: no secrets, real live prices, simulated fills. No production trading mode exists.
        </motion.p>

        <motion.p {...fade(0.22)} className="mt-3 max-w-3xl text-sm leading-relaxed text-gray-400">
          Before any order exists it clears a deterministic, zero-LLM Python gate: 19 ordered checks, stdlib only, 1.5 to 2.5 µs
          measured across runs, and the status bar prints the median measured on your machine. The gate owns equity, drawdown, the
          kill switch and the open-position registry, so a prompt cannot claim a balance or an empty book.
        </motion.p>

        <motion.ul {...fade(0.25)} className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {HERO_CLAIMS.map((c, i) => (
            <li key={c.title} className="rounded-lg border border-ink-700 bg-ink-900/80 p-5">
              <p className="font-mono text-xs text-gray-500">0{i + 1}</p>
              <h3 className="mt-1 text-base font-semibold text-gray-100">{c.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-gray-400">{c.body}</p>
            </li>
          ))}
        </motion.ul>
      </div>
    </header>
  );
}
