"""
Dartboard Grinder — Density-based scoring for setup detection.

Replaces the beam search / bounding box approach with continuous scoring:
  1. Profile: compute mean + std for every expression across all examples
  2. Weight: rank expressions by discriminating power (how well they separate
     examples from the general universe)
  3. Score: for every ticker-day in 5yr history, compute a composite similarity
     score — how much does this bar look like the example cluster?
  4. Threshold: pick a score cutoff that produces the target signals/day

Key differences from pyramid_grinder:
  - No beam search, no tiers, no condition selection — all expressions contribute
  - Scoring is continuous (0-1), not binary (pass/fail)
  - More examples = sharper profile = better discrimination (not worse)
  - Deterministic — same inputs always produce the same output
  - Small input changes produce small output changes (no chaotic divergence)

Output format is identical to pyramid_grinder.py for pipeline compatibility.

Usage:
    python local_runner/dartboard_grinder.py --setup dtss
    python local_runner/dartboard_grinder.py --setup dtss --top-n 500 --threshold 0.6
    python local_runner/dartboard_grinder.py --setup dtss --target-peak 5

Requires:
  - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
  - Expression series cache (local_runner/cache/expr_series/)
  - Example data (via Railway API)
"""

import os
import sys
import time
import json
import pickle
import argparse

# Force UTF-8 output on Windows (cp1252 can't handle ≤, ✓, etc.)
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

API_BASE = "https://web-production-e3025.up.railway.app"


# ══════════════════════════════════════════════════════════════
# DATA LOADING (shared with pyramid_grinder)
# ══════════════════════════════════════════════════════════════

