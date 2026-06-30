"""
main.py — Entry point for the multi-asset crypto 5-min Polymarket bot (BTC/ETH/SOL).

One AssetWorker per asset: its own feeds (Binance/Coinbase/Chainlink-RTDS), CLOB book,
window discovery, signal engine, executor, and risk scope — all running concurrent 1s
loops against a shared SQLite store and one dashboard.

Usage:
    python main.py --mode paper                  # paper trade (default), all configured assets
    python main.py --mode paper --assets BTC,ETH # subset of config.ASSETS / ASSET_PARAMS
    python main.py --mode live                   # live CLOB orders
    python main.py --mode paper --no-dashboard   # suppress dashboard server
"""

import argparse
import json
import time
import threading
from typing import Optional
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
    parser = argparse.ArgumentParser(description="Polymarket crypto 5-min bot (multi-asset)")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="paper = simulate trades; live = real CLOB orders")
    parser.add_argument("--assets", default=None,
                        help="comma-separated subset, e.g. BTC,ETH,SOL (default: config.ASSETS)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Do not start the WebSocket dashboard server")
    return parser.parse_args()


def start_dashboard_server(bot_state_fn, toggle_fn=None):
    """Single aiohttp server serves both the page and the live WS feed on one port.
    `toggle_fn` (optional) is the LIVE kill switch invoked by the dashboard button."""
    try:
        from dashboard_server import DashboardServer
        server = DashboardServer(state_fn=bot_state_fn, toggle_fn=toggle_fn)
        t = threading.Thread(target=server.run, daemon=True, name="dashboard")
        t.start()
    except Exception as exc:
        logger.warning(f"Dashboard server could not start: {exc}")


