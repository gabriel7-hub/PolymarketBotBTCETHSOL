"""
state.py — SQLite persistence layer (multi-asset: every row is tagged with its asset).
Tables: positions, trades, signals, ticks, outcomes, daily_summary.
Thread-safe via connection-per-call pattern; daily_summary stays GLOBAL across assets
(one shared bankroll / one daily-loss limit).
"""

import sqlite3
import time
from typing import Optional
from loguru import logger
import config


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Three asset workers write concurrently; without a busy timeout a writer that
    # collides with another thread's transaction raises SQLITE_BUSY immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    # synchronous=NORMAL is the documented-safe pairing for WAL (no corruption on app
    # crash; only a power-loss can lose the last committed txn) and is markedly faster
    # than the FULL default for our ~3 writes/sec tick load. wal_autocheckpoint keeps the
    # -wal file from growing unbounded between the explicit TRUNCATE checkpoints below.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return conn


def checkpoint() -> None:
    """Fold the WAL back into the main DB and truncate it. Called periodically by the
    runner so a week-long session can't accumulate a giant -wal file (and so a clean
    snapshot/backup sees an up-to-date main file)."""
    try:
        with _conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        logger.warning(f"WAL checkpoint failed: {exc}")


def integrity_ok(path: Optional[str] = None) -> bool:
    """Return True iff the database passes PRAGMA quick_check. Used at startup to refuse
    to run on a malformed file (which would silently drop writes / crash mid-week)."""
    path = config.DB_PATH if path is None else path
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error(f"Integrity check could not run on {path}: {exc}")
        return False


def backup(dest: str) -> bool:
    """Make a consistent snapshot of the live DB using SQLite's online-backup API.
    Safe to call while the bot is running, unlike `cp` of a WAL-mode file (that is what
    corrupted the supplied 616 MB copy). Returns True on success."""
    try:
        src = _conn()
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return True
    except sqlite3.Error as exc:
        logger.error(f"Backup to {dest} failed: {exc}")
        return False


