// TypeScript mirror of deltr/models.py (the frozen pydantic contract).
// `Snapshot` is the ONLY payload the dashboard consumes (GET /api/snapshot, WS /ws/stream).
// datetime fields are ISO strings; enums are their string values; pydantic
// computed fields (Quote.mid, TradeProposal.is_delta_neutral,
// RiskDecisionRecord.latency_us) ARE present in the JSON.
// Field names are checked against the pydantic JSON schema by tests/test_ui_types.py.

export type Mode = "paper" | "testnet" | "live";
export type ExecutionStyle = "maker" | "taker";
export type LegOrder = "dex_first" | "cex_first";

export type Venue = "binance_futures" | "binance_spot" | "pancakeswap_v3" | "binance_agentic_wallet";
export type Side = "BUY" | "SELL";
export type DataSource =
  | "bsc-mainnet-chain"
  | "binance-futures-mainnet"
  | "binance-futures-testnet"
  | "binance-spot-mirror"
  | "binance-agentic-wallet"
  | "paper"
  | "replay"
  | "simulated";
export type TraceSource = "mcp" | "api" | "ui" | "cli" | "auto";
export type StressKind = "basis_shock" | "equity_shock" | "dex_leg_fail" | "funding_flip" | "feed_stale" | "reset";

export type DdState = "NORMAL" | "WARN" | "HALTED";
export type ReceiptStatus = "filled" | "vetoed" | "failed" | "unwound" | "expired";
export type StepName =
  | "intent"
  | "scan"
  | "plan"
  | "gate"
  | "dex_fill"
  | "cex_fill"
  | "unwind"
  | "position"
  | "receipt"
  | "error";
export type StepStatus = "ok" | "veto" | "error" | "skipped";

export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };
export type JsonObject = { [k: string]: Json };

// --------------------------------------------------------------------------- market data
export interface SymbolFilters {
  symbol: string;
  step_size: number;
  min_qty: number;
  tick_size: number;
  min_notional: number;
  price_precision: number;
  qty_precision: number;
  funding_interval_h: number;
  source: DataSource;
}

export interface Quote {
  venue: Venue;
  symbol: string;
  bid: number;
  ask: number;
  bid_qty: number;
  ask_qty: number;
  ts: string;
  source: DataSource;
  mid: number;
}

export interface DexQuote {
  pool: string;
  fee_tier: number;
  fee_bps: number;
  sqrt_price_x96: number;
  tick: number;
  mid_price: number;
  size_base: number;
  exec_price_buy: number;
  amount_in_usdt: number;
  exec_price_sell: number;
  amount_out_usdt: number;
  impact_bps: number;
  gas_units: number;
  gas_price_wei: number;
  gas_usd: number;
  block: number | null;
  ts: string;
  source: DataSource;
}

export interface FundingSnapshot {
  symbol: string;
  mark_price: number;
  index_price: number;
  last_funding_rate: number;
  next_funding_time_ms: number;
  interval_h: number;
  annualized_pct: number;
  ts: string;
  source: DataSource;
}

export interface Freshness {
  cex_age_ms: number;
  dex_age_ms: number;
  spot_age_ms: number;
  ok: boolean;
  reason: string | null;
}

export interface MarketState {
  symbol: string;
  dex: DexQuote | null;
  cex_perp_book: Quote | null;
  cex_spot_ref: Quote | null;
  funding: FundingSnapshot | null;
  perp_ref_price: number | null;
  freshness: Freshness;
  ts: string;
  source: DataSource;
}

export interface SpreadPoint {
  ts: string;
  dex_exec: number;
  perp_ref: number;
  basis_bps: number;
  net_edge_bps: number;
  funding_rate: number;
  actionable: boolean;
  source: DataSource;
}

// --------------------------------------------------------------------------- edge / opportunity
export interface EdgeComponent {
  label: string;
  bps: number;
  kind: "gain" | "cost" | "net";
}

export interface EdgeBreakdown {
  notional_usd: number;
  basis_entry_bps: number;
  dex_fee_bps: number;
  dex_impact_bps: number;
  perp_slip_bps: number;
  cex_taker_bps: number;
  gas_bps_leg: number;
  roundtrip_cost_bps: number;
  funding_rate_last: number;
  horizon_h: number;
  settlements: number;
  funding_bps_horizon: number;
  basis_exit_assumed_bps: number;
  basis_shock_bps: number;
  net_edge_bps: number;
  expected_edge_usd: number;
  allocated_risk_usd: number;
}

export interface ArbOpportunity {
  id: string;
  symbol: string;
  direction: "long_dex_short_perp";
  dex_price: number;
  perp_price: number;
  edge: EdgeBreakdown;
  size_base: number;
  notional_usd: number;
  horizon_h: number;
  is_actionable: boolean;
  reason: string;
  min_edge_bps_used: number;
  freshness: Freshness;
  ts: string;
}

