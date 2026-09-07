"""
Deltr settings — one frozen configuration object built at launch.

* Mode is chosen ONCE (``--mode`` flag or ``DELTR_MODE``) and never changed at
  runtime.  Three modes: PAPER (simulated fills on live prices, zero secrets),
  TESTNET (real Binance USDⓈ-M Futures **testnet** orders) and LIVE (real
  mainnet perp orders plus a real on-chain leg executed by the Binance Agentic
  Wallet CLI).  LIVE is opt-in three times over and refuses to start otherwise.
* Environment variable names mirror the official Binance Agent OS ``binance``
  skill (``BINANCE_API_KEY``, ``BINANCE_SECRET_KEY``, ``BINANCE_API_ENV``).
* Hosts are a frozen table keyed by mode so a paper run can never reach an
  order endpoint.  Market DATA is real mainnet in every mode (keyless, read-only);
  only LIVE carries a mainnet ORDER host.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from risk_gate import RiskLimits

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0.0"



def mask_url(url: str) -> str:
    """Scheme + host only. RPC providers often embed an API key in the path or
    query, so the redacted view never echoes anything past the host."""
    try:
        from urllib.parse import urlsplit

        u = urlsplit(url)
        if not u.scheme or not u.netloc:
            return "<masked>"
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        tail = "/…" if (u.path.strip("/") or u.query or u.fragment) else "/"
        return f"{u.scheme}://{host}{port}{tail}"
    except Exception:  # pragma: no cover - defensive
        return "<masked>"


class Mode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"       # real mainnet perp orders + a real on-chain leg; arms only with the full opt-in


class ExecutionStyle(str, Enum):
    """How the perp leg reaches the book.

    MAKER posts a post-only (GTX) limit order and never crosses the spread.  The measured
    funding evidence (docs/STRATEGY_EVIDENCE.md) puts the taken round trip at about 16.6 bps
    against about 8.6 bps posted, so MAKER is the default in LIVE and TAKER must be chosen
    deliberately.
    """

    MAKER = "maker"
    TAKER = "taker"


class LegOrder(str, Enum):
    DEX_FIRST = "dex_first"
    CEX_FIRST = "cex_first"


# --------------------------------------------------------------------------- #
# Frozen host table (per mode).  Production trading hosts are deliberately     #
# absent: Deltr never places a production order.                              #
# --------------------------------------------------------------------------- #
# Real mainnet futures market data: keyless, read-only, the same host in every mode.
FUTURES_MAINNET_REST = "https://fapi.binance.com"
# Mainnet spot REST (api.binance.com) is NOT used: it answers 403 from many networks.
# The keyless mirror below is the spot reference in every mode.
SPOT_MIRROR_REST = "https://data-api.binance.vision"
FUTURES_TESTNET_REST = "https://testnet.binancefuture.com"

HOSTS: Dict[Mode, Dict[str, str]] = {
    Mode.PAPER: {
        # public market data only — no keys, no orders
        "spot_rest": SPOT_MIRROR_REST,
        "futures_rest": FUTURES_TESTNET_REST,  # mark/index/funding track the mainnet index
        "futures_data_rest": FUTURES_MAINNET_REST,  # keyless mainnet market data
        "futures_order_rest": "",  # NO order endpoint in paper mode
    },
    Mode.TESTNET: {
        "spot_rest": SPOT_MIRROR_REST,
        "futures_rest": FUTURES_TESTNET_REST,
        "futures_data_rest": FUTURES_MAINNET_REST,
        "futures_order_rest": FUTURES_TESTNET_REST,
    },
    Mode.LIVE: {
        "spot_rest": SPOT_MIRROR_REST,
        "futures_rest": FUTURES_MAINNET_REST,
        "futures_data_rest": FUTURES_MAINNET_REST,
        "futures_order_rest": FUTURES_MAINNET_REST,  # real money; armed only by the full opt-in below
    },
}

# The exact acknowledgement an operator must set to arm the value-moving on-chain leg.
ONCHAIN_ACK_PHRASE = "i-understand-this-moves-real-funds"
# The exact acknowledgement an operator must set to arm LIVE trading.
LIVE_ACK_PHRASE = "i-understand-this-trades-real-money"
# The LIVE test override: lets the min-edge threshold go negative in LIVE so the whole real-money
# path can be exercised on a day the edge is negative. Only with this phrase AND a per-trade cap at
# or below LIVE_TEST_MAX_NOTIONAL_USD, so the knowingly accepted loss is a few cents.
LIVE_TEST_ACK_PHRASE = "i-accept-a-small-known-loss"
LIVE_TEST_MAX_NOTIONAL_USD = 25.0
# Unattended LIVE: auto-execute may run with real money only with this phrase, and then only while
# the min edge is at or above 0 (an override, test or otherwise, switches auto off on its own).
LIVE_AUTO_ACK_PHRASE = "i-understand-this-trades-real-money-unattended"
# The only BINANCE_API_ENV value LIVE accepts.  "prod" stays refused in every mode: it is the
# spelling that appears in copied-and-pasted configs, so it never silently arms anything.
LIVE_API_ENV = "mainnet"

# Per-mode hard caps enforced by the risk gate (USD notional per trade).
# LIVE is deliberately SMALL and has to be raised on purpose.
MAX_NOTIONAL_BY_MODE: Dict[Mode, float] = {Mode.PAPER: 50_000.0, Mode.TESTNET: 5_000.0, Mode.LIVE: 250.0}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True)

    # ---- run mode & strategy ------------------------------------------------
    mode: Mode = Field(default=Mode.PAPER, alias="DELTR_MODE")
    symbols: str = Field(default="BNBUSDT", alias="DELTR_SYMBOLS")
    capital_usd: float = Field(default=10_000.0, alias="DELTR_CAPITAL_USD", gt=0)
    min_edge_bps: float = Field(default=3.0, alias="DELTR_MIN_EDGE_BPS")
    default_leverage: float = Field(default=2.0, alias="DELTR_DEFAULT_LEVERAGE", gt=0, le=3.0)
    funding_horizon_hours: float = Field(default=72.0, alias="DELTR_FUNDING_HORIZON_HOURS", gt=0)
    auto_execute: bool = Field(default=False, alias="DELTR_AUTO_EXECUTE")
    plan_ttl_seconds: float = Field(default=60.0, alias="DELTR_PLAN_TTL_SECONDS", gt=0)
    leg_order: LegOrder = Field(default=LegOrder.DEX_FIRST, alias="DELTR_LEG_ORDER")
    version: str = Field(default=VERSION, alias="DELTR_VERSION")

    # ---- cost model (bps unless stated) -------------------------------------
    perp_taker_fee_bps: float = Field(default=5.0, alias="DELTR_PERP_TAKER_FEE_BPS")  # 0.05 % VIP0 taker
    perp_slippage_bps: float = Field(default=2.0, alias="DELTR_PERP_SLIPPAGE_BPS")  # modelled half-spread on mark
    dex_fee_tier: int = Field(default=100, alias="DELTR_DEX_FEE_TIER")  # PancakeSwap fee units (100 = 0.01 %)
    dex_gas_units: int = Field(default=150_000, alias="DELTR_DEX_GAS_UNITS")
    basis_exit_bps: float = Field(default=0.0, alias="DELTR_BASIS_EXIT_BPS")  # assumed basis at unwind
    basis_shock_bps: float = Field(default=100.0, alias="DELTR_BASIS_SHOCK_BPS")
    paper_dex_extra_slippage_bps: float = Field(default=1.0, alias="DELTR_PAPER_DEX_SLIPPAGE_BPS")
    max_dex_impact_bps: float = Field(default=5.0, alias="DELTR_MAX_DEX_IMPACT_BPS")

    # ---- safety thresholds --------------------------------------------------
    price_sanity_bps: float = Field(default=100.0, alias="DELTR_PRICE_SANITY_BPS")
    price_drift_bps: float = Field(default=20.0, alias="DELTR_PRICE_DRIFT_BPS")
    cex_stale_ms: int = Field(default=5_000, alias="DELTR_CEX_STALE_MS")
    dex_stale_ms: int = Field(default=9_000, alias="DELTR_DEX_STALE_MS")
    testnet_ioc_band_bps: float = Field(default=10.0, alias="DELTR_TESTNET_IOC_BAND_BPS")
    testnet_ioc_retry_band_bps: float = Field(default=25.0, alias="DELTR_TESTNET_IOC_RETRY_BAND_BPS")

    # ---- LIVE mode: real mainnet perp orders + a real on-chain leg -----------
    # LIVE is opt-in three times over: DELTR_MODE=live, both Binance credentials, and
    # DELTR_LIVE_ACK set to the phrase below.  A fourth and fifth requirement (the wallet
    # CLI installed and signed in) are checked against the CLI in the engine's preflight.
    live_ack: Optional[str] = Field(default=None, alias="DELTR_LIVE_ACK")
    live_test_ack: Optional[str] = Field(default=None, alias="DELTR_LIVE_TEST_ACK")
    live_auto_ack: Optional[str] = Field(default=None, alias="DELTR_LIVE_AUTO_ACK")
    live_max_notional_usd: float = Field(default=250.0, alias="DELTR_LIVE_MAX_NOTIONAL_USD", gt=0)
    live_max_aggregate_usd: float = Field(default=1_000.0, alias="DELTR_LIVE_MAX_AGGREGATE_USD", gt=0)
    execution_style: ExecutionStyle = Field(default=ExecutionStyle.MAKER, alias="DELTR_EXECUTION_STYLE")
    perp_maker_fee_bps: float = Field(default=2.0, alias="DELTR_PERP_MAKER_FEE_BPS", ge=0)
    maker_offset_ticks: int = Field(default=0, alias="DELTR_MAKER_OFFSET_TICKS", ge=0)
    maker_wait_ms: int = Field(default=8_000, alias="DELTR_MAKER_WAIT_MS", gt=0)
    maker_poll_ms: int = Field(default=400, alias="DELTR_MAKER_POLL_MS", gt=0)
    maker_reprice_attempts: int = Field(default=3, alias="DELTR_MAKER_REPRICE_ATTEMPTS", ge=1, le=20)

    # ---- Binance credentials (Agent OS / binance-cli names) -----------------
    # ``repr=False`` on every credential: pydantic builds BOTH ``__repr__`` and ``__str__`` from
    # the same field list, so without it ``repr(settings)`` / ``f"{settings}"`` print the key and
    # the secret in full.  Settings is held by State, the Executor, both routers and the on-chain
    # leg, so a single ``log.debug("... %r", self.settings)`` or an exception rendered with the
    # object in it would put the secret into stderr, an activity row or an API error envelope.
    # The values stay readable through the attribute; only the printed form drops them.
    binance_api_key: Optional[str] = Field(default=None, alias="BINANCE_API_KEY", repr=False)
    binance_secret_key: Optional[str] = Field(default=None, alias="BINANCE_SECRET_KEY", repr=False)
    binance_api_env: str = Field(default="testnet", alias="BINANCE_API_ENV")
    binance_mcp_url: str = Field(default="https://agent.binance.com/mcp/agentic", alias="BINANCE_MCP_URL")
    binance_mcp_token: Optional[str] = Field(default=None, alias="BINANCE_MCP_TOKEN", repr=False)

    # ---- on-chain leg: the Binance Agentic Wallet CLI (Deltr holds no key) ----
    # Deltr never holds, reads, stores or signs with a private key.  The wallet CLI custodies the
    # key, applies its own daily limits and does the signing; these settings are Deltr's own
    # ceiling on top of that, plus the three-part opt-in that arms the value-moving path.
    baw_bin: str = Field(default="baw", alias="DELTR_BAW_BIN")
    baw_timeout_s: float = Field(default=25.0, alias="DELTR_BAW_TIMEOUT_S", gt=0)
    baw_swap_timeout_s: float = Field(default=150.0, alias="DELTR_BAW_SWAP_TIMEOUT_S", gt=0)
    baw_confirm_timeout_s: float = Field(default=180.0, alias="DELTR_BAW_CONFIRM_TIMEOUT_S", gt=0)
    wallet_chain_id: int = Field(default=56, alias="DELTR_WALLET_CHAIN_ID")
    onchain_mode: str = Field(default="off", alias="DELTR_ONCHAIN_MODE")       # "off" | "live"
    onchain_ack: Optional[str] = Field(default=None, alias="DELTR_ONCHAIN_ACK")
    onchain_max_notional_usd: float = Field(default=250.0, alias="DELTR_ONCHAIN_MAX_NOTIONAL_USD", gt=0)
    onchain_max_aggregate_usd: float = Field(default=1_000.0, alias="DELTR_ONCHAIN_MAX_AGGREGATE_USD", gt=0)
    onchain_max_slippage_pct: float = Field(default=0.5, alias="DELTR_ONCHAIN_MAX_SLIPPAGE_PCT", gt=0, le=50.0)
    onchain_max_price_impact_pct: float = Field(default=1.0, alias="DELTR_ONCHAIN_MAX_PRICE_IMPACT_PCT", gt=0, le=50.0)

    # ---- x402 (B402) payments on BNB Smart Chain -------------------------------
    x402_pay_to: Optional[str] = Field(default=None, alias="DELTR_X402_PAY_TO")
    x402_price_units: float = Field(default=0.05, alias="DELTR_X402_PRICE_UNITS", gt=0)
    x402_max_payment_units: float = Field(default=1.0, alias="DELTR_X402_MAX_PAYMENT_UNITS", gt=0)

    # ---- BNB Smart Chain (read-only quotes) -----------------------------------
    # Also ``repr=False``: RPC providers routinely put an API key in the URL path or query, which
    # is why every consumer runs these through ``mask_url`` / ``redact_url``.  The printed form of
    # Settings has to follow the same rule as the banner and ``redacted()`` do.
    bsc_rpc_url: str = Field(default="https://bsc-dataseed.binance.org/", alias="BSC_RPC_URL", repr=False)
    bsc_rpc_fallbacks: str = Field(
        default="https://bsc-dataseed1.bnbchain.org/,https://bsc-rpc.publicnode.com/", alias="BSC_RPC_FALLBACKS",
        repr=False,
    )
    bsc_chain_id: int = Field(default=56, alias="BSC_CHAIN_ID")
    dex_quote_cache_s: float = Field(default=2.0, alias="DELTR_DEX_QUOTE_CACHE_S")

    # ---- public deployment ------------------------------------------------------
    # A public showcase (VPS, judges) must be VIEW-ONLY: every mutating API route and
    # every mutating MCP tool is refused unless the caller presents the token.
    public_readonly: bool = Field(default=False, alias="DELTR_PUBLIC_READONLY")
    api_token: Optional[str] = Field(default=None, alias="DELTR_API_TOKEN")  # secret; never in redacted()

    # ---- process --------------------------------------------------------------
    api_port: int = Field(default=8000, alias="DELTR_API_PORT")
    # Extra Host header values the MCP transport accepts (comma-separated, e.g.
    # "38.49.213.208:8000,deltr.example.com:*"). Loopback is always allowed. "*" turns the
    # DNS-rebinding check off entirely (only behind a reverse proxy that pins the Host).
    mcp_allowed_hosts: str = Field(default="", alias="DELTR_MCP_ALLOWED_HOSTS")
    # Browser origins allowed by the API's CORS middleware and accepted as MCP Origins.
    cors_origins: str = Field(default="", alias="DELTR_CORS_ORIGINS")
    ui_port: int = Field(default=3000, alias="DELTR_UI_PORT")
    bridge_port: int = Field(default=8788, alias="DELTR_BRIDGE_PORT")
    state_dir: str = Field(default=str(REPO_ROOT / "state"), alias="DELTR_STATE_DIR")
    replay_path: Optional[str] = Field(default=None, alias="DELTR_REPLAY_PATH")
    replay_speed: float = Field(default=1.0, alias="DELTR_REPLAY_SPEED", gt=0)
    poll_interval_cex_s: float = Field(default=1.0, alias="DELTR_POLL_CEX_S", gt=0)
    poll_interval_dex_s: float = Field(default=3.0, alias="DELTR_POLL_DEX_S", gt=0)

    # ---- validation -----------------------------------------------------------
    @field_validator("binance_api_env")
    @classmethod
    def _env_not_prod(cls, v: str) -> str:
        v = (v or "testnet").lower()
        if v == "prod":
            raise ValueError(
                "BINANCE_API_ENV=prod is refused. Mainnet trading is spelled "
                f"BINANCE_API_ENV={LIVE_API_ENV} and needs DELTR_MODE=live plus DELTR_LIVE_ACK."
            )
        return v

    @field_validator("onchain_mode")
    @classmethod
    def _onchain_mode_known(cls, v: str) -> str:
        v = (v or "off").strip().lower()
        if v not in {"off", "live"}:
            raise ValueError("DELTR_ONCHAIN_MODE must be 'off' or 'live'")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols_upper(cls, v: str) -> str:
        syms = [s.strip().upper() for s in v.split(",") if s.strip()]
        if not syms:
            raise ValueError("DELTR_SYMBOLS must list at least one symbol")
        return ",".join(syms)

    @model_validator(mode="after")
    def _testnet_needs_keys(self) -> "Settings":
        if self.mode == Mode.TESTNET and not (self.binance_api_key and self.binance_secret_key):
            raise ValueError(
                "DELTR_MODE=testnet requires BINANCE_API_KEY and BINANCE_SECRET_KEY "
                "(create testnet keys at https://testnet.binancefuture.com)."
            )
        return self

    @model_validator(mode="after")
    def _live_needs_the_full_opt_in(self) -> "Settings":
        """LIVE refuses to build unless every static requirement is present, named one at a time.

        The two runtime requirements (the wallet CLI installed, and signed in) cannot be checked
        here without running a subprocess, so the engine's preflight checks them before the first
        tick.  Nothing arms from a default: every part has to be set on purpose.
        """
        if self.mode != Mode.LIVE:
            return self
        err = self._live_static_error()
        if err:
            raise ValueError(err)
        return self

    def _live_static_error(self) -> Optional[str]:
        """The first missing static LIVE requirement, named exactly, or None."""
        if not self.binance_api_key or not self.binance_secret_key:
            missing = " and ".join(
                n for n, v in (("BINANCE_API_KEY", self.binance_api_key), ("BINANCE_SECRET_KEY", self.binance_secret_key)) if not v
            )
            return (f"DELTR_MODE=live requires real Binance mainnet credentials: {missing} is not set. "
                    "Deltr will not start LIVE without them.")
        if (self.binance_api_env or "").lower() != LIVE_API_ENV:
            return (f"DELTR_MODE=live requires BINANCE_API_ENV={LIVE_API_ENV} "
                    f"(it is {self.binance_api_env!r}). This is the switch that says the keys are mainnet keys.")
        if (self.live_ack or "").strip().lower() != LIVE_ACK_PHRASE:
            return (f"the LIVE acknowledgement is not set: set DELTR_LIVE_ACK={LIVE_ACK_PHRASE} "
                    "to confirm you understand this places real orders with real money.")
        onchain = self.onchain_arming_error()
        if onchain:
            return ("DELTR_MODE=live routes the on-chain leg through the Binance Agentic Wallet, so the "
                    f"on-chain opt-in must also be complete: {onchain}")
        return None

    # ---- derived ----------------------------------------------------------------
    @property
    def symbol(self) -> str:
        return self.symbol_list[0]

    @property
    def symbol_list(self) -> List[str]:
        return self.symbols.split(",")

    @property
    def hosts(self) -> Dict[str, str]:
        return HOSTS[self.mode]

    @property
    def max_notional_usd(self) -> float:
        """Per-trade USD notional ceiling the gate enforces.

        In LIVE the operator's own ``DELTR_LIVE_MAX_NOTIONAL_USD`` can only make the small
        default smaller, never larger: the table entry is a hard ceiling.
        """
        cap = MAX_NOTIONAL_BY_MODE[self.mode]
        if self.mode == Mode.LIVE:
            return min(cap, float(self.live_max_notional_usd))
        return cap

    @property
    def max_aggregate_usd(self) -> float:
        """Aggregate open USD notional ceiling (LIVE only; other modes use the gate's own limits)."""
        return float(self.live_max_aggregate_usd) if self.mode == Mode.LIVE else float("inf")

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE

    @property
    def real_orders(self) -> bool:
        """True when an order from this process reaches a real venue (TESTNET funds or LIVE funds)."""
        return self.mode in (Mode.TESTNET, Mode.LIVE)

    @property
    def perp_fee_bps(self) -> float:
        """The perp fee actually paid per side under the configured execution style.

        The gate's cost model deliberately keeps using the TAKER fee: quoting the more
        expensive round trip means the gate demands more edge than a maker fill costs.
        """
        return self.perp_maker_fee_bps if self.execution_style == ExecutionStyle.MAKER else self.perp_taker_fee_bps

    @property
    def live_ack_ok(self) -> bool:
        return (self.live_ack or "").strip().lower() == LIVE_ACK_PHRASE

    @property
    def live_auto_ok(self) -> bool:
        """Auto-execute is allowed to spend real money: LIVE/TESTNET with the unattended phrase set."""
        return self.real_orders and (self.live_auto_ack or "").strip().lower() == LIVE_AUTO_ACK_PHRASE

    @property
    def live_test_override(self) -> bool:
        """LIVE may run with a negative min edge: the test phrase is set AND the per-trade cap is tiny."""
        return (
            self.mode == Mode.LIVE
            and (self.live_test_ack or "").strip().lower() == LIVE_TEST_ACK_PHRASE
            and float(self.live_max_notional_usd) <= LIVE_TEST_MAX_NOTIONAL_USD
        )

    def live_arming_error(self) -> Optional[str]:
        """Which LIVE requirement is missing, named exactly, or None when the static ones are set.

        Does not touch the wallet CLI: installation and sign-in are checked by the engine's
        preflight because they need a subprocess.
        """
        if self.mode != Mode.LIVE:
            return f"LIVE is not selected: mode is {self.mode.value.upper()} (set DELTR_MODE=live)."
        return self._live_static_error()

    @property
    def secrets_present(self) -> bool:
        return bool(self.binance_api_key and self.binance_secret_key)

    @property
    def rpc_urls(self) -> List[str]:
        urls = [self.bsc_rpc_url] + [u.strip() for u in self.bsc_rpc_fallbacks.split(",") if u.strip()]
        seen: List[str] = []
        for u in urls:
            if u not in seen:
                seen.append(u)
        return seen

    @property
    def data_source_label(self) -> str:
        """Where market data comes from, for the banner and deltr_status. Mainnet in every mode."""
        return "binance-futures-mainnet (keyless, read-only) + binance-spot-mirror + bsc-mainnet-chain"

    @property
    def execution_style_label(self) -> str:
        if self.mode == Mode.PAPER:
            return "simulated (no order reaches a venue)"
        if self.execution_style == ExecutionStyle.MAKER:
            return "maker: post-only (GTX) perp leg, never crosses the spread"
        return "taker: LIMIT IOC perp leg, crosses the spread"

    @property
    def gate_state_path(self) -> str:
        return str(Path(self.state_dir) / self.mode.value / "gate.json")

    @property
    def portfolio_state_path(self) -> str:
        return str(Path(self.state_dir) / self.mode.value / "portfolio.json")

    @property
    def journal_path(self) -> str:
        return str(Path(self.state_dir) / self.mode.value / "journal.jsonl")

    @property
    def receipts_path(self) -> str:
        return str(Path(self.state_dir) / self.mode.value / "receipts.jsonl")

    @property
    def mcp_http_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}/mcp"

    @property
    def onchain_live_selected(self) -> bool:
        """Part 1 of the opt-in: the on-chain leg was explicitly selected."""
        return self.onchain_mode == "live"

    @property
    def onchain_ack_ok(self) -> bool:
        """Part 3 of the opt-in: the operator typed the acknowledgement phrase."""
        return (self.onchain_ack or "").strip().lower() == ONCHAIN_ACK_PHRASE

    def onchain_arming_error(self) -> Optional[str]:
        """Which part of the opt-in is missing, named exactly, or None when both local parts are set.

        Part 2 (the wallet session) is not a Deltr credential: it lives inside the Binance
        Agentic Wallet CLI and is checked against the CLI at call time, never stored here.
        """
        if not self.onchain_live_selected:
            return ("the on-chain leg is not selected: set DELTR_ONCHAIN_MODE=live "
                    "(it is 'off' by default and no on-chain value can move while it is)")
        if not self.onchain_ack_ok:
            return (f"the on-chain acknowledgement is not set: set DELTR_ONCHAIN_ACK={ONCHAIN_ACK_PHRASE} "
                    "to confirm you understand this moves real funds")
        return None

    def risk_limits(self) -> RiskLimits:
        """The gate limits derived from settings (spec invariants stay clamped)."""
        return RiskLimits(
            max_notional_usd=self.max_notional_usd,
            max_quote_age_ms=float(self.cex_stale_ms),
            max_price_drift_bps=self.price_drift_bps,
            basis_shock_bps=self.basis_shock_bps,
            default_roundtrip_cost_bps=20.0,
            price_sanity_bps=self.price_sanity_bps,
            min_expected_edge_bps=self.min_edge_bps if self.mode == Mode.PAPER else max(0.0, self.min_edge_bps),
            allowed_symbols=tuple(self.symbol_list),
            max_open_positions=3,
        )

    @property
    def effective_leg_order(self) -> LegOrder:
        """Which leg to send first, given the execution style.

        ``DELTR_LEG_ORDER`` decides it in general, but a MAKER perp leg is the one leg that may
        simply not fill, and the cheapest place for an uncertain leg is FIRST.  Posting the perp
        first and abandoning it costs nothing: an order that never filled pays no fee.  Doing
        the on-chain swap first and then missing the post costs a whole DEX round trip — two
        pool fees, two lots of gas and two lots of price impact, tens of bps against an edge
        measured in single-digit bps — and it does that on the *common* outcome, because a
        post-only order missing inside its budget is ordinary, not exceptional.  The reverse
        case (perp filled, swap fails) is both rarer and far cheaper to undo, because a perp
        reversal crosses the spread once and is certain.
        """
        # Scoped to LIVE: it is the only mode whose router actually posts.  PAPER and TESTNET
        # ignore execution_style entirely, so their leg order is left exactly as configured.
        if self.mode == Mode.LIVE and self.execution_style == ExecutionStyle.MAKER:
            return LegOrder.CEX_FIRST
        return self.leg_order

    def min_edge_floor_bps(self, measured_roundtrip_bps: float) -> float:
        """Runtime min-edge can never go below this outside PAPER.

        Zero: the net edge is already net of the measured round trip and the assumed exit
        basis, so "never target a loss" means net >= 0, not net >= round trip (which would
        count the costs twice and refuse every trade the model calls profitable). PAPER also
        reports 0 here; its [-50, 50] demo override lives in Engine.set_min_edge.
        """
        del measured_roundtrip_bps
        return 0.0

    def mcp_client_snippets(self) -> Dict[str, str]:
        """Ready-to-paste client configs (absolute paths resolved)."""
        py = str(REPO_ROOT / ".venv" / "bin" / "python")
        main = str(REPO_ROOT / "main.py")
        http = self.mcp_http_url
        return {
            "claude_code": f"claude mcp add deltr --transport http {http}",
            "claude_code_binance": f"claude mcp add binance-mcp-server --transport http {self.binance_mcp_url}",
            "codex": f"codex mcp add deltr --url {http}",
            "cursor_mcp_json": (
                '{\n  "mcpServers": {\n    "deltr": { "url": "' + http + '" },\n'
                '    "binance-mcp-server": { "url": "' + self.binance_mcp_url + '" }\n  }\n}'
            ),
            "claude_desktop_stdio": (
                '{\n  "mcpServers": {\n    "deltr": {\n      "command": "' + py + '",\n'
                '      "args": ["' + main + '", "--mcp"]\n    }\n  }\n}'
            ),
            "claude_desktop_remote": (
                '{\n  "mcpServers": {\n    "deltr": {\n      "command": "npx",\n'
                '      "args": ["-y", "mcp-remote", "' + http + '"]\n    }\n  }\n}'
            ),
        }

    def redacted(self) -> Dict[str, Any]:
        """Safe-to-print view: never exposes secret values (auth.md rule)."""
        return {
            "version": self.version,
            "mode": self.mode.value,
            "symbols": self.symbol_list,
            "capital_usd": self.capital_usd,
            "min_edge_bps": self.min_edge_bps,
            "default_leverage": self.default_leverage,
            "funding_horizon_hours": self.funding_horizon_hours,
            "auto_execute": self.auto_execute,
            "leg_order": self.leg_order.value,
            # what the Executor actually does: a MAKER perp leg in LIVE goes first (see
            # effective_leg_order), so reporting only the configured value would be misleading
            "effective_leg_order": self.effective_leg_order.value,
            "plan_ttl_seconds": self.plan_ttl_seconds,
            "binance_api_env": self.binance_api_env,
            "secrets_present": self.secrets_present,
            "binance_mcp_url": self.binance_mcp_url,
            "binance_mcp_token_present": bool(self.binance_mcp_token),
            "bsc_rpc_urls": [mask_url(u) for u in self.rpc_urls],
            "bsc_chain_id": self.bsc_chain_id,
            "hosts": self.hosts,
            "max_notional_usd": self.max_notional_usd,
            "price_sanity_bps": self.price_sanity_bps,
            "price_drift_bps": self.price_drift_bps,
            "api_port": self.api_port,
            "mcp_allowed_hosts": [h for h in self.mcp_allowed_hosts.split(",") if h.strip()],
            "bridge_port": self.bridge_port,
            "replay_path": self.replay_path,
            "public_readonly": self.public_readonly,
            "api_token_present": bool(self.api_token),
            "state_dir": self.state_dir,
            "execution_style": self.execution_style.value,
            "execution_style_label": self.execution_style_label,
            "data_source": self.data_source_label,
            "live_ack_present": self.live_ack_ok,
            "live_test_override": self.live_test_override,
            "live_auto_ok": self.live_auto_ok,
            "live_arming_error": self.live_arming_error(),
            "live_max_notional_usd": self.live_max_notional_usd,
            "live_max_aggregate_usd": self.live_max_aggregate_usd,
            "perp_maker_fee_bps": self.perp_maker_fee_bps,
            "onchain_mode": self.onchain_mode,
            "onchain_armed": self.onchain_arming_error() is None,
            "onchain_arming_error": self.onchain_arming_error(),
            "onchain_max_notional_usd": self.onchain_max_notional_usd,
            "onchain_max_aggregate_usd": self.onchain_max_aggregate_usd,
            "onchain_max_slippage_pct": self.onchain_max_slippage_pct,
            "wallet_chain_id": self.wallet_chain_id,
            "baw_bin": self.baw_bin,
            "x402_pay_to_present": bool(self.x402_pay_to),
            "x402_price_units": self.x402_price_units,
            "x402_max_payment_units": self.x402_max_payment_units,
        }