def init_db():
    """Create tables if they don't exist."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id       TEXT NOT NULL,
                market_title    TEXT NOT NULL,
                side            TEXT NOT NULL,   -- 'UP' or 'DOWN'
                entry_price     REAL NOT NULL,
                size_usdc       REAL NOT NULL,
                order_id        TEXT,
                order_type      TEXT NOT NULL,   -- 'MAKER' or 'TAKER'
                status          TEXT NOT NULL,   -- 'OPEN', 'FILLED', 'CANCELLED', 'RESOLVED'
                opened_at       REAL NOT NULL,
                closed_at       REAL,
                exit_price      REAL,
                pnl_usdc        REAL,
                rebate_usdc     REAL DEFAULT 0,
                outcome         TEXT            -- 'WIN', 'LOSS', 'PUSH'
            );

            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,
                market_id       TEXT NOT NULL,
                btc_ref         REAL,
                btc_now         REAL,
                distance_bp     REAL,
                momentum_bp     REAL,
                time_remaining  INTEGER,
                p_up            REAL,
                p_down          REAL,
                up_ask          REAL,
                down_ask        REAL,
                edge_up         REAL,
                edge_down       REAL,
                action          TEXT,           -- 'POST_MAKER_UP', 'IOC_DOWN', 'SKIP', etc.
                reason          TEXT,
                phase           INTEGER
            );

            CREATE TABLE IF NOT EXISTS daily_summary (
                date            TEXT PRIMARY KEY,
                trades          INTEGER DEFAULT 0,
                wins            INTEGER DEFAULT 0,
                losses          INTEGER DEFAULT 0,
                gross_profit    REAL DEFAULT 0,
                gross_loss      REAL DEFAULT 0,
                net_pnl         REAL DEFAULT 0,
                rebates         REAL DEFAULT 0,
                max_drawdown    REAL DEFAULT 0
            );

            -- Full per-tick record for forward-test / backtest (M4).
            CREATE TABLE IF NOT EXISTS ticks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,
                market_id       TEXT NOT NULL,
                start_ts        INTEGER,
                t_remaining     REAL,
                binance_price   REAL,
                oracle_price    REAL,
                cex_basis_bp    REAL,
                realized_vol    REAL,
                ref_price       REAL,
                momentum_bp     REAL,
                p_up            REAL,
                sigma_price     REAL,
                up_bid          REAL,
                up_ask          REAL,
                down_bid        REAL,
                down_ask        REAL,
                ev_up           REAL,
                ev_down         REAL,
                action          TEXT,
                mode            TEXT
            );

            -- Unified audit ledger: one row per executed event across ALL legs
            -- (TAKER fill / ARB pair / FARM reward-cycle). Source for the dashboard
            -- trade history. The positions table remains the directional risk lifecycle.
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,      -- when executed / opened
                market_id   TEXT,
                start_ts    INTEGER,
                leg         TEXT NOT NULL,      -- 'TAKER' | 'ARB' | 'FARM'
                side        TEXT,               -- 'UP' | 'DOWN' | 'PAIR' | 'TWO-SIDED'
                price       REAL,               -- entry (taker) / pair cost (arb)
                detail      TEXT,               -- human readable, e.g. "UP@0.52 / DOWN@0.43"
                size_usdc   REAL,
                pnl_usdc    REAL DEFAULT 0,     -- realized P&L / reward
                status      TEXT,               -- 'OPEN'|'RESOLVED'|'LOCKED'|'ACCRUING'|'CANCELLED'
                outcome     TEXT,               -- 'WIN'|'LOSS'|'ARB'|'FARM'|None
                closed_at   REAL
            );

            -- True settlement outcome per (asset, window) (from Polymarket resolution).
            -- Composite PK: BTC/ETH/SOL windows share the same start_ts grid.
            CREATE TABLE IF NOT EXISTS outcomes (
                asset           TEXT NOT NULL DEFAULT 'BTC',
                start_ts        INTEGER NOT NULL,
                market_id       TEXT,
                ref_price       REAL,
                settle_price    REAL,
                winning_side    TEXT,           -- 'UP' or 'DOWN'
                predicted_side  TEXT,           -- oracle-derived diagnostic, NOT truth
                resolved_at     REAL,
                resolution_source TEXT,         -- 'REAL' (Polymarket) | 'FALLBACK' (our oracle).
                                                -- Calibrate (backtest.py) on REAL only.
                PRIMARY KEY (asset, start_ts)
            );

            CREATE INDEX IF NOT EXISTS idx_signals_ts      ON signals(ts);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_ticks_start     ON ticks(start_ts);
            CREATE INDEX IF NOT EXISTS idx_trades_ts       ON trades(ts);
        """)
        _migrate_multi_asset(conn)
    logger.info(f"Database initialised at {config.DB_PATH} (assets: {', '.join(config.ASSETS)})")


