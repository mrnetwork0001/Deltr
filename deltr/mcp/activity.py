"""deltr/mcp/activity.py — inbound MCP activity capture.

Every Deltr MCP tool is wrapped by :func:`instrumented`, which times the call,
reads the calling client's ``clientInfo`` (name/version) from the FastMCP
``Context``, redacts the arguments and records a :class:`deltr.models.McpActivity`
row into the :class:`ActivityLog` (and, through it, into ``State.activity`` so the
dashboard's MCP-activity panel shows exactly what the LLM did).

Nothing here touches stdout: the stdio transport owns it.  All diagnostics go
through :mod:`logging` (stderr).
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Optional, TypeVar

from mcp.server.fastmcp import Context

from deltr.models import McpActivity

log = logging.getLogger("deltr.mcp.activity")

F = TypeVar("F", bound=Callable[..., Any])

_SENSITIVE = re.compile(r"(key|secret|token|password)", re.IGNORECASE)
_MAX_STR = 200
_DEFAULT_CLIENT = "mcp-client"
_ERROR_CODES_AS_FAILURE = frozenset(
    {
        "PLAN_NOT_FOUND",
        "PLAN_EXPIRED",
        "CONFIRM_REQUIRED",
        "EXECUTION_IN_FLIGHT",
        "STRESS_REFUSED",
        "HALT_NOT_CLEARABLE",
        "INVALID_ARGUMENT",
        "INTERNAL_ERROR",
        "NO_CREDENTIALS",
    }
)


# --------------------------------------------------------------------------- URL redaction
def redact_url(url: str) -> str:
    """``scheme://host[:port]`` only — many RPC providers embed the API key in the path or query."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "<redacted>"
    if not parts.scheme or not parts.netloc:
        return "<redacted>" if url else ""
    host = parts.hostname or ""
    if "@" in parts.netloc:  # user:pass@host
        host = parts.netloc.rsplit("@", 1)[-1].split(":")[0]
    port = f":{parts.port}" if parts.port else ""
    tail = "/…" if (parts.path not in ("", "/") or parts.query) else ""
    return f"{parts.scheme}://{host}{port}{tail}"


def redact_urls(urls: Any) -> list[str]:
    return [redact_url(u) for u in (urls or [])]


# --------------------------------------------------------------------------- client identity
def client_name(ctx: Optional[Context]) -> str:
    """Return ``"<clientInfo.name>/<clientInfo.version>"`` for the calling MCP client.

    Falls back to ``"mcp-client"`` when there is no request context (in-process
    calls), when the client sent no ``clientInfo``, or when the session is not
    reachable for any reason.  Never raises.
    """
    if ctx is None:
        return _DEFAULT_CLIENT
    try:
        params = ctx.session.client_params  # raises ValueError outside a request
    except Exception:  # noqa: BLE001 — identity is best-effort, never fatal
        return _DEFAULT_CLIENT
    info = getattr(params, "clientInfo", None) if params is not None else None
    if info is None:
        return _DEFAULT_CLIENT
    name = str(getattr(info, "name", "") or "").strip() or _DEFAULT_CLIENT
    version = str(getattr(info, "version", "") or "").strip()
    return f"{name}/{version}" if version else name


