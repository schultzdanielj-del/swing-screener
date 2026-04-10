"""
Earnings Date Fetcher -- Yahoo Finance via yfinance.

Fetches ~20 historical + future earnings dates per ticker and writes them
to the local SQLite earnings_dates table. Used by the vetting chart to
flag trades that landed on earnings (avoiding accidental earnings bets).

This is a quarterly batch job, not nightly. Run it manually:
    python scripts/fetch_earnings.py
    python scripts/fetch_earnings.py --max-tickers 50      # test run
    python scripts/fetch_earnings.py --retry-missing        # only tickers not in DB

Data source: yfinance get_earnings_dates() -- scrapes Yahoo Finance HTML.
Rate: ~2 tickers/second. Full universe (~11,500 tickers) takes ~90 min.

Resumable -- tickers already in the DB are skipped by default.
"""

import os
import sys
import time
import pickle
import sqlite3
import argparse
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")

DEFAULT_DELAY = 0.5  # seconds between requests (heavier than quoteSummary)


# ==============================================================
# TICKER LIST
# ==============================================================

def load_universe_tickers():
    """Load ticker list from the daily OHLCV cache."""
    for name in ("universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                cache = pickle.load(f)
            tickers = sorted(cache.keys())
            print(f"  Loaded {len(tickers)} tickers from {name}")
            return tickers

    raise FileNotFoundError(
        "No OHLCV cache found in local_runner/cache/. "
        "Run cache_builder.py --daily first."
    )


# ==============================================================
# DB OPERATIONS
# ==============================================================

def get_db():
    """Open SQLite connection with earnings_dates table ensured."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS earnings_dates (
            ticker TEXT NOT NULL,
            earnings_date TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(ticker, earnings_date)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_earnings_ticker
        ON earnings_dates(ticker)
    """)
    db.commit()
    return db


def get_tickers_in_db(db):
    """Return set of tickers that already have earnings data."""
    rows = db.execute("SELECT DISTINCT ticker FROM earnings_dates").fetchall()
    return set(r[0] for r in rows)


def insert_dates(db, ticker, dates):
    """Insert earnings dates for a ticker. Returns count of new rows."""
    inserted = 0
    for d in dates:
        try:
            db.execute(
                "INSERT OR IGNORE INTO earnings_dates (ticker, earnings_date) VALUES (?, ?)",
                (ticker, d)
            )
            inserted += 1
        except Exception:
            pass
    db.commit()
    return inserted


# ==============================================================
# YAHOO FINANCE FETCH
# ==============================================================

def fetch_earnings_dates(ticker, limit=20):
    """Fetch earnings dates for one ticker via yfinance.

    Returns list of date strings (YYYY-MM-DD) or None on failure.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.get_earnings_dates(limit=limit)
        if df is None or df.empty:
            return None
        # Index is DatetimeIndex with earnings dates
        dates = sorted(set(d.strftime("%Y-%m-%d") for d in df.index))
        return dates
    except Exception:
        return None


# ==============================================================
# MAIN
# ==============================================================

def run(delay=DEFAULT_DELAY, max_tickers=None, retry_missing=False):
    print("\n" + "=" * 70)
    print("  EARNINGS DATE FETCHER -- Yahoo Finance (yfinance)")
    print("=" * 70)

    # Load ticker list
    print("\n  Loading universe tickers...")
    all_tickers = load_universe_tickers()

    # Check DB state
    db = get_db()
    existing_tickers = get_tickers_in_db(db)
    total_rows = db.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0]
    print(f"  DB state: {len(existing_tickers)} tickers, {total_rows} date rows")

    # Determine which tickers to fetch
    if retry_missing:
        to_fetch = [t for t in all_tickers if t not in existing_tickers]
        print(f"  Fetching {len(to_fetch)} tickers not in DB")
    else:
        to_fetch = [t for t in all_tickers if t not in existing_tickers]
        print(f"  To fetch: {len(to_fetch)} "
              f"(skipping {len(all_tickers) - len(to_fetch)} already in DB)")

    if max_tickers:
        to_fetch = to_fetch[:max_tickers]
        print(f"  Limited to {max_tickers} tickers")

    if not to_fetch:
        print("\n  Nothing to fetch -- all tickers have earnings data!")
        db.close()
        return

    # Fetch loop
    est_min = len(to_fetch) * delay / 60
    print(f"\n  Fetching {len(to_fetch)} tickers "
          f"(delay: {delay}s, est: {est_min:.0f} min)...\n")

    n_ok = 0
    n_empty = 0
    n_err = 0
    total_dates = 0
    t0 = time.time()

    for i, ticker in enumerate(to_fetch):
        dates = fetch_earnings_dates(ticker)

        if dates:
            new_rows = insert_dates(db, ticker, dates)
            total_dates += len(dates)
            n_ok += 1
        elif dates is not None:
            # Empty result (no earnings for this ticker -- ETFs, warrants, etc.)
            n_empty += 1
        else:
            n_err += 1

        # Progress
        if (i + 1) % 50 == 0 or i == len(to_fetch) - 1:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(to_fetch) - i - 1) / rate if rate > 0 else 0
            print(f"    {i + 1}/{len(to_fetch)}  "
                  f"ok={n_ok} empty={n_empty} err={n_err}  "
                  f"dates={total_dates}  "
                  f"[{elapsed:.0f}s, {rate:.1f}/s, ~{eta:.0f}s left]")

        time.sleep(delay)

    elapsed = time.time() - t0
    final_rows = db.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0]
    final_tickers = db.execute("SELECT COUNT(DISTINCT ticker) FROM earnings_dates").fetchone()[0]
    db.close()

    print(f"\n  Done in {elapsed / 60:.1f} min")
    print(f"  Fetched: {n_ok} with dates, {n_empty} empty, {n_err} errors")
    print(f"  DB total: {final_tickers} tickers, {final_rows} date rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch earnings dates from Yahoo Finance"
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY,
        help=f"Seconds between requests (default: {DEFAULT_DELAY})"
    )
    parser.add_argument(
        "--max-tickers", type=int, default=None,
        help="Max tickers to fetch (for testing)"
    )
    parser.add_argument(
        "--retry-missing", action="store_true",
        help="Only fetch tickers not already in DB"
    )
    args = parser.parse_args()

    run(delay=args.delay, max_tickers=args.max_tickers,
        retry_missing=args.retry_missing)
