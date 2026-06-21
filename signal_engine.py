"""
signal_engine.py — Fair-value model + layered trading strategy.

Probability (driftless arithmetic random-walk barrier):

    σ_price = S · σ_ret · √t_remaining        (σ_ret = realized per-second return vol)
    σ_total = √( σ_price²  +  (k · S · basis_bp/1e4)² )   # widen for CEX disagreement
    z       = (S − ref + drift) / σ_total
    P(Up)   = Φ(z)

This is the theoretically-correct model for "will the price be ≥ ref after t seconds"
under a near-driftless short horizon, and is directly calibratable (see backtest.py).

Two strategy modes share this one fair value (user choice: "both, layered"):
  - TAKER (default): fire an IOC only when fee-net EV per share clears MIN_EV_TAKER,
    and only inside the mid-window zone (TAKER_ZONE_*). We do NOT attempt last-second
    sniping — we cannot win that latency race against co-located bots.
  - MAKER_FARM: early in the window, when the spread is wide, post a resting quote on
    the side whose maker EV (incl. rebate, minus an adverse-selection haircut) is +EV.
    (True two-sided delta-neutral quoting is a live-execution follow-up.)
"""

import time
import math
from dataclasses import dataclass
from typing import Optional
from scipy.stats import norm
from loguru import logger
import config
import pricing
from market_discovery import MarketWindow


# ─── Signal Action Enum ────────────────────────────────────────────────────────
class Action:
    ARB_PAIR        = "ARB_PAIR"      # risk-free: buy UP+DOWN < $1
    POST_FARM       = "POST_FARM"     # two-sided delta-neutral reward farm
    IOC_UP          = "IOC_UP"
    IOC_DOWN        = "IOC_DOWN"
    LATE_MOMENTUM   = "LATE_MOMENTUM"  # EXPERIMENTAL paper-only late-leader bet (shadow leg)
    SKIP            = "SKIP"
    NO_MARKET       = "NO_MARKET"
    FEEDS_DOWN      = "FEEDS_DOWN"
    # retained for compatibility (no longer emitted directly)
    POST_MAKER_UP   = "POST_MAKER_UP"
    POST_MAKER_DOWN = "POST_MAKER_DOWN"


@dataclass
class Signal:
    ts:             float
    market_id:      str
    btc_ref:        float
    btc_now:        float
    distance_bp:    float
    momentum_bp:    float
    time_remaining: float
    phase:          int          # 1=farm zone, 2=taker zone, 3=closeout
    p_up:           float
    p_down:         float
    sigma_price:    float        # σ of terminal price ($), incl. basis inflation
    up_ask:         Optional[float]
    down_ask:       Optional[float]
    up_bid:         Optional[float]
    down_bid:       Optional[float]
    up_spread:      Optional[float]
    edge_up:        float        # p_up - up_ask  (raw, pre-fee; diagnostic)
    edge_down:      float        # p_down - down_ask
    ev_up:          float        # fee-net EV/share for taking UP at ask
    ev_down:        float        # fee-net EV/share for taking DOWN at ask
    action:         str
    reason:         str
    mode:           str = "NONE"        # 'TAKER' | 'FARM' | 'ARB' | 'NONE'
    order_side:     Optional[str] = None
    order_price:    Optional[float] = None
    order_type:     Optional[str] = None   # 'MAKER' | 'TAKER'
    # YES/NO arbitrage
    arb_edge:       float = 0.0          # locked $/share from buying both sides
    # Reward farm (two-sided)
    mid:            Optional[float] = None
    farm_up_px:     Optional[float] = None
    farm_down_px:   Optional[float] = None
    farm_size:      float = 0.0
    est_reward_per_sec: float = 0.0


def barrier_p_up(btc_now: float, btc_ref: float, t_remaining: float,
                 sigma_ret: float, basis_bp: float = 0.0,
                 momentum_bp: float = 0.0, vol_mult: float = 1.0) -> tuple[float, float]:
    """
    Pure driftless-barrier P(Up) and σ_price. Reused by the live engine AND backtest.py
    (the `vol_mult` knob lets the backtester sweep σ for calibration).
    """
    t = max(1e-6, t_remaining)
    sigma_price = max(1e-9, btc_now * sigma_ret * vol_mult * math.sqrt(t))
    basis_price = btc_now * (basis_bp / 1e4)
    sigma_total = math.sqrt(sigma_price ** 2 +
                            (config.BASIS_VOL_INFLATE * basis_price) ** 2)
    drift = config.DRIFT_WEIGHT * (momentum_bp / 1e4) * btc_now
    z = (btc_now - btc_ref + drift) / sigma_total
    p = float(norm.cdf(z))
    return max(0.01, min(0.99, p)), sigma_total


