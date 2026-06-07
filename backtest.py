"""
backtest.py — Forward-test the model + strategy against our own recorded data.

Historical Polymarket L2 order-book data is not downloadable, so we record ticks live
(see state.ticks / state.outcomes via the running bot) and replay them here. This is an
honest forward-test, not a look-ahead backtest.

Two things are measured:
  1. MODEL CALIBRATION — is P(Up) trustworthy? Brier score + a calibration table
     (predicted probability vs realized UP frequency). A `--vol-mult` sweep finds the
     σ scaling that best calibrates the barrier model.
  2. STRATEGY P&L — replay the fee-net taker rule per window using recorded asks and
     the REAL resolved outcome; report win rate, profit factor, EV/trade, drawdown.

Usage:
    python backtest.py                # score recorded data with current config
    python backtest.py --sweep        # sweep vol-mult to minimise Brier score
    python backtest.py --vol-mult 1.3 # score with a specific σ scaling
"""

import argparse
import sqlite3
import config
import pricing
from signal_engine import barrier_p_up


def _load(include_fallback: bool = False):
    """
    Return tick rows joined to their resolved outcome. By default ONLY windows resolved
    from the REAL Polymarket outcome are used: fallback windows were settled on our own
    oracle price (the model's own input), so calibrating on them is circular and makes the
    model look better than it is. Pass include_fallback=True to inspect everything.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    src_filter = "" if include_fallback else "AND o.resolution_source = 'REAL'"
    rows = conn.execute(f"""
        SELECT t.*, o.winning_side
        FROM ticks t
        JOIN outcomes o ON o.start_ts = t.start_ts
        WHERE o.winning_side IN ('UP', 'DOWN')
          {src_filter}
          AND t.ref_price > 0 AND t.oracle_price > 0
        ORDER BY t.ts
    """).fetchall()
    conn.close()
    return rows


def _p_up(r, vol_mult: float) -> float:
    p, _ = barrier_p_up(
        r["oracle_price"], r["ref_price"], r["t_remaining"],
        r["realized_vol"] or config.VOL_FLOOR_PER_SEC,
        r["cex_basis_bp"] or 0.0, r["momentum_bp"] or 0.0, vol_mult=vol_mult,
    )
    return p


# ─── Calibration ──────────────────────────────────────────────────────────────

def calibration(rows, vol_mult: float, bins: int = 10):
    brier = 0.0
    buckets = [[0.0, 0, 0] for _ in range(bins)]   # sum_p, n, n_up
    for r in rows:
        p = _p_up(r, vol_mult)
        y = 1 if r["winning_side"] == "UP" else 0
        brier += (p - y) ** 2
        b = min(bins - 1, int(p * bins))
        buckets[b][0] += p
        buckets[b][1] += 1
        buckets[b][2] += y
    brier = brier / len(rows) if rows else float("nan")
    return brier, buckets


def print_calibration(rows, vol_mult: float):
    brier, buckets = calibration(rows, vol_mult)
    print(f"\nMODEL CALIBRATION (vol_mult={vol_mult}, n={len(rows)} ticks)")
    print(f"  Brier score: {brier:.4f}   (0=perfect, 0.25=coin-flip baseline)")
    print(f"  {'pred bucket':>12} | {'mean pred':>9} | {'emp UP freq':>11} | {'n':>6}")
    for i, (sp, n, nup) in enumerate(buckets):
        if n == 0:
            continue
        print(f"  {i/10:.1f}-{(i+1)/10:.1f}      | {sp/n:>9.3f} | "
              f"{nup/n:>11.3f} | {n:>6}")
    return brier


# ─── Strategy P&L (taker leg) ──────────────────────────────────────────────────

def simulate_taker(rows, vol_mult: float):
    """One trade per window: first tick in the taker zone that clears MIN_EV_TAKER."""
    by_window: dict[int, list] = {}
    for r in rows:
        by_window.setdefault(r["start_ts"], []).append(r)

    pnls = []
    for start_ts, ticks in by_window.items():
        winning = ticks[0]["winning_side"]
        for r in sorted(ticks, key=lambda x: -x["t_remaining"]):
            t = r["t_remaining"]
            if not (config.TAKER_ZONE_END <= t <= config.TAKER_ZONE_START):
                continue
            p = _p_up(r, vol_mult)
            up_ask, dn_ask = r["up_ask"], r["down_ask"]
            spread = (up_ask - r["up_bid"]) if (up_ask and r["up_bid"]) else None
            if spread is not None and spread > config.MAX_SPREAD:
                continue
            # Evaluate both sides, take the best +EV that clears the threshold.
            best = None
            if up_ask:
                ev = pricing.taker_ev_per_share(p, up_ask)
                if ev >= config.MIN_EV_TAKER:
                    best = ("UP", up_ask, ev)
            if dn_ask:
                ev = pricing.taker_ev_per_share(1 - p, dn_ask)
                if ev >= config.MIN_EV_TAKER and (best is None or ev > best[2]):
                    best = ("DOWN", dn_ask, ev)
            if best is None:
                continue
            side, entry, _ = best
            shares = config.MAX_STAKE_PER_MARKET / entry
            won = (side == winning)
            fee = pricing.taker_fee_per_share(entry) * shares
            pnl = ((1.0 - entry) if won else -entry) * shares - fee
            pnls.append(pnl)
            break   # one trade per window

    return pnls


def report_pnl(pnls):
    print(f"\nSTRATEGY P&L (taker leg)")
    if not pnls:
        print("  No trades triggered on recorded data.")
        return
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    net = sum(pnls)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    # Max drawdown on the cumulative equity curve.
    eq, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    print(f"  trades        : {n}")
    print(f"  win rate      : {len(wins)/n:.1%}")
    print(f"  net P&L       : ${net:+.2f}")
    print(f"  EV / trade    : ${net/n:+.3f}")
    print(f"  profit factor : {pf:.2f}   (target ≥ 1.5)")
    print(f"  max drawdown  : ${mdd:.2f}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol-mult", type=float, default=config.VOL_MULT)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--include-fallback", action="store_true",
                    help="also use oracle-fallback-resolved windows (circular — inspection only)")
    args = ap.parse_args()

    rows = _load(include_fallback=args.include_fallback)
    if not rows:
        print("No REAL-resolved tick data yet. Run `python main.py --mode paper` for a while "
              "to record ticks + outcomes, then re-run backtest.py.\n"
              "(Most BTC 5-min windows resolve via oracle fallback; use --include-fallback "
              "to inspect those, but do NOT calibrate on them.)")
        return

    n_windows = len({r["start_ts"] for r in rows})
    if n_windows < 100:
        print(f"⚠  Only {n_windows} resolved window(s) of data — far below the ~300 needed "
              f"for trustworthy calibration. Treat results as directional, not final.\n")

    if args.sweep:
        print("Vol-mult sweep (lower Brier = better calibrated):")
        best = None
        for vm in (0.5, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0, 3.0):
            brier, _ = calibration(rows, vm)
            flag = ""
            if best is None or brier < best[1]:
                best, flag = (vm, brier), "  <- best"
            print(f"  vol_mult={vm:>4}:  Brier={brier:.4f}{flag}")
        print(f"\nBest vol_mult = {best[0]} (Brier {best[1]:.4f}). "
              f"Consider tuning VOL_WINDOW_SECS or applying this scaling.")
        print_calibration(rows, best[0])
        report_pnl(simulate_taker(rows, best[0]))
    else:
        print_calibration(rows, args.vol_mult)
        report_pnl(simulate_taker(rows, args.vol_mult))


if __name__ == "__main__":
    main()
