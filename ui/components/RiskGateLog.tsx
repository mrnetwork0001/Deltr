"use client";
// Latest gate decision with the full check matrix in CHECK_ORDER, plus history.
// Fonts >= 13 px: the video zooms on this panel.
import { useState } from "react";
import { Check, X, Minus } from "lucide-react";
import type { RiskDecisionRecord, SystemStatus } from "@/lib/types";
import { STATUS, anyVal, clock, us } from "@/lib/format";
import { postResetHalt } from "@/lib/api";
import { Panel, StatusTag, ddKind } from "@/components/StatusBar";

/** Matrix cells stay one line: long observed values (dicts, side strings) are cut; the full text is in the row title. */
function short(v: string, max = 14): string {
  if (v.startsWith("{") || v.startsWith("[")) {
    const inner = v.replace(/^[{[]|[}\]]$/g, "").replace(/"/g, "");
    return inner.length > max ? inner.slice(0, max - 1) + "…" : inner;
  }
  return v.length > max ? v.slice(0, max - 1) + "…" : v;
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
      className="text-[13px]"
      right={
        <span className="font-mono tabular-nums" title="measured median of 10k evaluate() calls at startup">
          median {us(status?.gate_median_us)}
        </span>
      }
    >
      <div className="flex h-full flex-col gap-2">
        {ddState !== "NORMAL" || status?.kill_switch ? (
          <div
            className="flex items-center gap-2 rounded border px-2 py-1"
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
                className="ml-auto rounded border border-ink-700 bg-ink-800 px-2 py-[2px] text-[13px] text-gray-200 hover:border-gray-500 disabled:opacity-50"
              >
                reset halt
              </button>
            ) : null}
          </div>
        ) : null}
        {msg ? <div className="text-[13px] text-gray-400">{msg}</div> : null}

        {d ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <StatusTag kind={d.approved ? "good" : "critical"} big>
                {d.approved ? "APPROVED" : "VETO"}
              </StatusTag>
              <span className="font-mono text-[14px] font-bold text-gray-100">{d.code}</span>
              {d.dry_run ? <span className="rounded border border-ink-700 px-1 text-[13px] text-gray-400">dry run</span> : null}
              <span className="ml-auto font-mono text-[13px] tabular-nums text-gray-400" title={`${d.latency_ns} ns`}>
                {us(d.latency_us, 2)}
              </span>
            </div>
            <div className="text-[13px] leading-5 text-gray-300">{d.reason}</div>
            {!d.approved ? (
              <div className="font-mono text-[13px] tabular-nums text-gray-200">
                observed <span style={{ color: STATUS.critical }}>{anyVal(d.observed)}</span> · limit {anyVal(d.limit)} {d.unit}
              </div>
            ) : null}

            <table className="w-full table-fixed text-[13px]">
              <colgroup>
                <col style={{ width: "44%" }} />
                <col style={{ width: "21%" }} />
                <col style={{ width: "17%" }} />
                <col style={{ width: "10%" }} />
                <col style={{ width: "8%" }} />
              </colgroup>
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500">
                  <th className="py-0.5 font-medium">check</th>
                  <th className="py-0.5 text-right font-medium">observed</th>
                  <th className="py-0.5 text-right font-medium">limit</th>
                  <th className="py-0.5 pl-1 font-medium">unit</th>
                  <th className="py-0.5 text-center font-medium"> </th>
                </tr>
              </thead>
              <tbody className="font-mono tabular-nums">
                {order.map((name) => {
                  const c = byName.get(name);
                  const failed = c ? !c.passed : false;
                  const reached = !!c;
                  const color = !reached ? "#6b7280" : failed ? STATUS.critical : STATUS.good;
                  const obs = reached ? anyVal(c!.observed) : "·";
                  const lim = reached ? anyVal(c!.limit) : "·";
                  return (
                    <tr
                      key={name}
                      className="border-t border-ink-700"
                      style={failed ? { background: STATUS.critical + "1f" } : undefined}
                      title={
                        !reached
                          ? "not reached: the gate short-circuits at the first veto"
                          : `${name}: observed ${obs} · limit ${lim} ${c!.unit}`
                      }
                    >
                      <td className="truncate py-[3px] pr-1" style={{ color: reached ? "#e5e7eb" : "#6b7280" }}>
                        {name}
                      </td>
                      <td className="truncate py-[3px] text-right" style={{ color: failed ? STATUS.critical : "#d1d5db" }}>
                        {short(obs)}
                      </td>
                      <td className="truncate py-[3px] text-right text-gray-400">{short(lim)}</td>
                      <td className="truncate py-[3px] pl-1 text-gray-500">{reached ? c!.unit : ""}</td>
                      <td className="py-[3px] text-center">
                        <span className="inline-flex" style={{ color }}>
                          {!reached ? <Minus size={13} /> : failed ? <X size={14} strokeWidth={3} /> : <Check size={14} strokeWidth={3} />}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        ) : (
          <div className="flex h-32 items-center justify-center text-gray-500">no decisions yet</div>
        )}

        {sorted.length > 1 ? (
          <div className="mt-auto border-t border-ink-700 pt-2">
            <div className="mb-1 text-[11px] uppercase tracking-wide text-gray-500">history</div>
            <ul className="flex max-h-40 flex-col gap-[2px] overflow-y-auto">
              {sorted.map((x) => (
                <li key={x.id}>
                  <button
                    onClick={() => setPicked(x.id === d?.id ? null : x.id)}
                    className="flex w-full items-center gap-2 rounded px-1 py-[2px] text-left font-mono text-[13px] hover:bg-ink-800"
                    style={{
                      color: x.approved ? "#d1d5db" : STATUS.critical,
                      background: x.id === d?.id ? "#111827" : undefined,
                    }}
                  >
                    <span className="text-gray-500">{clock(x.ts)}</span>
                    <span className="font-semibold">{x.approved ? "OK" : "VETO"}</span>
                    <span>{x.approved ? "" : x.code}</span>
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
