"""deltr/api/routes.py — the ``/api`` router (section 6.1).

Every route delegates to the :class:`deltr.engine.Engine` facade found on
``request.app.state.engine``; nothing here imports the gate, a venue client or a
router (``tests/test_choke_point.py``).  Errors leave as the envelope
``{"error": {"code", "message"}}`` (see ``deltr.api.app`` for the status mapping).

Every POST records ``source=ui`` when the ``X-Deltr-Source: ui`` header is present
(the dashboard sets it), else ``api``; ``client`` is the User-Agent truncated to 40 chars.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from deltr.funding_history import DEFAULT_LOOKBACK_DAYS, render_funding_report
from deltr.horizon import horizon_analysis
from deltr.mcp.activity import redact_urls
from deltr.models import StressScenario, TraceSource, UpstreamStatus

router = APIRouter()

RECEIPT_ERROR_STATUS = {"PLAN_NOT_FOUND": 404, "PLAN_EXPIRED": 410, "CONFIRM_REQUIRED": 409, "POSITION_NOT_FOUND": 404}

# On-chain / x402 refusals carry their own code; these are the HTTP statuses they map to.
ONCHAIN_ERROR_STATUS = {
    "CONFIRM_REQUIRED": 409, "ONCHAIN_NOT_ARMED": 409, "KILL_SWITCH": 409, "HALTED_DRAWDOWN": 409,
    "MAX_NOTIONAL": 409, "AGGREGATE_NOTIONAL": 409, "CHAIN_NOT_ALLOWED": 400, "INVALID_ARGUMENT": 400,
    "SWAP_PREVIEW_REJECTED": 409, "SWAP_UNCONFIRMED": 409, "SECRET_IN_ARGV": 500,
    "BAW_NOT_INSTALLED": 503, "BAW_NOT_SIGNED_IN": 401, "BAW_TIMEOUT": 504, "BAW_BAD_JSON": 502,
    "BAW_CLI_ERROR": 502, "BAD_PAYMENT_REQUIRED": 400, "X402_NO_PAY_TO": 409,
}


class ApiError(Exception):
    """Raised by routes; rendered as the JSON envelope by the app's exception handler."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------- bodies
class PromptBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ProposeBody(BaseModel):
    capital_usd: float = Field(gt=0)
    leverage: Optional[float] = Field(default=None, gt=0)
    symbol: Optional[str] = None


class ExecuteBody(BaseModel):
    plan_id: str = Field(min_length=1)
    confirm: bool = False


class UnwindBody(BaseModel):
    position_id: str = Field(min_length=1, description='position id or "all"')
    reason: str = "user"
    confirm: bool = False


class EvaluateBody(BaseModel):
    capital_usd: float
    leverage: float
    symbol: Optional[str] = None


class KillBody(BaseModel):
    on: bool
    reason: str = "operator"


class ResetHaltBody(BaseModel):
    reason: str = Field(min_length=3)


class OnchainSwapBody(BaseModel):
    from_token: str = Field(min_length=4, description="Source token contract address")
    to_token: str = Field(min_length=4, description="Destination token contract address")
    amount: float = Field(gt=0)
    notional_usd: float = Field(gt=0, description="USD notional, checked against Deltr's on-chain caps")
    confirm: bool = False
    chain_id: Optional[int] = None
    slippage_pct: Optional[float] = Field(default=None, gt=0, le=50)
    min_receive: Optional[float] = Field(default=None, gt=0)


class X402PayBody(BaseModel):
    payment_required: str = Field(min_length=2, description="402 body as JSON, or the base64 PAYMENT-REQUIRED header value")
    selected_index: Optional[int] = Field(default=None, ge=0)
    confirm: bool = False


class MinEdgeBody(BaseModel):
    min_edge_bps: float = Field(ge=-50, le=50)


# --------------------------------------------------------------------------- helpers
def _engine(request: Request) -> Any:
    return request.app.state.engine


def _source(request: Request) -> TraceSource:
    return TraceSource.UI if request.headers.get("x-deltr-source", "").lower() == "ui" else TraceSource.API


def _client(request: Request) -> str:
    return (request.headers.get("user-agent") or "api")[:40]


