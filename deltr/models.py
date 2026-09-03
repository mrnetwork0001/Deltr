"""deltr/models.py — shared pydantic v2 contracts.  Lead-owned; frozen after wave 0.
Zero I/O.  Every subsystem imports from here; nothing here imports a subsystem.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from deltr.config import LegOrder, Mode  # the ONLY mode/leg-order enums (PAPER | TESTNET)


# --------------------------------------------------------------------------- helpers
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical_json(obj: Any) -> str:
    """Deterministic serialisation for plan hashes, receipt digests and the replay-determinism test."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


class DeltrModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, use_enum_values=False)


# --------------------------------------------------------------------------- enums
class Venue(str, Enum):
    BINANCE_FUTURES = "binance_futures"
    BINANCE_SPOT = "binance_spot"
    PANCAKESWAP_V3 = "pancakeswap_v3"
    BINANCE_AGENTIC_WALLET = "binance_agentic_wallet"  # on-chain leg executed by the Binance Agentic Wallet (baw); Deltr holds no key


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class DataSource(str, Enum):
    """Provenance tag carried by every price, fill and history point (shown in the UI legend)."""
    BSC_MAINNET_CHAIN = "bsc-mainnet-chain"
    BINANCE_AGENTIC_WALLET = "binance-agentic-wallet"  # reported by the Binance Agentic Wallet CLI (custody and signing stay with Binance)
    BINANCE_FUTURES_MAINNET = "binance-futures-mainnet"   # real fapi.binance.com market data (keyless, read-only)
    BINANCE_FUTURES_TESTNET = "binance-futures-testnet"
    BINANCE_SPOT_MIRROR = "binance-spot-mirror"
    PAPER = "paper"
    REPLAY = "replay"
    SIMULATED = "simulated"


class TraceSource(str, Enum):
    MCP = "mcp"
    API = "api"
    UI = "ui"
    CLI = "cli"
    AUTO = "auto"


class StressKind(str, Enum):
    BASIS_SHOCK = "basis_shock"      # magnitude = bps adverse basis move applied to open positions' mark-to-close
    EQUITY_SHOCK = "equity_shock"    # magnitude = pct of equity removed (labelled; demonstrates the halt path)
    DEX_LEG_FAIL = "dex_leg_fail"    # next DEX leg raises -> exercises reverse-on-failure
    FUNDING_FLIP = "funding_flip"    # magnitude = new funding rate (e.g. -0.0003) applied to the paper accrual
    FEED_STALE = "feed_stale"        # freezes freshness ages -> STALE_QUOTE / no actionable
    RESET = "reset"


DdState = Literal["NORMAL", "WARN", "HALTED"]
ReceiptStatus = Literal["filled", "vetoed", "failed", "unwound", "expired"]
StepName = Literal["intent", "scan", "plan", "gate", "dex_fill", "cex_fill", "unwind", "position", "receipt", "error"]
StepStatus = Literal["ok", "veto", "error", "skipped"]


# --------------------------------------------------------------------------- market data
class SymbolFilters(DeltrModel):
    symbol: str
    step_size: float = 0.01
    min_qty: float = 0.01
    tick_size: float = 0.01
    min_notional: float = 5.0
    price_precision: int = 3
    qty_precision: int = 2
    funding_interval_h: int = 8
    source: DataSource = DataSource.BINANCE_FUTURES_TESTNET


