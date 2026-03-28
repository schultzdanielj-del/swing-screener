"""
Cache Builder — Pull all tradable universe OHLCV data and store locally.

Usage:
    python local_runner/cache_builder.py [--force]
    python local_runner/cache_builder.py --5yr [--force]
    python local_runner/cache_builder.py --htf [--force]

Stores:
  - local_runner/cache/universe_ohlcv.pkl — 300-bar daily cache (legacy)
  - local_runner/cache/universe_ohlcv_5yr.pkl — full 5yr daily cache
  - local_runner/cache/universe_ohlcv_weekly.pkl — 5yr weekly cache
  - local_runner/cache/universe_ohlcv_monthly.pkl — 5yr monthly cache

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
WEEKLY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_weekly.pkl")
WEEKLY_META = os.path.join(CACHE_DIR, "cache_weekly_meta.txt")
MONTHLY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_monthly.pkl")
MONTHLY_META = os.path.join(CACHE_DIR, "cache_monthly_meta.txt")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
MAX_WORKERS = 20
LOOKBACK = 300  # bars per ticker (daily matrix)
LOOKBACK_5YR = 1260  # ~5 years of trading days
HTF_BATCH_SIZE = 50  # tickers per batch for HTF full build (rate limit safety)
HTF_BATCH_SLEEP = 1.0  # seconds between batches


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

def _yf_download(ticker, period="5y", interval="1d"):
    """Download OHLCV for one ticker from yfinance.

    Args:
        ticker: stock symbol
        period: yfinance period string ('5y', '1y', '1mo', etc.)
        interval: '1d', '1wk', or '1mo'

    Returns (ticker, DataFrame) or (ticker, None) on failure.
    DataFrame has columns: date, open, high, low, close, volume
    """
    try:
        raw = yf.download(ticker, period=period, interval=interval, progress=False)
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

        min_bars = 3 if interval in ("1wk", "1mo") else 5
        if len(df) < min_bars:
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
        futures = {pool.submit(_yf_download, t, "1y"): t for t in tickers}
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
        futures = {pool.submit(_yf_download, t, "5y"): t for t in tickers}
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
            futures = {pool.submit(_yf_download, t, "5y"): t for t in to_fetch_full}
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
# HTF CACHES — Weekly + Monthly OHLCV from yfinance
# ══════════════════════════════════════════════════════════════

def _merge_htf_bars(existing_df, new_df):
    """Merge new HTF bars into existing cache.

    Rules:
    - If new bar's date matches last cached bar: overwrite (partial period updated)
    - If new bar's date is after last cached bar: append (new period started)
    - Bars before last cached bar: ignore (frozen history)

    Returns updated DataFrame.
    """
    if existing_df is None or len(existing_df) == 0:
        return new_df
    if new_df is None or len(new_df) == 0:
        return existing_df

    last_cached_date = existing_df["date"].iloc[-1]
    rows_to_append = []

    for _, row in new_df.iterrows():
        row_date = row["date"]

        if row_date == last_cached_date:
            # Overwrite last bar (partial period — close/high/low may have changed)
            for col in ["open", "high", "low", "close", "volume"]:
                existing_df.at[existing_df.index[-1], col] = row[col]
        elif row_date > last_cached_date:
            # New period — collect for append
            rows_to_append.append(row)
            last_cached_date = row_date
        # else: row_date < last_cached_date → frozen, skip

    if rows_to_append:
        append_df = pd.DataFrame(rows_to_append)
        existing_df = pd.concat([existing_df, append_df], ignore_index=True)
        existing_df = existing_df.sort_values("date").reset_index(drop=True)

    return existing_df


def _build_htf_cache(interval, output_file, meta_file, label):
    """Full build for one HTF timeframe.

    Downloads 5yr of data for all tickers in batches to avoid rate limiting.
    Ticker list comes from existing 5yr daily cache keys.
    """
    print(f"\n  {'=' * 50}")
    print(f"  {label} OHLCV CACHE — Full Build")
    print(f"  {'=' * 50}")

    if not os.path.exists(CACHE_5YR_FILE):
        raise FileNotFoundError(
            f"Daily 5yr cache not found: {CACHE_5YR_FILE}\n"
            "  Run cache_builder.py --5yr first."
        )
    with open(CACHE_5YR_FILE, "rb") as f:
        daily_cache = pickle.load(f)
    tickers = list(daily_cache.keys())
    del daily_cache
    print(f"  {len(tickers)} tickers to fetch")

    t0 = time.time()
    universe = {}
    failed = []
    n_batches = (len(tickers) + HTF_BATCH_SIZE - 1) // HTF_BATCH_SIZE

    for batch_idx in range(n_batches):
        batch_start = batch_idx * HTF_BATCH_SIZE
        batch_end = min(batch_start + HTF_BATCH_SIZE, len(tickers))
        batch = tickers[batch_start:batch_end]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_yf_download, t, "5y", interval): t
                for t in batch
            }
            for future in as_completed(futures):
                ticker, df = future.result()
                if df is not None:
                    universe[ticker] = df
                else:
                    failed.append(ticker)

        done = batch_end
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(tickers) - done) / rate if rate > 0 else 0
        print(f"    {done:,}/{len(tickers):,} "
              f"({len(universe):,} ok, {len(failed)} failed) "
              f"[{elapsed/60:.1f}m elapsed, ~{eta/60:.1f}m left]")

        # Sleep between batches to avoid rate limiting
        if batch_idx < n_batches - 1:
            time.sleep(HTF_BATCH_SLEEP)

    elapsed = time.time() - t0

    # Save
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(output_file, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(meta_file, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(output_file) / 1024 / 1024
    print(f"\n  {label} build complete:")
    print(f"    Tickers: {len(universe):,} ok, {len(failed)} failed")
    print(f"    File: {output_file} ({size_mb:.1f} MB)")
    print(f"    Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")

    if failed and len(failed) <= 20:
        print(f"    Failed: {', '.join(failed[:20])}")

    return universe


def _append_htf_cache(interval, output_file, meta_file, label, recent_period):
    """Nightly append for one HTF timeframe.

    For each ticker in the existing cache:
    - Fetch recent bars from yfinance
    - Merge: overwrite partial period, append new closed periods

    New tickers (in 5yr daily but not in HTF cache) get full 5yr fetch.
    """
    print(f"\n  {label} OHLCV Cache — Append")

    if not os.path.exists(output_file):
        print(f"  No existing {label.lower()} cache. Running full build...")
        return _build_htf_cache(interval, output_file, meta_file, label)

    # Load existing
    with open(output_file, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe)} tickers in cache")

    # Check for new tickers from 5yr daily cache
    try:
        if os.path.exists(CACHE_5YR_FILE):
            with open(CACHE_5YR_FILE, "rb") as f:
                daily_cache = pickle.load(f)
            all_tickers = list(daily_cache.keys())
            del daily_cache
            new_tickers = [t for t in all_tickers if t not in universe]
        else:
            new_tickers = []
    except Exception:
        new_tickers = []

    to_update = list(universe.keys())

    print(f"  Tickers to update: {len(to_update)}")
    if new_tickers:
        print(f"  New tickers (full fetch): {len(new_tickers)}")

    t0 = time.time()
    updated = 0
    no_change = 0
    failed = 0

    # Update existing tickers — fetch recent bars
    if to_update:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_yf_download, t, recent_period, interval): t
                for t in to_update
            }
            done = 0
            for future in as_completed(futures):
                ticker = futures[future]
                done += 1
                try:
                    _, new_df = future.result()
                    if new_df is not None and len(new_df) > 0:
                        old_len = len(universe[ticker])
                        old_last = str(universe[ticker]["date"].iloc[-1])[:10]
                        universe[ticker] = _merge_htf_bars(universe[ticker], new_df)
                        new_len = len(universe[ticker])
                        new_last = str(universe[ticker]["date"].iloc[-1])[:10]
                        if new_len != old_len or new_last != old_last:
                            updated += 1
                        else:
                            no_change += 1
                    else:
                        no_change += 1
                except Exception:
                    failed += 1

                if done % 500 == 0 or done == len(to_update):
                    elapsed = time.time() - t0
                    print(f"    {done:,}/{len(to_update):,} checked "
                          f"({updated} updated, {no_change} current, {failed} failed) "
                          f"[{elapsed:.0f}s]")

    # Full fetch for new tickers
    new_added = 0
    if new_tickers:
        print(f"\n  Fetching {len(new_tickers)} new tickers...")
        for batch_start in range(0, len(new_tickers), HTF_BATCH_SIZE):
            batch = new_tickers[batch_start:batch_start + HTF_BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(_yf_download, t, "5y", interval): t
                    for t in batch
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        _, df = future.result()
                        if df is not None and len(df) >= 3:
                            universe[ticker] = df
                            new_added += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
            if batch_start + HTF_BATCH_SIZE < len(new_tickers):
                time.sleep(HTF_BATCH_SLEEP)

    elapsed = time.time() - t0

    # Save
    with open(output_file, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(meta_file, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(output_file) / 1024 / 1024
    print(f"  {label} append: {updated} updated, {new_added} new, "
          f"{no_change} unchanged, {failed} failed ({elapsed:.0f}s, {size_mb:.1f} MB)")

    return universe


def build_htf_caches():
    """Full build of both weekly and monthly OHLCV caches."""
    print("\n" + "=" * 60)
    print("  HTF CACHE BUILDER — Weekly + Monthly OHLCV from yfinance")
    print("=" * 60)

    _build_htf_cache("1wk", WEEKLY_FILE, WEEKLY_META, "WEEKLY")
    _build_htf_cache("1mo", MONTHLY_FILE, MONTHLY_META, "MONTHLY")


def append_weekly():
    """Nightly append for weekly cache."""
    return _append_htf_cache(
        interval="1wk",
        output_file=WEEKLY_FILE,
        meta_file=WEEKLY_META,
        label="Weekly",
        recent_period="1mo"  # last month covers current + recently closed weeks
    )


def append_monthly():
    """Nightly append for monthly cache."""
    return _append_htf_cache(
        interval="1mo",
        output_file=MONTHLY_FILE,
        meta_file=MONTHLY_META,
        label="Monthly",
        recent_period="3mo"  # last 3 months covers current + recently closed months
    )


def append_htf_caches():
    """Nightly append for both weekly and monthly caches."""
    append_weekly()
    append_monthly()


def htf_status():
    """Show HTF cache status."""
    print("\n  HTF OHLCV Cache Status")
    print("  " + "─" * 40)

    for label, pkl_file, meta_file in [
        ("Weekly", WEEKLY_FILE, WEEKLY_META),
        ("Monthly", MONTHLY_FILE, MONTHLY_META),
    ]:
        if not os.path.exists(pkl_file):
            print(f"\n  {label}: not built")
            continue

        with open(pkl_file, "rb") as f:
            data = pickle.load(f)

        size_mb = os.path.getsize(pkl_file) / 1024 / 1024
        bar_counts = [len(df) for df in data.values()]

        meta_ts = "unknown"
        if os.path.exists(meta_file):
            with open(meta_file) as f:
                meta_ts = f.read().strip()

        print(f"\n  {label}:")
        print(f"    Tickers: {len(data):,}")
        print(f"    File: {size_mb:.1f} MB")
        print(f"    Updated: {meta_ts}")
        if bar_counts:
            print(f"    Bars: min={min(bar_counts)}, "
                  f"avg={sum(bar_counts)/len(bar_counts):.0f}, "
                  f"max={max(bar_counts)}")

        # Sample last dates
        for t in ["SPY", "AAPL", "MSFT"]:
            if t in data and len(data[t]) > 0:
                last = str(data[t]["date"].iloc[-1])[:10]
                print(f"    {t} last bar: {last} ({len(data[t])} bars)")

        del data


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
    mode_htf = "--htf" in sys.argv
    mode_htf_status = "--htf-status" in sys.argv

    if mode_htf_status:
        htf_status()
    elif mode_htf:
        build_htf_caches()
    elif mode_5yr:
        data = build_5yr_cache(force=force)
        print(f"5yr cache ready: {len(data)} tickers")
    else:
        data = build_cache(force=force)
        print(f"Cache ready: {len(data)} tickers")
