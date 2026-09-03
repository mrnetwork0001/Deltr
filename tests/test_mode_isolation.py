"""tests/test_mode_isolation.py — mode isolation across PAPER, TESTNET and LIVE.

The invariant is no longer "there is no LIVE mode"; LIVE exists and places real mainnet
orders.  What is invariant is the *isolation*:

* PAPER can never reach an order endpoint and never receives credentials;
* only the mode that owns a venue carries that venue's order host;
* market DATA is real mainnet everywhere and always keyless;
* LIVE refuses to build unless every part of the opt-in is present, one named at a time;
* each router asserts its own host, and inherits no permission from the shared base.
"""
from __future__ import annotations

import inspect
from urllib.parse import urlsplit

import pytest

from deltr.config import (
    HOSTS,
    LIVE_ACK_PHRASE,
    MAX_NOTIONAL_BY_MODE,
    ONCHAIN_ACK_PHRASE,
    ExecutionStyle,
    Mode,
    Settings,
    load_settings,
)
from deltr.executor import PaperRouter, TestnetRouter
from deltr.models import SymbolFilters

FILTERS = SymbolFilters(symbol="BNBUSDT")
SECRET_WORDS = ("key", "secret", "token", "password", "credential")


def test_mode_is_exactly_paper_testnet_live():
    assert [m.value for m in Mode] == ["paper", "testnet", "live"]


def test_hosts_table_is_frozen_and_only_live_carries_a_mainnet_order_host():
    assert set(HOSTS) == {Mode.PAPER, Mode.TESTNET, Mode.LIVE}
    assert HOSTS[Mode.PAPER]["futures_order_rest"] == ""
    for mode, hosts in HOSTS.items():
        for name, url in hosts.items():
            assert not url or url.startswith("https://"), f"{mode}.{name} must be https"
            host = urlsplit(url).hostname or ""
            # mainnet spot REST answers 403 from many networks and is never depended on
            assert host != "api.binance.com", f"{mode}.{name} uses mainnet spot REST"
            if name == "futures_order_rest" and mode != Mode.LIVE:
                assert host != "fapi.binance.com", f"{mode}.{name} points at the mainnet order endpoint"
    assert "testnet" in HOSTS[Mode.TESTNET]["futures_order_rest"]
    assert HOSTS[Mode.LIVE]["futures_order_rest"] == "https://fapi.binance.com"
    # market data is real mainnet in EVERY mode
    for mode in HOSTS:
        assert HOSTS[mode]["futures_data_rest"] == "https://fapi.binance.com"
    # the LIVE cap is the smallest by a wide margin and has to be raised deliberately
    assert MAX_NOTIONAL_BY_MODE[Mode.LIVE] < MAX_NOTIONAL_BY_MODE[Mode.TESTNET] < MAX_NOTIONAL_BY_MODE[Mode.PAPER]
    assert MAX_NOTIONAL_BY_MODE[Mode.LIVE] == 250.0


def test_paper_router_has_no_credential_parameters():
    params = list(inspect.signature(PaperRouter.__init__).parameters)
    assert params == ["self", "dex", "settings", "filters"]
    for name in params:
        assert not any(w in name.lower() for w in SECRET_WORDS)
    assert PaperRouter.mode == Mode.PAPER
    router = PaperRouter(object(), Settings(_env_file=None, DELTR_MODE="paper"), FILTERS)  # type: ignore[call-arg]
    assert not any(any(w in attr.lower() for w in SECRET_WORDS) for attr in vars(router))


def test_prod_api_env_is_refused_at_load_settings():
    with pytest.raises(Exception, match="prod"):
        load_settings(_env_file=None, BINANCE_API_ENV="prod")
    with pytest.raises(Exception, match="prod"):
        load_settings(_env_file=None, BINANCE_API_ENV="PROD", DELTR_MODE="testnet", BINANCE_API_KEY="k", BINANCE_SECRET_KEY="s")


def test_testnet_requires_both_keys_and_paper_ignores_them():
    with pytest.raises(Exception, match="BINANCE_API_KEY"):
        load_settings(_env_file=None, DELTR_MODE="testnet")
    with pytest.raises(Exception):
        load_settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY="only-one")
    s = load_settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real")
    assert s.mode == Mode.TESTNET and s.secrets_present and s.hosts["futures_order_rest"].startswith("https://testnet")
    p = load_settings(_env_file=None, DELTR_MODE="paper")
    assert p.mode == Mode.PAPER and not p.secrets_present and p.hosts["futures_order_rest"] == ""
    assert "k-not-real" not in str(s.redacted()) and "s-not-real" not in str(s.redacted())


def test_testnet_router_asserts_testnet_host():
    s = Settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY="k", BINANCE_SECRET_KEY="s")  # type: ignore[call-arg]

    class Futures:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

    with pytest.raises(AssertionError, match="non-testnet"):
        TestnetRouter(Futures("https://fapi.binance.com"), object(), s, FILTERS)
    assert TestnetRouter(Futures("https://testnet.binancefuture.com"), object(), s, FILTERS).mode == Mode.TESTNET


