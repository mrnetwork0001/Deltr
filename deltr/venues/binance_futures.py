"""
Binance USDⓈ-M Futures REST client (public + HMAC-SHA256 signed), testnet only.

Design (DESIGN_FINAL 3.8 / 4.4):

* Public: ``/fapi/v1/time`` (clock offset), ``/fapi/v1/exchangeInfo`` →
  ``SymbolFilters`` (never hardcoded), ``/fapi/v1/fundingInfo`` (interval),
  ``/fapi/v1/premiumIndex`` → ``FundingSnapshot`` (mark = perp reference),
  ``/fapi/v1/ticker/bookTicker`` → ``Quote`` (fill simulation + sanity only),
  ``/fapi/v1/depth``.
* Signed (``X-MBX-APIKEY`` header; ``signature`` = HMAC-SHA256 over the
  url-encoded query incl. ``timestamp`` (+server offset) and ``recvWindow``):
  ``prepare_account`` (dual-side check, ISOLATED margin ignoring −4046,
  leverage only when flat ignoring −4028), ``place_limit_ioc`` (LIMIT +
  timeInForce=IOC **only**, never MARKET), ``get_order_by_client_id``,
  ``position_risk``, ``usdt_balance``.
* Errors are typed ``FuturesError(code, msg, retryable)`` using the map below;
  transport failures are ``FuturesError(code=-1)`` and timeouts
  ``FuturesTimeout`` so the router can *query before retrying*.
* **Safety:** the constructor refuses credentials unless ``"testnet"`` is in
  ``base_url`` — this client can never sign against production.

Secrets are never logged; only paths and redacted parameter names appear in
log records (stderr).  Nothing here prints to stdout.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

import httpx

from deltr.models import DataSource, FundingSnapshot, Quote, Side, SymbolFilters, Venue, VenueHealth

log = logging.getLogger("deltr.venues.binance_futures")

CLIENT_ID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")
TRANSPORT_ERROR_CODE = -1
TIMEOUT_ERROR_CODE = -1007  # Binance's own "Timeout waiting for response from backend server"
DEFAULT_FUNDING_INTERVAL_H = 8
SECRET_PARAM_NAMES = ("signature", "api_key", "apikey", "secret", "token", "listenkey")

# code -> (retryable, human note).  Everything not listed is non-retryable (fail fast, honest).
ERROR_MAP: dict[int, tuple[bool, str]] = {
    -1000: (True, "unknown error"),
    -1001: (True, "internal error / disconnected"),
    -1003: (True, "rate limit"),
    -1007: (True, "backend timeout — query the order before retrying"),
    -1021: (True, "timestamp outside recvWindow — resync time and retry"),
    -1022: (False, "invalid signature"),
    -1111: (False, "precision over the maximum — sizing bug, do not retry"),
    -1013: (False, "filter failure (LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL)"),
    -2010: (False, "new order rejected"),
    -2011: (False, "unknown order"),
    -2013: (False, "order does not exist"),
    -2014: (False, "API-key format invalid"),
    -2015: (False, "invalid API-key, IP or permissions"),
    -2019: (False, "margin insufficient"),
    -2021: (False, "order would immediately trigger"),
    -2022: (False, "reduceOnly order rejected"),
    -4028: (False, "leverage not valid / unchanged"),
    -4046: (False, "margin type unchanged (no need to change)"),
    -4061: (True, "positionSide mismatch with account mode — re-read dual-side and retry once"),
    -4164: (False, "order notional below the 5 USDT minimum"),
}


class FuturesError(Exception):
    """Typed Binance Futures failure. ``retryable`` follows the 3.8 map."""

    def __init__(self, code: int, msg: str, retryable: bool) -> None:
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.retryable = retryable

    @classmethod
    def from_code(cls, code: int, msg: str) -> "FuturesError":
        retryable, note = ERROR_MAP.get(code, (False, "unmapped"))
        return cls(code, f"{msg} ({note})" if note != "unmapped" else msg, retryable)


class FuturesTimeout(FuturesError):
    """The request may or may not have reached the matching engine: look the order up before retrying."""

    def __init__(self, msg: str) -> None:
        super().__init__(TIMEOUT_ERROR_CODE, msg, True)


@dataclass(frozen=True)
class OrderResult:
    order_id: int
    client_id: str
    status: str
    executed_qty: float
    avg_price: float
    raw: dict

    @property
    def filled_any(self) -> bool:
        return self.executed_qty > 0


@dataclass(frozen=True)
class AccountPrep:
    dual_side: bool
    position_side: str  # "SHORT" in hedge mode (both open SELL and closing BUY), "BOTH" in one-way
    margin_type: str
    leverage: int
    usdt_balance: float


def redact(params: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``params`` with key/secret/token/signature values masked (for logs and activity rows)."""
    out: dict[str, Any] = {}
    for k, v in params.items():
        kl = k.lower()
        out[k] = "***" if any(s in kl for s in SECRET_PARAM_NAMES) or "key" in kl else v
    return out


