"""deltr/venues/agentic_wallet.py — the on-chain leg, delegated to the Binance Agentic Wallet CLI.

CUSTODY MODEL (the central safety property of this venue)
--------------------------------------------------------
Deltr never holds, reads, stores or signs with a private key.  There is no signer
in this module and no code path that accepts one.  Deltr *requests* an on-chain
action by shelling out to the Binance Agentic Wallet CLI (``baw``); Binance's own
wallet custodies the key, applies its own daily spending limits, decides whether
to sign, and broadcasts.  Deltr only reads the JSON that comes back.  Every limit
Deltr enforces is therefore an *additional* ceiling on top of the wallet's own —
never a replacement for it.

WHAT THIS WRAPS (taken from ``baw <cmd> --help``, CLI version 1.9.0)
-------------------------------------------------------------------
Read-only:
    baw wallet status --json
    baw wallet address --json
    baw wallet balance [--symbol S] [--tokenAddress 0x..] [--binanceChainId N] --json
    baw wallet left-quota --json
    baw wallet chains --json
    baw wallet tx-history [--tx 0x..] [--type all|pending|confirmed]
                          [--binanceChainId N] [--size N] [--nextCursor C]
                          [--startTime MS] [--endTime MS] --json
    baw approvals list [--spender 0x..] [--filterTypes T] [--limit N] [--offset C] --json
    baw market-order quote --binanceChainId N --fromTokenQty Q --fromToken 0x..
                           --toToken 0x.. [--slippage auto|0-100] --json
    baw market-order list [--orderId ID] [--binanceChainId N] [--status PENDING|FINISHED|FAILED]
                          [--fromToken 0x..] [--toToken 0x..] [--page N] [--pageSize N] --json
Value-moving:
    baw market-order swap --binanceChainId N --fromTokenQty Q --fromToken 0x..
                          --toToken 0x.. [--slippage auto|0-100] [--mev true|false]
                          [--gasLevel LOW|MEDIUM|HIGH] --json

``market-order`` has no separate ``preview``/``execute`` pair, so :meth:`AgenticWalletClient.swap`
implements the preview-then-execute discipline itself: ``market-order quote`` is the preview, the
caller's own bounds are checked against that preview, and only then is ``market-order swap`` run —
followed by ``market-order list --orderId`` to *confirm* the result.  Nothing is reported as filled
that the CLI did not confirm.

CLI JSON CONTRACT (observed on 1.9.0)
-------------------------------------
    success: {"success": true,  "data": {...}}
    failure: {"success": false, "error": {"code": 10003000, "name": "NOT_LOGGED_IN",
                                          "message": "Not logged in"}}
Argument errors are printed by commander on stderr with a non-zero exit and no JSON at all.

Never prints to stdout (the stdio MCP transport owns it); diagnostics go to stderr via logging.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from deltr.models import DataSource, Fill, Side, Venue, utcnow

log = logging.getLogger("deltr.venues.agentic_wallet")

DEFAULT_BIN = "baw"
DEFAULT_TIMEOUT_S = 25.0
DEFAULT_SWAP_TIMEOUT_S = 150.0
DEFAULT_CONFIRM_TIMEOUT_S = 180.0
CONFIRM_POLL_S = 3.0
BSC_CHAIN_ID = 56

#: The CLI's own error names that mean "the operator has not signed in yet".
NOT_SIGNED_IN_NAMES = frozenset({"NOT_LOGGED_IN", "UNAUTHORIZED", "SESSION_EXPIRED", "TOKEN_EXPIRED"})
#: ``wallet status --json`` -> ``data.status`` values that mean a usable session.
CONNECTED_STATES = frozenset({"CONNECTED", "ACTIVE", "READY", "OK"})

SIGNIN_REMEDY = (
    "run `baw auth signin --json`, scan the QR code in the Binance Wallet app, then "
    "`baw auth verify --qrCodeId <id> --json`. Deltr never receives, stores or signs with the key: "
    "the wallet keeps custody and does the signing."
)


# --------------------------------------------------------------------------- #
# argv hygiene: a secret must never reach a command line                       #
# --------------------------------------------------------------------------- #
#: flag names that carry a credential.  ``--tokenAddress`` / ``--fromToken`` and friends carry a
#: *contract address*, which is public, so they are allow-listed below.
_SECRET_FLAG_RE = re.compile(r"(secret|passphrase|password|mnemonic|seed|privkey|private[-_]?key|apikey|api[-_]key|credential|bearer|auth[-_]?token)", re.I)
_TOKENISH_FLAG_RE = re.compile(r"(token|key)", re.I)
_SAFE_FLAGS = frozenset(
    {
        "--tokenaddress", "--fromtoken", "--totoken", "--tokencontract",
        "--fromtokenqty", "--paymentid", "--qrcodeid", "--orderid",
    }
)
#: flags whose VALUE is public chain data that can legitimately look like 32 bytes of hex — a
#: transaction hash is the same shape as a private key, so the shape test is scoped to values
#: Deltr did not pass as one of these known-public identifiers.
_PUBLIC_VALUE_FLAGS = frozenset(
    {
        "--tx", "--spender", "--tokenaddress", "--fromtoken", "--totoken", "--tokencontract",
        "--nextcursor", "--offset", "--paymentrequirements", "--paymentid", "--qrcodeid", "--orderid",
    }
)
#: value shapes that look like key material: a raw 32-byte hex secret, with or without ``0x``.
_HEX64_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_MNEMONIC_RE = re.compile(r"^(?:[a-z]{3,10}\s+){11,}[a-z]{3,10}$")
#: shortest ``forbidden`` value worth substring-matching.  Binance keys and secrets are 64 chars;
#: below this a "secret" is a placeholder and matching it only produces false refusals.
MIN_FORBIDDEN_LEN = 8


def assert_no_secret_in_argv(argv: Sequence[str], *, forbidden: Sequence[str] = ()) -> None:
    """Raise :class:`AgenticWalletError` if ``argv`` carries anything key-shaped.

    Three rules, all structural (no value is ever logged):

    1. a flag whose name reads as a credential (``--secret``, ``--apiKey``, ``--privateKey`` …)
       is refused outright, allow-listing the contract-address flags the CLI really has;
    2. a *value* shaped like key material (32-byte hex, with or without ``0x``, or a BIP-39
       word list) is refused;
    3. any caller-supplied ``forbidden`` substring (e.g. the configured Binance API secret)
       appearing anywhere in the vector is refused.
    """
    previous_flag = ""
    for i, raw in enumerate(argv):
        arg = str(raw)
        flag_for_value = previous_flag
        if arg.startswith("-"):
            name = arg.split("=", 1)[0].lower()
            if name not in _SAFE_FLAGS and (_SECRET_FLAG_RE.search(name) or _TOKENISH_FLAG_RE.search(name)):
                raise AgenticWalletError(
                    "SECRET_IN_ARGV",
                    f"refusing to run the wallet CLI: argument {i} names a credential ({name}). "
                    "Deltr never passes a secret on a command line.",
                )
            flag_for_value = name
            previous_flag = name
        else:
            previous_flag = ""
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        public_value = flag_for_value in _PUBLIC_VALUE_FLAGS
        if not public_value and (_HEX64_RE.match(value.strip()) or _MNEMONIC_RE.match(value.strip().lower())):
            raise AgenticWalletError(
                "SECRET_IN_ARGV",
                f"refusing to run the wallet CLI: argument {i} is shaped like key material. "
                "Deltr never passes a secret on a command line.",
            )
        for needle in forbidden:
            # A real Binance key or secret is 64 characters.  Substring-matching anything shorter
            # than MIN_FORBIDDEN_LEN is false-positive machinery, not a safety check: a
            # placeholder such as BINANCE_SECRET_KEY=s matches the "s" in "wallet status" and
            # refuses EVERY wallet command, including the read-only ones, with a message that
            # says a secret was found when none was.  Short values are skipped so the check keeps
            # meaning what it says.
            if needle and len(needle) >= MIN_FORBIDDEN_LEN and needle in arg:
                raise AgenticWalletError(
                    "SECRET_IN_ARGV",
                    f"refusing to run the wallet CLI: argument {i} contains a configured secret value "
                    "(a Binance credential must never reach a command line).",
                )


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Argv as it is safe to log or return.  Long opaque blobs (an x402 payload) are elided."""
    out: list[str] = []
    for arg in argv:
        a = str(arg)
        out.append(a if len(a) <= 64 else f"{a[:24]}…<{len(a)} chars>")
    return out


