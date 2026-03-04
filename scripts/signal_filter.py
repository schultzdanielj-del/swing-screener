"""
Signal Filter -- Deduplicate, apply exit, rank for vetting.

Scans all 5yr history for signal conditions, then:
  1. Deduplicates: consecutive signal bars for same ticker -> keep rightmost
  2. Applies exit condition: run each signal forward, check if exit fires
  3. Measures exit distance: signal close -> exit close (in ADR)
  4. Filters: keep only signals where exit distance >= example floor
  5. Ranks: sort by exit distance descending
  6. Outputs: ranked JSON for chart vetting + uploads to Railway

Also deduplicates examples the same way (one signal bar per example).

Usage:
    python scripts/signal_filter.py --setup dtss
    python scripts/signal_filter.py --setup dtss --min-adr 2.0
    python scripts/signal_filter.py --setup dtss --charts  # also generate charts
"""

import argparse
import os
import sys
import time
import json
import pickle
import numpy as np
import pandas as pd
import requests
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

# Force UTF-8 output on Windows (cp1252 can't handle ✓, ⚠, etc.)
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from expr_cache_builder import ExprSeriesCache

# ============================================================
# Config
# ============================================================
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD = 120
DEFAULT_WORKERS = os.cpu_count() or 8

SETUP_CONFIGS = {
    "dtss": {"direction": "short", "examples_endpoint": "/api/examples/dtss"},
}


