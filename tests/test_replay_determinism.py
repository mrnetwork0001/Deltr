"""tests/test_replay_determinism.py — two ``--auto`` engine runs over the replay fixture
produce identical decision logs (``ReceiptStore.decision_log_sha256``).

Both runs use the same pinned clock (plan hashes cover ``created_at``; replay rows are
re-stamped onto the clock so freshness ages are the recorded ones), a private state
dir, and no network (``ReplayDexQuoter`` answers every re-quote from the fixture).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from deltr.engine import build_engine
from tests.conftest import REPLAY_FIXTURE, make_replay_settings, refusing_http

TICKS = 25


async def _run(state_dir: Path, clock_at: datetime) -> tuple[str, list[str], list[tuple[str, bool]], int]:
    settings = make_replay_settings(state_dir, DELTR_AUTO_EXECUTE=True)
    eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http(), clock=lambda: clock_at)
    await eng.start(loops=False, benchmark_iterations=500)
    try:
        for _ in range(TICKS - 1):
            await eng.hub.tick_once()
        statuses = [r.status for r in eng.receipts.all()]
        decisions = [(d.code, d.approved) for d in eng.state.decisions]
        return eng.receipts.decision_log_sha256(), statuses, decisions, len(eng.portfolio.positions("open"))
    finally:
        await eng.stop()


async def test_two_auto_runs_have_identical_decision_logs(tmp_path: Path):
    clock_at = datetime.now(timezone.utc)
    sha_a, statuses_a, decisions_a, open_a = await _run(tmp_path / "a", clock_at)
    sha_b, statuses_b, decisions_b, open_b = await _run(tmp_path / "b", clock_at)
    assert statuses_a and statuses_a[0] == "filled", "auto mode must execute the first actionable tick"
    assert open_a == 1 and open_b == 1, "max one open position per symbol"
    assert statuses_a == statuses_b and decisions_a == decisions_b
    assert sha_a == sha_b and len(sha_a) == 64


async def test_auto_run_records_its_receipt_and_never_double_executes(tmp_path: Path):
    clock_at = datetime.now(timezone.utc)
    settings = make_replay_settings(tmp_path / "c", DELTR_AUTO_EXECUTE=True)
    eng = build_engine(settings, replay_path=str(REPLAY_FIXTURE), min_edge_override=-50.0, http=refusing_http(), clock=lambda: clock_at)
    await eng.start(loops=False, benchmark_iterations=500)
    try:
        for _ in range(10):
            await eng.hub.tick_once()
        receipts = eng.receipts.all()
        assert len(receipts) == 1 and receipts[0].source.value == "auto" and receipts[0].status == "filled"
        assert eng.snapshot().status.open_positions == 1
        # the decision log is a function of the fixture, not of the wall clock between ticks
        entries = eng.receipts.decision_entries()
        assert entries[0]["code"] == "OK" and entries[0]["approved"] is True
    finally:
        await eng.stop()
