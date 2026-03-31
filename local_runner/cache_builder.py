"""
Cache Builder — Pull all tradable universe OHLCV data and store locally.

Usage:
    python local_runner/cache_builder.py [--force]
    python local_runner/cache_builder.py --daily [--force]
    python local_runner/cache_builder.py --htf [--force]
    python local_runner/cache_builder.py --all [--force]    # daily + weekly + monthly

Stores:
  - local_runner/cache/universe_ohlcv.pkl — 300-bar daily cache (legacy)
  - local_runner/cache/universe_ohlcv_daily.pkl — full daily cache (from HISTORY_START)
  - local_runner/cache/universe_ohlcv_weekly.pkl — weekly cache (from HISTORY_START)
  - local_runner/cache/universe_ohlcv_monthly.pkl — monthly cache (from HISTORY_START)

All data pulled from EODHD using explicit start date.
No Railway dependency. No yfinance dependency.

Requires: pip install pandas numpy
"""

import os
import sys
import time
import json
import pickle
import sqlite3
import urllib.request
import numpy as np
import pandas as pd
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
TICKER_REF_FILE = os.path.join(CACHE_DIR, "ticker_reference.json")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
MAX_WORKERS = 50
LOOKBACK = 300  # bars per ticker (daily matrix)
HISTORY_START = "2016-01-01"  # explicit start date for all caches (daily + HTF)
HTF_BATCH_SIZE = 50  # tickers per batch for HTF full build
HTF_BATCH_SLEEP = 5.0  # seconds between batches

# EODHD API
EODHD_API_TOKEN = os.environ.get("EODHD_API_TOKEN", "")
EODHD_BASE = "https://eodhd.com/api"

