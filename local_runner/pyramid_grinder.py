"""
Pyramidal Grinder — Nested time-horizon expression discovery.

Replaces the old Phase 1 (spiderweb on today) + Phase 2 (flat 5yr historical scorer)
with a single unified system that progressively widens the time window.

Each tier:
  1. Builds a matrix of ticker-day rows for its window (pre-filtered by locked conditions)
  2. Runs spiderweb beam search scoring by peak signals/day (not total pass rate)
  3. Locks any conditions that reduce peak below threshold
  4. Advances to the next wider window

Tiers:
  D1:  Today (1 bar/ticker) — classic spiderweb, scored by pass count
  T2:  5 trading days        — scored by max(daily_signal_counts)
  T3:  21 trading days (1mo) — scored by max(daily_signal_counts)
  T4:  126 trading days (6mo)— scored by max(daily_signal_counts)
  T5:  252 trading days (1yr)— scored by max(daily_signal_counts)
  T6:  Full history (5yr)    — scored by max(daily_signal_counts)

Constraint: 100% of setup examples ALWAYS pass all conditions (zero false negatives).

Usage:
    python local_runner/pyramid_grinder.py --setup dtss [--peak-target 15] [--beam 50] [--depth 10]

Requires:
  - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
  - Example data (via Railway API)
  - Expression library (brute_expressions.py)
"""

import os
import sys
import time
import json
import pickle
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series
from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache

API_BASE = "https://web-production-e3025.up.railway.app"

# Tier definitions: (name, n_bars_from_end, description)
# n_bars=0 means "last bar only" (D1 tier uses point-value matrix like current spiderweb)
TIERS = [
    ("D1",   1,    "Today (last bar)"),
    ("1wk",  5,    "5 trading days"),
    ("1mo",  21,   "1 month"),
    ("6mo",  126,  "6 months"),
    ("1yr",  252,  "1 year"),
    ("5yr",  0,    "Full history"),  # 0 = use all available bars
]


# ══════════════════════════════════════════════════════════════
# DATA LOADING
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


def load_example_data(setup_type):
    """Load example OHLCV data from Railway API."""
    import requests
    resp = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=30)
    data = resp.json()
    if "examples" not in data:
        raise KeyError(f"API response missing 'examples' key. Status: {resp.status_code}")
    examples = data["examples"]

    example_dfs = []
    for ex in examples:
        eid = ex["id"]
        r = requests.get(f"{API_BASE}/api/ohlcv/local/{setup_type}/{eid}", timeout=30)
        candles = r.json().get("candles", [])
        if not candles:
            continue

        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        entry_date = ex.get("entryDate") or ex.get("entry_date")
        scan_idx = None
        if entry_date:
            entry_dt = pd.to_datetime(entry_date)
            match = df[df["date"] < entry_dt]
            if len(match) > 0:
                scan_idx = match.index[-1]

        example_dfs.append({
            "ticker": ex["ticker"],
            "entry_date": entry_date,
            "scan_idx": scan_idx,
            "df": df,
        })

    return example_dfs


# ══════════════════════════════════════════════════════════════
# EXPRESSION LIBRARY + EXAMPLE RANGES
# ══════════════════════════════════════════════════════════════

def compute_example_ranges(example_dfs, expressions):
    """Compute [min, max] range for every expression across all example scan bars.

    Returns:
        ranges: dict {expr_name: (low, high)} — only for expressions with enough valid examples
        example_matrix: np.array (n_examples, n_exprs) — point values at scan bars
    """
    n_ex = len(example_dfs)
    n_expr = len(expressions)
    example_matrix = np.full((n_ex, n_expr), np.nan)

    for i, ex in enumerate(example_dfs):
        if ex["scan_idx"] is None:
            continue
        df = ex["df"]
        scan_idx = ex["scan_idx"]
        engine = ExpressionEngine(df)
        for j, expr in enumerate(expressions):
            try:
                series = compute_series(engine, expr["compute"])
                if series is not None and scan_idx < len(series):
                    val = series[scan_idx]
                    if not np.isnan(val):
                        example_matrix[i, j] = val
            except:
                pass

    # Derive ranges with 5% margin (same as spiderweb)
    # CRITICAL: require ALL examples (with scan_idx) to have non-NaN values.
    # If any example returns NaN for an expression, that expression cannot be
    # used as a condition — it would fail validation for that example.
    ranges = {}
    n_with_scan = sum(1 for ex in example_dfs if ex["scan_idx"] is not None)
    for j, expr in enumerate(expressions):
        vals = example_matrix[:, j]
        valid = vals[~np.isnan(vals)]
        if len(valid) < n_with_scan:
            # At least one example has NaN — skip this expression
            continue
        ex_min, ex_max = np.min(valid), np.max(valid)
        margin = (ex_max - ex_min) * 0.05
        ranges[expr["name"]] = (ex_min - margin, ex_max + margin)

    return ranges, example_matrix


