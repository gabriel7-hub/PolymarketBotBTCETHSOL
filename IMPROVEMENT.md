# IMPROVEMENT.md — Post-9PM IST Trade Readiness Findings

**Analysis window:** trades after **2026-06-30 21:00 IST**  
**UTC cutoff:** `2026-06-30 15:30:00 UTC` (`1782833400`)  
**DB analyzed:** `/Users/chandreshj/Downloads/bot_state.db`

## Summary

The system is not missing a totally different edge. The downloaded DB shows we are mostly **not trading the edge we already identified**.

The validated edge is late-window feed-lag certainty: buy near-certain favorites when the book is still stale in the final `10-45s`. But the DB shows most certainty fills happened around **T-90s to T-105s**, which is the weaker mid-window regime.

Public tape from the same markets confirms that profitable bot-like wallets are buying late favorites, often at `.95-.99`, but much closer to expiry and at materially larger clips.

There is also a more fundamental issue: our recorded Price to Beat can differ materially from Polymarket's official `priceToBeat`. This can make the model confidently wrong. Fixing strike truth and Chainlink staleness is higher priority than further parameter tuning.

## P0: Price-to-Beat Mismatch / Chainlink Staleness

Resolved Gamma events expose official settlement metadata:

```json
{
  "eventMetadata": {
    "priceToBeat": 59837.95305042357,
    "finalPrice": 60348.95030704937
  }
}
```

Active markets do **not** expose this metadata before resolution, so live trading still needs an estimate. But after resolution we can audit exactly how wrong our strike/final estimate was.

In a 160-window post-9PM IST sample, our recorded values differed from official Gamma metadata by up to:

| Asset | Max abs `ref_price` error | Side mismatches in 40-window sample |
|---|---:|---:|
| BTC | ~15.3 bp | 4 |
| ETH | ~11.0 bp | 8 |
| SOL | ~20.8 bp | 7 |
| XRP | ~11.7 bp | 6 |

This is larger than the current `CERTAINTY_MIN_MOVE_BP = 5bp`. A 5bp move gate is not safe if our strike can be wrong by 10-20bp.

### Root Cause

The system currently treats `oracle.price` as both:

1. the strike/Price-to-Beat estimate at window open, and
2. the live moving price used for model distance during the window.

That conflates two different jobs.

Chainlink RTDS is the closest source for Price to Beat, but it is sparse. It does not update continuously like Coinbase/Binance. On-chain Chainlink is also heartbeat-driven and can be stale. CEX feeds update fast, but can carry several bp of basis vs Polymarket's official Chainlink Data Streams number.

### Required Architecture Change

Split the price surfaces:

```python
strike_estimate        # best T=0 estimate of Price to Beat
live_fair_price        # high-frequency Coinbase/Binance/CEX price during window
official_price_to_beat # post-resolution Gamma eventMetadata.priceToBeat
official_final_price   # post-resolution Gamma eventMetadata.finalPrice
```

Do not use one `oracle.price` for every role.

### Required Schema Additions

Add official settlement/audit fields to `outcomes`:

```sql
official_price_to_beat REAL,
official_final_price REAL,
official_winning_side TEXT,
ref_error_bp REAL,
final_error_bp REAL
```

Populate them after resolution from Gamma `eventMetadata`.

Official winner should be computed as:

```python
official_winning_side = "UP" if official_final_price >= official_price_to_beat else "DOWN"
```

Use this to correct positions, ledger P&L, calibration, and backtests.

### Required Resolution Logic

`market_discovery.fetch_resolution()` should return more than just `UP`/`DOWN`.

Recommended structure:

```python
{
    "winning_side": "UP",
    "price_to_beat": 59837.95305042357,
    "final_price": 60348.95030704937,
    "source": "GAMMA_EVENT_METADATA"
}
```

If `eventMetadata` is missing, fall back to existing `outcomePrices`, but mark official numeric fields as null.

### Required Trading Gate Until Strike Audit Is Clean

Until official strike error is proven small, do not trade tiny moves.

Preferred safe rule:

```python
if strike_source != "rtds":
    skip_live_certainty()
```

Less strict alternative:

```python
if strike_source == "rtds":
    min_move_bp = 5
elif strike_source == "onchain":
    min_move_bp = 20
else:  # CEX proxy
    min_move_bp = 25
```

Short-term safer config:

```python
CERTAINTY_MIN_MOVE_BP = 20.0
```

Only lower this after the stored official `ref_error_bp` distribution proves the live strike estimate is reliably inside the move gate.

### RTDS / Chainlink Dashboard Improvements

Add dashboard fields:

- RTDS connected/disconnected
- RTDS last tick age
- RTDS payload timestamp age
- current strike source
- strike source per active window
- official `ref_error_bp` after resolution
- rolling median/max official ref error by asset
- count of windows traded with `rtds`, `onchain`, and `proxy` strike source

If Chainlink is stale or source is proxy, the dashboard should show a visible warning.

