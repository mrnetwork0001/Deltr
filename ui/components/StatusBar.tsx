"use client";
// Row 1: mode + provenance + equity + drawdown + safety badges. Also exports the
// small primitives (Panel, Chip, SourceBadge, StatusTag) every other panel uses.
import {
  AlertTriangle,
  Check,
  CircleDot,
  FlaskConical,
  KeyRound,
  Pause,
  Power,
  Radio,
  Rewind,
  X,
} from "lucide-react";
import type { ReactNode } from "react";
import type { DataSource, DdState, SystemStatus, PortfolioSnapshot } from "@/lib/types";
import { STATUS, age, pct, sourceLabel, uptime, us, usd } from "@/lib/format";

// --------------------------------------------------------------------------- primitives
export function Panel({
  title,
  right,
  children,
  className = "",
  id,
}: {
  title: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section id={id} className={`flex min-h-0 flex-col rounded-md border border-ink-700 bg-ink-900 ${className}`}>
      <header className="flex items-center justify-between gap-2 border-b border-ink-700 px-3 py-1.5">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-gray-400">{title}</h2>
        {right ? <div className="flex items-center gap-1.5 text-[11px] text-gray-400">{right}</div> : null}
      </header>
      <div className="min-h-0 flex-1 p-3">{children}</div>
    </section>
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

// --------------------------------------------------------------------------- the bar
export interface StatusBarProps {
  status: SystemStatus | null;
  portfolio: PortfolioSnapshot | null;
  mock: boolean;
  transport: "ws" | "poll" | "down";
  lastUpdate: string | null;
}

const VENUE_LABEL: Record<string, string> = {
  pancakeswap_v3: "PancakeSwap V3",
  binance_futures_mainnet: "Binance Futures mainnet (data)",
  binance_futures_testnet: "Binance Futures testnet",
  binance_spot_mirror: "Binance spot mirror",
};

function DrawdownBar({ pctValue, state }: { pctValue: number; state: DdState }) {
  const max = 4; // bar spans 0..4 %
  const w = Math.min(100, Math.max(0, (pctValue / max) * 100));
  const color = state === "HALTED" ? STATUS.serious : state === "WARN" ? STATUS.warning : STATUS.good;
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-2 w-28 rounded-sm bg-ink-800" title={`drawdown ${pct(pctValue)} of peak (WARN 2 %, HALT 3 %)`}>
        <div className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${w}%`, background: color }} />
        <div className="absolute inset-y-[-2px] w-px bg-gray-500" style={{ left: `${(2 / max) * 100}%` }} />
        <div className="absolute inset-y-[-2px] w-px" style={{ left: `${(3 / max) * 100}%`, background: STATUS.critical }} />
      </div>
      <span className="font-mono text-xs tabular-nums text-gray-200">{pct(pctValue)}</span>
      <StatusTag kind={ddKind(state)}>{state}</StatusTag>
    </div>
  );
}

export default function StatusBar({ status, portfolio, mock, transport, lastUpdate }: StatusBarProps) {
  const mode = status?.mode ?? "paper";
  const override = (status?.min_edge_bps ?? 0) < 0;
  const stress = status?.stress_active ?? null;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-ink-700 bg-ink-900 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="flex items-center gap-1.5 text-base font-bold tracking-tight text-bnb">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/brand/deltr-mark.png" alt="" className="h-5 w-5" aria-hidden />
          Deltr
        </span>
        <Chip color={mode === "testnet" ? "#1FC7D4" : "#F0B90B"} solid>
          {mode.toUpperCase()}
        </Chip>
        {status?.replay ? (
          <Chip color="#1FC7D4" title="Driven by ReplayHub: recorded ticks, not live feeds">
            <Rewind size={11} /> REPLAY
          </Chip>
        ) : null}
        {override ? (
          <Chip color={STATUS.warning} title="min-edge below 0: negative-edge hedges are allowed so the mechanics are visible (PAPER only)">
            <AlertTriangle size={11} /> MIN-EDGE OVERRIDE {status?.min_edge_bps} bps
          </Chip>
        ) : null}
        {stress ? (
          <Chip color={STATUS.critical} solid title="Stress scenario active: portfolio numbers are simulated, market feeds untouched">
            <FlaskConical size={11} /> SIMULATED · {stress}
          </Chip>
        ) : null}
        {mock ? (
          <Chip color={STATUS.warning} title="Backend unreachable: showing bundled mock snapshot">
            MOCK
          </Chip>
        ) : null}
      </div>

      <div className="flex items-center gap-3">
        {(status?.venues ?? []).map((v) => (
          <div key={v.name} className="flex items-center gap-1.5" title={`${VENUE_LABEL[v.name] ?? v.name} · ${v.detail || ""}`}>
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ background: v.ok ? STATUS.good : STATUS.critical, boxShadow: `0 0 6px ${v.ok ? STATUS.good : STATUS.critical}` }}
            />
            <span className="text-xs text-gray-300">{VENUE_LABEL[v.name] ?? v.name}</span>
            <span className="font-mono text-[11px] tabular-nums text-gray-400">{age(v.age_ms)}</span>
            <SourceBadge source={v.source} />
          </div>
        ))}
        {!status?.venues?.length ? <span className="text-xs text-gray-500">venues warming up</span> : null}
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">equity</span>
        <span className="font-mono text-sm font-semibold tabular-nums text-gray-100">{usd(portfolio?.equity_usd ?? status?.equity_usd, 2)}</span>
        <span className="text-[11px] text-gray-500">peak {usd(portfolio?.peak_equity_usd, 0)}</span>
      </div>

      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-gray-500">drawdown</span>
        <DrawdownBar pctValue={portfolio?.drawdown_pct ?? status?.drawdown_pct ?? 0} state={portfolio?.dd_state ?? status?.dd_state ?? "NORMAL"} />
      </div>

      <div className="flex items-center gap-1.5">
        {status?.kill_switch ? (
          <StatusTag kind="critical">
            <Power size={11} /> KILL
          </StatusTag>
        ) : null}
        {status?.halted ? <StatusTag kind="serious">HALTED</StatusTag> : null}
      </div>

      <div className="ml-auto flex flex-wrap items-center gap-3 text-[11px] text-gray-400">
        <span title="Median of 10,000 gate.evaluate() calls measured at startup on this machine">
          gate median <span className="font-mono tabular-nums text-gray-200">{us(status?.gate_median_us)}</span> (measured)
        </span>
        <span className="inline-flex items-center gap-1" title={`BINANCE_API_ENV=${status?.binance_api_env ?? "?"}`}>
          <KeyRound size={11} /> secrets {status?.secrets_present ? "present" : "absent"}
        </span>
        <Chip
          color={status?.upstream?.kind === "official" ? STATUS.good : status?.upstream?.kind === "shim" ? "#1FC7D4" : "#6b7280"}
          title={status?.upstream?.error ?? status?.upstream?.url ?? "no Binance MCP upstream"}
        >
          upstream: {status?.upstream?.kind ?? "none"}
        </Chip>
        <span className="inline-flex items-center gap-1" title={lastUpdate ? `last frame ${lastUpdate}` : ""}>
          <Radio size={11} style={{ color: transport === "down" ? STATUS.critical : transport === "ws" ? STATUS.good : "#9ca3af" }} />
          {transport === "ws" ? "ws" : transport === "poll" ? "poll 1 Hz" : "offline"}
        </span>
        <span className="font-mono">
          v{status?.version ?? "?"} · up {uptime(status?.uptime_s)}
        </span>
      </div>
    </div>
  );
}