# ══════════════════════════════════════════════════════════════
# TIER MATRIX BUILDER (multiprocessing)
# ══════════════════════════════════════════════════════════════

_w_cache = None
_w_locked = None
_w_exprs = None
_w_ranges = None
_w_candidate_indices = None
_w_n_bars_window = None
_w_use_expr_cache = False
_w_expr_name_to_idx = None


def _init_tier_worker(cache, locked_conditions, expressions, ranges,
                      candidate_indices, n_bars_window,
                      use_expr_cache=False, expr_name_to_idx=None):
    """Initializer: serialize cache + config once per worker."""
    global _w_cache, _w_locked, _w_exprs, _w_ranges, _w_candidate_indices
    global _w_n_bars_window, _w_use_expr_cache, _w_expr_name_to_idx
    _w_cache = cache
    _w_locked = locked_conditions
    _w_exprs = expressions
    _w_ranges = ranges
    _w_candidate_indices = candidate_indices
    _w_n_bars_window = n_bars_window
    _w_use_expr_cache = use_expr_cache
    _w_expr_name_to_idx = expr_name_to_idx or {}


def _build_tier_batch(tickers):
    """For each ticker, compute candidate expression values at each bar in the window,
    but ONLY for bars that pass all locked conditions.

    If _w_use_expr_cache is True, loads pre-computed series from disk instead of
    calling compute_series(). Falls back to compute_series() for uncached tickers.

    Returns list of (ticker, row_dates, candidate_values) where:
      - row_dates: list of date strings for surviving bars
      - candidate_values: np.array (n_surviving_bars, n_candidates) float
    """
    results = []
    for ticker in tickers:
        df = _w_cache.get(ticker)
        if df is None or len(df) < 50:
            results.append((ticker, [], None))
            continue

        try:
            n_bars = len(df)

            # Determine window
            if _w_n_bars_window == 0:
                start_idx = 50  # skip warmup
            else:
                start_idx = max(50, n_bars - _w_n_bars_window)

            # Try loading from expression cache
            cached_data = None
            if _w_use_expr_cache:
                cached_data = _load_ticker_expr_cache(ticker, n_bars)

            if cached_data is not None:
                # ── CACHED PATH: slice pre-computed arrays ──
                cached_dates, cached_matrix = cached_data
                # cached_matrix shape: (n_bars, n_all_expressions)

                # Step 1: Apply locked conditions using cached series
                pass_mask = np.ones(n_bars, dtype=bool)
                pass_mask[:start_idx] = False

                for cond in _w_locked:
                    col_idx = _w_expr_name_to_idx.get(cond["name"])
                    if col_idx is None:
                        # Expression not in cache — can't apply, fail safe
                        pass_mask[:] = False
                        break
                    series = cached_matrix[:, col_idx]
                    in_range = (series >= cond["low"]) & (series <= cond["high"])
                    in_range[np.isnan(series)] = False
                    pass_mask &= in_range

                surviving_indices = np.where(pass_mask)[0]
                if len(surviving_indices) == 0:
                    results.append((ticker, [], None))
                    continue

                # Step 2: Extract candidate columns at surviving bars
                n_cands = len(_w_candidate_indices)
                # Map candidate_indices (position in expression list) to cache column indices
                cand_col_indices = []
                for ci in _w_candidate_indices:
                    expr_name = _w_exprs[ci]["name"]
                    col_idx = _w_expr_name_to_idx.get(expr_name)
                    cand_col_indices.append(col_idx)

                cand_values = np.full((len(surviving_indices), n_cands), np.nan, dtype=np.float32)
                for ci_out, col_idx in enumerate(cand_col_indices):
                    if col_idx is not None:
                        cand_values[:, ci_out] = cached_matrix[surviving_indices, col_idx]

                row_dates = [str(cached_dates[idx]) for idx in surviving_indices]
                results.append((ticker, row_dates, cand_values))

            else:
                # ── FALLBACK: compute from scratch ──
                engine = ExpressionEngine(df)

                pass_mask = np.ones(n_bars, dtype=bool)
                pass_mask[:start_idx] = False

                for cond in _w_locked:
                    series = compute_series(engine, cond["compute"])
                    if series is None or len(series) != n_bars:
                        pass_mask[:] = False
                        break
                    in_range = (series >= cond["low"]) & (series <= cond["high"])
                    in_range[np.isnan(series)] = False
                    pass_mask &= in_range

                surviving_indices = np.where(pass_mask)[0]
                if len(surviving_indices) == 0:
                    results.append((ticker, [], None))
                    continue

                n_cands = len(_w_candidate_indices)
                cand_values = np.full((len(surviving_indices), n_cands), np.nan)

                for ci, expr_idx in enumerate(_w_candidate_indices):
                    expr = _w_exprs[expr_idx]
                    try:
                        series = compute_series(engine, expr["compute"])
                        if series is not None and len(series) == n_bars:
                            cand_values[:, ci] = series[surviving_indices]
                    except:
                        pass

                dates = df["date"].values
                row_dates = [str(dates[idx])[:10] for idx in surviving_indices]
                results.append((ticker, row_dates, cand_values))

        except:
            results.append((ticker, [], None))

    return results


