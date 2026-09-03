// Section 10: FAQ as native disclosure widgets.
import { ChevronDown } from "lucide-react";
import { FAQ } from "@/lib/landing";
import { Section } from "./Section";

export default function Faq() {
  return (
    <Section id="faq" eyebrow="FAQ" title="Questions judges ask">
      <div className="divide-y divide-ink-700 rounded-lg border border-ink-700 bg-ink-900">
        {FAQ.map((f) => (
          <details key={f.q} className="group px-5 py-4">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-[15px] font-semibold text-gray-100 [&::-webkit-details-marker]:hidden">
              {f.q}
              <ChevronDown size={18} className="shrink-0 text-gray-500 transition group-open:rotate-180" />
            </summary>
            <p className="mt-3 max-w-3xl text-sm leading-relaxed text-gray-400">{f.a}</p>
          </details>
        ))}
      </div>
    </Section>
  );
}
