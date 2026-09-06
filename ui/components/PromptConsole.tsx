"use client";
// Prompt input + canonical chips, PromptResult stepper -> Execute, stress row,
// min-edge slider, kill switch. Every POST goes through lib/api with X-Deltr-Source: ui.
import { useEffect, useState } from "react";
import { FlaskConical, Power, Send } from "lucide-react";
import type { PromptResult, StressKind, SystemStatus } from "@/lib/types";
import { STATUS, bps, lev, qty, usd } from "@/lib/format";
import { DeltrApiError, postExecute, postKill, postMinEdge, postPrompt, postStress } from "@/lib/api";
import { Chip, Panel, StatusTag } from "@/components/StatusBar";

const CANONICAL = [
  "Rebalance $5,000 USDC into delta-neutral BNB arbitrage.",
  "Do it again with $50,000 at 10x.",
  "Fine, $50,000 at 3x.",
  "Hedge $2,000.",
  "Kill switch on.",
  "Kill switch off.",
  "Reset halt.",
  "Unwind all.",
];

const STRESS: Array<{ kind: StressKind; magnitude: number; label: string; title: string }> = [
  { kind: "basis_shock", magnitude: 130, label: "basis shock 130 bps", title: "adverse basis move applied to open positions' mark-to-close" },
  { kind: "equity_shock", magnitude: 3.5, label: "equity shock 3.5 %", title: "removes 3.5 % of equity to exercise the drawdown halt" },
  { kind: "dex_leg_fail", magnitude: 0, label: "DEX-leg fail", title: "next DEX leg raises: exercises reverse-on-failure" },
  { kind: "funding_flip", magnitude: -0.0003, label: "funding flip -0.03 %", title: "new funding rate applied to the paper accrual" },
  { kind: "feed_stale", magnitude: 0, label: "feed stale", title: "freezes freshness ages: STALE_QUOTE" },
];

function errText(e: unknown): string {
  if (e instanceof DeltrApiError) return `${e.code}: ${e.message}`;
  return e instanceof Error ? e.message : String(e);
}

export interface PromptConsoleProps {
  status: SystemStatus | null;
  mock: boolean;
  onTrace: (id: string) => void;
  onChanged?: () => void;
}