// --------------------------------------------------------------------------- plans / proposals
export interface OrderLeg {
  venue: Venue;
  symbol: string;
  side: Side;
  qty: number;
  price_hint: number;
  reduce_only: boolean;
  leverage: number;
  client_id: string | null;
  position_side: "BOTH" | "LONG" | "SHORT";
}

export interface HedgePlan {
  id: string;
  opportunity_id: string | null;
  symbol: string;
  legs: OrderLeg[];
  qty: number;
  notional_usd: number;
  leverage: number;
  margin_usd: number;
  cash_required_usd: number;
  allocated_risk_usd: number;
  expected_edge_bps: number;
  roundtrip_cost_bps: number;
  ref_dex_price: number;
  ref_perp_price: number;
  reduce_only: boolean;
  position_id: string | null;
  source: TraceSource;
  client: string | null;
  prompt: string | null;
  created_at: string;
  expires_at: string;
  plan_hash: string;
}

export interface TradeProposal {
  plan_id: string;
  symbol: string;
  dex_side: Side;
  perp_side: Side;
  dex_qty: number;
  perp_qty: number;
  qty_step: number;
  leverage: number;
  notional: number;
  allocated_risk: number;
  roundtrip_cost_bps: number;
  expected_edge_bps: number;
  quote_age_ms: number;
  price_drift_bps: number;
  dex_ref_price: number;
  perp_ref_price: number;
  reduce_only: boolean;
  position_id: string | null;
  is_delta_neutral: boolean;
}

// --------------------------------------------------------------------------- risk
export interface CheckResult {
  name: string;
  passed: boolean;
  observed: Json;
  limit: Json;
  unit: string;
}

export interface RiskDecisionRecord {
  id: string;
  plan_id: string | null;
  approved: boolean;
  code: string;
  reason: string;
  observed: Json;
  limit: Json;
  unit: string;
  checks: CheckResult[];
  latency_ns: number;
  dd_state: DdState;
  drawdown_pct: number;
  halted: boolean;
  kill_switch: boolean;
  mode: Mode;
  dry_run: boolean;
  ts: string;
  latency_us: number;
}

// --------------------------------------------------------------------------- execution
export interface Fill {
  leg_index: number;
  venue: Venue;
  symbol: string;
  side: Side;
  qty: number;
  price: number;
  fee_usd: number;
  ref: string;
  simulated: boolean;
  source: DataSource;
  client_id: string | null;
  attempt: number;
  reference_divergence_bps: number | null;
  latency_ms: number;
  ts: string;
}

export interface TraceStep {
  step: StepName;
  status: StepStatus;
  summary: string;
  data: JsonObject;
  latency_ms: number | null;
  ts: string;
}

export interface ExecutionReceipt {
  id: string;
  plan_id: string;
  source: TraceSource;
  client: string | null;
  prompt: string | null;
  mode: Mode;
  status: ReceiptStatus;
  decision: RiskDecisionRecord;
  plan: HedgePlan;
  fills: Fill[];
  position_id: string | null;
  residual_delta_base: number;
  realized_cost_usd: number;
  legging_window_ms: number | null;
  steps: TraceStep[];
  stress_active: string | null;
  sha256: string;
  ts: string;
}

// --------------------------------------------------------------------------- portfolio
export interface Position {
  id: string;
  plan_id: string;
  symbol: string;
  dex_qty: number;
  dex_entry: number;
  perp_qty: number;
  perp_entry: number;
  leverage: number;
  margin_usd: number;
  notional_usd: number;
  allocated_risk_usd: number;
  basis_entry_bps: number;
  opened_at: string;
  funding_accrued_usd: number;
  unrealized_pnl_usd: number;
  est_exit_cost_usd: number;
  realized_pnl_usd: number;
  delta_base: number;
  stop_distance_usd: number;
  liq_price_est: number;
  status: "open" | "closed";
  stress_applied: string | null;
  closed_at: string | null;
  close_reason: string | null;
}

export interface PortfolioSnapshot {
  equity_usd: number;
  cash_usd: number;
  reserved_cash_usd: number;
  peak_equity_usd: number;
  drawdown_pct: number;
  dd_state: DdState;
  total_pnl_usd: number;
  spread_pnl_usd: number;
  funding_pnl_usd: number;
  fees_paid_usd: number;
  open_positions: number;
  daily_realized_usd: number;
  equity_curve: [string, number][];
  ts: string;
}

// --------------------------------------------------------------------------- intents / prompts
export type IntentAction =
  | "rebalance"
  | "hedge"
  | "scan"
  | "explain"
  | "unwind"
  | "status"
  | "kill"
  | "reset_halt"
  | "stress"
  | "set_min_edge"
  | "unknown";

