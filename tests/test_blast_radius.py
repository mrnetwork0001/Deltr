"""tests/test_blast_radius.py — what one armed process can actually reach and spend.

These are the isolation invariants an operator is entitled to rely on before arming real money,
written as the attacks rather than as the happy path:

* PAPER and TESTNET can never build a credentialed mainnet order client or a ``LiveRouter``;
* the LIVE per-trade ceiling cannot be raised by an environment variable;
* the on-chain AGGREGATE ceiling counts every swap and every x402 payment the wallet was asked
  to sign, including the ones that come back unconfirmed — it used to count only confirmed
  swaps and no payments at all, which made it unenforceable for exactly those cases;
* a charge is given back only when the failure code proves nothing was signed;
* an exposure-REDUCING on-chain swap is never blocked by the caps that bound new exposure,
  because a refused reversal is a naked leg;
* the banner never tells an operator that no funds are at risk while the on-chain leg is armed.

Nothing here places an order, broadcasts a transaction or invokes ``baw``: the wallet is a fake
object with no subprocess and the HTTP client is a MockTransport that answers nothing.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import main as deltr_main
from deltr.config import LIVE_ACK_PHRASE, ONCHAIN_ACK_PHRASE, Mode, Settings
from deltr.engine import OnchainRefused, build_engine
from deltr.executor import LiveRouter
from deltr.payments import PaymentRefused, X402Payment
from deltr.venues.agentic_wallet import AgenticWalletError
from deltr.venues.binance_futures import FuturesClient
from tests._fakes_d import FILTERS
from tests.test_live_mode import FakeMainnetFutures, FakeQuoter, FakeWallet

pytestmark = pytest.mark.asyncio

MAINNET = "https://fapi.binance.com"


class CountingWallet(FakeWallet):
    """FakeWallet plus the ``calls`` list the engine reads back into its response."""

    calls: list = []


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def armed_engine(tmp_path, mode: str = "paper", **over: Any):
    """An engine whose on-chain leg is armed, with a fake wallet in place of the CLI."""
    kw: dict[str, Any] = {
        "_env_file": None, "DELTR_MODE": mode, "DELTR_STATE_DIR": str(tmp_path),
        "DELTR_ONCHAIN_MODE": "live", "DELTR_ONCHAIN_ACK": ONCHAIN_ACK_PHRASE,
    }
    if mode in ("testnet", "live"):
        kw.update(BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real")
    if mode == "live":
        kw.update(BINANCE_API_ENV="mainnet", DELTR_LIVE_ACK=LIVE_ACK_PHRASE)
    kw.update(over)
    engine = build_engine(Settings(**kw), http=_http())  # type: ignore[arg-type]
    wallet = CountingWallet()
    engine.wallet = wallet
    engine.x402_buyer.wallet = wallet
    return engine, wallet


# --------------------------------------------------------------------------- venue isolation
async def test_paper_and_testnet_cannot_reach_a_mainnet_order_endpoint(tmp_path):
    """The two things that would be needed, refused at construction in both modes."""
    http = _http()
    for mode, extra in (("paper", {}), ("testnet", {"BINANCE_API_KEY": "k", "BINANCE_SECRET_KEY": "s"})):
        s = Settings(_env_file=None, DELTR_MODE=mode, DELTR_STATE_DIR=str(tmp_path), **extra)  # type: ignore[arg-type]
        assert s.hosts["futures_order_rest"] != MAINNET
        with pytest.raises(ValueError, match="allow_mainnet_orders"):
            FuturesClient(http, MAINNET, api_key="k", secret_key="s")
        with pytest.raises(AssertionError, match=f"refuses to run in {mode.upper()}"):
            LiveRouter(FakeMainnetFutures(), object(), s, FILTERS)
    await http.aclose()


async def test_the_live_per_trade_cap_cannot_be_raised_by_an_env_var(tmp_path):
    s = Settings(  # type: ignore[arg-type]
        _env_file=None, DELTR_MODE="live", DELTR_STATE_DIR=str(tmp_path),
        BINANCE_API_KEY="k", BINANCE_SECRET_KEY="s", BINANCE_API_ENV="mainnet",
        DELTR_LIVE_ACK=LIVE_ACK_PHRASE, DELTR_ONCHAIN_MODE="live", DELTR_ONCHAIN_ACK=ONCHAIN_ACK_PHRASE,
        DELTR_LIVE_MAX_NOTIONAL_USD=1_000_000.0,
    )
    assert s.max_notional_usd == 250.0, "the table ceiling wins; the env var may only lower it"
    assert s.risk_limits().max_notional_usd == 250.0


# --------------------------------------------------------------------------- on-chain aggregate cap
async def test_unconfirmed_swaps_still_consume_the_aggregate_cap(tmp_path):
    """A PENDING swap has a transaction hash and has moved value; it must count."""
    engine, wallet = armed_engine(
        tmp_path, DELTR_ONCHAIN_MAX_AGGREGATE_USD=1_000.0, DELTR_ONCHAIN_MAX_NOTIONAL_USD=250.0,
    )
    wallet.outcome = "unconfirmed"
    sent = 0
    with pytest.raises(OnchainRefused) as caught:
        for _ in range(10):
            await engine.onchain_swap(from_token="0xA", to_token="0xB", amount=1.0,
                                      notional_usd=250.0, confirm=True)
            sent += 1
    assert "aggregate on-chain notional" in caught.value.reason
    assert sent == 4, f"$1,000 cap must stop the 5th $250 swap, not let {sent + 1} through"
    assert len(wallet.swaps) == 4
    await engine.stop()


async def test_x402_payments_consume_the_same_aggregate_cap(tmp_path):
    engine, _wallet = armed_engine(
        tmp_path, DELTR_ONCHAIN_MAX_AGGREGATE_USD=1_000.0, DELTR_ONCHAIN_MAX_NOTIONAL_USD=250.0,
        DELTR_X402_MAX_PAYMENT_UNITS=250.0,
    )
    signed: list[int] = []

    class FakeBuyer:
        asset_decimals = 18

        async def pay(self, parsed, selected_index=None):
            signed.append(1)
            return X402Payment(payment_id="p", selected_index=0, option=parsed.options[0],
                               header_name="X-PAYMENT", header_value="hdr", raw={})

    engine.x402_buyer = FakeBuyer()
    challenge = json.dumps({"x402Version": 1, "accepts": [{
        "scheme": "exact", "network": "bsc", "maxAmountRequired": str(200 * 10 ** 18),
        "payTo": "0x" + "d" * 40, "asset": "0x" + "c" * 40, "resource": "https://x/y",
        "description": "d", "mimeType": "application/json", "maxTimeoutSeconds": 60}]})
    with pytest.raises(OnchainRefused) as caught:
        for _ in range(10):
            await engine.x402_pay(challenge, confirm=True)
    assert "aggregate on-chain notional" in caught.value.reason
    assert len(signed) == 5, f"$1,000 cap must stop the 6th $200 payment, not let {len(signed) + 1} through"
    await engine.stop()


async def test_a_charge_is_returned_only_when_nothing_was_signed(tmp_path):
    engine, wallet = armed_engine(tmp_path, DELTR_ONCHAIN_MAX_AGGREGATE_USD=1_000.0)
    # the wallet refused at preview: provably nothing submitted, so the charge comes back
    wallet.outcome = "declined"
    with pytest.raises(AgenticWalletError):
        await engine.onchain_swap(from_token="0xA", to_token="0xB", amount=1.0, notional_usd=250.0, confirm=True)
    assert engine._onchain_notional_usd == 0.0

    # an unmapped failure is NOT proof: the charge stays, because the swap may be on chain
    async def timeout_swap(*a: Any, **k: Any):
        raise AgenticWalletError("BAW_TIMEOUT", "the CLI did not answer in time")

    wallet.swap = timeout_swap  # type: ignore[method-assign]
    with pytest.raises(AgenticWalletError):
        await engine.onchain_swap(from_token="0xA", to_token="0xB", amount=1.0, notional_usd=250.0, confirm=True)
    assert engine._onchain_notional_usd == 250.0
    await engine.stop()


async def test_a_policy_refusal_of_an_x402_payment_returns_the_charge(tmp_path):
    engine, _wallet = armed_engine(tmp_path, DELTR_ONCHAIN_MAX_AGGREGATE_USD=1_000.0)

    class RefusingBuyer:
        asset_decimals = 18

        async def pay(self, parsed, selected_index=None):
            raise PaymentRefused("X402_AMOUNT_OVER_CAP", "over the per-payment ceiling")

    engine.x402_buyer = RefusingBuyer()
    challenge = json.dumps({"x402Version": 1, "accepts": [{
        "scheme": "exact", "network": "bsc", "maxAmountRequired": str(10 * 10 ** 18),
        "payTo": "0x" + "d" * 40, "asset": "0x" + "c" * 40, "resource": "https://x/y",
        "description": "d", "mimeType": "application/json", "maxTimeoutSeconds": 60}]})
    with pytest.raises(PaymentRefused):
        await engine.x402_pay(challenge, confirm=True)
    assert engine._onchain_notional_usd == 0.0, "Deltr's own policy refused before anything was signed"
    await engine.stop()


# --------------------------------------------------------------------------- caps never block a close
async def test_an_exhausted_aggregate_cap_never_blocks_a_reversal(tmp_path):
    """A cap that refuses the swap which REMOVES an exposure turns a stop into a naked leg."""
    from deltr.models import OrderLeg, Side, Venue
    from deltr.onchain_leg import WalletDexLeg
    from tests._fakes_d import make_market

    engine, wallet = armed_engine(tmp_path, mode="live", DELTR_ONCHAIN_MAX_AGGREGATE_USD=250.0)
    leg = WalletDexLeg(wallet, engine.settings, quoter=FakeQuoter(), guard=engine._leg_guard, chain_id=56)
    ms = make_market()
    opening = OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.BUY, qty=0.3, price_hint=686.0)
    entry = await leg.fill(opening, ms, 0.3, "plan-1", 1)
    assert engine._onchain_notional_usd > 0.0
    reversal = await leg.reverse(entry, ms, "plan-1")           # must not raise
    assert reversal.qty == pytest.approx(entry.qty)
    assert engine._onchain_notional_usd == pytest.approx(0.0), "a close gives its notional back"
    await engine.stop()


# --------------------------------------------------------------------------- honest banner
async def test_the_paper_banner_never_hides_an_armed_on_chain_leg(tmp_path):
    engine, _wallet = armed_engine(tmp_path)
    line = deltr_main._armed_line(engine.settings, engine.status())
    assert "ON-CHAIN LEG IS ARMED" in line and "REAL funds" in line
    assert "no funds are at risk" not in line
    await engine.stop()

    off = Settings(_env_file=None, DELTR_MODE="paper", DELTR_STATE_DIR=str(tmp_path))  # type: ignore[arg-type]
    plain = build_engine(off, http=_http())
    line = deltr_main._armed_line(off, plain.status())
    assert "no funds are at risk" in line and "ARMED" not in line
    await plain.stop()


async def test_status_reports_the_on_chain_arming_in_every_mode(tmp_path):
    engine, _wallet = armed_engine(tmp_path)
    st = engine.status()
    assert st.mode is Mode.PAPER and st.real_funds_armed is False
    assert st.onchain_armed is True, "deltr_status must not present an armed wallet as unarmed"
    await engine.stop()


async def test_a_mainnet_host_cannot_be_disguised_as_a_testnet_url(tmp_path):
    """The credential guard reads the HOSTNAME, not the URL string.

    ``https://fapi.binance.com/testnet`` contains the word "testnet" and used to satisfy a
    substring check, which would have handed a credentialed client the mainnet order host with
    no ``allow_mainnet_orders`` and no host assertion behind it.
    """
    http = _http()
    for disguised in (
        "https://fapi.binance.com/testnet",
        "https://fapi.binance.com/?x=testnet",
        "https://fapi.binance.com#testnet",
    ):
        with pytest.raises(ValueError, match="allow_mainnet_orders"):
            FuturesClient(http, disguised, api_key="k", secret_key="s")
    await http.aclose()


async def test_no_mcp_tool_or_rest_route_can_change_the_mode(tmp_path):
    """Mode is chosen once at launch. Nothing on either surface accepts it as an argument."""
    from deltr.api.routes import router as api_router
    from deltr.mcp.server import build_mcp

    engine, _wallet = armed_engine(tmp_path)
    mcp = build_mcp(engine, engine.activity)
    tools = await mcp.list_tools()
    for tool in tools:
        params = set((tool.inputSchema or {}).get("properties", {}))
        assert not (params & {"mode", "deltr_mode", "live", "arm", "real_funds_armed", "onchain_mode"}), (
            f"{tool.name} accepts a mode-shaped argument: {sorted(params)}"
        )
    bodies = " ".join(str(r.endpoint.__annotations__) for r in api_router.routes if hasattr(r, "endpoint"))
    assert "Mode" not in bodies, "no REST body may carry a Mode"
    assert engine.settings.model_config.get("frozen") is True
    await engine.stop()


async def test_a_decimals_mismatch_cannot_slip_a_payment_past_every_ceiling():
    """200 USDC at 6 decimals read as 18 is 0.0000002 units: under every cap, charged as nothing."""
    from deltr.payments import PaymentOption, X402Buyer, X402Preview

    buyer = X402Buyer(wallet=None, max_amount_units=1.0)  # type: ignore[arg-type]
    def option(atomic: int) -> PaymentOption:
        return PaymentOption(index=0, scheme="exact", network="bsc", asset="0x" + "c" * 40,
                             amount_atomic=str(atomic), pay_to="0x" + "d" * 40,
                             resource="https://x/y", description="d")

    six_dec = option(200 * 10 ** 6)
    with pytest.raises(PaymentRefused, match="almost certainly a different decimals count"):
        buyer.select(X402Preview(payment_id="p", options=(six_dec,), raw={}))

    # the same amount stated at the 18 decimals BSC USDT actually uses is refused on the CEILING,
    # which is the honest reason, not on the decimals
    eighteen = option(200 * 10 ** 18)
    with pytest.raises(PaymentRefused, match="per-payment ceiling"):
        buyer.select(X402Preview(payment_id="p", options=(eighteen,), raw={}))
