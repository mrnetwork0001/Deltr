"use client";
// Open positions with the delta gauge, mark-to-close PnL split and unwind.
import { useState } from "react";
import type { DataSource, ExecutionReceipt, MarketState, Position, PortfolioSnapshot, SystemStatus } from "@/lib/types";
import { STATUS, SURFACE, bps, clock, isNum, lev, px, qty, usd, usdSigned } from "@/lib/format";
import { postUnwind } from "@/lib/api";
import { Chip, Panel, SourceBadge } from "@/components/StatusBar";

const QTY_STEP = 0.01; // Binance BNBUSDT LOT_SIZE step; the ±1-step band on the gauge

function DeltaGauge({ delta, price }: { delta: number; price: number | null }) {
  const W = 150;
  const H = 14;
  const range = QTY_STEP * 4; // ±4 steps visible
  const x = (v: number) => (W / 2) * (1 + Math.max(-1, Math.min(1, v / range)));
  const neutral = Math.abs(delta) <= QTY_STEP + 1e-9;
  const color = neutral ? STATUS.good : Math.abs(delta) <= 2 * QTY_STEP ? STATUS.warning : STATUS.critical;
  return (
    <div className="flex items-center gap-2" title={`delta ${delta.toFixed(4)} BNB; band = ±1 lot step (${QTY_STEP})`}>
      <svg width={W} height={H} role="img" aria-label="delta gauge">
        <rect x={0} y={5} width={W} height={4} rx={2} fill={SURFACE.raised} />
        <rect x={x(-QTY_STEP)} y={3} width={x(QTY_STEP) - x(-QTY_STEP)} height={8} rx={2} fill={STATUS.good} opacity={0.25} />
        <line x1={W / 2} x2={W / 2} y1={1} y2={H - 1} stroke={SURFACE.text2} strokeWidth={1} />
        <circle cx={x(delta)} cy={7} r={4.5} fill={color} stroke={SURFACE.card} strokeWidth={2} />
      </svg>
      <span className="font-mono text-xs tabular-nums text-gray-100">{delta.toFixed(2)} BNB</span>
      <span className="font-mono text-[11px] tabular-nums text-gray-500">{price ? usdSigned(delta * price) : ""}</span>
    </div>
  );
}

function breakevenSettlements(p: Position, rate: number | null, intervalH: number): string {
  if (!isNum(rate) || rate <= 0) return "n/a (funding ≤ 0)";
  const perSettle = rate * p.notional_usd; // short receives when rate > 0
  if (perSettle <= 0) return "n/a";
  if (p.unrealized_pnl_usd >= 0) return "already positive";
  const n = Math.ceil(-p.unrealized_pnl_usd / perSettle);
  const hours = n * intervalH;
  return `${n} settlement${n === 1 ? "" : "s"} (${hours / 24 >= 1 ? `${(hours / 24).toFixed(1)} d` : `${hours} h`})`;
}

/** Provenance of a leg's fill: the receipt that opened the position knows; the executor tags DEX fills paper in every mode. */
function fillSource(p: Position, receipts: ExecutionReceipt[], leg: 0 | 1, fallback: DataSource): DataSource {
  const r = receipts.find((x) => x.plan_id === p.plan_id);
  return r?.fills.find((f) => f.leg_index === leg)?.source ?? fallback;
}

export interface PositionsTableProps {
  positions: Position[];
  portfolio: PortfolioSnapshot | null;
  market: MarketState | null;
  status: SystemStatus | null;
  receipts?: ExecutionReceipt[];
  mock: boolean;
  onReceipt?: (id: string) => void;
}

