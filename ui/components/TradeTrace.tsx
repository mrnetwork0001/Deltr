"use client";
// Hero panel: the latest execution receipt (or the latest prompt when nothing
// executed). Everything the video zooms on lives here, so fonts are >= 13 px.
import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Copy, Check } from "lucide-react";
import type { ExecutionReceipt, Fill, PromptResult, TraceStep } from "@/lib/types";
import { STATUS, bps, clockMs, ms, px, qty, shortHash, usd } from "@/lib/format";
import { Chip, Panel, SourceBadge, StatusTag } from "@/components/StatusBar";

function stepKind(s: TraceStep): "good" | "critical" | "warning" | "muted" {
  return s.status === "ok" ? "good" : s.status === "veto" || s.status === "error" ? "critical" : "muted";
}

function CopyButton({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch {
          /* clipboard unavailable */
        }
      }}
      title="copy"
      className="inline-flex items-center gap-1 rounded border border-ink-700 px-1.5 py-[1px] text-[11px] text-gray-400 hover:border-gray-500 hover:text-gray-200"
    >
      {done ? <Check size={11} /> : <Copy size={11} />}
      {label}
    </button>
  );
}

function SourceChip({ source, client }: { source: string; client: string | null }) {
  const label = source === "mcp" && client ? `mcp:${client}` : source;
  return (
    <Chip color={source === "mcp" ? "#1FC7D4" : source === "auto" ? "#6b7280" : "#9ca3af"} title="who initiated this trace">
      {label}
    </Chip>
  );
}

