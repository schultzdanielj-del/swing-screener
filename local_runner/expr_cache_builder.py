"""
Expression Series Cache Builder — Pre-compute all expression series for all tickers.

Eliminates the ~40 min recompute bottleneck in pyramid_grinder.py.
After building, the grinder loads pre-computed arrays instead of running
compute_series() from scratch.

Storage: One .npz file per ticker in local_runner/cache/expr_series/
  - "data": float32 array (n_bars, n_expressions)
  - "dates": date strings array

Manifest: local_runner/cache/expr_series/_manifest.json
  - expression names + order (to verify cache matches current library)
  - per-ticker bar counts
  - build timestamp

Usage:
    # First build (full, ~40 min):
    python local_runner/expr_cache_builder.py --build

    # Nightly append (1 bar per ticker, ~5-8 min):
    python local_runner/expr_cache_builder.py --append

    # Force full rebuild:
    python local_runner/expr_cache_builder.py --build --force

    # Check status:
    python local_runner/expr_cache_builder.py --status

Requires: 5yr OHLCV cache (universe_ohlcv_5yr.pkl)
"""

import os
import sys
import time
import json
import pickle
import hashlib
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")
MANIFEST_PATH = os.path.join(EXPR_CACHE_DIR, "_manifest.json")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# HTF RESAMPLING HELPERS
# ══════════════════════════════════════════════════════════════

def resample_ohlcv(daily_df, freq='W'):
    """Resample daily OHLCV to weekly ('W') or monthly ('ME').

    Returns a new DataFrame with OHLCV columns + 'date' column.
    Each row represents one week/month period.
    Returns None if too few bars to resample.
    """
    if len(daily_df) < 10:
        return None

    df = daily_df.copy()
    df = df.set_index('date')

    resampled = df.resample(freq).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna(subset=['close'])

    if len(resampled) < 5:
        return None

    resampled = resampled.reset_index()
    resampled.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    return resampled


def build_htf_to_daily_map(daily_dates, htf_df, freq='W'):
    """Build mapping from daily bar index → HTF bar index.

    For each daily bar, find which HTF period it belongs to.
    The HTF value is constant within each period (step function).

    Args:
        daily_dates: pd.Series or array of daily date values (datetime64)
        htf_df: resampled DataFrame with 'date' column (period end dates)
        freq: 'W' for weekly, 'ME' for monthly

    Returns:
        np.array of shape (n_daily_bars,) with HTF bar indices.
        -1 means no HTF bar available (before first HTF period).
    """
    n_daily = len(daily_dates)
    mapping = np.full(n_daily, -1, dtype=np.int32)

    # HTF dates are period-end dates. A daily bar belongs to the HTF period
    # whose end date is >= the daily date and whose start is <= the daily date.
    # Simplest: for each daily bar, find the latest HTF date that is >= daily date.
    # With sorted dates, we can use searchsorted.

    htf_dates = pd.to_datetime(htf_df['date']).values
    daily_dt = pd.to_datetime(daily_dates).values

    # For each daily bar, find the HTF period it falls into.
    # searchsorted gives us the insertion point — the HTF bar at that index
    # is the first one whose date >= daily date (using 'left' side).
    indices = np.searchsorted(htf_dates, daily_dt, side='left')

    # Clamp to valid range
    indices = np.clip(indices, 0, len(htf_dates) - 1)

    # Verify: the HTF date should be >= the daily date
    # If not (daily bar is after last HTF bar), map to last HTF bar
    for i in range(n_daily):
        idx = indices[i]
        if idx < len(htf_dates):
            mapping[i] = idx
        else:
            mapping[i] = len(htf_dates) - 1

    return mapping


def map_htf_series_to_daily(htf_series, htf_to_daily_map):
    """Map an HTF expression series back to daily bar indices.

    Args:
        htf_series: np.array of expression values at HTF frequency
        htf_to_daily_map: np.array from build_htf_to_daily_map()

    Returns:
        np.array of shape (n_daily_bars,) with HTF values stepped to daily.
    """
    n_daily = len(htf_to_daily_map)
    result = np.full(n_daily, np.nan, dtype=np.float32)

    valid = htf_to_daily_map >= 0
    valid_indices = htf_to_daily_map[valid]

    # Clamp to actual HTF series length
    in_range = valid_indices < len(htf_series)
    daily_mask = np.where(valid)[0][in_range]
    htf_indices = valid_indices[in_range]

    if len(daily_mask) > 0:
        result[daily_mask] = htf_series[htf_indices]

    return result


