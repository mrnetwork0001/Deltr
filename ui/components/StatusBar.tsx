"use client";
// The app header (brand, mode, safety chips, connection) and the KPI strip under it,
// plus the small primitives every panel shares: Panel, Chip, SourceBadge, StatusTag,
// Label, Field, Stat.
import Link from "next/link";
import { AlertTriangle, Check, CircleDot, FlaskConical, KeyRound, Pause, Power, Radio, Rewind, X } from "lucide-react";
import type { ReactNode } from "react";
import type {
  ArbOpportunity,
  DataSource,
  DdState,
  EdgeBreakdown,
  Position,
  PortfolioSnapshot,
  RiskDecisionRecord,
  SystemStatus,
} from "@/lib/types";
import { STATUS, age, bps, pct, sourceLabel, uptime, us, usd, usdSigned } from "@/lib/format";

// --------------------------------------------------------------------------- primitives
export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
  id,
}: {
  title: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
}) {
  return (
    <section id={id} className={`flex w-full min-w-0 flex-col rounded-lg border border-ink-700 bg-ink-900 ${className}`}>
      <header className="flex min-h-[42px] items-center justify-between gap-3 border-b border-ink-700/80 px-4 py-2">
        <h2 className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.14em] text-gray-400">{title}</h2>
        {right ? <div className="flex min-w-0 items-center gap-2 text-[11px] text-gray-400">{right}</div> : null}
      </header>
      <div className={`flex min-h-0 flex-1 flex-col p-4 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/** Small uppercase caption used above every value. */
export function Label({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`text-[10.5px] font-medium uppercase tracking-[0.12em] text-gray-500 ${className}`}>{children}</div>;
}

/** Caption + value pair. */
export function Field({ label, children, className = "", mono = true }: { label: ReactNode; children: ReactNode; className?: string; mono?: boolean }) {
  return (
    <div className={`min-w-0 ${className}`}>
      <Label>{label}</Label>
      <div className={`mt-0.5 text-[13px] text-gray-200 ${mono ? "font-mono tabular-nums" : ""}`}>{children}</div>
    </div>
  );
}

export function Chip({
  children,
  color,
  solid = false,
  title,
  className = "",
}: {
  children: ReactNode;
  color?: string;
  solid?: boolean;
  title?: string;
  className?: string;
}) {
  const c = color ?? "#9ca3af";
  const style = solid
    ? { background: c, color: "#07090f", borderColor: c }
    : { color: c, borderColor: c + "66", background: c + "14" };
  return (
    <span
      title={title}
      style={style}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-[1px] font-mono text-[11px] font-semibold leading-4 tracking-wide ${className}`}
    >
      {children}
    </span>
  );
}

const SOURCE_SHORT: Record<string, string> = {
  "bsc-mainnet-chain": "bsc-mainnet-chain",
  "binance-futures-mainnet": "binance-futures-mainnet",
  "binance-futures-testnet": "binance-futures-testnet",
  "binance-spot-mirror": "binance-spot-mirror",
  paper: "paper",
  replay: "replay",
  simulated: "simulated",
};

/** The provenance badge every price / rate carries. */
export function SourceBadge({ source, compact = false }: { source: DataSource | string | null | undefined; compact?: boolean }) {
  if (!source) return null;
  const s = String(source);
  const critical = s === "simulated";
  const label = compact ? s.replace("binance-", "").replace("-mainnet", "") : SOURCE_SHORT[s] ?? s;
  return (
    <span
      title={sourceLabel(s)}
      className="inline-flex items-center rounded-sm border px-1 font-mono text-[10px] leading-[14px] tracking-tight"
      style={
        critical
          ? { color: STATUS.critical, borderColor: STATUS.critical + "80", background: STATUS.critical + "14" }
          : { color: "#9ca3af", borderColor: "#1f2937", background: "#111827" }
      }
    >
      {label}
    </span>
  );
}

export type StatusKind = "good" | "warning" | "serious" | "critical" | "muted";

export function StatusTag({ kind, children, big = false }: { kind: StatusKind; children: ReactNode; big?: boolean }) {
  const color = kind === "muted" ? "#6b7280" : STATUS[kind];
  const Icon = kind === "good" ? Check : kind === "critical" ? X : kind === "serious" ? Pause : kind === "warning" ? AlertTriangle : CircleDot;
  return (
    <span
      style={{ color, borderColor: color + "80", background: color + "1a" }}
      className={`inline-flex items-center gap-1 rounded border font-semibold ${big ? "px-2 py-0.5 text-[13px]" : "px-1.5 py-[1px] text-[11px]"}`}
    >
      <Icon size={big ? 14 : 12} strokeWidth={3} />
      {children}
    </span>
  );
}

