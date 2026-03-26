"""
Tradable Universe Builder

Filters the 5yr OHLCV cache down to stocks meeting minimum liquidity
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
import pickle
from datetime import datetime
from pathlib import Path
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradable")

REPO_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent
DB_PATH = REPO_ROOT / "data" / "scanperfect.db"
CACHE_5YR = REPO_ROOT / "local_runner" / "cache" / "universe_ohlcv_5yr.pkl"

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
    # Check if existing table has the right schema
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tradable_universe'"
    ).fetchone()
    if row and "last_close" not in row[0]:
        db.execute("DROP TABLE tradable_universe")
        db.commit()
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
    """Main entry point. Rebuilds the tradable_universe table from 5yr OHLCV cache."""
    db = get_db(db_path)
    init_tradable_table(db)

    log.info("Building tradable universe...")

    # Load 5yr OHLCV cache
    cache_path = str(CACHE_5YR)
    if not os.path.exists(cache_path):
        log.error(f"5yr cache not found at {cache_path}")
        db.close()
        return 0

    log.info(f"Loading 5yr cache...")
    with open(cache_path, "rb") as f:
        universe = pickle.load(f)
    log.info(f"Evaluating {len(universe)} tickers against tradable filters")

    qualified = []
    now = datetime.utcnow().isoformat()

    for ticker, df in universe.items():
        if len(df) < 21:
            continue

        # Last 21 bars (chronological — df is already sorted by date)
        tail = df.iloc[-21:]

        last_close = tail.iloc[-1]["close"]
        if last_close is None or last_close < MIN_PRICE:
            continue

        # Compute APTR and avg dollar volume over last 20 days (index 1..20)
        aptr_sum = 0.0
        dvol_sum = 0.0
        valid_days = 0

        for i in range(1, 21):
            h = tail.iloc[i]["high"]
            l = tail.iloc[i]["low"]
            c = tail.iloc[i]["close"]
            prev_c = tail.iloc[i - 1]["close"]
            vol = tail.iloc[i]["volume"]

            if any(v is None for v in (h, l, c, prev_c, vol)) or c <= 0:
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

    total_evaluated = len(universe)
    del universe  # free memory

    # Rebuild table
    db.execute("DELETE FROM tradable_universe")
    if qualified:
        db.executemany(
            "INSERT INTO tradable_universe (ticker, last_close, aptr_pct, avg_dollar_vol, updated_at) VALUES (?,?,?,?,?)",
            qualified
        )
    db.commit()

    log.info(f"Tradable universe: {len(qualified)} tickers qualify out of {total_evaluated}")
    db.close()
    return len(qualified)


if __name__ == "__main__":
    build_tradable_universe()
