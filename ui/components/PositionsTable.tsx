"use client";
// Open positions: a header row, the two legs side by side, the headline metrics
// (mark-to-close, delta, entry basis) and the detail rows, then the portfolio totals.
import { useState } from "react";
import type { DataSource, ExecutionReceipt, MarketState, Position, PortfolioSnapshot, SystemStatus } from "@/lib/types";
import { SERIES, STATUS, SURFACE, bps, clock, isNum, lev, px, qty, usd, usdSigned } from "@/lib/format";
import { postUnwind } from "@/lib/api";
import { Chip, Field, Label, Panel, SourceBadge } from "@/components/StatusBar";

const QTY_STEP = 0.01; // Binance BNBUSDT LOT_SIZE step; the ±1-step band on the gauge

function DeltaGauge({ delta }: { delta: number }) {
  const W = 120;
  const H = 14;
  const range = QTY_STEP * 4; // ±4 steps visible
  const x = (v: number) => (W / 2) * (1 + Math.max(-1, Math.min(1, v / range)));
  const neutral = Math.abs(delta) <= QTY_STEP + 1e-9;
  const color = neutral ? STATUS.good : Math.abs(delta) <= 2 * QTY_STEP ? STATUS.warning : STATUS.critical;
  return (
    <svg width={W} height={H} role="img" aria-label="delta gauge" className="block">
      <rect x={0} y={5} width={W} height={4} rx={2} fill={SURFACE.raised} />
      <rect x={x(-QTY_STEP)} y={3} width={x(QTY_STEP) - x(-QTY_STEP)} height={8} rx={2} fill={STATUS.good} opacity={0.25} />
      <line x1={W / 2} x2={W / 2} y1={1} y2={H - 1} stroke={SURFACE.text2} strokeWidth={1} />
      <circle cx={x(delta)} cy={7} r={4.5} fill={color} stroke={SURFACE.card} strokeWidth={2} />
    </svg>
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

function Leg({ color, label, venue, q, price, source }: { color: string; label: string; venue: string; q: number; price: number; source: DataSource }) {
  return (
    <div className="flex min-w-0 gap-2.5 px-3 py-2.5">
      <span aria-hidden className="mt-0.5 h-9 w-1 shrink-0 rounded-full" style={{ background: color }} />
      <div className="min-w-0">
        <Label>
          {label} <span className="normal-case tracking-normal text-gray-600">· {venue}</span>
        </Label>
        <div className="mt-0.5 truncate font-mono text-[14px] font-semibold tabular-nums text-gray-100">
          {qty(q)} <span className="text-[12px] font-normal text-gray-400">@ {px(price)}</span>
        </div>
        <div className="mt-1">
          <SourceBadge source={source} />
        </div>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-[11px] uppercase tracking-[0.1em] text-gray-500">{label}</dt>
      <dd className="min-w-0 font-mono text-[12px] tabular-nums text-gray-300">{children}</dd>
    </>
  );
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
  const pnl = portfolio?.total_pnl_usd ?? 0;

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
      className="h-full"
      right={
        <span className="flex items-center gap-1.5 font-mono tabular-nums">
          {open.length} open · mark {px(mark)} <SourceBadge source={market?.funding?.source ?? "binance-futures-testnet"} />
        </span>
      }
    >
      <div className="flex h-full flex-col gap-3">
        {open.length ? (
          open.map((p) => {
            const stopPct = p.allocated_risk_usd > 0 ? Math.max(0, Math.min(1, p.stop_distance_usd / p.allocated_risk_usd)) : 0;
            const stopColor = stopPct > 0.5 ? STATUS.good : stopPct > 0.2 ? STATUS.warning : STATUS.critical;
            const neutral = Math.abs(p.delta_base) <= QTY_STEP + 1e-9;
            return (
              <div key={p.id} className="rounded-lg border border-ink-700 bg-ink-950/50">
                {/* header */}
                <div className="flex flex-wrap items-center gap-2 border-b border-ink-700/80 px-3 py-2">
                  <span className="text-[15px] font-semibold text-gray-50">{p.symbol}</span>
                  <Chip color="#9ca3af">{lev(p.leverage)}</Chip>
                  <span className="font-mono text-[11px] text-gray-500">{p.id}</span>
                  {p.stress_applied ? (
                    <Chip color={STATUS.critical} solid>
                      SIMULATED · {p.stress_applied}
                    </Chip>
                  ) : null}
                  <span className="ml-auto text-[11px] text-gray-500">opened {clock(p.opened_at)}</span>
                  <button
                    onClick={() => unwind(p.id)}
                    disabled={busy === p.id || mock}
                    title={mock ? "backend offline" : "reduce-only unwind through the gate"}
                    className="rounded-md border px-2.5 py-1 text-[12px] font-semibold transition hover:bg-ink-800 disabled:opacity-40"
                    style={{ borderColor: STATUS.serious + "99", color: STATUS.serious }}
                  >
                    {busy === p.id ? "unwinding…" : "Unwind"}
                  </button>
                </div>

                {/* legs */}
                <div className="grid grid-cols-2 divide-x divide-ink-700/80 border-b border-ink-700/80">
                  <Leg color={SERIES.dex} label="DEX long" venue="PancakeSwap V3" q={p.dex_qty} price={p.dex_entry} source={fillSource(p, receipts, 0, "paper")} />
                  <Leg
                    color={SERIES.perp}
                    label="Perp short"
                    venue="Binance USDⓈ-M"
                    q={p.perp_qty}
                    price={p.perp_entry}
                    source={fillSource(p, receipts, 1, testnet ? "binance-futures-testnet" : "paper")}
                  />
                </div>

                {/* headline metrics */}
                <div className="grid grid-cols-2 gap-3 border-b border-ink-700/80 px-3 py-3 sm:grid-cols-3">
                  <div className="min-w-0">
                    <Label>Mark-to-close</Label>
                    <div className="mt-0.5 font-mono text-[17px] font-semibold tabular-nums" style={{ color: p.unrealized_pnl_usd >= 0 ? STATUS.good : STATUS.critical }}>
                      {usdSigned(p.unrealized_pnl_usd)}
                    </div>
                    <div className="text-[10.5px] text-gray-500">net of est. exit {usd(p.est_exit_cost_usd)}</div>
                  </div>
                  <div className="min-w-0" title={`delta ${p.delta_base.toFixed(4)} BNB; band = ±1 lot step (${QTY_STEP})`}>
                    <Label>Delta</Label>
                    <div className="mt-0.5 font-mono text-[17px] font-semibold tabular-nums" style={{ color: neutral ? STATUS.good : STATUS.warning }}>
                      {p.delta_base.toFixed(2)} <span className="text-[11px] font-normal text-gray-500">BNB</span>
                    </div>
                    <div className="mt-1">
                      <DeltaGauge delta={p.delta_base} />
                    </div>
                  </div>
                  <div className="min-w-0">
                    <Label>Entry basis</Label>
                    <div className="mt-0.5 font-mono text-[17px] font-semibold tabular-nums text-gray-100">{bps(p.basis_entry_bps)}</div>
                    <div className="text-[10.5px] text-gray-500">notional {usd(p.notional_usd, 0)} · margin {usd(p.margin_usd, 0)}</div>
                  </div>
                </div>

                {/* details */}
                <dl className="grid grid-cols-[96px_1fr] items-center gap-x-3 gap-y-2 px-3 py-3">
                  <Row label="Split">
                    spread {usdSigned(p.unrealized_pnl_usd - p.funding_accrued_usd + p.est_exit_cost_usd)} · funding {usdSigned(p.funding_accrued_usd)} · est. exit{" "}
                    {usdSigned(-p.est_exit_cost_usd)}
                  </Row>
                  <Row label="Breakeven">
                    in {breakevenSettlements(p, rateNow, intervalH)} <SourceBadge source={market?.funding?.source ?? "binance-futures-testnet"} />
                  </Row>
                  <Row label="Stop distance">
                    <span className="flex items-center gap-2">
                      <span className="relative h-1.5 w-20 shrink-0 rounded-sm bg-ink-800">
                        <span className="absolute inset-y-0 left-0 rounded-sm" style={{ width: `${stopPct * 100}%`, background: stopColor }} />
                      </span>
                      <span className="truncate">
                        {usd(p.stop_distance_usd)} of {usd(p.allocated_risk_usd)} allocated
                      </span>
                    </span>
                  </Row>
                  <Row label="Liq. estimate">
                    {px(p.liq_price_est)} <span className="text-gray-500">({mark ? bps((1e4 * (p.liq_price_est - mark)) / mark, 0) : "–"} from mark)</span>
                  </Row>
                </dl>
              </div>
            );
          })
        ) : (
          <div className="flex h-28 items-center justify-center rounded-lg border border-dashed border-ink-700 text-[12.5px] text-gray-500">no open positions</div>
        )}
        {err ? (
          <div className="text-[12px]" style={{ color: STATUS.critical }}>
            {err}
          </div>
        ) : null}

        {/* portfolio totals */}
        <div className="mt-auto grid grid-cols-3 gap-x-3 gap-y-3 border-t border-ink-700 pt-3">
          <Field label="Equity">{usd(portfolio?.equity_usd, 2)}</Field>
          <Field label="Cash">{usd(portfolio?.cash_usd, 0)}</Field>
          <Field label="Reserved">{usd(portfolio?.reserved_cash_usd, 0)}</Field>
          <Field label="Total PnL">
            <span style={{ color: pnl >= 0 ? STATUS.good : STATUS.critical }}>{usdSigned(portfolio?.total_pnl_usd)}</span>
          </Field>
          <Field label="Spread · Funding">
            {usdSigned(portfolio?.spread_pnl_usd)} <span className="text-gray-600">·</span> {usdSigned(portfolio?.funding_pnl_usd)}
          </Field>
          <Field label="Fees · Today">
            {usd(portfolio?.fees_paid_usd)} <span className="text-gray-600">·</span> {usdSigned(portfolio?.daily_realized_usd)}
          </Field>
        </div>
      </div>
    </Panel>
  );
}
