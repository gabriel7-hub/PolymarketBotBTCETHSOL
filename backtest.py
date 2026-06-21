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


def _load(include_fallback: bool = False, asset: str = None, db_path: str = None):
    """
    Return tick rows joined to their resolved outcome (multi-asset: joined on BOTH
    asset and start_ts — BTC/ETH/SOL windows share the same start_ts grid). By default
    ONLY windows resolved from the REAL Polymarket outcome are used: fallback windows
    were settled on our own oracle price (the model's own input), so calibrating on them
    is circular and makes the model look better than it is. Pass include_fallback=True
    to inspect everything; pass asset='ETH' etc. to filter one asset.
    """
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    src_filter = "" if include_fallback else "AND o.resolution_source = 'REAL'"
    asset_filter = "AND t.asset = :asset" if asset else ""
    rows = conn.execute(f"""
        SELECT t.*, o.winning_side
        FROM ticks t
        JOIN outcomes o ON o.asset = t.asset AND o.start_ts = t.start_ts
        WHERE o.winning_side IN ('UP', 'DOWN')
          {src_filter}
          {asset_filter}
          AND t.ref_price > 0 AND t.oracle_price > 0
        ORDER BY t.ts
    """, {"asset": asset}).fetchall()
    conn.close()
    return rows


def _wkey(r):
    """Window key: (asset, start_ts) — start_ts alone collides across assets."""
    try:
        a = r["asset"]
    except (KeyError, IndexError):
        a = "BTC"
    return (a or "BTC", r["start_ts"])


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

def simulate_taker(rows, vol_mult: float, min_ev: float = None):
    """One trade per window: first tick in the taker zone that clears min_ev
    (defaults to config.MIN_EV_TAKER)."""
    min_ev = config.MIN_EV_TAKER if min_ev is None else min_ev
    by_window: dict[tuple, list] = {}
    for r in rows:
        by_window.setdefault(_wkey(r), []).append(r)

    pnls = []
    for wkey, ticks in by_window.items():
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
                if ev >= min_ev:
                    best = ("UP", up_ask, ev)
            if dn_ask:
                ev = pricing.taker_ev_per_share(1 - p, dn_ask)
                if ev >= min_ev and (best is None or ev > best[2]):
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


# ─── §3① Certainty / feed-lag gate (MEASUREMENT ONLY — not wired to live) ───────

