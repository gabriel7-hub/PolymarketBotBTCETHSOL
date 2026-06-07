"""
state.py — SQLite persistence layer.
Tables: positions, trades, signals, daily_summary.
Thread-safe via connection-per-call pattern.
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
    return conn


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

            -- True settlement outcome per window (from Polymarket resolution, M1).
            CREATE TABLE IF NOT EXISTS outcomes (
                start_ts        INTEGER PRIMARY KEY,
                market_id       TEXT,
                ref_price       REAL,
                settle_price    REAL,
                winning_side    TEXT,           -- 'UP' or 'DOWN'
                predicted_side  TEXT,           -- oracle-derived diagnostic, NOT truth
                resolved_at     REAL,
                resolution_source TEXT          -- 'REAL' (Polymarket) | 'FALLBACK' (our oracle).
                                                -- Calibrate (backtest.py) on REAL only.
            );

            CREATE INDEX IF NOT EXISTS idx_signals_ts      ON signals(ts);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_ticks_start     ON ticks(start_ts);
            CREATE INDEX IF NOT EXISTS idx_trades_ts       ON trades(ts);
        """)
        # Migration: add resolution_source to outcomes tables created before this column.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(outcomes)").fetchall()]
        if "resolution_source" not in cols:
            conn.execute("ALTER TABLE outcomes ADD COLUMN resolution_source TEXT")
    logger.info(f"Database initialised at {config.DB_PATH}")


# ─── Recorder (M4) ────────────────────────────────────────────────────────────

def insert_tick(data: dict):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO ticks
              (ts, market_id, start_ts, t_remaining, binance_price, oracle_price,
               cex_basis_bp, realized_vol, ref_price, momentum_bp, p_up, sigma_price,
               up_bid, up_ask, down_bid, down_ask, ev_up, ev_down, action, mode)
            VALUES
              (:ts, :market_id, :start_ts, :t_remaining, :binance_price, :oracle_price,
               :cex_basis_bp, :realized_vol, :ref_price, :momentum_bp, :p_up, :sigma_price,
               :up_bid, :up_ask, :down_bid, :down_ask, :ev_up, :ev_down, :action, :mode)
        """, data)


def upsert_outcome(data: dict):
    data.setdefault("resolution_source", None)
    with _conn() as conn:
        conn.execute("""
            INSERT INTO outcomes
              (start_ts, market_id, ref_price, settle_price, winning_side,
               predicted_side, resolved_at, resolution_source)
            VALUES
              (:start_ts, :market_id, :ref_price, :settle_price, :winning_side,
               :predicted_side, :resolved_at, :resolution_source)
            ON CONFLICT(start_ts) DO UPDATE SET
                winning_side      = excluded.winning_side,
                settle_price      = excluded.settle_price,
                predicted_side    = excluded.predicted_side,
                resolved_at       = excluded.resolved_at,
                resolution_source = excluded.resolution_source
        """, data)


def get_outcome(start_ts: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM outcomes WHERE start_ts = ?", (start_ts,)
        ).fetchone()
        return dict(row) if row else None


# ─── Unified trade ledger (all legs) ───────────────────────────────────────────

def record_trade(data: dict) -> int:
    """Append an executed event to the audit ledger. Returns the row id."""
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (ts, market_id, start_ts, leg, side, price, detail, size_usdc,
               pnl_usdc, status, outcome, closed_at)
            VALUES
              (:ts, :market_id, :start_ts, :leg, :side, :price, :detail, :size_usdc,
               :pnl_usdc, :status, :outcome, :closed_at)
        """, {
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
                        size_usdc: float, reward_delta: float):
    """
    Accumulate the reward farm into ONE ledger row per window (not per tick).
    Creates the row on first accrual, then adds reward_delta on each tick.
    """
    if reward_delta <= 0:
        return
    now = time.time()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, pnl_usdc FROM trades WHERE start_ts = ? AND leg = 'FARM'",
            (start_ts,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE trades SET pnl_usdc = ?, detail = ?, closed_at = ?, status = 'ACCRUING' WHERE id = ?",
                (row["pnl_usdc"] + reward_delta, detail, now, row["id"])
            )
        else:
            conn.execute("""
                INSERT INTO trades
                  (ts, market_id, start_ts, leg, side, price, detail, size_usdc,
                   pnl_usdc, status, outcome, closed_at)
                VALUES (?, ?, ?, 'FARM', ?, NULL, ?, ?, ?, 'ACCRUING', 'FARM', ?)
            """, (now, market_id, start_ts, side, detail, size_usdc, reward_delta, now))


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
        return cur.rowcount


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
    with _conn() as conn:
        conn.execute("""
            INSERT INTO signals
              (ts, market_id, btc_ref, btc_now, distance_bp, momentum_bp,
               time_remaining, p_up, p_down, up_ask, down_ask,
               edge_up, edge_down, action, reason, phase)
            VALUES
              (:ts, :market_id, :btc_ref, :btc_now, :distance_bp, :momentum_bp,
               :time_remaining, :p_up, :p_down, :up_ask, :down_ask,
               :edge_up, :edge_down, :action, :reason, :phase)
        """, data)


def open_position(data: dict) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO positions
              (market_id, market_title, side, entry_price, size_usdc,
               order_id, order_type, status, opened_at)
            VALUES
              (:market_id, :market_title, :side, :entry_price, :size_usdc,
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


def get_open_position() -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


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
