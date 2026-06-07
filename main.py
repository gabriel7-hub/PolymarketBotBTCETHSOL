"""
main.py — Entry point for the BTC 5-min Polymarket bot.

Usage:
    python main.py --mode paper          # paper trade (default)
    python main.py --mode live           # live CLOB orders
    python main.py --mode paper --no-dashboard   # suppress dashboard server
"""

import argparse
import time
import threading
from loguru import logger

import config
import state
from utils import setup_logging
from market_discovery import MarketDiscovery, current_window_start
from binance_feed import BinanceFeed
from oracle_feed import Oracle
from polymarket_book import PolymarketBook
from signal_engine import SignalEngine, Action
from executor import Executor
from risk import RiskGuard


def parse_args():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-min bot")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="paper = simulate trades; live = real CLOB orders")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Do not start the WebSocket dashboard server")
    return parser.parse_args()


def start_dashboard_server(bot_state_fn):
    """Single aiohttp server serves both the page and the live WS feed on one port."""
    try:
        from dashboard_server import DashboardServer
        server = DashboardServer(state_fn=bot_state_fn)
        t = threading.Thread(target=server.run, daemon=True, name="dashboard")
        t.start()
    except Exception as exc:
        logger.warning(f"Dashboard server could not start: {exc}")


class BotRunner:

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        state.init_db()   # must precede RiskGuard, which reads daily P&L
        healed = state.reconcile_taker_ledger()       # backfill resolved/cancelled takers in history
        if healed:
            logger.info(f"Reconciled {healed} TAKER ledger row(s) with resolved outcomes")
        pruned = state.prune_old_data(config.TICK_RETENTION_DAYS)  # bound DB growth on VPS
        if pruned:
            logger.info(f"Pruned {pruned} old tick/signal row(s) (> {config.TICK_RETENTION_DAYS}d)")

        self.discovery = MarketDiscovery()
        self.binance   = BinanceFeed()
        self.oracle    = Oracle(self.binance)         # owns a CoinbaseFeed
        self.book      = PolymarketBook()
        self.engine    = SignalEngine(self.oracle, self.binance, self.book)
        self.executor  = Executor(paper_mode=paper_mode)
        self.risk      = RiskGuard(paper_mode=paper_mode)

        self._dash_state: dict = {}
        self._last_signal = None
        self._current_window_id: str = ""
        self._refs: dict[int, float] = {}        # start_ts -> snapshotted strike
        self._missed: set[int] = set()           # windows we caught too late to strike
        self._ref_lock = threading.Lock()        # guards _refs/_missed (strike thread + main loop)
        self._pending: dict[int, str] = {}       # start_ts -> condition_id awaiting resolution
        self._settles: dict[int, float] = {}     # start_ts -> oracle price snapshotted at close
        self._awaiting_real: dict[int, str] = {} # fallback-settled; still chasing REAL outcome
        self._last_real_poll: float = 0.0        # throttle for the background REAL-outcome poll
        self._resolved: set[int] = set()
        self._arbed: set[int] = set()            # windows we already arbed
        self._last_tick_ts: float = time.time()
        self._farm_reward_session: float = 0.0   # est reward accrued this session
        self._last_farm: dict = {}               # last farm quote details for dashboard
        self._stop = threading.Event()
        self._readopt_open_positions()           # resume (not cancel) in-flight positions

    def _readopt_open_positions(self):
        """
        Re-adopt OPEN positions left by a previous run and queue them for resolution,
        instead of cancelling them. Cancelling on restart killed in-flight takers so they
        never got a WIN/LOSS and their history row stayed OPEN. We restore the strike +
        settle from the outcomes table so the fallback can settle them too.
        """
        adopted = 0
        for pos in state.get_open_positions():
            start_ts = int(pos["opened_at"] // config.MARKET_WINDOW_SECS) * config.MARKET_WINDOW_SECS
            self._pending[start_ts] = pos["market_id"]
            o = state.get_outcome(start_ts)
            if o:
                if o.get("ref_price"):
                    self._refs[start_ts] = o["ref_price"]
                if o.get("settle_price"):
                    self._settles[start_ts] = o["settle_price"]
            adopted += 1
        if adopted:
            logger.info(f"Re-adopted {adopted} open position(s) for resolution (not cancelled)")
        # Re-check recent oracle-FALLBACK settlements against the REAL Chainlink outcome and
        # correct any the cross-venue basis got wrong on borderline windows.
        for start_ts, mkt in state.get_recent_fallback_windows(limit=20):
            if start_ts not in self._resolved:
                self._awaiting_real[start_ts] = mkt

    def start(self):
        self.discovery.start()
        self.binance.start()
        self.oracle.start()
        self.book.start()
        self._start_strike_thread()   # snapshot strikes independent of main-loop stalls
        logger.info(
            f"Bot started in {'PAPER' if self.paper_mode else 'LIVE'} mode. "
            f"Waiting for window and feeds..."
        )
        self._main_loop()

    def _start_strike_thread(self):
        t = threading.Thread(target=self._strike_loop, daemon=True, name="strike-snapshot")
        t.start()

    def _strike_loop(self):
        """
        Snapshot the strike at the 300s boundary at high frequency, *independent* of the
        1s trading loop. The main loop can stall for many seconds on blocking network
        work (Gamma poll / resolution-fetch retry backoff), which used to push the
        snapshot past REFERENCE_MAX_LAG and flag every window MISSED → no trades. This
        dedicated ticker only reads the async-updated oracle price, so it reliably
        catches the boundary as long as the price feed is alive at T=0.
        """
        while not self._stop.is_set():
            try:
                self._snapshot_reference(current_window_start())
            except Exception as exc:
                logger.error(f"Strike snapshot error: {exc}")
            self._stop.wait(0.25)

    def get_dashboard_state(self) -> dict:
        return self._dash_state

    # ─── Main loop ─────────────────────────────────────────────────────────────

    def _main_loop(self):
        last_executed_window = ""
        no_market_ticks = 0

        while not self._stop.is_set():
            try:
                # The strike is snapshotted by a dedicated high-frequency thread (see
                # _strike_loop) so it is never missed when this loop stalls on network IO.
                self._retry_pending_resolutions()

                window = self.discovery.current
                if not window or not window.is_active:
                    no_market_ticks += 1
                    if no_market_ticks % 30 == 0:
                        logger.info("Waiting for active BTC 5-min window...")
                    self._update_dash_state(window=None)
                    time.sleep(1)
                    continue
                no_market_ticks = 0

                start_ts = int(window.start_ts)

                # New window: subscribe to its book.
                if window.condition_id != self._current_window_id:
                    self._current_window_id = window.condition_id
                    self.book.subscribe(window)
                    with self._ref_lock:
                        ref = self._refs.get(start_ts, 0.0)
                        missed = start_ts in self._missed
                    logger.info(
                        f"New window: {window.market_title} | "
                        f"ref={ref:.2f}{' (MISSED strike)' if missed else ''} | "
                        f"T-{window.time_remaining:.0f}s"
                    )

                window.reference_price = self._get_ref(start_ts)

                # Cancel stale maker quotes near close.
                if window.time_remaining <= config.CANCEL_OPEN_AT:
                    self.executor.cancel_open_order()

                signal = self.engine.evaluate(window)
                self._last_signal = signal
                self._record_signal(signal)
                self._record_tick(signal, window)

                # ── Route the signal to the right execution leg ───────────────
                now = time.time()
                dt = now - self._last_tick_ts
                self._last_tick_ts = now

                if signal.action == Action.ARB_PAIR:
                    # Risk-free; bypass the directional position guard, once per window.
                    if start_ts not in self._arbed:
                        if self.executor.execute_arb(signal, window):
                            self._arbed.add(start_ts)
                elif signal.action == Action.POST_FARM:
                    # Delta-neutral; accrue reward each tick while quotes are live.
                    accrued = self.executor.run_farm(signal, window, dt)
                    self._farm_reward_session += accrued
                    self._last_farm = {
                        "up_px": signal.farm_up_px, "down_px": signal.farm_down_px,
                        "size": signal.farm_size, "per_sec": signal.est_reward_per_sec,
                    }
                elif signal.action in (Action.IOC_UP, Action.IOC_DOWN):
                    allowed, _ = self.risk.check()
                    if allowed and window.condition_id != last_executed_window:
                        if self.executor.execute(signal, window):
                            last_executed_window = window.condition_id
                            # Guarantee this taker is resolved even if the loop stalls
                            # through the close tick (else it stays OPEN and blocks the
                            # open-position guard forever).
                            if start_ts not in self._resolved:
                                self._pending[start_ts] = window.condition_id

                # Window closing: queue for resolution and snapshot the settle price
                # (oracle price at close) — used as the paper fallback if the real
                # on-chain outcome never arrives.
                if window.time_remaining < 2 and start_ts not in self._resolved:
                    self._pending[start_ts] = window.condition_id
                    if start_ts not in self._settles and self.oracle.price > 0:
                        self._settles[start_ts] = self.oracle.price

                self._update_dash_state(window=window, signal=signal)
                time.sleep(1)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self.executor.cancel_open_order()
                break
            except Exception as exc:
                logger.error(f"Main loop error: {exc}", exc_info=True)
                time.sleep(2)

    # ─── Reference snapshot ─────────────────────────────────────────────────────

    def _get_ref(self, start_ts: int) -> float:
        with self._ref_lock:
            return self._refs.get(start_ts, 0.0)

    def _snapshot_reference(self, start_ts: int):
        """
        Snapshot the Chainlink-proxy strike at the window's open. Only trusted within
        REFERENCE_MAX_LAG of T=0; if we are already past that (e.g. just started up, or
        missed the boundary) the window is flagged MISSED and never traded — a wrong
        strike flips the favoured side and manufactures phantom edge.

        Runs on the dedicated strike thread (~4x/sec). We only flag a window MISSED once
        the price feed has had a real chance: a missing/zero price inside the lag window
        is left undecided so a feed that connects within REFERENCE_MAX_LAG still strikes.
        """
        with self._ref_lock:
            if start_ts in self._refs or start_ts in self._missed:
                return
            lag = time.time() - start_ts
            px = self.oracle.price
            if px and px > 0 and lag <= config.REFERENCE_MAX_LAG:
                self._refs[start_ts] = px
                logger.debug(f"Reference snapshot for {start_ts}: {px:.2f} (lag {lag:.1f}s)")
            elif lag > config.REFERENCE_MAX_LAG:
                reason = "no price feed" if not (px and px > 0) else f"lag {lag:.1f}s"
                self._missed.add(start_ts)
                logger.debug(f"Window {start_ts} strike MISSED ({reason}) — will not trade")

    # ─── Resolution (real outcome from Polymarket) ──────────────────────────────

    def _retry_pending_resolutions(self):
        # ── Pass 1: settle POSITIONS quickly at close so they don't carry into the next
        #            window. Prefer the real outcome if it's already available; otherwise
        #            (paper) settle on our oracle after a short grace and keep chasing REAL.
        for start_ts, condition_id in list(self._pending.items()):
            closed_at = start_ts + config.MARKET_WINDOW_SECS
            if time.time() < closed_at:
                continue
            winning = self.discovery.fetch_resolution(start_ts)
            ref = self._get_ref(start_ts)
            settle = self._settles.get(start_ts) or self.oracle.price
            predicted = "UP" if (ref and settle >= ref) else "DOWN"
            source = "REAL"

            if winning is None:
                grace_done = time.time() - closed_at >= config.RESOLUTION_FALLBACK_SECS
                if self.paper_mode and ref and grace_done:
                    winning, source = predicted, "FALLBACK"
                    self._awaiting_real[start_ts] = condition_id   # upgrade to REAL later
                elif time.time() - closed_at >= config.RESOLUTION_GIVEUP_SECS:
                    pos = state.get_open_position()
                    if pos and pos["market_id"] == condition_id:
                        state.cancel_position(pos["id"])
                        state.resolve_taker_ledger(condition_id, "VOID", 0.0)
                        logger.warning(f"Window {start_ts} unresolvable — position VOIDed")
                    self._pending.pop(start_ts, None)
                    self._settles.pop(start_ts, None)
                    self._resolved.add(start_ts)
                    continue
                else:
                    state.upsert_outcome({
                        "start_ts": start_ts, "market_id": condition_id,
                        "ref_price": ref, "settle_price": settle,
                        "winning_side": None, "predicted_side": predicted,
                        "resolved_at": None, "resolution_source": None,
                    })
                    continue

            state.upsert_outcome({
                "start_ts": start_ts, "market_id": condition_id,
                "ref_price": ref, "settle_price": settle,
                "winning_side": winning, "predicted_side": predicted,
                "resolved_at": time.time(), "resolution_source": source,
            })
            self._resolve_position(condition_id, winning)
            if source == "FALLBACK":
                logger.info(f"Window {start_ts} settled (paper, oracle): {winning} "
                            f"— chasing real outcome in background")
            self._pending.pop(start_ts, None)
            self._settles.pop(start_ts, None)
            self._resolved.add(start_ts)

        # ── Pass 2: the REAL Polymarket outcome lands ~minutes after close. Poll for it
        #            (throttled) and upgrade the calibration record — without touching the
        #            already-settled position. Keeps backtest data on REAL outcomes.
        if self._awaiting_real and time.time() - self._last_real_poll >= config.RESOLUTION_REAL_POLL_SECS:
            self._last_real_poll = time.time()
            fetched = 0
            for start_ts, condition_id in list(self._awaiting_real.items()):
                if time.time() - (start_ts + config.MARKET_WINDOW_SECS) > config.RESOLUTION_GIVEUP_SECS:
                    self._awaiting_real.pop(start_ts, None)
                    continue
                if fetched >= config.RESOLUTION_MAX_FETCH_PER_CYCLE:
                    break   # bound main-loop stall on slow/flaky VPS network
                fetched += 1
                winning = self.discovery.fetch_resolution(start_ts)
                if winning is None:
                    continue
                o = state.get_outcome(start_ts) or {}
                state.upsert_outcome({
                    "start_ts": start_ts, "market_id": condition_id,
                    "ref_price": o.get("ref_price") or self._get_ref(start_ts),
                    "settle_price": o.get("settle_price"),
                    "winning_side": winning, "predicted_side": o.get("predicted_side"),
                    "resolved_at": time.time(), "resolution_source": "REAL",
                })
                self._awaiting_real.pop(start_ts, None)
                # Correct the position if our fast proxy settle disagreed with the REAL
                # Chainlink outcome (e.g. a borderline window the CEX basis flipped).
                self._resettle_to_real(condition_id, winning)
                logger.debug(f"Window {start_ts} REAL outcome captured: {winning} (calibration)")

    def _resettle_to_real(self, condition_id: str, winning: str):
        import pricing
        pos = state.get_position_by_market(condition_id)
        if not pos or pos.get("outcome") not in ("WIN", "LOSS"):
            return
        correct = "WIN" if pos["side"] == winning else "LOSS"
        if correct == pos["outcome"]:
            return
        entry = pos["entry_price"]; shares = pos["size_usdc"] / entry
        fee = pricing.taker_fee_per_share(entry) if pos["order_type"] == "TAKER" else 0.0
        new_pnl = ((1.0 - entry) if correct == "WIN" else -entry) * shares - fee * shares
        if state.resettle_position(pos["id"], correct, new_pnl):
            state.update_taker_ledger(condition_id, correct, new_pnl)
            logger.info(f"Corrected {condition_id} to REAL outcome {winning}: "
                        f"{pos['outcome']}→{correct} pnl={new_pnl:+.2f}")

    def _resolve_position(self, condition_id: str, winning_side: str):
        """Resolve the open position ONLY if it belongs to the window that resolved."""
        pos = state.get_open_position()
        if not pos or pos["market_id"] != condition_id:
            return
        window = self.discovery.current
        self.executor.on_market_resolved(window, winning_side)
        recent = state.get_recent_trades(limit=1)
        if recent and recent[0]["outcome"] == "WIN":
            self.risk.on_win()
        elif recent and recent[0]["outcome"] == "LOSS":
            self.risk.on_loss()
        else:
            self.risk.on_push()

    # ─── Persistence helpers ────────────────────────────────────────────────────

    def _record_signal(self, s):
        state.insert_signal({
            "ts": s.ts, "market_id": s.market_id, "btc_ref": s.btc_ref,
            "btc_now": s.btc_now, "distance_bp": s.distance_bp,
            "momentum_bp": s.momentum_bp, "time_remaining": int(s.time_remaining),
            "p_up": s.p_up, "p_down": s.p_down, "up_ask": s.up_ask,
            "down_ask": s.down_ask, "edge_up": s.edge_up, "edge_down": s.edge_down,
            "action": s.action, "reason": s.reason, "phase": s.phase,
        })

    def _record_tick(self, s, window):
        state.insert_tick({
            "ts": s.ts, "market_id": s.market_id, "start_ts": int(window.start_ts),
            "t_remaining": s.time_remaining, "binance_price": self.binance.current_price,
            "oracle_price": s.btc_now, "cex_basis_bp": self.oracle.cex_basis_bp,
            "realized_vol": self.oracle.realized_vol_per_sec, "ref_price": s.btc_ref,
            "momentum_bp": s.momentum_bp, "p_up": s.p_up, "sigma_price": s.sigma_price,
            "up_bid": s.up_bid, "up_ask": s.up_ask, "down_bid": s.down_bid,
            "down_ask": s.down_ask, "ev_up": s.ev_up, "ev_down": s.ev_down,
            "action": s.action, "mode": s.mode,
        })

    # ─── Dashboard state ────────────────────────────────────────────────────────

    def _strike_status(self, window) -> str:
        """CAPTURED / MISSED / PENDING — lets the dashboard show strike-thread health."""
        if not window:
            return "NONE"
        sts = int(window.start_ts)
        with self._ref_lock:
            if sts in self._refs:
                return "CAPTURED"
            if sts in self._missed:
                return "MISSED"
        return "PENDING"

    def _update_dash_state(self, window=None, signal=None):
        daily = state.get_daily_stats()
        trades = daily.get("trades", 0)
        win_rate = daily.get("wins", 0) / trades if trades > 0 else 0.0

        self._dash_state = {
            "ts": time.time(),
            "bot": {
                "mode": "PAPER" if self.paper_mode else "LIVE",
                "status": self.risk.status_str(),
                "daily_pnl": round(daily.get("net_pnl", 0.0), 2),
                "daily_trades": trades,
                "win_rate": round(win_rate, 3),
                "rebates_today": round(daily.get("rebates", 0.0), 3),
            },
            "market": {
                "title": window.market_title if window else None,
                "condition_id": window.condition_id if window else None,
                "time_remaining": round(window.time_remaining, 1) if window else None,
                "phase": self.engine._phase(window.time_remaining) if window else None,
                "reference_price": window.reference_price if window else None,
                "strike_status": self._strike_status(window),
                "rewards_max_spread": window.rewards_max_spread if window else None,
                "rewards_min_size": window.rewards_min_size if window else None,
            },
            "btc": {
                "price": round(self.oracle.price, 2),
                "chainlink": round(self.oracle.chainlink.current_price, 2),
                "binance": round(self.binance.current_price, 2),
                "coinbase": round(self.oracle.coinbase.current_price, 2),
                "basis_bp": round(self.oracle.cex_basis_bp, 2),
                "momentum_15s": round(self.binance.momentum_15s, 2),
                "distance_bp": round(signal.distance_bp, 2) if signal else None,
                "connected": self.oracle.connected,
            },
            "book": {
                "up_bid": self.book.up_bid, "up_ask": self.book.up_ask,
                "down_bid": self.book.down_bid, "down_ask": self.book.down_ask,
                "spread": self.book.up_spread, "connected": self.book.connected,
            },
            "signal": {
                "p_up": signal.p_up if signal else None,
                "p_down": signal.p_down if signal else None,
                "ev_up": signal.ev_up if signal else None,
                "ev_down": signal.ev_down if signal else None,
                "arb_edge": signal.arb_edge if signal else None,
                "mid": signal.mid if signal else None,
                "sigma_price": signal.sigma_price if signal else None,
                "action": signal.action if signal else None,
                "mode": signal.mode if signal else None,
                "reason": signal.reason if signal else None,
                "phase": signal.phase if signal else None,
            },
            "strategy": {
                "farm_up_px": self._last_farm.get("up_px"),
                "farm_down_px": self._last_farm.get("down_px"),
                "farm_size": self._last_farm.get("size"),
                "farm_reward_per_sec": self._last_farm.get("per_sec"),
                "farm_reward_session": round(self._farm_reward_session, 4),
                "arbs_done": len(self._arbed),
                "active": (signal.mode if signal else "NONE"),
            },
            "position": state.get_open_position(),
            "recent_trades": state.get_recent_trades(limit=20),
            "ledger": state.get_recent_ledger(limit=25),
        }


def main():
    args = parse_args()
    setup_logging()
    paper_mode = (args.mode == "paper")

    if paper_mode:
        logger.info("=" * 60)
        logger.info("  PAPER MODE — no real orders will be placed")
        logger.info("=" * 60)
    else:
        logger.warning("=" * 60)
        logger.warning("  LIVE MODE — real USDC will be at risk")
        logger.warning("=" * 60)
        if not config.PRIVATE_KEY or not config.CLOB_API_KEY:
            logger.error("PRIVATE_KEY and CLOB_API_KEY must be set in .env for live mode")
            return

    runner = BotRunner(paper_mode=paper_mode)
    if not args.no_dashboard:
        start_dashboard_server(runner.get_dashboard_state)
    runner.start()


if __name__ == "__main__":
    main()
