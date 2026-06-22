# WINNERS.md — The Infrastructure Blueprint for a Profitable Polymarket 5‑Minute Crypto Bot

> **What this document is.** A complete, build-ready technical specification for the
> *infrastructure-grade* version of a Polymarket BTC/ETH/SOL Up-or-Down 5-minute bot — the
> approach the actual top wallets (`Bonereaper` +$966K, `0xb27bc932…` +$652K) use to make money.
> It documents the edge, why our current 1-second Python system structurally cannot capture it,
> and **literally everything required to build the stack that can**: hosting, feeds, latency
> budget, order path, strategy, risk, capital, cost, and the validation gate before any capital.
>
> **Honest preface.** This is a hard, competitive, capital- and engineering-intensive build. Our
> own validation (see `certainty-pnl-and-askfloor-fix`, `event-driven-taker-fails-oos`,
> `certainty-boxing-fails`) proved that *no software tweak on the current stack reaches this edge* —
> it is an **infrastructure problem**. Independent practitioners have built low-latency Polymarket
> bots and **mothballed them as unviable** when they "never won on pure speed." Read §12 before
> committing money or months. This document is the map; it is not a promise.

---

## 1. The Edge, Stated Precisely

The durable money in this niche is **stale-price capture** (a.k.a. feed-lag taking):

> When a **fresh move happens on the canonical settlement feed (Chainlink Data Streams)**, the
> outcome-favored side becomes more likely *before the Polymarket order book reprices it*. The
> winner **buys that side at the stale (pre-reprice) price** in the milliseconds before the book
> catches up, then **caps the downside by partial boxing** when a window flips.

It is **not** prediction (forecasting the random walk fails out-of-sample), **not** market-making
(adverse selection bleeds — see wallet `0x59Ae…` −$96K), and **not** last-second sniping (fees +
co-located competition kill it — wallet `EVP-HalfKelly` −$5.7K).

### 1.1 Quantified from our own recorded data (2026-06-22)

| Fact | Value | Source |
|---|---|---|
| A fresh ≥8bp move **holds to resolution** | **71%** of the time | `event_taker.py` TEST 2 |
| Winners' actual entry price band | **0.32 – 0.47** | `btc5m-leaderboard-research` |
| Raw EV at 70% win / 0.40 entry | **+0.30 / share** | `event_taker.py` TEST 3 |
| **Our** favored-side ask when the move registers in our 1s/15s feed | **median 0.75** | `event_taker.py` TEST 1 |
| Share of moves where our book has **already repriced** (ask ≥0.55) by the time we see it | **71%** | `event_taker.py` TEST 1 |
| Share of moves where we still see the winners' 0.32–0.47 price | **7%** | `event_taker.py` TEST 1 |

**The entire P&L difference is a ~35¢ entry-price gap, and that gap is a latency race.** The signal
is identical (71% hold-rate). The winners get 0.40; we get 0.75; at 0.75 the trade is fairly priced
and the dynamic fee makes it negative. Worse, the only cheap fills a slow bot *can* still get are the
**adversely-selected** ones (about-to-revert), so naïvely chasing the signal *inverts* P&L
out-of-sample. **This is the problem the infrastructure in this document exists to solve.**

---

## 2. The Winners' Playbook (observed, fill-by-fill)

From `LEADERBOARD_ANALYSIS.md` + `btc5m-leaderboard-research` (24K trades, on-chain feeds):

1. **Breadth — trade essentially every window**, across `btc / eth / sol / xrp -updown-5m`. 4×
   markets every 5 minutes. Small edge × enormous turnover = the law of large numbers works for them.
2. **Event-driven entry**, mid-window (**t ≈ 140–190s**), **avg price 0.32–0.47** — the stale,
   pre-reprice price on the side a fresh CEX/Chainlink move just favored.
3. **Scale in — 13–20 incremental fills per window** (sweep the book for cheap average), not one shot.
4. **Net side correct ~70–85%** (14/21 to 17/20 windows).
5. **0% SELLs. Cap losses by buying the OPPOSITE side (boxing).** When wrong, they lock $1/pair.
   Worst observed window: **−$124 on ~$700 staked.**
