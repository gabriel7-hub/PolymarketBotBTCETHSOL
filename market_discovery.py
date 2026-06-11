"""
market_discovery.py — Resolve the active Up/Down 5-minute window for one asset
(BTC/ETH/SOL — one MarketDiscovery instance per asset).

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
    asset:           str = "BTC"    # which Up/Down market this window belongs to
    reference_price: float = 0.0    # asset strike — snapshotted by the bot at start_ts
    tick_size:       str = "0.01"
    neg_risk:        bool = False
    slug:            str = ""
    rewards_max_spread: float = 0.0  # price distance from mid that earns rewards (e.g. 0.045)
    rewards_min_size:   float = 0.0  # min order size to qualify for rewards (e.g. 50)
    rewards_active:  bool = False    # TRUE only if a live liquidity-reward POOL exists.
                                     # BTC 5-min markets publish rewards_max_spread/min_size
                                     # as vestigial template fields but carry rewards.rates=null
                                     # (CLOB) / clobRewards=null (Gamma) → nothing to farm.
                                     # Verified against the CLOB API 2026-06-08. The farm leg
                                     # is gated on this so it never quotes for phantom yield.
    holding_rewards: bool = False    # market-level holdingRewardsEnabled flag (≈never set on 5-min)
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


def window_slug(start_ts: int, asset: str = "BTC") -> str:
    return f"{config.ASSET_PARAMS[asset]['slug_prefix']}{start_ts}"


class MarketDiscovery:
    """
    Resolves the active window deterministically from the clock and fetches it by slug.
    One instance per asset. Thread-safe: other modules call .current for the latest window.
    """

    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self._title_pattern = config.ASSET_PARAMS[asset]["title_pattern"]
        self._current: Optional[MarketWindow] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._clob_meta_cache: dict[str, dict] = {}   # condition_id → authoritative reward/fee meta

    @property
    def current(self) -> Optional[MarketWindow]:
        with self._lock:
            return self._current

    def start(self):
        t = threading.Thread(target=self._poll_loop, daemon=True,
                             name=f"discovery-{self.asset.lower()}")
        t.start()
        logger.info(f"MarketDiscovery[{self.asset}] started")

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
                    logger.debug(f"No active {self.asset} 5-min window found")
            except Exception as exc:
                logger.warning(f"MarketDiscovery poll error: {exc}")
            # Poll briefly so we catch each new window right at its boundary.
            self._stop.wait(timeout=config.GAMMA_POLL_INTERVAL)

    @retry(max_attempts=3, base_delay=2.0)
    def fetch_window(self, start_ts: int) -> Optional[MarketWindow]:
        """Fetch and parse the event for the window starting at start_ts."""
        ev = self._fetch_event(window_slug(start_ts, self.asset))
        if not ev:
            return None
        return self._parse_event(ev, start_ts)

    def fetch_resolution(self, start_ts: int) -> Optional[str]:
        """
        Return the winning side ('UP'/'DOWN') for a window once resolved, else None.
        Reads the real Polymarket resolution (outcomePrices), NOT a price proxy.
        """
        ev = self._fetch_event(window_slug(start_ts, self.asset))
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

    def _clob_meta(self, condition_id: str) -> dict:
        """
        Authoritative reward-pool + fee check from the CLOB, cached per condition_id so it
        costs one cheap GET per 5-min window (not per poll). Returns:
          {"rewards_active": bool, "checked": bool}

        Also a FEE-SCHEDULE TRIPWIRE (improvement #2): our EV engine hardcodes the dynamic
        crypto taker fee as config.TAKER_FEE_RATE · p·(1−p) (= 0.07, confirmed against the
        official fees doc 2026-06-08). The live dynamic-rate descriptor is NOT exposed on the
        public REST endpoint, so we do not auto-override the coefficient (misreading e.g.
        taker_base_fee=1000 as a rate would catastrophically mis-gate EV). Instead we WARN if
        the market grows a top-level flat `fee`, so a future schedule change is caught loudly
        rather than silently mispricing every trade.
        """
        if not condition_id:
            return {"rewards_active": False, "checked": False}
        cached = self._clob_meta_cache.get(condition_id)
        if cached is not None:
            return cached
        meta = {"rewards_active": False, "checked": False}
        try:
            resp = requests.get(f"{config.CLOB_HOST}/markets/{condition_id}", timeout=6)
            resp.raise_for_status()
            m = resp.json()
            rates = (m.get("rewards") or {}).get("rates")
            meta["rewards_active"] = bool(rates)
            meta["checked"] = True
            flat_fee = m.get("fee")
            if flat_fee not in (None, 0, "0", "", "0.0"):
                logger.warning(
                    f"FEE TRIPWIRE: market {condition_id[:12]} reports a non-null top-level "
                    f"fee={flat_fee!r}. Our EV model assumes ONLY the dynamic "
                    f"C·{config.TAKER_FEE_RATE}·p·(1−p) taker fee — verify the live schedule "
                    f"(getClobMarketInfo / docs.polymarket.com/trading/fees) before trusting EV."
                )
        except Exception as exc:
            logger.debug(f"CLOB meta fetch failed for {condition_id[:12]}: {exc}")
        # Cap the cache so a long-running host doesn't accumulate one entry per window forever.
        if len(self._clob_meta_cache) > 500:
            self._clob_meta_cache.clear()
        self._clob_meta_cache[condition_id] = meta
        return meta

    def _parse_event(self, ev: dict, start_ts: int) -> Optional[MarketWindow]:
        try:
            title = ev.get("title", "") or ""
            if self._title_pattern not in title:
                logger.debug(f"[{self.asset}] Event title mismatch: {title!r}")
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

            # Does a live reward POOL actually exist? Prefer the authoritative CLOB
            # rewards.rates; fall back to the Gamma clobRewards mirror if that call failed.
            condition_id = mkt.get("conditionId") or mkt.get("id") or ""
            meta = self._clob_meta(condition_id)
            rewards_active = (meta["rewards_active"] if meta.get("checked")
                              else _rewards_live(mkt.get("clobRewards")))
            holding_rewards = bool(mkt.get("holdingRewardsEnabled", False))

            return MarketWindow(
                condition_id=condition_id,
                market_title=title,
                up_token_id=str(up_token_id),
                down_token_id=str(down_token_id),
                start_ts=float(start_ts),
                end_ts=float(start_ts) + config.MARKET_WINDOW_SECS,
                asset=self.asset,
                tick_size=str(mkt.get("orderPriceMinTickSize") or config.TICK_SIZE),
                neg_risk=bool(mkt.get("negRisk", False)),
                slug=ev.get("slug", ""),
                rewards_max_spread=rewards_max_spread,
                rewards_min_size=rewards_min_size,
                rewards_active=rewards_active,
                holding_rewards=holding_rewards,
            )
        except Exception as exc:
            logger.warning(f"Failed to parse event {ev.get('slug','?')}: {exc}")
            return None


def _rewards_live(clob_rewards) -> bool:
    """
    Fallback reward-pool check from the Gamma `clobRewards` field (used only if the
    authoritative CLOB call failed). True iff it carries a positive reward rate/amount.
    Null/empty (the observed state for BTC 5-min) → no pool. Defensive about shape since
    a populated clobRewards schema isn't observable on these markets.
    """
    if not clob_rewards:
        return False
    items = clob_rewards if isinstance(clob_rewards, list) else [clob_rewards]
    for it in items:
        if not isinstance(it, dict):
            continue
        for key in ("rewardsDailyRate", "rewards_daily_rate", "dailyRate",
                    "rate", "rewardsAmount", "rewardsAmountPerDay"):
            try:
                if float(it.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


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
