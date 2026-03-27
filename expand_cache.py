"""
Phase 1: OHLCV Expansion — Pull missing tickers from Railway into local cache.

Loads existing universe_ohlcv_5yr.pkl, queries Railway for all tickers in
universe_ohlcv, pulls OHLCV for any tickers not already in the local cache,
merges them in, and saves.

Usage:
    python expand_cache.py

Run from the swing-screener repo root.
"""

import os
import sys
import time
import pickle
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://web-production-e3025.up.railway.app"
CACHE_DIR = os.path.join("local_runner", "cache")
CACHE_5YR_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
BACKUP_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_5yr_backup.pkl")
MAX_WORKERS = 20


def get_all_railway_tickers():
    """Fetch ALL distinct tickers from universe_ohlcv on Railway."""
    r = requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": "SELECT DISTINCT ticker FROM universe_ohlcv ORDER BY ticker",
        "limit": 15000
    }, timeout=120)
    r.raise_for_status()
    return [row["ticker"] for row in r.json()["results"]]


def compute_dvol_20d(df):
    """Add 20-day average dollar volume column."""
    df["dvol_20d"] = (df["close"] * df["volume"]).rolling(20).mean()
    return df


def fetch_one_ticker(ticker):
    """Fetch ALL OHLCV for a single ticker from Railway. No bar limit."""
    try:
        sql = (
            f"SELECT date, open, high, low, close, volume "
            f"FROM universe_ohlcv WHERE ticker = '{ticker}' "
            f"ORDER BY date DESC"
        )
        r = requests.post(f"{API_BASE}/api/query/bulk", json={
            "sql": sql, "limit": 10000
        }, timeout=60)
        if r.status_code != 200:
            return ticker, None

        rows = r.json().get("results", [])
        if not rows:
            return ticker, None

        df = pd.DataFrame(rows)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        compute_dvol_20d(df)
        return ticker, df
    except Exception:
        return ticker, None


def main():
    # Step 1: Load existing local cache
    print("=" * 60)
    print("  PHASE 1: OHLCV EXPANSION")
    print("=" * 60)

    if not os.path.exists(CACHE_5YR_FILE):
        print(f"ERROR: {CACHE_5YR_FILE} not found.")
        sys.exit(1)

    print(f"\nLoading existing cache: {CACHE_5YR_FILE}")
    with open(CACHE_5YR_FILE, "rb") as f:
        cache = pickle.load(f)
    print(f"  Existing: {len(cache)} tickers")

    # Step 2: Get all tickers from Railway
    print("\nQuerying Railway for all tickers in universe_ohlcv...")
    try:
        all_tickers = get_all_railway_tickers()
    except Exception as e:
        print(f"ERROR: Failed to query Railway: {e}")
        sys.exit(1)
    print(f"  Railway: {len(all_tickers)} tickers")

    # Step 3: Find the gap
    local_set = set(cache.keys())
    railway_set = set(all_tickers)
    missing = sorted(railway_set - local_set)
    print(f"  Missing from local: {len(missing)} tickers")

    if not missing:
        print("\nNo missing tickers. Cache is already complete.")
        return

    # Step 4: Backup existing cache
    print(f"\nBacking up existing cache to {BACKUP_FILE}...")
    with open(BACKUP_FILE, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    backup_mb = os.path.getsize(BACKUP_FILE) / 1024 / 1024
    print(f"  Backup saved ({backup_mb:.0f} MB)")

    # Step 5: Pull missing tickers from Railway
    print(f"\nPulling {len(missing)} tickers from Railway ({MAX_WORKERS} workers)...")
    t0 = time.time()
    fetched = 0
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one_ticker, t): t for t in missing}
        done = 0
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1
            if df is not None and len(df) >= 10:
                cache[ticker] = df
                fetched += 1
            else:
                failed.append(ticker)

            if done % 200 == 0 or done == len(missing):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(missing) - done) / rate if rate > 0 else 0
                print(f"  {done:,}/{len(missing):,} done "
                      f"({fetched:,} ok, {len(failed)} failed) "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining]")

    elapsed = time.time() - t0
    print(f"\nFetch complete in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  Added: {fetched} tickers")
    print(f"  Failed: {len(failed)} tickers")
    print(f"  Total in cache: {len(cache)} tickers")

    # Step 6: Save expanded cache
    print(f"\nSaving expanded cache...")
    with open(CACHE_5YR_FILE, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(CACHE_5YR_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_5YR_FILE} ({size_mb:.0f} MB)")

    # Stats
    bar_counts = [len(df) for df in cache.values()]
    total_rows = sum(bar_counts)
    print(f"\n  Total tickers: {len(cache)}")
    print(f"  Total rows: {total_rows:,}")
    print(f"  Bar range: {min(bar_counts)} - {max(bar_counts)}")
    print(f"  Avg bars/ticker: {total_rows // len(cache)}")

    if failed:
        print(f"\n  Failed tickers ({len(failed)}):")
        for t in failed[:20]:
            print(f"    {t}")
        if len(failed) > 20:
            print(f"    ... and {len(failed) - 20} more")

    print("\nPhase 1 complete.")


if __name__ == "__main__":
    main()
