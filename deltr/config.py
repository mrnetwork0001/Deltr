"""
Deltr settings — one frozen configuration object built at launch.

* Mode is chosen ONCE (``--mode`` flag or ``DELTR_MODE``) and never changed at
  runtime.  There is no LIVE mode: Deltr trades PAPER (simulated fills on live
  prices, zero secrets) or TESTNET (real Binance USDⓈ-M Futures testnet orders).
* Environment variable names mirror the official Binance Agent OS ``binance``
  skill (``BINANCE_API_KEY``, ``BINANCE_SECRET_KEY``, ``BINANCE_API_ENV``).
* Hosts are a frozen table keyed by mode so a paper run can never reach an
  order endpoint.
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


class LegOrder(str, Enum):
    DEX_FIRST = "dex_first"
    CEX_FIRST = "cex_first"


# --------------------------------------------------------------------------- #
# Frozen host table (per mode).  Production trading hosts are deliberately     #
# absent: Deltr never places a production order.                              #
# --------------------------------------------------------------------------- #
HOSTS: Dict[Mode, Dict[str, str]] = {
    Mode.PAPER: {
        # public market data only — no keys, no orders
        "spot_rest": "https://data-api.binance.vision",
        "futures_rest": "https://testnet.binancefuture.com",  # mark/index/funding track the mainnet index
        "futures_order_rest": "",  # NO order endpoint in paper mode
    },
    Mode.TESTNET: {
        "spot_rest": "https://data-api.binance.vision",
        "futures_rest": "https://testnet.binancefuture.com",
        "futures_order_rest": "https://testnet.binancefuture.com",
    },
}

# Per-mode hard caps enforced by the risk gate (USD notional per trade)
MAX_NOTIONAL_BY_MODE: Dict[Mode, float] = {Mode.PAPER: 50_000.0, Mode.TESTNET: 5_000.0}


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

    # ---- Binance credentials (Agent OS / binance-cli names) -----------------
    binance_api_key: Optional[str] = Field(default=None, alias="BINANCE_API_KEY")
    binance_secret_key: Optional[str] = Field(default=None, alias="BINANCE_SECRET_KEY")
    binance_api_env: str = Field(default="testnet", alias="BINANCE_API_ENV")
    binance_mcp_url: str = Field(default="https://agent.binance.com/mcp/agentic", alias="BINANCE_MCP_URL")
    binance_mcp_token: Optional[str] = Field(default=None, alias="BINANCE_MCP_TOKEN")

    # ---- BNB Smart Chain (read-only quotes) -----------------------------------
    bsc_rpc_url: str = Field(default="https://bsc-dataseed.binance.org/", alias="BSC_RPC_URL")
    bsc_rpc_fallbacks: str = Field(
        default="https://bsc-dataseed1.bnbchain.org/,https://bsc-rpc.publicnode.com/", alias="BSC_RPC_FALLBACKS"
    )
    bsc_chain_id: int = Field(default=56, alias="BSC_CHAIN_ID")
    dex_quote_cache_s: float = Field(default=2.0, alias="DELTR_DEX_QUOTE_CACHE_S")

    # ---- process --------------------------------------------------------------
    api_port: int = Field(default=8000, alias="DELTR_API_PORT")
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
            raise ValueError("BINANCE_API_ENV=prod is refused: Deltr never trades on production hosts.")
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
        return MAX_NOTIONAL_BY_MODE[self.mode]

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

    def min_edge_floor_bps(self, measured_roundtrip_bps: float) -> float:
        """Runtime min-edge can never go below this outside PAPER."""
        return 0.0 if self.mode == Mode.PAPER else float(measured_roundtrip_bps)

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
            "bridge_port": self.bridge_port,
            "replay_path": self.replay_path,
            "state_dir": self.state_dir,
        }


def load_settings(**overrides: object) -> Settings:
    """Build settings from env/.env with explicit CLI overrides (by alias name)."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**clean)  # type: ignore[arg-type]


__all__ = ["Mode", "LegOrder", "HOSTS", "MAX_NOTIONAL_BY_MODE", "Settings", "load_settings", "mask_url", "REPO_ROOT", "VERSION"]