# --------------------------------------------------------------------------- #
# errors                                                                        #
# --------------------------------------------------------------------------- #
class AgenticWalletError(RuntimeError):
    """Every failure of the wallet leg, carrying the CLI's own message unchanged.

    ``code`` is one of the wrapper codes (``BAW_NOT_INSTALLED``, ``BAW_NOT_SIGNED_IN``,
    ``BAW_TIMEOUT``, ``BAW_BAD_JSON``, ``BAW_CLI_ERROR``, ``SECRET_IN_ARGV``,
    ``SWAP_PREVIEW_REJECTED``, ``SWAP_UNCONFIRMED``) or, when the CLI named its own error,
    that name (``NOT_LOGGED_IN``, ``INSUFFICIENT_BALANCE``, …).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        cli_code: Optional[int] = None,
        exit_code: Optional[int] = None,
        command: Sequence[str] = (),
        remedy: Optional[str] = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.cli_code = cli_code
        self.exit_code = exit_code
        self.command = tuple(redact_argv(command))
        self.remedy = remedy
        self.stderr = stderr[:400]

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.cli_code is not None:
            out["cli_code"] = self.cli_code
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.command:
            out["command"] = list(self.command)
        if self.remedy:
            out["remedy"] = self.remedy
        return out

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message} ({self.code})" + (f" — {self.remedy}" if self.remedy else "")


# --------------------------------------------------------------------------- #
# value objects                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CliResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class AuthStatus:
    signed_in: bool
    status: str
    address: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"signed_in": self.signed_in, "status": self.status, "address": self.address}


@dataclass(frozen=True, slots=True)
class SwapQuote:
    """``market-order quote`` — the preview leg of the preview-then-execute flow."""

    chain_id: int
    from_token: str
    to_token: str
    from_amount: float
    to_amount: float           # expected receive, CLI units
    min_receive: Optional[float]
    price_impact_pct: Optional[float]
    slippage: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id, "from_token": self.from_token, "to_token": self.to_token,
            "from_amount": self.from_amount, "to_amount": self.to_amount,
            "min_receive": self.min_receive, "price_impact_pct": self.price_impact_pct,
            "slippage": self.slippage,
        }


@dataclass(frozen=True, slots=True)
class SwapResult:
    """The outcome of a swap **as the CLI reported it**.

    ``confirmed`` is True only when the CLI itself said the order finished and gave a
    transaction hash.  A pending or unknown order is never presented as a fill.
    """

    confirmed: bool
    status: str
    order_id: Optional[str]
    tx_hash: Optional[str]
    chain_id: int
    from_token: str
    to_token: str
    from_amount: float
    received_amount: Optional[float]
    quote: Optional[SwapQuote] = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    custody_note: str = (
        "Requested through the Binance Agentic Wallet CLI. Binance's wallet holds the key, "
        "applies its own daily limits and performs the signing; Deltr holds no key and signs nothing."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed, "status": self.status, "order_id": self.order_id,
            "tx_hash": self.tx_hash, "chain_id": self.chain_id,
            "from_token": self.from_token, "to_token": self.to_token,
            "from_amount": self.from_amount, "received_amount": self.received_amount,
            "quote": self.quote.as_dict() if self.quote else None,
            "custody": self.custody_note,
        }


# --------------------------------------------------------------------------- #
# JSON helpers                                                                  #
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    """A float from a CLI field that may be a number, a decimal string, or absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def fmt_amount(value: float) -> str:
    """A plain decimal string for ``--fromTokenQty`` — never scientific notation, no trailing zeros."""
    d = Decimal(repr(float(value))).normalize()
    text = format(d, "f")
    return text or "0"


