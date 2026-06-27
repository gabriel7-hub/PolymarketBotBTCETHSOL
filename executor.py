"""
executor.py — Order placement via py_clob_client_v2.

Paper mode: logs the hypothetical trade, never submits to CLOB.
Live mode: signs + submits GTC maker or IOC taker orders.

Tracks open orders so they can be cancelled before window close.
"""

import time
from typing import Optional
from loguru import logger
import config
import state
import pricing
from signal_engine import Signal, Action
from market_discovery import MarketWindow

# py_clob_client_v2 is only required in live mode
try:
    from py_clob_client_v2 import ClobClient, OrderArgs, PartialCreateOrderOptions
    from py_clob_client_v2.clob_types import ApiCreds
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    _CLIENT_AVAILABLE = True
except ImportError:
    _CLIENT_AVAILABLE = False


def _tick(tick_size) -> float:
    """Parse a window's tick size (a string like '0.01') into a float, default 0.01."""
    try:
        t = float(tick_size)
        return t if t > 0 else 0.01
    except (TypeError, ValueError):
        return 0.01


class Executor:
    """One instance per asset (tracks that asset's active order/position only)."""

    def __init__(self, paper_mode: bool = True, asset: str = "BTC"):
        self.paper_mode = paper_mode
        self.asset = asset
        self._client: Optional[object] = None
        self._active_order_id: Optional[str] = None
        self._active_pos_id: Optional[int] = None
        self._active_trade_id: Optional[int] = None
        self._active_order_type: Optional[str] = None   # 'MAKER' | 'TAKER' of the open order

        if not paper_mode:
            self._init_client()

    # ─── Public API ────────────────────────────────────────────────────────────

    def execute(self, signal: Signal, window: MarketWindow, book=None) -> bool:
        """
        Execute the signal if it calls for a trade.
        Returns True if an order was placed/simulated.
        `book` (the live PolymarketBook) enables depth-aware paper fills; optional so
        callers/tests without a book fall back to the optimistic best-ask fill.
        """
        if signal.action not in (
            Action.POST_MAKER_UP, Action.POST_MAKER_DOWN,
            Action.IOC_UP, Action.IOC_DOWN,
        ):
            return False

        side      = signal.order_side      # 'UP' or 'DOWN'
        price     = signal.order_price
        order_type = signal.order_type     # 'MAKER' or 'TAKER'
        size_usdc = config.MAX_STAKE_PER_MARKET
        token_id  = window.up_token_id if side == "UP" else window.down_token_id

        if self.paper_mode:
            return self._paper_execute(signal, window, side, price, size_usdc,
                                       order_type, book)
        else:
            return self._live_execute(signal, window, side, price, size_usdc,
                                      order_type, token_id)

    def execute_arb(self, signal, window) -> bool:
        """
        YES/NO pair arbitrage: buy UP+DOWN for a guaranteed $1 payout. Risk-free.
        Paper mode books the locked profit immediately.
        """
        up_ask, down_ask = signal.up_ask, signal.down_ask
        if up_ask is None or down_ask is None:
            return False
        cost_per_pair = up_ask + down_ask
        pairs = signal.farm_size / cost_per_pair if cost_per_pair > 0 else 0.0
        profit = signal.arb_edge * pairs
        if self.paper_mode:
            state.add_arb_pnl(profit)
            state.record_trade({
                "asset": self.asset,
                "market_id": window.condition_id, "start_ts": int(window.start_ts),
                "leg": "ARB", "side": "PAIR", "price": round(cost_per_pair, 4),
                "detail": f"UP@{up_ask:.2f}+DOWN@{down_ask:.2f}", "size_usdc": signal.farm_size,
                "pnl_usdc": round(profit, 4), "status": "LOCKED", "outcome": "ARB",
                "closed_at": time.time(),
            })
            logger.info(
                f"[PAPER][ARB][{self.asset}] buy UP@{up_ask:.2f}+DOWN@{down_ask:.2f}="
                f"{cost_per_pair:.3f} | {pairs:.0f} pairs | locked +${profit:.2f}"
            )
            return True
        logger.warning("Live ARB execution not implemented this round")
        return False

    def run_farm(self, signal, window, dt: float) -> float:
        """
        Two-sided reward farm: maintain quotes near mid on both tokens, delta-neutral.
        Paper mode accrues the estimated reward yield for the elapsed dt seconds into a
        single per-window ledger row.
        Returns the reward accrued this tick.
        """
        accrued = signal.est_reward_per_sec * max(0.0, dt)
        if self.paper_mode:
            if accrued > 0:
                state.add_reward(accrued)
                detail = f"UP@{signal.farm_up_px} / DOWN@{signal.farm_down_px}"
                state.record_farm_accrual(
                    int(window.start_ts), window.condition_id, "TWO-SIDED",
                    detail, signal.farm_size, accrued, asset=self.asset,
                )
            return accrued
        logger.warning("Live FARM execution not implemented this round")
        return 0.0

    def box_position(self, window: MarketWindow, pos: dict, opp_ask: float,
                     book=None) -> Optional[float]:
        """
        Hedge-to-box stop-loss: buy the OPPOSITE side of the open taker position, share
        for share, so the pair redeems a guaranteed $1 regardless of outcome. Locks a
        small defined loss (occasionally a gain) instead of riding a flipped signal to a
        full-stake loss. Returns the locked P&L, or None if the box did NOT execute
        (order failed, or — under fill realism — the opposite book can't absorb the full
        hedge cheaply enough, in which case the position rides to natural resolution).
        """
        side      = pos["side"]
        opp_side  = "DOWN" if side == "UP" else "UP"
        entry     = pos["entry_price"]
        size_usdc = pos["size_usdc"]
        shares    = size_usdc / entry

        # ── Conservative fill realism: price the hedge against the REAL opposite-ask
        # depth (VWAP) + a latency tick. The box locks profit by lifting the cheap tail,
        # exactly where depth is thinnest — so if we can't fill the full hedge within
        # BOX_MAX_FILL_SLIPPAGE of the touch, we DON'T box and let the position ride.
        hedge_px = opp_ask
        if config.PAPER_FILL_REALISM and book is not None and self.paper_mode:
            tick = _tick(getattr(window, "tick_size", "0.01"))
            filled_shares, vwap = book.fill_ask(opp_side, shares)
            if filled_shares + 1e-6 < shares:
                logger.info(f"[PAPER][BOX][{self.asset}] skip — {opp_side} depth only "
                            f"{filled_shares:.1f}/{shares:.1f}sh; position rides to resolution")
                return None
            hedge_px = min(0.99, vwap + config.PAPER_SLIPPAGE_TICKS * tick)
            if hedge_px > opp_ask + config.BOX_MAX_FILL_SLIPPAGE:
                logger.info(f"[PAPER][BOX][{self.asset}] skip — hedge VWAP {hedge_px:.3f} > "
                            f"ask {opp_ask:.3f}+{config.BOX_MAX_FILL_SLIPPAGE:.2f}; rides")
                return None

        hedge_usdc = shares * hedge_px

        # Locked P&L: $1/pair redemption minus both legs' cost and taker fees.
        pnl = (shares
               - size_usdc - shares * pricing.taker_fee_per_share(entry)
               - hedge_usdc - shares * pricing.taker_fee_per_share(hedge_px))

        if not self.paper_mode:
            token_id = window.up_token_id if opp_side == "UP" else window.down_token_id
            try:
                options = PartialCreateOrderOptions(
                    tick_size=window.tick_size, neg_risk=window.neg_risk,
                    time_in_force="IOC",
                )
                self._client.create_and_post_order(
                    # OrderArgs.size is SHARES (outcome tokens), not USDC — hedge the same
                    # share count as the position so the pair redeems $1.
                    OrderArgs(token_id=token_id, price=opp_ask, size=round(shares, 2), side=BUY),
                    options=options,
                )
            except Exception as exc:
                logger.error(f"Box hedge order failed: {exc}")
                return None

        state.close_position(pos["id"], hedge_px, round(pnl, 4), 0.0, "BOXED")
        state.resolve_taker_ledger(pos["market_id"], "BOXED", round(pnl, 4))
        state.record_trade({
            "asset": self.asset,
            "market_id": pos["market_id"], "start_ts": int(window.start_ts),
            "leg": "BOX", "side": opp_side, "price": round(hedge_px, 4),
            "detail": f"box {side}@{entry:.3f} + {opp_side}@{hedge_px:.3f} "
                      f"({shares:.1f}sh, locked {pnl:+.2f})",
            "size_usdc": round(hedge_usdc, 2), "pnl_usdc": 0.0,
            "status": "RESOLVED", "outcome": "BOXED", "closed_at": time.time(),
        })
        logger.info(
            f"[{'PAPER' if self.paper_mode else 'LIVE'}][BOX][{self.asset}] {side}@{entry:.3f} "
            f"hedged with {opp_side}@{hedge_px:.3f} | locked pnl={pnl:+.2f}"
        )
        self._active_order_id   = None
        self._active_pos_id     = None
        self._active_order_type = None
        return pnl

    def _ledger_open_taker(self, window, side, price, size_usdc) -> int:
        """Append a TAKER fill to the audit ledger (OPEN until the window resolves)."""
        return state.record_trade({
            "asset": self.asset,
            "market_id": window.condition_id, "start_ts": int(window.start_ts),
            "leg": "TAKER", "side": side, "price": price,
            "detail": f"{side}@{price:.3f}", "size_usdc": size_usdc,
            "pnl_usdc": 0.0, "status": "OPEN", "outcome": None,
        })

    def cancel_open_order(self):
        """
        Cancel any unfilled MAKER quote from the current window.

        A filled TAKER (IOC) is a held directional position, NOT a resting quote — it
        must be carried to settlement so it resolves WIN/LOSS. Cancelling it (the old
        behaviour) wiped every taker bet at T-30s, so paper P&L never accrued and it
        looked like the bot "never traded". Leave takers alone; they close in
        on_market_resolved().
        """
        if self._active_order_type == "TAKER":
            return
        if self._active_order_id and not self.paper_mode:
            try:
                self._client.cancel_order(order_id=self._active_order_id)
                logger.info(f"Cancelled open maker order {self._active_order_id}")
            except Exception as exc:
                logger.warning(f"Cancel failed: {exc}")
        if self._active_pos_id:
            state.cancel_position(self._active_pos_id)
        self._active_order_id   = None
        self._active_pos_id     = None
        self._active_order_type = None

    def on_market_resolved(self, window: MarketWindow, winning_side: str):
        """
        Called when a window closes and we know the outcome.
        winning_side: 'UP' or 'DOWN'
        Resolves this asset's open position and records P&L.
        """
        pos = state.get_open_position(self.asset)
        if not pos:
            return

        entry_price = pos["entry_price"]
        size_usdc   = pos["size_usdc"]
        side        = pos["side"]
        pos_id      = pos["id"]

        won = (side == winning_side)
        shares = size_usdc / entry_price

        # Per-share fee/rebate from the single source of truth (pricing.py).
        if pos["order_type"] == "MAKER":
            fee_per_share = 0.0
            rebate = pricing.maker_rebate_per_share(entry_price) * shares
        else:  # TAKER pays the fee on entry
            fee_per_share = pricing.taker_fee_per_share(entry_price)
            rebate = 0.0

        if won:
            pnl = (1.0 - entry_price) * shares - fee_per_share * shares
            outcome = "WIN"
        else:
            pnl = -entry_price * shares - fee_per_share * shares
            outcome = "LOSS"

        state.close_position(pos_id, 1.0 if won else 0.0, pnl, rebate, outcome)
        state.resolve_taker_ledger(pos["market_id"], outcome, pnl)   # sync dashboard history
        logger.info(
            f"Resolved [{self.asset}]: {side} side {outcome} | "
            f"entry={entry_price:.3f} pnl={pnl:+.2f} rebate={rebate:.3f}"
        )

        self._active_order_id   = None
        self._active_pos_id     = None
        self._active_order_type = None

    # ─── Paper execution ───────────────────────────────────────────────────────

    def _paper_execute(self, signal: Signal, window: MarketWindow, side: str,
                       price: float, size_usdc: float, order_type: str,
                       book=None) -> bool:
        # ── Conservative fill realism for the directional TAKER leg ───────────────
        # Walk the REAL displayed ask depth (VWAP) instead of assuming our full stake
        # fills at the touch, and pay an extra adverse tick for snapshot→order latency.
        # A too-thin / empty book means no fill (we don't manufacture liquidity).
        if (config.PAPER_FILL_REALISM and book is not None
                and order_type == "TAKER" and price and price > 0):
            tick = _tick(getattr(window, "tick_size", "0.01"))
            want_shares = size_usdc / price
            filled_shares, vwap = book.fill_ask(side, want_shares)
            if filled_shares <= 0:
                logger.info(f"[PAPER][{self.asset}] {side} IOC unfilled — ask book empty/"
                            f"too thin (wanted {want_shares:.1f}sh) — skipped")
                return False
            fill_price = min(0.99, vwap + config.PAPER_SLIPPAGE_TICKS * tick)
            price = round(fill_price, 4)
            size_usdc = round(filled_shares * price, 2)   # actual cash deployed (may be partial)

        order_id = f"PAPER-{self.asset}-{int(signal.ts * 1000)}"
        pos_id = state.open_position({
            "asset":        self.asset,
            "market_id":    window.condition_id,
            "market_title": window.market_title,
            "side":         side,
            "entry_price":  price,
            "size_usdc":    size_usdc,
            "order_id":     order_id,
            "order_type":   order_type,
            "opened_at":    signal.ts,
        })
        self._active_order_id   = order_id
        self._active_pos_id     = pos_id
        self._active_order_type = order_type
        self._active_trade_id = self._ledger_open_taker(window, side, price, size_usdc)
        logger.info(
            f"[PAPER][{self.asset}] {order_type} {side} @ {price:.3f} | "
            f"size=${size_usdc} | P(side)={signal.p_up if side=='UP' else signal.p_down:.3f}"
        )
        return True

    # ─── Live execution ────────────────────────────────────────────────────────

    def execute_certainty_live(self, signal: Signal, window: MarketWindow,
                               side: str, ask: float, size_usdc: float) -> bool:
        """LIVE certainty / feed-lag order (live assets only). Places an IOC buy at the touch
        and opens a REAL position that settles through on_market_resolved() — i.e. it reuses
        the exact resolution + P&L + risk path as any taker fill. The ledger row is leg='TAKER'
        (so resolve_taker_ledger settles it) but the detail is tagged CERTAINTY. Returns True
        if the order was placed."""
        if self._active_pos_id is not None:
            return False                                   # one position per asset-window
        token_id = window.up_token_id if side == "UP" else window.down_token_id
        price = round(min(config.CERTAINTY_MAX_ASK, ask), 2)
        logger.warning(f"[LIVE·CERT][{self.asset}] {side} @ {price:.2f} | size=${size_usdc} "
                       f"| p={signal.p_up if side=='UP' else signal.p_down:.3f} "
                       f"T-{signal.time_remaining:.0f}s")
        return self._live_execute(signal, window, side, price, size_usdc, "TAKER", token_id)

    def _live_execute(self, signal: Signal, window: MarketWindow, side: str,
                      price: float, size_usdc: float, order_type: str,
                      token_id: str) -> bool:
        if not self._client:
            logger.error("CLOB client not initialised — cannot place live order")
            return False
        try:
            from py_clob_client_v2.order_builder.constants import BUY
            options = PartialCreateOrderOptions(
                tick_size=window.tick_size,
                neg_risk=window.neg_risk,
                **({"time_in_force": "IOC"} if order_type == "TAKER" else {}),
            )
            # Polymarket OrderArgs.size is the number of OUTCOME TOKENS (shares), NOT the USDC
            # notional. Deploy `size_usdc` dollars at `price` => shares = size_usdc / price.
            shares = round(size_usdc / price, 2) if price > 0 else 0.0
            if shares <= 0:
                logger.error(f"[LIVE][{self.asset}] computed 0 shares for ${size_usdc} @ {price}")
                return False
            resp = self._client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side=BUY),
                options=options,
            )
            order_id = resp.get("orderID") or resp.get("id") or ""
            pos_id = state.open_position({
                "asset":        self.asset,
                "market_id":    window.condition_id,
                "market_title": window.market_title,
                "side":         side,
                "entry_price":  price,
                "size_usdc":    size_usdc,
                "order_id":     order_id,
                "order_type":   order_type,
                "opened_at":    signal.ts,
            })
            self._active_order_id   = order_id
            self._active_pos_id     = pos_id
            self._active_order_type = order_type
            self._active_trade_id = self._ledger_open_taker(window, side, price, size_usdc)
            logger.info(
                f"[LIVE][{self.asset}] {order_type} {side} @ {price:.3f} | "
                f"size=${size_usdc} | order_id={order_id}"
            )
            return True
        except Exception as exc:
            logger.error(f"Order placement failed: {exc}")
            return False

    def _init_client(self):
        if not _CLIENT_AVAILABLE:
            raise ImportError(
                "py_clob_client_v2 is not installed. "
                "Run: pip install py-clob-client-v2"
            )
        from py_clob_client_v2.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=config.CLOB_API_KEY,
            api_secret=config.CLOB_API_SECRET,
            api_passphrase=config.CLOB_API_PASSPHRASE,
        )
        self._client = ClobClient(
            host=config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=137,
            creds=creds,
            signature_type=config.SIGNATURE_TYPE,
            funder=config.WALLET_ADDRESS,
        )
        logger.info(f"CLOB client initialised (live mode) | sig_type={config.SIGNATURE_TYPE} "
                    f"funder={config.WALLET_ADDRESS}")