def _migrate_multi_asset(conn: sqlite3.Connection):
    """
    Upgrade a pre-multi-asset database in place. All pre-existing rows were BTC.
      1. positions/signals/ticks/trades gain an `asset` column (default 'BTC').
      2. outcomes moves from PRIMARY KEY(start_ts) to PRIMARY KEY(asset, start_ts) —
         SQLite can't alter a PK, so the table is rebuilt and rows copied as BTC.
    Idempotent: runs once, no-ops on an already-migrated DB.
    """
    for table in ("positions", "signals", "ticks", "trades"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if cols and "asset" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN asset TEXT NOT NULL DEFAULT 'BTC'")
            logger.info(f"Migration: added asset column to {table}")

    out_cols = [r[1] for r in conn.execute("PRAGMA table_info(outcomes)").fetchall()]
    if out_cols and "resolution_source" not in out_cols:
        conn.execute("ALTER TABLE outcomes ADD COLUMN resolution_source TEXT")
        out_cols.append("resolution_source")
    if out_cols and "asset" not in out_cols:
        conn.executescript("""
            ALTER TABLE outcomes RENAME TO outcomes_v1;
            CREATE TABLE outcomes (
                asset           TEXT NOT NULL DEFAULT 'BTC',
                start_ts        INTEGER NOT NULL,
                market_id       TEXT,
                ref_price       REAL,
                settle_price    REAL,
                winning_side    TEXT,
                predicted_side  TEXT,
                resolved_at     REAL,
                resolution_source TEXT,
                PRIMARY KEY (asset, start_ts)
            );
            INSERT INTO outcomes
                (asset, start_ts, market_id, ref_price, settle_price,
                 winning_side, predicted_side, resolved_at, resolution_source)
            SELECT 'BTC', start_ts, market_id, ref_price, settle_price,
                   winning_side, predicted_side, resolved_at, resolution_source
            FROM outcomes_v1;
            DROP TABLE outcomes_v1;
        """)
        logger.info("Migration: rebuilt outcomes with PRIMARY KEY (asset, start_ts)")


# ─── Recorder (M4) ────────────────────────────────────────────────────────────

def insert_tick(data: dict):
    data.setdefault("asset", "BTC")
    with _conn() as conn:
        conn.execute("""
            INSERT INTO ticks
              (asset, ts, market_id, start_ts, t_remaining, binance_price, oracle_price,
               cex_basis_bp, realized_vol, ref_price, momentum_bp, p_up, sigma_price,
               up_bid, up_ask, down_bid, down_ask, ev_up, ev_down, action, mode)
            VALUES
              (:asset, :ts, :market_id, :start_ts, :t_remaining, :binance_price, :oracle_price,
               :cex_basis_bp, :realized_vol, :ref_price, :momentum_bp, :p_up, :sigma_price,
               :up_bid, :up_ask, :down_bid, :down_ask, :ev_up, :ev_down, :action, :mode)
        """, data)


def upsert_outcome(data: dict):
    data.setdefault("resolution_source", None)
    data.setdefault("asset", "BTC")
    with _conn() as conn:
        conn.execute("""
            INSERT INTO outcomes
              (asset, start_ts, market_id, ref_price, settle_price, winning_side,
               predicted_side, resolved_at, resolution_source)
            VALUES
              (:asset, :start_ts, :market_id, :ref_price, :settle_price, :winning_side,
               :predicted_side, :resolved_at, :resolution_source)
            ON CONFLICT(asset, start_ts) DO UPDATE SET
                winning_side      = excluded.winning_side,
                settle_price      = excluded.settle_price,
                predicted_side    = excluded.predicted_side,
                resolved_at       = excluded.resolved_at,
                resolution_source = excluded.resolution_source
        """, data)


def get_outcome(start_ts: int, asset: str = "BTC") -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcomes WHERE asset = ? AND start_ts = ?", (asset, start_ts)
        ).fetchone()
        return dict(row) if row else None


# ─── Unified trade ledger (all legs) ───────────────────────────────────────────

def record_trade(data: dict) -> int:
    """Append an executed event to the audit ledger. Returns the row id."""
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (asset, ts, market_id, start_ts, leg, side, price, detail, size_usdc,
               pnl_usdc, status, outcome, closed_at)
            VALUES
              (:asset, :ts, :market_id, :start_ts, :leg, :side, :price, :detail, :size_usdc,
               :pnl_usdc, :status, :outcome, :closed_at)
        """, {
            "asset": data.get("asset", "BTC"),
            "ts": data.get("ts", time.time()),
            "market_id": data.get("market_id"), "start_ts": data.get("start_ts"),
            "leg": data["leg"], "side": data.get("side"), "price": data.get("price"),
            "detail": data.get("detail"), "size_usdc": data.get("size_usdc"),
            "pnl_usdc": data.get("pnl_usdc", 0.0), "status": data.get("status"),
            "outcome": data.get("outcome"), "closed_at": data.get("closed_at"),
        })
        return cur.lastrowid


def update_trade(trade_id: int, **fields):
    """Update a ledger row (e.g. resolve a taker fill with pnl/outcome)."""
    if not trade_id or not fields:
        return
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    fields["_id"] = trade_id
    with _conn() as conn:
        conn.execute(f"UPDATE trades SET {cols} WHERE id = :_id", fields)


def record_farm_accrual(start_ts: int, market_id: str, side: str, detail: str,
                        size_usdc: float, reward_delta: float, asset: str = "BTC"):
    """
    Accumulate the reward farm into ONE ledger row per window (not per tick).
    Creates the row on first accrual, then adds reward_delta on each tick.
    Keyed by market_id (unique per asset-window; start_ts collides across assets).
    """
    if reward_delta <= 0:
        return
    now = time.time()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, pnl_usdc FROM trades WHERE market_id = ? AND leg = 'FARM'",
            (market_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE trades SET pnl_usdc = ?, detail = ?, closed_at = ?, status = 'ACCRUING' WHERE id = ?",
                (row["pnl_usdc"] + reward_delta, detail, now, row["id"])
            )
        else:
            conn.execute("""
                INSERT INTO trades
                  (asset, ts, market_id, start_ts, leg, side, price, detail, size_usdc,
                   pnl_usdc, status, outcome, closed_at)
                VALUES (?, ?, ?, ?, 'FARM', ?, NULL, ?, ?, ?, 'ACCRUING', 'FARM', ?)
            """, (asset, now, market_id, start_ts, side, detail, size_usdc, reward_delta, now))


def get_recent_ledger(limit: int = 25) -> list:
    """Recent executed events across all legs, newest first (dashboard history)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def reconcile_taker_ledger() -> int:
    """
    One-time self-heal: backfill OPEN TAKER ledger rows whose position has already
    resolved (copies outcome + P&L from the positions table). Fixes history rows left
    OPEN before the resolve-sync existed. Returns rows updated.
    """
    with _conn() as conn:
        cur = conn.execute("""
            UPDATE trades
            SET status='RESOLVED', closed_at=?,
                outcome  = (SELECT p.outcome  FROM positions p
                            WHERE p.market_id=trades.market_id AND p.status='RESOLVED'
                            ORDER BY p.closed_at DESC LIMIT 1),
                pnl_usdc = (SELECT p.pnl_usdc FROM positions p
                            WHERE p.market_id=trades.market_id AND p.status='RESOLVED'
                            ORDER BY p.closed_at DESC LIMIT 1)
            WHERE leg='TAKER' AND status='OPEN'
              AND EXISTS (SELECT 1 FROM positions p
                          WHERE p.market_id=trades.market_id AND p.status='RESOLVED')
        """, (time.time(),))
        n = cur.rowcount
        # Takers whose position was CANCELLED (e.g. by an old restart) → mark CANCELLED,
        # not left dangling OPEN in the history.
        conn.execute("""
            UPDATE trades SET status='CANCELLED', outcome='CANCELLED', closed_at=?
            WHERE leg='TAKER' AND status='OPEN'
              AND EXISTS (SELECT 1 FROM positions p
                          WHERE p.market_id=trades.market_id AND p.status='CANCELLED')
        """, (time.time(),))
        return n


