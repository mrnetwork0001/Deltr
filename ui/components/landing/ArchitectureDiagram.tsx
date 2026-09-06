// Section 3: inline-SVG architecture diagram (hosts -> Deltr MCP/API -> Engine ->
// Gate -> routers -> venues, with the Agent OS bridge on the right). The SVG
// keeps a minimum width close to its viewBox width and scrolls horizontally on
// narrow screens, so no label renders below about 11.5 px.
import Reveal from "./Reveal";
import { Section, SERIES, BRAND, nth } from "./Section";

const INK = "#0b0f19";
const RAISED = "#111827";
const LINE = "#374151";
const TEXT = "#e5e7eb";
const MUTED = "#9ca3af";
const DIM = "#6b7280";

function Box({
  x,
  y,
  w,
  h,
  title,
  sub,
  accent,
  double = false,
  titleSize = 14,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  title: string;
  sub?: string[];
  accent?: string;
  double?: boolean;
  titleSize?: number;
}) {
  const stroke = accent ?? LINE;
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={6} fill={RAISED} stroke={stroke} strokeWidth={double ? 2 : 1} />
      {double ? <rect x={x + 4} y={y + 4} width={w - 8} height={h - 8} rx={4} fill="none" stroke={stroke} strokeWidth={1} opacity={0.6} /> : null}
      <text x={x + 12} y={y + 20} fill={TEXT} fontSize={titleSize} fontWeight={600}>
        {title}
      </text>
      {(sub ?? []).map((s, i) => (
        <text key={s} x={x + 12} y={y + 20 + (i + 1) * 15} fill={MUTED} fontSize={12} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
          {s}
        </text>
      ))}
    </g>
  );
}

function Arrow({ d, dashed = false, color = LINE }: { d: string; dashed?: boolean; color?: string }) {
  return <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeDasharray={dashed ? "5 4" : undefined} markerEnd="url(#arrow)" />;
}

function Label({ x, y, text, anchor = "start", color = DIM }: { x: number; y: number; text: string; anchor?: "start" | "middle" | "end"; color?: string }) {
  return (
    <text x={x} y={y} fill={color} fontSize={12} textAnchor={anchor} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
      {text}
    </text>
  );
}

