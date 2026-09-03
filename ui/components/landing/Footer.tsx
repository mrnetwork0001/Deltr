// Section 11: license, disclaimer, links.
import Link from "next/link";
import { FOOTER_LINKS } from "@/lib/landing";

export default function Footer() {
  return (
    <footer className="border-t border-ink-700/70 py-10">
      <div className="mx-auto max-w-6xl px-4 sm:px-6">
        <div className="flex flex-col gap-6 md:flex-row md:items-start md:justify-between">
          <div className="max-w-xl">
            <p className="text-base font-bold text-bnb">Deltr</p>
            <p className="mt-1 text-sm text-gray-400">
              CEX ↔ DEX delta-neutral basis and funding arbitrage agent on Binance Agent OS, with its own MCP server and a deterministic risk gate.
            </p>
            <p className="mt-3 text-sm text-gray-400">Code Apache-2.0; the skill folder (skills/deltr-binance) MIT.</p>
            <p className="mt-2 text-sm text-gray-400">
              Deltr is a technical demonstration and does not provide financial, investment, legal or tax advice. Nothing it outputs is a
              recommendation to enter any position, and you are solely responsible for any decision you make with it. Funding figures are
              testnet-derived and indicative. Paper fills are simulated at live quoted prices; testnet fills are real testnet orders. There is no
              production trading mode.
            </p>
          </div>
          <nav aria-label="Footer">
            <ul className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-3">
              {FOOTER_LINKS.map((l) =>
                l.href.startsWith("/") ? (
                  <li key={l.label}>
                    <Link href={l.href} className="text-gray-300 hover:text-bnb">
                      {l.label}
                    </Link>
                  </li>
                ) : (
                  <li key={l.label}>
                    <a href={l.href} target="_blank" rel="noreferrer" className="text-gray-300 hover:text-bnb">
                      {l.label}
                    </a>
                  </li>
                ),
              )}
            </ul>
          </nav>
        </div>
        <p className="mt-8 font-mono text-sm text-gray-500">Delta-neutral by construction, deterministic by design.</p>
      </div>
    </footer>
  );
}
