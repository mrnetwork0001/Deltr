"""EVM ABI encoding/decoding + PancakeV3Client behaviour (offline via httpx.MockTransport).

Golden numbers come from the 2026-09-02 on-chain probe recorded in DESIGN_FINAL.md / RESEARCH_FACTS.md:
sqrtPriceX96 3026198540864043993837911404 → 685.4319 USDT/BNB; fee-100 pool mid 686.10 at
sqrtPriceX96 3024721431835227127309627620; buying 4.86 WBNB costs 3,334.888 USDT (exec 686.191, gas 162,878);
selling 4.86 WBNB yields 3,334.22 USDT (exec 686.053, gas 139,006).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Callable

import httpx
import pytest

from deltr.models import DataSource, DexQuote
from deltr.venues import pancakeswap_v3 as pv3
from deltr.venues.pancake_constants import MAINNET, SELECTORS, bnb_usdt_from_sqrt_price
from deltr.venues.pancakeswap_v3 import (
    SELECTOR_QUOTE_EXACT_INPUT_SINGLE,
    SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE,
    SELECTOR_SLOT0,
    PancakeV3Client,
    RpcError,
    address_word,
    decode_quote_result,
    decode_words,
    encode_call,
    encode_quote_single,
    encode_word,
    sqrt_price_to_usdt_per_bnb,
    to_signed,
)

# --------------------------------------------------------------------------- fixtures (probe 2026-09-02)
SQRT_GOLDEN = 3026198540864043993837911404  # → 685.4319
SQRT_MID = 3024721431835227127309627620  # → 686.10 (fee-100 pool at probe)
TICK_MID = -65_314  # floor(log(1/686.10)/log(1.0001))
SIZE = 4.86
AMOUNT_IN_WEI = 3_334_888_000_000_000_000_000  # 3,334.888 USDT for 4.86 WBNB (exact-output)
AMOUNT_OUT_WEI = 3_334_220_000_000_000_000_000  # 3,334.22 USDT from 4.86 WBNB (exact-input)
GAS_BUY, GAS_SELL = 162_878, 139_006
GAS_PRICE_WEI = 50_000_000  # 0.05 gwei
BLOCK = 119_456_920


def words(*vals: int) -> str:
    return "0x" + "".join(encode_word(v) for v in vals)


SLOT0_RESULT = words(SQRT_MID, TICK_MID, 12, 1000, 1000, 0, 1)
EXACT_OUTPUT_RESULT = words(AMOUNT_IN_WEI, SQRT_MID - 10**20, 1, GAS_BUY)
EXACT_INPUT_RESULT = words(AMOUNT_OUT_WEI, SQRT_MID + 10**20, 1, GAS_SELL)


class FakeRpc:
    """Deterministic JSON-RPC node behind httpx.MockTransport. Failures configurable per host."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (host, method, selector-or-"")
        self.fail_hosts: dict[str, Callable[[], httpx.Response | Exception]] = {}
        self.slot0 = SLOT0_RESULT
        self.exact_output = EXACT_OUTPUT_RESULT
        self.exact_input = EXACT_INPUT_RESULT
        self.revert_quotes = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        selector = body["params"][0]["data"][:10] if method == "eth_call" else ""
        self.calls.append((request.url.host, method, selector))
        if request.url.host in self.fail_hosts:
            out = self.fail_hosts[request.url.host]()
            if isinstance(out, Exception):
                raise out
            return out
        rid = body["id"]
        if method == "eth_blockNumber":
            return self._ok(rid, hex(BLOCK))
        if method == "eth_gasPrice":
            return self._ok(rid, hex(GAS_PRICE_WEI))
        if method == "eth_call":
            to = body["params"][0]["to"].lower()
            data = body["params"][0]["data"]
            assert body["params"][1] == "latest"
            if selector == SELECTOR_SLOT0:
                assert to == MAINNET.pools_wbnb_usdt[100].lower()
                return self._ok(rid, self.slot0)
            if selector in (SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE, SELECTOR_QUOTE_EXACT_INPUT_SINGLE):
                assert to == MAINNET.quoter_v2.lower()
                assert len(data) == 2 + 8 + 5 * 64, "QuoterV2 tuple must be 5 inline static words (no offset word)"
                if self.revert_quotes:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "error": {"code": 3, "message": "execution reverted"}})
                return self._ok(rid, self.exact_output if selector == SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE else self.exact_input)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})

    @staticmethod
    def _ok(rid: Any, result: str) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rpc() -> FakeRpc:
    return FakeRpc()


@pytest.fixture
def urls() -> tuple[str, ...]:
    return ("https://rpc-a.example/", "https://rpc-b.example/", "https://rpc-c.example/")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def client(rpc: FakeRpc, urls: tuple[str, ...], clock: FakeClock):
    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc.handler)) as http:
        yield PancakeV3Client(http, urls, chain=MAINNET, fee_tier=100, cache_ttl_s=2.0, clock=clock)


# --------------------------------------------------------------------------- words / selectors
def test_encode_word_pads_and_handles_negative():
    assert encode_word(0) == "0" * 64
    assert encode_word(1) == "0" * 63 + "1"
    assert encode_word(-1) == "f" * 64
    assert len(encode_word(2**256 - 1)) == 64
    with pytest.raises(ValueError):
        encode_word(2**256)