def get_position_by_market(market_id: str) -> Optional[dict]:
    """Most recent position for a market (any status) — used to re-settle to the real outcome."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE market_id = ? ORDER BY id DESC LIMIT 1", (market_id,)
        ).fetchone()
        return dict(row) if row else None


def get_recent_fallback_windows(limit: int = 50, asset: str = "BTC") -> list:
    """(start_ts, market_id) of this asset's windows we settled by oracle FALLBACK —
    candidates to re-check against the real Chainlink outcome and correct if they disagree."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT start_ts, market_id FROM outcomes "
            "WHERE resolution_source='FALLBACK' AND asset = ? "
            "ORDER BY start_ts DESC LIMIT ?", (asset, limit)
        ).fetchall()
        return [(r["start_ts"], r["market_id"]) for r in rows]


def _resettle_daily(old_pnl: float, old_outcome: str, new_pnl: float, new_outcome: str):
    """Adjust today's summary by the DELTA when a trade is re-settled (no new trade count)."""
    import datetime
    d_win  = (1 if new_outcome == 'WIN' else 0)  - (1 if old_outcome == 'WIN' else 0)
    d_loss = (1 if new_outcome == 'LOSS' else 0) - (1 if old_outcome == 'LOSS' else 0)
    d_gp   = max(new_pnl, 0.0) - max(old_pnl, 0.0)
    d_gl   = max(-new_pnl, 0.0) - max(-old_pnl, 0.0)
    d_net  = new_pnl - old_pnl
    with _conn() as conn:
        conn.execute("""
            UPDATE daily_summary SET wins=wins+?, losses=losses+?,
                gross_profit=gross_profit+?, gross_loss=gross_loss+?, net_pnl=net_pnl+?
            WHERE date=?
        """, (d_win, d_loss, d_gp, d_gl, d_net, datetime.date.today().isoformat()))


def resettle_position(pos_id: int, new_outcome: str, new_pnl: float) -> bool:
    """Correct an already-resolved position to the real outcome; fixes daily stats by delta."""
    with _conn() as conn:
        row = conn.execute("SELECT pnl_usdc, outcome FROM positions WHERE id=?", (pos_id,)).fetchone()
        if not row or row["outcome"] == new_outcome:
            return False
        old_pnl, old_outcome = row["pnl_usdc"] or 0.0, row["outcome"]
        conn.execute("UPDATE positions SET outcome=?, pnl_usdc=?, exit_price=? WHERE id=?",
                     (new_outcome, new_pnl, 1.0 if new_outcome == 'WIN' else 0.0, pos_id))
    _resettle_daily(old_pnl, old_outcome, new_pnl, new_outcome)
    return True


