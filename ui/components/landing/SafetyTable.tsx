// Section 5: the hard invariants, the 19 ordered checks (code, threshold,
// owner) and what Deltr never does.
import { Ban, Check } from "lucide-react";
import { GATE_CHECKS, HARD_INVARIANTS, NEVER_DOES } from "@/lib/landing";
import { Card, Mono, Section, STATUS, Tag } from "./Section";

const OWNER_COLOR: Record<string, string> = {
  gate: "#F0B90B",
  executor: "#1FC7D4",
  config: "#9ca3af",
  caller: "#9ca3af",
  scout: "#9ca3af",
};

function ownerColor(owner: string): string {
  const key = Object.keys(OWNER_COLOR).find((k) => owner.startsWith(k));
  return key ? OWNER_COLOR[key] : "#9ca3af";
}

export default function SafetyTable() {
  return (
    <Section
      id="safety"
      eyebrow="Safety by construction"
      title="19 ordered checks, every veto with a number"
      lead={
        <>
          Checks run cheapest and most decisive first and short-circuit on the first failure. Every veto carries the observed value, the
          limit and the unit, so the dashboard renders <Mono>leverage 10.0 &gt; 3.0 x</Mono> rather than a bare code. Two runs over the same
          replay fixture produce a byte-identical decision log: no model sits in the loop.
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {HARD_INVARIANTS.map((inv) => (
          <div key={inv.constant} className="rounded-lg border border-ink-700 bg-ink-900 p-4">
            <p className="text-xs text-gray-500">{inv.name}</p>
            <p className="mt-1 font-mono text-2xl font-semibold tabular-nums text-gray-100">{inv.value}</p>
            <p className="mt-1 font-mono text-xs text-gray-500">{inv.constant}</p>
          </div>
        ))}
      </div>
      <p className="mt-3 text-sm text-gray-400">
        <Mono>RiskLimits</Mono> clamps the three spec invariants so they can be tightened but never loosened. A drawdown of 2 % warns; 3 % halts
        and stays halted until the book recovers.
      </p>

      <div className="mt-8 overflow-x-auto rounded-lg border border-ink-700 bg-ink-900">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="bg-ink-800 text-xs uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-3 py-2 font-medium">#</th>
              <th className="px-3 py-2 font-medium">code</th>
              <th className="px-3 py-2 font-medium">threshold (default)</th>
              <th className="px-3 py-2 font-medium">input owner</th>
            </tr>
          </thead>
          <tbody>
            {GATE_CHECKS.map((c) => (
              <tr key={c.code} className="border-t border-ink-700 align-top">
                <td className="px-3 py-2 font-mono tabular-nums text-gray-500">{c.n}</td>
                <td className="px-3 py-2 font-mono text-gray-100">{c.code}</td>
                <td className="px-3 py-2 text-gray-300">{c.threshold}</td>
                <td className="px-3 py-2">
                  <Tag color={ownerColor(c.owner)}>{c.owner}</Tag>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-sm text-gray-400">
        Why check 10 has a floor: the proposer&apos;s own risk estimate is only a lower bound. The gate assumes the full round trip is paid and a
        100 bps adverse basis move, so an optimistic caller cannot talk its way past the 2 % rule. Only the Executor assembles a gate input;
        an AST test checks that no other module constructs one.
      </p>

      <h3 className="mt-10 flex items-center gap-2 text-lg font-semibold text-gray-100">
        <Ban size={18} style={{ color: STATUS.critical }} /> What Deltr never does
      </h3>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {NEVER_DOES.map((n) => (
          <Card key={n.never} className="!p-4">
            <p className="flex items-start gap-2 text-[15px] font-semibold text-gray-100">
              <Check size={16} className="mt-1 shrink-0" style={{ color: STATUS.good }} />
              {n.never}
            </p>
            <p className="mt-1.5 pl-6 text-sm text-gray-400">{n.how}</p>
          </Card>
        ))}
      </div>
    </Section>
  );
}