6. **Partial, late, informed boxing.** Bonereaper boxes **57/66** windows but only **~50% of shares**
   (partial), starting **median t=228s** (last ~70s), at **median pair cost $0.984** (often a small
   *credit*), and **56% of hedged legs were heading to a loss** (informed timing). He **keeps trading
   after the hedge** (44/57) — post-hedge re-entry.
7. Weakest of the three (`UUDDLRLR`) **full-boxes at a premium ($1.013)** — the worst variant, and the
   one our retired full-box implementation resembled. The upgrade path is **partial + late + re-entry**.

> The losers: `suntori` −$1.25M (fades moves), `EVP-HalfKelly` −$5.7K (99¢ sniping). Do not copy them.

---

## 3. Why the Current System Structurally Cannot Do This

| Dimension | Current system | Required |
|---|---|---|
| Loop cadence | 1 second (Python) | sub-millisecond hot path |
| Price signal | 15s Binance momentum proxy, sampled at 1s | sub-second canonical feed (Chainlink Data Streams) |
| Entry price seen | 0.75 median (post-reprice) | 0.32–0.47 (pre-reprice) |
| Order path | Python `clob-client`, REST, single shot | pre-signed EIP-712, FAK, persistent conn, compiled lang |
| Hosting | generic VPS / laptop | co-located with the CLOB matching engine |
| Coverage | ~19% of windows (selective) | ~100% of windows × 4 assets |
| Loss-capping | none (certainty leg rides to full −$25) | partial/late/informed boxing |

By the time our stack *perceives* a move, the book has repriced in 71% of cases. **No amount of
strategy tuning fixes a perception-latency deficit.** Everything below is about closing it.

---

## 4. The Settlement Truth: Chainlink Data Streams

This is the single most important infrastructure insight, and the thing our current "CEX-proxy"
approach gets wrong.

- **Polymarket settles BTC/ETH/SOL/XRP 5-minute markets on Chainlink Data Streams** (BTC/USD etc.),
  *not* Binance. Strike = the Data Streams report at **T=0**; settlement = the report at **T=300s**.
  Up wins when end ≥ start (ties → Up). [Chainlink × Polymarket, see §13]
- **Data Streams is a PULL oracle with sub-second latency** — low-latency, timestamped,
  cryptographically signed reports generated off-chain and delivered on demand (vs. legacy push feeds
  that only update on 0.5% deviation / 3600s). Multi-source aggregated; DON-signed.
- **Implication:** the canonical price that decides every window is the Chainlink report. The bot that
  prices the market off **the Chainlink Data Streams report itself** — not a Binance proxy that is
  ~4–5bp off and milliseconds-to-seconds divergent — sees the *true* probability first. CEX feeds are
  **leading indicators** (they move slightly before Chainlink aggregates), but the **truth the book
  must converge to is Chainlink.**

### 4.1 Two ways to consume it (build both, prefer the first)

1. **Direct Chainlink Data Streams subscription** (the professional path). Chainlink Data Streams
   exposes a low-latency API/WebSocket + a verifiable report (the `report` blob + DON signature).
   Requires a **sponsored/credentialed API key** (commercial access). This is the lowest-latency,
   highest-fidelity copy of the settlement price — the same data the matching makers price off.
2. **Polymarket RTDS** (`wss://ws-live-data.polymarket.com`), topic `crypto_prices_chainlink`,
   `type: "*"`, filter `{"symbol":"btc/usd"}`. Payload = `{symbol, timestamp(ms), value}`. **PING
   every 5 seconds.** This is Polymarket *re-broadcasting* Chainlink — one extra hop of latency vs.
   direct, but free and already co-located in London. Use as a cross-check and fallback.

> Our current bot snapshots the strike from RTDS with an on-chain Polygon Chainlink fallback and a
> CEX proxy (`chainlink-strike-source-fix`). The infra version **replaces the CEX proxy as the
> pricing input with direct Data Streams**, and keeps the on-chain read only as a settlement audit.