# ══════════════════════════════════════════════════════════════
# EXPRESSION LIBRARY FINGERPRINT
# ══════════════════════════════════════════════════════════════

def _expr_fingerprint(expressions):
    """Hash the expression library to detect changes."""
    # Hash names + compute specs — if either changes, cache is stale
    data = json.dumps(
        [(e["name"], e["compute"]) for e in expressions],
        sort_keys=True
    ).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def _load_expressions():
    """Load expression library."""
    from brute_expressions import generate_all
    return generate_all()


def _load_5yr_cache():
    """Load 5yr OHLCV cache."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py --5yr first.")
    with open(path, "rb") as f:
        return pickle.load(f)


# ══════════════════════════════════════════════════════════════
# MANIFEST
# ══════════════════════════════════════════════════════════════

def load_manifest():
    """Load manifest, return None if doesn't exist."""
    if not os.path.exists(MANIFEST_PATH):
        return None
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(manifest):
    """Save manifest to disk."""
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


# ══════════════════════════════════════════════════════════════
# PER-TICKER COMPUTATION (worker functions)
# ══════════════════════════════════════════════════════════════

_w_expressions = None
_w_daily_indices = None      # indices of daily (non-precomputed) expressions
_w_lsp_indices = None        # indices of LSP precomputed expressions
_w_htf_weekly_indices = None  # indices of weekly HTF expressions
_w_htf_monthly_indices = None # indices of monthly HTF expressions
_w_htf_weekly_base = None    # base compute specs for weekly HTF expressions
_w_htf_monthly_base = None   # base compute specs for monthly HTF expressions


def _init_worker(expressions):
    """Initialize worker with expression list and pre-classify indices."""
    global _w_expressions, _w_daily_indices, _w_lsp_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base

    _w_expressions = expressions

    _w_daily_indices = []
    _w_lsp_indices = []
    _w_htf_weekly_indices = []
    _w_htf_monthly_indices = []
    _w_htf_weekly_base = []
    _w_htf_monthly_base = []

    for j, expr in enumerate(expressions):
        compute = expr["compute"]
        if compute.get("op") == "precomputed":
            source = compute.get("source")
            if source == "lsp":
                _w_lsp_indices.append(j)
            elif source == "htf":
                tf = compute.get("timeframe")
                if tf == "w":
                    _w_htf_weekly_indices.append(j)
                    _w_htf_weekly_base.append(compute.get("base_compute"))
                elif tf == "m":
                    _w_htf_monthly_indices.append(j)
                    _w_htf_monthly_base.append(compute.get("base_compute"))
        else:
            _w_daily_indices.append(j)