def fmt_decimal(value: float, precision: Optional[int] = None) -> str:
    """Exchange-safe number string: no scientific notation, floored to ``precision`` decimals when given."""
    d = Decimal(str(value))
    if precision is not None:
        d = d.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
    s = f"{d:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def parse_order(raw: Mapping[str, Any]) -> OrderResult:
    """Normalise a POST /order (RESULT) or GET /order payload."""
    executed = float(raw.get("executedQty") or 0.0)
    avg = float(raw.get("avgPrice") or 0.0)
    if avg == 0.0 and executed > 0 and raw.get("cumQuote"):
        avg = float(raw["cumQuote"]) / executed
    return OrderResult(
        order_id=int(raw.get("orderId") or 0),
        client_id=str(raw.get("clientOrderId") or ""),
        status=str(raw.get("status") or "UNKNOWN"),
        executed_qty=executed,
        avg_price=avg,
        raw=dict(raw),
    )


def parse_symbol_filters(exchange_info: Mapping[str, Any], symbol: str, funding_interval_h: int = DEFAULT_FUNDING_INTERVAL_H) -> SymbolFilters:
    """Pull LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL and precisions for ``symbol`` out of exchangeInfo."""
    for s in exchange_info.get("symbols", []):
        if s.get("symbol") == symbol:
            f = {flt.get("filterType"): flt for flt in s.get("filters", [])}
            lot = f.get("LOT_SIZE", {})
            price = f.get("PRICE_FILTER", {})
            notional = f.get("MIN_NOTIONAL", {})
            return SymbolFilters(
                symbol=symbol,
                step_size=float(lot.get("stepSize", 0.01)),
                min_qty=float(lot.get("minQty", 0.01)),
                tick_size=float(price.get("tickSize", 0.01)),
                min_notional=float(notional.get("notional", 5.0)),
                price_precision=int(s.get("pricePrecision", 2)),
                qty_precision=int(s.get("quantityPrecision", 3)),
                funding_interval_h=funding_interval_h,
                source=DataSource.BINANCE_FUTURES_TESTNET,
            )
    raise FuturesError(-1121, f"symbol {symbol} not in exchangeInfo", False)


def parse_premium_index(raw: Mapping[str, Any], interval_h: int = DEFAULT_FUNDING_INTERVAL_H) -> FundingSnapshot:
    rate = float(raw.get("lastFundingRate") or 0.0)
    return FundingSnapshot(
        symbol=str(raw["symbol"]),
        mark_price=float(raw["markPrice"]),
        index_price=float(raw.get("indexPrice") or raw["markPrice"]),
        last_funding_rate=rate,
        next_funding_time_ms=int(raw.get("nextFundingTime") or 0),
        interval_h=interval_h,
        annualized_pct=rate * (24.0 / interval_h) * 365.0 * 100.0,
        ts=datetime.now(timezone.utc),
        source=DataSource.BINANCE_FUTURES_TESTNET,
    )


