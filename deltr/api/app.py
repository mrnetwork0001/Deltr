"""deltr/api/app.py — FastAPI application factory (section 6.1 / 6.2).

``create_app(engine, mcp, activity)``:

* lifespan runs ``mcp.session_manager.run()`` (the streamable-HTTP MCP transport);
* ``/mcp``      -> the Deltr MCP server (``mcp.streamable_http_app()``), so the URL
                   is ``http://127.0.0.1:8000/mcp``;
* ``/api/*``    -> :mod:`deltr.api.routes` (table in section 6.1);
* ``/ws/stream``-> optional push of the same Snapshot the UI polls (section 6.2);
* ``/``         -> the committed static export ``ui/out`` (mounted LAST, only when present);
* CORS for the ``next dev`` origin on :3000.

Error envelope: ``{"error": {"code", "message"}}`` with 400 (invalid), 404
(plan / position / receipt), 409 (``EXECUTION_IN_FLIGHT``, ``CONFIRM_REQUIRED``,
``HALT_NOT_CLEARABLE``, ``STRESS_REFUSED``), 410 (``PLAN_EXPIRED``).

Nothing here imports the gate, a venue client or a router — all mutations go
through the Engine (``tests/test_choke_point.py``).
"""
from __future__ import annotations

import hmac
import json
import os

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from deltr.api import routes
from deltr.api.routes import ApiError
from deltr.config import REPO_ROOT
from deltr.models import AgentEvent, utcnow

log = logging.getLogger("deltr.api")

UI_OUT_DIR = REPO_ROOT / "ui" / "out"
# Extra browser origins may be admitted with DELTR_CORS_ORIGINS="https://deltrapp.vercel.app,https://example.com"
# (the front end can be hosted elsewhere, e.g. Vercel, while this process runs on a VPS).
CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"] + [
    o.strip() for o in os.environ.get("DELTR_CORS_ORIGINS", "").split(",") if o.strip()
]
WS_SNAPSHOT_INTERVAL_S = 1.0
WS_EVENT_TOPICS = {"gate", "fill", "position", "mcp", "stress", "plan"}
WS_LOG_LEVELS = {"warn", "error"}

# exception class name -> (HTTP status, code); by name so this module never imports the executor / stress modules
_EXC_BY_NAME: dict[str, tuple[int, str]] = {
    "PositionNotFound": (404, "POSITION_NOT_FOUND"),
    "HaltNotClearable": (409, "HALT_NOT_CLEARABLE"),
    "StressRefused": (409, "STRESS_REFUSED"),
    "ExecutionInFlight": (409, "EXECUTION_IN_FLIGHT"),
    "SizingError": (400, "INVALID_ARGUMENT"),
}


