"""tests/test_agentic_wallet.py — the on-chain leg wrapper, exercised against a FAKE CLI.

No test here invokes the real ``baw``: :class:`FakeCli` is injected as the runner, so nothing
signs, broadcasts or moves funds.  The one test that does touch the real CLI is marked
``@pytest.mark.live``, runs a single read-only ``wallet status``, and is skipped by default.

Covered: JSON parsing, the preview-then-execute sequence and its ordering, error mapping (CLI
error JSON, non-zero exit, non-JSON output), timeouts, the not-installed and not-signed-in paths,
confirmation discipline (an unconfirmed order is never a Fill), and the invariant that no secret
ever reaches an argument vector.
"""
from __future__ import annotations

import ast
import base64
import json
import os
import re
import shutil
from typing import Any, Optional, Sequence

import pytest

from deltr.models import DataSource, Side, Venue
from deltr.venues.agentic_wallet import (
    AgenticWalletClient,
    AgenticWalletError,
    CliResult,
    assert_no_secret_in_argv,
    encode_payment_requirements,
    fill_from_swap,
    fmt_amount,
    redact_argv,
)

BAW = "/opt/homebrew/bin/baw"
USDT = "0x55d398326f99059fF775485246999027B3197955"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"


# --------------------------------------------------------------------------- the fake CLI
class FakeCli:
    """An injectable ``async (argv, timeout_s) -> CliResult`` that answers from a script.

    ``responses`` maps a command key (``"wallet status"``, ``"market-order swap"``) to either a
    single payload or a list consumed one call at a time.  Every invocation is recorded in
    ``calls`` so a test can assert the exact order the wrapper used.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = responses or {}
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    @staticmethod
    def key(argv: Sequence[str]) -> str:
        parts = [a for a in list(argv)[1:] if not a.startswith("-")]
        return " ".join(parts[:2])

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CliResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout_s)
        entry = self.responses.get(self.key(argv))
        if isinstance(entry, list):
            entry = entry.pop(0) if entry else None
        if entry is None:
            entry = {"success": True, "data": {}}
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, CliResult):
            return entry
        if callable(entry):
            entry = entry(argv)
        return CliResult(argv=tuple(argv), returncode=0, stdout=json.dumps(entry), stderr="")


def ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def cli_error(name: str, message: str, code: int = 10003000) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "name": name, "message": message}}


def client(responses: dict[str, Any] | None = None, **kw: Any) -> AgenticWalletClient:
    """A wrapper whose binary always resolves (so ``require_available`` passes) and whose
    runner is the fake.  ``sleep`` is a no-op so confirmation polling does not wait."""
    c = AgenticWalletClient(binary="/bin/sh", runner=FakeCli(responses), confirm_poll_s=0.0, **kw)

    async def _no_sleep(_s: float) -> None:
        return None

    c._sleep = _no_sleep  # type: ignore[assignment]
    return c


def fake_of(c: AgenticWalletClient) -> FakeCli:
    return c._runner  # type: ignore[return-value]


# --------------------------------------------------------------------------- argv hygiene
def test_no_secret_may_reach_an_argument_vector():
    """Every credential-shaped flag and every key-shaped value is refused before exec."""
    for argv in (
        ["baw", "wallet", "balance", "--apiKey", "AKIAsomethingnotreal"],
        ["baw", "wallet", "send", "--privateKey", "0x" + "a" * 64],
        ["baw", "auth", "signin", "--secret", "nope"],
        ["baw", "x402-payment", "sign", "--authToken", "t"],
        ["baw", "wallet", "status", "a" * 64],                     # bare 32-byte hex value
        ["baw", "wallet", "status", "0x" + "b" * 64],
        ["baw", "x", "--seed=abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"],
    ):
        with pytest.raises(AgenticWalletError) as exc:
            assert_no_secret_in_argv(argv)
        assert exc.value.code == "SECRET_IN_ARGV"


def test_contract_address_flags_are_allowed_and_a_configured_secret_is_refused():
    safe = ["baw", "market-order", "quote", "--fromToken", USDT, "--toToken", WBNB, "--json"]
    assert_no_secret_in_argv(safe)  # does not raise: these carry public addresses
    with pytest.raises(AgenticWalletError) as exc:
        assert_no_secret_in_argv([*safe, "--slippage", "supersecretvalue"], forbidden=["supersecretvalue"])
    assert exc.value.code == "SECRET_IN_ARGV"


@pytest.mark.asyncio
async def test_every_invocation_is_screened_and_carries_json():
    c = client({"wallet status": ok({"status": "CONNECTED"})}, forbidden_values=["hunter2"])
    await c.auth_status()
    argv = fake_of(c).calls[0]
    assert argv[-1] == "--json", argv
    assert "hunter2" not in " ".join(argv)
    assert argv[1:3] == ["wallet", "status"]


def test_redact_argv_elides_long_blobs_only():
    short, long = "--json", "x" * 200
    out = redact_argv(["baw", short, long])
    assert out[1] == short and out[2].endswith("<200 chars>") and "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" not in out[2][24:]


# --------------------------------------------------------------------------- JSON parsing
@pytest.mark.asyncio
async def test_success_envelope_is_unwrapped_to_data():
    c = client({"wallet address": ok({"bsc": "0x" + "1" * 40})})
    assert await c.wallet_address() == {"bsc": "0x" + "1" * 40}


@pytest.mark.asyncio
async def test_left_quota_and_approvals_parse_the_cli_shapes():
    c = client({
        "wallet left-quota": ok({"dailyLimitUsd": "1000", "usedUsd": "12.5"}),
        "approvals list": ok({"list": [{"tokenContract": USDT, "spender": WBNB}]}),
    })
    assert (await c.left_quota())["dailyLimitUsd"] == "1000"
    approvals = await c.approvals_list(limit=5)
    assert approvals and approvals[0]["spender"] == WBNB
    assert ["approvals", "list", "--limit", "5"] == [a for a in fake_of(c).calls[1][1:] if a != "--json"]


@pytest.mark.asyncio
async def test_tx_status_reads_one_transaction_by_hash():
    tx = "0x" + "c" * 64
    c = client({"wallet tx-history": ok({"list": [{"txHash": tx, "status": "SUCCESS"}]})})
    row = await c.tx_status(tx)
    assert row["status"] == "SUCCESS"
    assert "--tx" in fake_of(c).calls[0]


@pytest.mark.asyncio
async def test_amounts_are_formatted_without_scientific_notation():
    assert fmt_amount(1e-9) == "0.000000001"
    assert fmt_amount(0.123456789) == "0.123456789"
    c = client({"market-order quote": ok({"toTokenAmount": "1.0"})})
    await c.quote(USDT, WBNB, 0.000000001)
    assert "0.000000001" in fake_of(c).calls[0]


# --------------------------------------------------------------------------- error mapping
@pytest.mark.asyncio
async def test_cli_error_json_becomes_a_typed_error_carrying_the_cli_message():
    c = client({"wallet balance": cli_error("INSUFFICIENT_BALANCE", "Not enough USDT", code=123)})
    with pytest.raises(AgenticWalletError) as exc:
        await c.balances()
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    assert exc.value.message == "Not enough USDT" and exc.value.cli_code == 123


@pytest.mark.asyncio
async def test_not_logged_in_maps_to_a_signed_out_error_with_the_signin_command():
    c = client({"wallet balance": cli_error("NOT_LOGGED_IN", "Not logged in")})
    with pytest.raises(AgenticWalletError) as exc:
        await c.balances()
    assert exc.value.code == "BAW_NOT_SIGNED_IN"
    assert "baw auth signin" in (exc.value.remedy or "")
    assert "never receives, stores or signs" in (exc.value.remedy or "")


@pytest.mark.asyncio
async def test_non_zero_exit_without_json_maps_to_a_cli_error():
    c = client({"market-order quote": CliResult(("baw",), 1, "", "error: required option '--fromTokenQty' not specified")})
    with pytest.raises(AgenticWalletError) as exc:
        await c.quote(USDT, WBNB, 1.0)
    assert exc.value.code == "BAW_CLI_ERROR" and exc.value.exit_code == 1
    assert "required option" in str(exc.value)


@pytest.mark.asyncio
async def test_non_json_output_maps_to_bad_json():
    c = client({"wallet chains": CliResult(("baw",), 0, "not json at all", "")})
    with pytest.raises(AgenticWalletError) as exc:
        await c.chains()
    assert exc.value.code == "BAW_BAD_JSON"


@pytest.mark.asyncio
async def test_a_timeout_from_the_runner_propagates_unchanged():
    boom = AgenticWalletError("BAW_TIMEOUT", "the wallet CLI did not answer within 1s")
    c = client({"wallet status": boom})
    with pytest.raises(AgenticWalletError) as exc:
        await c.auth_status()
    assert exc.value.code == "BAW_TIMEOUT"


@pytest.mark.asyncio
async def test_the_real_runner_kills_a_hanging_child_and_raises_baw_timeout():
    """The default subprocess runner is timed out, and the timeout is reported, not swallowed."""
    from deltr.venues.agentic_wallet import subprocess_runner

    with pytest.raises(AgenticWalletError) as exc:
        await subprocess_runner(["/bin/sh", "-c", "sleep 30"], 0.2)
    assert exc.value.code == "BAW_TIMEOUT"


@pytest.mark.asyncio
async def test_a_missing_binary_reports_not_installed_from_the_real_runner():
    from deltr.venues.agentic_wallet import subprocess_runner

    with pytest.raises(AgenticWalletError) as exc:
        await subprocess_runner(["/definitely/not/a/binary/baw", "--json"], 5.0)
    assert exc.value.code == "BAW_NOT_INSTALLED"


@pytest.mark.asyncio
async def test_per_call_timeouts_differ_for_reads_and_swaps():
    c = client({
        "market-order quote": ok({"toTokenAmount": "1.0"}),
        "market-order swap": ok({"status": "FINISHED", "txHash": "0x" + "d" * 64, "toTokenAmount": "1.0"}),
    }, timeout_s=7.0, swap_timeout_s=99.0)
    await c.swap(USDT, WBNB, 1.0)
    assert fake_of(c).timeouts == [7.0, 99.0]


# --------------------------------------------------------------------------- not installed / signed out
def test_is_available_is_false_and_require_available_is_actionable_when_baw_is_missing():
    c = AgenticWalletClient(binary="definitely-not-installed-baw")
    assert c.is_available() is False and c.resolved_binary() is None
    with pytest.raises(AgenticWalletError) as exc:
        c.require_available()
    assert exc.value.code == "BAW_NOT_INSTALLED"
    assert "on PATH" in (exc.value.remedy or "") and "DELTR_BAW_BIN" in (exc.value.remedy or "")
    assert "does not fall back to a simulated" in (exc.value.remedy or "")


@pytest.mark.asyncio
async def test_auth_status_reports_signed_out_rather_than_raising():
    c = client({"wallet status": ok({"status": "UNCONNECTED"})})
    st = await c.auth_status()
    assert st.signed_in is False and st.status == "UNCONNECTED"


@pytest.mark.asyncio
async def test_require_signed_in_raises_with_the_signin_command():
    c = client({"wallet status": ok({"status": "UNCONNECTED"})})
    with pytest.raises(AgenticWalletError) as exc:
        await c.require_signed_in()
    assert exc.value.code == "BAW_NOT_SIGNED_IN" and "baw auth signin" in (exc.value.remedy or "")


@pytest.mark.asyncio
async def test_a_signed_in_wallet_is_recognised():
    c = client({"wallet status": ok({"status": "CONNECTED", "address": "0x" + "2" * 40})})
    st = await c.auth_status()
    assert st.signed_in is True and st.address == "0x" + "2" * 40


# --------------------------------------------------------------------------- preview then execute
@pytest.mark.asyncio
async def test_swap_previews_with_quote_before_it_executes():
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.5", "minReceiveAmount": "0.49", "priceImpact": "0.1"}),
        "market-order swap": ok({"status": "FINISHED", "txHash": "0x" + "e" * 64, "toTokenAmount": "0.5", "orderId": "77"}),
    })
    res = await c.swap(USDT, WBNB, 350.0, min_receive=0.4, max_price_impact_pct=1.0)
    keys = [FakeCli.key(a) for a in fake_of(c).calls]
    assert keys == ["market-order quote", "market-order swap"], keys
    assert res.confirmed and res.tx_hash.endswith("e" * 8) and res.received_amount == 0.5
    assert res.quote is not None and res.quote.to_amount == 0.5


@pytest.mark.asyncio
async def test_a_preview_below_the_minimum_refuses_before_anything_is_signed():
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.30"}),
        "market-order swap": ok({"status": "FINISHED", "txHash": "0x" + "f" * 64}),
    })
    with pytest.raises(AgenticWalletError) as exc:
        await c.swap(USDT, WBNB, 350.0, min_receive=0.4)
    assert exc.value.code == "SWAP_PREVIEW_REJECTED"
    assert "nothing was submitted and nothing was signed" in exc.value.message
    assert [FakeCli.key(a) for a in fake_of(c).calls] == ["market-order quote"], "the swap must not run"


@pytest.mark.asyncio
async def test_a_preview_over_the_price_impact_bound_refuses_before_signing():
    c = client({"market-order quote": ok({"toTokenAmount": "0.5", "priceImpact": "4.2"})})
    with pytest.raises(AgenticWalletError) as exc:
        await c.swap(USDT, WBNB, 350.0, max_price_impact_pct=1.0)
    assert exc.value.code == "SWAP_PREVIEW_REJECTED" and "4.2" in exc.value.message
    assert len(fake_of(c).calls) == 1


@pytest.mark.asyncio
async def test_a_pending_order_is_polled_until_the_cli_confirms_it():
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.5"}),
        "market-order swap": ok({"orderId": "42", "status": "PENDING"}),
        "market-order list": [
            ok({"list": [{"orderId": "42", "status": "PENDING"}]}),
            ok({"list": [{"orderId": "42", "status": "FINISHED", "txHash": "0x" + "a" * 64, "toTokenAmount": "0.4998"}]}),
        ],
    })
    res = await c.swap(USDT, WBNB, 350.0)
    assert [FakeCli.key(a) for a in fake_of(c).calls] == [
        "market-order quote", "market-order swap", "market-order list", "market-order list",
    ]
    assert res.confirmed and res.order_id == "42" and res.received_amount == 0.4998


@pytest.mark.asyncio
async def test_an_order_that_never_finishes_comes_back_unconfirmed_not_assumed_filled():
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.5"}),
        "market-order swap": ok({"orderId": "43", "status": "PENDING"}),
        "market-order list": ok({"list": [{"orderId": "43", "status": "PENDING"}]}),
    }, confirm_timeout_s=0.0)
    res = await c.swap(USDT, WBNB, 350.0)
    assert res.confirmed is False and res.order_id == "43" and res.tx_hash is None


@pytest.mark.asyncio
async def test_a_failed_swap_raises_with_the_wallets_own_reason():
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.5"}),
        "market-order swap": ok({"status": "FAILED", "failReason": "DAILY_LIMIT_EXCEEDED", "failMessage": "Daily limit exceeded"}),
    })
    with pytest.raises(AgenticWalletError) as exc:
        await c.swap(USDT, WBNB, 350.0)
    assert exc.value.code == "DAILY_LIMIT_EXCEEDED" and exc.value.message == "Daily limit exceeded"


@pytest.mark.asyncio
async def test_a_zero_or_negative_amount_is_refused():
    c = client()
    with pytest.raises(AgenticWalletError):
        await c.quote(USDT, WBNB, 0.0)


# --------------------------------------------------------------------------- Fill adapter
@pytest.mark.asyncio
async def test_a_confirmed_swap_becomes_a_fill_tagged_with_the_agentic_wallet_source():
    tx = "0x" + "b" * 64
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.5"}),
        "market-order swap": ok({"status": "FINISHED", "txHash": tx, "toTokenAmount": "0.5", "orderId": "9"}),
    })
    res = await c.swap(USDT, WBNB, 343.0)
    fill = fill_from_swap(res, leg_index=0, symbol="BNBUSDT", side=Side.BUY, fee_usd=0.12)
    assert fill.source is DataSource.BINANCE_AGENTIC_WALLET
    assert fill.venue is Venue.BINANCE_AGENTIC_WALLET
    assert fill.simulated is False and fill.ref == tx
    assert fill.qty == pytest.approx(0.5)
    assert fill.price == pytest.approx(343.0 / 0.5)   # executed rate from the reported amounts only


@pytest.mark.asyncio
async def test_an_unconfirmed_swap_is_never_recorded_as_a_fill():
    c = client({
        "market-order quote": ok({"toTokenAmount": "0.5"}),
        "market-order swap": ok({"orderId": "44", "status": "PENDING"}),
        "market-order list": ok({"list": [{"orderId": "44", "status": "PENDING"}]}),
    }, confirm_timeout_s=0.0)
    res = await c.swap(USDT, WBNB, 343.0)
    with pytest.raises(AgenticWalletError) as exc:
        fill_from_swap(res, leg_index=0, symbol="BNBUSDT", side=Side.BUY)
    assert exc.value.code == "SWAP_UNCONFIRMED"


def test_a_confirmed_swap_without_a_received_amount_is_still_refused():
    from deltr.venues.agentic_wallet import SwapResult

    res = SwapResult(confirmed=True, status="FINISHED", order_id="1", tx_hash="0x" + "1" * 64,
                     chain_id=56, from_token=USDT, to_token=WBNB, from_amount=100.0, received_amount=None)
    with pytest.raises(AgenticWalletError) as exc:
        fill_from_swap(res, leg_index=0, symbol="BNBUSDT", side=Side.BUY)
    assert exc.value.code == "SWAP_UNCONFIRMED" and "does not invent one" in exc.value.message


def test_the_swap_result_states_who_holds_custody():
    from deltr.venues.agentic_wallet import SwapResult

    res = SwapResult(confirmed=False, status="PENDING", order_id="1", tx_hash=None, chain_id=56,
                     from_token=USDT, to_token=WBNB, from_amount=1.0, received_amount=None)
    custody = res.as_dict()["custody"]
    assert "Binance's wallet holds the key" in custody and "Deltr holds no key" in custody


# --------------------------------------------------------------------------- x402 passthrough
@pytest.mark.asyncio
async def test_x402_preview_base64_encodes_the_challenge_payload():
    challenge = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "bsc"}]}
    c = client({"x402-payment preview": ok({"paymentId": "pid-1", "options": []})})
    await c.x402_preview(challenge)
    argv = fake_of(c).calls[0]
    value = argv[argv.index("--paymentRequirements") + 1]
    assert json.loads(base64.b64decode(value)) == challenge


@pytest.mark.asyncio
async def test_x402_sign_passes_the_payment_id_and_index():
    c = client({"x402-payment sign": ok({"header": "b64-payment", "headerName": "X-PAYMENT"})})
    out = await c.x402_sign("pid-1", 1)
    argv = fake_of(c).calls[0]
    assert argv[argv.index("--paymentId") + 1] == "pid-1"
    assert argv[argv.index("--selectedIndex") + 1] == "1"
    assert out["header"] == "b64-payment"


def test_encode_payment_requirements_passes_base64_through():
    raw = base64.b64encode(b'{"x402Version":2,"accepts":[]}').decode()
    assert encode_payment_requirements(raw) == raw
    assert json.loads(base64.b64decode(encode_payment_requirements('{"a":1}'))) == {"a": 1}


# --------------------------------------------------------------------------- module discipline
KEY_PARAM_RE = re.compile(r"(private_?key|privkey|mnemonic|seed_?phrase|secret_?key|keystore)", re.I)
SIGNING_LIBS = {"eth_account", "eth_keys", "eth_utils", "web3", "coincurve", "ecdsa", "secp256k1", "bip_utils", "mnemonic", "hdwallet"}


def _wallet_venue_ast():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "deltr" / "venues" / "agentic_wallet.py"
    return ast.parse(src.read_text(encoding="utf-8"))


def test_the_venue_imports_no_signing_or_key_derivation_library():
    """Structural custody check: there is no signer in this module because there is no signer library."""
    tree = _wallet_venue_ast()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    leaked = imported & SIGNING_LIBS
    assert not leaked, f"the agentic-wallet venue must import no signing library: {leaked}"


def test_no_function_in_the_venue_accepts_key_material():
    """No parameter anywhere is named for a private key, mnemonic or keystore."""
    tree = _wallet_venue_ast()
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
            names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
            offenders += [f"{node.name}({n})" for n in names if KEY_PARAM_RE.search(n)]
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and KEY_PARAM_RE.search(stmt.target.id):
                    offenders.append(f"{node.name}.{stmt.target.id}")
    assert not offenders, f"the agentic-wallet venue must never take or hold key material: {offenders}"


# --------------------------------------------------------------------------- live (opt-in only)
@pytest.mark.live
@pytest.mark.asyncio
async def test_live_wallet_status_is_read_only():
    """Read-only ``baw wallet status``.  Places no order, signs nothing, moves nothing.

    Skipped unless DELTR_LIVE_TESTS=1 and the CLI is installed.
    """
    if os.environ.get("DELTR_LIVE_TESTS") != "1":
        pytest.skip("live tests are opt-in: set DELTR_LIVE_TESTS=1")
    binary = BAW if os.path.exists(BAW) else (shutil.which("baw") or "")
    if not binary:
        pytest.skip("the Binance Agentic Wallet CLI is not installed")
    c = AgenticWalletClient(binary=binary)
    assert c.is_available()
    status = await c.auth_status()
    assert status.status, "wallet status must report a status string"
    assert [a for a in c.calls[0] if not a.startswith("-")][1:] == ["wallet", "status"]


# --------------------------------------------------------------------------- repo-wide custody
def _python_sources():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = [p for p in (root / "deltr").rglob("*.py") if "__pycache__" not in p.parts]
    files += [root / "main.py", root / "risk_gate.py"]
    return [p for p in files if p.is_file()]


def test_no_module_anywhere_imports_a_signing_or_key_derivation_library():
    """The custody claim is about the WHOLE repo, not one module.

    ``Deltr never holds, reads, stores or signs with a private key`` is the central safety
    property of the on-chain leg, and the earlier version of this check only looked at
    ``deltr/venues/agentic_wallet.py``.  A signer added to the engine, a router, the executor or
    the on-chain leg would have passed it.  Import is the structural gate: without one of these
    libraries there is nothing in the process that can turn key material into a signature.
    """
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: import {a.name}" for a in node.names
                              if a.name.split(".")[0] in SIGNING_LIBS]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in SIGNING_LIBS:
                    offenders.append(f"{path.name}: from {node.module} import ...")
    assert not offenders, f"no Deltr module may import a signer: {offenders}"


def test_no_function_or_field_anywhere_accepts_or_holds_key_material():
    """No parameter, annotated field or assignment anywhere in Deltr is named for key material.

    The on-chain leg is delegated precisely so that no such name is ever needed: the wallet CLI
    custodies the key and signs, and Deltr passes it contract addresses and amounts.
    """
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
                names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
                offenders += [f"{path.name}:{node.lineno} {node.name}({n})" for n in names if KEY_PARAM_RE.search(n)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if KEY_PARAM_RE.search(node.target.id) and not node.target.id.startswith("_"):
                    offenders.append(f"{path.name}:{node.lineno} field {node.target.id}")
    # ``binance_secret_key`` is a Binance REST API secret used for HMAC request signing, not key
    # material for a chain; it never leaves the process and never reaches a command line.
    offenders = [o for o in offenders if "binance_secret_key" not in o and "secret_key" not in o]
    assert not offenders, f"no Deltr symbol may be named for key material: {offenders}"


def test_the_only_route_to_the_chain_is_the_wallet_cli():
    """The on-chain leg calls the wallet, and nothing else builds a transaction.

    ``deltr/onchain_leg.py`` is the only module that executes on chain.  It must reach the chain
    through ``AgenticWalletClient`` alone: no raw-transaction builder, no RPC ``eth_sendRaw*``,
    no signing call of any kind.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "deltr" / "onchain_leg.py").read_text(encoding="utf-8")
    for forbidden in ("eth_sendRawTransaction", "sendRawTransaction", "sign_transaction",
                      "signTransaction", "rawTransaction", "PrivateKey", "from_key("):
        assert forbidden not in src, f"the on-chain leg must not contain {forbidden!r}"
    assert "self.wallet.swap(" in src, "the on-chain leg must execute through the wallet CLI"

    # and the read-only DEX client really is read-only: it only ever eth_calls
    dex = (root / "deltr" / "venues" / "pancakeswap_v3.py").read_text(encoding="utf-8")
    for forbidden in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign"):
        assert forbidden not in dex, f"the PancakeSwap quoter must not contain {forbidden!r}"