---

## 5. The Fee Battlefield (this reshapes the strategy)

Polymarket introduced **dynamic taker fees specifically to kill latency arbitrage** on short-term
crypto markets. You must design around this. [Finance Magnates, see §13]

- **Taker fee = `0.07 · p · (1 − p)` per share.** Maker fee = 0. 20% of the pool is rebated to makers.
- The fee **peaks at the 50¢ coin-flip zone (~1.75%, quoted up to ~3.15% on some contracts)** — the
  exact zone the old "enter near 50/50, exit on convergence" latency-arb used. **That classic play is
  now dead by design.**
- The fee **falls toward the extremes**: at 0.40/0.60 ≈ 1.68%, at 0.85 ≈ 0.89%, near 0 past ~0.95.

**Strategic consequences:**
1. **Do not trade the 50¢ zone as a taker.** Enter the favored side *after* a move has pushed it off
   50/50, where the fee is lower and the signal (71% hold) is real.
2. **Maker fills are fee-free and earn the rebate.** The fastest infra can sometimes *rest* a
   marketable limit that gets taken at the stale price — capturing the move as a **maker** (0 fee +
   rebate) rather than a taker. This is the highest-EV fill *if* you can win queue priority; it is
   also where adverse selection lives, so it is conditional on speed, not a default.
3. **Net EV per trade must clear the fee at the entry price**, every time:
   `EV/share = p·(1−entry) − (1−p)·entry − 0.07·entry·(1−entry)`.

---

## 6. Infrastructure Architecture (the full stack)

```
                 ┌─────────────────────────── DUBLIN / LONDON (AWS eu-west-1 / eu-west-2) ───────────────────────────┐
                 │                                                                                                   │
 Chainlink Data  │   ┌────────────────┐     ┌──────────────────┐     ┌───────────────────┐     ┌─────────────────┐  │
 Streams (direct)│──▶│ FEED INGEST    │────▶│ FAIR-VALUE +     │────▶│ DECISION /        │────▶│ ORDER ENGINE    │──┼──▶ clob.polymarket.com
   (sub-second)  │   │ (Chainlink +   │     │ EDGE ENGINE      │     │ EXECUTION POLICY  │     │ (pre-signed     │  │    (CLOB matching, London)
 Binance WS  ───▶│   │  Binance/CB    │     │  • strike@T0     │     │  • is move fresh? │     │  EIP-712, FAK,  │  │
 Coinbase WS ───▶│   │  as leading    │     │  • live p vs book│     │  • book lagging?  │     │  HTTP/2 keep-   │  │
 Polymarket book │   │  indicators)   │     │  • fee-net EV    │     │  • scale-in plan  │     │  alive, nonce   │  │
   WS (book)  ──▶│   └────────────────┘     └──────────────────┘     │  • box trigger    │     │  pool)          │  │
                 │            ▲                                       └───────────────────┘     └─────────────────┘  │
                 │            │  PTP/NTP clock sync (windows are clock-driven, start_ts % 300)                        │
                 │   ┌────────┴────────┐                                                                              │
                 │   │ RISK / CAPITAL  │  per-asset stake, global daily-loss, correlated-exposure cap, kill switch   │
                 │   └─────────────────┘                                                                             │
                 └──────────────────────────── monitoring: tick-to-trade latency, fill quality, P&L attribution ─────┘
```

### 6.1 Compute location — the decisive choice

- **Polymarket's CLOB matching engine, Gamma API, WebSocket, and RTDS all run on AWS `eu-west-2`
  (London).** [QuantVPS / NYC Servers, see §13]
- **Host compute in AWS `eu-west-1` (Dublin)** → **<2ms** inter-region fiber to London, community-
  measured **0–1ms** ping to Polymarket's backend. From US-East it is ~130ms — **disqualifying**.
- Dublin/Ireland is **not** on Polymarket's geo-restricted list, so orders pass the geoblock. (You are
  responsible for your own legal jurisdiction — see §11.)
