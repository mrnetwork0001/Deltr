"use client";
// Section 6: client snippets as tabs plus the 18 tools as chips (summary) and a
// per-phase list of name + use that works without hover.
import { useState } from "react";
import { CLIENT_SNIPPETS, MCP_RESOURCES, MCP_URL, STDIO_SNIPPET, TOOLS } from "@/lib/landing";
import Reveal from "./Reveal";
import { Card, Code, Mono, Section, SERIES, Tag, nth } from "./Section";

const PHASE_COLOR: Record<NonNullable<(typeof TOOLS)[number]["phase"]>, string> = {
  read: "#9ca3af",
  propose: SERIES.blue,
  execute: SERIES.orange,
  operator: SERIES.gold,
};
const PHASES = ["read", "propose", "execute", "operator"] as const;

export default function ConnectPanel() {
  const [active, setActive] = useState(CLIENT_SNIPPETS[0].id);
  const snippet = CLIENT_SNIPPETS.find((s) => s.id === active) ?? CLIENT_SNIPPETS[0];

  return (
    <Section
      id="connect"
      eyebrow="Connect your agent"
      title="Deltr is an MCP server. Point any host at it."
      lead={
        <>
          Start it once with <Mono>python main.py</Mono>. The same process serves this page, the dashboard, the REST API and the MCP endpoint at{" "}
          <Mono>{MCP_URL}</Mono>, so what your agent does is what the dashboard shows.
        </>
      }
    >
      <Reveal className="stagger grid gap-4 lg:grid-cols-5">
        <div className="min-w-0 lg:col-span-3" style={nth(0)}>
        <Card>
          <div role="tablist" aria-label="MCP client" className="flex flex-wrap gap-1.5">
            {CLIENT_SNIPPETS.map((s) => {
              const on = s.id === snippet.id;
              return (
                <button
                  key={s.id}
                  role="tab"
                  aria-selected={on}
                  onClick={() => setActive(s.id)}
                  className={`rounded-md border px-3 py-1.5 text-sm font-medium transition ${
                    on ? "border-bnb bg-bnb/15 text-bnb" : "border-ink-700 text-gray-400 hover:border-gray-500 hover:text-gray-200"
                  }`}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
          <Code className="mt-4">{snippet.code}</Code>
          <p className="mt-2 text-sm text-gray-400">{snippet.hint}</p>
          <details className="mt-4 rounded-md border border-ink-700 bg-ink-950/60 p-3 text-sm text-gray-400">
            <summary className="cursor-pointer text-gray-300">stdio alternative (hosts that only speak stdio)</summary>
            <p className="mt-2">
              <Mono>python main.py --mcp</Mono> adds a stdio transport to the same process; it still serves the dashboard on :8000. Do not run a
              second engine next to it, two engines would mean two books.
            </p>
            <Code className="mt-2">{STDIO_SNIPPET}</Code>
          </details>
        </Card>
        </div>

        <div className="min-w-0 lg:col-span-2" style={nth(1)}>
        <Card>
          <h3 className="text-base font-semibold text-gray-100">The 18 tools</h3>
          <p className="mt-1 text-sm text-gray-400">Coloured by phase; each one is described below. A VETO is final for the same inputs; change capital or leverage instead of retrying.</p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {TOOLS.map((t) => (
              <Tag key={t.name} color={PHASE_COLOR[t.phase ?? "read"]}>
                <span title={t.use}>{t.name}</span>
              </Tag>
            ))}
          </div>
          <p className="mt-4 text-xs text-gray-500">
            Resources: {MCP_RESOURCES.map((r, i) => (
              <span key={r}>
                <Mono>{r}</Mono>
                {i < MCP_RESOURCES.length - 1 ? ", " : ""}
              </span>
            ))}
          </p>
          <ul className="mt-4 space-y-1.5 text-sm text-gray-400">
            <li>
              <Mono>deltr_propose_hedge</Mono> is phase 1: nothing executes, you get a <Mono>plan_id</Mono>.
            </li>
            <li>
              <Mono>deltr_execute_hedge</Mono> is phase 2: re-priced, re-gated, single-use, 60 s TTL.
            </li>
            <li>
              <Mono>deltr_prompt</Mono> turns free text into a plan and a pre-check. It never executes.
            </li>
          </ul>
        </Card>
        </div>
      </Reveal>
      <Reveal className="mt-6">
      <h3 className="text-base font-semibold text-gray-100">What each tool does</h3>
      <div className="stagger mt-2 grid gap-2 md:grid-cols-2">
        {PHASES.map((ph, i) => {
          const list = TOOLS.filter((t) => (t.phase ?? "read") === ph);
          return (
            <details key={ph} open className="rounded-md border border-ink-700 bg-ink-900" style={nth(i)}>
              <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-gray-200">
                <span className="inline-block h-2 w-2 rounded-sm" style={{ background: PHASE_COLOR[ph] }} aria-hidden />
                <span className="font-medium">{ph}</span>
                <span className="text-gray-500">{list.length} tools</span>
              </summary>
              <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 border-t border-ink-700 px-3 py-2 text-sm sm:grid-cols-[auto_1fr]">
                {list.map((t) => (
                  <div key={t.name} className="contents">
                    <dt className="font-mono text-[13px] text-gray-200">{t.name}</dt>
                    <dd className="pb-1 text-gray-400 sm:pb-0">{t.use}</dd>
                  </div>
                ))}
              </dl>
            </details>
          );
        })}
      </div>
      </Reveal>
    </Section>
  );
}
