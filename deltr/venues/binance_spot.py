"""
Binance Spot market-data mirror (``https://data-api.binance.vision``), keyless.

The mirror serves **real mainnet spot** prices: the same book Binance shows, on a
public host that needs no key and exposes no signed endpoint, so this client has no
way to sign or place an order by construction.

The spot ``bookTicker`` is a *displayed sanity reference* only (DESIGN_FINAL
decision 8): the perp reference price is the futures ``markPrice``, and no execution
ever touches spot.

Two provenance rules this module enforces:

* the ``DataSource`` tag is DERIVED from the host that answers
  (:func:`deltr.funding_history.data_source_for`), never asserted by the caller, so a
  quote can never claim a provenance it did not come from;
* an unrecognised host is refused at construction rather than quietly tagged, and a
  hostname that will not resolve produces an error naming the resolver to check.
  Nothing here falls back to another venue or to simulated data.

``api.binance.com`` (mainnet spot REST) is deliberately not used: it answers 403 from
some networks, and the mirror carries the same market data without a key.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from deltr.funding_history import SPOT_MIRROR_REST, data_source_for, dns_hint_for, resolver_hint
from deltr.models import DataSource, Quote, Venue, VenueHealth

log = logging.getLogger("deltr.venues.binance_spot")

PING_PATH = "/api/v3/ping"
BOOK_TICKER_PATH = "/api/v3/ticker/bookTicker"


class SpotMirrorError(Exception):
    """Spot mirror request failed (transport, HTTP status, or Binance error payload).

    ``hint`` carries the operator-actionable next step (for example which resolver to
    check when the hostname did not resolve).
    """

    def __init__(self, msg: str, code: Optional[int] = None, *, hint: str = "") -> None:
        super().__init__(f"{msg}. {hint}" if hint else msg)
        self.message = msg
        self.code = code
        self.hint = hint


class SpotMirror:
    """Read-only spot ``bookTicker`` client. Nothing here can sign or place an order."""

    VENUE_NAME = "binance_spot_mirror"

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        source = data_source_for(base_url)
        if source is not DataSource.BINANCE_SPOT_MIRROR:
            raise ValueError(
                f"SpotMirror refuses an unrecognised market-data host {base_url!r}: it would have to guess "
                f"the provenance tag. Use {SPOT_MIRROR_REST} (keyless mainnet spot market data)."
            )
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.source: DataSource = source
        self._last_ok_at: Optional[float] = None

    def __repr__(self) -> str:
        return f"SpotMirror(base_url={self.base_url!r}, source={self.source.value!r})"

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        try:
            r = await self.http.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as e:
            hint = dns_hint_for(e, self.base_url) or ""
            if hint:
                raise SpotMirrorError(f"GET {path}: hostname did not resolve", hint=resolver_hint(self.base_url)) from e
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
        raw = await self._get(BOOK_TICKER_PATH, {"symbol": symbol})
        return Quote(
            venue=Venue.BINANCE_SPOT,
            symbol=str(raw["symbol"]),
            bid=float(raw["bidPrice"]),
            ask=float(raw["askPrice"]),
            bid_qty=float(raw.get("bidQty") or 0.0),
            ask_qty=float(raw.get("askQty") or 0.0),
            ts=datetime.now(timezone.utc),
            source=self.source,
        )

    async def probe(self) -> VenueHealth:
        """``/api/v3/ping`` round trip; never raises."""
        t0 = time.monotonic()
        try:
            await self._get(PING_PATH)
        except SpotMirrorError as e:
            age = int((time.monotonic() - self._last_ok_at) * 1000) if self._last_ok_at else 0
            detail = e.hint or str(e)
            return VenueHealth(name=self.VENUE_NAME, ok=False, age_ms=age, source=self.source, detail=detail[:200])
        latency = int((time.monotonic() - t0) * 1000)
        return VenueHealth(
            name=self.VENUE_NAME, ok=True, age_ms=0, source=self.source,
            detail=f"{self.base_url} rtt {latency} ms (keyless mainnet spot market-data mirror)",
        )


__all__ = ["SpotMirror", "SpotMirrorError", "SPOT_MIRROR_REST", "PING_PATH", "BOOK_TICKER_PATH"]
