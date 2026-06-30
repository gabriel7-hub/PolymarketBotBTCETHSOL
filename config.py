"""
config.py — All tunable constants for the crypto 5-min Polymarket bot (BTC/ETH/SOL).
Edit these to change risk parameters, edge thresholds, and model weights.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Authentication ────────────────────────────────────────────────────────────
PRIVATE_KEY          = os.getenv("PRIVATE_KEY", "")
CLOB_API_KEY         = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET      = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE  = os.getenv("CLOB_API_PASSPHRASE", "")
WALLET_ADDRESS       = os.getenv("WALLET_ADDRESS", "")
# CLOB order signature type: 0=EOA, 1=POLY_PROXY (Magic/email proxy),
# 2=POLY_GNOSIS_SAFE (browser wallet), 3=POLY_1271 (EIP-1271 smart-contract wallet — the
# new Polymarket Gmail/Magic account that holds pUSD). Verified 2026-06-27: this account is
# type 3 with funder=WALLET_ADDRESS (0x77ef…), $200.53 pUSD, allowances already set.
SIGNATURE_TYPE       = int(os.getenv("SIGNATURE_TYPE", "3"))

# ─── API Endpoints ─────────────────────────────────────────────────────────────
CLOB_HOST            = "https://clob.polymarket.com"
GAMMA_API            = "https://gamma-api.polymarket.com"
BINANCE_WS_BASE      = "wss://stream.binance.com:9443/ws"      # /<symbol>@aggTrade
COINBASE_WS_URL      = "wss://ws-feed.exchange.coinbase.com"   # Chainlink-proxy venue
# Polymarket Real-Time Data Socket — the ACTUAL Chainlink data-stream price that settles
# these markets (the "Price to Beat"). No auth. Using this for price + strike makes our
# Strike(ref) exactly equal Polymarket's published Price to Beat (zero proxy basis).
CHAINLINK_RTDS_URL   = "wss://ws-live-data.polymarket.com"
# The RTDS Chainlink feed is sparse (heartbeat/deviation-driven), so use its price only
# when it ticked within this many seconds; otherwise fall back to the high-frequency
# Coinbase proxy so the strike is always captured reliably at T=0.
CHAINLINK_MAX_STALE  = 8
POLYMARKET_BOOK_WS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polymarket's RTDS WebSocket now sits behind Cloudflare bot protection and silently drops
# a plain client handshake — so we send browser-like headers on connect. (Verified 2026-06-21:
# a no-header handshake times out; the dashboard CHAINLINK field went blank and every strike
# fell back to the CEX proxy, which carries a ~4–5bp basis vs the real Price to Beat.)
RTDS_WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Origin: https://polymarket.com",
]

# ─── On-chain Chainlink fallback (strike anchor when RTDS is unreachable) ──────────
# Polymarket settles on Chainlink Data Streams (the RTDS feed above). If that socket is down,
# the on-chain Chainlink aggregator on Polygon is a far better strike anchor than the CEX proxy
# (verified 2026-06-21: on-chain BTC/USD $64,079.81 vs real Price to Beat $64,083.33 ≈ 0.5bp,
# vs the proxy's ~4.5bp). It updates on a heartbeat (~13–40s), so it is used only as a fallback.
POLYGON_RPC          = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
# Fallback list tried in order (the default public RPC is sometimes gated):
CHAINLINK_RPC_URLS = [u for u in [os.getenv("POLYGON_RPC", "")] if u] + [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
]
CHAINLINK_ONCHAIN_ENABLED   = True
CHAINLINK_ONCHAIN_POLL_SECS = 12     # heartbeat poll cadence
CHAINLINK_ONCHAIN_MAX_STALE = 90     # accept an on-chain price only if updatedAt within this

# ─── Market Target (multi-asset) ───────────────────────────────────────────────
# 5-min markets are Gamma *events* with slug  "<asset>-updown-5m-<start_unix_ts>",
# where start_ts is always a unix multiple of MARKET_WINDOW_SECS. We construct the
# current window's slug directly from the clock instead of scanning the API.
# All assets verified live on Gamma 2026-06-11: same schema, tick=0.01, negRisk=False.
MARKET_WINDOW_SECS   = 300                    # 5 minutes

ASSET_PARAMS = {
    # chainlink_agg = Chainlink price-feed aggregator on Polygon (on-chain strike fallback).
    "BTC": {"name": "Bitcoin",  "binance_symbol": "btcusdt",
            "coinbase_product": "BTC-USD", "chainlink_symbol": "btc/usd",
            "chainlink_agg": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
            "slug_prefix": "btc-updown-5m-", "title_pattern": "Bitcoin Up or Down"},
    "ETH": {"name": "Ethereum", "binance_symbol": "ethusdt",
            "coinbase_product": "ETH-USD", "chainlink_symbol": "eth/usd",
            "chainlink_agg": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
            "slug_prefix": "eth-updown-5m-", "title_pattern": "Ethereum Up or Down"},
    "SOL": {"name": "Solana",   "binance_symbol": "solusdt",
            "coinbase_product": "SOL-USD", "chainlink_symbol": "sol/usd",
            "chainlink_agg": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
            "slug_prefix": "sol-updown-5m-", "title_pattern": "Solana Up or Down"},
    # XRP (xrp-updown-5m-) — active since 2026-06-22; 4th asset, paper-only like the rest.
    "XRP": {"name": "XRP",      "binance_symbol": "xrpusdt",
            "coinbase_product": "XRP-USD", "chainlink_symbol": "xrp/usd",
            "chainlink_agg": "0x785ba89291f676b5386652eB12b30cF361020694",
            "slug_prefix": "xrp-updown-5m-", "title_pattern": "XRP Up or Down"},
    # BNB (bnb-updown-5m-) — added 2026-06-29 as a RECORD-ONLY shadow asset. Resolves on the
    # Chainlink BNB/USD data stream (verified RTDS `bnb/usd` streams live), so the strike comes
    # from the RTDS feed; Binance has `bnbusdt`. NOTE: Coinbase does NOT list BNB, so the oracle
    # has no Coinbase leg — it degrades gracefully to Binance (vol/blend) and the basis goes to 0.
    # `chainlink_agg` is intentionally omitted: a WRONG on-chain aggregator would manufacture a
    # phantom strike, so the on-chain fallback self-disables and we rely on RTDS + Binance.
    # BNB is in CERTAINTY_ASSETS (the leg fires & records trades) but NOT CERTAINTY_LIVE_ASSETS
    # (never real capital) and is listed in PNL_EXCLUDED_ASSETS (kept out of Session/Total P&L).
    "BNB": {"name": "BNB",      "binance_symbol": "bnbusdt",
            "coinbase_product": "BNB-USD", "chainlink_symbol": "bnb/usd",
            "slug_prefix": "bnb-updown-5m-", "title_pattern": "BNB Up or Down"},
}

# Assets traded this session (env override: ASSETS=BTC,ETH,SOL,XRP,BNB).
ASSETS = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL,XRP,BNB").split(",")
          if a.strip().upper() in ASSET_PARAMS]

# Record-only shadow assets: the certainty leg fires & logs their trades (visible on their own
# dashboard tab), but their P&L is EXCLUDED from Session/Total P&L (see state._pnl_from_ledgers).
# Use this for an asset you want to observe without it moving the headline P&L. BNB joined
# 2026-06-29 this way — paper-only data gathering, never counted toward the bankroll.
PNL_EXCLUDED_ASSETS = tuple(
    a.strip().upper() for a in os.getenv("PNL_EXCLUDED_ASSETS", "BNB").split(",")
    if a.strip().upper() in ASSET_PARAMS)

# ─── Risk Limits ───────────────────────────────────────────────────────────────
MAX_STAKE_PER_MARKET = 25.0    # USDC — max single position size (per asset-window)
MAX_DAILY_LOSS       = 50.0    # USDC — hard halt, GLOBAL across all assets
MAX_OPEN_POSITIONS   = 1       # never hold more than 1 position at once PER ASSET
POST_LOSS_COOLDOWN   = 0       # windows to skip after a loss (0 = cooldown disabled)
MAX_CONSECUTIVE_LOSSES = 0     # halt after this many losses in a row (0 = disabled)

# ─── EV Thresholds (replace static edge thresholds — see pricing.py) ───────────
# We trade on fee-net expected value per share, NOT a flat cent edge. The taker fee
# C·0.07·p·(1−p) is largest at 50¢ and tiny near the extremes, so a flat threshold
# over-trades the coin-flip zone and under-trades confident edges.
MIN_EV_TAKER         = 0.03    # min fee-net EV per share ($) to fire an IOC taker. Raised from
                               # 0.015 after out-of-sample validation: 0.03 cleared the coin-flip
                               # fee-peak zone and gave the best held-out P&L at vol_mult=0.7.
MIN_EV_MAKER         = 0.005   # min EV per share ($) for a rebate-farm maker quote
MIN_TAKER_ENTRY      = 0.72    # never IOC a side whose ask is below this. RAISED 0.50→0.72
                               # 2026-06-20: the old 0.50 floor (from 684-window VPS data that
                               # claimed "the edge lives at 0.50-0.65") is OVERTURNED by the
                               # 8,044-window recovered backtest. By resolved entry-price bucket:
                               #   0.50-0.60  win 50.9%  net −$1,269   (paying 55¢ for coin flips)
                               #   0.60-0.70  win 63.8%  net −$306
                               #   0.70-0.80  win 85.1%  net +$620  (+$3.09/trade)  ← edge starts
                               #   0.80+      win 95.6%  net +$539  (+$2.63/trade)
                               # The coin-flip zone (<0.70) is where ALL the bleed is and where the
                               # directional taker fails out-of-sample. 0.72 confines the live taker
                               # to the favorite zone — the same near-certain region the validated
                               # certainty/feed-lag gate trades (APPROACH.md §1.6). NOTE: this stops
                               # the bleed but the bare taker still isn't OOS-clean even here; the
                               # real fix is promoting the certainty shadow leg once it survives
                               # depth-realistic paper fills. Revisit with `backtest.py --buckets`.

# Master switch for the BARE directional EV-gated taker (Action.IOC_*). The recovered
# 8,044-window backtest proved this leg FAILS out-of-sample even at best calibration
# (TEST −$1,140, PF 0.97 — APPROACH.md §1.5b) and it is the live bleed source (the
# 2026-06-20 −$52 session was 100% this leg). The validated directional edge is the
# certainty/feed-lag leg below, not this one. OFF by default: do not place real
# directional orders on a leg with no OOS edge. (Set True only to reproduce the old
# behaviour for comparison.)
DIRECTIONAL_TAKER_ENABLED = False

BOX_STOP_ENABLED     = False   # OFF for live: the only live positions are CERTAINTY, and
                               # boxing the certainty leg is net-negative (clips small wins,
                               # can't catch late flips — see memory "certainty boxing fails").
                               # Re-enable only if the directional taker is ever turned back on.
# Box trigger: p_side < 1 − opposite_ask − margin. One margin was doing two opposing
# jobs, so it is split by what the box would lock (entry + opposite_ask vs $1):
#   LOSS side  — tight, react while the hedge is still cheap (−$10 beats −$26). Replay
#     2026-06-10 (464 pre-deploy positions): tight margins won (+$147 vs hold at 0.10).
#   PROFIT side — wide, because the model is UNDERCONFIDENT on favorites (0.50-0.65
#     bucket wins 65.5% vs 57.3% implied): at 0.10 the first 100 live trades boxed 73%
#     of positions, clipping winners early (−$44 vs hold); wide margins won on that
#     sample (0.20→+$22, 0.25→+$38 vs hold).
# Pair CROSS-VALIDATED 2026-06-10 on two independent samples: pre-deploy 474 trades
# (hold $614 → $707) and post-deploy 119 trades (hold $275 → $384, actual sym-0.10
# realized only $286). 0.10/0.20 was the max-min choice across both; wider profit
# margins only won on the post sample. Mechanical alternatives (late-window lock,
# pair-cost trailing lock) all LOST $60-170 vs hold — the late-flip full losses they
# catch are cheaper than the winners they clip. Full losses on fast gaps are
# irreducible: the EV trigger can't fire once opp_ask > 0.90 (1−c−margin ≤ 0), and
# that's correct behavior. Judge this rule by NET, not by the LOSS line.
BOX_STOP_MARGIN_LOSS   = 0.10
BOX_STOP_MARGIN_PROFIT = 0.20
MAX_SPREAD           = 0.06    # skip if order book spread is wider than this
MAX_SLIPPAGE         = 0.02    # 2¢: cancel if ask moves more than this before fill

# ─── Paper-fill realism (conservative; makes paper P&L a believable lower bound) ──
# The old paper model filled every IOC taker AND every hedge-to-box at the displayed
# best ask, full size, instantly. That is optimistic — especially the box, which locks
# profit by buying the cheap (~3¢) tail where the book is thinnest. With this enabled,
# paper fills walk the REAL displayed ask depth (VWAP) and pay an extra adverse tick for
# the 1s snapshot→order latency, so the recorded edge is one we could actually capture.
# Set False to restore the old optimistic behaviour (e.g. to A/B against prior data).
PAPER_FILL_REALISM   = True
PAPER_SLIPPAGE_TICKS = 1       # extra adverse ticks (×TICK_SIZE) applied to every paper fill
BOX_MAX_FILL_SLIPPAGE = 0.02   # if hedging the full box costs more than opp_ask + this (VWAP
                               # slippage through thin depth), DON'T box — let the position ride
                               # to natural resolution. Tests whether the box edge survives real
                               # liquidity instead of assuming a free lock at the touch.

# ─── Strategy Timing ───────────────────────────────────────────────────────────
# Taker fires only in the mid-window zone where 1s loop latency is tolerable.
# Last-second sniping is intentionally NOT attempted — we cannot win that latency race.
TAKER_ZONE_START     = 220     # seconds remaining: taker mode may start at/below this
TAKER_ZONE_END       = 45      # seconds remaining: taker mode stops at/below this
MIN_SECONDS_TO_TRADE = 45      # never open a new position inside the last 45s
CANCEL_OPEN_AT       = 30      # cancel any unfilled maker quotes when ≤ this many secs remain
REBATE_FARM_UNTIL    = 60      # only run two-sided rebate quoting above this t_remaining
REFERENCE_MAX_LAG    = 3       # secs: only trust a strike snapshot taken this close to T=0;
                               # windows caught later have an unreliable strike → never traded
RESOLUTION_FALLBACK_SECS = 4   # secs after window close to wait for the REAL outcome before
                               # settling the PAPER position on our own oracle price, so the
                               # old position doesn't linger into the next window. The real
                               # Polymarket outcome (which arrives ~minutes later) is still
                               # captured in the background and upgrades the calibration record.
RESOLUTION_REAL_POLL_SECS = 15 # how often to poll for the delayed REAL outcome (calibration)
RESOLUTION_MAX_FETCH_PER_CYCLE = 4  # cap REAL-outcome HTTP calls per poll so a flaky VPS
                                    # network can't stall the main loop for long
RESOLUTION_GIVEUP_SECS   = 900 # if a window still can't be resolved this long after close
                               # (no real outcome AND no strike for fallback), cancel the
                               # position so it can't block the one-position guard forever

# ─── Probability Model (driftless random-walk barrier — calibrate via backtest.py)
# P(Up) = Φ( (S_now − ref) / (σ_price · √t_remaining) )
VOL_WINDOW_SECS      = 45      # rolling window of log-returns for realized vol estimate
MOMENTUM_WINDOW_SECS = 15      # seconds of price history for the 15s momentum diagnostic
VOL_FLOOR_PER_SEC    = 1.0e-5  # floor on per-second return vol (avoid div-by-zero / overconfidence)
VOL_MULT             = 0.5     # live σ scaling. RECALIBRATED 2026-06-19 on 8,044 REAL-resolved
                               # windows (recovered DB; prior 0.7 was tuned on only 258). The Brier
                               # sweep bottoms at vol_mult=0.5 on ALL three assets (BTC 0.163, ETH
                               # 0.158, SOL 0.155; all monotonic, well under the 0.25 gate). The
                               # calibration table showed the model was OVER-DISPERSED at 0.7 —
                               # empirical outcomes are more extreme than predicted on both tails
                               # (model said P=0.65 when reality was ~0.72), i.e. σ too wide →
                               # probabilities pulled toward 0.5. Shrinking σ fixes that and lets the
                               # model correctly flag genuine high-certainty states. NOTE: this is a
                               # CALIBRATION fix only — the bare directional EV-gated taker still
                               # FAILS out-of-sample at 0.5 (validate: TEST net −$1140, PF 0.97), so
                               # do NOT read this as "the taker is now profitable." It is the
                               # prerequisite for the certainty/feed-lag gate (APPROACH.md §3①).
                               # Re-confirm with `backtest.py --validate` as more REAL data accrues.
DRIFT_WEIGHT         = 0.0     # momentum→drift weight; 0 = pure driftless (theoretically correct)
BASIS_VOL_INFLATE    = 1.0     # how much CEX disagreement (bp) inflates σ → pulls P toward 0.5

# Safety rail (active until the model is calibrated): a taker fill requires the model to
# agree with a liquid market within this margin. A huge model-vs-market gap on a tight book
# is almost always model error, not a real mispricing — and that is exactly the pattern that
# lost money in paper (buying deep underdogs). Set to 1.0 to disable once calibrated.
MAX_MODEL_MARKET_DISAGREE = 0.20

# ─── Maker / Rebate-Farm Pricing ───────────────────────────────────────────────
ADVERSE_SELECTION_HAIRCUT = 0.02  # $/share haircut applied to resting maker EV (fills are informed)
MAKER_QUOTE_OFFSET   = 0.02    # post two-sided quotes this far either side of midpoint

# ─── Liquidity-Reward Farm (Group C on the leaderboard) ────────────────────────
# Quote BOTH sides within rewardsMaxSpread of the midpoint at >= rewardsMinSize to
# harvest the daily liquidity-rewards pool + maker rebates. Delta-neutral; the trade
# PnL is ~0 by design — return is the incentive accrual.
FARM_ENABLED         = True
FARM_SIZE_USDC       = 50.0    # per-side notional (must clear market rewardsMinSize, ~$50)
# Reward score is QUADRATIC in proximity to mid: S = ((maxspread − spread)/maxspread)².
# Quoting at the old 0.5·maxspread only scored 0.25; quoting ~1 tick off mid scores ~0.6
# (≈2× the reward for the same capital). Quote as tight as fill-risk allows.
FARM_QUOTE_TICKS     = 1       # place quotes this many ticks off mid (tighter = quadratically more reward)
FARM_MAX_SPREAD_FRAC = 0.9     # hard cap: never quote beyond this fraction of rewardsMaxSpread
FARM_EST_APR         = 0.40    # estimated reward yield on quoted notional (paper accrual only)
FARM_MIN_MID         = 0.15    # don't farm when mid is in the tails (skewed/no two-sided book)
FARM_MAX_MID         = 0.85

# ─── YES/NO Pair Arbitrage (risk-free; Group B, infra-light) ───────────────────
# P_UP + P_DOWN must equal $1.00. If up_ask + down_ask < 1 − fees, buy both for a
# guaranteed $1 payout. Infrequent on thin 5-min books but pure profit when it appears.
ARB_ENABLED          = True
MIN_ARB_EDGE         = 0.005   # min locked profit per pair ($/share) after both taker fees
ARB_SIZE_USDC        = 25.0    # notional per arb pair leg
TICK_SIZE            = "0.01"  # Polymarket CLOB tick size for these crypto markets

# ─── Late-Window Momentum (EXPERIMENTAL · paper-only · OFF by default) ──────────
# Hypothesis tested on 258 recorded windows: near expiry, when one side DECISIVELY
# leads, the book under-prices it — that side wins more often than its ask implies
# (~81–90% when ask>0.5 at T-20s). Adding a bet on the late leader nudged net
# +$615→+$664 in-sample and +$320→+$370 out-of-sample (70/30 split) — BUT on only
# ~10 OOS bets and with optimistic fills (it assumes we fill at the snapshot ask while
# the leader's price is rising). So we MEASURE it in paper first, exactly like the
# VOL_MULT fix. This is NOT a hedge / loss-recovery (blind hedging lost money); it is a
# standalone +EV directional signal. The leg is an isolated SHADOW: it writes its own
# leg='LATE_MOM' ledger rows, never opens a real position, never touches the risk guard
# or the taker leg, and is hard-gated to paper mode (cannot place a live order).
LATE_MOMENTUM_ENABLED    = False   # master switch. False = complete no-op.
LATE_MOMENTUM_THRESHOLD  = 0.62    # only bet the leader if its ask ≥ this (conservative;
                                   # the in-sample-peak was 0.65 — deliberately NOT tuned to it)
LATE_MOMENTUM_MAX_ASK    = 0.90    # don't chase near-certainties (fee makes EV ≤0 past here)
LATE_MOMENTUM_ZONE_START = 25      # secs remaining: late-momentum window opens
LATE_MOMENTUM_ZONE_END   = 12      # secs remaining: stop (stay out of the latency-dead final ~10s)
LATE_MOMENTUM_SIZE_USDC  = 25.0    # paper notional per late-momentum bet

# ─── Certainty / Feed-Lag Gate (APPROACH.md §3① · paper SHADOW · measurement) ───
# The ONLY leg with a genuine out-of-sample edge in backtest (recovered 8,044 windows,
# vol_mult=0.5): buy the side the recalibrated model is already confident in WHEN the book
# still underprices that confidence (feed lag). OOS test +$719 net / PF 1.24 even after a
# 1-tick adverse-fill stress — but PF is below the 1.5 live-capital gate, AND the backtest
# could not model the order-book depth-walk (recorded ticks are top-of-book only). So we run
# it as an ISOLATED PAPER SHADOW first: it reads the already-computed signal, records its own
# leg='CERTAINTY' ledger rows, never opens a real position, never touches the risk guard, and
# is hard-gated to paper in main.py. The point is to capture DEPTH-REALISTIC fills via the live
# book's PAPER_FILL_REALISM path before any real-capital leg. Fires in the TAKER zone.
# Mirrors the backtest gate: enter the confident side iff  p_side ≥ FLOOR  AND
# ask ≤ p_side − LAG_MARGIN  AND  ask ≤ MAX_ASK  AND spread ≤ MAX_SPREAD AND fee-net EV ≥ 0.
CERTAINTY_SHADOW_ENABLED = True    # master switch (paper-only effect). False = complete no-op.
# Per-asset gate: the certainty edge is concentrated in SOL/XRP. On fresh post-2026-06-21 paper
# data (recovered.db, 1,124 resolved shadows since Jun-22 09:00 ET, realistic +1-tick fill):
#   SOL+XRP : PF 1.72 / win 93.0% / EV $1.29   (CLEARS the 1.5 live gate)
#   BTC+ETH : PF 1.06 / win 88.7% / EV ~0       (carries adverse-selection risk for no edge)
# So the leg only fires on the assets below; BTC/ETH still trade/record everything else but skip
# the certainty shadow. Revisit if BTC/ETH accumulate a PF>=1.5 sample. Validated 2026-06-24.
# 2026-06-28: GATED TO SOL — the only asset with a survivorship-free edge. Three independent lines
# of evidence converge: (1) post-22 backtest realistic fills SOL +$100/PF 1.12 vs BTC −$303/ETH −$317;
# (2) BTC/ETH stay NEGATIVE even at perfect "touch" fills (latency/London cannot rescue them);
# (3) apples-to-apples reconciliation of the live dashboard vs backtest — the live +P&L on BTC/ETH/XRP
# was SURVIVORSHIP (bot traded 622/1206 windows @ 92.4% vs the true 86.7% pop = −$637), only SOL was
# positive in BOTH. XRP is marginal (backtest ~breakeven, live +$206 was a 97.7%/86-trade streak) — it
# can be re-added if it clears on London-tightened fills. NOTE: this gates only the certainty SHADOW;
# `_record_tick` still records ticks for ALL config.ASSETS, so the BTC/ETH/XRP London A/B backtest is
# fully preserved. See memory dashboard-pnl-survivorship-bias / certainty-asset-gate-sol-xrp.
CERTAINTY_ASSETS = ("BTC", "ETH", "SOL", "XRP", "BNB")   # leg fires & records on all 5; BNB is record-only (PNL_EXCLUDED_ASSETS) — 2026-06-29
# Live-capital whitelist: in --mode live, ONLY these assets place REAL orders; any other asset that
# fires the leg falls through to a paper shadow even in live mode. 2026-06-28: WIDENED to all 4 at
# the user's direction (was SOL-only). RISK NOTE: SOL is the only survivorship-free edge; XRP was the
# only NEGATIVE-edge asset on fresh on-chain data and BTC/ETH are marginal — the cross-asset stake
# guard below (bounds an all-4 same-side loss to CERTAINTY_CORR_STAKE_USDC) is the safety belt that
# makes this defensible. Watch XRP's live P&L closely. See certainty-asset-gate-sol-xrp / golive-sol-only.
CERTAINTY_LIVE_ASSETS = ("BTC", "ETH", "SOL", "XRP")
CERTAINTY_FLOOR      = 0.80        # min model prob for the side to count as "certain"
CERTAINTY_LAG_MARGIN = 0.03        # min book lag (p_side − ask) required to enter
CERTAINTY_MAX_ASK    = 0.97        # never buy above this — taker fee eats the edge past here
CERTAINTY_MIN_ASK    = 0.82        # never buy BELOW this. RAISED back 0.78→0.82 (2026-06-30) — see note.
CERTAINTY_SIZE_USDC  = 1.5         # base notional per certainty bet ($1.5 live tranche 2026-06-27)
CERTAINTY_MIN_ORDER_USDC = 1.0     # Polymarket minimum order ($1); never place a live order below this
#                                    (a guard-reduced share under $1 falls through to a paper shadow)

# ─── Fill-quality / depth telemetry (2026-06-30) ──────────────────────────────────────────
# At each certainty fire, snapshot the ask-book DEPTH the trade actually saw: the size at the
# touch and the depth-walk VWAP + fill-fraction for several USDC notional tiers. We currently log
# top-of-book PRICES everywhere but never QUANTITIES, so we cannot answer "how big can we fill
# before slippage eats the ~1.3%/trade edge" — the gating question for sizing up past $1.5. One
# row per fire (~550/day, negligible) in the `cert_fills` table; replay edge-vs-size offline.
CERT_DEPTH_LOG_ENABLED = True
CERT_DEPTH_TIERS = (1.5, 5.0, 15.0, 25.0, 50.0)   # USDC notionals to price the depth-walk at
# CERTAINTY_MIN_ASK: only enter when the BOOK already prices the favorite ≥ this. A large
# model-vs-book gap (model 0.90 while the book sits near 0.50) is NOT feed-lag — it is model
# overconfidence against a fairly-priced book, and those entries LOSE under realistic fills.
# The genuine edge is buying a favorite the book AGREES is a favorite but lags slightly.
# Validation (recovered bot_state.db, 1,671 REAL windows, realistic +1-tick fill, OOS 70/30):
#   no floor       : full PF 0.93 / OOS PF 0.92  (net-negative — the live bleed)
#   ask ≥ 0.82     : full PF 1.07 / OOS PF 1.09-1.18; per-asset BTC 1.18 ETH 1.11 (was 0.70) SOL 0.99
# REFINED 2026-06-28: a fine sub-floor sweep (analyze_barbell, full DB, cert leg) found the real edge
# CLIFF is at 0.78, not 0.82 — the old floor over-corrected and discarded a profitable band:
#   0.70-0.78 : 67-74% win, net edge -3 to -6  → -$142  (model overconfidence — STILL excluded)
#   0.78-0.80 : 86.8% win,  net edge +7.0      → +$74
#   0.80-0.82 : 87.2% win,  net edge +5.3      → +$25
# Realized win% AT a given book ask is a market property (book underprices favorites here, 87% vs
# ~79% breakeven), so it is more model-independent / robust than the rejected barbell. The floor was
# lowered to 0.78 with the CAVEAT to "raise back if it regresses" on a larger sample.
# REGRESSED — RAISED BACK TO 0.82 (2026-06-30): the first FULL live day (bot_state.db, 541 real
# certainty fills, 4 assets @ $1.5) invalidated the 0.78 band. Entry-bucket P&L for 2026-06-30:
#   <0.78    : 72.4% win  -$2.48   (leaked past the 0.78 gate via signal→fill ask drift)
#   0.78-0.80: 76.2% win  -$6.56   ← biggest single bleeder
#   0.80-0.82: 78.6% win  -$3.95
#   0.82-0.86: 88.5% win  +$7.92
#   0.86-0.90: 87.6% win  -$0.92
#   >=0.90   : 93.2% win  -$0.50
# Sub-0.82 summed to -$12.99 ≈ the ENTIRE day's -$12.93 loss; entries >=0.82 netted +$6.51.
# Per asset, restoring the 0.82 floor flips today: SOL -10.47→+2.06, total -6.48→+6.51 (ETH/XRP
# stay positive; BTC stays slightly negative -4.06 even at 0.82 — it is the persistently weak asset).
# The 0.78-0.82 band wins only ~76-79% live vs the ~80-82% breakeven there — exactly the model-
# overconfidence-vs-fair-book bleed the floor exists to exclude. Monitor with `analyze_barbell.py
# --leg CERT_LIVE`; do not lower again without a large bias-free sample that reproduces the edge.
# Do NOT go below 0.78: sub-0.78 is the validated bleed zone. (A model-vs-book gap CAP was separately
# tried and REJECTED, PF→0.85.) Bonus: cheaper entries also shrink the "1 loss eats N wins" ratio.
# REJECTED by the same validation: a model-vs-book gap CAP (hurt, PF→0.85) and firing only in
# the last ≤45s (non-monotonic; ≤45s was net-negative here, unlike the older 8,044-window DB).

# ─── Entry-price BARBELL gate (2026-06-28) — TESTED AND REJECTED, kept OFF for the record ──
# Hypothesis from 733 REAL on-chain trades (J27+J28, data-api /activity, resolved via Gamma):
# entry win% vs breakeven looked like a BARBELL — edge in 0.78-0.85 and 0.91-0.97, with the
# 0.85-0.91 "murky middle" a fee-funded NET LOSER (-$13). It did NOT replicate: analyze_barbell.py
# on the full DB (4,191 cert trades) shows 0.85-0.91 is solidly POSITIVE (net_edge +3.6 / +2.3pts,
# +$1,572) and the curve is ~monotonic — the only band that loses in BOTH samples is the <0.78
# longshot tail, which CERTAINTY_MIN_ASK=0.82 already excludes. Skipping the middle would DELETE
# edge. So the gate stays OFF. The 2-day barbell was small-sample noise (~1 SE per bucket) — a
# textbook reminder to validate on the larger sample before acting. Re-enable only if a large,
# bias-free REAL sample reproduces it (check with analyze_barbell.py). See longshot-tail-miscalibration.
CERTAINTY_BARBELL_ENABLED = False
CERTAINTY_DEAD_ASK_LO = 0.85       # skip entries with DEAD_LO <= ask < DEAD_HI (only if ENABLED)
CERTAINTY_DEAD_ASK_HI = 0.91

# Zone: the certainty edge is CONCENTRATED IN THE LAST 10-45s, not the mid-window. Probe
# (sy/cert_zone_experiment.py, recovered DB, realistic +1-tick fill, 2026-06-21):
#   zone 45..220s : PF 1.14 / EV $0.68   (mid-window — below the 1.5 gate)
#   zone 10..45s  : PF 1.59 / EV $2.03   (LATE slice — CLEARS the 1.5 gate)
#   zone 10..45s + move>=5bp : PF 1.91 / EV $2.51
#   zone 10..45s + move>=10bp: PF 2.57 / EV $3.18
# Extending the gate to fire down to T-10s is also OOS-stable (TEST +$719 -> +$814). This
# overturns the old "never trade the last 45s" doctrine for THIS leg: we are not racing a new
# move, we are buying a favorite whose ask the book has left stale (lag persisting into our 1s
# tick). Still measured top-of-book + 1 tick; the depth-walk is what the live paper run proves.
CERTAINTY_ZONE_START = 220         # secs remaining: gate may start at/below this
CERTAINTY_ZONE_END   = 10          # secs remaining: gate stops at/below this (extended 45->10)

# Window-Delta gate (the winners' DOMINANT signal): only fire when the oracle has ALREADY
# moved >= this many bp from the strike. Raises win%/PF (late slice 1.59 -> 1.91 at 5bp ->
# 2.57 at 10bp) by skipping the near-boundary windows where the favorite can still flip.
CERTAINTY_MIN_MOVE_BP = 5.0

# ─── Cross-asset CORRELATION guard (2026-06-28) ───────────────────────────────────────────
# The 4 assets move together, so N simultaneous same-direction certainty bets in one 5-min window
# are really ONE bet at N× size — fake diversification. Measured (4,191 cert trades): windows where
# >=2 assets lost together = 44% of ALL loss dollars; 4 windows lost all four; worst window -$102.
#
# Design = COMBINED STAKE CAP (not a count cap): bound the TOTAL same-side live certainty stake per
# window to CERTAINTY_CORR_STAKE_USDC, split equally across the live universe. Each firing asset
# requests its fair share (budget / #live-assets), capped at its base size; the guard grants up to
# the remaining budget. This keeps BREADTH — when all 4 agree and WIN, you still win all 4 (smaller
# size each) instead of forgoing the 3rd/4th (which historically win 92.4%) — while an all-4-LOSS is
# bounded to the budget. With SOL-only live (1 asset) the share = full budget, capped at base, so it
# is a NO-OP today; it only reshapes sizing once CERTAINTY_LIVE_ASSETS widens. Trade-off: in a
# multi-asset-live regime a window where only ONE asset fires is sized at its fair share, not full
# base (conservative under-bet on uncorrelated windows — tune the budget when you go multi-live).
# Only the LIVE order path is constrained; paper shadows still record every fire for analysis.
# Budget = $4 with 4 live assets ⇒ $1 each (the Polymarket minimum) when all 4 agree, total $4/window
# same-side; all-4 WIN takes all four, all-4 LOSS bounded to -$4. A share that would round below
# CERTAINTY_MIN_ORDER_USDC is not placed live (→ paper) — so keep budget ≥ #live × $1.
# 2026-06-29: DISABLED at the user's direction — they want each position at the FULL base size
# ($1.5), NOT reduced to budget/#assets ($1 each). Side effect this fixes: when all assets fired
# the shared $4 budget drained (worse with FAK-failure retries that still reserve), so the last
# asset's grant rounded to $0 < $1 min order and fell to a PAPER shadow — which still shows in the
# dashboard Trade History but never reaches Polymarket (the "frontend shows BTC, Polymarket didn't
# take it" report). With the guard OFF, every live asset places its full $1.5 order, no phantom
# paper rows. TRADE-OFF: the all-4-same-side-loss bound is gone; worst case is now 4×$1.5 = $6/window
# (still far under MAX_DAILY_LOSS=$50). To restore the cap WITHOUT shrinking size, re-enable and set
# CERTAINTY_CORR_STAKE_USDC = 6.0 (= #live × base) so each share = min(1.5, 6/4=1.5) = full $1.5.
CERTAINTY_CORR_GUARD_ENABLED = False
CERTAINTY_CORR_STAKE_USDC    = 6.0    # max TOTAL same-side live certainty stake across assets / window (only used if guard re-enabled)

# Confidence sizing (P3): in the validated late slice the edge is large and low-variance, so
# size up there instead of flat $25. Stake = base, bumped to LATE_SIZE inside the late zone
# when the move gate is strongly cleared. Capped by CERTAINTY_MAX_SIZE_USDC. Paper-only effect.
CERTAINTY_LATE_FROM   = 45         # secs remaining at/below which "late-slice" sizing applies
CERTAINTY_LATE_SIZE_USDC = 1.5     # flat $1.5 (late 2x dropped)
CERTAINTY_MAX_SIZE_USDC  = 1.5     # hard cap on any single certainty bet

# ─── Maker-first entry (PAPER) — the execution lever ────────────────────────────────────
# 2026-06-28. Measured finding (recovered.db, 1,665 trades): the certainty leg's P&L is almost
# entirely a function of FILL price, not signal — taker (+1 adverse tick) = −$693 / PF 0.87, while
# a maker fill (−1 tick) = +$213 / PF 1.04 (each 1c of fill ≈ $453 / ~$0.27/trade). The +$213 is an
# UPPER BOUND, though: a resting limit fills only when the book trades through it — which is
# disproportionately when the favorite is WEAKENING (adverse selection — retired thesis #1). So this
# models the REALISTIC recoverable middle: when the gate fires we POST a limit at ask − OFFSET ticks
# for WAIT secs; it fills (as a MAKER, fee=0) ONLY if the live book actually trades down to our price
# within the window; otherwise we CROSS and take at the current ask + 1 tick (today's behavior). The
# adverse-selection bias is captured, not assumed away. PAPER-only; live entry path is unchanged.
# 2026-06-28: DISABLED. The realistic offline test landed at −$705 ≈ pure taker (only 22% fill as
# maker, and those are the losers: PF 0.84). It does not recover P&L AND it muddies the clean
# before/after taker-fill measurement we need for the London migration. Keep it as a pure TAKER
# baseline so the London latency improvement (tighter taker fills) is cleanly attributable. Code is
# retained; flip back to True only to re-measure the maker path. See maker-first-adverse-selection.
CERTAINTY_MAKER_ENABLED      = False
CERTAINTY_MAKER_OFFSET_TICKS = 1     # rest the buy limit this many ticks below the displayed ask
CERTAINTY_MAKER_WAIT_SECS    = 2.0   # how long the limit rests before crossing to a taker fill

# ─── CERTAINTY box-stop (SHADOW: measures the loss-capping hedge, never places it) ──────
# 2026-06-27. On ~/Downloads/bot_state.db (1759 resolved cert trades) a hedge-to-box on an
# ORACLE-vs-strike trigger (NOT the model prob — that conceded too late: see
# certainty-boxing-fails) recovered +$564–$829 OOS-robust on all 4 assets: in the last ~30s,
# if the bet side is on the WRONG side of the strike, buy the opposite side → locked $1 box.
# This leg is a pure measurement: it walks the REAL opposite book for a depth-realistic hedge
# fill and logs a leg='CERTAINTY_BOX' shadow row + a counterfactual "saved" tally, but never
# places a real order and is excluded from the P&L ledgers. It exists to validate the live
# hedge fill (the one thing the backtest could not model) before any real-capital hedge.
CERTAINTY_BOX_ENABLED     = True
CERTAINTY_BOX_FROM        = 30     # only consider boxing at/below this t_remaining (edge is here)
CERTAINTY_BOX_MIN_T       = 2      # too late to model a hedge fill below this t_remaining
CERTAINTY_BOX_MARGIN_BP   = 1.0    # bet side must be adverse (oracle past strike) by ≥ this
CERTAINTY_BOX_PERSIST     = 2      # SECONDS the oracle must stay adverse before boxing (de-noise;
                                   # time-based so it's invariant to the event-driven loop cadence)
CERTAINTY_BOX_MAX_OPP_ASK = 0.88   # skip if the hedge is already this rich (late flip — no benefit)
CERTAINTY_BOX_MAX_TOTAL   = 1.5    # never pay more than this per pair to lock a $1 box
# Partial boxing — the leaderboard winners (Bonereaper) hedge only ~50% of the position so a false
# trigger doesn't clip the whole win. 1.0 = full box (the validated +$552 default on our data); set
# 0.5 to shadow-test the winners' partial profile. See WINNERS.md §8.4.
CERTAINTY_BOX_FRACTION    = 1.0
# Credit boxing — box opportunistically when the pair already locks a CREDIT (entry+opp_ask ≤ cap),
# risk-free regardless of direction. This is the winners' "median pair cost $0.984" mechanic, but it
# is an artifact of their CHEAP ~0.40 entries: with our 0.82+ favorite entries a credit (total<1)
# needs opp_ask ≤ ~0.17, which means our side is already a deep favorite (likely to win) — so boxing
# it usually FORFEITS EV. Default OFF; enable only to MEASURE how often/whether it ever helps us.
CERTAINTY_BOX_CREDIT      = False
CERTAINTY_BOX_CREDIT_MAX  = 0.99   # box if entry + opp_ask ≤ this (locks ≥1¢/pair)
CERTAINTY_BOX_CREDIT_FROM = 70     # credit box may fire in the last N secs (winners' ~70s window)
# The boxed hedge is recorded as its own leg='CERTAINTY_BOX' trade row — VISIBLE in the dashboard
# Trade History (rendered as BOX·SHADOW) — but its P&L is deliberately kept OUT of the session and
# total P&L counters (every ledger aggregate filters to leg='CERTAINTY'). So the certainty leg's
# headline P&L always reflects the un-boxed ride; the box row is a visible-but-uncounted measurement.

# ─── Fee Constants (Fee Structure V2, effective Mar 30 2026) ───────────────────
# Crypto taker fee = C × 0.07 × p × (1−p), per share. Makers pay zero.
TAKER_FEE_RATE       = 0.07    # crypto category coefficient
MAKER_REBATE_SHARE   = 0.20    # crypto: 20% of taker fee pool redistributed to makers daily
# NOTE: verify the live coefficient with getClobMarketInfo() before any live session —
# the fee schedule has changed before and is per-category.

# ─── WebSocket Heartbeat ────────────────────────────────────────────────────────
POLYMARKET_PING_INTERVAL = 10  # seconds between PING messages
RECONNECT_BASE_DELAY     = 2   # seconds for first reconnect attempt
RECONNECT_MAX_DELAY      = 30  # seconds cap on reconnect backoff

# ─── Market Discovery Polling ──────────────────────────────────────────────────
GAMMA_POLL_INTERVAL  = 10      # seconds between Gamma event fetches (single slug, cheap)

# ─── Dashboard / State Push ────────────────────────────────────────────────────
DASHBOARD_PORT       = int(os.getenv("DASHBOARD_PORT", "8888"))       # WebSocket state feed
DASHBOARD_HTTP_PORT  = int(os.getenv("DASHBOARD_HTTP_PORT", "8000"))  # serves the UI page
# Bind host for the dashboard. Default localhost = NOT exposed publicly (use an SSH tunnel:
# `ssh -L 8000:localhost:8000 user@vps`). Set DASHBOARD_HOST=0.0.0.0 to expose it on the VPS
# IP — only do that behind a firewall, since it reveals your live trading state to anyone.
DASHBOARD_HOST       = os.getenv("DASHBOARD_HOST", "127.0.0.1")
# Optional shared secret to protect the LIVE kill-switch endpoint when the dashboard is
# reachable beyond localhost. If set, POST /api/live/toggle requires header
# X-Dashboard-Token to match (the UI prompts once and stores it locally). Empty = no check
# (fine for a localhost/SSH-tunnel-only dashboard).
DASHBOARD_TOKEN      = os.getenv("DASHBOARD_TOKEN", "")
STATE_PUSH_INTERVAL  = 1.0     # seconds between dashboard WebSocket pushes

# ─── Event-driven entry latency ────────────────────────────────────────────────
# The trade loop used to poll every 1s, adding up to ~1000ms between a fresh oracle move and the
# entry decision. It now WAKES the instant the settlement price moves (RTDS Chainlink / CEX blend,
# which lead), cutting that to ~FAST_POLL_SEC. Heavy/IO work (DB tick+signal writes, resolution
# retries, dashboard snapshot) is throttled to HEAVY_MIN_GAP so the faster cadence keeps the SAME
# ~1Hz data granularity and does NOT bloat the ticks/signals tables. See WINNERS.md §3.
FAST_POLL_SEC   = 0.05   # price-sampling interval while waiting ≈ worst-case entry latency
LOOP_MAX_WAIT   = 1.0    # never wait longer than this (heartbeat for discovery/resolution/box)
HEAVY_MIN_GAP   = 1.0    # min seconds between DB-write / network-retry / snapshot passes

# ─── Data retention (bound DB growth on a long-running host) ────────────────────
TICK_RETENTION_DAYS  = int(os.getenv("TICK_RETENTION_DAYS", "14"))   # 0 = keep forever
# How often the runner prunes old ticks + truncates the WAL during a long session
# (prune used to run only at startup, so a week-long run never reclaimed space).
MAINTENANCE_INTERVAL_SECS = int(os.getenv("MAINTENANCE_INTERVAL_SECS", "3600"))

# ─── SQLite Database ───────────────────────────────────────────────────────────
DB_PATH              = "bot_state.db"

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL            = "INFO"
LOG_FILE             = "bot.log"
LOG_ROTATION         = "10 MB"
