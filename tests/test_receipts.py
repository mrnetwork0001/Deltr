"""Receipts: make_receipt fields, sha256 stability, ReceiptStore deque + JSONL + decision-log hash."""
from __future__ import annotations

import json

from deltr.config import Mode
from deltr.models import DataSource, Fill, RiskDecisionRecord, Side, TraceSource, TraceStep, Venue, sha256_of
from deltr.receipts import STORE_MAXLEN, ReceiptStore, make_receipt
from tests._fakes_d import FakeState, make_market, make_plan, make_settings


def _decision(plan, code="OK", approved=True):
    return RiskDecisionRecord(plan_id=plan.id, approved=approved, code=code, reason=f"{code} reason", latency_ns=1200, mode=Mode.PAPER)


def _fills(ms):
    return [
        Fill(leg_index=0, venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.BUY, qty=4.85, price=686.2, fee_usd=0.0056, ref="paper", simulated=True, source=DataSource.PAPER),
        Fill(leg_index=1, venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.SELL, qty=4.85, price=686.2, fee_usd=1.66, ref="paper", simulated=True, source=DataSource.PAPER, client_id="DLTRabcdef01" + "11"),
    ]


def _receipt(plan, ms, status="filled", code="OK", stress=None):
    steps = [TraceStep(step="plan", status="ok", summary="taken"), TraceStep(step="gate", status="ok", summary="approved")]
    return make_receipt(plan, _decision(plan, code, code == "OK"), _fills(ms), status, steps, Mode.PAPER, TraceSource.MCP, "claude-desktop/0.12",
                        "pos_1", 0.0, 1.6656, 42, stress)


def test_make_receipt_fills_every_field_and_seals(tmp_path):
    ms = make_market()
    plan = make_plan(ms, prompt="Hedge 5k at 2x")
    r = _receipt(plan, ms)
    assert r.plan_id == plan.id and r.plan.plan_hash == plan.plan_hash and r.prompt == "Hedge 5k at 2x"
    assert r.source == TraceSource.MCP and r.client == "claude-desktop/0.12" and r.mode == Mode.PAPER
    assert r.status == "filled" and r.position_id == "pos_1" and r.legging_window_ms == 42
    assert len(r.fills) == 2 and [s.step for s in r.steps] == ["plan", "gate"]
    assert len(r.sha256) == 64 and r.sha256 == sha256_of(r.digest_body())


def test_sha256_is_stable_across_ids_and_timestamps_but_not_content(tmp_path):
    ms = make_market()
    plan = make_plan(ms)
    a, b = _receipt(plan, ms), _receipt(plan, ms)
    assert a.id != b.id and a.sha256 == b.sha256  # id/ts are not part of the digest
    c = _receipt(plan, ms, status="vetoed", code="LEVERAGE")
    d = _receipt(plan, ms, stress="SIMULATED · basis_shock 150")
    assert len({a.sha256, c.sha256, d.sha256}) == 3


def test_store_deque_jsonl_and_state_mirror(tmp_path):
    settings = make_settings(tmp_path)
    state = FakeState(settings)
    store = ReceiptStore(state, settings.receipts_path)
    ms = make_market()
    ids = []
    for i in range(12):
        r = _receipt(make_plan(ms), ms)
        store.put(r)
        ids.append(r.id)
    assert len(store) == 12 and store.get(ids[0]) is not None and store.get("nope") is None
    assert [r.id for r in store.recent(3)] == ids[-3:]
    assert len(state.receipts) == 10  # State keeps the last 10; the store keeps the log
    lines = open(settings.receipts_path).read().splitlines()
    assert len(lines) == 12
    row = json.loads(lines[-1])
    assert row["id"] == ids[-1] and row["sha256"] == store.get(ids[-1]).sha256 and row["mode"] == "paper"


def test_store_is_bounded(tmp_path):
    store = ReceiptStore(None, None)
    ms = make_market()
    first = None
    for i in range(STORE_MAXLEN + 5):
        r = _receipt(make_plan(ms), ms)
        first = first or r
        store.put(r)
    assert len(store) == STORE_MAXLEN and store.get(first.id) is None


def test_decision_log_sha256_is_deterministic_and_order_sensitive(tmp_path):
    ms = make_market()
    plan_ok = make_plan(ms, created_at=ms.ts)
    plan_veto = make_plan(ms, created_at=ms.ts, leverage=10.0)

    def run(order):
        store = ReceiptStore(None, None)
        for kind in order:
            if kind == "ok":
                store.put(_receipt(plan_ok, ms))
            else:
                store.put(_receipt(plan_veto, ms, status="vetoed", code="LEVERAGE"))
        return store.decision_log_sha256()

    assert run(["ok", "veto", "ok"]) == run(["ok", "veto", "ok"])
    assert run(["ok", "veto", "ok"]) != run(["veto", "ok", "ok"])
    assert ReceiptStore(None, None).decision_log_sha256() == ReceiptStore(None, None).decision_log_sha256()
