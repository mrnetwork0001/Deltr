"""tests/test_skill_manifest.py — the Agent OS skill package is complete and in sync.

* ``skills/deltr-binance/SKILL.md`` frontmatter carries ``name``, ``description``, ``version``,
  ``license`` and ``metadata.version/author/openclaw`` (Skills-Hub + CONTRIBUTING fields) and parses
  as YAML with the ``requires.bins`` / ``install`` shell script of design section 8.1;
* it is mirrored byte-for-byte to ``.agents/skills/deltr-binance/SKILL.md``;
* the body has the sections the design asks for and links the three references;
* ``references/tools.md`` lists exactly the server's ``TOOL_NAMES`` and every error code / resource,
  and is byte-identical to what ``scripts/dump_mcp_tools.py`` renders from the real ``tools/list``;
* ``references/risk-model.md`` names every gate check;
* ``scripts/deltr.sh`` exists, is executable, parses, and the skill ships an identical copy + MIT LICENSE;
* nothing in the skill uses the scrubbed wording (decision 24), a wallet address or a key value.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from deltr.mcp.server import ERROR_CODES, TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "deltr-binance"
SKILL_MD = SKILL / "SKILL.md"
MIRROR = ROOT / ".agents" / "skills" / "deltr-binance" / "SKILL.md"
TOOLS_MD = SKILL / "references" / "tools.md"
SCRUBBED = ("binance-mcp-server`)", "MCP Server Suite", "risk-free", "guaranteed", "risk free")
REQUIRED_SECTIONS = ("Overview", "Preflight", "Command routing", "MCP tools", "Risk model", "Auth", "CONFIRM semantics", "Outputs")
EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _frontmatter(text: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    return m.group(1)


def _skill_files() -> list[Path]:
    return [SKILL_MD, MIRROR, *sorted((SKILL / "references").glob("*.md")), SKILL / "scripts" / "deltr.sh"]


def test_frontmatter_has_the_required_fields():
    fm = _frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    assert re.search(r"^name: deltr-binance$", fm, re.M)
    assert re.search(r"^description: .{40,}", fm, re.M)
    assert re.search(r"^version: 1\.0\.0$", fm, re.M) and re.search(r"^license: MIT$", fm, re.M)
    assert re.search(r"^metadata:\n  version: 1\.0\.0\n  author: mrnetwork0001\n  openclaw:", fm, re.M)
    assert "mrnetwork0001/Deltr.git" in fm and "python3 -m venv .venv" in fm


def test_frontmatter_parses_as_yaml_with_the_openclaw_install_block():
    yaml = pytest.importorskip("yaml")
    fm = yaml.safe_load(_frontmatter(SKILL_MD.read_text(encoding="utf-8")))
    assert fm["name"] == "deltr-binance" and fm["version"] == "1.0.0" and fm["license"] == "MIT"
    # The description must name every mode a caller can be in and state the custody model,
    # so an agent reading the Skills Hub entry cannot mistake LIVE for a simulation.
    desc = fm["description"]
    assert "Paper mode by default" in desc and "testnet" in desc
    assert "LIVE" in desc and "real mainnet orders" in desc
    assert "never holds a private key" in desc
    meta = fm["metadata"]
    assert meta["version"] == "1.0.0" and meta["author"] == "mrnetwork0001"
    openclaw = meta["openclaw"]
    assert openclaw["requires"]["bins"] == ["python3", "node"]
    (install,) = openclaw["install"]
    assert install["kind"] == "shell" and install["label"]
    script = install["script"]
    assert script.startswith("set -e") and "DELTR_HOME" in script and "requirements.txt" in script and "npm ci" in script


def test_mirror_is_byte_identical():
    assert MIRROR.read_bytes() == SKILL_MD.read_bytes()


def test_body_has_the_design_sections_and_links_the_references():
    text = SKILL_MD.read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    headings = [h.strip() for h in re.findall(r"^## (.+)$", body, re.M)]
    for section in REQUIRED_SECTIONS:
        assert any(h.startswith(section) for h in headings), f"missing section {section!r}: {headings}"
    for ref in ("references/tools.md", "references/cli.md", "references/risk-model.md"):
        assert f"]({ref})" in body, f"SKILL.md must link {ref}"
        assert (SKILL / ref).exists()
    for must in ("BINANCE_API_KEY", "BINANCE_SECRET_KEY", "BINANCE_API_ENV=testnet", "scripts/deltr.sh status", "plan_id", "--confirm"):
        assert must in body, must
    assert "prod" in body and "never" in body.lower()


def test_tools_reference_matches_the_server():
    text = TOOLS_MD.read_text(encoding="utf-8")
    listed = re.findall(r"^\| `(deltr_[a-z0-9_]+)` \|", text, re.M)  # tool names may contain digits (x402)
    assert listed == list(TOOL_NAMES) and len(listed) == len(TOOL_NAMES), "tools.md rows must be the server's TOOL_NAMES, in order"
    for res in ("deltr://status", "deltr://risk-limits", "deltr://config"):
        assert res in text
    for code in ERROR_CODES:
        assert f"`{code}`" in text, f"error code {code} missing from tools.md"


def test_tools_reference_is_generated_from_the_live_tools_list():
    """The table is exactly what ``scripts/dump_mcp_tools.py`` renders from ``tools/list`` (no network)."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dump_mcp_tools.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"references/tools.md is stale — run scripts/dump_mcp_tools.py --write\n{proc.stderr[-2000:]}"


def test_wrapper_script_ships_and_parses():
    for path in (ROOT / "scripts" / "deltr.sh", SKILL / "scripts" / "deltr.sh"):
        assert path.exists() and path.stat().st_mode & 0o111, f"{path} must be executable"
        subprocess.run(["bash", "-n", str(path)], check=True)
    assert (ROOT / "scripts" / "deltr.sh").read_bytes() == (SKILL / "scripts" / "deltr.sh").read_bytes()
    script = (ROOT / "scripts" / "deltr.sh").read_text(encoding="utf-8")
    for cmd in ("status)", "once)", "scan)", "explain)", "propose)", "execute)", "unwind)", "mcp)", "serve)"):
        assert cmd in script, f"deltr.sh must route {cmd[:-1]}"
    assert "--once --json" in script and "--mcp" in script and "DELTR_HOME" in script and ".venv/bin/python" in script
    assert (SKILL / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    for name in ("cli.md", "risk-model.md"):
        assert (SKILL / "references" / name).exists()


def test_risk_model_reference_names_every_check():
    import risk_gate

    risk = (SKILL / "references" / "risk-model.md").read_text(encoding="utf-8")
    assert [c for c in risk_gate.CHECK_ORDER if f"`{c}`" not in risk] == []
    assert len(risk_gate.CHECK_ORDER) == 19 and "19" in risk
    assert "measured" in risk.lower() and "µs" in risk


def test_skill_uses_no_scrubbed_wording_addresses_or_keys():
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        for bad in SCRUBBED:
            assert bad.lower() not in low, f"{path}: {bad!r}"
        assert not EVM_ADDRESS.search(text), f"{path}: wallet address in user-facing skill text"
        assert not re.search(r"BINANCE_(?:API|SECRET)_KEY\s*=\s*\S", text), f"{path}: key assignment with a value"