class Quote(DeltrModel):
    venue: Venue
    symbol: str
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    ts: datetime
    source: DataSource

    @computed_field  # type: ignore[misc]
    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class DexQuote(DeltrModel):
    pool: str
    fee_tier: int = 100                 # PancakeSwap units: 100 = 0.01 %
    fee_bps: float = 1.0
    sqrt_price_x96: int
    tick: int
    mid_price: float                    # USDT per BNB from slot0
    size_base: float                    # quoted size (BNB)
    exec_price_buy: float               # amount_in_usdt / size_base (quoteExactOutputSingle USDT->WBNB)
    amount_in_usdt: float
    exec_price_sell: float              # amount_out_usdt / size_base (quoteExactInputSingle WBNB->USDT)
    amount_out_usdt: float
    impact_bps: float                   # 1e4*(exec_buy/mid - 1) - fee_bps
    gas_units: int
    gas_price_wei: int
    gas_usd: float
    block: Optional[int] = None
    ts: datetime
    source: DataSource = DataSource.BSC_MAINNET_CHAIN


class FundingSnapshot(DeltrModel):
    symbol: str
    mark_price: float
    index_price: float
    last_funding_rate: float            # per interval, signed (short RECEIVES when > 0)
    next_funding_time_ms: int
    interval_h: int = 8
    annualized_pct: float               # rate * (24/interval_h) * 365 * 100
    ts: datetime
    source: DataSource = DataSource.BINANCE_FUTURES_TESTNET


class Freshness(DeltrModel):
    cex_age_ms: int
    dex_age_ms: int
    spot_age_ms: int
    ok: bool
    reason: Optional[str] = None        # "cex_stale" | "dex_stale" | "price_sanity" | "feed_stale(stress)"


class MarketState(DeltrModel):
    symbol: str
    dex: Optional[DexQuote] = None
    cex_perp_book: Optional[Quote] = None      # testnet bookTicker (fill simulation + sanity only)
    cex_spot_ref: Optional[Quote] = None       # spot data-mirror bookTicker (displayed reference)
    funding: Optional[FundingSnapshot] = None  # mark/index/funding from testnet premiumIndex
    perp_ref_price: Optional[float] = None     # == funding.mark_price (the perp "truth" price)
    freshness: Freshness
    ts: datetime
    source: DataSource = DataSource.BINANCE_FUTURES_TESTNET  # REPLAY when driven by ReplayHub


class SpreadPoint(DeltrModel):
    ts: datetime
    dex_exec: float
    perp_ref: float
    basis_bps: float
    net_edge_bps: float
    funding_rate: float
    actionable: bool
    source: DataSource


# --------------------------------------------------------------------------- edge / opportunity
class EdgeComponent(DeltrModel):
    label: str
    bps: float
    kind: Literal["gain", "cost", "net"]


class EdgeBreakdown(DeltrModel):
    notional_usd: float
    basis_entry_bps: float
    dex_fee_bps: float
    dex_impact_bps: float
    perp_slip_bps: float
    cex_taker_bps: float
    gas_bps_leg: float
    roundtrip_cost_bps: float           # 2*(dex_fee+dex_impact+perp_slip+cex_taker+gas_bps_leg)
    funding_rate_last: float
    horizon_h: float
    settlements: int
    funding_bps_horizon: float          # 1e4 * funding_rate_last * settlements (sign-aware, short receives +)
    basis_exit_assumed_bps: float = 0.0
    basis_shock_bps: float = 100.0
    net_edge_bps: float                 # basis_entry + funding_bps_horizon - roundtrip - basis_exit_assumed
    expected_edge_usd: float            # net_edge_bps/1e4 * notional
    allocated_risk_usd: float           # notional * (roundtrip + basis_shock)/1e4

    def components(self) -> list[EdgeComponent]:
        c = [
            EdgeComponent(label="Entry basis (perp ref − DEX exec)", bps=self.basis_entry_bps, kind="gain" if self.basis_entry_bps >= 0 else "cost"),
            EdgeComponent(label="DEX pool fee ×2", bps=-2 * self.dex_fee_bps, kind="cost"),
            EdgeComponent(label="DEX price impact ×2", bps=-2 * self.dex_impact_bps, kind="cost"),
            EdgeComponent(label="Perp slippage ×2", bps=-2 * self.perp_slip_bps, kind="cost"),
            EdgeComponent(label="Perp taker fee ×2", bps=-2 * self.cex_taker_bps, kind="cost"),
            EdgeComponent(label="BSC gas ×2", bps=-2 * self.gas_bps_leg, kind="cost"),
            EdgeComponent(label=f"Funding over {self.horizon_h:g} h ({self.settlements} settlements)", bps=self.funding_bps_horizon, kind="gain" if self.funding_bps_horizon >= 0 else "cost"),
        ]
        if self.basis_exit_assumed_bps:
            c.append(EdgeComponent(label="Assumed exit basis", bps=-self.basis_exit_assumed_bps, kind="cost"))
        c.append(EdgeComponent(label="Net edge", bps=self.net_edge_bps, kind="net"))
        return c


