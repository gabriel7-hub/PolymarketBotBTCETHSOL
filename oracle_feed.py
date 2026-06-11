"""
oracle_feed.py — Chainlink-proxy price layer.

Polymarket's 5-min BTC markets settle on **Chainlink Data Streams** (an aggregate of
major venues incl. Coinbase), snapshotted at the exact window end — NOT on Binance.
Modeling against Binance alone introduces basis error precisely at the boundary where
the binary is decided.

This module adds a **Coinbase** BTC-USD feed (a much closer proxy to the Chainlink
aggregate than Binance) and an `Oracle` that:
  - blends Coinbase + Binance into a settlement-proxy `price`,
  - reports `cex_basis_bp` (venue disagreement) so the model can widen σ,
  - exposes realized vol and a price-at-timestamp snapshot for the reference price.

OPTIONAL UPGRADE (left as a hook): replace `price` with true Chainlink Data Streams
(credentialed low-latency API) or read the on-chain aggregator via `config.POLYGON_RPC`.
That is out of scope for the foundation round.
"""

import json
import math
import time
import threading
from collections import deque
from typing import Optional
import websocket
from loguru import logger
import config
from utils import bp, realized_vol_per_sec as _rv


class CoinbaseFeed:
    """Real-time <asset>-USD price from Coinbase's `ticker` channel. Mirrors BinanceFeed.
    One instance per asset, each on its own socket."""

    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self._product = config.ASSET_PARAMS[asset]["coinbase_product"]
        self.current_price: float = 0.0
        self.connected: bool = False
        self._history: deque = deque(maxlen=3600)
        self._lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stop = threading.Event()
        self._reconnect_delay = config.RECONNECT_BASE_DELAY

    def start(self):
        t = threading.Thread(target=self._run_loop, daemon=True,
                             name=f"coinbase-{self.asset.lower()}")
        t.start()
        logger.info(f"CoinbaseFeed[{self.asset}] started ({self._product})")

    def stop(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def price_at(self, ref_ts: float) -> float:
        """Price closest to ref_ts from history, or current."""
        with self._lock:
            best = min(self._history, key=lambda x: abs(x[0] - ref_ts), default=None)
        return best[1] if best else self.current_price

    @property
    def realized_vol_per_sec(self) -> float:
        """Robust realized per-second return vol over VOL_WINDOW_SECS (1s-grid resampled)."""
        with self._lock:
            hist = list(self._history)
        return _rv(hist, config.VOL_WINDOW_SECS, config.VOL_FLOOR_PER_SEC)

    # ─── WebSocket internals ───────────────────────────────────────────────────

    def _run_loop(self):
        while not self._stop.is_set():
            self._connect()
            if not self._stop.is_set():
                self.connected = False
                logger.warning(f"CoinbaseFeed[{self.asset}] disconnected. "
                               f"Reconnecting in {self._reconnect_delay}s")
                self._stop.wait(timeout=self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, config.RECONNECT_MAX_DELAY)

    def _connect(self):
        self._ws = websocket.WebSocketApp(
            config.COINBASE_WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        self.connected = True
        self._reconnect_delay = config.RECONNECT_BASE_DELAY
        sub = {
            "type": "subscribe",
            "product_ids": [self._product],
            "channels": ["ticker"],
        }
        try:
            ws.send(json.dumps(sub))
        except Exception as exc:
            logger.warning(f"CoinbaseFeed[{self.asset}] subscribe failed: {exc}")
        logger.info(f"CoinbaseFeed[{self.asset}] connected")

    def _on_close(self, ws, code, msg):
        self.connected = False
        logger.info(f"CoinbaseFeed[{self.asset}] closed (code={code})")

    def _on_error(self, ws, error):
        self.connected = False
        logger.warning(f"CoinbaseFeed[{self.asset}] error: {error}")

    def _on_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)
            if msg.get("type") == "ticker" and msg.get("price"):
                price = float(msg["price"])
                ts = time.time()
                self.current_price = price
                with self._lock:
                    self._history.append((ts, price))
        except Exception as exc:
            logger.warning(f"CoinbaseFeed parse error: {exc}")


class ChainlinkFeed:
    """
    The EXACT Chainlink <asset>/USD data-stream price Polymarket uses to settle these
    markets, via the Real-Time Data Socket (no auth). This is the "Price to Beat" source —
    using it for the strike snapshot makes Strike(ref) equal Polymarket's published value
    exactly. One instance (one socket) per asset.
    """

    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self._symbol = config.ASSET_PARAMS[asset]["chainlink_symbol"]
        self.current_price: float = 0.0
        self.connected: bool = False
        self._last_ts: float = 0.0          # wall-clock of last real price update
        self._history: deque = deque(maxlen=3600)
        self._lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stop = threading.Event()
        self._reconnect_delay = config.RECONNECT_BASE_DELAY

    def start(self):
        threading.Thread(target=self._run_loop, daemon=True,
                         name=f"chainlink-{self.asset.lower()}").start()
        logger.info(f"ChainlinkFeed[{self.asset}] started "
                    f"(Polymarket RTDS settlement price, {self._symbol})")

    @property
    def fresh(self) -> bool:
        """True only if a real price arrived recently (the feed is sparse)."""
        return self.current_price > 0 and (time.time() - self._last_ts) <= config.CHAINLINK_MAX_STALE

    def stop(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def price_at(self, ref_ts: float) -> float:
        with self._lock:
            best = min(self._history, key=lambda x: abs(x[0] - ref_ts), default=None)
        return best[1] if best else self.current_price

    @property
    def realized_vol_per_sec(self) -> float:
        with self._lock:
            hist = list(self._history)
        return _rv(hist, config.VOL_WINDOW_SECS, config.VOL_FLOOR_PER_SEC)

    def _run_loop(self):
        while not self._stop.is_set():
            self._connect()
            if not self._stop.is_set():
                self.connected = False
                logger.warning(f"ChainlinkFeed[{self.asset}] disconnected. "
                               f"Reconnecting in {self._reconnect_delay}s")
                self._stop.wait(timeout=self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, config.RECONNECT_MAX_DELAY)

    def _connect(self):
        self._ws = websocket.WebSocketApp(
            config.CHAINLINK_RTDS_URL,
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        # Polymarket's RTDS does NOT reply to protocol ping frames, so enforcing a
        # pong timeout (ping_timeout) kills a healthy connection every ~10s. Send pings
        # to keep the link alive but DON'T disconnect on a missing pong (ping_timeout=None).
        self._ws.run_forever(ping_interval=20, ping_timeout=None)

    def _on_open(self, ws):
        self.connected = True
        self._reconnect_delay = config.RECONNECT_BASE_DELAY
        sub = {"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*",
             "filters": json.dumps({"symbol": self._symbol})}]}
        try:
            ws.send(json.dumps(sub))
        except Exception as exc:
            logger.warning(f"ChainlinkFeed[{self.asset}] subscribe failed: {exc}")
        logger.info(f"ChainlinkFeed[{self.asset}] connected")

    def _on_close(self, ws, code, msg):
        self.connected = False
        logger.info(f"ChainlinkFeed[{self.asset}] closed (code={code})")

    def _on_error(self, ws, error):
        self.connected = False
        logger.warning(f"ChainlinkFeed[{self.asset}] error: {error}")

    def _on_message(self, ws, raw: str):
        if not raw or raw[0] not in "[{":   # ignore empty/keepalive frames quietly
            return
        try:
            msg = json.loads(raw)
            if msg.get("topic") != "crypto_prices_chainlink":
                return
            payload = msg.get("payload") or {}
            # Defensive: only accept our own symbol in case the server-side filter
            # is ignored and the socket streams every asset.
            sym = payload.get("symbol")
            if sym and str(sym).lower() != self._symbol:
                return
            val = payload.get("value")
            if val is None:
                return
            price = float(val)
            if price <= 0:
                return
            pts = payload.get("timestamp")
            ts = (pts / 1000.0) if pts else time.time()
            self.current_price = price
            self._last_ts = time.time()
            with self._lock:
                self._history.append((ts, price))
        except Exception as exc:
            logger.warning(f"ChainlinkFeed parse error: {exc}")


class Oracle:
    """
    Settlement price layer. Primary = the EXACT Chainlink data-stream price (Polymarket
    RTDS) that settles these markets, so `.price` and the strike match Polymarket's "Price
    to Beat" exactly. Coinbase/Binance are kept as a high-frequency vol estimate, a basis
    diagnostic, and a fallback if the Chainlink socket drops.
    """

    def __init__(self, binance, asset: str = "BTC",
                 coinbase: Optional[CoinbaseFeed] = None,
                 chainlink: Optional[ChainlinkFeed] = None,
                 coinbase_weight: float = 0.6):
        self.asset = asset
        self._binance = binance
        self._coinbase = coinbase or CoinbaseFeed(asset)
        self._chainlink = chainlink or ChainlinkFeed(asset)
        self._w = coinbase_weight

    def start(self):
        self._coinbase.start()
        self._chainlink.start()

    @property
    def chainlink(self) -> ChainlinkFeed:
        return self._chainlink

    @property
    def coinbase(self) -> CoinbaseFeed:
        return self._coinbase

    @property
    def connected(self) -> bool:
        return (self._chainlink.connected or self._coinbase.connected
                or self._binance.connected)

    @property
    def _cex_blend(self) -> float:
        cb = self._coinbase.current_price
        bn = self._binance.current_price
        if cb > 0 and bn > 0:
            return self._w * cb + (1 - self._w) * bn
        return cb or bn or 0.0

    @property
    def price(self) -> float:
        """
        EXACT Chainlink settlement price (Polymarket's Price to Beat) WHEN it is fresh.
        The RTDS Chainlink feed is sparse, so when it's stale/absent we use the reliable
        high-frequency Coinbase-weighted CEX blend — the strike is then a ~4bp proxy
        (already covered by basis-σ widening) rather than going stale or empty.
        """
        if self._chainlink.fresh:
            return self._chainlink.current_price
        return self._cex_blend

    @property
    def cex_basis_bp(self) -> float:
        """
        Disagreement (bp) used to widen σ. When the Chainlink feed is fresh, |Chainlink −
        Coinbase| (the true settlement-vs-proxy gap); otherwise |Binance − Coinbase|.
        """
        cb = self._coinbase.current_price
        bn = self._binance.current_price
        if self._chainlink.fresh and cb > 0:
            return abs(bp(self._chainlink.current_price, cb))
        if cb > 0 and bn > 0:
            return abs(bp(bn, cb))
        return 0.0

    @property
    def realized_vol_per_sec(self) -> float:
        """
        Coinbase-weighted BLEND of the two venues' realized vol. (Previously took the
        max, which systematically inflated σ — it double-counted whichever venue was
        noisiest and pushed the model toward 0.5, manufacturing phantom edges.)
        """
        cv = self._coinbase.realized_vol_per_sec
        bv = self._binance.realized_vol_per_sec
        if cv > 0 and bv > 0:
            return self._w * cv + (1 - self._w) * bv
        return cv or bv or config.VOL_FLOOR_PER_SEC

    def price_at(self, ref_ts: float) -> float:
        """Settlement-proxy price closest to a timestamp (for reference snapshots)."""
        cb = self._coinbase.price_at(ref_ts)
        return cb if cb > 0 else self._binance.price_at_ref_or_now(ref_ts)
