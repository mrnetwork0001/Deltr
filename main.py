#!/usr/bin/env python3
"""
Deltr — CEX <-> DEX delta-neutral arbitrage agent on Binance Agent OS + MCP.
Built for the Binance Agent OS Mini Hackathon (Track A: skill · Track B: connect your MCPs).

One command runs everything:

    python main.py                       # PAPER mode, keyless: landing page on :8000, dashboard at /app/, MCP at /mcp
    python main.py --mcp                 # + stdio MCP transport in the SAME process (logs -> stderr)
    python main.py --once --json         # one tick + scan + explain + propose + gate pre-check as JSON
    python main.py --replay tests/fixtures/replay.jsonl   # deterministic replay of a recorded feed

Modes are PAPER (simulated fills on live prices) or TESTNET (real Binance USDS-M
Futures testnet orders; needs BINANCE_API_KEY / BINANCE_SECRET_KEY).  There is no
LIVE mode.  Every order passes the deterministic zero-LLM risk gate first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Optional

from deltr.config import HOSTS, REPO_ROOT, VERSION, Mode, Settings, load_settings
from deltr.mcp.activity import redact_url

log = logging.getLogger("deltr.main")


# --------------------------------------------------------------------------- CLI
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="deltr", description="Deltr — delta-neutral CEX<->DEX arbitrage agent (PAPER | TESTNET).")
    p.add_argument("--mode", choices=["paper", "testnet"], default=None, help="overrides DELTR_MODE (default paper)")
    p.add_argument("--symbol", default=None, help="e.g. BNBUSDT (overrides DELTR_SYMBOLS)")
    p.add_argument("--capital", type=float, default=None, help="portfolio capital in USD (overrides DELTR_CAPITAL_USD)")
    p.add_argument("--leverage", type=float, default=None, help="default perp leverage, max 3 (overrides DELTR_DEFAULT_LEVERAGE)")
    p.add_argument("--port", type=int, default=None, help="API/dashboard/MCP port (default 8000)")
    p.add_argument("--no-ui", action="store_true", help="do not serve or spawn the dashboard")
    p.add_argument("--dev-ui", action="store_true", help="spawn `npm run dev` on :3000 (stdout -> stderr) instead of the static ui/out")
    p.add_argument("--mcp", action="store_true", help="add the stdio MCP transport to THIS process (banner and logs go to stderr)")
    p.add_argument("--auto", action="store_true", help="PAPER only: auto-execute gate-approved actionable opportunities")
    p.add_argument("--once", action="store_true", help="one tick + scan + explain + propose + gate pre-check, then exit")
    p.add_argument("--json", action="store_true", help="machine-readable --once output; nothing else on stdout")
    p.add_argument("--replay", default=None, metavar="PATH", help="replay a MarketState/v1 JSONL fixture instead of live feeds")
    p.add_argument("--replay-speed", type=float, default=None, help="replay speed multiplier (default 1.0)")
    p.add_argument("--min-edge-bps", type=float, default=None, help="PAPER: [-50, 50] demo override of the min net edge; TESTNET: >= measured round trip")
    p.add_argument("--state-dir", default=None, help="persistence dir (default <repo>/state; per-mode subfolders)")
    p.add_argument("--version", action="version", version=f"deltr {VERSION}")
    return p.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {
        "DELTR_MODE": args.mode,
        "DELTR_SYMBOLS": args.symbol,
        "DELTR_CAPITAL_USD": args.capital,
        "DELTR_DEFAULT_LEVERAGE": args.leverage,
        "DELTR_API_PORT": args.port,
        "DELTR_AUTO_EXECUTE": True if args.auto else None,
        "DELTR_REPLAY_PATH": args.replay,
        "DELTR_REPLAY_SPEED": args.replay_speed,
        "DELTR_STATE_DIR": args.state_dir,
    }
    return load_settings(**overrides)


def configure_logging(quiet: bool) -> None:
    """stderr always; INFO (DEBUG with DELTR_DEBUG=1); WARNING under --json so stderr stays terse."""
    level = logging.DEBUG if os.environ.get("DELTR_DEBUG") == "1" else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=level, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


# --------------------------------------------------------------------------- banner
def banner(settings: Settings, engine: Any, port: int, *, mcp_stdio: bool, ui_note: str) -> str:
    st = engine.status()
    s = settings
    lines = [
        "==========================================================================",
        f" DELTR {s.version} — CEX <-> DEX delta-neutral arbitrage agent",
        " Binance Agent OS Mini Hackathon · Track A (skill) · Track B (connect your MCPs)",
        "==========================================================================",
        f" mode       : {s.mode.value.upper()}" + ("   [REPLAY " + Path(engine.replay_path).name + "]" if engine.replay_path else "")
        + ("   [MIN-EDGE OVERRIDE %g bps]" % engine.min_edge_override if engine.min_edge_override is not None else ""),
        f" symbol     : {s.symbol}   capital ${s.capital_usd:,.0f}   leverage {s.default_leverage:g}x   horizon {s.funding_horizon_hours:g} h   leg order {s.leg_order.value}",
        f" secrets    : {'present' if s.secrets_present else 'absent'} (BINANCE_API_ENV={s.binance_api_env}; values never printed)",
        f" hosts      : futures {s.hosts['futures_rest']}   spot mirror {s.hosts['spot_rest']}   BSC chainId {s.bsc_chain_id} ({redact_url(s.rpc_urls[0])})",
        "              production trading hosts are not configured in any mode (no LIVE mode exists)",
    ]
    for v in st.venues:
        lines.append(f" venue      : {v.name:<24} {'ok ' if v.ok else 'DOWN'}  age {v.age_ms} ms  source {v.source.value}  {v.detail[:60]}")
    lines += [
        f" risk gate  : 19 checks, median {st.gate_median_us:g} µs measured on this machine ({st.dd_state}, kill={'on' if st.kill_switch else 'off'})",
        f" min edge   : {st.min_edge_bps:g} bps (floor {st.min_edge_floor_bps:g})   equity ${st.equity_usd:,.2f}   open positions {st.open_positions}",
        f" state dir  : {getattr(engine, 'state_root', Path(s.state_dir) / s.mode.value)}" + ("   (replay book, isolated from live runs)" if engine.replay_path else ""),
        f" landing    : http://127.0.0.1:{port}/        (Launch App button)   ({ui_note})",
        f" dashboard  : http://127.0.0.1:{port}/app/",
        f" REST API   : http://127.0.0.1:{port}/api/snapshot   docs http://127.0.0.1:{port}/api/docs",
        f" MCP (http) : http://127.0.0.1:{port}/mcp" + ("   + stdio transport on this process" if mcp_stdio else ""),
        f" Claude Code: claude mcp add deltr --transport http http://127.0.0.1:{port}/mcp",
        " Claude Desktop (mcp-remote):",
    ]
    snippet = s.mcp_client_snippets()["claude_desktop_remote"].replace(f":{s.api_port}/mcp", f":{port}/mcp")
    lines += ["   " + ln for ln in snippet.splitlines()]
    if engine.start_errors:
        lines.append(" warnings   : " + " | ".join(engine.start_errors)[:300])
    lines.append("==========================================================================")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main
async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    quiet_stdout = args.mcp or args.json
    configure_logging(quiet=args.json)

    # 1. settings
    try:
        settings = settings_from_args(args)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError or ValueError
        msg = str(exc).strip().splitlines()
        reason = next((ln.strip() for ln in msg if "Value error" in ln or "refused" in ln or "requires" in ln), msg[-1] if msg else str(exc))
        reason = reason.replace('Value error, ', '').split(' [type=')[0]
        print(f"deltr: invalid configuration: {reason}", file=sys.stderr)
        return 2
    port = int(args.port or settings.api_port)
    if port in (3000, 3001):
        print("deltr: ports 3000/3001 are reserved for the Next.js dev server; pick another --port", file=sys.stderr)
        return 2

    # never start a second engine next to a running Deltr (two engines = two books); under --mcp
    # point the client at the running instance instead
    if not args.once and port_in_use(port):
        hint = (
            f"Point your MCP client at the running instance instead:\n{settings.mcp_client_snippets()['claude_desktop_remote']}"
            if args.mcp else "Pick another --port or stop the other process."
        )
        print(f"deltr: port {port} is already in use; not starting a second engine.\n{hint}", file=sys.stderr)
        return 2

    # 2/3. engine
    from deltr.engine import build_engine

    try:
        engine = build_engine(settings, replay_path=args.replay, min_edge_override=args.min_edge_bps)
    except Exception as exc:  # noqa: BLE001
        print(f"deltr: cannot build engine: {exc}", file=sys.stderr)
        return 2
    try:
        await engine.start(loops=not args.once)
    except Exception as exc:  # noqa: BLE001
        print(f"deltr: startup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        await engine.stop()
        return 2

    # 4. --once
    if args.once:
        try:
            result = await engine.run_once()
        finally:
            await engine.stop()
        text = json.dumps(result, default=str, indent=None if args.json else 2, sort_keys=False)
        print(text)
        sys.stdout.flush()
        return 0 if result.get("ok") else 3

    # 5. API + MCP (http) in one process
    import uvicorn

    from deltr.api.app import create_app
    from deltr.mcp.server import build_mcp, run_stdio

    mcp = build_mcp(engine, engine.activity)
    app = create_app(engine, mcp, engine.activity, ui_dir=None if not args.no_ui else Path("/nonexistent-ui"))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False,
                            log_level="warning" if quiet_stdout else "info", lifespan="on")
    server = uvicorn.Server(config)
    async def _serve() -> None:
        try:
            await server.serve()
        except SystemExit as exc:  # uvicorn's STARTUP_FAILURE (e.g. port taken between the check and the bind)
            raise RuntimeError(f"uvicorn exited with status {exc.code}") from exc

    serve_task = asyncio.create_task(_serve(), name="deltr-uvicorn")

    # 6. --mcp stdio transport (same engine, same book)
    stdio_task: Optional[asyncio.Task[Any]] = None
    if args.mcp:
        stdio_task = asyncio.create_task(run_stdio(mcp), name="deltr-mcp-stdio")

    # 7. UI
    dev_proc: Optional[asyncio.subprocess.Process] = None
    if args.no_ui:
        ui_note = "--no-ui: dashboard not served"
    elif app.state.ui_dir:
        ui_note = "static ui/out served by FastAPI"
    elif args.dev_ui and (REPO_ROOT / "node_modules").exists():
        env = {**os.environ, "NEXT_PUBLIC_API": f"http://127.0.0.1:{port}"}
        try:
            dev_proc = await asyncio.create_subprocess_exec(
                "npm", "run", "dev", cwd=str(REPO_ROOT), env=env, stdout=sys.stderr, stderr=sys.stderr
            )
            ui_note = "next dev on http://localhost:3000 (stdout -> stderr)"
        except Exception as exc:  # noqa: BLE001
            ui_note = f"npm run dev failed to start: {exc}"
    else:
        ui_note = "UI not built: run `npm ci && npm run build` (one-time) or use --dev-ui"

    # 8. banner (stderr under --mcp so stdout stays JSON-RPC only)
    print(banner(settings, engine, port, mcp_stdio=bool(args.mcp), ui_note=ui_note), file=sys.stderr if quiet_stdout else sys.stdout, flush=True)

    # 9. run until SIGINT/SIGTERM (uvicorn owns the signal handlers) or the stdio transport ends
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()

    def _request_stop(*_: Any) -> None:
        stop_requested.set()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX
            pass
    waiters = {serve_task, asyncio.create_task(stop_requested.wait(), name="deltr-stop-wait")}
    if stdio_task is not None:
        waiters.add(stdio_task)
    rc = 0
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if serve_task.done() and not serve_task.cancelled() and serve_task.exception() is not None:
            print(f"deltr: API server failed: {serve_task.exception()!r}", file=sys.stderr)
            rc = 1
    finally:
        server.should_exit = True
        if stdio_task is not None and not stdio_task.done():
            stdio_task.cancel()
        try:
            await asyncio.wait_for(serve_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if dev_proc is not None and dev_proc.returncode is None:
            dev_proc.terminate()
        await engine.stop()
        for w in waiters:
            if not w.done():
                w.cancel()
    return rc


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)
