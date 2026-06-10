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


class Executor:

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self._client: Optional[object] = None
        self._active_order_id: Optional[str] = None
        self._active_pos_id: Optional[int] = None
        self._active_trade_id: Optional[int] = None
        self._active_order_type: Optional[str] = None   # 'MAKER' | 'TAKER' of the open order

        if not paper_mode:
            self._init_client()

    # ─── Public API ────────────────────────────────────────────────────────────

    def execute(self, signal: Signal, window: MarketWindow) -> bool:
        """
        Execute the signal if it calls for a trade.
        Returns True if an order was placed/simulated.
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
            return self._paper_execute(signal, window, side, price, size_usdc, order_type)
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
                "market_id": window.condition_id, "start_ts": int(window.start_ts),
                "leg": "ARB", "side": "PAIR", "price": round(cost_per_pair, 4),
                "detail": f"UP@{up_ask:.2f}+DOWN@{down_ask:.2f}", "size_usdc": signal.farm_size,
                "pnl_usdc": round(profit, 4), "status": "LOCKED", "outcome": "ARB",
                "closed_at": time.time(),
            })
            logger.info(
                f"[PAPER][ARB] buy UP@{up_ask:.2f}+DOWN@{down_ask:.2f}="
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
                    detail, signal.farm_size, accrued,
                )
            return accrued
        logger.warning("Live FARM execution not implemented this round")
        return 0.0

    def box_position(self, window: MarketWindow, pos: dict, opp_ask: float) -> Optional[float]:
        """
        Hedge-to-box stop-loss: buy the OPPOSITE side of the open taker position, share
        for share, so the pair redeems a guaranteed $1 regardless of outcome. Locks a
        small defined loss (occasionally a gain) instead of riding a flipped signal to a
        full-stake loss. Returns the locked P&L, or None if the order failed.
        """
        side      = pos["side"]
        opp_side  = "DOWN" if side == "UP" else "UP"
        entry     = pos["entry_price"]
        size_usdc = pos["size_usdc"]
        shares    = size_usdc / entry
        hedge_usdc = shares * opp_ask

        # Locked P&L: $1/pair redemption minus both legs' cost and taker fees.
        pnl = (shares
               - size_usdc - shares * pricing.taker_fee_per_share(entry)
               - hedge_usdc - shares * pricing.taker_fee_per_share(opp_ask))

        if not self.paper_mode:
            token_id = window.up_token_id if opp_side == "UP" else window.down_token_id
            try:
                options = PartialCreateOrderOptions(
                    tick_size=window.tick_size, neg_risk=window.neg_risk,
                    time_in_force="IOC",
                )
                self._client.create_and_post_order(
                    OrderArgs(token_id=token_id, price=opp_ask, size=hedge_usdc, side=BUY),
                    options=options,
                )
            except Exception as exc:
                logger.error(f"Box hedge order failed: {exc}")
                return None

        state.close_position(pos["id"], opp_ask, round(pnl, 4), 0.0, "BOXED")
        state.resolve_taker_ledger(pos["market_id"], "BOXED", round(pnl, 4))
        state.record_trade({
            "market_id": pos["market_id"], "start_ts": int(window.start_ts),
            "leg": "BOX", "side": opp_side, "price": opp_ask,
            "detail": f"box {side}@{entry:.3f} + {opp_side}@{opp_ask:.3f} "
                      f"({shares:.1f}sh, locked {pnl:+.2f})",
            "size_usdc": round(hedge_usdc, 2), "pnl_usdc": 0.0,
            "status": "RESOLVED", "outcome": "BOXED", "closed_at": time.time(),
        })
        logger.info(
            f"[{'PAPER' if self.paper_mode else 'LIVE'}][BOX] {side}@{entry:.3f} hedged with "
            f"{opp_side}@{opp_ask:.3f} | locked pnl={pnl:+.2f}"
        )
        self._active_order_id   = None
        self._active_pos_id     = None
        self._active_order_type = None
        return pnl

    def _ledger_open_taker(self, window, side, price, size_usdc) -> int:
        """Append a TAKER fill to the audit ledger (OPEN until the window resolves)."""
        return state.record_trade({
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
        Resolves the open position and records P&L.
        """
        pos = state.get_open_position()
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
            f"Resolved: {side} side {outcome} | "
            f"entry={entry_price:.3f} pnl={pnl:+.2f} rebate={rebate:.3f}"
        )

        self._active_order_id   = None
        self._active_pos_id     = None
        self._active_order_type = None

    # ─── Paper execution ───────────────────────────────────────────────────────

    def _paper_execute(self, signal: Signal, window: MarketWindow, side: str,
                       price: float, size_usdc: float, order_type: str) -> bool:
        order_id = f"PAPER-{int(signal.ts * 1000)}"
        pos_id = state.open_position({
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
            f"[PAPER] {order_type} {side} @ {price:.3f} | "
            f"size=${size_usdc} | P(side)={signal.p_up if side=='UP' else signal.p_down:.3f}"
        )
        return True

    # ─── Live execution ────────────────────────────────────────────────────────

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
            resp = self._client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=size_usdc, side=BUY),
                options=options,
            )
            order_id = resp.get("orderID") or resp.get("id") or ""
            pos_id = state.open_position({
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
                f"[LIVE] {order_type} {side} @ {price:.3f} | "
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
            signature_type=2,
            funder=config.WALLET_ADDRESS,
        )
        logger.info("CLOB client initialised (live mode)")
