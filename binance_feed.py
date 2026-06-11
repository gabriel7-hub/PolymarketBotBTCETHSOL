"""
binance_feed.py — Real-time <asset>/USDT price feed from Binance aggTrade WebSocket.
One instance per asset (BTC/ETH/SOL), each on its own socket.

Maintains:
  - current_price     : most recent trade price
  - rolling_window    : last 60s of (timestamp, price) tuples
  - momentum_15s      : price change over last 15s in basis points
  - connected         : True when WebSocket is live
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


class BinanceFeed:

    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self._symbol = config.ASSET_PARAMS[asset]["binance_symbol"]
        self._url = f"{config.BINANCE_WS_BASE}/{self._symbol}@aggTrade"
        self.current_price: float = 0.0
        self.connected: bool = False

        # Rolling price history: deque of (ts_float, price_float)
        self._history: deque = deque(maxlen=3600)   # 1h of ticks
        self._lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stop = threading.Event()
        self._reconnect_delay = config.RECONNECT_BASE_DELAY

    # ─── Public API ────────────────────────────────────────────────────────────

    def start(self):
        t = threading.Thread(target=self._run_loop, daemon=True,
                             name=f"binance-{self.asset.lower()}")
        t.start()
        logger.info(f"BinanceFeed[{self.asset}] started ({self._symbol})")

    def stop(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    @property
    def momentum_15s(self) -> float:
        """15-second BTC momentum in basis points (positive = rising)."""
        with self._lock:
            now = time.time()
            cutoff = now - config.MOMENTUM_WINDOW_SECS
            history = [(ts, p) for ts, p in self._history if ts >= cutoff]
        if len(history) < 2:
            return 0.0
        oldest_price = history[0][1]
        return bp(self.current_price, oldest_price)

    @property
    def realized_vol_per_sec(self) -> float:
        """
        Robust realized per-second return vol over VOL_WINDOW_SECS (1s-grid resampled to
        suppress microstructure noise — see utils.realized_vol_per_sec). Feeds the model:
            σ_price(t) = S_now · realized_vol_per_sec · √t_remaining
        """
        with self._lock:
            hist = list(self._history)
        return _rv(hist, config.VOL_WINDOW_SECS, config.VOL_FLOOR_PER_SEC)

    @property
    def price_60s_ago(self) -> float:
        """BTC price ~60 seconds ago (for longer-window checks)."""
        with self._lock:
            cutoff = time.time() - 60
            old = [(ts, p) for ts, p in self._history if ts <= cutoff]
        return old[-1][1] if old else self.current_price

    def price_at_ref_or_now(self, ref_ts: float) -> float:
        """Return the price closest to ref_ts from history, or current."""
        with self._lock:
            best = min(self._history, key=lambda x: abs(x[0] - ref_ts), default=None)
        return best[1] if best else self.current_price

    # ─── WebSocket internals ───────────────────────────────────────────────────

    def _run_loop(self):
        while not self._stop.is_set():
            self._connect()
            if not self._stop.is_set():
                logger.warning(f"BinanceFeed[{self.asset}] disconnected. "
                               f"Reconnecting in {self._reconnect_delay}s")
                self._stop.wait(timeout=self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, config.RECONNECT_MAX_DELAY
                )

    def _connect(self):
        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        self.connected = True
        self._reconnect_delay = config.RECONNECT_BASE_DELAY
        logger.info(f"BinanceFeed[{self.asset}] connected")

    def _on_close(self, ws, code, msg):
        self.connected = False
        logger.info(f"BinanceFeed[{self.asset}] closed (code={code})")

    def _on_error(self, ws, error):
        self.connected = False
        logger.warning(f"BinanceFeed[{self.asset}] error: {error}")

    def _on_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)

            # Graceful shutdown signal from Binance (sent 10 min before close)
            if msg.get("e") == "serverShutdown":
                logger.warning("Binance WS serverShutdown received — reconnecting")
                ws.close()
                return

            # aggTrade event
            if msg.get("e") == "aggTrade":
                price = float(msg["p"])
                ts = msg["T"] / 1000.0   # ms → seconds
                self.current_price = price
                with self._lock:
                    self._history.append((ts, price))

        except Exception as exc:
            logger.warning(f"BinanceFeed message parse error: {exc}")
