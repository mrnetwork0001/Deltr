"""tests/conftest.py — shared fixtures + the network guard.

* No test in the default run may touch the network: the real ``httpx`` transports
  are patched to raise unless ``DELTR_LIVE_TESTS=1``.  ``httpx.MockTransport`` and
  ``httpx.ASGITransport`` keep working (they never reach the real transports).
* Developer ``.env`` / ``DELTR_*`` / ``BINANCE_*`` environment values are stripped
  so settings built in tests are exactly what the test asked for.
* Replay-driven engine fixtures for the integration / API / determinism tests:
  ``replay_path``, ``replay_settings``, ``engine`` (async, started without loops),
  ``mock_http`` (an httpx client whose transport refuses every request).
* ``fake_router`` / ``fake_hub`` re-export agent D's offline fakes from
  ``tests/_fakes_d.py`` for tests that want them as fixtures.
"""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "replay.jsonl"
LIVE = os.environ.get("DELTR_LIVE_TESTS") == "1"

_KEEP_ENV = {"DELTR_LIVE_TESTS", "DELTR_DEBUG"}


class NetworkBlocked(RuntimeError):
    """A test tried to use a real httpx transport."""


def _blocked_async(self: Any, request: httpx.Request) -> Any:  # pragma: no cover - only hit by a misbehaving test
    raise NetworkBlocked(f"network blocked in tests: {request.method} {request.url} (set DELTR_LIVE_TESTS=1 for live tests)")


def _blocked_sync(self: Any, request: httpx.Request) -> Any:  # pragma: no cover
    raise NetworkBlocked(f"network blocked in tests: {request.method} {request.url} (set DELTR_LIVE_TESTS=1 for live tests)")


@pytest.fixture(scope="session", autouse=True)
def _offline_and_clean_env() -> Iterator[None]:
    mp = pytest.MonkeyPatch()
    for key in list(os.environ):
        if (key.startswith("DELTR_") or key.startswith("BINANCE_")) and key not in _KEEP_ENV:
            mp.delenv(key, raising=False)
    if not LIVE:
        mp.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async, raising=True)
        mp.setattr(httpx.HTTPTransport, "handle_request", _blocked_sync, raising=True)
    try:
        yield
    finally:
        mp.undo()


# --------------------------------------------------------------------------- helpers
def free_port(start: int = 8010) -> int:
    """A free TCP port >= ``start`` on 127.0.0.1 (never 3000/3001, which belong to other apps)."""
    for port in range(start, start + 200):
        if port in (3000, 3001):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port found")


def make_replay_settings(state_dir: Path, **extra: Any):
    """PAPER settings on the replay fixture with the demo min-edge override (-50 bps) and a private state dir."""
    from deltr.config import Settings

    kw: dict[str, Any] = {
        "DELTR_MODE": "paper",
        "DELTR_STATE_DIR": str(state_dir),
        "DELTR_MIN_EDGE_BPS": -50.0,
        "DELTR_CAPITAL_USD": 10_000.0,
        "DELTR_REPLAY_PATH": str(REPLAY_FIXTURE),
    }
    kw.update(extra)
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def refusing_http() -> httpx.AsyncClient:
    """An httpx client that fails loudly if anything tries to use it (replay engines never should)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise NetworkBlocked(f"unexpected HTTP call in a replay test: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2.0)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def replay_path() -> Path:
    assert REPLAY_FIXTURE.exists(), f"missing replay fixture {REPLAY_FIXTURE}"
    return REPLAY_FIXTURE


@pytest.fixture
def replay_settings(tmp_path: Path):
    return make_replay_settings(tmp_path / "state")


@pytest.fixture
def mock_http() -> Iterator[httpx.AsyncClient]:
    client = refusing_http()
    yield client
    try:
        asyncio.get_event_loop_policy()
        loop = asyncio.new_event_loop()
        loop.run_until_complete(client.aclose())
        loop.close()
    except Exception:  # noqa: BLE001 - closing a mock client is best effort
        pass


@pytest.fixture
async def engine(replay_settings, replay_path: Path) -> AsyncIterator[Any]:
    """A started (no background loops) PAPER engine on the replay fixture."""
    from deltr.engine import build_engine

    eng = build_engine(replay_settings, replay_path=str(replay_path), min_edge_override=-50.0, http=refusing_http())
    await eng.start(loops=False, benchmark_iterations=1_000)
    try:
        yield eng
    finally:
        await eng.stop()


@pytest.fixture
def fake_router():
    from tests._fakes_d import FakeRouter

    return FakeRouter


@pytest.fixture
def fake_hub():
    from tests._fakes_d import FakeHub

    return FakeHub
