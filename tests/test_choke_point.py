"""tests/test_choke_point.py — AST-level import discipline.

* ``deltr/mcp/*``, ``deltr/api/*`` and ``agents/*`` import neither ``deltr.venues.*``
  nor the ``Executor`` / ``PaperRouter`` / ``TestnetRouter`` / ``LiveRouter`` symbols: every order goes
  through the Engine -> Executor choke point.
* Routers are defined in ``deltr/executor.py`` and constructed only by ``deltr/engine.py``.
* ``risk_gate`` is imported only where the design allows (portfolio, executor, engine,
  main — plus ``deltr/config.py``, frozen, which types its ``risk_limits()`` helper).
* Library code never prints to stdout (the stdio MCP transport owns it).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NO_VENUE_OR_ROUTER = sorted(list((ROOT / "deltr" / "mcp").glob("*.py")) + list((ROOT / "deltr" / "api").glob("*.py")) + list((ROOT / "agents").glob("*.py")))
ROUTER_SYMBOLS = {"Executor", "PaperRouter", "TestnetRouter", "LiveRouter"}
RISK_GATE_ALLOWED = {"deltr/portfolio.py", "deltr/executor.py", "deltr/engine.py", "main.py", "deltr/config.py"}
ROUTER_IMPORTERS_ALLOWED = {"deltr/engine.py"}
LIBRARY_FILES = sorted(p for p in list((ROOT / "deltr").rglob("*.py")) + list((ROOT / "agents").glob("*.py")) + [ROOT / "risk_gate.py"])


def _imports(path: Path) -> list[tuple[str, set[str]]]:
    """[(module, {names})] for every import statement in the file."""
    out: list[tuple[str, set[str]]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name, set()))
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", {a.name for a in node.names}))
    return out


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def test_mcp_api_and_agents_never_import_venues_or_routers():
    assert NO_VENUE_OR_ROUTER, "nothing to scan"
    for path in NO_VENUE_OR_ROUTER:
        for mod, names in _imports(path):
            assert not mod.startswith("deltr.venues"), f"{_rel(path)} imports {mod}"
            assert mod != "deltr.executor", f"{_rel(path)} imports the executor"
            assert not (names & ROUTER_SYMBOLS), f"{_rel(path)} imports {names & ROUTER_SYMBOLS}"


def test_routers_are_defined_once_and_constructed_only_by_the_engine():
    definers: dict[str, list[str]] = {}
    for path in LIBRARY_FILES + [ROOT / "main.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in ROUTER_SYMBOLS:
                definers.setdefault(node.name, []).append(_rel(path))
        if _rel(path) == "deltr/executor.py":
            continue
        for mod, names in _imports(path):
            if mod == "deltr.executor" and names & {"PaperRouter", "TestnetRouter", "LiveRouter"}:
                assert _rel(path) in ROUTER_IMPORTERS_ALLOWED, f"{_rel(path)} imports a router"
    assert definers == {"Executor": ["deltr/executor.py"], "PaperRouter": ["deltr/executor.py"],
                        "TestnetRouter": ["deltr/executor.py"], "LiveRouter": ["deltr/executor.py"]}


def test_risk_gate_is_imported_only_where_allowed():
    importers = set()
    for path in LIBRARY_FILES + [ROOT / "main.py"]:
        if path.name == "risk_gate.py":
            continue
        for mod, _ in _imports(path):
            if mod == "risk_gate" or mod.startswith("risk_gate."):
                importers.add(_rel(path))
    assert importers <= RISK_GATE_ALLOWED, f"unexpected risk_gate importers: {importers - RISK_GATE_ALLOWED}"
    assert "deltr/engine.py" in importers, "the engine constructs the gate"


def test_library_code_never_prints_to_stdout():
    """print() is allowed only in main.py and scripts/; library modules log to stderr."""
    offenders: list[str] = []
    for path in LIBRARY_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                # allow print(..., file=sys.stderr) and the risk_gate __main__ demo block
                if any(k.arg == "file" for k in node.keywords):
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno}")
    allowed_prefixes = ("risk_gate.py",)  # its `if __name__ == "__main__":` benchmark block
    offenders = [o for o in offenders if not o.startswith(allowed_prefixes)]
    assert not offenders, f"stdout prints in library code: {offenders}"


def test_state_module_does_not_import_risk_gate():
    mods = {m for m, _ in _imports(ROOT / "deltr" / "state.py")}
    assert "risk_gate" not in mods
