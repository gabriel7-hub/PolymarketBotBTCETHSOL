"""
polymarket_book.py — Polymarket CLOB WebSocket client.

Maintains a local order book for the currently active market's Up and Down tokens.
Sends PING every 10s to keep the connection alive.
Reconnects automatically on drop.
"""

import json
import time
import threading
from typing import Optional
import websocket
from loguru import logger
import config
from market_discovery import MarketWindow


class OrderBookSide:
    """Single-side order book (bids or asks). price → size. Thread-safe: the CLOB WS
    thread writes while the main loop reads, so all access is under a lock (otherwise
    max()/min() can raise 'dictionary changed size during iteration')."""

    def __init__(self):
        self._levels: dict[float, float] = {}
        self._lock = threading.Lock()

    def apply_snapshot(self, levels: list):
        """Replace book with snapshot: [{price: str, size: str}, ...]"""
        new = {
            round(float(lvl["price"]), 4): float(lvl["size"])
            for lvl in levels if float(lvl.get("size", 0)) > 0
        }
        with self._lock:
            self._levels = new

    def apply_delta(self, price: float, size: float):
        price = round(price, 4)
        with self._lock:
            if size <= 0:
                self._levels.pop(price, None)
            else:
                self._levels[price] = size

    @property
    def best_bid(self) -> Optional[float]:
        with self._lock:
            return max(self._levels) if self._levels else None

    @property
    def best_ask(self) -> Optional[float]:
        with self._lock:
            return min(self._levels) if self._levels else None

    def size_at(self, price: float) -> float:
        with self._lock:
            return self._levels.get(round(price, 4), 0.0)

    def vwap_buy(self, shares: float) -> tuple[float, float]:
        """
        Simulate a marketable BUY that lifts up to `shares` off this (ask) ladder,
        cheapest level first. Returns (filled_shares, avg_price). If the book is empty
        or too thin, filled_shares < shares (or 0). Used by paper execution so a fill is
        priced against real displayed depth instead of assuming infinite size at the touch.
        """
        if shares <= 0:
            return 0.0, 0.0
        with self._lock:
            levels = sorted(self._levels.items())   # (price, size) ascending price
        remaining = shares
        cost = 0.0
        for price, size in levels:
            take = min(remaining, size)
            cost += take * price
            remaining -= take
            if remaining <= 1e-9:
                break
        filled = shares - remaining
        if filled <= 0:
            return 0.0, 0.0
        return filled, cost / filled


class TokenBook:
    """Combined bid+ask book for one token (Up or Down)."""

    def __init__(self):
        self.bids = OrderBookSide()
        self.asks = OrderBookSide()
        self.last_update = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids.best_bid

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks.best_ask

    @property
    def spread(self) -> Optional[float]:
        bid = self.best_bid
        ask = self.best_ask
        if bid is not None and ask is not None:
            return round(ask - bid, 4)
        return None

    @property
    def mid(self) -> Optional[float]:
        bid = self.best_bid
        ask = self.best_ask
        if bid is not None and ask is not None:
            return round((bid + ask) / 2, 4)
        return None

    def apply_snapshot(self, bids: list, asks: list):
        self.bids.apply_snapshot(bids)
        self.asks.apply_snapshot(asks)
        self.last_update = time.time()

    def fill_ask(self, shares: float) -> tuple[float, float]:
        """Depth-aware marketable buy against this token's asks. See OrderBookSide.vwap_buy."""
        return self.asks.vwap_buy(shares)

    def apply_price_change(self, side: str, price: float, size: float):
        if side == "BID":
            self.bids.apply_delta(price, size)
        else:
            self.asks.apply_delta(price, size)
        self.last_update = time.time()