def _compute_ticker_full(args):
    """Compute all expression series for one ticker (daily + LSP + HTF).

    Args: (ticker, df_dict) where df_dict has OHLCV columns + date

    Returns: (ticker, dates_array, data_array) or (ticker, None, None)
    """
    ticker, df_dict = args
    global _w_expressions, _w_daily_indices, _w_lsp_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base

    try:
        from scripts.expression_engine import ExpressionEngine
        from scripts.backtest_conditions import compute_series

        # Reconstruct DataFrame from dict (avoids pickle overhead of full DataFrame)
        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        n_bars = len(df)
        if n_bars < 50:
            return (ticker, None, None)

        n_exprs = len(_w_expressions)
        engine = ExpressionEngine(df)

        # Allocate output array
        data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

        # ── 1. Daily expressions (non-precomputed) ──
        for j in _w_daily_indices:
            try:
                series = compute_series(engine, _w_expressions[j]["compute"])
                if series is not None:
                    arr = np.asarray(series, dtype=np.float32)
                    if len(arr) == n_bars:
                        data[:, j] = arr
                    elif len(arr) < n_bars:
                        data[n_bars - len(arr):, j] = arr
            except:
                pass

        # ── 2. LSP expressions (precomputed by lsp_detector_v2) ──
        if _w_lsp_indices:
            try:
                from scripts.lsp_detector_v2 import compute_all_lsp_series
                lsp_dict = compute_all_lsp_series(df)
                for j in _w_lsp_indices:
                    col_name = _w_expressions[j]["compute"]["column"]
                    if col_name in lsp_dict:
                        arr = lsp_dict[col_name]
                        if len(arr) == n_bars:
                            data[:, j] = arr.astype(np.float32)
            except Exception:
                pass  # LSP fails silently — columns stay NaN

        # ── 3. HTF expressions (weekly + monthly) ──
        for tf_freq, tf_indices, tf_base_computes in [
            ("W", _w_htf_weekly_indices, _w_htf_weekly_base),
            ("ME", _w_htf_monthly_indices, _w_htf_monthly_base),
        ]:
            if not tf_indices:
                continue

            htf_df = resample_ohlcv(df, tf_freq)
            if htf_df is None or len(htf_df) < 5:
                continue  # too few bars — HTF columns stay NaN

            # Build daily→HTF mapping once per timeframe
            htf_map = build_htf_to_daily_map(df["date"], htf_df, tf_freq)

            # Create engine for HTF data
            htf_engine = ExpressionEngine(htf_df)

            for k, j in enumerate(tf_indices):
                try:
                    base_compute = tf_base_computes[k]
                    htf_series = compute_series(htf_engine, base_compute)
                    if htf_series is not None:
                        htf_arr = np.asarray(htf_series, dtype=np.float32)
                        # Map HTF series back to daily indices
                        daily_arr = map_htf_series_to_daily(htf_arr, htf_map)
                        data[:, j] = daily_arr
                except:
                    pass

        dates = df["date"].dt.strftime("%Y-%m-%d").values
        return (ticker, dates, data)

    except Exception as e:
        return (ticker, None, None)


def _append_ticker(args):
    """Compute expression values for just the new bars of a ticker.

    Used for nightly append — extends the cached array by N new rows.
    Must recompute full series (indicators need history) then extract new bars.

    Args: (ticker, df_dict, existing_n_bars)

    Returns: (ticker, new_dates, new_data) or (ticker, None, None)
    """
    ticker, df_dict, existing_n_bars = args
    global _w_expressions, _w_daily_indices, _w_lsp_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base

    try:
        from scripts.expression_engine import ExpressionEngine
        from scripts.backtest_conditions import compute_series

        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        n_bars = len(df)
        if n_bars < 50 or n_bars <= existing_n_bars:
            return (ticker, None, None)

        n_new = n_bars - existing_n_bars
        n_exprs = len(_w_expressions)
        engine = ExpressionEngine(df)

        # Compute full series but only extract the new bars
        new_data = np.full((n_new, n_exprs), np.nan, dtype=np.float32)

        # ── 1. Daily expressions ──
        for j in _w_daily_indices:
            try:
                series = compute_series(engine, _w_expressions[j]["compute"])
                if series is not None and len(series) == n_bars:
                    arr = np.asarray(series, dtype=np.float32)
                    new_data[:, j] = arr[-n_new:]
            except:
                pass

        # ── 2. LSP expressions ──
        if _w_lsp_indices:
            try:
                from scripts.lsp_detector_v2 import compute_all_lsp_series
                lsp_dict = compute_all_lsp_series(df)
                for j in _w_lsp_indices:
                    col_name = _w_expressions[j]["compute"]["column"]
                    if col_name in lsp_dict:
                        arr = lsp_dict[col_name]
                        if len(arr) == n_bars:
                            new_data[:, j] = arr[-n_new:].astype(np.float32)
            except Exception:
                pass

        # ── 3. HTF expressions ──
        for tf_freq, tf_indices, tf_base_computes in [
            ("W", _w_htf_weekly_indices, _w_htf_weekly_base),
            ("ME", _w_htf_monthly_indices, _w_htf_monthly_base),
        ]:
            if not tf_indices:
                continue

            htf_df = resample_ohlcv(df, tf_freq)
            if htf_df is None or len(htf_df) < 5:
                continue

            htf_map = build_htf_to_daily_map(df["date"], htf_df, tf_freq)
            htf_engine = ExpressionEngine(htf_df)

            for k, j in enumerate(tf_indices):
                try:
                    base_compute = tf_base_computes[k]
                    htf_series = compute_series(htf_engine, base_compute)
                    if htf_series is not None:
                        htf_arr = np.asarray(htf_series, dtype=np.float32)
                        daily_arr = map_htf_series_to_daily(htf_arr, htf_map)
                        new_data[:, j] = daily_arr[-n_new:]
                except:
                    pass

        new_dates = df["date"].dt.strftime("%Y-%m-%d").values[-n_new:]
        return (ticker, new_dates, new_data)

    except Exception as e:
        return (ticker, None, None)


