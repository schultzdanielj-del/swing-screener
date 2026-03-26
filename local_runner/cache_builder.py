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
CACHE_5YR_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
CACHE_5YR_META = os.path.join(CACHE_DIR, "cache_5yr_meta.txt")
MAX_WORKERS = 20
LOOKBACK = 300  # bars per ticker (daily matrix)
LOOKBACK_5YR = 1260  # ~5 years of trading days


def get_tradable_tickers():
    """Fetch all tickers from tradable_universe table."""
    r = requests.post(f"{API_BASE}/api/query/bulk", json={
        "sql": "SELECT ticker FROM tradable_universe ORDER BY ticker",
        "limit": 5000
    }, timeout=30)
    r.raise_for_status()
    return [row["ticker"] for row in r.json()["results"]]


def compute_dvol_20d(df):
    """Add 20-day average dollar volume column to an OHLCV DataFrame.
    
    dvol_20d = rolling 20-bar mean of (close * volume).
    First 19 bars will be NaN. Computed in-place.
    """
    df["dvol_20d"] = (df["close"] * df["volume"]).rolling(20).mean()
    return df


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
        if not rows:
            return ticker, None

        df = pd.DataFrame(rows)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        compute_dvol_20d(df)
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


# ══════════════════════════════════════════════════════════════
# 5-YEAR CACHE — For historical scorer (Phase 2)
# ══════════════════════════════════════════════════════════════

def fetch_one_ticker_5yr(ticker):
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
    except Exception as e:
        return ticker, None


def cache_5yr_is_fresh():
    """Check if 5yr cache exists and is less than 7 days old."""
    if not os.path.exists(CACHE_5YR_FILE) or not os.path.exists(CACHE_5YR_META):
        return False
    try:
        with open(CACHE_5YR_META) as f:
            ts = datetime.fromisoformat(f.read().strip())
        return datetime.now() - ts < timedelta(days=7)
    except:
        return False


