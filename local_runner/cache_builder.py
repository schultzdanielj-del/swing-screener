"""
Cache Builder — Pull all tradable universe OHLCV data and store locally.

Usage:
    python local_runner/cache_builder.py [--force]
    python local_runner/cache_builder.py --daily [--force]
    python local_runner/cache_builder.py --htf [--force]
    python local_runner/cache_builder.py --all [--force]    # daily + weekly + monthly

Stores:
  - local_runner/cache/universe_ohlcv.pkl — 300-bar daily cache (legacy)
  - local_runner/cache/universe_ohlcv_daily.pkl — full daily cache (10yr from HISTORY_START)
  - local_runner/cache/universe_ohlcv_weekly.pkl — weekly cache (from HISTORY_START)
  - local_runner/cache/universe_ohlcv_monthly.pkl — monthly cache (from HISTORY_START)

All data pulled from yfinance using explicit start date (not period parameter).
No Railway dependency.

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
CACHE_DAILY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
CACHE_DAILY_META = os.path.join(CACHE_DIR, "cache_daily_meta.txt")
# Legacy path — checked as fallback when loading
CACHE_LEGACY_5YR = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
WEEKLY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_weekly.pkl")
WEEKLY_META = os.path.join(CACHE_DIR, "cache_weekly_meta.txt")
MONTHLY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_monthly.pkl")
MONTHLY_META = os.path.join(CACHE_DIR, "cache_monthly_meta.txt")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
MAX_WORKERS = 20
LOOKBACK = 300  # bars per ticker (daily matrix)
HISTORY_START = "2016-01-01"  # explicit start date for all caches (daily + HTF)
HTF_BATCH_SIZE = 50  # tickers per batch for HTF full build (rate limit safety)
HTF_BATCH_SLEEP = 2.0  # seconds between batches


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

def _yf_download(ticker, start=None, interval="1d"):
    """Download OHLCV for one ticker from yfinance.

    Uses explicit start date instead of period parameter to avoid
    silent truncation from Yahoo's unreliable period interpretation.

    Args:
        ticker: stock symbol
        start: start date string 'YYYY-MM-DD' (defaults to HISTORY_START)
        interval: '1d', '1wk', or '1mo'

    Returns (ticker, DataFrame) or (ticker, None) on failure.
    DataFrame has columns: date, open, high, low, close, volume
    """
    if start is None:
        start = HISTORY_START
    try:
        raw = yf.download(ticker, start=start, interval=interval, progress=False)
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


# ══════════════════════════════════════════════════════════════
# BATCHED FETCH — Adaptive backoff + retry sweeps
# ══════════════════════════════════════════════════════════════

def _batched_fetch(tickers, fetch_fn, label="Fetch", batch_size=50,
                   min_sleep=1.0, max_sleep=30.0, max_retries=3):
    """Fetch data for a list of tickers with adaptive rate limiting and retry.

    Args:
        tickers: list of ticker symbols to fetch
        fetch_fn: callable(ticker) → (ticker, result_or_None)
        label: display label for progress
        batch_size: tickers per batch
        min_sleep: minimum sleep between batches (seconds)
        max_sleep: maximum sleep between batches (seconds)
        max_retries: how many full retry sweeps on failed tickers

    Returns:
        results: dict {ticker: result} for successful fetches
        permanently_failed: list of tickers that failed all retries
    """
    if not tickers:
        return {}, []

    results = {}
    remaining = list(tickers)
    sleep_time = min_sleep
    consecutive_clean = 0  # batches with 0 failures in a row

    for attempt in range(1 + max_retries):
        if attempt > 0:
            print(f"\n  {label} retry {attempt}/{max_retries} — "
                  f"{len(remaining)} tickers to retry...")

        batch_failed = []
        t0 = time.time()
        n_batches = (len(remaining) + batch_size - 1) // batch_size

        for batch_idx in range(n_batches):
            b_start = batch_idx * batch_size
            b_end = min(b_start + batch_size, len(remaining))
            batch = remaining[b_start:b_end]

            batch_ok = 0
            batch_fail = 0
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(fetch_fn, t): t for t in batch}
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        _, result = future.result()
                        if result is not None:
                            results[ticker] = result
                            batch_ok += 1
                        else:
                            batch_failed.append(ticker)
                            batch_fail += 1
                    except Exception:
                        batch_failed.append(ticker)
                        batch_fail += 1

            # Adaptive backoff
            fail_rate = batch_fail / len(batch) if batch else 0
            if fail_rate > 0.1:
                sleep_time = min(sleep_time * 2, max_sleep)
                consecutive_clean = 0
            else:
                consecutive_clean += 1
                if consecutive_clean >= 3 and sleep_time > min_sleep:
                    sleep_time = max(sleep_time / 2, min_sleep)

            done = b_end
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(remaining) - done) / rate if rate > 0 else 0
            print(f"    {label}: {done:,}/{len(remaining):,} "
                  f"({len(results):,} ok, {len(batch_failed)} failed, "
                  f"sleep={sleep_time:.0f}s) "
                  f"[{elapsed/60:.1f}m elapsed, ~{eta/60:.1f}m left]")

            if batch_idx < n_batches - 1:
                time.sleep(sleep_time)

        # Check if retry made progress
        if not batch_failed:
            break  # all succeeded
        if attempt > 0 and len(batch_failed) >= len(remaining):
            break  # retry made zero progress — stop

        remaining = batch_failed

    return results, remaining

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

    print(f"\nFetching OHLCV data via yfinance...")
    t0 = time.time()

    from datetime import date
    one_yr_ago = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")

    results, permanently_failed = _batched_fetch(
        tickers,
        fetch_fn=lambda t: _yf_download(t, one_yr_ago),
        label="Legacy",
    )

    universe = {}
    for ticker, df in results.items():
        compute_dvol_20d(df)
        universe[ticker] = df

    elapsed = time.time() - t0
    print(f"\nFetch complete: {len(universe):,} tickers in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    if permanently_failed:
        print(f"  Permanently failed: {len(permanently_failed)} tickers")

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

def cache_daily_is_fresh():
    """Check if daily cache exists and is less than 7 days old."""
    if not os.path.exists(CACHE_DAILY_FILE) or not os.path.exists(CACHE_DAILY_META):
        return False
    try:
        with open(CACHE_DAILY_META) as f:
            ts = datetime.fromisoformat(f.read().strip())
        return datetime.now() - ts < timedelta(days=7)
    except:
        return False


def build_daily_cache(force=False):
    """Build the daily OHLCV cache from yfinance using HISTORY_START."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force and cache_daily_is_fresh():
        with open(CACHE_DAILY_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"Daily cache is fresh ({len(data)} tickers). Use --force to rebuild.")
        return data

    print("=" * 60)
    print("  DAILY CACHE BUILDER — Full history via yfinance")
    print("=" * 60)

    # Get ticker list from existing cache (has all ~10,856 tickers).
    # Only fall back to SQLite tradable_universe if no cache exists at all.
    print("\nGetting ticker list...")
    tickers = []
    for pkl in [CACHE_DAILY_FILE, CACHE_LEGACY_5YR]:
        if os.path.exists(pkl):
            with open(pkl, "rb") as f:
                existing = pickle.load(f)
            tickers = sorted(existing.keys())
            del existing
            print(f"  {len(tickers)} tickers from existing cache")
            break
    if not tickers:
        try:
            tickers = get_tradable_tickers_local()
            print(f"  {len(tickers)} tickers from local DB")
        except FileNotFoundError:
            print("  ERROR: No existing cache and no local DB. Nothing to build from.")
            return {}
    print(f"  Start date: {HISTORY_START}")

    print(f"\nFetching daily OHLCV data via yfinance...")
    t0 = time.time()

    results, permanently_failed = _batched_fetch(
        tickers,
        fetch_fn=lambda t: _yf_download(t, HISTORY_START),
        label="Daily",
    )

    universe = {}
    for ticker, df in results.items():
        compute_dvol_20d(df)
        universe[ticker] = df

    elapsed = time.time() - t0
    print(f"\nFetch complete: {len(universe):,} tickers in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    if permanently_failed:
        print(f"  Permanently failed: {len(permanently_failed)} tickers")

    print(f"\nSaving daily cache...")
    with open(CACHE_DAILY_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    with open(CACHE_DAILY_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_DAILY_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_DAILY_FILE} ({size_mb:.1f} MB)")

    total_rows = sum(len(df) for df in universe.values())
    avg_bars = total_rows / len(universe) if universe else 0
    bar_counts = [len(df) for df in universe.values()]
    print(f"  Total rows: {total_rows:,}")
    print(f"  Avg bars/ticker: {avg_bars:.0f}")
    if bar_counts:
        print(f"  Min/Max bars: {min(bar_counts)}/{max(bar_counts)}")
    print(f"  File size: {size_mb:.0f} MB")
    print()

    return universe


def load_daily_cache():
    """Load the daily cache, building if needed. Checks legacy filename as fallback."""
    if os.path.exists(CACHE_DAILY_FILE):
        with open(CACHE_DAILY_FILE, "rb") as f:
            return pickle.load(f)
    if os.path.exists(CACHE_LEGACY_5YR):
        with open(CACHE_LEGACY_5YR, "rb") as f:
            return pickle.load(f)
    return build_daily_cache()


def append_daily_cache():
    """Append new bars to existing daily cache via yfinance. Never touches old bars.

    For each ticker already in the cache, fetches only bars after the last
    cached date from yfinance and appends them. New tickers (in tradable
    universe but not in cache) get a full fetch from HISTORY_START.
    Old bars are never modified.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Check both new and legacy filenames
    cache_file = CACHE_DAILY_FILE
    if not os.path.exists(cache_file):
        if os.path.exists(CACHE_LEGACY_5YR):
            cache_file = CACHE_LEGACY_5YR
        else:
            print("  No existing daily cache. Running full build...")
            return build_daily_cache()

    # Load existing cache
    print("  Loading existing daily cache...")
    with open(cache_file, "rb") as f:
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
        print(f"\n  Appending new bars via yfinance...")

        def _append_one(ticker):
            return _yf_append_after_date(ticker, last_dates.get(ticker, "2020-01-01"))

        append_results, append_failed = _batched_fetch(
            to_append, fetch_fn=_append_one, label="Append",
        )
        failed += len(append_failed)

        for ticker, new_df in append_results.items():
            if len(new_df) > 0:
                existing = universe[ticker]
                combined = pd.concat([existing, new_df], ignore_index=True)
                combined = combined.sort_values("date").reset_index(drop=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last")
                combined = combined.reset_index(drop=True)
                compute_dvol_20d(combined)
                universe[ticker] = combined
                appended += 1
            else:
                no_new += 1

    # Full fetch for new tickers
    if to_fetch_full:
        print(f"\n  Fetching {len(to_fetch_full)} new tickers via yfinance...")

        new_results, new_failed = _batched_fetch(
            to_fetch_full,
            fetch_fn=lambda t: _yf_download(t, HISTORY_START),
            label="New tickers",
        )
        failed += len(new_failed)

        for ticker, df in new_results.items():
            if len(df) >= 50:
                compute_dvol_20d(df)
                universe[ticker] = df
                new_added += 1

    elapsed = time.time() - t0

    # Save (always to new filename)
    print(f"\n  Saving daily cache...")
    with open(CACHE_DAILY_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    with open(CACHE_DAILY_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_DAILY_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_DAILY_FILE} ({size_mb:.1f} MB)")
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


def _sync_htf_cache(interval, output_file, meta_file, label, recent_start,
                    full_sweep=False):
    """Unified HTF cache sync.

    full_sweep=False (nightly): only update existing tickers with recent bars.
    full_sweep=True  (--htf CLI): also fetch missing tickers (from HISTORY_START).

    Args:
        recent_start: explicit start date for stale ticker updates (e.g. 30 days ago)
    """
    mode = "Full Sweep" if full_sweep else "Append"
    print(f"\n  {label} OHLCV Cache — {mode}")

    # Find daily cache (new or legacy filename)
    daily_file = CACHE_DAILY_FILE
    if not os.path.exists(daily_file):
        daily_file = CACHE_LEGACY_5YR
    if not os.path.exists(daily_file):
        raise FileNotFoundError(
            f"Daily cache not found. Run cache_builder.py --daily first."
        )

    # Load existing HTF cache (empty dict if first build)
    universe = {}
    if os.path.exists(output_file):
        with open(output_file, "rb") as f:
            universe = pickle.load(f)
        print(f"  Existing cache: {len(universe)} tickers")
    else:
        if not full_sweep:
            print(f"  No existing cache. Run --htf first.")
            return {}
        print(f"  No existing cache — building from scratch")

    # Get full ticker list from daily cache
    with open(daily_file, "rb") as f:
        daily_cache = pickle.load(f)
    all_tickers = list(daily_cache.keys())
    del daily_cache

    # Classify tickers
    # Only update existing tickers that are stale (last date < reference date)
    to_update = []
    skipped = 0
    if universe:
        ref_ticker = "SPY" if "SPY" in universe else next(iter(universe))
        ref_last = str(universe[ref_ticker]["date"].iloc[-1])[:10]
        for t in all_tickers:
            if t not in universe:
                continue
            t_last = str(universe[t]["date"].iloc[-1])[:10]
            if t_last < ref_last:
                to_update.append(t)
            else:
                skipped += 1

    to_fetch = [t for t in all_tickers if t not in universe] if full_sweep else []

    print(f"  Total tickers: {len(all_tickers)}")
    print(f"  Already current: {skipped}")
    print(f"  To update (stale): {len(to_update)}")
    if full_sweep:
        print(f"  To fetch (missing): {len(to_fetch)}")

    t0 = time.time()
    updated = 0
    no_change = 0
    new_added = 0
    failed = 0

    # Update stale existing tickers — fetch recent bars using explicit start date
    if to_update:
        def _update_one(ticker):
            return _yf_download(ticker, recent_start, interval)

        update_results, update_failed = _batched_fetch(
            to_update, fetch_fn=_update_one, label=f"{label} update",
        )
        failed += len(update_failed)

        for ticker, new_df in update_results.items():
            if len(new_df) > 0:
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

    # Full fetch for missing tickers (only in full_sweep mode)
    if to_fetch:
        print(f"\n  Fetching {len(to_fetch)} missing tickers (start={HISTORY_START})...")

        def _fetch_htf(ticker):
            return _yf_download(ticker, HISTORY_START, interval)

        fetch_results, fetch_failed_list = _batched_fetch(
            to_fetch, fetch_fn=_fetch_htf, label=f"{label} fetch",
        )
        failed += len(fetch_failed_list)

        for ticker, df in fetch_results.items():
            if len(df) >= 3:
                universe[ticker] = df
                new_added += 1

    elapsed = time.time() - t0

    # Save
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(output_file, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(meta_file, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(output_file) / 1024 / 1024
    print(f"\n  {label} sync complete:")
    print(f"    Total in cache: {len(universe):,}")
    print(f"    Updated: {updated}, New: {new_added}, "
          f"Unchanged: {no_change}, Failed: {failed}")
    print(f"    File: {output_file} ({size_mb:.1f} MB)")
    print(f"    Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")

    return universe


def build_htf_caches():
    """Full sweep of both weekly and monthly OHLCV caches."""
    print("\n" + "=" * 60)
    print("  HTF CACHE — Weekly + Monthly OHLCV from yfinance")
    print("=" * 60)

    # For stale updates during full sweep, fetch last 60 days
    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    _sync_htf_cache("1wk", WEEKLY_FILE, WEEKLY_META, "WEEKLY", recent,
                    full_sweep=True)
    _sync_htf_cache("1mo", MONTHLY_FILE, MONTHLY_META, "MONTHLY", recent,
                    full_sweep=True)


def append_weekly():
    """Nightly append for weekly cache."""
    # Fetch last 60 days to catch any missed bars
    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    return _sync_htf_cache(
        interval="1wk",
        output_file=WEEKLY_FILE,
        meta_file=WEEKLY_META,
        label="Weekly",
        recent_start=recent,
        full_sweep=False,
    )


def append_monthly():
    """Nightly append for monthly cache."""
    # Fetch last 120 days to catch any missed bars
    recent = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    return _sync_htf_cache(
        interval="1mo",
        output_file=MONTHLY_FILE,
        meta_file=MONTHLY_META,
        label="Monthly",
        recent_start=recent,
        full_sweep=False,
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
    # Check both new and legacy filenames
    cache_file = CACHE_DAILY_FILE
    if not os.path.exists(cache_file):
        cache_file = CACHE_LEGACY_5YR
    if not os.path.exists(cache_file):
        print("  No daily cache exists — new data available by default")
        return True

    # Get last cached date from SPY (or first ticker in cache)
    with open(cache_file, "rb") as f:
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
        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        raw = yf.download("SPY", start=recent, interval="1d", progress=False)
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
    mode_all = "--all" in sys.argv
    mode_daily = "--daily" in sys.argv or "--5yr" in sys.argv  # accept legacy flag
    mode_htf = "--htf" in sys.argv
    mode_htf_status = "--htf-status" in sys.argv

    if mode_htf_status:
        htf_status()
    elif mode_all:
        data = build_daily_cache(force=force)
        print(f"Daily cache ready: {len(data)} tickers")
        del data  # free memory before HTF
        import gc; gc.collect()
        build_htf_caches()
    elif mode_htf:
        build_htf_caches()
    elif mode_daily:
        data = build_daily_cache(force=force)
        print(f"Daily cache ready: {len(data)} tickers")
    else:
        data = build_cache(force=force)
        print(f"Cache ready: {len(data)} tickers")
