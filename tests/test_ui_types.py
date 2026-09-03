"""ui/lib/types.ts must mirror deltr.models.Snapshot field-for-field.

Walks `Snapshot.model_json_schema()` (including `$defs` for nested models) and
asserts that every property name of every model reachable from Snapshot appears
in the TypeScript interface of the same name.  Computed fields (Quote.mid,
RiskDecisionRecord.latency_us, TradeProposal.is_delta_neutral) are part of the
serialisation schema and therefore must be present too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from deltr.models import Snapshot

ROOT = Path(__file__).resolve().parents[1]
TYPES_TS = ROOT / "ui" / "lib" / "types.ts"

_IFACE_RE = re.compile(r"export\s+interface\s+(\w+)\s*\{(.*?)\n\}", re.S)
_PROP_RE = re.compile(r"^\s*(\w+)\??\s*:", re.M)


def parse_interfaces(src: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for m in _IFACE_RE.finditer(src):
        name, body = m.group(1), m.group(2)
        out[name] = {p.group(1) for p in _PROP_RE.finditer(body)}
    return out


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _collect_refs(node, acc: set[str]) -> None:
    """Every `$ref` reachable from a schema node."""
    if isinstance(node, dict):
        if "$ref" in node:
            acc.add(_ref_name(node["$ref"]))
        for v in node.values():
            _collect_refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, acc)


def reachable_models(schema: dict) -> dict[str, dict]:
    """Snapshot itself plus every object model in $defs reachable from it (enums excluded)."""
    defs = schema.get("$defs", {})
    models = {"Snapshot": schema}
    todo = set()
    _collect_refs({k: v for k, v in schema.items() if k != "$defs"}, todo)
    while todo:
        name = todo.pop()
        if name in models or name not in defs:
            continue
        d = defs[name]
        if "properties" not in d:  # enums (Mode, Venue, DataSource, ...) are TS string unions, not interfaces
            continue
        models[name] = d
        _collect_refs(d, todo)
    return models


@pytest.fixture(scope="module")
def ts_interfaces() -> dict[str, set[str]]:
    assert TYPES_TS.exists(), f"missing {TYPES_TS}"
    ifaces = parse_interfaces(TYPES_TS.read_text(encoding="utf-8"))
    assert "Snapshot" in ifaces, "types.ts must export `interface Snapshot`"
    return ifaces


@pytest.fixture(scope="module")
def models() -> dict[str, dict]:
    schema = Snapshot.model_json_schema(mode="serialization")
    return reachable_models(schema)


def test_every_reachable_model_has_an_interface(ts_interfaces, models):
    missing = sorted(n for n in models if n not in ts_interfaces)
    assert not missing, f"types.ts lacks interfaces for: {missing}"


def test_every_property_is_mirrored(ts_interfaces, models):
    problems: list[str] = []
    for name, schema in models.items():
        want = set(schema.get("properties", {}).keys())
        have = ts_interfaces.get(name, set())
        gap = sorted(want - have)
        if gap:
            problems.append(f"{name}: missing {gap}")
    assert not problems, "\n".join(problems)


def test_no_stray_properties_on_snapshot(ts_interfaces, models):
    """The reverse direction for the top-level payload: the UI must not invent Snapshot keys."""
    want = set(models["Snapshot"]["properties"].keys())
    extra = sorted(ts_interfaces["Snapshot"] - want)
    assert not extra, f"types.ts Snapshot has keys not in the pydantic model: {extra}"


def test_computed_fields_present(ts_interfaces):
    assert "mid" in ts_interfaces["Quote"]
    assert "latency_us" in ts_interfaces["RiskDecisionRecord"]
    assert "is_delta_neutral" in ts_interfaces["TradeProposal"]


def test_mock_snapshot_validates():
    mock = ROOT / "ui" / "mock" / "snapshot.json"
    assert mock.exists(), "ui/mock/snapshot.json is the UI's first data source"
    snap = Snapshot.model_validate_json(mock.read_text(encoding="utf-8"))
    assert snap.positions and snap.receipts and snap.decisions and snap.activity
    assert any(not d.approved for d in snap.decisions), "mock needs a veto decision"
    assert len(snap.receipts[0].steps) == 8 and snap.receipts[0].sha256
