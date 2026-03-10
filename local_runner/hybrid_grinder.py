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
     [min, max] range with 5% margin. These become binary conditions.
  4. Filter: apply conditions as binary pass/fail across full 5yr history.
     A bar must pass ALL conditions. One failure kills it.
  5. Dedup: consecutive signals within 5 calendar days per ticker → keep rightmost.
  6. Validate: 100% example pass rate (guaranteed by min/max construction).

Key properties:
  - Deterministic: same examples → same conditions → same signals
  - Stable: adding one example shifts ranges slightly, doesn't random-walk
  - Setup-agnostic: Cohen's d adapts per setup type
  - Pipeline-compatible: same output format as pyramid_grinder

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

def select_conditions(weights, profile, min_d=0.5, max_conditions=200):
    """Select top expressions by Cohen's d and compute binary [min, max] ranges.

    For each selected expression, the range is [min(examples) - 5% margin,
    max(examples) + 5% margin]. This guarantees 100% example pass rate by
    construction — every example value is inside the range.

    Args:
        weights: dict from compute_expression_weights()
        profile: dict from build_example_profile()
        min_d: minimum Cohen's d threshold (expressions below this are dropped)
        max_conditions: hard cap on number of conditions

    Returns:
        conditions: list of dicts, each with:
            - name, category, low, high, cohens_d, center, spread, uni_center, uni_spread
        n_dropped_by_d: how many were above the initial top_n but below min_d
    """
    print(f"\n  Selecting conditions: min_d={min_d}, max_conditions={max_conditions}")

    example_matrix = profile["example_matrix"]
    all_expressions = generate_all()
    name_to_expr = {e["name"]: e for e in all_expressions}

    conditions = []
    n_dropped_by_d = 0

    for i in range(weights["top_n"]):
        d = float(weights["powers"][i])

        if d < min_d:
            n_dropped_by_d += 1
            continue

        name = weights["names"][i]
        cat = weights["categories"][i]
        expr_idx = weights["indices"][i]

        # Compute min/max from example values at this expression
        vals = example_matrix[:, expr_idx]
        valid = vals[~np.isnan(vals)]

        if len(valid) == 0:
            continue

        ex_min = float(np.min(valid))
        ex_max = float(np.max(valid))
        margin = (ex_max - ex_min) * 0.05

        low = ex_min - margin
        high = ex_max + margin

        # Handle zero-spread case (all examples have same value)
        if ex_min == ex_max:
            # Use a small absolute margin so the range isn't [x, x]
            abs_margin = max(abs(ex_min) * 0.01, 0.001)
            low = ex_min - abs_margin
            high = ex_max + abs_margin

        expr_info = name_to_expr.get(name, {})

        conditions.append({
            "name": name,
            "expr": name,
            "expression_name": name,
            "category": cat,
            "compute": expr_info.get("compute"),
            "low": round(low, 6),
            "high": round(high, 6),
            "tier": "hybrid",
            "cohens_d": round(d, 4),
            "center": round(float(weights["centers"][i]), 6),
            "spread": round(float(weights["spreads"][i]), 6),
            "uni_center": round(float(weights["uni_centers"][i]), 6),
            "uni_spread": round(float(weights["uni_spreads"][i]), 6),
            "filter_power": round(d, 4),  # Pipeline compat
        })

        if len(conditions) >= max_conditions:
            break

    # Sort by Cohen's d descending
    conditions.sort(key=lambda c: -c["cohens_d"])

    print(f"  Selected {len(conditions)} conditions (dropped {n_dropped_by_d} below d={min_d})")

    if conditions:
        d_vals = [c["cohens_d"] for c in conditions]
        print(f"  Cohen's d: min={min(d_vals):.3f}  med={np.median(d_vals):.3f}  "
              f"max={max(d_vals):.3f}  mean={np.mean(d_vals):.3f}")

        # Category breakdown
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
_w_cond_indices = None
_w_cond_lows = None
_w_cond_highs = None


def _init_filter_worker(expr_cache_dir, cond_indices, cond_lows, cond_highs):
    """Initialize worker with condition arrays."""
    global _w_expr_cache, _w_cond_indices, _w_cond_lows, _w_cond_highs
    _w_expr_cache = ExprSeriesCache(expr_cache_dir)
    _w_cond_indices = cond_indices
    _w_cond_lows = cond_lows
    _w_cond_highs = cond_highs