# ══════════════════════════════════════════════════════════════
# CACHE I/O
# ══════════════════════════════════════════════════════════════

def _ticker_cache_path(ticker):
    """Path for a ticker's cached expression series."""
    # Handle tickers with special chars
    safe = ticker.replace("/", "_").replace(".", "_")
    return os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")


def save_ticker_cache(ticker, dates, data):
    """Save one ticker's expression series to disk."""
    path = _ticker_cache_path(ticker)
    np.savez_compressed(path, data=data, dates=dates)


def load_ticker_cache(ticker):
    """Load one ticker's cached expression series.

    Returns: (dates, data) or (None, None)
    """
    path = _ticker_cache_path(ticker)
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        return loaded["dates"], loaded["data"]
    except:
        return None, None


# ══════════════════════════════════════════════════════════════
# FULL BUILD
# ══════════════════════════════════════════════════════════════

def build_full(force=False):
    """Build the complete expression series cache from scratch."""
    print("\n" + "=" * 70)
    print("  EXPRESSION SERIES CACHE — FULL BUILD")
    print("=" * 70)

    # Load expression library
    print("\n  Loading expressions...")
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)
    print(f"  {len(expressions)} expressions (fingerprint: {fingerprint})")

    # Check if cache is fresh
    if not force:
        manifest = load_manifest()
        if manifest and manifest.get("fingerprint") == fingerprint:
            n_cached = len(manifest.get("tickers", {}))
            print(f"\n  Cache is fresh ({n_cached} tickers, fingerprint matches).")
            print(f"  Use --force to rebuild, or --append to add new bars.")
            return manifest

    # Load OHLCV
    print("\n  Loading 5yr OHLCV cache...")
    universe_cache = _load_5yr_cache()
    print(f"  {len(universe_cache)} tickers loaded")

    # Filter valid tickers
    valid_tickers = {t: df for t, df in universe_cache.items() if len(df) >= 50}
    print(f"  {len(valid_tickers)} tickers with ≥50 bars")

    # Prepare work items — convert DataFrames to dicts for cheaper serialization
    print("\n  Preparing work items...")
    work_items = []
    for ticker, df in valid_tickers.items():
        df_dict = {
            "date": df["date"].values,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values,
        }
        work_items.append((ticker, df_dict))

    # Free the large caches — work_items has everything we need now
    del universe_cache, valid_tickers
    import gc; gc.collect()

    # Create output directory
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)

    # Parallel computation
    n_workers = int(os.environ.get("EXPR_CACHE_WORKERS", max(cpu_count() - 1, 1)))
    print(f"\n  Computing {len(work_items)} tickers × {len(expressions)} expressions")
    print(f"  Workers: {n_workers}")

    t0 = time.time()
    ticker_info = {}
    completed = 0
    failed = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_worker,
        initargs=(expressions,)
    ) as pool:
        # TRUE chunked submission: only n_workers*2 items in flight at a time.
        # Each result is saved to disk and discarded immediately to minimize memory.
        # This prevents OOM from queued futures holding large numpy arrays.
        max_in_flight = n_workers * 2
        pending = {}
        work_idx = 0
        first_errors = []

        def _submit_next():
            """Submit one work item if available."""
            nonlocal work_idx
            if work_idx < len(work_items):
                item = work_items[work_idx]
                future = pool.submit(_compute_ticker_full, item)
                pending[future] = item[0]  # ticker name
                work_idx += 1
                return True
            return False

        def _collect_one(future):
            """Collect one result, save to disk, free memory."""
            nonlocal completed, failed
            ticker = pending.pop(future)
            try:
                ticker_out, dates, data = future.result()
                if dates is not None and data is not None:
                    save_ticker_cache(ticker_out, dates, data)
                    ticker_info[ticker_out] = {
                        "n_bars": len(dates),
                        "last_date": str(dates[-1]),
                    }
                else:
                    failed += 1
                    if len(first_errors) < 5:
                        first_errors.append(f"{ticker}: returned None")
            except Exception as e:
                failed += 1
                if len(first_errors) < 5:
                    first_errors.append(f"{ticker}: {type(e).__name__}: {str(e)[:200]}")
            del future  # free the result memory immediately
            completed += 1
            if completed % 100 == 0 or completed == len(work_items):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(work_items) - completed) / rate if rate > 0 else 0
                pct = completed / len(work_items) * 100
                print(f"    {completed:,}/{len(work_items):,} ({pct:.0f}%) "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s left] "
                      f"({len(ticker_info)} ok, {failed} failed)")

        # Seed the initial batch
        for _ in range(min(max_in_flight, len(work_items))):
            _submit_next()

        # Process as completed, immediately submit replacements
        while pending:
            # Wait for ANY one future to complete
            done_iter = as_completed(pending)
            future = next(done_iter)
            _collect_one(future)
            # Submit replacement to keep pipeline full
            _submit_next()

    total_time = time.time() - t0

    if first_errors:
        print(f"\n  First {len(first_errors)} errors:")
        for err in first_errors:
            print(f"    ✗ {err}")

    # Save manifest
    manifest = {
        "fingerprint": fingerprint,
        "n_expressions": len(expressions),
        "expr_names": [e["name"] for e in expressions],
        "n_tickers": len(ticker_info),
        "tickers": ticker_info,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "build_time_s": round(total_time, 1),
    }
    save_manifest(manifest)

    # Disk usage
    total_bytes = sum(
        os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
        for f in os.listdir(EXPR_CACHE_DIR)
        if f.endswith(".npz")
    )
    total_gb = total_bytes / (1024 ** 3)

    print(f"\n  {'=' * 50}")
    print(f"  BUILD COMPLETE")
    print(f"  {'=' * 50}")
    print(f"  Tickers cached: {len(ticker_info):,}")
    print(f"  Failed: {failed}")
    print(f"  Total disk: {total_gb:.1f} GB")
    print(f"  Time: {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  Manifest: {MANIFEST_PATH}")

    return manifest


