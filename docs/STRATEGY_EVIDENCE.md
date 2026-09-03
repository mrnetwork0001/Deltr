# Strategy evidence — real mainnet funding, measured 2026-09-03

Everything here is measured from `fapi.binance.com` (public, keyless, read-only), not assumed.
It became available when we found the blocked hostname was a LAN resolver fault, not a geoblock.

**Reproduce it yourself.** These figures are no longer a static table: `deltr/funding_history.py`
fetches the same public `GET /fapi/v1/fundingRate` data and recomputes every number below, so the
claim is regenerable rather than reported. Three ways in, all read-only and keyless:

```bash
# REST (the running agent)
curl -s 'http://127.0.0.1:8000/api/funding/history?symbol=BNBUSDT&lookback_days=500&markdown=true'
```

```
# MCP, from any connected client
deltr_funding_history(symbol="BNBUSDT", lookback_days=500)
```

```python
# library
from deltr.funding_history import FundingHistoryService
analysis = await FundingHistoryService(http).analyse("BNBUSDT", lookback_days=500)
```

The settlement interval is **measured** from the gaps in the data rather than assumed, so a 4 h
symbol such as CAKEUSDT is not annualised as if it settled every 8 h. Each record is annualised as
`fundingRate * (24 / interval_h) * 365`, which is `* 3 * 365` at the usual 8 h cadence. A window
"clears" when its summed carry in bps is at least the cost model's round trip.

`tests/fixtures/funding_history.json` holds 400 verbatim mainnet settlements each for BNBUSDT and
BTCUSDT, so `tests/test_funding_history.py` reproduces the analysis offline; the live re-fetch is a
`@pytest.mark.live` test, skipped unless `DELTR_LIVE_TESTS=1`.

On a machine whose default resolver refuses `fapi.binance.com`, Deltr does not fall back to another
venue or to simulated data: it fails with an error naming the hostname and telling you to compare
`dig +short fapi.binance.com` against `dig +short @1.1.1.1 fapi.binance.com`.

## Why this file exists

Deltr's differentiator is the strategy, not the risk gate. A strategy claim needs evidence.
Until today the only funding rate Deltr could read was the Binance **testnet** rate, which is
structurally ~0, so the edge was always negative and we could not tell whether that was the
strategy talking or the data. Now we can measure it.

## The measurement

BNBUSDT, 1500 settlements = 500 days (2025-04-21 to 2026-09-03):

| Metric | Value |
|---|---|
| Mean carry | +1.42% annualised |
| Median carry | 0.00% |
| Range | -54.45% to +35.98% annualised |
| Settlements where the short is paid | 491 / 1500 = 32.7% |

## The finding: the round trip is the whole game

Round trip today is **16.6 bps**, of which **10 bps is the Binance taker fee** (2 x 5 bps).
Posting the perp leg as a maker instead of crossing the spread takes the round trip to ~8.6 bps.

Share of historical windows where funding carry alone clears the round trip:

| Hold | Perp leg TAKEN (16.6 bps) | Perp leg POSTED (8.6 bps) |
|---|---|---|
| 3 days | 0.0% (0/1492) | 1.1% (17/1492) |
| 7 days | 1.2% (18/1480) | 19.3% (286/1480) |
| 14 days | 17.2% (251/1459) | 45.7% (667/1459) |
| 30 days | 49.4% (697/1411) | 61.9% (874/1411) |

Regenerated on 2026-09-03 by `deltr/funding_history.py` against the same 1500 settlements. The
14-day taken figure reads 17.2% here against 17.1% in the original ad-hoc query, a one-window
rounding difference in the rolling count.

Per symbol, 166 days of history, 7-day hold (regenerated 2026-09-03 by `deltr/funding_history.py`):

| Symbol | Settlements | Interval | Mean carry | Short paid | Taken | Posted |
|---|---|---|---|---|---|---|
| BNBUSDT | 498 | 8 h | +3.53%/yr | 55.0% | 3.8% | **36.4%** |
| ETHUSDT | 498 | 8 h | +2.25%/yr | 71.9% | 2.7% | **30.3%** |
| BTCUSDT | 498 | 8 h | +3.23%/yr | 75.5% | 6.3% | **48.5%** |
| CAKEUSDT | 995 | 4 h | +5.21%/yr | 78.2% | 15.6% | **62.4%** |

The first three lines reproduce the original ad-hoc query to within a tenth of a point. CAKEUSDT is
a **correction**: it settles every 4 h, not 8 h, so the earlier row covered 83 days rather than 166
and annualised at half the true cadence. Measuring the interval from the data removes that class of
error, which is exactly why the analysis derives it instead of assuming 8 h.

## What it means

1. **This strategy lives or dies on execution style, not on signal.** Taking the perp leg makes
   it almost never work; posting it makes it work roughly a third to half of the time. That is a
   far more interesting and defensible finding than "the edge is negative today."
2. **BTCUSDT (via BTCB on BNB Chain) is the strongest pair** in the tradeable universe, not BNB.
3. **The wider market has a fat tail**: 66 of 846 perps currently pay a short more than 20%
   annualised, topping out near +188%. Almost none have a PancakeSwap V3 leg, which is itself
   the honest reason Deltr stays on the four majors.

## Honesty notes

- Funding history above is real mainnet data, and so is every mark price, funding rate and book
  ticker the agent reads: market data comes from `fapi.binance.com` in every mode, tagged
  `binance-futures-mainnet`, with the provenance derived from the host that answered rather than
  asserted by the caller. Deltr's **orders** remain on the Futures testnet and
  the DEX leg remains a live mainnet quote with a simulated fill. Data source and execution venue
  are deliberately different, and both are labelled everywhere they appear.
- The "posted" column is a **model**, not a measurement: it recomputes the same round trip with a
  maker fee in place of the taker fee. It assumes the limit order fills, which a real maker order
  may not. Do not present it as a realised result.
- `api.binance.com` (spot mainnet) returns 403 from here; only `fapi` market data is used.
