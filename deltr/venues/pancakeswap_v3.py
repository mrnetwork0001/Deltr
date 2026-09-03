"""
PancakeSwap V3 read-only client over raw JSON-RPC ``eth_call`` (no web3).

Everything Deltr needs from the chain is two static-word calls per tick:

* ``slot0()`` on the WBNB/USDT pool → ``sqrtPriceX96`` (mid) and ``tick``;
* ``QuoterV2.quoteExactOutputSingle`` (USDT→WBNB, *buy* ``size`` WBNB) and
  ``QuoterV2.quoteExactInputSingle`` (WBNB→USDT, *sell* ``size`` WBNB) →
  executable prices incl. the pool fee, plus the router's ``gasEstimate``;
* ``eth_gasPrice`` / ``eth_blockNumber`` for the gas leg and provenance.

Calldata is hand-encoded: selector + N × 32-byte words (addresses and
uints left-padded).  The QuoterV2 parameter is a tuple of five *static* fields
(tokenIn, tokenOut, amount, fee, sqrtPriceLimitX96) so there is **no** offset
word — the tuple is laid out inline (verified on-chain 2026-09-02).

RPC failover: ``rpc_urls`` are tried in order starting from the last endpoint
that answered; transport / HTTP / node errors move to the next URL, a
deterministic EVM revert does not (all nodes would revert alike).  Quotes are
cached per (size, ttl) so a 1 Hz engine loop does not hammer public nodes.

Never prints to stdout (stdio MCP transport lives there); logs to stderr.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from deltr.models import DataSource, DexQuote, VenueHealth
from deltr.venues.pancake_constants import MAINNET, Q192, SELECTORS, ChainAddresses, fee_tier_to_bps

log = logging.getLogger("deltr.venues.pancakeswap_v3")

# --------------------------------------------------------------------------- #
# Selectors (keccak-confirmed; exact-output executed on-chain 2026-09-02)      #
# --------------------------------------------------------------------------- #
SELECTOR_SLOT0: str = SELECTORS["slot0"]  # 0x3850c7bd
SELECTOR_LIQUIDITY: str = SELECTORS["liquidity"]  # 0x1a686502
SELECTOR_QUOTE_EXACT_INPUT_SINGLE: str = SELECTORS["quoteExactInputSingle"]  # 0xc6a5026a
# quoteExactOutputSingle((address,address,uint256,uint24,uint160)) — missing from pancake_constants.SELECTORS
SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE: str = "0xbd21704a"

WORD_HEX_LEN = 64  # 32 bytes
WEI = 10**18
GAS_PRICE_CACHE_S = 30.0
MAX_CACHE_ENTRIES = 16
DEFAULT_USER_AGENT = "deltr/1.0 (+https://github.com/mrnetwork0001/Deltr)"


# --------------------------------------------------------------------------- #
# ABI word helpers                                                             #
# --------------------------------------------------------------------------- #
def address_word(addr: str) -> int:
    """Integer value of a 20-byte hex address (checksum case ignored)."""
    h = addr[2:] if addr.startswith(("0x", "0X")) else addr
    if len(h) != 40:
        raise ValueError(f"not a 20-byte address: {addr!r}")
    return int(h, 16)


def encode_word(value: int) -> str:
    """One 32-byte ABI word (unsigned, left-padded) without the 0x prefix."""
    if value < 0:
        value += 1 << 256  # two's complement for signed ints
    if value >= 1 << 256:
        raise ValueError("word overflows 256 bits")
    return f"{value:064x}"


def encode_call(selector: str, *words: int) -> str:
    """``0x`` + 4-byte selector + static 32-byte words (uint / address left-padded)."""
    sel = selector[2:] if selector.startswith(("0x", "0X")) else selector
    if len(sel) != 8:
        raise ValueError(f"selector must be 4 bytes: {selector!r}")
    return "0x" + sel.lower() + "".join(encode_word(w) for w in words)


def decode_words(hexdata: str) -> list[int]:
    """Split an ABI return blob into unsigned 256-bit words (partial tail word ignored)."""
    h = hexdata[2:] if hexdata.startswith(("0x", "0X")) else hexdata
    if h == "":
        return []
    return [int(h[i : i + WORD_HEX_LEN], 16) for i in range(0, len(h) - len(h) % WORD_HEX_LEN, WORD_HEX_LEN)]


def to_signed(word: int, bits: int = 256) -> int:
    """Interpret an unsigned ABI word as a two's-complement ``int<bits>`` (e.g. the ``int24`` tick).

    The ABI sign-extends small ints to 256 bits; masking to ``bits`` first makes both the
    sign-extended and the raw ``bits``-wide encodings decode identically."""
    if not 1 <= bits <= 256:
        raise ValueError("bits must be in 1..256")
    word &= (1 << bits) - 1
    return word - (1 << bits) if word >= 1 << (bits - 1) else word


def sqrt_price_to_usdt_per_bnb(sqrt_price_x96: int) -> float:
    """Mid price for the WBNB/USDT pools: token0 = USDT, token1 = WBNB, both 18 dec ⇒ 2^192 / sqrtP²."""
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrtPriceX96 must be > 0")
    return float(Q192 / (sqrt_price_x96 * sqrt_price_x96))


def encode_quote_single(selector: str, token_in: str, token_out: str, amount_wei: int, fee: int) -> str:
    """QuoterV2 single-hop calldata: 5 inline static words, ``sqrtPriceLimitX96 = 0`` (no limit)."""
    if amount_wei <= 0:
        raise ValueError("amount must be > 0")
    return encode_call(selector, address_word(token_in), address_word(token_out), amount_wei, fee, 0)


def decode_quote_result(hexdata: str) -> tuple[int, int, int, int]:
    """(amount, sqrtPriceX96After, initializedTicksCrossed, gasEstimate) from a QuoterV2 return blob."""
    words = decode_words(hexdata)
    if len(words) < 4:
        raise RpcError(f"QuoterV2 returned {len(words)} words, expected 4 (empty return usually means a revert)")
    return words[0], words[1], words[2], words[3]


# --------------------------------------------------------------------------- #
# Client                                                                       #
# --------------------------------------------------------------------------- #
class RpcError(Exception):
    """JSON-RPC / transport failure after exhausting every configured endpoint (or an EVM revert)."""

    def __init__(self, message: str, code: Optional[int] = None, url: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.url = url

    @property
    def is_revert(self) -> bool:
        return "revert" in self.message.lower() or self.code == 3


def _is_revert(err: dict[str, Any]) -> bool:
    msg = str(err.get("message", "")).lower()
    return "revert" in msg or err.get("code") == 3


class PancakeV3Client:
    """Async PancakeSwap V3 quoting over ``eth_call`` with RPC failover and a per-size quote cache."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        rpc_urls: tuple[str, ...],
        chain: ChainAddresses = MAINNET,
        fee_tier: int = 100,
        cache_ttl_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not rpc_urls:
            raise ValueError("at least one RPC url is required")
        if fee_tier not in chain.pools_wbnb_usdt:
            raise ValueError(f"no WBNB/USDT pool for fee tier {fee_tier} on chain {chain.chain_id}")
        self.http = http
        self.rpc_urls: tuple[str, ...] = tuple(rpc_urls)
        self.chain = chain
        self.fee_tier = fee_tier
        self.cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._preferred = 0  # index of the last RPC that answered
        self._req_id = 0
        self._quote_cache: dict[float, tuple[float, DexQuote]] = {}
        self._gas_price_cache: Optional[tuple[float, int]] = None
        self._last_ok_at: Optional[float] = None
        self._last_url: Optional[str] = None
        self._last_error: Optional[str] = None

    # ---- properties --------------------------------------------------------
    @property
    def pool(self) -> str:
        return self.chain.pools_wbnb_usdt[self.fee_tier]

    @property
    def fee_bps(self) -> float:
        return fee_tier_to_bps(self.fee_tier)

    @property
    def active_rpc_url(self) -> str:
        return self.rpc_urls[self._preferred]

    # ---- raw JSON-RPC with failover ----------------------------------------
    def _ordered_urls(self) -> list[tuple[int, str]]:
        n = len(self.rpc_urls)
        return [((self._preferred + i) % n, self.rpc_urls[(self._preferred + i) % n]) for i in range(n)]

    async def rpc(self, method: str, params: list[Any]) -> Any:
        """Call ``method`` on the first endpoint that answers; ``RpcError`` after all fail or on revert."""
        last_err: Optional[str] = None
        for idx, url in self._ordered_urls():
            self._req_id += 1
            body = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
            try:
                r = await self.http.post(url, json=body, headers={"User-Agent": DEFAULT_USER_AGENT})
                if r.status_code != 200:
                    raise RpcError(f"HTTP {r.status_code}", url=url)
                payload = r.json()
            except RpcError as e:
                last_err = str(e)
            except (httpx.HTTPError, ValueError) as e:  # transport, timeout, non-JSON body
                last_err = f"{type(e).__name__}: {e}"
            else:
                err = payload.get("error") if isinstance(payload, dict) else None
                if err:
                    if _is_revert(err):
                        self._last_error = f"revert via {url}: {err.get('message')}"
                        raise RpcError(str(err.get("message", "execution reverted")), code=err.get("code"), url=url)
                    last_err = f"rpc error {err.get('code')}: {err.get('message')}"
                elif isinstance(payload, dict) and "result" in payload:
                    self._preferred = idx
                    self._last_ok_at = self._clock()
                    self._last_url = url
                    return payload["result"]
                else:
                    last_err = "malformed JSON-RPC response"
            log.warning("rpc %s failed on %s: %s", method, url, last_err)
        self._last_error = last_err
        raise RpcError(f"{method}: all {len(self.rpc_urls)} RPC endpoints failed (last: {last_err})")

    async def eth_call(self, to: str, data: str) -> str:
        """``eth_call`` against ``latest``; returns the raw ``0x…`` hex result."""
        result = await self.rpc("eth_call", [{"to": to, "data": data}, "latest"])
        if not isinstance(result, str):
            raise RpcError("eth_call returned a non-string result")
        return result

    async def block_number(self) -> int:
        return int(await self.rpc("eth_blockNumber", []), 16)

    async def gas_price_wei(self) -> int:
        """Node gas price in wei, cached 30 s (BSC gas is flat; this only feeds a ~0.02 bps cost)."""
        now = self._clock()
        if self._gas_price_cache and now - self._gas_price_cache[0] < GAS_PRICE_CACHE_S:
            return self._gas_price_cache[1]
        gp = int(await self.rpc("eth_gasPrice", []), 16)
        self._gas_price_cache = (now, gp)
        return gp

    # ---- pool / quoter views ------------------------------------------------
    async def slot0(self, pool: str) -> tuple[int, int]:
        """(sqrtPriceX96, tick) — selector 0x3850c7bd."""
        words = decode_words(await self.eth_call(pool, encode_call(SELECTOR_SLOT0)))
        if len(words) < 2:
            raise RpcError(f"slot0 returned {len(words)} words (pool missing?)")
        return words[0], to_signed(words[1], 24)

    async def liquidity(self, pool: str) -> int:
        """In-range liquidity (uint128) — selector 0x1a686502."""
        words = decode_words(await self.eth_call(pool, encode_call(SELECTOR_LIQUIDITY)))
        if not words:
            raise RpcError("liquidity() returned no data")
        return words[0]

    async def quote_exact_output_single(
        self, token_in: str, token_out: str, amount_out_wei: int, fee: int
    ) -> tuple[int, int, int, int]:
        """0xbd21704a → (amountIn, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)."""
        data = encode_quote_single(SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE, token_in, token_out, amount_out_wei, fee)
        return decode_quote_result(await self.eth_call(self.chain.quoter_v2, data))

    async def quote_exact_input_single(
        self, token_in: str, token_out: str, amount_in_wei: int, fee: int
    ) -> tuple[int, int, int, int]:
        """0xc6a5026a → (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)."""
        data = encode_quote_single(SELECTOR_QUOTE_EXACT_INPUT_SINGLE, token_in, token_out, amount_in_wei, fee)
        return decode_quote_result(await self.eth_call(self.chain.quoter_v2, data))

    # ---- the one call the engine uses --------------------------------------
    async def dex_quote(self, size_base: float) -> DexQuote:
        """Executable two-sided quote for ``size_base`` WBNB (cached per size for ``cache_ttl_s``).

        * ``exec_price_buy``  = amountIn(USDT) / size  from exact-OUTPUT (USDT→WBNB)
        * ``exec_price_sell`` = amountOut(USDT) / size from exact-INPUT  (WBNB→USDT)
        * ``impact_bps``      = max(0, 1e4·(exec_buy/mid − 1) − fee_bps)  (fee never counted twice)
        * ``gas_usd``         = max(gasEstimate) × eth_gasPrice × mid
        """
        if size_base <= 0:
            raise ValueError("size_base must be > 0")
        key = round(float(size_base), 8)
        now = self._clock()
        cached = self._quote_cache.get(key)
        if cached and now - cached[0] < self.cache_ttl_s:
            return cached[1]

        pool = self.pool
        sqrt_p, tick = await self.slot0(pool)
        mid = sqrt_price_to_usdt_per_bnb(sqrt_p)
        size_wei = int(round(size_base * WEI))
        amount_in, _, _, gas_buy = await self.quote_exact_output_single(
            self.chain.usdt, self.chain.wbnb, size_wei, self.fee_tier
        )
        amount_out, _, _, gas_sell = await self.quote_exact_input_single(
            self.chain.wbnb, self.chain.usdt, size_wei, self.fee_tier
        )
        gas_price = await self.gas_price_wei()
        block = await self.block_number()

        amount_in_usdt = amount_in / 10**self.chain.usdt_decimals
        amount_out_usdt = amount_out / 10**self.chain.usdt_decimals
        exec_buy = amount_in_usdt / size_base
        exec_sell = amount_out_usdt / size_base
        gas_units = max(gas_buy, gas_sell)
        quote = DexQuote(
            pool=pool,
            fee_tier=self.fee_tier,
            fee_bps=self.fee_bps,
            sqrt_price_x96=sqrt_p,
            tick=tick,
            mid_price=mid,
            size_base=float(size_base),
            exec_price_buy=exec_buy,
            amount_in_usdt=amount_in_usdt,
            exec_price_sell=exec_sell,
            amount_out_usdt=amount_out_usdt,
            impact_bps=max(0.0, (exec_buy / mid - 1.0) * 1e4 - self.fee_bps),
            gas_units=gas_units,
            gas_price_wei=gas_price,
            gas_usd=gas_units * gas_price / WEI * mid,
            block=block,
            ts=datetime.now(timezone.utc),
            source=DataSource.BSC_MAINNET_CHAIN,
        )
        if len(self._quote_cache) >= MAX_CACHE_ENTRIES:
            oldest = min(self._quote_cache, key=lambda k: self._quote_cache[k][0])
            self._quote_cache.pop(oldest, None)
        self._quote_cache[key] = (now, quote)
        log.debug("dex_quote size=%s mid=%.4f buy=%.4f sell=%.4f impact=%.3fbps block=%s", size_base, mid, exec_buy, exec_sell, quote.impact_bps, block)
        return quote

    def invalidate_cache(self) -> None:
        self._quote_cache.clear()
        self._gas_price_cache = None

    async def probe(self) -> VenueHealth:
        """One ``slot0`` + ``eth_blockNumber`` round trip; never raises."""
        t0 = self._clock()
        try:
            sqrt_p, _ = await self.slot0(self.pool)
            block = await self.block_number()
        except RpcError as e:
            age = int((self._clock() - self._last_ok_at) * 1000) if self._last_ok_at is not None else -1
            return VenueHealth(name="pancakeswap_v3", ok=False, age_ms=max(age, 0), source=DataSource.BSC_MAINNET_CHAIN, detail=str(e))
        mid = sqrt_price_to_usdt_per_bnb(sqrt_p)
        latency_ms = int((self._clock() - t0) * 1000)
        return VenueHealth(
            name="pancakeswap_v3",
            ok=True,
            age_ms=0,
            source=DataSource.BSC_MAINNET_CHAIN,
            detail=f"block {block} mid {mid:.2f} USDT/BNB fee{self.fee_tier} via {self._last_url} ({latency_ms} ms)",
        )


__all__ = [
    "SELECTOR_SLOT0", "SELECTOR_LIQUIDITY", "SELECTOR_QUOTE_EXACT_INPUT_SINGLE", "SELECTOR_QUOTE_EXACT_OUTPUT_SINGLE",
    "address_word", "encode_word", "encode_call", "decode_words", "to_signed", "sqrt_price_to_usdt_per_bnb",
    "encode_quote_single", "decode_quote_result", "RpcError", "PancakeV3Client",
]
