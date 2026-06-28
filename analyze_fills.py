#!/usr/bin/env python3
"""
Fill-quality analyzer — measure realized certainty-leg slippage (fill − displayed ask) from the
recorded CERTAINTY trade rows. Purpose: prove whether moving the VPS closer to Polymarket's London
matching engine (e.g. Bangalore → DigitalOcean LON1) actually TIGHTENS taker fills, which is the
whole P&L lever (each 1c of fill ≈ ~$0.27/trade on the certainty leg).

Run it once BEFORE the migration and once AFTER, on each session's DB, and compare avg slip:
    python3 analyze_fills.py --db bot_state.db
    python3 analyze_fills.py --db bot_state.db --since 2026-06-29   # only fills on/after a date

Slippage is parsed from the trade `detail` ("... ask=0.85 fill=0.863 ..."), so it works on any DB
that recorded CERTAINTY rows — no schema change needed. Lower (closer to 0) slip = better execution.
"""
import argparse, re, sqlite3, datetime

ASK_FILL = re.compile(r"ask=([0-9.]+)\s+fill=([0-9.]+)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="bot_state.db")
    ap.add_argument("--since", help="YYYY-MM-DD; only fills with ts on/after this date")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    q = "SELECT asset, ts, detail, price FROM trades WHERE leg='CERTAINTY' AND detail LIKE '%ask=%'"
    params = []
    if args.since:
        cutoff = datetime.datetime.fromisoformat(args.since).timestamp()
        q += " AND ts >= ?"
        params.append(cutoff)
    rows = conn.execute(q, params).fetchall()
    conn.close()

    by_asset = {}
    overall = []
    for r in rows:
        m = ASK_FILL.search(r["detail"] or "")
        if not m:
            continue
        ask, fill = float(m.group(1)), float(m.group(2))
        slip_c = (fill - ask) * 100.0          # cents; + = paid up (adverse), 0 = touch, − = maker
        by_asset.setdefault(r["asset"], []).append(slip_c)
        overall.append(slip_c)

    if not overall:
        print(f"No CERTAINTY fills with ask/fill found in {args.db}"
              + (f" since {args.since}" if args.since else ""))
        return

    def stat(xs):
        xs = sorted(xs)
        n = len(xs)
        avg = sum(xs) / n
        med = xs[n // 2]
        adverse = sum(1 for x in xs if x > 0) / n * 100
        return n, avg, med, adverse

    print(f"\nCERTAINTY fill quality — {args.db}"
          + (f"  (since {args.since})" if args.since else ""))
    print(f"  slippage = fill − displayed ask, in cents (+ = paid up / adverse, 0 = touch, − = maker)\n")
    print(f"  {'scope':<10}{'fills':>7}{'avg slip¢':>11}{'median¢':>9}{'% adverse':>11}")
    n, avg, med, adv = stat(overall)
    print(f"  {'ALL':<10}{n:>7}{avg:>+11.2f}{med:>+9.2f}{adv:>10.0f}%")
    for a in sorted(by_asset):
        n, avg, med, adv = stat(by_asset[a])
        print(f"  {a:<10}{n:>7}{avg:>+11.2f}{med:>+9.2f}{adv:>10.0f}%")
    print(f"\n  Lower avg slip ⇒ better execution. ~$0.27/trade per 1c on the certainty leg.")
    print(f"  Compare this number before vs after the London move to verify the latency win.\n")


if __name__ == "__main__":
    main()