def load_settings(**overrides: object) -> Settings:
    """Build settings from env/.env with explicit CLI overrides (by alias name)."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**clean)  # type: ignore[arg-type]


__all__ = [
    "Mode", "LegOrder", "ExecutionStyle", "HOSTS", "MAX_NOTIONAL_BY_MODE",
    "ONCHAIN_ACK_PHRASE", "LIVE_ACK_PHRASE", "LIVE_TEST_ACK_PHRASE", "LIVE_TEST_MAX_NOTIONAL_USD", "LIVE_AUTO_ACK_PHRASE", "LIVE_API_ENV",
    "FUTURES_MAINNET_REST", "FUTURES_TESTNET_REST", "SPOT_MIRROR_REST",
    "Settings", "load_settings", "mask_url", "REPO_ROOT", "VERSION",
]


def auto_allowed(settings: "Settings", min_edge_bps: float) -> tuple[bool, Optional[str]]:
    """May the auto loop place the next order right now?

    PAPER: always (its labelled override is the point of the demo). Real orders: only with the
    unattended phrase, and only while the threshold is >= 0, so an override (the LIVE test
    override included) switches unattended trading off rather than letting it chase a known loss.
    """
    if not settings.real_orders:
        return True, None
    if not settings.live_auto_ok:
        return False, f"unattended {settings.mode.value.upper()} trading needs DELTR_LIVE_AUTO_ACK"
    if min_edge_bps < 0:
        return False, f"min edge is {min_edge_bps:g} bps; unattended real orders run only at >= 0 bps"
    return True, None
