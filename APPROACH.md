# APPROACH.md — Diagnosis & Operating Plan (current: 2026-06-21)

The evidence-driven strategy doc. `CLAUDE.md` has architecture/config; `LEADERBOARD_ANALYSIS.md` has
the wallet evidence. This file is **why we trade what we trade**, grounded in the recovered 8,044-window
backtest + live market/wallet research.

**One line:** We stopped trying to predict the 5-minute random walk. We buy **near-certain favorites in
the last 10–45s that a stale book hasn't repriced** (feed-lag certainty) — the winners' actual edge —
and we prove it in paper before risking a dollar.

---

## 1. What the evidence says

### 1.1 The directional model is good signal but a bad bet
`backtest.py --sweep` on 8,044 REAL-resolved windows: Brier **0.155–0.163** (well under the 0.25
coin-flip line), monotonic, best at `VOL_MULT=0.5`. But the calibration table shows the model is
**over-dispersed** — reality is more extreme than it predicts on both tails:

| Model P(Up) | Empirical UP freq (BTC / ETH / SOL) |
|---|---|
| 0.15 | 0.11 / 0.09 / 0.11 |
| 0.65 | 0.72 / 0.74 / 0.71 |
| 0.85 | 0.89 / 0.90 / 0.90 |

Shrinking σ (`VOL_MULT 0.7 → 0.5`) is a free calibration win and makes the model correctly flag genuine
high-certainty states — the input the certainty gate needs.

### 1.2 The bare directional taker FAILS out-of-sample (decisive)
Pure EV-gated directional taker, buy-at-ask, hold to resolution:

| | trades | win % | net | PF | EV/trade |
|---|---|---|---|---|---|
| Combined (vol_mult 0.5) | 7,982 | 49.9% | **−$1,733** | 0.98 | −$0.22 |

Chronological 70/30 OOS: best train params → **TEST −$1,140, PF 0.97 → FAILS.** By entry-price bucket
the bleed is entirely in the coin-flip zone (0.50–0.70 wins ~51–64%, pays the fee on top); only 0.70+
is positive, and there we under-allocated. **Conclusion: directional alpha on the random walk is
zero-to-negative.** `DIRECTIONAL_TAKER_ENABLED=False`.

### 1.3 The box exit was selection bias, not edge
Historically the system was net-positive only because the hedge-to-box exit harvested favorable drift
(entered ~0.65, hedged out ~0.21 for ~+$4) while adverse drift rode to a full −$26. We locked winners
and let losers run. `PAPER_FILL_REALISM=True` stresses whether that 21¢ hedge is real in live depth.
Any new mechanical loss-cut must **beat the hold baseline in backtest first** (prior cut variants lost
vs hold; the existing `BOX_STOP_MARGIN_LOSS=0.10` is the incumbent).

### 1.4 Reward farm + arb are dead on 5-min
- **FARM:** CLOB `getMarketInfo` (2026-06-21) → `rewards = {rates: None, min_size: 50, max_spread: 4.5}`.
  The pool is **unfunded**; quoting earns ~$0. Dropped from the 5-min thesis (only EVENT markets with a
  funded `rates` pool pay). The leg self-skips on a null pool.
- **ARB:** +$29 over 34 trades historically — statistically nil. Left enabled (risk-free) but not a
  pillar.

---

## 2. What the winners actually do (research + live tape)

Pulled from the live `data-api` trade tape of a closed BTC 5-min window + public write-ups; all sources
converge:

- Winners fire in the **last 10–30s**, when direction is "largely locked in," buying **favorites at
  $0.78–0.96 in $100–275 clips** (observed: repeated 253-share BUY-favorite at 0.94/0.96).
- Their **dominant signal is "Window Delta"** — is spot up/down vs the window open, weighted by move
  magnitude — not a fancy vol model.
- The $313→$414K / 98%-win bot does **not** predict direction: it "enters when actual probability
  reaches ~85% but Polymarket still shows 50/50," living in the **0.80–0.95 zone** — exploiting the lag
  between a confirmed CEX/oracle move and a stale CLOB book.
- Durable, infra-light money is **rebate farming** (Group C, PnL≈0 on huge volume) and **arb** (Group
  B) — never directional conviction. (But farming needs a funded pool, which 5-min markets lack — §1.4.)

**The gap we were in:** small flat $25 bets in the 45–220s mid-zone with the validated leg disabled,
$0 rebates, and frequent missed strikes. The winners concentrate **size** in the **late favorite zone**
gated on **confirmed spot-vs-book lag**.

---

## 3. The validated edge: late-window feed-lag certainty

The certainty/feed-lag gate buys the confident side only when the book still underprices it. On the
recovered DB (realistic +1-tick fill) it is **the only leg with a genuine out-of-sample edge** — and
the edge is **concentrated in the last 10–45s**:

| Firing zone | n | win % | PF | EV/trade |
|---|---|---|---|---|
| 45–220s (old config) | 3561 | 81.5% | 1.14 | $0.68 |
| **10–45s (late slice)** | 367 | 86.4% | **1.59** | **$2.03** |
| **10–45s + move ≥ 5bp** | 324 | 89.2% | **1.91** | **$2.51** |
| **10–45s + move ≥ 10bp** | 137 | 92.0% | **2.57** | **$3.18** |

Extending the firing zone to T-10s is OOS-stable (chronological 70/30 TEST: +$719 → **+$814**, PF
1.24 → **1.27**). The window-delta move gate is the winners' dominant signal and monotonically raises
PF. (Reproduce: `python3 backtest.py --db <recovered.db> --certainty`; grid probe
`sy/cert_zone_experiment.py`.)

