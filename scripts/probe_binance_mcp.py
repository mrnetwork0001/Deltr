#!/usr/bin/env python
"""scripts/probe_binance_mcp.py — one-shot MCP ``initialize`` + ``tools/list`` against the official Binance MCP server.

Streamable-HTTP JSON-RPC over plain httpx (no SDK), so the raw responses are visible:

    BINANCE_MCP_URL=https://agent.binance.com/mcp/agentic \\
    BINANCE_MCP_TOKEN=<optional bearer> \\
    .venv/bin/python scripts/probe_binance_mcp.py [--timeout 15] [--out tests/fixtures/binance_mcp_tools.json]

Prints every JSON-RPC response (and the HTTP status / ``www-authenticate`` header on
401).  Writes the fixture ONLY when ``tools/list`` returns at least one tool, replacing the
shipped transcription with a first-party capture and stamping a ``provenance`` block that
says so.  Exits 0 with a clear "unauthorized / unreachable" message otherwise — the endpoint is
OAuth-gated (RFC 9728 ``resource_metadata`` challenge), so a 401 is the expected
outcome without a browser-completed OAuth session.  This script never calls
``tools/call``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://agent.binance.com/mcp/agentic"
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "binance_mcp_tools.json"
PROTOCOL_VERSION = "2025-06-18"


def parse_body(resp: httpx.Response) -> Optional[Any]:
    """Return the JSON-RPC message from a JSON or SSE (``text/event-stream``) body."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw": text[:500]} if text else None


def rpc(client: httpx.Client, url: str, method: str, params: Optional[dict] = None, *, rid: Optional[int], session: Optional[str], token: Optional[str]) -> tuple[httpx.Response, Optional[Any]]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if rid is not None:
        msg["id"] = rid
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.post(url, json=msg, headers=headers)
    body = parse_body(resp)
    print(f"--> {method}  HTTP {resp.status_code}  {resp.headers.get('content-type', '')}")
    if resp.status_code == 401:
        print(f"    www-authenticate: {resp.headers.get('www-authenticate', '<absent>')}")
    print("<-- " + (json.dumps(body, indent=2) if body is not None else "<empty body>"))
    return resp, body


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("BINANCE_MCP_URL", DEFAULT_URL))
    ap.add_argument("--token", default=os.environ.get("BINANCE_MCP_TOKEN") or None, help="bearer token (optional)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    print(f"probe: {args.url}  (bearer: {'present' if args.token else 'absent'})")
    try:
        with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
            init_resp, init_body = rpc(
                client, args.url, "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "deltr-probe", "version": "1.0.0"}},
                rid=1, session=None, token=args.token,
            )
            if init_resp.status_code == 401:
                print("\nRESULT: unauthorized — the endpoint is OAuth-gated; complete OAuth in a supported client "
                      "(e.g. `claude mcp add binance-mcp-server --transport http <url>` then /mcp) and re-run with BINANCE_MCP_TOKEN.")
                return 0
            if init_resp.status_code >= 400 or not isinstance(init_body, dict) or "result" not in init_body:
                print(f"\nRESULT: initialize failed (HTTP {init_resp.status_code}); no tools discovered, fixture not written.")
                return 0
            server_info = init_body["result"].get("serverInfo", {})
            print(f"serverInfo: {server_info}")
            session = init_resp.headers.get("mcp-session-id")
            rpc(client, args.url, "notifications/initialized", {}, rid=None, session=session, token=args.token)
            list_resp, list_body = rpc(client, args.url, "tools/list", {}, rid=2, session=session, token=args.token)
            if list_resp.status_code == 401:
                print("\nRESULT: initialize succeeded anonymously but tools/list is unauthorized (OAuth required for discovery).")
                return 0
            tools = (list_body or {}).get("result", {}).get("tools", []) if isinstance(list_body, dict) else []
            if not tools:
                print("\nRESULT: tools/list returned no tools; fixture not written.")
                return 0
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            # Overwrite the shipped transcription with a FIRST-PARTY capture, and say so in the
            # provenance block the fixture is required to carry (tests/test_shim_tool_names.py).
            provenance = {
                "captured_from_our_own_session": True,
                "official_endpoint_reached_from_this_machine": True,
                "official_endpoint": args.url,
                "method": "captured_from_our_own_oauth_session",
                "captured_on": datetime.now(timezone.utc).date().isoformat(),
                "statement": (
                    f"Captured by scripts/probe_binance_mcp.py from an authorised MCP session against {args.url} "
                    f"on {datetime.now(timezone.utc).date().isoformat()}: this is the endpoint's own tools/list "
                    "response, not a transcription."
                ),
                "claim": "tools/list as returned to this machine by the official endpoint",
                "not_claimed": ["any tool was called", "any order was placed"],
                "source_reported_counts": {"tools_total": len(tools)},
                "observations": [f"{len(tools)} tool(s) visible to this session"],
            }
            out.write_text(
                json.dumps({"url": args.url, "serverInfo": server_info, "provenance": provenance, "tools": tools}, indent=2),
                encoding="utf-8",
            )
            print(f"\nRESULT: {len(tools)} tool(s) discovered: {[t.get('name') for t in tools]}\nwrote {out}")
            return 0
    except httpx.HTTPError as exc:
        print(f"\nRESULT: unreachable — {type(exc).__name__}: {exc}\n(agent.binance.com is DNS-blocked on some networks; "
              "try a public resolver such as 1.1.1.1 or a hotspot). No tools discovered, fixture not written.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
