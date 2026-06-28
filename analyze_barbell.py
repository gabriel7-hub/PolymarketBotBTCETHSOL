#!/usr/bin/env python3
"""
Entry-price BARBELL analyzer — recompute realized win% vs breakeven (= entry price) per ask band
and per asset, from recorded certainty trade rows. This is the live-data validation for the
CERTAINTY_BARBELL_ENABLED gate (config.py): it tells you, from your OWN fills, where the edge
actually is so you can re-confirm (or retire) the 0.85-0.91 dead-band skip.

Breakeven win rate for a favorite bought at ask p is exactly p (win pays 1-p, loss costs p), so
the real edge is realized_win% - avg_entry, and you must clear the taker fee (~0.07*p*(1-p)) on
top. A band whose realized win% only matches its entry price is a fee-funded loser.

Run weekly and watch whether the barbell (cheap-favorite + near-lock positive, middle negative)
holds out of sample before trusting it enough to change sizing:
    python3 analyze_barbell.py --db bot_state.db
    python3 analyze_barbell.py --db bot_state.db --since 2026-06-29
    python3 analyze_barbell.py --db bot_state.db --leg CERT_LIVE   # live-capital fills only

No schema change needed — uses the trades table's `price` (entry ask) and `outcome` columns.
"""
import argparse, sqlite3, datetime

BANDS = [(0.70, 0.78), (0.78, 0.82), (0.82, 0.85), (0.85, 0.88),
         (0.88, 0.91), (0.91, 0.94), (0.94, 0.97), (0.97, 1.00)]
DEAD_LO, DEAD_HI = 0.85, 0.91   # the band the gate currently skips (for annotation only)


def fee(p):
    return 0.07 * p * (1 - p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="bot_state.db")
    ap.add_argument("--since", help="YYYY-MM-DD; only trades with ts on/after this date")
    ap.add_argument("--leg", default="CERTAINTY,CERT_LIVE",
                    help="comma-separated legs to include (default: CERTAINTY,CERT_LIVE)")
    args = ap.parse_args()

    legs = [s.strip() for s in args.leg.split(",") if s.strip()]
    q = (f"SELECT asset, price, size_usdc, pnl_usdc, outcome FROM trades "
         f"WHERE leg IN ({','.join('?' * len(legs))}) AND outcome IN ('WIN','LOSS') "
         f"AND price IS NOT NULL")
    params = list(legs)
    if args.since:
        q += " AND ts >= ?"
        params.append(datetime.datetime.fromisoformat(args.since).timestamp())

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(q, params).fetchall()
    conn.close()
    if not rows:
        print("No matching trades.")
        return

    n_all = len(rows)
    w_all = sum(r["outcome"] == "WIN" for r in rows)
    pnl_all = sum(r["pnl_usdc"] or 0 for r in rows)
    stk_all = sum(r["size_usdc"] or 0 for r in rows)
    print(f"Legs {','.join(legs)} | trades {n_all} | win {100*w_all/n_all:.1f}% | "
          f"net ${pnl_all:+.2f} on ${stk_all:.0f} staked ({100*pnl_all/stk_all:+.2f}%)\n")

    hdr = f"{'band':>11} {'n':>4} {'avg_entry':>9} {'real_win%':>9} {'edge':>6} {'fee':>5} {'net_edge':>8} {'pnl':>9}"
    print("ENTRY-PRICE BARBELL (edge = realized_win% - entry; net_edge subtracts the taker fee):")
    print(hdr)
    for lo, hi in BANDS:
        sub = [r for r in rows if lo <= r["price"] < hi]
        if not sub:
            continue
        n = len(sub)
        be = sum(r["price"] for r in sub) / n
        wr = sum(r["outcome"] == "WIN" for r in sub) / n
        pnl = sum(r["pnl_usdc"] or 0 for r in sub)
        edge = (wr - be) * 100
        netedge = edge - fee(be) * 100
        flag = "  <-DEAD(skipped)" if (lo >= DEAD_LO and hi <= DEAD_HI) else \
               ("  KEEP" if netedge > 0 else "  weak")
        print(f"{lo:.2f}-{hi:.2f} {n:>4} {be:>9.3f} {100*wr:>8.1f}% {edge:>+6.1f} "
              f"{fee(be)*100:>5.1f} {netedge:>+8.1f} {pnl:>+9.2f}{flag}")

    print("\nBY ASSET:")
    for a in sorted(set(r["asset"] for r in rows)):
        sub = [r for r in rows if r["asset"] == a]
        n = len(sub)
        be = sum(r["price"] for r in sub) / n
        wr = sum(r["outcome"] == "WIN" for r in sub) / n
        pnl = sum(r["pnl_usdc"] or 0 for r in sub)
        print(f"  {a:4} n={n:>4} avg_entry={be:.3f} real_win={100*wr:>5.1f}% "
              f"edge={100*(wr-be):>+5.1f}pts pnl=${pnl:>+8.2f}")


if __name__ == "__main__":
    main()