export function ddKind(state: DdState | null | undefined): StatusKind {
  return state === "HALTED" ? "serious" : state === "WARN" ? "warning" : "good";
}

// --------------------------------------------------------------------------- the header
export interface EngineChoice {
  key: string;
  label: string;
}

export interface StatusBarProps {
  status: SystemStatus | null;
  portfolio: PortfolioSnapshot | null;
  mock: boolean;
  transport: "ws" | "poll" | "down";
  lastUpdate: string | null;
  /** More than one engine (e.g. the public PAPER and LIVE showcases) shows a switcher. */
  engines?: EngineChoice[];
  engine?: string;
  onEngine?: (key: string) => void;
}

/** Segmented control that changes which engine the dashboard reads. It never changes a mode:
 *  each engine runs its own process with its own opt-in. */
function EngineSwitch({ engines, engine, onEngine }: { engines: EngineChoice[]; engine: string; onEngine: (k: string) => void }) {
  return (
    <div role="tablist" aria-label="Engine" className="inline-flex rounded-md border border-ink-700 bg-ink-900 p-0.5">
      {engines.map((e) => {
        const on = e.key === engine;
        const live = e.key === "live";
        return (
          <button
            key={e.key}
            role="tab"
            aria-selected={on}
            onClick={() => onEngine(e.key)}
            title={live ? "the LIVE engine: real mainnet orders, read-only here" : "the PAPER engine: simulated fills on real mainnet prices"}
            className="rounded px-2.5 py-0.5 font-mono text-[11px] font-semibold tracking-wide transition"
            style={
              on
                ? { background: live ? STATUS.critical : "#F0B90B", color: "#07090f" }
                : { color: "#9ca3af" }
            }
          >
            {e.label}
          </button>
        );
      })}
    </div>
  );
}