# --------------------------------------------------------------------------- redaction
def redact(args: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``args`` safe for logs and the dashboard.

    * keys matching ``key|secret|token|password`` (case-insensitive) are dropped;
    * nested dicts/lists are redacted recursively;
    * strings longer than 200 characters are truncated with an ellipsis marker;
    * non-JSON-native values are stringified so the row always serialises.
    """
    return {k: _redact_value(v) for k, v in dict(args).items() if not _SENSITIVE.search(str(k))}


def _redact_value(v: Any) -> Any:
    if isinstance(v, dict):
        return redact(v)
    if isinstance(v, (list, tuple)):
        return [_redact_value(x) for x in v]
    if isinstance(v, str):
        return v if len(v) <= _MAX_STR else v[:_MAX_STR] + f"…(+{len(v) - _MAX_STR} chars)"
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return str(v)[:_MAX_STR]


# --------------------------------------------------------------------------- log
class ActivityLog:
    """Bounded in-memory log of MCP activity rows, mirrored into ``State`` when given.

    ``state`` is duck-typed: if it exposes ``record_activity(a)`` every row is
    forwarded there (that is how ``State.activity`` and the dashboard get fed);
    if it exposes ``emit(topic, message, level, data)`` an ``"mcp"`` event is
    published too.  ``state`` may be ``None`` for tests and standalone use.
    """

    def __init__(self, state: Any = None, maxlen: int = 50) -> None:
        self.state = state
        self._rows: Deque[McpActivity] = deque(maxlen=maxlen)

    def record(self, a: McpActivity) -> None:
        """Append a row (newest last) and forward it to ``State`` if available."""
        self._rows.append(a)
        recorder = getattr(self.state, "record_activity", None)
        if callable(recorder):
            try:
                recorder(a)
            except Exception:  # noqa: BLE001 — never let bookkeeping break a tool call
                log.exception("State.record_activity failed")
        emit = getattr(self.state, "emit", None)
        if callable(emit):
            try:
                emit(
                    "mcp",
                    f"{a.direction} {a.server}:{a.tool} {'ok' if a.ok else 'FAIL'} {a.latency_ms} ms",
                    "info" if a.ok else "warn",
                    {"client": a.client, "tool": a.tool, "trace_id": a.trace_id},
                )
            except Exception:  # noqa: BLE001
                log.exception("State.emit failed")
        log.info(
            "mcp %s %s:%s client=%s ok=%s latency_ms=%d trace=%s",
            a.direction, a.server, a.tool, a.client, a.ok, a.latency_ms, a.trace_id,
        )

    def recent(self, n: int = 30) -> list[McpActivity]:
        """Newest-first slice of the last ``n`` rows."""
        rows = list(self._rows)
        rows.reverse()
        return rows[: max(0, int(n))]

    @property
    def size(self) -> int:
        return len(self._rows)


# --------------------------------------------------------------------------- result inspection
def _find_trace_id(result: Any) -> Optional[str]:
    """Best-effort trace id: a ``plan_id`` or a receipt/plan id inside the result."""
    if isinstance(result, list):
        for item in result:
            found = _find_trace_id(item)
            if found:
                return found
        return None
    if not isinstance(result, dict):
        return None
    for key in ("plan_id",):
        v = result.get(key)
        if isinstance(v, str) and v:
            return v
    v = result.get("id")
    if isinstance(v, str) and (v.startswith("rcpt_") or v.startswith("plan_")):
        return v
    for key in ("receipt", "plan", "proposal", "precheck"):
        nested = result.get(key)
        if isinstance(nested, dict):
            found = _find_trace_id(nested)
            if found:
                return found
    return None


def _is_error(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("error"), dict)


def _summarise(result: Any) -> str:
    """One-line, human-readable summary of a tool result for the activity panel."""
    if _is_error(result):
        err = result["error"]
        return f"error {err.get('code', '?')}: {str(err.get('message', ''))[:120]}"
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    if not isinstance(result, dict):
        return str(result)[:120]
    if "approved" in result and "code" in result:
        return f"{'APPROVED' if result['approved'] else 'VETO'} {result['code']}"
    if "precheck" in result and isinstance(result["precheck"], dict):
        pc = result["precheck"]
        return f"plan {result.get('plan_id', '?')} precheck {'APPROVED' if pc.get('approved') else 'VETO'} {pc.get('code', '')}".strip()
    if "status" in result and "decision" in result:
        code = result["decision"].get("code") if isinstance(result["decision"], dict) else ""
        return f"receipt {result.get('status')} {code}".strip()
    if "opportunity" in result and isinstance(result["opportunity"], dict):
        opp = result["opportunity"]
        return f"{'actionable' if opp.get('is_actionable') else 'not actionable'}: {opp.get('reason', '')}"[:120]
    if "message" in result and isinstance(result["message"], str):
        return result["message"][:120]
    if "halted" in result and "kill_switch" in result:
        return f"mode={result.get('mode')} halted={result.get('halted')} kill={result.get('kill_switch')} dd={result.get('dd_state')}"
    keys = ", ".join(list(result.keys())[:6])
    return f"ok ({keys})"


# --------------------------------------------------------------------------- decorator
def instrumented(activity: ActivityLog, server: str = "deltr") -> Callable[[F], F]:
    """Decorator factory: record every call of an MCP tool function as an inbound activity row.

    Works on both ``async def`` and plain ``def`` tools and preserves the wrapped
    function's signature (``functools.wraps`` + ``__wrapped__``) so FastMCP still
    derives the JSON schema and finds the ``Context`` parameter.  The row carries
    the client name/version, the tool name, redacted args, ``ok`` (False when the
    result is an ``{"error": ...}`` envelope or the tool raised), latency and a
    ``trace_id`` (plan id or receipt id found in the result).  Exceptions are
    recorded and re-raised; Deltr's tools return error envelopes instead of raising.
    """

    def decorate(fn: F) -> F:
        tool_name = fn.__name__

        def _split(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Optional[Context], dict[str, Any]]:
            ctx: Optional[Context] = None
            call_args: dict[str, Any] = {}
            try:
                bound = inspect.signature(fn).bind_partial(*args, **kwargs)
                items = bound.arguments.items()
            except Exception:  # noqa: BLE001 — fall back to kwargs only
                items = kwargs.items()
            for k, v in items:
                if isinstance(v, Context):
                    ctx = v
                else:
                    call_args[k] = v
            return ctx, call_args

        def _record(ctx: Optional[Context], call_args: dict[str, Any], result: Any, t0: float, exc: Optional[BaseException]) -> None:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            if exc is not None:
                row = McpActivity(
                    direction="inbound", client=client_name(ctx), server=server, tool=tool_name,  # type: ignore[arg-type]
                    args=redact(call_args), result_summary=f"exception {type(exc).__name__}: {str(exc)[:120]}",
                    ok=False, latency_ms=latency_ms, trace_id=None,
                )
            else:
                row = McpActivity(
                    direction="inbound", client=client_name(ctx), server=server, tool=tool_name,  # type: ignore[arg-type]
                    args=redact(call_args), result_summary=_summarise(result), ok=not _is_error(result),
                    latency_ms=latency_ms, trace_id=_find_trace_id(result),
                )
            activity.record(row)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx, call_args = _split(args, kwargs)
                t0 = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 — record, then re-raise
                    _record(ctx, call_args, None, t0, exc)
                    raise
                _record(ctx, call_args, result, t0, None)
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx, call_args = _split(args, kwargs)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                _record(ctx, call_args, None, t0, exc)
                raise
            _record(ctx, call_args, result, t0, None)
            return result

        return sync_wrapper  # type: ignore[return-value]

    return decorate


__all__ = ["ActivityLog", "client_name", "redact", "instrumented"]