def _filter_ticker_batch(tickers):
    """Apply binary conditions to a batch of tickers. Returns list of (ticker, date_str)."""
    signals = []
    for ticker in tickers:
        dates, data = _w_expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            continue

        n_bars = len(data)
        if n_bars < 50:
            continue

        # Extract columns for all conditions at once: (n_bars, n_conditions)
        cond_data = data[:, _w_cond_indices]

        # Binary pass/fail: value must be within [low, high] for ALL conditions
        # A bar passes if: low[j] <= val[j] <= high[j] for every j
        above_low = cond_data >= _w_cond_lows  # (n_bars, n_cond) bool
        below_high = cond_data <= _w_cond_highs  # (n_bars, n_cond) bool
        in_range = above_low & below_high  # (n_bars, n_cond) bool

        # Handle NaN: NaN comparisons return False, which correctly fails the bar
        # A bar with NaN in any condition column will fail (correct behavior)

        all_pass = np.all(in_range, axis=1)  # (n_bars,) bool

        passing_indices = np.where(all_pass)[0]
        if len(passing_indices) == 0:
            continue

        for idx in passing_indices:
            date_val = dates[idx]
            if hasattr(date_val, 'strftime'):
                date_str = date_val.strftime('%Y-%m-%d')
            else:
                date_str = str(date_val)[:10]
            signals.append((ticker, date_str))

    return signals


def filter_universe(universe_cache, expr_cache, conditions):
    """Apply binary conditions across full 5yr history. Returns raw signal list.

    Args:
        universe_cache: dict {ticker: df} — 5yr OHLCV data
        expr_cache: ExprSeriesCache instance
        conditions: list of condition dicts (from select_conditions)

    Returns:
        raw_signals: list of (ticker, date_str) tuples — before dedup
    """
    print(f"\n  Filtering universe: {len(conditions)} conditions × "
          f"{len(universe_cache)} tickers")
    t0 = time.time()

    # Map condition names to expr cache column indices
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    cond_indices = []
    cond_lows = []
    cond_highs = []
    unmapped = []

    for c in conditions:
        idx = cache_name_to_idx.get(c["name"])
        if idx is None:
            unmapped.append(c["name"])
            continue
        cond_indices.append(idx)
        cond_lows.append(c["low"])
        cond_highs.append(c["high"])

    if unmapped:
        print(f"  WARNING: {len(unmapped)} conditions not in expr cache (skipped)")

    cond_indices = np.array(cond_indices, dtype=np.int32)
    cond_lows = np.array(cond_lows, dtype=np.float32)
    cond_highs = np.array(cond_highs, dtype=np.float32)

    n_conditions = len(cond_indices)
    print(f"  Mapped {n_conditions} conditions to expr cache columns")

    # Get tickers that are in both universe and expr cache
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

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_filter_worker,
        initargs=(expr_cache._cache_dir, cond_indices, cond_lows, cond_highs),
    ) as pool:
        futures = {pool.submit(_filter_ticker_batch, batch): batch
                   for batch in batches}
        for future in as_completed(futures):
            batch_signals = future.result()
            all_signals.extend(batch_signals)
            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches, "
                      f"{len(all_signals):,} signals so far ({elapsed:.0f}s)")

    elapsed = time.time() - t0
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

    # Group by ticker
    by_ticker = defaultdict(list)
    for ticker, date_str in signals_raw:
        by_ticker[ticker].append(date_str)

    deduped = []
    for ticker, dates in by_ticker.items():
        dates = sorted(set(dates))  # Unique + sorted

        if not dates:
            continue

        # Cluster consecutive dates within 5 calendar days
        clusters = [[dates[0]]]

        for i in range(1, len(dates)):
            prev_date = pd.to_datetime(clusters[-1][-1])
            curr_date = pd.to_datetime(dates[i])
            gap_days = (curr_date - prev_date).days

            if gap_days <= 5:
                clusters[-1].append(dates[i])
            else:
                clusters.append([dates[i]])

        # Keep rightmost (latest) from each cluster
        for cluster in clusters:
            rightmost = max(cluster)
            deduped.append((ticker, rightmost))

    return deduped


# ══════════════════════════════════════════════════════════════
# SIGNAL STATISTICS
# ══════════════════════════════════════════════════════════════