def _first(data: Mapping[str, Any], *names: str) -> Any:
    """First present, non-null value among ``names`` (the CLI spells amounts several ways)."""
    for n in names:
        if isinstance(data, Mapping) and data.get(n) is not None:
            return data[n]
    return None


def _rows(data: Any) -> list[dict[str, Any]]:
    """The list inside a CLI payload, whichever key it hides behind."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, Mapping):
        for key in ("list", "items", "records", "data", "orders", "transactions", "approvals", "balances"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
    return []


# --------------------------------------------------------------------------- #
# the default runner: asyncio subprocess, always timed out                      #
# --------------------------------------------------------------------------- #
CliRunner = Callable[[Sequence[str], float], Awaitable[CliResult]]


async def subprocess_runner(argv: Sequence[str], timeout_s: float) -> CliResult:
    """Run ``argv`` with no shell, a clean stdin and a hard timeout.

    The child is killed and reaped on timeout so a hung CLI can never leak a process.
    """
    t0 = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AgenticWalletError(
            "BAW_NOT_INSTALLED",
            f"the Binance Agentic Wallet CLI was not found at {argv[0]!r}",
            command=argv,
            remedy="install the Binance Agentic Wallet CLI so that `baw` is on PATH, or set DELTR_BAW_BIN to its absolute path.",
        ) from exc
    except OSError as exc:  # pragma: no cover - platform specific
        raise AgenticWalletError("BAW_NOT_INSTALLED", f"cannot start {argv[0]!r}: {exc}", command=argv) from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
        try:
            await proc.wait()
        except Exception:  # pragma: no cover - defensive reap
            pass
        raise AgenticWalletError(
            "BAW_TIMEOUT",
            f"the wallet CLI did not answer within {timeout_s:g}s",
            command=argv,
            remedy="retry, or run the same command by hand to see where it blocks. No transaction is assumed either way.",
        ) from exc
    return CliResult(
        argv=tuple(str(a) for a in argv),
        returncode=int(proc.returncode or 0),
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )


# --------------------------------------------------------------------------- #
# the client                                                                    #
# --------------------------------------------------------------------------- #
class AgenticWalletClient:
    """Async wrapper around ``baw``.  Every call passes ``--json`` and parses the result.

    ``runner`` is injectable so tests never invoke the real CLI: it is any
    ``async (argv, timeout_s) -> CliResult``.
    """

    def __init__(
        self,
        *,
        binary: str = DEFAULT_BIN,
        chain_id: int = BSC_CHAIN_ID,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        swap_timeout_s: float = DEFAULT_SWAP_TIMEOUT_S,
        confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
        confirm_poll_s: float = CONFIRM_POLL_S,
        runner: Optional[CliRunner] = None,
        forbidden_values: Sequence[str] = (),
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self.binary = binary or DEFAULT_BIN
        self.chain_id = int(chain_id)
        self.timeout_s = float(timeout_s)
        self.swap_timeout_s = float(swap_timeout_s)
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.confirm_poll_s = float(confirm_poll_s)
        self._runner: CliRunner = runner or subprocess_runner
        self._forbidden = tuple(v for v in forbidden_values if v)
        self._sleep = sleep or asyncio.sleep
        self.calls: list[list[str]] = []  # redacted argv of every invocation, for the trace

    # ------------------------------------------------------------------ plumbing
    def _argv(self, *args: str) -> list[str]:
        return [self.binary, *[str(a) for a in args], "--json"]

    async def _run(self, *args: str, timeout_s: Optional[float] = None) -> Any:
        """Run one CLI command and return its ``data`` payload, or raise :class:`AgenticWalletError`."""
        argv = self._argv(*args)
        assert_no_secret_in_argv(argv, forbidden=self._forbidden)
        self.calls.append(redact_argv(argv))
        res = await self._runner(argv, float(timeout_s if timeout_s is not None else self.timeout_s))
        return self._parse(res)

    def _parse(self, res: CliResult) -> Any:
        text = (res.stdout or "").strip()
        payload: Any = None
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None

        if isinstance(payload, Mapping) and payload.get("success") is False:
            err = payload.get("error")
            err = err if isinstance(err, Mapping) else {}
            name = str(err.get("name") or err.get("code") or "BAW_CLI_ERROR")
            message = str(err.get("message") or "the wallet CLI reported an error")
            cli_code = err.get("code") if isinstance(err.get("code"), int) else None
            if name.upper() in NOT_SIGNED_IN_NAMES:
                raise AgenticWalletError(
                    "BAW_NOT_SIGNED_IN", message, cli_code=cli_code, exit_code=res.returncode,
                    command=res.argv, remedy=SIGNIN_REMEDY,
                )
            raise AgenticWalletError(name, message, cli_code=cli_code, exit_code=res.returncode, command=res.argv)

        if res.returncode != 0:
            detail = (res.stderr or text or "").strip()
            raise AgenticWalletError(
                "BAW_CLI_ERROR",
                f"the wallet CLI exited {res.returncode}" + (f": {detail.splitlines()[0][:200]}" if detail else ""),
                exit_code=res.returncode, command=res.argv, stderr=res.stderr,
                remedy="run the same command by hand with --json to see the CLI's own message.",
            )

        if payload is None:
            raise AgenticWalletError(
                "BAW_BAD_JSON",
                "the wallet CLI returned output that is not JSON" + (f": {text[:120]}" if text else " (empty)"),
                exit_code=res.returncode, command=res.argv, stderr=res.stderr,
                remedy="check the CLI version: Deltr was built against baw 1.9.0, where every command accepts --json.",
            )
        if isinstance(payload, Mapping) and "data" in payload:
            return payload["data"]
        return payload

    # ------------------------------------------------------------------ availability
    def resolved_binary(self) -> Optional[str]:
        """Absolute path of the CLI, or None when it is not installed."""
        if os.path.sep in self.binary:
            return self.binary if os.path.isfile(self.binary) and os.access(self.binary, os.X_OK) else None
        return shutil.which(self.binary)

    def is_available(self) -> bool:
        """True when the CLI binary exists and is executable.  No subprocess is started."""
        return self.resolved_binary() is not None

    def require_available(self) -> None:
        if not self.is_available():
            raise AgenticWalletError(
                "BAW_NOT_INSTALLED",
                f"the Binance Agentic Wallet CLI ({self.binary!r}) is not installed or not on PATH",
                remedy="install the Binance Agentic Wallet CLI so that `baw` is on PATH, or set DELTR_BAW_BIN to its absolute path. "
                       "Deltr does not fall back to a simulated on-chain leg.",
            )

    async def version(self) -> str:
        """``baw --version``.  Returns the raw string; never raises for a parse failure."""
        self.require_available()
        argv = [self.binary, "--version"]
        self.calls.append(redact_argv(argv))
        res = await self._runner(argv, self.timeout_s)
        if res.returncode != 0:
            raise AgenticWalletError(
                "BAW_CLI_ERROR", f"`{self.binary} --version` exited {res.returncode}",
                exit_code=res.returncode, command=argv, stderr=res.stderr,
            )
        return (res.stdout or "").strip()

    # ------------------------------------------------------------------ auth / read-only
    async def auth_status(self) -> AuthStatus:
        """``baw wallet status --json`` — is there a usable wallet session?

        A signed-out wallet is reported, not raised: callers decide whether that is fatal.
        """
        self.require_available()
        try:
            data = await self._run("wallet", "status")
        except AgenticWalletError as exc:
            if exc.code == "BAW_NOT_SIGNED_IN":
                return AuthStatus(signed_in=False, status="UNCONNECTED", raw={"error": exc.as_dict()})
            raise
        data = data if isinstance(data, Mapping) else {}
        status = str(_first(data, "status", "state", "walletStatus") or "UNKNOWN").upper()
        address = _first(data, "address", "walletAddress", "evmAddress")
        return AuthStatus(
            signed_in=status in CONNECTED_STATES,
            status=status,
            address=str(address) if address else None,
            raw=dict(data),
        )

    async def require_signed_in(self) -> AuthStatus:
        st = await self.auth_status()
        if not st.signed_in:
            raise AgenticWalletError(
                "BAW_NOT_SIGNED_IN",
                f"the Binance Agentic Wallet is not signed in (status {st.status})",
                remedy=SIGNIN_REMEDY,
            )
        return st

    async def wallet_address(self) -> dict[str, str]:
        """``baw wallet address --json`` -> ``{chain: address}`` (chain keys as the CLI spells them)."""
        self.require_available()
        data = await self._run("wallet", "address")
        out: dict[str, str] = {}
        if isinstance(data, Mapping):
            for key, value in data.items():
                if isinstance(value, str) and value.startswith("0x"):
                    out[str(key)] = value
            if not out:
                for row in _rows(data):
                    addr = _first(row, "address", "walletAddress")
                    chain = _first(row, "chain", "chainName", "binanceChainId", "chainId")
                    if addr:
                        out[str(chain or "default")] = str(addr)
        elif isinstance(data, str):
            out["default"] = data
        return out

    async def balances(self, *, symbol: Optional[str] = None, chain_id: Optional[int] = None) -> list[dict[str, Any]]:
        """``baw wallet balance --json`` (non-zero balances only, per the CLI's own docs)."""
        self.require_available()
        args = ["wallet", "balance"]
        if symbol:
            args += ["--symbol", str(symbol)]
        if chain_id is not None:
            args += ["--binanceChainId", str(int(chain_id))]
        return _rows(await self._run(*args))

    async def left_quota(self) -> dict[str, Any]:
        """``baw wallet left-quota --json`` — the WALLET's own remaining daily trading limit.

        This is Binance's ceiling, enforced by Binance.  Deltr's caps sit on top of it.
        """
        self.require_available()
        data = await self._run("wallet", "left-quota")
        return dict(data) if isinstance(data, Mapping) else {"raw": data}

    async def chains(self) -> list[dict[str, Any]]:
        self.require_available()
        return _rows(await self._run("wallet", "chains"))

    async def approvals_list(
        self,
        *,
        spender: Optional[str] = None,
        filter_types: Optional[str] = None,
        limit: int = 20,
        offset: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``baw approvals list --json`` — standing token approvals, the main on-chain exposure."""
        self.require_available()
        args = ["approvals", "list", "--limit", str(int(limit))]
        if spender:
            args += ["--spender", str(spender)]
        if filter_types:
            args += ["--filterTypes", str(filter_types)]
        if offset:
            args += ["--offset", str(offset)]
        return _rows(await self._run(*args))

    async def tx_status(self, tx_hash: str) -> dict[str, Any]:
        """``baw wallet tx-history --tx <hash> --json`` — one transaction as the chain reports it."""
        self.require_available()
        if not tx_hash:
            raise AgenticWalletError("INVALID_ARGUMENT", "tx_status needs a transaction hash")
        data = await self._run("wallet", "tx-history", "--tx", str(tx_hash))
        rows = _rows(data)
        if rows:
            return rows[0]
        return dict(data) if isinstance(data, Mapping) else {"raw": data}

    # ------------------------------------------------------------------ quote (the preview)
    async def quote(
        self,
        from_token: str,
        to_token: str,
        amount: float,
        *,
        chain_id: Optional[int] = None,
        slippage: str = "auto",
    ) -> SwapQuote:
        """``baw market-order quote --json`` — the preview half of preview-then-execute.

        Read-only: nothing is signed and nothing moves.
        """
        self.require_available()
        chain = int(chain_id if chain_id is not None else self.chain_id)
        qty = float(amount)
        if not (qty > 0):
            raise AgenticWalletError("INVALID_ARGUMENT", f"swap amount must be positive, got {amount!r}")
        data = await self._run(
            "market-order", "quote",
            "--binanceChainId", str(chain),
            "--fromTokenQty", fmt_amount(qty),
            "--fromToken", str(from_token),
            "--toToken", str(to_token),
            "--slippage", str(slippage),
        )
        d = data if isinstance(data, Mapping) else {}
        return SwapQuote(
            chain_id=chain,
            from_token=str(from_token),
            to_token=str(to_token),
            from_amount=qty,
            to_amount=_num(_first(d, "toTokenAmount", "toAmount", "receiveAmount", "outAmount", "amountOut")) or 0.0,
            min_receive=_num(_first(d, "minReceiveAmount", "minimumReceived", "minReceive", "minAmountOut")),
            price_impact_pct=_num(_first(d, "priceImpact", "priceImpactPercentage", "priceImpactPct", "impact")),
            slippage=str(slippage),
            raw=dict(d),
        )

    # ------------------------------------------------------------------ swap (preview then execute)
    async def swap(
        self,
        from_token: str,
        to_token: str,
        amount: float,
        *,
        chain_id: Optional[int] = None,
        slippage: str = "auto",
        mev: bool = True,
        gas_level: str = "HIGH",
        min_receive: Optional[float] = None,
        max_price_impact_pct: Optional[float] = None,
        wait_for_confirmation: bool = True,
    ) -> SwapResult:
        """Request an on-chain swap: preview, check the caller's bounds, execute, then confirm.

        Custody: Deltr only *asks*.  Binance's wallet decides whether to sign, applies its own
        daily limits on top of Deltr's caps, and broadcasts.  If it declines, the CLI's own
        message comes back unchanged in :class:`AgenticWalletError`.

        ``min_receive`` and ``max_price_impact_pct`` are checked against the **preview** and, when
        breached, the swap is refused before anything is signed (``SWAP_PREVIEW_REJECTED``).

        The returned :class:`SwapResult` is ``confirmed`` only when the CLI itself reported a
        finished order with a transaction hash.  A pending order comes back ``confirmed=False``
        with its ``order_id``: Deltr never claims a fill the CLI did not confirm.
        """
        self.require_available()
        chain = int(chain_id if chain_id is not None else self.chain_id)

        # ---- 1. preview -------------------------------------------------------------
        preview = await self.quote(from_token, to_token, amount, chain_id=chain, slippage=slippage)
        if min_receive is not None and preview.to_amount < float(min_receive):
            raise AgenticWalletError(
                "SWAP_PREVIEW_REJECTED",
                f"preview would receive {preview.to_amount:g}, below the required minimum {float(min_receive):g}; "
                "nothing was submitted and nothing was signed.",
            )
        if min_receive is not None and preview.min_receive is not None and preview.min_receive < float(min_receive):
            # ``to_amount`` is the EXPECTED output; ``min_receive`` is the worst case the CLI will
            # actually accept on chain, and it is the only place the ``--slippage`` value Deltr
            # passed comes back as a number.  If the CLI read "0.5" as 50% rather than 0.5%, this
            # is where it shows, and a swap that could legally settle far below the quote is
            # refused instead of signed.
            raise AgenticWalletError(
                "SWAP_PREVIEW_REJECTED",
                f"the wallet would accept as little as {preview.min_receive:g}, below the required minimum "
                f"{float(min_receive):g} (the slippage bound it applied is wider than Deltr asked for); "
                "nothing was submitted and nothing was signed.",
            )
        if max_price_impact_pct is not None and preview.price_impact_pct is not None:
            if abs(preview.price_impact_pct) > float(max_price_impact_pct):
                raise AgenticWalletError(
                    "SWAP_PREVIEW_REJECTED",
                    f"preview price impact {preview.price_impact_pct:g}% exceeds the {float(max_price_impact_pct):g}% bound; "
                    "nothing was submitted and nothing was signed.",
                )

        # ---- 2. execute --------------------------------------------------------------
        qty = float(amount)
        data = await self._run(
            "market-order", "swap",
            "--binanceChainId", str(chain),
            "--fromTokenQty", fmt_amount(qty),
            "--fromToken", str(from_token),
            "--toToken", str(to_token),
            "--slippage", str(slippage),
            "--mev", "true" if mev else "false",
            "--gasLevel", str(gas_level).upper(),
            timeout_s=self.swap_timeout_s,
        )
        d = data if isinstance(data, Mapping) else {}
        order_id = _first(d, "orderId", "order_id", "id")
        order_id = str(order_id) if order_id is not None else None
        result = self._swap_result(d, preview=preview, chain=chain, from_token=from_token, to_token=to_token, qty=qty, order_id=order_id)

        # ---- 3. confirm ----------------------------------------------------------------
        if result.confirmed or not wait_for_confirmation or not order_id:
            return result
        return await self.await_order(order_id, preview=preview, chain=chain, from_token=from_token, to_token=to_token, qty=qty)

    def _swap_result(
        self,
        d: Mapping[str, Any],
        *,
        preview: Optional[SwapQuote],
        chain: int,
        from_token: str,
        to_token: str,
        qty: float,
        order_id: Optional[str],
    ) -> SwapResult:
        status = str(_first(d, "status", "orderStatus", "state") or "PENDING").upper()
        tx_hash = _first(d, "txHash", "transactionHash", "hash", "tx")
        tx_hash = str(tx_hash) if tx_hash else None
        received = _num(_first(d, "toTokenAmount", "receivedAmount", "actualToAmount", "toAmount", "filledAmount"))
        confirmed = status in {"FINISHED", "SUCCESS", "CONFIRMED", "FILLED"} and bool(tx_hash)
        if status in {"FAILED", "CANCELLED", "REJECTED"}:
            raise AgenticWalletError(
                str(_first(d, "failReason", "errorName") or "SWAP_FAILED"),
                str(_first(d, "failMessage", "message", "reason") or f"the wallet reported the swap {status.lower()}"),
                command=(),
            )
        return SwapResult(
            confirmed=confirmed,
            status=status,
            order_id=order_id,
            tx_hash=tx_hash,
            chain_id=chain,
            from_token=str(from_token),
            to_token=str(to_token),
            from_amount=qty,
            received_amount=received,
            quote=preview,
            raw=dict(d),
        )

    async def order(self, order_id: str) -> dict[str, Any]:
        """``baw market-order list --orderId <id> --json`` — one order as the CLI reports it."""
        self.require_available()
        data = await self._run("market-order", "list", "--orderId", str(order_id))
        rows = _rows(data)
        if rows:
            return rows[0]
        return dict(data) if isinstance(data, Mapping) else {"raw": data}

    async def await_order(
        self,
        order_id: str,
        *,
        preview: Optional[SwapQuote] = None,
        chain: Optional[int] = None,
        from_token: str = "",
        to_token: str = "",
        qty: float = 0.0,
    ) -> SwapResult:
        """Poll ``market-order list --orderId`` until the CLI calls the order finished.

        On timeout the order is returned **unconfirmed** rather than assumed filled; the caller
        gets the ``order_id`` so an operator can look it up.
        """
        deadline = time.monotonic() + self.confirm_timeout_s
        last: SwapResult | None = None
        while True:
            row = await self.order(order_id)
            last = self._swap_result(
                row, preview=preview, chain=int(chain if chain is not None else self.chain_id),
                from_token=from_token, to_token=to_token, qty=qty, order_id=order_id,
            )
            if last.confirmed:
                return last
            if time.monotonic() >= deadline:
                return last
            await self._sleep(self.confirm_poll_s)

    # ------------------------------------------------------------------ x402 (B402)
    async def x402_preview(self, payment_required: Mapping[str, Any] | str) -> dict[str, Any]:
        """``baw x402-payment preview --paymentRequirements <b64> --json``.

        The payload is base64-encoded (a form the CLI documents) so a large 402 body never
        has to survive argv quoting.  A 402 challenge is public data, not a credential.
        """
        self.require_available()
        return dict_or_raw(await self._run("x402-payment", "preview", "--paymentRequirements", encode_payment_requirements(payment_required)))

    async def x402_sign(self, payment_id: str, selected_index: int) -> dict[str, Any]:
        """``baw x402-payment sign --paymentId <id> --selectedIndex <i> --json``.

        The wallet signs; Deltr receives only the replay header value to put on the retry.
        """
        self.require_available()
        if not payment_id:
            raise AgenticWalletError("INVALID_ARGUMENT", "x402 sign needs the paymentId returned by preview")
        return dict_or_raw(
            await self._run("x402-payment", "sign", "--paymentId", str(payment_id), "--selectedIndex", str(int(selected_index)))
        )


def dict_or_raw(data: Any) -> dict[str, Any]:
    return dict(data) if isinstance(data, Mapping) else {"raw": data}


def encode_payment_requirements(payment_required: Mapping[str, Any] | str) -> str:
    """The ``--paymentRequirements`` value: base64 of the challenge JSON.

    A ``str`` that already looks like base64 is passed through unchanged (it is what the
    ``PAYMENT-REQUIRED`` header carries); anything else is serialised then encoded.
    """
    if isinstance(payment_required, str):
        text = payment_required.strip()
        if text.startswith("{"):
            return base64.b64encode(text.encode("utf-8")).decode("ascii")
        return text
    return base64.b64encode(json.dumps(payment_required, separators=(",", ":"), sort_keys=True).encode("utf-8")).decode("ascii")


# --------------------------------------------------------------------------- #
# Fill adapter                                                                  #
# --------------------------------------------------------------------------- #
def fill_from_swap(
    result: SwapResult,
    *,
    leg_index: int,
    symbol: str,
    side: Side,
    fee_usd: float = 0.0,
    venue: Venue = Venue.BINANCE_AGENTIC_WALLET,
    latency_ms: int = 0,
) -> Fill:
    """Turn a **confirmed** swap into a :class:`~deltr.models.Fill`.

    Refuses an unconfirmed result: a fill Deltr cannot point at a transaction hash for is not a
    fill.  ``price`` is the executed rate derived from the amounts the CLI actually returned,
    ``ref`` is the transaction hash, ``simulated`` is False and ``source`` is
    ``binance-agentic-wallet`` so the provenance is visible everywhere the Fill is shown.
    """
    if not result.confirmed or not result.tx_hash:
        raise AgenticWalletError(
            "SWAP_UNCONFIRMED",
            f"the wallet CLI has not confirmed order {result.order_id or '<unknown>'} (status {result.status}); "
            "Deltr does not record an unconfirmed swap as a fill.",
            remedy="check `baw market-order list --orderId <id> --json` or `baw wallet tx-history --tx <hash> --json`.",
        )
    received = result.received_amount
    if received is None or not (received > 0) or not (result.from_amount > 0):
        raise AgenticWalletError(
            "SWAP_UNCONFIRMED",
            f"order {result.order_id or '<unknown>'} is confirmed but the CLI reported no received amount; "
            "Deltr does not invent one.",
        )
    # Buying base with quote: price = quote spent / base received.  Selling base for quote:
    # price = quote received / base sold.  Either way the executed rate uses only reported amounts.
    price = (result.from_amount / received) if side == Side.BUY else (received / result.from_amount)
    qty = received if side == Side.BUY else result.from_amount
    return Fill(
        leg_index=leg_index,
        venue=venue,
        symbol=symbol,
        side=side,
        qty=float(qty),
        price=float(price),
        fee_usd=float(fee_usd),
        ref=str(result.tx_hash),
        simulated=False,
        source=DataSource.BINANCE_AGENTIC_WALLET,
        latency_ms=int(latency_ms),
        ts=utcnow(),
    )


__all__ = [
    "AgenticWalletClient",
    "AgenticWalletError",
    "AuthStatus",
    "CliResult",
    "CliRunner",
    "SwapQuote",
    "SwapResult",
    "assert_no_secret_in_argv",
    "encode_payment_requirements",
    "fmt_amount",
    "fill_from_swap",
    "redact_argv",
    "subprocess_runner",
    "BSC_CHAIN_ID",
    "SIGNIN_REMEDY",
]
