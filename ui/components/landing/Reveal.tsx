"use client";
// Scroll-triggered entrance for landing sections. The element starts faded and
// slightly lowered (.reveal in globals.css) and gains `in` the first time it
// enters the viewport. `i` staggers siblings by 70 ms steps. Reduced-motion users
// and browsers without IntersectionObserver see everything immediately.
import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";

type Tag = "div" | "li" | "ul" | "p" | "section" | "header" | "figure";

export default function Reveal({
  as = "div",
  i = 0,
  className = "",
  style,
  children,
}: {
  as?: Tag;
  i?: number;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      el.classList.add("in");
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          el.classList.add("in");
          io.disconnect();
        }
      },
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  const El = as as "div";
  return (
    <El ref={ref} className={`reveal ${className}`} style={{ ...style, transitionDelay: `${Math.min(i, 8) * 70}ms` }}>
      {children}
    </El>
  );
}
