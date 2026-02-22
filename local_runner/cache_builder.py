"""
Cache Builder — Pull all tradable universe OHLCV data and store locally.

Usage:
    python local_runner/cache_builder.py [--force]

Stores: local_runner/cache/universe_ohlcv.pkl
  - Dict of {ticker: pd.DataFrame} with OHLCV data
  - Only rebuilds if cache is >24h old (or --force)

Requires: pip install requests pandas
"""

import os
import sys
import time
import pickle
import hashlib
import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://web-production-e3025.up.railway.app"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
CACHE_META = os.path.join(CACHE_DIR, "cache_meta.txt")
MAX_WORKERS = 20
LOOKBACK = 300  # bars per ticker


def get_tradable_tickers():
    """Fetch all tickers from tradable_universe table."""
    r = requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": "SELECT ticker FROM tradable_universe ORDER BY ticker",
        "limit": 5000
    }, timeout=30)
    r.raise_for_status()
    return [row["ticker"] for row in r.json()["results"]]


def fetch_one_ticker(ticker):
    """Fetch OHLCV for a single ticker."""
    try:
        sql = (
            f"SELECT date, open, high, low, close, volume "
            f"FROM universe_ohlcv WHERE ticker = '{ticker}' "
            f"ORDER BY date DESC LIMIT {LOOKBACK}"
        )
        r = requests.post(f"{API_BASE}/api/query/bulk", json={
            "sql": sql, "limit": LOOKBACK
        }, timeout=30)
        if r.status_code != 200:
            return ticker, None

        rows = r.json().get("results", [])
        if not rows or len(rows) < 50:
            return ticker, None

        df = pd.DataFrame(rows)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return ticker, df
    except Exception as e:
        return ticker, None


def cache_is_fresh():
    """Check if cache exists and is less than 24h old."""
    if not os.path.exists(CACHE_FILE) or not os.path.exists(CACHE_META):
        return False
    try:
        with open(CACHE_META) as f:
            ts = datetime.fromisoformat(f.read().strip())
        return datetime.now() - ts < timedelta(hours=24)
    except:
        return False


def build_cache(force=False):
    """Build the full OHLCV cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force and cache_is_fresh():
        # Load existing
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"Cache is fresh ({len(data)} tickers). Use --force to rebuild.")
        return data

    print("=" * 60)
    print("  CACHE BUILDER — Fetching tradable universe OHLCV")
    print("=" * 60)

    # Get ticker list
    print("\nFetching ticker list...")
    tickers = get_tradable_tickers()
    print(f"  {len(tickers)} tradable tickers")

    # Fetch all OHLCV concurrently
    print(f"\nFetching OHLCV data ({MAX_WORKERS} concurrent workers)...")
    t0 = time.time()
    universe = {}
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one_ticker, t): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1
            if df is not None:
                universe[ticker] = df
            else:
                failed.append(ticker)

            if done % 100 == 0 or done == len(tickers):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(tickers) - done) / rate if rate > 0 else 0
                print(f"  {done:,}/{len(tickers):,} fetched "
                      f"({len(universe):,} ok, {len(failed)} failed) "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining]")

    elapsed = time.time() - t0
    print(f"\nFetch complete: {len(universe):,} tickers in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    if failed:
        print(f"  Failed: {len(failed)} tickers (too short or no data)")

    # Save cache
    print(f"\nSaving cache...")
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    # Save metadata
    with open(CACHE_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_FILE} ({size_mb:.1f} MB)")

    # Stats
    total_rows = sum(len(df) for df in universe.values())
    avg_bars = total_rows / len(universe) if universe else 0
    print(f"  Total rows: {total_rows:,}")
    print(f"  Avg bars/ticker: {avg_bars:.0f}")
    print()

    return universe


def load_cache():
    """Load the cache, building if needed."""
    if not os.path.exists(CACHE_FILE):
        return build_cache()
    with open(CACHE_FILE, "rb") as f:
        return pickle.load(f)


if __name__ == "__main__":
    force = "--force" in sys.argv
    data = build_cache(force=force)
    print(f"Cache ready: {len(data)} tickers")
