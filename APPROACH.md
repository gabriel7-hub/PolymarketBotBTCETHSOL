# APPROACH.md — Diagnosis & Redesign (2026-06-19)

Based on an audit of `bot_state.db` (5 days, 1,829 positions, 2026-06-15 → 06-19) plus
external research on what actually wins these markets. This supersedes the directional
thesis as the operating plan; see `CLAUDE.md` for architecture and `LEADERBOARD_ANALYSIS.md`
for wallet evidence.

---

## 0. Progress log

- **2026-06-19 — Audit complete.** DB analyzed, three flaws identified (below), research done,
  redesign drafted (§3).
- **2026-06-19 — Step ③ (repair recorder DB) DONE.** `/Users/chandreshj/Downloads/bot_state.db`
  had b-tree corruption isolated to the `ticks` table (rootpage 7 — invalid page pointers, the
  signature of copying a live-WAL DB, exactly what `CLAUDE.md` warns against). It cascaded to make
  `ticks` and `signals` unreadable, which is why calibration couldn't run.
  - Recovered with `sqlite3 .recover` into a fresh DB (50s, no errors, no `lost_and_found` →
    nothing orphaned). `PRAGMA integrity_check` → **ok**.
  - Restored: **`ticks` 2,280,455** and **`signals` 2,280,456** rows (were 0/unreadable);
    `outcomes` 8,054, `positions` 1,829, `trades` 2,515 intact.
  - Corrupt original **quarantined** (preserved, not deleted) as `bot_state.db.corrupt.<ts>`;
    clean recovery promoted to `bot_state.db`.
  - **Calibration-ready:** REAL (Chainlink) resolved windows = **BTC 3,455 / ETH 2,300 /
    SOL 2,297** — all far above the ≥300-window Paper→Live gate. Backtest reads it via
    `python3 backtest.py --db /Users/chandreshj/Downloads/bot_state.db [--sweep|--validate|--asset X --buckets]`.
  - ⚠ This is recovered *historical* data (pre-redesign) — good for diagnosing/validating the
    new gate, but fresh post-change recording is still what proves the new thesis.
- **2026-06-19 — Calibration backtest run on recovered DB (8,044 REAL-resolved windows).**
  Results in `Downloads/backtest_out.log`. Summary in §1.5 below. Confirms the diagnosis with
  out-of-sample rigor: model is *good signal but over-dispersed*; directional taker leg *fails
  out-of-sample*. Recalibration (VOL_MULT 0.7→0.5) is a clear win; directional prediction alone
  is still not an edge.
- **2026-06-19 — Recalibration shipped (config.py).** `VOL_MULT 0.7 → 0.5` (best Brier on all
  three assets over 8,044 windows; old 0.7 was tuned on 258). Calibration fix only — the bare
  taker still fails OOS at 0.5, so this is the *prerequisite* for the certainty gate, not a
  profit fix on its own. Single consumption point: `signal_engine.py:116`. Config loads clean.
- **2026-06-19 — §3① certainty/feed-lag gate built as a backtest probe (not live).** Added
  `simulate_certainty()` + `--certainty` to `backtest.py` (measurement only — cannot place an
  order). Result (recovered DB, 8,044 windows) in §1.6 below: **it is the first leg that passes
  out-of-sample** — OOS test net **+$946**, 82.5% win, PF 1.31, +$1.38/trade, positive on all
  three assets and across the whole floor×lag sweep. Caveats: PF still **< the 1.5 live gate**,
  and the sim fills at the displayed ask (optimistic — not yet stressed with `PAPER_FILL_REALISM`
  depth-walk/slippage).
- **2026-06-19 — Realistic-fill stress DONE** (`simulate_certainty(slippage=…)`): +1 adverse
  tick erodes ~32%; OOS test still **+$719 / PF 1.24**, positive everywhere, but below the 1.5
  live gate and depth-walk still unmeasured (top-of-book ticks only). See §1.6.
- **2026-06-19 — Certainty gate wired as an ISOLATED PAPER SHADOW** (your call). Mirrors the
  `LATE_MOMENTUM` pattern but is a *separate* `if` in the main loop (not the elif dispatch) so it
  can never preempt/be preempted by the real legs. `config.py` CERTAINTY_SHADOW_* block (default
  ON, paper-only effect); `SignalEngine.certainty_shadow()` reads the already-computed signal;
  `main` walks the **live book VWAP + adverse tick** (`book.fill_ask`, verified non-mutating) to
  get the **depth-realistic fill** the backtest couldn't model, records `leg='CERTAINTY'` rows,
  resolves them in `_resolve_cert_shadow` against the REAL outcome, and reports a session tally on
  the dashboard. Hard-gated to paper — cannot place a live order. Compiles, imports, and 7/7 gate
  unit-tests pass.