### Backtest Improvement

Once official fields are stored, rerun all certainty analysis using:

- official `priceToBeat`
- official `finalPrice`
- recorded book prices at signal time

This answers the real question: does the edge survive Polymarket's true settlement numbers?

Until that is done, any result using our own recorded `ref_price` is partly contaminated by strike error.

## Our Post-9PM IST Results

Resolved positions after the cutoff:

| Asset | Trades | W/L | P&L | PF |
|---|---:|---:|---:|---:|
| BTC | 134 | 122 / 12 | +$3.48 | 1.19 |
| ETH | 132 | 122 / 10 | +$9.14 | 1.60 |
| SOL | 140 | 125 / 15 | +$0.97 | 1.04 |
| XRP | 112 | 97 / 15 | -$3.94 | 0.83 |
| **Total** | **518** | **466 / 52** | **+$9.65** | **1.12** |

This is not a catastrophic absolute loss, but it is a bad edge profile: 518 trades for only +$9.65, with XRP negative and SOL nearly flat.

## Main Leak

Certainty fills by timing:

| Zone | Fills | W/L | P&L | PF | EV/trade |
|---|---:|---:|---:|---:|---:|
| `10-45s` live-candidate zone | 48 | 45 / 3 | +$4.50 | 1.99 | +$0.094 |
| `45s+` early zone | 476 | 421 / 49 | +$5.15 | ~1.07 | +$0.011 |

Almost the same total money came from **48 late trades** as from **476 early trades**.

Only about 9% of our fills were in the validated late zone. The bot is over-trading weak mid-window certainty.

## Same-Market Wallet Research

Public tape was pulled for the exact markets we traded:

- Markets sampled: 521
- Public fills: 265,702
- Unique wallets: 10,000
- API failures after retry: 0
- Sources:
  - `https://data-api.polymarket.com/trades`
  - `https://data-api.polymarket.com/v1/leaderboard?category=crypto&limit=1000`

Top same-lane wallets by late `BUY >= .80` in our traded markets:

| Wallet / Name | Late >=.80 notional | Late trades | Median px | Median size |
|---|---:|---:|---:|---:|
| `0xeebde7a0...` Bonereaper | $51,243 | 146 | .946 | 186 sh |
| `0x7b2b8d28...` | $38,946 | 71 | .990 | 22 sh |
| `0x562a11bc...` | $38,619 | 2,251 | .990 | 10 sh |
| `0xdf0d2ccf...` | $35,924 | 44 | .990 | 443 sh |
| `0x2277c18f...` EVP-HalfKelly | $32,105 | 99 | .990 | 227 sh |

Leaderboard wallets that also showed the same late-favorite fingerprint:

| Crypto rank | Wallet / Name | Crypto P&L | Volume | Same-window late >=.80 |
|---:|---|---:|---:|---:|
| 2 | `0xb55fa129...` | $11,150 | $512k | $1,274 |
| 7 | `0xeebde7a0...` Bonereaper | $6,908 | $1.85M | $51,243 |
| 12 | `0xce25e214...` | $5,049 | $351k | $2,216 |
| 26 | `0x20d2309c...` | $3,489 | $332k | $11,361 |
| 42 | `0xdf0d2ccf...` | $2,579 | $165k | $35,924 |

## What Similar Bots Do Differently

They are not buying generic model certainty at T-100s.

They wait for the market to be nearly decided and buy favorites late:

- Late `BUY >= .80`, often `>= .95`
- Much closer to expiry
- Larger clips, often 50-900 shares
- Likely taking stale favorite offers only after the book has failed to reprice

Our average certainty fill:

- `t_remaining`: about 90-105s
- Ask: about `.89`
- Model probability: about `.96`
- Realized edge: thin after occasional full losses

## Additional Pattern Findings

High ask entries are dangerous when they occur too early.

Across the post-9PM IST sample:

| Ask bucket | Trades | W/L | P&L | Notes |
|---|---:|---:|---:|---|
| `< .85` | 118 | 101 / 16 | +$8.01 | Best raw result |
| `.85-.90` | 142 | 128 / 13 | +$9.88 | Good |
| `.90-.95` | 159 | 141 / 15 | -$3.06 | Weak |
| `>= .95` | 105 | 96 / 8 | -$5.18 | Bad unless very late |

Inside the late `10-45s` zone:

| Rule | Trades | W/L | P&L | PF | EV/trade |
|---|---:|---:|---:|---:|---:|
| `late_10_45` | 48 | 45 / 3 | +$4.50 | 1.99 | +$0.094 |
| `late_no_xrp` | 41 | 39 / 2 | +$5.00 | 2.65 | +$0.122 |
| `late_lag8` | 18 | 17 / 1 | +$3.07 | 3.02 | +$0.170 |
| `late_lag8_no_xrp` | 15 | 14 / 1 | +$2.58 | 2.70 | +$0.172 |
| `late_max94` | 36 | 34 / 2 | +$5.03 | 2.66 | +$0.140 |
| `t20_45` | 37 | 34 / 3 | +$2.40 | 1.53 | +$0.065 |