# ══════════════════════════════════════════════════════════════
# NIGHTLY APPEND
# ══════════════════════════════════════════════════════════════

def append_new_bars():
    """Append new bars to existing cache.

    Compares current OHLCV cache bar counts against manifest,
    then computes and appends only new bars for each ticker.
    Also handles brand new tickers (full compute).
    """
    print("\n" + "=" * 70)
    print("  EXPRESSION SERIES CACHE — NIGHTLY APPEND")
    print("=" * 70)

    # Load manifest
    manifest = load_manifest()
    if manifest is None:
        print("\n  No existing cache. Running full build instead.")
        return build_full()

    # Load expression library and verify fingerprint
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)

    if fingerprint != manifest.get("fingerprint"):
        print(f"\n  Expression library changed!")
        print(f"    Cached: {manifest.get('fingerprint')}")
        print(f"    Current: {fingerprint}")
        print(f"  Running full rebuild...")
        return build_full(force=True)

    print(f"\n  Fingerprint OK: {fingerprint}")
    print(f"  Cached tickers: {manifest['n_tickers']}")

    # Load OHLCV
    print("\n  Loading 5yr OHLCV cache...")
    universe_cache = _load_5yr_cache()
    print(f"  {len(universe_cache)} tickers in OHLCV cache")

    # Find tickers that need updating
    cached_tickers = manifest.get("tickers", {})
    work_append = []  # (ticker, df_dict, existing_n_bars) — extend
    work_new = []     # (ticker, df_dict) — full compute

    for ticker, df in universe_cache.items():
        if len(df) < 50:
            continue

        df_dict = {
            "date": df["date"].values,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values,
        }

        if ticker in cached_tickers:
            existing_n = cached_tickers[ticker]["n_bars"]
            if len(df) > existing_n:
                work_append.append((ticker, df_dict, existing_n))
        else:
            work_new.append((ticker, df_dict))

    print(f"\n  Tickers to append: {len(work_append)}")
    print(f"  New tickers (full compute): {len(work_new)}")

    if not work_append and not work_new:
        print("  Nothing to do — cache is up to date.")
        return manifest

    t0 = time.time()
    n_workers = int(os.environ.get("EXPR_CACHE_WORKERS", max(cpu_count() - 1, 1)))
    updated = 0
    failed = 0

    # Process appends
    if work_append:
        print(f"\n  Appending new bars ({n_workers} workers)...")
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_worker,
            initargs=(expressions,)
        ) as pool:
            futures = {pool.submit(_append_ticker, item): item[0]
                       for item in work_append}

            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    ticker_out, new_dates, new_data = future.result()
                    if new_dates is not None and new_data is not None:
                        # Load existing, concatenate, save
                        old_dates, old_data = load_ticker_cache(ticker_out)
                        if old_dates is not None:
                            merged_dates = np.concatenate([old_dates, new_dates])
                            merged_data = np.vstack([old_data, new_data])
                            save_ticker_cache(ticker_out, merged_dates, merged_data)
                            cached_tickers[ticker_out] = {
                                "n_bars": len(merged_dates),
                                "last_date": str(merged_dates[-1]),
                            }
                            updated += 1
                        else:
                            failed += 1
                    else:
                        # No new bars (might be same length)
                        pass
                except:
                    failed += 1

                if (updated + failed) % 200 == 0:
                    elapsed = time.time() - t0
                    print(f"    Appended: {updated}/{len(work_append)} "
                          f"[{elapsed:.0f}s]")

    # Process new tickers (full compute)
    if work_new:
        print(f"\n  Computing {len(work_new)} new tickers...")
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_worker,
            initargs=(expressions,)
        ) as pool:
            futures = {pool.submit(_compute_ticker_full, item): item[0]
                       for item in work_new}

            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    ticker_out, dates, data = future.result()
                    if dates is not None and data is not None:
                        save_ticker_cache(ticker_out, dates, data)
                        cached_tickers[ticker_out] = {
                            "n_bars": len(dates),
                            "last_date": str(dates[-1]),
                        }
                        updated += 1
                    else:
                        failed += 1
                except:
                    failed += 1

    total_time = time.time() - t0

    # Update manifest
    manifest["tickers"] = cached_tickers
    manifest["n_tickers"] = len(cached_tickers)
    manifest["last_append"] = datetime.now(timezone.utc).isoformat()
    manifest["last_append_time_s"] = round(total_time, 1)
    save_manifest(manifest)

    print(f"\n  Append complete: {updated} updated, {failed} failed ({total_time:.0f}s)")
    return manifest


