"""tests/helpers_mcp.py — self-contained fakes for the Deltr MCP tests.

``FakeEngine`` implements the section-4.14 Engine surface with canned models
(probe numbers from 2026-09-02) and a REAL ``risk_gate.BinanceRiskGate`` so
veto paths are meaningful.  ``FakeState`` mimics the parts of ``deltr.state.State``
the activity log touches.  No network, no I/O.

Run ``python -m tests.helpers_mcp`` to serve the fake engine over stdio (used by
the stdout-cleanliness test).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from pathlib import Path
from datetime import timedelta
from typing import Any, Deque, Optional

from deltr.config import LegOrder, Mode, Settings
from deltr.edge import Sizing, size_from_capital
from deltr.funding_history import (
    DEFAULT_HOLDS_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    FUTURES_MAINNET_REST,
    FundingAnalysis,
    analyse_funding,
    parse_funding_rows,
)
from deltr.horizon import HorizonAnalysis, horizon_analysis
from deltr.models import (
    AgentEvent,
    ArbOpportunity,
    DataSource,
    DexQuote,
    EdgeBreakdown,
    EdgeComponent,
    ExecutionReceipt,
    Fill,
    Freshness,
    FundingSnapshot,
    HedgePlan,
    Intent,
    MarketState,
    McpActivity,
    OrderLeg,
    PortfolioSnapshot,
    Position,
    PromptResult,
    Quote,
    RiskDecisionRecord,
    Side,
    Snapshot,
    SpreadPoint,
    StressResult,
    StressScenario,
    SystemStatus,
    TraceSource,
    TradeProposal,
    TraceStep,
    Venue,
    VenueHealth,
    new_id,
    utcnow,
)
from risk_gate import CHECK_ORDER, BinanceRiskGate

FUNDING_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "funding_history.json"


def load_funding_fixture(symbol: str) -> Optional[list[dict[str, Any]]]:
    """Verbatim mainnet ``/fapi/v1/fundingRate`` rows captured for ``symbol`` (None when absent)."""
    data = json.loads(FUNDING_FIXTURE.read_text(encoding="utf-8"))
    return data["symbols"].get(str(symbol).upper())


DEX_EXEC = 686.19
PERP_MARK = 686.34
STEP = 0.01
FUNDING_RATE = 0.0003  # per 8 h; 9 settlements over 72 h -> +27 bps so the canned hedge clears the 3 bps floor


# --------------------------------------------------------------------------- exceptions (engine-shaped)
class PlanNotFound(Exception):
    code = "PLAN_NOT_FOUND"


class PlanExpired(Exception):
    code = "PLAN_EXPIRED"


class ConfirmRequired(Exception):
    code = "CONFIRM_REQUIRED"


class HaltNotClearable(Exception):
    """Same class name as deltr.engine.HaltNotClearable; no ``code`` attr on purpose (tests the name mapping)."""


class OnchainRefused(Exception):
    """Same shape as deltr.engine.OnchainRefused: the ``code`` attribute drives the MCP envelope."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class StressRefused(Exception):
    """Same class name as deltr.stress.StressRefused; no ``code`` attr on purpose."""


# --------------------------------------------------------------------------- fake state
class FakeState:
    def __init__(self) -> None:
        self.history: Deque[SpreadPoint] = deque(maxlen=600)
        self.decisions: Deque[RiskDecisionRecord] = deque(maxlen=100)
        self.receipts: Deque[ExecutionReceipt] = deque(maxlen=10)
        self.prompts: Deque[PromptResult] = deque(maxlen=5)
        self.activity: Deque[McpActivity] = deque(maxlen=50)
        self.events: Deque[AgentEvent] = deque(maxlen=100)
        self.plans: dict[str, HedgePlan] = {}
        self.positions: dict[str, Position] = {}

    def record_activity(self, a: McpActivity) -> None:
        self.activity.append(a)

    def emit(self, topic: str, message: str, level: str = "info", data: Optional[dict] = None) -> AgentEvent:
        ev = AgentEvent(topic=topic, message=message, level=level, data=data or {})  # type: ignore[arg-type]
        self.events.append(ev)
        return ev