class ArbOpportunity(DeltrModel):
    id: str = Field(default_factory=lambda: new_id("opp"))
    symbol: str
    direction: Literal["long_dex_short_perp"] = "long_dex_short_perp"
    dex_price: float                    # DEX exec buy price for size
    perp_price: float                   # perp reference (mark)
    edge: EdgeBreakdown
    size_base: float
    notional_usd: float
    horizon_h: float
    is_actionable: bool
    reason: str                         # "ok" | "net_edge 1.2 < 3.0 bps" | "stale: dex_age 9000ms" | "price_sanity 140 bps" ...
    min_edge_bps_used: float
    freshness: Freshness
    ts: datetime


# --------------------------------------------------------------------------- plans / proposals
class OrderLeg(DeltrModel):
    venue: Venue
    symbol: str
    side: Side
    qty: float
    price_hint: float
    reduce_only: bool = False
    leverage: float = 1.0
    client_id: Optional[str] = None     # perp leg only: DLTR{plan8}{leg}{attempt}
    position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH"


class HedgePlan(DeltrModel):
    id: str = Field(default_factory=lambda: new_id("plan"))
    opportunity_id: Optional[str] = None
    symbol: str
    legs: list[OrderLeg]                # exactly 2: [DEX leg, PERP leg] (order of execution decided by LegOrder)
    qty: float
    notional_usd: float
    leverage: float
    margin_usd: float
    cash_required_usd: float            # notional + margin (DEX leg is unlevered)
    allocated_risk_usd: float
    expected_edge_bps: float
    roundtrip_cost_bps: float
    ref_dex_price: float
    ref_perp_price: float
    reduce_only: bool = False
    position_id: Optional[str] = None   # set on unwind plans
    source: TraceSource = TraceSource.API
    client: Optional[str] = None        # e.g. "claude-desktop/0.12"
    prompt: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    plan_hash: str = ""

    @field_validator("legs")
    @classmethod
    def _two_legs(cls, v: list[OrderLeg]) -> list[OrderLeg]:
        if len(v) != 2:
            raise ValueError("HedgePlan needs exactly two legs")
        venues = {leg.venue for leg in v}
        if venues != {Venue.PANCAKESWAP_V3, Venue.BINANCE_FUTURES}:
            raise ValueError("legs must be one PancakeSwap V3 leg and one Binance Futures leg")
        return v

    @model_validator(mode="after")
    def _hash(self) -> "HedgePlan":
        if not self.plan_hash:
            body = {
                "symbol": self.symbol, "qty": self.qty, "notional_usd": self.notional_usd, "leverage": self.leverage,
                "legs": [leg.model_dump(mode="json") for leg in self.legs], "reduce_only": self.reduce_only,
                "position_id": self.position_id, "created_at": self.created_at.isoformat(),
            }
            object.__setattr__(self, "plan_hash", sha256_of(body))
        return self