# ══════════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════════

def show_status():
    """Show cache status."""
    print("\n  Expression Series Cache Status")
    print("  " + "─" * 40)

    manifest = load_manifest()
    if manifest is None:
        print("  No cache exists. Run --build first.")
        return

    print(f"  Expressions: {manifest['n_expressions']}")
    print(f"  Fingerprint: {manifest['fingerprint']}")
    print(f"  Tickers: {manifest['n_tickers']}")
    print(f"  Built: {manifest.get('built_at', 'unknown')}")
    print(f"  Build time: {manifest.get('build_time_s', '?')}s")

    if manifest.get("last_append"):
        print(f"  Last append: {manifest['last_append']}")
        print(f"  Append time: {manifest.get('last_append_time_s', '?')}s")

    # Check current library match
    try:
        expressions = _load_expressions()
        fp = _expr_fingerprint(expressions)
        if fp == manifest["fingerprint"]:
            print(f"\n  ✓ Expression library matches cache")
        else:
            print(f"\n  ⚠ Expression library CHANGED — rebuild needed")
            print(f"    Cached: {manifest['fingerprint']}")
            print(f"    Current: {fp}")
    except:
        print(f"\n  ? Could not verify expression library")

    # Disk usage
    if os.path.exists(EXPR_CACHE_DIR):
        total_bytes = sum(
            os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
            for f in os.listdir(EXPR_CACHE_DIR)
            if f.endswith(".npz")
        )
        print(f"\n  Disk usage: {total_bytes / (1024**3):.1f} GB "
              f"({total_bytes / (1024**2):.0f} MB)")

    # Sample ticker stats
    tickers = manifest.get("tickers", {})
    if tickers:
        bar_counts = [t["n_bars"] for t in tickers.values()]
        print(f"\n  Bar counts: min={min(bar_counts)}, "
              f"avg={sum(bar_counts)/len(bar_counts):.0f}, "
              f"max={max(bar_counts)}")