def simulate_certainty(rows, vol_mult: float, certainty_floor: float = 0.80,
                       lag_margin: float = 0.03, max_ask: float = 0.97,
                       min_ev: float = 0.0, slippage: float = 0.0,
                       zone_end: float = None, zone_start: float = None,
                       min_move_bp: float = None):
    """
    APPROACH.md §3① — buy NEAR-CERTAIN outcomes the book hasn't repriced yet.

    The leaderboard research + our own calibration say the durable edge is NOT predicting the
    5-min random walk (that taker leg fails out-of-sample) but entering a side only when:
      • the recalibrated model is already CONFIDENT      (p_side ≥ certainty_floor), AND
      • the book still UNDERPRICES that confidence (lag)  (ask ≤ p_side − lag_margin), AND
      • the ask isn't so high the fee eats the edge       (ask ≤ max_ask),
    with the usual spread + fee-net-EV guards. p_up + p_down = 1, so at most one side clears a
    floor ≥ 0.5 — no coin-flip-zone trades by construction. One trade per window, flat stake
    (kept flat for an apples-to-apples comparison with simulate_taker; Kelly sizing is a later,
    separately-validated refinement). This is a backtest probe only; nothing here can place an
    order.

    `slippage` ($/share) stresses fill realism the way config.PAPER_FILL_REALISM does live: we
    DECIDE on the displayed ask but FILL one adverse tick worse (entry = min(0.99, ask+slippage)).
    Only the latency-tick component is modelled — recorded ticks store top-of-book only, so the
    VWAP depth-walk over the ladder (the other half of live realism) cannot be reproduced from
    this data and is NOT captured.
    """
    # Gate bounds default to the live certainty config (the leg these numbers govern).
    ze = config.CERTAINTY_ZONE_END if zone_end is None else zone_end
    zs = config.CERTAINTY_ZONE_START if zone_start is None else zone_start
    mb = config.CERTAINTY_MIN_MOVE_BP if min_move_bp is None else min_move_bp

    by_window: dict[tuple, list] = {}
    for r in rows:
        by_window.setdefault(_wkey(r), []).append(r)

    pnls = []
    for wkey, ticks in by_window.items():
        winning = ticks[0]["winning_side"]
        for r in sorted(ticks, key=lambda x: -x["t_remaining"]):
            t = r["t_remaining"]
            if not (ze <= t <= zs):
                continue
            # Window-Delta gate: oracle must already have moved >= mb bp from the strike.
            ref = r["ref_price"] or 0
            if ref > 0 and abs((r["oracle_price"] or 0) - ref) / ref * 1e4 < mb:
                continue
            p = _p_up(r, vol_mult)
            up_ask, dn_ask = r["up_ask"], r["down_ask"]
            # Confident side only (at most one of UP/DOWN can clear a floor ≥ 0.5).
            if p >= certainty_floor and up_ask:
                side, p_side, ask, bid = "UP", p, up_ask, r["up_bid"]
            elif (1 - p) >= certainty_floor and dn_ask:
                side, p_side, ask, bid = "DOWN", 1 - p, dn_ask, r["down_bid"]
            else:
                continue
            spread = (ask - bid) if (ask and bid) else None
            if spread is not None and spread > config.MAX_SPREAD:
                continue
            if ask > max_ask:                          # too rich — fee eats the edge
                continue
            if (p_side - ask) < lag_margin:            # book hasn't lagged enough — no edge
                continue
            if pricing.taker_ev_per_share(p_side, ask) < min_ev:
                continue
            # Decide on the displayed ask; fill one adverse tick worse (latency realism).
            entry = min(0.99, ask + slippage)
            shares = config.MAX_STAKE_PER_MARKET / entry
            won = (side == winning)
            fee = pricing.taker_fee_per_share(entry) * shares
            pnl = ((1.0 - entry) if won else -entry) * shares - fee
            pnls.append(pnl)
            break   # one trade per window

    return pnls


# ─── Out-of-sample validation (train/test split) ───────────────────────────────

def _pnl_metrics(pnls) -> dict:
    """Summary stats for a list of per-trade P&Ls."""
    if not pnls:
        return {"n": 0, "win": 0.0, "net": 0.0, "ev": 0.0, "pf": 0.0}
    n = len(pnls)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    return {
        "n": n,
        "win": sum(1 for p in pnls if p > 0) / n,
        "net": sum(pnls),
        "ev": sum(pnls) / n,
        "pf": (gw / gl) if gl > 0 else float("inf"),
    }


def _split_chrono(rows, train_frac: float = 0.7):
    """Split by WINDOW ((asset, start_ts)) chronologically: earliest train_frac of windows
    are train, the rest are test. Chronological (not random) so it honestly mimics choosing
    params on past data and trading them forward — no look-ahead leakage across a window."""
    starts = sorted({_wkey(r) for r in rows}, key=lambda k: (k[1], k[0]))
    cut = int(len(starts) * train_frac)
    train_starts = set(starts[:cut])
    train = [r for r in rows if _wkey(r) in train_starts]
    test = [r for r in rows if _wkey(r) not in train_starts]
    return train, test, len(train_starts), len(starts) - len(train_starts)