def _load_ticker_expr_cache(ticker, expected_n_bars):
    """Load cached expression series for a ticker.

    Returns (dates, data) or None if not available/mismatched.
    """
    from expr_cache_builder import load_ticker_cache
    dates, data = load_ticker_cache(ticker)
    if dates is None:
        return None
    # Verify bar count matches current OHLCV
    if len(dates) != expected_n_bars:
        return None
    return dates, data


# ══════════════════════════════════════════════════════════════
# PEAK-BASED SPIDERWEB SEARCH
# ══════════════════════════════════════════════════════════════

class PeakSpiderweb:
    """Beam search that scores by max(daily_signal_counts) instead of total pass rate.

    Matrix shape: (n_rows, n_candidates) where rows = ticker-day combos.
    Each row has an associated date. Score = max signals on any single date.
    """

    def __init__(self, candidate_values, row_dates, example_ranges,
                 candidate_names, candidate_categories):
        """
        Args:
            candidate_values: np.array (n_rows, n_candidates) — expression values
            row_dates: list of date strings, one per row
            example_ranges: dict {expr_name: (low, high)}
            candidate_names: list of expression names (len = n_candidates)
            candidate_categories: list of category strings
        """
        self.n_rows, self.n_cands = candidate_values.shape
        self.candidate_names = candidate_names
        self.candidate_categories = candidate_categories

        # Build date-to-row mapping for peak scoring
        self.unique_dates = sorted(set(row_dates))
        self.n_dates = len(self.unique_dates)
        date_to_idx = {d: i for i, d in enumerate(self.unique_dates)}
        self.row_date_indices = np.array([date_to_idx[d] for d in row_dates], dtype=np.int32)

        # Precompute: for each candidate, which rows pass its example range?
        self.cand_passes = np.zeros((self.n_cands, self.n_rows), dtype=bool)
        self.valid_cands = []

        for ci in range(self.n_cands):
            name = candidate_names[ci]
            if name not in example_ranges:
                # No valid range — treat as always passing (can't filter)
                self.cand_passes[ci, :] = True
                continue
            low, high = example_ranges[name]
            vals = candidate_values[:, ci]
            passes = ((vals >= low) & (vals <= high)) | np.isnan(vals)
            self.cand_passes[ci, :] = passes

            # "Useful" = filters out at least 5% of rows
            if np.sum(passes) < self.n_rows * 0.95:
                self.valid_cands.append(ci)

        print(f"    PeakSpiderweb: {self.n_rows:,} rows, {self.n_dates:,} dates, "
              f"{len(self.valid_cands)} useful candidates out of {self.n_cands}")

    def _peak_score(self, row_mask):
        """Given a boolean mask over rows, compute max signals on any single date."""
        if not np.any(row_mask):
            return 0
        # Count signals per date using bincount
        active_dates = self.row_date_indices[row_mask]
        counts = np.bincount(active_dates, minlength=self.n_dates)
        return int(np.max(counts))

    def _daily_stats(self, row_mask):
        """Return (peak, avg, total) for a row mask."""
        if not np.any(row_mask):
            return 0, 0.0, 0
        active_dates = self.row_date_indices[row_mask]
        counts = np.bincount(active_dates, minlength=self.n_dates)
        nonzero = counts[counts > 0]
        total = int(np.sum(row_mask))
        peak = int(np.max(counts))
        avg = float(np.mean(nonzero)) if len(nonzero) > 0 else 0.0
        return peak, avg, total

    def run(self, depth=10, beam_width=50, peak_target=15):
        """Run beam search minimizing peak signals/day.

        Returns dict with conditions, stats, progression.
        """
        t0 = time.time()
        nodes_explored = 0

        if not self.valid_cands:
            return {"error": "No useful candidates", "conditions": [], "levels": []}

        print(f"\n    PeakSpiderweb: depth={depth}, beam={beam_width}, "
              f"target peak<{peak_target}")

        # Current state: all rows active
        base_mask = np.ones(self.n_rows, dtype=bool)
        base_peak, base_avg, base_total = self._daily_stats(base_mask)
        print(f"    Baseline: {base_total:,} signals, peak={base_peak}/day, avg={base_avg:.1f}/day")

        if base_peak <= peak_target:
            print(f"    Already below target. No conditions needed.")
            return {
                "conditions": [],
                "levels": [],
                "stats": {"baseline_peak": base_peak, "baseline_avg": base_avg,
                           "baseline_total": base_total, "final_peak": base_peak},
            }

        # Seed: score each valid candidate individually
        scored = []
        for ci in self.valid_cands:
            mask = base_mask & self.cand_passes[ci]
            peak = self._peak_score(mask)
            scored.append((ci, peak, mask))
            nodes_explored += 1

        scored.sort(key=lambda x: x[1])

        # Build initial beam from best individuals
        from dataclasses import dataclass
        from typing import Tuple

        @dataclass
        class Node:
            conditions: Tuple[int, ...]
            row_mask: np.ndarray
            peak: int

        n_seeds = min(beam_width * 2, len(scored))
        current_level = []
        for ci, peak, mask in scored[:n_seeds]:
            current_level.append(Node(conditions=(ci,), row_mask=mask, peak=peak))

        current_level.sort(key=lambda n: n.peak)
        current_level = current_level[:beam_width]

        best = current_level[0]
        levels = [self._level_summary(1, current_level, time.time() - t0)]
        self._print_level(1, current_level, nodes_explored, time.time() - t0)

        if best.peak <= peak_target:
            return self._build_result(best, levels, nodes_explored, t0, base_peak)

        # Deepen
        for lv in range(2, depth + 1):
            next_level = []
            seen = set()

            for node in current_level:
                used = set(node.conditions)
                if not np.any(node.row_mask):
                    continue

                for ci in self.valid_cands:
                    if ci in used:
                        continue
                    combo = tuple(sorted(node.conditions + (ci,)))
                    if combo in seen:
                        continue
                    seen.add(combo)

                    mask = node.row_mask & self.cand_passes[ci]
                    peak = self._peak_score(mask)
                    nodes_explored += 1

                    next_level.append(Node(conditions=combo, row_mask=mask, peak=peak))

                # Limit expansion per level
                if len(next_level) >= beam_width * 8:
                    break

            if not next_level:
                print(f"\n    ▓ Ceiling at level {lv}")
                break

            next_level.sort(key=lambda n: n.peak)
            current_level = next_level[:beam_width]

            if current_level[0].peak < best.peak:
                best = current_level[0]

            levels.append(self._level_summary(lv, current_level, time.time() - t0))
            self._print_level(lv, current_level, nodes_explored, time.time() - t0)

            if best.peak <= peak_target:
                print(f"\n    ✓ Peak target reached: {best.peak}/day ≤ {peak_target}")
                break

            if not np.any(best.row_mask):
                print(f"\n    ▓ Zero signals at level {lv}")
                break

            # Ceiling: if peak didn't improve this level, stop
            if len(levels) >= 2 and levels[-1]["best_peak"] == levels[-2]["best_peak"]:
                print(f"\n    ▓ Peak ceiling at level {lv} ({best.peak}/day)")
                break

        return self._build_result(best, levels, nodes_explored, t0, base_peak)

    def _build_result(self, best, levels, nodes_explored, t0, baseline_peak):
        peak, avg, total = self._daily_stats(best.row_mask)
        elapsed = time.time() - t0
        conditions = []
        for ci in best.conditions:
            name = self.candidate_names[ci]
            cat = self.candidate_categories[ci]
            # We need the range — it's stored implicitly via cand_passes
            # Reconstruct from the fact that cand_passes was built from example_ranges
            conditions.append({
                "expr": name,
                "name": name,
                "category": cat,
                "cand_index": int(ci),
            })
        return {
            "conditions": conditions,
            "final_peak": peak,
            "final_avg": round(avg, 1),
            "final_total": total,
            "baseline_peak": baseline_peak,
            "levels": levels,
            "stats": {
                "nodes_explored": nodes_explored,
                "elapsed_s": round(elapsed, 1),
                "depth_reached": len(levels),
            }
        }

    def _level_summary(self, level, nodes, elapsed):
        best = nodes[0]
        peak, avg, total = self._daily_stats(best.row_mask)
        return {
            "level": level,
            "best_peak": peak,
            "best_avg": round(avg, 1),
            "best_total": total,
            "n_conditions": len(best.conditions),
            "paths_explored": len(nodes),
            "elapsed_s": round(elapsed, 1),
        }

    def _print_level(self, level, nodes, nodes_explored, elapsed):
        best = nodes[0]
        peak, avg, total = self._daily_stats(best.row_mask)
        print(f"    Level {level:2d}: peak={peak:>4}/day  avg={avg:>5.1f}/day  "
              f"total={total:>7,}  |  {len(nodes)} paths  {nodes_explored:,} nodes  {elapsed:.1f}s")