# ══════════════════════════════════════════════════════════════
# LOADER — Used by pyramid_grinder.py
# ══════════════════════════════════════════════════════════════

class ExprSeriesCache:
    """Interface for the pyramid grinder to load cached expression series.

    Usage:
        cache = ExprSeriesCache()
        if cache.is_valid():
            dates, data = cache.get_ticker("AAPL")
            # data shape: (n_bars, n_expressions)
            # To get expression j's series: data[:, j]
    """

    def __init__(self):
        self.manifest = load_manifest()
        self._expr_name_to_idx = {}
        if self.manifest:
            for i, name in enumerate(self.manifest.get("expr_names", [])):
                self._expr_name_to_idx[name] = i

    def is_valid(self, expressions=None):
        """Check if cache exists and matches current expression library."""
        if self.manifest is None:
            return False
        if expressions is not None:
            fp = _expr_fingerprint(expressions)
            return fp == self.manifest.get("fingerprint")
        return True

    @property
    def expr_names(self):
        return self.manifest.get("expr_names", []) if self.manifest else []

    @property
    def n_expressions(self):
        return self.manifest.get("n_expressions", 0) if self.manifest else 0

    def expr_index(self, name):
        """Get column index for an expression name."""
        return self._expr_name_to_idx.get(name)

    def get_ticker(self, ticker):
        """Load cached series for a ticker.

        Returns: (dates, data) where data is (n_bars, n_expressions) float32
                 or (None, None) if not cached.
        """
        return load_ticker_cache(ticker)

    def get_ticker_series(self, ticker, expr_indices):
        """Load specific expression columns for a ticker.

        More memory-efficient than loading all columns.

        Args:
            ticker: ticker symbol
            expr_indices: list of expression column indices to load

        Returns: (dates, data) where data is (n_bars, len(expr_indices)) float32
                 or (None, None) if not cached.
        """
        dates, full_data = load_ticker_cache(ticker)
        if dates is None:
            return None, None
        return dates, full_data[:, expr_indices]

    def get_available_tickers(self):
        """Return set of tickers in cache."""
        if self.manifest is None:
            return set()
        return set(self.manifest.get("tickers", {}).keys())

    def get_ticker_bar_count(self, ticker):
        """Get cached bar count for a ticker."""
        if self.manifest is None:
            return 0
        return self.manifest.get("tickers", {}).get(ticker, {}).get("n_bars", 0)


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Expression Series Cache Builder")
    parser.add_argument("--build", action="store_true", help="Full build from scratch")
    parser.add_argument("--append", action="store_true", help="Append new bars (nightly)")
    parser.add_argument("--status", action="store_true", help="Show cache status")
    parser.add_argument("--force", action="store_true", help="Force rebuild even if fresh")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.append:
        append_new_bars()
    elif args.build:
        build_full(force=args.force)
    else:
        parser.print_help()
        print("\n  Hint: Run --build for first time, --append for nightly updates.")


if __name__ == "__main__":
    main()
