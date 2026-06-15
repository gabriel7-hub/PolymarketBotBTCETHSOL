#!/usr/bin/env python3
"""
backup_db.py — safe, consistent snapshot of the live bot DB.

Uses SQLite's online-backup API (state.backup), which is safe to run WHILE the bot is
writing — unlike `cp bot_state.db ...`, which copies a WAL-mode file mid-write and produces
a malformed image (that is exactly how the supplied 616 MB copy got corrupted).

Usage (e.g. hourly cron on the VPS):
    0 * * * * cd /home/polybot/PolymarketBot && python3 backup_db.py --keep 48

Writes backups/bot_state.<YYYYmmdd-HHMMSS>.db and prunes to the newest --keep files.
"""
import argparse
import glob
import os
import time

import config
import state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="backups", help="backup directory")
    ap.add_argument("--keep", type=int, default=48, help="how many recent backups to retain")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    if not state.integrity_ok():
        print(f"REFUSING to back up: {config.DB_PATH} fails integrity check")
        raise SystemExit(1)

    dest = os.path.join(args.dir, f"bot_state.{time.strftime('%Y%m%d-%H%M%S')}.db")
    if not state.backup(dest):
        raise SystemExit(1)
    print(f"backup ok: {dest} ({os.path.getsize(dest)/1e6:.1f} MB)")

    snaps = sorted(glob.glob(os.path.join(args.dir, "bot_state.*.db")))
    for old in snaps[: max(0, len(snaps) - args.keep)]:
        os.remove(old)
        print(f"pruned old backup: {old}")


if __name__ == "__main__":
    main()