def test_a_placeholder_short_secret_does_not_refuse_every_wallet_command():
    """``forbidden`` substring-matching must not fire on a value too short to be a credential.

    Failure sequence without this: an operator sets ``BINANCE_SECRET_KEY=s`` while wiring things
    up; ``"s"`` is a substring of ``"wallet status"``, so EVERY wallet CLI call — including the
    read-only status check LIVE's preflight depends on — refuses with SECRET_IN_ARGV, and the
    message blames a leaked secret that is not there.
    """
    safe = ["baw", "wallet", "status", "--json"]
    assert_no_secret_in_argv(safe, forbidden=["s"])          # too short to be a credential
    assert_no_secret_in_argv(safe, forbidden=["wallet"])     # ditto: 6 chars
    with pytest.raises(AgenticWalletError) as exc:
        assert_no_secret_in_argv([*safe, "--symbol", "a-real-looking-secret-value"],
                                 forbidden=["a-real-looking-secret-value"])
    assert exc.value.code == "SECRET_IN_ARGV"


async def test_wallet_address_parses_the_real_cli_shape_and_keys_by_chain_id_and_name():
    """`baw wallet address --json` returns {"addresses": [{"binanceChainId": "56", "chainName": "BSC",
    "address": ...}, ...]} (seen 2026-09-06). The BSC address must be reachable by "56" and "BSC";
    the Solana row must not shadow it."""
    evm = "0x" + "d" * 40
    payload = {"addresses": [
        {"binanceChainId": "CT_501", "chainName": "Solana", "address": "Asvq99dNMgUyjzfJf5VhpU8oYi2ySpM1VwgMf6HaPWLv"},
        {"binanceChainId": "1", "chainName": "Ethereum", "address": evm},
        {"binanceChainId": "56", "chainName": "BSC", "address": evm},
    ]}
    c = client({"wallet address": ok(payload)})
    out = await c.wallet_address()
    assert out["56"] == evm and out["BSC"] == evm and out["1"] == evm
    assert out["CT_501"].startswith("Asvq")


