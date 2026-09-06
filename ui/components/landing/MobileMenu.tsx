"use client";
// Mobile section menu for the sticky nav. A native <details> keeps it dependency
// free; the click handler closes it as soon as a section link is tapped so the
// panel never lingers over the content.
import { Menu } from "lucide-react";
import { useRef } from "react";
import { NAV_LINKS } from "@/lib/landing";

export default function MobileMenu() {
  const ref = useRef<HTMLDetailsElement>(null);
  const close = () => {
    if (ref.current) ref.current.open = false;
  };
  return (
    <details ref={ref} className="relative lg:hidden">
      <summary
        className="flex h-9 w-9 cursor-pointer list-none items-center justify-center rounded-md border border-ink-700 text-gray-300 [&::-webkit-details-marker]:hidden"
        aria-label="Open section menu"
      >
        <Menu size={18} />
      </summary>
      <ul className="absolute right-0 mt-2 w-52 rounded-md border border-ink-700 bg-ink-900 p-1 shadow-xl" onClick={close}>
        {NAV_LINKS.map((l) => (
          <li key={l.id}>
            <a href={`#${l.id}`} className="block rounded px-3 py-2 text-sm text-gray-300 hover:bg-ink-800 hover:text-gray-100">
              {l.label}
            </a>
          </li>
        ))}
        <li className="border-t border-ink-700 sm:hidden">
        </li>
      </ul>
    </details>
  );
}
