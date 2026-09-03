"""deltr/payments.py — the x402 (B402) payment workflow, in both directions.

x402 is the HTTP-native payment handshake: a server answers a request it will not serve for
free with ``402 Payment Required`` and a machine-readable list of ways to pay; the client picks
one, signs it, and replays the request with the payment header attached.

Deltr uses it two ways, and both go through the Binance Agentic Wallet CLI:

**As a BUYER** (:class:`X402Buyer`) — when an upstream data or compute endpoint answers 402,
Deltr previews the options with ``baw x402-payment preview``, applies its own policy (network
allow-list, per-payment ceiling, explicit index), and asks the wallet to sign the chosen one with
``baw x402-payment sign``.  **The wallet signs; Deltr never holds a key** and receives only the
replay header value to put on the retry.

**As a SELLER** (:class:`X402Seller`) — a Deltr endpoint can answer 402 with a well-formed
challenge whose paid artifact is the existing edge report (``deltr_edge_report`` /
``render_edge_report``), sealed with the same sha256 the rest of Deltr uses.  The challenge is
built and the artifact is sealed locally; **settlement is not claimed**.  Verification of a
presented payment needs a facilitator, so a payment Deltr has not had verified is recorded as
*presented, unverified* and the artifact is not released on its own.

SCOPE AND HONESTY
-----------------
* Network is BNB Smart Chain (BSC) and is labelled as such on every object this module returns.
* This is Deltr's local implementation of the challenge and payment shapes.  No mainnet B402
  application was made, no B402 endpoint of Deltr's is registered anywhere, and nothing here
  should be read as a claim that a payment settled.

Never prints to stdout (the stdio MCP transport owns it).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from deltr.models import sha256_of, utcnow
from deltr.venues.agentic_wallet import AgenticWalletClient, AgenticWalletError

log = logging.getLogger("deltr.payments")

X402_VERSION = 2
BSC_NETWORK = "bsc"
BSC_CHAIN_ID = 56
#: BSC USDT — 18 decimals on this chain, not 6 (see deltr/venues/pancake_constants.py).
BSC_USDT_ADDRESS = "0x55d398326f99059fF775485246999027B3197955"
BSC_USDT_DECIMALS = 18

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_HEADER = "X-PAYMENT"

NETWORK_LABEL = "BNB Smart Chain (BSC) mainnet"
SCOPE_NOTE = (
    "x402 (B402) on BNB Smart Chain. Deltr builds the challenge and asks the Binance Agentic "
    "Wallet to sign; Deltr holds no key. No mainnet B402 application was made and no settlement "
    "is claimed by this object."
)
UNVERIFIED_NOTE = (
    "Presented, not verified. Verifying a payment needs a facilitator; until one confirms it, "
    "Deltr records the header and does not treat the artifact as paid for."
)

#: A default ceiling so a buyer cannot be talked into an arbitrary amount by a 402 response.
DEFAULT_MAX_PAYMENT_USD = 1.00
# Below this, a non-zero atomic amount is not a tiny payment: it is a decimals mismatch.
DUST_UNITS = 1e-6


# --------------------------------------------------------------------------- #
# errors                                                                        #
# --------------------------------------------------------------------------- #
class PaymentError(RuntimeError):
    """A payment could not be built, previewed or signed."""

    def __init__(self, code: str, message: str, *, remedy: Optional[str] = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remedy = remedy
        self.extra = dict(extra)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.remedy:
            out["remedy"] = self.remedy
        out.update(self.extra)
        return out

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message} ({self.code})"


class PaymentRefused(PaymentError):
    """Deltr's own policy refused the payment before anything was signed."""


# --------------------------------------------------------------------------- #
# the 402 challenge, as received                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PaymentOption:
    """One entry of a 402 response's ``accepts`` array, normalised."""

    index: int
    scheme: str
    network: str
    asset: Optional[str]
    amount_atomic: Optional[str]
    pay_to: Optional[str]
    resource: Optional[str]
    description: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "scheme": self.scheme, "network": self.network,
            "asset": self.asset, "amount_atomic": self.amount_atomic, "pay_to": self.pay_to,
            "resource": self.resource, "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class PaymentRequired:
    """A parsed ``402 Payment Required`` body."""

    version: int
    options: tuple[PaymentOption, ...]
    error: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "x402_version": self.version,
            "error": self.error,
            "options": [o.as_dict() for o in self.options],
            "network_label": NETWORK_LABEL,
            "scope": SCOPE_NOTE,
        }


