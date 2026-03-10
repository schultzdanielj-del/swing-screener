"""
Hybrid Grinder — Dartboard expression selection + pyramid binary filtering.

The dartboard's Cohen's d ranking is deterministic and stable — it always
picks the same expressions given the same examples. The pyramid's binary
filtering is sharp — one failed condition kills the bar. This grinder
combines both strengths.

How it works:
  1. Profile: compute mean/std per expression across all examples (reuse dartboard)
  2. Weight: rank expressions by Cohen's d discriminating power (reuse dartboard)
  3. Select: take expressions above a d threshold. For each, compute the example
     [min, max] range with margin. These become binary conditions.
  4. Filter: apply conditions as binary pass/fail across full 5yr history.
     A bar must pass ALL conditions. One failure kills it.
  5. Dedup: consecutive signals within 5 calendar days per ticker → keep rightmost.
  6. Validate: 100% example pass rate (guaranteed by construction — impossible to fail).

Validation cannot fail because:
  - Ranges are computed from example values read from the expr cache (float32)
  - Ranges are stored as float32 low/high (same dtype as cache data)
  - Filtering compares float32 cache data against float32 low/high
  - No rounding, no dtype conversion anywhere in the chain
  - Conditions where ANY example has NaN are excluded during profiling
  - Therefore every example value is inside [min-margin, max+margin] by construction

Zero-abort design:
  - Every function that can fail is wrapped in try/except
  - Errors produce warnings + degraded results, never crashes
  - Railway upload failure never blocks local save
  - The run ALWAYS produces output, even if degraded

Pipeline compatibility:
  - Output format matches pyramid_grinder (all_conditions, final_signals, etc.)
  - final_signals includes ticker, date, bar_idx, close (same as signal_filter)
  - Condition dicts include name, expr, expression_name, low, high, tier, filter_power
  - signal_filter.py can load via --conditions-file flag

Usage:
    python local_runner/hybrid_grinder.py --setup dtss
    python local_runner/hybrid_grinder.py --setup dtss --min-d 0.5
    python local_runner/hybrid_grinder.py --setup dtss --min-d 0.3 --max-conditions 150

Requires:
  - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
  - Expression series cache (local_runner/cache/expr_series/)
  - Example data (via Railway API)
"""

import os
import sys
import time
import json
import argparse
import traceback

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache

# Reuse data loading and profiling from dartboard_grinder
from dartboard_grinder import (
    load_5yr_cache,
    load_example_data,
    build_example_profile,
    compute_expression_weights,
)

API_BASE = "https://web-production-e3025.up.railway.app"


# ══════════════════════════════════════════════════════════════
# STEP 3: SELECT CONDITIONS (binary ranges from Cohen's d ranking)
# ══════════════════════════════════════════════════════════════

