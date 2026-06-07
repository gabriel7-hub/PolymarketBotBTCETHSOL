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
MIN_EV_TAKER         = 0.015   # min fee-net EV per share ($) to fire an IOC taker
MIN_EV_MAKER         = 0.005   # min EV per share ($) for a rebate-farm maker quote
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
RESOLUTION_FALLBACK_SECS = 20  # secs after window close to wait for the REAL Polymarket
                               # outcome before settling a paper position on our own oracle
                               # price (so positions never hang OPEN if on-chain res lags)

# ─── Probability Model (driftless random-walk barrier — calibrate via backtest.py)
# P(Up) = Φ( (S_now − ref) / (σ_price · √t_remaining) )
VOL_WINDOW_SECS      = 45      # rolling window of log-returns for realized vol estimate
MOMENTUM_WINDOW_SECS = 15      # seconds of price history for the 15s momentum diagnostic
VOL_FLOOR_PER_SEC    = 1.0e-5  # floor on per-second return vol (avoid div-by-zero / overconfidence)
VOL_MULT             = 1.0     # live σ scaling. Calibrate with `backtest.py --sweep` on REAL
                               # outcomes and set the Brier-minimising value here.
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
STATE_PUSH_INTERVAL  = 1.0     # seconds between dashboard WebSocket pushes

# ─── SQLite Database ───────────────────────────────────────────────────────────
DB_PATH              = "bot_state.db"

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL            = "INFO"
LOG_FILE             = "bot.log"
LOG_ROTATION         = "10 MB"