def validate(rows, train_frac: float = 0.7,
             vol_grid=(0.5, 0.7, 0.85, 1.0, 1.2, 1.5),
             ev_grid=(0.015, 0.03, 0.05, 0.07)):
    """
    Honest out-of-sample test. Pick (vol_mult, min_ev) that maximise TRAIN net P&L
    (subject to ≥10 trades), then report how those SAME params perform on unseen TEST
    windows. A strategy that only works in-sample collapses here.
    """
    train, test, n_tr, n_te = _split_chrono(rows, train_frac)
    print(f"\n{'='*64}\nOUT-OF-SAMPLE VALIDATION (chronological {train_frac:.0%}/{1-train_frac:.0%} split)")
    print(f"  train windows: {n_tr}   test windows: {n_te}")
    if n_te < 20:
        print(f"  ⚠  Only {n_te} test windows — out-of-sample result is INDICATIVE, not final.")

    # 1) Select params on TRAIN only. Require a ≥50% win rate so the search can't pick a
    #    degenerate deep-underdog combo (few big lucky wins, high σ) that maximises in-sample
    #    net but collapses out-of-sample — a real failure mode observed at 258 windows.
    best = None  # (net, vm, ev, metrics)
    print(f"\n  Param search on TRAIN (maximise net, require n≥15 and win≥50%):")
    for vm in vol_grid:
        for ev in ev_grid:
            m = _pnl_metrics(simulate_taker(train, vm, ev))
            if m["n"] >= 15 and m["win"] >= 0.50 and (best is None or m["net"] > best[0]):
                best = (m["net"], vm, ev, m)
    if best is None:
        print("  No param combo produced ≥10 train trades. Insufficient data.")
        return
    _, vm, ev, tm = best
    print(f"  → chosen on TRAIN: vol_mult={vm}, min_ev={ev}  "
          f"(train: n={tm['n']}, win={tm['win']:.1%}, net=${tm['net']:+.2f}, PF={tm['pf']:.2f})")

    # 2) Evaluate those frozen params on TEST.
    te = _pnl_metrics(simulate_taker(test, vm, ev))
    print(f"\n  OUT-OF-SAMPLE (TEST) with the frozen params:")
    if te["n"] == 0:
        print("    No trades triggered on test windows.")
    else:
        print(f"    trades={te['n']}  win={te['win']:.1%}  net=${te['net']:+.2f}  "
              f"EV/trade=${te['ev']:+.3f}  PF={te['pf']:.2f}  (live gate: PF≥1.5)")
        verdict = ("PASSES" if te["pf"] >= 1.5 and te["net"] > 0
                   else "PROFITABLE but below PF≥1.5 gate" if te["net"] > 0
                   else "FAILS — loses out-of-sample")
        print(f"    verdict: {verdict}")

    # 3) Baseline: how the CURRENT live config does on the same test windows.
    base = _pnl_metrics(simulate_taker(test, config.VOL_MULT, config.MIN_EV_TAKER))
    print(f"\n  For reference — CURRENT live config "
          f"(vol_mult={config.VOL_MULT}, min_ev={config.MIN_EV_TAKER}) on TEST:")
    print(f"    trades={base['n']}  win={base['win']:.1%}  net=${base['net']:+.2f}  PF={base['pf']:.2f}")
    return vm, ev, te


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


def report_buckets(db_path: str, asset: str = None):
    """
    Calibration of EXECUTED trades by entry price, from the positions table — works on
    any state.db copy (pass --db for a downloaded VPS file). This is the evidence base
    for MIN_TAKER_ENTRY: a bucket only pays if its actual win rate beats its average
    entry price plus the taker fee. The 2026-06-10 audit showed all edge in 0.50-0.65
    and pure bleed below 0.35; rerun this as data accrues before moving the floor.
    """
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
    asset_filter = "AND asset = :asset" if (asset and "asset" in cols) else ""
    rows = conn.execute(f"""
        SELECT entry_price, pnl_usdc, (outcome = 'WIN') AS win
        FROM positions
        WHERE status = 'RESOLVED' AND order_type = 'TAKER' {asset_filter}
    """, {"asset": asset}).fetchall()
    conn.close()
    if not rows:
        print(f"No resolved taker positions in {db_path}.")
        return

    label = f", {asset} only" if asset else ""
    print(f"\nEXECUTED-TRADE CALIBRATION by entry price  "
          f"({len(rows)} resolved takers, {db_path}{label})")
    print(f"  current MIN_TAKER_ENTRY = {config.MIN_TAKER_ENTRY}")
    print(f"  {'bucket':<12}{'n':>5}{'win%':>8}{'breakeven%':>12}{'edge_pts':>10}{'net P&L':>11}  verdict")
    edges = [(0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.0)]
    for lo, hi in edges:
        sub = [r for r in rows if lo <= r[0] < hi]
        if not sub:
            continue
        n = len(sub)
        win = 100.0 * sum(r[2] for r in sub) / n
        avg_entry = sum(r[0] for r in sub) / n
        # Breakeven win rate = entry + fee (fee in $/share == probability points here).
        breakeven = 100.0 * (avg_entry + pricing.taker_fee_per_share(avg_entry))
        edge = win - breakeven
        pnl = sum(r[1] for r in sub)
        if n < 50:
            verdict = "too few trades"
        elif edge >= 3:
            verdict = "EDGE"
        elif edge <= -3:
            verdict = "BLEED — keep below floor"
        else:
            verdict = "breakeven"
        print(f"  {f'{lo:.2f}-{hi:.2f}':<12}{n:>5}{win:>8.1f}{breakeven:>12.1f}"
              f"{edge:>+10.1f}{pnl:>11.2f}  {verdict}")
    print("  (need ≥50 trades/bucket and edge_pts ≥ +3 before lowering MIN_TAKER_ENTRY)")


