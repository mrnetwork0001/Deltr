"""deltr/mcp/server.py — the Deltr MCP server (FastMCP, name ``deltr``).

Nineteen tools that wrap the :class:`deltr.engine.Engine` facade (section 4.14 of
the build plan).  Design rules:

* **Two-phase trading.** ``deltr_propose_hedge`` / ``deltr_prompt`` only build a
  plan (``plan_id`` + risk pre-check); nothing trades until
  ``deltr_execute_hedge(plan_id)`` re-prices, re-gates and consumes the plan
  (single use, 60 s TTL).  ``deltr_evaluate_risk`` is a dry-run probe.
* **No mode-changing tool.**  PAPER/TESTNET is an immutable launch flag.
* **Errors are values.**  Tools never raise through the transport; failures come
  back as ``{"error": {"code": ..., "message": ...}}``.
* **Every call is recorded** by :func:`deltr.mcp.activity.instrumented` with the
  calling client's ``clientInfo`` so the dashboard shows what the LLM did.
* **Stdout is sacred** (stdio transport): all logging goes to stderr.

The engine is taken by duck type: any object exposing the 4.14 surface works
(the tests use ``tests/helpers_mcp.FakeEngine``).
"""

from __future__ import annotations

import inspect

import dataclasses
import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Awaitable, Callable, Literal, Optional, TypeVar

from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field, ValidationError

from deltr.config import Settings
from deltr.funding_history import DEFAULT_LOOKBACK_DAYS, render_funding_report
from deltr.horizon import horizon_analysis, render_edge_report
from deltr.mcp.activity import ActivityLog, client_name, instrumented, redact_urls
from deltr.models import StressKind, StressScenario, TraceSource

log = logging.getLogger("deltr.mcp.server")

T = TypeVar("T")

VETO_SENTENCE = (
    "A VETO is final for these inputs — do not retry with the same parameters; "
    "change capital/leverage or ask deltr_explain_edge."
)

TOOL_NAMES: tuple[str, ...] = (
    "deltr_status",
    "deltr_market",
    "deltr_scan",
    "deltr_explain_edge",
    "deltr_propose_hedge",
    "deltr_evaluate_risk",
    "deltr_execute_hedge",
    "deltr_unwind",
    "deltr_positions",
    "deltr_risk_log",
    "deltr_receipt",
    "deltr_activity",
    "deltr_prompt",
    "deltr_kill_switch",
    "deltr_reset_halt",
    "deltr_set_min_edge",
    "deltr_stress",
    "deltr_edge_report",
    "deltr_funding_history",
    "deltr_wallet_status",
    "deltr_onchain_swap",
    "deltr_x402_pay",
)

ERROR_CODES: frozenset[str] = frozenset(
    {
        "PLAN_NOT_FOUND",
        "PLAN_EXPIRED",
        "CONFIRM_REQUIRED",
        "EXECUTION_IN_FLIGHT",
        "STRESS_REFUSED",
        "HALT_NOT_CLEARABLE",
        "INVALID_ARGUMENT",
        "RECEIPT_NOT_FOUND",
        "POSITION_NOT_FOUND",
        "INTERNAL_ERROR",
        # on-chain leg (Binance Agentic Wallet) and x402 payments
        "ONCHAIN_NOT_ARMED",
        "CHAIN_NOT_ALLOWED",
        "BAW_NOT_INSTALLED",
        "BAW_NOT_SIGNED_IN",
        "BAW_TIMEOUT",
        "BAW_BAD_JSON",
        "BAW_CLI_ERROR",
        "SECRET_IN_ARGV",
        "SWAP_PREVIEW_REJECTED",
        "SWAP_UNCONFIRMED",
        "BAD_PAYMENT_REQUIRED",
        "X402_NETWORK_NOT_ALLOWED",
        "X402_AMOUNT_OVER_CAP",
        "X402_NO_ACCEPTABLE_OPTION",
        "X402_SIGN_FAILED",
        "X402_NO_PAY_TO",
    }
)

INSTRUCTIONS = (
    "Deltr is a CEX<->DEX delta-neutral arbitrage agent: it buys BNB on PancakeSwap V3 (BSC mainnet "
    "quotes) and shorts the same quantity on Binance USDS-M Futures (testnet), earning the basis plus "
    "funding.  It runs in PAPER (simulated fills on live prices) or TESTNET (real Binance Futures TESTNET "
    "perp orders; the PancakeSwap leg is always simulated at the live QuoterV2 price and badged "
    "simulated/paper in the receipt); the mode is fixed at launch and no tool can change it.\n\n"
    "Workflow: deltr_status -> deltr_market / deltr_scan -> deltr_explain_edge(capital, leverage) -> "
    "deltr_propose_hedge(capital_usd, leverage) which returns a plan_id and a risk pre-check (nothing "
    "trades) -> deltr_execute_hedge(plan_id, confirm=true) which re-prices, re-gates and executes once "
    "(plans expire after 60 s and are single-use).  deltr_prompt(text) turns natural language into a "
    "proposal only.  deltr_evaluate_risk is a dry run of the 19-check deterministic risk gate "
    "(max 3x leverage, max 2% capital at risk, 3% drawdown halt).  A VETO is deterministic: identical "
    "inputs always veto again, so change capital/leverage instead of retrying.  Errors come back as "
    "{\"error\": {\"code\", \"message\"}}.  deltr_stress mutates only the paper book and is badged SIMULATED."
)

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