def test_paper_engine_builds_keyless_venue_clients(tmp_path):
    """Even with keys in the environment a PAPER engine never hands them to the futures client."""
    from deltr.engine import build_engine
    from tests.conftest import make_replay_settings, refusing_http

    settings = make_replay_settings(tmp_path, BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real")
    eng = build_engine(settings, replay_path=str(settings.replay_path), http=refusing_http())
    assert settings.secrets_present and eng.futures.has_credentials is False
    assert type(eng.router).__name__ == "PaperRouter"


def test_replay_and_auto_are_refused_in_testnet(tmp_path):
    from deltr.engine import build_engine
    from tests.conftest import REPLAY_FIXTURE, refusing_http

    s = Settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY="k", BINANCE_SECRET_KEY="s", DELTR_STATE_DIR=str(tmp_path))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="replay"):
        build_engine(s, replay_path=str(REPLAY_FIXTURE), http=refusing_http())
    s2 = Settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY="k", BINANCE_SECRET_KEY="s", DELTR_AUTO_EXECUTE=True, DELTR_STATE_DIR=str(tmp_path))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="auto"):
        build_engine(s2, http=refusing_http())


# --------------------------------------------------------------------------- review fixes
def test_testnet_engine_refuses_an_account_leverage_the_gate_never_approved(tmp_path):
    from types import SimpleNamespace

    from deltr.engine import build_engine
    from tests.conftest import refusing_http

    s = Settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY="k", BINANCE_SECRET_KEY="s", DELTR_STATE_DIR=str(tmp_path), DELTR_DEFAULT_LEVERAGE=2)  # type: ignore[call-arg]
    eng = build_engine(s, http=refusing_http())
    eng._verify_account_leverage(SimpleNamespace(leverage=2))  # matches: ok
    for bad in (10, 3, 1, None, float("nan")):
        with pytest.raises(RuntimeError, match="leverage"):
            eng._verify_account_leverage(SimpleNamespace(leverage=bad))


async def test_testnet_refuses_stress_that_could_touch_a_real_order_even_when_flat(tmp_path):
    from deltr.models import StressKind, StressScenario
    from deltr.stress import StressController, StressRefused
    from tests._fakes_d import make_stack

    s = make_stack(tmp_path, mode="testnet")
    ctl = StressController(s.state, s.portfolio, s.executor, s.hub, s.settings)
    for kind in (StressKind.DEX_LEG_FAIL, StressKind.FEED_STALE, StressKind.EQUITY_SHOCK):
        with pytest.raises(StressRefused):
            await ctl.apply(StressScenario(kind=kind, magnitude=3.5))
    assert s.portfolio.stress_flags.dex_leg_fail is False and s.portfolio.stress_flags.feed_stale is False and s.state.stress_active is None
    res = await ctl.apply(StressScenario(kind=StressKind.BASIS_SHOCK, magnitude=25.0))  # book arithmetic only: allowed on a flat book
    assert res.active_label
    await ctl.reset()


# --------------------------------------------------------------------------- LIVE opt-in
LIVE_BASE = dict(_env_file=None, DELTR_MODE="live")
FULL_LIVE = dict(
    LIVE_BASE,
    BINANCE_API_KEY="k-not-real", BINANCE_SECRET_KEY="s-not-real", BINANCE_API_ENV="mainnet",
    DELTR_LIVE_ACK=LIVE_ACK_PHRASE, DELTR_ONCHAIN_MODE="live", DELTR_ONCHAIN_ACK=ONCHAIN_ACK_PHRASE,
)


def test_live_refuses_to_build_naming_each_missing_requirement_in_turn():
    """Every part of the opt-in is refused on its own, and the message names it."""
    steps = [
        ({"BINANCE_API_KEY": "", "BINANCE_SECRET_KEY": ""}, "requires real Binance mainnet credentials"),
        ({"BINANCE_API_KEY": "k-not-real", "BINANCE_SECRET_KEY": "s-not-real"}, "BINANCE_API_ENV=mainnet"),
        ({"BINANCE_API_KEY": "k-not-real", "BINANCE_SECRET_KEY": "s-not-real", "BINANCE_API_ENV": "mainnet"},
         "DELTR_LIVE_ACK"),
        ({"BINANCE_API_KEY": "k-not-real", "BINANCE_SECRET_KEY": "s-not-real", "BINANCE_API_ENV": "mainnet",
          "DELTR_LIVE_ACK": LIVE_ACK_PHRASE}, "DELTR_ONCHAIN_MODE=live"),
        ({"BINANCE_API_KEY": "k-not-real", "BINANCE_SECRET_KEY": "s-not-real", "BINANCE_API_ENV": "mainnet",
          "DELTR_LIVE_ACK": LIVE_ACK_PHRASE, "DELTR_ONCHAIN_MODE": "live"}, "DELTR_ONCHAIN_ACK"),
    ]
    for extra, named in steps:
        with pytest.raises(Exception) as exc:
            load_settings(**dict(LIVE_BASE, **extra))
        assert named in str(exc.value), f"refusal did not name {named}: {exc.value}"
    # and with everything set it builds
    s = load_settings(**FULL_LIVE)
    assert s.mode == Mode.LIVE and s.live_arming_error() is None


