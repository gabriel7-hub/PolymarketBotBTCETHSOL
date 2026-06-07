# Polymarket Leaderboard — Bot Strategy Analysis

**Date:** 2026-06-06 · **Source:** `data-api.polymarket.com/v1/leaderboard` (overall + crypto,
all windows) cross-referenced with each wallet's on-chain feed (`/activity`, `/positions`)
and public write-ups. All wallets/PnL/volume below are pulled live, not estimated.

## How strategy was inferred

You cannot read intent directly, but three signals identify a strategy with high confidence:

1. **PnL ÷ Volume ratio** — the single strongest tell.
   - **> 10%** → directional / informed conviction (high margin per dollar traded).
   - **0.1%–3%** → market-making / arbitrage (thin margin, high churn).
   - **≈ 0% (or slightly negative) on huge volume** → pure rebate/reward farming
     (trade PnL is ~0 by design; the real income is the incentive programs).
2. **Activity-feed event types** — `TRADE` vs `MERGE`/`SPLIT` (minting both sides = MM),
   and especially `MAKER_REBATE`, `REWARD`, `YIELD` (income from incentives, not edge).
3. **Trade size + market mix** — micro clips ($0.10) spread across many markets = reward
   farming; huge clips ($100k–$14M) concentrated in one event = a sharp.

---

## Top wallets and their strategies

### Group A — Directional sharps / informed whales
High margin, large concentrated clips, mostly taker BUYs on big binary events. **Not what
our 5-min BTC bot competes with** — there is no insider edge on a 5-minute random walk.

| Wallet | Name | PnL | Volume | PnL/Vol | Evidence → Strategy |
|---|---|---|---|---|---|
| `0x5668…5839` | **Theo4** | $22.0M | $43M | **51%** | Single trades up to **$14.7M**, all in 2024 US-election markets (Kamala/Trump popular vote). The famous "Théo" neighbour-poll whale. Macro conviction + research edge. |
| `0x1f2d…d0cf` | Fredi9999 | $16.6M | $76.6M | 22% | Concentrated election directional, large clips. |
| `0x8631…aa53` | RepTrump | $7.5M | $14M | **54%** | Pure directional political conviction. |
| `0xa380…21ff` | JewishNinja | $1.25M | $1.9M | **66%** | Highest margin on the board — sharp event picker, low churn. |
| `0xf883…cd1f` | Inaccuratestake | $2.68M | $13M | 21% | Top monthly PnL; directional. |
| `0xf284…b9f9` | strike123 | $0.47M | $3.7M | 13% | Activity = **Knicks vs Spurs**, tennis. Large directional **sports** taker (clips to $400k), live in-game. (Ranks on "crypto" board but trades sports.) |

### Group B — HFT market-makers / arbitrageurs
Massive volume, **thin margin**, tiny clips, continuous two-sided churn on liquid
sports/politics. Sub-100ms infra. **This is the cluster our 5-min bot lives in.**

| Wallet | Name | PnL | Volume | PnL/Vol | Evidence → Strategy |
|---|---|---|---|---|---|
| `0x204f…5e14` | **swisstony** | $9.4M | **$857M** | **1.1%** | Median trade **$9.8**, 100% churn across sports spreads/tennis/soccer. Textbook automated MM — thin edge × enormous turnover. |
| `0x2005…75ea` | RN1 | $9.3M | $617M | 1.5% | Same profile, MM/arb. |
| `0x6a72…33ee` | kch123 | $11.6M | $293M | 4.0% | MM with slightly fatter spreads. |
| `0xd218…b5c9` | cigarettes | $1.0M | $511M | 0.2% | Near-pure MM/arb. |
| `0xa61e…0abd` | risk-manager | $0.32M | $672M | 0.05% | MM/arb at scale, almost flat PnL — spread + rebates. |
| `0x9d84…1344` | ImJustKen | $3.1M | $475M | 0.7% | MM/arb. |
| `0x24c8…23e1` | debased | $1.5M | $463M | 0.3% | MM/arb. |
| `0x4924…3782` | (addr) | **−$3.6M** | $503M | −0.7% | MM that **bled** — inventory/adverse selection, or rewards offsetting the trading loss. The cautionary tale. |