# ============================================================
# Data Loading
# ============================================================
def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    print(f"  Loading 5yr cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_pyramid_conditions(setup_type, conditions_file=None):
    """Load signal conditions from pyramid results.

    Args:
        setup_type: e.g. "dtss"
        conditions_file: optional path override — load conditions directly from
            this file instead of searching for pyramid results. Supports both
            pyramid result files and condition_pruner output files.
            Keys checked in order: "all_conditions", "pruned_conditions".

    Searches both local_runner/cache/ and data/ directories.
    If multiple files exist, picks the latest by timestamp in filename.
    """
    import glob

    if conditions_file:
        if not os.path.exists(conditions_file):
            raise FileNotFoundError(f"Conditions file not found: {conditions_file}")
        with open(conditions_file) as f:
            data = json.load(f)
        conditions = data.get("all_conditions", data.get("pruned_conditions", []))
        n_dropped = data.get("n_dropped", 0)
        print(f"  Loaded {len(conditions)} conditions from {os.path.basename(conditions_file)}")
        if n_dropped:
            print(f"  (pruned from {data.get('n_input_conditions', '?')}, {n_dropped} dropped)")
        return conditions

    search_dirs = [
        os.path.join(REPO_ROOT, "local_runner", "cache"),
        os.path.join(REPO_ROOT, "data"),
    ]

    # Collect all matching files
    candidates = []
    for d in search_dirs:
        # Exact name match
        exact = os.path.join(d, f"pyramid_results_{setup_type}.json")
        if os.path.exists(exact):
            candidates.append(exact)
        # Timestamped files (e.g. pyramid_dtss_mp_sig264_pk3_20260228_163923.json)
        pattern = os.path.join(d, f"pyramid_{setup_type}_*.json")
        candidates.extend(glob.glob(pattern))

    if not candidates:
        raise FileNotFoundError(f"No pyramid results found for {setup_type}")

    # Pick the most recently modified file
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    best = candidates[0]

    with open(best) as f:
        data = json.load(f)
    conditions = data.get("all_conditions", [])
    total = data.get("summary", {}).get("final_total", "?")
    print(f"  Loaded {len(conditions)} conditions from {os.path.basename(best)}")
    print(f"  Grinder result: {total} total signals, {data.get('timestamp', '?')}")
    return conditions


def load_exit_condition(setup_type):
    """Load best exit condition from signal exit grind results.
    
    Loads from signal_exit_grinder.py output (cache-compatible exits).
    NOT from exit_grinder.py (trade management exits — entry-relative, shelved).
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Signal exit grind (cache-compatible — what we want)
    signal_exit_paths = [
        os.path.join(repo_root, "data", "signal_exit_grind", f"signal_exit_{setup_type}.json"),
    ]

    for path in signal_exit_paths:
        if not os.path.exists(path):
            continue
        with open(path) as f:
            data = json.load(f)

        if data.get("grinder_type") != "signal_exit":
            continue  # wrong file type

        if "top_conditions" in data and len(data["top_conditions"]) > 0:
            best = data["top_conditions"][0]
            print(f"  Exit condition (signal exit): {best['expression']} {best['direction']} {best['threshold']}")
            print(f"  Median capture eff: {best.get('median_efficiency', '?')}")
            return best

    raise FileNotFoundError(
        f"No signal exit grind results found for {setup_type}.\n"
        f"  Run: python scripts/signal_exit_grinder.py --setup {setup_type}\n"
        f"  (This uses expression cache only — same computation path as signal grinder)"
    )


def load_examples(setup_type):
    """Load validated examples from Railway."""
    import requests
    try:
        r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}", timeout=30)
        r.raise_for_status()
        examples = r.json().get("examples", [])
        print(f"  Loaded {len(examples)} examples from Railway")
        return examples
    except Exception as e:
        print(f"  Warning: couldn't load examples from Railway: {e}")
        return []


# ============================================================
# Phase 1: Scan all signals (parallel)
# ============================================================
_worker_cache = None
_worker_conditions = None
_worker_expr_cache = None
_worker_cond_col_indices = None


def _init_scan_worker(cache, conditions, expr_cache_dir, cond_col_indices):
    global _worker_cache, _worker_conditions, _worker_expr_cache, _worker_cond_col_indices
    _worker_cache = cache
    _worker_conditions = conditions
    _worker_expr_cache = expr_cache_dir
    _worker_cond_col_indices = cond_col_indices


def _load_ticker_npz(ticker):
    """Load expression cache .npz for a ticker."""
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = os.path.join(_worker_expr_cache, f"{safe}.npz")
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        return loaded["dates"], loaded["data"]
    except:
        return None, None


def _scan_batch(tickers):
    """Scan a batch of tickers using expression cache. Returns list of signals."""
    signals = []
    skipped = 0
    for ticker in tickers:
        df = _worker_cache.get(ticker)
        if df is None or len(df) < 100:
            skipped += 1
            continue
        try:
            dates_cache, data_cache = _load_ticker_npz(ticker)
            if dates_cache is None:
                skipped += 1
                continue

            n_bars = len(df)
            # Verify bar count matches
            if len(dates_cache) != n_bars:
                skipped += 1
                continue

            pass_mask = np.ones(n_bars, dtype=bool)
            pass_mask[:50] = False  # warmup

            for i, cond in enumerate(_worker_conditions):
                col_idx = _worker_cond_col_indices[i]
                if col_idx is None:
                    pass_mask[:] = False
                    break
                series = data_cache[:, col_idx]
                low, high = cond["low"], cond["high"]
                in_range = (series >= low) & (series <= high)
                in_range[np.isnan(series)] = False
                pass_mask &= in_range

            signal_indices = np.where(pass_mask)[0]
            if len(signal_indices) > 0:
                dates = df["date"].values
                closes = df["close"].values
                for idx in signal_indices:
                    signals.append({
                        "ticker": ticker,
                        "date": str(dates[idx])[:10],
                        "bar_idx": int(idx),
                        "close": float(closes[idx]),
                    })
        except Exception:
            skipped += 1
    return signals, skipped


def scan_all_signals(cache, conditions, workers, expr_cache):
    """Scan full universe for signal conditions using expression cache."""
    tickers = list(cache.keys())
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    # Map condition names to expression cache column indices
    cond_col_indices = []
    for cond in conditions:
        col_idx = expr_cache.expr_index(cond["name"])
        if col_idx is None:
            print(f"  WARNING: condition '{cond['name']}' not in expression cache!")
        cond_col_indices.append(col_idx)

    expr_cache_dir = os.path.join(REPO_ROOT, "local_runner", "cache", "expr_series")

    print(f"\n  Scanning {len(tickers):,} tickers x {len(conditions)} conditions...")
    print(f"  {workers} workers, {len(batches)} batches (using expression cache)")
    t0 = time.time()

    all_signals = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_scan_worker,
        initargs=(cache, conditions, expr_cache_dir, cond_col_indices)
    ) as pool:
        futures = [pool.submit(_scan_batch, batch) for batch in batches]
        done = 0
        for future in futures:
            batch_signals, _ = future.result()
            all_signals.extend(batch_signals)
            done += 1
            if done % max(len(batches) // 5, 1) == 0 or done == len(batches):
                elapsed = time.time() - t0
                pct = done / len(batches) * 100
                print(f"    {pct:.0f}% [{elapsed:.0f}s] {len(all_signals):,} signals")

    elapsed = time.time() - t0
    print(f"\n  OK: {len(all_signals):,} raw signals in {elapsed:.0f}s")
    return all_signals


# ============================================================
# Phase 2: Deduplicate -- consecutive bars -> keep rightmost
# ============================================================
def deduplicate_signals(signals):
    """
    Group consecutive signal bars for the same ticker, keep only the rightmost
    (latest date) in each consecutive run.

    Rule: if there is ANY gap (even 1 bar where conditions didn't fire),
    it's a separate signal. Only truly back-to-back bars get collapsed.
    """
    # Sort by ticker then bar_idx
    signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))

    deduped = []
    i = 0
    while i < len(signals):
        current = signals[i]
        ticker = current["ticker"]

        # Walk forward through consecutive bars for same ticker
        j = i + 1
        while j < len(signals):
            nxt = signals[j]
            if nxt["ticker"] != ticker:
                break
            if nxt["bar_idx"] != signals[j - 1]["bar_idx"] + 1:
                break
            j += 1

        # signals[i:j] is a consecutive run -- keep the rightmost (j-1)
        rightmost = signals[j - 1]
        cluster_size = j - i
        rightmost["cluster_size"] = cluster_size
        rightmost["cluster_start_date"] = signals[i]["date"]
        deduped.append(rightmost)

        i = j

    print(f"  OK: Deduplicated: {len(signals):,} -> {len(deduped):,} signals "
          f"({len(signals) - len(deduped):,} collapsed)")
    return deduped


# ============================================================
# Phase 3: Apply exit condition, measure distance
# ============================================================
def apply_exit_and_measure(signals, cache, exit_cond, direction, expr_cache, max_forward=MAX_FORWARD):
    """
    For each signal, run forward and check if exit condition fires.
    Measure signal close -> exit close in ADR units.
    Uses expression cache for exit condition (same computation path).
    """
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]  # ">=" or "<="

    exit_col_idx = expr_cache.expr_index(expr_name)
    if exit_col_idx is None:
        print(f"  ERROR: exit expression '{expr_name}' not in expression cache!")
        print(f"  Rebuild cache: python local_runner/expr_cache_builder.py --build --force")
        return []

    # Also need ADR -- check if it's in the cache
    adr_col_idx = expr_cache.expr_index("adr14")

    print(f"\n  Applying exit: {expr_name} {exit_dir} {exit_thresh}")
    print(f"  Direction: {direction}, max forward: {max_forward} bars")

    results = []
    no_exit = 0
    errors = 0

    # Pre-load expression cache per ticker (avoid repeated file loads)
    _ticker_cache = {}

    for i, sig in enumerate(signals):
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        df = cache.get(ticker)

        if df is None or bar_idx >= len(df) - 1:
            errors += 1
            continue

        try:
            # Load expression cache for this ticker (cached per ticker)
            if ticker not in _ticker_cache:
                dates, data = expr_cache.get_ticker(ticker)
                _ticker_cache[ticker] = (dates, data)
            cached_dates, cached_data = _ticker_cache[ticker]

            if cached_dates is None or len(cached_dates) != len(df):
                errors += 1
                continue

            # ADR at signal bar
            if adr_col_idx is not None:
                adr_at_signal = float(cached_data[bar_idx, adr_col_idx])
            else:
                # Fallback: compute ADR manually from OHLCV
                h = df["high"].values
                l = df["low"].values
                start = max(0, bar_idx - 13)
                adr_at_signal = float(np.mean(h[start:bar_idx+1] - l[start:bar_idx+1]))

            if adr_at_signal <= 0 or np.isnan(adr_at_signal):
                errors += 1
                continue

            signal_close = float(df["close"].values[bar_idx])
            n_available = len(df) - bar_idx - 1
            actual_forward = min(max_forward, n_available)

            if actual_forward < 5:
                errors += 1
                continue

            # Get exit expression series from cache
            exit_series = cached_data[:, exit_col_idx]

            # Find first bar after signal where exit fires
            exit_bar = None
            exit_close = None
            for fwd in range(1, actual_forward + 1):
                check_idx = bar_idx + fwd
                val = exit_series[check_idx]
                if np.isnan(val):
                    continue
                if exit_dir == ">=" and val >= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break
                elif exit_dir == "<=" and val <= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break

            if exit_bar is None:
                no_exit += 1
                continue

            # Measure distance: signal close -> exit close in ADR
            if direction == "short":
                move_pct = (signal_close - exit_close) / signal_close * 100
                move_adr = (signal_close - exit_close) / adr_at_signal
            else:
                move_pct = (exit_close - signal_close) / signal_close * 100
                move_adr = (exit_close - signal_close) / adr_at_signal

            # Also compute MFE for reference
            fwd_slice = slice(bar_idx + 1, bar_idx + exit_bar + 1)
            if direction == "short":
                mfe_price = float(df["low"].values[fwd_slice].min())
                mfe_adr = (signal_close - mfe_price) / adr_at_signal
            else:
                mfe_price = float(df["high"].values[fwd_slice].max())
                mfe_adr = (mfe_price - signal_close) / adr_at_signal

            exit_date = str(df["date"].values[bar_idx + exit_bar])[:10]

            results.append({
                **sig,
                "signal_close": round(signal_close, 2),
                "adr_at_signal": round(adr_at_signal, 2),
                "exit_bar": exit_bar,
                "exit_date": exit_date,
                "exit_close": round(exit_close, 2),
                "move_pct": round(move_pct, 2),
                "move_adr": round(move_adr, 2),
                "mfe_adr": round(mfe_adr, 2),
                "capture_eff": round(move_adr / mfe_adr, 3) if mfe_adr > 0 else 0,
            })

        except Exception as e:
            errors += 1
            continue

        if (i + 1) % 50 == 0:
            print(f"    {i + 1}/{len(signals)} processed, {len(results)} with exit")

    print(f"\n  OK: Exit applied: {len(results)} triggered, {no_exit} no exit, {errors} errors")
    return results


# ============================================================
# Phase 4: Filter by example floor + rank
# ============================================================
def exclude_existing_examples(signals, example_signals):
    """
    Remove signals that match existing examples.
    Match by ticker + signal bar within 5 bars of an example's signal bar.
    This ensures the vetting pile only shows NEW potential examples.
    """
    # Build lookup: ticker -> set of signal bar indices from examples
    example_bars = {}
    for ex in example_signals:
        ticker = ex["ticker"]
        bar_idx = ex["signal_bar_idx"]
        if ticker not in example_bars:
            example_bars[ticker] = set()
        # Mark a window around the example signal bar
        for offset in range(-5, 6):
            example_bars[ticker].add(bar_idx + offset)

    before = len(signals)
    filtered = []
    for sig in signals:
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        if ticker in example_bars and bar_idx in example_bars[ticker]:
            continue  # skip -- this is an existing example
        filtered.append(sig)

    removed = before - len(filtered)
    if removed > 0:
        print(f"  OK: Excluded {removed} signals matching existing examples")
    return filtered


def filter_and_rank(results, min_adr, direction):
    """Filter to signals above min_adr threshold, sort by move descending."""
    filtered = [r for r in results if r["move_adr"] >= min_adr]
    filtered.sort(key=lambda r: r["move_adr"], reverse=True)

    print(f"  OK: Filtered: {len(results)} -> {len(filtered)} signals (>= {min_adr:.1f} ADR)")

    if filtered:
        moves = [r["move_adr"] for r in filtered]
        print(f"    Floor: {min(moves):.1f} ADR")
        print(f"    Median: {sorted(moves)[len(moves)//2]:.1f} ADR")
        print(f"    Max: {max(moves):.1f} ADR")
        print(f"    Avg exit bar: {sum(r['exit_bar'] for r in filtered)/len(filtered):.0f}")

    return filtered


# ============================================================
# Phase 5: Deduplicate examples (same logic)
# ============================================================
def deduplicate_examples(examples, cache, conditions, expr_cache):
    """
    For each example, verify it passes all conditions at scan bar using expression cache,
    then record the signal bar. Uses the SAME computation path as the pyramid grinder.
    """
    # Map condition names to cache column indices
    cond_col_indices = []
    for cond in conditions:
        col_idx = expr_cache.expr_index(cond["name"])
        cond_col_indices.append(col_idx)

    results = []
    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        df = cache.get(ticker)
        if df is None:
            print(f"    {ticker}: not in cache, skipping")
            continue

        # Find entry bar
        dates_str = [str(d)[:10] for d in df["date"].values]
        if entry_date not in dates_str:
            print(f"    {ticker}: entry date {entry_date} not found")
            continue

        entry_idx = dates_str.index(entry_date)
        scan_idx = entry_idx - 1  # scan candle is day before entry

        # Load expression cache for this ticker
        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None:
            print(f"    {ticker}: not in expression cache, skipping")
            continue

        # Verify bar count matches
        if len(cached_dates) != len(df):
            print(f"    {ticker}: bar count mismatch (ohlcv={len(df)}, expr_cache={len(cached_dates)})")
            continue

        # Verify ALL conditions pass at scan bar using expression cache
        n_fail = 0
        for i, cond in enumerate(conditions):
            col_idx = cond_col_indices[i]
            if col_idx is None:
                n_fail += 1
                continue
            val = cached_data[scan_idx, col_idx]
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                n_fail += 1
                print(f"    {ticker}: FAIL {cond['name']} = {val:.4f} range [{cond['low']:.4f}, {cond['high']:.4f}]")

        if n_fail > 0:
            print(f"    {ticker}: {n_fail}/{len(conditions)} conditions failed -- GRINDER BUG")
            continue

        signal_date = dates_str[scan_idx]
        signal_close = float(df["close"].values[scan_idx])

        results.append({
            "id": ex.get("id"),
            "ticker": ticker,
            "entry_date": entry_date,
            "signal_date": signal_date,
            "signal_bar_idx": scan_idx,
            "signal_close": round(signal_close, 2),
            "cluster_size": 1,
            "is_example": True,
        })
        print(f"    {ticker}: signal {signal_date} -> entry {entry_date}")

    print(f"  OK: {len(results)}/{len(examples)} examples matched with signal bars")
    return results


# ============================================================
# Output
# ============================================================
def save_results(filtered, example_signals, setup_type, args):
    """Save ranked results for chart vetting."""
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "signal_filter")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "exit_condition": args.get("exit_expr", ""),
        "min_adr_threshold": args.get("min_adr", 0),
        "n_raw_signals": args.get("n_raw", 0),
        "n_deduped": args.get("n_deduped", 0),
        "n_with_exit": args.get("n_with_exit", 0),
        "n_filtered": len(filtered),
        "n_examples_matched": len(example_signals),
        "example_signals": example_signals,
        "signals": filtered,
    }

    # Timestamped
    ts_path = os.path.join(out_dir, f"filtered_{setup_type}_{len(filtered)}sig_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {ts_path}")

    # Latest
    latest_path = os.path.join(out_dir, f"filtered_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {latest_path}")

    # Upload to Railway so vetting UI can read it
    _upload_to_railway(output, setup_type)

    return latest_path


def _upload_to_railway(output, setup_type):
    """Upload filtered signals to Railway for vetting UI."""
    url = f"{RAILWAY_URL}/api/vetting/{setup_type}/upload-signals"
    try:
        print(f"  Uploading to Railway...")
        r = requests.post(url, json=output, timeout=120)
        r.raise_for_status()
        result = r.json()
        n = result.get("n_signals", "?")
        print(f"  ✓ Uploaded {n} signals to Railway")
    except Exception as e:
        print(f"  ⚠ Railway upload failed (vetting UI won't have new signals): {e}")
        print(f"  Manual fallback: python scripts/upload_vetting_data.py --setup {setup_type}")


def _upload_exit_grind_to_railway(setup_type):
    """Upload exit grind JSON to Railway so vetting UI has exit data."""
    exit_path = os.path.join(REPO_ROOT, "data", "signal_exit_grind",
                             f"signal_exit_{setup_type}.json")
    if not os.path.exists(exit_path):
        print(f"  ⚠ No exit grind file to upload: {exit_path}")
        return
    url = f"{RAILWAY_URL}/api/vetting/{setup_type}/upload-exit"
    try:
        with open(exit_path) as f:
            data = json.load(f)
        print(f"  Uploading exit grind to Railway...")
        r = requests.post(url, json=data, timeout=60)
        r.raise_for_status()
        print(f"  ✓ Exit grind uploaded to Railway")
    except Exception as e:
        print(f"  ⚠ Exit grind upload failed: {e}")


def measure_example_exit_distances(example_signals, cache, exit_cond, direction, expr_cache, max_forward=MAX_FORWARD):
    """
    For each deduplicated example signal bar, measure signal close -> exit close in ADR.
    Uses expression cache for exit condition (same computation path).
    """
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]

    exit_col_idx = expr_cache.expr_index(expr_name)
    if exit_col_idx is None:
        print(f"  ERROR: exit expression '{expr_name}' not in expression cache!")
        print(f"  Rebuild cache: python local_runner/expr_cache_builder.py --build --force")
        return 0.0
    adr_col_idx = expr_cache.expr_index("adr14")

    print(f"  Measuring example exit distances from deduplicated signal bars...")

    for ex in example_signals:
        ticker = ex["ticker"]
        bar_idx = ex["signal_bar_idx"]
        df = cache.get(ticker)

        if df is None or bar_idx >= len(df) - 1:
            ex["move_adr"] = None
            ex["error"] = "no data"
            continue

        try:
            # Load expression cache
            cached_dates, cached_data = expr_cache.get_ticker(ticker)
            if cached_dates is None or len(cached_dates) != len(df):
                ex["move_adr"] = None
                ex["error"] = "expr cache mismatch"
                continue

            # ADR at signal bar
            if adr_col_idx is not None:
                adr_at_signal = float(cached_data[bar_idx, adr_col_idx])
            else:
                h = df["high"].values
                l = df["low"].values
                start = max(0, bar_idx - 13)
                adr_at_signal = float(np.mean(h[start:bar_idx+1] - l[start:bar_idx+1]))

            signal_close = float(df["close"].values[bar_idx])

            if adr_at_signal <= 0 or np.isnan(adr_at_signal):
                ex["move_adr"] = None
                ex["error"] = "bad ADR"
                continue

            n_available = len(df) - bar_idx - 1
            actual_forward = min(max_forward, n_available)

            # Get exit series from expression cache
            exit_series = cached_data[:, exit_col_idx]

            # Find first exit bar after signal
            exit_bar = None
            exit_close = None
            for fwd in range(1, actual_forward + 1):
                check_idx = bar_idx + fwd
                val = exit_series[check_idx]
                if np.isnan(val):
                    continue
                if exit_dir == ">=" and val >= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break
                elif exit_dir == "<=" and val <= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break

            if exit_bar is None:
                ex["move_adr"] = None
                ex["exit_bar"] = None
                ex["error"] = "no exit triggered"
                print(f"    {ticker}: NO EXIT within {actual_forward} bars")
                continue

            if direction == "short":
                move_adr = (signal_close - exit_close) / adr_at_signal
            else:
                move_adr = (exit_close - signal_close) / adr_at_signal

            exit_date = str(df["date"].values[bar_idx + exit_bar])[:10]

            ex["adr_at_signal"] = round(adr_at_signal, 2)
            ex["exit_bar"] = exit_bar
            ex["exit_date"] = exit_date
            ex["exit_close"] = round(exit_close, 2)
            ex["move_adr"] = round(move_adr, 2)

            print(f"    {ticker}: signal {ex['signal_date']} -> exit {exit_date} "
                  f"= {move_adr:.1f} ADR ({exit_bar} bars)")

        except Exception as e:
            ex["move_adr"] = None
            ex["error"] = str(e)

    # Compute floor from examples that had valid exits
    valid_adrs = [ex["move_adr"] for ex in example_signals if ex.get("move_adr") is not None]
    if valid_adrs:
        floor = min(valid_adrs)
        median = sorted(valid_adrs)[len(valid_adrs) // 2]
        print(f"\n  OK: Example exit distances (from deduped signal bars):")
        print(f"    {len(valid_adrs)}/{len(example_signals)} examples had valid exits")
        print(f"    Floor: {floor:.1f} ADR")
        print(f"    Median: {median:.1f} ADR")
        print(f"    Mean: {sum(valid_adrs)/len(valid_adrs):.1f} ADR")
        return floor
    else:
        print(f"  WARNING: No examples had valid exit distances!")
        return 0.0


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Signal Filter -- Dedup + Exit + Rank")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--min-adr", type=float, default=None,
                        help="Min exit distance in ADR (default: derived from examples)")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--conditions-file", default=None,
                        help="Path to conditions JSON file — load conditions directly "
                             "instead of auto-discovering latest pyramid result. "
                             "Accepts pyramid result files or condition_pruner output.")
    args = parser.parse_args()

    setup = args.setup
    config = SETUP_CONFIGS.get(setup, {"direction": "short"})
    direction = config["direction"]

    print(f"\n{'='*60}")
    print(f"  SIGNAL FILTER -- {setup.upper()}")
    print(f"{'='*60}")
    t0 = time.time()

    # Load data
    cache = load_5yr_cache()
    conditions = load_pyramid_conditions(setup, conditions_file=args.conditions_file)
    exit_cond = load_exit_condition(setup)
    examples = load_examples(setup)

    # Load expression cache -- SAME computation path as pyramid grinder
    print(f"  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache not found or invalid.")
        print("  Run: python local_runner/expr_cache_builder.py --build")
        sys.exit(1)
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # Phase 1: Deduplicate examples FIRST -- need their exit distances for the floor
    print(f"\n  PHASE 1: Deduplicate example signal bars")
    example_signals = deduplicate_examples(examples, cache, conditions, expr_cache)

    # Phase 2: Measure example exit distances from deduplicated signal bars
    print(f"\n  PHASE 2: Measure example exit distances")
    example_floor = measure_example_exit_distances(
        example_signals, cache, exit_cond, direction, expr_cache, args.max_forward)

    if args.min_adr is not None:
        min_adr = args.min_adr
    else:
        # 10% wiggle below example floor — don't cut right at the dot
        min_adr = round(example_floor * 0.9, 1)
    print(f"\n  ADR floor from examples: {example_floor:.1f}")
    print(f"  Using filter threshold: {min_adr:.1f} ADR (90% of floor)")

    # Phase 3: Scan all backtest signals
    print(f"\n  PHASE 3: Scan all signals")
    raw_signals = scan_all_signals(cache, conditions, args.workers, expr_cache)

    # Phase 4: Deduplicate backtest signals
    print(f"\n  PHASE 4: Deduplicate (consecutive -> rightmost)")
    deduped = deduplicate_signals(raw_signals)

    # Phase 5: Apply exit + measure
    print(f"\n  PHASE 5: Apply exit condition + measure distance")
    with_exit = apply_exit_and_measure(deduped, cache, exit_cond, direction, expr_cache, args.max_forward)

    # Phase 6: Exclude existing examples
    print(f"\n  PHASE 6: Exclude existing examples")
    new_signals = exclude_existing_examples(with_exit, example_signals)

    # Phase 7: Filter + rank
    print(f"\n  PHASE 7: Filter + rank (>= {min_adr:.1f} ADR)")
    filtered = filter_and_rank(new_signals, min_adr, direction)

    # Save
    print(f"\n  SAVING RESULTS")
    save_results(filtered, example_signals, setup, {
        "exit_expr": f"{exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}",
        "min_adr": min_adr,
        "example_floor_adr": example_floor,
        "n_raw": len(raw_signals),
        "n_deduped": len(deduped),
        "n_with_exit": len(with_exit),
    })

    # Also upload exit grind to Railway (vetting UI needs it)
    _upload_exit_grind_to_railway(setup)

    total_time = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  Examples: {len(example_signals)} deduped, floor {example_floor:.1f} ADR")
    print(f"  Signals: {len(raw_signals):,} raw -> {len(deduped):,} deduped -> "
          f"{len(with_exit):,} with exit -> {len(filtered):,} filtered")
    print(f"  Ready for chart vetting in data/signal_filter/")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