def test_a_wrong_live_acknowledgement_is_not_accepted():
    for bad in ("yes", "true", "i-understand", LIVE_ACK_PHRASE[:-1]):
        with pytest.raises(Exception, match="DELTR_LIVE_ACK"):
            load_settings(**dict(FULL_LIVE, DELTR_LIVE_ACK=bad))


def test_live_caps_are_small_and_cannot_be_raised_past_the_table():
    s = load_settings(**FULL_LIVE)
    assert s.max_notional_usd == 250.0 and s.max_aggregate_usd == 1_000.0
    tight = load_settings(**dict(FULL_LIVE, DELTR_LIVE_MAX_NOTIONAL_USD=50))
    assert tight.max_notional_usd == 50.0                      # smaller is honoured
    loose = load_settings(**dict(FULL_LIVE, DELTR_LIVE_MAX_NOTIONAL_USD=100_000))
    assert loose.max_notional_usd == 250.0                     # larger is clamped by the table
    assert loose.risk_limits().max_notional_usd == 250.0       # and that is what the gate enforces


def test_live_defaults_to_maker_execution():
    """Taking the perp leg loses money on the measured funding: posting is the default."""
    s = load_settings(**FULL_LIVE)
    assert s.execution_style is ExecutionStyle.MAKER
    assert s.perp_fee_bps == s.perp_maker_fee_bps
    assert load_settings(**dict(FULL_LIVE, DELTR_EXECUTION_STYLE="taker")).execution_style is ExecutionStyle.TAKER


def test_no_secret_appears_in_a_live_redacted_view_or_repr():
    s = load_settings(**dict(FULL_LIVE, BINANCE_API_KEY="AAAsecretkeyAAA", BINANCE_SECRET_KEY="BBBsecretBBB"))
    blob = str(s.redacted()) + repr(s.hosts) + str(s.live_arming_error())
    assert "AAAsecretkeyAAA" not in blob and "BBBsecretBBB" not in blob


def test_futures_client_refuses_mainnet_credentials_without_the_explicit_opt_in():
    import httpx

    from deltr.venues.binance_futures import FuturesClient

    http = httpx.AsyncClient()
    with pytest.raises(ValueError, match="allow_mainnet_orders"):
        FuturesClient(http, "https://fapi.binance.com", api_key="k", secret_key="s")
    with pytest.raises(ValueError, match="fapi.binance.com"):
        FuturesClient(http, "https://not-binance.example.com", api_key="k", secret_key="s", allow_mainnet_orders=True)
    ok = FuturesClient(http, "https://fapi.binance.com", api_key="AAAkeyAAA", secret_key="BBBsecretBBB", allow_mainnet_orders=True)
    assert ok.has_credentials
    assert "AAAkeyAAA" not in repr(ok) and "BBBsecretBBB" not in repr(ok)
    # the keyless mainnet data client is unaffected and stays keyless
    assert FuturesClient(http, "https://fapi.binance.com").has_credentials is False


def test_live_router_asserts_its_own_host_credentials_and_arming():
    from deltr.executor import LiveRouter

    s = load_settings(**FULL_LIVE)

    class Futures:
        def __init__(self, base_url: str, creds: bool = True) -> None:
            self.base_url = base_url
            self.has_credentials = creds

    with pytest.raises(AssertionError, match="mainnet futures host"):
        LiveRouter(Futures("https://testnet.binancefuture.com"), object(), s, FILTERS)
    with pytest.raises(AssertionError, match="credentialed"):
        LiveRouter(Futures("https://fapi.binance.com", creds=False), object(), s, FILTERS)
    with pytest.raises(AssertionError, match="on-chain leg"):
        LiveRouter(Futures("https://fapi.binance.com"), None, s, FILTERS)
    paper = load_settings(_env_file=None, DELTR_MODE="paper")
    with pytest.raises(AssertionError, match="refuses to run in PAPER"):
        LiveRouter(Futures("https://fapi.binance.com"), object(), paper, FILTERS)
    r = LiveRouter(Futures("https://fapi.binance.com"), object(), s, FILTERS)
    assert r.mode == Mode.LIVE and r.is_maker


def test_the_shared_perp_base_grants_no_host_permission():
    """TestnetRouter and LiveRouter share order machinery but each asserts its own host."""
    from deltr.executor import LiveRouter, _PerpLegRouter

    assert issubclass(TestnetRouter, _PerpLegRouter) and issubclass(LiveRouter, _PerpLegRouter)
    assert not issubclass(LiveRouter, TestnetRouter) and not issubclass(TestnetRouter, LiveRouter)
    # the base itself has no constructor that could bypass either assertion
    assert "__init__" not in vars(_PerpLegRouter)