class WindowExposureGuard:
    """Cross-asset correlation guard shared by all AssetWorkers (config.CERTAINTY_CORR_*).

    The 4 assets move together, so several same-direction certainty bets in one window are one
    correlated bet at N× size. This bounds the TOTAL same-side live certainty STAKE per (window,
    side) to a budget, so an all-asset wipeout is capped while breadth (taking all agreeing assets)
    is preserved. Each asset requests its fair share; the guard grants up to the remaining budget.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._used: dict[tuple, float] = {}   # (start_ts, side) -> stake $ already reserved

    def reserve_stake(self, start_ts: int, side: str, want: float, budget: float) -> float:
        """Reserve up to `want` of the (window, side) budget. Returns the granted stake ($)."""
        with self._lock:
            # prune windows older than ~1h so the map stays bounded
            cutoff = start_ts - 3600
            for k in [k for k in self._used if k[0] < cutoff]:
                del self._used[k]
            key = (start_ts, side)
            used = self._used.get(key, 0.0)
            grant = max(0.0, min(want, budget - used))
            if grant > 0:
                self._used[key] = used + grant
            return grant


class AssetWorker:
    """
    Everything one asset needs to trade its 5-min Up/Down market, isolated from the
    other assets: feeds, book, discovery, engine, executor, per-asset risk scope,
    strike snapshots, resolution tracking, and a dashboard snapshot fragment.
    """

    def __init__(self, asset: str, paper_mode: bool = True,
                 live_halt: Optional[threading.Event] = None,
                 corr_guard: Optional["WindowExposureGuard"] = None):
        self.asset = asset
        self.paper_mode = paper_mode
        # Shared across all workers: when SET, no new LIVE orders are placed (dashboard kill
        # switch). Open positions still resolve normally. Paper mode ignores it.
        self.live_halt = live_halt if live_halt is not None else threading.Event()
        # Shared cross-asset correlation guard (caps simultaneous same-side LIVE certainty bets
        # per window). A per-worker fallback keeps single-asset runs working standalone.
        self.corr_guard = corr_guard if corr_guard is not None else WindowExposureGuard()

        self.discovery = MarketDiscovery(asset)
        self.binance   = BinanceFeed(asset)
        self.oracle    = Oracle(self.binance, asset)  # owns Coinbase + Chainlink feeds
        self.book      = PolymarketBook(asset)
        self.engine    = SignalEngine(self.oracle, self.binance, self.book)
        self.executor  = Executor(paper_mode=paper_mode, asset=asset)
        self.risk      = RiskGuard(paper_mode=paper_mode, asset=asset)

        self.snapshot: dict = {}                 # per-asset dashboard fragment
        self._last_signal = None
        self._current_window_id: str = ""
        self._refs: dict[int, float] = {}        # start_ts -> snapshotted strike
        self._ref_source: dict[int, str] = {}    # start_ts -> strike source (rtds|onchain|proxy)
        self._missed: set[int] = set()           # windows we caught too late to strike
        self._ref_lock = threading.Lock()        # guards _refs/_missed (strike thread + loop)
        self._pending: dict[int, str] = {}       # start_ts -> condition_id awaiting resolution
        self._settles: dict[int, float] = {}     # start_ts -> oracle price snapshotted at close
        self._awaiting_real: dict[int, str] = {} # fallback-settled; still chasing REAL outcome
        self._last_real_poll: float = 0.0        # throttle for the background REAL-outcome poll
        self._resolved: set[int] = set()
        self._arbed: set[int] = set()            # windows we already arbed
        self._late_mom: dict[int, dict] = {}     # start_ts -> open late-momentum shadow bet
        self._late_mom_session: float = 0.0      # paper P&L of the experimental late-mom leg
        self._cert_shadow: dict[int, dict] = {}  # start_ts -> open certainty/feed-lag shadow bet
        self._cert_shadow_session: float = 0.0   # paper P&L of the certainty shadow leg
        self._maker_pending: dict[int, dict] = {} # start_ts -> resting maker-first cert limit (paper)
        self._cert_slip_sum: float = 0.0         # Σ(fill − displayed ask) over cert fills — fill-quality
        self._cert_slip_n: int = 0               # count, for avg realized slippage (London A/B telemetry)
        self._box_shadow: dict[int, dict] = {}   # start_ts -> open certainty BOX hedge (shadow)
        self._box_shadow_session: float = 0.0    # cumulative counterfactual P&L impact of boxing
        self._box_adv_run: dict[int, float] = {} # start_ts -> ts the adverse run began (box de-noise)
        self._last_heavy: float = 0.0            # last DB-write/IO pass (throttle the fast loop to ~1Hz)
        self._last_tick_ts: float = time.time()
        self._farm_reward_session: float = 0.0   # est reward accrued this session
        self._last_farm: dict = {}               # last farm quote details for dashboard
        self._stop = threading.Event()
        self._readopt_open_positions()           # resume (not cancel) in-flight positions
        self._reconcile_shadow_trades()          # settle shadow rows orphaned by a restart

    def _readopt_open_positions(self):
        """
        Re-adopt this asset's OPEN positions left by a previous run and queue them for
        resolution, instead of cancelling them. Cancelling on restart killed in-flight
        takers so they never got a WIN/LOSS and their history row stayed OPEN. We restore
        the strike + settle from the outcomes table so the fallback can settle them too.
        """
        adopted = 0
        for pos in state.get_open_positions():
            if pos.get("asset", "BTC") != self.asset:
                continue
            start_ts = int(pos["opened_at"] // config.MARKET_WINDOW_SECS) * config.MARKET_WINDOW_SECS
            self._pending[start_ts] = pos["market_id"]
            o = state.get_outcome(start_ts, self.asset)
            if o:
                if o.get("ref_price"):
                    self._refs[start_ts] = o["ref_price"]
                if o.get("settle_price"):
                    self._settles[start_ts] = o["settle_price"]
            adopted += 1
        if adopted:
            logger.info(f"[{self.asset}] Re-adopted {adopted} open position(s) for resolution")
        # Re-check recent oracle-FALLBACK settlements against the REAL Chainlink outcome and
        # correct any the cross-venue basis got wrong on borderline windows.
        for start_ts, mkt in state.get_recent_fallback_windows(limit=20, asset=self.asset):
            if start_ts not in self._resolved:
                self._awaiting_real[start_ts] = mkt

    def _reconcile_shadow_trades(self):
        """Settle isolated shadow-leg rows (CERTAINTY) orphaned by a restart: the in-memory
        tracker is lost on restart, so a row whose window has since resolved would otherwise
        hang OPEN forever. Score any OPEN row whose window now has a winning outcome.

        Wrapped so a single bad row can NEVER crash worker construction — a crash here would
        loop the whole bot through restart, and every restart misses that window's strike."""
        try:
            self._do_reconcile_shadow_trades()
        except Exception as exc:
            logger.error(f"[{self.asset}] shadow reconcile failed (non-fatal): {exc}")

    def _do_reconcile_shadow_trades(self):
        import pricing
        healed = void = 0
        stale_after = config.MARKET_WINDOW_SECS + config.RESOLUTION_GIVEUP_SECS
        for tr in state.get_open_shadow_trades(self.asset, "CERTAINTY"):
            o = state.get_outcome(tr["start_ts"], self.asset)
            winning = o.get("winning_side") if o else None
            if winning in ("UP", "DOWN"):
                entry = tr["price"]
                shares = (tr["size_usdc"] / entry) if entry else 0.0
                won = (tr["side"] == winning)
                fee = pricing.taker_fee_per_share(entry) * shares
                pnl = ((1.0 - entry) if won else -entry) * shares - fee
                state.update_trade(tr["id"], status="RESOLVED",
                                   outcome=("WIN" if won else "LOSS"),
                                   pnl_usdc=round(pnl, 4), closed_at=time.time())
                state.add_certainty_pnl(pnl, "WIN" if won else "LOSS")
                self._cert_shadow_session += pnl
                healed += 1
            elif time.time() - tr["start_ts"] > stale_after:
                # Window long past with no real outcome (e.g. a STRIKE-MISSED bogus row from
                # before the has_reference fix) — VOID it so it can't hang OPEN forever.
                state.update_trade(tr["id"], status="VOID", outcome="VOID",
                                   pnl_usdc=0.0, closed_at=time.time())
                void += 1
        # CERTAINTY_BOX shadow rows orphaned by a restart: heal the hedge-leg pnl onto the
        # ledger so the row doesn't hang OPEN (the in-memory `saved` tally for the session is
        # lost across a restart — the row stays out of the P&L ledgers either way).
        for tr in state.get_open_shadow_trades(self.asset, "CERTAINTY_BOX"):
            o = state.get_outcome(tr["start_ts"], self.asset)
            winning = o.get("winning_side") if o else None
            if winning in ("UP", "DOWN"):
                entry = tr["price"]
                shares = (tr["size_usdc"] / entry) if entry else 0.0
                won = (tr["side"] == winning)
                fee = pricing.taker_fee_per_share(entry) * shares
                hpnl = ((1.0 - entry) if won else -entry) * shares - fee
                state.update_trade(tr["id"], status="RESOLVED",
                                   outcome=("WIN" if won else "LOSS"),
                                   pnl_usdc=round(hpnl, 4), closed_at=time.time())
                healed += 1
            elif time.time() - tr["start_ts"] > stale_after:
                state.update_trade(tr["id"], status="VOID", outcome="VOID",
                                   pnl_usdc=0.0, closed_at=time.time())
                void += 1
        if healed or void:
            logger.info(f"[{self.asset}] Reconciled CERTAINTY shadows: "
                        f"{healed} settled, {void} voided")

    def start(self):
        self.discovery.start()
        self.binance.start()
        self.oracle.start()
        self.book.start()
        threading.Thread(target=self._strike_loop, daemon=True,
                         name=f"strike-{self.asset.lower()}").start()
        threading.Thread(target=self._main_loop, daemon=True,
                         name=f"loop-{self.asset.lower()}").start()
        logger.info(f"[{self.asset}] worker started")

    def stop(self):
        self._stop.set()

    def _strike_loop(self):
        """
        Snapshot the strike at the 300s boundary at high frequency, *independent* of the
        1s trading loop. The trading loop can stall for many seconds on blocking network
        work (Gamma poll / resolution-fetch retry backoff), which used to push the
        snapshot past REFERENCE_MAX_LAG and flag every window MISSED → no trades. This
        dedicated ticker only reads the async-updated oracle price, so it reliably
        catches the boundary as long as the price feed is alive at T=0.
        """
        while not self._stop.is_set():
            try:
                self._snapshot_reference(current_window_start())
            except Exception as exc:
                logger.error(f"[{self.asset}] Strike snapshot error: {exc}")
            self._stop.wait(0.25)

    # ─── Main loop ─────────────────────────────────────────────────────────────

    def _main_loop(self):
        last_executed_window = ""
        no_market_ticks = 0

        while not self._stop.is_set():
            try:
                # The loop now wakes on each fresh oracle tick (~20Hz) for low entry latency, but
                # DB writes / network retries / the snapshot are throttled to ~1Hz (heavy_due) so the
                # faster cadence doesn't bloat the ledgers or hammer the CLOB. The latency-critical
                # path (evaluate → certainty gate → box) runs every wake.
                now = time.time()
                heavy_due = (now - self._last_heavy) >= config.HEAVY_MIN_GAP
                if heavy_due:
                    self._last_heavy = now

                # The strike is snapshotted by a dedicated high-frequency thread (see
                # _strike_loop) so it is never missed when this loop stalls on network IO.
                if heavy_due:
                    self._retry_pending_resolutions()

                window = self.discovery.current
                if not window or not window.is_active:
                    no_market_ticks += 1
                    if no_market_ticks % 30 == 0:
                        logger.info(f"[{self.asset}] Waiting for active 5-min window...")
                    self._update_snapshot(window=None)
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
                        f"[{self.asset}] New window: {window.market_title} | "
                        f"ref={ref:.2f}{' (MISSED strike)' if missed else ''} | "
                        f"T-{window.time_remaining:.0f}s"
                    )

                window.reference_price = self._get_ref(start_ts)

                # Cancel stale maker quotes near close.
                if window.time_remaining <= config.CANCEL_OPEN_AT:
                    self.executor.cancel_open_order()

                signal = self.engine.evaluate(window)
                self._last_signal = signal
                if heavy_due:                          # keep the ledgers at ~1Hz granularity
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
                        if self.executor.execute(signal, window, book=self.book):
                            last_executed_window = window.condition_id
                            # Guarantee this taker is resolved even if the loop stalls
                            # through the close tick (else it stays OPEN and blocks the
                            # open-position guard forever).
                            if start_ts not in self._resolved:
                                self._pending[start_ts] = window.condition_id
                elif signal.action == Action.LATE_MOMENTUM:
                    # EXPERIMENTAL shadow leg — PAPER ONLY, hard-gated here so it can never
                    # place a live order. Records one isolated leg='LATE_MOM' ledger row per
                    # window (no real position, no risk-guard interaction). Resolved later
                    # from the same outcome in _resolve_late_mom.
                    if (self.paper_mode and config.LATE_MOMENTUM_ENABLED
                            and start_ts not in self._late_mom):
                        tid = state.record_trade({
                            "asset": self.asset,
                            "market_id": window.condition_id, "start_ts": start_ts,
                            "leg": "LATE_MOM", "side": signal.order_side,
                            "price": signal.order_price, "detail": signal.reason,
                            "size_usdc": config.LATE_MOMENTUM_SIZE_USDC,
                            "status": "OPEN", "outcome": None,
                        })
                        self._late_mom[start_ts] = {
                            "side": signal.order_side, "price": signal.order_price,
                            "trade_id": tid,
                        }
                        logger.info(f"[PAPER·EXPERIMENTAL][{self.asset}] late-momentum "
                                    f"{signal.order_side}@{signal.order_price:.2f}")

                # Certainty / feed-lag leg (APPROACH.md §3①) — a separate `if` (not in the
                # elif dispatch), so it never preempts or is preempted by the real legs.
                #   PAPER mode: records an isolated leg='CERTAINTY' shadow row via the live
                #     book's PAPER_FILL_REALISM path; no real position. Resolved in
                #     _resolve_cert_shadow.
                #   LIVE mode (2026-06-26): places a REAL IOC order through the risk guard and
                #     the dashboard kill switch; opens a real position resolved by
                #     _resolve_position (a sentinel marks the window done in _cert_shadow).
                # Require a valid strike: with no Price-to-Beat the model prices against
                # ref=0 and returns a garbage p≈0.99, which would fire the gate on cheap
                # asks (the STRIKE-MISSED bug). The real taker/late-mom legs gate on this too.
                if (window.has_reference
                        and self.asset in config.CERTAINTY_ASSETS
                        and start_ts not in self._cert_shadow
                        and start_ts not in self._maker_pending):
                    pick = self.engine.certainty_shadow(signal)
                    if (pick is not None and not self.paper_mode
                            and self.asset in config.CERTAINTY_LIVE_ASSETS):
                        # ── LIVE certainty order ──────────────────────────────────────────
                        # Real USDC — ONLY for assets on the CERTAINTY_LIVE_ASSETS whitelist (SOL).
                        # Any other asset that fires the leg in live mode falls to the paper branch
                        # below. Gated further by (1) the dashboard kill switch and (2) the per-asset
                        # risk guard (open-position + global daily-loss halt). Opens a real position
                        # resolved by _resolve_position; a sentinel in _cert_shadow stops the gate
                        # from re-firing this window.
                        cside, cask, csize = pick
                        # Cross-asset correlation guard (combined STAKE cap): the 4 assets move
                        # together, so reserve this asset's fair share (budget / #live-assets) of a
                        # per-(window, side) budget. Breadth is kept — every agreeing asset still
                        # trades — but the TOTAL same-side stake (hence an all-asset wipeout) is
                        # bounded. SOL-only ⇒ share = full budget, capped at base ⇒ no-op today.
                        if config.CERTAINTY_CORR_GUARD_ENABLED:
                            n_live = max(1, len(config.CERTAINTY_LIVE_ASSETS))
                            want = min(csize, config.CERTAINTY_CORR_STAKE_USDC / n_live)
                            csize = self.corr_guard.reserve_stake(
                                start_ts, cside, want, config.CERTAINTY_CORR_STAKE_USDC)
                        if self.live_halt.is_set():
                            logger.info(f"[LIVE·CERT][{self.asset}] skip — LIVE trading "
                                        f"STOPPED (dashboard kill switch)")
                        elif csize < config.CERTAINTY_MIN_ORDER_USDC:
                            # Below the Polymarket minimum order (budget exhausted or share too
                            # small) — placing it would error. Record paper so analysis sees it.
                            logger.info(f"[LIVE·CERT][{self.asset}] skip — share ${csize:.2f} "
                                        f"< min order ${config.CERTAINTY_MIN_ORDER_USDC:.2f} "
                                        f"({cside}, correlation budget "
                                        f"${config.CERTAINTY_CORR_STAKE_USDC:.2f}) → paper")
                            # Budget gone for real capital, but still record the paper shadow so
                            # the analysis sees the full opportunity set (and the guard's cost).
                            entry, shares = cask, pick[2] / cask
                            if config.PAPER_FILL_REALISM and self.book is not None:
                                tick = float(getattr(window, "tick_size", None) or config.TICK_SIZE)
                                filled, vwap = self.book.fill_ask(cside, shares)
                                if filled > 0:
                                    entry = min(0.99, vwap + config.PAPER_SLIPPAGE_TICKS * tick)
                                    shares = filled
                                else:
                                    shares = 0
                            if shares > 0:
                                self._open_cert_paper(window, start_ts, cside, cask, entry, shares,
                                                      maker=False, label="corr-guard",
                                                      t_fired=signal.time_remaining,
                                                      p_up=signal.p_up)
                        else:
                            ok, why = self.risk.check()
                            if not ok:
                                logger.info(f"[LIVE·CERT][{self.asset}] skip — risk: {why}")
                            elif self.executor.execute_certainty_live(
                                    signal, window, cside, cask, csize):
                                self._cert_shadow[start_ts] = {"live": True, "side": cside}
                                if start_ts not in self._resolved:
                                    self._pending[start_ts] = window.condition_id
                                self._record_cert_fill(
                                    start_ts=start_ts, side=cside, cask=cask,
                                    t_remaining=signal.time_remaining,
                                    p_side=(signal.p_up if cside == "UP"
                                            else 1.0 - signal.p_up),
                                    fill_price=cask, size_usdc=csize, trade_id=None)
                    elif pick is not None:
                        cside, cask, csize = pick
                        if config.CERTAINTY_MAKER_ENABLED:
                            # Maker-first (the execution lever): rest a buy limit one tick below the
                            # displayed ask instead of crossing now. It fills as a MAKER only if the
                            # book trades down to it within the wait window (_check_maker_fills);
                            # otherwise it crosses to a taker fill at the deadline. Captures the
                            # adverse selection a perfect-maker sim assumes away.
                            tick = float(getattr(window, "tick_size", None) or config.TICK_SIZE)
                            limit = round(max(tick, cask - config.CERTAINTY_MAKER_OFFSET_TICKS * tick), 4)
                            self._maker_pending[start_ts] = {
                                "side": cside, "limit": limit, "size_usdc": csize, "cask": cask,
                                "deadline": time.time() + config.CERTAINTY_MAKER_WAIT_SECS,
                                "p_up": signal.p_up, "t_fired": signal.time_remaining,
                                "condition_id": window.condition_id,
                            }
                            if start_ts not in self._resolved:
                                self._pending[start_ts] = window.condition_id
                            logger.info(f"[PAPER·MAKER][{self.asset}] certainty {cside} "
                                        f"rest limit@{limit:.3f} (ask={cask:.2f}) "
                                        f"T-{signal.time_remaining:.0f}s, wait "
                                        f"{config.CERTAINTY_MAKER_WAIT_SECS:.0f}s")
                        else:
                            # Depth-realistic fill: DECIDE on the displayed ask, FILL by walking
                            # the live book (VWAP) + one adverse latency tick — the very realism
                            # the backtest could not model from top-of-book ticks. A too-thin /
                            # empty ask book means no fill (we don't manufacture liquidity).
                            # csize = confidence-scaled paper notional (late slice gets more).
                            entry, shares = cask, csize / cask
                            if config.PAPER_FILL_REALISM and self.book is not None:
                                tick = float(getattr(window, "tick_size", None) or config.TICK_SIZE)
                                filled, vwap = self.book.fill_ask(cside, shares)
                                if filled <= 0:
                                    logger.info(f"[PAPER·SHADOW][{self.asset}] certainty {cside} "
                                                f"unfilled — ask book empty/too thin")
                                    filled = 0
                                else:
                                    entry = min(0.99, vwap + config.PAPER_SLIPPAGE_TICKS * tick)
                                    shares = filled
                            else:
                                filled = shares
                            if filled > 0:
                                self._open_cert_paper(window, start_ts, cside, cask, entry,
                                                      filled, maker=False, label="",
                                                      t_fired=signal.time_remaining,
                                                      p_up=signal.p_up)

                # Maker-first: settle / cross any resting cert limit against the live book.
                self._check_maker_fills(signal, window, start_ts)

                # Box-stop: hedge the open taker into a $1 box if the signal flipped.
                self._maybe_box_position(signal, window)

                # Certainty box-stop (SHADOW, not live): measure the loss-capping hedge on the
                # oracle-vs-strike trigger. Logs a CERTAINTY_BOX shadow row; never a real order.
                self._maybe_box_certainty(signal, window, start_ts)

                # Window closing: queue for resolution and snapshot the settle price
                # (oracle price at close) — used as the paper fallback if the real
                # on-chain outcome never arrives. Only for windows with a real strike:
                # a STRIKE-MISSED window has no position to settle (every leg requires a
                # strike) and its ticks are dropped from calibration (ref_price>0 filter),
                # so tracking it only feeds the unresolvable-pending backlog.
                if (window.has_reference and window.time_remaining < 2
                        and start_ts not in self._resolved):
                    self._pending[start_ts] = window.condition_id
                    if start_ts not in self._settles:
                        # Prefer the Chainlink reading (Polymarket's settlement feed) for the
                        # close snapshot so a live self-settle matches the official outcome;
                        # fall back to the blended proxy only if Chainlink is stale.
                        cl = self.oracle.chainlink_price
                        snap = cl if cl and cl > 0 else self.oracle.price
                        if snap > 0:
                            self._settles[start_ts] = snap

                if heavy_due:
                    self._update_snapshot(window=window, signal=signal)

                # Wake on the next fresh oracle tick (low entry latency) or after LOOP_MAX_WAIT.
                self._wait_for_tick(config.LOOP_MAX_WAIT)

            except Exception as exc:
                logger.error(f"[{self.asset}] Main loop error: {exc}", exc_info=True)
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
                src = self.oracle.strike_source            # rtds | onchain | proxy
                self._refs[start_ts] = px
                self._ref_source[start_ts] = src
                if src == "proxy":
                    # The strike is a CEX proxy (~4-5bp basis vs the real Chainlink Price to
                    # Beat) because BOTH Chainlink sources were stale at T=0. Visible warning:
                    # the directional/certainty edge depends on an accurate strike.
                    logger.warning(f"[{self.asset}] Strike {start_ts} from CEX PROXY "
                                    f"(both Chainlink feeds stale) {px:.2f} — basis risk")
                else:
                    logger.debug(f"[{self.asset}] Reference snapshot for {start_ts}: "
                                 f"{px:.2f} src={src} (lag {lag:.1f}s)")
            elif lag > config.REFERENCE_MAX_LAG:
                reason = "no price feed" if not (px and px > 0) else f"lag {lag:.1f}s"
                self._missed.add(start_ts)
                # WARNING (not debug) so the root cause is visible in production logs: a
                # "no price feed" reason means oracle.price==0 within REFERENCE_MAX_LAG of T=0,
                # which almost always means feeds were down at the boundary (usually the bot
                # process restarting at window open). Cross-check against repeating "Bot started".
                logger.warning(f"[{self.asset}] Window {start_ts} strike MISSED ({reason}, "
                                f"oracle.connected={self.oracle.connected}) — will not trade")

    # ─── Resolution (real outcome from Polymarket) ──────────────────────────────

    def _retry_pending_resolutions(self):
        # ── Pass 1: settle POSITIONS quickly at close so they don't carry into the next
        #            window. Prefer the real outcome if it's already available; otherwise
        #            (paper) settle on our oracle after a short grace and keep chasing REAL.
        fetched = 0
        for start_ts, condition_id in list(self._pending.items()):
            closed_at = start_ts + config.MARKET_WINDOW_SECS
            if time.time() < closed_at:
                continue
            # Bound synchronous HTTP per cycle (as Pass 2 already does). A missed-strike
            # window has no ref → can't fallback-settle → it would otherwise hammer
            # fetch_resolution every cycle for 900s and stall the 1s loop (which delays the
            # state push → dashboard flips to BOT DISCONNECTED). Over budget we skip the fetch
            # this cycle; valid-strike windows still settle via the fallback path below.
            if fetched < config.RESOLUTION_MAX_FETCH_PER_CYCLE:
                winning = self.discovery.fetch_resolution(start_ts)
                fetched += 1
            else:
                winning = None
            ref = self._get_ref(start_ts)
            settle = self._settles.get(start_ts) or self.oracle.price
            predicted = "UP" if (ref and settle >= ref) else "DOWN"
            source = "REAL"

            if winning is None:
                grace_done = time.time() - closed_at >= config.RESOLUTION_FALLBACK_SECS
                # Self-settle (paper AND live) on our Chainlink-vs-strike reading if the
                # official Gamma outcome hasn't posted yet — it lags close by minutes and was
                # leaving live positions OPEN, blocking the next window. Chainlink IS the feed
                # Polymarket settles on, and certainty bets need a ≥5bp move (non-borderline),
                # so this matches the official result ~always; Pass 2 still chases the REAL
                # outcome and _resettle_to_real corrects the rare disagreement.
                if ref and grace_done:
                    winning, source = predicted, "FALLBACK"
                    self._awaiting_real[start_ts] = condition_id   # upgrade to REAL later
                elif time.time() - closed_at >= config.RESOLUTION_GIVEUP_SECS:
                    pos = state.get_open_position(self.asset)
                    if pos and pos["market_id"] == condition_id:
                        state.cancel_position(pos["id"])
                        state.resolve_taker_ledger(condition_id, "VOID", 0.0)
                        logger.warning(f"[{self.asset}] Window {start_ts} unresolvable "
                                       f"— position VOIDed")
                    self._pending.pop(start_ts, None)
                    self._settles.pop(start_ts, None)
                    self._resolved.add(start_ts)
                    continue
                else:
                    state.upsert_outcome({
                        "asset": self.asset,
                        "start_ts": start_ts, "market_id": condition_id,
                        "ref_price": ref, "settle_price": settle,
                        "winning_side": None, "predicted_side": predicted,
                        "resolved_at": None, "resolution_source": None,
                    })
                    continue

            state.upsert_outcome({
                "asset": self.asset,
                "start_ts": start_ts, "market_id": condition_id,
                "ref_price": ref, "settle_price": settle,
                "winning_side": winning, "predicted_side": predicted,
                "resolved_at": time.time(), "resolution_source": source,
            })
            self._resolve_position(condition_id, winning)
            self._resolve_late_mom(start_ts, winning)
            self._resolve_cert_shadow(start_ts, winning)
            self._resolve_box_shadow(start_ts, winning)
            if source == "FALLBACK":
                logger.info(f"[{self.asset}] Window {start_ts} self-settled (Chainlink vs "
                            f"strike): {winning} — confirming vs official outcome in background")
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
                    break   # bound loop stall on slow/flaky VPS network
                fetched += 1
                winning = self.discovery.fetch_resolution(start_ts)
                if winning is None:
                    continue
                o = state.get_outcome(start_ts, self.asset) or {}
                state.upsert_outcome({
                    "asset": self.asset,
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
                logger.debug(f"[{self.asset}] Window {start_ts} REAL outcome captured: "
                             f"{winning} (calibration)")

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
            logger.info(f"[{self.asset}] Corrected {condition_id} to REAL outcome {winning}: "
                        f"{pos['outcome']}→{correct} pnl={new_pnl:+.2f}")

    def _resolve_late_mom(self, start_ts: int, winning_side: str):
        """Settle the EXPERIMENTAL late-momentum shadow bet for a window (paper-only).
        Isolated from the real position lifecycle: just scores the ledger row + a session
        tally so we can compare it against the taker leg. Authoritative measurement is
        still backtest.py --validate on REAL outcomes."""
        lm = self._late_mom.pop(start_ts, None)
        if not lm:
            return
        import pricing
        entry = lm["price"]
        shares = config.LATE_MOMENTUM_SIZE_USDC / entry
        won = (lm["side"] == winning_side)
        fee = pricing.taker_fee_per_share(entry) * shares
        pnl = ((1.0 - entry) if won else -entry) * shares - fee
        state.update_trade(lm["trade_id"], status="RESOLVED",
                           outcome=("WIN" if won else "LOSS"),
                           pnl_usdc=round(pnl, 4), closed_at=time.time())
        self._late_mom_session += pnl
        logger.info(f"[PAPER·EXPERIMENTAL][{self.asset}] late-momentum {lm['side']} "
                    f"{'WIN' if won else 'LOSS'} pnl={pnl:+.2f} "
                    f"(session {self._late_mom_session:+.2f})")

    def _record_cert_fill(self, *, start_ts, side, cask, t_remaining, p_side,
                          fill_price, size_usdc, trade_id=None):
        """Snapshot the ask-book depth this certainty fire saw (size at the touch + depth-walk
        VWAP/fill-fraction per USDC tier) so edge-vs-size is measurable offline. Best-effort:
        any failure here must never disturb the trade path."""
        if not config.CERT_DEPTH_LOG_ENABLED or self.book is None:
            return
        try:
            snap = self.book.ask_snapshot(side, config.CERT_DEPTH_TIERS)
            state.record_cert_fill({
                "asset": self.asset, "start_ts": start_ts, "side": side,
                "t_remaining": t_remaining, "p_side": p_side, "ask_at_signal": cask,
                "best_ask": snap.get("best_ask"), "best_ask_size": snap.get("best_ask_size"),
                "fill_price": fill_price, "size_usdc": size_usdc,
                "depth_json": json.dumps(snap.get("tiers", {})), "trade_id": trade_id,
            })
        except Exception as e:
            logger.warning(f"[{self.asset}] cert_fill depth snapshot failed: {e}")

    def _open_cert_paper(self, window, start_ts, side, cask, entry, shares,
                         *, maker: bool, label: str, t_fired: float, p_up: float = 0.0):
        """Record an open paper CERTAINTY shadow row + register it for resolution. Shared by the
        immediate-taker path and the maker-first fill path. `maker` flags a maker fill (no taker
        fee, resolved fee-free in _resolve_cert_shadow); `label` tags the fill kind in the detail."""
        if shares <= 0:
            return
        tag = f" {label}" if label else ""
        tid = state.record_trade({
            "asset": self.asset,
            "market_id": window.condition_id, "start_ts": start_ts,
            "leg": "CERTAINTY", "side": side, "price": round(entry, 4),
            "detail": f"CERTAINTY {side}{tag} ask={cask:.2f} fill={entry:.3f} "
                      f"p={p_up:.3f} T-{t_fired:.0f}s (paper shadow)",
            "size_usdc": round(shares * entry, 2),
            "status": "OPEN", "outcome": None,
        })
        self._cert_shadow[start_ts] = {
            "side": side, "price": entry, "shares": shares, "trade_id": tid, "maker": maker,
        }
        if start_ts not in self._resolved:
            self._pending[start_ts] = window.condition_id
        # Fill-quality telemetry: realized slippage vs the displayed ask, in cents. This is the number
        # the London migration is meant to shrink (Bangalore taker pays ~+1 tick; closer to the engine
        # ⇒ less adverse movement before the fill). Tracked as a running average for the dashboard.
        slip_c = (entry - cask) * 100.0
        self._cert_slip_sum += (entry - cask)
        self._cert_slip_n += 1
        logger.info(f"[PAPER·SHADOW][{self.asset}] certainty {side}{tag} ask={cask:.2f} "
                    f"fill={entry:.3f} slip={slip_c:+.1f}c sh={shares:.1f}")
        self._record_cert_fill(
            start_ts=start_ts, side=side, cask=cask, t_remaining=t_fired,
            p_side=(p_up if side == "UP" else 1.0 - p_up), fill_price=entry,
            size_usdc=round(shares * entry, 2), trade_id=tid)

    def _check_maker_fills(self, signal, window, start_ts: int):
        """Settle a resting maker-first cert limit against the live book each tick. It fills as a
        MAKER (at the limit, fee=0) only if the book's ask has traded DOWN to our price — which is
        disproportionately when the favorite is weakening, so adverse selection is captured, not
        assumed away. If the wait deadline passes unfilled, CROSS to a depth-realistic taker fill
        (current ask VWAP + 1 adverse tick), i.e. today's behavior. Paper-only."""
        mk = self._maker_pending.get(start_ts)
        if not mk:
            return
        side = mk["side"]
        ask = signal.up_ask if side == "UP" else signal.down_ask
        # MAKER fill: the book traded through our resting limit.
        if ask is not None and ask <= mk["limit"]:
            self._maker_pending.pop(start_ts, None)
            shares = mk["size_usdc"] / mk["limit"]
            self._open_cert_paper(window, start_ts, side, mk["cask"], mk["limit"], shares,
                                  maker=True, label="MAKER", t_fired=mk["t_fired"],
                                  p_up=mk["p_up"])
            return
        # Deadline: cross to a taker fill (depth-realistic VWAP + adverse tick).
        if time.time() >= mk["deadline"]:
            self._maker_pending.pop(start_ts, None)
            cask_now = ask if ask is not None else mk["cask"]
            shares = mk["size_usdc"] / cask_now
            entry = cask_now
            if config.PAPER_FILL_REALISM and self.book is not None:
                tick = float(getattr(window, "tick_size", None) or config.TICK_SIZE)
                filled, vwap = self.book.fill_ask(side, shares)
                if filled <= 0:
                    logger.info(f"[PAPER·MAKER][{self.asset}] certainty {side} cross unfilled "
                                f"— ask book empty/too thin; limit canceled")
                    return
                entry, shares = min(0.99, vwap + config.PAPER_SLIPPAGE_TICKS * tick), filled
            self._open_cert_paper(window, start_ts, side, cask_now, entry, shares,
                                  maker=False, label="TAKER-cross", t_fired=mk["t_fired"],
                                  p_up=mk["p_up"])

    def _resolve_cert_shadow(self, start_ts: int, winning_side: str):
        """Settle the certainty/feed-lag shadow bet for a window (paper-only). Isolated from
        the real position lifecycle — scores its own ledger row + a session tally so the leg's
        live (depth-realistic) P&L can be compared against the backtest. Authoritative number
        is still backtest.py --certainty on REAL outcomes."""
        self._maker_pending.pop(start_ts, None)   # cancel any limit still resting at close
        cs = self._cert_shadow.pop(start_ts, None)
        if not cs:
            return
        if cs.get("live"):
            return        # live certainty is a REAL position; settled by _resolve_position
        import pricing
        entry = cs["price"]
        shares = cs.get("shares") or (config.CERTAINTY_SIZE_USDC / entry)
        won = (cs["side"] == winning_side)
        # Maker fills pay NO taker fee (maker fee = 0); the 5-min reward pool is unfunded so there
        # is no rebate either, but skipping the taker fee is itself part of the maker lever's edge.
        fee = 0.0 if cs.get("maker") else pricing.taker_fee_per_share(entry) * shares
        # The certainty leg always settles on its un-boxed ride; the boxed hedge is recorded as a
        # separate, VISIBLE-but-uncounted CERTAINTY_BOX row in _resolve_box_shadow (it shows in the
        # Trade History but is excluded from session/total P&L by design — see config note).
        pnl = ((1.0 - entry) if won else -entry) * shares - fee
        state.update_trade(cs["trade_id"], status="RESOLVED",
                           outcome=("WIN" if won else "LOSS"),
                           pnl_usdc=round(pnl, 4), closed_at=time.time())
        state.add_certainty_pnl(pnl, "WIN" if won else "LOSS")
        self._cert_shadow_session += pnl
        logger.info(f"[PAPER·SHADOW][{self.asset}] certainty {cs['side']} "
                    f"{'WIN' if won else 'LOSS'} pnl={pnl:+.2f} "
                    f"(session {self._cert_shadow_session:+.2f})")

    def _maybe_box_certainty(self, signal, window, start_ts: int):
        """
        SHADOW hedge-to-box for the certainty/feed-lag leg (2026-06-27). MEASURES — but never
        places — the loss-capping hedge the leaderboard winners use, on the trigger that
        validated OOS: the ORACLE crossing to the LOSING side of the strike in the final
        seconds (the actual resolution variable, not the noisy model prob that conceded too
        late — see certainty-boxing-fails). It walks the REAL opposite book for a
        depth-realistic hedge fill (the one number the backtest could not model), logs a
        leg='CERTAINTY_BOX' shadow row labelled '(shadow, not live)', and tallies the
        counterfactual it would have saved. Real money is untouched — paper cert keeps its
        un-boxed headline P&L, a live cert position settles normally — so we can confirm the
        live hedge fills before ever committing real capital to the hedge.
        """
        if not config.CERTAINTY_BOX_ENABLED or not window.has_reference:
            return
        if start_ts in self._box_shadow:
            return                       # already boxed this window
        # Cheap timing gate BEFORE the position lookup — the loop now wakes ~20Hz, and the box can
        # only act inside the last CREDIT_FROM/FROM secs, so skip the per-wake DB read outside it.
        t = window.time_remaining
        box_window = (config.CERTAINTY_BOX_CREDIT_FROM if config.CERTAINTY_BOX_CREDIT
                      else config.CERTAINTY_BOX_FROM)
        if t > box_window or t < config.CERTAINTY_BOX_MIN_T:
            return
        # Resolve the protected position.
        #   LIVE: box the REAL open position directly — do NOT depend on the in-memory cert
        #   sentinel (a restart wipes _cert_shadow, which would silently leave an already-open
        #   live position un-boxed) nor on its ledger leg/order_type (a live certainty
        #   market-buy is recorded as 'TAKER'). The directional taker is disabled, so the only
        #   open position is the certainty leg.
        #   PAPER: the cert leg has no real position — read the paper shadow's side/price/shares.
        if not self.paper_mode:
            pos = state.get_open_position(self.asset)
            if (not pos or pos["market_id"] != window.condition_id
                    or not pos.get("entry_price")):
                return
            side, entry = pos["side"], pos["entry_price"]
            shares = pos["size_usdc"] / entry
        else:
            cs = self._cert_shadow.get(start_ts)
            if not cs or cs.get("live"):
                return
            side, entry, shares = cs.get("side"), cs.get("price"), cs.get("shares")
        if not side or not entry or not shares:
            return
        # Opposite side + how far the oracle has moved AGAINST our bet (the resolution variable).
        # distance_bp = (oracle − ref)/ref·1e4 → UP loses when negative, DOWN when positive.
        if side == "UP":
            adverse_bp, opp_side, opp_ask = -signal.distance_bp, "DOWN", signal.down_ask
        else:
            adverse_bp, opp_side, opp_ask = signal.distance_bp, "UP", signal.up_ask
        if opp_ask is None:
            return

        # Two shadow triggers, CREDIT first:
        #   CREDIT  — the pair already locks a credit (entry+opp_ask ≤ cap): risk-free to lock,
        #             box regardless of direction. Rare for us (expensive favorite entries).
        #   LOSS-CAP — the oracle has crossed to our LOSING side for ≥persist ticks inside the last
        #             FROM secs and the hedge is not yet too rich: cap the loss.
        kind = None
        if (config.CERTAINTY_BOX_CREDIT
                and config.CERTAINTY_BOX_MIN_T <= t <= config.CERTAINTY_BOX_CREDIT_FROM
                and entry + opp_ask <= config.CERTAINTY_BOX_CREDIT_MAX):
            kind = "CREDIT"
        if kind is None:
            if t > config.CERTAINTY_BOX_FROM or t < config.CERTAINTY_BOX_MIN_T:
                return
            if adverse_bp < config.CERTAINTY_BOX_MARGIN_BP:
                self._box_adv_run.pop(start_ts, None)
                return
            # De-noise: require the adverse condition to PERSIST for CERTAINTY_BOX_PERSIST seconds.
            # Time-based (not tick-count) so it is invariant to the now event-driven loop cadence.
            ts_now = time.time()
            t0 = self._box_adv_run.setdefault(start_ts, ts_now)
            if ts_now - t0 < config.CERTAINTY_BOX_PERSIST:
                return
            if opp_ask >= config.CERTAINTY_BOX_MAX_OPP_ASK:
                return                   # late flip: hedge already too rich to help — ride it
            kind = "LOSS-CAP"

        # Partial sizing: hedge only a fraction of the position (winners run ~0.5 to keep upside on
        # a false trigger; 1.0 = full box, the validated default on our data).
        frac = max(0.0, min(1.0, config.CERTAINTY_BOX_FRACTION))
        want = shares * frac
        if want <= 0:
            return
        # Depth-realistic hedge fill: walk the REAL opposite book + one adverse latency tick.
        tick = float(getattr(window, "tick_size", None) or config.TICK_SIZE)
        if config.PAPER_FILL_REALISM and self.book is not None:
            filled, vwap = self.book.fill_ask(opp_side, want)
            if filled <= 0:
                logger.info(f"[BOX·SHADOW][{self.asset}] {kind} {side} trigger but hedge "
                            f"book empty/too thin — riding")
                return                   # retry next tick (adv_run preserved)
            hedge_fill = min(0.99, vwap + config.PAPER_SLIPPAGE_TICKS * tick)
        else:
            filled, hedge_fill = want, opp_ask
        # Cost guard: never pay more than the cap to lock the box.
        if entry + hedge_fill > config.CERTAINTY_BOX_MAX_TOTAL:
            logger.info(f"[BOX·SHADOW][{self.asset}] {kind} skip — box cost "
                        f"{entry + hedge_fill:.2f} > cap {config.CERTAINTY_BOX_MAX_TOTAL}")
            return
        box_tid = state.record_trade({
            "asset": self.asset, "market_id": window.condition_id, "start_ts": start_ts,
            "leg": "CERTAINTY_BOX", "side": opp_side, "price": round(hedge_fill, 4),
            "detail": f"BOX[{kind}] {side} → hedge {opp_side}@{hedge_fill:.3f} x{frac:.0%} "
                      f"total={entry + hedge_fill:.2f} T-{t:.0f}s (shadow, not live)",
            "size_usdc": round(filled * hedge_fill, 2),
            "status": "OPEN", "outcome": None,
        })
        self._box_shadow[start_ts] = {
            "long_side": side, "long_entry": entry, "long_shares": shares,
            "hedge_side": opp_side, "hedge_fill": hedge_fill, "hedge_shares": filled,
            "trade_id": box_tid, "t": t, "kind": kind,
        }
        logger.info(f"[BOX·SHADOW][{self.asset}] {kind} {side} adverse {adverse_bp:.1f}bp "
                    f"T-{t:.0f}s → hedge {opp_side}@{hedge_fill:.3f} x{frac:.0%} "
                    f"(box cost {entry + hedge_fill:.2f}, shadow not live)")

    def _resolve_box_shadow(self, start_ts: int, winning_side: str):
        """Settle the certainty BOX shadow hedge and tally the counterfactual it measures: what
        the depth-realistic hedge would have locked vs. riding the bet to resolution. Pure
        measurement — leg='CERTAINTY_BOX' is excluded from the P&L ledgers, so it NEVER moves
        real or headline P&L; it only proves out the live hedge before any real-capital box."""
        self._box_adv_run.pop(start_ts, None)
        box = self._box_shadow.pop(start_ts, None)
        if not box:
            return
        import pricing
        le, ls, lside = box["long_entry"], box["long_shares"], box["long_side"]
        hf, hs, hside = box["hedge_fill"], box["hedge_shares"], box["hedge_side"]
        long_pnl = (((1.0 - le) if lside == winning_side else -le) * ls
                    - pricing.taker_fee_per_share(le) * ls)
        hedge_pnl = (((1.0 - hf) if hside == winning_side else -hf) * hs
                     - pricing.taker_fee_per_share(hf) * hs)
        boxed = long_pnl + hedge_pnl          # net if we DID box
        saved = boxed - long_pnl              # = hedge_pnl: the counterfactual impact of boxing
        state.update_trade(box["trade_id"], status="RESOLVED",
                           outcome=("WIN" if hside == winning_side else "LOSS"),
                           pnl_usdc=round(hedge_pnl, 4), closed_at=time.time())
        self._box_shadow_session += saved
        logger.info(f"[BOX·SHADOW][{self.asset}] {box.get('kind','?')} resolved: ride "
                    f"{long_pnl:+.2f} vs boxed {boxed:+.2f} (saved {saved:+.2f}; "
                    f"box row visible in Trade History, excluded from session/total P&L; "
                    f"box-impact tally {self._box_shadow_session:+.2f})")

    def _maybe_box_position(self, signal, window):
        """
        Hedge-to-box stop-loss (see BOX_STOP_MARGIN_LOSS/_PROFIT in config.py). Each tick,
        if the model probability of our open taker's side has collapsed enough that buying
        the opposite side — locking $1/pair — beats holding by the margin, box it. The
        leaderboard winners' loss-capping mechanic: they never ride a flipped window to
        a full-stake loss, and neither should we.
        """
        if not config.BOX_STOP_ENABLED or not window.has_reference:
            return
        if window.time_remaining < 3:        # too late to expect the hedge to fill
            return
        pos = state.get_open_position(self.asset)
        if (not pos or pos["market_id"] != window.condition_id
                or pos["order_type"] != "TAKER"):
            return
        if pos["side"] == "UP":
            p_side, opp_ask = signal.p_up, signal.down_ask
        else:
            p_side, opp_ask = signal.p_down, signal.up_ask
        if opp_ask is None or opp_ask >= 1.0:
            return
        # Asymmetric margin: tight when the box caps a loss (pair costs ≥ $1),
        # wide when it takes profit — see the rationale in config.py.
        locking_loss = (pos["entry_price"] + opp_ask) >= 1.0
        margin = (config.BOX_STOP_MARGIN_LOSS if locking_loss
                  else config.BOX_STOP_MARGIN_PROFIT)
        if p_side < 1.0 - opp_ask - margin:
            pnl = self.executor.box_position(window, pos, opp_ask, book=self.book)
            if pnl is not None and pnl < 0:
                # A boxed loss is still a wrong call — count it for the cooldown.
                self.risk.on_loss()

    def _resolve_position(self, condition_id: str, winning_side: str):
        """Resolve the open position ONLY if it belongs to the window that resolved."""
        pos = state.get_open_position(self.asset)
        if not pos or pos["market_id"] != condition_id:
            return
        window = self.discovery.current
        self.executor.on_market_resolved(window, winning_side)
        # Read the outcome back from THIS market's position — recent-trades could
        # surface another asset's resolution and miscount the win/loss streak.
        resolved = state.get_position_by_market(condition_id)
        outcome = (resolved or {}).get("outcome")
        if outcome == "WIN":
            self.risk.on_win()
        elif outcome == "LOSS":
            self.risk.on_loss()
        else:
            self.risk.on_push()

    # ─── Event-driven wait ──────────────────────────────────────────────────────

    def _wait_for_tick(self, timeout: float) -> bool:
        """Block up to `timeout`s, returning as soon as the settlement price moves — so the entry
        decision reacts within ~FAST_POLL_SEC of a fresh oracle tick (RTDS Chainlink / CEX blend,
        which lead the book) instead of a fixed 1s poll. Returns True if woken by a price move,
        False on timeout. Heavy/IO loop work is throttled separately (heavy_due), so this faster
        cadence does not change the ~1Hz ledger granularity."""
        deadline = time.time() + timeout
        last = self.oracle.price
        while not self._stop.is_set():
            time.sleep(config.FAST_POLL_SEC)
            p = self.oracle.price
            if p > 0 and p != last:
                return True
            if time.time() >= deadline:
                return False
        return False

    # ─── Persistence helpers ────────────────────────────────────────────────────

    def _record_signal(self, s):
        state.insert_signal({
            "asset": self.asset,
            "ts": s.ts, "market_id": s.market_id, "btc_ref": s.btc_ref,
            "btc_now": s.btc_now, "distance_bp": s.distance_bp,
            "momentum_bp": s.momentum_bp, "time_remaining": int(s.time_remaining),
            "p_up": s.p_up, "p_down": s.p_down, "up_ask": s.up_ask,
            "down_ask": s.down_ask, "edge_up": s.edge_up, "edge_down": s.edge_down,
            "action": s.action, "reason": s.reason, "phase": s.phase,
        })

    def _record_tick(self, s, window):
        state.insert_tick({
            "asset": self.asset,
            "ts": s.ts, "market_id": s.market_id, "start_ts": int(window.start_ts),
            "t_remaining": s.time_remaining, "binance_price": self.binance.current_price,
            "oracle_price": s.btc_now, "cex_basis_bp": self.oracle.cex_basis_bp,
            "realized_vol": self.oracle.realized_vol_per_sec, "ref_price": s.btc_ref,
            "momentum_bp": s.momentum_bp, "p_up": s.p_up, "sigma_price": s.sigma_price,
            "up_bid": s.up_bid, "up_ask": s.up_ask, "down_bid": s.down_bid,
            "down_ask": s.down_ask, "ev_up": s.ev_up, "ev_down": s.ev_down,
            "action": s.action, "mode": s.mode,
        })

    # ─── Dashboard snapshot (per asset) ─────────────────────────────────────────

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

    def _update_snapshot(self, window=None, signal=None):
        day = state.get_asset_day_stats(self.asset)
        decided = (day.get("wins") or 0) + (day.get("losses") or 0)
        self.snapshot = {
            "asset": self.asset,
            "name": config.ASSET_PARAMS[self.asset]["name"],
            "day": {
                "net_pnl": round(day.get("net_pnl") or 0.0, 2),
                "trades": day.get("trades") or 0,
                "wins": day.get("wins") or 0,
                "losses": day.get("losses") or 0,
                "win_rate": round((day.get("wins") or 0) / decided, 3) if decided else None,
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
            "px": {
                "price": round(self.oracle.price, 4),
                "chainlink": round(self.oracle.chainlink_price, 4),
                "strike_source": self.oracle.strike_source,
                "binance": round(self.binance.current_price, 4),
                "coinbase": round(self.oracle.coinbase.current_price, 4),
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
                "late_mom_enabled": config.LATE_MOMENTUM_ENABLED,
                "late_mom_open": len(self._late_mom),
                "late_mom_session": round(self._late_mom_session, 2),
                "cert_shadow_enabled": config.CERTAINTY_SHADOW_ENABLED,
                "cert_shadow_open": len(self._cert_shadow),
                "cert_shadow_session": round(self._cert_shadow_session, 2),
                "maker_pending": len(self._maker_pending),
                "cert_avg_slip_cents": (round(100.0 * self._cert_slip_sum / self._cert_slip_n, 2)
                                        if self._cert_slip_n else 0.0),
                "cert_fills": self._cert_slip_n,
                "box_shadow_enabled": config.CERTAINTY_BOX_ENABLED,
                "box_shadow_open": len(self._box_shadow),
                "box_shadow_session": round(self._box_shadow_session, 2),
            },
            "position": state.get_open_position(self.asset) or self._open_cert_position(),
            "risk_status": self.risk.status_str(),
        }

    def _open_cert_position(self) -> Optional[dict]:
        """Surface an OPEN certainty/feed-lag shadow as the dashboard position. The cert leg
        is a paper shadow (no real `positions` row), so without this the trade log shows OPEN
        while the OPEN POSITION panel says 'No open position'. Cert shadows are popped on
        resolution, so anything left in _cert_shadow is genuinely live."""
        # Live certainty entries are sentinels (no price/shares) — their real position shows
        # via state.get_open_position; only paper shadows surface here.
        shadows = {k: v for k, v in self._cert_shadow.items() if not v.get("live")}
        if not shadows:
            return None
        start_ts = max(shadows)                     # most recent open shadow
        cs = shadows[start_ts]
        px = cs.get("price") or 0.0
        return {
            "side": cs.get("side"),
            "entry_price": px,
            "size_usdc": round((cs.get("shares") or 0.0) * px, 2),
            "order_type": "CERTAINTY",
        }


class BotRunner:
    """Owns the shared DB, the asset workers, and the aggregated dashboard state."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        state.init_db()   # must precede RiskGuard, which reads daily P&L
        healed = state.reconcile_taker_ledger()       # backfill resolved/cancelled takers
        if healed:
            logger.info(f"Reconciled {healed} TAKER ledger row(s) with resolved outcomes")
        pruned = state.prune_old_data(config.TICK_RETENTION_DAYS)  # bound DB growth on VPS
        if pruned:
            logger.info(f"Pruned {pruned} old tick/signal row(s) (> {config.TICK_RETENTION_DAYS}d)")

        # One shared kill switch for every worker — flipped from the dashboard. When SET,
        # workers place no new LIVE orders; open positions still resolve.
        self.live_halt = threading.Event()
        # One shared cross-asset correlation guard — caps simultaneous same-side LIVE certainty
        # bets across assets per window (fake-diversification protection).
        self.corr_guard = WindowExposureGuard()
        self.workers = {a: AssetWorker(a, paper_mode=paper_mode, live_halt=self.live_halt,
                                       corr_guard=self.corr_guard)
                        for a in config.ASSETS}
        self._dash_state: dict = {}
        self._last_maintenance = time.time()
        self._stop = threading.Event()

    def get_dashboard_state(self) -> dict:
        return self._dash_state

    def toggle_live_halt(self) -> bool:
        """Dashboard kill switch. Returns the new halted state (True = LIVE trading stopped)."""
        if self.live_halt.is_set():
            self.live_halt.clear()
            logger.warning("LIVE trading RESUMED via dashboard kill switch")
        else:
            self.live_halt.set()
            logger.warning("LIVE trading STOPPED via dashboard kill switch — "
                           "no new live orders will be placed")
        return self.live_halt.is_set()

    def start(self):
        for w in self.workers.values():
            w.start()
        logger.info(
            f"Bot started in {'PAPER' if self.paper_mode else 'LIVE'} mode | "
            f"assets: {', '.join(self.workers)} | waiting for windows and feeds..."
        )
        # The main thread aggregates the dashboard state once per second; the workers
        # run their own loops. Ctrl-C lands here.
        try:
            while not self._stop.is_set():
                self._update_dash_state()
                self._maybe_maintain()
                time.sleep(config.STATE_PUSH_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            for w in self.workers.values():
                w.stop()
                w.executor.cancel_open_order()

    def _maybe_maintain(self):
        """Periodically reclaim DB space and truncate the WAL so a multi-day session stays
        bounded and crash-safe. Cheap and off the hot per-tick path (runs ~hourly)."""
        if time.time() - self._last_maintenance < config.MAINTENANCE_INTERVAL_SECS:
            return
        self._last_maintenance = time.time()
        try:
            pruned = state.prune_old_data(config.TICK_RETENTION_DAYS)
            state.checkpoint()
            if pruned:
                logger.info(f"Maintenance: pruned {pruned} old tick/signal row(s); WAL checkpointed")
        except Exception as exc:
            logger.warning(f"Maintenance pass failed: {exc}")

    def _update_dash_state(self):
        try:
            daily = state.get_daily_stats()
            overall = state.get_overall_stats()
            decided = daily.get("wins", 0) + daily.get("losses", 0)
            win_rate = daily.get("wins", 0) / decided if decided > 0 else 0.0
            # Global status: HALTED if the shared daily-loss guard tripped (live), else
            # the mode label. Per-asset cooldowns show in each asset's risk_status.
            any_halt = any(w.risk.is_halted for w in self.workers.values())
            live_stopped = self.live_halt.is_set()
            if self.paper_mode:
                status = "PAPER"
            elif live_stopped:
                status = "STOPPED"
            elif any_halt:
                status = "HALTED"
            else:
                status = "LIVE"
            self._dash_state = {
                "ts": time.time(),
                "bot": {
                    "mode": "PAPER" if self.paper_mode else "LIVE",
                    "status": status,
                    "live_halt": live_stopped,
                    "assets": list(self.workers.keys()),
                    "daily_pnl": round(daily.get("net_pnl", 0.0), 2),
                    "daily_trades": daily.get("trades", 0),
                    "win_rate": round(win_rate, 3),
                    "rebates_today": round(daily.get("rebates", 0.0), 3),
                    "overall_pnl": round(overall.get("net_pnl", 0.0)
                                         + overall.get("rebates", 0.0), 2),
                    "overall_trades": overall.get("trades", 0),
                },
                "assets": {a: w.snapshot for a, w in self.workers.items() if w.snapshot},
                "ledger": state.get_recent_ledger(limit=30),
                "recent_trades": state.get_recent_trades(limit=20),
            }
        except Exception as exc:
            logger.error(f"Dashboard state aggregation error: {exc}")


def _ensure_healthy_db():
    """Refuse to run on a malformed database. A corrupt bot_state.db (e.g. from an unclean
    shutdown, or from `cp`-ing a live WAL file) silently drops writes and can crash the
    loop mid-week. If the existing file fails its integrity check, quarantine it aside and
    start fresh so the session still records clean data."""
    import os
    if not os.path.exists(config.DB_PATH):
        return
    if state.integrity_ok():
        return
    quarantine = f"{config.DB_PATH}.corrupt.{int(time.time())}"
    logger.error(f"DB {config.DB_PATH} FAILED integrity check — quarantining to "
                 f"{quarantine} and starting fresh. Recover with `sqlite3 {quarantine} "
                 f"'.recover' | sqlite3 recovered.db` if you need its rows.")
    for suffix in ("", "-wal", "-shm"):
        src = config.DB_PATH + suffix
        if os.path.exists(src):
            try:
                os.rename(src, quarantine + suffix)
            except OSError as exc:
                logger.error(f"Could not move {src} aside: {exc}")


def main():
    args = parse_args()
    setup_logging()
    paper_mode = (args.mode == "paper")
    _ensure_healthy_db()

    if args.assets:
        wanted = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
        bad = [a for a in wanted if a not in config.ASSET_PARAMS]
        if bad:
            logger.error(f"Unknown asset(s): {', '.join(bad)} "
                         f"(available: {', '.join(config.ASSET_PARAMS)})")
            return
        config.ASSETS = wanted

    if paper_mode:
        logger.info("=" * 60)
        logger.info(f"  PAPER MODE — no real orders | assets: {', '.join(config.ASSETS)}")
        logger.info("=" * 60)
    else:
        logger.warning("=" * 60)
        logger.warning(f"  LIVE MODE — real USDC at risk | assets: {', '.join(config.ASSETS)}")
        logger.warning("=" * 60)
        if not config.PRIVATE_KEY or not config.CLOB_API_KEY:
            logger.error("PRIVATE_KEY and CLOB_API_KEY must be set in .env for live mode")
            return

    runner = BotRunner(paper_mode=paper_mode)
    if not args.no_dashboard:
        start_dashboard_server(runner.get_dashboard_state,
                               toggle_fn=runner.toggle_live_halt)
    runner.start()


if __name__ == "__main__":
    main()