export interface Intent {
  action: IntentAction;
  capital_usd: number | null;
  leverage: number | null;
  symbol: string;
  position_id: string | null;
  stress_kind: StressKind | null;
  magnitude: number | null;
  min_edge_bps: number | null;
  stablecoin_note: string | null;
  confidence: number;
  raw: string;
  source: TraceSource;
}

export interface PromptResult {
  intent: Intent;
  opportunity: ArbOpportunity | null;
  plan: HedgePlan | null;
  plan_id: string | null;
  proposal: TradeProposal | null;
  precheck: RiskDecisionRecord | null;
  message: string;
  steps: TraceStep[];
  executes: false;
}

// --------------------------------------------------------------------------- MCP / bridge / status
export interface McpActivity {
  id: string;
  direction: "inbound" | "outbound";
  client: string | null;
  server: "deltr" | "binance-shim" | "binance-official" | "rest";
  tool: string;
  args: JsonObject;
  result_summary: string;
  ok: boolean;
  latency_ms: number;
  trace_id: string | null;
  ts: string;
}

export interface UpstreamStatus {
  kind: "official" | "shim" | "none";
  url: string | null;
  authorized: boolean;
  tools_discovered: string[];
  error: string | null;
  ts: string;
}

export interface VenueHealth {
  name: string;
  ok: boolean;
  age_ms: number;
  source: DataSource;
  detail: string;
}

export interface StressScenario {
  kind: StressKind;
  magnitude: number;
  label: string;
}

export interface StressResult {
  scenario: StressScenario;
  equity_before: number;
  equity_after: number;
  drawdown_pct: number;
  dd_state: DdState;
  halted: boolean;
  positions_affected: number;
  stops_fired: string[];
  active_label: string | null;
  ts: string;
}

export interface AgentEvent {
  topic: string;
  level: "debug" | "info" | "warn" | "error";
  message: string;
  data: JsonObject;
  ts: string;
}

export interface SystemStatus {
  mode: Mode;
  symbol: string;
  version: string;
  uptime_s: number;
  started_at: string;
  halted: boolean;
  kill_switch: boolean;
  dd_state: DdState;
  equity_usd: number;
  drawdown_pct: number;
  open_positions: number;
  venues: VenueHealth[];
  upstream: UpstreamStatus | null;
  secrets_present: boolean;
  binance_api_env: string;
  replay: boolean;
  stress_active: string | null;
  gate_median_us: number;
  min_edge_bps: number;
  min_edge_floor_bps: number;
  leg_order: LegOrder;
  limits: JsonObject;
  check_order: string[];
  /** "maker" | "taker"; null in PAPER, where no order reaches a venue. */
  execution_style: ExecutionStyle | null;
  execution_style_label: string | null;
  /** Market data provenance. Real mainnet in every mode. */
  data_source: string | null;
  /** True ONLY in LIVE, and only after the preflight passed. Real money is at risk. */
  real_funds_armed: boolean;
  /** The Binance Agentic Wallet opt-in is complete (mode + acknowledgement). */
  onchain_armed: boolean;
  /** The wallet's PUBLIC address. Deltr never holds a key. */
  wallet_address: string | null;
  max_notional_usd: number | null;
  max_aggregate_usd: number | null;
  /** Unattended trading: configured on, allowed right now, and why not when it is not. */
  auto_execute?: boolean;
  auto_armed?: boolean;
  auto_note?: string | null;
}

export interface Snapshot {
  status: SystemStatus;
  market: MarketState | null;
  edge: EdgeBreakdown | null;
  opportunity: ArbOpportunity | null;
  history: SpreadPoint[];
  decisions: RiskDecisionRecord[];
  positions: Position[];
  portfolio: PortfolioSnapshot;
  receipts: ExecutionReceipt[];
  prompts: PromptResult[];
  activity: McpActivity[];
  events: AgentEvent[];
  ts: string;
}

// --------------------------------------------------------------------------- API envelopes (section 6.1)
export interface ApiError {
  error: { code: string; message: string };
}

export interface ProposeResponse {
  plan_id: string;
  plan: HedgePlan;
  proposal: TradeProposal;
  precheck: RiskDecisionRecord;
  expires_at: string;
}

export interface MinEdgeResponse {
  min_edge_bps: number;
  floor_bps: number;
  mode: Mode;
}

export interface HealthResponse {
  ok: boolean;
  mode: Mode;
  version: string;
  uptime_s: number;
}

export type WsFrame =
  | { type: "hello"; ts: string; data: { version: string; mode: Mode; poll_fallback_ms: number } }
  | { type: "snapshot"; ts: string; data: Snapshot }
  | { type: "event"; ts: string; data: AgentEvent }
  | { type: "pong"; ts?: string };
