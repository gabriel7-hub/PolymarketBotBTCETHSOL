# PolymarketBot — CLAUDE.md

**Market:** Polymarket BTC / ETH / SOL Up/Down 5-Minute (multi-asset since 2026-06-11)  
**Strategy:** Calibrated fair-value engine → (1) fee-net IOC taker, (2) rebate/reward farming  
**Edge:** A correctly-modeled, vol-calibrated probability vs a sometimes-stale order book, traded only when expected value clears the actual per-price fee. Plus maker rebates as yield.

> **Design note (foundation rebuild).** The original "stale-order maker harvester" thesis
> was retired: resting limit orders below fair value get **adversely selected** (you only
> fill when informed flow trades against you), and the ~0.35% rebate does not pay for it.
> We also do **not** attempt last-second (Phase-3) sniping — a 1s Python loop cannot win
> that latency race against co-located bots. We compete on **calibration + mid-window
> mispricings + rebate yield**, and we **measure edge in paper before risking capital.**

---

## Project Status (2026-06-11) — FINAL paper-trading architecture

**Multi-asset expansion (this round, final):** the bot now trades **BTC, ETH and SOL**
5-min windows concurrently (XRP defined in `ASSET_PARAMS`, off by default).
- One **`AssetWorker` per asset** (`main.py`): its own Binance/Coinbase/Chainlink-RTDS
  feeds, CLOB book socket, window discovery, signal engine, executor, strike thread and
  1s loop — fully isolated; one asset's stall cannot miss another's strike.
- **Asset-aware schema**: `asset` column on positions/signals/ticks/trades; `outcomes`
  rebuilt with `PRIMARY KEY (asset, start_ts)` (the 300s grid collides across assets).
  Migration is automatic, idempotent, and verified on the real DB (all prior rows = BTC).
- **Risk**: open-position guard + cooldown are **per asset**; `MAX_DAILY_LOSS` stays
  **global** (one bankroll). `MAX_STAKE_PER_MARKET` applies per asset-window.
- **Dashboard**: POLYDESK has asset tabs (per-asset day P&L + open-position dot);
  trade history shows an Asset column. State shape: `{bot, assets:{BTC,ETH,SOL}, ledger}`.
- **Backtest**: joins on `(asset, start_ts)`, `--asset` filter, per-asset breakdown.
  3× window throughput ⇒ the ≥300-window calibration bar fills ~3× faster.
- Run subsets via `--assets BTC,ETH` or env `ASSETS=...`.

## Previous Status (2026-06-06)

**Foundation rebuild — complete & verified live (paper):**
- ✅ **Oracle/settlement fixed.** Coinbase-weighted `Oracle` proxy + cross-venue basis;
  real Chainlink-based resolution fetched from the resolved event (no Binance proxy).
- ✅ **Deterministic discovery.** Window slug built from the clock; events endpoint;
  token IDs + reward params parsed from the real `clobTokenIds`/`outcomes` schema.
- ✅ **Vol-barrier model + EV gating** (`barrier_p_up`, `pricing.py`). Verified sane
  (0.5 at boundary, sharper as t→0, basis inflates σ).
- ✅ **Recorder + backtester** (`ticks`/`outcomes` tables, `backtest.py --sweep`) —
  runs end-to-end on real recorded data (Brier/calibration/P&L).
- ✅ **Strike snapshot** clock-driven at the T=0 boundary with a MISSED guard (verified
  rejecting late windows so we never trade a wrong strike).
- ✅ **Leaderboard strategies added** (see `LEADERBOARD_ANALYSIS.md`): ① YES/NO arbitrage
  and ③ two-sided reward farm, both firing live in paper.
- ✅ **Dashboard rewritten** (POLYDESK) and fully wired to live WS state.

**Bugs found & fixed during validation:**
- Polymarket market WS **ignores a 2nd subscription on the same socket** (proven: 0 books
  on re-subscribe) → every window after the first traded on an empty book. Now reconnects
  per window. *(`polymarket_book.subscribe`)*