class PolymarketBook:
    """
    Manages a WebSocket connection to Polymarket's CLOB.
    Subscribes to the active market's Up and Down token books.
    Re-subscribes automatically when the market changes.
    One instance (one socket) per asset.
    """

    def __init__(self, asset: str = "BTC"):
        self.asset = asset
        self.up_book   = TokenBook()
        self.down_book = TokenBook()
        self.connected = False

        self._ws: Optional[websocket.WebSocketApp] = None
        self._subscribed_market: Optional[str] = None
        self._up_token_id: Optional[str] = None
        self._down_token_id: Optional[str] = None
        self._stop = threading.Event()
        self._reconnect_delay = config.RECONNECT_BASE_DELAY
        self._ping_thread: Optional[threading.Thread] = None

    # ─── Public API ────────────────────────────────────────────────────────────

    def start(self):
        t = threading.Thread(target=self._run_loop, daemon=True,
                             name=f"poly-book-{self.asset.lower()}")
        t.start()
        logger.info(f"PolymarketBook[{self.asset}] started")

    def stop(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def subscribe(self, window: MarketWindow):
        """Subscribe to a new market's books. Safe to call from any thread."""
        if self._subscribed_market == window.condition_id:
            return
        self._subscribed_market = window.condition_id
        # Store token ids NOW so _on_open re-subscribes to the right market.
        self._up_token_id   = window.up_token_id
        self._down_token_id = window.down_token_id
        # Reset books for new market
        self.up_book   = TokenBook()
        self.down_book = TokenBook()
        # Polymarket's market WS IGNORES a 2nd subscription on the same socket — it keeps
        # streaming the first market only. So we reconnect to get a fresh book snapshot;
        # _on_open re-subscribes to the (now updated) token ids.
        if self._ws and self.connected:
            self._reconnect_delay = config.RECONNECT_BASE_DELAY
            try:
                self._ws.close()
            except Exception:
                pass

    @property
    def up_ask(self) -> Optional[float]:
        return self.up_book.best_ask

    @property
    def down_ask(self) -> Optional[float]:
        return self.down_book.best_ask

    @property
    def up_bid(self) -> Optional[float]:
        return self.up_book.best_bid

    @property
    def down_bid(self) -> Optional[float]:
        return self.down_book.best_bid

    def fill_ask(self, side: str, shares: float) -> tuple[float, float]:
        """Depth-aware marketable buy of `shares` on the UP or DOWN token's asks.
        Returns (filled_shares, avg_price)."""
        book = self.up_book if side == "UP" else self.down_book
        return book.fill_ask(shares)

    @property
    def up_spread(self) -> Optional[float]:
        return self.up_book.spread

    @property
    def down_spread(self) -> Optional[float]:
        return self.down_book.spread

    # ─── WebSocket internals ───────────────────────────────────────────────────

    def _run_loop(self):
        while not self._stop.is_set():
            self._connect()
            if not self._stop.is_set():
                logger.warning(f"PolymarketBook[{self.asset}] disconnected. "
                               f"Reconnecting in {self._reconnect_delay}s")
                self.connected = False
                self._stop.wait(timeout=self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, config.RECONNECT_MAX_DELAY
                )

    def _connect(self):
        self._ws = websocket.WebSocketApp(
            config.POLYMARKET_BOOK_WS,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever()

    def _on_open(self, ws):
        self.connected = True
        self._reconnect_delay = config.RECONNECT_BASE_DELAY
        logger.info(f"PolymarketBook[{self.asset}] connected")
        # Re-subscribe to the active market after any (re)connect, else the book
        # silently stays empty until the next market change.
        if self._up_token_id and self._down_token_id:
            self.up_book   = TokenBook()
            self.down_book = TokenBook()
            self._send_subscription(self._up_token_id, self._down_token_id)
        # Start heartbeat thread
        self._ping_thread = threading.Thread(
            target=self._heartbeat, daemon=True, name=f"poly-ping-{self.asset.lower()}"
        )
        self._ping_thread.start()

    def _on_close(self, ws, code, msg):
        self.connected = False
        logger.info(f"PolymarketBook[{self.asset}] closed (code={code})")

    def _on_error(self, ws, error):
        self.connected = False
        logger.warning(f"PolymarketBook[{self.asset}] error: {error}")

    def _on_message(self, ws, raw: str):
        # Polymarket replies to our PING with a plain "PONG" string (not JSON).
        if not raw or raw[0] not in "[{":
            return
        try:
            events = json.loads(raw)
            if not isinstance(events, list):
                events = [events]
            for evt in events:
                self._handle_event(evt)
        except Exception as exc:
            logger.warning(f"PolymarketBook parse error: {exc}")

    def _handle_event(self, evt: dict):
        evt_type = evt.get("event_type") or evt.get("type") or ""

        if evt_type == "book":
            token_id = evt.get("asset_id") or evt.get("market") or ""
            book = self._book_for_token(token_id)
            if book:
                book.apply_snapshot(
                    bids=evt.get("bids", []),
                    asks=evt.get("asks", []),
                )

        elif evt_type == "price_change":
            token_id = evt.get("asset_id") or evt.get("market") or ""
            book = self._book_for_token(token_id)
            if book:
                changes = evt.get("changes", [])
                for ch in changes:
                    book.apply_price_change(
                        side=ch.get("side", "").upper(),
                        price=float(ch.get("price", 0)),
                        size=float(ch.get("size", 0)),
                    )

        elif evt_type in ("last_trade_price", "tick_size_change", "PONG"):
            pass  # not needed for this strategy

    def _book_for_token(self, token_id: str) -> Optional[TokenBook]:
        """Return the Up or Down book matching this token ID."""
        # We need to know which is up/down — stored at subscription time
        if token_id == self._up_token_id:
            return self.up_book
        if token_id == self._down_token_id:
            return self.down_book
        return None

    def _send_subscription(self, up_token_id: str, down_token_id: str):
        self._up_token_id   = up_token_id
        self._down_token_id = down_token_id
        msg = {
            "auth": {},
            "type": "Market",
            "markets": [],
            "assets_ids": [up_token_id, down_token_id],
        }
        try:
            self._ws.send(json.dumps(msg))
            logger.info(f"PolymarketBook[{self.asset}] subscribed to "
                        f"{up_token_id[:12]}… / {down_token_id[:12]}…")
        except Exception as exc:
            logger.warning(f"PolymarketBook[{self.asset}] subscription send failed: {exc}")

    def _heartbeat(self):
        while self.connected and not self._stop.is_set():
            try:
                if self._ws:
                    self._ws.send(json.dumps({"type": "PING"}))
            except Exception:
                break
            self._stop.wait(timeout=config.POLYMARKET_PING_INTERVAL)
