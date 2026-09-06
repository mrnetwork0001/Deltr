// Footer: brand block on the left (mark, wordmark, mono tagline, blurb, GitHub / X),
// three mono link columns on the right.
import Link from "next/link";
import { Github, Twitter } from "lucide-react";
import { FOOTER_BLURB, FOOTER_COLUMNS, FOOTER_TAGLINE, REPO_URL, X_URL } from "@/lib/landing";

function FooterLink({ label, href }: { label: string; href: string }) {
  const cls = "font-mono text-[15px] text-gray-300 transition hover:text-bnb";
  if (href.startsWith("/")) return <Link href={href} className={cls}>{label}</Link>;
  if (href.startsWith("#")) return <a href={href} className={cls}>{label}</a>;
  return <a href={href} target="_blank" rel="noreferrer" className={cls}>{label}</a>;
}

export default function Footer() {
  return (
    <footer className="border-t border-ink-700/70 bg-ink-950 pb-8 pt-14">
      <div className="mx-auto max-w-[calc(50vw+36rem)] px-4 sm:px-6">
        <div className="grid gap-12 md:grid-cols-[1.4fr_1fr_1fr_1fr] md:gap-8">
          {/* brand block */}
          <div className="max-w-md">
            <a href="#top" className="inline-flex items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-md border border-bnb/50" aria-hidden>
                <span className="h-3.5 w-3.5 rounded-sm bg-bnb" />
              </span>
              <span>
                <span className="block font-mono text-lg font-bold uppercase tracking-[0.28em] text-gray-50">Deltr</span>
                <span className="block font-mono text-[10px] uppercase tracking-[0.3em] text-bnb/80">{FOOTER_TAGLINE}</span>
              </span>
            </a>
            <p className="mt-8 text-[15px] leading-relaxed text-gray-400">{FOOTER_BLURB}</p>
            <div className="mt-8 flex items-center gap-4">
              <a href={REPO_URL} target="_blank" rel="noreferrer" aria-label="GitHub" className="text-gray-400 transition hover:text-bnb">
                <Github size={20} />
              </a>
              <a href={X_URL} target="_blank" rel="noreferrer" aria-label="X" className="text-gray-400 transition hover:text-bnb">
                <Twitter size={20} />
              </a>
            </div>
          </div>

          {/* link columns */}
          {FOOTER_COLUMNS.map((col) => (
            <nav key={col.heading} aria-label={col.heading}>
              <p className="font-mono text-xs uppercase tracking-[0.28em] text-bnb">{col.heading}</p>
              <ul className="mt-6 space-y-4">
                {col.links.map((l) => (
                  <li key={l.label}>
                    <FooterLink {...l} />
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>

      </div>
    </footer>
  );
}