- Use an **EC2 cluster placement group** (single AZ, same rack domain) for the order engine to shave
  intra-AZ microseconds; AWS documents this for tick-to-trade workloads. Pin the order engine and the
  feed ingest in the same placement group.

### 6.2 The geographic tension (and its resolution)

The price *sources* and the order *destination* are far apart:

| Component | Region | Round-trip from Dublin |
|---|---|---|
| Polymarket CLOB (orders) | London `eu-west-2` | **~1–2ms** |
| Chainlink Data Streams | (delivered to your subscriber; co-locate the subscriber in EU) | sub-second |
| Binance matching/data | Tokyo `ap-northeast-1` | ~230ms |
| Coinbase data | Virginia `us-east-1` | ~70ms |

**Resolution:** the **settlement feed is Chainlink, delivered to a London/Dublin subscriber** — so the
price that *matters* is already next to your order engine. **Do not architect around racing Tokyo.**
Binance/Coinbase are optional *leading* indicators; if you want them at low latency, run a thin
**forwarder** in Tokyo/Virginia that normalizes and ships only deltas over a persistent connection to
Dublin — but the primary clock is Chainlink-in-London. This is the opposite of the naïve "co-locate
next to Binance" instinct, and it is the single biggest architecture correction in this document.

### 6.3 The latency budget (tick-to-trade target: < 10ms)

Every millisecond is the 35¢ gap. Budget and measure each hop:

| Stage | Target | How |
|---|---|---|
| Feed decode (Chainlink report → price) | < 0.5ms | zero-copy parse, no JSON reflection; pre-allocated structs |
| Edge computation | < 0.2ms | precomputed σ tables, no allocation in hot path, branch-light |
| Order construct + **EIP-712 sign** | < 1ms | **pre-sign order templates**; cache signer; fast secp256k1 (libsecp256k1) |
| Network to CLOB (Dublin→London) | ~1–2ms | persistent HTTP/2 / keep-alive TCP, warmed connection pool |
| Matching engine | exchange-side | use **FAK** marketable limit (partial fills OK) to grab whatever rests |
| **Total tick-to-trade** | **< 5–10ms** | the bar to beat the book reprice in >7% (today) → majority of cases |

> Reality check (§12): an independent team building a low-latency Polymarket bot in **Rust, hosted in
> Dublin**, found "most of the latency exists in the network," that the **official SDK's network
> checks must be stripped**, and that even a 500ms signal was too slow — "the orderbook is swept
> before we receive the wires." Your edge over them is that **your signal (Chainlink) is co-located,
> not a 500ms newswire** — but the lesson stands: kill every avoidable millisecond.

---

## 7. Software Components (module by module)

Build in a **compiled, async, low-GC language — Rust (preferred) or Go**. Python is disqualified for
the hot path (GIL, allocation, 1s-loop heritage). Keep Python only for offline research/backtest.

| Module | Responsibility | Key implementation notes |
|---|---|---|
| `feed/chainlink_streams` | Subscribe to Chainlink Data Streams; decode signed reports → `(symbol, price, ts)` | Direct API (sponsored key) + RTDS fallback. Verify DON signature out-of-band; trust the value in-path. |
| `feed/cex` | Binance/Coinbase WS as leading indicators | Optional Tokyo/Virginia forwarder shipping deltas only. |
| `feed/book` | Polymarket CLOB market WS per asset; local L2 book | Re-subscribe on every (re)connect (a 2nd sub on the same socket is silently ignored — known bug). PING per heartbeat. |
| `discovery/window` | Clock → slug `<asset>-updown-5m-<start_ts>` → token IDs | **Pre-fetch token IDs ~30s before T=0** so the entry path never blocks on Gamma. |
| `strike/capture` | Snapshot Chainlink report at T=0 (within ≤3s); flag MISSED otherwise | A wrong strike manufactures phantom edge — never trade a MISSED window. |
| `model/fair_value` | Driftless-barrier `P(Up)=Φ((S−ref)/σ_total)`; σ from realized vol + basis | Precompute σ vs t-remaining tables. The model is a *confidence gate on the move*, not a predictor. |
| `signal/edge` | `p` vs book ask; **is the move fresh?** (Chainlink delta in last N ms); fee-net EV | The freshness gate is the whole game: only act on a move the book has not yet absorbed. |
| `exec/order_engine` | Pre-signed EIP-712 orders, nonce pool, FAK marketable limits, partial-fill accounting, scale-in | Persistent warmed HTTP/2 pool to clob.polymarket.com. Strip SDK preflight checks. |
| `exec/position_mgr` | Track open shares per window; **partial/late/informed box**; post-hedge re-entry | Box when holding is dominated; partial size; trigger by Chainlink-implied p, not lagging book. |
| `risk/governor` | Per-asset stake, global daily-loss kill, correlated-exposure cap, breadth limiter | Hardware kill switch + auto-halt on latency/feed degradation. |
| `ops/telemetry` | Tick-to-trade histograms, fill price vs decision price, slippage, P&L attribution per leg | If you cannot measure tick-to-trade in **microseconds**, you cannot tune it. |

