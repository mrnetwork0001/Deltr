// Landing-page primitives: section shell, eyebrow + heading, code block, chips.
import type { CSSProperties, ReactNode } from "react";
import Reveal from "./Reveal";

// Stagger index for children of a `.stagger` list inside a Reveal (globals.css).
export const nth = (n: number): CSSProperties => ({ ["--n" as string]: n }) as CSSProperties;

export function Section({
  id,
  eyebrow,
  title,
  lead,
  children,
  className = "",
}: {
  id: string;
  eyebrow: string;
  title: string;
  lead?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section id={id} className={`scroll-mt-20 border-t border-ink-700/70 py-14 sm:py-20 ${className}`}>
      <div className="mx-auto w-full max-w-[calc(50vw+36rem)] px-4 sm:px-6">
        <Reveal>
          <p className="font-mono text-xs uppercase tracking-[0.18em] text-bnb">{eyebrow}</p>
          <h2 className="mt-2 text-2xl font-bold tracking-tight text-gray-100 sm:text-3xl">{title}</h2>
          {lead ? <div className="mt-3 max-w-3xl text-[15px] leading-relaxed text-gray-400">{lead}</div> : null}
        </Reveal>
        <div className="mt-8">{children}</div>
      </div>
    </section>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={`min-w-0 rounded-lg border border-ink-700 bg-ink-900 p-5 transition-[border-color,box-shadow,transform] duration-300 hover:-translate-y-0.5 hover:border-gray-600 hover:shadow-[0_14px_40px_rgba(0,0,0,0.35)] ${className}`}
    >
      {children}
    </div>
  );
}

export function Code({ children, className = "" }: { children: string; className?: string }) {
  return (
    <pre className={`overflow-x-auto rounded-md border border-ink-700 bg-ink-950 p-3 font-mono text-[13px] leading-relaxed text-gray-200 ${className}`}>
      <code>{children}</code>
    </pre>
  );
}

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <code className={`rounded bg-ink-800 px-1 py-0.5 font-mono text-[0.92em] text-gray-200 ${className}`}>{children}</code>;
}

export function Tag({ children, color = "#9ca3af", className = "" }: { children: ReactNode; color?: string; className?: string }) {
  return (
    <span
      style={{ color, borderColor: color + "66", background: color + "14" }}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-2 py-0.5 font-mono text-xs font-semibold leading-5 ${className}`}
    >
      {children}
    </span>
  );
}

export const BRAND = { bnb: "#F0B90B", cake: "#1FC7D4" };
export const SERIES = { blue: "#3987e5", orange: "#d95926", green: "#199e70", gold: "#c98500" };
export const STATUS = { good: "#0ca30c", warning: "#fab219", serious: "#ec835a", critical: "#d03b3b" };
