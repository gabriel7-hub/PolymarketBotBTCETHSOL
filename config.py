"""
config.py — All tunable constants for the BTC 5-min Polymarket bot.
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
BINANCE_WS_URL       = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
COINBASE_WS_URL      = "wss://ws-feed.exchange.coinbase.com"   # Chainlink-proxy venue
COINBASE_PRODUCT     = "BTC-USD"
# Polymarket Real-Time Data Socket — the ACTUAL Chainlink BTC/USD data-stream price that
# settles these markets (the "Price to Beat"). No auth. Using this for price + strike makes
# our Strike(ref) exactly equal Polymarket's published Price to Beat (zero proxy basis).
CHAINLINK_RTDS_URL   = "wss://ws-live-data.polymarket.com"
CHAINLINK_SYMBOL     = "btc/usd"
# The RTDS Chainlink feed is sparse (heartbeat/deviation-driven), so use its price only
# when it ticked within this many seconds; otherwise fall back to the high-frequency
# Coinbase proxy so the strike is always captured reliably at T=0.
CHAINLINK_MAX_STALE  = 8
POLYMARKET_BOOK_WS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYGON_RPC          = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

# ─── Market Target ─────────────────────────────────────────────────────────────
# 5-min markets are Gamma *events* with slug  "<asset>-updown-5m-<start_unix_ts>",
# where start_ts is always a unix multiple of MARKET_WINDOW_SECS. We construct the
# current window's slug directly from the clock instead of scanning the API.
MARKET_SLUG_PREFIX   = "btc-updown-5m-"       # asset selector (eth-/sol-/xrp- also exist)
MARKET_TITLE_PATTERN = "Bitcoin Up or Down"   # sanity check on the event title
MARKET_WINDOW_SECS   = 300                    # 5 minutes

# ─── Risk Limits ───────────────────────────────────────────────────────────────
MAX_STAKE_PER_MARKET = 25.0    # USDC — max single position size
MAX_DAILY_LOSS       = 50.0    # USDC — hard halt, no override
MAX_OPEN_POSITIONS   = 1       # never hold more than 1 position at once
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
MIN_TAKER_ENTRY      = 0.50    # never IOC a side whose ask is below this. VPS data (684 resolved
                               # trades, 2026-06-07..10): entries <0.20 won 5.5% vs 12.7% implied
                               # (model said 14-21%) — the Gaussian barrier tails are badly over-
                               # confident, mostly right at zone-open (t_rem≈215-219). Sub-0.35
                               # entries netted −$431; 0.35-0.50 was breakeven noise (±$250/day,
                               # +$23 net). The entire edge lives at 0.50-0.65 (+$509). Replay with
                               # this floor: +$661 on 464 trades vs +$253 on all 684. Revisit with
                               # `backtest.py --buckets` once a bucket shows real edge.
MAX_SPREAD           = 0.06    # skip if order book spread is wider than this
MAX_SLIPPAGE         = 0.02    # 2¢: cancel if ask moves more than this before fill

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
VOL_MULT             = 0.7     # live σ scaling. Validated out-of-sample (backtest.py --validate,
                               # 258 REAL windows, 2026-06-08): vol_mult=1.0 lost on every held-out
                               # test slice (53% win, −$ net); vol_mult=0.7 was profitable on all
                               # splits (66–71% win, PF 1.47–2.13). At 1.0 the model's σ was too
                               # wide → probabilities too timid → fake EV near 50¢ paying peak fee.
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
TICK_SIZE            = "0.01"  # Polymarket CLOB tick size for BTC markets

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

# ─── SQLite Database ───────────────────────────────────────────────────────────
DB_PATH              = "bot_state.db"

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL            = "INFO"
LOG_FILE             = "bot.log"
LOG_ROTATION         = "10 MB"