### 7.1 Order signing (the EIP-712 structure)

Orders are **EIP-712 typed-data messages**, validated by the Exchange contract for signature, tick
size, market status, collateral, and expiry, then matched off-chain and settled atomically on Polygon.
CLOB **V2** bumped the EIP-712 **domain version "1" → "2"** and added **EIP-1271** (smart-contract
wallet) support. Authentication is two-layer: **L1** = wallet EIP-712 signature (on-chain authority),
**L2** = API key/secret/passphrase (HMAC) for fast order management without re-signing the session.
**Pre-build and pre-sign order templates** (side, token, price ladder) for the imminent window so the
hot path only fills in size/nonce and ships.

### 7.2 Order type policy

- **FAK (Fill-And-Kill) marketable limit** for entries — grab whatever liquidity rests at/under your
  price, cancel the rest. This is the stale-price grab. (FOK is too brittle; GTC rests and gets
  adversely selected.)
- **Scale-in:** submit a *price ladder* of FAK clips (e.g. 5–20 small orders from best ask up to your
  max entry) to replicate the winners' 13–20-fill cheap average, instead of one large marketable order
  that walks the book against you.
- **Box leg:** FAK on the opposite token when the box trigger fires (§8.4), partial size.

---

## 8. Strategy Logic (the window lifecycle)

### 8.1 Pre-window (T-30s → T=0)
Pre-fetch token IDs, pre-sign order templates, confirm feeds healthy, sync clock.

### 8.2 Strike capture (T=0)
Snapshot the **Chainlink Data Streams** report as the strike (≤3s tolerance). Persist. MISSED ⇒ skip.

### 8.3 Entry (the stale-price grab, primarily t≈120–200s)
On each Chainlink tick:
1. Compute `p = P(side | current Chainlink price vs strike)`.
2. **Freshness gate:** did Chainlink just move ≥ θ bp within the last N ms *and* the book has not yet
   repriced (favored-side ask still ≤ `p − margin`)? If not, wait.
3. **Fee-net EV gate:** `EV/share > 0` at the live ask.
4. Fire a **FAK price ladder** on the favored side, sized small per clip, total ≤ per-window cap.
5. Record decision-price vs fill-price (your latency scoreboard).

### 8.4 Position management — partial, late, informed boxing
Each tick after entry, for an open position with shares `N` at avg entry `e`:
- Compute `p_side` from the **live Chainlink price** (not the lagging book).
- `opp_ask` = best ask on the opposite token. `locking_loss = (e + opp_ask) ≥ 1`.
- **Box trigger:** `p_side < 1 − opp_ask − margin` (margin tight ~0.10 when capping a loss, wider
  ~0.20 when locking profit).
