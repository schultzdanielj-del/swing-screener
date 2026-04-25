"""Compute per-feature market-wide range across the full expression cache.

For each of the ~16,039 expression features, streams per-feature min/max
across every bar of every ticker in the expr cache. Output is cached to
disk so Stage 2 of the two-stage scanner can reuse without recomputing.

Stamp: ticker count + cache_daily_meta.txt mtime. Reuse cached output
only if stamp matches. Otherwise recompute.

Output:
  research/rand_range/rand_range.npz
    - feature_min[n_feat] float32
    - feature_max[n_feat] float32
    - feature_range[n_feat] float32 (max - min)
    - meta: n_tickers, total_rows, cache_daily_meta_mtime
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "rand_range")
OUT_FILE = os.path.join(OUT_DIR, "rand_range.npz")
META_FILE = os.path.join(OUT_DIR, "meta.json")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402


def current_stamp():
    cache_meta_path = os.path.join(CACHE_DIR, "cache_daily_meta.txt")
    mtime = os.path.getmtime(cache_meta_path) if os.path.exists(cache_meta_path) else 0.0
    ec = ExprSeriesCache()
    n_tickers = len(ec.get_available_tickers())
    return {"n_tickers": n_tickers, "cache_daily_meta_mtime": mtime}


def load_cached_if_match():
    if not os.path.exists(OUT_FILE) or not os.path.exists(META_FILE):
        return None
    try:
        with open(META_FILE) as f:
            stored = json.load(f)
    except Exception:
        return None
    cur = current_stamp()
    if stored.get("n_tickers") == cur["n_tickers"] and \
       abs(stored.get("cache_daily_meta_mtime", 0) - cur["cache_daily_meta_mtime"]) < 1e-6:
        return stored
    return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    if load_cached_if_match() is not None:
        print(f"rand_range.npz already exists with matching stamp, reusing.", flush=True)
        # Quick load-verify
        d = np.load(OUT_FILE)
        print(f"  feature_min shape: {d['feature_min'].shape}  "
              f"feature_max shape: {d['feature_max'].shape}", flush=True)
        return

    print("Opening expression cache...", flush=True)
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expr cache invalid", flush=True)
        sys.exit(1)
    n_feat = ec.n_expressions
    tickers = sorted(ec.get_available_tickers())
    print(f"Expr cache: {n_feat} features x {len(tickers)} tickers", flush=True)
    if len(tickers) < 11000:
        print("FAIL: expr cache ticker count too low", flush=True)
        sys.exit(1)

    feature_min = np.full(n_feat, np.inf, dtype=np.float32)
    feature_max = np.full(n_feat, -np.inf, dtype=np.float32)
    total_rows = 0

    t0 = time.time()
    for i, ticker in enumerate(tickers):
        dates, data = ec.get_ticker(ticker)
        if dates is None or data is None:
            continue
        arr = data.astype(np.float32, copy=False)
        # NaN-safe: compute per-feature min/max ignoring NaN.
        with np.errstate(all='ignore'):
            tm_min = np.nanmin(arr, axis=0)
            tm_max = np.nanmax(arr, axis=0)
        # Update global running min/max where this ticker has non-NaN values.
        fin_min = np.isfinite(tm_min)
        fin_max = np.isfinite(tm_max)
        np.minimum(feature_min, np.where(fin_min, tm_min, np.inf), out=feature_min)
        np.maximum(feature_max, np.where(fin_max, tm_max, -np.inf), out=feature_max)
        total_rows += arr.shape[0]
        del arr, data, dates
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(tickers) - (i + 1)) / rate
            print(f"  {i+1}/{len(tickers)} tickers, {total_rows:,} rows, "
                  f"{elapsed:.1f}s elapsed, ETA {eta:.1f}s", flush=True)

    elapsed = time.time() - t0
    print(f"\nFinished: {len(tickers)} tickers, {total_rows:,} rows in {elapsed:.1f}s",
          flush=True)

    # Any feature with no finite values anywhere -> NaN for min/max/range.
    no_data = ~np.isfinite(feature_min) | ~np.isfinite(feature_max)
    feature_min[no_data] = np.nan
    feature_max[no_data] = np.nan
    feature_range = feature_max - feature_min
    feature_range[no_data] = np.nan

    n_good = int(np.sum(~no_data))
    print(f"Features with data: {n_good:,}/{n_feat:,}", flush=True)
    print(f"Range stats (data features): "
          f"min_range={np.nanmin(feature_range):.4g}, "
          f"median_range={np.nanmedian(feature_range):.4g}, "
          f"max_range={np.nanmax(feature_range):.4g}", flush=True)

    np.savez(OUT_FILE,
             feature_min=feature_min,
             feature_max=feature_max,
             feature_range=feature_range)
    stamp = current_stamp()
    stamp["n_features"] = int(n_feat)
    stamp["total_rows"] = int(total_rows)
    stamp["compute_seconds"] = float(elapsed)
    with open(META_FILE, 'w') as f:
        json.dump(stamp, f, indent=2)
    print(f"Wrote {OUT_FILE} and {META_FILE}", flush=True)


if __name__ == "__main__":
    main()
