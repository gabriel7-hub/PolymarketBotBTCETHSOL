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
    # XRP exists too (xrp-updown-5m-) — add to ASSETS when desired.
    "XRP": {"name": "XRP",      "binance_symbol": "xrpusdt",
            "coinbase_product": "XRP-USD", "chainlink_symbol": "xrp/usd",
            "chainlink_agg": "0x785ba89291f676b5386652eB12b30cF361020694",
            "slug_prefix": "xrp-updown-5m-", "title_pattern": "XRP Up or Down"},
}

# Assets traded this session (env override: ASSETS=BTC,ETH,SOL,XRP).
ASSETS = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL").split(",")
          if a.strip().upper() in ASSET_PARAMS]

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

BOX_STOP_ENABLED     = True    # hedge-to-box stop-loss on the open taker position
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
CERTAINTY_FLOOR      = 0.80        # min model prob for the side to count as "certain"
CERTAINTY_LAG_MARGIN = 0.03        # min book lag (p_side − ask) required to enter
CERTAINTY_MAX_ASK    = 0.97        # never buy above this — taker fee eats the edge past here
CERTAINTY_MIN_ASK    = 0.82        # never buy BELOW this — see note. Validated 2026-06-22.
CERTAINTY_SIZE_USDC  = 25.0        # base paper notional per certainty bet
# CERTAINTY_MIN_ASK: only enter when the BOOK already prices the favorite ≥ this. A large
# model-vs-book gap (model 0.90 while the book sits near 0.50) is NOT feed-lag — it is model
# overconfidence against a fairly-priced book, and those entries LOSE under realistic fills.
# The genuine edge is buying a favorite the book AGREES is a favorite but lags slightly.
# Validation (recovered bot_state.db, 1,671 REAL windows, realistic +1-tick fill, OOS 70/30):
#   no floor       : full PF 0.93 / OOS PF 0.92  (net-negative — the live bleed)
#   ask ≥ 0.82     : full PF 1.07 / OOS PF 1.09-1.18; per-asset BTC 1.18 ETH 1.11 (was 0.70) SOL 0.99
# Still < the 1.5 live gate, so the leg stays PAPER-ONLY; this only stops the measured bleed.
# REJECTED by the same validation: a model-vs-book gap CAP (hurt, PF→0.85) and firing only in
# the last ≤45s (non-monotonic; ≤45s was net-negative here, unlike the older 8,044-window DB).

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

# Confidence sizing (P3): in the validated late slice the edge is large and low-variance, so
# size up there instead of flat $25. Stake = base, bumped to LATE_SIZE inside the late zone
# when the move gate is strongly cleared. Capped by CERTAINTY_MAX_SIZE_USDC. Paper-only effect.
CERTAINTY_LATE_FROM   = 45         # secs remaining at/below which "late-slice" sizing applies
CERTAINTY_LATE_SIZE_USDC = 50.0    # paper notional in the validated late slice
CERTAINTY_MAX_SIZE_USDC  = 50.0    # hard cap on any single certainty bet

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
STATE_PUSH_INTERVAL  = 1.0     # seconds between dashboard WebSocket pushes

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