# --------------------------------------------------------------------------- canned market
def make_settings(mode: str = "paper", **extra: Any) -> Settings:
    kw: dict[str, Any] = {"DELTR_MODE": mode, "DELTR_MIN_EDGE_BPS": 3.0}
    if mode == "testnet":
        kw.update(BINANCE_API_KEY="test-key-not-real", BINANCE_SECRET_KEY="test-secret-not-real")
    kw.update(extra)
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def make_market(symbol: str = "BNBUSDT") -> MarketState:
    now = utcnow()
    dex = DexQuote(
        pool="0x36696169c63e42cd08ce11f5deebbcebae652050", fee_tier=100, fee_bps=1.0,
        sqrt_price_x96=3024721431835227127309627620, tick=65280, mid_price=686.10, size_base=4.86,
        exec_price_buy=DEX_EXEC, amount_in_usdt=3334.89, exec_price_sell=686.053, amount_out_usdt=3334.22,
        impact_bps=0.3, gas_units=162878, gas_price_wei=50_000_000, gas_usd=0.0056, block=60_000_000, ts=now,
    )
    perp = Quote(venue=Venue.BINANCE_FUTURES, symbol=symbol, bid=686.03, ask=686.34, ts=now, source=DataSource.BINANCE_FUTURES_TESTNET)
    spot = Quote(venue=Venue.BINANCE_SPOT, symbol=symbol, bid=686.30, ask=686.31, ts=now, source=DataSource.BINANCE_SPOT_MIRROR)
    funding = FundingSnapshot(
        symbol=symbol, mark_price=PERP_MARK, index_price=686.129, last_funding_rate=0.0001,
        next_funding_time_ms=int(now.timestamp() * 1000) + 3_600_000, interval_h=8, annualized_pct=10.95, ts=now,
    )
    return MarketState(
        symbol=symbol, dex=dex, cex_perp_book=perp, cex_spot_ref=spot, funding=funding, perp_ref_price=PERP_MARK,
        freshness=Freshness(cex_age_ms=120, dex_age_ms=900, spot_age_ms=200, ok=True), ts=now,
    )