export function Diagram() {
  return (
    <svg viewBox="0 0 980 724" role="img" aria-labelledby="arch-title arch-desc" className="block h-auto w-full" style={{ minWidth: 940 }}>
      <title id="arch-title">Deltr architecture</title>
      <desc id="arch-desc">
        MCP hosts talk to the Deltr MCP server and FastAPI, which share one Engine. The Engine drives the Scout, Hedger and Executor;
        every order passes the BinanceRiskGate, then a paper or testnet router, then the venues. A TypeScript bridge discovers the
        hosted Binance MCP server or a local shim.
      </desc>
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill={LINE} />
        </marker>
      </defs>
      <rect x={0} y={0} width={980} height={724} fill={INK} rx={8} />

      {/* Column headers */}
      <Label x={40} y={22} text="MCP HOSTS" color={MUTED} />
      <Label x={720} y={22} text="BINANCE AGENT OS" color={BRAND.bnb} />

      {/* Hosts */}
      <Box x={40} y={32} w={420} h={46} title="Claude · Claude Code · Codex · Cursor · VS Code" sub={["MCP (streamable HTTP :8000/mcp or stdio)"]} />
      <Arrow d="M 170 78 L 170 118" />
      <Label x={180} y={104} text="tools/call" />

      {/* Binance MCP (hosted) */}
      <Box
        x={720}
        y={32}
        w={250}
        h={78}
        title="Hosted Binance MCP server"
        sub={["agent.binance.com/mcp/agentic", "OAuth via the host", "no withdrawal scope"]}
        accent={BRAND.bnb}
      />

      {/* Deltr MCP + FastAPI */}
      <Box x={40} y={120} w={260} h={48} title="Deltr MCP server" sub={["18 tools · deltr/mcp/server.py"]} accent={BRAND.cake} />
      <Box x={330} y={120} w={260} h={48} title="FastAPI /api + /ws/stream" sub={["deltr/api/ · serves ui/out"]} />
      <Arrow d="M 690 144 L 592 144" />
      <Label x={600} y={136} text="dashboard" />
      <Label x={600} y={162} text="/ and /app/" />

      {/* to Engine */}
      <path d="M 170 168 L 170 200 L 315 200" fill="none" stroke={LINE} strokeWidth={1.5} />
      <path d="M 460 168 L 460 200 L 315 200" fill="none" stroke={LINE} strokeWidth={1.5} />
      <Arrow d="M 315 200 L 315 224" />

      {/* Engine */}
      <Box x={185} y={226} w={260} h={48} title="Engine (facade)" sub={["scan · explain · propose · evaluate · execute · unwind"]} />

      {/* to subsystems */}
      <path d="M 315 274 L 315 296 L 130 296" fill="none" stroke={LINE} strokeWidth={1.5} />
      <path d="M 315 296 L 565 296" fill="none" stroke={LINE} strokeWidth={1.5} />
      <Arrow d="M 130 296 L 130 320" />
      <Arrow d="M 315 296 L 315 320" />
      <Arrow d="M 565 296 L 565 320" />

      <Box x={40} y={322} w={180} h={62} title="ArbitrageScout" sub={["agents/arbitrage_scout.py", "edge · history"]} />
      <Box x={225} y={322} w={180} h={62} title="Hedger" sub={["agents/hedger.py", "sizing · plans"]} />
      <Box x={445} y={322} w={240} h={62} title="Executor" sub={["deltr/executor.py", "asyncio.Lock · builds gate input"]} />

      {/* Executor to gate */}
      <Arrow d="M 565 384 L 565 414" />

      {/* Gate */}
      <Box
        x={300}
        y={416}
        w={400}
        h={80}
        title="BinanceRiskGate  (risk_gate.py)"
        sub={["19 ordered checks · stdlib only · zero LLM", "owns equity / drawdown / kill / registry", "median 1.5 to 2.5 µs, measured at startup"]}
        accent={BRAND.bnb}
        double
        titleSize={15}
      />
      <Arrow d="M 500 496 L 500 524" />
      <Label x={510} y={514} text="APPROVED only" />

      {/* Routers */}
      <Box x={300} y={526} w={400} h={46} title="PaperRouter  |  TestnetRouter (LIMIT IOC, signed)" sub={["fills labelled paper, or carry a testnet order id"]} />

      {/* to venues */}
      <path d="M 500 572 L 500 596" fill="none" stroke={LINE} strokeWidth={1.5} />
      <path d="M 180 596 L 845 596" fill="none" stroke={LINE} strokeWidth={1.5} />
      <Arrow d="M 180 596 L 180 620" color={SERIES.orange} />
      <Arrow d="M 500 596 L 500 620" color={SERIES.blue} />
      <Arrow d="M 845 596 L 845 620" />

      <Box x={40} y={622} w={280} h={70} title="PancakeSwap V3 (BSC mainnet)" sub={["slot0 + QuoterV2 via eth_call", "source: bsc-mainnet-chain"]} accent={SERIES.orange} />
      <Box x={350} y={622} w={300} h={70} title="Binance USDⓈ-M Futures testnet" sub={["premiumIndex · bookTicker · orders", "source: binance-futures-testnet"]} accent={SERIES.blue} />
      <Box x={720} y={622} w={250} h={70} title="Spot data mirror" sub={["bookTicker (reference only)", "source: binance-spot-mirror"]} />

      {/* Bridge column */}
      <Box x={720} y={190} w={250} h={62} title="Agent OS bridge" sub={["agents/agent_os_bridge.ts", "upstream: official > shim > none"]} accent={BRAND.cake} />
      <Arrow d="M 955 190 L 955 112" dashed color={BRAND.bnb} />
      <Label x={730} y={144} text="initialize / tools/list" color={MUTED} />
      <Label x={730} y={159} text="401 unless authorised" color={DIM} />

      <Box x={720} y={300} w={250} h={62} title="Binance MCP shim (local twin)" sub={["deltr/mcp/binance_shim_server.py", "stdio · testnet-backed"]} />
      <Arrow d="M 845 252 L 845 298" />
      <Arrow d="M 955 362 L 955 620" dashed color={SERIES.blue} />
      <Label x={730} y={520} text="reads testnet REST" color={DIM} />
      <Label x={730} y={535} text="and the spot mirror" color={DIM} />

      {/* Honesty note */}
      <Label x={730} y={400} text="Deltr's executor never routes" color={DIM} />
      <Label x={730} y={415} text="orders through the bridge or" color={DIM} />
      <Label x={730} y={430} text="the shim; the gate always sits" color={DIM} />
      <Label x={730} y={445} text="in front of the exchange." color={DIM} />

      {/* Legend */}
      <Label x={40} y={714} text="provenance:" color={DIM} />
      <rect x={118} y={705} width={10} height={10} fill={SERIES.orange} rx={2} />
      <Label x={134} y={714} text="DEX (orange)" color={MUTED} />
      <rect x={228} y={705} width={10} height={10} fill={SERIES.blue} rx={2} />
      <Label x={244} y={714} text="perp (blue)" color={MUTED} />
      <rect x={330} y={705} width={10} height={10} fill={BRAND.bnb} rx={2} />
      <Label x={346} y={714} text="Binance Agent OS / gate" color={MUTED} />
      <rect x={510} y={705} width={10} height={10} fill={BRAND.cake} rx={2} />
      <Label x={526} y={714} text="MCP surfaces" color={MUTED} />
    </svg>
  );
}