export default function PositionsTable({ positions, portfolio, market, status, receipts = [], mock, onReceipt }: PositionsTableProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const open = positions.filter((p) => p.status === "open");
  const rateNow = market?.funding?.last_funding_rate ?? null;
  const intervalH = market?.funding?.interval_h ?? 8;
  const mark = market?.perp_ref_price ?? null;
  const testnet = status?.mode === "testnet";

  const unwind = async (id: string) => {
    if (!window.confirm(`Unwind ${id}? Perp reduce-only BUY first, then DEX sell.`)) return;
    setBusy(id);
    setErr(null);
    try {
      const rs = await postUnwind({ position_id: id, reason: "ui unwind", confirm: testnet ? true : undefined });
      if (rs[0]?.id) onReceipt?.(rs[0].id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel
      title="Positions"
      right={
        <span className="font-mono tabular-nums">
          {open.length} open · mark {px(mark)} <SourceBadge source={market?.funding?.source ?? "binance-futures-testnet"} />
        </span>
      }
    >
      <div className="flex h-full flex-col gap-3">
        {open.length ? (
          open.map((p) => {
            const stopPct = p.allocated_risk_usd > 0 ? Math.max(0, Math.min(1, p.stop_distance_usd / p.allocated_risk_usd)) : 0;
            const stopColor = stopPct > 0.5 ? STATUS.good : stopPct > 0.2 ? STATUS.warning : STATUS.critical;
            return (
              <div key={p.id} className="rounded border border-ink-700 bg-ink-950/60 p-2 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-gray-100">{p.symbol}</span>
                  <span className="font-mono text-gray-500">{p.id}</span>
                  <Chip color="#9ca3af">{lev(p.leverage)}</Chip>
                  {p.stress_applied ? (
                    <Chip color={STATUS.critical} solid>
                      SIMULATED · {p.stress_applied}
                    </Chip>
                  ) : null}
                  <span className="ml-auto text-gray-500">opened {clock(p.opened_at)}</span>
                  <button
                    onClick={() => unwind(p.id)}
                    disabled={busy === p.id || mock}
                    title={mock ? "backend offline" : "reduce-only unwind through the gate"}
                    className="rounded border px-2 py-[2px] font-semibold disabled:opacity-40"
                    style={{ borderColor: STATUS.serious + "99", color: STATUS.serious }}
                  >
                    {busy === p.id ? "unwinding…" : "Unwind"}
                  </button>
                </div>

                <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 font-mono tabular-nums">
                  <div className="flex items-center gap-1.5">
                    <span className="inline-block h-2 w-2 rounded-sm" style={{ background: "#d95926" }} />
                    <span className="text-gray-400">DEX long</span>
                    <span className="text-gray-100">{qty(p.dex_qty)}</span>
                    <span className="text-gray-500">@ {px(p.dex_entry)}</span>
                    <SourceBadge source={fillSource(p, receipts, 0, "paper")} />
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="inline-block h-2 w-2 rounded-sm" style={{ background: "#3987e5" }} />
                    <span className="text-gray-400">perp short</span>
                    <span className="text-gray-100">{qty(p.perp_qty)}</span>
                    <span className="text-gray-500">@ {px(p.perp_entry)}</span>
                    <SourceBadge source={fillSource(p, receipts, 1, testnet ? "binance-futures-testnet" : "paper")} />
                  </div>
                </div>

                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span className="text-[11px] uppercase tracking-wide text-gray-500">delta</span>
                  <DeltaGauge delta={p.delta_base} price={mark} />
                  <span className="text-gray-500">entry basis {bps(p.basis_entry_bps)}</span>
                </div>

                <div className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-[2px]">
                  <span className="text-[11px] uppercase tracking-wide text-gray-500">mark-to-close</span>
                  <span className="font-mono text-sm font-semibold tabular-nums" style={{ color: p.unrealized_pnl_usd >= 0 ? STATUS.good : STATUS.critical }}>
                    {usdSigned(p.unrealized_pnl_usd)}
                    <span className="ml-2 text-[11px] font-normal text-gray-500">net of est. exit {usd(p.est_exit_cost_usd)}</span>
                  </span>
                  <span className="text-gray-500">split</span>
                  <span className="font-mono tabular-nums text-gray-300">
                    spread {usdSigned(p.unrealized_pnl_usd - p.funding_accrued_usd + p.est_exit_cost_usd)} · funding {usdSigned(p.funding_accrued_usd)} · est. exit{" "}
                    {usdSigned(-p.est_exit_cost_usd)}
                  </span>
                  <span className="text-gray-500">breakeven</span>
                  <span className="font-mono tabular-nums text-gray-300">
                    in {breakevenSettlements(p, rateNow, intervalH)} <SourceBadge source={market?.funding?.source ?? "binance-futures-testnet"} />
                  </span>
                  <span className="text-gray-500">stop distance</span>
                  <span className="flex items-center gap-2 font-mono tabular-nums text-gray-300">
                    <span className="relative h-1.5 w-20 rounded-sm bg-ink-800">
                      <span className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${stopPct * 100}%`, background: stopColor }} />
                    </span>
                    {usd(p.stop_distance_usd)} of {usd(p.allocated_risk_usd)} allocated
                  </span>
                  <span className="text-gray-500">liq. estimate</span>
                  <span className="font-mono tabular-nums text-gray-300">
                    {px(p.liq_price_est)} <span className="text-gray-500">({mark ? bps((1e4 * (p.liq_price_est - mark)) / mark, 0) : "–"} from mark)</span>
                  </span>
                </div>
              </div>
            );
          })
        ) : (
          <div className="flex h-24 items-center justify-center text-xs text-gray-500">no open positions</div>
        )}
        {err ? <div className="text-xs" style={{ color: STATUS.critical }}>{err}</div> : null}

        <div className="mt-auto grid grid-cols-3 gap-x-3 gap-y-1 border-t border-ink-700 pt-2 text-[11px] text-gray-400">
          <div>
            equity <span className="font-mono tabular-nums text-gray-100">{usd(portfolio?.equity_usd, 2)}</span>
          </div>
          <div>
            cash <span className="font-mono tabular-nums text-gray-200">{usd(portfolio?.cash_usd, 0)}</span>
          </div>
          <div>
            reserved <span className="font-mono tabular-nums text-gray-200">{usd(portfolio?.reserved_cash_usd, 0)}</span>
          </div>
          <div>
            total pnl{" "}
            <span className="font-mono tabular-nums" style={{ color: (portfolio?.total_pnl_usd ?? 0) >= 0 ? STATUS.good : STATUS.critical }}>
              {usdSigned(portfolio?.total_pnl_usd)}
            </span>
          </div>
          <div>
            spread <span className="font-mono tabular-nums text-gray-200">{usdSigned(portfolio?.spread_pnl_usd)}</span> · funding{" "}
            <span className="font-mono tabular-nums text-gray-200">{usdSigned(portfolio?.funding_pnl_usd)}</span>
          </div>
          <div>
            fees <span className="font-mono tabular-nums text-gray-200">{usd(portfolio?.fees_paid_usd)}</span> · today{" "}
            <span className="font-mono tabular-nums text-gray-200">{usdSigned(portfolio?.daily_realized_usd)}</span>
          </div>
        </div>
      </div>
    </Panel>
  );
}