def test_address_word_and_encode_call_layout():
    usdt = address_word(MAINNET.usdt)
    assert usdt == int(MAINNET.usdt, 16)
    data = encode_call(SELECTOR_SLOT0)
    assert data == "0x3850c7bd"
    data = encode_quote_single(SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE, MAINNET.usdt, MAINNET.wbnb, 10**18, 100)
    assert data.startswith("0xbd21704a")
    assert len(data) == 2 + 8 + 5 * 64  # selector + 5 static words, NO tuple offset word
    body = data[10:]
    assert body[0:64] == MAINNET.usdt[2:].lower().rjust(64, "0")
    assert body[64:128] == MAINNET.wbnb[2:].lower().rjust(64, "0")
    assert int(body[128:192], 16) == 10**18
    assert int(body[192:256], 16) == 100
    assert int(body[256:320], 16) == 0  # sqrtPriceLimitX96 = 0
    with pytest.raises(ValueError):
        address_word("0x1234")
    with pytest.raises(ValueError):
        encode_call("0x12")


def test_selectors_match_verified_values():
    assert SELECTOR_SLOT0 == "0x3850c7bd" == SELECTORS["slot0"]
    assert pv3.SELECTOR_LIQUIDITY == "0x1a686502" == SELECTORS["liquidity"]
    assert SELECTOR_QUOTE_EXACT_INPUT_SINGLE == "0xc6a5026a" == SELECTORS["quoteExactInputSingle"]
    assert SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE == "0xbd21704a"


def test_decode_words_and_signed_int24():
    assert decode_words("0x") == []
    assert decode_words(words(1, 2, 3)) == [1, 2, 3]
    assert to_signed((1 << 256) - 65_314, 24) == -65_314  # ABI sign-extended int24
    assert to_signed((1 << 24) - 1, 24) == -1  # raw 24-bit two's complement
    assert to_signed(12_345, 24) == 12_345
    sqrt, tick, *_ = decode_words(SLOT0_RESULT)
    assert sqrt == SQRT_MID and to_signed(tick, 24) == TICK_MID


def test_sqrt_price_goldens():
    assert math.isclose(sqrt_price_to_usdt_per_bnb(SQRT_GOLDEN), 685.4319, rel_tol=1e-6)
    assert math.isclose(bnb_usdt_from_sqrt_price(SQRT_MID), 686.10, rel_tol=1e-4)
    assert math.isclose(sqrt_price_to_usdt_per_bnb(SQRT_MID), bnb_usdt_from_sqrt_price(SQRT_MID))
    with pytest.raises(ValueError):
        sqrt_price_to_usdt_per_bnb(0)


def test_decode_probed_exact_output_response():
    amount_in, sqrt_after, ticks, gas = decode_quote_result(EXACT_OUTPUT_RESULT)
    assert math.isclose(amount_in / 1e18, 3334.888, rel_tol=1e-9)
    assert math.isclose(amount_in / 1e18 / SIZE, 686.191, rel_tol=1e-5)
    assert ticks == 1 and gas == GAS_BUY and sqrt_after > 0
    with pytest.raises(RpcError):
        decode_quote_result("0x")  # a revert comes back as empty data


# --------------------------------------------------------------------------- client behaviour
async def test_slot0_and_quotes_roundtrip(client: PancakeV3Client, rpc: FakeRpc):
    sqrt, tick = await client.slot0(client.pool)
    assert sqrt == SQRT_MID and tick == TICK_MID
    ain, _, _, gas = await client.quote_exact_output_single(MAINNET.usdt, MAINNET.wbnb, int(SIZE * 1e18), 100)
    assert ain == AMOUNT_IN_WEI and gas == GAS_BUY
    aout, _, _, gas = await client.quote_exact_input_single(MAINNET.wbnb, MAINNET.usdt, int(SIZE * 1e18), 100)
    assert aout == AMOUNT_OUT_WEI and gas == GAS_SELL
    assert await client.block_number() == BLOCK
    assert await client.gas_price_wei() == GAS_PRICE_WEI
    assert all(host == "rpc-a.example" for host, _, _ in rpc.calls)


async def test_dex_quote_matches_probe_arithmetic(client: PancakeV3Client):
    q = await client.dex_quote(SIZE)
    assert isinstance(q, DexQuote)
    assert q.pool == MAINNET.pools_wbnb_usdt[100] and q.fee_tier == 100 and q.fee_bps == 1.0
    assert q.sqrt_price_x96 == SQRT_MID and q.tick == TICK_MID
    assert math.isclose(q.mid_price, 686.10, rel_tol=1e-4)
    assert math.isclose(q.exec_price_buy, 686.191, rel_tol=1e-5)  # exact-OUTPUT USDT→WBNB
    assert math.isclose(q.exec_price_sell, 686.053, rel_tol=1e-5)  # exact-INPUT  WBNB→USDT
    assert math.isclose(q.amount_in_usdt, 3334.888) and math.isclose(q.amount_out_usdt, 3334.22)
    assert q.exec_price_sell < q.mid_price < q.exec_price_buy
    expected_impact = max(0.0, 1e4 * (q.exec_price_buy / q.mid_price - 1) - q.fee_bps)
    assert math.isclose(q.impact_bps, expected_impact) and 0.0 < q.impact_bps < 1.0
    assert q.gas_units == max(GAS_BUY, GAS_SELL) and q.gas_price_wei == GAS_PRICE_WEI
    assert math.isclose(q.gas_usd, GAS_BUY * GAS_PRICE_WEI / 1e18 * q.mid_price)
    assert q.gas_usd < 0.01  # ≈ $0.0056 per swap at 0.05 gwei
    assert q.block == BLOCK and q.source == DataSource.BSC_MAINNET_CHAIN and q.size_base == SIZE