def _envelope(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _map_exception(exc: Exception) -> tuple[int, str]:
    name = type(exc).__name__
    if name in _EXC_BY_NAME:
        return _EXC_BY_NAME[name]
    if isinstance(exc, (ValueError, TypeError, ValidationError)):
        return 400, "INVALID_ARGUMENT"
    if isinstance(exc, (KeyError, LookupError)):
        return 404, "NOT_FOUND"
    return 500, "INTERNAL_ERROR"



class NoServerStreamMiddleware:
    """Answer ``GET /mcp`` with 405: this transport is stateless and never pushes server-initiated
    messages, so there is nothing to stream. Clients (the official SDK, Claude Code) treat 405 as
    "no notification stream" and carry on with plain POSTs. Behind a buffering reverse proxy
    (Vercel rewrites, some CDNs) an open GET stream stalls the next POST on the same origin, which
    is exactly what happened on the public showcase; without the stream every request completes."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("method") == "GET" and str(scope.get("path", "")).rstrip("/") == "/mcp":
            await send({"type": "http.response.start", "status": 405,
                        "headers": [(b"allow", b"POST, DELETE"), (b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"Method Not Allowed: this MCP transport is stateless and offers no server-initiated stream; use POST."})
            return
        await self.app(scope, receive, send)


class PublicReadOnlyMiddleware:
    """Refuse every mutating /api request unless the caller presents DELTR_API_TOKEN.

    Only active when settings.public_readonly is set. GET/HEAD/OPTIONS, the dashboard,
    the websocket stream and /mcp (guarded separately, tool by tool) pass through.
    """

    def __init__(self, app: Any, settings: Any) -> None:
        self.app = app
        self.enabled = bool(getattr(settings, "public_readonly", False))
        self.token = getattr(settings, "api_token", None) or ""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            self.enabled
            and scope.get("type") == "http"
            and scope.get("method", "GET").upper() in {"POST", "PUT", "PATCH", "DELETE"}
            and str(scope.get("path", "")).startswith("/api/")
        ):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            presented = headers.get("x-deltr-token", "")
            if not (self.token and presented and hmac.compare_digest(presented, self.token)):
                body = json.dumps({"error": {"code": "READ_ONLY", "message": "This is a public read-only Deltr instance; mutations need the X-Deltr-Token header."}}).encode()
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)

class ErrorEnvelopeMiddleware:
    """Pure-ASGI middleware: any exception escaping a route becomes the JSON envelope with the
    mapped status (a Starlette ``Exception`` handler would re-raise after responding)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def _send(message: Any) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:  # noqa: BLE001 - translated into the envelope
            if started:
                raise
            status, code = _map_exception(exc)
            if status == 500:
                log.exception("unhandled API error on %s", scope.get("path"))
            await _envelope(status, code, str(exc) or type(exc).__name__)(scope, receive, send)


class McpBarePathMiddleware:
    """Serve the advertised ``/mcp`` URL directly: rewrite ``/mcp`` to ``/mcp/`` so the MCP mount
    answers (without this the bare path falls through to the static UI mount -> 405, or a 307
    redirect that not every MCP client follows)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


def _ws_frame(kind: str, data: Any) -> dict[str, Any]:
    return {"type": kind, "ts": utcnow().isoformat(), "data": data}


def create_app(engine: Any, mcp: Any, activity: Any, *, ui_dir: Optional[Path] = None) -> FastAPI:
    """Build the FastAPI app around an Engine + the FastMCP server + the ActivityLog."""
    mcp_app = mcp.streamable_http_app()  # must be created before the lifespan touches session_manager

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            log.info("MCP streamable-HTTP transport mounted at /mcp")
            yield

    app = FastAPI(
        title="Deltr",
        version=str(getattr(engine.settings, "version", "1.0.0")),
        description="CEX<->DEX delta-neutral arbitrage agent on Binance Agent OS + MCP",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.engine = engine
    app.state.mcp = mcp
    app.state.activity = activity

    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=False, allow_methods=["*"], allow_headers=["*"]
    )

    # ---- error envelope ------------------------------------------------------------------
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.status, exc.code, exc.message, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _envelope(400, "INVALID_ARGUMENT", str(exc.errors()[0].get("msg", "invalid request")) if exc.errors() else "invalid request",
                         details=[{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg", "")} for e in exc.errors()])

    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(PublicReadOnlyMiddleware, settings=getattr(engine, "settings", None))
    app.add_middleware(NoServerStreamMiddleware)
    app.add_middleware(McpBarePathMiddleware)

    # ---- routes --------------------------------------------------------------------------
    app.include_router(routes.router, prefix="/api")

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket) -> None:
        await ws.accept()
        eng = ws.app.state.engine
        send_lock = asyncio.Lock()

        async def send(frame: dict[str, Any], *, skip_if_busy: bool = False) -> None:
            if skip_if_busy and send_lock.locked():
                return  # coalesce: the previous snapshot frame is still in flight
            async with send_lock:
                await ws.send_json(frame)

        def snapshot_frame() -> dict[str, Any]:
            return _ws_frame("snapshot", eng.snapshot().model_dump(mode="json"))

        await send(_ws_frame("hello", {"version": eng.settings.version, "mode": eng.settings.mode.value, "poll_fallback_ms": 1000}))
        await send(snapshot_frame())
        queue = eng.bus.subscribe()

        async def pusher() -> None:
            while True:
                await asyncio.sleep(WS_SNAPSHOT_INTERVAL_S)
                await send(snapshot_frame(), skip_if_busy=True)

        async def events() -> None:
            while True:
                ev: AgentEvent = await queue.get()
                if ev.topic in WS_EVENT_TOPICS or (ev.topic == "log" and ev.level in WS_LOG_LEVELS):
                    await send(_ws_frame("event", ev.model_dump(mode="json")))

        async def reader() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await send({"type": "pong", "ts": utcnow().isoformat()})

        tasks = [asyncio.create_task(pusher()), asyncio.create_task(events()), asyncio.create_task(reader())]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError, ValueError)):
                    log.warning("ws/stream task ended: %s: %s", type(exc).__name__, exc)
        finally:
            for t in tasks:
                t.cancel()
            eng.bus.unsubscribe(queue)

    # ---- MCP + static UI (mounted last) ----------------------------------------------------
    app.mount("/mcp", mcp_app, name="mcp")
    ui = Path(ui_dir) if ui_dir is not None else UI_OUT_DIR
    if (ui / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(ui), html=True), name="ui")
        app.state.ui_dir = str(ui)
    else:
        app.state.ui_dir = None
    return app


__all__ = ["create_app", "UI_OUT_DIR", "CORS_ORIGINS", "McpBarePathMiddleware"]