# ══════════════════════════════════════════════════════════════
# D1 TIER (uses existing SpiderwebSearch for last-bar matrix)
# ══════════════════════════════════════════════════════════════

def run_d1_tier(universe_cache, expressions, example_ranges, example_matrix,
                beam_width=50, depth=10):
    """Run D1 tier using the classic spiderweb on today's snapshot.

    Uses cached universe matrix from matrix_builder (same expressions, same last bar).
    Returns list of condition dicts with {name, category, compute, low, high}.
    """
    from spiderweb import SpiderwebSearch
    from matrix_builder import get_universe_matrix

    print(f"\n  ═══ D1: Loading universe matrix (cached if fresh) ═══")
    t0 = time.time()

    uni_data = get_universe_matrix()
    uni_matrix = uni_data["universe_matrix"]
    tickers = uni_data["universe_tickers"]
    expr_names = uni_data["expr_names"]
    expr_categories = uni_data["expr_categories"]

    # Verify expressions match
    expected_names = [e["name"] for e in expressions]
    if expr_names != expected_names:
        print(f"  ⚠ Expression mismatch — cached {len(expr_names)} vs current {len(expected_names)}")
        print(f"  Forcing matrix rebuild...")
        uni_data = get_universe_matrix(force=True)
        uni_matrix = uni_data["universe_matrix"]
        tickers = uni_data["universe_tickers"]
        expr_names = uni_data["expr_names"]
        expr_categories = uni_data["expr_categories"]

    print(f"  D1 matrix: {len(tickers)} tickers × {len(expr_names)} expressions ({time.time()-t0:.1f}s)")

    # Run spiderweb
    search = SpiderwebSearch(
        example_values=example_matrix,
        universe_values=uni_matrix,
        expr_names=expr_names,
        expr_categories=expr_categories,
        universe_tickers=tickers,
    )

    result = search.run(depth=depth, beam_width=beam_width)

    # Convert to condition list
    conditions = []
    for t in result.get("best_thresholds", []):
        expr_name = t["expr"]
        # Find compute spec
        compute = None
        for e in expressions:
            if e["name"] == expr_name:
                compute = e["compute"]
                break
        conditions.append({
            "name": expr_name,
            "expr": expr_name,
            "category": t.get("category", "unknown"),
            "compute": compute,
            "low": t["low"],
            "high": t["high"],
            "tier": "D1",
        })

    peak_info = {
        "pass_rate": result.get("best_rate", 0),
        "passing_tickers": result.get("passing_tickers", []),
        "n_passing": result.get("best_passing", 0),
    }

    return conditions, result, peak_info