async def test_dex_quote_cache_per_size_and_ttl(client: PancakeV3Client, rpc: FakeRpc, clock: FakeClock):
    q1 = await client.dex_quote(SIZE)
    n = len(rpc.calls)
    assert n == 5  # slot0 + 2 quotes + gasPrice + blockNumber
    q2 = await client.dex_quote(SIZE)
    assert q2 is q1 and len(rpc.calls) == n  # cache hit, no RPC
    await client.dex_quote(1.0)
    assert len(rpc.calls) == n + 4  # different size → new quote (gas price still cached 30 s)
    clock.t += 2.5  # past the 2 s ttl
    q3 = await client.dex_quote(SIZE)
    assert q3 is not q1 and len(rpc.calls) == n + 8
    clock.t += 31
    await client.dex_quote(SIZE)
    assert rpc.calls[-2][1] == "eth_gasPrice"  # gas price cache expired after 30 s
    with pytest.raises(ValueError):
        await client.dex_quote(0)


async def test_rpc_failover_then_sticky_preference(client: PancakeV3Client, rpc: FakeRpc):
    rpc.fail_hosts["rpc-a.example"] = lambda: httpx.ConnectError("dns")
    assert await client.block_number() == BLOCK
    assert [h for h, _, _ in rpc.calls] == ["rpc-a.example", "rpc-b.example"]
    assert client.active_rpc_url == "https://rpc-b.example/"
    rpc.calls.clear()
    await client.block_number()
    assert [h for h, _, _ in rpc.calls] == ["rpc-b.example"]  # sticks to the last good node


async def test_rpc_failover_on_http_and_node_errors(client: PancakeV3Client, rpc: FakeRpc):
    rpc.fail_hosts["rpc-a.example"] = lambda: httpx.Response(429, text="rate limited")
    rpc.fail_hosts["rpc-b.example"] = lambda: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit exceeded"}}
    )
    assert await client.block_number() == BLOCK
    assert [h for h, _, _ in rpc.calls] == ["rpc-a.example", "rpc-b.example", "rpc-c.example"]


async def test_rpc_error_after_all_endpoints_fail(client: PancakeV3Client, rpc: FakeRpc):
    for h in ("rpc-a.example", "rpc-b.example", "rpc-c.example"):
        rpc.fail_hosts[h] = lambda: httpx.ReadTimeout("slow")
    with pytest.raises(RpcError) as ei:
        await client.block_number()
    assert "all 3 RPC endpoints failed" in str(ei.value)
    health = await client.probe()
    assert health.ok is False and health.name == "pancakeswap_v3" and "failed" in health.detail


async def test_revert_does_not_failover(client: PancakeV3Client, rpc: FakeRpc):
    rpc.revert_quotes = True
    with pytest.raises(RpcError) as ei:
        await client.quote_exact_input_single(MAINNET.wbnb, MAINNET.usdt, 10**18, 100)
    assert ei.value.is_revert
    assert [h for h, _, _ in rpc.calls] == ["rpc-a.example"]  # deterministic revert: no retry elsewhere


async def test_probe_reports_block_and_mid(client: PancakeV3Client):
    h = await client.probe()
    assert h.ok and h.age_ms == 0 and h.source == DataSource.BSC_MAINNET_CHAIN
    assert f"block {BLOCK}" in h.detail and "686.10" in h.detail


def test_constructor_validation():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError):
        PancakeV3Client(http, ())
    with pytest.raises(ValueError):
        PancakeV3Client(http, ("https://x/",), fee_tier=333)


# --------------------------------------------------------------------------- live (opt-in)
@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DELTR_LIVE_TESTS") != "1", reason="set DELTR_LIVE_TESTS=1 to hit BSC mainnet RPC")
async def test_live_bsc_mainnet_quote():
    from deltr.venues.pancake_constants import BSC_MAINNET_RPCS

    async with httpx.AsyncClient(timeout=15.0) as http:
        c = PancakeV3Client(http, BSC_MAINNET_RPCS, chain=MAINNET, fee_tier=100)
        q = await c.dex_quote(SIZE)
        assert 50 < q.mid_price < 50_000
        assert q.exec_price_sell < q.mid_price < q.exec_price_buy
        assert 0 <= q.impact_bps < 50 and q.gas_units > 50_000 and q.block and q.block > BLOCK
        assert (await c.probe()).ok
