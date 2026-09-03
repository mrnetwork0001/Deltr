"""
PancakeSwap V3 constants for BNB Smart Chain — verified on-chain on 2026-09-02
(factory.getPool / router.factory() / router.WETH9() / QuoterV2 quotes via
eth_call) and cross-checked against developer.pancakeswap.finance and the
pancake-v3-contracts deployment JSONs.

Notes that differ from Uniswap V3 and bite people:
* BSC USDT (0x55d3…) has **18 decimals**, not 6.
* `slot0()` returns `feeProtocol` as **uint32** (Uniswap: uint8).
* SmartRouter.exactInputSingle has **no** `deadline` field (selector
  0x04e45aaf); the v3-periphery SwapRouter variant has it (0x414bf389).
* QuoterV2 struct field order is (tokenIn, tokenOut, amountIn, fee,
  sqrtPriceLimitX96) — `amountIn` before `fee`.
* In every WBNB/USDT pool token0 = USDT and token1 = WBNB, so
  USDT-per-BNB = 2^192 / sqrtPriceX96^2.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Tuple

getcontext().prec = 60

Q96: int = 2**96
Q192: int = 2**192

# --------------------------------------------------------------------------- #
# Chains                                                                      #
# --------------------------------------------------------------------------- #
BSC_MAINNET_CHAIN_ID = 56
BSC_TESTNET_CHAIN_ID = 97

BSC_MAINNET_RPCS: Tuple[str, ...] = (
    "https://bsc-dataseed.binance.org/",
    "https://bsc-dataseed1.bnbchain.org/",
    "https://bsc-dataseed2.bnbchain.org/",
    "https://bsc-rpc.publicnode.com/",
    "https://bsc-dataseed1.defibit.io/",
    "https://bsc-dataseed1.ninicoin.io/",
)
BSC_TESTNET_RPCS: Tuple[str, ...] = (
    "https://data-seed-prebsc-1-s1.binance.org:8545/",
    "https://bsc-testnet-rpc.publicnode.com/",
    "https://data-seed-prebsc-2-s1.bnbchain.org:8545/",
)


@dataclass(frozen=True, slots=True)
class ChainAddresses:
    chain_id: int
    factory: str
    quoter_v2: str
    smart_router: str  # no-deadline exactInputSingle + multicall(deadline, data[])
    swap_router: str  # v3-periphery variant (deadline inside params)
    wbnb: str
    usdt: str
    usdt_decimals: int
    wbnb_decimals: int
    pools_wbnb_usdt: Dict[int, str]  # fee tier (bps units: 100 = 0.01 %) -> pool


MAINNET = ChainAddresses(
    chain_id=BSC_MAINNET_CHAIN_ID,
    factory="0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
    quoter_v2="0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
    smart_router="0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
    swap_router="0x1b81D678ffb9C0263b24A97847620C99d213eB14",
    wbnb="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    usdt="0x55d398326f99059fF775485246999027B3197955",
    usdt_decimals=18,
    wbnb_decimals=18,
    pools_wbnb_usdt={
        100: "0x172fcD41E0913e95784454622d1c3724f546f849",  # 0.01 % — deepest in-range liquidity
        500: "0x36696169C63e42cd08ce11f5deeBbCeBae652050",  # 0.05 %
        2500: "0x1401ff943D08a7E098328C1d3a9d388923B115D2",  # 0.25 %
        10000: "0x6805E0E5333c5c3acCF2930Be4734E2b98f4Ce06",  # 1 %
    },
)

TESTNET = ChainAddresses(
    chain_id=BSC_TESTNET_CHAIN_ID,
    factory="0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
    quoter_v2="0xbC203d7f83677c7ed3F7acEc959963E7F4ECC5C2",
    smart_router="0x9a489505a00cE272eAa5e07Dba6491314CaE3796",
    swap_router="0x1b81D678ffb9C0263b24A97847620C99d213eB14",
    wbnb="0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd",
    usdt="0x0fB5D7c73FA349A90392f873a4FA1eCf6a3d0a96",  # PancakeSwap's mock USDT on chapel
    usdt_decimals=18,
    wbnb_decimals=18,
    pools_wbnb_usdt={
        100: "0xD936b344c329290BE03f7fbbFa522215b1615eC9",  # tiny liquidity (~2e19)
        2500: "0x5147173E452AE4dd23dcEe7BaAaaAB7318F16F6B",
    },
)

# Other BSC mainnet majors Deltr may hedge (spot token on PancakeSwap ↔ Binance USDⓈ-M perp)
MAINNET_TOKENS: Dict[str, Tuple[str, int]] = {
    "WBNB": (MAINNET.wbnb, 18),
    "USDT": (MAINNET.usdt, 18),
    "ETH": ("0x2170Ed0880ac9A755fd29B2688956BD959F933F8", 18),  # Binance-Peg Ethereum
    "BTCB": ("0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", 18),  # Binance-Peg BTCB
    "CAKE": ("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", 18),
}

# Binance perp symbol -> (PancakeSwap base token symbol, quote token symbol)
SYMBOL_MAP: Dict[str, Tuple[str, str]] = {
    "BNBUSDT": ("WBNB", "USDT"),
    "ETHUSDT": ("ETH", "USDT"),
    "BTCUSDT": ("BTCB", "USDT"),
    "CAKEUSDT": ("CAKE", "USDT"),
}

FEE_TIERS_BPS: Tuple[int, ...] = (100, 500, 2500, 10000)  # 1e-6 units: 100 = 0.01 %
TICK_SPACING: Dict[int, int] = {100: 1, 500: 10, 2500: 50, 10000: 200}

# --------------------------------------------------------------------------- #
# ABIs (minimal, web3.py-ready)                                               #
# --------------------------------------------------------------------------- #
POOL_ABI: List[dict] = [
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint32"},
            {"name": "unlocked", "type": "bool"},
        ],
    },
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
]

FACTORY_ABI: List[dict] = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee", "type": "uint24"},
        ],
        "outputs": [{"name": "pool", "type": "address"}],
    }
]

QUOTER_V2_ABI: List[dict] = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
    {
        "name": "quoteExactOutputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
]

# SmartRouter (IV3SwapRouter): exactInputSingle WITHOUT deadline + multicall(deadline, data[])
SMART_ROUTER_ABI: List[dict] = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "name": "multicall",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "deadline", "type": "uint256"}, {"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
    },
    {"name": "WETH9", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "factory", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

ERC20_ABI: List[dict] = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
]

# Selectors (verified against deployed bytecode) — handy for raw eth_call fallbacks
SELECTORS: Dict[str, str] = {
    "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502",
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "getPool": "0x1698ee82",
    "quoteExactInputSingle": "0xc6a5026a",
    "smartRouter.exactInputSingle": "0x04e45aaf",
    "swapRouter.exactInputSingle": "0x414bf389",
    "multicall(uint256,bytes[])": "0x5ae401dc",
}


# --------------------------------------------------------------------------- #
# Math helpers                                                                #
# --------------------------------------------------------------------------- #
def price_token1_per_token0(sqrt_price_x96: int, dec0: int, dec1: int) -> Decimal:
    """Human price of 1 token0 expressed in token1: (sqrtP/2^96)^2 · 10^(dec0−dec1)."""
    raw = Decimal(sqrt_price_x96 * sqrt_price_x96) / Decimal(Q192)
    return raw * (Decimal(10) ** (dec0 - dec1))


def price_token0_per_token1(sqrt_price_x96: int, dec0: int, dec1: int) -> Decimal:
    """Human price of 1 token1 expressed in token0 (e.g. USDT per WBNB when token0=USDT)."""
    p = price_token1_per_token0(sqrt_price_x96, dec0, dec1)
    return Decimal(1) / p if p != 0 else Decimal(0)


def bnb_usdt_from_sqrt_price(sqrt_price_x96: int) -> float:
    """Fast path for the WBNB/USDT pools (token0 = USDT, token1 = WBNB, both 18 dec)."""
    return float(Q192 / (sqrt_price_x96 * sqrt_price_x96))


def fee_tier_to_bps(fee_tier: int) -> float:
    """PancakeSwap fee units are 1e-6 (100 = 0.01 %).  Returns basis points."""
    return fee_tier / 100.0


def addresses_for_chain(chain_id: int) -> ChainAddresses:
    if chain_id == BSC_MAINNET_CHAIN_ID:
        return MAINNET
    if chain_id == BSC_TESTNET_CHAIN_ID:
        return TESTNET
    raise ValueError(f"Unsupported chain id {chain_id}; PancakeSwap V3 constants exist for 56 and 97 only")