## Implementation Changes

### 0. Fix Price-to-Beat Truth Before More Strategy Tuning

This is the highest-priority change.

Implementation checklist:

- Parse `eventMetadata.priceToBeat` and `eventMetadata.finalPrice` from Gamma after resolution.
- Store official settlement fields in `outcomes`.
- Compute and store `ref_error_bp` / `final_error_bp`.
- Use official winner to correct positions and trades.
- Report official strike error by asset in dashboard/backtest.
- Gate live certainty when strike source is stale/non-RTDS.
- Re-score the post-9PM IST sample using official settlement numbers.

### 1. Redeploy Late-Only Certainty

The active deployment must use the late-only certainty candidate:

```python
CERTAINTY_ZONE_START = 45
CERTAINTY_ZONE_END = 10
```

Do not allow certainty fills before T-45s in live/paper acceptance mode.

Keep wider `10-220s` certainty only as an offline research diagnostic, not as the capital candidate.

### 2. Disable XRP for Now

XRP was negative in the analyzed period:

```text
XRP: 112 trades, 97 / 15, -$3.94, PF 0.83
```

Recommended active assets:

```python
ASSETS = ["BTC", "ETH", "SOL"]
```

BNB should also stay out of the core 5-minute thesis until separately validated.

### 3. Tighten Lag Requirement

The current `3c` lag threshold admits too many weak states.

Recommended candidate:

```python
CERTAINTY_LAG_MARGIN = 0.08
```

Reason: `late_lag8` produced PF about 3.02 in the analyzed sample, albeit with fewer trades.

### 4. Tighten Max Ask

Our `ask >= .95` entries were negative overall because they were often too early.

Recommended default:

```python
CERTAINTY_MAX_ASK = 0.94
```

Then add a separate high-ask exception only when the trade is extremely late and near locked:

```python
allow_high_ask = (
    ask <= 0.97
    and t_remaining <= 20
    and p_side >= 0.99
    and (p_side - ask) >= 0.04
)
```

### 5. Keep Size Small Until the Gate Clears

Do not scale a blended PF 1.12 system.

Only scale after the late-only candidate clears the paper/live-observed gate:

- At least 300 fresh resolved windows per active asset
- Late-only PF >= 1.5
- Depth-realistic fills only
- No missed-strike windows traded
- XRP excluded unless separately validated

### 6. Add Dashboard/Report Breakdowns

Every paper/live run should report certainty by:

- Asset
- `10-45s` vs `45s+`
- Ask bucket
- Lag bucket
- `ask >= .95` high-ask exception bucket
- Strike source
- Book age
- Filled vs unfilled attempts

The headline metric must be the late-only candidate, not blended wide certainty.

## Recommended Candidate Config

Until strike truth is fixed and audited, use the safer move gate:

```python
ASSETS = ["BTC", "ETH", "SOL"]

CERTAINTY_ZONE_START = 45
CERTAINTY_ZONE_END = 10
CERTAINTY_MIN_MOVE_BP = 20.0
CERTAINTY_LAG_MARGIN = 0.08
CERTAINTY_MAX_ASK = 0.94
CERTAINTY_FLOOR = 0.80
CERTAINTY_LIVE_ENABLED = False
```

After official `ref_error_bp` is proven consistently small, revisit:

```python
CERTAINTY_MIN_MOVE_BP = 5.0
```

Optional high-ask exception:

```python
def allow_certainty_entry(t_remaining, p_side, ask):
    normal = ask <= CERTAINTY_MAX_ASK
    high_ask = (
        ask <= 0.97
        and t_remaining <= 20
        and p_side >= 0.99
        and (p_side - ask) >= 0.04
    )
    return normal or high_ask
```

## What Not To Do

- Do not re-enable bare directional taker.
- Do not farm 5-minute rewards while `rewards.rates = null`.
- Do not copy wallets blindly.
- Do not buy `.95-.99` favorites at T-90s.
- Do not include `45s+` entries in the live-readiness PF gate.
- Do not trust our recorded `ref_price` as truth until it is reconciled against official Gamma `priceToBeat`.
- Do not use stale on-chain Chainlink as a continuously updating live price.
- Do not trade `5bp` moves when strike source is `onchain` or `proxy`.
- Do not scale size until the late-only candidate is proven on fresh depth-realistic fills.

## Conclusion

The top wallets are not using a magical different model. They are exploiting the same stale-book favorite effect, but with better timing discipline.

Our system is losing edge because it is buying too early and too broadly. The fix is to make the capital candidate narrower:

1. `10-45s` only
2. no XRP
3. stronger lag margin
4. lower max ask unless extremely late
5. official Price-to-Beat reconciliation
6. report late-only PF as the only live gate
