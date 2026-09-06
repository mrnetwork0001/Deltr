// Section 7: the Skills Hub packaging status (stated as it is in the checkout),
// the bridge CLI and an honest note on the Binance MCP upstream.
import { BINANCE_MCP_ADD, BINANCE_MCP_URL, BRIDGE_COMMANDS, SKILL_INSTALL, SKILL_RUN, SKILL_STATUS, UPSTREAM_KINDS } from "@/lib/landing";
import Reveal from "./Reveal";
import { BRAND, Card, Code, Mono, Section, STATUS, Tag, nth } from "./Section";

const KIND_COLOR: Record<string, string> = { official: STATUS.good, shim: BRAND.cake, none: "#6b7280" };

export default function AgentOsPanel() {
  return (
    <Section
      id="agent-os"
      eyebrow="Binance Agent OS"
      title="A bridge to the Binance MCP server, and a Skills Hub skill"
      lead="Deltr discovers the hosted Binance MCP upstream, reports exactly which upstream it could reach, and ships as a Skills Hub skill. It never claims more than it exercised."
    >
      <Reveal className="stagger grid gap-4 lg:grid-cols-2">
        <div className="min-w-0" style={nth(0)}>
        <Card>
          <h3 className="text-base font-semibold text-gray-100">Connect the Binance MCP server next to Deltr</h3>
          <Code className="mt-3">{BINANCE_MCP_ADD}</Code>
          <p className="mt-2 text-sm text-gray-400">
            Binance Agent OS publishes one hosted endpoint, <Mono>{BINANCE_MCP_URL}</Mono>. It uses OAuth through a supported host, trades inside a
            dedicated Agentic sub-account, and has no withdrawal scope. Binance does not publish the tool names; discover them with{" "}
            <Mono>tools/list</Mono> after authorising.
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-gray-100">Skills Hub packaging</h3>
            <Tag color={STATUS.good}>{SKILL_STATUS.state}</Tag>
          </div>
          <p className="mt-2 text-sm text-gray-400">
            <Mono>{SKILL_STATUS.folder}</Mono>: {SKILL_STATUS.note}
          </p>
          <p className="mt-3 text-xs text-gray-500">Install, then run one tick (nothing executes):</p>
          <Code className="mt-1">{SKILL_INSTALL}</Code>
          <Code className="mt-1">{SKILL_RUN}</Code>
        </Card>
        </div>

        <div className="min-w-0" style={nth(1)}>
        <Card>
          <h3 className="text-base font-semibold text-gray-100">The bridge CLI</h3>
          <p className="mt-1 text-sm text-gray-400">
            <Mono>agents/agent_os_bridge.ts</Mono>, run with <Mono>npx tsx</Mono>.
          </p>
          <ul className="mt-3 space-y-2">
            {BRIDGE_COMMANDS.map((b) => (
              <li key={b.cmd}>
                <Code>{b.cmd}</Code>
                <p className="mt-1 text-sm text-gray-400">{b.what}</p>
              </li>
            ))}
          </ul>
        </Card>
        </div>
      </Reveal>

      <Reveal className="mt-4">
      <Card>
        <h3 className="text-base font-semibold text-gray-100">Upstream, reported honestly</h3>
        <p className="mt-1 text-sm text-gray-400">
          The official Binance MCP upstream is OAuth-gated. The bridge tries it first, falls back to the local testnet-backed shim, and reports the
          result on the dashboard status bar as one of three kinds. A 401 on the official endpoint is reported as <Mono>authorized: false</Mono>,
          never as a working connection.
        </p>
        <ul className="stagger mt-4 grid gap-3 md:grid-cols-3">
          {UPSTREAM_KINDS.map((u, i) => (
            <li key={u.kind} className="rounded-md border border-ink-700 bg-ink-950 p-3" style={nth(i + 1)}>
              <Tag color={KIND_COLOR[u.kind] ?? "#9ca3af"}>upstream: {u.kind}</Tag>
              <p className="mt-2 text-sm text-gray-400">{u.meaning}</p>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-sm text-gray-400">
          Deltr&apos;s own executor never routes orders through the bridge or the shim; it signs testnet REST orders directly, so the deterministic
          gate always sits in front of the exchange.
        </p>
      </Card>
      </Reveal>
    </Section>
  );
}