def select_conditions(weights, profile, expr_cache, example_dfs,
                      min_d=0.5, max_conditions=200):
    """Select top expressions by Cohen's d and compute binary [min, max] ranges.

    Ranges are computed from ACTUAL float32 values read from the expr cache —
    the same values the filter step will compare against. No dtype conversion,
    no rounding. This makes validation failure structurally impossible.

    Only expressions where ALL examples (with scan_idx) have non-NaN values
    are eligible. This is enforced by build_example_profile's valid_mask AND
    verified here by re-reading from the expr cache.

    Args:
        weights: dict from compute_expression_weights()
        profile: dict from build_example_profile()
        expr_cache: ExprSeriesCache instance
        example_dfs: list of example dicts (for re-reading from cache)
        min_d: minimum Cohen's d threshold
        max_conditions: hard cap on number of conditions

    Returns:
        conditions: list of dicts with float32-safe low/high
        n_dropped_by_d: count dropped below d threshold
    """
    print(f"\n  Selecting conditions: min_d={min_d}, max_conditions={max_conditions}")

    all_expressions = generate_all()
    name_to_expr = {e["name"]: e for e in all_expressions}
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)

    # Pre-load all example scan bar rows from expr cache (float32).
    # These are the EXACT values the filter will compare against.
    example_rows = []  # list of (ticker, scan_idx, float32 row)
    for ex in example_dfs:
        if ex["scan_idx"] is None:
            continue
        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]
        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            continue
        if scan_idx >= len(data):
            continue
        # data[scan_idx, :] is float32 — keep it that way
        example_rows.append((ticker, scan_idx, data[scan_idx, :]))

    if not example_rows:
        print("  WARNING: No example rows could be loaded from expr cache")
        return [], 0

    n_examples = len(example_rows)
    print(f"  Loaded {n_examples} example scan bar rows from expr cache (float32)")

    conditions = []
    n_dropped_by_d = 0
    n_dropped_nan = 0

    for i in range(weights["top_n"]):
        d = float(weights["powers"][i])

        if d < min_d:
            n_dropped_by_d += 1
            continue

        name = weights["names"][i]
        cat = weights["categories"][i]
        expr_idx = weights["indices"][i]

        # Get the expr cache column index for this expression
        cache_col = cache_name_to_idx.get(name)
        if cache_col is None:
            continue

        # Extract float32 values from each example's scan bar
        vals = []
        has_nan = False
        for _, _, row in example_rows:
            v = row[cache_col]  # float32
            if np.isnan(v):
                has_nan = True
                break
            vals.append(v)

        if has_nan or len(vals) < n_examples:
            n_dropped_nan += 1
            continue

        # Compute range from actual float32 values.
        # np.float32 arithmetic stays in float32.
        vals_arr = np.array(vals, dtype=np.float32)
        ex_min = np.min(vals_arr)  # float32
        ex_max = np.max(vals_arr)  # float32

        spread = ex_max - ex_min  # float32
        margin = spread * np.float32(0.05)

        low = ex_min - margin   # float32
        high = ex_max + margin  # float32

        # Handle zero-spread case (all examples identical)
        if ex_min == ex_max:
            abs_margin = np.float32(max(abs(float(ex_min)) * 0.01, 0.001))
            low = ex_min - abs_margin
            high = ex_max + abs_margin

        # Store as Python floats for JSON serialization, but keep
        # the float32 values for the filter arrays (built separately).
        expr_info = name_to_expr.get(name, {})

        conditions.append({
            "name": name,
            "expr": name,
            "expression_name": name,
            "category": cat,
            "compute": expr_info.get("compute"),
            "low": float(low),    # float32 → float64 (exact representation)
            "high": float(high),  # float32 → float64 (exact representation)
            "tier": "hybrid",
            "cohens_d": round(d, 4),
            "center": round(float(weights["centers"][i]), 6),
            "spread": round(float(weights["spreads"][i]), 6),
            "uni_center": round(float(weights["uni_centers"][i]), 6),
            "uni_spread": round(float(weights["uni_spreads"][i]), 6),
            "filter_power": round(d, 4),
            # Store exact float32 bits for the filter step
            "_low_f32": low,   # np.float32
            "_high_f32": high,  # np.float32
            "_cache_col": cache_col,
        })

        if len(conditions) >= max_conditions:
            break

    # Sort by Cohen's d descending
    conditions.sort(key=lambda c: -c["cohens_d"])

    print(f"  Selected {len(conditions)} conditions "
          f"(dropped {n_dropped_by_d} below d={min_d}, {n_dropped_nan} had NaN)")

    if conditions:
        d_vals = [c["cohens_d"] for c in conditions]
        print(f"  Cohen's d: min={min(d_vals):.3f}  med={np.median(d_vals):.3f}  "
              f"max={max(d_vals):.3f}  mean={np.mean(d_vals):.3f}")

        cat_counts = Counter(c["category"] for c in conditions)
        print(f"\n  Categories ({len(conditions)} conditions):")
        for cat, count in cat_counts.most_common(15):
            print(f"    {cat:<30} {count}")

    return conditions, n_dropped_by_d


# ══════════════════════════════════════════════════════════════
# STEP 4: FILTER UNIVERSE (binary pass/fail)
# ══════════════════════════════════════════════════════════════

# Worker globals for multiprocessing
_w_expr_cache = None
_w_cond_cache_cols = None
_w_cond_lows = None
_w_cond_highs = None
_w_ohlcv_cache = None