export default function StatusBar({ status, mock, transport, lastUpdate, engines = [], engine = "", onEngine }: StatusBarProps) {
  const mode = status?.mode ?? "paper";
  const override = (status?.min_edge_bps ?? 0) < 0;
  const stress = status?.stress_active ?? null;
  const upKind = status?.upstream?.kind ?? "none";
  return (
    <div className="sticky top-0 z-30 border-b border-ink-700/80 bg-ink-950/90 backdrop-blur">
      <div className="mx-auto flex h-12 max-w-[1800px] items-center gap-3 px-4">
        <Link href="/" title="Back to the landing page" className="flex items-center gap-2 transition hover:opacity-80">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/brand/deltr-mark.png" alt="" className="h-6 w-6" aria-hidden />
          <span className="text-[15px] font-bold tracking-tight text-gray-50">Deltr</span>
        </Link>
        <span aria-hidden className="h-5 w-px bg-ink-700" />
        {engines.length > 1 && onEngine ? (
          <EngineSwitch engines={engines} engine={engine} onEngine={onEngine} />
        ) : null}
        <Chip color={mode === "testnet" ? "#1FC7D4" : mode === "live" ? STATUS.critical : "#F0B90B"} solid title="the mode this engine runs in; no tool or button changes it">
          {status ? mode.toUpperCase() : "…"}
        </Chip>
        {status?.real_funds_armed ? (
          <StatusTag kind="critical">REAL FUNDS ARMED</StatusTag>
        ) : null}
        {status?.replay ? (
          <Chip color="#1FC7D4" title="Driven by ReplayHub: recorded ticks, not live feeds">
            <Rewind size={11} /> REPLAY
          </Chip>
        ) : null}
        {override ? (
          <Chip
            color={mode === "live" ? STATUS.critical : STATUS.warning}
            title={
              mode === "live"
                ? "LIVE TEST OVERRIDE: the min edge is below 0 under a tiny per-trade cap, so a knowingly small loss is accepted for testing"
                : "min-edge below 0: negative-edge hedges are allowed so the mechanics are visible (PAPER only)"
            }
          >
            <AlertTriangle size={11} /> {mode === "live" ? "LIVE TEST OVERRIDE" : "MIN-EDGE OVERRIDE"} {status?.min_edge_bps} bps
          </Chip>
        ) : null}
        {stress ? (
          <Chip color={STATUS.critical} solid title="Stress scenario active: portfolio numbers are simulated, market feeds untouched">
            <FlaskConical size={11} /> SIMULATED · {stress}
          </Chip>
        ) : null}
        {status?.kill_switch ? (
          <StatusTag kind="critical">
            <Power size={11} /> KILL
          </StatusTag>
        ) : null}
        {status?.halted ? <StatusTag kind="serious">HALTED</StatusTag> : null}
        {mock ? (
          <Chip color={STATUS.warning} title="Backend unreachable: showing bundled mock snapshot">
            MOCK
          </Chip>
        ) : null}

        <div className="ml-auto flex items-center gap-4 text-[11px] text-gray-400">
          <span className="hidden items-center gap-1.5 md:inline-flex" title={`BINANCE_API_ENV=${status?.binance_api_env ?? "?"}`}>
            <KeyRound size={11} /> secrets {status?.secrets_present ? "present" : "absent"}
          </span>
          <Chip
            className="hidden md:inline-flex"
            color={upKind === "official" ? STATUS.good : upKind === "shim" ? "#1FC7D4" : "#6b7280"}
            title={status?.upstream?.error ?? status?.upstream?.url ?? "no Binance MCP upstream"}
          >
            upstream: {upKind}
          </Chip>
          <span className="inline-flex items-center gap-1.5" title={lastUpdate ? `last frame ${lastUpdate}` : ""}>
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{
                background: transport === "down" ? STATUS.critical : STATUS.good,
                boxShadow: `0 0 6px ${transport === "down" ? STATUS.critical : STATUS.good}`,
              }}
            />
            <Radio size={11} className="text-gray-500" />
            {transport === "ws" ? "live · ws" : transport === "poll" ? "live · poll 1 Hz" : "offline"}
          </span>
          <span className="hidden font-mono tabular-nums sm:inline">
            v{status?.version ?? "?"} · up {uptime(status?.uptime_s)}
          </span>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- the KPI strip
const VENUE_LABEL: Record<string, string> = {
  pancakeswap_v3: "PancakeSwap V3",
  binance_futures_mainnet: "Futures mainnet",
  binance_futures_testnet: "Futures testnet",
  binance_spot_mirror: "Spot mirror",
};

function DrawdownBar({ pctValue, state }: { pctValue: number; state: DdState }) {
  const max = 4; // bar spans 0..4 %
  const w = Math.min(100, Math.max(0, (pctValue / max) * 100));
  const color = state === "HALTED" ? STATUS.serious : state === "WARN" ? STATUS.warning : STATUS.good;
  return (
    <div className="relative h-1.5 w-full rounded-sm bg-ink-800" title={`drawdown ${pct(pctValue)} of peak (WARN 2 %, HALT 3 %)`}>
      <div className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${w}%`, background: color }} />
      <div className="absolute inset-y-[-3px] w-px bg-gray-500" style={{ left: `${(2 / max) * 100}%` }} />
      <div className="absolute inset-y-[-3px] w-px" style={{ left: `${(3 / max) * 100}%`, background: STATUS.critical }} />
    </div>
  );
}

export function Stat({
  label,
  value,
  sub,
  accent,
  children,
  title,
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  accent?: string;
  children?: ReactNode;
  title?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col rounded-lg border border-ink-700 bg-ink-900 px-4 py-3" title={title}>
      <Label>{label}</Label>
      <div className="mt-1.5 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-mono text-[22px] font-semibold leading-none tabular-nums" style={{ color: accent ?? "#f3f4f6" }}>
          {value}
        </span>
        {sub ? <span className="text-[11px] text-gray-500">{sub}</span> : null}
      </div>
      {children ? <div className="mt-2 text-[11px] text-gray-400">{children}</div> : null}
    </div>
  );
}

export interface KpiStripProps {
  status: SystemStatus | null;
  portfolio: PortfolioSnapshot | null;
  edge: EdgeBreakdown | null;
  opportunity: ArbOpportunity | null;
  positions: Position[];
  decisions: RiskDecisionRecord[];
}

export function KpiStrip({ status, portfolio, edge, opportunity, positions, decisions }: KpiStripProps) {
  const equity = portfolio?.equity_usd ?? status?.equity_usd;
  const dd = portfolio?.drawdown_pct ?? status?.drawdown_pct ?? 0;
  const ddState = portfolio?.dd_state ?? status?.dd_state ?? "NORMAL";
  const net = edge?.net_edge_bps ?? null;
  const open = positions.filter((p) => p.status === "open");
  const notional = open.reduce((a, p) => a + p.notional_usd, 0);
  const delta = open.reduce((a, p) => a + p.delta_base, 0);
  const latest = [...decisions].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())[0] ?? null;
  const venues = status?.venues ?? [];
  const okCount = venues.filter((v) => v.ok).length;
  const pnl = portfolio?.total_pnl_usd ?? 0;

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
      <Stat label="Equity" value={usd(equity, 2)} sub={`peak ${usd(portfolio?.peak_equity_usd, 0)}`}>
        total pnl{" "}
        <span className="font-mono tabular-nums" style={{ color: pnl >= 0 ? STATUS.good : STATUS.critical }}>
          {usdSigned(pnl)}
        </span>
        <span className="text-gray-600"> · </span>today{" "}
        <span className="font-mono tabular-nums text-gray-300">{usdSigned(portfolio?.daily_realized_usd)}</span>
      </Stat>

      <Stat label="Drawdown" value={pct(dd)} sub={<StatusTag kind={ddKind(ddState)}>{ddState}</StatusTag>}>
        <DrawdownBar pctValue={dd} state={ddState} />
        <div className="mt-1.5 flex justify-between font-mono text-[10px] text-gray-600">
          <span>0</span>
          <span>warn 2 %</span>
          <span>halt 3 %</span>
        </div>
      </Stat>

      <Stat
        label={`Net edge · ${edge?.horizon_h ?? 72} h`}
        value={bps(net)}
        accent={net === null ? undefined : net >= 0 ? STATUS.good : STATUS.critical}
        sub={`min ${bps(status?.min_edge_bps ?? opportunity?.min_edge_bps_used)}`}
      >
        {opportunity ? (
          <span style={{ color: opportunity.is_actionable ? STATUS.good : STATUS.warning }}>
            {opportunity.is_actionable ? "actionable" : "not actionable"}
            {opportunity.reason && opportunity.reason !== "ok" ? <span className="font-mono text-gray-500"> · {opportunity.reason}</span> : null}
          </span>
        ) : (
          <span className="text-gray-500">waiting for quotes</span>
        )}
      </Stat>

      <Stat label="Open positions" value={String(open.length)} sub={open.length ? `notional ${usd(notional, 0)}` : "none"}>
        delta{" "}
        <span className="font-mono tabular-nums" style={{ color: Math.abs(delta) <= 0.01 ? STATUS.good : STATUS.warning }}>
          {delta.toFixed(2)} BNB
        </span>
        <span className="text-gray-600"> · </span>margin{" "}
        <span className="font-mono tabular-nums text-gray-300">{usd(open.reduce((a, p) => a + p.margin_usd, 0), 0)}</span>
      </Stat>

      <Stat
        label="Risk gate"
        value={latest ? (latest.approved ? "APPROVED" : "VETO") : "–"}
        accent={latest ? (latest.approved ? STATUS.good : STATUS.critical) : undefined}
        sub={latest ? (latest.approved ? (latest.dry_run ? "dry run" : latest.code) : latest.code) : "no decisions"}
        title="Median of 10,000 gate.evaluate() calls measured at startup on this machine"
      >
        median <span className="font-mono tabular-nums text-gray-300">{us(status?.gate_median_us)}</span> measured
        <span className="text-gray-600"> · </span>
        <span className="font-mono tabular-nums text-gray-300">{decisions.length}</span> decisions
      </Stat>

      <Stat
        label="Data feeds"
        value={venues.length ? `${okCount}/${venues.length}` : "–"}
        sub={venues.length ? (okCount === venues.length ? "all healthy" : "degraded") : "warming up"}
        accent={venues.length && okCount < venues.length ? STATUS.critical : undefined}
      >
        <ul className="flex flex-col gap-0.5">
          {venues.map((v) => (
            <li key={v.name} className="flex items-center gap-1.5" title={`${VENUE_LABEL[v.name] ?? v.name} · ${sourceLabel(v.source)} · ${v.detail || ""}`}>
              <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: v.ok ? STATUS.good : STATUS.critical }} />
              <span className="truncate text-gray-300">{VENUE_LABEL[v.name] ?? v.name}</span>
              <span className="ml-auto font-mono tabular-nums text-gray-500">{age(v.age_ms)}</span>
            </li>
          ))}
        </ul>
      </Stat>
    </div>
  );
}