export default function ArchitectureDiagram() {
  return (
    <Section
      id="architecture"
      eyebrow="Architecture"
      title="One process, one engine, two transports"
      lead="MCP hosts and the REST dashboard share one Engine. Every order crosses the same gate and the same router before it reaches a venue, and every number on screen carries the source it came from."
    >
      <Reveal className="overflow-x-auto rounded-lg border border-ink-700 bg-ink-900 p-2">
        <Diagram />
      </Reveal>
      <p className="mt-2 font-mono text-xs text-gray-500 lg:hidden">Scroll sideways to see the whole diagram.</p>
      <Reveal i={1} className="stagger mt-6 grid gap-3 text-sm text-gray-400 sm:grid-cols-2">
        <div className="rounded-lg border border-ink-700 bg-ink-900 p-4" style={nth(0)}>
          <p className="font-mono text-xs uppercase tracking-wide text-gray-500">Modes</p>
          <table className="mt-2 w-full text-left">
            <thead className="text-xs text-gray-500">
              <tr>
                <th className="py-1 pr-3 font-medium">mode</th>
                <th className="py-1 pr-3 font-medium">data</th>
                <th className="py-1 pr-3 font-medium">fills</th>
                <th className="py-1 font-medium">secrets</th>
              </tr>
            </thead>
            <tbody className="text-gray-300">
              <tr className="border-t border-ink-700">
                <td className="py-1.5 pr-3 font-mono">paper</td>
                <td className="py-1.5 pr-3">live</td>
                <td className="py-1.5 pr-3">simulated at quoted prices, labelled paper</td>
                <td className="py-1.5">none</td>
              </tr>
              <tr className="border-t border-ink-700">
                <td className="py-1.5 pr-3 font-mono">testnet</td>
                <td className="py-1.5 pr-3">live</td>
                <td className="py-1.5 pr-3">real USDⓈ-M testnet LIMIT IOC orders; DEX leg simulated</td>
                <td className="py-1.5">testnet keys</td>
              </tr>
            </tbody>
          </table>
          <p className="mt-2 text-xs text-gray-500">There is no live/production mode by design.</p>
        </div>
        <div className="rounded-lg border border-ink-700 bg-ink-900 p-4" style={nth(1)}>
          <p className="font-mono text-xs uppercase tracking-wide text-gray-500">Repository map</p>
          <ul className="mt-2 space-y-1 font-mono text-xs text-gray-300">
            <li>main.py <span className="text-gray-500">one-command launcher</span></li>
            <li>risk_gate.py <span className="text-gray-500">deterministic gate</span></li>
            <li>agents/ <span className="text-gray-500">scout, hedger, agent_os_bridge.ts</span></li>
            <li>deltr/edge.py <span className="text-gray-500">edge / fee / funding / sizing math</span></li>
            <li>deltr/executor.py <span className="text-gray-500">the choke point: gate, legs, receipts</span></li>
            <li>deltr/mcp/ <span className="text-gray-500">Deltr MCP server + Binance shim</span></li>
            <li>deltr/venues/ <span className="text-gray-500">PancakeSwap V3, Futures testnet, spot mirror</span></li>
            <li>ui/ <span className="text-gray-500">this landing page and the dashboard</span></li>
            <li>skills/deltr-binance/ <span className="text-gray-500">SKILL.md, scripts/deltr.sh, references/ (MIT)</span></li>
            <li>tests/ <span className="text-gray-500">offline, deterministic; replay fixture recorded live</span></li>
          </ul>
        </div>
      </Reveal>
    </Section>
  );
}