class FuturesClient:
    """USDⓈ-M Futures REST client. Keyless instances can only reach public endpoints."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if (api_key or secret_key) and "testnet" not in base_url.lower():
            raise ValueError("FuturesClient refuses credentials unless base_url contains 'testnet' (never signs against production)")
        if bool(api_key) != bool(secret_key):
            raise ValueError("api_key and secret_key must be given together")
        self.http = http
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._secret = secret_key.encode("utf-8") if secret_key else None
        self.recv_window_ms = int(recv_window_ms)
        self.time_offset_ms: int = 0
        self._filters: dict[str, SymbolFilters] = {}
        self._funding_interval: dict[str, int] = {}
        self._last_ok_at: Optional[float] = None

    def __repr__(self) -> str:  # never leaks the key
        return f"FuturesClient(base_url={self.base_url!r}, credentials={'yes' if self.has_credentials else 'no'})"

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._secret)

    # ---- signing -----------------------------------------------------------
    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def _sign(self, params: dict, timestamp_ms: Optional[int] = None) -> dict:
        """Append ``recvWindow``, ``timestamp`` (+offset) and the HMAC-SHA256 ``signature`` (insertion order kept)."""
        if not self.has_credentials:
            raise FuturesError(-2015, "signed endpoint called without credentials", False)
        assert self._secret is not None
        signed = {k: v for k, v in params.items() if v is not None}
        signed.setdefault("recvWindow", self.recv_window_ms)
        signed["timestamp"] = timestamp_ms if timestamp_ms is not None else self._timestamp_ms()
        query = urlencode(signed, doseq=True)
        signed["signature"] = hmac.new(self._secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        return signed

    # ---- transport ---------------------------------------------------------
    async def _request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if signed:
            params = self._sign(params)
            headers["X-MBX-APIKEY"] = self._api_key or ""
        url = f"{self.base_url}{path}"
        log.debug("%s %s %s", method, path, redact(params))
        try:
            r = await self.http.request(method, url, params=params, headers=headers)
        except httpx.TimeoutException as e:
            raise FuturesTimeout(f"{method} {path}: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise FuturesError(TRANSPORT_ERROR_CODE, f"{method} {path}: {type(e).__name__}: {e}", True) from e
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("code"), int) and payload["code"] < 0:
            raise FuturesError.from_code(int(payload["code"]), str(payload.get("msg", "")))
        if r.status_code >= 400:
            retryable = r.status_code in (418, 429, 500, 502, 503, 504)
            raise FuturesError(-r.status_code, f"{method} {path}: HTTP {r.status_code}", retryable)
        if payload is None:
            raise FuturesError(TRANSPORT_ERROR_CODE, f"{method} {path}: non-JSON body", True)
        self._last_ok_at = time.monotonic()
        return payload

    # ---- public ------------------------------------------------------------
    async def server_time_ms(self) -> int:
        return int((await self._request("GET", "/fapi/v1/time"))["serverTime"])

    async def sync_time(self) -> int:
        """Store and return ``serverTime − local`` in ms (Binance rejects |skew| > recvWindow)."""
        local = int(time.time() * 1000)
        server = await self.server_time_ms()
        self.time_offset_ms = server - local
        log.info("futures clock offset %+d ms", self.time_offset_ms)
        return self.time_offset_ms

    async def funding_interval_h(self, symbol: str) -> int:
        """Funding interval from ``/fapi/v1/fundingInfo`` (only lists non-default symbols) — default 8 h."""
        try:
            rows = await self._request("GET", "/fapi/v1/fundingInfo")
        except FuturesError as e:
            log.warning("fundingInfo unavailable (%s); assuming %d h", e, DEFAULT_FUNDING_INTERVAL_H)
            rows = []
        interval = DEFAULT_FUNDING_INTERVAL_H
        for row in rows if isinstance(rows, list) else []:
            if row.get("symbol") == symbol and row.get("fundingIntervalHours"):
                interval = int(row["fundingIntervalHours"])
        self._funding_interval[symbol] = interval
        return interval

    async def exchange_filters(self, symbol: str) -> SymbolFilters:
        """LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL + precisions from exchangeInfo (cached for order formatting)."""
        info = await self._request("GET", "/fapi/v1/exchangeInfo")
        interval = self._funding_interval.get(symbol) or await self.funding_interval_h(symbol)
        filters = parse_symbol_filters(info, symbol, interval)
        self._filters[symbol] = filters
        return filters

    async def premium_index(self, symbol: str) -> FundingSnapshot:
        raw = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return parse_premium_index(raw, self._funding_interval.get(symbol, DEFAULT_FUNDING_INTERVAL_H))

    async def book_ticker(self, symbol: str) -> Quote:
        raw = await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return Quote(
            venue=Venue.BINANCE_FUTURES,
            symbol=str(raw["symbol"]),
            bid=float(raw["bidPrice"]),
            ask=float(raw["askPrice"]),
            bid_qty=float(raw.get("bidQty") or 0.0),
            ask_qty=float(raw.get("askQty") or 0.0),
            ts=datetime.now(timezone.utc),
            source=DataSource.BINANCE_FUTURES_TESTNET,
        )

    async def depth(self, symbol: str, limit: int = 20) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        raw = await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
        return bids, asks

    async def probe(self) -> VenueHealth:
        t0 = time.monotonic()
        try:
            server = await self.server_time_ms()
        except FuturesError as e:
            age = int((time.monotonic() - self._last_ok_at) * 1000) if self._last_ok_at else 0
            return VenueHealth(name="binance_futures_testnet", ok=False, age_ms=age, source=DataSource.BINANCE_FUTURES_TESTNET, detail=str(e))
        latency = int((time.monotonic() - t0) * 1000)
        skew = server - int(time.time() * 1000)
        return VenueHealth(
            name="binance_futures_testnet", ok=True, age_ms=0, source=DataSource.BINANCE_FUTURES_TESTNET,
            detail=f"{self.base_url} rtt {latency} ms skew {skew:+d} ms credentials={'yes' if self.has_credentials else 'no'}",
        )

    # ---- signed ------------------------------------------------------------
    async def dual_side_position(self) -> bool:
        raw = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        v = raw.get("dualSidePosition")
        return v is True or str(v).lower() == "true"

    async def position_risk(self, symbol: str) -> list[dict]:
        rows = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        return [dict(r) for r in rows] if isinstance(rows, list) else []

    async def usdt_balance(self) -> float:
        rows = await self._request("GET", "/fapi/v2/balance", signed=True)
        for row in rows if isinstance(rows, list) else []:
            if row.get("asset") == "USDT":
                return float(row.get("availableBalance") or row.get("balance") or 0.0)
        return 0.0

    async def prepare_account(self, symbol: str, leverage: int, isolated: bool = True) -> AccountPrep:
        """Time sync → filters → dual-side → margin type (ignore −4046) → leverage when flat (ignore −4028) → balance."""
        await self.sync_time()
        await self.exchange_filters(symbol)
        dual = await self.dual_side_position()
        margin_type = "ISOLATED" if isolated else "CROSSED"
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)
        except FuturesError as e:
            if e.code != -4046:
                raise
        positions = await self.position_risk(symbol)
        flat = all(abs(float(p.get("positionAmt") or 0.0)) == 0.0 for p in positions)

        def _account_leverage(rows: list[dict]) -> int:
            return next((int(float(p["leverage"])) for p in rows if p.get("leverage")), int(leverage))

        if flat:
            try:
                raw = await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)
                effective_leverage = int(raw.get("leverage", leverage))
            except FuturesError as e:
                if e.code != -4028:
                    raise
                # the exchange refused the change: report what the account REALLY has, never the request
                effective_leverage = _account_leverage(await self.position_risk(symbol))
                log.warning("%s leverage change refused (-4028); account leverage is %dx", symbol, effective_leverage)
        else:
            effective_leverage = _account_leverage(positions)
            log.warning("%s not flat on testnet; keeping account leverage %dx", symbol, effective_leverage)
        balance = await self.usdt_balance()
        return AccountPrep(
            dual_side=dual, position_side="SHORT" if dual else "BOTH", margin_type=margin_type,
            leverage=effective_leverage, usdt_balance=balance,
        )

    def _fmt_qty(self, symbol: str, qty: float) -> str:
        f = self._filters.get(symbol)
        return fmt_decimal(qty, f.qty_precision if f else None)

    def _fmt_price(self, symbol: str, price: float) -> str:
        f = self._filters.get(symbol)
        return fmt_decimal(price, f.price_precision if f else None)

    def ioc_params(
        self, symbol: str, side: Side, qty: float, price: float, client_id: str,
        reduce_only: bool = False, position_side: str = "BOTH",
    ) -> dict[str, Any]:
        """Unsigned parameter set for a LIMIT IOC order (the ONLY order type Deltr sends)."""
        if not CLIENT_ID_RE.match(client_id):
            raise ValueError(f"client_id {client_id!r} violates ^[.A-Z:/a-z0-9_-]{{1,36}}$")
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be > 0")
        if position_side not in ("BOTH", "LONG", "SHORT"):
            raise ValueError(f"bad position_side {position_side!r}")
        side_v = side.value if isinstance(side, Side) else str(side).upper()
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side_v,
            "type": "LIMIT",
            "timeInForce": "IOC",
            "quantity": self._fmt_qty(symbol, qty),
            "price": self._fmt_price(symbol, price),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if position_side != "BOTH":
            params["positionSide"] = position_side  # hedge mode: reduceOnly must NOT be sent
        elif reduce_only:
            params["reduceOnly"] = "true"
        return params

    async def place_limit_ioc(
        self, symbol: str, side: Side, qty: float, price: float, client_id: str,
        reduce_only: bool = False, position_side: str = "BOTH",
    ) -> OrderResult:
        params = self.ioc_params(symbol, side, qty, price, client_id, reduce_only, position_side)
        raw = await self._request("POST", "/fapi/v1/order", params, signed=True)
        result = parse_order(raw)
        log.info("IOC %s %s %s @ %s -> %s filled %s", params["side"], params["quantity"], symbol, params["price"], result.status, result.executed_qty)
        return result

    async def get_order_by_client_id(self, symbol: str, client_id: str) -> OrderResult | None:
        try:
            raw = await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)
        except FuturesError as e:
            if e.code in (-2013, -2011):
                return None
            raise
        return parse_order(raw)

    async def place_limit_ioc_recovering(
        self, symbol: str, side: Side, qty: float, price: float, client_id: str,
        reduce_only: bool = False, position_side: str = "BOTH",
    ) -> OrderResult:
        """``place_limit_ioc`` + the 3.8 rule: on a timeout / transport failure, look the order up by
        ``client_id`` BEFORE the caller retries.  Found ⇒ return it (no duplicate order); not found ⇒
        re-raise the original retryable error so the router can retry with a fresh attempt id."""
        try:
            return await self.place_limit_ioc(symbol, side, qty, price, client_id, reduce_only, position_side)
        except FuturesError as e:
            if not (isinstance(e, FuturesTimeout) or e.code in (TRANSPORT_ERROR_CODE, -1007, -1001)):
                raise
            log.warning("order %s uncertain after %s; querying before retry", client_id, e)
            found = await self.get_order_by_client_id(symbol, client_id)
            if found is not None:
                return found
            raise


__all__ = [
    "ERROR_MAP", "CLIENT_ID_RE", "TRANSPORT_ERROR_CODE", "FuturesError", "FuturesTimeout", "OrderResult", "AccountPrep",
    "FuturesClient", "redact", "fmt_decimal", "parse_order", "parse_symbol_filters", "parse_premium_index",
]