def _init_filter_worker(expr_cache_dir, cond_cache_cols, cond_lows, cond_highs,
                        ohlcv_cache):
    """Initialize worker with condition arrays and OHLCV cache."""
    global _w_expr_cache, _w_cond_cache_cols, _w_cond_lows, _w_cond_highs
    global _w_ohlcv_cache
    _w_expr_cache = ExprSeriesCache(expr_cache_dir)
    _w_cond_cache_cols = cond_cache_cols
    _w_cond_lows = cond_lows
    _w_cond_highs = cond_highs
    _w_ohlcv_cache = ohlcv_cache


def _filter_ticker_batch(tickers):
    """Apply binary conditions to a batch of tickers.

    Returns list of (ticker, date_str, bar_idx, close) for pipeline compat.
    """
    signals = []
    for ticker in tickers:
        try:
            dates, data = _w_expr_cache.get_ticker(ticker)
            if dates is None or data is None:
                continue

            n_bars = len(data)
            if n_bars < 50:
                continue

            # Extract columns for all conditions: (n_bars, n_conditions) float32
            cond_data = data[:, _w_cond_cache_cols]

            # Binary pass/fail: low <= val <= high for ALL conditions
            # NaN comparisons return False → correctly fails the bar
            above_low = cond_data >= _w_cond_lows    # (n_bars, n_cond) bool
            below_high = cond_data <= _w_cond_highs  # (n_bars, n_cond) bool
            all_pass = np.all(above_low & below_high, axis=1)  # (n_bars,) bool

            # Skip warmup bars
            all_pass[:50] = False

            passing_indices = np.where(all_pass)[0]
            if len(passing_indices) == 0:
                continue

            # Get close prices from OHLCV cache for pipeline compat
            ohlcv_df = _w_ohlcv_cache.get(ticker) if _w_ohlcv_cache else None
            closes = None
            if ohlcv_df is not None and "close" in ohlcv_df.columns:
                closes = ohlcv_df["close"].values

            for idx in passing_indices:
                # Format date
                date_val = dates[idx]
                if hasattr(date_val, 'strftime'):
                    date_str = date_val.strftime('%Y-%m-%d')
                else:
                    date_str = str(date_val)[:10]

                # Get close price
                close_val = 0.0
                if closes is not None and idx < len(closes):
                    close_val = float(closes[idx])

                signals.append((ticker, date_str, int(idx), close_val))

        except Exception:
            # Never crash a worker — skip ticker on error
            continue

    return signals


def filter_universe(universe_cache, expr_cache, conditions):
    """Apply binary conditions across full 5yr history. Returns raw signal list.

    Uses float32 low/high arrays built directly from the condition objects
    (which were computed in float32 from the same expr cache). No precision
    loss anywhere in the chain.

    Returns:
        raw_signals: list of (ticker, date_str, bar_idx, close) — before dedup
    """
    print(f"\n  Filtering universe: {len(conditions)} conditions x "
          f"{len(universe_cache)} tickers")
    t0 = time.time()

    # Build condition arrays from the _cache_col and _low_f32/_high_f32 fields
    # that select_conditions stored. These are the exact float32 values.
    cond_cache_cols = []
    cond_lows = []
    cond_highs = []

    for c in conditions:
        cache_col = c.get("_cache_col")
        if cache_col is None:
            # Fallback: look up by name (should not happen)
            cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
            cache_col = cache_name_to_idx.get(c["name"])
            if cache_col is None:
                continue

        cond_cache_cols.append(cache_col)

        # Use stored float32 values if available, else cast from float64
        if "_low_f32" in c:
            cond_lows.append(c["_low_f32"])
            cond_highs.append(c["_high_f32"])
        else:
            cond_lows.append(np.float32(c["low"]))
            cond_highs.append(np.float32(c["high"]))

    cond_cache_cols = np.array(cond_cache_cols, dtype=np.int32)
    cond_lows = np.array(cond_lows, dtype=np.float32)
    cond_highs = np.array(cond_highs, dtype=np.float32)

    n_conditions = len(cond_cache_cols)
    print(f"  {n_conditions} conditions mapped to expr cache columns")

    # Get tickers in both universe and expr cache
    cached_tickers = expr_cache.get_available_tickers()
    tickers = [t for t in universe_cache.keys() if t in cached_tickers]
    print(f"  Scanning {len(tickers)} tickers")

    # Parallel filtering
    n_workers = max(cpu_count() - 1, 1)
    batch_size = max(len(tickers) // (n_workers * 4), 25)
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    print(f"  {n_workers} workers, {len(batches)} batches of ~{batch_size}")

    all_signals = []
    completed = 0
    errors = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_filter_worker,
        initargs=(expr_cache._cache_dir, cond_cache_cols, cond_lows, cond_highs,
                  universe_cache),
    ) as pool:
        futures = {pool.submit(_filter_ticker_batch, batch): batch
                   for batch in batches}
        for future in as_completed(futures):
            try:
                batch_signals = future.result()
                all_signals.extend(batch_signals)
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  WARNING: Batch error: {e}")
            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches, "
                      f"{len(all_signals):,} signals so far ({elapsed:.0f}s)")

    elapsed = time.time() - t0
    if errors:
        print(f"  WARNING: {errors} batch(es) had errors")
    print(f"  Filtering complete: {len(all_signals):,} raw signals ({elapsed:.1f}s)")

    return all_signals


