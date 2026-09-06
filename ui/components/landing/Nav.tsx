// Sticky top nav: brand and section anchors. The mobile menu is
// a small client component (MobileMenu) so it can close itself on link click.
import MobileMenu from "./MobileMenu";
import { NAV_LINKS } from "@/lib/landing";

export default function Nav() {
  return (
    <nav className="sticky top-0 z-40 border-b border-ink-700/80 bg-ink-950/90 backdrop-blur" aria-label="Sections">
      <div className="mx-auto flex h-14 max-w-[calc(50vw+36rem)] items-center gap-3 px-4 sm:px-6">
        <a href="#top" className="flex items-center gap-2 text-base font-bold tracking-tight text-bnb">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-bnb" aria-hidden />
          Deltr
        </a>
        <ul className="ml-auto hidden items-center gap-1 lg:flex">
          {NAV_LINKS.map((l) => (
            <li key={l.id}>
              <a href={`#${l.id}`} className="rounded px-2 py-1 text-sm text-gray-400 transition hover:bg-ink-800 hover:text-gray-100">
                {l.label}
              </a>
            </li>
          ))}
        </ul>
        <div className="flex items-center gap-2 lg:ml-2">
          <MobileMenu />
        </div>
      </div>
    </nav>
  );
}
