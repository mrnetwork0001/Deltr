"""tests/test_secrets_hygiene.py — no key-shaped literal anywhere in the tracked repo, and no
surface (banner, redacted config, MCP activity log, status) ever prints a secret value even when keys
are configured.

* ``BINANCE_API_KEY=<value>`` / ``BINANCE_SECRET_KEY=<value>`` with a non-empty value is only allowed
  in ``.env.example`` (where it must be empty) and, for the Python test suite, as an obvious short
  placeholder passed to ``Settings(...)``;
* no 64-character hex literal (the shape of a Binance secret) appears in any tracked text file
  except as a hash / address in fixtures or docs;
* ``Settings.redacted()`` never carries a key value;
* ``deltr.mcp.activity.redact`` drops ``key`` / ``secret`` / ``token`` / ``password`` arguments,
  recursively.
"""
from __future__ import annotations

import re
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".md", ".sh", ".toml", ".txt", ".yaml", ".yml", ".example", ".env", ".jsonl", ".cfg", ".ini"}
# BINANCE_API_KEY=<value>  (env / shell / markdown style: any non-empty value that is not an obvious placeholder)
ASSIGN_ENV = re.compile(r"^\s*(?:export\s+)?BINANCE_(?:API|SECRET)_KEY\s*=\s*(\S.*)$", re.M)
# BINANCE_API_KEY="<value>" inside Python / TypeScript source: a real Binance key is 32+ plain
# alphanumerics, so hyphenated fakes such as "fake-testnet-key" / "k-not-real" are tolerated
ASSIGN_CODE = re.compile(r"BINANCE_(?:API|SECRET)_KEY\s*[:=]\s*['\"]([A-Za-z0-9]{32,})['\"]")
HEX64 = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{64}(?![A-Za-z0-9])")
PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\.\.\.|\$\{?[A-Z_]+\}?|your[-_ ]|\"\"|'')", re.I)
HASH_TAGS = ("sha256", "sha-256", "plan_hash", "hash", "0x", "digest", "checksum", "integrity")


SKIP_DIRS = {".git", ".venv", "node_modules", "state", ".next", "__pycache__", ".pytest_cache", "out"}


def _walk_files() -> list[Path]:
    """Fallback for source ZIPs without a .git directory (what a judge downloads)."""
    found: list[Path] = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found.extend(Path(base) / n for n in names)
    return found


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
        files = [ROOT / p for p in out.decode("utf-8").split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = _walk_files()
    return [
        p for p in files
        if p.suffix in TEXT_SUFFIXES and p.is_file()
        and "node_modules" not in p.parts and "ui/out" not in str(p) and p.name != "package-lock.json"
    ]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def test_no_key_assignment_with_a_value_outside_env_example():
    hits = []
    for path in _tracked_files():
        if path.name == ".env.example":
            continue
        text = _read(path)
        if text is None:
            continue
        rel = path.relative_to(ROOT)
        if path.suffix in {".py", ".ts", ".tsx", ".js"}:
            for m in ASSIGN_CODE.finditer(text):
                hits.append(f"{rel}: {m.group(0)[:60]}")
        else:
            for m in ASSIGN_ENV.finditer(text):
                value = m.group(1).split("#", 1)[0].strip()
                if value and not PLACEHOLDER.match(value):
                    hits.append(f"{rel}: {m.group(0).strip()[:60]}")
    assert hits == [], "\n".join(hits)


def test_no_64_hex_secret_shaped_literal_in_the_repo():
    hits = []
    for path in _tracked_files():
        text = _read(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not HEX64.search(line):
                continue
            low = line.lower()
            if any(tag in low for tag in HASH_TAGS):
                continue  # hashes / addresses appear in fixtures, receipts and docs by design
            hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()[:80]}")
    assert hits == [], "\n".join(hits)


def test_env_example_has_no_values_for_keys():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    seen = set()
    for name in ("BINANCE_API_KEY", "BINANCE_SECRET_KEY"):
        for line in text.splitlines():
            if line.lstrip("# ").startswith(name + "="):
                seen.add(name)
                assert line.split("=", 1)[1].strip() == "", line
    assert seen == {"BINANCE_API_KEY", "BINANCE_SECRET_KEY"}, "the template must list both key names (empty)"
    assert "BINANCE_API_ENV=testnet" in text


def test_settings_redacted_never_carries_key_values(tmp_path):
    from deltr.config import Settings

    key, secret = "AKIATESTKEYVALUE1234567890abcdefXYZ", "SUPERSECRETVALUE0987654321zyxwvuQRS"
    s = Settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY=key, BINANCE_SECRET_KEY=secret,  # type: ignore[call-arg]
                 DELTR_STATE_DIR=str(tmp_path), BINANCE_MCP_TOKEN="bearer-token-value-not-real-1234567890")
    red = s.redacted()
    flat = str(red)
    assert key not in flat and secret not in flat and "bearer-token-value" not in flat
    assert red["secrets_present"] is True and red["binance_mcp_token_present"] is True
    assert not any(k.lower().endswith(("_key", "secret", "token")) for k in red), sorted(red)


