"""deltr/state.py — the single in-memory State shared by every subsystem.

Holds the latest market/edge/opportunity, bounded histories (deques), the
single-use / TTL-pruned plan store, open positions, the paper portfolio view,
venue health and the MCP-upstream status.  ``snapshot()`` renders the ONLY
payload the dashboard consumes; ``system_status()`` renders its status block.

State is constructible with just ``(settings, bus)`` and every snapshot field
has a sane default before anything has happened, so the API and UI can start
before the first tick.  Nothing here performs I/O and nothing prints to stdout
(``emit`` logs to stderr through ``logging``).
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from deltr.bus import EventBus
from deltr.config import Settings
from deltr.models import (
    ArbOpportunity,
    EdgeBreakdown,
    ExecutionReceipt,
    HedgePlan,
    MarketState,
    McpActivity,
    PortfolioSnapshot,
    Position,
    PromptResult,
    RiskDecisionRecord,
    Snapshot,
    SpreadPoint,
    SystemStatus,
    UpstreamStatus,
    VenueHealth,
    utcnow,
)

log = logging.getLogger("deltr.state")

HISTORY_MAXLEN = 600
DECISIONS_MAXLEN = 100
RECEIPTS_MAXLEN = 10
PROMPTS_MAXLEN = 5
ACTIVITY_MAXLEN = 50
EVENTS_IN_SNAPSHOT = 100
EXPIRED_PLANS_KEEP = 200  # ids of pruned / expired plans, so PLAN_EXPIRED stays distinguishable from PLAN_NOT_FOUND

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}

PlanStatus = Literal["pending", "expired", "missing"]


def _aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC so TTL comparisons never raise."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def empty_portfolio(capital_usd: float, ts: Optional[datetime] = None) -> PortfolioSnapshot:
    """A flat portfolio: all capital in cash, no drawdown, no positions."""
    now = ts or utcnow()
    return PortfolioSnapshot(
        equity_usd=capital_usd,
        cash_usd=capital_usd,
        reserved_cash_usd=0.0,
        peak_equity_usd=capital_usd,
        drawdown_pct=0.0,
        dd_state="NORMAL",
        total_pnl_usd=0.0,
        spread_pnl_usd=0.0,
        funding_pnl_usd=0.0,
        fees_paid_usd=0.0,
        open_positions=0,
        daily_realized_usd=0.0,
        equity_curve=[(now, capital_usd)],
        ts=now,
    )


class State:
    """Mutable engine state.  One instance per process; the Engine is the only writer of most fields."""

    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings: Settings = settings
        self.bus: EventBus = bus
        self.started_at: datetime = utcnow()
        self.mode = settings.mode
        self.replay: bool = bool(settings.replay_path)

        # latest market view (written by the market hub / scout)
        self.market: Optional[MarketState] = None
        self.edge: Optional[EdgeBreakdown] = None
        self.opportunity: Optional[ArbOpportunity] = None

        # bounded histories
        self.history: deque[SpreadPoint] = deque(maxlen=HISTORY_MAXLEN)
        self.decisions: deque[RiskDecisionRecord] = deque(maxlen=DECISIONS_MAXLEN)
        self.receipts: deque[ExecutionReceipt] = deque(maxlen=RECEIPTS_MAXLEN)
        self.prompts: deque[PromptResult] = deque(maxlen=PROMPTS_MAXLEN)
        self.activity: deque[McpActivity] = deque(maxlen=ACTIVITY_MAXLEN)

        # plans (pending, single-use, TTL-pruned) and positions
        self.plans: dict[str, HedgePlan] = {}
        self._expired_plan_ids: deque[str] = deque(maxlen=EXPIRED_PLANS_KEEP)
        self.positions: dict[str, Position] = {}
        self.portfolio: PortfolioSnapshot = empty_portfolio(settings.capital_usd, self.started_at)

        # venues / upstream / knobs
        self.venues: dict[str, VenueHealth] = {}
        self.upstream: Optional[UpstreamStatus] = None
        self.stress_active: Optional[str] = None
        self.min_edge_bps: float = float(settings.min_edge_bps)
        self.min_edge_floor_bps: float = settings.min_edge_floor_bps(0.0)
        self.gate_median_us: float = 0.0

    # ------------------------------------------------------------------ plans
    def put_plan(self, plan: HedgePlan) -> None:
        """Store a pending plan (keyed by ``plan.id``); prunes expired plans first."""
        self.prune_plans()
        self.plans[plan.id] = plan

    def take_plan(self, plan_id: str, now: Optional[datetime] = None) -> Optional[HedgePlan]:
        """Pop a plan for execution.  ``None`` if it is missing OR expired (expired plans are discarded)."""
        plan = self.plans.pop(plan_id, None)
        if plan is None:
            return None
        if _aware(plan.expires_at) <= _aware(now or utcnow()):
            self._remember_expired(plan_id)
            return None
        return plan

    def _remember_expired(self, plan_id: str) -> None:
        if plan_id not in self._expired_plan_ids:
            self._expired_plan_ids.append(plan_id)

    def peek_plan(self, plan_id: str) -> Optional[HedgePlan]:
        """Look at a stored plan without consuming it (expired-but-unpruned plans are still returned)."""
        return self.plans.get(plan_id)

    def plan_status(self, plan_id: str, now: Optional[datetime] = None) -> PlanStatus:
        """``pending`` | ``expired`` | ``missing`` — lets callers distinguish PLAN_EXPIRED from PLAN_NOT_FOUND."""
        plan = self.plans.get(plan_id)
        if plan is None:
            return "expired" if plan_id in self._expired_plan_ids else "missing"
        return "expired" if _aware(plan.expires_at) <= _aware(now or utcnow()) else "pending"

    def prune_plans(self, now: Optional[datetime] = None) -> int:
        """Drop every expired plan; returns how many were removed."""
        ref = _aware(now or utcnow())
        expired = [pid for pid, p in self.plans.items() if _aware(p.expires_at) <= ref]
        for pid in expired:
            del self.plans[pid]
            self._remember_expired(pid)
        return len(expired)

    # ---------------------------------------------------------------- records
    def record_decision(self, d: RiskDecisionRecord) -> None:
        self.decisions.append(d)

    def record_receipt(self, r: ExecutionReceipt) -> None:
        self.receipts.append(r)

    def record_prompt(self, p: PromptResult) -> None:
        self.prompts.append(p)

    def record_activity(self, a: McpActivity) -> None:
        self.activity.append(a)

    def record_venue(self, h: VenueHealth) -> None:
        self.venues[h.name] = h

    # ------------------------------------------------------------------ events
    def emit(self, topic: str, message: str, level: str = "info", data: Optional[dict[str, Any]] = None):
        """Publish on the bus AND log to stderr (never stdout — the stdio MCP transport owns it)."""
        ev = self.bus.publish(topic, message, level=level, data=data)
        log.log(_LEVELS.get(level, logging.INFO), "[%s] %s", topic, message)
        return ev

    # ------------------------------------------------------------------ views
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status == "open"]

    def uptime_s(self, now: Optional[datetime] = None) -> int:
        return max(0, int((_aware(now or utcnow()) - _aware(self.started_at)).total_seconds()))

    def system_status(self, gate_snapshot: Optional[dict[str, Any]] = None) -> SystemStatus:
        """Status block.  ``gate_snapshot`` is ``BinanceRiskGate.snapshot()`` (may be empty before the gate exists)."""
        g = gate_snapshot or {}
        s = self.settings
        limits = g.get("limits")
        if not isinstance(limits, dict):
            limits = s.risk_limits().as_dict()
        check_order = g.get("check_order")
        if not isinstance(check_order, list):
            check_order = []
        dd_state = g.get("state") or self.portfolio.dd_state
        if dd_state not in ("NORMAL", "WARN", "HALTED"):
            dd_state = "NORMAL"
        # ONE unit for every *_pct field a client sees: PERCENT (the gate reports a fraction)
        gate_dd = g.get("drawdown_pct")
        drawdown_pct = float(gate_dd) * 100.0 if isinstance(gate_dd, (int, float)) and not isinstance(gate_dd, bool) else float(self.portfolio.drawdown_pct)
        return SystemStatus(
            mode=s.mode,
            symbol=s.symbol,
            version=s.version,
            uptime_s=self.uptime_s(),
            started_at=self.started_at,
            halted=bool(g.get("halted", False)),
            kill_switch=bool(g.get("kill_switch", False)),
            dd_state=dd_state,
            equity_usd=float(self.portfolio.equity_usd),
            drawdown_pct=drawdown_pct,
            open_positions=len(self.open_positions()),
            venues=list(self.venues.values()),
            upstream=self.upstream,
            secrets_present=s.secrets_present,
            binance_api_env=s.binance_api_env,
            replay=self.replay,
            stress_active=self.stress_active,
            gate_median_us=float(self.gate_median_us),
            min_edge_bps=float(self.min_edge_bps),
            min_edge_floor_bps=float(self.min_edge_floor_bps),
            leg_order=s.leg_order,
            limits=dict(limits),
            check_order=[str(c) for c in check_order],
            execution_style=(None if s.mode.value == "paper" else s.execution_style.value),
            execution_style_label=s.execution_style_label,
            data_source=s.data_source_label,
            # real_funds_armed is asserted by the engine only after live_preflight() passed;
            # settings alone can say "configured for LIVE", never "armed".
            real_funds_armed=False,
            onchain_armed=s.onchain_arming_error() is None,
            wallet_address=None,
            max_notional_usd=float(s.max_notional_usd),
            max_aggregate_usd=(None if s.max_aggregate_usd == float("inf") else float(s.max_aggregate_usd)),
        )

    def snapshot(self, gate_snapshot: Optional[dict[str, Any]] = None) -> Snapshot:
        """The dashboard payload (GET /api/snapshot, WS /ws/stream)."""
        return Snapshot(
            status=self.system_status(gate_snapshot),
            market=self.market,
            edge=self.edge,
            opportunity=self.opportunity,
            history=list(self.history),
            decisions=list(self.decisions),
            positions=list(self.positions.values()),
            portfolio=self.portfolio,
            receipts=list(self.receipts),
            prompts=list(self.prompts),
            activity=list(self.activity),
            events=self.bus.history(EVENTS_IN_SNAPSHOT),
            ts=utcnow(),
        )


__all__ = [
    "State",
    "empty_portfolio",
    "HISTORY_MAXLEN",
    "DECISIONS_MAXLEN",
    "RECEIPTS_MAXLEN",
    "PROMPTS_MAXLEN",
    "ACTIVITY_MAXLEN",
    "EVENTS_IN_SNAPSHOT",
    "EXPIRED_PLANS_KEEP",
]
