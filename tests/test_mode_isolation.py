"""tests/test_mode_isolation.py — PAPER can never reach an order endpoint; TESTNET is the
only mode with credentials; there is no LIVE mode; the routers refuse the wrong host."""
from __future__ import annotations

import inspect

import pytest

from deltr.config import HOSTS, MAX_NOTIONAL_BY_MODE, Mode, Settings, load_settings
from deltr.executor import PaperRouter, TestnetRouter
from deltr.models import SymbolFilters

FILTERS = SymbolFilters(symbol="BNBUSDT")
SECRET_WORDS = ("key", "secret", "token", "password", "credential")


def test_mode_has_exactly_paper_and_testnet():
    assert [m.value for m in Mode] == ["paper", "testnet"]
    assert not hasattr(Mode, "LIVE") and "live" not in {m.value for m in Mode}


def test_hosts_table_is_frozen_and_paper_has_no_order_endpoint():
    assert set(HOSTS) == {Mode.PAPER, Mode.TESTNET}
    assert HOSTS[Mode.PAPER]["futures_order_rest"] == ""
    for mode, hosts in HOSTS.items():
        for name, url in hosts.items():
            assert "fapi.binance.com" not in url and "api.binance.com" not in url, f"{mode}.{name} points at production"
            assert not url or url.startswith("https://"), f"{mode}.{name} must be https"
    assert "testnet" in HOSTS[Mode.TESTNET]["futures_order_rest"]
    assert MAX_NOTIONAL_BY_MODE[Mode.TESTNET] < MAX_NOTIONAL_BY_MODE[Mode.PAPER]


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