class TradeProposal(DeltrModel):
    """Flat gate input.  ONLY the Executor builds this (from Portfolio + MarketState + HedgePlan).
    `as_gate_input()` is what `BinanceRiskGate.evaluate()` receives."""
    plan_id: str
    symbol: str
    dex_side: Side
    perp_side: Side
    dex_qty: float
    perp_qty: float
    qty_step: float
    leverage: float
    notional: float
    allocated_risk: float
    roundtrip_cost_bps: float
    expected_edge_bps: float
    quote_age_ms: float
    price_drift_bps: float
    dex_ref_price: float
    perp_ref_price: float
    reduce_only: bool = False
    position_id: Optional[str] = None

    @computed_field  # type: ignore[misc]
    @property
    def is_delta_neutral(self) -> bool:
        return (self.dex_side == Side.BUY and self.perp_side == Side.SELL) or (
            self.reduce_only and self.dex_side == Side.SELL and self.perp_side == Side.BUY
        )

    def as_gate_input(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["dex_side"] = self.dex_side.value
        d["perp_side"] = self.perp_side.value
        return d


# --------------------------------------------------------------------------- risk
class CheckResult(DeltrModel):
    name: str
    passed: bool
    observed: Any = None
    limit: Any = None
    unit: str = ""


class RiskDecisionRecord(DeltrModel):
    id: str = Field(default_factory=lambda: new_id("dec"))
    plan_id: Optional[str] = None
    approved: bool
    code: str
    reason: str
    observed: Any = None
    limit: Any = None
    unit: str = ""
    checks: list[CheckResult] = Field(default_factory=list)
    latency_ns: int
    dd_state: DdState = "NORMAL"
    drawdown_pct: float = 0.0
    halted: bool = False
    kill_switch: bool = False
    mode: Mode
    dry_run: bool = False               # True for deltr_evaluate_risk / propose pre-checks
    ts: datetime = Field(default_factory=utcnow)

    @computed_field  # type: ignore[misc]
    @property
    def latency_us(self) -> float:
        return round(self.latency_ns / 1000.0, 3)

    @classmethod
    def from_gate(cls, d: Any, *, plan_id: Optional[str], mode: Mode, gate_snapshot: dict[str, Any], dry_run: bool = False) -> "RiskDecisionRecord":
        """`d` is risk_gate.RiskDecision (stdlib dataclass)."""
        return cls(
            plan_id=plan_id, approved=d.approved, code=d.code, reason=d.reason, observed=d.observed, limit=d.limit,
            unit=d.unit, checks=[CheckResult(**c.as_dict()) for c in d.checks], latency_ns=d.latency_ns,
            dd_state=gate_snapshot["state"], drawdown_pct=gate_snapshot["drawdown_pct"],
            halted=gate_snapshot["halted"], kill_switch=gate_snapshot["kill_switch"], mode=mode, dry_run=dry_run,
        )


# --------------------------------------------------------------------------- execution
class Fill(DeltrModel):
    leg_index: int
    venue: Venue
    symbol: str
    side: Side
    qty: float
    price: float
    fee_usd: float
    ref: str                            # "paper" | futures orderId | "sim:<reason>"
    simulated: bool
    source: DataSource
    client_id: Optional[str] = None
    attempt: int = 1
    reference_divergence_bps: Optional[float] = None   # testnet fill vs mark
    latency_ms: int = 0
    ts: datetime = Field(default_factory=utcnow)


class TraceStep(DeltrModel):
    step: StepName
    status: StepStatus
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    latency_ms: Optional[int] = None
    ts: datetime = Field(default_factory=utcnow)


class ExecutionReceipt(DeltrModel):
    id: str = Field(default_factory=lambda: new_id("rcpt"))
    plan_id: str
    source: TraceSource
    client: Optional[str] = None
    prompt: Optional[str] = None
    mode: Mode
    status: ReceiptStatus
    decision: RiskDecisionRecord
    plan: HedgePlan
    fills: list[Fill] = Field(default_factory=list)
    position_id: Optional[str] = None
    residual_delta_base: float = 0.0
    realized_cost_usd: float = 0.0
    legging_window_ms: Optional[int] = None
    steps: list[TraceStep] = Field(default_factory=list)
    stress_active: Optional[str] = None
    sha256: str = ""
    ts: datetime = Field(default_factory=utcnow)

    def digest_body(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, "plan_hash": self.plan.plan_hash, "mode": self.mode.value, "status": self.status,
            "decision": {"code": self.decision.code, "approved": self.decision.approved, "reason": self.decision.reason},
            "fills": [f.model_dump(mode="json", exclude={"ts", "latency_ms"}) for f in self.fills],
            "position_id": self.position_id, "stress_active": self.stress_active,
        }

    @model_validator(mode="after")
    def _seal(self) -> "ExecutionReceipt":
        if not self.sha256:
            object.__setattr__(self, "sha256", sha256_of(self.digest_body()))
        return self


# --------------------------------------------------------------------------- portfolio
class Position(DeltrModel):
    id: str = Field(default_factory=lambda: new_id("pos"))
    plan_id: str
    symbol: str
    dex_qty: float
    dex_entry: float
    perp_qty: float
    perp_entry: float
    leverage: float
    margin_usd: float
    notional_usd: float
    allocated_risk_usd: float
    basis_entry_bps: float
    opened_at: datetime
    funding_accrued_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0     # MARK-TO-CLOSE: net of estimated exit costs
    est_exit_cost_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    delta_base: float = 0.0             # dex_qty - perp_qty
    stop_distance_usd: float = 0.0      # allocated_risk + unrealized (<= 0 -> stop fires)
    liq_price_est: float = 0.0          # perp_entry * (1 + 1/L) / (1 + 0.004)
    status: Literal["open", "closed"] = "open"
    stress_applied: Optional[str] = None
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None


class PortfolioSnapshot(DeltrModel):
    equity_usd: float                   # cash + Σ mark-to-close position value
    cash_usd: float
    reserved_cash_usd: float
    peak_equity_usd: float
    drawdown_pct: float
    dd_state: DdState
    total_pnl_usd: float
    spread_pnl_usd: float
    funding_pnl_usd: float
    fees_paid_usd: float
    open_positions: int
    daily_realized_usd: float = 0.0
    equity_curve: list[tuple[datetime, float]] = Field(default_factory=list)   # tail, max 600 points
    ts: datetime


# --------------------------------------------------------------------------- intents / prompts
class Intent(DeltrModel):
    action: Literal["rebalance", "hedge", "scan", "explain", "unwind", "status", "kill", "reset_halt", "stress", "set_min_edge", "unknown"]
    capital_usd: Optional[float] = None
    leverage: Optional[float] = None
    symbol: str = "BNBUSDT"
    position_id: Optional[str] = None   # "all" allowed
    stress_kind: Optional[StressKind] = None
    magnitude: Optional[float] = None
    min_edge_bps: Optional[float] = None
    stablecoin_note: Optional[str] = None   # "USDC treated as USDT-equivalent; no conversion leg in v1"
    confidence: float = 1.0
    raw: str
    source: TraceSource = TraceSource.UI


class PromptResult(DeltrModel):
    intent: Intent
    opportunity: Optional[ArbOpportunity] = None
    plan: Optional[HedgePlan] = None
    plan_id: Optional[str] = None
    proposal: Optional[TradeProposal] = None
    precheck: Optional[RiskDecisionRecord] = None
    message: str
    steps: list[TraceStep] = Field(default_factory=list)
    executes: Literal[False] = False    # prompts NEVER execute; call deltr_execute_hedge(plan_id)


# --------------------------------------------------------------------------- MCP / bridge / status
class McpActivity(DeltrModel):
    id: str = Field(default_factory=lambda: new_id("act"))
    direction: Literal["inbound", "outbound"]
    client: Optional[str] = None        # "claude-desktop/0.12" (clientInfo) | "mcp-client" | "bridge"
    server: Literal["deltr", "binance-shim", "binance-official", "rest"]
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)   # redacted
    result_summary: str = ""
    ok: bool = True
    latency_ms: int = 0
    trace_id: Optional[str] = None      # receipt id or plan id when applicable
    ts: datetime = Field(default_factory=utcnow)