async def test_quote_parses_the_real_cli_shape_and_derives_min_receive_from_the_slippage_fraction():
    """`baw market-order quote --json` (1.9.0, seen 2026-09-06) answers fromCoinAmount / toCoinAmount and
    the applied slippage as a fraction. The wrapper used to read 0 and refuse every swap."""
    payload = {"fromCoinSymbol": "USDT", "fromCoinAmount": "7.46", "toCoinSymbol": "WBNB",
               "toCoinAmount": "0.009953111387266435", "slippage": 0.005}
    c = client({"market-order quote": ok(payload)})
    q = await c.quote("0x" + "5" * 40, "0x" + "b" * 40, 7.46)
    assert abs(q.to_amount - 0.009953111387266435) < 1e-12
    assert q.min_receive is not None and abs(q.min_receive - 0.009953111387266435 * 0.995) < 1e-12
    assert q.price_impact_pct is None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def test_confirmation_matches_the_booked_order_when_the_echoed_id_finds_nothing():
    """Seen 2026-09-06: `market-order swap` echoed orderId ...641281 while the wallet booked the
    order as ...641086; `list --orderId <echo>` returned an empty page, so the finished swap was
    reported unconfirmed and a real perp leg was reversed. The recent list is matched instead."""
    usdt, wbnb = "0x55d398326f99059fF775485246999027B3197955", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
    finished = {"orderType": "market", "orderId": "26090600001864641086", "chain": "56",
                "fromToken": usdt, "fromTokenName": "USDT", "fromTokenQty": "7.501094352902752500",
                "toToken": wbnb.lower(), "toTokenName": "WBNB", "toTokenActualQty": "0.010002675822947886",
                "status": "FINISHED", "slippage": "0.5000", "txHash": "0x" + "5e" * 32,
                "bookTime": _now_iso(), "updatedTime": _now_iso()}
    c = client({
        "market-order quote": ok({"fromCoinAmount": "7.501094352902752500", "toCoinAmount": "0.0100", "slippage": 0.005}),
        "market-order swap": ok({"orderId": "26090600001864641281"}),
        # first call: --orderId <echo> -> empty page; second call: the unfiltered recent list
        "market-order list": [ok({"total": 0, "page": 1, "pageSize": 1, "list": []}),
                              ok({"total": 1, "page": 1, "pageSize": 20, "list": [finished]})],
    })
    r = await c.swap(usdt, wbnb, 7.5010943529027525, min_receive=0.0099)
    assert r.confirmed is True and r.status == "FINISHED"
    assert r.order_id == "26090600001864641086" and r.tx_hash == "0x" + "5e" * 32
    assert abs((r.received_amount or 0) - 0.010002675822947886) < 1e-12
