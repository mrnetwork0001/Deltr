// Sticky top nav: brand, section anchors, compact Launch App. The mobile menu is
// a small client component (MobileMenu) so it can close itself on link click.
import Link from "next/link";
import { ArrowRight, Github } from "lucide-react";
import MobileMenu from "./MobileMenu";
import { DASHBOARD_PATH, NAV_LINKS, REPO_URL } from "@/lib/landing";

export default function Nav() {
  return (
    <nav className="sticky top-0 z-40 border-b border-ink-700/80 bg-ink-950/90 backdrop-blur" aria-label="Sections">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4 sm:px-6">
        <a href="#top" className="flex items-center gap-2 text-base font-bold tracking-tight text-bnb">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-bnb" aria-hidden />
          Deltr
        </a>
        <ul className="ml-4 hidden items-center gap-1 lg:flex">
          {NAV_LINKS.map((l) => (
            <li key={l.id}>
              <a href={`#${l.id}`} className="rounded px-2 py-1 text-sm text-gray-400 transition hover:bg-ink-800 hover:text-gray-100">
                {l.label}
              </a>
            </li>
          ))}
        </ul>
        <div className="ml-auto flex items-center gap-2">
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="hidden items-center gap-1.5 rounded-md border border-ink-700 px-2.5 py-1.5 text-sm text-gray-300 hover:border-gray-500 hover:text-gray-100 sm:inline-flex"
          >
            <Github size={15} /> GitHub
          </a>
          <Link
            href={DASHBOARD_PATH}
            className="inline-flex items-center gap-1.5 rounded-md bg-bnb px-3 py-1.5 text-sm font-semibold text-ink-950 hover:bg-bnb-dim"
          >
            Launch App <ArrowRight size={15} />
          </Link>
          <MobileMenu />
        </div>
      </div>
    </nav>
  );
}
