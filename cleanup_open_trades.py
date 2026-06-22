"""
cleanup_open_trades.py — settle/void CERTAINTY shadow rows left OPEN by a restart.

The certainty leg tracks open bets in memory (lost on restart). _do_reconcile_shadow_trades
only runs at worker startup and only settles rows whose window already has a recorded outcome,
leaving genuinely-orphaned rows OPEN. This one-shot reconciles EVERY OPEN shadow row:

  • outcome in `outcomes`            → settle WIN/LOSS, score pnl, fold into daily_summary
  • no outcome, window long closed   → VOID (its resolution moment has passed; unsettleable)
  • no outcome, window still live     → leave OPEN (a real in-flight bet)

Safe by default (dry-run). Add --apply to write. Also reports OPEN positions.

    python3 cleanup_open_trades.py            # show what WOULD change
    python3 cleanup_open_trades.py --apply    # do it
"""
import sys, time, argparse
import config, state, pricing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    apply = args.apply
    now = time.time()
    window_closed_after = config.MARKET_WINDOW_SECS + 60   # window definitely resolved by now

    settled = voided = left = 0
    for asset in config.ASSETS:
        for tr in state.get_open_shadow_trades(asset, "CERTAINTY"):
            o = state.get_outcome(tr["start_ts"], asset)
            winning = o.get("winning_side") if o else None
            if winning in ("UP", "DOWN"):
                entry = tr["price"]
                shares = (tr["size_usdc"] / entry) if entry else 0.0
                won = (tr["side"] == winning)
                fee = pricing.taker_fee_per_share(entry) * shares
                pnl = ((1.0 - entry) if won else -entry) * shares - fee
                outcome = "WIN" if won else "LOSS"
                print(f"  SETTLE  {asset} id={tr['id']} {tr['side']} -> {outcome} "
                      f"pnl={pnl:+.2f}")
                if apply:
                    state.update_trade(tr["id"], status="RESOLVED", outcome=outcome,
                                       pnl_usdc=round(pnl, 4), closed_at=now)
                    state.add_certainty_pnl(pnl, outcome)
                settled += 1
            elif now - tr["start_ts"] > window_closed_after:
                print(f"  VOID    {asset} id={tr['id']} {tr['side']} (window closed, no outcome)")
                if apply:
                    state.update_trade(tr["id"], status="VOID", outcome="VOID",
                                       pnl_usdc=0.0, closed_at=now)
                voided += 1
            else:
                print(f"  KEEP    {asset} id={tr['id']} {tr['side']} (window still live)")
                left += 1

    open_pos = state.get_open_positions()
    print(f"\nCERTAINTY shadows: settled={settled}  voided={voided}  kept-open={left}")
    print(f"OPEN positions remaining: {len(open_pos)}"
          + (" (re-adopted on restart; resolve as outcomes arrive)" if open_pos else ""))
    if not apply:
        print("\nDRY-RUN — re-run with --apply to write these changes.")


if __name__ == "__main__":
    main()