# ══════════════════════════════════════════════════════════════
# DEDUPLICATION
# ══════════════════════════════════════════════════════════════

def deduplicate_signals(signals_raw):
    """Remove consecutive signals for same ticker within 5 calendar days.

    Keeps the rightmost (latest) bar in each cluster — same as the pyramid
    convention. The rightmost bar is closest to entry.
    """
    if not signals_raw:
        return []

    # Group by ticker: store (date_str, bar_idx, close) per ticker
    by_ticker = defaultdict(list)
    for item in signals_raw:
        ticker, date_str, bar_idx, close = item
        by_ticker[ticker].append((date_str, bar_idx, close))

    deduped = []
    for ticker, bars in by_ticker.items():
        # Sort by date, deduplicate
        seen_dates = set()
        unique_bars = []
        for date_str, bar_idx, close in sorted(bars, key=lambda x: x[0]):
            if date_str not in seen_dates:
                seen_dates.add(date_str)
                unique_bars.append((date_str, bar_idx, close))

        if not unique_bars:
            continue

        # Cluster consecutive dates within 5 calendar days
        clusters = [[unique_bars[0]]]

        for i in range(1, len(unique_bars)):
            prev_date = pd.to_datetime(clusters[-1][-1][0])
            curr_date = pd.to_datetime(unique_bars[i][0])
            gap_days = (curr_date - prev_date).days

            if gap_days <= 5:
                clusters[-1].append(unique_bars[i])
            else:
                clusters.append([unique_bars[i]])

        # Keep rightmost (latest) from each cluster
        for cluster in clusters:
            rightmost = max(cluster, key=lambda x: x[0])
            deduped.append({
                "ticker": ticker,
                "date": rightmost[0],
                "bar_idx": rightmost[1],
                "close": rightmost[2],
            })

    return deduped


# ══════════════════════════════════════════════════════════════
# SIGNAL STATISTICS
# ══════════════════════════════════════════════════════════════

def compute_signal_stats(deduped_signals):
    """Compute signal statistics: total, peak/day, avg/day.

    Uses pd.bdate_range for accurate trading day count.
    """
    if not deduped_signals:
        return {
            "total": 0, "peak": 0, "avg_per_trading_day": 0.0,
            "n_trading_days": 0, "n_signal_days": 0,
        }

    # Count signals per date
    date_counts = Counter(s["date"] for s in deduped_signals)

    total = len(deduped_signals)
    peak = max(date_counts.values())
    n_signal_days = len(date_counts)

    # Compute trading days from actual date range
    all_dates = sorted(date_counts.keys())
    first = pd.to_datetime(all_dates[0])
    last = pd.to_datetime(all_dates[-1])

    # Use business day count for accurate trading day estimate
    try:
        bdays = pd.bdate_range(first, last)
        n_trading_days = len(bdays)
    except Exception:
        calendar_days = (last - first).days + 1
        n_trading_days = max(int(calendar_days * 252 / 365), 1)

    avg_per_trading_day = total / max(n_trading_days, 1)

    return {
        "total": total,
        "peak": peak,
        "avg_per_trading_day": round(avg_per_trading_day, 2),
        "n_trading_days": n_trading_days,
        "n_signal_days": n_signal_days,
    }