function Steps({ steps }: { steps: TraceStep[] }) {
  if (!steps.length) return <div className="text-[13px] text-gray-500">no steps recorded</div>;
  return (
    <ol className="flex flex-col gap-1">
      {steps.map((s, i) => {
        const kind = stepKind(s);
        const color = kind === "muted" ? "#6b7280" : STATUS[kind];
        return (
          <li key={i} className="grid grid-cols-[22px_92px_1fr_64px] items-start gap-2 text-[13px] leading-5">
            <span className="mt-[5px] inline-block h-2.5 w-2.5 rounded-full" style={{ background: color, boxShadow: `0 0 5px ${color}` }} />
            <span className="font-mono font-semibold text-gray-200">{s.step}</span>
            <span className="text-gray-300">
              {s.summary}
              {s.status !== "ok" ? (
                <span className="ml-2 font-semibold uppercase" style={{ color }}>
                  {s.status}
                </span>
              ) : null}
            </span>
            <span className="text-right font-mono text-[13px] tabular-nums text-gray-500" title={clockMs(s.ts)}>
              {ms(s.latency_ms)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function Fills({ fills }: { fills: Fill[] }) {
  if (!fills.length) return null;
  return (
    <table className="w-full text-[13px]">
      <thead>
        <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500">
          <th className="py-1 font-medium">venue</th>
          <th className="py-1 font-medium">side</th>
          <th className="py-1 text-right font-medium">qty</th>
          <th className="py-1 text-right font-medium">price</th>
          <th className="py-1 text-right font-medium">fee</th>
          <th className="py-1 pl-2 font-medium">ref</th>
          <th className="py-1 text-right font-medium">vs ref</th>
          <th className="py-1 text-right font-medium">lat</th>
        </tr>
      </thead>
      <tbody className="font-mono tabular-nums">
        {fills.map((f) => (
          <tr key={f.leg_index} className="border-t border-ink-700">
            <td className="py-1 text-gray-200">{f.venue === "pancakeswap_v3" ? "PancakeSwap V3" : f.venue === "binance_futures" ? "Binance Futures" : "Binance Spot"}</td>
            <td className="py-1 font-semibold text-gray-200">{f.side}</td>
            <td className="py-1 text-right text-gray-200">{qty(f.qty)}</td>
            <td className="py-1 text-right text-gray-100">
              {px(f.price)} <SourceBadge source={f.source} />
            </td>
            <td className="py-1 text-right text-gray-400">{usd(f.fee_usd, 3)}</td>
            <td className="py-1 pl-2 text-gray-400" title={f.client_id ?? ""}>
              {f.ref}
              {f.simulated ? <SourceBadge source="simulated" /> : null}
            </td>
            <td className="py-1 text-right text-gray-400">{bps(f.reference_divergence_bps)}</td>
            <td className="py-1 text-right text-gray-500">{ms(f.latency_ms)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function receiptKind(status: ExecutionReceipt["status"]): "good" | "critical" | "warning" | "muted" {
  return status === "filled" ? "good" : status === "unwound" ? "warning" : status === "expired" ? "muted" : "critical";
}

export interface TradeTraceProps {
  receipts: ExecutionReceipt[];
  prompts: PromptResult[];
  selectedTraceId: string | null;
  onSelectTrace: (id: string | null) => void;
}

function promptTs(p: PromptResult): number {
  const last = p.steps[p.steps.length - 1]?.ts ?? p.precheck?.ts ?? p.plan?.created_at;
  return last ? new Date(last).getTime() : 0;
}

export default function TradeTrace({ receipts, prompts, selectedTraceId, onSelectTrace }: TradeTraceProps) {
  const [showJson, setShowJson] = useState(false);
  const pick = useMemo(() => {
    const rs = [...receipts].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
    const ps = [...prompts].sort((a, b) => promptTs(b) - promptTs(a));
    if (selectedTraceId) {
      const r = rs.find((x) => x.id === selectedTraceId || x.plan_id === selectedTraceId);
      if (r) return { receipt: r, prompt: null as PromptResult | null };
      const p = ps.find((x) => x.plan_id === selectedTraceId);
      if (p) return { receipt: null as ExecutionReceipt | null, prompt: p };
    }
    const r = rs[0] ?? null;
    const p = ps[0] ?? null;
    if (r && p && promptTs(p) > new Date(r.ts).getTime()) return { receipt: null, prompt: p };
    if (r) return { receipt: r, prompt: null };
    return { receipt: null, prompt: p };
  }, [receipts, prompts, selectedTraceId]);

  const r = pick.receipt;
  const p = pick.prompt;
  const traces = [
    ...receipts.map((x) => ({ id: x.id, label: `${x.status} ${x.id}`, ts: x.ts })),
    ...prompts.filter((x) => x.plan_id).map((x) => ({ id: x.plan_id as string, label: `prompt ${x.plan_id}`, ts: new Date(promptTs(x)).toISOString() })),
  ].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());

  return (
    <Panel
      id="trace"
      title="Trade trace"
      className="text-[13px]"
      right={
        traces.length > 1 ? (
          <select
            value={selectedTraceId ?? ""}
            onChange={(e) => onSelectTrace(e.target.value || null)}
            className="rounded border border-ink-700 bg-ink-800 px-1 py-[1px] font-mono text-[11px] text-gray-300"
          >
            <option value="">latest</option>
            {traces.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        ) : null
      }
    >
      {r ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusTag kind={receiptKind(r.status)} big>
              {r.status.toUpperCase()}
            </StatusTag>
            <SourceChip source={r.source} client={r.client} />
            <Chip color={r.mode === "testnet" ? "#1FC7D4" : "#F0B90B"}>{r.mode.toUpperCase()}</Chip>
            {r.stress_active ? (
              <Chip color={STATUS.critical} solid>
                SIMULATED · {r.stress_active}
              </Chip>
            ) : null}
            <span className="ml-auto font-mono text-[13px] tabular-nums text-gray-500">{clockMs(r.ts)}</span>
          </div>
          {r.prompt ? <blockquote className="border-l-2 border-cake pl-3 text-[14px] italic text-gray-100">“{r.prompt}”</blockquote> : null}
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-[1fr_1fr]">
            <div>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-gray-500">steps</div>
              <Steps steps={r.steps} />
            </div>
            <div className="flex flex-col gap-2">
              <div className="text-[11px] uppercase tracking-wide text-gray-500">fills</div>
              <Fills fills={r.fills} />
              <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[13px] text-gray-400">
                <div>
                  plan <span className="font-mono text-gray-200">{r.plan.qty} {r.plan.symbol.replace("USDT", "")}</span> · {r.plan.leverage}x · notional{" "}
                  <span className="font-mono tabular-nums text-gray-200">{usd(r.plan.notional_usd, 0)}</span>
                </div>
                <div>
                  gate <span className="font-mono text-gray-200">{r.decision.code}</span> in{" "}
                  <span className="font-mono tabular-nums text-gray-200">{r.decision.latency_us} µs</span>
                </div>
                <div>
                  legging window <span className="font-mono tabular-nums text-gray-200">{ms(r.legging_window_ms)}</span>
                </div>
                <div>
                  residual delta <span className="font-mono tabular-nums text-gray-200">{qty(r.residual_delta_base)}</span>
                </div>
                <div>
                  realized cost <span className="font-mono tabular-nums text-gray-200">{usd(r.realized_cost_usd)}</span>
                </div>
                <div>
                  position <span className="font-mono text-gray-200">{r.position_id ?? "–"}</span>
                </div>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-ink-700 pt-2 font-mono text-[13px]">
            <span className="text-gray-500">receipt</span>
            <span className="text-gray-200">{r.id}</span>
            <CopyButton text={r.id} />
            <span className="ml-2 text-gray-500">sha256</span>
            <span className="tabular-nums text-gray-200" title={r.sha256}>
              {shortHash(r.sha256, 16, 8)}
            </span>
            <CopyButton text={r.sha256} />
            <button
              onClick={() => setShowJson((v) => !v)}
              className="ml-auto inline-flex items-center gap-1 rounded border border-ink-700 px-1.5 py-[1px] text-[11px] text-gray-400 hover:text-gray-200"
            >
              {showJson ? <ChevronDown size={11} /> : <ChevronRight size={11} />} receipt JSON
            </button>
          </div>
          {showJson ? (
            <pre className="max-h-72 overflow-auto rounded border border-ink-700 bg-ink-950 p-2 font-mono text-[11px] leading-4 text-gray-300">
              {JSON.stringify(r, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : p ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            {p.precheck ? (
              <StatusTag kind={p.precheck.approved ? "good" : "critical"} big>
                {p.precheck.approved ? "PRECHECK APPROVED" : `VETO ${p.precheck.code}`}
              </StatusTag>
            ) : (
              <StatusTag kind="muted" big>
                PROMPT
              </StatusTag>
            )}
            <SourceChip source={p.intent.source} client={p.plan?.client ?? null} />
            <Chip color="#9ca3af">intent: {p.intent.action}</Chip>
            <span className="ml-auto text-[13px] text-gray-500">not executed (prompts never execute)</span>
          </div>
          <blockquote className="border-l-2 border-cake pl-3 text-[14px] italic text-gray-100">“{p.intent.raw}”</blockquote>
          <Steps steps={p.steps} />
          <div className="text-[13px] text-gray-300">{p.message}</div>
          {p.plan ? (
            <div className="font-mono text-[13px] text-gray-400">
              plan {p.plan.id} · {p.plan.qty} BNB · {p.plan.leverage}x · cash {usd(p.plan.cash_required_usd, 0)}
            </div>
          ) : null}
          {p.intent.stablecoin_note ? <div className="text-[13px] text-gray-500">{p.intent.stablecoin_note}</div> : null}
        </div>
      ) : (
        <div className="flex h-40 items-center justify-center text-[13px] text-gray-500">no trade yet: send a prompt below or call deltr_prompt over MCP</div>
      )}
    </Panel>
  );
}