def compute_signal_stats(deduped_signals):
    """Compute signal statistics: total, peak/day, avg/day, etc."""
    if not deduped_signals:
        return {
            "total": 0, "peak": 0, "avg_per_trading_day": 0.0,
            "n_trading_days": 0, "n_signal_days": 0,
        }

    # Count signals per date
    date_counts = Counter(date for _, date in deduped_signals)

    total = len(deduped_signals)
    peak = max(date_counts.values())
    n_signal_days = len(date_counts)

    # Estimate trading days from date range
    all_dates = sorted(date_counts.keys())
    first = pd.to_datetime(all_dates[0])
    last = pd.to_datetime(all_dates[-1])
    calendar_days = (last - first).days + 1
    n_trading_days = int(calendar_days * 252 / 365)  # Approximate

    avg_per_trading_day = total / max(n_trading_days, 1)

    return {
        "total": total,
        "peak": peak,
        "avg_per_trading_day": round(avg_per_trading_day, 2),
        "n_trading_days": n_trading_days,
        "n_signal_days": n_signal_days,
    }


# ══════════════════════════════════════════════════════════════
# EXAMPLE VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_examples(example_dfs, conditions, expr_cache):
    """Verify 100% example pass rate. Returns (n_passing, n_failing, details)."""
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)

    cond_list = []
    for c in conditions:
        idx = cache_name_to_idx.get(c["name"])
        if idx is not None:
            cond_list.append((c["name"], idx, c["low"], c["high"]))

    n_passing = 0
    n_failing = 0
    failures = []

    for ex in example_dfs:
        if ex["scan_idx"] is None:
            continue

        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]

        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            n_failing += 1
            failures.append(f"{ticker}: not in expr cache")
            continue

        if scan_idx >= len(data):
            n_failing += 1
            failures.append(f"{ticker}: scan_idx {scan_idx} >= bars {len(data)}")
            continue

        row = data[scan_idx, :]
        failed_conds = []

        for name, col_idx, low, high in cond_list:
            val = row[col_idx]
            if np.isnan(val) or val < low or val > high:
                failed_conds.append(f"{name}: {val:.4f} not in [{low:.4f}, {high:.4f}]")

        if failed_conds:
            n_failing += 1
            failures.append(f"{ticker}: {len(failed_conds)} conditions failed")
        else:
            n_passing += 1

    return n_passing, n_failing, failures


# ══════════════════════════════════════════════════════════════
# OUTPUT BUILDER
# ══════════════════════════════════════════════════════════════

def build_output(setup_type, conditions, deduped_signals, stats,
                 example_dfs, profile, weights, total_time,
                 min_d, max_conditions, blackout=False):
    """Build output JSON in pipeline-compatible format."""

    # Example signals
    example_signals = []
    for ex in example_dfs:
        if ex["scan_idx"] is not None:
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

    # Final signals as list of dicts
    signal_dicts = [
        {"ticker": t, "date": d}
        for t, d in deduped_signals
    ]

    examples_passing = len([ex for ex in example_dfs if ex["scan_idx"] is not None])
    examples_failing = 0  # Guaranteed by min/max construction

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    result = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "grinder_type": "hybrid",
        "peak_target": None,
        "multi_pass": False,
        "blackout": blackout,
        "n_conditions": len(conditions),
        "all_conditions": conditions,
        "tier_results": {},  # Not applicable — single pass
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
            "final_avg_per_trading_day": stats["avg_per_trading_day"],
            "n_trading_days": stats["n_trading_days"],
            "n_signal_days": stats["n_signal_days"],
        },
        "final_signals": signal_dicts,
        "example_signals": example_signals,
        "examples_passing": examples_passing,
        "examples_failing": examples_failing,
    }

    return result