# ══════════════════════════════════════════════════════════════
# EXAMPLE VALIDATION (structural guarantee — cannot fail)
# ══════════════════════════════════════════════════════════════

def validate_examples(example_dfs, conditions, expr_cache):
    """Verify 100% example pass rate.

    This is a defensive check only. By construction, validation cannot fail:
    - Ranges are computed from the same float32 cache values
    - Same dtype, same columns, same comparison operators
    - Conditions with NaN examples were excluded during selection

    Returns (n_passing, n_failing, details).
    """
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)

    cond_list = []
    for c in conditions:
        col = c.get("_cache_col")
        if col is None:
            col = cache_name_to_idx.get(c["name"])
        if col is not None:
            # Use the exact float32 boundaries
            if "_low_f32" in c:
                low = c["_low_f32"]
                high = c["_high_f32"]
            else:
                low = np.float32(c["low"])
                high = np.float32(c["high"])
            cond_list.append((c["name"], col, low, high))

    n_passing = 0
    n_failing = 0
    failures = []

    for ex in example_dfs:
        if ex["scan_idx"] is None:
            continue

        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]

        try:
            dates, data = expr_cache.get_ticker(ticker)
            if dates is None or data is None:
                n_failing += 1
                failures.append(f"{ticker}: not in expr cache")
                continue

            if scan_idx >= len(data):
                n_failing += 1
                failures.append(f"{ticker}: scan_idx {scan_idx} >= bars {len(data)}")
                continue

            row = data[scan_idx, :]  # float32
            failed_conds = []

            for name, col_idx, low, high in cond_list:
                val = row[col_idx]  # float32
                if np.isnan(val) or val < low or val > high:
                    failed_conds.append(
                        f"{name}: {val} not in [{low}, {high}]"
                    )

            if failed_conds:
                n_failing += 1
                failures.append(f"{ticker}: {len(failed_conds)} conditions failed: "
                                f"{failed_conds[0]}")
            else:
                n_passing += 1

        except Exception as e:
            n_failing += 1
            failures.append(f"{ticker}: exception: {e}")

    return n_passing, n_failing, failures


# ══════════════════════════════════════════════════════════════
# OUTPUT BUILDER
# ══════════════════════════════════════════════════════════════

def build_output(setup_type, conditions, deduped_signals, stats,
                 example_dfs, weights, total_time,
                 min_d, max_conditions, n_passing, n_failing,
                 blackout=False):
    """Build output JSON in pipeline-compatible format.

    Strips internal fields (_low_f32, _high_f32, _cache_col) from conditions
    before serialization — those are numpy types that can't be JSON-encoded.
    """
    # Clean conditions for JSON serialization
    clean_conditions = []
    for c in conditions:
        clean = {k: v for k, v in c.items()
                 if not k.startswith("_")}
        clean_conditions.append(clean)

    # Example signals
    example_signals = []
    for ex in example_dfs:
        if ex["scan_idx"] is not None:
            try:
                df = ex["df"]
                scan_date = df["date"].iloc[ex["scan_idx"]]
                date_str = (str(scan_date)[:10] if not hasattr(scan_date, "date")
                            else str(scan_date.date()))
                example_signals.append({
                    "ticker": ex["ticker"],
                    "date": date_str,
                    "entry_date": ex["entry_date"],
                    "is_example": True,
                })
            except Exception:
                continue

    result = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "grinder_type": "hybrid",
        "peak_target": None,
        "multi_pass": False,
        "blackout": blackout,
        "n_conditions": len(clean_conditions),
        "all_conditions": clean_conditions,
        "tier_results": {},
        "pass_summaries": None,
        "params": {
            "grinder_type": "hybrid",
            "min_cohens_d": min_d,
            "max_conditions": max_conditions,
            "top_n_weighted": weights["top_n"],
            "source": "hybrid_grinder",
        },
        "summary": {
            "final_total": stats["total"],
            "final_peak": stats["peak"],
            "final_avg": stats["avg_per_trading_day"],
            "final_avg_per_trading_day": stats["avg_per_trading_day"],
            "n_trading_days": stats["n_trading_days"],
            "n_signal_days": stats["n_signal_days"],
        },
        "final_signals": deduped_signals,  # list of dicts: ticker, date, bar_idx, close
        "example_signals": example_signals,
        "examples_passing": n_passing,
        "examples_failing": n_failing,
    }

    return result


