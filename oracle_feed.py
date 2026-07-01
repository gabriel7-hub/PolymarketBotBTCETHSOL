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
import urllib.request
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

    @property
    def last_tick_age(self) -> Optional[float]:
        """Seconds since the last real RTDS price tick, or None if none seen yet. Feeds the P0
        dashboard staleness indicator — a large age means Chainlink is stale and the strike may
        be falling back to on-chain / CEX proxy."""
        return (time.time() - self._last_ts) if self._last_ts > 0 else None

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
            header=config.RTDS_WS_HEADERS,   # Cloudflare drops a plain handshake (2026-06-21)
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


class ChainlinkOnchainFeed:
    """
    On-chain Chainlink aggregator (Polygon) read via plain JSON-RPC — a strike anchor for
    when the RTDS Data-Streams socket is unreachable. Polls `latestRoundData()` on a heartbeat
    cadence and exposes the same `current_price` / `fresh` surface as `ChainlinkFeed`, so the
    Oracle can prefer it over the CEX proxy. Verified far closer to the real Price to Beat than
    the proxy (2026-06-21: on-chain BTC/USD ≈ 0.5bp vs proxy ≈ 4.5bp). NOT the exact settlement
    price (the heartbeat lags Data Streams), so it is a fallback, not the primary.
    """
    _SEL_LATEST = "0xfeaf968c"   # latestRoundData()
    _SEL_DECIMALS = "0x313ce567"  # decimals()

    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self._addr = config.ASSET_PARAMS[asset].get("chainlink_agg")
        self.current_price: float = 0.0
        self._updated_at: float = 0.0     # on-chain updatedAt (unix secs)
        self._decimals: Optional[int] = None
        self._stop = threading.Event()

    def start(self):
        if not (config.CHAINLINK_ONCHAIN_ENABLED and self._addr):
            return
        threading.Thread(target=self._run_loop, daemon=True,
                         name=f"cl-onchain-{self.asset.lower()}").start()
        logger.info(f"ChainlinkOnchainFeed[{self.asset}] started (Polygon {self._addr[:10]}…)")

    def stop(self):
        self._stop.set()

    @property
    def fresh(self) -> bool:
        """True only if the latest on-chain round updated recently (heartbeat-driven)."""
        return (self.current_price > 0
                and (time.time() - self._updated_at) <= config.CHAINLINK_ONCHAIN_MAX_STALE)

    def _rpc(self, data: str) -> Optional[str]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                           "params": [{"to": self._addr, "data": data}, "latest"]}).encode()
        for url in config.CHAINLINK_RPC_URLS:
            try:
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/json", "User-Agent": "curl/8"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.load(r).get("result")
            except Exception:
                continue
        return None

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                if self._decimals is None:
                    d = self._rpc(self._SEL_DECIMALS)
                    if d:
                        self._decimals = int(d, 16)
                if self._decimals is not None:
                    res = self._rpc(self._SEL_LATEST)
                    if res and len(res) >= 2 + 64 * 5:
                        w = [res[2 + i * 64:2 + (i + 1) * 64] for i in range(5)]
                        ans = int(w[1], 16)
                        if ans >= 2 ** 255:
                            ans -= 2 ** 256
                        updated = int(w[3], 16)
                        if ans > 0 and updated > 0:
                            self.current_price = ans / (10 ** self._decimals)
                            self._updated_at = float(updated)
            except Exception as exc:
                logger.debug(f"ChainlinkOnchainFeed[{self.asset}] poll error: {exc}")
            self._stop.wait(timeout=config.CHAINLINK_ONCHAIN_POLL_SECS)


class Oracle:
    """
    Settlement price layer. Primary = the EXACT Chainlink data-stream price (Polymarket
    RTDS) that settles these markets, so `.price` and the strike match Polymarket's "Price
    to Beat" exactly. If the RTDS socket is down, an ON-CHAIN Chainlink aggregator (Polygon)
    is the next-best strike anchor; the Coinbase/Binance blend is the last resort (and the
    high-frequency vol estimate + basis diagnostic).
    """

    def __init__(self, binance, asset: str = "BTC",
                 coinbase: Optional[CoinbaseFeed] = None,
                 chainlink: Optional[ChainlinkFeed] = None,
                 coinbase_weight: float = 0.6):
        self.asset = asset
        self._binance = binance
        self._coinbase = coinbase or CoinbaseFeed(asset)
        self._chainlink = chainlink or ChainlinkFeed(asset)
        self._chainlink_onchain = ChainlinkOnchainFeed(asset)
        self._w = coinbase_weight

    def start(self):
        self._coinbase.start()
        self._chainlink.start()
        self._chainlink_onchain.start()

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
    def chainlink_price(self) -> float:
        """The best available Chainlink price: RTDS Data-Streams when fresh, else on-chain."""
        if self._chainlink.fresh:
            return self._chainlink.current_price
        if self._chainlink_onchain.fresh:
            return self._chainlink_onchain.current_price
        return 0.0

    @property
    def strike_source(self) -> str:
        """Which feed `price` (and therefore the snapshotted strike) is coming from RIGHT NOW.
        'rtds' = exact Price to Beat; 'onchain' = ~0.5bp Chainlink anchor; 'proxy' = CEX ~4-5bp."""
        if self._chainlink.fresh:
            return "rtds"
        if self._chainlink_onchain.fresh:
            return "onchain"
        return "proxy"

    @property
    def rtds_connected(self) -> bool:
        """RTDS (exact Chainlink Data-Streams) socket connectivity — for the P0 dashboard panel."""
        return bool(self._chainlink.connected)

    @property
    def rtds_last_tick_age(self) -> Optional[float]:
        """Seconds since the last RTDS Chainlink price tick (None if none yet)."""
        return self._chainlink.last_tick_age

    @property
    def onchain_fresh(self) -> bool:
        """Whether the on-chain Chainlink fallback currently has a fresh reading."""
        return bool(self._chainlink_onchain.fresh)

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
        Settlement price, best source first:
          1. RTDS Chainlink Data Streams (the EXACT Price to Beat) when fresh,
          2. else the on-chain Chainlink aggregator (~0.5bp strike anchor) when fresh,
          3. else the high-frequency Coinbase-weighted CEX blend (~4-5bp proxy; σ-widened).
        Keeps the strike always captured at T=0 while preferring the lowest-basis source.
        """
        if self._chainlink.fresh:
            return self._chainlink.current_price
        if self._chainlink_onchain.fresh:
            return self._chainlink_onchain.current_price
        return self._cex_blend

    @property
    def cex_basis_bp(self) -> float:
        """
        Disagreement (bp) used to widen σ. When a Chainlink price (RTDS or on-chain) is fresh,
        |Chainlink − Coinbase| (the true settlement-vs-proxy gap); otherwise |Binance − Coinbase|.
        """
        cb = self._coinbase.current_price
        bn = self._binance.current_price
        cl = self.chainlink_price
        if cl > 0 and cb > 0:
            return abs(bp(cl, cb))
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