class FairValueModel:
    """Pure probability model. Depends only on oracle + window; unit-testable."""

    def __init__(self, oracle, binance):
        self._oracle = oracle
        self._binance = binance

    def p_up(self, btc_now: float, btc_ref: float, t_remaining: float,
             sigma_ret: float, basis_bp: float, momentum_bp: float) -> tuple[float, float]:
        """Return (P(Up), σ_price). Caller supplies the live inputs."""
        return barrier_p_up(btc_now, btc_ref, t_remaining, sigma_ret,
                            basis_bp, momentum_bp, vol_mult=config.VOL_MULT)


class SignalEngine:

    def __init__(self, oracle, binance, book):
        self._oracle = oracle
        self._binance = binance
        self._book = book
        self._model = FairValueModel(oracle, binance)

    # ─── Main entry point ─────────────────────────────────────────────────────

    def evaluate(self, window: MarketWindow) -> Signal:
        ts = time.time()
        market_id = window.condition_id
        btc_ref = window.reference_price
        btc_now = self._oracle.price
        t_rem = window.time_remaining
        phase = self._phase(t_rem)
        momentum_bp = self._binance.momentum_15s
        basis_bp = self._oracle.cex_basis_bp
        sigma_ret = self._oracle.realized_vol_per_sec

        distance_bp = (btc_now - btc_ref) / btc_ref * 10_000 if btc_ref else 0.0
        p_up, sigma_price = self._model.p_up(
            btc_now, btc_ref, t_rem, sigma_ret, basis_bp, momentum_bp
        )
        p_down = round(1.0 - p_up, 4)
        p_up = round(p_up, 4)

        up_ask, down_ask = self._book.up_ask, self._book.down_ask
        up_bid, down_bid = self._book.up_bid, self._book.down_bid
        up_spread = self._book.up_spread

        edge_up = p_up - (up_ask if up_ask is not None else 1.0)
        edge_down = p_down - (down_ask if down_ask is not None else 1.0)
        ev_up = pricing.taker_ev_per_share(p_up, up_ask) if up_ask is not None else -1.0
        ev_down = pricing.taker_ev_per_share(p_down, down_ask) if down_ask is not None else -1.0
        arb_edge = pricing.pair_arb_edge(up_ask, down_ask)
        mid = round((up_bid + up_ask) / 2, 4) if (up_bid is not None and up_ask is not None) else None

        def mk(action, reason, **kw):
            return Signal(
                ts=ts, market_id=market_id, btc_ref=btc_ref, btc_now=btc_now,
                distance_bp=distance_bp, momentum_bp=momentum_bp, time_remaining=t_rem,
                phase=phase, p_up=p_up, p_down=p_down, sigma_price=round(sigma_price, 4),
                up_ask=up_ask, down_ask=down_ask, up_bid=up_bid, down_bid=down_bid,
                up_spread=up_spread, edge_up=edge_up, edge_down=edge_down,
                ev_up=round(ev_up, 4), ev_down=round(ev_down, 4),
                arb_edge=round(arb_edge, 4), mid=mid,
                action=action, reason=reason, **kw,
            )

        # ── Guards ───────────────────────────────────────────────────────────────
        if not self._oracle.connected or not self._book.connected:
            return mk(Action.FEEDS_DOWN, "price/book feed not connected")

        # ── EXPERIMENTAL late-window momentum (paper SHADOW leg; OFF by default). ──
        # Sits BEFORE the MIN_SECONDS_TO_TRADE guard because it deliberately fires inside
        # the last ~25s. When LATE_MOMENTUM_ENABLED is False this is a complete no-op.
        if config.LATE_MOMENTUM_ENABLED and window.has_reference:
            lm = self._late_momentum(mk, t_rem, up_ask, down_ask, up_spread, p_up, p_down)
            if lm is not None:
                return lm

        if t_rem < config.MIN_SECONDS_TO_TRADE:
            return mk(Action.SKIP, f"T-{t_rem:.0f}s < MIN_SECONDS_TO_TRADE")

        # ── 1. YES/NO ARBITRAGE (risk-free, runs whenever the book dislocates) ────
        if (config.ARB_ENABLED and arb_edge >= config.MIN_ARB_EDGE
                and up_ask is not None and down_ask is not None):
            s = mk(Action.ARB_PAIR,
                   f"ARB up_ask+down_ask={up_ask+down_ask:.3f} locked={arb_edge:.4f}/sh",
                   mode="ARB")
            s.farm_size = config.ARB_SIZE_USDC
            return s

        # ── 2. TAKER mode: mid-window, fee-net EV gating ──────────────────────────
        # ONLY the directional taker needs the strike (it prices price-vs-Price-to-Beat).
        # Safety rail: an enormous model-vs-market gap on a real book is far more likely
        # model error than edge (it's what bled money in paper). Cap how far we'll bet
        # against the market until the model is calibrated. (MAX_..=1.0 disables this.)
        cap = config.MAX_MODEL_MARKET_DISAGREE
        up_sane = up_ask is None or (p_up - up_ask) <= cap
        down_sane = down_ask is None or (p_down - down_ask) <= cap
        spread_ok = up_spread is None or up_spread <= config.MAX_SPREAD
        if (config.DIRECTIONAL_TAKER_ENABLED and window.has_reference and spread_ok
                and config.TAKER_ZONE_END <= t_rem <= config.TAKER_ZONE_START):
            if (ev_up >= config.MIN_EV_TAKER and up_ask is not None and up_sane
                    and up_ask >= config.MIN_TAKER_ENTRY):
                s = mk(Action.IOC_UP,
                       f"TAKER P(Up)={p_up:.3f} ev={ev_up:.4f}≥{config.MIN_EV_TAKER}",
                       mode="TAKER")
                s.order_side, s.order_price, s.order_type = "UP", up_ask, "TAKER"
                return s
            if (ev_down >= config.MIN_EV_TAKER and down_ask is not None and down_sane
                    and down_ask >= config.MIN_TAKER_ENTRY):
                s = mk(Action.IOC_DOWN,
                       f"TAKER P(Down)={p_down:.3f} ev={ev_down:.4f}≥{config.MIN_EV_TAKER}",
                       mode="TAKER")
                s.order_side, s.order_price, s.order_type = "DOWN", down_ask, "TAKER"
                return s

        # ── 3. REWARD FARM: two-sided, delta-neutral, early window ────────────────
        # Gated on a LIVE reward pool. BTC 5-min markets carry rewards.rates=null (verified
        # vs CLOB API 2026-06-08) — quoting there earns ~nothing and only invites adverse
        # selection, so we skip. Farming is an edge on EVENT markets that have a real pool.
        if (config.FARM_ENABLED and t_rem > config.REBATE_FARM_UNTIL
                and getattr(window, "rewards_active", False)):
            farm = self._reward_farm(mk, window, mid, up_bid, up_ask, down_bid, down_ask)
            if farm is not None:
                return farm

        farm_note = "" if getattr(window, "rewards_active", False) else " farm=no-pool"
        return mk(Action.SKIP, f"no signal: arb={arb_edge:.4f} ev_up={ev_up:.4f} "
                               f"ev_down={ev_down:.4f}{farm_note}")

    # ─── Late-window momentum leg (EXPERIMENTAL, paper shadow) ──────────────────

    def _late_momentum(self, mk, t_rem, up_ask, down_ask, up_spread, p_up, p_down):
        """
        Bet the LATE LEADER. Inside the late band, if exactly one side's ask is in
        [THRESHOLD, MAX_ASK] (the two asks sum to ~1, so at most one can lead), emit a
        paper-only bet on it. The edge (validated only weakly: ~10 OOS bets) is that the
        late leader is under-priced. Gating is PRICE-based — that is the signal we tested;
        the model p is recorded in the reason only as a diagnostic, not used as a gate.
        """
        if not (config.LATE_MOMENTUM_ZONE_END <= t_rem <= config.LATE_MOMENTUM_ZONE_START):
            return None
        if up_spread is not None and up_spread > config.MAX_SPREAD:
            return None
        lo, hi = config.LATE_MOMENTUM_THRESHOLD, config.LATE_MOMENTUM_MAX_ASK
        side = ask = model_p = None
        if up_ask is not None and lo <= up_ask <= hi:
            side, ask, model_p = "UP", up_ask, p_up
        elif down_ask is not None and lo <= down_ask <= hi:
            side, ask, model_p = "DOWN", down_ask, p_down
        if side is None:
            return None
        s = mk(Action.LATE_MOMENTUM,
               f"LATE-MOM {side}@{ask:.2f} model_p={model_p:.2f} T-{t_rem:.0f}s "
               f"(paper shadow · EXPERIMENTAL)",
               mode="LATE_MOM")
        s.order_side, s.order_price, s.order_type = side, ask, "TAKER"
        return s

    # ─── Certainty / feed-lag gate (APPROACH.md §3① · paper shadow) ─────────────

    def certainty_shadow(self, s: Signal) -> Optional[tuple]:
        """
        APPROACH.md §3① certainty/feed-lag pick, as an ISOLATED read of the already-computed
        signal. Deliberately NOT routed through evaluate()/Action dispatch, so it can neither
        preempt nor be preempted by the real legs — main records it on its own paper ledger.
        Mirrors backtest.simulate_certainty: take the confident side only when the book still
        underprices it. Returns (side, ask, size_usdc) or None.

        Validated 2026-06-21 (sy/cert_zone_experiment.py, recovered 8,044 windows, realistic
        +1-tick fill): the edge is concentrated in the LAST 10-45s (PF 1.59-2.57, clears the
        1.5 gate) vs the mid-window (PF 1.14). So the zone is extended to CERTAINTY_ZONE_END
        (10s) and a window-delta move gate + late-slice sizing are added.
        """
        if not config.CERTAINTY_SHADOW_ENABLED:
            return None
        t = s.time_remaining
        if not (config.CERTAINTY_ZONE_END <= t <= config.CERTAINTY_ZONE_START):
            return None
        # Window-Delta gate (winners' dominant signal): the oracle must already have moved.
        if abs(s.distance_bp) < config.CERTAINTY_MIN_MOVE_BP:
            return None
        floor = config.CERTAINTY_FLOOR
        # p_up + p_down = 1 ⇒ at most one side clears a floor ≥ 0.5 (no coin-flip trades).
        if s.p_up >= floor and s.up_ask is not None:
            side, p_side, ask, bid = "UP", s.p_up, s.up_ask, s.up_bid
        elif s.p_down >= floor and s.down_ask is not None:
            side, p_side, ask, bid = "DOWN", s.p_down, s.down_ask, s.down_bid
        else:
            return None
        if bid is not None and (ask - bid) > config.MAX_SPREAD:
            return None
        if ask > config.CERTAINTY_MAX_ASK:                 # too rich — fee eats the edge
            return None
        if (p_side - ask) < config.CERTAINTY_LAG_MARGIN:   # book hasn't lagged enough
            return None
        if pricing.taker_ev_per_share(p_side, ask) < 0:    # not fee-net positive
            return None
        # Confidence sizing: bump to the late-slice notional inside the validated late zone.
        size = config.CERTAINTY_SIZE_USDC
        if t <= config.CERTAINTY_LATE_FROM:
            size = config.CERTAINTY_LATE_SIZE_USDC
        size = min(size, config.CERTAINTY_MAX_SIZE_USDC)
        return side, ask, size

    # ─── Two-sided liquidity-reward farm leg ───────────────────────────────────

    def _reward_farm(self, mk, window, mid, up_bid, up_ask, down_bid, down_ask) -> Optional[Signal]:
        # Need a real two-sided book on both tokens.
        if None in (up_bid, up_ask, down_bid, down_ask) or mid is None:
            return None
        if not (config.FARM_MIN_MID <= mid <= config.FARM_MAX_MID):
            return None
        tick = _tick(window.tick_size)
        band = window.rewards_max_spread or (config.MAKER_QUOTE_OFFSET * 2)
        # Reward score is quadratic in proximity to mid, so quote as TIGHT as possible:
        # FARM_QUOTE_TICKS off mid, hard-capped at FARM_MAX_SPREAD_FRAC of the reward band.
        offset = min(config.FARM_QUOTE_TICKS * tick, band * config.FARM_MAX_SPREAD_FRAC)
        offset = max(offset, tick)
        up_px = pricing.round_to_tick(mid - offset, tick)
        down_px = pricing.round_to_tick((1.0 - mid) - offset, tick)
        score = pricing.reward_score(offset, band)
        s = mk(Action.POST_FARM,
               f"FARM two-sided UP@{up_px:.2f}/DOWN@{down_px:.2f} mid={mid:.2f} "
               f"off={offset*100:.1f}¢ score={score:.2f} size=${config.FARM_SIZE_USDC:.0f}/side",
               mode="FARM")
        s.farm_up_px = up_px
        s.farm_down_px = down_px
        s.farm_size = config.FARM_SIZE_USDC
        s.est_reward_per_sec = pricing.farm_reward_per_sec(config.FARM_SIZE_USDC * 2, score=score)
        return s

    # ─── Phase / helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _phase(t_remaining: float) -> int:
        """1 = rebate-farm zone (early), 2 = taker zone (mid), 3 = closeout (late)."""
        if t_remaining > config.TAKER_ZONE_START:
            return 1
        if t_remaining > config.TAKER_ZONE_END:
            return 2
        return 3


def _tick(tick_size: str) -> float:
    try:
        return float(tick_size)
    except (TypeError, ValueError):
        return 0.01
