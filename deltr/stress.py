"""deltr/stress.py — labelled SIMULATED stress scenarios (section 4.12).

Scenarios mutate only the paper portfolio / leg-failure flag / freshness flag —
never the market feed.  ``State.stress_active`` carries the badge text the UI shows
on StatusBar, Position and Receipt until ``reset``.  In TESTNET a scenario is
refused (``StressRefused``) while real orders are open: stress arithmetic on a real
book would be a lie, and ``dex_leg_fail`` would make a real perp leg reverse.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from deltr.config import Mode, Settings
from deltr.models import StressKind, StressResult, StressScenario, TraceSource

log = logging.getLogger("deltr.stress")

# scenarios that never run against a TESTNET engine (even on a flat book): a later real
# execution would consume the flag / the persisted HALT / the frozen feed
TESTNET_REFUSED = frozenset({StressKind.DEX_LEG_FAIL, StressKind.FEED_STALE, StressKind.EQUITY_SHOCK})
# accepted magnitude ranges: equity_shock in percent of equity, basis_shock in bps, funding_flip a per-settlement rate
MAGNITUDE_RANGE = {
    StressKind.EQUITY_SHOCK: (0.0, 100.0),
    StressKind.BASIS_SHOCK: (-2_000.0, 2_000.0),
    StressKind.FUNDING_FLIP: (-0.05, 0.05),
}


class StressRefused(Exception):
    """Raised when a scenario must not be applied (TESTNET with open real orders)."""


class StressController:
    def __init__(self, state: Any, portfolio: Any, executor: Any, hub: Any, settings: Settings) -> None:
        self.state = state
        self.portfolio = portfolio
        self.executor = executor
        self.hub = hub
        self.settings = settings

    # ------------------------------------------------------------------ helpers
    def active(self) -> Optional[str]:
        return getattr(self.state, "stress_active", None)

    def _set_badge(self, label: Optional[str]) -> None:
        try:
            self.state.stress_active = label
        except Exception as exc:  # pragma: no cover
            log.debug("stress: badge not set: %s", exc)

    def _hub_feed_stale(self, on: bool) -> None:
        """Freeze / thaw the hub's feed (``set_feed_frozen`` on the real hubs; ``set_feed_stale`` on older fakes)."""
        fn = getattr(self.hub, "set_feed_frozen", None) or getattr(self.hub, "set_feed_stale", None)
        if callable(fn):
            try:
                fn(on)
            except Exception as exc:  # pragma: no cover
                log.debug("stress: hub feed freeze hook failed: %s", exc)

    def _emit(self, message: str, level: str = "warn", data: Optional[dict] = None) -> None:
        emit = getattr(self.state, "emit", None)
        if callable(emit):
            try:
                emit("stress", message, level, data or {})
            except Exception:  # pragma: no cover
                pass

    @staticmethod
    def _validate(scenario: StressScenario) -> None:
        """Bound the magnitudes the API / MCP layer accepts (the models are frozen contracts)."""
        m = float(scenario.magnitude)
        if m != m or m in (float("inf"), float("-inf")):
            raise ValueError("stress magnitude must be finite")
        lo, hi = MAGNITUDE_RANGE.get(scenario.kind, (-1e9, 1e9))
        if not (lo <= m <= hi):
            raise ValueError(f"{scenario.kind.value} magnitude {m:g} outside [{lo:g}, {hi:g}]")

    def _real_orders_open(self) -> bool:
        return self.settings.mode == Mode.TESTNET and len(self.portfolio.positions("open")) > 0

    # ------------------------------------------------------------------ API
    async def apply(self, scenario: StressScenario) -> StressResult:
        """Apply one scenario; sets the SIMULATED badge; runs the stop monitor once so a
        breached stop unwinds exactly as it would in production."""
        if scenario.kind == StressKind.RESET:
            return await self.reset()
        self._validate(scenario)
        if self.settings.mode == Mode.TESTNET and scenario.kind in TESTNET_REFUSED:
            raise StressRefused(
                f"stress '{scenario.kind.value}' is refused in TESTNET: it would reverse a real perp order, "
                "persist a simulated HALT into the testnet gate state, or freeze the feed a real order is priced off"
            )
        if self._real_orders_open():
            raise StressRefused(
                f"stress '{scenario.kind.value}' refused in TESTNET while {len(self.portfolio.positions('open'))} real position(s) are open"
            )
        if self.executor is not None and getattr(self.executor, "in_flight", False):
            raise StressRefused("stress refused while an execution is in flight")
        result = self.portfolio.apply_stress(scenario)
        label = result.active_label
        self._set_badge(label)
        if scenario.kind == StressKind.FEED_STALE:
            self._hub_feed_stale(True)
        self._emit(f"stress applied: {label}", data={"kind": scenario.kind.value, "magnitude": scenario.magnitude})
        log.warning("stress: %s equity %.2f -> %.2f dd %.2f%% %s", label, result.equity_before, result.equity_after, result.drawdown_pct, result.dd_state)
        fired: list[str] = list(result.stops_fired)
        check = getattr(self.executor, "check_stops_once", None)
        if callable(check):
            receipts = await check(source=TraceSource.AUTO)
            fired = [r.position_id or r.plan.position_id or "" for r in receipts] or fired
        return result.model_copy(update={"stops_fired": fired, "active_label": label})

    async def reset(self) -> StressResult:
        result = self.portfolio.clear_stress()
        self._hub_feed_stale(False)
        self._set_badge(None)
        self._emit("stress reset", level="info")
        return result.model_copy(update={"active_label": None})


__all__ = ["StressController", "StressRefused", "TESTNET_REFUSED", "MAGNITUDE_RANGE"]
