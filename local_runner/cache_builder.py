"""
Cache Builder — Pull all tradable universe OHLCV data and store locally.

Usage:
    python local_runner/cache_builder.py [--force]
    python local_runner/cache_builder.py --5yr [--force]

Stores:
  - local_runner/cache/universe_ohlcv.pkl — 300-bar daily cache (legacy)
  - local_runner/cache/universe_ohlcv_5yr.pkl — full 5yr daily cache

All data pulled from yfinance. No Railway dependency.

Requires: pip install yfinance pandas
"""

import os
import sys
import time
import pickle
import sqlite3
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
CACHE_META = os.path.join(CACHE_DIR, "cache_meta.txt")
CACHE_5YR_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
CACHE_5YR_META = os.path.join(CACHE_DIR, "cache_5yr_meta.txt")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
MAX_WORKERS = 20
LOOKBACK = 300  # bars per ticker (daily matrix)
LOOKBACK_5YR = 1260  # ~5 years of trading days


# ══════════════════════════════════════════════════════════════
# TICKER LIST — from local SQLite (no Railway)
# ══════════════════════════════════════════════════════════════

def get_tradable_tickers_local():
    """Read tradable tickers from local SQLite database."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Local database not found: {DB_PATH}\n"
            "  Run build_tradable.py first to populate tradable_universe."
        )
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT ticker FROM tradable_universe ORDER BY ticker"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# YFINANCE HELPERS
# ══════════════════════════════════════════════════════════════

def _yf_download_daily(ticker, period="5y"):
    """Download daily OHLCV for one ticker from yfinance.

    Returns (ticker, DataFrame) or (ticker, None) on failure.
    DataFrame has columns: date, open, high, low, close, volume
    """
    try:
        raw = yf.download(ticker, period=period, interval="1d", progress=False)
        if raw.empty:
            return ticker, None

        # Handle MultiIndex columns (yfinance sometimes returns these)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw.reset_index()
        df.columns = [c.lower() for c in df.columns]

        # Ensure standard column names
        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"], errors="ignore")

        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                return ticker, None
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date"] = pd.to_datetime(df["date"])
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("date").reset_index(drop=True)

        # Drop rows with NaN close (bad data)
        df = df.dropna(subset=["close"]).reset_index(drop=True)

        if len(df) < 5:
            return ticker, None

        return ticker, df
    except Exception:
        return ticker, None


def _yf_append_after_date(ticker, after_date):
    """Download daily bars after a given date from yfinance.

    Args:
        ticker: stock symbol
        after_date: string 'YYYY-MM-DD' — fetch bars AFTER this date

    Returns (ticker, DataFrame of new bars) or (ticker, None)
    """
    try:
        # Start from the day after last cached date
        start = (pd.Timestamp(after_date) + timedelta(days=1)).strftime("%Y-%m-%d")

        raw = yf.download(ticker, start=start, interval="1d", progress=False)
        if raw.empty:
            return ticker, None

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw.reset_index()
        df.columns = [c.lower() for c in df.columns]

        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"], errors="ignore")

        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                return ticker, None
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date"] = pd.to_datetime(df["date"])
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("date").reset_index(drop=True)
        df = df.dropna(subset=["close"]).reset_index(drop=True)

        if len(df) == 0:
            return ticker, None

        return ticker, df
    except Exception:
        return ticker, None


# ══════════════════════════════════════════════════════════════
# DVOL HELPER
# ══════════════════════════════════════════════════════════════

def compute_dvol_20d(df):
    """Add 20-day average dollar volume column to an OHLCV DataFrame.

    dvol_20d = rolling 20-bar mean of (close * volume).
    First 19 bars will be NaN. Computed in-place.
    """
    df["dvol_20d"] = (df["close"] * df["volume"]).rolling(20).mean()
    return df


# ══════════════════════════════════════════════════════════════
# 300-BAR CACHE (legacy — kept for backward compat)
# ══════════════════════════════════════════════════════════════

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
    """Build the 300-bar OHLCV cache from yfinance."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force and cache_is_fresh():
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"Cache is fresh ({len(data)} tickers). Use --force to rebuild.")
        return data

    print("=" * 60)
    print("  CACHE BUILDER — Fetching tradable universe OHLCV")
    print("=" * 60)

    print("\nFetching ticker list from local DB...")
    tickers = get_tradable_tickers_local()
    print(f"  {len(tickers)} tradable tickers")

    print(f"\nFetching OHLCV data via yfinance ({MAX_WORKERS} concurrent workers)...")
    t0 = time.time()
    universe = {}
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        # Use 1y period for 300-bar cache
        futures = {pool.submit(_yf_download_daily, t, "1y"): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1
            if df is not None:
                compute_dvol_20d(df)
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

    print(f"\nSaving cache...")
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    with open(CACHE_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_FILE} ({size_mb:.1f} MB)")

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
    """Build the 5-year OHLCV cache from yfinance."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force and cache_5yr_is_fresh():
        with open(CACHE_5YR_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"5yr cache is fresh ({len(data)} tickers). Use --force to rebuild.")
        return data

    print("=" * 60)
    print("  5-YEAR CACHE BUILDER — Full history via yfinance")
    print("=" * 60)

    print("\nFetching ticker list from local DB...")
    tickers = get_tradable_tickers_local()
    print(f"  {len(tickers)} tradable tickers × ~{LOOKBACK_5YR} bars max")

    print(f"\nFetching 5yr OHLCV data via yfinance ({MAX_WORKERS} concurrent workers)...")
    t0 = time.time()
    universe = {}
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_yf_download_daily, t, "5y"): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            ticker, df = future.result()
            done += 1
            if df is not None:
                compute_dvol_20d(df)
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

    print(f"\nSaving 5yr cache...")
    with open(CACHE_5YR_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    with open(CACHE_5YR_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_5YR_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_5YR_FILE} ({size_mb:.1f} MB)")

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


def append_5yr_cache():
    """Append new bars to existing 5yr cache via yfinance. Never touches old bars.

    For each ticker already in the cache, fetches only bars after the last
    cached date from yfinance and appends them. New tickers (in tradable
    universe but not in cache) get a full 5yr fetch. Old bars are never modified.
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

    # Get current tradable tickers from local DB for new ticker detection
    try:
        tickers = get_tradable_tickers_local()
        print(f"  {len(tickers)} tradable tickers in DB")
    except FileNotFoundError:
        # No local DB yet — just append to existing tickers
        tickers = list(universe.keys())
        print(f"  No local DB — appending to {len(tickers)} cached tickers")

    # Find last date per cached ticker
    last_dates = {}
    for ticker, df in universe.items():
        if len(df) > 0:
            last_dates[ticker] = str(df["date"].iloc[-1])[:10]

    # Categorize work
    to_append = list(universe.keys())  # all existing tickers get checked
    to_fetch_full = [t for t in tickers if t not in universe]  # new tickers

    print(f"  Tickers to check for new bars: {len(to_append)}")
    print(f"  New tickers (full fetch): {len(to_fetch_full)}")

    t0 = time.time()
    appended = 0
    new_added = 0
    no_new = 0
    failed = 0

    # Append new bars for existing tickers
    if to_append:
        print(f"\n  Appending new bars via yfinance ({MAX_WORKERS} workers)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_yf_append_after_date, t, last_dates.get(t, "2020-01-01")): t
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
                        # Deduplicate by date (in case of overlap)
                        combined = combined.drop_duplicates(subset=["date"], keep="last")
                        combined = combined.reset_index(drop=True)
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
        print(f"\n  Fetching {len(to_fetch_full)} new tickers via yfinance...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_yf_download_daily, t, "5y"): t for t in to_fetch_full}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, df = future.result()
                    if df is not None and len(df) >= 50:
                        compute_dvol_20d(df)
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


# ══════════════════════════════════════════════════════════════
# FRESHNESS CHECK — Is there new market data today?
# ══════════════════════════════════════════════════════════════

def check_yfinance_freshness():
    """Check if yfinance has newer data than our cache.

    Downloads 1 bar for SPY and compares to last cached date.

    Returns:
        True if new data available (should continue pipeline)
        False if cache is already current (skip pipeline)
    """
    if not os.path.exists(CACHE_5YR_FILE):
        print("  No 5yr cache exists — new data available by default")
        return True

    # Get last cached date from SPY (or first ticker in cache)
    with open(CACHE_5YR_FILE, "rb") as f:
        universe = pickle.load(f)

    if "SPY" in universe:
        last_cached = str(universe["SPY"]["date"].iloc[-1])[:10]
    else:
        # Use first available ticker
        first_ticker = next(iter(universe))
        last_cached = str(universe[first_ticker]["date"].iloc[-1])[:10]

    del universe  # free memory

    # Download latest bar from yfinance
    try:
        raw = yf.download("SPY", period="5d", interval="1d", progress=False)
        if raw.empty:
            print("  Could not fetch SPY from yfinance — assuming new data")
            return True

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        yf_last = str(raw.index[-1])[:10]

        print(f"  Cache last date: {last_cached}")
        print(f"  yfinance last:   {yf_last}")

        if yf_last > last_cached:
            print(f"  → New trading day detected")
            return True
        else:
            print(f"  → Already up to date")
            return False

    except Exception as e:
        print(f"  Could not check yfinance: {e}")
        print("  → Assuming new data (safe fallback)")
        return True


if __name__ == "__main__":
    force = "--force" in sys.argv
    mode_5yr = "--5yr" in sys.argv

    if mode_5yr:
        data = build_5yr_cache(force=force)
        print(f"5yr cache ready: {len(data)} tickers")
    else:
        data = build_cache(force=force)
        print(f"Cache ready: {len(data)} tickers")
