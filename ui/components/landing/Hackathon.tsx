// Section 9: which Track A categories Deltr covers, plus the separate "connect your
// MCPs and trade" task reward (not a judged prize track).
import { HACKATHON_META, TRACK_A_CATEGORIES, TRACK_B } from "@/lib/landing";
import { Card, Section, Tag } from "./Section";

export default function Hackathon() {
  return (
    <Section
      id="hackathon"
      eyebrow="Hackathon"
      title={HACKATHON_META.name}
      lead={
        <>
          {HACKATHON_META.trackA}. {HACKATHON_META.trackB}. Submission deadline {HACKATHON_META.deadline}.
        </>
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <Tag color="#F0B90B">Track A</Tag>
          <h3 className="mt-2 text-lg font-semibold text-gray-100">Build an AI agent with Agent OS</h3>
          <p className="mt-1 text-sm text-gray-400">Judged categories Deltr covers:</p>
          <ul className="mt-3 space-y-3">
            {TRACK_A_CATEGORIES.map((c) => (
              <li key={c.name} className="rounded-md border border-ink-700 bg-ink-950 p-3">
                <p className="font-semibold text-gray-100">{c.name}</p>
                <p className="mt-1 text-sm text-gray-400">{c.how}</p>
              </li>
            ))}
          </ul>
        </Card>
        <Card>
          <Tag color="#1FC7D4">Task reward, not a judged track</Tag>
          <h3 className="mt-2 text-lg font-semibold text-gray-100">Connect your MCPs and trade</h3>
          <p className="mt-3 text-[15px] text-gray-300">{TRACK_B.body}</p>
          <ul className="mt-4 space-y-2 text-sm text-gray-400">
            <li>Deltr as an MCP server: 18 tools over streamable HTTP or stdio.</li>
            <li>Deltr as an MCP client: the bridge discovers the hosted Binance MCP endpoint and reports official, shim or none.</li>
            <li>Every order still passes the deterministic gate; PAPER by default, TESTNET with keys, no production mode.</li>
          </ul>
        </Card>
      </div>
    </Section>
  );
}