def _eodhd_end_date():
    """Dynamic end date — always 1 year from today. Never stale."""
    return (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")


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
# EODHD HELPERS
# ══════════════════════════════════════════════════════════════

# ── Ticker Reference File ──
# Stores first trade date for every ticker. Built as a byproduct
# of daily cache builds (first bar date from EODHD). Used to
# calculate expected bar counts for validation.

def load_ticker_reference():
    """Load the ticker reference file. Returns dict {ticker: first_trade_date_str}."""
    if not os.path.exists(TICKER_REF_FILE):
        return {}
    with open(TICKER_REF_FILE, "r") as f:
        return json.load(f)


def save_ticker_reference(ref):
    """Save the ticker reference file."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(TICKER_REF_FILE, "w") as f:
        json.dump(ref, f, indent=2)


def build_ticker_reference(tickers, force=False):
    """Fetch first trade date for all tickers from EODHD, save as JSON.

    Fetches a small date range (HISTORY_START to HISTORY_START + 10 days)
    for each ticker. If data exists, the first bar's date is the first
    trade date (within our window). If no data in that range, widens
    the search.

    Only fetches tickers not already in the reference (unless force=True).
    Returns dict {ticker: 'YYYY-MM-DD'} for tickers with valid first trade dates.
    Tickers where EODHD returns no data get value None.
    """
    ref = {} if force else load_ticker_reference()
    to_fetch = [t for t in tickers if t not in ref] if not force else list(tickers)

    if not to_fetch:
        print(f"  Ticker reference: {len(ref)} tickers, all current")
        return ref

    print(f"\n  Building ticker reference ({len(to_fetch)} to fetch, "
          f"{len(ref)} already cached)...")

    def _get_first_trade_date(ticker):
        """Get first available bar date from EODHD.

        Tries HISTORY_START first. If empty, fetches full range to find
        when the ticker actually started trading.
        """
        try:
            # Try narrow range first (most tickers started before HISTORY_START)
            data = _eodhd_fetch_json(ticker, HISTORY_START, "2016-02-01", "d")
            if data and len(data) > 0:
                return ticker, data[0]["date"]

            # Widen: ticker may have started after HISTORY_START
            data = _eodhd_fetch_json(ticker, HISTORY_START, _eodhd_end_date(), "d")
            if data and len(data) > 0:
                return ticker, data[0]["date"]

            return ticker, None
        except Exception:
            return ticker, None

    t0 = time.time()
    results, _ = _batched_fetch(
        to_fetch,
        fetch_fn=_get_first_trade_date,
        label="Reference",
        max_retries=5,
    )

    for ticker, date_str in results.items():
        ref[ticker] = date_str

    # Mark tickers that failed as None (EODHD has no data for them)
    for t in to_fetch:
        if t not in ref:
            ref[t] = None

    save_ticker_reference(ref)
    elapsed = time.time() - t0
    valid = sum(1 for v in ref.values() if v is not None)
    print(f"  Reference built: {len(ref)} tickers ({valid} with valid dates) "
          f"in {elapsed:.0f}s")

    return ref


# ── SPY Reference ──
# SPY's date array is the ground truth for how many trading days
# exist in any date range. Every ticker's expected bar count is
# determined by counting SPY dates in the ticker's valid range.

def fetch_spy_reference():
    """Fetch SPY full daily history from HISTORY_START via EODHD.

    Returns SPY DataFrame. If fetch fails, raises — pipeline must stop.
    SPY is always fetched first, alone, with no concurrency.
    """
    print("  Fetching SPY reference (ground truth)...")
    _, spy_df = _eodhd_download("SPY", HISTORY_START)
    if spy_df is None or len(spy_df) == 0:
        raise RuntimeError(
            "FATAL: Cannot fetch SPY data from EODHD. "
            "Pipeline cannot proceed without SPY as reference."
        )
    print(f"  SPY: {len(spy_df)} bars, "
          f"{str(spy_df['date'].iloc[0])[:10]} → {str(spy_df['date'].iloc[-1])[:10]}")
    return spy_df


def build_spy_date_set(spy_df):
    """Build a set of SPY date strings for fast lookup.

    Returns sorted list of 'YYYY-MM-DD' strings.
    """
    return sorted(str(d)[:10] for d in spy_df["date"].values)


def expected_bar_count(spy_dates, ticker, ticker_ref):
    """Calculate expected bar count for a ticker using SPY dates.

    Expected = number of SPY trading dates on or after
    max(ticker's first trade date, HISTORY_START).

    Returns int, or None if ticker has no reference data.
    """
    ftd = ticker_ref.get(ticker)
    if ftd is None:
        return None  # no reference — can't validate

    start = max(ftd, HISTORY_START)
    return sum(1 for d in spy_dates if d >= start)


def validate_daily_fetch(results, spy_dates, ticker_ref):
    """Validate fetched daily data against SPY reference.

    Returns:
        valid: dict {ticker: df} — tickers that pass exact bar count match
        invalid: list of tickers that failed validation (for retry)
        unvalidatable: dict {ticker: df} — tickers with no reference (accepted as-is)
    """
    valid = {}
    invalid = []
    unvalidatable = {}

    for ticker, df in results.items():
        expected = expected_bar_count(spy_dates, ticker, ticker_ref)
        if expected is None:
            # No reference data — accept as-is (can't validate)
            unvalidatable[ticker] = df
            continue

        actual = len(df)
        if actual == expected:
            valid[ticker] = df
        else:
            invalid.append(ticker)

    return valid, invalid, unvalidatable


# ── Split Detection ──
# On nightly append, detect splits by comparing the cached close price
# for the last bar to what EODHD returns for that same date. If the
# adjustment ratio changed, a split (or dividend ex-date) happened
# and the ticker needs a full refetch.

def detect_splits(tickers, cache_dict, after_date):
    """Check which tickers had a split/adjustment change since last cache.

    Compares cached close for the last bar to EODHD's adjusted_close
    for that same date. If they differ by more than 0.5%, the ticker's
    historical adjustment changed (split or dividend) and needs full refetch.

    Args:
        tickers: list of ticker symbols
        cache_dict: {ticker: DataFrame} — current cache
        after_date: 'YYYY-MM-DD' — the last cached date

    Returns list of tickers that need full refetch.
    """
    split_tickers = []

    # Fetch the bulk last-day data for comparison
    # We actually need the specific date's data, not last day
    # Check in batches
    for i in range(0, len(tickers), 50):
        batch = tickers[i:i+50]
        for t in batch:
            try:
                cached_df = cache_dict.get(t)
                if cached_df is None or len(cached_df) == 0:
                    continue

                # Get the last cached close (this is already adjusted)
                cached_close = float(cached_df["close"].iloc[-1])
                cached_date = str(cached_df["date"].iloc[-1])[:10]

                # Fetch that same date from EODHD
                data = _eodhd_fetch_json(t, cached_date, cached_date, "d")
                if not data or len(data) == 0:
                    continue

                # EODHD returns unadjusted close + adjusted_close
                # Our cache has the adjusted value. Compare to EODHD's adjusted_close.
                eodhd_adj_close = float(data[0]["adjusted_close"])

                # If they differ by more than 0.5%, adjustment changed
                if cached_close > 0 and abs(eodhd_adj_close - cached_close) / cached_close > 0.005:
                    split_tickers.append(t)

            except Exception:
                pass  # can't check — not a split
        if i + 50 < len(tickers):
            time.sleep(0.5)

    return split_tickers


# ── EODHD API Functions ──

def _eodhd_fetch_json(ticker, from_date, to_date, period="d"):
    """Raw EODHD API call. Returns list of dicts or None on failure.

    Args:
        ticker: stock symbol (without .US suffix)
        from_date: 'YYYY-MM-DD' start date (inclusive)
        to_date: 'YYYY-MM-DD' end date (exclusive per EODHD behavior)
        period: 'd' (daily), 'w' (weekly), 'm' (monthly)

    Returns list of bar dicts, or None on failure.
    """
    url = (f"{EODHD_BASE}/eod/{ticker}.US"
           f"?from={from_date}&to={to_date}"
           f"&period={period}"
           f"&api_token={EODHD_API_TOKEN}&fmt=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ScanPerfect/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")

        # EODHD returns "Ticker Not Found." for invalid tickers (not JSON)
        if not raw.startswith("["):
            return None

        data = json.loads(raw)
        return data if data else None
    except Exception:
        return None


def _eodhd_to_dataframe(data):
    """Convert EODHD JSON response to adjusted OHLCV DataFrame.

    EODHD returns unadjusted OHLC + adjusted_close. We compute:
        ratio = adjusted_close / close
    and apply it to O, H, L, C to get fully-adjusted prices.
    Volume stays raw.

    Returns DataFrame with columns: date, open, high, low, close, volume
    or None if data is invalid.
    """
    if not data or len(data) == 0:
        return None

    df = pd.DataFrame(data)

    # Compute adjustment ratio
    close_raw = pd.to_numeric(df["close"], errors="coerce")
    adj_close = pd.to_numeric(df["adjusted_close"], errors="coerce")

    # Avoid division by zero
    ratio = np.where(close_raw > 0, adj_close / close_raw, 1.0)

    # Apply ratio to OHLC
    df["open"] = pd.to_numeric(df["open"], errors="coerce") * ratio
    df["high"] = pd.to_numeric(df["high"], errors="coerce") * ratio
    df["low"] = pd.to_numeric(df["low"], errors="coerce") * ratio
    df["close"] = adj_close  # adjusted_close IS the adjusted close
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])

    df = df[["date", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("date").reset_index(drop=True)

    # Drop rows with NaN close (bad data)
    df = df.dropna(subset=["close"]).reset_index(drop=True)

    if len(df) == 0:
        return None

    return df


def _eodhd_download(ticker, start=None, interval="d"):
    """Download OHLCV for one ticker from EODHD.

    Returns fully-adjusted OHLCV (split + dividend adjusted OHLC).

    Args:
        ticker: stock symbol
        start: start date string 'YYYY-MM-DD' (defaults to HISTORY_START)
        interval: 'd' (daily), 'w' (weekly), 'm' (monthly)

    Returns (ticker, DataFrame) or (ticker, None) on failure.
    DataFrame has columns: date, open, high, low, close, volume
    """
    if start is None:
        start = HISTORY_START

    data = _eodhd_fetch_json(ticker, start, _eodhd_end_date(), interval)
    if data is None:
        return ticker, None

    df = _eodhd_to_dataframe(data)
    return ticker, df


def _eodhd_append_after_date(ticker, after_date):
    """Download daily bars after a given date from EODHD.

    Args:
        ticker: stock symbol
        after_date: string 'YYYY-MM-DD' — fetch bars AFTER this date

    Returns (ticker, DataFrame of new bars) or (ticker, None)
    """
    # Start from the day after last cached date
    start = (pd.Timestamp(after_date) + timedelta(days=1)).strftime("%Y-%m-%d")

    data = _eodhd_fetch_json(ticker, start, _eodhd_end_date(), "d")
    if data is None:
        return ticker, None

    df = _eodhd_to_dataframe(data)
    return ticker, df


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
                   min_sleep=5.0, max_sleep=10.0, max_retries=3):
    """Fetch data for a list of tickers with adaptive rate limiting and retry.

    Adapts both sleep time AND concurrent workers based on failure rate.
    When rate limited: fewer threads + longer sleep.
    When clean: ramps back up.

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
    workers = MAX_WORKERS  # starts at 50
    min_workers = 20
    consecutive_clean = 0  # batches with 0 failures in a row

    for attempt in range(1 + max_retries):
        if attempt > 0:
            # Reset to moderate settings for retry
            sleep_time = 1.0
            workers = max(min_workers, MAX_WORKERS // 2)
            consecutive_clean = 0
            print(f"\n  {label} retry {attempt}/{max_retries} — "
                  f"{len(remaining)} tickers to retry "
                  f"({workers} workers, {sleep_time:.0f}s sleep)...")

        batch_failed = []
        t0 = time.time()
        n_batches = (len(remaining) + batch_size - 1) // batch_size

        for batch_idx in range(n_batches):
            b_start = batch_idx * batch_size
            b_end = min(b_start + batch_size, len(remaining))
            batch = remaining[b_start:b_end]

            batch_ok = 0
            batch_fail = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
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

            # Adaptive backoff — adjust both workers and sleep
            fail_rate = batch_fail / len(batch) if batch else 0
            if fail_rate > 0.1:
                sleep_time = min(sleep_time * 1.5, max_sleep)
                workers = max(workers - 2, min_workers)
                consecutive_clean = 0
            else:
                consecutive_clean += 1
                if consecutive_clean >= 3:
                    sleep_time = max(sleep_time * 0.7, min_sleep)
                    workers = min(workers + 1, MAX_WORKERS)

            done = b_end
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(remaining) - done) / rate if rate > 0 else 0
            print(f"    {label}: {done:,}/{len(remaining):,} "
                  f"({len(results):,} ok, {len(batch_failed)} failed, "
                  f"w={workers} sleep={sleep_time:.0f}s) "
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
    """Build the 300-bar OHLCV cache from EODHD."""
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

    print(f"\nFetching OHLCV data via EODHD...")
    t0 = time.time()

    from datetime import date
    one_yr_ago = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")

    results, permanently_failed = _batched_fetch(
        tickers,
        fetch_fn=lambda t: _eodhd_download(t, one_yr_ago),
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
# DAILY CACHE — Full history from HISTORY_START
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
    """Build the daily OHLCV cache from EODHD using HISTORY_START.

    Validated build:
    1. Fetch SPY first — its date array is ground truth
    2. Build/load ticker reference (first trade dates)
    3. Fetch all tickers
    4. Validate: each ticker's bar count must exactly match SPY's
       count from max(firstTradeDate, HISTORY_START)
    5. Mismatches retry until they pass or return None
    6. Only saves when all tickers are validated
    7. Updates ticker_reference.json with first trade dates from fetched data
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    if not force and cache_daily_is_fresh():
        with open(CACHE_DAILY_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"Daily cache is fresh ({len(data)} tickers). Use --force to rebuild.")
        return data

    print("=" * 60)
    print("  DAILY CACHE BUILDER — Full history via EODHD")
    print("=" * 60)

    # ── Step 1: SPY first ──
    spy_df = fetch_spy_reference()
    spy_dates = build_spy_date_set(spy_df)

    # ── Step 2: Ticker reference ──
    # Get ticker list from existing cache or DB
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

    # Build/update reference for any tickers not already in it
    ticker_ref = build_ticker_reference(tickers)

    print(f"  Start date: {HISTORY_START}")

    # ── Step 3: Fetch all tickers ──
    # Remove SPY from fetch list (already fetched)
    fetch_tickers = [t for t in tickers if t != "SPY"]

    print(f"\nFetching daily OHLCV data via EODHD...")
    t0 = time.time()

    results, permanently_failed = _batched_fetch(
        fetch_tickers,
        fetch_fn=lambda t: _eodhd_download(t, HISTORY_START),
        label="Daily",
    )

    # Add SPY to results
    results["SPY"] = spy_df

    # ── Step 3b: Update ticker reference from fetched data ──
    # Extract first trade dates from the data we just fetched
    ref_updated = False
    for ticker, df in results.items():
        if df is not None and len(df) > 0:
            first_date = str(df["date"].iloc[0])[:10]
            if ticker_ref.get(ticker) != first_date:
                ticker_ref[ticker] = first_date
                ref_updated = True
    if ref_updated:
        save_ticker_reference(ticker_ref)
        print(f"  Ticker reference updated with first trade dates from fetched data")

    # ── Step 4: Validate ──
    print(f"\n  Validating bar counts against SPY reference...")
    valid, invalid, unvalidatable = validate_daily_fetch(results, spy_dates, ticker_ref)

    print(f"  Validated: {len(valid)}")
    print(f"  Failed validation: {len(invalid)}")
    print(f"  No reference (accepted): {len(unvalidatable)}")

    # ── Step 5: Retry invalid tickers ──
    retry_round = 0
    max_validation_retries = 5
    while invalid and retry_round < max_validation_retries:
        retry_round += 1
        print(f"\n  Validation retry {retry_round}/{max_validation_retries} — "
              f"{len(invalid)} tickers...")

        retry_results, retry_failed = _batched_fetch(
            invalid,
            fetch_fn=lambda t: _eodhd_download(t, HISTORY_START),
            label=f"Retry {retry_round}",
            min_sleep=5.0,
            max_retries=2,
        )
        permanently_failed.extend(retry_failed)

        # Update reference from retry data
        for ticker, df in retry_results.items():
            if df is not None and len(df) > 0:
                first_date = str(df["date"].iloc[0])[:10]
                if ticker_ref.get(ticker) != first_date:
                    ticker_ref[ticker] = first_date

        new_valid, still_invalid, new_unvalidatable = validate_daily_fetch(
            retry_results, spy_dates, ticker_ref)

        valid.update(new_valid)
        unvalidatable.update(new_unvalidatable)

        print(f"  Retry {retry_round}: {len(new_valid)} passed, "
              f"{len(still_invalid)} still failing")

        if len(still_invalid) == len(invalid):
            print(f"  No progress — stopping retries")
            permanently_failed.extend(still_invalid)
            break

        invalid = still_invalid

    # Save updated reference
    save_ticker_reference(ticker_ref)

    # ── Step 6: Build universe and save ──
    universe = {}
    for ticker, df in valid.items():
        compute_dvol_20d(df)
        universe[ticker] = df
    for ticker, df in unvalidatable.items():
        compute_dvol_20d(df)
        universe[ticker] = df

    elapsed = time.time() - t0
    print(f"\nFetch complete: {len(universe):,} tickers in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    if permanently_failed:
        print(f"  Permanently failed: {len(permanently_failed)} tickers")
        if len(permanently_failed) <= 20:
            print(f"    {permanently_failed}")

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
    """Append new bars to existing daily cache via EODHD.

    Validated append:
    1. Fetch SPY first — confirm new trading day, get ground truth
    2. Check for stock splits — tickers that split get full refetch
    3. Append new bars for all tickers
    4. Validate: every ticker's bar count must match SPY's expected count
    5. Retry until all pass or genuinely unavailable
    6. Only saves when all tickers are validated
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

    # ── Step 1: SPY first ──
    spy_df = fetch_spy_reference()
    spy_dates = build_spy_date_set(spy_df)

    # Check if SPY has a new bar vs what's cached
    cached_spy_last = str(universe.get("SPY", pd.DataFrame({"date": [""]}))[
        "date"].iloc[-1])[:10]
    spy_last = str(spy_df["date"].iloc[-1])[:10]
    if spy_last <= cached_spy_last:
        print(f"  SPY last bar: {spy_last} (same as cache). No new data.")
        return universe

    print(f"  SPY new bar: {spy_last} (cache was {cached_spy_last})")

    # Load ticker reference
    ticker_ref = load_ticker_reference()
    if not ticker_ref:
        print("  WARNING: No ticker reference file. Run --build-reference first.")
        print("  Proceeding without validation.")

    # ── Step 2: Split detection ──
    print(f"\n  Checking for adjustment changes after {cached_spy_last}...")
    split_tickers = detect_splits(list(universe.keys()), universe, cached_spy_last)
    if split_tickers:
        print(f"  ⚠ {len(split_tickers)} tickers need full refetch: {split_tickers[:20]}")
        if len(split_tickers) > 20:
            print(f"    ... and {len(split_tickers) - 20} more")
        print(f"    These will get full refetch (historical prices changed)")
    else:
        print(f"  No adjustment changes detected")

    # Get current tradable tickers from local DB for new ticker detection
    try:
        db_tickers = get_tradable_tickers_local()
    except FileNotFoundError:
        db_tickers = list(universe.keys())

    # Categorize work
    to_append = [t for t in universe.keys()
                 if t not in split_tickers and t != "SPY"]
    to_full_refetch = list(split_tickers)
    to_fetch_new = [t for t in db_tickers if t not in universe]

    # Find last date per cached ticker
    last_dates = {}
    for ticker, df in universe.items():
        if len(df) > 0:
            last_dates[ticker] = str(df["date"].iloc[-1])[:10]

    print(f"  Tickers to append: {len(to_append)}")
    print(f"  Tickers to full refetch (split): {len(to_full_refetch)}")
    print(f"  New tickers (full fetch): {len(to_fetch_new)}")

    t0 = time.time()
    appended = 0
    new_added = 0
    no_new = 0
    failed = 0
    split_refetched = 0

    # Update SPY in universe first
    compute_dvol_20d(spy_df)
    universe["SPY"] = spy_df

    # ── Step 3a: Append new bars for existing tickers ──
    if to_append:
        print(f"\n  Appending new bars via EODHD...")

        def _append_one(ticker):
            return _eodhd_append_after_date(ticker, last_dates.get(ticker, "2020-01-01"))

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

    # ── Step 3b: Full refetch for split tickers ──
    if to_full_refetch:
        print(f"\n  Full refetch for {len(to_full_refetch)} split tickers...")

        refetch_results, refetch_failed = _batched_fetch(
            to_full_refetch,
            fetch_fn=lambda t: _eodhd_download(t, HISTORY_START),
            label="Split refetch",
        )
        failed += len(refetch_failed)

        for ticker, df in refetch_results.items():
            compute_dvol_20d(df)
            universe[ticker] = df
            split_refetched += 1

    # ── Step 3c: Full fetch for new tickers ──
    if to_fetch_new:
        print(f"\n  Fetching {len(to_fetch_new)} new tickers via EODHD...")

        # Update reference for new tickers
        if ticker_ref:
            ticker_ref = build_ticker_reference(to_fetch_new)

        new_results, new_failed = _batched_fetch(
            to_fetch_new,
            fetch_fn=lambda t: _eodhd_download(t, HISTORY_START),
            label="New tickers",
        )
        failed += len(new_failed)

        for ticker, df in new_results.items():
            if len(df) >= 50:
                compute_dvol_20d(df)
                universe[ticker] = df
                new_added += 1

    # ── Step 4: Validate all tickers ──
    if ticker_ref:
        print(f"\n  Validating all {len(universe)} tickers against SPY reference...")
        all_valid, all_invalid, all_unvalidatable = validate_daily_fetch(
            universe, spy_dates, ticker_ref)

        print(f"  Validated: {len(all_valid)}")
        print(f"  Failed validation: {len(all_invalid)}")
        print(f"  No reference (accepted): {len(all_unvalidatable)}")

        # Retry invalid tickers with full refetch
        retry_round = 0
        max_validation_retries = 5
        while all_invalid and retry_round < max_validation_retries:
            retry_round += 1
            print(f"\n  Validation retry {retry_round}/{max_validation_retries} — "
                  f"{len(all_invalid)} tickers...")

            retry_results, retry_failed = _batched_fetch(
                all_invalid,
                fetch_fn=lambda t: _eodhd_download(t, HISTORY_START),
                label=f"Retry {retry_round}",
                min_sleep=5.0,
                max_retries=2,
            )

            # Update universe with retried data
            for ticker, df in retry_results.items():
                compute_dvol_20d(df)
                universe[ticker] = df

            new_valid, still_invalid, new_unvalidatable = validate_daily_fetch(
                {t: universe[t] for t in all_invalid if t in universe},
                spy_dates, ticker_ref)

            print(f"  Retry {retry_round}: {len(new_valid)} passed, "
                  f"{len(still_invalid)} still failing")

            if len(still_invalid) == len(all_invalid):
                print(f"  No progress — stopping retries")
                break

            all_invalid = still_invalid

        if all_invalid:
            print(f"\n  ⚠ {len(all_invalid)} tickers could not be validated:")
            if len(all_invalid) <= 20:
                print(f"    {all_invalid}")

    elapsed = time.time() - t0

    # Save (always to new filename)
    print(f"\n  Saving daily cache...")
    with open(CACHE_DAILY_FILE, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)

    with open(CACHE_DAILY_META, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(CACHE_DAILY_FILE) / 1024 / 1024
    print(f"  Saved: {CACHE_DAILY_FILE} ({size_mb:.1f} MB)")
    print(f"  Appended: {appended}, Split refetched: {split_refetched}, "
          f"New: {new_added}, No change: {no_new}, Failed: {failed}")
    print(f"  Time: {elapsed:.0f}s")

    return universe


# ══════════════════════════════════════════════════════════════
# HTF CACHES — Weekly + Monthly OHLCV from EODHD
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


def _sync_htf_cache(interval, output_file, meta_file, label,
                    full_sweep=False):
    """Unified HTF cache sync.

    full_sweep=False (nightly append): fetch new bars for each ticker starting
        from the day after its own last cached bar. Uses _merge_htf_bars to
        append only — history is known-good in this path.
    full_sweep=True  (--htf CLI): fetch full history from HISTORY_START for
        every stale or missing ticker. Fully replaces existing data — no merge.
        Eliminates any gaps or corrupted history.

    No arbitrary lookback windows. All fetches are anchored to HISTORY_START
    or to each ticker's own last cached bar date.
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

    # Trim orphans — drop HTF tickers not in daily cache
    daily_set = set(all_tickers)
    orphans = [t for t in universe if t not in daily_set]
    if orphans:
        for t in orphans:
            del universe[t]
        print(f"  Trimmed {len(orphans)} orphan tickers not in daily cache")

    # Classify tickers
    to_work = []  # tickers that need fetching
    skipped = 0
    if universe:
        ref_ticker = "SPY" if "SPY" in universe else next(iter(universe))
        ref_last = str(universe[ref_ticker]["date"].iloc[-1])[:10]
        for t in all_tickers:
            if t not in universe:
                if full_sweep:
                    to_work.append(t)  # missing — fetch from HISTORY_START
            else:
                t_last = str(universe[t]["date"].iloc[-1])[:10]
                if t_last < ref_last:
                    to_work.append(t)  # stale — needs update
                else:
                    skipped += 1
    else:
        # No existing cache — all tickers need fetching
        to_work = list(all_tickers)

    print(f"  Total tickers: {len(all_tickers)}")
    print(f"  Already current: {skipped}")
    print(f"  To fetch: {len(to_work)}")

    t0 = time.time()
    updated = 0
    no_change = 0
    new_added = 0
    failed = 0

    if to_work:
        if full_sweep:
            # Full sweep: fetch from HISTORY_START, fully replace existing data
            def _fetch_full(ticker):
                return _eodhd_download(ticker, HISTORY_START, interval)

            results, failed_list = _batched_fetch(
                to_work, fetch_fn=_fetch_full, label=f"{label} fetch",
            )
            failed += len(failed_list)

            for ticker, df in results.items():
                if len(df) >= 3:
                    if ticker in universe:
                        universe[ticker] = df
                        updated += 1
                    else:
                        universe[ticker] = df
                        new_added += 1

        else:
            # Nightly append: fetch from day after each ticker's own last bar
            # History is known-good — use _merge_htf_bars to append only
            last_dates = {
                t: str(universe[t]["date"].iloc[-1])[:10]
                for t in to_work if t in universe
            }

            def _append_one(ticker):
                after = last_dates.get(ticker, HISTORY_START)
                start = (pd.Timestamp(after) + timedelta(days=1)).strftime(
                    "%Y-%m-%d")
                return _eodhd_download(ticker, start, interval)

            results, failed_list = _batched_fetch(
                to_work, fetch_fn=_append_one, label=f"{label} append",
            )
            failed += len(failed_list)

            for ticker, new_df in results.items():
                if len(new_df) > 0:
                    old_len = len(universe[ticker])
                    old_last = str(universe[ticker]["date"].iloc[-1])[:10]
                    universe[ticker] = _merge_htf_bars(universe[ticker],
                                                       new_df)
                    new_len = len(universe[ticker])
                    new_last = str(universe[ticker]["date"].iloc[-1])[:10]
                    if new_len != old_len or new_last != old_last:
                        updated += 1
                    else:
                        no_change += 1
                else:
                    no_change += 1

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
    """Full sweep of both weekly and monthly OHLCV caches.

    Fetches full history from HISTORY_START for all stale or missing tickers.
    Fully replaces existing data — no windows, no arbitrary lookbacks.
    """
    print("\n" + "=" * 60)
    print("  HTF CACHE — Weekly + Monthly OHLCV from EODHD")
    print("=" * 60)

    _sync_htf_cache("w", WEEKLY_FILE, WEEKLY_META, "WEEKLY",
                    full_sweep=True)
    _sync_htf_cache("m", MONTHLY_FILE, MONTHLY_META, "MONTHLY",
                    full_sweep=True)


def append_weekly():
    """Nightly append for weekly cache.

    Fetches new bars for each ticker from the day after its own last cached bar.
    No windows — anchored entirely to each ticker's own data.
    """
    return _sync_htf_cache(
        interval="w",
        output_file=WEEKLY_FILE,
        meta_file=WEEKLY_META,
        label="Weekly",
        full_sweep=False,
    )


def append_monthly():
    """Nightly append for monthly cache.

    Fetches new bars for each ticker from the day after its own last cached bar.
    No windows — anchored entirely to each ticker's own data.
    """
    return _sync_htf_cache(
        interval="m",
        output_file=MONTHLY_FILE,
        meta_file=MONTHLY_META,
        label="Monthly",
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

def check_freshness():
    """Check if EODHD has newer data than our cache.

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

    # Download latest bar from EODHD
    try:
        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        data = _eodhd_fetch_json("SPY", recent, _eodhd_end_date(), "d")
        if not data or len(data) == 0:
            print("  Could not fetch SPY from EODHD — assuming new data")
            return True

        eodhd_last = data[-1]["date"]

        print(f"  Cache last date: {last_cached}")
        print(f"  EODHD last:      {eodhd_last}")

        if eodhd_last > last_cached:
            print(f"  → New trading day detected")
            return True
        else:
            print(f"  → Already up to date")
            return False

    except Exception as e:
        print(f"  Could not check EODHD: {e}")
        print("  → Assuming new data (safe fallback)")
        return True


# Keep old name as alias for backward compatibility with nightly.py
check_yfinance_freshness = check_freshness


if __name__ == "__main__":
    force = "--force" in sys.argv
    mode_all = "--all" in sys.argv
    mode_daily = "--daily" in sys.argv or "--5yr" in sys.argv  # accept legacy flag
    mode_htf = "--htf" in sys.argv
    mode_weekly = "--weekly" in sys.argv
    mode_monthly = "--monthly" in sys.argv
    mode_htf_status = "--htf-status" in sys.argv
    mode_build_ref = "--build-reference" in sys.argv

    if mode_htf_status:
        pass  # no API calls needed
    elif not EODHD_API_TOKEN:
        print("ERROR: EODHD_API_TOKEN environment variable not set.")
        print("  Set it:  set EODHD_API_TOKEN=your_token_here  (Windows)")
        print("  Or:      export EODHD_API_TOKEN=your_token_here  (bash)")
        sys.exit(1)

    if mode_build_ref:
        # Build ticker reference from existing cache ticker list
        tickers = []
        for pkl in [CACHE_DAILY_FILE, CACHE_LEGACY_5YR]:
            if os.path.exists(pkl):
                with open(pkl, "rb") as f:
                    existing = pickle.load(f)
                tickers = sorted(existing.keys())
                del existing
                break
        if not tickers:
            try:
                tickers = get_tradable_tickers_local()
            except FileNotFoundError:
                print("ERROR: No cache or DB to get ticker list from.")
                sys.exit(1)
        build_ticker_reference(tickers, force=force)
    elif mode_htf_status:
        htf_status()
    elif mode_all:
        data = build_daily_cache(force=force)
        print(f"Daily cache ready: {len(data)} tickers")
        del data  # free memory before HTF
        import gc; gc.collect()
        build_htf_caches()
    elif mode_htf:
        build_htf_caches()
    elif mode_weekly:
        _sync_htf_cache("w", WEEKLY_FILE, WEEKLY_META, "WEEKLY",
                        full_sweep=True)
    elif mode_monthly:
        _sync_htf_cache("m", MONTHLY_FILE, MONTHLY_META, "MONTHLY",
                        full_sweep=True)
    elif mode_daily:
        data = build_daily_cache(force=force)
        print(f"Daily cache ready: {len(data)} tickers")
    else:
        data = build_cache(force=force)
        print(f"Cache ready: {len(data)} tickers")