### Group C — Pure rebate / liquidity-reward farmers
**PnL ≈ 0 on tens-to-hundreds of millions of volume.** Income is invisible in trading PnL
because it comes from the **Maker Rebates** + **Liquidity Rewards** programs. Directly
relevant — this is the low-variance leg we already started building.

| Wallet | Name | PnL | Volume | PnL/Vol | Evidence → Strategy |
|---|---|---|---|---|---|
| `0x6480…8dc5` | **tripping** | $0.10M | **$715M** | **0.014%** | Median trade **$0.10**, balanced BUY/SELL across F1/election futures. Micro two-sided quoting to harvest liquidity rewards; trade PnL ≈ 0 by design. |
| `0x08ff…ba79` | **krazyagain** | **$0.00007** | $16.5M (crypto) | ~0% | Activity feed literally contains `MAKER_REBATE`, `REWARD`, `YIELD` events. Mints + quotes both sides; **all return is incentives.** The model for our reward-farm leg. |
| `0xdf17…97d1` | Soft-Lantern | $0.13M | $31.9M (crypto) | 0.4% | Two-sided MM on event markets + `REWARD`/`YIELD` events. |
| `0x6480…`(crypto wk) | tripping | $102 | $23M (1wk crypto) | ~0% | Same wallet, confirms reward-farm behaviour on crypto specifically. |
| `0x59Ae…22d5` | (addr) | **−$96k** | $20M (crypto) | −0.5% | Crypto MM **getting adversely selected** — exactly the failure mode our original "directional maker" thesis would have hit. |

---

## Confirmed facts that shape what we add

Pulled live from a BTC 5-min market (`Bitcoin Up or Down – 12:05–12:10 ET`):

- **Liquidity rewards are ACTIVE on 5-min BTC**: `rewardsMaxSpread = 4.5` (¢ from mid),
  `rewardsMinSize = 50` ($/side), `orderMinSize = 5`, tick `0.01`.
- **Fee schedule**: `{rate: 0.07, exponent: 1, takerOnly: true, rebateRate: 0.2}` — makers
  pay nothing, takers pay `0.07·p·(1−p)`, 20% of the pool is rebated. Our `pricing.py` already
  matches this exactly.
