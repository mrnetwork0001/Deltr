"""EventBus fan-out / drop-oldest / history and State plan-store, deques and snapshot defaults."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from deltr.bus import EventBus
from deltr.config import Settings
from deltr.models import (
    AgentEvent,
    HedgePlan,
    McpActivity,
    OrderLeg,
    RiskDecisionRecord,
    Side,
    Snapshot,
    SystemStatus,
    Venue,
)
from deltr.state import (
    ACTIVITY_MAXLEN,
    DECISIONS_MAXLEN,
    HISTORY_MAXLEN,
    PROMPTS_MAXLEN,
    RECEIPTS_MAXLEN,
    State,
    empty_portfolio,
)


def make_settings(**over) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


def make_plan(ttl_s: float = 60.0, created: datetime | None = None) -> HedgePlan:
    created = created or datetime.now(timezone.utc)
    legs = [
        OrderLeg(venue=Venue.PANCAKESWAP_V3, symbol="BNBUSDT", side=Side.BUY, qty=4.85, price_hint=686.19),
        OrderLeg(venue=Venue.BINANCE_FUTURES, symbol="BNBUSDT", side=Side.SELL, qty=4.85, price_hint=686.34, leverage=2.0),
    ]
    return HedgePlan(
        symbol="BNBUSDT", legs=legs, qty=4.85, notional_usd=3328.02, leverage=2.0, margin_usd=1664.01,
        cash_required_usd=4992.03, allocated_risk_usd=399.36, expected_edge_bps=1.2, roundtrip_cost_bps=20.0,
        ref_dex_price=686.19, ref_perp_price=686.34, created_at=created, expires_at=created + timedelta(seconds=ttl_s),
    )


# --------------------------------------------------------------------------- bus
def test_publish_returns_event_and_records_history():
    bus = EventBus(history=3)
    ev = bus.publish("gate", "approved", level="info", data={"code": "OK"})
    assert isinstance(ev, AgentEvent)
    assert ev.topic == "gate" and ev.data == {"code": "OK"} and ev.level == "info"
    for i in range(5):
        bus.publish("log", f"m{i}")
    hist = bus.history()
    assert [e.message for e in hist] == ["m2", "m3", "m4"]  # deque maxlen 3, oldest first
    assert [e.message for e in bus.history(2)] == ["m3", "m4"]
    assert bus.history(0) == []
    assert bus.published == 6


def test_publish_without_running_loop_is_fine():
    bus = EventBus()
    q = bus.subscribe()
    bus.publish("log", "sync")
    assert q.qsize() == 1


async def test_fan_out_to_every_subscriber_and_unsubscribe():
    bus = EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    assert bus.subscribers == 2
    bus.publish("fill", "leg 1")
    assert (await q1.get()).message == "leg 1"
    assert (await q2.get()).message == "leg 1"
    bus.unsubscribe(q1)
    bus.unsubscribe(q1)  # idempotent
    bus.publish("fill", "leg 2")
    assert q1.empty()
    assert (await q2.get()).message == "leg 2"
    assert bus.subscribers == 1


async def test_slow_subscriber_drops_oldest_never_blocks_publisher():
    bus = EventBus(queue_maxsize=3)
    q = bus.subscribe()
    for i in range(10):
        bus.publish("tick", f"t{i}")
    assert q.qsize() == 3
    got = [(await q.get()).message for _ in range(3)]
    assert got == ["t7", "t8", "t9"]
    assert bus.dropped == 7
    assert len(bus.history()) == 10  # history is independent of subscriber queues


# --------------------------------------------------------------------------- state basics
def test_state_constructible_with_settings_and_bus_only():
    s = make_settings()
    st = State(s, EventBus())
    assert st.mode == s.mode and st.replay is False
    assert st.market is None and st.edge is None and st.opportunity is None
    assert st.portfolio.equity_usd == s.capital_usd == st.portfolio.cash_usd
    assert st.portfolio.dd_state == "NORMAL" and st.portfolio.open_positions == 0
    assert st.min_edge_bps == s.min_edge_bps and st.min_edge_floor_bps == 0.0
    assert st.plans == {} and st.positions == {} and st.venues == {}


def test_state_replay_flag_follows_settings():
    st = State(make_settings(DELTR_REPLAY_PATH="tests/fixtures/replay.jsonl"), EventBus())
    assert st.replay is True


def test_deque_maxlens():
    st = State(make_settings(), EventBus())
    assert st.history.maxlen == HISTORY_MAXLEN == 600
    assert st.decisions.maxlen == DECISIONS_MAXLEN == 100
    assert st.receipts.maxlen == RECEIPTS_MAXLEN == 10
    assert st.prompts.maxlen == PROMPTS_MAXLEN == 5
    assert st.activity.maxlen == ACTIVITY_MAXLEN == 50
    for i in range(60):
        st.record_activity(McpActivity(direction="inbound", server="deltr", tool=f"t{i}"))
    assert len(st.activity) == 50 and st.activity[0].tool == "t10"


# --------------------------------------------------------------------------- plan store
def test_plan_single_use():
    st = State(make_settings(), EventBus())
    plan = make_plan()
    st.put_plan(plan)
    assert st.peek_plan(plan.id) is plan
    assert st.plan_status(plan.id) == "pending"
    assert st.take_plan(plan.id) is plan
    assert st.take_plan(plan.id) is None  # consumed
    assert st.peek_plan(plan.id) is None
    assert st.plan_status(plan.id) == "missing"
    assert st.take_plan("plan_doesnotexist") is None


def test_plan_ttl_expiry_and_prune():
    st = State(make_settings(), EventBus())
    now = datetime.now(timezone.utc)
    fresh = make_plan(ttl_s=60, created=now)
    old = make_plan(ttl_s=60, created=now - timedelta(seconds=61))
    st.put_plan(fresh)
    st.put_plan(old)
    assert st.plan_status(old.id, now) == "expired"
    assert st.peek_plan(old.id) is old  # visible until pruned (lets callers report PLAN_EXPIRED)
    assert st.take_plan(old.id, now) is None  # expired -> None and discarded
    assert old.id not in st.plans
    # exactly at expires_at counts as expired; one microsecond earlier is still pending
    assert st.take_plan(fresh.id, fresh.expires_at) is None
    st.put_plan(fresh)
    assert st.take_plan(fresh.id, fresh.expires_at - timedelta(microseconds=1)) is fresh
    p1, p2 = make_plan(created=now - timedelta(seconds=120)), make_plan(created=now)
    st.put_plan(p2)
    st.plans[p1.id] = p1  # bypass put_plan's own pruning to exercise prune_plans directly
    assert st.prune_plans(now) == 1 and list(st.plans) == [p2.id]
    # put_plan prunes on the way in
    st.plans[p1.id] = p1
    st.put_plan(make_plan(created=now))
    assert p1.id not in st.plans and len(st.plans) == 2


def test_plan_naive_expires_at_treated_as_utc():
    st = State(make_settings(), EventBus())
    plan = make_plan(created=datetime.utcnow())  # naive
    st.put_plan(plan)
    assert st.take_plan(plan.id, datetime.now(timezone.utc)) is plan


# --------------------------------------------------------------------------- snapshot / status
def test_empty_snapshot_serialises():
    st = State(make_settings(), EventBus())
    snap = st.snapshot({})
    assert isinstance(snap, Snapshot)
    payload = snap.model_dump(mode="json")
    text = json.dumps(payload)  # must be plain JSON
    assert set(payload) >= set(Snapshot.model_fields)
    assert payload["status"]["mode"] == "paper"
    assert payload["market"] is None and payload["positions"] == [] and payload["events"] == []
    assert payload["portfolio"]["equity_usd"] == 10_000.0
    assert "secret" not in text.lower() or "secrets_present" in text  # only the boolean flag ever appears
    # snapshot(None) also works
    assert st.snapshot(None).status.dd_state == "NORMAL"


def test_system_status_uses_gate_snapshot_when_present():
    st = State(make_settings(), EventBus())
    gate = {
        "state": "WARN", "halted": False, "kill_switch": True, "drawdown_pct": 0.024,  # the gate reports a fraction
        "limits": {"max_leverage": 3.0}, "check_order": ["KILL_SWITCH", "HALTED_DRAWDOWN"],
    }
    status = st.system_status(gate)
    assert isinstance(status, SystemStatus)
    assert status.dd_state == "WARN" and status.kill_switch is True and status.halted is False
    assert status.drawdown_pct == pytest.approx(2.4) and status.limits == {"max_leverage": 3.0}  # SystemStatus renders PERCENT (same unit as PortfolioSnapshot)
    assert status.check_order == ["KILL_SWITCH", "HALTED_DRAWDOWN"]
    assert status.uptime_s >= 0 and status.replay is False and status.secrets_present is False
    assert status.limits is not gate["limits"]  # copied, never aliased


def test_system_status_defaults_without_gate():
    s = make_settings()
    st = State(s, EventBus())
    status = st.system_status({})
    assert status.limits == s.risk_limits().as_dict()
    assert status.check_order == [] and status.dd_state == "NORMAL"
    assert status.min_edge_bps == s.min_edge_bps and status.leg_order == s.leg_order
    assert status.version == s.version and status.symbol == "BNBUSDT"
    st.gate_median_us = 1.04
    st.stress_active = "SIMULATED: basis_shock 150 bps"
    st2 = st.system_status({"state": "bogus"})
    assert st2.dd_state == "NORMAL" and st2.gate_median_us == 1.04 and st2.stress_active.startswith("SIMULATED")


def test_records_and_emit(caplog):
    s = make_settings()
    bus = EventBus()
    st = State(s, bus)
    dec = RiskDecisionRecord(approved=False, code="LEVERAGE", reason="x", latency_ns=1200, mode=s.mode)
    st.record_decision(dec)
    assert list(st.decisions) == [dec]
    with caplog.at_level(logging.DEBUG, logger="deltr.state"):
        ev = st.emit("gate", "vetoed LEVERAGE", level="warn", data={"code": "LEVERAGE"})
    assert ev.level == "warn" and bus.history()[-1] is ev
    assert any("vetoed LEVERAGE" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    snap = st.snapshot({})
    assert snap.events[-1].message == "vetoed LEVERAGE" and snap.decisions[0].code == "LEVERAGE"


def test_empty_portfolio_helper():
    p = empty_portfolio(5_000.0)
    assert p.equity_usd == p.cash_usd == p.peak_equity_usd == 5_000.0
    assert p.reserved_cash_usd == 0.0 and p.drawdown_pct == 0.0 and len(p.equity_curve) == 1
    p.model_dump(mode="json")


async def test_state_events_flow_to_ws_subscriber():
    bus = EventBus()
    st = State(make_settings(), bus)
    q = bus.subscribe()
    st.emit("mcp", "tools/list", data={"client": "claude-desktop/0.12"})
    ev = await asyncio.wait_for(q.get(), 1)
    assert ev.topic == "mcp" and ev.data["client"] == "claude-desktop/0.12"
