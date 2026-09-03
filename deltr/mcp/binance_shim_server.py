"""deltr/mcp/binance_shim_server.py — local Binance MCP shim (FastMCP, stdio, name ``binance-shim``).

A tiny SECOND MCP server the Agent OS bridge (``agents/agent_os_bridge.ts``) can
discover when the official hosted Binance MCP server
(``https://agent.binance.com/mcp/agentic``, OAuth-gated) is not reachable.  It
exposes Binance-shaped market, account and trade tools backed by the USDⓈ-M
Futures **testnet** REST API plus the keyless spot data mirror.

Safety properties (decision 14):

* Deltr's executor never routes through this server — it is off the execution path.
* ``BINANCE_API_ENV=prod`` is refused at build time; the base URL must contain ``testnet``.
* Market tools are keyless.  Account/trade tools need ``BINANCE_API_KEY`` /
  ``BINANCE_SECRET_KEY`` and otherwise return ``{"error": {"code": "NO_CREDENTIALS"}}``.
* ``set_leverage`` refuses leverage > 3; ``place_futures_order`` only places
  ``LIMIT`` + ``IOC`` orders and defaults to the ``/fapi/v1/order/test`` endpoint.
* Nothing is written to stdout except the MCP transport; logs go to stderr.

Tool names follow ``tests/fixtures/binance_mcp_tools.json`` when that file exists and
contains recognisable equivalents; otherwise the defaults below are used.  The shipped
fixture is a **transcription of a third-party published inventory** of the official
surface (see its ``provenance`` block) — not a capture from our own OAuth session, and
not verified against Binance.  A shim tool with no equivalent in that inventory (the
funding-rate read, and both write tools, for which the published surface has no
counterpart) keeps its Deltr-local name.

Run: ``python -m deltr.mcp.binance_shim_server``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Optional
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from deltr.config import REPO_ROOT, Settings, load_settings
from deltr.mcp.activity import ActivityLog, instrumented
from deltr.models import SymbolFilters

log = logging.getLogger("deltr.mcp.binance_shim")

MAX_LEVERAGE = 3
RECV_WINDOW_MS = 5000
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "binance_mcp_tools.json"

DEFAULT_TOOL_NAMES: dict[str, str] = {
    "get_ticker": "get_ticker",
    "get_order_book": "get_order_book",
    "get_mark_price": "get_mark_price",
    "get_funding_rate": "get_funding_rate",
    "get_exchange_filters": "get_exchange_filters",
    "get_account": "get_account",
    "get_positions": "get_positions",
    "set_leverage": "set_leverage",
    "place_futures_order": "place_futures_order",
}

# Substrings (lower-cased, punctuation-stripped) that identify an official tool as an
# equivalent of one of ours, **most specific first** — the first hint that matches any
# official name wins.  The official server uses dotted `<product>.<operation>` names such
# as `spot.ticker24hr`; only the published conventions are matched here.
#
# `place_futures_order` deliberately carries no bare `"order"` hint: the published
# inventory's order tools are all read-only queries (`futures_usds.queryOrder`,
# `spot.getOrder`), and aliasing a write tool onto a read-only official name would be a
# false claim.  Same for `set_leverage` — no write tool exists in the published surface,
# so both keep their Deltr-local names.
_FIXTURE_HINTS: dict[str, tuple[str, ...]] = {
    "get_ticker": ("bookticker", "tickerbook", "ticker24hr"),
    "get_order_book": ("depth", "orderbook"),
    "get_mark_price": ("premiumindex", "markprice"),
    "get_funding_rate": ("fundingrate",),
    "get_exchange_filters": ("exchangeinfo", "exchangefilters"),
    "get_account": ("balance", "account"),
    "get_positions": ("positionrisk", "positioninformation", "positions", "position"),
    "set_leverage": ("leverage",),
    "place_futures_order": ("neworder", "createorder", "placeorder"),
}

# Shim tools whose data comes from the spot mirror as well as the testnet perp, and which
# may therefore bind to a `spot.*` official name.  Every other tool is futures-backed and
# only binds to a name that looks like a futures product (`fut` / `um` in the key).
_SPOT_BINDABLE: frozenset[str] = frozenset({"get_ticker", "get_order_book"})


class NoCredentials(Exception):
    """Raised inside signed tools when the shim has no API key/secret."""

    code = "NO_CREDENTIALS"


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": {"code": code, "message": message}}
    out.update(extra)
    return out


def resolve_tool_names(fixture_path: Path = FIXTURE_PATH) -> dict[str, str]:
    """Map shim default tool names to the official names found in the fixture (if any).

    The fixture is either the raw ``tools/list`` result captured by
    ``scripts/probe_binance_mcp.py`` or the shipped transcription of a third-party
    published inventory (``{"tools": [{"name": ...}, ...]}`` or a bare list); the file's
    ``provenance`` block says which.  Hints are tried most-specific first, each official
    name is claimed by at most one shim tool, and an unmatched tool keeps its default
    name.  Never raises: a missing or unreadable fixture just means default names.
    """
    names = dict(DEFAULT_TOOL_NAMES)
    try:
        if not fixture_path.exists():
            return names
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        tools = data.get("tools", data) if isinstance(data, dict) else data
        official = [str(t.get("name", "")) for t in tools if isinstance(t, dict) and t.get("name")]
    except Exception as exc:  # noqa: BLE001 — a bad fixture must not break startup
        log.warning("binance_mcp_tools.json unreadable (%s); using default tool names", exc)
        return names
    keys = {c: c.lower().replace("_", "").replace("-", "").replace(".", "") for c in official}
    taken: set[str] = set()
    for ours, hints in _FIXTURE_HINTS.items():
        match = next(
            (
                candidate
                for hint in hints
                for candidate in official
                if candidate not in taken
                and (ours in _SPOT_BINDABLE or "fut" in keys[candidate] or "um" in keys[candidate])
                and hint in keys[candidate]
            ),
            None,
        )
        if match is not None:
            names[ours] = match
            taken.add(match)
    return names


# --------------------------------------------------------------------------- REST helper
class _Rest:
    """Minimal keyless + HMAC-signed httpx wrapper over the futures testnet and spot mirror."""

    def __init__(self, settings: Any, http: httpx.AsyncClient) -> None:
        hosts = settings.hosts
        self.futures_base: str = hosts["futures_rest"].rstrip("/")
        self.spot_base: str = hosts["spot_rest"].rstrip("/")
        self.api_key: Optional[str] = getattr(settings, "binance_api_key", None)
        self.secret_key: Optional[str] = getattr(settings, "binance_secret_key", None)
        self.http = http

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key)

    async def public(self, base: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        r = await self.http.get(base + path, params={k: v for k, v in (params or {}).items() if v is not None}, timeout=10.0)
        return _payload(r)

    async def signed(self, method: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        if not self.has_credentials:
            raise NoCredentials("BINANCE_API_KEY / BINANCE_SECRET_KEY not set; signed endpoints are unavailable")
        assert self.secret_key is not None and self.api_key is not None
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        clean["recvWindow"] = RECV_WINDOW_MS
        clean["timestamp"] = int(time.time() * 1000)
        query = urlencode(clean, doseq=True)
        signature = hmac.new(self.secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{self.futures_base}{path}?{query}&signature={signature}"
        r = await self.http.request(method, url, headers={"X-MBX-APIKEY": self.api_key}, timeout=10.0)
        return _payload(r)


def _payload(r: httpx.Response) -> Any:
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:500]}
    if r.status_code >= 400:
        code = body.get("code", r.status_code) if isinstance(body, dict) else r.status_code
        msg = body.get("msg", r.text[:200]) if isinstance(body, dict) else r.text[:200]
        raise httpx.HTTPStatusError(f"binance {code}: {msg}", request=r.request, response=r)
    return body


def _http_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, NoCredentials):
        return _error("NO_CREDENTIALS", str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
        except ValueError:
            body = {}
        return _error("BINANCE_ERROR", str(exc), status=exc.response.status_code, binance=body)
    if isinstance(exc, httpx.HTTPError):
        return _error("UPSTREAM_UNREACHABLE", f"{type(exc).__name__}: {exc}")
    log.exception("shim tool failed")
    return _error("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- server factory
def build_shim(
    settings: Any,
    http: Optional[httpx.AsyncClient] = None,
    activity: Optional[ActivityLog] = None,
    fixture_path: Optional[Path] = None,
) -> FastMCP:
    """Build the ``binance-shim`` FastMCP server.

    ``settings`` is duck-typed (``hosts``, ``binance_api_env``, ``binance_api_key``,
    ``binance_secret_key``).  Refuses ``binance_api_env == "prod"`` and any futures
    host that is not a testnet.  ``http`` may be an ``httpx.AsyncClient`` with a
    ``MockTransport`` for offline tests.  ``fixture_path`` overrides the official
    tool-name fixture (a path that does not exist ⇒ the default names).
    """
    env = str(getattr(settings, "binance_api_env", "testnet") or "testnet").lower()
    if env == "prod":
        raise RuntimeError("binance-shim refuses BINANCE_API_ENV=prod: Deltr never touches production trading hosts")
    futures_host = str(settings.hosts.get("futures_rest", ""))
    if "testnet" not in futures_host and "demo" not in futures_host:
        raise RuntimeError(f"binance-shim requires a testnet futures host, got {futures_host!r}")

    rest = _Rest(settings, http or httpx.AsyncClient())
    names = resolve_tool_names(fixture_path if fixture_path is not None else FIXTURE_PATH)
    record = instrumented(activity if activity is not None else ActivityLog(None), "binance-shim")
    mcp = FastMCP(
        name="binance-shim",
        instructions=(
            "Local Binance-shaped MCP shim backed by the USDS-M Futures TESTNET REST API and the keyless spot "
            "data mirror.  Market tools need no keys.  Account/trade tools need BINANCE_API_KEY/BINANCE_SECRET_KEY "
            "(testnet) and otherwise return NO_CREDENTIALS.  Leverage is capped at 3x; only LIMIT IOC orders are "
            "accepted and they go to /fapi/v1/order/test unless test=false.  Production is refused.  Deltr's own "
            "executor never routes through this server."
        ),
    )

    # ------------------------------------------------------------------ market (keyless)
    @mcp.tool(name=names["get_ticker"])
    @record
    async def get_ticker(ctx: Context, symbol: Annotated[str, Field(description="e.g. BNBUSDT")] = "BNBUSDT") -> dict[str, Any]:
        """Best bid/ask from the spot data mirror (data-api.binance.vision) and the futures TESTNET perp book. Keyless."""
        sym = symbol.upper()
        try:
            spot = await rest.public(rest.spot_base, "/api/v3/ticker/bookTicker", {"symbol": sym})
            perp = await rest.public(rest.futures_base, "/fapi/v1/ticker/bookTicker", {"symbol": sym})
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {
            "symbol": sym,
            "spot_mirror": {**spot, "source": "binance-spot-mirror"},
            "perp_testnet": {**perp, "source": "binance-futures-testnet"},
            "note": "testnet perp book is thin; use mark price for reference",
        }

    @mcp.tool(name=names["get_order_book"])
    @record
    async def get_order_book(
        ctx: Context,
        symbol: Annotated[str, Field(description="e.g. BNBUSDT")] = "BNBUSDT",
        limit: Annotated[int, Field(ge=5, le=1000, description="Depth levels")] = 20,
    ) -> dict[str, Any]:
        """Futures TESTNET order book (/fapi/v1/depth). Keyless."""
        try:
            book = await rest.public(rest.futures_base, "/fapi/v1/depth", {"symbol": symbol.upper(), "limit": limit})
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {"symbol": symbol.upper(), **book, "source": "binance-futures-testnet"}

    @mcp.tool(name=names["get_mark_price"])
    @record
    async def get_mark_price(ctx: Context, symbol: Annotated[str, Field(description="e.g. BNBUSDT")] = "BNBUSDT") -> dict[str, Any]:
        """Mark price, index price, last funding rate and next funding time (/fapi/v1/premiumIndex, testnet). Keyless."""
        try:
            pi = await rest.public(rest.futures_base, "/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {
            "symbol": symbol.upper(),
            "mark_price": float(pi.get("markPrice", 0.0)),
            "index_price": float(pi.get("indexPrice", 0.0)),
            "last_funding_rate": float(pi.get("lastFundingRate", 0.0)),
            "next_funding_time_ms": int(pi.get("nextFundingTime", 0)),
            "raw": pi,
            "source": "binance-futures-testnet",
        }

    @mcp.tool(name=names["get_funding_rate"])
    @record
    async def get_funding_rate(
        ctx: Context,
        symbol: Annotated[str, Field(description="e.g. BNBUSDT")] = "BNBUSDT",
        limit: Annotated[int, Field(ge=1, le=100, description="How many recent settlements")] = 3,
    ) -> dict[str, Any]:
        """Recent funding settlements (/fapi/v1/fundingRate) and the funding interval (/fapi/v1/fundingInfo). Testnet, indicative."""
        sym = symbol.upper()
        try:
            rows = await rest.public(rest.futures_base, "/fapi/v1/fundingRate", {"symbol": sym, "limit": limit})
            interval_h = 8
            try:
                info = await rest.public(rest.futures_base, "/fapi/v1/fundingInfo")
                for row in info if isinstance(info, list) else []:
                    if row.get("symbol") == sym and row.get("fundingIntervalHours"):
                        interval_h = int(row["fundingIntervalHours"])
            except Exception:  # noqa: BLE001 — interval is optional metadata
                pass
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        rows = rows[-limit:] if isinstance(rows, list) else []
        last = float(rows[-1]["fundingRate"]) if rows else 0.0
        return {
            "symbol": sym,
            "interval_h": interval_h,
            "history": rows,
            "last_rate": last,
            "annualized_pct": last * (24 / interval_h) * 365 * 100,
            "label": "Binance Futures testnet (indicative)",
            "source": "binance-futures-testnet",
        }

    @mcp.tool(name=names["get_exchange_filters"])
    @record
    async def get_exchange_filters(ctx: Context, symbol: Annotated[str, Field(description="e.g. BNBUSDT")] = "BNBUSDT") -> dict[str, Any]:
        """LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL and precisions for the symbol (/fapi/v1/exchangeInfo) as SymbolFilters. Keyless."""
        sym = symbol.upper()
        try:
            info = await rest.public(rest.futures_base, "/fapi/v1/exchangeInfo")
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        entry = next((s for s in info.get("symbols", []) if s.get("symbol") == sym), None)
        if entry is None:
            return _error("INVALID_ARGUMENT", f"symbol {sym} not found in exchangeInfo")
        filters = {f.get("filterType"): f for f in entry.get("filters", [])}
        sf = SymbolFilters(
            symbol=sym,
            step_size=float(filters.get("LOT_SIZE", {}).get("stepSize", 0.01)),
            min_qty=float(filters.get("LOT_SIZE", {}).get("minQty", 0.01)),
            tick_size=float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.01)),
            min_notional=float(filters.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
            price_precision=int(entry.get("pricePrecision", 3)),
            qty_precision=int(entry.get("quantityPrecision", 2)),
        )
        return sf.model_dump(mode="json")

    # ------------------------------------------------------------------ account (signed)
    @mcp.tool(name=names["get_account"])
    @record
    async def get_account(ctx: Context) -> dict[str, Any]:
        """Futures TESTNET wallet balances (/fapi/v2/balance). Signed: needs BINANCE_API_KEY/BINANCE_SECRET_KEY, else NO_CREDENTIALS."""
        try:
            rows = await rest.signed("GET", "/fapi/v2/balance")
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {"balances": rows, "env": env, "source": "binance-futures-testnet"}

    @mcp.tool(name=names["get_positions"])
    @record
    async def get_positions(ctx: Context, symbol: Annotated[Optional[str], Field(description="Filter by symbol")] = None) -> dict[str, Any]:
        """Futures TESTNET position risk (/fapi/v2/positionRisk). Signed: NO_CREDENTIALS without keys."""
        try:
            rows = await rest.signed("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper() if symbol else None})
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {"positions": rows, "env": env, "source": "binance-futures-testnet"}

    @mcp.tool(name=names["set_leverage"])
    @record
    async def set_leverage(
        ctx: Context,
        symbol: Annotated[str, Field(description="e.g. BNBUSDT")],
        leverage: Annotated[int, Field(ge=1, description="Target leverage; anything above 3 is refused")],
    ) -> dict[str, Any]:
        """Set the symbol's leverage (/fapi/v1/leverage) on TESTNET. Refuses leverage > 3 (LEVERAGE_LIMIT). Signed."""
        if leverage > MAX_LEVERAGE:
            return _error("LEVERAGE_LIMIT", f"leverage {leverage}x exceeds the {MAX_LEVERAGE}x hard cap", observed=leverage, limit=MAX_LEVERAGE)
        try:
            ack = await rest.signed("POST", "/fapi/v1/leverage", {"symbol": symbol.upper(), "leverage": leverage})
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {"ack": ack, "env": env, "source": "binance-futures-testnet"}

    @mcp.tool(name=names["place_futures_order"])
    @record
    async def place_futures_order(
        ctx: Context,
        symbol: Annotated[str, Field(description="e.g. BNBUSDT")],
        side: Annotated[Literal["BUY", "SELL"], Field(description="Order side")],
        quantity: Annotated[float, Field(gt=0, description="Base quantity (step-aligned)")],
        price: Annotated[float, Field(gt=0, description="Limit price (tick-aligned)")],
        timeInForce: Annotated[Literal["IOC"], Field(description="Only IOC is accepted")] = "IOC",
        reduceOnly: Annotated[bool, Field(description="Reduce-only flag")] = False,
        newClientOrderId: Annotated[Optional[str], Field(max_length=36, description="Idempotent client order id")] = None,
        test: Annotated[bool, Field(description="true -> /fapi/v1/order/test (validates, never fills)")] = True,
    ) -> dict[str, Any]:
        """Place a LIMIT IOC order on the futures TESTNET. Only LIMIT+IOC is accepted; test=true (default) hits
        /fapi/v1/order/test and never fills. Signed: NO_CREDENTIALS without keys. Production is impossible here."""
        if timeInForce != "IOC":
            return _error("INVALID_ARGUMENT", "only timeInForce=IOC is accepted by the shim")
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "LIMIT",
            "timeInForce": "IOC",
            "quantity": quantity,
            "price": price,
            "reduceOnly": "true" if reduceOnly else None,
            "newClientOrderId": newClientOrderId,
            "newOrderRespType": "RESULT",
        }
        path = "/fapi/v1/order/test" if test else "/fapi/v1/order"
        try:
            ack = await rest.signed("POST", path, params)
        except Exception as exc:  # noqa: BLE001
            return _http_error(exc)
        return {"ack": ack, "test": test, "endpoint": path, "type": "LIMIT", "timeInForce": "IOC", "env": env, "source": "binance-futures-testnet"}

    return mcp


def _configure_stderr_logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    """Entry point for ``python -m deltr.mcp.binance_shim_server`` (stdio transport)."""
    _configure_stderr_logging()
    try:
        # The shim only needs hosts + optional keys; a TESTNET .env without keys must not stop it,
        # but BINANCE_API_ENV=prod is always fatal (Settings refuses it in both branches).
        try:
            settings: Settings = load_settings()
        except Exception as first:  # noqa: BLE001
            if "prod" in str(first):
                raise
            settings = load_settings(DELTR_MODE="paper")
    except Exception as exc:  # noqa: BLE001 — validation errors are fatal
        log.error("binance-shim refused to start: %s", exc)
        raise SystemExit(2) from exc
    shim = build_shim(settings)
    log.info("binance-shim: stdio transport up (env=%s, keys=%s)", settings.binance_api_env, "present" if settings.secrets_present else "absent")
    await shim.run_stdio_async()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