# ══════════════════════════════════════════════════════════════
# HISTORICAL TIER (T2-T6)
# ══════════════════════════════════════════════════════════════

def run_historical_tier(tier_name, n_bars_window, universe_cache, expressions,
                        example_ranges, locked_conditions,
                        beam_width=50, depth=10, peak_target=15,
                        expr_cache=None):
    """Run a historical tier: build matrix of surviving ticker-day rows, then spiderweb.

    Args:
        tier_name: e.g. "1wk", "1mo", etc.
        n_bars_window: how many bars from end (0 = all)
        universe_cache: {ticker: DataFrame}
        expressions: full expression list
        example_ranges: {name: (low, high)}
        locked_conditions: list of condition dicts from prior tiers
        beam_width, depth: spiderweb params
        peak_target: stop when peak/day ≤ this
        expr_cache: ExprSeriesCache instance (or None to compute from scratch)

    Returns:
        new_conditions: list of condition dicts added by this tier
        tier_result: full result dict from PeakSpiderweb
    """
    # Identify candidate expressions (not already locked)
    locked_names = set(c["name"] for c in locked_conditions)
    candidate_indices = []
    for i, expr in enumerate(expressions):
        if expr["name"] not in locked_names and expr["name"] in example_ranges:
            candidate_indices.append(i)

    if not candidate_indices:
        print(f"\n  ═══ {tier_name}: No remaining candidates ═══")
        return [], {"skipped": True}

    print(f"\n  ═══ {tier_name}: Building tier matrix "
          f"({len(universe_cache)} tickers × {len(candidate_indices)} candidates, "
          f"window={n_bars_window or 'all'} bars) ═══")
    print(f"  Locked conditions: {len(locked_conditions)}")
    t0 = time.time()

    # Parallel matrix build
    n_workers = max(cpu_count() - 1, 1)
    all_tickers = list(universe_cache.keys())
    batch_size = max(len(all_tickers) // (n_workers * 4), 50)
    batches = [all_tickers[i:i+batch_size]
               for i in range(0, len(all_tickers), batch_size)]

    # Determine if expression cache is available
    use_expr_cache = expr_cache is not None and expr_cache.is_valid()
    expr_name_to_idx = {}
    if use_expr_cache:
        expr_name_to_idx = dict(expr_cache._expr_name_to_idx)
        print(f"  Using expression series cache ({expr_cache.n_expressions} expressions)")

    print(f"  {n_workers} workers, {len(batches)} batches of ~{batch_size} tickers")

    all_row_dates = []
    all_row_values = []

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_tier_worker,
        initargs=(universe_cache, locked_conditions, expressions,
                  example_ranges, candidate_indices, n_bars_window,
                  use_expr_cache, expr_name_to_idx)
    ) as pool:
        futures = {pool.submit(_build_tier_batch, batch): batch for batch in batches}
        completed = 0
        for future in as_completed(futures):
            batch_results = future.result()
            for ticker, row_dates, cand_values in batch_results:
                if row_dates and cand_values is not None and len(row_dates) > 0:
                    all_row_dates.extend(row_dates)
                    all_row_values.append(cand_values)
            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches "
                      f"[{elapsed:.0f}s, {len(all_row_dates):,} surviving rows]")

    build_time = time.time() - t0

    if not all_row_values:
        print(f"  {tier_name}: Zero surviving rows. Nothing to grind.")
        return [], {"skipped": True, "reason": "no_rows"}

    # Stack into matrix
    candidate_values = np.vstack(all_row_values)
    candidate_names = [expressions[i]["name"] for i in candidate_indices]
    candidate_categories = [expressions[i].get("category", "unknown") for i in candidate_indices]

    print(f"  {tier_name} matrix: {candidate_values.shape[0]:,} rows × "
          f"{candidate_values.shape[1]:,} candidates ({build_time:.0f}s)")

    # Run peak-based spiderweb
    search = PeakSpiderweb(
        candidate_values=candidate_values,
        row_dates=all_row_dates,
        example_ranges=example_ranges,
        candidate_names=candidate_names,
        candidate_categories=candidate_categories,
    )

    result = search.run(depth=depth, beam_width=beam_width, peak_target=peak_target)

    # Convert conditions from search result
    new_conditions = []
    for cond in result.get("conditions", []):
        name = cond["name"]
        # Find full expression spec
        expr_spec = None
        for e in expressions:
            if e["name"] == name:
                expr_spec = e
                break
        if expr_spec is None:
            continue
        low, high = example_ranges.get(name, (np.nan, np.nan))
        new_conditions.append({
            "name": name,
            "expr": name,
            "category": cond.get("category", "unknown"),
            "compute": expr_spec["compute"],
            "low": low,
            "high": high,
            "tier": tier_name,
        })

    return new_conditions, result