- Only **7.6% of Polymarket wallets are net profitable** (Dune). Of the profitable ones at
  scale, the repeatable, infra-light edge is **Group C (rewards)**, then **Group B (MM/arb)** —
  **not** Group A (you can't insider-trade a random walk).

---

## What we can add on top of our system (prioritised)

Our foundation already has the calibrated fair value + fee-net EV + a `MAKER_FARM` stub.
The leaderboard says the durable money in our niche is **rewards + intra-market arbitrage**,
not directional sniping. Concrete additions, in priority order:

> **Status:** #1 (reward farm) and #2 (YES/NO arb) are **implemented** (paper) — see
> `signal_engine.evaluate()` legs ①②③, `pricing.pair_arb_edge`, `executor.execute_arb/run_farm`.
> #3 (SPLIT/MERGE) and #4 (copy-trade) remain next-round.

### 1. Liquidity-reward farming leg (Group C) — highest ROI, lowest variance ⭐ [IMPLEMENTED, paper]
The zero-PnL wallets prove this is the real yield. Build a proper two-sided quoter:
- Read `rewardsMaxSpread`, `rewardsMinSize`, `clobRewards` from the event (we already parse
  the market object in `market_discovery.py` — just surface these fields on `MarketWindow`).
- Post **both** UP and DOWN limit orders within `rewardsMaxSpread/2` of midpoint at
  `≥ rewardsMinSize` ($50 — note this exceeds our current `MAX_STAKE_PER_MARKET=25`, so size
  must be raised or staked specifically for the farm leg), refreshed each tick, cancelled by
  `CANCEL_OPEN_AT`. Stay **delta-neutral** (equal notional both sides; net inventory ≈ 0).
- Track `MAKER_REBATE`/`REWARD`/`YIELD` accruals from `/activity` into the `rebates` column so
  the dashboard shows true yield. *This is the single most aligned upgrade to our `MAKER_FARM`.*

### 2. Intra-market YES/NO arbitrage (Group B, infra-light variant) ⭐ [IMPLEMENTED, paper]
The `P_UP + P_DOWN = 1` invariant gives a **risk-free** trade whenever the book dislocates:
- If `up_ask + down_ask < 1 − fees` → **buy both sides**, guaranteed $1 payout for < $1 cost.
- If `up_bid + down_bid > 1 + fees` → **mint** ($1 → 1 UP + 1 DOWN via SPLIT) and sell both.
- Add a `pricing.py` helper `pair_arb_edge(up_ask, down_ask)` and a strategy branch in
  `signal_engine.py`. On thin 5-min books these dislocations happen and can persist > 1s, so
  our loop can catch some even without sub-100ms infra.

### 3. Add SPLIT / MERGE (mint conditional pairs) to the executor
Group C mints YES+NO from USDC to provide two-sided inventory cheaply (seen as `MERGE`/`SPLIT`
events). Required to do #1 and #2 efficiently. A live-execution feature for the next round.

### 4. Sharp-wallet copy-trade signal (Group A/B, additive) 
PolyCop-style. Poll `data-api/activity?user=<sharp>` for crypto-market fills and use them as a
*secondary confirmation* on our own fair-value signal (not blind mirroring — we'd still be late).
Candidate crypto wallets to watch: `strike123`, `Dropper` (`0x6bab…1292`), `prayingnotbroke`
(`0x0f0e…a019`). Lower priority; mostly useful as a feature into the model, not a standalone bot.

### 5. Cross-asset consistency check (Group B, research) 
BTC/ETH/SOL/XRP 5-min windows share the same clock. Extreme divergence in implied moves across
correlated assets can flag a mispriced book. Exploratory — fold into the recorder first.

### ⭐ Reward-scoring MATH (the thing we were under-exploiting) [FIXED]
Polymarket's official liquidity-reward score per resting order (docs.polymarket.com):

```
S(v, s) = ((v − s) / v)²          0 ≤ s ≤ v, else 0
   v = rewardsMaxSpread (4.5¢ on BTC 5m)   s = order's distance from mid
maker epoch reward = Σ(your scores) / Σ(everyone's scores) × pool
two-sided: Q_min = max( min(Q1,Q2), max(Q1,Q2)/3 )   (single-sided ≈ 1/3 credit;
           required when mid ∉ [0.10, 0.90]);  positions sampled every minute.
```

The score is **quadratic in proximity to mid**. We were quoting ~2¢ off mid → score
**0.31**; at 1 tick (1¢) off mid → score **0.61** — ~**2× the reward for the same capital**.
Fix shipped: `pricing.reward_score()`, `config.FARM_QUOTE_TICKS=1`, tight placement in
`signal_engine._reward_farm`, and the paper reward estimate now scales by the score.
*Live tradeoff (next round): tighter quotes fill more often, so a real deployment needs
two-sided inventory rebalancing/cancel logic — closeness to mid trades reward for fill risk.*

### Directional alpha — confirmed ≈ 0 for us (internet cross-check)
Public 5-min-crypto write-ups offer only one directional "edge": **last-second
Chainlink-latency arbitrage** (oracle updates every ~10–30s / 0.5% deviation → a 2–5s
settlement-feed divergence). That is the co-located race we explicitly do **not** enter; the
quoted "55–60% win rate" is unvalidated and tied to sniping. Their pricing model (driftless
GBM, σ≈0.5%) is what we already have. Conclusion: do not chase direction — calibrate σ
(done), then farm rewards + arb.

### What NOT to copy
- **Directional conviction (Group A)** — no transferable edge on a 5-min random walk.
- **Naked directional maker quoting** — wallet `0x59Ae…` (−$96k) and `0x4924…` (−$3.6M) show
  exactly the adverse-selection bleed we already designed against. Quote **two-sided for
  rewards**, never one-sided for "edge".
- **Latency racing the last second** — Group B runs $500k sub-100ms infra; we won't win that.

### Suggested build order
`#1 reward-farm` (turn the `MAKER_FARM` stub into a real two-sided quoter + reward tracking) →
`#2 YES/NO arb` (pure +EV, infra-light) → `#3 SPLIT/MERGE` execution → `#4 copy-trade` feature.
All measurable in paper via the existing recorder before any capital.
