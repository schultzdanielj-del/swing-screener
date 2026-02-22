"""Local DB provider — lets analysis engines run in-process on Railway.

Instead of HTTP self-calls (engine → API → DB), this provides direct DB access
with the same interface the engines expect.
"""

import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager

# Railway volume path, fallback for local dev
DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_PATH = DB_DIR / "scanperfect.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, limit: int = 5000) -> list[dict]:
    """Run a read-only SELECT and return list of dicts."""
    sql_lower = sql.strip().lower()
    if not sql_lower.startswith("select"):
        raise ValueError("Only SELECT queries allowed")
    with get_db() as db:
        rows = db.execute(sql).fetchall()
        return [dict(r) for r in rows[:limit]]


def fetch_ohlcv_bulk(ticker: str, end_date: str = None, lookback: int = 250) -> list[dict]:
    """Fetch OHLCV rows for a ticker. Equivalent to /api/ohlcv/bulk/{ticker}."""
    lookback = min(lookback, 1500)
    with get_db() as db:
        if end_date:
            rows = db.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM universe_ohlcv "
                "WHERE ticker=? AND date<=? "
                "ORDER BY date DESC LIMIT ?",
                (ticker, end_date, lookback)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM universe_ohlcv "
                "WHERE ticker=? "
                "ORDER BY date DESC LIMIT ?",
                (ticker, lookback)
            ).fetchall()
        return [dict(r) for r in rows]


def get_examples(setup_type: str) -> list[dict]:
    """Get examples for a setup type."""
    return query(
        f"SELECT id, ticker, entry_date, chart_date FROM examples "
        f"WHERE setup_type='{setup_type}' ORDER BY ticker"
    )


def get_tradable_sample(n: int = 500) -> list[str]:
    """Random sample of tradable tickers."""
    rows = query(f"SELECT ticker FROM tradable_universe ORDER BY RANDOM() LIMIT {n}")
    return [r['ticker'] for r in rows]


def execute_write(sql: str, params=None):
    """Execute a write query (INSERT/UPDATE/DELETE)."""
    with get_db() as db:
        if params:
            db.execute(sql, params)
        else:
            db.execute(sql)
        db.commit()


def executemany_write(sql: str, params_list):
    """Execute many write queries."""
    with get_db() as db:
        db.executemany(sql, params_list)
        db.commit()


def executescript(sql: str):
    """Execute a script (multiple statements)."""
    with get_db() as db:
        db.executescript(sql)
        db.commit()