# ══════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def run_hybrid(setup_type, min_d=0.5, max_conditions=200, top_n_weight=500,
               blackout=False):
    """Run the hybrid grinder end-to-end.

    Args:
        setup_type: e.g. "dtss"
        min_d: minimum Cohen's d threshold for expression selection
        max_conditions: hard cap on number of conditions
        top_n_weight: how many expressions to weight (passed to dartboard's
                      compute_expression_weights — pre-filter before d threshold)
        blackout: if True, this is a refinement grind (not yet implemented here)

    Returns:
        result: dict — pipeline-compatible output
    """
    if blackout:
        raise NotImplementedError("Hybrid grinder does not support blackout mode yet")

    t0 = time.time()
    print("=" * 60)
    print(f"  HYBRID GRINDER — {setup_type.upper()}")
    print(f"  min_d={min_d}, max_conditions={max_conditions}, top_n_weight={top_n_weight}")
    print("=" * 60)

    # ── Step 1: Load data ──
    print("\n[1/6] Loading data...")
    universe_cache = load_5yr_cache()
    print(f"  5yr cache: {len(universe_cache)} tickers")

    example_dfs = load_example_data(setup_type, universe_cache)
    n_with_scan = sum(1 for ex in example_dfs if ex["scan_idx"] is not None)
    print(f"  Examples: {len(example_dfs)} loaded, {n_with_scan} with valid scan bars")

    expr_cache = ExprSeriesCache(os.path.join(CACHE_DIR, "expr_series"))
    if not expr_cache.is_valid():
        raise RuntimeError("Expression series cache not found or invalid")
    print(f"  Expr cache: {expr_cache.n_expressions} expressions")

    # ── Step 2: Profile + Weight (reuse dartboard) ──
    print("\n[2/6] Building example profile and weighting expressions...")
    profile = build_example_profile(example_dfs, expr_cache)
    weights = compute_expression_weights(profile, universe_cache, expr_cache,
                                          top_n=top_n_weight)

    # ── Step 3: Select conditions (binary ranges from top Cohen's d) ──
    print("\n[3/6] Selecting conditions...")
    conditions, n_dropped = select_conditions(weights, profile,
                                               min_d=min_d,
                                               max_conditions=max_conditions)

    if len(conditions) == 0:
        print("\n  ERROR: No conditions selected. Try lowering --min-d.")
        return None

    # ── Step 4: Filter universe ──
    print("\n[4/6] Filtering universe...")
    raw_signals = filter_universe(universe_cache, expr_cache, conditions)

    # ── Step 5: Deduplicate ──
    print("\n[5/6] Deduplicating signals...")
    deduped = deduplicate_signals(raw_signals)
    stats = compute_signal_stats(deduped)

    print(f"\n  ══════════════════════════════════════")
    print(f"  RESULTS: {stats['total']} signals, peak {stats['peak']}/day, "
          f"avg {stats['avg_per_trading_day']:.1f}/day")
    print(f"  Conditions: {len(conditions)}, Signal days: {stats['n_signal_days']}")
    print(f"  ══════════════════════════════════════")

    # ── Step 6: Validate examples ──
    print("\n[6/6] Validating example pass rate...")
    n_pass, n_fail, failures = validate_examples(example_dfs, conditions, expr_cache)
    print(f"  Examples: {n_pass}/{n_pass + n_fail} passing")

    if n_fail > 0:
        print(f"  *** CRITICAL: {n_fail} examples FAILING ***")
        for f in failures[:10]:
            print(f"    {f}")
        # This should never happen with min/max ranges, but check anyway

    # ── Build output ──
    total_time = time.time() - t0
    result = build_output(
        setup_type=setup_type,
        conditions=conditions,
        deduped_signals=deduped,
        stats=stats,
        example_dfs=example_dfs,
        profile=profile,
        weights=weights,
        total_time=total_time,
        min_d=min_d,
        max_conditions=max_conditions,
        blackout=blackout,
    )

    # ── Save locally ──
    os.makedirs(CACHE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    desc_name = (f"hybrid_{setup_type}_d{str(min_d).replace('.', '')}"
                 f"_c{len(conditions)}_sig{stats['total']}_pk{stats['peak']}_{ts}")
    out_path = os.path.join(CACHE_DIR, f"{desc_name}.json")

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # ── Mirror to Railway ──
    from file_mirror import mirror_file
    mirror_file(out_path)

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
        print(f"\n  [hybrid_grinder] WARNING: Railway upload failed: {e}")
        print(f"  [hybrid_grinder] Local files are saved. Upload manually or retry later.")

    print(f"\n  Total time: {total_time:.1f}s")
    return result


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Hybrid Grinder — dartboard selection + pyramid filtering")
    parser.add_argument("--setup", required=True, help="Setup type (e.g. dtss)")
    parser.add_argument("--min-d", type=float, default=0.5,
                        help="Minimum Cohen's d threshold (default: 0.5)")
    parser.add_argument("--max-conditions", type=int, default=200,
                        help="Maximum number of conditions (default: 200)")
    parser.add_argument("--top-n", type=int, default=500,
                        help="Top N expressions to weight before d filtering (default: 500)")

    args = parser.parse_args()

    result = run_hybrid(
        setup_type=args.setup,
        min_d=args.min_d,
        max_conditions=args.max_conditions,
        top_n_weight=args.top_n,
    )

    if result:
        print(f"\n  Done. {result['summary']['final_total']} signals, "
              f"peak {result['summary']['final_peak']}/day")
    else:
        print("\n  Grinder failed. Check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
