# PolymarketBot — CLAUDE.md

**Market:** Polymarket BTC / ETH / SOL Up/Down 5-Minute (multi-asset)
**Thesis (current):** A vol-calibrated fair-value engine that does **one** validated thing —
**buy near-certain favorites in the last 10–45s that a stale order book hasn't repriced yet**
(feed-lag certainty). Everything else (bare directional prediction, reward farming, arbitrage)
has been measured to be zero-to-negative edge on these markets and is **off or paper-only**.
**Edge is measured in paper before any capital — that gate has not yet been cleared.**

> **Read this first (design philosophy).** Three theses have been *retired by evidence*, not opinion:
> 1. **Stale-order maker harvesting** — resting limits below fair value get adversely selected;
>    the rebate doesn't pay for it.
> 2. **Last-second sniping** — a 1s Python loop cannot win a latency race to *react to a new move*
>    against co-located bots. (We do NOT do this.)
> 3. **Bare directional EV-taker** — predicting the 5-min random walk *fails out-of-sample* even at
>    best calibration (8,044-window backtest: TEST −$1,140, PF 0.97). **Disabled** (`DIRECTIONAL_TAKER_ENABLED=False`).
>
> What survives evidence is **feed-lag certainty buying** (the winners' actual edge) — and even that
> is run **paper-only** until depth-realistic fills clear PF ≥ 1.5. See `APPROACH.md`.

---

## Project Status (2026-06-21)

**Operating leg = certainty / feed-lag gate (paper).** On the recovered 8,044-window DB, this is the
only leg with a genuine out-of-sample edge, and the edge is **concentrated in the last 10–45s**:

| Firing zone (realistic +1-tick fill) | win % | PF | EV/trade |
|---|---|---|---|
| 45–220s (mid-window) | 81.8% | 1.14 | $0.68 |
| **10–45s (late slice)** | 86.4% | **1.59** | **$2.03** |
| **10–45s + move ≥ 5bp** | 89.2% | **1.91** | **$2.51** |
| **10–45s + move ≥ 10bp** | 92.0% | **2.57** | **$3.18** |

The late slice clears the PF ≥ 1.5 live gate; the mid-window does not. So the gate now fires down to
**T-10s**, requires the oracle to have **already moved ≥ 5bp from strike** (the winners' "Window
Delta" signal), and **sizes up** in the validated late slice. It is **paper-only** — it records a
`leg='CERTAINTY'` ledger row via the live book's depth-walk + adverse-tick fill, never opens a real
position. (Reproduce: `python3 backtest.py --db <recovered.db> --certainty`; probe
`sy/cert_zone_experiment.py`.)

**What's off / dead (by evidence):**
- **Bare directional taker** (`Action.IOC_*`) — `DIRECTIONAL_TAKER_ENABLED=False` (fails OOS, was the
  live bleed: a −$52 session was 100% this leg).
- **Reward farm** — BTC/ETH/SOL 5-min markets carry `rewards.rates=null` (pool unfunded; verified via
  CLOB `getMarketInfo` 2026-06-21). Quoting earns ~$0; **dropped from the 5-min thesis.** Only EVENT
  markets with a funded `rates` pool pay. (`FARM_ENABLED=True` is a harmless no-op here — the leg
  self-skips on a null pool.)
- **YES/NO arbitrage** — fires so rarely it's statistically nil (+$29 / 34 trades historically).
  Left enabled (risk-free when it does fire) but not a pillar.

**The box exit is acknowledged selection bias, not edge.** Historically the system was net-positive
*only* because the hedge-to-box exit harvested favorable drift while adverse drift rode to a full
loss. `PAPER_FILL_REALISM=True` exists to stress whether that 21¢ hedge is real in live depth.

**Not yet proven (gating live capital):** the certainty leg's PF is **1.24 OOS** (< the 1.5 gate), and
the order-book **depth-walk VWAP** is unmeasured (backtest used top-of-book ticks). A clean paper run
that walks the real book is what decides it. See Paper→Live Checklist.

---

## Multi-asset architecture (BTC / ETH / SOL)

One **`AssetWorker` per asset** (`main.py`): its own Binance/Coinbase/Chainlink-RTDS feeds, CLOB book
socket, clock-driven window discovery, signal engine, executor, strike thread and 1s loop — fully
isolated, so one asset's stall cannot miss another's strike. XRP is defined in `ASSET_PARAMS` but off
by default. Run subsets via `--assets BTC,ETH` or env `ASSETS=...`.

- **Asset-aware schema:** `asset` column on positions/signals/ticks/trades; `outcomes` keyed
  `PRIMARY KEY (asset, start_ts)` (the 300s grid collides across assets). Migration is automatic and
  idempotent.
- **Risk:** open-position guard + cooldown are **per asset**; `MAX_DAILY_LOSS` is **global** (one
  bankroll). `MAX_STAKE_PER_MARKET` applies per asset-window.
- **Dashboard:** POLYDESK with asset tabs (per-asset day P&L, open-position dot, strike-thread
  health), countdown ring, model-vs-market prob bar, strategy pipeline, and an asset-tagged trade log
  including the paper `CERTAINTY` shadow tally.
- **Backtest:** joins on `(asset, start_ts)`, `--asset` filter, per-asset breakdown.

---

## Market Mechanics

Every 5 minutes per asset: `"Bitcoin/Ethereum/Solana Up or Down — June 21, 8:40AM–8:45AM ET"`.
Markets are Gamma **events** with slug `<btc|eth|sol>-updown-5m-<start_unix_ts>`, where `start_ts`
is a unix multiple of 300 → we build each window's slug directly from the clock.

- **T=0:** window opens; the **Chainlink BTC/USD (Data Streams)** price is the reference (strike).
  The strike is **not published pre-resolution**, so the bot **snapshots it at window open** (within
  `REFERENCE_MAX_LAG=3s`; a late snapshot flags the window `MISSED` and it is never traded — a wrong
  strike manufactures phantom edge).
- **T=300s:** if Chainlink price ≥ reference → Up wins ($1.00), else Down wins ($1.00).
- **Resolution source is `https://data.chain.link/streams/btc-usd`** — NOT Binance. We model against a
  **Coinbase-weighted oracle proxy** and widen σ by the cross-venue basis; the **real** outcome is
  fetched from the resolved event.
- Core invariant: `P_UP + P_DOWN = $1.00`.
- **Fees (V2, Mar 2026):** Taker = `0.07 · p · (1−p)` per share (max ~1.75% at 50¢, ~0 at the
  extremes). Maker = 0. Maker rebate = 20% of the crypto taker pool — but the 5-min liquidity-rewards
  **pool is unfunded** (`rates=null`), so it pays nothing here.

---

## Strategy: one fair value, evaluated each tick (`signal_engine.evaluate`)

Priority order, but in the current config only the certainty shadow + (rare) arb actually act:

```
① ARBITRAGE  (any time, risk-free)            ENABLED but ~never fires
   up_ask + down_ask < 1 − fees  →  buy both, guaranteed $1. Gated by MIN_ARB_EDGE.

② BARE DIRECTIONAL TAKER  [45s ≤ t ≤ 220s]    DISABLED (DIRECTIONAL_TAKER_ENABLED=False)
   IOC at best ask on fee-net EV. Fails out-of-sample; off.

③ REWARD FARM  [t > 60s]                       NO-OP on 5-min (rewards.rates=null)
   Two-sided delta-neutral quotes. Only earns on EVENT markets with a funded pool.

★ CERTAINTY / FEED-LAG  [10s ≤ t ≤ 220s]       PAPER-ONLY, the validated edge
   Isolated shadow read of the computed signal (NOT in the elif dispatch, so it can neither
   preempt nor be preempted). Fires the confident side iff:
     p_side ≥ CERTAINTY_FLOOR (0.80)                     — model is sure
     AND |distance_bp| ≥ CERTAINTY_MIN_MOVE_BP (5bp)     — oracle already moved (Window Delta)
     AND ask ≤ p_side − CERTAINTY_LAG_MARGIN (0.03)      — book still underprices it (the lag)
     AND ask ≤ CERTAINTY_MAX_ASK (0.97) AND spread ≤ MAX_SPREAD AND fee-net EV ≥ 0
   Size: $25 base, $50 in the late slice (t ≤ CERTAINTY_LATE_FROM=45), capped $50.
   Records leg='CERTAINTY', resolved against the REAL outcome in _resolve_cert_shadow.

BOX EXIT  — open positions can hedge into a $1 box (BOX_STOP_*). Selection bias, not edge;
   under realistic-fill stress.
```

`LATE_MOMENTUM` is a separate experimental paper shadow (`LATE_MOMENTUM_ENABLED=False` by default).

---

## Probability Model — driftless random-walk barrier (`signal_engine.barrier_p_up`)

```
σ_price = S · σ_ret · VOL_MULT · √t_remaining          # σ_ret = realized per-second return vol
σ_total = √( σ_price² + (BASIS_VOL_INFLATE · S · basis_bp/1e4)² )   # widen for CEX disagreement
P(Up)   = Φ( (S − ref + drift) / σ_total )             # drift ≈ 0 (DRIFT_WEIGHT = 0)
```

`VOL_MULT=0.5` (recalibrated 2026-06-19 on 8,044 REAL-resolved windows; best Brier 0.155–0.163 on all
three assets — the old 0.7 was tuned on 258 windows and was over-dispersed). The model is good *signal*
(monotonic, sub-coin-flip Brier) but its only profitable *use* is identifying the genuine
high-certainty / feed-lag states the certainty gate trades — NOT a bare directional bet.

Trade gating is fee-net **expected value**, not a flat cent edge (`pricing.py`):
`taker_ev_per_share(p, ask) = (p − ask) − 0.07·ask·(1−ask)`. The fee peaks at 50¢ and ~0 at the
extremes, so EV gating naturally avoids the coin-flip zone — which is exactly where bare directional
prediction bled.

---

## Architecture

```
Binance WS (aggTrade)  ──┐
Coinbase WS (ticker)   ──┼── oracle_feed.Oracle (blend + basis + vol)
Chainlink RTDS WS      ──┘           │
                                      │
Polymarket CLOB WS (book)─── signal_engine.py ──(pricing.py EV)── executor.py ── CLOB
                                          │
                                      risk.py · state.py (SQLite: signals/positions/ticks/outcomes/trades)
                                          │
                                      dashboard_server.py → dashboard/index.html
                                      backtest.py (offline: calibration, taker, certainty)
```

| File | Role |
|---|---|
| `config.py` | All constants; `ASSETS` + `ASSET_PARAMS` (per-asset feeds/slugs/titles) |
| `binance_feed.py` | Binance WS per asset; rolling price, 15s momentum, realized vol |
| `oracle_feed.py` | Coinbase + Chainlink-RTDS WS per asset + `Oracle`: settlement-proxy price, CEX basis, vol |
| `polymarket_book.py` | CLOB WS per asset; local bid/ask book, PING heartbeat, reconnect re-subscribe, `fill_ask` VWAP depth-walk |
| `market_discovery.py` | Clock→slug→event fetch; token IDs, timing, reward params, **real resolution** |
| `signal_engine.py` | `barrier_p_up` model, `FairValueModel`, `evaluate()` legs, `certainty_shadow()` gate |
| `pricing.py` | Fee, EV, `pair_arb_edge`, reward-score math (single source of truth) |
| `executor.py` | Per-asset IOC taker, `execute_arb`, `run_farm`, `box_position`. Paper = log only |
| `risk.py` | Per-asset guard: cooldown + open-position; global daily-loss halt |
| `state.py` | SQLite (asset-tagged): signals, positions, P&L, **ticks**, **outcomes (PK asset+start_ts)**, **trades** (shadow legs) |
| `main.py` | `AssetWorker` per asset (1s loop, strike thread, resolution, certainty shadow, box-stop) + `BotRunner` |
| `backtest.py` | Calibration (Brier/table), `simulate_taker`, `simulate_certainty` (zone/move parametrised) |
| `dashboard_server.py` | WebSocket server → pushes JSON state every second |
| `dashboard/index.html` | POLYDESK dashboard (asset tabs, pipeline, certainty tally) |
| `sy/cert_zone_experiment.py` | One-off probe behind the late-zone finding (zone × move grid, OOS split) |

---

## Key Config (`config.py`)

```python
# Risk
MAX_STAKE_PER_MARKET = 25.0     # USDC per asset-window
MAX_DAILY_LOSS       = 50.0     # global hard halt
MAX_OPEN_POSITIONS   = 1        # per asset

# Bare directional taker — DISABLED (fails OOS)
DIRECTIONAL_TAKER_ENABLED = False
MIN_EV_TAKER  = 0.03 ; MIN_TAKER_ENTRY = 0.72   # only relevant if re-enabled
TAKER_ZONE_START/END = 220/45

# Model
VOL_MULT = 0.5                  # recalibrated on 8,044 windows
DRIFT_WEIGHT = 0.0 ; BASIS_VOL_INFLATE = 1.0
REFERENCE_MAX_LAG = 3           # trust a strike only within 3s of T=0

# CERTAINTY / FEED-LAG gate (the validated edge — PAPER ONLY)
CERTAINTY_SHADOW_ENABLED = True
CERTAINTY_FLOOR      = 0.80     # model must be this sure
CERTAINTY_LAG_MARGIN = 0.03     # book must underprice by this much
CERTAINTY_MAX_ASK    = 0.97
CERTAINTY_ZONE_START/END = 220/10   # extended to T-10s (edge concentrated in the late slice)
CERTAINTY_MIN_MOVE_BP = 5.0     # Window-Delta gate: oracle must already have moved
CERTAINTY_LATE_FROM  = 45       # ≤ this t_remaining ⇒ late-slice sizing
CERTAINTY_SIZE_USDC  = 25.0 ; CERTAINTY_LATE_SIZE_USDC = 50.0 ; CERTAINTY_MAX_SIZE_USDC = 50.0

# Box exit (selection-bias hedge, under realistic-fill stress)
BOX_STOP_ENABLED = True ; BOX_STOP_MARGIN_LOSS = 0.10 ; BOX_STOP_MARGIN_PROFIT = 0.20
PAPER_FILL_REALISM = True ; PAPER_SLIPPAGE_TICKS = 1

# Dead-on-5m legs (kept for event markets / safety)
FARM_ENABLED = True             # no-op when rewards.rates=null (always, on 5-min)
ARB_ENABLED  = True ; MIN_ARB_EDGE = 0.005
```

---

## Data Streams

| Stream | URL | Auth |
|---|---|---|
| Binance trades | `wss://stream.binance.com:9443/ws/<btc\|eth\|sol>usdt@aggTrade` (1/asset) | None |
| Coinbase trades | `wss://ws-feed.exchange.coinbase.com` (ticker, `<BTC\|ETH\|SOL>-USD`, 1/asset) | None |
| Chainlink RTDS | `wss://ws-live-data.polymarket.com` (`crypto_prices_chainlink`, `<btc\|eth\|sol>/usd`, 1/asset) | None |
| Polymarket book | `wss://ws-subscriptions-clob.polymarket.com/ws/market` (1/asset) | None |
| CLOB REST | `https://clob.polymarket.com` | HMAC L2 |
| Gamma events | `https://gamma-api.polymarket.com/events?slug=<btc\|eth\|sol>-updown-5m-<ts>` | None |

Heartbeats: Polymarket needs `PING` every 10s or drops, and `polymarket_book` re-subscribes on every
(re)connect (a 2nd subscription on the same socket is silently ignored — proven bug, now reconnect
per window).

---

## Run

```bash
pip install -r requirements.txt
cp .env.example .env                    # no keys needed for paper mode
python3 main.py --mode paper            # all of config.ASSETS; dashboard WS :8888 / HTTP :8000
python3 main.py --mode paper --assets BTC,ETH
python3 backtest.py --db <recovered.db> --certainty   # the validated leg's P&L / OOS / sweep
python3 backtest.py --db <recovered.db> --sweep        # calibrate σ (Brier/calibration table)
python3 backtest.py --asset ETH --buckets              # per-asset entry-bucket P&L
python3 main.py --mode live             # requires PRIVATE_KEY + CLOB_API_KEY; see warning below
```

> **⚠ Live mode is intentionally quiet.** With `DIRECTIONAL_TAKER_ENABLED=False` and the certainty
> leg paper-only, `--mode live` places **no real directional orders by design** — the only validated
> edge is not yet cleared for capital. This is correct per the Paper→Live Checklist. Run **paper** to
> see the certainty leg fire (~44% of windows) and accumulate depth-realistic fills.

Note: on a laptop, OS sleep stalls every feed and windows get flagged `MISSED` (correct — never trade
a window whose open you didn't see). Run on a VPS or `caffeinate -dims`. `python3` (not `python`).

### Unattended VPS run

```bash
tmux new -s polybot './run.sh'          # supervisor: auto-restarts on crash/OOM
sudo cp polybot.service /etc/systemd/system/ && sudo systemctl enable --now polybot
ssh -L 8000:localhost:8000 user@vps     # view the dashboard privately (host stays 127.0.0.1)
(crontab -l; echo "0 * * * * cd ~/PolymarketBot && python3 backup_db.py --keep 48") | crontab -
```

The bot quarantines a corrupt `bot_state.db` on startup and runs an hourly prune + WAL checkpoint
(`synchronous=NORMAL` + WAL) so a multi-day session stays bounded and crash-safe. **Never `cp` a live
WAL DB** — that corrupts the copy (it's how the `ticks`/`signals` tables were corrupted once; recovered
via `sqlite3 .recover`).

> **If the bot is "barely trading" on the VPS:** check the logs for repeated `"Bot started"` and the
> now-`WARNING`-level `strike MISSED (... oracle.connected=...)` line. A "no price feed" reason within
> 3s of T=0 means feeds were dead at the boundary — almost always the process restarting at window
> opens. A missed strike voids the window for **every** leg (all gate on `has_reference`).

---

## Paper → Live Checklist

- [ ] ≥ 300 windows of fresh (post-2026-06-21) recorded `ticks` + resolved `outcomes` **per asset**
- [ ] **Certainty leg PF ≥ 1.5 on depth-realistic paper fills** (real book VWAP, not top-of-book) —
      the live run is the only thing that measures the depth-walk the backtest could not
- [ ] Model calibrated per asset: Brier < 0.25 and a flat calibration table (`--asset`, `--buckets`)
- [ ] Edge survives the real CEX→Chainlink basis (it is in the recorded data)
- [ ] Strike snapshot verified within `REFERENCE_MAX_LAG` of T=0 (no `MISSED` windows traded)
- [ ] Both WebSocket reconnects tested (book re-subscribes, feeds recover)
- [ ] Live order path (fills/partials/settlement, slippage guard) hardened — separate round
- [ ] **Only then** flip the certainty leg from paper to live capital, with Kelly-bounded sizing

---

## Legal

Polymarket is geo-restricted. Verify jurisdiction. Start with ≤$50 USDC. Fee structure may change —
call `getClobMarketInfo()` before each session.

---

## References

- [Polymarket CLOB Docs](https://docs.polymarket.com/developers/CLOB/introduction) · [Maker Rebates](https://docs.polymarket.com/developers/market-makers/maker-rebates-program)
- [How BTC 5-Min Scalpers Work (Mountain Movers, May 2026)](https://medium.com/mountain-movers/how-btc-5-minute-scalpers-actually-work-on-polymarket-building-the-bot-that-trades-stale-order-a16e84eb3140)
- [Profitable 5-min bot guide (Benjamin Cup, Substack)](https://benjamincup.substack.com/p/the-ultimate-guide-to-building-a)
- [Polymarket Dynamic Fees](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- Internal: `APPROACH.md` (diagnosis + operating plan), `LEADERBOARD_ANALYSIS.md` (wallet evidence)