def _decode_maybe_base64(text: str) -> Any:
    """A ``PAYMENT-REQUIRED`` header value: raw JSON, or base64 of it."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    try:
        decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise PaymentError("BAD_PAYMENT_REQUIRED", "the 402 payload is neither JSON nor base64 JSON") from exc
    return json.loads(decoded)


def parse_payment_required(source: Any) -> PaymentRequired:
    """Parse a 402 into :class:`PaymentRequired`.

    Accepts the response body as a mapping, the raw/base64 ``PAYMENT-REQUIRED`` header value, or
    anything response-shaped that has ``status_code``/``headers``/``json()`` (an ``httpx.Response``).
    """
    payload: Any = source
    if hasattr(source, "status_code") and not isinstance(source, Mapping):
        status = int(getattr(source, "status_code", 0) or 0)
        if status != 402:
            raise PaymentError("NOT_PAYMENT_REQUIRED", f"expected HTTP 402, got {status}")
        headers = getattr(source, "headers", {}) or {}
        header_value = None
        for key in (PAYMENT_REQUIRED_HEADER, PAYMENT_REQUIRED_HEADER.lower(), "payment-required"):
            try:
                header_value = headers.get(key)
            except AttributeError:  # pragma: no cover - non-mapping headers
                header_value = None
            if header_value:
                break
        if header_value:
            payload = _decode_maybe_base64(str(header_value))
        else:
            try:
                payload = source.json()
            except Exception as exc:  # noqa: BLE001 - any body that is not JSON
                raise PaymentError("BAD_PAYMENT_REQUIRED", "the 402 response carried neither a PAYMENT-REQUIRED header nor a JSON body") from exc
    elif isinstance(source, (str, bytes)):
        payload = _decode_maybe_base64(source.decode("utf-8") if isinstance(source, bytes) else source)

    if not isinstance(payload, Mapping):
        raise PaymentError("BAD_PAYMENT_REQUIRED", "the 402 payload is not a JSON object")

    accepts = payload.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise PaymentError("BAD_PAYMENT_REQUIRED", "the 402 payload has no non-empty `accepts` array")

    options: list[PaymentOption] = []
    for i, entry in enumerate(accepts):
        if not isinstance(entry, Mapping):
            continue
        options.append(
            PaymentOption(
                index=i,
                scheme=str(entry.get("scheme") or "exact"),
                network=str(entry.get("network") or "").lower(),
                asset=str(entry["asset"]) if entry.get("asset") else None,
                amount_atomic=str(entry["maxAmountRequired"]) if entry.get("maxAmountRequired") is not None else None,
                pay_to=str(entry["payTo"]) if entry.get("payTo") else None,
                resource=str(entry["resource"]) if entry.get("resource") else None,
                description=str(entry.get("description") or ""),
                raw=dict(entry),
            )
        )
    if not options:
        raise PaymentError("BAD_PAYMENT_REQUIRED", "the 402 payload's `accepts` array holds no usable option")
    return PaymentRequired(
        version=int(payload.get("x402Version") or X402_VERSION),
        options=tuple(options),
        error=str(payload.get("error") or ""),
        raw=dict(payload),
    )


# --------------------------------------------------------------------------- #
# buyer                                                                         #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class X402Preview:
    payment_id: str
    options: tuple[PaymentOption, ...]
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "options": [o.as_dict() for o in self.options],
            "network_label": NETWORK_LABEL,
            "scope": SCOPE_NOTE,
        }


@dataclass(frozen=True, slots=True)
class X402Payment:
    """A signed payment: the header value to replay the original request with."""

    payment_id: str
    selected_index: int
    option: Optional[PaymentOption]
    header_name: str
    header_value: str
    raw: Mapping[str, Any] = field(default_factory=dict)
    signed_by: str = "binance-agentic-wallet"

    def headers(self) -> dict[str, str]:
        """The header(s) to attach to the retried request."""
        return {self.header_name: self.header_value}

    def as_dict(self, *, include_header_value: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "payment_id": self.payment_id,
            "selected_index": self.selected_index,
            "option": self.option.as_dict() if self.option else None,
            "header_name": self.header_name,
            "header_present": bool(self.header_value),
            "signed_by": self.signed_by,
            "network_label": NETWORK_LABEL,
            "scope": SCOPE_NOTE,
        }
        if include_header_value:
            out["header_value"] = self.header_value
        return out


def amount_from_atomic(amount_atomic: Optional[str], decimals: int) -> Optional[float]:
    if amount_atomic is None:
        return None
    try:
        return int(str(amount_atomic)) / (10 ** int(decimals))
    except (TypeError, ValueError):
        return None


class X402Buyer:
    """Pay a 402 through the Binance Agentic Wallet: preview, apply policy, sign.

    Policy is Deltr's own ceiling and is applied *before* anything is signed.  The wallet's own
    daily limits apply on top of it, and the wallet may still decline.
    """

    def __init__(
        self,
        wallet: AgenticWalletClient,
        *,
        allowed_networks: Sequence[str] = (BSC_NETWORK,),
        max_amount_units: float = DEFAULT_MAX_PAYMENT_USD,
        asset_decimals: int = BSC_USDT_DECIMALS,
    ) -> None:
        self.wallet = wallet
        self.allowed_networks = tuple(n.lower() for n in allowed_networks)
        self.max_amount_units = float(max_amount_units)
        self.asset_decimals = int(asset_decimals)

    # ---------------------------------------------------------------- preview
    async def preview(self, payment_required: Any) -> X402Preview:
        """``baw x402-payment preview`` on a parsed or raw 402.  Read-only: nothing is signed."""
        parsed = payment_required if isinstance(payment_required, PaymentRequired) else parse_payment_required(payment_required)
        data = await self.wallet.x402_preview(dict(parsed.raw))
        payment_id = data.get("paymentId") or data.get("payment_id") or data.get("id")
        if not payment_id:
            raise PaymentError(
                "X402_PREVIEW_FAILED",
                "the wallet CLI previewed the payment but returned no paymentId",
                remedy="run `baw x402-payment preview --paymentRequirements <payload> --json` by hand to see its output.",
            )
        options = _options_from_preview(data) or parsed.options
        return X402Preview(payment_id=str(payment_id), options=options, raw=dict(data))

    # ---------------------------------------------------------------- policy
    def select(self, preview: X402Preview, selected_index: Optional[int] = None) -> PaymentOption:
        """Choose which option to sign, refusing anything outside Deltr's policy.

        An explicit ``selected_index`` is honoured but still checked; without one, the first
        option that passes the network allow-list and the ceiling is used.
        """
        if not preview.options:
            raise PaymentRefused("X402_NO_OPTIONS", "the payment preview offered no options")
        if selected_index is not None:
            match = [o for o in preview.options if o.index == int(selected_index)]
            if not match:
                raise PaymentRefused(
                    "X402_BAD_INDEX",
                    f"option {selected_index} is not offered; available: {[o.index for o in preview.options]}",
                )
            self._check(match[0])
            return match[0]
        problems: list[str] = []
        for option in preview.options:
            try:
                self._check(option)
            except PaymentRefused as exc:
                problems.append(f"[{option.index}] {exc.message}")
                continue
            return option
        raise PaymentRefused(
            "X402_NO_ACCEPTABLE_OPTION",
            "no offered payment option passed Deltr's policy: " + "; ".join(problems),
            remedy=f"allowed networks {list(self.allowed_networks)}, ceiling {self.max_amount_units:g} units per payment.",
        )

    def _check(self, option: PaymentOption) -> None:
        if self.allowed_networks and option.network not in self.allowed_networks:
            raise PaymentRefused(
                "X402_NETWORK_NOT_ALLOWED",
                f"network {option.network or '<unset>'!s} is not in the allow-list {list(self.allowed_networks)}",
            )
        amount = amount_from_atomic(option.amount_atomic, self.asset_decimals)
        if amount is None:
            raise PaymentRefused("X402_AMOUNT_UNREADABLE", "the option does not state a readable maxAmountRequired")
        # A decimals mismatch is a silent cap bypass, not a rounding nuisance: 200 USDC quoted at 6
        # decimals reads as 0.0000002 units against an 18-decimal assumption, so it clears every
        # ceiling AND is charged as nothing against the run's aggregate. An atomic amount that is
        # non-zero but decodes to dust is that mismatch, and Deltr refuses rather than guesses.
        atomic = amount_from_atomic(option.amount_atomic, 0) or 0.0
        if atomic > 0 and amount < DUST_UNITS:
            raise PaymentRefused(
                "X402_DECIMALS_MISMATCH",
                f"asks {option.amount_atomic} atomic units, which is {amount:g} at the {self.asset_decimals} "
                f"decimals Deltr assumes for {NETWORK_LABEL}. That is almost certainly a different "
                "decimals count, and a payment Deltr cannot size is a payment it will not sign.",
                remedy="check the asset's decimals and set X402Buyer(asset_decimals=...) to match.",
            )
        if amount > self.max_amount_units:
            raise PaymentRefused(
                "X402_AMOUNT_OVER_CAP",
                f"asks {amount:g} units, above Deltr's {self.max_amount_units:g} per-payment ceiling",
            )

    # ---------------------------------------------------------------- sign
    async def pay(self, payment_required: Any, *, selected_index: Optional[int] = None) -> X402Payment:
        """Preview, apply policy, then ask the wallet to sign the chosen option.

        Returns the replay header to attach to the retried request.  Deltr never signs: the
        signature is produced inside Binance's wallet, which may decline.
        """
        preview = await self.preview(payment_required)
        option = self.select(preview, selected_index)
        try:
            data = await self.wallet.x402_sign(preview.payment_id, option.index)
        except AgenticWalletError as exc:
            raise PaymentError(exc.code, exc.message, remedy=exc.remedy) from exc
        header_value = (
            data.get("header")
            or data.get("paymentHeader")
            or data.get("xPayment")
            or data.get("X-PAYMENT")
            or data.get("replayHeader")
            or data.get("value")
        )
        if not header_value:
            raise PaymentError(
                "X402_SIGN_FAILED",
                "the wallet signed nothing Deltr can replay: no payment header came back",
                remedy="check `baw x402-payment sign --paymentId <id> --selectedIndex <i> --json` output.",
            )
        header_name = str(data.get("headerName") or PAYMENT_HEADER)
        return X402Payment(
            payment_id=preview.payment_id,
            selected_index=option.index,
            option=option,
            header_name=header_name,
            header_value=str(header_value),
            raw=dict(data),
        )


def _options_from_preview(data: Mapping[str, Any]) -> tuple[PaymentOption, ...]:
    """Options as the CLI's preview describes them, when it echoes them back."""
    rows = data.get("options") or data.get("accepts") or data.get("paymentOptions")
    if not isinstance(rows, list):
        return ()
    out: list[PaymentOption] = []
    for i, entry in enumerate(rows):
        if not isinstance(entry, Mapping):
            continue
        idx = entry.get("index")
        out.append(
            PaymentOption(
                index=int(idx) if isinstance(idx, int) else i,
                scheme=str(entry.get("scheme") or "exact"),
                network=str(entry.get("network") or "").lower(),
                asset=str(entry["asset"]) if entry.get("asset") else None,
                amount_atomic=str(entry["maxAmountRequired"]) if entry.get("maxAmountRequired") is not None
                else (str(entry["amount"]) if entry.get("amount") is not None else None),
                pay_to=str(entry["payTo"]) if entry.get("payTo") else None,
                resource=str(entry["resource"]) if entry.get("resource") else None,
                description=str(entry.get("description") or ""),
                raw=dict(entry),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# seller                                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PaidArtifact:
    """The thing behind the paywall: the existing edge report, sealed.

    ``sha256`` is over the canonical body (title, format, markdown, symbol, generated_at) with the
    same hash Deltr seals receipts with, so a buyer can check they got what the challenge named.
    """

    title: str
    symbol: str
    markdown: str
    generated_at: datetime
    fmt: str = "markdown"
    sha256: str = ""

    def digest_body(self) -> dict[str, Any]:
        return {
            "title": self.title, "format": self.fmt, "symbol": self.symbol,
            "markdown": self.markdown, "generated_at": self.generated_at.isoformat(),
        }

    def sealed(self) -> "PaidArtifact":
        return self if self.sha256 else PaidArtifact(
            title=self.title, symbol=self.symbol, markdown=self.markdown,
            generated_at=self.generated_at, fmt=self.fmt, sha256=sha256_of(self.digest_body()),
        )

    def as_dict(self, *, include_markdown: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "title": self.title, "format": self.fmt, "symbol": self.symbol,
            "generated_at": self.generated_at.isoformat(), "sha256": self.sealed().sha256,
            "bytes": len(self.markdown.encode("utf-8")),
        }
        if include_markdown:
            out["markdown"] = self.markdown
        return out


@dataclass(frozen=True, slots=True)
class X402Challenge:
    """A well-formed 402 a Deltr endpoint can return, plus the sealed artifact it unlocks."""

    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]
    artifact: PaidArtifact
    price_units: float
    network: str = BSC_NETWORK

    def as_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "body": dict(self.body),
            "headers": dict(self.headers),
            "artifact": self.artifact.as_dict(include_markdown=False),
            "price_units": self.price_units,
            "network": self.network,
            "network_label": NETWORK_LABEL,
            "scope": SCOPE_NOTE,
        }


