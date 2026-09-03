"""tests/test_payments.py — the x402 (B402) workflow, both directions, against a FAKE CLI.

Nothing here invokes the real ``baw`` and nothing signs or settles.  The buyer side is driven
through the same injectable runner the wallet tests use; the seller side is pure local
construction of a challenge over the existing edge report.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from deltr.models import sha256_of, utcnow
from deltr.payments import (
    BSC_USDT_ADDRESS,
    PAYMENT_HEADER,
    PAYMENT_REQUIRED_HEADER,
    PaidArtifact,
    PaymentError,
    PaymentRefused,
    X402Buyer,
    X402Seller,
    artifact_from_edge_report,
    parse_payment_required,
)
from deltr.venues.agentic_wallet import AgenticWalletClient, AgenticWalletError
from tests.test_agentic_wallet import FakeCli, cli_error, fake_of, ok

PAY_TO = "0x" + "9" * 40
ONE_CENT_ATOMIC = str(10 ** 16)          # 0.01 units at 18 decimals
TEN_UNITS_ATOMIC = str(10 * 10 ** 18)


def challenge_body(*, network: str = "bsc", amount: str = ONE_CENT_ATOMIC, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    option = {
        "scheme": "exact",
        "network": network,
        "maxAmountRequired": amount,
        "resource": "https://example.test/report",
        "description": "an upstream data endpoint",
        "mimeType": "application/json",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "asset": BSC_USDT_ADDRESS,
    }
    option.update(extra or {})
    return {"x402Version": 2, "error": "payment required", "accepts": [option]}


def buyer(responses: dict[str, Any] | None = None, **kw: Any) -> X402Buyer:
    wallet = AgenticWalletClient(binary="/bin/sh", runner=FakeCli(responses))
    return X402Buyer(wallet, **kw)


class FakeResponse:
    """Just enough of an ``httpx.Response`` for the parser."""

    def __init__(self, *, status_code: int = 402, headers: dict[str, str] | None = None, body: Any = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


# --------------------------------------------------------------------------- parsing a 402
def test_a_402_body_parses_into_typed_options():
    pr = parse_payment_required(challenge_body())
    assert pr.version == 2 and len(pr.options) == 1
    option = pr.options[0]
    assert option.network == "bsc" and option.pay_to == PAY_TO and option.amount_atomic == ONE_CENT_ATOMIC
    assert pr.as_dict()["network_label"].startswith("BNB Smart Chain")


def test_a_402_response_object_is_read_from_its_header_first_then_its_body():
    encoded = base64.b64encode(json.dumps(challenge_body()).encode()).decode()
    from_header = parse_payment_required(FakeResponse(headers={PAYMENT_REQUIRED_HEADER: encoded}))
    from_body = parse_payment_required(FakeResponse(body=challenge_body()))
    assert from_header.options[0].pay_to == from_body.options[0].pay_to == PAY_TO


def test_a_non_402_response_is_refused():
    with pytest.raises(PaymentError) as exc:
        parse_payment_required(FakeResponse(status_code=200, body=challenge_body()))
    assert exc.value.code == "NOT_PAYMENT_REQUIRED"


@pytest.mark.parametrize("payload", [{"x402Version": 2}, {"x402Version": 2, "accepts": []}, {"x402Version": 2, "accepts": ["nope"]}])
def test_a_malformed_402_is_refused(payload):
    with pytest.raises(PaymentError) as exc:
        parse_payment_required(payload)
    assert exc.value.code == "BAD_PAYMENT_REQUIRED"


def test_raw_json_and_base64_forms_both_parse():
    body = challenge_body()
    assert parse_payment_required(json.dumps(body)).options[0].pay_to == PAY_TO
    assert parse_payment_required(base64.b64encode(json.dumps(body).encode()).decode()).options[0].pay_to == PAY_TO


# --------------------------------------------------------------------------- buyer
@pytest.mark.asyncio
async def test_the_buyer_previews_then_signs_in_that_order():
    b = buyer({
        "x402-payment preview": ok({"paymentId": "pid-9", "options": [dict(challenge_body()["accepts"][0], index=0)]}),
        "x402-payment sign": ok({"headerName": PAYMENT_HEADER, "header": "b64-signed-payment"}),
    })
    payment = await b.pay(challenge_body())
    keys = [FakeCli.key(a) for a in fake_of(b.wallet).calls]
    assert keys == ["x402-payment preview", "x402-payment sign"], keys
    assert payment.payment_id == "pid-9" and payment.selected_index == 0
    assert payment.headers() == {PAYMENT_HEADER: "b64-signed-payment"}
    assert payment.signed_by == "binance-agentic-wallet"


@pytest.mark.asyncio
async def test_the_signed_header_value_is_withheld_from_the_default_view():
    b = buyer({
        "x402-payment preview": ok({"paymentId": "pid-9", "options": [dict(challenge_body()["accepts"][0], index=0)]}),
        "x402-payment sign": ok({"header": "b64-signed-payment"}),
    })
    payment = await b.pay(challenge_body())
    default = payment.as_dict()
    assert "header_value" not in default and default["header_present"] is True
    assert payment.as_dict(include_header_value=True)["header_value"] == "b64-signed-payment"


@pytest.mark.asyncio
async def test_a_payment_over_the_ceiling_is_refused_before_anything_is_signed():
    b = buyer({"x402-payment preview": ok({"paymentId": "pid-1", "options": [dict(challenge_body(amount=TEN_UNITS_ATOMIC)["accepts"][0], index=0)]})},
              max_amount_units=1.0)
    with pytest.raises(PaymentRefused) as exc:
        await b.pay(challenge_body(amount=TEN_UNITS_ATOMIC))
    assert exc.value.code == "X402_NO_ACCEPTABLE_OPTION" and "X402_AMOUNT_OVER_CAP" not in exc.value.code
    assert [FakeCli.key(a) for a in fake_of(b.wallet).calls] == ["x402-payment preview"], "sign must not run"


@pytest.mark.asyncio
async def test_a_payment_on_another_network_is_refused():
    body = challenge_body(network="ethereum")
    b = buyer({"x402-payment preview": ok({"paymentId": "pid-2", "options": [dict(body["accepts"][0], index=0)]})})
    with pytest.raises(PaymentRefused) as exc:
        await b.pay(body)
    assert exc.value.code == "X402_NO_ACCEPTABLE_OPTION" and "not in the allow-list" in exc.value.message


@pytest.mark.asyncio
async def test_an_explicit_index_is_honoured_but_still_policy_checked():
    cheap = dict(challenge_body()["accepts"][0], index=0)
    dear = dict(challenge_body(amount=TEN_UNITS_ATOMIC)["accepts"][0], index=1)
    body = {"x402Version": 2, "accepts": [cheap, dear]}
    b = buyer({
        "x402-payment preview": ok({"paymentId": "pid-3", "options": [cheap, dear]}),
        "x402-payment sign": ok({"header": "h"}),
    }, max_amount_units=1.0)
    payment = await b.pay(body, selected_index=0)
    assert payment.selected_index == 0
    with pytest.raises(PaymentRefused) as exc:
        await b.pay(body, selected_index=1)
    assert exc.value.code == "X402_AMOUNT_OVER_CAP"


@pytest.mark.asyncio
async def test_the_first_acceptable_option_is_chosen_when_none_is_named():
    dear = dict(challenge_body(amount=TEN_UNITS_ATOMIC)["accepts"][0], index=0)
    cheap = dict(challenge_body()["accepts"][0], index=1)
    b = buyer({
        "x402-payment preview": ok({"paymentId": "pid-4", "options": [dear, cheap]}),
        "x402-payment sign": ok({"header": "h"}),
    }, max_amount_units=1.0)
    payment = await b.pay({"x402Version": 2, "accepts": [dear, cheap]})
    assert payment.selected_index == 1


@pytest.mark.asyncio
async def test_a_preview_without_a_payment_id_is_an_error():
    b = buyer({"x402-payment preview": ok({"options": []})})
    with pytest.raises(PaymentError) as exc:
        await b.preview(challenge_body())
    assert exc.value.code == "X402_PREVIEW_FAILED"


@pytest.mark.asyncio
async def test_a_sign_that_returns_no_header_is_an_error():
    b = buyer({
        "x402-payment preview": ok({"paymentId": "pid-5", "options": [dict(challenge_body()["accepts"][0], index=0)]}),
        "x402-payment sign": ok({"status": "ok"}),
    })
    with pytest.raises(PaymentError) as exc:
        await b.pay(challenge_body())
    assert exc.value.code == "X402_SIGN_FAILED"


@pytest.mark.asyncio
async def test_a_signed_out_wallet_surfaces_as_the_cli_signed_out_code():
    b = buyer({"x402-payment preview": cli_error("NOT_LOGGED_IN", "Not logged in")})
    with pytest.raises(AgenticWalletError) as exc:
        await b.preview(challenge_body())
    assert exc.value.code == "BAW_NOT_SIGNED_IN"


@pytest.mark.asyncio
async def test_the_wallet_sign_failure_becomes_a_payment_error_carrying_the_cli_message():
    b = buyer({
        "x402-payment preview": ok({"paymentId": "pid-6", "options": [dict(challenge_body()["accepts"][0], index=0)]}),
        "x402-payment sign": cli_error("USER_REJECTED", "The wallet declined the signature", code=99),
    })
    with pytest.raises(PaymentError) as exc:
        await b.pay(challenge_body())
    assert exc.value.code == "USER_REJECTED" and "declined" in exc.value.message


# --------------------------------------------------------------------------- seller
def artifact() -> PaidArtifact:
    return artifact_from_edge_report(
        {"title": "Deltr edge report: BNBUSDT", "markdown": "# Deltr edge report: BNBUSDT\n\nNet edge +1.20 bps\n"},
        symbol="BNBUSDT",
        generated_at=utcnow(),
    )


def test_the_seller_builds_a_well_formed_402_over_the_sealed_edge_report():
    seller = X402Seller(pay_to=PAY_TO, price_units=0.05)
    art = artifact()
    ch = seller.challenge(art)
    assert ch.status_code == 402
    body = ch.body
    assert body["x402Version"] == 2 and len(body["accepts"]) == 1
    option = body["accepts"][0]
    assert option["scheme"] == "exact" and option["network"] == "bsc"
    assert option["asset"] == BSC_USDT_ADDRESS and option["payTo"] == PAY_TO
    assert option["maxAmountRequired"] == str(int(0.05 * 10 ** 18))
    assert option["extra"]["artifact_sha256"] == art.sha256
    assert option["mimeType"] == "text/markdown"


def test_the_challenge_header_round_trips_and_never_ships_the_artifact():
    seller = X402Seller(pay_to=PAY_TO)
    ch = seller.challenge(artifact())
    decoded = json.loads(base64.b64decode(ch.headers[PAYMENT_REQUIRED_HEADER]))
    assert decoded == ch.body
    flat = json.dumps(ch.as_dict())
    assert "Net edge +1.20 bps" not in flat, "the paid artifact must not ship inside the challenge"
    assert ch.artifact.sha256 in flat


def test_the_artifact_digest_is_the_same_sha256_deltr_seals_receipts_with():
    art = artifact()
    assert art.sha256 == sha256_of(art.digest_body())
    assert len(art.sha256) == 64


def test_the_seller_refuses_to_build_a_challenge_without_a_receiving_address():
    with pytest.raises(PaymentError) as exc:
        X402Seller(pay_to=None).challenge(artifact())
    assert exc.value.code == "X402_NO_PAY_TO" and "never invents or derives one" in (exc.value.remedy or "")


def test_an_empty_edge_report_is_not_sellable():
    with pytest.raises(PaymentError) as exc:
        artifact_from_edge_report({"title": "t", "markdown": "   "})
    assert exc.value.code == "X402_EMPTY_ARTIFACT"


@pytest.mark.asyncio
async def test_a_presented_payment_is_recorded_unverified_and_says_so():
    seller = X402Seller(pay_to=PAY_TO)
    ch = seller.challenge(artifact())
    presented = await seller.record_payment(base64.b64encode(b'{"paymentId":"pid-77"}').decode(), ch)
    assert presented.verified is False and presented.payment_id == "pid-77"
    assert "not verified" in presented.note.lower() and "facilitator" in presented.note.lower()


@pytest.mark.asyncio
async def test_a_verifier_can_mark_a_payment_verified():
    async def verifier(_header: str, _challenge: Any) -> bool:
        return True

    seller = X402Seller(pay_to=PAY_TO, verifier=verifier)
    ch = seller.challenge(artifact())
    presented = await seller.record_payment("opaque-header", ch)
    assert presented.verified is True and presented.payment_id.startswith("sha256:")


@pytest.mark.asyncio
async def test_a_missing_payment_header_is_refused():
    seller = X402Seller(pay_to=PAY_TO)
    with pytest.raises(PaymentError) as exc:
        await seller.record_payment("", seller.challenge(artifact()))
    assert exc.value.code == "X402_NO_PAYMENT_HEADER"


def test_every_returned_object_is_labelled_bsc_and_claims_no_settlement():
    seller = X402Seller(pay_to=PAY_TO)
    flat = json.dumps(seller.challenge(artifact()).as_dict())
    assert "BNB Smart Chain" in flat
    assert "No mainnet B402 application was made" in flat and "no settlement" in flat