**Why this is allowed when "last-second sniping" is forbidden:** we are not racing to *react to a new
move* (we lose that to co-located bots). We buy a favorite whose ask the book left **stale** long
enough to still be there in our 1s tick. Different game, winnable on our latency.

### 3.1 What's shipped (paper-only effect, all gated)
- `CERTAINTY_ZONE_START/END = 220/10` — fire down to T-10s.
- `CERTAINTY_MIN_MOVE_BP = 5.0` — Window-Delta gate (oracle must already have moved ≥5bp from strike).
- Confidence sizing: `CERTAINTY_LATE_FROM=45`, late notional $50 vs $25 base, cap $50.
- `signal_engine.certainty_shadow()` returns `(side, ask, size)`, gates on the move, uses the late
  zone. `backtest.simulate_certainty` parametrised (zone/move) so `--certainty` mirrors live.
- `DIRECTIONAL_TAKER_ENABLED=False` — the OOS-failing bare taker no longer places orders.

The leg remains an **isolated paper shadow**: it records a `leg='CERTAINTY'` row via the live book's
depth-walk (`fill_ask` VWAP) + adverse tick, resolved against the REAL outcome — it never opens a real
position and never touches the risk guard.

### 3.2 Honest caveats (why it's still paper)
- As configured (wide 10–220 firing zone) the blended PF is **1.15 full / 1.24 OOS** — still **below
  the 1.5 live gate**. The 1.5+ is the late *slice*; confidence sizing weights toward it but does not
  by itself lift the blended figure over 1.5.
- The backtest fills at top-of-book + 1 tick. The **order-book depth-walk VWAP is unmeasured** — a $25–50
  clip is 26–60+ shares; if top-of-book size is thinner, true fills are worse. **Only a live paper run
  that walks the real book measures this.** Do not put capital behind it on the current evidence.

---

## 4. Operating plan / sequencing

1. **Run `--mode paper`** for a few days at `VOL_MULT=0.5`. The certainty leg now fires ~44% of windows
   and records depth-realistic late-zone fills — the exact data the 1.5 gate needs.
2. **Compare** the live certainty P&L vs the +$719 backtest. If the depth-walk holds (PF → 1.5),
   promote the leg to real capital with Kelly-bounded sizing. If it collapses, the edge was a
   top-of-book illusion — keep it shadow.
3. **Optionally** tighten the live firing zone to the late slice only (`CERTAINTY_ZONE_START≈45`) for a
   higher blended PF at fewer trades — decide from the paper data, not a priori.
4. **Loser-cut:** only add a new stop if it beats the `BOX_STOP_MARGIN_LOSS=0.10` hold baseline in
   backtest. Not a blind edit.
5. **P0 reliability (needs VPS logs):** the recurring `STRIKE MISSED` / `BOT DISCONNECTED` means feeds
   are dead within 3s of T=0 — almost always the process restarting at window opens. The MISSED reason
   is now logged at `WARNING` (+`oracle.connected`); check whether `"Bot started"` repeats. A voided
   strike blocks **every** leg.

**Do NOT** re-enable the bare directional taker, quote one-sided for "edge" (adverse selection), farm
the unfunded 5-min pool, or trust numbers off a `cp`-corrupted WAL DB.

---

## 5. Changelog (condensed)

- **2026-06-21** — Late-zone discovery: certainty edge concentrated in the last 10–45s (PF 1.59–2.57,
  clears the 1.5 gate; mid-window only 1.14). Shipped zone→10s + move≥5bp gate + late sizing.
  Disabled the bare directional taker (`DIRECTIONAL_TAKER_ENABLED=False`). Resolved rewards
  contradiction: 5-min `rewards.rates=null` → farm dropped. MISSED log → WARNING for VPS diagnosis.
- **2026-06-19** — Recovered the `cp`-corrupted DB via `sqlite3 .recover` (ticks 2.28M, signals 2.28M).
  Recalibrated `VOL_MULT 0.7 → 0.5` (best Brier on 8,044 windows). Proved the bare taker fails OOS and
  the certainty gate passes OOS (+$946, later +$719 under realistic fills). Wired certainty as an
  isolated paper shadow.
- **2026-06-15** — Added `PAPER_FILL_REALISM` (depth-walk VWAP + adverse tick) to stress the box exit.
- **2026-06-11** — Multi-asset (BTC/ETH/SOL) AssetWorkers; asset-aware schema (PK asset+start_ts).
- **2026-06-06** — Foundation rebuild: oracle/settlement, deterministic discovery, vol-barrier model +
  EV gating, recorder + backtester, clock-driven strike snapshot.

---

## Sources
- [How BTC 5-Min Scalpers Work (Mountain Movers, Medium)](https://medium.com/mountain-movers/how-btc-5-minute-scalpers-actually-work-on-polymarket-building-the-bot-that-trades-stale-order-a16e84eb3140)
- [Profitable 5-min bot guide (Benjamin Cup, Substack)](https://benjamincup.substack.com/p/the-ultimate-guide-to-building-a)
- [Arbitrage bots dominate Polymarket (Yahoo Finance)](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- Internal: `LEADERBOARD_ANALYSIS.md`; memories `late-zone-certainty-edge`, `farm-rewards-null-on-5min`,
  `certainty-feedlag-gate-validated`, `calibration-inversion-and-box-selection`.