@dataclass(frozen=True, slots=True)
class PresentedPayment:
    """A payment header a buyer presented.  ``verified`` is False until a facilitator says otherwise."""

    payment_id: str
    verified: bool
    artifact_sha256: str
    note: str = UNVERIFIED_NOTE
    ts: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id, "verified": self.verified,
            "artifact_sha256": self.artifact_sha256, "note": self.note,
            "ts": self.ts.isoformat(),
        }


Verifier = Callable[[str, X402Challenge], Awaitable[bool]]


class X402Seller:
    """Build the 402 challenge whose paid artifact is Deltr's edge report.

    ``pay_to`` is the address that would receive the payment.  It is supplied by the operator;
    this module never derives, stores or invents one, and the challenge is refused without it.
    """

    def __init__(
        self,
        *,
        pay_to: Optional[str] = None,
        price_units: float = 0.05,
        asset: str = BSC_USDT_ADDRESS,
        asset_decimals: int = BSC_USDT_DECIMALS,
        network: str = BSC_NETWORK,
        resource: str = "/api/x402/edge-report",
        max_timeout_seconds: int = 120,
        verifier: Optional[Verifier] = None,
    ) -> None:
        self.pay_to = pay_to
        self.price_units = float(price_units)
        self.asset = asset
        self.asset_decimals = int(asset_decimals)
        self.network = network
        self.resource = resource
        self.max_timeout_seconds = int(max_timeout_seconds)
        self.verifier = verifier

    def amount_atomic(self) -> str:
        return str(int(round(self.price_units * (10 ** self.asset_decimals))))

    def challenge(self, artifact: PaidArtifact, *, resource: Optional[str] = None) -> X402Challenge:
        """The 402 body + ``PAYMENT-REQUIRED`` header for this artifact.

        The artifact itself is **not** included: only its title, size and sha256, so a buyer knows
        exactly what they are paying for and can verify it after payment.
        """
        if not self.pay_to:
            raise PaymentError(
                "X402_NO_PAY_TO",
                "no receiving address is configured for the seller side",
                remedy="set the receiving address explicitly (DELTR_X402_PAY_TO); Deltr never invents or derives one.",
            )
        sealed = artifact.sealed()
        body = {
            "x402Version": X402_VERSION,
            "error": "payment required for the Deltr edge report",
            "accepts": [
                {
                    "scheme": "exact",
                    "network": self.network,
                    "maxAmountRequired": self.amount_atomic(),
                    "resource": resource or self.resource,
                    "description": f"{sealed.title} (sha256 {sealed.sha256[:16]}…)",
                    "mimeType": "text/markdown",
                    "payTo": self.pay_to,
                    "maxTimeoutSeconds": self.max_timeout_seconds,
                    "asset": self.asset,
                    "extra": {
                        "artifact_sha256": sealed.sha256,
                        "artifact_bytes": len(sealed.markdown.encode("utf-8")),
                        "symbol": sealed.symbol,
                        "network_label": NETWORK_LABEL,
                        "scope": SCOPE_NOTE,
                    },
                }
            ],
        }
        header = base64.b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")).decode("ascii")
        return X402Challenge(
            status_code=402,
            body=body,
            headers={PAYMENT_REQUIRED_HEADER: header, "Content-Type": "application/json"},
            artifact=sealed,
            price_units=self.price_units,
            network=self.network,
        )

    async def record_payment(self, payment_header: str, challenge: X402Challenge) -> PresentedPayment:
        """Record a presented ``X-PAYMENT`` header against a challenge.

        Without a verifier the result is ``verified=False`` and says so: Deltr does not treat an
        unverified header as settlement, and the caller must not release the artifact on it.
        """
        if not payment_header:
            raise PaymentError("X402_NO_PAYMENT_HEADER", f"the request carried no {PAYMENT_HEADER} header")
        payment_id = _payment_id_of(payment_header)
        verified = False
        if self.verifier is not None:
            verified = bool(await self.verifier(payment_header, challenge))
        return PresentedPayment(
            payment_id=payment_id,
            verified=verified,
            artifact_sha256=challenge.artifact.sha256,
            note="Verified by the configured facilitator." if verified else UNVERIFIED_NOTE,
        )


