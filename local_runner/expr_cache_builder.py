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


def _init_worker(expressions):
    """Initialize worker with expression list."""
    global _w_expressions
    _w_expressions = expressions


def _compute_ticker_full(args):
    """Compute all expression series for one ticker.

    Args: (ticker, df_dict) where df_dict has OHLCV columns + date

    Returns: (ticker, dates_array, data_array) or (ticker, None, None)
    """
    ticker, df_dict = args
    global _w_expressions

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

        for j, expr in enumerate(_w_expressions):
            try:
                series = compute_series(engine, expr["compute"])
                if series is not None:
                    if len(series) == n_bars:
                        data[:, j] = series.astype(np.float32) if hasattr(series, 'astype') else np.array(series, dtype=np.float32)
                    elif len(series) < n_bars:
                        # Pad front with NaN (some indicators need warmup)
                        data[n_bars - len(series):, j] = series.astype(np.float32) if hasattr(series, 'astype') else np.array(series, dtype=np.float32)
            except:
                pass

        dates = df["date"].dt.strftime("%Y-%m-%d").values
        return (ticker, dates, data)

    except Exception as e:
        return (ticker, None, None)


def _append_ticker(args):
    """Compute expression values for just the last bar of a ticker.

    Used for nightly append — extends the cached array by 1 row.

    Args: (ticker, df_dict, existing_n_bars)

    Returns: (ticker, new_date, new_row) or (ticker, None, None)
    """
    ticker, df_dict, existing_n_bars = args
    global _w_expressions

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

        # How many new bars since last cache?
        n_new = n_bars - existing_n_bars

        n_exprs = len(_w_expressions)
        engine = ExpressionEngine(df)

        # Compute full series but only extract the new bars
        new_data = np.full((n_new, n_exprs), np.nan, dtype=np.float32)

        for j, expr in enumerate(_w_expressions):
            try:
                series = compute_series(engine, expr["compute"])
                if series is not None and len(series) == n_bars:
                    arr = series.astype(np.float32) if hasattr(series, 'astype') else np.array(series, dtype=np.float32)
                    new_data[:, j] = arr[-n_new:]
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

    # Create output directory
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)

    # Parallel computation
    n_workers = max(cpu_count() - 1, 1)
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
        # Submit in chunks to manage memory — don't queue everything at once
        chunk_size = n_workers * 4
        all_futures = {}

        for i in range(0, len(work_items), chunk_size):
            chunk = work_items[i:i + chunk_size]
            for item in chunk:
                future = pool.submit(_compute_ticker_full, item)
                all_futures[future] = item[0]  # ticker name

        for future in as_completed(all_futures):
            ticker = all_futures[future]
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
            except Exception as e:
                failed += 1

            completed += 1
            if completed % 100 == 0 or completed == len(work_items):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(work_items) - completed) / rate if rate > 0 else 0
                pct = completed / len(work_items) * 100
                print(f"    {completed:,}/{len(work_items):,} ({pct:.0f}%) "
                      f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s left] "
                      f"({len(ticker_info)} ok, {failed} failed)")

    total_time = time.time() - t0

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
    n_workers = max(cpu_count() - 1, 1)
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
