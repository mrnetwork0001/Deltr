"use client";
// Latest gate decision with the full check list in CHECK_ORDER, plus history.
// Each check is a two-line row (name, then observed / limit unit) so nothing is
// ever truncated. Fonts >= 12.5 px: the video zooms on this panel.
import { useState } from "react";
import { Check, X, Minus } from "lucide-react";
import type { RiskDecisionRecord, SystemStatus } from "@/lib/types";
import { STATUS, anyVal, clock, us } from "@/lib/format";
import { postResetHalt } from "@/lib/api";
import { Label, Panel, StatusTag, ddKind } from "@/components/StatusBar";

/** Dict / list values print without braces and quotes; the full text is in the row title. */
function plain(v: string): string {
  if (v.startsWith("{") || v.startsWith("[")) return v.replace(/^[{[]|[}\]]$/g, "").replace(/"/g, "");
  return v;
}

export interface RiskGateLogProps {
  decisions: RiskDecisionRecord[];
  status: SystemStatus | null;
  mock: boolean;
  onChanged?: () => void;
}

export default function RiskGateLog({ decisions, status, mock, onChanged }: RiskGateLogProps) {
  const sorted = [...decisions].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
  const [picked, setPicked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const d = (picked && sorted.find((x) => x.id === picked)) || sorted[0] || null;
  const order = status?.check_order?.length ? status.check_order : d?.checks.map((c) => c.name) ?? [];
  const byName = new Map((d?.checks ?? []).map((c) => [c.name, c]));
  const ddState = status?.dd_state ?? d?.dd_state ?? "NORMAL";
  const passed = d ? d.checks.filter((c) => c.passed).length : 0;

  const resetHalt = async () => {
    const reason = window.prompt("Reason for clearing the drawdown halt (recorded in the journal):", "demo reset");
    if (!reason) return;
    setBusy(true);
    setMsg(null);
    try {
      const s = await postResetHalt({ reason });
      setMsg(s.halted ? "refused: still under water (HALT_NOT_CLEARABLE)" : "halt cleared, peak unchanged");
      onChanged?.();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel
      title="Risk gate"
      className="h-full"
      right={
        <span className="font-mono tabular-nums" title="measured median of 10k evaluate() calls at startup">
          median {us(status?.gate_median_us)}
        </span>
      }
    >
      <div className="flex h-full min-h-0 flex-col gap-3">
        {ddState !== "NORMAL" || status?.kill_switch ? (
          <div
            className="flex items-center gap-2 rounded-md border px-3 py-2 text-[12.5px]"
            style={{
              borderColor: (ddState === "HALTED" ? STATUS.serious : STATUS.warning) + "80",
              background: (ddState === "HALTED" ? STATUS.serious : STATUS.warning) + "14",
            }}
          >
            <StatusTag kind={status?.kill_switch && ddState === "NORMAL" ? "critical" : ddKind(ddState)}>
              {status?.kill_switch && ddState === "NORMAL" ? "KILL SWITCH" : ddState}
            </StatusTag>
            <span className="text-gray-300">
              {ddState === "HALTED"
                ? "drawdown ≥ 3 %: new hedges vetoed, only reduce-only unwinds pass"
                : ddState === "WARN"
                  ? "drawdown ≥ 2 %: warning, gate still open"
                  : "kill switch engaged: only verified unwinds pass"}
            </span>
            {ddState === "HALTED" ? (
              <button
                onClick={resetHalt}
                disabled={busy || mock}
                className="ml-auto rounded border border-ink-700 bg-ink-800 px-2 py-[2px] text-[12px] text-gray-200 hover:border-gray-500 disabled:opacity-50"
              >
                reset halt
              </button>
            ) : null}
          </div>
        ) : null}
        {msg ? <div className="text-[12.5px] text-gray-400">{msg}</div> : null}

        {d ? (
          <>
            {/* verdict */}
            <div className="rounded-md border border-ink-700/80 bg-ink-950/60 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <StatusTag kind={d.approved ? "good" : "critical"} big>
                  {d.approved ? "APPROVED" : "VETO"}
                </StatusTag>
                <span className="font-mono text-[13px] font-bold text-gray-100">{d.code}</span>
                {d.dry_run ? <span className="rounded border border-ink-700 px-1.5 text-[11px] text-gray-400">dry run</span> : null}
                <span className="ml-auto font-mono text-[12px] tabular-nums text-gray-500" title={`${d.latency_ns} ns`}>
                  {us(d.latency_us, 2)}
                </span>
              </div>
              <div className="mt-1.5 text-[12.5px] leading-5 text-gray-300">{d.reason}</div>
              {!d.approved ? (
                <div className="mt-1 font-mono text-[12.5px] tabular-nums text-gray-200">
                  observed <span style={{ color: STATUS.critical }}>{anyVal(d.observed)}</span> · limit {anyVal(d.limit)} {d.unit}
                </div>
              ) : null}
            </div>

            {/* check list */}
            <div className="flex items-center justify-between">
              <Label>checks in order</Label>
              <span className="font-mono text-[11px] tabular-nums text-gray-500">
                {passed}/{order.length} passed
              </span>
            </div>
            <ol className="-mx-1 flex min-h-0 flex-1 flex-col overflow-y-auto">
              {order.map((name, i) => {
                const c = byName.get(name);
                const failed = c ? !c.passed : false;
                const reached = !!c;
                const color = !reached ? "#4b5563" : failed ? STATUS.critical : STATUS.good;
                const obs = reached ? plain(anyVal(c!.observed)) : "";
                const lim = reached ? plain(anyVal(c!.limit)) : "";
                return (
                  <li
                    key={name}
                    className="flex items-center gap-2.5 rounded-md px-2 py-[5px]"
                    style={failed ? { background: STATUS.critical + "1a" } : undefined}
                    title={!reached ? "not reached: the gate short-circuits at the first veto" : `${name}: observed ${obs} · limit ${lim} ${c!.unit}`}
                  >
                    <span className="w-4 shrink-0 text-right font-mono text-[10.5px] tabular-nums text-gray-600">{i + 1}</span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-[12.5px]" style={{ color: reached ? "#e5e7eb" : "#6b7280" }}>
                        {name}
                      </div>
                      {reached ? (
                        <div className="truncate font-mono text-[11px] tabular-nums text-gray-500">
                          <span style={{ color: failed ? STATUS.critical : "#9ca3af" }}>{obs}</span>
                          <span className="text-gray-600"> / </span>
                          {lim}
                          {c!.unit ? <span className="text-gray-600"> {c!.unit}</span> : null}
                        </div>
                      ) : (
                        <div className="text-[11px] text-gray-600">not reached</div>
                      )}
                    </div>
                    <span className="inline-flex shrink-0" style={{ color }}>
                      {!reached ? <Minus size={13} /> : failed ? <X size={14} strokeWidth={3} /> : <Check size={14} strokeWidth={3} />}
                    </span>
                  </li>
                );
              })}
            </ol>
          </>
        ) : (
          <div className="flex h-32 items-center justify-center text-[12.5px] text-gray-500">no decisions yet</div>
        )}

        {sorted.length > 1 ? (
          <div className="mt-auto border-t border-ink-700 pt-3">
            <Label className="mb-1.5">history</Label>
            <ul className="flex max-h-36 flex-col overflow-y-auto">
              {sorted.map((x) => (
                <li key={x.id}>
                  <button
                    onClick={() => setPicked(x.id === d?.id ? null : x.id)}
                    className="flex w-full items-center gap-2 rounded px-1.5 py-[3px] text-left font-mono text-[12px] hover:bg-ink-800"
                    style={{
                      color: x.approved ? "#d1d5db" : STATUS.critical,
                      background: x.id === d?.id ? "#111827" : undefined,
                    }}
                  >
                    <span className="text-gray-500">{clock(x.ts)}</span>
                    <span className="font-semibold">{x.approved ? "OK" : "VETO"}</span>
                    <span className="truncate">{x.approved ? "" : x.code}</span>
                    {x.dry_run ? <span className="text-gray-500">dry</span> : null}
                    <span className="ml-auto tabular-nums text-gray-500">{us(x.latency_us, 1)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