def build_5yr_cache(force=False):
    """Build the 5-year OHLCV cache for historical scoring."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force and cache_5yr_is_fresh():
        with open(CACHE_5YR_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"5yr cache is fresh ({len(data)} tickers). Use --force to rebuild.")
        return data

    print("=" * 60)
    print("  5-YEAR CACHE BUILDER — Full history for Phase 2")
    print("=" * 60)

    # Get ticker list (filtered — excludes leveraged/inverse etc.)
    print("\nFetching ticker list...")
    tickers = get_tradable_tickers()
    print(f"  {len(tickers)} tradable tickers × {LOOKBACK_5YR} bars max")

    # Fetch all OHLCV concurrently
    print(f"\nFetching 5yr OHLCV data ({MAX_WORKERS} concurrent workers)...")
    t0 = time.time()
    universe = {}
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one_ticker_5yr, t): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1
            if df is not None:
                universe[ticker] = df
            else:
                failed.append(ticker)

            if done % 200 == 0 or done == len(tickers):
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
    print(f"\nSaving 5yr cache...")
    with open(CACHE_5YR_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    # Save metadata
    with open(CACHE_5YR_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_5YR_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_5YR_FILE} ({size_mb:.1f} MB)")

    # Stats
    total_rows = sum(len(df) for df in universe.values())
    avg_bars = total_rows / len(universe) if universe else 0
    bar_counts = [len(df) for df in universe.values()]
    print(f"  Total rows: {total_rows:,}")
    print(f"  Avg bars/ticker: {avg_bars:.0f}")
    print(f"  Min/Max bars: {min(bar_counts)}/{max(bar_counts)}")
    print(f"  File size: {size_mb:.0f} MB")
    print()

    return universe


def load_5yr_cache():
    """Load the 5yr cache, building if needed."""
    if not os.path.exists(CACHE_5YR_FILE):
        return build_5yr_cache()
    with open(CACHE_5YR_FILE, "rb") as f:
        return pickle.load(f)


def _fetch_ticker_after_date(ticker, after_date):
    """Fetch OHLCV bars for a ticker after a given date from Railway."""
    try:
        sql = (
            f"SELECT date, open, high, low, close, volume "
            f"FROM universe_ohlcv WHERE ticker = '{ticker}' "
            f"AND date > '{after_date}' "
            f"ORDER BY date ASC"
        )
        r = requests.post(f"{API_BASE}/api/query/bulk", json={
            "sql": sql, "limit": 500
        }, timeout=30)
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
        return ticker, df
    except Exception:
        return ticker, None


def append_5yr_cache():
    """Append new bars to existing 5yr cache. Never touches old bars.

    For each ticker already in the cache, fetches only bars after the last
    cached date from Railway and appends them. New tickers (in tradable
    universe but not in cache) get a full fetch. Old bars are never modified.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not os.path.exists(CACHE_5YR_FILE):
        print("  No existing 5yr cache. Running full build...")
        return build_5yr_cache()

    # Load existing cache
    print("  Loading existing 5yr cache...")
    with open(CACHE_5YR_FILE, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe)} tickers in cache")

    # Get current tradable tickers
    print("  Fetching tradable ticker list...")
    tickers = get_tradable_tickers()
    print(f"  {len(tickers)} tradable tickers")

    # Find last date per cached ticker
    last_dates = {}
    for ticker, df in universe.items():
        if len(df) > 0:
            last_dates[ticker] = str(df["date"].iloc[-1])[:10]

    # Categorize work
    to_append = []  # existing tickers needing new bars
    to_fetch_full = []  # new tickers not in cache

    for ticker in tickers:
        if ticker in last_dates:
            to_append.append(ticker)
        else:
            to_fetch_full.append(ticker)

    print(f"  Tickers to append: {len(to_append)}")
    print(f"  New tickers (full fetch): {len(to_fetch_full)}")

    t0 = time.time()
    appended = 0
    new_added = 0
    no_new = 0
    failed = 0

    # Append new bars for existing tickers
    if to_append:
        print(f"\n  Appending new bars ({MAX_WORKERS} workers)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_ticker_after_date, t, last_dates[t]): t
                for t in to_append
            }
            done = 0
            for future in as_completed(futures):
                ticker = futures[future]
                done += 1
                try:
                    _, new_df = future.result()
                    if new_df is not None and len(new_df) > 0:
                        existing = universe[ticker]
                        # Recompute dvol_20d on combined data
                        combined = pd.concat([existing, new_df], ignore_index=True)
                        combined = combined.sort_values("date").reset_index(drop=True)
                        compute_dvol_20d(combined)
                        universe[ticker] = combined
                        appended += 1
                    else:
                        no_new += 1
                except Exception:
                    failed += 1

                if done % 500 == 0 or done == len(to_append):
                    elapsed = time.time() - t0
                    print(f"    {done:,}/{len(to_append):,} checked "
                          f"({appended} appended, {no_new} current, {failed} failed) "
                          f"[{elapsed:.0f}s]")

    # Full fetch for new tickers
    if to_fetch_full:
        print(f"\n  Fetching {len(to_fetch_full)} new tickers...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_one_ticker_5yr, t): t for t in to_fetch_full}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, df = future.result()
                    if df is not None and len(df) >= 50:
                        universe[ticker] = df
                        new_added += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1

    elapsed = time.time() - t0

    # Save
    print(f"\n  Saving 5yr cache...")
    with open(CACHE_5YR_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    with open(CACHE_5YR_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_5YR_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_5YR_FILE} ({size_mb:.1f} MB)")
    print(f"  Appended: {appended}, New: {new_added}, "
          f"No change: {no_new}, Failed: {failed}")
    print(f"  Time: {elapsed:.0f}s")

    return universe


if __name__ == "__main__":
    force = "--force" in sys.argv
    mode_5yr = "--5yr" in sys.argv

    if mode_5yr:
        data = build_5yr_cache(force=force)
        print(f"5yr cache ready: {len(data)} tickers")
    else:
        data = build_cache(force=force)
        print(f"Cache ready: {len(data)} tickers")