# ══════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_examples(example_dfs, conditions):
    """Verify 100% of examples pass all conditions at scan bar."""
    all_pass = True
    for ex in example_dfs:
        if ex["scan_idx"] is None:
            continue
        df = ex["df"]
        engine = ExpressionEngine(df)
        for cond in conditions:
            series = compute_series(engine, cond["compute"])
            val = series[ex["scan_idx"]]
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                print(f"    ✗ {ex['ticker']} FAILS {cond['name']}: "
                      f"{val:.4f} not in [{cond['low']:.4f}, {cond['high']:.4f}]")
                all_pass = False
    return all_pass


# ══════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def run_pyramid(setup_type, peak_target=15, beam_width=50, depth=10,
                d1_depth=None, d1_beam=None):
    """Run the full pyramid grinder.

    Args:
        setup_type: e.g. "dtss"
        peak_target: target for max signals/day at each historical tier
        beam_width: beam width for historical tiers
        depth: search depth for historical tiers
        d1_depth: override depth for D1 tier (default: same as depth)
        d1_beam: override beam for D1 tier (default: same as beam_width)
    """
    d1_depth = d1_depth or depth
    d1_beam = d1_beam or beam_width

    print("\n" + "=" * 70)
    print("  PYRAMIDAL GRINDER")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Peak target: ≤{peak_target} signals/day")
    print(f"  D1: beam={d1_beam}, depth={d1_depth}")
    print(f"  Historical tiers: beam={beam_width}, depth={depth}")

    t_total = time.time()

    # ── Load data ──
    print(f"\n  Loading OHLCV cache...")
    universe_cache = load_5yr_cache()
    print(f"  {len(universe_cache)} tickers loaded")

    print(f"\n  Loading examples...")
    example_dfs = load_example_data(setup_type)
    print(f"  {len(example_dfs)} examples loaded")

    print(f"\n  Loading expressions...")
    expressions = generate_all()
    print(f"  {len(expressions)} expressions")

    # ── Compute example ranges ──
    print(f"\n  Computing example ranges...")
    t0 = time.time()
    example_ranges, example_matrix = compute_example_ranges(example_dfs, expressions)
    print(f"  {len(example_ranges)} expressions have valid ranges ({time.time()-t0:.0f}s)")

    # ── Tier results accumulator ──
    all_conditions = []
    tier_results = {}

    # ── Detect expression series cache ──
    expr_cache = ExprSeriesCache()
    if expr_cache.is_valid(expressions):
        n_cached = len(expr_cache.get_available_tickers())
        print(f"\n  ✓ Expression series cache detected: {n_cached} tickers, "
              f"{expr_cache.n_expressions} expressions")
    else:
        expr_cache = None
        print(f"\n  ⚠ No expression series cache — computing from scratch (slow)")
        print(f"    Run: python local_runner/expr_cache_builder.py --build")

    # ══ D1 TIER ══
    d1_conditions, d1_result, d1_info = run_d1_tier(
        universe_cache, expressions, example_ranges, example_matrix,
        beam_width=d1_beam, depth=d1_depth,
    )

    all_conditions.extend(d1_conditions)
    tier_results["D1"] = {
        "conditions_added": len(d1_conditions),
        "pass_rate": d1_info.get("pass_rate", 0),
        "n_passing": d1_info.get("n_passing", 0),
        "passing_tickers": d1_info.get("passing_tickers", []),
    }

    print(f"\n  D1 result: {len(d1_conditions)} conditions, "
          f"{d1_info['n_passing']} tickers passing ({d1_info['pass_rate']:.2%})")

    # Validate examples
    if all_conditions:
        print(f"\n  Validating examples after D1...")
        if not validate_examples(example_dfs, all_conditions):
            print("  ⚠ WARNING: Some examples fail D1 conditions!")

    # ══ HISTORICAL TIERS (T2-T6) ══
    for tier_name, n_bars, description in TIERS[1:]:
        print(f"\n{'─'*70}")
        print(f"  TIER: {tier_name} — {description}")
        print(f"  Locked: {len(all_conditions)} conditions from prior tiers")
        print(f"{'─'*70}")

        new_conds, tier_result = run_historical_tier(
            tier_name=tier_name,
            n_bars_window=n_bars,
            universe_cache=universe_cache,
            expressions=expressions,
            example_ranges=example_ranges,
            locked_conditions=all_conditions,
            beam_width=beam_width,
            depth=depth,
            peak_target=peak_target,
            expr_cache=expr_cache,
        )

        all_conditions.extend(new_conds)

        tier_results[tier_name] = {
            "conditions_added": len(new_conds),
            "conditions": [c["name"] for c in new_conds],
            "final_peak": tier_result.get("final_peak"),
            "final_avg": tier_result.get("final_avg"),
            "final_total": tier_result.get("final_total"),
            "baseline_peak": tier_result.get("baseline_peak"),
            "stats": tier_result.get("stats"),
        }

        if new_conds:
            print(f"\n  {tier_name} added {len(new_conds)} conditions:")
            for c in new_conds:
                print(f"    + [{c['category']:>18}] {c['name']}")
            print(f"  Peak: {tier_result.get('baseline_peak')} → {tier_result.get('final_peak')}/day")

            # Validate
            print(f"\n  Validating examples after {tier_name}...")
            if not validate_examples(example_dfs, all_conditions):
                print(f"  ⚠ WARNING: Some examples fail after {tier_name}!")
        else:
            final_peak = tier_result.get("final_peak") or tier_result.get("baseline_peak", "?")
            if tier_result.get("skipped"):
                print(f"  {tier_name}: Skipped ({tier_result.get('reason', 'no candidates')})")
            else:
                print(f"  {tier_name}: No conditions added (peak already ≤{peak_target} or at ceiling)")

        # Check if we've hit target at this tier
        final_peak = tier_result.get("final_peak")
        if final_peak is not None and final_peak <= peak_target:
            print(f"\n  ✓ Peak target reached at {tier_name}: {final_peak}/day ≤ {peak_target}")
            # Still run remaining tiers — they might find more
            # But if peak is already 0, no point
            if final_peak == 0:
                print(f"  Zero signals — stopping early.")
                break

    # ══ FINAL SUMMARY ══
    total_time = time.time() - t_total

    print(f"\n{'='*70}")
    print(f"  PYRAMID COMPLETE")
    print(f"{'='*70}")
    print(f"  Total conditions: {len(all_conditions)}")
    print(f"  Total time: {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"\n  Conditions by tier:")
    for tier_name, _, desc in TIERS:
        tr = tier_results.get(tier_name, {})
        n = tr.get("conditions_added", 0)
        if n > 0:
            print(f"    {tier_name:>4}: {n} conditions")

    print(f"\n  All conditions:")
    for i, c in enumerate(all_conditions, 1):
        tier = c.get("tier", "?")
        cat = c.get("category", "unknown")
        print(f"    {i:2d}. [{tier:>4}] [{cat:>18}] {c['name']:35s} "
              f"[{c['low']:.4f} — {c['high']:.4f}]")

    # ── Save ──
    result = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "peak_target": peak_target,
        "n_conditions": len(all_conditions),
        "all_conditions": all_conditions,
        "tier_results": tier_results,
        "d1_result": {
            "pass_rate": d1_info.get("pass_rate"),
            "n_passing": d1_info.get("n_passing"),
            "passing_tickers": d1_info.get("passing_tickers", []),
        },
        "params": {
            "beam_width": beam_width,
            "depth": depth,
            "d1_beam": d1_beam,
            "d1_depth": d1_depth,
            "peak_target": peak_target,
        },
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"pyramid_results_{setup_type}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Also save in historical_results format for compatibility with signal_distribution.py
    compat_result = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "target_per_day": peak_target,
        "phase1_conditions": [c for c in all_conditions if c.get("tier") == "D1"],
        "phase2_additions": [c for c in all_conditions if c.get("tier") != "D1"],
        "all_conditions": all_conditions,
        "n_phase1": len([c for c in all_conditions if c.get("tier") == "D1"]),
        "n_phase2": len([c for c in all_conditions if c.get("tier") != "D1"]),
        "source": "pyramid_grinder",
    }
    compat_path = os.path.join(CACHE_DIR, f"historical_results_{setup_type}.json")
    with open(compat_path, "w") as f:
        json.dump(compat_result, f, indent=2)
    print(f"  Saved (compat): {compat_path}")

    return result


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Pyramidal Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--peak-target", type=int, default=15,
                        help="Target peak signals/day (default: 15)")
    parser.add_argument("--beam", type=int, default=50,
                        help="Beam width for historical tiers")
    parser.add_argument("--depth", type=int, default=10,
                        help="Search depth for historical tiers")
    parser.add_argument("--d1-beam", type=int, default=None,
                        help="Beam width for D1 tier (default: same as --beam)")
    parser.add_argument("--d1-depth", type=int, default=None,
                        help="Depth for D1 tier (default: same as --depth)")
    args = parser.parse_args()

    run_pyramid(
        setup_type=args.setup,
        peak_target=args.peak_target,
        beam_width=args.beam,
        depth=args.depth,
        d1_depth=args.d1_depth,
        d1_beam=args.d1_beam,
    )


if __name__ == "__main__":
    main()