def make_edge(notional_usd: float = 3328.02, horizon_h: float = 72.0) -> EdgeBreakdown:
    settlements = int(horizon_h // 8)
    funding_bps = 1e4 * FUNDING_RATE * settlements
    basis = 1e4 * (PERP_MARK - DEX_EXEC) / DEX_EXEC
    roundtrip = 2 * (1.0 + 0.3 + 2.0 + 5.0 + 0.017)
    net = basis + funding_bps - roundtrip
    return EdgeBreakdown(
        notional_usd=notional_usd, basis_entry_bps=basis, dex_fee_bps=1.0, dex_impact_bps=0.3, perp_slip_bps=2.0,
        cex_taker_bps=5.0, gas_bps_leg=0.017, roundtrip_cost_bps=roundtrip, funding_rate_last=FUNDING_RATE, horizon_h=horizon_h,
        settlements=settlements, funding_bps_horizon=funding_bps, net_edge_bps=net,
        expected_edge_usd=net / 1e4 * notional_usd, allocated_risk_usd=notional_usd * (roundtrip + 100) / 1e4,
    )


# --------------------------------------------------------------------------- fake engine
class FakeEngine:
    """Duck-typed Engine (section 4.14) with canned market data and a real risk gate."""

    def __init__(self, settings: Optional[Settings] = None, *, halted: bool = False, refuse_stress: bool = False) -> None:
        self.settings = settings or make_settings()
        self.state = FakeState()
        self.gate = BinanceRiskGate(capital_usd=self.settings.capital_usd, limits=self.settings.risk_limits(), mode=self.settings.mode.value)
        self.started_at = utcnow()
        self._market = make_market(self.settings.symbol)
        self._edge = make_edge()
        self._halted = halted
        self._refuse_stress = refuse_stress
        self._stress_label: Optional[str] = None
        self._min_edge = self.settings.min_edge_bps
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._equity = self.settings.capital_usd
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.wallet_installed = True
        self.wallet_signed_in = True
        self._onchain_notional_usd = 0.0
        for i in range(5):
            self.state.history.append(SpreadPoint(ts=utcnow(), dex_exec=DEX_EXEC, perp_ref=PERP_MARK, basis_bps=2.2,
                                                  net_edge_bps=8.0 - i * 0.1, funding_rate=FUNDING_RATE, actionable=True,
                                                  source=DataSource.PAPER))

    # ---- lifecycle -----------------------------------------------------------
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    # ---- read -----------------------------------------------------------------
    def _portfolio(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            equity_usd=self._equity, cash_usd=self._equity, reserved_cash_usd=0.0, peak_equity_usd=self.settings.capital_usd,
            drawdown_pct=self.gate.drawdown_pct, dd_state=self.gate.state, total_pnl_usd=0.0, spread_pnl_usd=0.0,  # type: ignore[arg-type]
            funding_pnl_usd=0.0, fees_paid_usd=0.0, open_positions=len(self.state.positions), ts=utcnow(),
        )

    def status(self) -> SystemStatus:
        snap = self.gate.snapshot()
        return SystemStatus(
            mode=self.settings.mode, symbol=self.settings.symbol, version=self.settings.version,
            uptime_s=int((utcnow() - self.started_at).total_seconds()), started_at=self.started_at,
            halted=self._halted or bool(snap["halted"]), kill_switch=bool(snap["kill_switch"]), dd_state=snap["state"],
            equity_usd=self._equity, drawdown_pct=snap["drawdown_pct"], open_positions=len(self.state.positions),
            venues=[
                VenueHealth(name="pancakeswap_v3", ok=True, age_ms=900, source=DataSource.BSC_MAINNET_CHAIN),
                VenueHealth(name="binance_futures_testnet", ok=True, age_ms=120, source=DataSource.BINANCE_FUTURES_TESTNET),
                VenueHealth(name="binance_spot_mirror", ok=True, age_ms=200, source=DataSource.BINANCE_SPOT_MIRROR),
            ],
            upstream=None, secrets_present=self.settings.secrets_present, binance_api_env=self.settings.binance_api_env,
            replay=False, stress_active=self._stress_label, gate_median_us=1.04, min_edge_bps=self._min_edge,
            min_edge_floor_bps=0.0, leg_order=LegOrder.DEX_FIRST, limits=snap["limits"], check_order=list(CHECK_ORDER),
            # mirror the real State.system_status so the MCP contract is actually exercised
            execution_style=(None if self.settings.mode.value == "paper" else self.settings.execution_style.value),
            execution_style_label=self.settings.execution_style_label,
            data_source=self.settings.data_source_label,
            real_funds_armed=False,          # a fake engine is never armed
            onchain_armed=self.settings.onchain_arming_error() is None,
            wallet_address=None,
            max_notional_usd=float(self.settings.max_notional_usd),
            max_aggregate_usd=(None if self.settings.max_aggregate_usd == float("inf") else float(self.settings.max_aggregate_usd)),
        )

    def snapshot(self) -> Snapshot:
        return Snapshot(
            status=self.status(), market=self._market, edge=self._edge, opportunity=self.scan(), history=list(self.state.history),
            decisions=list(self.state.decisions), positions=list(self.state.positions.values()), portfolio=self._portfolio(),
            receipts=list(self.state.receipts), prompts=list(self.state.prompts), activity=list(self.state.activity),
            events=list(self.state.events),
        )

    def market(self) -> tuple[Optional[MarketState], Optional[EdgeBreakdown]]:
        return self._market, self._edge

    def scan(self, notional_usd: Optional[float] = None, horizon_h: Optional[float] = None, min_edge_bps: Optional[float] = None) -> ArbOpportunity:
        edge = make_edge(notional_usd or 3328.02, horizon_h or self.settings.funding_horizon_hours)
        thr = self._min_edge if min_edge_bps is None else min_edge_bps
        ok = edge.net_edge_bps >= thr
        return ArbOpportunity(
            symbol=self.settings.symbol, dex_price=DEX_EXEC, perp_price=PERP_MARK, edge=edge, size_base=edge.notional_usd / DEX_EXEC,
            notional_usd=edge.notional_usd, horizon_h=edge.horizon_h, is_actionable=ok,
            reason="ok" if ok else f"net_edge {edge.net_edge_bps:.1f} < {thr:.1f} bps", min_edge_bps_used=thr,
            freshness=self._market.freshness, ts=utcnow(),
        )

    def explain_edge(
        self, capital_usd: Optional[float], leverage: Optional[float], horizon_h: Optional[float],
        assumed_funding_rate: Optional[float] = None,
    ) -> tuple[EdgeBreakdown, Sizing, list[EdgeComponent], HorizonAnalysis]:
        sizing = size_from_capital(capital_usd or self.settings.capital_usd, leverage or self.settings.default_leverage, DEX_EXEC, STEP)
        edge = make_edge(sizing.notional_usd, horizon_h or self.settings.funding_horizon_hours)
        return edge, sizing, edge.components(), self.edge_horizon(edge, assumed_funding_rate)

    async def funding_history(
        self,
        symbol: Optional[str] = None,
        *,
        lookback_days: Optional[float] = None,
        holds_days: Optional[list[float]] = None,
        refresh: bool = False,
    ) -> FundingAnalysis:
        """The real Engine method's shape, computed offline from the captured mainnet fixture."""
        sym = str(symbol or self.settings.symbol).upper().strip()
        if not sym.isalnum() or not (5 <= len(sym) <= 20):
            raise ValueError(f"symbol {symbol!r} is not a valid perpetual symbol, e.g. BNBUSDT")
        look = float(lookback_days) if lookback_days is not None else DEFAULT_LOOKBACK_DAYS
        if not (1.0 <= look <= 2000.0):
            raise ValueError("lookback_days must be between 1 and 2000")
        rows = load_funding_fixture(sym)
        if rows is None:
            raise ValueError(f"symbol {sym!r} is not in the offline funding fixture")
        return analyse_funding(
            parse_funding_rows(rows, symbol=sym),
            symbol=sym,
            holds_days=[float(h) for h in (holds_days or DEFAULT_HOLDS_DAYS)],
            base_url=FUTURES_MAINNET_REST,
        )

    def edge_horizon(self, edge: EdgeBreakdown, assumed_funding_rate: Optional[float] = None) -> HorizonAnalysis:
        funding = self._market.funding
        return horizon_analysis(
            edge,
            interval_h=float(funding.interval_h) if funding else None,
            symbol=self._market.symbol,
            measured_source=funding.source.value if funding else None,
            assumed_rate=assumed_funding_rate,
        )

    # ---- gate inputs ------------------------------------------------------------
    def _gate_input(self, plan_id: str, sizing: Sizing, leverage: float, edge: EdgeBreakdown, symbol: str) -> TradeProposal:
        return TradeProposal(
            plan_id=plan_id, symbol=symbol, dex_side=Side.BUY, perp_side=Side.SELL, dex_qty=sizing.base_qty, perp_qty=sizing.base_qty,
            qty_step=STEP, leverage=leverage, notional=sizing.notional_usd, allocated_risk=edge.allocated_risk_usd,
            roundtrip_cost_bps=edge.roundtrip_cost_bps, expected_edge_bps=edge.net_edge_bps, quote_age_ms=120.0,
            price_drift_bps=0.5, dex_ref_price=DEX_EXEC, perp_ref_price=PERP_MARK,
        )

    def _decision(self, proposal: TradeProposal, dry_run: bool, explain: bool = True) -> RiskDecisionRecord:
        d = self.gate.explain(proposal.as_gate_input()) if explain else self.gate.evaluate(proposal.as_gate_input())
        rec = RiskDecisionRecord.from_gate(d, plan_id=proposal.plan_id, mode=self.settings.mode, gate_snapshot=self.gate.snapshot(), dry_run=dry_run)
        self.state.decisions.append(rec)
        return rec

    # ---- two-phase --------------------------------------------------------------
    async def propose(self, capital_usd: float, leverage: Optional[float], symbol: Optional[str], source: TraceSource,
                      client: Optional[str] = None, prompt: Optional[str] = None) -> tuple[HedgePlan, TradeProposal, RiskDecisionRecord]:
        self.calls.append(("propose", {"capital_usd": capital_usd, "leverage": leverage, "client": client, "source": source}))
        lev = leverage or self.settings.default_leverage
        sym = symbol or self.settings.symbol
        sizing = size_from_capital(capital_usd, lev, DEX_EXEC, STEP)
        edge = make_edge(sizing.notional_usd, self.settings.funding_horizon_hours)
        now = utcnow()
        plan = HedgePlan(
            symbol=sym, legs=[
                OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=sym, side=Side.BUY, qty=sizing.base_qty, price_hint=DEX_EXEC),
                OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=sym, side=Side.SELL, qty=sizing.base_qty, price_hint=PERP_MARK, leverage=lev),
            ],
            qty=sizing.base_qty, notional_usd=sizing.notional_usd, leverage=lev, margin_usd=sizing.margin_usd,
            cash_required_usd=sizing.capital_required_usd, allocated_risk_usd=edge.allocated_risk_usd,
            expected_edge_bps=edge.net_edge_bps, roundtrip_cost_bps=edge.roundtrip_cost_bps, ref_dex_price=DEX_EXEC,
            ref_perp_price=PERP_MARK, source=source, client=client, prompt=prompt, created_at=now,
            expires_at=now + timedelta(seconds=self.settings.plan_ttl_seconds),
        )
        proposal = self._gate_input(plan.id, sizing, lev, edge, sym)
        precheck = self._decision(proposal, dry_run=True)
        self.state.plans[plan.id] = plan
        return plan, proposal, precheck

    def evaluate_risk(self, capital_usd: float, leverage: float, symbol: Optional[str]) -> RiskDecisionRecord:
        sizing = size_from_capital(capital_usd, leverage, DEX_EXEC, STEP)
        edge = make_edge(sizing.notional_usd, self.settings.funding_horizon_hours)
        return self._decision(self._gate_input("dry_run", sizing, leverage, edge, symbol or self.settings.symbol), dry_run=True)

    async def execute(self, plan_id: str, confirm: bool, source: TraceSource, client: Optional[str] = None) -> ExecutionReceipt:
        self.calls.append(("execute", {"plan_id": plan_id, "confirm": confirm, "client": client}))
        plan = self.state.plans.pop(plan_id, None)
        if plan is None:
            raise PlanNotFound(f"plan {plan_id} not found or already used")
        if plan.expires_at < utcnow():
            raise PlanExpired(f"plan {plan_id} expired at {plan.expires_at.isoformat()}")
        if self.settings.mode == Mode.TESTNET and not confirm:
            raise ConfirmRequired("TESTNET execution needs confirm=true")
        sizing = size_from_capital(plan.cash_required_usd, plan.leverage, DEX_EXEC, STEP)
        edge = make_edge(plan.notional_usd)
        proposal = self._gate_input(plan.id, sizing, plan.leverage, edge, plan.symbol)
        decision = self._decision(proposal, dry_run=False)
        steps = [TraceStep(step="plan", status="ok", summary=f"plan {plan.id}"),
                 TraceStep(step="gate", status="ok" if decision.approved else "veto", summary=decision.reason)]
        fills: list[Fill] = []
        position_id: Optional[str] = None
        status = "vetoed"
        if decision.approved:
            fills = [
                Fill(leg_index=0, venue=Venue.PANCAKESWAP_V3, symbol=plan.symbol, side=Side.BUY, qty=plan.qty, price=DEX_EXEC, fee_usd=0.33, ref="paper", simulated=True, source=DataSource.PAPER),
                Fill(leg_index=1, venue=Venue.BINANCE_FUTURES, symbol=plan.symbol, side=Side.SELL, qty=plan.qty, price=PERP_MARK, fee_usd=1.66, ref="paper", simulated=True, source=DataSource.PAPER),
            ]
            pos = Position(plan_id=plan.id, symbol=plan.symbol, dex_qty=plan.qty, dex_entry=DEX_EXEC, perp_qty=plan.qty, perp_entry=PERP_MARK,
                           leverage=plan.leverage, margin_usd=plan.margin_usd, notional_usd=plan.notional_usd,
                           allocated_risk_usd=plan.allocated_risk_usd, basis_entry_bps=edge.basis_entry_bps, opened_at=utcnow())
            self.state.positions[pos.id] = pos
            self.gate.register_position(pos.id, plan.symbol, plan.qty, plan.notional_usd, plan.allocated_risk_usd, plan.leverage)
            position_id = pos.id
            status = "filled"
            steps += [TraceStep(step="dex_fill", status="ok", summary="paper"), TraceStep(step="cex_fill", status="ok", summary="paper"),
                      TraceStep(step="position", status="ok", summary=pos.id)]
        receipt = ExecutionReceipt(plan_id=plan.id, source=source, client=client, prompt=plan.prompt, mode=self.settings.mode,
                                   status=status, decision=decision, plan=plan, fills=fills, position_id=position_id, steps=steps,  # type: ignore[arg-type]
                                   stress_active=self._stress_label, legging_window_ms=7 if fills else None)
        self._receipts[receipt.id] = receipt
        self.state.receipts.append(receipt)
        return receipt

    async def unwind(self, position_id: str, reason: str, confirm: bool, source: TraceSource, client: Optional[str] = None) -> list[ExecutionReceipt]:
        ids = list(self.state.positions) if position_id == "all" else [position_id]
        out: list[ExecutionReceipt] = []
        for pid in ids:
            pos = self.state.positions.pop(pid, None)
            if pos is None:
                raise KeyError(f"POSITION_NOT_FOUND: {pid}")
            self.gate.release_position(pid)
            now = utcnow()
            plan = HedgePlan(symbol=pos.symbol, legs=[
                OrderLeg(venue=Venue.BINANCE_FUTURES, symbol=pos.symbol, side=Side.BUY, qty=pos.perp_qty, price_hint=PERP_MARK, reduce_only=True),
                OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol=pos.symbol, side=Side.SELL, qty=pos.dex_qty, price_hint=DEX_EXEC)],
                qty=pos.perp_qty, notional_usd=pos.notional_usd, leverage=pos.leverage, margin_usd=pos.margin_usd,
                cash_required_usd=0.0, allocated_risk_usd=pos.allocated_risk_usd, expected_edge_bps=0.0, roundtrip_cost_bps=16.6,
                ref_dex_price=DEX_EXEC, ref_perp_price=PERP_MARK, reduce_only=True, position_id=pid, source=source, client=client,
                created_at=now, expires_at=now + timedelta(seconds=60))
            decision = RiskDecisionRecord(plan_id=plan.id, approved=True, code="OK", reason=f"reduce-only unwind ({reason})", latency_ns=900, mode=self.settings.mode)
            out.append(ExecutionReceipt(plan_id=plan.id, source=source, client=client, mode=self.settings.mode, status="unwound", decision=decision,
                                        plan=plan, position_id=pid, steps=[TraceStep(step="unwind", status="ok", summary=reason)]))
        return out

    async def prompt(self, text: str, source: TraceSource, client: Optional[str] = None) -> PromptResult:
        import re

        m = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k)?", text)
        amount = float(m.group(1).replace(",", "")) * (1000 if m and m.group(2) else 1) if m else None
        lev_m = re.search(r"([0-9.]+)\s*x", text)
        lev = float(lev_m.group(1)) if lev_m else None
        note = "USDC treated as USDT-equivalent; no conversion leg in v1" if "usdc" in text.lower() else None
        if amount:
            intent = Intent(action="rebalance", capital_usd=amount, leverage=lev, stablecoin_note=note, raw=text, source=source)
            plan, proposal, precheck = await self.propose(amount, lev, None, source, client=client, prompt=text)
            res = PromptResult(intent=intent, opportunity=self.scan(), plan=plan, plan_id=plan.id, proposal=proposal, precheck=precheck,
                               message=f"Proposed plan {plan.id}; nothing executed. {note or ''}".strip())
        else:
            res = PromptResult(intent=Intent(action="unknown", confidence=0.0, raw=text, source=source), message="Could not parse a hedge request")
        self.state.prompts.append(res)
        return res

    # ---- operator ----------------------------------------------------------------
    def kill_switch(self, on: bool, reason: str) -> SystemStatus:
        self.gate.set_kill_switch(on)
        return self.status()

    def reset_halt(self, reason: str) -> SystemStatus:
        if self._halted or self.state.positions:
            raise HaltNotClearable("drawdown still >= 3 % or positions open")
        self.gate.reset_halt()
        return self.status()

    async def stress(self, scenario: StressScenario) -> StressResult:
        if self._refuse_stress:
            raise StressRefused("TESTNET with open real orders")
        before = self._equity
        if scenario.kind.value == "equity_shock":
            self._equity = before * (1 - scenario.magnitude / 100)
        self._stress_label = None if scenario.kind.value == "reset" else f"SIMULATED {scenario.kind.value} {scenario.magnitude:g}"
        return StressResult(scenario=scenario, equity_before=before, equity_after=self._equity, drawdown_pct=self.gate.drawdown_pct,
                            dd_state=self.gate.state, halted=self.gate.halted, positions_affected=len(self.state.positions),  # type: ignore[arg-type]
                            active_label=self._stress_label)

    def set_min_edge(self, bps: float) -> tuple[float, float]:
        floor = self.settings.min_edge_floor_bps(16.6)
        self._min_edge = bps if self.settings.mode == Mode.PAPER else max(bps, floor)
        return self._min_edge, floor

    def get_receipt(self, receipt_id: str) -> Optional[ExecutionReceipt]:
        return self._receipts.get(receipt_id)

    def activity_log(self, n: int = 30) -> list[McpActivity]:
        return list(self.state.activity)[-n:][::-1]

    # ---- on-chain leg (agentic wallet) + x402 -------------------------------------
    async def wallet_status(self) -> dict:
        """Canned read-only view; no CLI is started and nothing moves."""
        self.calls.append(("wallet_status", {}))
        return {
            "custody": ("Binance's Agentic Wallet holds the key, enforces its own daily limits and performs the "
                        "signing. Deltr never holds, reads, stores or signs with a private key."),
            "binary": self.settings.baw_bin,
            "installed": self.wallet_installed,
            "signed_in": self.wallet_signed_in,
            "status": "CONNECTED" if self.wallet_signed_in else "UNCONNECTED",
            "chain_id": self.settings.wallet_chain_id,
            "armed": self.settings.onchain_arming_error() is None,
            "arming_error": self.settings.onchain_arming_error(),
            "caps": {
                "per_request_usd": self.settings.onchain_max_notional_usd,
                "aggregate_usd": self.settings.onchain_max_aggregate_usd,
                "aggregate_used_usd": self._onchain_notional_usd,
            },
            "gate": {k: self.gate.snapshot()[k] for k in ("state", "halted", "kill_switch", "drawdown_pct")},
        }

    def _onchain_guard(self, notional_usd: float, confirm: bool) -> None:
        snap = self.gate.snapshot()
        if snap["kill_switch"]:
            raise OnchainRefused("KILL_SWITCH", "REFUSED: kill switch engaged; the on-chain leg is closed.")
        if snap["halted"] or self._halted:
            raise OnchainRefused("HALTED_DRAWDOWN", "REFUSED: gate HALTED; the on-chain leg is closed.")
        arming = self.settings.onchain_arming_error()
        if arming:
            raise OnchainRefused("ONCHAIN_NOT_ARMED", f"REFUSED: {arming}.")
        if notional_usd > self.settings.onchain_max_notional_usd:
            raise OnchainRefused("MAX_NOTIONAL", f"REFUSED: ${notional_usd:,.2f} exceeds the on-chain per-request cap.")
        if not confirm:
            raise OnchainRefused("CONFIRM_REQUIRED", "REFUSED: this moves real funds; call again with confirm=true.")
        if not self.wallet_installed:
            raise OnchainRefused("BAW_NOT_INSTALLED", "the Binance Agentic Wallet CLI is not installed")
        if not self.wallet_signed_in:
            raise OnchainRefused("BAW_NOT_SIGNED_IN", "the Binance Agentic Wallet is not signed in")

    async def onchain_swap(self, *, from_token: str, to_token: str, amount: float, notional_usd: float,
                           confirm: bool = False, chain_id: Optional[int] = None,
                           slippage: Optional[float] = None, min_receive: Optional[float] = None) -> dict:
        self.calls.append(("onchain_swap", {"notional_usd": notional_usd, "confirm": confirm}))
        self._onchain_guard(float(notional_usd), confirm)
        self._onchain_notional_usd += float(notional_usd)
        return {
            "decision": {"approved": True, "code": "OK", "reason": "APPROVED: on-chain request."},
            "swap": {"confirmed": True, "status": "FINISHED", "order_id": "1", "tx_hash": "0x" + "a" * 64,
                     "chain_id": chain_id or self.settings.wallet_chain_id, "from_token": from_token,
                     "to_token": to_token, "from_amount": amount, "received_amount": amount,
                     "custody": "Binance's wallet holds the key; Deltr holds no key and signs nothing."},
        }

    async def x402_pay(self, payment_required: Any, *, selected_index: Optional[int] = None, confirm: bool = False) -> dict:
        self.calls.append(("x402_pay", {"confirm": confirm}))
        self._onchain_guard(0.05, confirm)
        return {
            "decision": {"approved": True, "code": "OK", "reason": "APPROVED: x402 payment."},
            "payment": {"payment_id": "pid-fake", "selected_index": selected_index or 0,
                        "header_name": "X-PAYMENT", "header_value": "b64-signed", "header_present": True,
                        "signed_by": "binance-agentic-wallet"},
        }

    def x402_edge_report_challenge(self, *, capital_usd: Optional[float] = None, leverage: float = 2.0,
                                   horizon_h: float = 24.0, resource: Optional[str] = None) -> dict:
        from deltr.payments import X402Seller, artifact_from_edge_report

        seller = X402Seller(pay_to=self.settings.x402_pay_to, price_units=self.settings.x402_price_units)
        artifact = artifact_from_edge_report(
            {"title": f"Deltr edge report: {self.settings.symbol}", "markdown": "# fake edge report\n"},
            symbol=self.settings.symbol,
        )
        return seller.challenge(artifact, resource=resource).as_dict()

    async def run_once(self) -> dict:
        return {"status": self.status().model_dump(mode="json"), "opportunity": self.scan().model_dump(mode="json")}


def build_fake_server(mode: str = "paper", **kw: Any):
    """(engine, activity, mcp) triple around a FakeEngine."""
    from deltr.mcp.activity import ActivityLog
    from deltr.mcp.server import build_mcp

    engine = FakeEngine(make_settings(mode), **kw)
    activity = ActivityLog(engine.state)
    return engine, activity, build_mcp(engine, activity)


if __name__ == "__main__":  # stdio server over the fake engine (stdout-cleanliness test)
    import logging

    from deltr.mcp.server import run_stdio

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    _, _, _mcp = build_fake_server()
    asyncio.run(run_stdio(_mcp))