def _json(model: Any) -> Any:
    if model is None:
        return None
    if isinstance(model, (list, tuple)):
        return [_json(m) for m in model]
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


def _horizon(eng: Any, ms: Any, edge: Any, assumed_funding_rate: Optional[float] = None) -> Any:
    """Breakeven holding period + edge-versus-horizon curve for the edge just returned.

    Prefers the engine's own view (it knows the funding interval and the source tag);
    falls back to the pure function so a duck-typed engine still answers.
    """
    if edge is None:
        return None
    fn = getattr(eng, "edge_horizon", None)
    if callable(fn):
        return fn(edge, assumed_funding_rate)
    funding = getattr(ms, "funding", None)
    return horizon_analysis(
        edge,
        interval_h=float(getattr(funding, "interval_h", 0) or 0) or None,
        symbol=getattr(ms, "symbol", None),
        measured_source=getattr(getattr(funding, "source", None), "value", None),
        assumed_rate=assumed_funding_rate,
    )


def _onchain_error(exc: Exception) -> ApiError:
    """Any wallet / payment failure that carries its own ``code`` becomes the JSON envelope."""
    code = str(getattr(exc, "code", "") or "INTERNAL_ERROR")
    message = str(getattr(exc, "message", "") or exc) or code
    extra: dict[str, Any] = {}
    remedy = getattr(exc, "remedy", None)
    if remedy:
        extra["remedy"] = str(remedy)
    return ApiError(ONCHAIN_ERROR_STATUS.get(code, 400), code, message, **extra)


def _receipt_payload(receipt: Any) -> dict[str, Any]:
    """Receipts whose decision is a transport-level code become error envelopes (404/409/410)."""
    code = getattr(getattr(receipt, "decision", None), "code", None)
    if code in RECEIPT_ERROR_STATUS and not receipt.decision.approved:
        raise ApiError(RECEIPT_ERROR_STATUS[code], code, receipt.decision.reason, receipt=_json(receipt))
    return _json(receipt)


# --------------------------------------------------------------------------- GET
@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    eng = _engine(request)
    return {"ok": True, "mode": eng.settings.mode.value, "version": eng.settings.version, "uptime_s": eng.state.uptime_s()}


@router.get("/snapshot")
def snapshot(request: Request) -> Any:
    return _json(_engine(request).snapshot())


@router.get("/status")
def status(request: Request) -> Any:
    return _json(_engine(request).status())


@router.get("/market")
def market(
    request: Request,
    symbol: Optional[str] = None,
    horizon_h: Optional[float] = Query(default=None, gt=0, le=720),
    assumed_funding_rate: Optional[float] = Query(
        default=None, ge=-0.01, le=0.01,
        description="What-if funding rate per settlement; always returned labelled as an assumption, never as a measurement",
    ),
) -> Any:
    eng = _engine(request)
    if symbol and symbol.upper() not in eng.settings.symbol_list:
        raise ApiError(400, "INVALID_ARGUMENT", f"symbol {symbol!r} is not configured; allowed: {eng.settings.symbol_list}")
    ms, edge = eng.market()
    if horizon_h is not None and ms is not None:
        try:
            edge = eng.scan(horizon_h=horizon_h).edge
        except ValueError as exc:
            raise ApiError(400, "INVALID_ARGUMENT", str(exc)) from exc
    return {
        "market": _json(ms),
        "edge": _json(edge),
        "components": _json(edge.components()) if edge is not None else [],
        "horizon": _json(_horizon(eng, ms, edge, assumed_funding_rate)) if edge is not None else None,
    }