def load_5yr_cache():
    """Load 5-year OHLCV cache."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")
    with open(path, "rb") as f:
        return pickle.load(f)


def load_example_data(setup_type, universe_cache):
    """Load example data using the 5yr universe cache for OHLCV."""
    import requests
    resp = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=30)
    data = resp.json()
    if "examples" not in data:
        raise KeyError(f"API response missing 'examples' key. Status: {resp.status_code}")
    examples = data["examples"]

    example_dfs = []
    skipped = []
    for ex in examples:
        ticker = ex["ticker"]
        entry_date = ex.get("entryDate") or ex.get("entry_date")

        df = universe_cache.get(ticker)
        if df is None:
            skipped.append(f"{ticker} (not in 5yr cache)")
            continue

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        scan_idx = None
        if entry_date:
            entry_dt = pd.to_datetime(entry_date)
            match = df[df["date"] < entry_dt]
            if len(match) > 0:
                scan_idx = match.index[-1]

        if scan_idx is None:
            skipped.append(f"{ticker} (no scan bar before {entry_date})")
            continue

        example_dfs.append({
            "ticker": ticker,
            "entry_date": entry_date,
            "scan_idx": scan_idx,
            "df": df,
        })

    if skipped:
        print(f"  ⚠ Skipped {len(skipped)} examples: {', '.join(skipped)}")

    return example_dfs


# ══════════════════════════════════════════════════════════════
# STEP 1: BUILD EXAMPLE PROFILE
# ══════════════════════════════════════════════════════════════

def build_example_profile(example_dfs, expr_cache):
    """Compute mean and std for every expression across all example scan bars.

    For each expression, extracts the value at each example's scan bar, then
    computes the center (mean) and spread (std) of those values.

    Expressions where any example has NaN are excluded — same constraint as
    the pyramid grinder (all examples must have valid values).

    Returns:
        profile: dict with keys:
            - centers: np.array (n_expressions,) — mean value per expression
            - spreads: np.array (n_expressions,) — std dev per expression
            - valid_mask: np.array (n_expressions,) bool — True if usable
            - example_matrix: np.array (n_examples, n_expressions) — raw values
            - n_examples: int
            - expr_names: list of expression names
    """
    n_ex = len(example_dfs)
    n_expr = expr_cache.n_expressions
    expr_names = expr_cache.expr_names

    print(f"\n  Building example profile: {n_ex} examples × {n_expr} expressions")
    t0 = time.time()

    # Extract example values from expr cache
    example_matrix = np.full((n_ex, n_expr), np.nan, dtype=np.float32)

    for i, ex in enumerate(example_dfs):
        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]

        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            raise RuntimeError(f"{ticker}: not in expr cache")
        if scan_idx >= len(data):
            raise RuntimeError(f"{ticker}: scan_idx {scan_idx} >= cached bars {len(data)}")

        example_matrix[i, :] = data[scan_idx, :]

    # Compute profile: require ALL examples to have non-NaN
    centers = np.full(n_expr, np.nan, dtype=np.float64)
    spreads = np.full(n_expr, np.nan, dtype=np.float64)
    valid_mask = np.zeros(n_expr, dtype=bool)

    for j in range(n_expr):
        col = example_matrix[:, j]
        if np.any(np.isnan(col)):
            continue  # At least one example has NaN — skip

        center = np.mean(col)
        spread = np.std(col, ddof=1) if n_ex > 1 else 0.0

        centers[j] = center
        spreads[j] = spread
        valid_mask[j] = True

    n_valid = int(np.sum(valid_mask))
    elapsed = time.time() - t0

    print(f"  Profile built: {n_valid}/{n_expr} expressions valid ({elapsed:.1f}s)")
    print(f"  Expressions with zero spread (all examples identical): "
          f"{int(np.sum(valid_mask & (spreads == 0)))}")

    return {
        "centers": centers,
        "spreads": spreads,
        "valid_mask": valid_mask,
        "example_matrix": example_matrix,
        "n_examples": n_ex,
        "expr_names": expr_names,
    }


# ══════════════════════════════════════════════════════════════
# UNIVERSE STATS CACHE
# ══════════════════════════════════════════════════════════════

UNI_STATS_PATH = os.path.join(CACHE_DIR, "dartboard_universe_stats.npz")


def build_universe_stats_cache(expr_cache=None, universe_cache=None):
    """Pre-compute universe sum/sum_sq/count across all tickers × all bars.

    Saves to dartboard_universe_stats.npz. Run during nightly refresh.
    Takes ~2-5 min parallelized, saves ~2-5 min on every dartboard grind.
    """
    print(f"\n  Building universe stats cache...")
    t0 = time.time()

    if expr_cache is None:
        expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression series cache not found or invalid.")

    if universe_cache is None:
        universe_cache = load_5yr_cache()

    n_expr = expr_cache.n_expressions
    tickers = [t for t in universe_cache.keys()
               if t in expr_cache.get_available_tickers()]
    n_tickers = len(tickers)

    n_workers = max(cpu_count() - 1, 1)
    batch_size = max(n_tickers // (n_workers * 4), 25)
    batches = [tickers[i:i+batch_size] for i in range(0, n_tickers, batch_size)]

    print(f"  {n_tickers} tickers, {n_expr} expressions, "
          f"{n_workers} workers, {len(batches)} batches")

    uni_sum = np.zeros(n_expr, dtype=np.float64)
    uni_sum_sq = np.zeros(n_expr, dtype=np.float64)
    uni_count = np.zeros(n_expr, dtype=np.float64)
    completed = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_uni_stats_worker,
        initargs=(n_expr,)
    ) as pool:
        futures = {pool.submit(_compute_uni_stats_batch, batch): batch
                   for batch in batches}
        for future in as_completed(futures):
            partial_sum, partial_sq, partial_count = future.result()
            uni_sum += partial_sum
            uni_sum_sq += partial_sq
            uni_count += partial_count
            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches ({elapsed:.0f}s)")

    # Save
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.savez_compressed(UNI_STATS_PATH,
                        uni_sum=uni_sum,
                        uni_sum_sq=uni_sum_sq,
                        uni_count=uni_count,
                        n_tickers=np.array([n_tickers]),
                        n_expr=np.array([n_expr]))

    elapsed = time.time() - t0
    avg_bars = int(uni_count.mean()) if uni_count.mean() > 0 else 0
    size_kb = os.path.getsize(UNI_STATS_PATH) / 1024
    print(f"  Universe stats cache built: {n_tickers} tickers, "
          f"~{avg_bars:,} bars/expr, {size_kb:.0f}KB ({elapsed:.0f}s)")
    print(f"  Saved: {UNI_STATS_PATH}")

    return uni_sum, uni_sum_sq, uni_count


def load_universe_stats_cache(n_expr_expected):
    """Load pre-computed universe stats. Returns (sum, sum_sq, count) or None."""
    if not os.path.exists(UNI_STATS_PATH):
        return None

    data = np.load(UNI_STATS_PATH)
    n_expr = int(data["n_expr"][0])
    if n_expr != n_expr_expected:
        print(f"  ⚠ Universe stats cache has {n_expr} expressions, "
              f"expected {n_expr_expected} — rebuilding")
        return None

    n_tickers = int(data["n_tickers"][0])
    print(f"  Loaded universe stats cache: {n_tickers} tickers, "
          f"{n_expr} expressions")

    return data["uni_sum"], data["uni_sum_sq"], data["uni_count"]


# ══════════════════════════════════════════════════════════════
# STEP 2: COMPUTE EXPRESSION WEIGHTS
# ══════════════════════════════════════════════════════════════

# Worker globals for universe stats parallelization
_w_uni_n_expr = None


def _init_uni_stats_worker(n_expr):
    global _w_uni_n_expr
    _w_uni_n_expr = n_expr


def _compute_uni_stats_batch(tickers):
    """Compute partial sum/sum_sq/count for a batch of tickers."""
    from expr_cache_builder import load_ticker_cache
    n = _w_uni_n_expr
    partial_sum = np.zeros(n, dtype=np.float64)
    partial_sq = np.zeros(n, dtype=np.float64)
    partial_count = np.zeros(n, dtype=np.float64)

    for ticker in tickers:
        dates, data = load_ticker_cache(ticker)
        if dates is None or data is None or len(data) < 50:
            continue

        # Skip warmup bars (first 50, same as scoring pass)
        hist = data[50:].astype(np.float64)
        nan_valid = ~np.isnan(hist)
        data_clean = np.where(nan_valid, hist, 0.0)

        partial_count += nan_valid.sum(axis=0)
        partial_sum += data_clean.sum(axis=0)
        partial_sq += (data_clean ** 2).sum(axis=0)

    return partial_sum, partial_sq, partial_count


def compute_expression_weights(profile, universe_cache, expr_cache, top_n=500):
    """Weight expressions by how well they separate examples from the universe.

    For each valid expression:
    - Compute universe mean and std from D1 (last bar per ticker)
    - Discriminating power = |example_mean - universe_mean| / pooled_std
      (similar to Cohen's d — measures separation in std units)

    Returns top_n expressions sorted by discriminating power.

    Returns:
        weights: dict with keys:
            - indices: np.array of expression column indices (sorted by weight)
            - names: list of expression names
            - categories: list of expression categories
            - powers: np.array of discriminating power values
            - centers: np.array of example centers (for these expressions)
            - spreads: np.array of example spreads (for these expressions)
            - uni_centers: np.array of universe centers
            - uni_spreads: np.array of universe spreads
            - top_n: int
    """
    print(f"\n  Computing expression weights (discriminating power)...")
    t0 = time.time()

    centers = profile["centers"]
    spreads = profile["spreads"]
    valid_mask = profile["valid_mask"]
    expr_names = profile["expr_names"]

    # Load expression library for category info
    all_expressions = generate_all()
    name_to_expr = {e["name"]: e for e in all_expressions}

    # Compute universe stats from expr cache — full 5yr history.
    # Try loading pre-computed cache first (built during nightly refresh).
    # Falls back to parallel computation if cache is missing/stale.
    n_expr = len(expr_names)

    cached = load_universe_stats_cache(n_expr)
    if cached is not None:
        uni_sum, uni_sum_sq, uni_count = cached
    else:
        print(f"  Universe stats cache not found — computing from scratch...")
        tickers = list(universe_cache.keys())
        cached_tickers = expr_cache.get_available_tickers()
        tickers = [t for t in tickers if t in cached_tickers]
        n_tickers = len(tickers)

        print(f"  Computing universe stats from {n_tickers} tickers (full 5yr, parallel)...")

        n_workers = max(cpu_count() - 1, 1)
        batch_size = max(len(tickers) // (n_workers * 4), 25)
        batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]
        print(f"  {n_workers} workers, {len(batches)} batches of ~{batch_size}")

        uni_sum = np.zeros(n_expr, dtype=np.float64)
        uni_sum_sq = np.zeros(n_expr, dtype=np.float64)
        uni_count = np.zeros(n_expr, dtype=np.float64)
        completed = 0

        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_uni_stats_worker,
            initargs=(n_expr,)
        ) as pool:
            futures = {pool.submit(_compute_uni_stats_batch, batch): batch
                       for batch in batches}
            for future in as_completed(futures):
                partial_sum, partial_sq, partial_count = future.result()
                uni_sum += partial_sum
                uni_sum_sq += partial_sq
                uni_count += partial_count
                completed += 1
                if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                    elapsed = time.time() - t0
                    print(f"    {completed}/{len(batches)} batches ({elapsed:.0f}s)")

        elapsed = time.time() - t0
        avg_bars = int(uni_count.mean()) if uni_count.mean() > 0 else 0
        print(f"  Universe profiling complete: {n_tickers} tickers, "
              f"~{avg_bars:,} bars/expression ({elapsed:.0f}s)")

        # Auto-save for next run
        try:
            np.savez_compressed(UNI_STATS_PATH,
                                uni_sum=uni_sum, uni_sum_sq=uni_sum_sq,
                                uni_count=uni_count,
                                n_tickers=np.array([n_tickers]),
                                n_expr=np.array([n_expr]))
            print(f"  Saved universe stats cache for next run")
        except Exception as e:
            print(f"  ⚠ Could not save universe stats cache: {e}")

    # Compute universe mean and std
    uni_centers = np.full(n_expr, np.nan, dtype=np.float64)
    uni_spreads = np.full(n_expr, np.nan, dtype=np.float64)

    has_data = uni_count > 1
    uni_centers[has_data] = uni_sum[has_data] / uni_count[has_data]
    variance = (uni_sum_sq[has_data] / uni_count[has_data]) - (uni_centers[has_data] ** 2)
    variance = np.maximum(variance, 0)  # Numerical safety
    uni_spreads[has_data] = np.sqrt(variance)

    # Compute discriminating power for valid expressions
    # Cohen's d variant: |mean_diff| / pooled_std
    # pooled_std = sqrt((ex_std² + uni_std²) / 2)
    disc_power = np.zeros(n_expr, dtype=np.float64)

    for j in range(n_expr):
        if not valid_mask[j]:
            continue
        if np.isnan(uni_centers[j]):
            continue

        ex_std = max(spreads[j], 1e-10)
        un_std = max(uni_spreads[j], 1e-10)
        pooled_std = np.sqrt((ex_std ** 2 + un_std ** 2) / 2)

        if pooled_std < 1e-10:
            continue

        d = abs(centers[j] - uni_centers[j]) / pooled_std
        disc_power[j] = d

    # Also add a penalty for expressions where the example spread is very wide
    # relative to the universe spread (low precision even if center differs)
    # Skip for v1 — keep it simple, let discriminating power handle it

    # Select top_n by discriminating power
    valid_indices = np.where(valid_mask & has_data & (disc_power > 0))[0]
    if len(valid_indices) == 0:
        raise RuntimeError("No expressions with discriminating power > 0")

    sorted_by_power = valid_indices[np.argsort(-disc_power[valid_indices])]
    selected = sorted_by_power[:top_n]

    elapsed = time.time() - t0
    print(f"  Weighting complete: {len(valid_indices)} valid → top {len(selected)} selected ({elapsed:.1f}s)")

    # Print top 20
    print(f"\n  Top 20 expressions by discriminating power:")
    print(f"  {'Rank':>4} {'Power':>7} {'Expression':<45} {'Ex.Center':>10} {'Ex.Spread':>10} "
          f"{'Uni.Center':>10} {'Uni.Spread':>10}")
    for rank, idx in enumerate(selected[:20], 1):
        name = expr_names[idx]
        print(f"  {rank:>4} {disc_power[idx]:>7.3f} {name:<45} "
              f"{centers[idx]:>10.3f} {spreads[idx]:>10.3f} "
              f"{uni_centers[idx]:>10.3f} {uni_spreads[idx]:>10.3f}")

    # Power distribution
    sel_powers = disc_power[selected]
    print(f"\n  Power stats (selected {len(selected)}):")
    print(f"    min={sel_powers.min():.3f}  med={np.median(sel_powers):.3f}  "
          f"max={sel_powers.max():.3f}  mean={sel_powers.mean():.3f}")

    # Category breakdown
    cat_counts = Counter()
    for idx in selected:
        name = expr_names[idx]
        expr = name_to_expr.get(name, {})
        cat_counts[expr.get("category", "unknown")] += 1
    print(f"\n  Categories in top {len(selected)}:")
    for cat, count in cat_counts.most_common(15):
        print(f"    {cat:<30} {count}")

    # Build output
    sel_names = [expr_names[idx] for idx in selected]
    sel_categories = []
    for idx in selected:
        name = expr_names[idx]
        expr = name_to_expr.get(name, {})
        sel_categories.append(expr.get("category", "unknown"))

    return {
        "indices": selected,
        "names": sel_names,
        "categories": sel_categories,
        "powers": disc_power[selected],
        "centers": centers[selected],
        "spreads": spreads[selected],
        "uni_centers": uni_centers[selected],
        "uni_spreads": uni_spreads[selected],
        "top_n": len(selected),
    }


# ══════════════════════════════════════════════════════════════
# STEP 3: SCORE UNIVERSE
# ══════════════════════════════════════════════════════════════

# Worker globals for multiprocessing
_w_expr_cache_dir = None
_w_indices = None
_w_centers = None
_w_spreads = None
_w_powers = None
_w_min_bars = None


def _init_score_worker(expr_cache_dir, indices, centers, spreads, powers, min_bars):
    """Initialize worker with scoring parameters."""
    global _w_expr_cache_dir, _w_indices, _w_centers, _w_spreads, _w_powers, _w_min_bars
    _w_expr_cache_dir = expr_cache_dir
    _w_indices = indices
    _w_centers = centers
    _w_spreads = spreads
    _w_powers = powers
    _w_min_bars = min_bars


def _score_ticker_batch(tickers):
    """Score all bars for a batch of tickers.

    For each bar, compute:
      1. Per-expression z-score: |value - center| / spread
      2. Per-expression score: exp(-0.5 * z²) — Gaussian kernel, 1.0 at center
      3. Composite: weighted average using discriminating power as weight

    NaN values get score 0 (unlike old system where NaN = pass).

    Returns list of (ticker, dates, scores) tuples.
    """
    from expr_cache_builder import load_ticker_cache
    results = []

    total_weight = np.sum(_w_powers)

    for ticker in tickers:
        dates, data = load_ticker_cache(ticker)
        if dates is None or data is None or len(data) < _w_min_bars:
            results.append((ticker, [], np.array([])))
            continue

        # Extract selected expression columns
        # data shape: (n_bars, n_all_expressions)
        selected_data = data[:, _w_indices].astype(np.float64)
        # selected_data shape: (n_bars, top_n)

        n_bars = selected_data.shape[0]

        # Compute z-scores: |value - center| / spread
        # Handle zero spread: if spread is 0, use a small epsilon for expressions
        # where all examples have the same value (perfect agreement)
        safe_spreads = np.where(_w_spreads > 1e-10, _w_spreads, 1e-10)

        z_scores = np.abs(selected_data - _w_centers) / safe_spreads
        # z_scores shape: (n_bars, top_n)

        # For zero-spread expressions (all examples identical):
        # Use tight tolerance — value must be very close to the exact value
        # z = |value - center| / epsilon → huge z if any distance → score ≈ 0
        # This is correct: if all 69 examples have exactly value X, anything
        # that's not X should score 0 on this expression.

        # Gaussian kernel: score = exp(-0.5 * z²)
        # Caps z at 10 to avoid underflow (exp(-50) ≈ 0 anyway)
        z_scores = np.minimum(z_scores, 10.0)
        expr_scores = np.exp(-0.5 * z_scores ** 2)
        # expr_scores shape: (n_bars, top_n), values in [0, 1]

        # NaN handling: NaN values get score 0
        nan_mask = np.isnan(selected_data)
        expr_scores[nan_mask] = 0.0

        # Composite score: weighted average
        # weight_i * score_i summed / total_weight
        # But we adjust for NaN: only count weights where we have data
        weights_broadcast = _w_powers[np.newaxis, :]  # (1, top_n)
        active_weights = np.where(nan_mask, 0.0, weights_broadcast)  # (n_bars, top_n)
        active_weight_sums = np.sum(active_weights, axis=1)  # (n_bars,)

        # Minimum evidence threshold: need at least 50% of weight to have data
        min_weight = total_weight * 0.5
        has_enough = active_weight_sums >= min_weight

        composite = np.zeros(n_bars, dtype=np.float64)
        composite[has_enough] = (
            np.sum(expr_scores[has_enough] * weights_broadcast, axis=1)
            / active_weight_sums[has_enough]
        )

        # Skip warmup bars (first 50)
        composite[:50] = 0.0

        date_strings = [str(d)[:10] for d in dates]
        results.append((ticker, date_strings, composite))

    return results


def score_universe(universe_cache, expr_cache, weights, min_bars=50):
    """Score every bar in 5yr history for all tickers.

    Returns:
        all_scores: list of (ticker, date, score) sorted by score descending
        stats: dict with scoring statistics
    """
    print(f"\n  Scoring full 5yr universe...")
    t0 = time.time()

    tickers = list(universe_cache.keys())
    cached_tickers = expr_cache.get_available_tickers()
    tickers = [t for t in tickers if t in cached_tickers]

    # Prepare worker data
    indices = weights["indices"].copy()
    centers = weights["centers"].copy()
    spreads = weights["spreads"].copy()
    powers = weights["powers"].copy()

    n_workers = max(cpu_count() - 1, 1)
    batch_size = max(len(tickers) // (n_workers * 4), 25)
    batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    print(f"  {len(tickers)} tickers, {n_workers} workers, {len(batches)} batches")

    all_ticker_scores = []
    completed = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_score_worker,
        initargs=(expr_cache_dir, indices, centers, spreads, powers, min_bars)
    ) as pool:
        futures = {pool.submit(_score_ticker_batch, batch): batch for batch in batches}

        for future in as_completed(futures):
            batch_results = future.result()
            for ticker, dates, scores in batch_results:
                if len(dates) > 0 and len(scores) > 0:
                    all_ticker_scores.append((ticker, dates, scores))

            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches ({elapsed:.0f}s)")

    scoring_time = time.time() - t0
    print(f"  Scoring complete: {len(all_ticker_scores)} tickers scored ({scoring_time:.0f}s)")

    return all_ticker_scores, {"scoring_time_s": round(scoring_time, 1)}


# ══════════════════════════════════════════════════════════════
# STEP 4: RANK, DEDUPLICATE, THRESHOLD
# ══════════════════════════════════════════════════════════════

def rank_and_threshold(all_ticker_scores, example_dfs, threshold=None,
                       target_peak=None, target_avg=None):
    """Convert raw scores to a signal list.

    Deduplication: consecutive bars for the same ticker within 5 days →
    keep only the highest-scoring bar.

    Args:
        all_ticker_scores: list of (ticker, dates, scores)
        example_dfs: list of example dicts (for validation)
        threshold: explicit score cutoff (0-1)
        target_peak: find threshold that produces ≤ this peak signals/day
        target_avg: find threshold that produces ≤ this avg signals/day

    Returns:
        signals: list of {ticker, date, score}
        threshold_used: float
        stats: dict
    """
    print(f"\n  Ranking and thresholding...")
    t0 = time.time()

    # First: what do examples score? (sanity check)
    example_scores = []
    ex_lookup = {}
    for ex in example_dfs:
        key = (ex["ticker"], ex["scan_idx"])
        ex_lookup[key] = ex

    for ticker, dates, scores in all_ticker_scores:
        for ex in example_dfs:
            if ex["ticker"] == ticker and ex["scan_idx"] < len(scores):
                example_scores.append({
                    "ticker": ticker,
                    "entry_date": ex["entry_date"],
                    "score": float(scores[ex["scan_idx"]]),
                })

    example_scores.sort(key=lambda x: x["score"])

    print(f"\n  Example scores ({len(example_scores)} examples):")
    if example_scores:
        scores_arr = np.array([x["score"] for x in example_scores])
        print(f"    min={scores_arr.min():.4f}  p10={np.percentile(scores_arr, 10):.4f}  "
              f"med={np.median(scores_arr):.4f}  p90={np.percentile(scores_arr, 90):.4f}  "
              f"max={scores_arr.max():.4f}")

        # Show lowest-scoring examples (most likely to be the outliers)
        print(f"\n  Lowest scoring examples:")
        for ex in example_scores[:5]:
            print(f"    {ex['ticker']:>6} {ex['entry_date']}  score={ex['score']:.4f}")

    # Collect all scored bars above a minimum (0.3) to avoid processing millions of zeros
    all_bars = []
    for ticker, dates, scores in all_ticker_scores:
        above = np.where(scores > 0.3)[0]
        for idx in above:
            all_bars.append((ticker, dates[idx], float(scores[idx])))

    print(f"  Bars with score > 0.3: {len(all_bars):,}")

    # If target_peak specified, binary search for threshold
    if threshold is None and target_peak is not None:
        threshold = _find_threshold_for_peak(all_bars, target_peak)
        print(f"  Auto-threshold for peak≤{target_peak}: {threshold:.4f}")
    elif threshold is None and target_avg is not None:
        threshold = _find_threshold_for_avg(all_bars, target_avg)
        print(f"  Auto-threshold for avg≤{target_avg}: {threshold:.4f}")
    elif threshold is None:
        # Default: use the minimum example score minus a small margin
        if example_scores:
            min_ex = min(x["score"] for x in example_scores)
            threshold = max(0.3, min_ex - 0.05)
            print(f"  Auto-threshold from min example score: {threshold:.4f}")
        else:
            threshold = 0.5
            print(f"  Default threshold: {threshold:.4f}")

    # Report examples that fall below threshold (outlier detection for free)
    if example_scores:
        min_example_score = min(x["score"] for x in example_scores)
        below = [x for x in example_scores if x["score"] < threshold]
        if below:
            print(f"\n  ⚠ {len(below)} examples score below threshold {threshold:.4f}:")
            for ex in below:
                print(f"    {ex['ticker']:>6} {ex['entry_date']}  score={ex['score']:.4f}")
            print(f"  These examples don't look like the rest of the cluster.")
            print(f"  Consider removing them from the example library.")

    # Filter by threshold
    signals_raw = [(t, d, s) for t, d, s in all_bars if s >= threshold]
    signals_raw.sort(key=lambda x: (x[0], x[1]))  # Sort by ticker, date

    # Deduplicate: consecutive signals for same ticker within 5 calendar days
    # Keep the highest-scoring bar in each cluster
    deduped = _deduplicate_signals(signals_raw)

    # Compute daily stats
    date_counts = Counter(d for _, d, _ in deduped)
    n_dates = len(date_counts)
    total = len(deduped)
    peak = max(date_counts.values()) if date_counts else 0
    avg = sum(date_counts.values()) / n_dates if n_dates > 0 else 0

    elapsed = time.time() - t0
    print(f"\n  Threshold: {threshold:.4f}")
    print(f"  Signals: {total:,} total, peak {peak}/day, avg {avg:.1f}/day")
    print(f"  Unique tickers: {len(set(t for t, _, _ in deduped))}")
    print(f"  ({elapsed:.1f}s)")

    signals = [{"ticker": t, "date": d, "score": round(s, 4)} for t, d, s in deduped]

    return signals, threshold, {
        "total": total,
        "peak": peak,
        "avg": round(avg, 1),
        "n_dates_with_signals": n_dates,
        "example_scores": example_scores,
    }


def _deduplicate_signals(signals_raw):
    """Remove consecutive signals for same ticker within 5 calendar days.

    Keeps the highest-scoring bar in each cluster.
    """
    if not signals_raw:
        return []

    # Group by ticker
    by_ticker = defaultdict(list)
    for ticker, date, score in signals_raw:
        by_ticker[ticker].append((date, score))

    deduped = []
    for ticker, bars in by_ticker.items():
        bars.sort(key=lambda x: x[0])  # Sort by date

        # Cluster consecutive bars within 5 calendar days
        clusters = []
        current_cluster = [bars[0]]

        for i in range(1, len(bars)):
            prev_date = pd.to_datetime(current_cluster[-1][0])
            curr_date = pd.to_datetime(bars[i][0])
            gap_days = (curr_date - prev_date).days

            if gap_days <= 5:
                current_cluster.append(bars[i])
            else:
                clusters.append(current_cluster)
                current_cluster = [bars[i]]

        clusters.append(current_cluster)

        # Keep highest-scoring bar from each cluster
        for cluster in clusters:
            best = max(cluster, key=lambda x: x[1])
            deduped.append((ticker, best[0], best[1]))

    return deduped


def _find_threshold_for_peak(all_bars, target_peak):
    """Binary search for score threshold that produces ≤ target_peak signals/day."""
    lo, hi = 0.3, 1.0

    for _ in range(30):  # Binary search converges in ~30 iterations
        mid = (lo + hi) / 2
        signals = [(t, d, s) for t, d, s in all_bars if s >= mid]
        deduped = _deduplicate_signals(signals)
        date_counts = Counter(d for _, d, _ in deduped)
        peak = max(date_counts.values()) if date_counts else 0

        if peak > target_peak:
            lo = mid
        else:
            hi = mid

    return hi  # Use the tighter threshold


def _find_threshold_for_avg(all_bars, target_avg):
    """Binary search for score threshold that produces ≤ target_avg signals/day."""
    lo, hi = 0.3, 1.0

    for _ in range(30):
        mid = (lo + hi) / 2
        signals = [(t, d, s) for t, d, s in all_bars if s >= mid]
        deduped = _deduplicate_signals(signals)
        date_counts = Counter(d for _, d, _ in deduped)
        n_dates = len(date_counts)
        avg = sum(date_counts.values()) / n_dates if n_dates > 0 else 0

        if avg > target_avg:
            lo = mid
        else:
            hi = mid

    return hi


# ══════════════════════════════════════════════════════════════
# STEP 5: VALIDATE EXAMPLES
# ══════════════════════════════════════════════════════════════

def validate_example_scores(example_scores, threshold):
    """Verify all examples score above the threshold.

    Returns (all_pass: bool, failures: list of dicts)
    """
    failures = []
    for ex in example_scores:
        if ex["score"] < threshold:
            failures.append(ex)

    all_pass = len(failures) == 0

    if all_pass:
        print(f"  ✓ All {len(example_scores)} examples pass threshold {threshold:.4f}")
    else:
        print(f"\n{'!'*80}")
        print(f"  VALIDATION FAILED: {len(failures)} examples below threshold {threshold:.4f}")
        for ex in failures:
            print(f"    ✗ {ex['ticker']} {ex['entry_date']} score={ex['score']:.4f}")
        print(f"{'!'*80}")

    return all_pass, failures


# ══════════════════════════════════════════════════════════════
# OUTPUT BUILDER
# ══════════════════════════════════════════════════════════════

def build_output(setup_type, weights, signals, threshold, stats,
                 example_dfs, profile, total_time, blackout=False):
    """Build output JSON in the same format as pyramid_grinder.

    The 'all_conditions' field contains the top weighted expressions,
    formatted as conditions for compatibility. The 'low' and 'high'
    values represent mean ± 2*std (the approximate 95% range of examples).
    """
    # Build conditions from weighted expressions (for compatibility)
    all_expressions = generate_all()
    name_to_expr = {e["name"]: e for e in all_expressions}

    all_conditions = []
    for i in range(weights["top_n"]):
        name = weights["names"][i]
        cat = weights["categories"][i]
        center = float(weights["centers"][i])
        spread = float(weights["spreads"][i])
        power = float(weights["powers"][i])

        # Compute "low" and "high" from example min/max + 5% margin
        # This guarantees 100% example pass rate (same logic as pyramid_grinder).
        # The actual scoring uses continuous distance, not these bounds — they
        # exist for pipeline compatibility with signal_filter.py.
        ex_matrix = profile.get("example_matrix")
        expr_idx = weights["indices"][i]
        if ex_matrix is not None:
            vals = ex_matrix[:, expr_idx]
            valid = vals[~np.isnan(vals)]
            if len(valid) > 0:
                ex_min = float(np.min(valid))
                ex_max = float(np.max(valid))
                margin = (ex_max - ex_min) * 0.05
                low = ex_min - margin
                high = ex_max + margin
            else:
                low = center - max(spread * 3, 0.01)
                high = center + max(spread * 3, 0.01)
        else:
            low = center - max(spread * 3, 0.01)
            high = center + max(spread * 3, 0.01)

        expr = name_to_expr.get(name, {})

        all_conditions.append({
            "name": name,
            "expr": name,
            "category": cat,
            "compute": expr.get("compute"),
            "low": round(low, 6),
            "high": round(high, 6),
            "tier": "dartboard",
            "dartboard_center": round(center, 6),
            "dartboard_spread": round(spread, 6),
            "dartboard_power": round(power, 4),
            "dartboard_uni_center": round(float(weights["uni_centers"][i]), 6),
            "dartboard_uni_spread": round(float(weights["uni_spreads"][i]), 6),
        })

    # Sort conditions by power (highest first)
    all_conditions.sort(key=lambda c: -c.get("dartboard_power", 0))

    # Example signals
    example_signals = []
    for ex in example_dfs:
        if ex["scan_idx"] is not None:
            df = ex["df"]
            scan_date = df["date"].iloc[ex["scan_idx"]]
            date_str = str(scan_date)[:10] if not hasattr(scan_date, "date") else str(scan_date.date())
            example_signals.append({
                "ticker": ex["ticker"],
                "date": date_str,
                "entry_date": ex["entry_date"],
                "is_example": True,
            })

    # Signal stats
    total_signals = stats["total"]
    peak = stats["peak"]
    avg = stats["avg"]

    result = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "grinder_type": "dartboard",
        "peak_target": peak,
        "multi_pass": False,
        "blackout": blackout,
        "n_conditions": len(all_conditions),
        "all_conditions": all_conditions,
        "tier_results": {},  # Not applicable — single pass
        "pass_summaries": None,
        "params": {
            "grinder_type": "dartboard",
            "top_n": weights["top_n"],
            "threshold": round(threshold, 6),
            "scoring": "gaussian_kernel",
            "weighting": "cohens_d",
            "source": "dartboard_grinder",
        },
        "summary": {
            "final_total": total_signals,
            "final_peak": peak,
            "final_avg": avg,
        },
        "example_signals": example_signals,
        "example_scores": stats.get("example_scores", []),
        "examples_passing": len([ex for ex in example_dfs if ex["scan_idx"] is not None]),
        "examples_failing": 0,
        "dartboard_threshold": round(threshold, 6),
    }

    return result


# ══════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def run_dartboard(setup_type, top_n=500, threshold=None, target_peak=None,
                  target_avg=None, blackout_map=None):
    """Run the full dartboard grinder.

    Args:
        setup_type: e.g. "dtss"
        top_n: number of top-weighted expressions to use (default 500)
        threshold: explicit score cutoff (0-1). If None, auto-determined.
        target_peak: find threshold for ≤ this peak signals/day
        target_avg: find threshold for ≤ this avg signals/day
        blackout_map: reserved for future refinement grind compatibility
    """
    print("\n" + "=" * 70)
    print("  DARTBOARD GRINDER")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Top expressions: {top_n}")
    if threshold:
        print(f"  Threshold: {threshold}")
    elif target_peak:
        print(f"  Target peak: ≤{target_peak}/day (auto-threshold)")
    elif target_avg:
        print(f"  Target avg: ≤{target_avg}/day (auto-threshold)")
    else:
        print(f"  Threshold: auto (from min example score)")

    t_total = time.time()

    # ── Load data ──
    print(f"\n  Loading OHLCV cache...")
    universe_cache = load_5yr_cache()
    print(f"  {len(universe_cache)} tickers loaded")

    print(f"\n  Loading examples...")
    example_dfs = load_example_data(setup_type, universe_cache)
    print(f"  {len(example_dfs)} examples loaded")

    # ── Expression cache ──
    print(f"\n  Detecting expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression series cache not found or invalid.")
    n_cached = len(expr_cache.get_available_tickers())
    print(f"  Expression series cache: {n_cached} tickers, "
          f"{expr_cache.n_expressions} expressions")

    # Filter examples to those in expr cache
    cached_tickers = expr_cache.get_available_tickers()
    before_count = len(example_dfs)
    filtered_dfs = []
    excluded = []
    for ex in example_dfs:
        if ex["ticker"] in cached_tickers:
            n_cached_bars = expr_cache.get_ticker_bar_count(ex["ticker"])
            if ex["scan_idx"] < n_cached_bars:
                filtered_dfs.append(ex)
            else:
                excluded.append(f"{ex['ticker']} (scan_idx out of range)")
        else:
            excluded.append(f"{ex['ticker']} (not in expr cache)")
    example_dfs = filtered_dfs
    if excluded:
        print(f"  ⚠ Excluded {len(excluded)} examples: {', '.join(excluded)}")
        print(f"  Examples: {before_count} → {len(example_dfs)}")

    # ── Step 1: Build example profile ──
    profile = build_example_profile(example_dfs, expr_cache)

    # ── Step 2: Compute expression weights ──
    weights = compute_expression_weights(profile, universe_cache, expr_cache, top_n=top_n)

    # ── Step 3: Score full universe ──
    all_ticker_scores, scoring_stats = score_universe(
        universe_cache, expr_cache, weights)

    # ── Step 4: Rank and threshold ──
    signals, threshold_used, signal_stats = rank_and_threshold(
        all_ticker_scores, example_dfs,
        threshold=threshold, target_peak=target_peak, target_avg=target_avg)

    # ── Validate: all examples must pass ──
    example_scores = signal_stats.get("example_scores", [])
    all_pass, failures = validate_example_scores(example_scores, threshold_used)

    # ── Build output ──
    total_time = time.time() - t_total
    result = build_output(
        setup_type, weights, signals, threshold_used, signal_stats,
        example_dfs, profile, total_time, blackout=bool(blackout_map))
    result["examples_failing"] = len(failures)

    # ── Save ──
    os.makedirs(CACHE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    total_signals = signal_stats["total"]
    peak = signal_stats["peak"]
    desc_name = f"dartboard_{setup_type}_sig{total_signals}_pk{peak}_{ts}"

    out_path = os.path.join(CACHE_DIR, f"{desc_name}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # ── Mirror to Railway ──
    try:
        from file_mirror import mirror_file
        mirror_file(out_path)
    except Exception as e:
        print(f"\n  WARNING: File mirror failed: {e}")

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
        print(f"\n  WARNING: Railway upload failed: {e}")
        print(f"  Local file saved. Upload manually or retry later.")

    # ── Final summary ──
    print(f"\n{'='*70}")
    print(f"  DARTBOARD COMPLETE")
    print(f"{'='*70}")
    print(f"  Expressions used: {weights['top_n']}")
    print(f"  Threshold: {threshold_used:.4f}")
    print(f"  Signals: {total_signals:,} total, peak {peak}/day, "
          f"avg {signal_stats['avg']:.1f}/day")
    print(f"  Time: {total_time:.0f}s ({total_time/60:.1f} min)")

    return result


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Dartboard Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--top-n", type=int, default=500,
                        help="Number of top expressions to use (default: 500)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Explicit score threshold (0-1)")
    parser.add_argument("--target-peak", type=int, default=None,
                        help="Auto-find threshold for this peak signals/day")
    parser.add_argument("--target-avg", type=float, default=None,
                        help="Auto-find threshold for this avg signals/day")
    parser.add_argument("--blackout", action="store_true",
                        help="Reserved for refinement grind compatibility")
    parser.add_argument("--build-cache", action="store_true",
                        help="Build universe stats cache only (no grind)")
    args = parser.parse_args()

    if args.build_cache:
        build_universe_stats_cache()
        return

    run_dartboard(
        setup_type=args.setup,
        top_n=args.top_n,
        threshold=args.threshold,
        target_peak=args.target_peak,
        target_avg=args.target_avg,
    )


if __name__ == "__main__":
    main()