- **Cross-window resolution misattribution** — a resolving window closed whatever position
  was open; now matched by `condition_id`. *(`main._resolve_position`)*
- `RiskGuard` read the DB before `init_db()`; `_dash_state` field drift; PONG parse noise.

**Not yet proven (gating live):** the model is calibrated machinery but only ~1 valid-strike
window has resolved so far — needs ≥300 windows of recorded data before the Brier/P&L numbers
mean anything. See Paper→Live Checklist.

---

## Upcoming Enhancements (roadmap)

**Next round — make edge measurable & real:**
1. **Accumulate ≥300 windows** of paper `ticks`+`outcomes`, then `backtest.py --sweep` to
   calibrate `VOL_WINDOW_SECS` / vol-mult until the calibration table is flat (Brier well < 0.25).
2. **Tune EV thresholds** (`MIN_EV_TAKER`, `MIN_ARB_EDGE`) and `BASIS_VOL_INFLATE` from the
   recorded basis distribution; confirm profit factor ≥ 1.5 on the taker leg.

**Then — live execution hardening (separate, risk-bearing round):**
3. **SPLIT / MERGE** in `executor.py` (mint $1 → 1 UP + 1 DOWN) to fund two-sided inventory
   cheaply — prerequisite for live farm + arb. *(LEADERBOARD_ANALYSIS #3)*
4. **Real two-sided order management** for the farm leg: place/refresh/cancel both quotes,
   track net inventory & delta-neutrality, reconcile fills, real reward accrual from
   `/activity` (`MAKER_REBATE`/`REWARD`/`YIELD`) instead of the paper estimate.
5. **Live fill handling** for the taker/arb legs: partial fills, slippage guard
   (`MAX_SLIPPAGE`), settlement reconciliation against on-chain outcome.

**Additive edges (after the above):**
6. **Copy-trade signal** — poll `data-api/activity` for sharp crypto wallets (e.g. `strike123`,
   `Dropper`, `prayingnotbroke`) as a *confirmation* feature into the model. *(LEADERBOARD #4)*
7. **Cross-asset consistency** — compare implied moves across BTC/ETH/SOL/XRP 5-min windows
   to flag mispriced books. *(LEADERBOARD #5)*
8. **Latency**: move the 1s loop to event-driven (react to book ticks) so mid-window taker
   fills are less stale. Still NOT attempting last-second sniping.
9. **Optional**: true Chainlink Data Streams feed (credentialed) to replace the Coinbase
   proxy and shrink strike basis error.

---

## Market Mechanics

Every 5 minutes per asset: `"Bitcoin/Ethereum/Solana Up or Down — June 6, 11:30AM–11:35AM ET"`.
Markets are Gamma **events** with slug `<btc|eth|sol>-updown-5m-<start_unix_ts>`, where
`start_ts` is a unix multiple of 300 → we construct each window's slug directly from the
clock (all assets share the same grid and the same schema — verified live 2026-06-11).
- T=0: window opens; **Chainlink BTC/USD (Data Streams)** price is the reference (strike).
- T=300s: if Chainlink BTC ≥ reference → Up wins ($1.00), else Down wins ($1.00).
- **Resolution source is `https://data.chain.link/streams/btc-usd`** — NOT Binance. We
  model against a **Coinbase-weighted oracle proxy** and widen σ by the cross-venue basis.
  The strike is not published pre-resolution, so the bot **snapshots it at window open**.
- Core invariant: `P_UP + P_DOWN = $1.00`.
- **Fees (V2, Mar 2026):** Taker = `C × 0.07 × p × (1−p)` per share (max ~1.75% at 50¢,
  tiny at the extremes). Maker = 0. Maker rebate = 20% of the crypto taker pool, paid daily.

---

## Competitive Landscape

| Generation | Strategy | Status |
|---|---|---|
| Gen 1 | Pure latency arb (Binance lag → Polymarket) | **DEAD** — fees > spread |
| Gen 2 | Last-second IOC takers (30–60s snipe) | Alive but 50+ co-located bots — we don't compete here |
| Gen 3 | Directional stale-order maker harvester | **RETIRED** — adverse selection (see design note) |
| **Gen 4 (us)** | **Calibrated fair value → reward farm + YES/NO arb + mid-window taker** | **Current** |

Gen 1 turned $313 → $438K then died on dynamic fees. The leaderboard's durable, infra-light
money in our niche is **reward/rebate farming** (Group C: ~0 PnL on huge volume, all yield is
incentives) and **arbitrage** — not directional conviction. Full breakdown with wallets and
inferred strategies in `LEADERBOARD_ANALYSIS.md`.

---

## Strategy: One Fair Value, Three Legs (priority order)

Each tick `signal_engine.evaluate()` checks legs in priority order (see `LEADERBOARD_ANALYSIS.md`
for why these are the durable edges in our niche):

```
① ARBITRAGE  (any time)        risk-free, leaderboard Group B
   up_ask + down_ask < 1 − fees  →  buy both, guaranteed $1 payout.
   Gated by MIN_ARB_EDGE (locked $/pair after both taker fees). Once per window.

② FAIR-VALUE TAKER  [45s ≤ t_rem ≤ 220s]   directional, needs a strike
   IOC at best ask only when fee-net EV/share ≥ MIN_EV_TAKER and spread ≤ MAX_SPREAD.

③ REWARD FARM  [t_rem > 60s]   delta-neutral, leaderboard Group C
   Two-sided quotes within rewardsMaxSpread of mid at ≥ rewardsMinSize ($50).
   Trade PnL ≈ 0 by design; return is the liquidity-rewards pool + maker rebates.

   CLOSEOUT [t_rem < 45s] — no new positions. We do NOT snipe the last seconds;
   latency loss to co-located bots is unwinnable.
```

Positions resolve on the **real** Polymarket outcome (fetched from the resolved event),
never a Binance proxy. Reward/arb P&L is accrued separately (`state.add_reward/add_arb_pnl`).
Live execution of the farm/arb legs (SPLIT/MERGE, two-sided order management) is the next round.

---

## Probability Model — driftless random-walk barrier (`signal_engine.barrier_p_up`)

```
σ_price = S · σ_ret · √t_remaining           # σ_ret = realized per-second return vol
σ_total = √( σ_price² + (k · S · basis_bp/1e4)² )   # widen for CEX disagreement
P(Up)   = Φ( (S − ref + drift) / σ_total )   # drift ≈ 0 (DRIFT_WEIGHT default 0)
```

This is the correct closed form for "will price be ≥ ref after t seconds" and is
**calibratable** via `backtest.py` (Brier score + calibration table + `--sweep` over σ).

**Trade gating is expected-value, not a flat cent edge** (`pricing.py`):
- `taker_ev_per_share(p, ask) = (p − ask) − 0.07·ask·(1−ask)` ; trade if `≥ MIN_EV_TAKER`.
- `maker_ev_per_share(p, px)  = (p − px) + rebate(px) − haircut` ; quote if `≥ MIN_EV_MAKER`.

A flat threshold is wrong because the fee is largest at 50¢ and ~0 at the extremes —
EV gating naturally avoids the coin-flip zone and allows thinner edges when confident.

---

## Architecture

```
Binance WS (aggTrade)  ──┐
Coinbase WS (ticker)   ──┼── oracle_feed.Oracle (blend + basis + vol)
                          │           │
Polymarket CLOB WS (book)─┘   signal_engine.py ──(pricing.py EV)── executor.py ── CLOB
                                          │
                                      risk.py · state.py (SQLite: signals/positions/ticks/outcomes)
                                          │
                                      dashboard_server.py → dashboard/index.html
                                      backtest.py (offline, reads ticks+outcomes)
```

| File | Role |
|---|---|
| `config.py` | All constants; `ASSETS` + `ASSET_PARAMS` (per-asset feeds/slugs/titles) |
| `binance_feed.py` | Binance WS per asset, rolling price, 15s momentum, realized vol |
| `oracle_feed.py` | Coinbase + Chainlink-RTDS WS per asset + `Oracle`: settlement price, CEX basis, vol |
| `polymarket_book.py` | CLOB WS per asset, local bid/ask book, PING heartbeat, reconnect re-subscribe |
| `market_discovery.py` | Per-asset slug → event fetch; token IDs, timing, reward params, **real resolution** |
| `signal_engine.py` | `barrier_p_up` model, `FairValueModel`, 3-leg strategy (arb/taker/farm) |
| `pricing.py` | Fee, EV, `pair_arb_edge`, farm-reward estimate (single source of truth) |
| `executor.py` | Per-asset: IOC taker, `execute_arb`, `run_farm`, `box_position`. Paper = log only |
| `risk.py` | Per-asset guard: cooldown + open-position; global daily-loss halt |
| `state.py` | SQLite (asset-tagged): signals, positions, P&L, **ticks**, **outcomes (PK asset+start_ts)** |
| `main.py` | `AssetWorker` per asset (1s loop, strike thread, resolution, box-stop) + `BotRunner` aggregator |
| `backtest.py` | Forward-test: Brier/calibration + taker P&L, per-asset or combined (`--asset`) |
| `dashboard_server.py` | WebSocket server → pushes JSON state every second |
| `dashboard/index.html` | POLYDESK dashboard: asset tabs, countdown ring, model-vs-market prob bar, strategy pipeline, EV/arb meters, farm/reward panels, asset-tagged trade log |

---

## Key Config (`config.py`)

```python
MAX_STAKE_PER_MARKET = 25      # USDC per window
MAX_DAILY_LOSS       = 50      # hard halt
MIN_EV_TAKER         = 0.015   # min fee-net EV/share to fire an IOC taker
MIN_ARB_EDGE         = 0.005   # min locked $/pair (after both fees) to fire YES/NO arb
FARM_SIZE_USDC       = 50      # per-side reward-farm size (must clear rewardsMinSize)
FARM_EST_APR         = 0.40    # paper-only estimate of reward yield on quoted notional
MAX_SPREAD           = 0.06    # skip if book too wide
TAKER_ZONE_START/END = 220/45  # taker only fires in this t_remaining band
REFERENCE_MAX_LAG    = 3       # only trust a strike snapshotted within 3s of T=0
VOL_WINDOW_SECS      = 45      # realized-vol window (calibrate via backtest.py --sweep)
DRIFT_WEIGHT         = 0.0     # 0 = driftless (theoretically correct short horizon)
ADVERSE_SELECTION_HAIRCUT = 0.02  # $/share drag on resting maker EV
POST_LOSS_COOLDOWN   = 3       # skip N windows after a loss
CANCEL_OPEN_AT       = 30      # cancel maker quotes at T-30s
```

---

## Data Streams

| Stream | URL | Auth |
|---|---|---|
| Binance trades | `wss://stream.binance.com:9443/ws/<btc\|eth\|sol>usdt@aggTrade` (1 socket/asset) | None |
| Coinbase trades | `wss://ws-feed.exchange.coinbase.com` (ticker, `<BTC\|ETH\|SOL>-USD`, 1 socket/asset) | None |
| Chainlink RTDS | `wss://ws-live-data.polymarket.com` (`crypto_prices_chainlink`, `<btc\|eth\|sol>/usd`, 1 socket/asset) | None |
| Polymarket book | `wss://ws-subscriptions-clob.polymarket.com/ws/market` (1 socket/asset) | None |
| CLOB REST | `https://clob.polymarket.com` | HMAC L2 |
| Gamma events | `https://gamma-api.polymarket.com/events?slug=<btc\|eth\|sol>-updown-5m-<ts>` | None |

Heartbeats: Binance reconnects every 24h (listen for `serverShutdown`). Polymarket needs
`PING` every 10s or drops, and `polymarket_book` re-subscribes on every (re)connect.

---

## Run

```bash
pip install -r requirements.txt
cp .env.example .env                    # no keys needed for paper mode
python3 main.py --mode paper            # all of config.ASSETS (BTC,ETH,SOL); dashboard :8000
python3 main.py --mode paper --assets BTC,ETH   # subset
python3 backtest.py --sweep             # calibrate σ on recorded data (all assets)
python3 backtest.py --asset ETH         # per-asset calibration/P&L
python3 main.py --mode live             # requires PRIVATE_KEY + CLOB_API_KEY in .env
```

Note: on a laptop, OS sleep stalls every feed and windows get flagged MISSED (correct
behaviour — never trade a window whose open you didn't see). Run on a VPS, or keep the
machine awake (`caffeinate -dims`) for unattended paper recording.

Note: `python3` (not `python`) on this machine. Live execution is intentionally NOT
hardened yet (foundation round) — prove edge in paper first.

### Unattended VPS run (the supported way to record a clean week)

```bash
tmux new -s polybot './run.sh'          # supervisor: auto-restarts on crash/OOM
#   …or install the systemd unit:
sudo cp polybot.service /etc/systemd/system/ && sudo systemctl enable --now polybot
ssh -L 8000:localhost:8000 user@vps     # view the dashboard privately (host stays 127.0.0.1)
# Hourly SAFE snapshot (never `cp` a live WAL DB — that corrupts the copy):
(crontab -l; echo "0 * * * * cd ~/PolymarketBot && python3 backup_db.py --keep 48") | crontab -
```

The bot now **quarantines a corrupt `bot_state.db` on startup** (renames it
`*.corrupt.<ts>` and starts fresh) and runs an **hourly prune + WAL checkpoint** so a
multi-day session stays bounded and crash-safe (`synchronous=NORMAL` + WAL).

### Paper-fill realism (`config.PAPER_FILL_REALISM`, default ON)

Paper taker/box fills now walk the **real displayed ask depth (VWAP)** + one adverse tick
of latency slippage, instead of assuming the full stake fills at the touch. A box that
can't fully hedge within `BOX_MAX_FILL_SLIPPAGE` of the touch is **skipped** (the position
rides to resolution) — so the recorded box P&L is one we could actually capture. Set
`PAPER_FILL_REALISM=False` to reproduce the old optimistic ledger for comparison.

> **Open question this week answers:** the recorded directional taker leg is net-negative
> on all three assets (BTC −$823 / ETH −$250 / SOL −$705 on raw WIN−LOSS); the hedge-to-box
> exit is what makes the system positive. Re-run `backtest.py --validate --asset …` after a
> few days of realistic-fill data to confirm the box edge survives real liquidity.

---

## Paper → Live Checklist

- [ ] ≥ 300 windows of recorded `ticks` + resolved `outcomes` **per asset**
- [ ] **Model calibrated per asset**: Brier < 0.25 and a flat calibration table
      (`backtest.py --asset BTC/ETH/SOL` — vol dynamics differ; don't assume BTC's
      VOL_MULT transfers until each asset's table is flat)
- [ ] Profit factor ≥ 1.5 and positive EV/trade on the taker leg (per asset)
- [ ] Edge survives the real CEX→Chainlink basis (it is in the recorded data)
- [ ] Both WebSocket reconnects tested (book re-subscribes, feeds recover)
- [ ] Strike snapshot verified within REFERENCE_MAX_LAG of T=0 (no MISSED windows traded)
- [ ] Live order path (fills/partials/settlement) hardened — separate round

---

## Legal

Polymarket is geo-restricted. Verify jurisdiction. Start with ≤$50 USDC. Fee structure may change — call `getClobMarketInfo()` before each session.

---

## References

- [Polymarket CLOB Docs](https://docs.polymarket.com/developers/CLOB/introduction) · [Maker Rebates](https://docs.polymarket.com/developers/market-makers/maker-rebates-program)
- [How BTC 5-Min Scalpers Work (May 2026)](https://medium.com/mountain-movers/how-btc-5-minute-scalpers-actually-work-on-polymarket-building-the-bot-that-trades-stale-order-a16e84eb3140)
- [Polymarket Dynamic Fees](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [py_clob_client_v2](https://pypi.org/project/py-clob-client-v2/) · [Binance WS Docs](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)