def test_activity_redact_strips_key_secret_token_password():
    from deltr.mcp.activity import redact

    args = {
        "capital_usd": 5000, "leverage": 2, "api_key": "AKIA-not-real", "BINANCE_SECRET_KEY": "s3cr3t-not-real",
        "token": "tok-not-real", "Password": "pw-not-real", "accessToken": "tok2-not-real",
        "nested": {"secret": "deep-not-real", "symbol": "BNBUSDT", "list": [{"apiKey": "k-not-real", "qty": 1}]},
        "text": "x" * 500,
    }
    out = redact(args)
    flat = str(out)
    for leaked in ("AKIA-not-real", "s3cr3t-not-real", "tok-not-real", "pw-not-real", "tok2-not-real", "deep-not-real", "k-not-real"):
        assert leaked not in flat, leaked
    assert out["capital_usd"] == 5000 and out["leverage"] == 2
    assert out["nested"]["symbol"] == "BNBUSDT" and out["nested"]["list"] == [{"qty": 1}]
    assert len(out["text"]) < 500 and "…" in out["text"]
    assert args["api_key"] == "AKIA-not-real", "redact must not mutate its input"


def test_banner_and_config_never_print_secret_values(tmp_path):
    """Keys never reach stdout / clients; an RPC provider key embedded in BSC_RPC_URL is stripped by
    every consumer (banner, /api/config, deltr://config, status)."""
    from fastapi.testclient import TestClient

    from deltr.api.app import create_app
    from deltr.config import Settings
    from deltr.engine import build_engine
    from deltr.mcp.activity import redact_url
    from deltr.mcp.server import build_mcp
    from main import banner
    from tests.conftest import refusing_http

    key, secret = "AKIATESTKEYVALUE1234567890abcdefXYZ", "SUPERSECRETVALUE0987654321zyxwvuQRS"
    s = Settings(_env_file=None, DELTR_MODE="testnet", BINANCE_API_KEY=key, BINANCE_SECRET_KEY=secret, DELTR_STATE_DIR=str(tmp_path),  # type: ignore[call-arg]
                 BSC_RPC_URL="https://rpc.example.com/v1/" + secret)
    eng = build_engine(s, http=refusing_http())
    text = banner(s, eng, 8000, mcp_stdio=False, ui_note="test")
    assert key not in text and secret not in text and "secrets    : present" in text
    assert "rpc.example.com" in text and "/v1/" not in text
    assert redact_url(s.rpc_urls[0]) == "https://rpc.example.com/…"
    status = str(eng.status().model_dump())
    assert key not in status and secret not in status
    assert key not in str(s.redacted()) and secret not in str(s.redacted())
    mcp = build_mcp(eng, eng.activity)
    app = create_app(eng, mcp, eng.activity, ui_dir=tmp_path / "no-ui")
    with TestClient(app) as c:
        resp = c.get("/api/config")
        cfg = resp.json()
        assert key not in resp.text and secret not in resp.text
        assert cfg["secrets_present"] is True and cfg["bsc_rpc_urls"][0] == "https://rpc.example.com/…"