- **2026-06-20 — Fixed CERTAINTY rows hanging OPEN.** Root cause: the real taker leg queues its
  window for resolution (`self._pending[start_ts]=…`) but the certainty shadow didn't — so when a
  cert bet was the only activity in a window (no real taker) and the 1s loop missed the close tick,
  the window never resolved and the row hung OPEN (it fires at t-45..220s, far from close; LATE_MOM
  escapes this firing at t-12..25s). Fix: (1) cert-open now sets `_pending` like the taker; (2) new
  `state.get_open_shadow_trades()` + `AssetWorker._reconcile_shadow_trades()` settles rows orphaned
  across a restart (in-memory tracker is lost on restart) against the resolved outcome on startup.
  Verified on a temp DB (resolved→settled WIN/LOSS, unresolved→left OPEN). Deploy + restart heals
  the currently-stuck BTC 08:00 row automatically.
- **2026-06-20 — Stopped the live bleed: `MIN_TAKER_ENTRY 0.50 → 0.72`.** Diagnosis from the
  live dashboard: session −$52 was 100% the legacy directional TAKER leg firing at ask 0.57/0.60
  (the coin-flip zone, both LOSS); the CERTAINTY shadow won (SOL UP@0.80 +$5.97) but is shadow-only
  so it doesn't offset. The old 0.50 floor's justification is overturned by the 8,044-window buckets
  (edge only ≥0.70). 0.72 confines the live taker to the favorite zone. Stopgap — the bare taker is
  still not OOS-clean; the real fix is promoting the certainty gate once depth-realistic paper fills
  pass. (Also seen: `BOT DISCONNECTED` — bot was down; the startup reconcile heals the orphan on
  restart.)
- **2026-06-20 — Fixed STRIKE-MISSED bogus trades + BOT-DISCONNECTED stall (linked bugs).**
  Symptom: CERTAINTY trades firing at ask 0.18/0.33/0.34 (not "certain"), and frequent
  `BOT DISCONNECTED` / `STRIKE MISSED`. Root causes: (1) `certainty_shadow` didn't gate on
  `window.has_reference` (the real taker/late-mom do) — with no strike, the model prices vs ref=0
  and returns garbage p≈0.99, firing the gate on cheap asks. (2) Pass-1 resolution called
  `fetch_resolution` (HTTP) for EVERY closed pending window EVERY cycle, unbounded; a missed-strike
  window (ref=0) can't fallback-settle, so it hammered HTTP for 900s and stalled the 1s loop →
  state push starves → dashboard flips to BOT DISCONNECTED → strike thread misses more (cascade).
  Fixes: (1) gate certainty on `window.has_reference`; (2) cap Pass-1 fetches at
  `RESOLUTION_MAX_FETCH_PER_CYCLE` (valid-strike windows still fallback-settle when over budget);
  (3) don't add STRIKE-MISSED windows to `_pending` at all (nothing to settle; ticks excluded from
  calibration anyway); (4) startup reconcile now VOIDs stale OPEN certainty orphans. All compile;
  has_reference gate verified.