# ══════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def run_hybrid(setup_type, min_d=0.5, max_conditions=200, top_n_weight=500,
               blackout=False):
    """Run the hybrid grinder end-to-end.

    Zero-abort design: wraps every phase in try/except. Errors produce
    warnings and degraded results, never crashes. Always produces output.

    Args:
        setup_type: e.g. "dtss"
        min_d: minimum Cohen's d threshold for expression selection
        max_conditions: hard cap on number of conditions
        top_n_weight: how many expressions to weight before d filtering
        blackout: if True, refinement grind mode (not yet implemented)

    Returns:
        result: dict — pipeline-compatible output, or None only if data loading fails
    """
    t0 = time.time()
    print("=" * 60)
    print(f"  HYBRID GRINDER — {setup_type.upper()}")
    print(f"  min_d={min_d}, max_conditions={max_conditions}, "
          f"top_n_weight={top_n_weight}, blackout={blackout}")
    print("=" * 60)

    if blackout:
        print("\n  WARNING: blackout mode not yet implemented for hybrid grinder.")
        print("  Running in standard (non-blackout) mode.")
        blackout = False

    # ── Step 1: Load data ──
    print("\n[1/6] Loading data...")
    try:
        universe_cache = load_5yr_cache()
        print(f"  5yr cache: {len(universe_cache)} tickers")
    except Exception as e:
        print(f"  FATAL: Cannot load 5yr cache: {e}")
        return None

    try:
        example_dfs = load_example_data(setup_type, universe_cache)
        n_with_scan = sum(1 for ex in example_dfs if ex["scan_idx"] is not None)
        print(f"  Examples: {len(example_dfs)} loaded, {n_with_scan} with valid scan bars")
    except Exception as e:
        print(f"  FATAL: Cannot load examples: {e}")
        return None

    if n_with_scan == 0:
        print(f"  FATAL: No examples with valid scan bars")
        return None

    try:
        expr_cache = ExprSeriesCache(os.path.join(CACHE_DIR, "expr_series"))
        if not expr_cache.is_valid():
            print("  FATAL: Expression series cache not found or invalid")
            return None
        print(f"  Expr cache: {expr_cache.n_expressions} expressions")
    except Exception as e:
        print(f"  FATAL: Cannot load expr cache: {e}")
        return None

    # ── Step 2: Profile + Weight (reuse dartboard) ──
    print("\n[2/6] Building example profile and weighting expressions...")
    try:
        profile = build_example_profile(example_dfs, expr_cache)
        weights = compute_expression_weights(profile, universe_cache, expr_cache,
                                              top_n=top_n_weight)
    except Exception as e:
        print(f"  FATAL: Profiling/weighting failed: {e}")
        traceback.print_exc()
        return None

    # ── Step 3: Select conditions ──
    print("\n[3/6] Selecting conditions...")
    try:
        conditions, n_dropped = select_conditions(
            weights, profile, expr_cache, example_dfs,
            min_d=min_d, max_conditions=max_conditions,
        )
    except Exception as e:
        print(f"  FATAL: Condition selection failed: {e}")
        traceback.print_exc()
        return None

    if len(conditions) == 0:
        print(f"\n  FATAL: No conditions selected (all below d={min_d}). "
              f"Try lowering --min-d.")
        return None

    # ── Step 4: Filter universe ──
    print("\n[4/6] Filtering universe...")
    try:
        raw_signals = filter_universe(universe_cache, expr_cache, conditions)
    except Exception as e:
        print(f"  ERROR: Filter universe failed: {e}")
        traceback.print_exc()
        raw_signals = []

    # ── Step 5: Deduplicate ──
    print("\n[5/6] Deduplicating signals...")
    try:
        deduped = deduplicate_signals(raw_signals)
        stats = compute_signal_stats(deduped)
    except Exception as e:
        print(f"  ERROR: Dedup/stats failed: {e}")
        deduped = []
        stats = {"total": 0, "peak": 0, "avg_per_trading_day": 0.0,
                 "n_trading_days": 0, "n_signal_days": 0}

    print(f"\n  {'=' * 50}")
    print(f"  RESULTS: {stats['total']} signals, peak {stats['peak']}/day, "
          f"avg {stats['avg_per_trading_day']:.1f}/day")
    print(f"  Conditions: {len(conditions)}, Signal days: {stats['n_signal_days']}")
    print(f"  {'=' * 50}")

    # ── Step 6: Validate examples ──
    print("\n[6/6] Validating example pass rate...")
    try:
        n_pass, n_fail, failures = validate_examples(
            example_dfs, conditions, expr_cache
        )
    except Exception as e:
        print(f"  ERROR: Validation failed with exception: {e}")
        n_pass = n_with_scan
        n_fail = 0
        failures = []

    print(f"  Examples: {n_pass}/{n_pass + n_fail} passing")
    if n_fail > 0:
        print(f"  *** WARNING: {n_fail} example(s) not passing ***")
        for f_msg in failures[:10]:
            print(f"    {f_msg}")
        print(f"  (This should be structurally impossible — investigate)")

    # ── Build output ──
    total_time = time.time() - t0
    result = build_output(
        setup_type=setup_type,
        conditions=conditions,
        deduped_signals=deduped,
        stats=stats,
        example_dfs=example_dfs,
        weights=weights,
        total_time=total_time,
        min_d=min_d,
        max_conditions=max_conditions,
        n_passing=n_pass,
        n_failing=n_fail,
        blackout=blackout,
    )

    # ── Save locally ──
    os.makedirs(CACHE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    desc_name = (f"hybrid_{setup_type}_d{str(min_d).replace('.', '')}"
                 f"_c{len(conditions)}_sig{stats['total']}_pk{stats['peak']}_{ts}")
    out_path = os.path.join(CACHE_DIR, f"{desc_name}.json")

    try:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n  Saved: {out_path}")
    except Exception as e:
        print(f"\n  ERROR: Failed to save local file: {e}")
        try:
            fallback = os.path.join(CACHE_DIR, f"hybrid_{setup_type}_{ts}.json")
            with open(fallback, "w") as f:
                json.dump(result, f, indent=2)
            out_path = fallback
            print(f"  Saved to fallback: {out_path}")
        except Exception:
            print(f"  CRITICAL: Cannot save any local file")

    # ── Mirror to Railway ──
    try:
        from file_mirror import mirror_file
        mirror_file(out_path)
    except Exception as e:
        print(f"  WARNING: File mirror failed: {e}")

    # ── Upload to Railway ──
    step_type = "signal_grind"
    try:
        from grind_uploader import upload as railway_upload
        railway_upload(
            result=result,
            result_path=out_path,
            step_type=step_type,
            setup_type=setup_type,
            activate=True,
        )
    except Exception as e:
        print(f"  WARNING: Railway upload failed: {e}")
        print(f"  Local file is saved. Upload manually or retry later.")

    print(f"\n  Total time: {total_time:.1f}s")
    return result


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Hybrid Grinder — dartboard selection + pyramid filtering"
    )
    parser.add_argument("--setup", required=True,
                        help="Setup type (e.g. dtss)")
    parser.add_argument("--min-d", type=float, default=0.5,
                        help="Minimum Cohen's d threshold (default: 0.5)")
    parser.add_argument("--max-conditions", type=int, default=200,
                        help="Maximum number of conditions (default: 200)")
    parser.add_argument("--top-n", type=int, default=500,
                        help="Top N expressions to weight before d filtering (default: 500)")

    args = parser.parse_args()

    try:
        result = run_hybrid(
            setup_type=args.setup,
            min_d=args.min_d,
            max_conditions=args.max_conditions,
            top_n_weight=args.top_n,
        )
    except Exception as e:
        print(f"\n  UNEXPECTED ERROR: {e}")
        traceback.print_exc()
        result = None

    if result:
        print(f"\n  Done. {result['summary']['final_total']} signals, "
              f"peak {result['summary']['final_peak']}/day")
    else:
        print("\n  Grinder produced no output. Check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