export default function PromptConsole({ status, mock, onTrace, onChanged }: PromptConsoleProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PromptResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [stressBusy, setStressBusy] = useState<string | null>(null);
  const [stressMsg, setStressMsg] = useState<string | null>(null);
  const [minEdge, setMinEdge] = useState<number>(status?.min_edge_bps ?? 3);
  const [minEdgeMsg, setMinEdgeMsg] = useState<string | null>(null);
  useEffect(() => {
    if (status && Number.isFinite(status.min_edge_bps)) setMinEdge(status.min_edge_bps);
  }, [status?.min_edge_bps, status]);

  const testnet = status?.mode === "testnet";
  const floor = testnet ? Math.ceil(status?.min_edge_floor_bps ?? 0) : -50;
  const stress = status?.stress_active ?? null;

  const send = async (t: string) => {
    const s = t.trim();
    if (!s) return;
    setBusy(true);
    setError(null);
    setReceiptId(null);
    try {
      const r = await postPrompt(s);
      setResult(r);
      if (r.plan_id) onTrace(r.plan_id);
      onChanged?.();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const execute = async () => {
    if (!result?.plan_id) return;
    setBusy(true);
    setError(null);
    try {
      const rc = await postExecute({ plan_id: result.plan_id, confirm: testnet ? confirm : undefined });
      setReceiptId(rc.id);
      onTrace(rc.id);
      onChanged?.();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const runStress = async (kind: StressKind, magnitude: number, label: string) => {
    setStressBusy(kind);
    setStressMsg(null);
    try {
      const r = await postStress({ kind, magnitude, label });
      setStressMsg(
        kind === "reset"
          ? "stress reset"
          : `${label}: equity ${usd(r.equity_before)} → ${usd(r.equity_after)}, dd ${r.drawdown_pct.toFixed(2)} % ${r.dd_state}${r.stops_fired.length ? `, stops fired: ${r.stops_fired.join(", ")}` : ""}`,
      );
      onChanged?.();
    } catch (e) {
      setStressMsg(errText(e));
    } finally {
      setStressBusy(null);
    }
  };

  const commitMinEdge = async (v: number) => {
    setMinEdgeMsg(null);
    try {
      const r = await postMinEdge({ min_edge_bps: v });
      setMinEdgeMsg(`min edge ${bps(r.min_edge_bps)} (floor ${bps(r.floor_bps)}, ${r.mode})`);
      onChanged?.();
    } catch (e) {
      setMinEdgeMsg(errText(e));
    }
  };

  const kill = async (on: boolean) => {
    try {
      await postKill({ on, reason: "ui" });
      onChanged?.();
    } catch (e) {
      setError(errText(e));
    }
  };

  const pre = result?.precheck ?? null;
  const stepper: Array<{ label: string; ok: boolean | null; text: string }> = result
    ? [
        {
          label: "intent",
          ok: result.intent.action !== "unknown",
          text: `${result.intent.action}${result.intent.capital_usd ? ` ${usd(result.intent.capital_usd, 0)}` : ""}${result.intent.leverage ? ` ${lev(result.intent.leverage)}` : ""}`,
        },
        {
          label: "sizing",
          ok: result.plan ? true : null,
          text: result.plan ? `${qty(result.plan.qty)} · notional ${usd(result.plan.notional_usd, 0)} · cash ${usd(result.plan.cash_required_usd, 0)}` : "–",
        },
        { label: "plan", ok: result.plan ? true : null, text: result.plan_id ?? "–" },
        {
          label: "precheck",
          ok: pre ? pre.approved : null,
          text: pre ? `${pre.approved ? "APPROVED" : `VETO ${pre.code}`} · ${pre.latency_us} µs` : "–",
        },
      ]
    : [];

  return (
    <Panel
      title="Prompt console"
      right={
        <>
          {stress ? (
            <Chip color={STATUS.critical} solid>
              <FlaskConical size={11} /> SIMULATED · {stress}
            </Chip>
          ) : null}
          {mock ? <span className="text-gray-500">backend offline: controls disabled</span> : null}
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-2">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void send(text);
            }}
            className="flex gap-2"
          >
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder='e.g. "Rebalance $5,000 USDC into delta-neutral BNB arbitrage."'
              disabled={mock || busy}
              className="flex-1 rounded-md border border-ink-700 bg-ink-950 px-3 py-2 text-[13px] text-gray-100 placeholder:text-gray-600 focus:border-cake focus:outline-none disabled:opacity-60"
            />
            <button
              type="submit"
              disabled={mock || busy || !text.trim()}
              className="inline-flex items-center gap-1.5 rounded-md border border-cake bg-cake/10 px-3.5 py-2 text-[13px] font-semibold text-cake transition hover:bg-cake/20 disabled:opacity-40"
            >
              <Send size={13} /> propose
            </button>
          </form>
          <div className="flex flex-wrap gap-1">
            {CANONICAL.map((c) => (
              <button
                key={c}
                onClick={() => setText(c)}
                disabled={mock}
                className="rounded border border-ink-700 bg-ink-800 px-2 py-[2px] text-[11px] text-gray-300 hover:border-gray-500 disabled:opacity-60"
              >
                {c}
              </button>
            ))}
          </div>
          {error ? (
            <div className="rounded border px-2 py-1 text-xs" style={{ borderColor: STATUS.critical + "80", color: STATUS.critical }}>
              {error}
            </div>
          ) : null}
          {result ? (
            <div className="rounded border border-ink-700 bg-ink-950/60 p-2">
              <ol className="grid grid-cols-4 gap-2">
                {stepper.map((s, i) => (
                  <li key={s.label} className="flex flex-col gap-1 text-xs">
                    <div className="flex items-center gap-1">
                      <span
                        className="inline-flex h-4 w-4 items-center justify-center rounded-full font-mono text-[10px] font-bold"
                        style={{
                          background: s.ok === null ? "#1f2937" : s.ok ? STATUS.good : STATUS.critical,
                          color: s.ok === null ? "#9ca3af" : "#07090f",
                        }}
                      >
                        {i + 1}
                      </span>
                      <span className="uppercase tracking-wide text-gray-500">{s.label}</span>
                    </div>
                    <span className="font-mono tabular-nums text-gray-200">{s.text}</span>
                  </li>
                ))}
              </ol>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-300">
                <span>{result.message}</span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-3">
                {result.plan_id && pre?.approved ? (
                  <>
                    <button
                      onClick={execute}
                      disabled={busy || (testnet && !confirm) || !!receiptId}
                      className="rounded px-3 py-1 text-[13px] font-bold disabled:opacity-40"
                      style={{ background: STATUS.good, color: "#07090f" }}
                    >
                      Execute plan
                    </button>
                    {testnet ? (
                      <label className="flex items-center gap-1 text-xs text-gray-300">
                        <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> confirm real testnet order
                      </label>
                    ) : null}
                  </>
                ) : pre ? (
                  <StatusTag kind="critical">VETO {pre.code}: nothing to execute</StatusTag>
                ) : null}
                {receiptId ? (
                  <a
                    href="#trace"
                    onClick={(e) => {
                      e.preventDefault();
                      onTrace(receiptId);
                      document.getElementById("trace")?.scrollIntoView({ behavior: "smooth" });
                    }}
                    className="font-mono text-xs text-cake hover:underline"
                  >
                    receipt {receiptId}
                  </a>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>

        <div className="grid gap-4 border-t border-ink-700 pt-4 text-xs">
          <div>
            <div className="mb-1.5 flex items-center gap-2 text-[10.5px] font-medium uppercase tracking-[0.12em] text-gray-500">
              stress (labelled SIMULATED, feeds untouched)
            </div>
            <div className="flex flex-wrap gap-1">
              {STRESS.map((s) => (
                <button
                  key={s.kind}
                  title={s.title}
                  disabled={mock || !!stressBusy}
                  onClick={() => runStress(s.kind, s.magnitude, s.label)}
                  className="rounded border px-2 py-[2px] font-mono text-[11px] disabled:opacity-40"
                  style={{ borderColor: STATUS.critical + "80", color: STATUS.critical, background: STATUS.critical + "0f" }}
                >
                  {s.label}
                </button>
              ))}
              <button
                disabled={mock || !!stressBusy}
                onClick={() => runStress("reset", 0, "reset")}
                className="rounded border border-ink-700 bg-ink-800 px-2 py-[2px] font-mono text-[11px] text-gray-200 disabled:opacity-40"
              >
                RESET
              </button>
            </div>
            {stressMsg ? <div className="mt-1 text-gray-400">{stressMsg}</div> : null}
          </div>

          <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
          <div>
            <div className="mb-1.5 flex items-center justify-between text-[10.5px] font-medium uppercase tracking-[0.12em] text-gray-500">
              <span>min edge (scout + gate)</span>
              <span className="font-mono normal-case tabular-nums text-gray-200">
                {bps(minEdge)}
                {minEdge < 0 ? (
                  <span className="ml-1" style={{ color: STATUS.warning }}>
                    override
                  </span>
                ) : null}
              </span>
            </div>
            <input
              type="range"
              min={floor}
              max={50}
              step={1}
              value={Math.max(floor, Math.min(50, minEdge))}
              disabled={mock}
              onChange={(e) => setMinEdge(Number(e.target.value))}
              onMouseUp={() => commitMinEdge(minEdge)}
              onTouchEnd={() => commitMinEdge(minEdge)}
              onKeyUp={() => commitMinEdge(minEdge)}
              className="w-full accent-[#F0B90B]"
            />
            <div className="flex justify-between font-mono text-[10px] text-gray-600">
              <span>{floor} {testnet ? "(floor: measured round trip)" : ""}</span>
              <span>+50</span>
            </div>
            {minEdgeMsg ? <div className="text-gray-400">{minEdgeMsg}</div> : null}
          </div>

          <div className="flex flex-col gap-1.5">
            <span className="text-[10.5px] font-medium uppercase tracking-[0.12em] text-gray-500">kill switch</span>
            <button
              disabled={mock}
              onClick={() => kill(!status?.kill_switch)}
              className="inline-flex w-fit items-center gap-1.5 rounded-md border px-2.5 py-1 font-semibold disabled:opacity-40"
              style={
                status?.kill_switch
                  ? { borderColor: STATUS.critical, color: "#07090f", background: STATUS.critical }
                  : { borderColor: "#1f2937", color: "#d1d5db", background: "#111827" }
              }
            >
              <Power size={12} /> {status?.kill_switch ? "ON: click to release" : "off"}
            </button>
          </div>
          </div>
        </div>
      </div>
    </Panel>
  );
}