- **Partial:** box only ~50% of `N` (Bonereaper's winning variant), not the full position.
- **Timing:** prefer the **last ~70s** (t≤70) so transient mid-window wobbles don't clip winners —
  this is the fix for why our full/early box destroyed P&L (`certainty-boxing-fails`).
- **Re-entry:** after hedging, you may re-enter if a new fresh move re-favors a side.

> **Why this works for the infra version when it failed for us:** our certainty leg boxed *full*,
> *early*, off a *lagging* book — it clipped small wins and missed late flips. The infra version boxes
> *partial*, *late*, off the *live Chainlink* p — exactly the profile Bonereaper runs profitably.

### 8.5 Settlement (T=300s + finality)
Outcome = Chainlink end ≥ start. Note Polygon **64-block (~2min) finality**; reconcile fills, realize
P&L, attribute by leg. Audit the on-chain Chainlink read against your in-path value.

---

## 9. Risk & Capital Management

- **Breadth, small clips.** The edge per window is thin; profit comes from volume × law of large
  numbers (the winners' model). Do **not** concentrate.
- **Per-window stake cap**, fractional. **Quarter-Kelly at most** — model uncertainty + competition
  make full Kelly ruinous. At ~71% hold / 0.40 entry the raw Kelly is large; cap it hard.
- **Correlated-exposure cap.** BTC/ETH/SOL move together (proven: a single correlated move flipped two
  −$25 certainty bets in one window). Cap *aggregate* same-direction exposure across assets per window,
  not just per-asset.
- **Global daily-loss kill switch** + **auto-halt on degradation** (feed gap, tick-to-trade p99 blows
  out, fill-price drifting from decision-price → your edge is gone; stop trading immediately).
- **Inventory neutrality drift:** track net delta across open windows; the box leg is the primary
  inventory control.

---

## 10. Cost Estimate (order-of-magnitude)

| Item | Est. monthly | Notes |
|---|---|---|
| AWS Dublin compute (placement group, order engine + feeds) | $200–800 | bare-metal/dedicated instance for jitter control |
| Optional CEX forwarders (Tokyo + Virginia) | $100–300 | thin delta forwarders |
| **Chainlink Data Streams commercial/sponsored access** | **$$$ (quote)** | the gating cost; pricing is commercial — budget meaningfully |
| Monitoring/log storage | $50–150 | latency + fill telemetry |
| **One-time engineering** | **2–6 months senior systems dev (Rust/Go)** | the real cost; this is not a weekend port |
| Trading capital (Phase 1) | $50–100 | tuition to measure live fills (see §11) |

**The dominant costs are engineering time and Data Streams access — not servers.**

---

## 11. Validation & Go-Live Path (do not skip)

Reuse our existing discipline (`Paper→Live Checklist` in `CLAUDE.md`), tightened for latency:

1. **Shadow mode in Dublin.** Run the full stack live but *paper* — record **decision-price vs the
   actual best ask you could have FAK'd**, and your **tick-to-trade** distribution. This is the one
   measurement that decides everything: *do we actually see 0.40, or still 0.75?*
2. **Latency gate:** tick-to-trade **p99 < 10ms**, and decision→achievable-fill slippage stable.
3. **Edge gate:** on shadow fills (real achievable prices, not top-of-book assumptions), **OOS PF ≥
   1.5 across all of BTC/ETH/SOL**, robust to dropping the top 5% of wins, with a **positive bootstrap
   5th-percentile**. (Our certainty leg is PF ≈ 1.07 — the infra must clear materially higher.)
4. **Regime coverage:** the edge holds across **high-vol and low-vol** days (≥2–4 weeks). The late-zone
   edge already failed to replicate once across datasets — demand regime robustness.
5. **Phase 1 capital = $50–100, $5 clips**, purely to validate the **live order path** (partial fills,
   nonce handling, settlement, real slippage). Treat it as buying information, not income.
6. **Scale only on confirmed live PF**, fractional-Kelly, with the kill switch armed.

---

## 12. Honest Risk Assessment (read before you commit)

- **You are racing professionals.** The makers on these books run sub-100ms infra and price directly
  off Chainlink. Being in Dublin gets you to the table; it does not guarantee you win queue priority.
- **Polymarket is actively defending against you.** Dynamic fees were introduced *specifically* to kill
  latency arbitrage and will likely keep evolving. Your edge can be regulated away by a fee tweak.
- **Practitioners have failed.** A documented Rust/Dublin low-latency Polymarket effort was
  **mothballed as unviable** — they "never won on pure speed," profiting only when "other bots hit
  their size limits." Your structural advantage is a **co-located Chainlink signal** (not a 500ms
  newswire), which is materially better — but it is not a guarantee.
- **Our own edge is thin.** Even the *idealized* signal is 71% hold — a real, but not enormous, edge
  that lives entirely on getting the cheap fill and capping losers. Miss either and it is negative.
- **Capital + time at risk.** Budget 2–6 months of senior systems engineering and commercial Data
  Streams access *before* the first dollar of edge. This is a venture, not a script.

**Bottom line:** this is the only architecture that can plausibly make money in this niche, and it is
genuinely hard, contested, and defended. Build it as a serious low-latency trading system or not at
all — a half-measure inherits all the cost and none of the edge.

---

## 13. References

**Internal (this repo):**
`CLAUDE.md` · `APPROACH.md` · `LEADERBOARD_ANALYSIS.md` · memories: `btc5m-leaderboard-research`,
`event-driven-taker-fails-oos`, `certainty-pnl-and-askfloor-fix`, `certainty-boxing-fails`,
`chainlink-strike-source-fix`.

**External (researched 2026-06-22):**
- Chainlink × Polymarket settlement deep dive — [BlockEden forum](https://blockeden.xyz/forum/t/deep-dive-how-chainlink-data-streams-power-polymarkets-5-minute-settlement-oracle-architecture-for-high-frequency-prediction-markets/786)
- Chainlink Data Streams — [chain.link/data-streams](https://chain.link/data-streams) · [BTC/USD stream](https://data.chain.link/streams/btc-usd-cexprice-streams)
- Polymarket × Chainlink partnership — [PRNewswire](https://www.prnewswire.com/news-releases/polymarket-partners-with-chainlink-to-enhance-accuracy-of-prediction-market-resolutions-302555123.html) · [The Block](https://www.theblock.co/post/370444/polymarket-turns-to-chainlink-oracles-for-resolution-of-price-focused-bets)
- Polymarket RTDS WebSocket — [docs.polymarket.com/market-data/websocket/rtds](https://docs.polymarket.com/market-data/websocket/rtds)
- CLOB V2 order execution deep dive — [Benjamin-Cup, Medium](https://medium.com/@benjamin.bigdev/how-polymarket-orders-actually-get-executed-a-deep-dive-into-clob-v2-for-developers-fdcd5d395ef5) · [Polymarket Trading Overview](https://docs.polymarket.com/trading/overview)
- CLOB V2 matching-engine refactor — [QuantVPS](https://www.quantvps.com/blog/polymarket-fixes-ghost-fills-clob-v2-upgrade)
- Polymarket hosting region (AWS eu-west-2 London) + Dublin latency — [QuantVPS](https://www.quantvps.com/blog/polymarket-servers-location) · [NYC Servers](https://newyorkcityservers.com/blog/polymarket-server-location-latency-guide) · [TradoxVPS](https://tradoxvps.com/best-vps-location-for-polymarket-trading-in-2026/)
- Dynamic fees vs latency arbitrage — [Finance Magnates](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- CEX colocation / AWS latency — [Crypto HFT latency](https://medium.com/@laostjen/high-frequency-trading-in-crypto-latency-infrastructure-and-reality-594e994132fd) · [AWS EC2 placement groups for MM latency](https://aws.amazon.com/blogs/industries/crypto-market-making-latency-and-amazon-ec2-shared-placement-groups/) · [Exchange server-location map](https://arbitron.app/learn/crypto-exchange-server-locations)
- Practitioner post-mortem (low-latency Polymarket bot, mothballed) — [EventWaves](https://eventwaves.substack.com/p/trying-to-build-a-low-latency-polymarket)

> Fee schedule, tick size, and reward params change — always call `getClobMarketInfo()` /
> re-read the live market object before a session. Verify your legal jurisdiction independently.