def report_certainty(rows, vol_mult: float, certainty_floor: float,
                     lag_margin: float, max_ask: float):
    """Score the §3① certainty/feed-lag gate: full-sample P&L, per-asset, and an HONEST
    fixed-rule out-of-sample split (SAME params on chrono train/test — no param search, so it
    can't overfit the test set). Plus a sensitivity sweep over floor × lag_margin."""
    slip = config.PAPER_SLIPPAGE_TICKS * float(config.TICK_SIZE)   # adverse latency tick ($/share)
    print(f"\n{'='*64}\nCERTAINTY / FEED-LAG GATE  (APPROACH.md §3① — measurement only, not live)")
    print(f"  params: vol_mult={vol_mult}  certainty_floor={certainty_floor}  "
          f"lag_margin={lag_margin}  max_ask={max_ask}")
    print(f"  fill realism: IDEAL = fill at displayed ask;  REALISTIC = +{slip:.2f}/share "
          f"adverse tick (latency). Depth-walk VWAP not capturable from top-of-book ticks.")

    def _line(tag, m):
        print(f"    {tag:<11} n={m['n']:>4}  win={m['win']:.1%}  net=${m['net']:+.2f}  "
              f"PF={m['pf']:.2f}  EV/t=${m['ev']:+.3f}")

    print("\n  FULL SAMPLE:")
    _line("IDEAL", _pnl_metrics(simulate_certainty(rows, vol_mult, certainty_floor, lag_margin, max_ask)))
    _line("REALISTIC", _pnl_metrics(simulate_certainty(rows, vol_mult, certainty_floor, lag_margin,
                                                       max_ask, slippage=slip)))

    by_asset: dict[str, list] = {}
    for r in rows:
        by_asset.setdefault(_wkey(r)[0], []).append(r)
    if len(by_asset) > 1:
        print("\n  per-asset (REALISTIC fills):")
        for a in sorted(by_asset):
            m = _pnl_metrics(simulate_certainty(by_asset[a], vol_mult, certainty_floor,
                                                lag_margin, max_ask, slippage=slip))
            print(f"    {a}: trades={m['n']:>4}  win={m['win']:.1%}  "
                  f"net=${m['net']:+.2f}  PF={m['pf']:.2f}")

    # Honest OOS: identical fixed rule on chronological train/test (no tuning on test).
    train, test, n_tr, n_te = _split_chrono(rows, 0.7)
    print(f"\n  FIXED-RULE OOS (chrono 70/30, no param search — honest)  [REALISTIC fills]:")
    _line("train", _pnl_metrics(simulate_certainty(train, vol_mult, certainty_floor, lag_margin,
                                                   max_ask, slippage=slip)))
    mte = _pnl_metrics(simulate_certainty(test, vol_mult, certainty_floor, lag_margin,
                                          max_ask, slippage=slip))
    _line("test", mte)
    print(f"    → live gate: PF≥1.5 & net>0  ⇒  {'PASS' if (mte['pf']>=1.5 and mte['net']>0) else 'below gate'}")
    if n_te < 20:
        print(f"    ⚠  only {n_te} test windows — indicative, not final.")

    print(f"\n  sensitivity sweep (full sample, REALISTIC fills, max_ask={max_ask}):")
    print(f"    {'floor':>6}{'lag':>6}{'trades':>8}{'win%':>7}{'net$':>11}{'PF':>6}{'EV/t':>8}")
    for floor in (0.75, 0.80, 0.85, 0.90):
        for lag in (0.02, 0.03, 0.05):
            m = _pnl_metrics(simulate_certainty(rows, vol_mult, floor, lag, max_ask, slippage=slip))
            print(f"    {floor:>6.2f}{lag:>6.2f}{m['n']:>8}{100*m['win']:>7.1f}"
                  f"{m['net']:>11.2f}{m['pf']:>6.2f}{m['ev']:>8.3f}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol-mult", type=float, default=config.VOL_MULT)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--validate", action="store_true",
                    help="chronological train/test split: pick params on train, score out-of-sample")
    ap.add_argument("--train-frac", type=float, default=0.7,
                    help="fraction of windows used for training in --validate (default 0.7)")
    ap.add_argument("--include-fallback", action="store_true",
                    help="also use oracle-fallback-resolved windows (circular — inspection only)")
    ap.add_argument("--asset", default=None,
                    help="filter to one asset (BTC/ETH/SOL); default = all recorded assets")
    ap.add_argument("--buckets", action="store_true",
                    help="executed-trade win rate vs breakeven by entry-price bucket "
                         "(evidence for MIN_TAKER_ENTRY)")
    ap.add_argument("--certainty", action="store_true",
                    help="score the §3① certainty/feed-lag gate (measurement only, not live)")
    ap.add_argument("--cert-floor", type=float, default=0.80,
                    help="min model prob for the side to qualify as 'certain' (default 0.80)")
    ap.add_argument("--lag-margin", type=float, default=0.03,
                    help="min book lag p_side−ask required to enter (default 0.03)")
    ap.add_argument("--max-ask", type=float, default=0.97,
                    help="never buy above this ask — fee eats the edge (default 0.97)")
    ap.add_argument("--db", default=config.DB_PATH,
                    help="state.db to read (e.g. a downloaded VPS copy)")
    args = ap.parse_args()
    asset = args.asset.upper() if args.asset else None

    if args.buckets:
        report_buckets(args.db, asset=asset)
        return

    rows = _load(include_fallback=args.include_fallback, asset=asset, db_path=args.db)
    if not rows:
        print("No REAL-resolved tick data yet. Run `python main.py --mode paper` for a while "
              "to record ticks + outcomes, then re-run backtest.py.\n"
              "(Most 5-min windows resolve via oracle fallback; use --include-fallback "
              "to inspect those, but do NOT calibrate on them.)")
        return

    # Per-asset inventory so a thin asset can't hide inside the combined numbers.
    by_asset: dict[str, set] = {}
    for r in rows:
        a, sts = _wkey(r)
        by_asset.setdefault(a, set()).add(sts)
    n_windows = sum(len(v) for v in by_asset.values())
    print("Recorded REAL-resolved windows: "
          + "  ".join(f"{a}={len(v)}" for a, v in sorted(by_asset.items()))
          + f"  (total {n_windows})")
    if n_windows < 100:
        print(f"⚠  Only {n_windows} resolved window(s) of data — far below the ~300 needed "
              f"for trustworthy calibration. Treat results as directional, not final.\n")

    if args.certainty:
        report_certainty(rows, args.vol_mult, args.cert_floor, args.lag_margin, args.max_ask)
    elif args.validate:
        validate(rows, train_frac=args.train_frac)
    elif args.sweep:
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
        if not asset and len(by_asset) > 1:
            print("\nPER-ASSET BREAKDOWN (same params):")
            for a in sorted(by_asset):
                sub = [r for r in rows if _wkey(r)[0] == a]
                m = _pnl_metrics(simulate_taker(sub, args.vol_mult))
                print(f"  {a}: trades={m['n']}  win={m['win']:.1%}  "
                      f"net=${m['net']:+.2f}  PF={m['pf']:.2f}")


if __name__ == "__main__":
    main()
