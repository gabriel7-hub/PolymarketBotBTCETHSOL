"""
utils.py — Logging setup, retry decorators, timestamp helpers.
"""

import time
import math
import functools
from loguru import logger
import config


def realized_vol_per_sec(history, window_secs: float, floor: float) -> float:
    """
    Robust realized per-second return volatility from a (ts, price) history.

    The old estimator computed log(p1/p0)/√Δt on EVERY raw tick. Exchange ticker/trade
    streams fire many times per second, so that picks up bid-ask bounce and quote flicker
    (microstructure noise) and — because Δt is tiny — amplifies it, inflating σ several-
    fold. An over-large σ makes the barrier model treat near-certain windows as coin-flips
    and "see" phantom edges on the cheap side. (Confirmed by the backtest vol-mult sweep:
    Brier improves monotonically as σ is scaled down.)

    Fix: resample to a 1-second grid (last price per whole second), take 1s log-returns,
    and return their standard deviation. 1s sampling is slow enough to wash out
    microstructure noise while still capturing real short-horizon vol.
    """
    cutoff = time.time() - window_secs
    # Last price seen in each integer-second bucket → regular 1s spacing.
    buckets: dict[int, float] = {}
    for ts, p in history:
        if ts >= cutoff and p > 0:
            buckets[int(ts)] = p
    if len(buckets) < 3:
        return floor
    series = [buckets[s] for s in sorted(buckets)]
    rets = [math.log(series[i] / series[i - 1])
            for i in range(1, len(series)) if series[i - 1] > 0]
    if len(rets) < 2:
        return floor
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return max(floor, math.sqrt(var))


def setup_logging():
    """Configure loguru for console + rotating file output."""
    logger.remove()
    logger.add(
        sink=lambda msg: print(msg, end=""),
        level=config.LOG_LEVEL,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan> — {message}",
        colorize=True,
    )
    logger.add(
        config.LOG_FILE,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name} — {message}",
        rotation=config.LOG_ROTATION,
        retention="7 days",
    )


def retry(max_attempts: int = 3, base_delay: float = 1.0, backoff: float = 2.0):
    """Decorator: retry on any Exception with exponential backoff."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_attempts:
                        logger.error(f"{fn.__name__} failed after {max_attempts} attempts: {exc}")
                        raise
                    logger.warning(f"{fn.__name__} attempt {attempt} failed ({exc}). Retry in {delay:.1f}s")
                    time.sleep(delay)
                    delay = min(delay * backoff, config.RECONNECT_MAX_DELAY)
        return wrapper
    return decorator


def now_ms() -> int:
    """Current UNIX timestamp in milliseconds."""
    return int(time.time() * 1000)


def now_ts() -> float:
    """Current UNIX timestamp as float seconds."""
    return time.time()


def bp(price_now: float, price_ref: float) -> float:
    """Basis points distance: (price_now - price_ref) / price_ref * 10000."""
    if price_ref == 0:
        return 0.0
    return (price_now - price_ref) / price_ref * 10_000


def format_usdc(amount: float) -> str:
    """Format a USDC amount with sign and 2 decimal places."""
    sign = "+" if amount >= 0 else ""
    return f"{sign}{amount:.2f}"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
