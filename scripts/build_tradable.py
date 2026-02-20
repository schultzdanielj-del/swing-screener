"""
Tradable Universe Builder

Filters the full universe_ohlcv down to stocks meeting minimum liquidity
and volatility standards. Designed to run nightly after OHLCV refresh.

Criteria (based on most recent 20 trading days):
  - Last close >= $1.00
  - 20-day avg APTR (Average Percentage True Range) >= 1.5%
  - 20-day avg dollar volume >= $5,000,000

APTR = average of daily (TrueRange / Close * 100) over 20 days
  where TrueRange = max(H-L, abs(H-prevC), abs(L-prevC))
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradable")

DB_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
DB_PATH = DB_DIR / "scanperfect.db"

# --- Filters ---
MIN_PRICE = 1.0
MIN_APTR_PCT = 1.5
MIN_AVG_DOLLAR_VOL = 5_000_000
LOOKBACK_DAYS = 20


def get_db(db_path=None):
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_tradable_table(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tradable_universe (
            ticker TEXT PRIMARY KEY,
            last_close REAL,
            aptr_pct REAL,
            avg_dollar_vol REAL,
            updated_at TEXT
        );
    """)
    db.commit()


def build_tradable_universe(db_path=None):
    """Main entry point. Rebuilds the tradable_universe table from scratch."""
    db = get_db(db_path)
    init_tradable_table(db)

    log.info("Building tradable universe...")

    # Get all tickers that have data
    tickers = [r[0] for r in db.execute(
        "SELECT DISTINCT ticker FROM universe_ohlcv"
    ).fetchall()]

    log.info(f"Evaluating {len(tickers)} tickers against tradable filters")

    qualified = []
    now = datetime.utcnow().isoformat()

    for ticker in tickers:
        # Get last 21 rows (need 21 for 20 days of true range with prev close)
        rows = db.execute("""
            SELECT date, open, high, low, close, volume
            FROM universe_ohlcv
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT 21
        """, (ticker,)).fetchall()

        if len(rows) < 21:
            continue

        # Rows are newest-first, reverse for chronological
        rows = list(reversed(rows))

        last_close = rows[-1]["close"]
        if last_close is None or last_close < MIN_PRICE:
            continue

        # Compute APTR and avg dollar volume over last 20 days (index 1..20)
        aptr_sum = 0.0
        dvol_sum = 0.0
        valid_days = 0

        for i in range(1, 21):
            h = rows[i]["high"]
            l = rows[i]["low"]
            c = rows[i]["close"]
            prev_c = rows[i - 1]["close"]
            vol = rows[i]["volume"]

            if None in (h, l, c, prev_c, vol) or c <= 0:
                continue

            # True Range
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            aptr_sum += (tr / c) * 100.0
            dvol_sum += c * vol
            valid_days += 1

        if valid_days < 15:  # need at least 15 of 20 days
            continue

        aptr_pct = aptr_sum / valid_days
        avg_dvol = dvol_sum / valid_days

        if aptr_pct >= MIN_APTR_PCT and avg_dvol >= MIN_AVG_DOLLAR_VOL:
            qualified.append((ticker, last_close, round(aptr_pct, 2), round(avg_dvol, 0), now))

    # Rebuild table
    db.execute("DELETE FROM tradable_universe")
    if qualified:
        db.executemany(
            "INSERT INTO tradable_universe (ticker, last_close, aptr_pct, avg_dollar_vol, updated_at) VALUES (?,?,?,?,?)",
            qualified
        )
    db.commit()

    log.info(f"Tradable universe: {len(qualified)} tickers qualify out of {len(tickers)}")
    db.close()
    return len(qualified)


if __name__ == "__main__":
    build_tradable_universe()
