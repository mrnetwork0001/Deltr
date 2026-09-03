"""
Deltr — Hardened Zero-LLM Deterministic Risk Gate (Subsystem 4)

Every order Deltr routes to Binance (via MCP, the Agent OS bridge, or the REST
executor) passes through `BinanceRiskGate.evaluate()` first.  The gate is:

* **Deterministic** — pure Python, no I/O on the hot path, no randomness, no
  model calls.  Two runs over the same inputs produce byte-identical decisions.
* **Fast** — 19 ordered checks of dict lookups and float comparisons; benchmarked in
  `tests/test_risk_gate.py` (≈0.5–1.5 µs median on Apple Silicon, hard assertion
  < 5 µs so CI on slower machines stays green).
* **Fail-closed** — any missing or malformed field vetoes the trade.  A missing
  leverage is a veto, never "assume 1x".
* **Owns its own truth** — equity, peak equity, drawdown, and the set of open
  positions are fed by the executor/portfolio, *never* by the caller of
  `evaluate()`.  A proposal cannot rescale the 2 % limit by claiming a larger
  balance or fewer open positions.
* **Stateful for drawdown** — `update_equity()` tracks peak equity.  At 2 %
  drawdown the gate enters WARN; at the 3 % stop-loss it HALTS: only verified
  reduce-only unwinds pass until an operator calls `reset_halt()` *after* the
  portfolio has recovered (the peak is never silently re-based).  State is
  persisted per mode so a process restart is not a drawdown-evasion path.

Hard invariants (from DELTR_PROJECT_SPEC.md):
    MAX_LEVERAGE          = 3.0x   futures leverage ceiling
    MAX_CAPITAL_RISK_PCT  = 2 %    of equity at risk per trade
    MAX_DRAWDOWN_PCT      = 3 %    mandatory stop-loss / halt threshold

Backwards compatibility: `evaluate_arbitrage_trade(dict) -> (bool, str)` is
kept for the original hackathon skeleton.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "MAX_CAPITAL_RISK_PCT",
    "MAX_LEVERAGE",
    "MAX_DRAWDOWN_PCT",
    "WARN_DRAWDOWN_PCT",
    "RiskLimits",
    "CheckResult",
    "RiskDecision",
    "OpenPosition",
    "BinanceRiskGate",
    "CHECK_ORDER",
    "benchmark",
]

# --------------------------------------------------------------------------- #
# Hard-coded invariants (spec)                                                #
# --------------------------------------------------------------------------- #
MAX_CAPITAL_RISK_PCT: float = 0.02  # Max 2% of equity per trade
MAX_LEVERAGE: float = 3.0  # Max 3x leverage on Futures
MAX_DRAWDOWN_PCT: float = 0.03  # Mandatory stop-loss at 3% drawdown
WARN_DRAWDOWN_PCT: float = 0.02  # informational WARN state

# Decision / veto codes (stable identifiers surfaced to the UI / MCP / bridge)
OK = "OK"
KILL_SWITCH = "KILL_SWITCH"
HALTED_DRAWDOWN = "HALTED_DRAWDOWN"
MALFORMED = "MALFORMED"
REDUCE_ONLY_UNVERIFIED = "REDUCE_ONLY_UNVERIFIED"
NOT_DELTA_NEUTRAL = "NOT_DELTA_NEUTRAL"
HEDGE_MISMATCH = "HEDGE_MISMATCH"
LEVERAGE = "LEVERAGE"
MIN_NOTIONAL = "MIN_NOTIONAL"
MAX_NOTIONAL = "MAX_NOTIONAL"
CAPITAL_RISK = "CAPITAL_RISK"
AGGREGATE_RISK = "AGGREGATE_RISK"
CAPITAL_CAPACITY = "CAPITAL_CAPACITY"
AGGREGATE_NOTIONAL = "AGGREGATE_NOTIONAL"
SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
MAX_POSITIONS = "MAX_POSITIONS"
STALE_QUOTE = "STALE_QUOTE"
PRICE_DRIFT = "PRICE_DRIFT"
PRICE_SANITY = "PRICE_SANITY"
NEGATIVE_EDGE = "NEGATIVE_EDGE"

# Order in which checks run (cheapest / most decisive first).  Exposed so the
# UI and docs can render the exact gate pipeline.
CHECK_ORDER: Tuple[str, ...] = (
    KILL_SWITCH,
    HALTED_DRAWDOWN,
    MALFORMED,
    REDUCE_ONLY_UNVERIFIED,
    NOT_DELTA_NEUTRAL,
    HEDGE_MISMATCH,
    LEVERAGE,
    MIN_NOTIONAL,
    MAX_NOTIONAL,
    CAPITAL_RISK,
    AGGREGATE_RISK,
    CAPITAL_CAPACITY,
    AGGREGATE_NOTIONAL,
    SYMBOL_NOT_ALLOWED,
    MAX_POSITIONS,
    STALE_QUOTE,
    PRICE_SANITY,
    PRICE_DRIFT,
    NEGATIVE_EDGE,
)

_STATE_NORMAL = "NORMAL"
_STATE_WARN = "WARN"
_STATE_HALTED = "HALTED"


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Tunable limits.  The three spec invariants are clamped so they can be
    tightened but never loosened beyond the hackathon hard bounds."""

    max_leverage: float = MAX_LEVERAGE
    max_capital_risk_pct: float = MAX_CAPITAL_RISK_PCT
    max_drawdown_pct: float = MAX_DRAWDOWN_PCT
    warn_drawdown_pct: float = WARN_DRAWDOWN_PCT
    max_capital_utilization: float = 0.90  # all legs together may use ≤ 90 % of equity
    max_notional_usd: float = 50_000.0  # per-trade cap (PAPER 50k, TESTNET 5k by config)
    min_notional_usd: float = 5.0  # Binance USDⓈ-M MIN_NOTIONAL filter
    hedge_qty_tolerance: float = 0.005  # |dex_qty − perp_qty| ≤ max(1 step, 0.5 %)
    max_open_positions: int = 5
    max_quote_age_ms: float = 5_000.0
    max_price_drift_bps: float = 20.0  # plan price vs execution re-quote
    price_sanity_bps: float = 100.0  # |dex_ref − perp_ref| beyond this is junk data, not edge
    min_expected_edge_bps: float = 0.0
    basis_shock_bps: float = 100.0  # adverse basis move assumed in the risk floor
    default_roundtrip_cost_bps: float = 20.0  # used only if the executor omits it
    allowed_symbols: Tuple[str, ...] = ()  # empty = allow all

    def __post_init__(self) -> None:  # clamp to spec hard bounds
        object.__setattr__(self, "max_leverage", min(float(self.max_leverage), MAX_LEVERAGE))
        object.__setattr__(
            self, "max_capital_risk_pct", min(float(self.max_capital_risk_pct), MAX_CAPITAL_RISK_PCT)
        )
        object.__setattr__(self, "max_drawdown_pct", min(float(self.max_drawdown_pct), MAX_DRAWDOWN_PCT))
        object.__setattr__(
            self, "warn_drawdown_pct", min(float(self.warn_drawdown_pct), self.max_drawdown_pct)
        )
        object.__setattr__(self, "allowed_symbols", tuple(s.upper() for s in self.allowed_symbols))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_leverage": self.max_leverage,
            "max_capital_risk_pct": self.max_capital_risk_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "warn_drawdown_pct": self.warn_drawdown_pct,
            "max_capital_utilization": self.max_capital_utilization,
            "max_notional_usd": self.max_notional_usd,
            "min_notional_usd": self.min_notional_usd,
            "hedge_qty_tolerance": self.hedge_qty_tolerance,
            "max_open_positions": self.max_open_positions,
            "max_quote_age_ms": self.max_quote_age_ms,
            "max_price_drift_bps": self.max_price_drift_bps,
            "price_sanity_bps": self.price_sanity_bps,
            "min_expected_edge_bps": self.min_expected_edge_bps,
            "basis_shock_bps": self.basis_shock_bps,
            "default_roundtrip_cost_bps": self.default_roundtrip_cost_bps,
            "allowed_symbols": list(self.allowed_symbols),
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    observed: Any = None
    limit: Any = None
    unit: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "observed": self.observed, "limit": self.limit, "unit": self.unit}


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    code: str
    reason: str
    latency_ns: int
    observed: Any = None
    limit: Any = None
    unit: str = ""
    checks: List[CheckResult] = field(default_factory=list)  # populated when explain=True

    @property
    def latency_us(self) -> float:
        return self.latency_ns / 1_000.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "code": self.code,
            "reason": self.reason,
            "observed": self.observed,
            "limit": self.limit,
            "unit": self.unit,
            "latency_ns": self.latency_ns,
            "latency_us": round(self.latency_us, 3),
            "checks": [c.as_dict() for c in self.checks],
        }