class UpstreamStatus(DeltrModel):
    kind: Literal["official", "shim", "none"]
    url: Optional[str] = None
    authorized: bool = False
    tools_discovered: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    ts: datetime = Field(default_factory=utcnow)


class VenueHealth(DeltrModel):
    name: str                           # "pancakeswap_v3", "binance_futures_testnet", "binance_spot_mirror"
    ok: bool
    age_ms: int
    source: DataSource
    detail: str = ""


class StressScenario(DeltrModel):
    kind: StressKind
    magnitude: float = 0.0
    label: str = ""


class StressResult(DeltrModel):
    scenario: StressScenario
    equity_before: float
    equity_after: float
    drawdown_pct: float
    dd_state: DdState
    halted: bool
    positions_affected: int
    stops_fired: list[str] = Field(default_factory=list)
    active_label: Optional[str] = None  # what the SIMULATED badge shows, None after reset
    ts: datetime = Field(default_factory=utcnow)


class AgentEvent(DeltrModel):
    topic: str                          # "tick" | "scan" | "plan" | "gate" | "fill" | "position" | "pnl" | "mcp" | "stress" | "log"
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)


class SystemStatus(DeltrModel):
    mode: Mode
    symbol: str
    version: str
    uptime_s: int
    started_at: datetime
    halted: bool
    kill_switch: bool
    dd_state: DdState
    equity_usd: float
    drawdown_pct: float
    open_positions: int
    venues: list[VenueHealth]
    upstream: Optional[UpstreamStatus] = None
    secrets_present: bool
    binance_api_env: str
    replay: bool
    stress_active: Optional[str] = None
    gate_median_us: float               # measured at startup (10k evaluate() calls), quoted, never asserted
    min_edge_bps: float
    min_edge_floor_bps: float           # 0 in PAPER; measured roundtrip in TESTNET
    leg_order: LegOrder
    limits: dict[str, Any]
    check_order: list[str]
    # ---- mode / provenance / custody (additive; default to the pre-LIVE behaviour) ----
    execution_style: Optional[str] = None      # "maker" | "taker" (None in PAPER: nothing reaches a venue)
    execution_style_label: Optional[str] = None
    data_source: Optional[str] = None          # where market data comes from: mainnet in every mode
    real_funds_armed: bool = False             # True ONLY in LIVE, after the preflight passed
    onchain_armed: bool = False                # the agentic-wallet opt-in triple is complete
    wallet_address: Optional[str] = None       # the Agentic Wallet's PUBLIC address; never a key
    max_notional_usd: Optional[float] = None
    max_aggregate_usd: Optional[float] = None


class Snapshot(DeltrModel):
    """The ONLY payload the dashboard consumes (GET /api/snapshot, WS /ws/stream)."""
    status: SystemStatus
    market: Optional[MarketState] = None
    edge: Optional[EdgeBreakdown] = None
    opportunity: Optional[ArbOpportunity] = None
    history: list[SpreadPoint] = Field(default_factory=list)          # last 600 ticks
    decisions: list[RiskDecisionRecord] = Field(default_factory=list) # last 100
    positions: list[Position] = Field(default_factory=list)
    portfolio: PortfolioSnapshot
    receipts: list[ExecutionReceipt] = Field(default_factory=list)   # last 10 (steps = trace)
    prompts: list[PromptResult] = Field(default_factory=list)        # last 5 (for TradeTrace when nothing executed)
    activity: list[McpActivity] = Field(default_factory=list)        # last 50
    events: list[AgentEvent] = Field(default_factory=list)           # last 100
    ts: datetime = Field(default_factory=utcnow)


__all__ = [n for n in dir() if n[0].isupper() or n in ("utcnow", "new_id", "canonical_json", "sha256_of")]