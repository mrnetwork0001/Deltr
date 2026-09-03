// Section 2: the four subsystems as cards with their file paths.
import { Blocks, Radar, Scale, ShieldCheck } from "lucide-react";
import { SUBSYSTEMS } from "@/lib/landing";
import { Card, Mono, Section } from "./Section";

const ICONS = [Radar, Scale, Blocks, ShieldCheck];

export default function HowItWorks() {
  return (
    <Section
      id="how"
      eyebrow="How it works"
      title="Four subsystems, one engine, one gate"
      lead={
        <>
          Everything an agent can do goes through the same <Mono>Engine</Mono> facade: scan, explain, propose, evaluate, execute, unwind.
          Every order goes through the same gate. Two transports (MCP and REST) share one process, so what your agent does is what the
          dashboard shows.
        </>
      }
    >
      <div className="grid min-w-0 gap-4 md:grid-cols-2">
        {SUBSYSTEMS.map((s, i) => {
          const Icon = ICONS[i] ?? Blocks;
          return (
            <Card key={s.name} className="flex flex-col">
              <div className="flex items-start gap-3">
                <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-ink-700 bg-ink-800 text-bnb">
                  <Icon size={18} />
                </span>
                <div className="min-w-0">
                  <h3 className="text-lg font-semibold text-gray-100">{s.name}</h3>
                  <p className="mt-0.5 break-all font-mono text-xs text-gray-500">{s.path}</p>
                </div>
              </div>
              <p className="mt-4 text-[15px] text-gray-300">{s.summary}</p>
              <ul className="mt-3 space-y-1.5 text-sm text-gray-400">
                {s.bullets.map((b) => (
                  <li key={b} className="flex gap-2">
                    <span className="mt-[9px] h-1 w-1 shrink-0 rounded-full bg-gray-500" aria-hidden />
                    <span>{b}</span>
                  </li>
                ))}
              </ul>
            </Card>
          );
        })}
      </div>

      <div className="mt-6 grid gap-3 text-sm text-gray-400 sm:grid-cols-3">
        <Card className="!p-4">
          <p className="font-mono text-xs uppercase tracking-wide text-gray-500">One tick</p>
          <p className="mt-1">
            Hub polls venues, Scout prices the edge, Portfolio marks positions to close and feeds equity into the gate.
          </p>
        </Card>
        <Card className="!p-4">
          <p className="font-mono text-xs uppercase tracking-wide text-gray-500">Two phases</p>
          <p className="mt-1">
            <Mono>propose</Mono> sizes and pre-checks; <Mono>execute(plan_id)</Mono> re-quotes, re-gates, then places DEX first and sizes the
            perp from the actual fill.
          </p>
        </Card>
        <Card className="!p-4">
          <p className="font-mono text-xs uppercase tracking-wide text-gray-500">Provenance</p>
          <p className="mt-1">
            Every price, rate, fill and history point carries a <Mono>DataSource</Mono> tag and an age. Receipts embed the ordered trace and a
            SHA-256.
          </p>
        </Card>
      </div>
    </Section>
  );
}