def update_taker_ledger(market_id: str, outcome: str, pnl: float):
    """Force-update the latest TAKER ledger row for a market (used when re-settling to REAL)."""
    with _conn() as conn:
        conn.execute("""
            UPDATE trades SET status='RESOLVED', outcome=?, pnl_usdc=?, closed_at=?
            WHERE id = (SELECT id FROM trades WHERE market_id=? AND leg='TAKER'
                        ORDER BY id DESC LIMIT 1)
        """, (outcome, pnl, time.time(), market_id))


def resolve_taker_ledger(market_id: str, outcome: str, pnl: float):
    """
    Sync the audit ledger when a TAKER position settles: the positions table gets the
    WIN/LOSS + P&L, but the ledger row stayed OPEN/$0, so the dashboard Trade History
    showed resolved takers as still OPEN. Update the open TAKER row(s) for this market.
    """
    with _conn() as conn:
        conn.execute("""
            UPDATE trades SET status = 'RESOLVED', outcome = ?, pnl_usdc = ?, closed_at = ?
            WHERE market_id = ? AND leg = 'TAKER' AND status = 'OPEN'
        """, (outcome, pnl, time.time(), market_id))


# ─── Strategy-leg P&L (reward farm + arbitrage) ────────────────────────────────

def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def add_reward(amount: float):
    """Accrue estimated liquidity-reward / rebate yield (delta-neutral farm leg)."""
    if amount <= 0:
        return
    with _conn() as conn:
        conn.execute("""
            INSERT INTO daily_summary (date, rebates, net_pnl)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                rebates = rebates + excluded.rebates,
                net_pnl = net_pnl + excluded.net_pnl
        """, (_today(), amount, amount))


def add_arb_pnl(amount: float):
    """Record a locked YES/NO arbitrage profit (counts as a winning trade)."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO daily_summary (date, trades, wins, gross_profit, net_pnl)
            VALUES (?, 1, 1, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                trades       = trades + 1,
                wins         = wins + 1,
                gross_profit = gross_profit + excluded.gross_profit,
                net_pnl      = net_pnl + excluded.net_pnl
        """, (_today(), max(0.0, amount), amount))


def insert_signal(data: dict):
    data.setdefault("asset", "BTC")
    with _conn() as conn:
        conn.execute("""
            INSERT INTO signals
              (asset, ts, market_id, btc_ref, btc_now, distance_bp, momentum_bp,
               time_remaining, p_up, p_down, up_ask, down_ask,
               edge_up, edge_down, action, reason, phase)
            VALUES
              (:asset, :ts, :market_id, :btc_ref, :btc_now, :distance_bp, :momentum_bp,
               :time_remaining, :p_up, :p_down, :up_ask, :down_ask,
               :edge_up, :edge_down, :action, :reason, :phase)
        """, data)


def open_position(data: dict) -> int:
    data.setdefault("asset", "BTC")
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO positions
              (asset, market_id, market_title, side, entry_price, size_usdc,
               order_id, order_type, status, opened_at)
            VALUES
              (:asset, :market_id, :market_title, :side, :entry_price, :size_usdc,
               :order_id, :order_type, 'OPEN', :opened_at)
        """, data)
        return cur.lastrowid


def close_position(pos_id: int, exit_price: float, pnl: float,
                   rebate: float, outcome: str):
    with _conn() as conn:
        conn.execute("""
            UPDATE positions
            SET status     = 'RESOLVED',
                closed_at  = ?,
                exit_price = ?,
                pnl_usdc   = ?,
                rebate_usdc = ?,
                outcome    = ?
            WHERE id = ?
        """, (time.time(), exit_price, pnl, rebate, outcome, pos_id))
    _update_daily(pnl, rebate, outcome)


def cancel_position(pos_id: int):
    with _conn() as conn:
        conn.execute("""
            UPDATE positions SET status = 'CANCELLED', closed_at = ?
            WHERE id = ?
        """, (time.time(), pos_id))


def get_open_position(asset: Optional[str] = None) -> Optional[dict]:
    """Latest OPEN position — scoped to one asset when given (each asset worker may
    hold at most one position; an open BTC taker must not block an ETH entry)."""
    with _conn() as conn:
        if asset:
            row = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' AND asset = ? "
                "ORDER BY id DESC LIMIT 1", (asset,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None


def get_open_positions() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def prune_old_data(days: int) -> int:
    """Delete ticks/signals older than `days` so the DB doesn't grow unbounded on a long
    VPS run (ticks accrue ~1/sec). Keeps positions/outcomes/trades (small, valuable)."""
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    with _conn() as conn:
        n = conn.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,)).rowcount
        n += conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,)).rowcount
    return n


def cancel_stale_open_positions() -> int:
    """
    Cancel any OPEN positions left over from a previous session. Resolution is tracked
    in-memory (main._pending), so a position whose window closed while the bot was down
    can never resolve — it would sit OPEN forever and block the open-position risk guard,
    silently preventing all future taker trades. Returns how many were cancelled.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE positions SET status = 'CANCELLED', closed_at = ? WHERE status = 'OPEN'",
            (time.time(),),
        )
        return cur.rowcount


