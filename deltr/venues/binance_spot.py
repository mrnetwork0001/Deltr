"""
Binance Spot market-data mirror (``https://data-api.binance.vision``), keyless.

The spot ``bookTicker`` is a *displayed sanity reference* only (DESIGN_FINAL
decision 8): the perp reference price is the futures-testnet ``markPrice``,
and no execution ever touches spot.  Public market-data host, no keys, no
signed endpoints exist on this client by construction.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from deltr.models import DataSource, Quote, Venue, VenueHealth

log = logging.getLogger("deltr.venues.binance_spot")


class SpotMirrorError(Exception):
    """Spot mirror request failed (transport, HTTP status, or Binance error payload)."""

    def __init__(self, msg: str, code: Optional[int] = None) -> None:
        super().__init__(msg)
        self.code = code


class SpotMirror:
    """Read-only spot ``bookTicker`` client. Nothing here can sign or place an order."""

    VENUE_NAME = "binance_spot_mirror"

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.http = http
        self.base_url = base_url.rstrip("/")
        self._last_ok_at: Optional[float] = None

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        try:
            r = await self.http.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as e:
            raise SpotMirrorError(f"GET {path}: {type(e).__name__}: {e}") from e
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("code"), int) and payload["code"] < 0:
            raise SpotMirrorError(f"GET {path}: {payload.get('msg', '')}", code=int(payload["code"]))
        if r.status_code >= 400 or payload is None:
            raise SpotMirrorError(f"GET {path}: HTTP {r.status_code}")
        self._last_ok_at = time.monotonic()
        return payload

    async def book_ticker(self, symbol: str) -> Quote:
        raw = await self._get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return Quote(
            venue=Venue.BINANCE_SPOT,
            symbol=str(raw["symbol"]),
            bid=float(raw["bidPrice"]),
            ask=float(raw["askPrice"]),
            bid_qty=float(raw.get("bidQty") or 0.0),
            ask_qty=float(raw.get("askQty") or 0.0),
            ts=datetime.now(timezone.utc),
            source=DataSource.BINANCE_SPOT_MIRROR,
        )

    async def probe(self) -> VenueHealth:
        """``/api/v3/ping`` round trip; never raises."""
        t0 = time.monotonic()
        try:
            await self._get("/api/v3/ping")
        except SpotMirrorError as e:
            age = int((time.monotonic() - self._last_ok_at) * 1000) if self._last_ok_at else 0
            return VenueHealth(name=self.VENUE_NAME, ok=False, age_ms=age, source=DataSource.BINANCE_SPOT_MIRROR, detail=str(e))
        latency = int((time.monotonic() - t0) * 1000)
        return VenueHealth(
            name=self.VENUE_NAME, ok=True, age_ms=0, source=DataSource.BINANCE_SPOT_MIRROR,
            detail=f"{self.base_url} rtt {latency} ms (keyless market-data mirror)",
        )


__all__ = ["SpotMirror", "SpotMirrorError"]
