"""
Tradable Universe Builder

Filters the full daily OHLCV cache down to stocks meeting minimum liquidity
and volatility standards. Designed to run nightly after OHLCV refresh.

Criteria (based on most recent 20 trading days):
  - Last close >= $1.00
  - 20-day avg dollar volume >= $4,000,000
  - 20-day ADRP (TC2000 formula) >= 1.8%

ADRP = (mean(High[i] / Low[i] for i in last 20 bars) - 1) * 100

Source: local_runner/cache/universe_ohlcv_daily.pkl (NOT the empty SQLite table)
Output: tradable_universe table in scanperfect.db
"""

import sqlite3
import logging
import pickle
import sys
from datetime import datetime
from pathlib import Path
import os
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradable")

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "data" / "scanperfect.db"
PICKLE_PATH = REPO_ROOT / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"

# --- Filters (match pyramid_grinder.compute_tradable_masks) ---
MIN_PRICE = 1.0
MIN_AVG_DOLLAR_VOL = 4_000_000
MIN_ADRP_PCT = 1.8
LOOKBACK_DAYS = 20


def get_db(db_path=None):
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_tradable_table(db):
    """Drop and recreate tradable_universe with current schema (adrp_pct column)."""
    db.executescript("""
        DROP TABLE IF EXISTS tradable_universe;
        CREATE TABLE tradable_universe (
            ticker TEXT PRIMARY KEY,
            last_close REAL,
            adrp_pct REAL,
            avg_dollar_vol REAL,
            updated_at TEXT
        );
    """)
    db.commit()


def build_tradable_universe(db_path=None):
    """Main entry point. Rebuilds the tradable_universe table from the OHLCV pickle."""
    if not PICKLE_PATH.exists():
        log.error(f"OHLCV pickle not found: {PICKLE_PATH}")
        log.error("Run nightly refresh first to populate the cache.")
        return 0

    db = get_db(db_path)
    init_tradable_table(db)

    log.info(f"Loading OHLCV pickle: {PICKLE_PATH}")
    with open(PICKLE_PATH, "rb") as f:
        cache = pickle.load(f)
    log.info(f"Loaded {len(cache)} tickers from pickle")

    qualified = []
    now = datetime.utcnow().isoformat()
    skipped_short_history = 0
    skipped_filter = 0

    for ticker, df in cache.items():
        if df is None or len(df) < LOOKBACK_DAYS + 1:
            skipped_short_history += 1
            continue

        # Use the most recent LOOKBACK_DAYS bars for tradability check
        last_n = df.tail(LOOKBACK_DAYS)
        closes = last_n["close"].values.astype(np.float64)
        highs = last_n["high"].values.astype(np.float64)
        lows = last_n["low"].values.astype(np.float64)

        last_close = float(closes[-1]) if len(closes) > 0 else 0.0
        if not np.isfinite(last_close) or last_close < MIN_PRICE:
            skipped_filter += 1
            continue

        # ADRP_20 (TC2000 formula): (mean(H/L) - 1) * 100
        if np.any(lows <= 0):
            skipped_filter += 1
            continue
        adrp_pct = float((np.mean(highs / lows) - 1.0) * 100.0)
        if not np.isfinite(adrp_pct) or adrp_pct < MIN_ADRP_PCT:
            skipped_filter += 1
            continue

        # 20-day avg dollar volume — prefer dvol_20d column if present
        if "dvol_20d" in df.columns:
            avg_dvol = float(df["dvol_20d"].iloc[-1])
        else:
            volumes = last_n["volume"].values.astype(np.float64)
            avg_dvol = float(np.mean(closes * volumes))
        if not np.isfinite(avg_dvol) or avg_dvol < MIN_AVG_DOLLAR_VOL:
            skipped_filter += 1
            continue

        qualified.append((
            ticker,
            round(last_close, 4),
            round(adrp_pct, 2),
            round(avg_dvol, 0),
            now,
        ))

    # Insert qualified rows
    if qualified:
        db.executemany(
            "INSERT INTO tradable_universe (ticker, last_close, adrp_pct, avg_dollar_vol, updated_at) VALUES (?,?,?,?,?)",
            qualified
        )
    db.commit()

    log.info(f"Tradable universe: {len(qualified)} qualify "
             f"({skipped_filter} filtered, {skipped_short_history} short history)")
    db.close()
    return len(qualified)


if __name__ == "__main__":
    n = build_tradable_universe()
    sys.exit(0 if n > 0 else 1)