@dataclass(slots=True)
class OpenPosition:
    position_id: str
    symbol: str
    base_qty: float
    notional_usd: float
    allocated_risk_usd: float
    leverage: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "base_qty": self.base_qty,
            "notional_usd": self.notional_usd,
            "allocated_risk_usd": self.allocated_risk_usd,
            "leverage": self.leverage,
        }


class BinanceRiskGate:
    """Deterministic pre-trade risk gate.

    The **executor** assembles proposals from the portfolio and market state;
    MCP/API callers never touch these fields directly.  Recognised keys::

        symbol            str    e.g. "BNBUSDT"
        notional          float  USD notional of the DEX leg (== perp leg)
        leverage          float  futures leverage for the perp leg (REQUIRED)
        allocated_risk    float  proposer's risk estimate (USD); floored by the gate
        roundtrip_cost_bps float total entry+exit cost estimate (bps)
        dex_side/perp_side str   "BUY"/"SELL"
        dex_qty/perp_qty  float  base quantities of the two legs
        qty_step          float  exchange LOT_SIZE step (default 0.01)
        is_delta_neutral  bool   proposer assertion (must also be re-derivable)
        expected_edge_bps float  net edge after costs (bps)
        quote_age_ms      float  age of the oldest quote used
        price_drift_bps   float  |execution re-quote − plan price| in bps
        dex_ref_price     float  DEX reference price (for PRICE_SANITY)
        perp_ref_price    float  perp reference price (for PRICE_SANITY)
        reduce_only       bool   unwind of an open position (needs position_id)
        position_id       str    registered position being unwound
    """

    __slots__ = (
        "limits",
        "mode",
        "state_path",
        "initial_capital",
        "equity",
        "peak_equity",
        "drawdown_pct",
        "state",
        "kill_switch",
        "positions",
        "decisions",
        "vetoes",
        "_allowed",
        "_open_risk",
        "_open_notional",
        "_open_capital",
    )

    def __init__(
        self,
        capital_usd: float = 10_000.0,
        limits: Optional[RiskLimits] = None,
        allowed_symbols: Iterable[str] = (),
        mode: str = "paper",
        state_path: Optional[str] = None,
        portfolio_balance: Optional[float] = None,  # legacy alias
    ) -> None:
        if portfolio_balance is not None:
            capital_usd = portfolio_balance
        if not _num(capital_usd) or capital_usd <= 0:
            raise ValueError("capital_usd must be a finite number > 0")
        lim = limits or RiskLimits()
        if allowed_symbols:
            lim = RiskLimits(**{**lim.as_dict(), "allowed_symbols": tuple(allowed_symbols)})
        self.limits: RiskLimits = lim
        self.mode: str = str(mode).lower()
        self.state_path: Optional[str] = state_path
        self.initial_capital: float = float(capital_usd)
        self.equity: float = float(capital_usd)
        self.peak_equity: float = float(capital_usd)
        self.drawdown_pct: float = 0.0
        self.state: str = _STATE_NORMAL
        self.kill_switch: bool = False
        self.positions: Dict[str, OpenPosition] = {}
        self.decisions: int = 0
        self.vetoes: int = 0
        self._allowed: Optional[frozenset] = frozenset(lim.allowed_symbols) or None
        self._open_risk: float = 0.0
        self._open_notional: float = 0.0
        self._open_capital: float = 0.0
        if state_path:
            self.load()

    # ------------------------------------------------------------------ #
    # Legacy aliases                                                       #
    # ------------------------------------------------------------------ #
    @property
    def portfolio_balance(self) -> float:
        return self.equity

    @property
    def max_allowed_risk(self) -> float:
        return self.equity * self.limits.max_capital_risk_pct

    @property
    def halted(self) -> bool:
        return self.state == _STATE_HALTED

    @property
    def open_positions(self) -> int:
        return len(self.positions)

    # ------------------------------------------------------------------ #
    # State management (called by executor / portfolio / operator)        #
    # ------------------------------------------------------------------ #
    def update_equity(self, equity: float) -> float:
        """Feed the latest mark-to-close equity.  Returns the drawdown fraction.
        NORMAL → WARN at warn_drawdown_pct, → HALTED (sticky) at max_drawdown_pct."""
        if not _num(equity):
            return self.drawdown_pct
        if equity < 0:
            equity = 0.0  # a wiped book is a 100 % drawdown, not a no-op
        self.equity = float(equity)
        if equity > self.peak_equity:
            self.peak_equity = float(equity)
        self.drawdown_pct = 0.0 if self.peak_equity <= 0 else (self.peak_equity - equity) / self.peak_equity
        if self.drawdown_pct >= self.limits.max_drawdown_pct:
            self.state = _STATE_HALTED
        elif self.state != _STATE_HALTED:
            self.state = _STATE_WARN if self.drawdown_pct >= self.limits.warn_drawdown_pct else _STATE_NORMAL
        self._persist()
        return self.drawdown_pct

    def register_position(
        self, position_id: str, symbol: str, base_qty: float, notional_usd: float, allocated_risk_usd: float, leverage: float
    ) -> None:
        self.positions[position_id] = OpenPosition(
            position_id, symbol.upper(), float(base_qty), float(notional_usd), float(allocated_risk_usd), float(leverage)
        )
        self._recompute_open()
        self._persist()

    def release_position(self, position_id: str) -> None:
        self.positions.pop(position_id, None)
        self._recompute_open()
        self._persist()

    def _recompute_open(self) -> None:
        self._open_risk = sum(p.allocated_risk_usd for p in self.positions.values())
        self._open_notional = sum(p.notional_usd for p in self.positions.values())
        self._open_capital = sum(p.notional_usd * (1.0 + 1.0 / p.leverage) for p in self.positions.values() if p.leverage > 0)

    def set_kill_switch(self, on: bool) -> None:
        self.kill_switch = bool(on)
        self._persist()

    def reset_halt(self) -> bool:
        """Operator action.  Clears the HALTED state **only if** drawdown has
        recovered below the stop-loss (the peak is never re-based here).
        Returns True if the halt was cleared."""
        if self.drawdown_pct >= self.limits.max_drawdown_pct:
            return False
        self.state = _STATE_WARN if self.drawdown_pct >= self.limits.warn_drawdown_pct else _STATE_NORMAL
        self._persist()
        return True

    def rebase_peak(self, new_equity: float, reason: str) -> None:
        """Explicit, logged operator action (e.g. capital top-up).  Requires a
        reason string; re-bases peak and clears WARN/HALTED."""
        if not reason or not reason.strip():
            raise ValueError("rebase_peak requires a non-empty reason")
        if not _num(new_equity) or new_equity <= 0:
            raise ValueError("new_equity must be a finite number > 0")
        self.equity = float(new_equity)
        self.peak_equity = float(new_equity)
        self.drawdown_pct = 0.0
        self.state = _STATE_NORMAL
        self._persist(extra={"last_rebase_reason": reason})

    # Legacy alias kept for the original skeleton
    def set_portfolio_balance(self, balance: float) -> None:
        self.rebase_peak(balance, reason="legacy set_portfolio_balance")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "initial_capital": self.initial_capital,
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "drawdown_pct": self.drawdown_pct,
            "state": self.state,
            "halted": self.halted,
            "kill_switch": self.kill_switch,
            "max_allowed_risk_usd": self.max_allowed_risk,
            "open_positions": self.open_positions,
            "open_risk_usd": self._open_risk,
            "open_notional_usd": self._open_notional,
            "decisions": self.decisions,
            "vetoes": self.vetoes,
            "limits": self.limits.as_dict(),
            "check_order": list(CHECK_ORDER),
            "positions": [p.as_dict() for p in self.positions.values()],
        }

    # ------------------------------------------------------------------ #
    # Persistence (off the hot path)                                      #
    # ------------------------------------------------------------------ #
    def _persist(self, extra: Optional[Dict[str, Any]] = None) -> None:
        if not self.state_path:
            return
        data = {
            "mode": self.mode,
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "drawdown_pct": self.drawdown_pct,
            "state": self.state,
            "kill_switch": self.kill_switch,
            "positions": [p.as_dict() for p in self.positions.values()],
            "decisions": self.decisions,
            "vetoes": self.vetoes,
        }
        if extra:
            data.update(extra)
        tmp = self.state_path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def save(self) -> None:
        self._persist()

    def load(self) -> bool:
        if not self.state_path or not os.path.exists(self.state_path):
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if data.get("mode") != self.mode:
            return False  # never mix paper and testnet state
        if _num(data.get("equity")) and data["equity"] > 0:
            self.equity = float(data["equity"])
        if _num(data.get("peak_equity")) and data["peak_equity"] > 0:
            self.peak_equity = float(data["peak_equity"])
        self.drawdown_pct = 0.0 if self.peak_equity <= 0 else max(0.0, (self.peak_equity - self.equity) / self.peak_equity)
        self.state = data.get("state") if data.get("state") in (_STATE_NORMAL, _STATE_WARN, _STATE_HALTED) else _STATE_NORMAL
        if self.drawdown_pct >= self.limits.max_drawdown_pct:
            self.state = _STATE_HALTED
        self.kill_switch = bool(data.get("kill_switch", False))
        self.decisions = int(data.get("decisions", 0))
        self.vetoes = int(data.get("vetoes", 0))
        self.positions = {}
        for p in data.get("positions", []):
            try:
                self.positions[p["position_id"]] = OpenPosition(
                    p["position_id"], p["symbol"], float(p["base_qty"]), float(p["notional_usd"]),
                    float(p["allocated_risk_usd"]), float(p.get("leverage", 1.0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._recompute_open()
        return True

    # ------------------------------------------------------------------ #
    # Hot path                                                            #
    # ------------------------------------------------------------------ #
    def evaluate(self, p: Mapping[str, Any], explain: bool = False) -> RiskDecision:
        t0 = time.perf_counter_ns()
        lim = self.limits
        get = p.get
        res: Optional[List[CheckResult]] = [] if explain else None
        reduce_only = get("reduce_only", False) is True

        # -- 3. Malformed / fail-closed parsing (done first so we can verify reduce-only) --
        leverage = get("leverage")
        notional = get("notional")
        allocated_risk = get("allocated_risk", 0.0)
        malformed = (
            not _num(leverage) or leverage <= 0
            or not _num(notional) or notional <= 0
            or not _num(allocated_risk) or allocated_risk < 0
        )
        dex_qty = get("dex_qty")
        perp_qty = get("perp_qty")
        legs_given = dex_qty is not None or perp_qty is not None
        if legs_given and (not _num(dex_qty) or not _num(perp_qty) or dex_qty <= 0 or perp_qty <= 0):
            malformed = True
        # Fail closed: the freshness, drift, edge and reference-price inputs are
        # mandatory for a NEW position (an unwind is judged by the registry).
        if not reduce_only and not (
            _num(get("quote_age_ms")) and _num(get("price_drift_bps")) and _num(get("expected_edge_bps"))
            and _num(get("dex_ref_price")) and _num(get("perp_ref_price"))
            and get("dex_ref_price") > 0 and get("perp_ref_price") > 0
        ):
            malformed = True

        # -- verified reduce-only: must reference a registered position with qty <= open --
        verified_unwind = False
        if reduce_only and not malformed:
            pid = get("position_id")
            pos = self.positions.get(pid) if isinstance(pid, str) else None
            if pos is not None:
                q = perp_qty if _num(perp_qty) else (dex_qty if _num(dex_qty) else pos.base_qty)
                sym = get("symbol")
                reversed_legs = get("dex_side", "SELL") == "SELL" and get("perp_side", "BUY") == "BUY"
                same_symbol = isinstance(sym, str) and sym.upper() == pos.symbol
                verified_unwind = reversed_legs and same_symbol and q <= pos.base_qty * (1.0 + lim.hedge_qty_tolerance) + 1e-12

        # 1. Kill switch — nothing passes except verified unwinds
        if res is not None:
            res.append(CheckResult(KILL_SWITCH, not self.kill_switch or verified_unwind, self.kill_switch, False, "bool"))
        if self.kill_switch and not verified_unwind:
            return self._veto(KILL_SWITCH, "VETO: kill switch engaged; only verified reduce-only unwinds allowed.", t0, res, True, False, "bool")

        # 2. Drawdown halt (sticky) — only verified unwinds pass
        if res is not None:
            res.append(CheckResult(HALTED_DRAWDOWN, self.state != _STATE_HALTED or verified_unwind, round(self.drawdown_pct * 1e4, 2), round(lim.max_drawdown_pct * 1e4, 2), "bps"))
        if self.state == _STATE_HALTED and not verified_unwind:
            return self._veto(
                HALTED_DRAWDOWN,
                f"VETO: drawdown {self.drawdown_pct * 100:.2f}% ≥ {lim.max_drawdown_pct * 100:.1f}% stop-loss; gate HALTED until the portfolio recovers and an operator calls reset_halt().",
                t0, res, round(self.drawdown_pct * 1e4, 2), round(lim.max_drawdown_pct * 1e4, 2), "bps",
            )

        # 3. Malformed
        if res is not None:
            res.append(CheckResult(MALFORMED, not malformed, {"leverage": leverage, "notional": notional, "allocated_risk": allocated_risk}, "finite, >0", ""))
        if malformed:
            return self._veto(MALFORMED, "VETO: proposal missing or malformed fields (leverage/notional/allocated_risk/legs/quote_age_ms/price_drift_bps/expected_edge_bps/ref prices) — fail closed.", t0, res, None, "finite, >0", "")

        # 4. Reduce-only must be verified against a registered position
        if res is not None:
            res.append(CheckResult(REDUCE_ONLY_UNVERIFIED, (not reduce_only) or verified_unwind, get("position_id"), "registered position_id", ""))
        if reduce_only:
            if not verified_unwind:
                return self._veto(REDUCE_ONLY_UNVERIFIED, "VETO: reduce_only requires a registered position_id and qty ≤ open qty.", t0, res, get("position_id"), "registered position_id", "")
            # A verified unwind reduces risk: approve without the entry checks.
            self.decisions += 1
            d = RiskDecision(True, OK, "APPROVED: verified reduce-only unwind of an open position.", time.perf_counter_ns() - t0)
            if res is not None:
                d.checks = res
            return d

        # 5. Delta-neutral pairing: asserted by the proposer AND re-derived from the legs
        dex_side = get("dex_side", "BUY")
        perp_side = get("perp_side", "SELL")
        neutral = get("is_delta_neutral", False) is True and dex_side == "BUY" and perp_side == "SELL"
        if res is not None:
            res.append(CheckResult(NOT_DELTA_NEUTRAL, neutral, f"dex {dex_side} / perp {perp_side}", "dex BUY / perp SELL", ""))
        if not neutral:
            return self._veto(NOT_DELTA_NEUTRAL, "VETO: Only paired delta-neutral arbitrage (DEX Long + CEX Short) is permitted.", t0, res, f"dex {dex_side} / perp {perp_side}", "dex BUY / perp SELL", "")

        # 6. Hedge legs must match in size: |Δ| ≤ max(1 step, tolerance)
        if legs_given:
            step = get("qty_step", 0.01)
            step = step if _num(step) and step > 0 else 0.01
            big = dex_qty if dex_qty > perp_qty else perp_qty
            diff = abs(dex_qty - perp_qty)
            allowed_diff = step if step > big * lim.hedge_qty_tolerance else big * lim.hedge_qty_tolerance
            ok = diff <= allowed_diff + 1e-12
            if res is not None:
                res.append(CheckResult(HEDGE_MISMATCH, ok, round(diff, 8), round(allowed_diff, 8), "base"))
            if not ok:
                return self._veto(
                    HEDGE_MISMATCH,
                    f"VETO: Hedge legs mismatched (DEX {dex_qty:g} vs perp {perp_qty:g}, Δ {diff:g} > {allowed_diff:g}) — position would not be delta-neutral.",
                    t0, res, round(diff, 8), round(allowed_diff, 8), "base",
                )
        elif res is not None:
            res.append(CheckResult(HEDGE_MISMATCH, True, None, None, "base"))

        # 7. Leverage ceiling
        if res is not None:
            res.append(CheckResult(LEVERAGE, leverage <= lim.max_leverage, leverage, lim.max_leverage, "x"))
        if leverage > lim.max_leverage:
            return self._veto(LEVERAGE, f"VETO: Leverage {leverage:g}x exceeds {lim.max_leverage:g}x limit.", t0, res, leverage, lim.max_leverage, "x")

        # 8. Exchange minimum notional
        if res is not None:
            res.append(CheckResult(MIN_NOTIONAL, notional >= lim.min_notional_usd, round(notional, 2), lim.min_notional_usd, "USD"))
        if notional < lim.min_notional_usd:
            return self._veto(MIN_NOTIONAL, f"VETO: Notional ${notional:.2f} below exchange minimum ${lim.min_notional_usd:.2f}.", t0, res, round(notional, 2), lim.min_notional_usd, "USD")

        # 9. Per-trade notional cap (per mode)
        if res is not None:
            res.append(CheckResult(MAX_NOTIONAL, notional <= lim.max_notional_usd, round(notional, 2), lim.max_notional_usd, "USD"))
        if notional > lim.max_notional_usd:
            return self._veto(MAX_NOTIONAL, f"VETO: Notional ${notional:.2f} exceeds {self.mode} cap ${lim.max_notional_usd:.2f}.", t0, res, round(notional, 2), lim.max_notional_usd, "USD")

        # 10. Capital at risk ≤ 2 % of equity.  The gate floors the proposer's
        #     estimate with notional × (roundtrip cost + basis shock).
        rt = get("roundtrip_cost_bps")
        rt = rt if _num(rt) and rt >= 0 else lim.default_roundtrip_cost_bps
        risk_floor = notional * (rt + lim.basis_shock_bps) / 1e4
        risk = allocated_risk if allocated_risk > risk_floor else risk_floor
        max_risk = self.equity * lim.max_capital_risk_pct
        if res is not None:
            res.append(CheckResult(CAPITAL_RISK, risk <= max_risk, round(risk, 2), round(max_risk, 2), "USD"))
        if risk > max_risk:
            return self._veto(
                CAPITAL_RISK,
                f"VETO: Risk ${risk:.2f} (max of proposer ${allocated_risk:.2f} and floor ${risk_floor:.2f}) exceeds {lim.max_capital_risk_pct * 100:.0f}% of equity (${max_risk:.2f}).",
                t0, res, round(risk, 2), round(max_risk, 2), "USD",
            )

        # 11. Aggregate risk budget: open risk + new ≤ (max_dd − current_dd) × equity
        budget = (lim.max_drawdown_pct - self.drawdown_pct) * self.equity
        agg = self._open_risk + risk
        if res is not None:
            res.append(CheckResult(AGGREGATE_RISK, agg <= budget, round(agg, 2), round(budget, 2), "USD"))
        if agg > budget:
            return self._veto(
                AGGREGATE_RISK,
                f"VETO: Aggregate risk ${agg:.2f} (open ${self._open_risk:.2f} + new ${risk:.2f}) exceeds remaining drawdown budget ${budget:.2f}.",
                t0, res, round(agg, 2), round(budget, 2), "USD",
            )

        # 12. Capital sufficiency for BOTH legs across all positions:
        #     DEX spot needs full notional; the perp short needs notional/leverage margin.
        required = notional * (1.0 + 1.0 / leverage)
        cap = self.equity * lim.max_capital_utilization
        total_cap = self._open_capital + required
        if res is not None:
            res.append(CheckResult(CAPITAL_CAPACITY, total_cap <= cap, round(total_cap, 2), round(cap, 2), "USD"))
        if total_cap > cap:
            return self._veto(
                CAPITAL_CAPACITY,
                f"VETO: Legs need ${required:.2f} (spot ${notional:.2f} + margin ${notional / leverage:.2f}) on top of ${self._open_capital:.2f} in use > {lim.max_capital_utilization * 100:.0f}% of ${self.equity:.2f} equity.",
                t0, res, round(total_cap, 2), round(cap, 2), "USD",
            )

        # 13. Aggregate notional ≤ max_leverage × equity
        agg_n = self._open_notional + notional
        cap_n = lim.max_leverage * self.equity
        if res is not None:
            res.append(CheckResult(AGGREGATE_NOTIONAL, agg_n <= cap_n, round(agg_n, 2), round(cap_n, 2), "USD"))
        if agg_n > cap_n:
            return self._veto(AGGREGATE_NOTIONAL, f"VETO: Aggregate notional ${agg_n:.2f} exceeds {lim.max_leverage:g}× equity (${cap_n:.2f}).", t0, res, round(agg_n, 2), round(cap_n, 2), "USD")

        # 14. Symbol whitelist
        sym = get("symbol")
        allowed = self._allowed
        sym_ok = allowed is None or (isinstance(sym, str) and sym.upper() in allowed)
        if res is not None:
            res.append(CheckResult(SYMBOL_NOT_ALLOWED, sym_ok, sym, list(allowed) if allowed else "any", ""))
        if not sym_ok:
            return self._veto(SYMBOL_NOT_ALLOWED, f"VETO: Symbol {sym!r} not in allowed list.", t0, res, sym, list(allowed) if allowed else "any", "")

        # 15. Max concurrent positions (gate-owned count)
        n = len(self.positions)
        if res is not None:
            res.append(CheckResult(MAX_POSITIONS, n < lim.max_open_positions, n, lim.max_open_positions, "positions"))
        if n >= lim.max_open_positions:
            return self._veto(MAX_POSITIONS, f"VETO: {n} open positions ≥ max {lim.max_open_positions}.", t0, res, n, lim.max_open_positions, "positions")

        # 16. Stale quotes
        age = get("quote_age_ms")
        stale = _num(age) and age > lim.max_quote_age_ms
        if res is not None:
            res.append(CheckResult(STALE_QUOTE, not stale, age, lim.max_quote_age_ms, "ms"))
        if stale:
            return self._veto(STALE_QUOTE, f"VETO: Quote age {age:.0f} ms > {lim.max_quote_age_ms:.0f} ms — refusing to trade on stale prices.", t0, res, age, lim.max_quote_age_ms, "ms")

        # 16b. Price sanity: a DEX/perp gap beyond price_sanity_bps is bad data (thin
        #     testnet book, stuck RPC), never a real edge.
        dref = get("dex_ref_price")
        pref = get("perp_ref_price")
        if _num(dref) and _num(pref) and dref > 0 and pref > 0:
            gap = abs(pref - dref) / dref * 1e4
            sane = gap <= lim.price_sanity_bps
            if res is not None:
                res.append(CheckResult(PRICE_SANITY, sane, round(gap, 2), lim.price_sanity_bps, "bps"))
            if not sane:
                return self._veto(PRICE_SANITY, f"VETO: DEX/perp reference gap {gap:.1f} bps > {lim.price_sanity_bps:.0f} bps — treating as bad data, not edge.", t0, res, round(gap, 2), lim.price_sanity_bps, "bps")
        elif res is not None:
            res.append(CheckResult(PRICE_SANITY, True, None, lim.price_sanity_bps, "bps"))

        # 17. Price drift between plan and execution re-quote
        drift = get("price_drift_bps")
        drifted = _num(drift) and abs(drift) > lim.max_price_drift_bps
        if res is not None:
            res.append(CheckResult(PRICE_DRIFT, not drifted, drift, lim.max_price_drift_bps, "bps"))
        if drifted:
            return self._veto(PRICE_DRIFT, f"VETO: Price drifted {drift:.1f} bps since the plan was priced (> {lim.max_price_drift_bps:.0f} bps) — re-propose.", t0, res, drift, lim.max_price_drift_bps, "bps")

        # 18. Expected edge must be non-negative (never pay to enter)
        edge = get("expected_edge_bps")
        bad_edge = _num(edge) and edge < lim.min_expected_edge_bps
        if res is not None:
            res.append(CheckResult(NEGATIVE_EDGE, not bad_edge, edge, lim.min_expected_edge_bps, "bps"))
        if bad_edge:
            return self._veto(NEGATIVE_EDGE, f"VETO: Expected edge {edge:.2f} bps < minimum {lim.min_expected_edge_bps:.2f} bps.", t0, res, edge, lim.min_expected_edge_bps, "bps")

        self.decisions += 1
        d = RiskDecision(True, OK, "APPROVED: Trade satisfies all Binance risk gates.", time.perf_counter_ns() - t0)
        if res is not None:
            d.checks = res
        return d

    # ------------------------------------------------------------------ #
    def _veto(self, code: str, reason: str, t0: int, res: Optional[List[CheckResult]], observed: Any, limit: Any, unit: str) -> RiskDecision:
        self.decisions += 1
        self.vetoes += 1
        d = RiskDecision(False, code, reason, time.perf_counter_ns() - t0, observed, limit, unit)
        if res is not None:
            d.checks = res
        return d

    # ------------------------------------------------------------------ #
    # Convenience / legacy API                                             #
    # ------------------------------------------------------------------ #
    def explain(self, p: Mapping[str, Any]) -> RiskDecision:
        """Slow path: same verdict, plus the per-check list with observed/limit."""
        return self.evaluate(p, explain=True)

    def evaluate_arbitrage_trade(self, trade_proposal: Dict[str, Any]) -> Tuple[bool, str]:
        """Original skeleton signature: returns (is_approved, reason).
        Proposals lacking `notional` are treated as notional = allocated_risk
        so the legacy sample keeps working."""
        p = dict(trade_proposal)
        if "notional" not in p:
            p["notional"] = max(float(p.get("allocated_risk", 0.0)), 5.0)
        # the skeleton never supplied freshness/drift/edge/reference inputs: treat them as neutral
        p.setdefault("quote_age_ms", 0.0)
        p.setdefault("price_drift_bps", 0.0)
        p.setdefault("expected_edge_bps", 0.0)
        p.setdefault("dex_ref_price", 1.0)
        p.setdefault("perp_ref_price", 1.0)
        d = self.evaluate(p)
        return d.approved, d.reason


if __name__ == "__main__":
    gate = BinanceRiskGate(capital_usd=10_000.0)
    sample_trade = {
        "pair": "BNB/USDT",
        "symbol": "BNBUSDT",
        "strategy": "PancakeSwap Long + Binance Futures Short",
        "is_delta_neutral": True,
        "leverage": 2.0,
        "allocated_risk": 150.0,
        "notional": 3_000.0,
        "dex_qty": 4.38,
        "perp_qty": 4.38,
        "quote_age_ms": 120.0,
        "price_drift_bps": 1.0,
        "expected_edge_bps": 5.0,
        "dex_ref_price": 686.19,
        "perp_ref_price": 686.34,
    }
    approved, reason = gate.evaluate_arbitrage_trade(sample_trade)
    print(f"Binance Risk Evaluation Result: {approved} -> {reason}")
    import statistics

    N = 100_000
    lat = [gate.evaluate(sample_trade).latency_ns for _ in range(N)]
    print(f"evaluate(): median {statistics.median(lat) / 1000:.2f} µs, p99 {sorted(lat)[int(N * 0.99)] / 1000:.2f} µs over {N:,} runs")


def benchmark(iterations: int = 10_000, capital_usd: float = 10_000.0) -> Tuple[float, float, float]:
    """Measure the hot path on this machine: (median_us, p99_us, amortised_us).
    Used once at startup so the banner/UI quote a *measured* number, never the target."""
    import statistics

    gate = BinanceRiskGate(capital_usd=capital_usd)
    p = {
        "symbol": "BNBUSDT", "is_delta_neutral": True, "dex_side": "BUY", "perp_side": "SELL",
        "leverage": 2.0, "allocated_risk": 40.0, "roundtrip_cost_bps": 16.6, "notional": 3_328.0,
        "dex_qty": 4.85, "perp_qty": 4.85, "qty_step": 0.01, "quote_age_ms": 120.0,
        "price_drift_bps": 1.0, "dex_ref_price": 686.19, "perp_ref_price": 686.34, "expected_edge_bps": 5.0,
    }
    for _ in range(200):
        gate.evaluate(p)
    t0 = time.perf_counter_ns()
    lat = [gate.evaluate(p).latency_ns for _ in range(iterations)]
    amortised = (time.perf_counter_ns() - t0) / iterations / 1_000.0
    lat.sort()
    return statistics.median(lat) / 1_000.0, lat[int(iterations * 0.99)] / 1_000.0, amortised
