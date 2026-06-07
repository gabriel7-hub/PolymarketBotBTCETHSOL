"""
market_discovery.py — Resolve the active BTC Up/Down 5-minute window.

5-min markets are Gamma *events* with slug "<asset>-updown-5m-<start_unix_ts>",
where start_ts is always a unix multiple of 300. So instead of scanning the API we
construct the current window's slug directly from the clock and fetch that one event.

Important facts (verified against the live Gamma API):
  - event.markets[0] holds: conditionId, outcomes='["Up","Down"]',
    clobTokenIds='[upTokenId, downTokenId]' (JSON-string arrays, aligned by index),
    orderPriceMinTickSize, negRisk, closed, outcomePrices (becomes ["1","0"]/["0","1"]
    once resolved).
  - The reference (strike) price is NOT published before resolution — it is the
    Chainlink BTC/USD price at window start. We snapshot it ourselves via the Oracle.
  - resolutionSource == https://data.chain.link/streams/btc-usd (hence oracle_feed.py).
"""

import json
import time
import threading
from dataclasses import dataclass, field
from typing import Optional
import requests
from loguru import logger
import config
from utils import retry


@dataclass
class MarketWindow:
    condition_id:    str
    market_title:    str
    up_token_id:     str
    down_token_id:   str
    start_ts:        float          # UNIX timestamp of window open (from slug)
    end_ts:          float          # UNIX timestamp of window close
    reference_price: float = 0.0    # BTC strike — snapshotted by the bot at start_ts
    tick_size:       str = "0.01"
    neg_risk:        bool = False
    slug:            str = ""
    rewards_max_spread: float = 0.0  # price distance from mid that earns rewards (e.g. 0.045)
    rewards_min_size:   float = 0.0  # min order size to qualify for rewards (e.g. 50)
    fetched_at:      float = field(default_factory=time.time)

    @property
    def time_remaining(self) -> float:
        return max(0.0, self.end_ts - time.time())

    @property
    def has_reference(self) -> bool:
        return self.reference_price and self.reference_price > 0

    @property
    def is_active(self) -> bool:
        return 0 < self.time_remaining < config.MARKET_WINDOW_SECS + 10


def current_window_start(now: Optional[float] = None) -> int:
    """Unix start_ts of the 5-min window containing `now` (epoch-aligned to 300s)."""
    now = time.time() if now is None else now
    return int(now // config.MARKET_WINDOW_SECS) * config.MARKET_WINDOW_SECS


def window_slug(start_ts: int) -> str:
    return f"{config.MARKET_SLUG_PREFIX}{start_ts}"


class MarketDiscovery:
    """
    Resolves the active window deterministically from the clock and fetches it by slug.
    Thread-safe: other modules call .current to get the latest window.
    """

    def __init__(self):
        self._current: Optional[MarketWindow] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def current(self) -> Optional[MarketWindow]:
        with self._lock:
            return self._current

    def start(self):
        t = threading.Thread(target=self._poll_loop, daemon=True, name="market-discovery")
        t.start()
        logger.info("MarketDiscovery started")

    def stop(self):
        self._stop.set()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                window = self.fetch_window(current_window_start())
                if window and window.is_active:
                    with self._lock:
                        self._current = window
                    logger.debug(
                        f"Active: {window.market_title} | T-{window.time_remaining:.0f}s"
                    )
                else:
                    logger.debug("No active BTC 5-min window found")
            except Exception as exc:
                logger.warning(f"MarketDiscovery poll error: {exc}")
            # Poll briefly so we catch each new window right at its boundary.
            self._stop.wait(timeout=config.GAMMA_POLL_INTERVAL)

    @retry(max_attempts=3, base_delay=2.0)
    def fetch_window(self, start_ts: int) -> Optional[MarketWindow]:
        """Fetch and parse the event for the window starting at start_ts."""
        ev = self._fetch_event(window_slug(start_ts))
        if not ev:
            return None
        return self._parse_event(ev, start_ts)

    def fetch_resolution(self, start_ts: int) -> Optional[str]:
        """
        Return the winning side ('UP'/'DOWN') for a window once resolved, else None.
        Reads the real Polymarket resolution (outcomePrices), NOT a price proxy.
        """
        ev = self._fetch_event(window_slug(start_ts))
        if not ev:
            return None
        mkt = self._first_market(ev)
        if not mkt:
            return None
        prices = _json_list(mkt.get("outcomePrices"))
        outcomes = _json_list(mkt.get("outcomes"))
        if not prices or not outcomes or len(prices) != len(outcomes):
            return None
        # Resolved when one outcome price is ~1.0
        for outcome, px in zip(outcomes, prices):
            try:
                if float(px) >= 0.99:
                    return "UP" if str(outcome).lower() in ("up", "yes", "higher") else "DOWN"
            except (TypeError, ValueError):
                continue
        return None

    # ─── HTTP ────────────────────────────────────────────────────────────────────

    def _fetch_event(self, slug: str) -> Optional[dict]:
        resp = requests.get(
            f"{config.GAMMA_API}/events", params={"slug": slug}, timeout=6
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    @staticmethod
    def _first_market(ev: dict) -> Optional[dict]:
        markets = ev.get("markets") or []
        return markets[0] if markets else None

    def _parse_event(self, ev: dict, start_ts: int) -> Optional[MarketWindow]:
        try:
            title = ev.get("title", "") or ""
            if config.MARKET_TITLE_PATTERN not in title:
                logger.debug(f"Event title mismatch: {title!r}")
                return None
            mkt = self._first_market(ev)
            if not mkt:
                return None

            outcomes = _json_list(mkt.get("outcomes"))
            token_ids = _json_list(mkt.get("clobTokenIds"))
            if len(outcomes) < 2 or len(token_ids) < 2:
                return None

            # Map outcome label → token id, then pick Up / Down.
            label_to_token = {str(o).lower(): t for o, t in zip(outcomes, token_ids)}
            up_token_id = (label_to_token.get("up") or label_to_token.get("yes")
                           or token_ids[0])
            down_token_id = (label_to_token.get("down") or label_to_token.get("no")
                             or token_ids[1])

            # rewardsMaxSpread is published in CENTS (e.g. 4.5) → convert to a price.
            rms = mkt.get("rewardsMaxSpread")
            rewards_max_spread = (float(rms) / 100.0) if rms not in (None, "") else 0.0
            rmin = mkt.get("rewardsMinSize")
            rewards_min_size = float(rmin) if rmin not in (None, "") else 0.0

            return MarketWindow(
                condition_id=mkt.get("conditionId") or mkt.get("id") or "",
                market_title=title,
                up_token_id=str(up_token_id),
                down_token_id=str(down_token_id),
                start_ts=float(start_ts),
                end_ts=float(start_ts) + config.MARKET_WINDOW_SECS,
                tick_size=str(mkt.get("orderPriceMinTickSize") or config.TICK_SIZE),
                neg_risk=bool(mkt.get("negRisk", False)),
                slug=ev.get("slug", ""),
                rewards_max_spread=rewards_max_spread,
                rewards_min_size=rewards_min_size,
            )
        except Exception as exc:
            logger.warning(f"Failed to parse event {ev.get('slug','?')}: {exc}")
            return None


def _json_list(value) -> list:
    """Parse a Gamma JSON-string array like '["Up","Down"]' into a Python list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