@router.get("/funding/history")
async def funding_history(
    request: Request,
    symbol: Optional[str] = None,
    lookback_days: float = Query(default=DEFAULT_LOOKBACK_DAYS, ge=1, le=2000),
    holds_days: Optional[str] = Query(
        default=None, description="Comma-separated holding periods in days, e.g. 3,7,14,30", max_length=120
    ),
    refresh: bool = False,
    markdown: bool = Query(default=False, description="Also render the analysis as a markdown report"),
) -> Any:
    """Real mainnet funding history and the carry analysis built on it (read-only, keyless, cached)."""
    eng = _engine(request)
    holds: Optional[list[float]] = None
    if holds_days:
        try:
            holds = [float(x) for x in holds_days.split(",") if x.strip()]
        except ValueError as exc:
            raise ApiError(400, "INVALID_ARGUMENT", f"holds_days must be a comma-separated list of numbers: {exc}") from exc
    try:
        analysis = await eng.funding_history(symbol, lookback_days=lookback_days, holds_days=holds, refresh=refresh)
    except ValueError as exc:
        raise ApiError(400, "INVALID_ARGUMENT", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - upstream market-data failure, reported with its hint
        raise ApiError(502, "UPSTREAM_UNAVAILABLE", str(exc)) from exc
    payload = analysis.as_dict()
    if markdown:
        payload["format"] = "markdown"
        payload["markdown"] = render_funding_report(analysis)
    return payload


@router.get("/opportunities")
def opportunities(request: Request, n: int = Query(default=60, ge=0, le=600)) -> Any:
    eng = _engine(request)
    return {"opportunity": _json(eng.state.opportunity), "history": _json(eng.history(n))}


@router.get("/positions")
def positions(request: Request) -> Any:
    snap = _engine(request).snapshot()
    return {"positions": _json(snap.positions), "portfolio": _json(snap.portfolio)}


@router.get("/risk/decisions")
def risk_decisions(request: Request, limit: int = Query(default=20, ge=1, le=100), only_vetoes: bool = False) -> Any:
    rows = list(_engine(request).state.decisions)[::-1]
    if only_vetoes:
        rows = [d for d in rows if not d.approved]
    return _json(rows[:limit])


@router.get("/receipts")
def receipts(request: Request) -> Any:
    return _json(_engine(request).receipts.recent(10)[::-1])


@router.get("/receipts/{receipt_id}")
def receipt(request: Request, receipt_id: str) -> Any:
    r = _engine(request).get_receipt(receipt_id)
    if r is None:
        raise ApiError(404, "RECEIPT_NOT_FOUND", f"no receipt {receipt_id!r}")
    return _json(r)


@router.get("/mcp/activity")
def mcp_activity(request: Request, limit: int = Query(default=30, ge=1, le=50)) -> Any:
    return _json(_engine(request).activity_log(limit))


@router.get("/config")
def config(request: Request) -> Any:
    s = _engine(request).settings
    out = dict(s.redacted())
    out["bsc_rpc_urls"] = redact_urls(out.get("bsc_rpc_urls"))  # provider keys often live in the URL path
    out["mcp_client_snippets"] = s.mcp_client_snippets()
    out["risk_limits"] = s.risk_limits().as_dict()
    return out


# --------------------------------------------------------------------------- POST
@router.post("/prompt")
async def prompt(request: Request, body: PromptBody) -> Any:
    eng = _engine(request)
    return _json(await eng.prompt(body.text, _source(request), client=_client(request)))


@router.post("/hedge/propose")
async def propose(request: Request, body: ProposeBody) -> Any:
    eng = _engine(request)
    plan, proposal, precheck = await eng.propose(body.capital_usd, body.leverage, body.symbol, _source(request), client=_client(request))
    return {
        "plan_id": plan.id,
        "plan": _json(plan),
        "proposal": _json(proposal),
        "precheck": _json(precheck),
        "expires_at": plan.expires_at.isoformat(),
    }


@router.post("/hedge/execute")
async def execute(request: Request, body: ExecuteBody) -> Any:
    eng = _engine(request)
    r = await eng.execute(body.plan_id, body.confirm, _source(request), client=_client(request))
    return _receipt_payload(r)


@router.post("/hedge/unwind")
async def unwind(request: Request, body: UnwindBody) -> Any:
    eng = _engine(request)
    rs = await eng.unwind(body.position_id, body.reason, body.confirm, _source(request), client=_client(request))
    return [_receipt_payload(r) for r in rs]


@router.post("/risk/evaluate")
def evaluate(request: Request, body: EvaluateBody) -> Any:
    return _json(_engine(request).evaluate_risk(body.capital_usd, body.leverage, body.symbol))


@router.post("/kill")
def kill(request: Request, body: KillBody) -> Any:
    return _json(_engine(request).kill_switch(body.on, body.reason))


@router.post("/risk/reset_halt")
def reset_halt(request: Request, body: ResetHaltBody) -> Any:
    return _json(_engine(request).reset_halt(body.reason))


@router.post("/stress")
async def stress(request: Request, body: StressScenario) -> Any:
    return _json(await _engine(request).stress(body))


@router.post("/upstream")
def upstream(request: Request, body: UpstreamStatus) -> Any:
    """The Agent OS bridge reports which Binance MCP upstream it reached (official | shim | none)."""
    eng = _engine(request)
    eng.state.upstream = body
    eng.state.emit("mcp", f"upstream {body.kind}: {len(body.tools_discovered)} tool(s)" + (f" ({body.error})" if body.error else ""),
                   data={"kind": body.kind, "tools": len(body.tools_discovered), "authorized": body.authorized})
    return _json(body)


@router.post("/scout/min_edge")
def min_edge(request: Request, body: MinEdgeBody) -> Any:
    eng = _engine(request)
    effective, floor = eng.set_min_edge(body.min_edge_bps)
    return {"min_edge_bps": float(effective), "floor_bps": float(floor), "mode": eng.settings.mode.value}


# --------------------------------------------------------------------------- on-chain leg + x402
@router.get("/wallet/status")
async def wallet_status(request: Request) -> Any:
    """Read-only status of the Binance Agentic Wallet leg.  Deltr holds no key and signs nothing:
    the wallet custodies the key, applies its own daily limits and performs the signing."""
    return await _engine(request).wallet_status()


@router.post("/onchain/swap")
async def onchain_swap(request: Request, body: OnchainSwapBody) -> Any:
    """Request an on-chain swap through the agentic wallet.  Behind the deterministic pre-flight
    (kill switch, drawdown halt, arming, chain, per-request and aggregate caps, confirm)."""
    try:
        return await _engine(request).onchain_swap(
            from_token=body.from_token, to_token=body.to_token, amount=body.amount,
            notional_usd=body.notional_usd, confirm=body.confirm, chain_id=body.chain_id,
            slippage=body.slippage_pct, min_receive=body.min_receive,
        )
    except Exception as exc:  # noqa: BLE001 - refusals and CLI failures become the JSON envelope
        if not hasattr(exc, "code"):
            raise
        raise _onchain_error(exc) from exc


@router.post("/x402/pay")
async def x402_pay(request: Request, body: X402PayBody) -> Any:
    """BUYER side: pay an HTTP 402 (x402 / B402) challenge on BNB Smart Chain through the wallet."""
    try:
        return await _engine(request).x402_pay(
            body.payment_required, selected_index=body.selected_index, confirm=body.confirm
        )
    except Exception as exc:  # noqa: BLE001
        if not hasattr(exc, "code"):
            raise
        raise _onchain_error(exc) from exc


@router.get("/x402/edge-report")
def x402_edge_report(
    request: Request,
    capital_usd: Optional[float] = Query(default=None, gt=0),
    leverage: float = Query(default=2.0, gt=0, le=3),
    horizon_h: float = Query(default=24.0, gt=0, le=720),
    preview: bool = Query(default=False, description="Return the challenge as a 200 body instead of a real 402"),
) -> Any:
    """SELLER side: answer with a well-formed x402 (B402) challenge on BNB Smart Chain whose paid
    artifact is Deltr's existing edge report, sealed with the same sha256 Deltr seals receipts with.

    The report itself is not included: only its title, size and digest, so a buyer knows what they
    are paying for.  No settlement is claimed and no mainnet B402 application was made.
    """
    eng = _engine(request)
    try:
        challenge = eng.x402_edge_report_challenge(capital_usd=capital_usd, leverage=leverage, horizon_h=horizon_h)
    except Exception as exc:  # noqa: BLE001
        if not hasattr(exc, "code"):
            raise
        raise _onchain_error(exc) from exc
    if preview:
        return challenge
    headers = {k: v for k, v in dict(challenge["headers"]).items() if k.lower() != "content-type"}
    return JSONResponse(status_code=402, content=challenge["body"], headers=headers)


__all__ = ["router", "ApiError", "RECEIPT_ERROR_STATUS", "ONCHAIN_ERROR_STATUS"]