def get_recent_trades(limit: int = 20) -> list:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE status = 'RESOLVED'
            ORDER BY closed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_daily_pnl(date_str: Optional[str] = None) -> float:
    """Return net_pnl for today (or given date YYYY-MM-DD)."""
    import datetime
    if not date_str:
        date_str = datetime.date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT net_pnl FROM daily_summary WHERE date = ?", (date_str,)
        ).fetchone()
        return row["net_pnl"] if row else 0.0


def get_daily_stats(date_str: Optional[str] = None) -> dict:
    import datetime
    if not date_str:
        date_str = datetime.date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (date_str,)
        ).fetchone()
        if row:
            return dict(row)
        return {"date": date_str, "trades": 0, "wins": 0, "losses": 0,
                "gross_profit": 0, "gross_loss": 0, "net_pnl": 0, "rebates": 0}


def get_asset_day_stats(asset: str) -> dict:
    """
    Today's taker results for ONE asset, derived from the positions table (the global
    daily_summary is shared across assets). Used by the dashboard's per-asset tabs.
    BOXED exits count in net P&L but not in the win/loss denominators.
    """
    import datetime
    midnight = time.mktime(datetime.date.today().timetuple())
    with _conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*)                                        AS trades,
                   COALESCE(SUM(pnl_usdc), 0)                      AS net_pnl,
                   COALESCE(SUM(outcome = 'WIN'), 0)               AS wins,
                   COALESCE(SUM(outcome = 'LOSS'), 0)              AS losses
            FROM positions
            WHERE asset = ? AND status = 'RESOLVED' AND closed_at >= ?
        """, (asset, midnight)).fetchone()
        return dict(row)


def get_overall_stats() -> dict:
    """Lifetime totals across every recorded day (taker P&L; rebates/rewards separate)."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(net_pnl), 0)  AS net_pnl,
                   COALESCE(SUM(rebates), 0)  AS rebates,
                   COALESCE(SUM(trades), 0)   AS trades,
                   COALESCE(SUM(wins), 0)     AS wins
            FROM daily_summary
        """).fetchone()
        return dict(row)


def get_recent_signals(limit: int = 50) -> list:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM signals ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def _update_daily(pnl: float, rebate: float, outcome: str):
    import datetime
    date_str = datetime.date.today().isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO daily_summary (date, trades, wins, losses, gross_profit, gross_loss, net_pnl, rebates)
            VALUES (?, 1,
                    CASE WHEN ? = 'WIN' THEN 1 ELSE 0 END,
                    CASE WHEN ? = 'LOSS' THEN 1 ELSE 0 END,
                    CASE WHEN ? > 0 THEN ? ELSE 0 END,
                    CASE WHEN ? < 0 THEN ABS(?) ELSE 0 END,
                    ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                trades       = trades + 1,
                wins         = wins + (CASE WHEN excluded.wins > 0 THEN 1 ELSE 0 END),
                losses       = losses + (CASE WHEN excluded.losses > 0 THEN 1 ELSE 0 END),
                gross_profit = gross_profit + excluded.gross_profit,
                gross_loss   = gross_loss + excluded.gross_loss,
                net_pnl      = net_pnl + excluded.net_pnl,
                rebates      = rebates + excluded.rebates
        """, (date_str, outcome, outcome, pnl, pnl, pnl, pnl, pnl, rebate))