# --------------------------------------------------------------------------- serialisation helpers
def dump(obj: Any) -> Any:
    """JSON-native view of pydantic models, dataclasses, enums, Decimals, datetimes, lists and dicts."""
    if isinstance(obj, Settings):
        # Settings is a BaseModel, so the branch below would ``model_dump()`` it and put
        # BINANCE_API_KEY, BINANCE_SECRET_KEY and BINANCE_MCP_TOKEN on the wire in full.  Nothing
        # currently hands one to a tool; this is the guard that keeps it that way, because the
        # cost of the mistake is the operator's mainnet credentials in an LLM's context.
        return obj.redacted()
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [dump(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """The one error envelope every tool returns instead of raising."""
    out: dict[str, Any] = {"error": {"code": code, "message": message}}
    out.update(extra)
    return out


def _code_from_exception(exc: BaseException) -> str:
    """Map an engine exception to a Deltr error code.

    Precedence: an explicit ``code`` attribute → the CamelCase class name as
    UPPER_SNAKE (``PlanNotFound`` → ``PLAN_NOT_FOUND``) when that is a known code →
    a known code mentioned in the message → ``INVALID_ARGUMENT`` for
    ``ValueError``/``TypeError``/``KeyError`` → ``INTERNAL_ERROR``.
    """
    explicit = getattr(exc, "code", None)
    if isinstance(explicit, str) and explicit:
        return explicit.upper()
    by_name = _CAMEL_RE.sub("_", type(exc).__name__).upper()
    if by_name in ERROR_CODES:
        return by_name
    text = str(exc).upper()
    for known in ERROR_CODES:
        if known in text:
            return known
    if isinstance(exc, (ValueError, TypeError, KeyError, LookupError, ValidationError)):
        return "INVALID_ARGUMENT"
    return "INTERNAL_ERROR"


def _errored(exc: BaseException) -> dict[str, Any]:
    code = _code_from_exception(exc)
    if code == "INTERNAL_ERROR":
        log.exception("MCP tool failed")
    return error(code, str(exc) or type(exc).__name__)


async def _call_async(fn: Callable[[], Awaitable[T]]) -> T | dict[str, Any]:
    try:
        return await fn()
    except Exception as exc:  # noqa: BLE001 — errors become values on the wire
        return _errored(exc)


def _call(fn: Callable[[], T]) -> T | dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return _errored(exc)


def _is_error(x: Any) -> bool:
    return isinstance(x, dict) and isinstance(x.get("error"), dict)


def _receipt_or_error(receipt: Any) -> dict[str, Any]:
    """Receipts whose decision code is a transport-level error also get the error envelope.

    The executor may report a missing/expired plan or a missing confirm flag as a
    receipt (``status`` ``expired``/``vetoed`` with that code) instead of raising;
    MCP clients get both the envelope and the receipt so the trace is not lost.
    """
    if _is_error(receipt):
        return receipt
    data = dump(receipt)
    decision = data.get("decision") if isinstance(data, dict) else None
    code = decision.get("code") if isinstance(decision, dict) else None
    if isinstance(code, str) and code in ERROR_CODES and not decision.get("approved", False):
        return error(code, str(decision.get("reason", code)), receipt=data)
    return data


def _newest_first(items: list[Any]) -> list[Any]:
    out = list(items)
    try:
        out.sort(key=lambda d: getattr(d, "ts", None) or datetime.min, reverse=True)
    except TypeError:
        out.reverse()
    return out


def _state_attr(engine: Any, name: str, default: Any) -> Any:
    state = getattr(engine, "state", None)
    return getattr(state, name, default) if state is not None else default


def _symbol_ok(engine: Any, symbol: str) -> Optional[dict[str, Any]]:
    settings = getattr(engine, "settings", None)
    allowed = list(getattr(settings, "symbol_list", []) or []) if settings is not None else []
    if allowed and symbol.upper() not in allowed:
        return error("INVALID_ARGUMENT", f"symbol {symbol!r} is not configured; allowed: {allowed}")
    return None


def _market_of(engine: Any) -> Any:
    """The engine's latest MarketState, or None (never raises)."""
    res = _call(engine.market)
    if _is_error(res) or not isinstance(res, tuple) or not res:
        return None
    return res[0]


def _explained(engine: Any, capital_usd: Any, leverage: Any, horizon_h: Any, assumed_rate: Optional[float]) -> Any:
    """``engine.explain_edge`` normalised to (edge, sizing, components, horizon analysis).

    ``Engine.explain_edge`` returns the horizon view itself; a duck-typed engine that
    still returns the older 3-tuple gets the analysis derived here from the same
    breakdown, so the breakeven verdict is never silently missing.
    """
    try:
        res = engine.explain_edge(capital_usd, leverage, horizon_h, assumed_rate)
    except TypeError:
        res = _call(lambda: engine.explain_edge(capital_usd, leverage, horizon_h))
    except Exception as exc:  # noqa: BLE001 — errors become values on the wire
        return _errored(exc)
    if _is_error(res):
        return res
    edge, sizing, components = res[0], res[1], res[2]
    analysis = res[3] if len(res) > 3 else None
    if analysis is None:
        market = _market_of(engine)
        funding = getattr(market, "funding", None)
        analysis = horizon_analysis(
            edge,
            interval_h=float(getattr(funding, "interval_h", 0) or 0) or None,
            symbol=getattr(market, "symbol", None),
            measured_source=getattr(getattr(funding, "source", None), "value", None),
            assumed_rate=assumed_rate,
        )
    return edge, sizing, components, analysis


# --------------------------------------------------------------------------- server factory

MUTATING_TOOLS = (
    "deltr_execute_hedge", "deltr_unwind", "deltr_kill_switch", "deltr_reset_halt",
    "deltr_set_min_edge", "deltr_stress", "deltr_onchain_swap", "deltr_x402_pay",
)


def apply_public_readonly(mcp: FastMCP, engine: Any) -> list[str]:
    """In a public read-only deployment, mutating tools answer with a READ_ONLY error.

    Tools stay listed (so the surface is visible to a judge) but cannot move state.
    Returns the names that were disabled.
    """
    settings = getattr(engine, "settings", None)
    if not getattr(settings, "public_readonly", False):
        return []
    disabled: list[str] = []
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {}) or {}
    for name in MUTATING_TOOLS:
        tool = tools.get(name)
        if tool is None:
            continue
        original = tool.fn

        def _make(orig: Any, tool_name: str) -> Any:
            envelope = {"error": {"code": "READ_ONLY", "message": f"{tool_name} is disabled on this public read-only instance. Run Deltr locally to execute."}}

            # FastMCP decided at registration whether to await this tool, so the
            # replacement must match the original's async-ness exactly.
            if inspect.iscoroutinefunction(orig):
                async def _blocked(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    return dict(envelope)
            else:
                def _blocked(*args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[misc]
                    return dict(envelope)
            _blocked.__name__ = getattr(orig, "__name__", tool_name)
            _blocked.__doc__ = getattr(orig, "__doc__", None)
            return _blocked

        tool.fn = _make(original, name)
        # the READ_ONLY envelope is not the tool's declared structured output: drop the
        # output schema so FastMCP does not reject the refusal itself
        for attr in ("output_schema",):
            if hasattr(tool, attr):
                try:
                    setattr(tool, attr, None)
                except Exception:  # noqa: BLE001 - frozen model; fall through to fn_metadata
                    pass
        meta = getattr(tool, "fn_metadata", None)
        for attr in ("output_schema", "output_model", "wrap_output"):
            if meta is not None and hasattr(meta, attr):
                try:
                    setattr(meta, attr, None if attr != "wrap_output" else False)
                except Exception:  # noqa: BLE001
                    pass
        disabled.append(name)
    return disabled

LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def transport_security_for(settings: Any) -> TransportSecuritySettings:
    """Host / Origin allow-list for the streamable-HTTP transport.

    Loopback is always accepted (the local ``python main.py`` case). ``DELTR_MCP_ALLOWED_HOSTS``
    adds the public names a deployment answers on (``ip:port`` or ``name:*``); the CORS origins
    are accepted as Origins too. A single ``*`` disables the DNS-rebinding check, for a reverse
    proxy that already pins the Host header.
    """
    raw = str(getattr(settings, "mcp_allowed_hosts", "") or "")
    extra = [h.strip() for h in raw.split(",") if h.strip()]
    if "*" in extra:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    cors = str(getattr(settings, "cors_origins", "") or "")
    origins = [o.strip() for o in cors.split(",") if o.strip()]
    origins += [f"http://{h}" for h in extra] + [f"https://{h}" for h in extra]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=LOOPBACK_HOSTS + extra,
        allowed_origins=LOOPBACK_ORIGINS + origins,
    )


def build_mcp(engine: Any, activity: ActivityLog) -> FastMCP:
    """Build the ``deltr`` FastMCP server around an Engine (duck-typed, section 4.14).

    Configured for streamable HTTP mounted by FastAPI at ``/mcp``
    (``stateless_http=True, json_response=True, streamable_http_path="/"``) and
    reusable as-is for stdio via :func:`run_stdio`.
    """
    mcp = FastMCP(
        name="deltr",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=transport_security_for(getattr(engine, "settings", None)),
    )
    record = instrumented(activity, "deltr")

    # ------------------------------------------------------------------ read-only
    @mcp.tool(name="deltr_status")
    @record
    def deltr_status(ctx: Context) -> dict[str, Any]:
        """Current system status.  Read-only.

        Includes, alongside halt/kill state, drawdown, equity, open positions, venue health,
        risk limits and the gate's check order:

        * `mode` — PAPER (simulated fills), TESTNET (real testnet orders) or LIVE (REAL MONEY);
        * `real_funds_armed` — true ONLY in LIVE and only after the startup preflight passed.
          When it is true, every execution tool spends real money;
        * `execution_style` / `execution_style_label` — maker (post-only, never crosses the
          spread) or taker.  Maker is the default because the measured round trip is about
          8.6 bps posted against about 16.6 bps taken;
        * `data_source` — market data provenance.  Real mainnet in every mode, keyless;
        * `onchain_armed` and `wallet_address` — the Binance Agentic Wallet leg.  The address is
          public; Deltr never holds, reads or stores a key and no secret appears on this object;
        * `max_notional_usd` / `max_aggregate_usd` — the caps in force right now.

        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        return dump(_call(engine.status))

    @mcp.tool(name="deltr_market")
    @record
    def deltr_market(
        ctx: Context,
        symbol: Annotated[str, Field(description="Trading pair, e.g. BNBUSDT")] = "BNBUSDT",
    ) -> dict[str, Any]:
        """Latest market state for the symbol: PancakeSwap V3 quote (BSC mainnet), Binance Futures MAINNET
        mark/index/funding (keyless, read-only, in every mode), spot-mirror reference, freshness, plus the
        current horizon edge breakdown and its components (bps).  Read-only.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        bad = _symbol_ok(engine, symbol)
        if bad:
            return bad
        res = _call(engine.market)
        if _is_error(res):
            return res
        market, edge = res
        return {
            "market": dump(market),
            "edge": dump(edge),
            "components": dump(edge.components()) if edge is not None else [],
        }

    @mcp.tool(name="deltr_scan")
    @record
    def deltr_scan(
        ctx: Context,
        notional_usd: Annotated[Optional[float], Field(ge=5, description="Notional to size the quote for (USD)")] = None,
        horizon_h: Annotated[Optional[float], Field(ge=1, le=720, description="Funding horizon in hours")] = None,
        min_edge_bps: Annotated[Optional[float], Field(description="Override the min-edge threshold for this scan")] = None,
        history_points: Annotated[int, Field(le=600, ge=0, description="How many recent spread points to return")] = 60,
    ) -> dict[str, Any]:
        """Scan for a delta-neutral opportunity (long DEX / short perp): horizon-based net edge after every cost,
        actionable flag with the reason, and the recent spread history.  Read-only; nothing is proposed or executed.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        opp = _call(lambda: engine.scan(notional_usd, horizon_h, min_edge_bps))
        if _is_error(opp):
            return opp
        history = list(_state_attr(engine, "history", []) or [])
        if not history:
            snap = _call(engine.snapshot)
            history = [] if _is_error(snap) else list(getattr(snap, "history", []) or [])
        tail = history[-history_points:] if history_points > 0 else []
        return {"opportunity": dump(opp), "history": dump(tail)}

    @mcp.tool(name="deltr_explain_edge")
    @record
    def deltr_explain_edge(
        ctx: Context,
        capital_usd: Annotated[Optional[float], Field(ge=10, description="Capital to size for (USD); default from config")] = None,
        leverage: Annotated[float, Field(gt=0, le=3, description="Perp leverage (max 3x)")] = 2.0,
        horizon_h: Annotated[float, Field(gt=0, le=720, description="Funding horizon in hours (what-if)")] = 24.0,
        assumed_funding_rate: Annotated[
            Optional[float],
            Field(ge=-0.01, le=0.01, description="What-if funding rate per settlement; always returned labelled as an assumption, never as a measurement"),
        ] = None,
    ) -> dict[str, Any]:
        """Explain the edge math for a hypothetical hedge: entry basis, every cost leg (DEX fee, impact, perp slippage,
        taker fees, gas), funding over the horizon, net edge, expected USD and allocated risk, with the sizing
        (N = capital/(1+1/L), step-rounded), the breakeven holding period (settlements, hours and days against the
        configured horizon, with a one-line verdict), the net-edge-versus-horizon curve and its zero crossing, and the
        formulas used.  Testnet funding is near zero, so any mainnet-typical rate is returned as a labelled assumption
        beside the measured rate.  Dry: no plan, no side effects.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = _explained(engine, capital_usd, leverage, horizon_h, assumed_funding_rate)
        if _is_error(res):
            return res
        edge, sizing, components, analysis = res
        return {
            "edge": dump(edge),
            "components": dump(components),
            "sizing": dump(sizing),
            "horizon": dump(analysis),
            "breakeven_verdict": analysis.verdict,
            "formulas": {
                "net_edge_bps": "basis_entry_bps + funding_bps_horizon - roundtrip_cost_bps - basis_exit_assumed_bps",
                "roundtrip_cost_bps": "2 * (dex_fee_bps + dex_impact_bps + perp_slip_bps + cex_taker_bps + gas_bps_leg)",
                "allocated_risk_usd": "notional_usd * (roundtrip_cost_bps + basis_shock_bps) / 1e4",
                "funding_bps_horizon": "1e4 * funding_rate_last * settlements (short receives when rate > 0)",
                "sizing": "notional = capital_usd / (1 + 1/leverage); qty = round_step(notional / dex_exec_price)",
                "cost_to_recover_bps": "roundtrip_cost_bps + basis_exit_assumed_bps - basis_entry_bps",
                "breakeven_settlements": "smallest n with n * rate * 1e4 >= cost_to_recover_bps (none when rate <= 0)",
            },
        }

    # ------------------------------------------------------------------ two-phase trading
    @mcp.tool(name="deltr_propose_hedge")
    @record
    async def deltr_propose_hedge(
        ctx: Context,
        capital_usd: Annotated[float, Field(ge=10, description="Capital to deploy across both legs (USD)")],
        leverage: Annotated[float, Field(gt=0, le=3, description="Perp leverage (max 3x)")] = 2.0,
        symbol: Annotated[str, Field(description="Trading pair, e.g. BNBUSDT")] = "BNBUSDT",
    ) -> dict[str, Any]:
        """Phase 1 of 2: build a delta-neutral hedge plan (DEX BUY + perp SELL, equal step-aligned qty) for the capital
        and leverage, run the risk gate as a pre-check and store the plan under plan_id (single-use, expires in 60 s).
        NOTHING EXECUTES here — call deltr_execute_hedge(plan_id, confirm=true) to trade.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        bad = _symbol_ok(engine, symbol)
        if bad:
            return bad
        res = await _call_async(
            lambda: engine.propose(capital_usd, leverage, symbol, TraceSource.MCP, client=client_name(ctx))
        )
        if _is_error(res):
            return res
        plan, proposal, precheck = res
        approved = bool(getattr(precheck, "approved", False))
        message = (
            f"Plan {plan.id} stored (expires {plan.expires_at.isoformat()}). Pre-check "
            f"{'APPROVED' if approved else 'VETO ' + str(getattr(precheck, 'code', ''))}: {getattr(precheck, 'reason', '')} "
            + ("Nothing executed — call deltr_execute_hedge(plan_id, confirm=true) to trade." if approved
               else "Nothing executed; a VETO is deterministic for these inputs.")
        )
        return {
            "plan_id": plan.id,
            "plan": dump(plan),
            "proposal": dump(proposal),
            "precheck": dump(precheck),
            "expires_at": plan.expires_at.isoformat(),
            "message": message,
        }

    @mcp.tool(name="deltr_evaluate_risk")
    @record
    def deltr_evaluate_risk(
        ctx: Context,
        capital_usd: Annotated[float, Field(description="Capital to size for (USD)")],
        leverage: Annotated[float, Field(description="Perp leverage to probe (values above 3x are vetoed, not clamped)")],
        symbol: Annotated[str, Field(description="Trading pair, e.g. BNBUSDT")] = "BNBUSDT",
    ) -> dict[str, Any]:
        """Dry-run the deterministic 19-check risk gate for a hypothetical hedge and return the full check matrix
        (name, observed, limit, pass/fail) with the decision code and latency.  No plan is stored, nothing executes.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        bad = _symbol_ok(engine, symbol)
        if bad:
            return bad
        res = _call(lambda: engine.evaluate_risk(capital_usd, leverage, symbol))
        if _is_error(res):
            return res
        data = dump(res)
        if isinstance(data, dict):
            data["dry_run"] = True
        return data

    @mcp.tool(name="deltr_execute_hedge")
    @record
    async def deltr_execute_hedge(
        ctx: Context,
        plan_id: Annotated[str, Field(description="plan_id returned by deltr_propose_hedge or deltr_prompt")],
        confirm: Annotated[bool, Field(description="Must be true in TESTNET (real testnet perp order, simulated DEX leg) and in LIVE (REAL MONEY: a real mainnet perp order plus a real on-chain swap signed by the Binance Agentic Wallet)")] = False,
    ) -> dict[str, Any]:
        """Phase 2 of 2: execute a stored plan.  The plan is consumed (single-use), re-priced against live quotes,
        re-gated (PRICE_DRIFT veto at 20 bps, NEGATIVE_EDGE at the re-priced edge) and then both legs are placed
        with reverse-on-failure — in PAPER both fills are simulated on live prices; in TESTNET the perp leg is a
        real Binance Futures testnet LIMIT IOC order and the PancakeSwap leg is simulated at the live QuoterV2
        price (badged simulated/paper in the receipt); returns the
        ExecutionReceipt with the ordered trace steps and a sha256 digest.  Errors: PLAN_NOT_FOUND, PLAN_EXPIRED,
        CONFIRM_REQUIRED (TESTNET or LIVE without confirm), EXECUTION_IN_FLIGHT.
        In LIVE this places a REAL mainnet perp order (posted as a maker by default, never crossing
        the spread) and a REAL on-chain swap that the Binance Agentic Wallet signs.  An unfilled
        maker leg is a failure and the other leg is reversed; it is never silently skipped.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = await _call_async(
            lambda: engine.execute(plan_id, confirm, TraceSource.MCP, client=client_name(ctx))
        )
        return _receipt_or_error(res)

    @mcp.tool(name="deltr_unwind")
    @record
    async def deltr_unwind(
        ctx: Context,
        position_id: Annotated[str, Field(description='position id or "all"')],
        reason: Annotated[str, Field(description="Why (recorded on the receipt)")] = "user",
        confirm: Annotated[bool, Field(description="Must be true in TESTNET (real reduce-only testnet perp order; DEX leg simulated) and in LIVE (REAL MONEY: closes a real position with real orders)")] = False,
    ) -> dict[str, Any]:
        """Close an open position (or "all"): reduce-only perp BUY first (a real testnet order in TESTNET), then the
        DEX sell (simulated at the live quote in both modes).  Allowed while HALTED or
        with the kill switch on because it only reduces risk (the gate verifies the position binding).
        Returns the receipts under "receipts".
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = await _call_async(
            lambda: engine.unwind(position_id, reason, confirm, TraceSource.MCP, client=client_name(ctx))
        )
        if _is_error(res):
            return res
        receipts = list(res) if isinstance(res, (list, tuple)) else [res]
        return {"position_id": position_id, "count": len(receipts), "receipts": [_receipt_or_error(r) for r in receipts]}

    # ------------------------------------------------------------------ books and logs
    @mcp.tool(name="deltr_positions")
    @record
    def deltr_positions(ctx: Context) -> dict[str, Any]:
        """Open positions (both legs, delta in base units, mark-to-close PnL split into spread / funding / fees, stop
        distance) and the portfolio snapshot (equity, cash, peak, drawdown, dd_state).  Read-only.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        snap = _call(engine.snapshot)
        if _is_error(snap):
            return snap
        return {"positions": dump(list(snap.positions)), "portfolio": dump(snap.portfolio)}

    @mcp.tool(name="deltr_risk_log")
    @record
    def deltr_risk_log(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100, description="Max decisions to return")] = 20,
        only_vetoes: Annotated[bool, Field(description="Return only vetoed decisions")] = False,
    ) -> dict[str, Any]:
        """Recent risk-gate decisions, newest first, each with the check matrix (observed vs limit) and latency in µs.
        Returned under "decisions".  Read-only.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        decisions = list(_state_attr(engine, "decisions", []) or [])
        if not decisions:
            snap = _call(engine.snapshot)
            decisions = [] if _is_error(snap) else list(getattr(snap, "decisions", []) or [])
        rows = _newest_first(decisions)
        if only_vetoes:
            rows = [d for d in rows if not getattr(d, "approved", False)]
        return {"count": len(rows[:limit]), "decisions": dump(rows[:limit])}

    @mcp.tool(name="deltr_receipt")
    @record
    def deltr_receipt(
        ctx: Context,
        receipt_id: Annotated[str, Field(description="ExecutionReceipt id (rcpt_...)")],
    ) -> dict[str, Any]:
        """Fetch one execution receipt by id: decision, plan, fills with provenance, trace steps, legging window and
        sha256 digest.  Read-only.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = _call(lambda: engine.get_receipt(receipt_id))
        if _is_error(res):
            return res
        if res is None:
            return error("RECEIPT_NOT_FOUND", f"no receipt {receipt_id!r}")
        return dump(res)

    @mcp.tool(name="deltr_activity")
    @record
    def deltr_activity(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50, description="Max rows to return")] = 30,
    ) -> dict[str, Any]:
        """Recent MCP activity (which client called which tool, redacted args, ok, latency, trace id), newest first,
        under "activity".  Read-only.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        rows = activity.recent(limit)
        if not rows:
            fetch = getattr(engine, "activity_log", None)
            if callable(fetch):
                rows = _call(lambda: fetch(limit))
                if _is_error(rows):
                    return rows
        return {"count": len(rows), "activity": dump(list(rows))}

    # ------------------------------------------------------------------ natural language
    @mcp.tool(name="deltr_prompt")
    @record
    async def deltr_prompt(
        ctx: Context,
        text: Annotated[str, Field(min_length=1, description='e.g. "Rebalance $5,000 USDC into delta-neutral BNB arbitrage"')],
    ) -> dict[str, Any]:
        """Turn a natural-language instruction into a typed intent and, for hedge/rebalance intents, a stored plan with
        its risk pre-check (plan_id).  PROPOSE-ONLY: this tool never executes; call deltr_execute_hedge(plan_id).
        USDC amounts are treated as USDT-equivalent for sizing (no conversion leg in v1).
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = await _call_async(lambda: engine.prompt(text, TraceSource.MCP, client=client_name(ctx)))
        return dump(res)

    # ------------------------------------------------------------------ operator controls
    @mcp.tool(name="deltr_kill_switch")
    @record
    def deltr_kill_switch(
        ctx: Context,
        on: Annotated[bool, Field(description="true engages the kill switch, false releases it")],
        reason: Annotated[str, Field(description="Recorded in the gate journal")] = "operator",
    ) -> dict[str, Any]:
        """Engage or release the kill switch.  While engaged the gate vetoes everything except verified reduce-only
        unwinds.  Returns the SystemStatus.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        return dump(_call(lambda: engine.kill_switch(on, reason)))

    @mcp.tool(name="deltr_reset_halt")
    @record
    def deltr_reset_halt(
        ctx: Context,
        reason: Annotated[str, Field(min_length=3, description="Operator justification (logged)")],
    ) -> dict[str, Any]:
        """Clear a sticky drawdown HALT.  Only possible when the book is flat and drawdown is back below 3 %; never
        re-bases the peak.  Returns SystemStatus or HALT_NOT_CLEARABLE.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        return dump(_call(lambda: engine.reset_halt(reason)))

    @mcp.tool(name="deltr_set_min_edge")
    @record
    def deltr_set_min_edge(
        ctx: Context,
        min_edge_bps: Annotated[float, Field(ge=-50, le=50, description="Runtime min-edge threshold (bps)")],
    ) -> dict[str, Any]:
        """Set the runtime minimum net edge (bps) the scout requires before flagging an opportunity actionable.
        PAPER allows [-50, 50] for demos; TESTNET floors it at the measured round-trip cost.  Returns the effective value.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = _call(lambda: engine.set_min_edge(min_edge_bps))
        if _is_error(res):
            return res
        effective, floor = res
        mode = getattr(getattr(engine, "settings", None), "mode", None)
        return {"min_edge_bps": float(effective), "floor_bps": float(floor), "mode": dump(mode) if mode is not None else "paper"}

    @mcp.tool(name="deltr_stress")
    @record
    async def deltr_stress(
        ctx: Context,
        kind: Annotated[
            Literal["basis_shock", "equity_shock", "dex_leg_fail", "funding_flip", "feed_stale", "reset"],
            Field(description="Scenario; 'reset' clears the SIMULATED state"),
        ],
        magnitude: Annotated[float, Field(description="bps for basis_shock, pct for equity_shock, rate for funding_flip")] = 0.0,
    ) -> dict[str, Any]:
        """Apply a labelled stress scenario to the PAPER book only (market feed untouched): basis_shock (bps),
        equity_shock (pct), dex_leg_fail, funding_flip (rate), feed_stale, or reset.  The dashboard shows a SIMULATED
        badge until reset.  Refused (STRESS_REFUSED) in TESTNET while real orders are open.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        try:
            scenario = StressScenario(kind=StressKind(kind), magnitude=magnitude)
        except (ValueError, ValidationError) as exc:
            return error("INVALID_ARGUMENT", str(exc))
        return dump(await _call_async(lambda: engine.stress(scenario)))

    # ------------------------------------------------------------------ data and analysis
    @mcp.tool(name="deltr_edge_report")
    @record
    def deltr_edge_report(
        ctx: Context,
        capital_usd: Annotated[Optional[float], Field(ge=10, description="Capital to size for (USD); default from config")] = None,
        leverage: Annotated[float, Field(gt=0, le=3, description="Perp leverage (max 3x)")] = 2.0,
        horizon_h: Annotated[float, Field(gt=0, le=720, description="Funding horizon in hours")] = 24.0,
        assumed_funding_rate: Annotated[
            Optional[float],
            Field(ge=-0.01, le=0.01, description="What-if funding rate per settlement; rendered as a labelled assumption, never as a measurement"),
        ] = None,
    ) -> dict[str, Any]:
        """Render the current edge decomposition as a titled, readable markdown report: the round-trip cost waterfall,
        the sizing, funding over the horizon, the breakeven holding period and verdict, the net-edge-versus-horizon
        table with its zero crossing, every data-source tag with its feed age, and the honesty labels (testnet funding
        is near zero and indicative, the PancakeSwap leg is quoted live and always simulated, any mainnet-typical rate
        is an assumption).  Pure presentation of numbers deltr_explain_edge already returns: no new analysis, no plan,
        no side effects.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        res = _explained(engine, capital_usd, leverage, horizon_h, assumed_funding_rate)
        if _is_error(res):
            return res
        edge, sizing, components, analysis = res
        market = _market_of(engine)
        settings = getattr(engine, "settings", None)
        mode = getattr(getattr(settings, "mode", None), "value", None) or "paper"
        symbol = getattr(market, "symbol", None) or getattr(settings, "symbol", None)
        markdown = render_edge_report(
            edge=edge, analysis=analysis, components=components, market=market, sizing=sizing,
            symbol=symbol, mode=mode,
        )
        return {
            "title": f"Deltr edge report: {symbol or 'the configured pair'}",
            "format": "markdown",
            "markdown": markdown,
            "breakeven_verdict": analysis.verdict,
            "assumption_label": analysis.assumption_label,
            "assumed_rate": analysis.assumed_rate,
            "measured_rate": analysis.measured_rate,
            "edge": dump(edge),
            "components": dump(components),
            "horizon": dump(analysis),
        }

    @mcp.tool(name="deltr_funding_history")
    @record
    async def deltr_funding_history(
        ctx: Context,
        symbol: Annotated[str, Field(description="USDS-M perpetual symbol, e.g. BNBUSDT or BTCUSDT")] = "BNBUSDT",
        lookback_days: Annotated[float, Field(ge=1, le=2000, description="How far back to fetch settlements")] = DEFAULT_LOOKBACK_DAYS,
        holds_days: Annotated[
            Optional[list[float]],
            Field(description="Holding periods in days to test, e.g. [3, 7, 14, 30]"),
        ] = None,
        refresh: Annotated[bool, Field(description="Bypass the cache and re-fetch (funding only settles every 4-8 h)")] = False,
    ) -> dict[str, Any]:
        """REAL MAINNET funding history for a perpetual, and what it says about the carry trade.

        Fetches settled funding from the public keyless mainnet endpoint (read-only market data; no key,
        no order path, in any mode) and reports: annualised carry (mean, median, range), the share of
        settlements where a SHORT is paid, and the share of rolling holding windows whose carry alone
        clears the round trip under two cost models — the perp leg TAKEN (crossing the spread, where the
        Binance taker fee dominates) and the perp leg POSTED (maker). The settlement interval is measured
        from the data, not assumed, so a 4 h symbol is not annualised as if it were 8 h.

        The POSTED column is a model, not a realised result: it recomputes the same windows against a
        maker round trip and assumes the limit order fills, which a real maker order may not. Historical
        carry is not a forecast. Read-only; nothing is proposed or executed.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        analysis = await _call_async(
            lambda: engine.funding_history(symbol, lookback_days=lookback_days, holds_days=holds_days, refresh=refresh)
        )
        if _is_error(analysis):
            return analysis
        payload = analysis.as_dict()
        payload["format"] = "markdown"
        payload["markdown"] = render_funding_report(analysis)
        payload["title"] = f"Funding carry: {payload.get('symbol', symbol)}"
        return payload

    # ------------------------------------------------------------------ on-chain leg + x402
    @mcp.tool(name="deltr_wallet_status")
    @record
    async def deltr_wallet_status(ctx: Context) -> dict[str, Any]:
        """Read-only status of the on-chain leg, which runs through the Binance Agentic Wallet CLI (`baw`):
        whether the CLI is installed, whether a wallet session exists, the wallet addresses and Binance's own
        remaining daily quota, plus Deltr's arming state and its notional caps.  Deltr never holds, reads,
        stores or signs with a private key: the wallet keeps custody, applies its own limits and does the
        signing.  When the CLI is missing or signed out the answer says exactly which command to run and no
        simulated fallback is used.  Nothing here moves value.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        return dump(await _call_async(lambda: engine.wallet_status()))

    @mcp.tool(name="deltr_onchain_swap")
    @record
    async def deltr_onchain_swap(
        ctx: Context,
        from_token: Annotated[str, Field(description="Source token contract address (0x…)")],
        to_token: Annotated[str, Field(description="Destination token contract address (0x…)")],
        amount: Annotated[float, Field(gt=0, description="Amount of the source token to swap")],
        notional_usd: Annotated[float, Field(gt=0, description="USD notional of the swap, checked against Deltr's on-chain caps")],
        confirm: Annotated[bool, Field(description="Must be true: this moves real funds")] = False,
        chain_id: Annotated[Optional[int], Field(description="Binance chain id; defaults to the configured chain (56 = BSC)")] = None,
        slippage_pct: Annotated[Optional[float], Field(gt=0, le=50, description="Slippage tolerance in percent; capped by DELTR_ONCHAIN_MAX_SLIPPAGE_PCT")] = None,
        min_receive: Annotated[Optional[float], Field(gt=0, description="Refuse before signing if the preview would receive less than this")] = None,
    ) -> dict[str, Any]:
        """Request an on-chain swap through the Binance Agentic Wallet.  Deltr asks; Binance's wallet holds the key,
        decides whether to sign, applies its own daily limits on top of Deltr's caps, and broadcasts.  A deterministic
        pre-flight runs first (kill switch, drawdown halt, arming, chain, per-request and aggregate notional caps,
        confirm) and is recorded in the risk log.  The swap is previewed with `market-order quote` and refused before
        anything is signed if the preview breaches `min_receive` or the price-impact bound; the result carries the
        transaction hash and the amount the CLI said was actually received, and is reported unconfirmed rather than
        assumed filled when the CLI has not finished the order.  Requires `confirm: true`.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        return dump(await _call_async(lambda: engine.onchain_swap(
            from_token=from_token, to_token=to_token, amount=amount, notional_usd=notional_usd,
            confirm=confirm, chain_id=chain_id, slippage=slippage_pct, min_receive=min_receive,
        )))

    @mcp.tool(name="deltr_x402_pay")
    @record
    async def deltr_x402_pay(
        ctx: Context,
        payment_required: Annotated[str, Field(min_length=2, description="The 402 body as JSON, or the base64 PAYMENT-REQUIRED header value")],
        selected_index: Annotated[Optional[int], Field(ge=0, description="Which offered option to pay; default is the first that passes policy")] = None,
        confirm: Annotated[bool, Field(description="Must be true: this moves real funds")] = False,
    ) -> dict[str, Any]:
        """Pay an HTTP 402 (x402 / B402) challenge on BNB Smart Chain through the Binance Agentic Wallet: preview the
        offered options, apply Deltr's policy (network allow-list and per-payment ceiling), then have the wallet sign
        the chosen one.  Deltr holds no key and signs nothing; it receives only the replay header value to attach to
        the retried request.  The same pre-flight as an on-chain swap runs first and is recorded in the risk log.
        Requires `confirm: true`.
        A VETO is final for these inputs — do not retry with the same parameters; change capital/leverage or ask deltr_explain_edge."""
        return dump(await _call_async(lambda: engine.x402_pay(
            payment_required, selected_index=selected_index, confirm=confirm,
        )))

    # ------------------------------------------------------------------ resources
    @mcp.resource("deltr://status", name="status", mime_type="application/json",
                  description="SystemStatus JSON (mode, halt/kill, drawdown, venues, limits)")
    def status_resource() -> str:
        return json.dumps(dump(_call(engine.status)), default=str)

    @mcp.resource("deltr://risk-limits", name="risk-limits", mime_type="application/json",
                  description="Risk gate limits, CHECK_ORDER and the measured gate latency")
    def risk_limits_resource() -> str:
        st = _call(engine.status)
        if _is_error(st):
            return json.dumps(st)
        return json.dumps(
            {
                "limits": dump(st.limits),
                "check_order": list(st.check_order),
                "gate_median_us": st.gate_median_us,
                "min_edge_bps": st.min_edge_bps,
                "min_edge_floor_bps": st.min_edge_floor_bps,
                "mode": dump(st.mode),
            },
            default=str,
        )

    @mcp.resource("deltr://config", name="config", mime_type="application/json",
                  description="Redacted runtime configuration (never contains key values)")
    def config_resource() -> str:
        settings = getattr(engine, "settings", None)
        redacted = getattr(settings, "redacted", None)
        data = dump(redacted()) if callable(redacted) else {}
        if isinstance(data, dict) and "bsc_rpc_urls" in data:
            data["bsc_rpc_urls"] = redact_urls(data["bsc_rpc_urls"])  # provider keys often live in the URL path
        return json.dumps(data, default=str)

    apply_public_readonly(mcp, engine)
    return mcp


async def run_stdio(mcp: FastMCP) -> None:
    """Serve ``mcp`` over stdio (used by ``python main.py --mcp`` and Claude Desktop's stdio config).

    The transport owns stdout; callers must have routed logging to stderr already.
    """
    log.info("deltr MCP server: stdio transport up (%d tools)", len(TOOL_NAMES))
    await mcp.run_stdio_async()


__all__ = ["build_mcp", "run_stdio", "TOOL_NAMES", "ERROR_CODES", "VETO_SENTENCE", "INSTRUCTIONS", "dump", "error"]