def _payment_id_of(payment_header: str) -> str:
    """The payment id inside a presented header, or a stable digest of the header when absent."""
    try:
        decoded = json.loads(base64.b64decode(payment_header, validate=True).decode("utf-8"))
        if isinstance(decoded, Mapping):
            for key in ("paymentId", "payment_id", "id", "nonce"):
                if decoded.get(key):
                    return str(decoded[key])
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        pass
    return "sha256:" + sha256_of({"header": payment_header})[:32]


def artifact_from_edge_report(
    report: Mapping[str, Any],
    *,
    symbol: str = "",
    generated_at: Optional[datetime] = None,
) -> PaidArtifact:
    """Wrap the dict ``deltr_edge_report`` already returns as the sealed paid artifact."""
    markdown = str(report.get("markdown") or "")
    if not markdown.strip():
        raise PaymentError("X402_EMPTY_ARTIFACT", "the edge report is empty; there is nothing to sell")
    return PaidArtifact(
        title=str(report.get("title") or "Deltr edge report"),
        symbol=symbol or str(report.get("symbol") or ""),
        markdown=markdown,
        generated_at=generated_at or utcnow(),
    ).sealed()


__all__ = [
    "BSC_CHAIN_ID",
    "BSC_NETWORK",
    "BSC_USDT_ADDRESS",
    "BSC_USDT_DECIMALS",
    "DEFAULT_MAX_PAYMENT_USD",
    "NETWORK_LABEL",
    "PAYMENT_HEADER",
    "PAYMENT_REQUIRED_HEADER",
    "SCOPE_NOTE",
    "UNVERIFIED_NOTE",
    "X402_VERSION",
    "PaidArtifact",
    "PaymentError",
    "PaymentOption",
    "PaymentRefused",
    "PaymentRequired",
    "PresentedPayment",
    "X402Buyer",
    "X402Challenge",
    "X402Payment",
    "X402Preview",
    "X402Seller",
    "amount_from_atomic",
    "artifact_from_edge_report",
    "parse_payment_required",
]
