#!/usr/bin/env python
"""
Record N minutes of REAL market state to a JSONL replay fixture.

Standalone (httpx only — no venue modules) so it can run before the rest of
the engine exists.  Line 1 is a provenance header; every following line is
``MarketState.model_dump_json()`` (pure recording, no synthetic rows).

Usage:
    .venv/bin/python scripts/record_replay.py --minutes 10 --out tests/fixtures/replay.jsonl
    .venv/bin/python scripts/record_replay.py --convert old.jsonl --out tests/fixtures/replay.jsonl   # legacy → current schema
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deltr.models import DataSource, DexQuote, Freshness, FundingSnapshot, MarketState, Quote, Venue  # noqa: E402
from deltr.venues.pancake_constants import MAINNET, Q192, SELECTORS  # noqa: E402

FUT = "https://testnet.binancefuture.com"
SPOT = "https://data-api.binance.vision"
QUOTE_EXACT_OUTPUT_SINGLE = "0xbd21704a"  # quoteExactOutputSingle((address,address,uint256,uint24,uint160))


def _word(x: int) -> str:
    return hex(x)[2:].rjust(64, "0")


def _addr(a: str) -> str:
    return a[2:].lower().rjust(64, "0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def eth_call(client: httpx.Client, rpc: str, to: str, data: str) -> str:
    r = client.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]})
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def rpc_call(client: httpx.Client, rpc: str, method: str, params: list) -> str:
    r = client.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r.raise_for_status()
    return r.json()["result"]


def _quote(client: httpx.Client, rpc: str, selector: str, token_in: str, token_out: str, amount: int, fee: int) -> tuple[int, int]:
    data = selector + _addr(token_in) + _addr(token_out) + _word(amount) + _word(fee) + _word(0)
    res = eth_call(client, rpc, MAINNET.quoter_v2, data)[2:]
    return int(res[0:64], 16), int(res[192:256], 16)  # amount (out|in), gasEstimate


def dex_quote(client: httpx.Client, rpc: str, size_base: float, fee_tier: int) -> DexQuote:
    pool = MAINNET.pools_wbnb_usdt[fee_tier]
    s0 = eth_call(client, rpc, pool, SELECTORS["slot0"])[2:]
    sqrt_p = int(s0[0:64], 16)
    tick = int(s0[64:128], 16)
    if tick >= 2**255:
        tick -= 2**256
    mid = float(Q192 / (sqrt_p * sqrt_p))
    size_wei = int(size_base * 1e18)
    # BUY size_base WBNB with USDT: exact-output → amountIn USDT
    amount_in, gas_buy = _quote(client, rpc, QUOTE_EXACT_OUTPUT_SINGLE, MAINNET.usdt, MAINNET.wbnb, size_wei, fee_tier)
    # SELL size_base WBNB for USDT: exact-input → amountOut USDT
    amount_out, gas_sell = _quote(client, rpc, SELECTORS["quoteExactInputSingle"], MAINNET.wbnb, MAINNET.usdt, size_wei, fee_tier)
    exec_buy = amount_in / 1e18 / size_base
    exec_sell = amount_out / 1e18 / size_base
    fee_bps = fee_tier / 100.0
    gas_price = int(rpc_call(client, rpc, "eth_gasPrice", []), 16)
    block = int(rpc_call(client, rpc, "eth_blockNumber", []), 16)
    gas_units = max(gas_buy, gas_sell)
    return DexQuote(
        pool=pool, fee_tier=fee_tier, fee_bps=fee_bps, sqrt_price_x96=sqrt_p, tick=tick, mid_price=mid,
        size_base=size_base, exec_price_buy=exec_buy, amount_in_usdt=amount_in / 1e18,
        exec_price_sell=exec_sell, amount_out_usdt=amount_out / 1e18,
        impact_bps=max(0.0, (exec_buy / mid - 1.0) * 1e4 - fee_bps),
        gas_units=gas_units, gas_price_wei=gas_price, gas_usd=gas_units * gas_price / 1e18 * mid,
        block=block, ts=_now(), source=DataSource.BSC_MAINNET_CHAIN,
    )


def cex_state(client: httpx.Client, symbol: str) -> tuple[Quote, FundingSnapshot, Quote]:
    pi = client.get(f"{FUT}/fapi/v1/premiumIndex", params={"symbol": symbol}).json()
    bt = client.get(f"{FUT}/fapi/v1/ticker/bookTicker", params={"symbol": symbol}).json()
    sp = client.get(f"{SPOT}/api/v3/ticker/bookTicker", params={"symbol": symbol}).json()
    now = _now()
    perp = Quote(venue=Venue.BINANCE_FUTURES, symbol=symbol, bid=float(bt["bidPrice"]), ask=float(bt["askPrice"]),
                 bid_qty=float(bt["bidQty"]), ask_qty=float(bt["askQty"]), ts=now, source=DataSource.BINANCE_FUTURES_TESTNET)
    rate = float(pi["lastFundingRate"])
    fund = FundingSnapshot(symbol=symbol, mark_price=float(pi["markPrice"]), index_price=float(pi["indexPrice"]),
                           last_funding_rate=rate, next_funding_time_ms=int(pi["nextFundingTime"]), interval_h=8,
                           annualized_pct=rate * 3 * 365 * 100, ts=now, source=DataSource.BINANCE_FUTURES_TESTNET)
    spot = Quote(venue=Venue.BINANCE_SPOT, symbol=symbol, bid=float(sp["bidPrice"]), ask=float(sp["askPrice"]),
                 bid_qty=float(sp["bidQty"]), ask_qty=float(sp["askQty"]), ts=now, source=DataSource.BINANCE_SPOT_MIRROR)
    return perp, fund, spot


def header(symbol: str, size: float, fee_tier: int, rpc: str, note: str) -> str:
    return json.dumps({
        "header": True, "schema": "MarketState/v1", "symbol": symbol, "recorded_at": _now().isoformat(), "size_base": size,
        "fee_tier": fee_tier,
        "sources": {"dex": DataSource.BSC_MAINNET_CHAIN.value, "perp": DataSource.BINANCE_FUTURES_TESTNET.value, "spot": DataSource.BINANCE_SPOT_MIRROR.value},
        "hosts": {"futures": FUT, "spot": SPOT, "rpc": rpc}, "note": note,
    })


def convert_legacy(src: Path, dst: Path) -> int:
    """Convert the day-1 recording (int-ms timestamps, old field names) to the current schema.
    Pure transformation: no values are invented; freshness ages are computed from the recorded timestamps."""
    n = 0
    with src.open() as fh, dst.open("w") as out:
        first = json.loads(fh.readline())
        out.write(header(first.get("symbol", "BNBUSDT"), first.get("size_base", 4.85), first.get("fee_tier", 100),
                         first.get("hosts", {}).get("rpc", ""), "converted from legacy day-1 recording; no synthetic rows") + "\n")
        for line in fh:
            r = json.loads(line)
            d, p, f, s = r["dex"], r["cex_perp"], r["funding"], r["cex_spot"]
            ts = datetime.fromtimestamp(r["ts"] / 1000, tz=timezone.utc)
            fee_bps = d["fee_tier"] / 100.0
            mid = d["mid"]
            dex = DexQuote(
                pool=d["pool"], fee_tier=d["fee_tier"], fee_bps=fee_bps, sqrt_price_x96=d["sqrt_price_x96"], tick=d["tick"],
                mid_price=mid, size_base=d["size_base"], exec_price_buy=d["exec_buy_price"], amount_in_usdt=d["exec_buy_price"] * d["size_base"],
                exec_price_sell=d["exec_sell_price"], amount_out_usdt=d["exec_sell_price"] * d["size_base"],
                impact_bps=max(0.0, (d["exec_buy_price"] / mid - 1.0) * 1e4 - fee_bps), gas_units=d["gas_estimate"],
                gas_price_wei=d["gas_price_wei"], gas_usd=d["gas_estimate"] * d["gas_price_wei"] / 1e18 * mid, block=d["block_number"],
                ts=datetime.fromtimestamp(d["ts"] / 1000, tz=timezone.utc), source=DataSource.BSC_MAINNET_CHAIN,
            )
            perp = Quote(venue=Venue.BINANCE_FUTURES, symbol=r["symbol"], bid=p["bid"], ask=p["ask"], bid_qty=p["bid_qty"], ask_qty=p["ask_qty"],
                         ts=datetime.fromtimestamp(p["ts"] / 1000, tz=timezone.utc), source=DataSource.BINANCE_FUTURES_TESTNET)
            spot = Quote(venue=Venue.BINANCE_SPOT, symbol=r["symbol"], bid=s["bid"], ask=s["ask"], bid_qty=s["bid_qty"], ask_qty=s["ask_qty"],
                         ts=datetime.fromtimestamp(s["ts"] / 1000, tz=timezone.utc), source=DataSource.BINANCE_SPOT_MIRROR)
            fund = FundingSnapshot(symbol=r["symbol"], mark_price=f["mark_price"], index_price=f["index_price"], last_funding_rate=f["last_funding_rate"],
                                   next_funding_time_ms=f["next_funding_time"], interval_h=8, annualized_pct=f["last_funding_rate"] * 3 * 365 * 100,
                                   ts=datetime.fromtimestamp(f["ts"] / 1000, tz=timezone.utc), source=DataSource.BINANCE_FUTURES_TESTNET)
            fr = Freshness(cex_age_ms=r["ts"] - f["ts"], dex_age_ms=r["ts"] - d["ts"], spot_age_ms=r["ts"] - s["ts"], ok=True)
            ms = MarketState(symbol=r["symbol"], dex=dex, cex_perp_book=perp, cex_spot_ref=spot, funding=fund,
                             perp_ref_price=fund.mark_price, freshness=fr, ts=ts, source=DataSource.REPLAY)
            out.write(ms.model_dump_json() + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--out", default="tests/fixtures/replay.jsonl")
    ap.add_argument("--symbol", default="BNBUSDT")
    ap.add_argument("--size", type=float, default=4.85)
    ap.add_argument("--fee-tier", type=int, default=100)
    ap.add_argument("--rpc", default="https://bsc-dataseed.binance.org/")
    ap.add_argument("--dex-every", type=float, default=3.0)
    ap.add_argument("--convert", default=None, help="convert a legacy recording instead of recording")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.convert:
        n = convert_legacy(Path(args.convert), out)
        print(f"converted {n} rows → {out}", file=sys.stderr)
        return 0

    deadline = time.time() + args.minutes * 60
    rows = 0
    last_dex: DexQuote | None = None
    last_dex_t = 0.0
    with httpx.Client(timeout=15.0, headers={"User-Agent": "deltr-recorder/1.0"}) as client, out.open("w", encoding="utf-8") as fh:
        fh.write(header(args.symbol, args.size, args.fee_tier, args.rpc, "pure recording, no synthetic rows") + "\n")
        while time.time() < deadline:
            t0 = time.time()
            try:
                if last_dex is None or t0 - last_dex_t >= args.dex_every:
                    last_dex = dex_quote(client, args.rpc, args.size, args.fee_tier)
                    last_dex_t = t0
                perp, fund, spot = cex_state(client, args.symbol)
                now = _now()
                fr = Freshness(cex_age_ms=int((now - fund.ts).total_seconds() * 1000), dex_age_ms=int((now - last_dex.ts).total_seconds() * 1000),
                               spot_age_ms=int((now - spot.ts).total_seconds() * 1000), ok=True)
                ms = MarketState(symbol=args.symbol, dex=last_dex, cex_perp_book=perp, cex_spot_ref=spot, funding=fund,
                                 perp_ref_price=fund.mark_price, freshness=fr, ts=now, source=DataSource.BINANCE_FUTURES_TESTNET)
                fh.write(ms.model_dump_json() + "\n")
                fh.flush()
                rows += 1
                if rows % 30 == 0:
                    print(f"[{rows}] dex {last_dex.exec_price_buy:.3f} mark {fund.mark_price:.3f} fund {fund.last_funding_rate:+.6f}", file=sys.stderr)
            except Exception as e:  # keep recording through transient errors
                print(f"warn: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    print(f"recorded {rows} rows → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