- **2026-06-20 — BOT-DISCONNECTED, server-side hardening.** Found two concrete fixable causes:
  (1) the WS handler dropped the client on ANY transient state-build/serialize error (now isolated
  per-iteration — only a real transport error ends the loop); (2) `index.html` served with no cache
  headers, so a normal browser refresh kept running the OLD (latching) dashboard JS — added
  `Cache-Control: no-cache`. Also wrapped the startup shadow-reconcile so a bad row can't crash
  worker construction (a crash there would restart-loop the bot → miss strikes). **Open root:** the
  recurring **STRIKE MISSED** in every screenshot means `oracle.price` is 0 within 3s of T=0 — i.e.
  feeds dead at the boundary, which almost always means the BOT PROCESS is restarting (or feeds
  dropping) at window opens. Historical recovered DB captured strikes fine, so this is a NEW runtime
  issue needing the VPS logs to pin (is `"Bot started"` repeating? what's the MISSED reason line?).
- **NEXT:** (a) run paper (`python3 main.py --mode paper`) for a few days at `VOL_MULT=0.5` to
  accumulate live depth-realistic `leg='CERTAINTY'` rows; (b) compare that live shadow P&L vs the
  backtest's +$719 OOS — if the depth-walk doesn't kill it (PF holds, ideally → 1.5), promote to a
  real-capital leg; if it does, the edge was a top-of-book illusion. §3② loser-cut: box LOSS-side
  stop already exists (`BOX_STOP_MARGIN_LOSS=0.10`) and prior mechanical cut variants LOST vs hold
  — any new cut must beat that baseline in backtest first.

---

## 1.5 Calibration backtest results (recovered DB, 8,044 windows)

`python3 backtest.py --db Downloads/bot_state.db --sweep / --validate / --asset X --buckets`

**(a) Model has real signal but is systematically OVER-DISPERSED (under-confident).**
Brier **0.155–0.163** across all assets — well under the 0.25 coin-flip / Paper→Live gate,
and monotonic. But the calibration table shows empirical outcomes are *more extreme than the
model predicts* on both tails — the model pulls everything toward 0.5:

| Model says P(Up) | Reality (emp UP freq) BTC / ETH / SOL |
|---|---|
| 0.15 | 0.11 / 0.09 / 0.11 |
| 0.65 | 0.72 / 0.74 / 0.71 |
| 0.75 | 0.81 / 0.83 / 0.81 |
| 0.85 | 0.89 / 0.90 / 0.90 |

→ **σ is ~too wide.** Best `vol_mult = 0.5` on **all three** assets (live config uses
**0.7** — too dispersed). Shrinking σ is a clear, free calibration win and makes the model
correctly flag genuine high-certainty states (the input the §3 certainty/feed-lag gate needs).

**(b) The directional EV-gated taker leg FAILS out-of-sample (the decisive result).**
This sim is the *pure directional taker with no box exit* — buy at ask, hold to resolution:

| | trades | win % | net P&L | PF | EV/trade |
|---|---|---|---|---|---|
| Combined (vol_mult 0.5) | 7,982 | 49.9% | **−$1,733** | 0.98 | −$0.22 |
| BTC | 3,425 | 50.9% | −$1,925 | 0.96 | −$0.56 |
| ETH | 2,275 | 47.7% | +$1,918 | 1.06 | +$0.84 |
| SOL | 2,282 | 50.5% | −$1,726 | 0.94 | −$0.76 |

Out-of-sample validation (chronological 70/30 split): train picks `vol_mult=0.5, min_ev=0.05`
(train +$1,124, PF 1.02) → **TEST net −$1,140, PF 0.97 → FAILS.** Current live `0.7/0.03` on
TEST: **−$5,434, PF 0.87.** PF never reaches the ≥1.5 gate; mostly below 1.0.

**Conclusion:** the model is *recalibratable and informative*, but a naive EV-gated directional
taker has **no out-of-sample edge** even at best calibration. This is the rigorous confirmation
of §1 Flaws A & B — the only reason the live ledger is positive is the box exit, not directional
prediction. So the plan stands: **recalibrate σ (VOL_MULT→0.5), then use the model to identify
genuine certainty / feed-lag states with box discipline — do not run the bare EV taker.**

*(Note: `--buckets` win% mixes boxed exits into the denominator, so its per-bucket "BLEED"
verdict understates the resolved-only win rates in §1 Flaw A; both agree the bare directional
hold is sub-breakeven.)*

---

## 1.6 Certainty / feed-lag gate — first leg that passes out-of-sample

`python3 backtest.py --db Downloads/bot_state.db --certainty`
(`simulate_certainty()` — buy a side only when recalibrated `p_side ≥ floor` AND `ask ≤ p_side −
lag_margin` AND `ask ≤ max_ask`; defaults floor 0.80, lag 0.03, max_ask 0.97; flat stake, one
trade/window. **Measurement only — not wired to the live loop.**)

| | trades | win % | net P&L | PF | EV/trade | max DD |
|---|---|---|---|---|---|---|
| Full sample (vol_mult 0.5) | 3,561 | 81.5% | **+$3,571** | 1.21 | +$1.00 | −$544 |
| BTC | 1,507 | 80.6% | +$1,661 | 1.22 | | |
| ETH | 846 | 81.7% | +$803 | 1.20 | | |
| SOL | 1,208 | 82.4% | +$1,106 | 1.20 | | |

**Honest OOS (chronological 70/30, SAME fixed rule on test — no param search):**
train net +$2,625 (PF 1.19) → **TEST net +$946, 82.5% win, PF 1.31, +$1.38/trade.** Contrast the
bare directional taker (§1.5b), which *failed* OOS at −$1,140. The sensitivity sweep is **net-positive
in all 12 floor×lag cells** (PF 1.13–1.22; EV/trade best ≈ floor 0.80 / lag 0.05 at +$1.08) — a broad
plateau, not a lucky corner.

**Reading it honestly:**
- This is the **first leg with a genuine out-of-sample edge** — it concentrates on the favorite /
  feed-lag zone the winners trade and skips the coin-flip zone by construction (`p_up+p_down=1`
  ⇒ only one side clears floor ≥ 0.5).
- **Not yet live-ready:** PF 1.21–1.31 is below the **1.5 live gate**, and the sim fills at the
  *displayed ask* (optimistic). Next gate before any live wiring: re-run with `PAPER_FILL_REALISM`
  depth-walk + adverse-tick slippage; if PF survives, promote to a paper-mode live leg.

**Realistic-fill stress (2026-06-19, `simulate_certainty(slippage=…)`):** decide on the displayed
ask, fill **+1 adverse tick (+$0.01/share)** worse — the latency half of `PAPER_FILL_REALISM`
(the VWAP depth-walk can't be reproduced: recorded ticks store top-of-book only).

| floor 0.80 / lag 0.03 | trades | win % | net | PF | EV/trade |
|---|---|---|---|---|---|
| Full IDEAL | 3,561 | 81.5% | +$3,571 | 1.21 | +$1.00 |
| Full REALISTIC | 3,561 | 81.5% | **+$2,414** | 1.14 | +$0.68 |
| **OOS test REALISTIC** | 686 | 82.5% | **+$719** | 1.24 | +$1.05 |

- One tick costs ~$1,156 (≈32% of net). Edge **still positive everywhere** — all 3 assets, all 12
  sweep cells (PF 1.05–1.16) — and **OOS-positive (+$719)**. But PF **1.14 (full) / 1.24 (OOS)**
  is **below the 1.5 live-capital gate.**
- **Unmeasured downside:** the depth-walk VWAP is *not* in these numbers. In the certainty zone
  asks are 0.80–0.97; a $25 clip is ~26–31 shares, so if top-of-book size < that, true fills are
  worse than modelled here. **Only a paper-mode live run (which walks the real book) can measure
  it.** ⇒ The edge is real but thin; do not put live capital behind it on this evidence.

---

## 1. What the DB says — where the system is failing

**Headline:** net **+$2,641** over 5 days. That number hides three structural problems.

### Flaw A — the directional model is *inverse-calibrated* (the core failure)

Resolved directional trades, win rate vs. price paid:

| Entry bucket | n | Avg paid | Realized win % | Net P&L | EV / trade |
|---|---|---|---|---|---|
| **0.50–0.60** | 489 | .547 | **50.9%** | **−$1,269** | **−$2.60** |
| **0.60–0.70** | 282 | .647 | 63.8% | **−$306** | −$1.08 |
| 0.70–0.80 | 201 | .746 | 85.1% | +$620 | +$3.09 |
| 0.80+ | 205 | .860 | 95.6% | +$539 | +$2.63 |

- We pay **55¢ for coin flips that win 50.9%** — that one bucket loses more than the whole
  directional book makes. The model believes it has edge near 50¢; it has none (the market
  is correctly priced) and we pay the fee on top.
- The 0.80+ favorites win **more** than we pay (95.6% at 86¢) yet we **under-allocate** —
  only 205 flat-$25 trades.
- Net directional leg: **BTC −$260, ETH −$311, SOL +$155 → −$416 total.** Two losing
  buckets (−$1,575) nearly cancel the two winning buckets (+$1,159). **Directional alpha
  is zero-to-negative.**

### Flaw B — all profit is box *selection bias*, not edge

The box exit is **+$3,028** (the only thing keeping the system positive). Mechanic:
boxed positions entered ~0.65, hedged out ~0.21 on the opposite side for ~+$4 each —
**only the positions that drifted in our favor.** The 381 that drifted against us rode to
a full **−$26 loss**. We lock winners and let losers run to zero. It looks profitable only
because favorable drift is harvested while adverse drift is not symmetrically cut. Fragile —
`PAPER_FILL_REALISM` exists precisely because that 21¢ hedge may not be there in real depth.

### Flaw C — two of three legs are dead, and the calibration data is corrupt

- **FARM leg: 0 trades, ever.** Rebates = **$0** every day. The "reward yield" pillar
  produces nothing.
- **ARB leg: +$29 over 34 trades.** Statistically nil.
- **`ticks` and `signals` tables are corrupt** (`PRAGMA quick_check` fails — Tree 7, invalid
  page numbers). The recorder that feeds `backtest.py --sweep` — the exact Paper→Live gate —
  cannot be run off this DB.

---

## 2. What the winners actually do (research)

Every credible source converges, and it is the opposite of our volume distribution:

- **The $313 → $414K / 98%-win-rate bot** (BTC/ETH/SOL short windows, $4–5K clips) does
  **not** predict direction. It *"enters when actual probability reaches ~85% but Polymarket
  still shows 50/50, capturing mispriced certainty through thousands of micro-trades."* It
  lives **entirely in the 0.80–0.95 near-certain zone**, exploiting the lag between confirmed
  CEX/oracle spot moves and a stale CLOB book.
- **Arbitrage bots pulled ~$40M/yr** via structural lag + buy-both-sides-under-$1;
  **73% of arb profit goes to sub-100ms execution.**
- Build guides gate on **>3–10% edge after fees** and explicitly **avoid the coin-flip zone**
  — the zone where *we* do most of our trading.
- Our own `LEADERBOARD_ANALYSIS.md`: durable, infra-light money is **Group B (MM/arb,
  ~1% margin × huge churn)** and **Group C (pure rebate farming, PnL ≈ 0)** — never
  directional conviction on a random walk.

**The gap:** winners concentrate size in the favorite / feed-lag zone we treat as a minor
bucket, and they farm rebates we collect **$0** of. We spread flat $25 across a coin-flip
zone with no edge.

---

## 3. The better approach

**Thesis shift: stop trying to predict the 5-minute random walk. Trade only confirmed
mispriced certainty + harvest rebates.** Three changes, in priority order.

### ① Kill the coin-flip zone; concentrate on feed-lag certainty
- Raise `MIN_TAKER_ENTRY` from **0.50 → 0.78** (only the validated +EV band 0.78–0.97).
  Cap entries near ~0.97 (no edge buying 99¢ favorites after fees).
- **Re-gate the trigger on spot-vs-book lag, not just the barrier model:** fire only when
  the CEX/Chainlink oracle has *already moved* enough to put true P(win) materially above the
  displayed ask (the "true 85% / book still shows less" condition). This is the winners' real
  edge and is buildable from feeds we already run (`oracle_feed.py` + `polymarket_book.py`).
- **Size up in this zone** instead of flat $25 — Kelly-scaled within `MAX_STAKE_PER_MARKET`,
  since this is where EV is positive and variance is low. (Stake scaling was rejected before,
  but that test scaled the *losing* zone; scale only the validated 0.78+ feed-lag entries.)

### ② Cut losers symmetrically so the box edge is real, not selection
Add a stop: if an open directional position drifts against us past a threshold, exit at
market (eat the small loss) instead of riding to −$26. This converts the box from
"harvest winners, eat full losers" into a genuine two-sided exit policy. Validate on
`PAPER_FILL_REALISM=True` data.

### ③ Fix the dead infrastructure before trusting any of the above
- **Repair the recording DB.** The corrupt `ticks`/`signals` mean we are blind on
  calibration. Quarantine the corrupt file, start a clean recorder, and do **not** re-validate
  off this DB.
- **Make the FARM leg place real orders or drop it from the thesis.** It has produced $0 and
  rebates are $0. If we want Group-C yield (the most durable edge per research), it needs real
  two-sided order management, not a paper estimate.

### Sequencing
1. **③ clean data** — repair recorder, accumulate fresh `ticks`+`outcomes`.
2. Re-run `backtest.py --asset {BTC,ETH,SOL}` to confirm the 0.78+ feed-lag gate is +EV
   out-of-sample (per asset; vol dynamics differ).
3. Implement **①** and **②**.
4. Only then revisit live. **Do not tune off the current corrupt DB.**

**One-line version:** we are a mediocre directional predictor surviving on a fragile exit
trick; the winners are feed-lag certainty-buyers plus rebate farmers. Move our volume into
the favorite zone, gate it on real spot-book lag, cut losers, and either fix or drop the
farm/rebate leg.

---

## Sources
- [Polymarket 5-min edges (Medium)](https://medium.com/@benjamin.bigdev/unlocking-edges-in-polymarkets-5-minute-crypto-markets-last-second-dynamics-bot-strategies-and-db8efcb5c196)
- [Profitable 5-min bot guide (Substack)](https://benjamincup.substack.com/p/the-ultimate-guide-to-building-a)
- [Arbitrage bots dominate Polymarket (Yahoo Finance)](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- [Prediction markets bot playground (Finance Magnates)](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)
- [Copy-trading profitable wallets (QuantVPS)](https://www.quantvps.com/blog/polymarket-copy-trading-bot)
- Internal: `LEADERBOARD_ANALYSIS.md`, memory `calibration-inversion-and-box-selection`
