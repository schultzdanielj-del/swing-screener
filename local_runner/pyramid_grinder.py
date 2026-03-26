"""
Pyramidal Grinder — Nested time-horizon expression discovery.

MULTI-PASS MODE (default):
  Runs 3 sequential passes to prevent HTF expressions from crowding out daily:
    Pass 1 (Daily+LSP+Algo): Full pyramid (D1→5yr) with 4,141 daily+LSP+algo expressions
    Pass 2 (Weekly):     1mo→5yr tiers with 4,017 weekly expressions on top
    Pass 3 (Monthly):    6mo→5yr tiers with 4,017 monthly expressions on top
  Daily gets first crack at every horizon. Weekly/monthly only add value
  where daily couldn't finish the job.

SINGLE-PASS MODE (--single-pass):
  Legacy mode: all 12,131 expressions in one pass through D1→5yr.

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
    # Multi-pass (default):
    python local_runner/pyramid_grinder.py --setup dtss --beam 10000 --depth 100 --peak-target 3

    # Legacy single-pass:
    python local_runner/pyramid_grinder.py --setup dtss --single-pass --beam 10000 --depth 100

Requires:
  - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
  - Expression series cache (local_runner/cache/expr_series/)
  - Example data (via Railway API)
  - Expression library (brute_expressions.py)
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
from collections import defaultdict
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


def load_example_data(setup_type, universe_cache):
    """Load example data using the 5yr universe cache for OHLCV.

    Examples metadata (ticker, entry_date) comes from local SQLite.
    OHLCV data comes from the same 5yr cache used by the backtest scanner,
    ensuring identical indicator values and history depth.
    """
    import sqlite3
    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker",
        (setup_type,)
    ).fetchall()
    conn.close()
    examples = [{"ticker": r["ticker"], "entry_date": r["entry_date"]} for r in rows]
    if not examples:
        raise ValueError(f"No examples found for setup '{setup_type}' in local DB")
    print(f"  Loaded {len(examples)} examples from local DB")

    example_dfs = []
    skipped = []
    for ex in examples:
        ticker = ex["ticker"]
        entry_date = ex.get("entry_date")

        # Use 5yr cache — same data source as backtest scanner
        df = universe_cache.get(ticker)
        if df is None:
            skipped.append(f"{ticker} (not in 5yr cache)")
            continue

        # Ensure proper types
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
# EXPRESSION LIBRARY + EXAMPLE RANGES
# ══════════════════════════════════════════════════════════════

def compute_example_ranges(example_dfs, expressions, expr_cache=None):
    """Compute [min, max] range for every expression across all example scan bars.

    Loads values from expr cache .npz files. expr_cache is REQUIRED — all grinders
    must use the same computation path (no live compute_series fallback).

    Returns:
        ranges: dict {expr_name: (low, high)} — only for expressions with enough valid examples
        example_matrix: np.array (n_examples, n_exprs) — point values at scan bars
    """
    if expr_cache is None or not expr_cache.is_valid():
        raise RuntimeError(
            "Expression series cache is REQUIRED. No fallback computation paths allowed.\n"
            "Run: python local_runner/expr_cache_builder.py --build"
        )

    n_ex = len(example_dfs)
    n_expr = len(expressions)
    example_matrix = np.full((n_ex, n_expr), np.nan)

    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    expr_name_list = [e["name"] for e in expressions]

    # Build mapping: our expression index → cache column index
    expr_to_cache_col = []
    for j, name in enumerate(expr_name_list):
        expr_to_cache_col.append(cache_name_to_idx.get(name))

    # Build vectorized column remapping array once
    # valid_cache_cols[j] = cache column index for expression j, or -1 if unmapped
    valid_cache_cols = np.array(
        [c if c is not None else -1 for c in expr_to_cache_col], dtype=np.int32)
    has_mapping = valid_cache_cols >= 0
    mapped_our_indices = np.where(has_mapping)[0]
    mapped_cache_indices = valid_cache_cols[has_mapping]

    for i, ex in enumerate(example_dfs):
        if ex["scan_idx"] is None:
            continue
        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]

        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            raise RuntimeError(f"{ticker}: not in expr cache — cannot compute example ranges")
        if scan_idx >= len(data):
            raise RuntimeError(f"{ticker}: scan_idx {scan_idx} >= cached bars {len(data)}")

        # Extract scan bar row and remap columns in one numpy operation
        cached_row = data[scan_idx, :]
        n_cache_cols = len(cached_row)
        # Filter to columns that exist in this ticker's cache
        col_mask = mapped_cache_indices < n_cache_cols
        our_idx = mapped_our_indices[col_mask]
        cache_idx = mapped_cache_indices[col_mask]
        vals = cached_row[cache_idx]
        # Only assign non-NaN values
        valid_mask = ~np.isnan(vals)
        example_matrix[i, our_idx[valid_mask]] = vals[valid_mask]

    n_valid_total = int(np.sum(~np.isnan(example_matrix)))
    print(f"  Loaded from expr cache: {n_valid_total:,} values "
          f"({n_valid_total / max(n_ex * n_expr, 1) * 100:.1f}% fill)")

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


def prefilter_candidates(expressions, example_ranges, threshold=0.85):
    """Remove expressions whose example range passes too much of the universe.

    Only filters the ranges dict — expressions without a range are naturally
    excluded from being candidates. Does NOT modify the expression list
    (to keep example_matrix columns in sync).

    Args:
        expressions: list of expression dicts (returned unchanged)
        example_ranges: {name: (low, high)} from compute_example_ranges
        threshold: drop expressions with pass rate >= this (default 0.85)

    Returns:
        expressions: same list (unchanged)
        filtered_ranges: dict {name: (low, high)} (subset of input)
        stats: dict with counts
    """
    from matrix_builder import get_universe_matrix

    uni_data = get_universe_matrix()
    uni_matrix = uni_data["universe_matrix"]
    uni_expr_names = uni_data["expr_names"]
    n_universe = uni_matrix.shape[0]

    name_to_col = {name: i for i, name in enumerate(uni_expr_names)}

    dropped = 0
    dropped_by_cat = defaultdict(int)
    filtered_ranges = {}

    # Build name → category lookup
    name_to_cat = {e["name"]: e.get("category", "unknown") for e in expressions}

    for name, (low, high) in example_ranges.items():
        cat = name_to_cat.get(name, "unknown")
        col = name_to_col.get(name)

        if col is None:
            # Not in D1 universe matrix (HTF etc) — keep, will be
            # evaluated at proper tier using expr cache
            filtered_ranges[name] = (low, high)
            continue

        vals = uni_matrix[:, col]
        passes = ((vals >= low) & (vals <= high)) | np.isnan(vals)
        pass_rate = float(np.sum(passes)) / n_universe

        if pass_rate >= threshold:
            dropped += 1
            dropped_by_cat[cat] += 1
        else:
            filtered_ranges[name] = (low, high)

    stats = {
        "threshold": threshold,
        "before": len(example_ranges),
        "after": len(filtered_ranges),
        "dropped": dropped,
        "dropped_by_category": dict(dropped_by_cat),
    }

    print(f"  Pre-filter ({threshold:.0%} threshold): "
          f"{len(example_ranges)} → {len(filtered_ranges)} candidates "
          f"(dropped {dropped})")
    if dropped_by_cat:
        top_dropped = sorted(dropped_by_cat.items(), key=lambda x: -x[1])[:5]
        print(f"  Top dropped: {', '.join(f'{c}={n}' for c, n in top_dropped)}")

    return expressions, filtered_ranges, stats


# ══════════════════════════════════════════════════════════════
# TIER MATRIX BUILDER (multiprocessing)
# ══════════════════════════════════════════════════════════════

_w_cache = None
_w_locked = None
_w_exprs = None
_w_ranges = None
_w_candidate_indices = None
_w_n_bars_window = None
_w_expr_name_to_idx = None
_w_blackout = None  # {ticker: [(entry_idx, exit_idx), ...]} — bars to exclude
_w_whitelist = None  # {ticker: set(bar_idx)} — if set, ONLY these bars count


def _init_tier_worker(cache, locked_conditions, expressions, ranges,
                      candidate_indices, n_bars_window, expr_name_to_idx=None,
                      blackout_map=None, whitelist_map=None):
    """Initializer: serialize cache + config once per worker."""
    global _w_cache, _w_locked, _w_exprs, _w_ranges, _w_candidate_indices
    global _w_n_bars_window, _w_expr_name_to_idx, _w_blackout, _w_whitelist
    _w_cache = cache
    _w_locked = locked_conditions
    _w_exprs = expressions
    _w_ranges = ranges
    _w_candidate_indices = candidate_indices
    _w_n_bars_window = n_bars_window
    _w_expr_name_to_idx = expr_name_to_idx or {}
    _w_blackout = blackout_map or {}
    _w_whitelist = whitelist_map


def _build_tier_batch(tickers):
    """For each ticker, compute candidate expression values at each bar in the window,
    but ONLY for bars that pass all locked conditions.

    Loads pre-computed series from expr cache. Expr cache is REQUIRED — no fallback
    to compute_series() (all grinders must use the same computation path).

    Returns list of (ticker, row_dates, candidate_values) where:
      - row_dates: list of date strings for surviving bars
      - candidate_values: np.array (n_surviving_bars, n_candidates) float
    """
    results = []
    for ticker in tickers:
        n_bars = _w_cache.get(ticker)
        if n_bars is None or n_bars < 50:
            results.append((ticker, [], None))
            continue

        try:

            # Determine window
            if _w_n_bars_window == 0:
                start_idx = 50  # skip warmup
            else:
                start_idx = max(50, n_bars - _w_n_bars_window)

            # Load from expression cache (REQUIRED)
            cached_data = _load_ticker_expr_cache(ticker, n_bars)

            if cached_data is None:
                # Not in cache — skip (filtered examples already exclude these)
                results.append((ticker, [], None))
                continue

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

            # Step 1b: Apply blackout mask — exclude post-entry bars per example
            # These are bars between entry and exit for any example in this ticker.
            # Prevents the re-grind from learning conditions that fire on in-play
            # post-entry price action rather than legitimate pre-entry setups.
            if _w_blackout and ticker in _w_blackout:
                for entry_idx, exit_idx in _w_blackout[ticker]:
                    # Mask bars entry_idx+1 through exit_idx (inclusive)
                    blackout_start = max(0, entry_idx + 1)
                    blackout_end = min(n_bars, exit_idx + 1)
                    if blackout_start < blackout_end:
                        pass_mask[blackout_start:blackout_end] = False

            # Step 1c: Apply whitelist — if set, ONLY whitelisted bars count
            # Used by refinement grind: only loser signal bars are eligible
            if _w_whitelist is not None:
                if ticker in _w_whitelist:
                    wl_mask = np.zeros(n_bars, dtype=bool)
                    for idx in _w_whitelist[ticker]:
                        if 0 <= idx < n_bars:
                            wl_mask[idx] = True
                    pass_mask &= wl_mask
                else:
                    # Ticker has no loser bars — nothing to count
                    pass_mask[:] = False

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
                 candidate_names, candidate_categories, row_tickers=None):
        """
        Args:
            candidate_values: np.array (n_rows, n_candidates) — expression values
            row_dates: list of date strings, one per row
            row_tickers: list of ticker strings, one per row (optional)
            example_ranges: dict {expr_name: (low, high)}
            candidate_names: list of expression names (len = n_candidates)
            candidate_categories: list of category strings
        """
        self.n_rows, self.n_cands = candidate_values.shape
        self.candidate_names = candidate_names
        self.candidate_categories = candidate_categories
        self.row_dates_list = row_dates
        self.row_tickers_list = row_tickers or (["?"] * self.n_rows)

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

            # Any expression that filters at least 1 row is useful
            if np.sum(passes) < self.n_rows:
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

            if current_level[0].peak < best.peak or (
                current_level[0].peak == best.peak and
                np.sum(current_level[0].row_mask) < np.sum(best.row_mask)
            ):
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

        # Per-condition leave-one-out signal counts — pure numpy, instant.
        # signals_without[i] = total signals if condition i were removed.
        # filter_power[i] = (signals_without - signals_with_all) / signals_with_all
        # This lets setup_refiner prune weak conditions from JSON alone, no rescan.
        cond_list = list(best.conditions)
        loo_totals = []
        for drop_i, ci in enumerate(cond_list):
            # AND all masks except this one
            without_mask = np.ones(self.n_rows, dtype=bool)
            for j, cj in enumerate(cond_list):
                if j != drop_i:
                    without_mask &= self.cand_passes[cj]
            loo_totals.append(int(np.sum(without_mask)))

        conditions = []
        for drop_i, ci in enumerate(cond_list):
            name = self.candidate_names[ci]
            cat = self.candidate_categories[ci]
            without = loo_totals[drop_i]
            fp = (without - total) / total if total > 0 else 0.0
            conditions.append({
                "expr": name,
                "name": name,
                "category": cat,
                "cand_index": int(ci),
                "signals_with_all": total,
                "signals_without": without,
                "filter_power": round(fp, 4),
            })

        # Extract final signals (date + ticker for every surviving row)
        surviving_indices = np.where(best.row_mask)[0]
        final_signals = []
        final_tickers_set = set()
        for idx in surviving_indices:
            date = self.row_dates_list[idx]
            ticker = self.row_tickers_list[idx]
            final_signals.append({"date": str(date)[:10], "ticker": ticker})
            final_tickers_set.add(ticker)
        final_signals.sort(key=lambda s: (s["date"], s["ticker"]))

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
            },
            "final_signals": final_signals,
            "final_tickers": sorted(final_tickers_set),
            "n_unique_tickers": len(final_tickers_set),
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


class RefinementSearch:
    """Beam search that scores by total losers remaining (not peak/day).

    Used by run_refinement() (step 4) only. Optimizes for maximum loser
    elimination while maintaining 100% winner pass rate via bounding box.

    Same data layout and output format as PeakSpiderweb so run_refinement()
    needs no structural changes.
    """

    def __init__(self, candidate_values, row_dates, example_ranges,
                 candidate_names, candidate_categories, row_tickers=None):
        self.n_rows, self.n_cands = candidate_values.shape
        self.candidate_names = candidate_names
        self.candidate_categories = candidate_categories
        self.row_dates_list = row_dates
        self.row_tickers_list = row_tickers or (["?"] * self.n_rows)

        # Date mapping still needed for output stats (peak/avg reporting)
        self.unique_dates = sorted(set(row_dates))
        self.n_dates = len(self.unique_dates)
        date_to_idx = {d: i for i, d in enumerate(self.unique_dates)}
        self.row_date_indices = np.array([date_to_idx[d] for d in row_dates], dtype=np.int32)

        # Precompute pass/fail per candidate — identical to PeakSpiderweb
        self.cand_passes = np.zeros((self.n_cands, self.n_rows), dtype=bool)
        self.valid_cands = []

        for ci in range(self.n_cands):
            name = candidate_names[ci]
            if name not in example_ranges:
                self.cand_passes[ci, :] = True
                continue
            low, high = example_ranges[name]
            vals = candidate_values[:, ci]
            passes = ((vals >= low) & (vals <= high)) | np.isnan(vals)
            self.cand_passes[ci, :] = passes

            if np.sum(passes) < self.n_rows:
                self.valid_cands.append(ci)

        print(f"    RefinementSearch: {self.n_rows:,} rows, "
              f"{len(self.valid_cands)} useful candidates out of {self.n_cands}")

    def _total_score(self, row_mask):
        """Score = total losers remaining. Lower is better."""
        return int(np.sum(row_mask))

    def _daily_stats(self, row_mask):
        """Return (peak, avg, total) for reporting."""
        if not np.any(row_mask):
            return 0, 0.0, 0
        active_dates = self.row_date_indices[row_mask]
        counts = np.bincount(active_dates, minlength=self.n_dates)
        nonzero = counts[counts > 0]
        total = int(np.sum(row_mask))
        peak = int(np.max(counts))
        avg = float(np.mean(nonzero)) if len(nonzero) > 0 else 0.0
        return peak, avg, total

    def run(self, depth=100, beam_width=10000, peak_target=3):
        """Run beam search minimizing total losers remaining.

        peak_target is accepted for interface compatibility but not used
        for termination — search continues until no more losers can be cut.
        """
        t0 = time.time()
        nodes_explored = 0

        if not self.valid_cands:
            return {"error": "No useful candidates", "conditions": [], "levels": []}

        base_mask = np.ones(self.n_rows, dtype=bool)
        base_total = self._total_score(base_mask)
        base_peak, base_avg, _ = self._daily_stats(base_mask)
        print(f"\n    RefinementSearch: depth={depth}, beam={beam_width}")
        print(f"    Baseline: {base_total:,} losers, peak={base_peak}/day, avg={base_avg:.1f}/day")

        if base_total == 0:
            print(f"    No losers to eliminate.")
            return {
                "conditions": [], "levels": [],
                "stats": {"baseline_peak": base_peak, "baseline_avg": base_avg,
                          "baseline_total": base_total, "final_peak": base_peak},
            }

        # Seed: score each candidate by total losers remaining
        from dataclasses import dataclass
        from typing import Tuple

        @dataclass
        class Node:
            conditions: Tuple[int, ...]
            row_mask: np.ndarray
            total: int

        scored = []
        for ci in self.valid_cands:
            mask = base_mask & self.cand_passes[ci]
            total = self._total_score(mask)
            scored.append((ci, total, mask))
            nodes_explored += 1

        scored.sort(key=lambda x: x[1])

        n_seeds = min(beam_width * 2, len(scored))
        current_level = []
        for ci, total, mask in scored[:n_seeds]:
            current_level.append(Node(conditions=(ci,), row_mask=mask, total=total))

        current_level.sort(key=lambda n: n.total)
        current_level = current_level[:beam_width]

        best = current_level[0]
        levels = [self._level_summary(1, current_level, time.time() - t0)]
        self._print_level(1, current_level, base_total, nodes_explored, time.time() - t0)

        if best.total == 0:
            return self._build_result(best, levels, nodes_explored, t0, base_peak, base_total)

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
                    total = self._total_score(mask)
                    nodes_explored += 1

                    next_level.append(Node(conditions=combo, row_mask=mask, total=total))

                if len(next_level) >= beam_width * 8:
                    break

            if not next_level:
                print(f"\n    ▓ Ceiling at level {lv}")
                break

            next_level.sort(key=lambda n: n.total)
            current_level = next_level[:beam_width]

            if current_level[0].total < best.total:
                best = current_level[0]

            levels.append(self._level_summary(lv, current_level, time.time() - t0))
            self._print_level(lv, current_level, base_total, nodes_explored, time.time() - t0)

            if best.total == 0:
                print(f"\n    ✓ All losers eliminated")
                break

            # Ceiling: if total didn't improve this level, stop
            if len(levels) >= 2 and levels[-1]["best_total"] == levels[-2]["best_total"]:
                print(f"\n    ▓ Ceiling at level {lv} ({best.total} losers remaining)")
                break

        return self._build_result(best, levels, nodes_explored, t0, base_peak, base_total)

    def _build_result(self, best, levels, nodes_explored, t0, baseline_peak, baseline_total):
        peak, avg, total = self._daily_stats(best.row_mask)
        elapsed = time.time() - t0

        cond_list = list(best.conditions)
        loo_totals = []
        for drop_i, ci in enumerate(cond_list):
            without_mask = np.ones(self.n_rows, dtype=bool)
            for j, cj in enumerate(cond_list):
                if j != drop_i:
                    without_mask &= self.cand_passes[cj]
            loo_totals.append(int(np.sum(without_mask)))

        conditions = []
        for drop_i, ci in enumerate(cond_list):
            name = self.candidate_names[ci]
            cat = self.candidate_categories[ci]
            without = loo_totals[drop_i]
            fp = (without - total) / total if total > 0 else 0.0
            conditions.append({
                "expr": name,
                "name": name,
                "category": cat,
                "cand_index": int(ci),
                "signals_with_all": total,
                "signals_without": without,
                "filter_power": round(fp, 4),
            })

        surviving_indices = np.where(best.row_mask)[0]
        final_signals = []
        final_tickers_set = set()
        for idx in surviving_indices:
            date = self.row_dates_list[idx]
            ticker = self.row_tickers_list[idx]
            final_signals.append({"date": str(date)[:10], "ticker": ticker})
            final_tickers_set.add(ticker)
        final_signals.sort(key=lambda s: (s["date"], s["ticker"]))

        eliminated = baseline_total - total
        print(f"\n    Eliminated {eliminated}/{baseline_total} losers "
              f"({eliminated/max(baseline_total,1)*100:.1f}%)")

        return {
            "conditions": conditions,
            "final_peak": peak,
            "final_avg": round(avg, 1),
            "final_total": total,
            "baseline_peak": baseline_peak,
            "baseline_total": baseline_total,
            "losers_eliminated": eliminated,
            "levels": levels,
            "stats": {
                "nodes_explored": nodes_explored,
                "elapsed_s": round(elapsed, 1),
                "depth_reached": len(levels),
            },
            "final_signals": final_signals,
            "final_tickers": sorted(final_tickers_set),
            "n_unique_tickers": len(final_tickers_set),
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

    def _print_level(self, level, nodes, baseline_total, nodes_explored, elapsed):
        best = nodes[0]
        total = best.total
        eliminated = baseline_total - total
        print(f"    Level {level:2d}: {total:>5} remaining  "
              f"(-{eliminated})  |  {len(nodes)} paths  "
              f"{nodes_explored:,} nodes  {elapsed:.1f}s")





class ClusterAwareRefinementSearch:
    """Cluster-aware beam search for refinement grind (step 4).

    Scores by surviving losing CLUSTERS, not individual rows.
    A losing cluster is only eliminated when ALL its bars are dead.

    Must-pass set: winning cluster rightmost bars (bounding boxes).
    Expendable set: all bars from losing clusters + leftward bars from winning clusters.

    OPTIMIZATION: Uses set-based cluster tracking instead of row-mask matrix
    operations in the search loop. Each candidate's "surviving clusters" set
    is pre-computed once at init. The beam search then uses set intersections
    (microseconds) instead of numpy matrix indexing (milliseconds per eval).
    Row masks are only reconstructed for the final best node.
    """

    def __init__(self, candidate_values, row_dates, example_ranges,
                 candidate_names, candidate_categories, row_tickers=None,
                 row_cluster_ids=None, n_losing_clusters=0):
        """
        row_cluster_ids: int array, one per row. >=0 means losing cluster index,
                         -1 means winning leftward bar (sacrificial, no cluster).
        n_losing_clusters: total number of losing clusters.
        """
        self.n_rows, self.n_cands = candidate_values.shape
        self.candidate_names = candidate_names
        self.candidate_categories = candidate_categories
        self.row_dates_list = row_dates
        self.row_tickers_list = row_tickers or (["?"] * self.n_rows)
        self.n_losing_clusters = n_losing_clusters
        self.row_cluster_ids = np.array(row_cluster_ids, dtype=np.int32)

        # Date mapping for output stats
        self.unique_dates = sorted(set(row_dates))
        self.n_dates = len(self.unique_dates)
        date_to_idx = {d: i for i, d in enumerate(self.unique_dates)}
        self.row_date_indices = np.array([date_to_idx[d] for d in row_dates], dtype=np.int32)

        # Precompute: boolean matrix (n_rows x n_losing_clusters) for vectorized scoring
        # cluster_membership[row, cluster] = True if row belongs to that cluster
        self.cluster_membership = np.zeros((self.n_rows, n_losing_clusters), dtype=bool)
        for ci in range(n_losing_clusters):
            indices = np.where(self.row_cluster_ids == ci)[0]
            self.cluster_membership[indices, ci] = True

        # Also keep index lists for _build_result
        self.cluster_row_indices = []
        for ci in range(n_losing_clusters):
            self.cluster_row_indices.append(np.where(self.row_cluster_ids == ci)[0])

        # Precompute pass/fail per candidate
        self.cand_passes = np.zeros((self.n_cands, self.n_rows), dtype=bool)
        self.valid_cands = []

        for ci in range(self.n_cands):
            name = candidate_names[ci]
            if name not in example_ranges:
                self.cand_passes[ci, :] = True
                continue
            low, high = example_ranges[name]
            vals = candidate_values[:, ci]
            passes = ((vals >= low) & (vals <= high)) | np.isnan(vals)
            self.cand_passes[ci, :] = passes

            if np.sum(passes) < self.n_rows:
                self.valid_cands.append(ci)

        print(f"    ClusterAwareRefinementSearch: {self.n_rows:,} rows, "
              f"{n_losing_clusters} losing clusters, "
              f"{len(self.valid_cands)} useful candidates out of {self.n_cands}")

        # ── Pre-compute per-candidate surviving cluster sets (vectorized) ──
        # Matrix multiply: cand_passes (n_valid × n_rows) @ cluster_membership (n_rows × n_clusters)
        # Result: (n_valid × n_clusters) — nonzero means that cluster has at least one surviving row.
        # Then convert each row to a frozenset of surviving cluster indices.
        t_pre = time.time()
        all_clusters = frozenset(range(n_losing_clusters))
        self.cand_surviving_clusters = {}

        if self.valid_cands and n_losing_clusters > 0:
            # Extract valid candidate pass rows as a contiguous matrix
            valid_indices = np.array(self.valid_cands, dtype=np.int32)
            valid_passes = self.cand_passes[valid_indices]  # (n_valid, n_rows) bool

            # Matrix multiply: bool @ bool → int. Nonzero = cluster has surviving rows.
            # Use float32 to avoid bool multiplication issues, then threshold
            survival_matrix = valid_passes.astype(np.float32) @ self.cluster_membership.astype(np.float32)
            # survival_matrix[i, j] > 0 means candidate i leaves cluster j alive

            for idx, ci in enumerate(self.valid_cands):
                surviving_mask = survival_matrix[idx] > 0
                self.cand_surviving_clusters[ci] = frozenset(np.where(surviving_mask)[0].tolist())

        # Also store the full set for baseline
        self.all_clusters = all_clusters

        pre_time = time.time() - t_pre
        print(f"    Pre-computed cluster sets for {len(self.valid_cands)} candidates ({pre_time:.1f}s)")

    def _cluster_score(self, row_mask):
        """Score = number of losing clusters with at least one surviving row. Lower is better."""
        # row_mask is bool array (n_rows,). For each cluster, check if ANY member row survives.
        # cluster_membership is (n_rows, n_clusters). Mask out dead rows, check columns.
        return int(np.any(self.cluster_membership[row_mask], axis=0).sum())

    def _daily_stats(self, row_mask):
        """Return (peak, avg, total) for reporting."""
        if not np.any(row_mask):
            return 0, 0.0, 0
        active_dates = self.row_date_indices[row_mask]
        counts = np.bincount(active_dates, minlength=self.n_dates)
        nonzero = counts[counts > 0]
        total = int(np.sum(row_mask))
        peak = int(np.max(counts))
        avg = float(np.mean(nonzero)) if len(nonzero) > 0 else 0.0
        return peak, avg, total

    def run(self, depth=100, beam_width=10000, peak_target=3):
        """Run beam search minimizing surviving losing clusters.

        Uses set-based cluster tracking with threaded beam expansion.
        Each node carries a frozenset of surviving cluster indices.
        Adding a candidate = set intersection with pre-computed set.
        Frozenset operations release the GIL, so ThreadPoolExecutor
        gives real parallelism with zero memory duplication.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t0 = time.time()
        nodes_explored = 0

        if not self.valid_cands:
            return {"error": "No useful candidates", "conditions": [], "levels": []}

        base_cluster_score = self.n_losing_clusters
        base_mask = np.ones(self.n_rows, dtype=bool)
        base_peak, base_avg, base_row_total = self._daily_stats(base_mask)
        print(f"\n    ClusterAwareRefinementSearch: depth={depth}, beam={beam_width}")
        print(f"    Baseline: {base_cluster_score} surviving clusters, "
              f"{base_row_total:,} expendable rows")

        if base_cluster_score == 0:
            print(f"    No losing clusters to eliminate.")
            return {
                "conditions": [], "levels": [],
                "stats": {"baseline_peak": base_peak, "baseline_avg": base_avg,
                          "baseline_total": base_cluster_score, "final_peak": base_peak},
            }

        # Node: (conditions_tuple, surviving_clusters_frozenset)
        # No row_mask stored during search — reconstructed only for final winner

        # Seed: score each candidate by surviving clusters (set intersection)
        scored = []
        for ci in self.valid_cands:
            surviving = self.cand_surviving_clusters[ci]
            scored.append((ci, len(surviving), surviving))
            nodes_explored += 1

        scored.sort(key=lambda x: x[1])

        n_seeds = min(beam_width * 2, len(scored))
        # current_level: list of (conditions_tuple, surviving_clusters_frozenset)
        current_level = []
        for ci, cs, surviving in scored[:n_seeds]:
            current_level.append(((ci,), surviving))

        current_level.sort(key=lambda n: len(n[1]))
        current_level = current_level[:beam_width]

        best_conditions = current_level[0][0]
        best_surviving = current_level[0][1]
        best_score = len(best_surviving)

        levels = [self._level_summary_fast(1, current_level, time.time() - t0)]
        self._print_level_fast(1, current_level, base_cluster_score, nodes_explored, time.time() - t0)

        if best_score == 0:
            best_node = self._make_result_node(best_conditions, best_score)
            return self._build_result(best_node, levels, nodes_explored, t0,
                                      base_peak, base_cluster_score)

        # Deepen — threaded beam expansion
        # Frozenset intersection releases the GIL → threads run in parallel
        # All threads share the same cand_surviving_clusters dict (read-only)
        n_threads = max(cpu_count() - 1, 1)
        cand_surv = self.cand_surviving_clusters  # local ref for thread closure
        valid_cands = self.valid_cands

        def _expand_batch(batch):
            """Expand a batch of beam nodes. Runs in a thread."""
            results = []
            for conditions, surviving in batch:
                if len(surviving) == 0:
                    continue
                used = set(conditions)
                for ci in valid_cands:
                    if ci in used:
                        continue
                    new_surviving = surviving & cand_surv[ci]
                    combo = tuple(sorted(conditions + (ci,)))
                    results.append((combo, new_surviving, frozenset(conditions + (ci,))))
            return results

        for lv in range(2, depth + 1):
            # Split beam nodes into batches for threads
            batch_size = max(len(current_level) // (n_threads * 2), 1)
            batches = [current_level[i:i+batch_size]
                       for i in range(0, len(current_level), batch_size)]

            # Submit batches to thread pool
            next_level = []
            seen = set()

            with ThreadPoolExecutor(max_workers=n_threads) as pool:
                futures = [pool.submit(_expand_batch, batch) for batch in batches]
                for future in as_completed(futures):
                    batch_results = future.result()
                    for combo, new_surviving, combo_key in batch_results:
                        if combo_key in seen:
                            continue
                        seen.add(combo_key)
                        next_level.append((combo, new_surviving))
                        nodes_explored += 1

            if not next_level:
                print(f"\n    \u2593 Ceiling at level {lv}")
                break

            next_level.sort(key=lambda n: len(n[1]))
            current_level = next_level[:beam_width]

            if len(current_level[0][1]) < best_score:
                best_conditions = current_level[0][0]
                best_surviving = current_level[0][1]
                best_score = len(best_surviving)

            levels.append(self._level_summary_fast(lv, current_level, time.time() - t0))
            self._print_level_fast(lv, current_level, base_cluster_score, nodes_explored, time.time() - t0)

            if best_score == 0:
                print(f"\n    \u2713 All losing clusters eliminated")
                break

            # Ceiling: if score didn't improve this level, stop
            if len(levels) >= 2 and levels[-1]["best_cluster_score"] == levels[-2]["best_cluster_score"]:
                print(f"\n    \u2593 Ceiling at level {lv} ({best_score} clusters remaining)")
                break

        # Reconstruct row_mask for the best node (needed by _build_result)
        best_node = self._make_result_node(best_conditions, best_score)
        return self._build_result(best_node, levels, nodes_explored, t0,
                                  base_peak, base_cluster_score)


    def _make_result_node(self, conditions, cluster_score):
        """Reconstruct a Node-like object with row_mask for _build_result."""
        from dataclasses import dataclass
        from typing import Tuple

        @dataclass
        class Node:
            conditions: Tuple[int, ...]
            row_mask: np.ndarray
            cluster_score: int

        # Reconstruct row_mask by ANDing all condition passes
        mask = np.ones(self.n_rows, dtype=bool)
        for ci in conditions:
            mask &= self.cand_passes[ci]
        return Node(conditions=conditions, row_mask=mask, cluster_score=cluster_score)

    def _level_summary_fast(self, level, nodes, elapsed):
        """Level summary using (conditions, surviving_set) tuples."""
        best_conds, best_surviving = nodes[0]
        return {
            "level": level,
            "best_cluster_score": len(best_surviving),
            "n_conditions": len(best_conds),
            "best_condition_indices": list(best_conds),
            "best_condition_names": [self.candidate_names[ci] for ci in best_conds],
            "paths_explored": len(nodes),
            "elapsed_s": round(elapsed, 1),
        }

    def _print_level_fast(self, level, nodes, baseline_clusters, nodes_explored, elapsed):
        """Print level using (conditions, surviving_set) tuples."""
        best_conds, best_surviving = nodes[0]
        score = len(best_surviving)
        eliminated = baseline_clusters - score
        print(f"    Level {level:2d}: {score:>5} clusters remaining  "
              f"(-{eliminated})  |  {len(nodes)} paths  "
              f"{nodes_explored:,} nodes  {elapsed:.1f}s")

    def _build_result(self, best, levels, nodes_explored, t0, baseline_peak, baseline_clusters):
        peak, avg, row_total = self._daily_stats(best.row_mask)
        elapsed = time.time() - t0

        cond_list = list(best.conditions)

        # Leave-one-out: how many clusters survive without each condition
        loo_scores = []
        for drop_i, ci in enumerate(cond_list):
            without_mask = np.ones(self.n_rows, dtype=bool)
            for j, cj in enumerate(cond_list):
                if j != drop_i:
                    without_mask &= self.cand_passes[cj]
            loo_scores.append(self._cluster_score(without_mask))

        conditions = []
        for drop_i, ci in enumerate(cond_list):
            name = self.candidate_names[ci]
            cat = self.candidate_categories[ci]
            without = loo_scores[drop_i]
            fp = (without - best.cluster_score) / best.cluster_score if best.cluster_score > 0 else 0.0
            conditions.append({
                "expr": name,
                "name": name,
                "category": cat,
                "cand_index": int(ci),
                "clusters_with_all": best.cluster_score,
                "clusters_without": without,
                "filter_power": round(fp, 4),
            })

        # Determine which losing clusters survived vs were eliminated
        surviving_cluster_indices = set()
        eliminated_cluster_indices = set()
        for ci, indices in enumerate(self.cluster_row_indices):
            if np.any(best.row_mask[indices]):
                surviving_cluster_indices.add(ci)
            else:
                eliminated_cluster_indices.add(ci)

        # Build surviving row info for reporting
        surviving_indices = np.where(best.row_mask)[0]
        final_signals = []
        final_tickers_set = set()
        for idx in surviving_indices:
            date = self.row_dates_list[idx]
            ticker = self.row_tickers_list[idx]
            final_signals.append({"date": str(date)[:10], "ticker": ticker})
            final_tickers_set.add(ticker)
        final_signals.sort(key=lambda s: (s["date"], s["ticker"]))

        eliminated = baseline_clusters - best.cluster_score
        print(f"\n    Eliminated {eliminated}/{baseline_clusters} losing clusters "
              f"({eliminated/max(baseline_clusters,1)*100:.1f}%)")
        print(f"    Surviving clusters: {best.cluster_score}")
        print(f"    Surviving expendable rows: {row_total}")

        return {
            "conditions": conditions,
            "final_peak": peak,
            "final_avg": round(avg, 1),
            "final_total": best.cluster_score,
            "baseline_peak": baseline_peak,
            "baseline_total": baseline_clusters,
            "losers_eliminated": eliminated,
            "surviving_cluster_indices": surviving_cluster_indices,
            "eliminated_cluster_indices": eliminated_cluster_indices,
            "levels": levels,
            "stats": {
                "nodes_explored": nodes_explored,
                "elapsed_s": round(elapsed, 1),
                "depth_reached": len(levels),
            },
            "final_signals": final_signals,
            "final_tickers": sorted(final_tickers_set),
            "n_unique_tickers": len(final_tickers_set),
        }

    def _level_summary(self, level, nodes, elapsed):
        best = nodes[0]
        return {
            "level": level,
            "best_cluster_score": best.cluster_score,
            "n_conditions": len(best.conditions),
            "best_condition_indices": list(best.conditions),
            "best_condition_names": [self.candidate_names[ci] for ci in best.conditions],
            "paths_explored": len(nodes),
            "elapsed_s": round(elapsed, 1),
        }

    def _print_level(self, level, nodes, baseline_clusters, nodes_explored, elapsed):
        best = nodes[0]
        eliminated = baseline_clusters - best.cluster_score
        print(f"    Level {level:2d}: {best.cluster_score:>5} clusters remaining  "
              f"(-{eliminated})  |  {len(nodes)} paths  "
              f"{nodes_explored:,} nodes  {elapsed:.1f}s")



# ══════════════════════════════════════════════════════════════
# D1 TIER (uses existing SpiderwebSearch for last-bar matrix)
# ══════════════════════════════════════════════════════════════

def run_d1_tier(universe_cache, expressions, example_ranges, example_matrix,
                beam_width=50, depth=10):
    """Run D1 tier using the classic spiderweb on today's snapshot.

    Uses cached universe matrix from matrix_builder (all expressions), then filters
    to the expressions passed in (which may be a timeframe subset for multi-pass mode).
    Returns list of condition dicts with {name, category, compute, low, high}.
    """
    from spiderweb import SpiderwebSearch
    from matrix_builder import get_universe_matrix

    print(f"\n  ═══ D1: Loading universe matrix (cached if fresh) ═══")
    t0 = time.time()

    # Load the full universe matrix (all expressions)
    uni_data = get_universe_matrix()
    full_uni_matrix = uni_data["universe_matrix"]
    tickers = uni_data["universe_tickers"]
    full_expr_names = uni_data["expr_names"]
    full_expr_categories = uni_data["expr_categories"]

    # Build column subset: only the expressions in our pass
    pass_expr_names = [e["name"] for e in expressions]
    pass_expr_set = set(pass_expr_names)

    # Map from full matrix column index to our pass expression index
    full_name_to_col = {name: i for i, name in enumerate(full_expr_names)}
    col_indices = []
    for name in pass_expr_names:
        idx = full_name_to_col.get(name)
        if idx is not None:
            col_indices.append(idx)
        else:
            col_indices.append(-1)  # not in matrix

    # Filter matrices to pass-specific columns
    valid_cols = [i for i in col_indices if i >= 0]
    valid_pass_indices = [j for j, i in enumerate(col_indices) if i >= 0]

    if len(valid_cols) == len(pass_expr_names):
        # All expressions found in full matrix — just slice columns
        uni_matrix = full_uni_matrix[:, valid_cols]
        expr_names = [pass_expr_names[j] for j in valid_pass_indices]
        expr_categories = [expressions[j].get("category", "unknown") for j in valid_pass_indices]
        # Also filter example_matrix columns
        filtered_example_matrix = example_matrix[:, valid_pass_indices]
    else:
        # Some expressions missing — build with what we have
        uni_matrix = full_uni_matrix[:, valid_cols]
        expr_names = [pass_expr_names[j] for j in valid_pass_indices]
        expr_categories = [expressions[j].get("category", "unknown") for j in valid_pass_indices]
        filtered_example_matrix = example_matrix[:, valid_pass_indices]
        n_missing = len(pass_expr_names) - len(valid_cols)
        print(f"  ⚠ {n_missing} expressions not in universe matrix (likely HTF/LSP)")

    print(f"  D1 matrix: {len(tickers)} tickers × {len(expr_names)} expressions ({time.time()-t0:.1f}s)")

    # Run spiderweb
    search = SpiderwebSearch(
        example_values=filtered_example_matrix,
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
                        expr_cache=None, blackout_map=None, whitelist_map=None):
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
        blackout_map: {ticker: [(entry_idx, exit_idx), ...]} — post-entry bars to exclude
        whitelist_map: {ticker: set(bar_idx)} — if set, only these bars count

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

    # Expression cache is REQUIRED — all grinders use the same computation path
    if expr_cache is None or not expr_cache.is_valid():
        raise RuntimeError(
            "Expression series cache is REQUIRED for historical tier matrix build. "
            "No fallback computation paths allowed."
        )
    expr_name_to_idx = dict(expr_cache._expr_name_to_idx)
    print(f"  Using expression series cache ({expr_cache.n_expressions} expressions)")

    print(f"  {n_workers} workers, {len(batches)} batches of ~{batch_size} tickers")

    all_row_dates = []
    all_row_tickers = []
    all_row_values = []

    # Build slim cache: workers only need bar count per ticker
    # Avoids serializing full DataFrames to each worker process
    slim_cache = {ticker: len(df) for ticker, df in universe_cache.items()
                  if df is not None and len(df) >= 50}

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_tier_worker,
        initargs=(slim_cache, locked_conditions, expressions,
                  example_ranges, candidate_indices, n_bars_window,
                  expr_name_to_idx, blackout_map, whitelist_map)
    ) as pool:
        futures = {pool.submit(_build_tier_batch, batch): batch for batch in batches}
        completed = 0
        for future in as_completed(futures):
            batch_results = future.result()
            for ticker, row_dates, cand_values in batch_results:
                if row_dates and cand_values is not None and len(row_dates) > 0:
                    all_row_dates.extend(row_dates)
                    all_row_tickers.extend([ticker] * len(row_dates))
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
        row_tickers=all_row_tickers,
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
            "filter_power": cond.get("filter_power"),
            "signals_with_all": cond.get("signals_with_all"),
            "signals_without": cond.get("signals_without"),
        })

    return new_conditions, result


# ══════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_examples(example_dfs, conditions, expr_cache=None):
    """Verify 100% of examples pass all conditions at scan bar.

    Uses expr cache (REQUIRED — all grinders must use the same computation path).
    """
    if expr_cache is None or not expr_cache.is_valid():
        raise RuntimeError(
            "Expression series cache is REQUIRED for validation. "
            "No fallback computation paths allowed."
        )

    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)

    all_pass = True
    for ex in example_dfs:
        if ex["scan_idx"] is None:
            continue

        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]
        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None or scan_idx >= len(data):
            print(f"    ✗ {ticker} — not in expr cache or scan_idx out of range")
            all_pass = False
            continue

        cached_row = data[scan_idx, :]
        for cond in conditions:
            col_idx = cache_name_to_idx.get(cond["name"])
            if col_idx is None:
                print(f"    ✗ {ticker} FAILS {cond['name']}: expression not in cache")
                all_pass = False
                continue
            val = float(cached_row[col_idx])
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                print(f"    ✗ {ticker} FAILS {cond['name']}: "
                      f"{val:.4f} not in [{cond['low']:.4f}, {cond['high']:.4f}]")
                all_pass = False
    return all_pass


# ══════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def _filter_expressions_by_timeframe(expressions, timeframe):
    """Filter expression list to a specific timeframe.

    Args:
        expressions: full expression list from generate_all()
        timeframe: 'daily' (daily + LSP + algo), 'weekly' (htf_weekly), 'monthly' (htf_monthly)

    Returns:
        filtered list of expression dicts
    """
    if timeframe == "daily":
        return [e for e in expressions if e["category"] not in ("htf_weekly", "htf_monthly")]
    elif timeframe == "weekly":
        return [e for e in expressions if e["category"] == "htf_weekly"]
    elif timeframe == "monthly":
        return [e for e in expressions if e["category"] == "htf_monthly"]
    else:
        return expressions


# Pass definitions for multi-pass pyramid
# (pass_name, timeframe, tiers_to_run)
# tiers_to_run = list of (tier_name, n_bars) from TIERS to use in this pass
MULTI_PASS_DEFS = [
    ("Pass 1 (Daily+LSP+Algo)", "daily", [
        ("D1",   1),
        ("1wk",  5),
        ("1mo",  21),
        ("6mo",  126),
        ("1yr",  252),
        ("5yr",  0),
    ]),
    ("Pass 2 (Weekly)", "weekly", [
        ("1mo",  21),
        ("6mo",  126),
        ("1yr",  252),
        ("5yr",  0),
    ]),
    ("Pass 3 (Monthly)", "monthly", [
        ("6mo",  126),
        ("1yr",  252),
        ("5yr",  0),
    ]),
]


def _run_single_pass(pass_name, pass_expressions, pass_tiers,
                     universe_cache, example_dfs, all_expressions,
                     example_ranges_full, example_matrix_full,
                     locked_conditions, expr_cache,
                     beam_width, depth, peak_target,
                     d1_beam, d1_depth, blackout_map=None, whitelist_map=None):
    """Run one pass of the multi-pass pyramid.

    Args:
        pass_name: display name for logging
        pass_expressions: filtered expression list for this pass's timeframe
        pass_tiers: list of (tier_name, n_bars) to run
        universe_cache: {ticker: DataFrame}
        example_dfs: list of example dicts
        all_expressions: FULL expression list (needed for D1 matrix builder)
        example_ranges_full: ranges computed from ALL expressions
        example_matrix_full: matrix computed from ALL expressions
        locked_conditions: conditions locked from previous passes
        expr_cache: ExprSeriesCache instance
        beam_width, depth, peak_target: search params
        d1_beam, d1_depth: D1-specific params
        blackout_map: {ticker: [(entry_idx, exit_idx), ...]} — post-entry bars to exclude
        whitelist_map: {ticker: set(bar_idx)} — if set, only these bars count

    Returns:
        new_conditions: list of conditions added by this pass
        tier_results: dict of tier-level results
    """
    print(f"\n{'▓'*70}")
    print(f"  {pass_name}")
    print(f"  {len(pass_expressions)} candidate expressions, "
          f"{len(locked_conditions)} locked conditions")
    print(f"  Tiers: {' → '.join(t[0] for t in pass_tiers)}")
    print(f"{'▓'*70}")

    # Build example ranges for THIS pass's expressions only
    # (We need ranges for the pass-specific candidates, but also need
    # existing locked conditions to still work via the full ranges)
    pass_ranges, pass_matrix = compute_example_ranges(
        example_dfs, pass_expressions, expr_cache=expr_cache)
    print(f"  {len(pass_ranges)} expressions have valid ranges for this pass")

    new_conditions = []
    tier_results = {}

    for tier_name, n_bars in pass_tiers:

        # D1 tier uses special spiderweb path
        if tier_name == "D1":
            # Skip D1 in refinement mode — D1 uses full universe matrix,
            # not the whitelist. It can't compare winners vs losers.
            if whitelist_map is not None:
                print(f"\n{'─'*70}")
                print(f"  TIER: D1 — SKIPPED (refinement mode)")
                print(f"{'─'*70}")
                continue

            print(f"\n{'─'*70}")
            print(f"  TIER: D1 — Today (last bar) [{pass_name}]")
            print(f"  Locked: {len(locked_conditions)} conditions from prior passes/tiers")
            print(f"{'─'*70}")

            d1_conditions, d1_result, d1_info = run_d1_tier(
                universe_cache, pass_expressions, pass_ranges, pass_matrix,
                beam_width=d1_beam, depth=d1_depth,
            )

            new_conditions.extend(d1_conditions)
            tier_results["D1"] = {
                "conditions_added": len(d1_conditions),
                "pass_rate": d1_info.get("pass_rate", 0),
                "n_passing": d1_info.get("n_passing", 0),
                "passing_tickers": d1_info.get("passing_tickers", []),
            }

            print(f"\n  D1 result: {len(d1_conditions)} conditions, "
                  f"{d1_info['n_passing']} tickers passing ({d1_info['pass_rate']:.2%})")

            # Validate
            all_so_far = locked_conditions + new_conditions
            if all_so_far:
                print(f"\n  Validating examples after D1...")
                if not validate_examples(example_dfs, all_so_far, expr_cache=expr_cache):
                    print(f"\n{'!'*80}")
                    print(f"VALIDATION FAILED after D1 — dropping D1 conditions, continuing.")
                    print(f"{'!'*80}")
                    new_conditions = []  # discard D1 conditions that broke validation

            continue

        # Historical tiers (1wk through 5yr)
        description = next((d for n, _, d in TIERS if n == tier_name), tier_name)

        print(f"\n{'─'*70}")
        print(f"  TIER: {tier_name} — {description} [{pass_name}]")
        all_so_far = locked_conditions + new_conditions
        print(f"  Locked: {len(all_so_far)} conditions (pass-inherited + this pass)")
        print(f"{'─'*70}")

        tier_new_conds, tier_result = run_historical_tier(
            tier_name=tier_name,
            n_bars_window=n_bars,
            universe_cache=universe_cache,
            expressions=pass_expressions,
            example_ranges=pass_ranges,
            locked_conditions=all_so_far,
            beam_width=beam_width,
            depth=depth,
            peak_target=peak_target,
            expr_cache=expr_cache,
            blackout_map=blackout_map,
            whitelist_map=whitelist_map,
        )

        new_conditions.extend(tier_new_conds)

        tier_results[tier_name] = {
            "conditions_added": len(tier_new_conds),
            "conditions": [c["name"] for c in tier_new_conds],
            "final_peak": tier_result.get("final_peak"),
            "final_avg": tier_result.get("final_avg"),
            "final_total": tier_result.get("final_total"),
            "baseline_peak": tier_result.get("baseline_peak"),
            "stats": tier_result.get("stats"),
            "final_signals": tier_result.get("final_signals", []),
            "n_unique_tickers": tier_result.get("n_unique_tickers", 0),
        }

        if tier_new_conds:
            print(f"\n  {tier_name} added {len(tier_new_conds)} conditions:")
            for c in tier_new_conds:
                print(f"    + [{c['category']:>18}] {c['name']}")
            print(f"  Peak: {tier_result.get('baseline_peak')} → "
                  f"{tier_result.get('final_peak')}/day")

            # Validate
            all_so_far = locked_conditions + new_conditions
            print(f"\n  Validating examples after {tier_name}...")
            if not validate_examples(example_dfs, all_so_far, expr_cache=expr_cache):
                print(f"\n{'!'*80}")
                print(f"VALIDATION FAILED after {tier_name} — dropping {tier_name} conditions, continuing.")
                print(f"{'!'*80}")
                # Roll back only the conditions added by this tier
                new_conditions = new_conditions[:-len(tier_new_conds)]
        else:
            if tier_result.get("skipped"):
                print(f"  {tier_name}: Skipped ({tier_result.get('reason', 'no candidates')})")
            else:
                print(f"  {tier_name}: No conditions added (peak ≤{peak_target} or ceiling)")

        # Early stop if zero signals
        final_peak = tier_result.get("final_peak")
        if final_peak is not None and final_peak == 0:
            print(f"  Zero signals — stopping pass early.")
            break

    return new_conditions, tier_results


def run_pyramid(setup_type, peak_target=15, beam_width=50, depth=10,
                d1_depth=None, d1_beam=None, multi_pass=True,
                blackout_map=None, whitelist_map=None,
                override_example_dfs=None):
    """Run the full pyramid grinder.

    Args:
        setup_type: e.g. "dtss"
        peak_target: target for max signals/day at each historical tier
        beam_width: beam width for historical tiers
        depth: search depth for historical tiers
        d1_depth: override depth for D1 tier (default: same as depth)
        d1_beam: override beam for D1 tier (default: same as beam_width)
        multi_pass: if True, run 3-pass pyramid (daily→weekly→monthly).
                    if False, run single-pass with all expressions (legacy mode).
        blackout_map: {ticker: [(entry_idx, exit_idx), ...]} — post-entry bars to exclude
                      from universe matrix. Pass None to disable (default).
        whitelist_map: {ticker: set(bar_idx)} — if set, only these bars count as signals.
        override_example_dfs: if set, use these instead of load_example_data.
    """
    d1_depth = d1_depth or depth
    d1_depth = min(d1_depth, 15)  # Cap D1 — more than 15 overfits to today's snapshot
    d1_beam = d1_beam or beam_width

    print("\n" + "=" * 70)
    print("  PYRAMIDAL GRINDER" + (" — MULTI-PASS" if multi_pass else " — SINGLE-PASS"))
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Peak target: ≤{peak_target} signals/day")
    print(f"  D1: beam={d1_beam}, depth={d1_depth}")
    print(f"  Historical tiers: beam={beam_width}, depth={depth}")
    if multi_pass:
        print(f"  Mode: 3-pass (daily→weekly→monthly)")

    t_total = time.time()

    # ── Load data ──
    print(f"\n  Loading OHLCV cache...")
    universe_cache = load_5yr_cache()
    print(f"  {len(universe_cache)} tickers loaded")

    print(f"\n  Loading examples...")
    if override_example_dfs is not None:
        example_dfs = override_example_dfs
        print(f"  {len(example_dfs)} examples (override — win pile)")
    else:
        example_dfs = load_example_data(setup_type, universe_cache)
        print(f"  {len(example_dfs)} examples loaded")

    print(f"\n  Loading expressions...")
    # Signal expressions for grinding (what the grinder actually uses)
    all_expressions = generate_all()
    print(f"  {len(all_expressions)} signal expressions for grinding")

    if multi_pass:
        daily_exprs = _filter_expressions_by_timeframe(all_expressions, "daily")
        weekly_exprs = _filter_expressions_by_timeframe(all_expressions, "weekly")
        monthly_exprs = _filter_expressions_by_timeframe(all_expressions, "monthly")
        print(f"    Daily+LSP+Algo: {len(daily_exprs)}  Weekly: {len(weekly_exprs)}  "
              f"Monthly: {len(monthly_exprs)}")

    # ── Detect expression series cache ──
    print(f"\n  Detecting expression cache...")
    expr_cache = ExprSeriesCache()
    if expr_cache.is_valid():
        n_cached = len(expr_cache.get_available_tickers())
        print(f"  Expression series cache: {n_cached} tickers, "
              f"{expr_cache.n_expressions} expressions")

        # Filter examples to those in expr cache
        cached_tickers = expr_cache.get_available_tickers()
        before_count = len(example_dfs)
        excluded = []
        filtered_dfs = []
        for ex in example_dfs:
            if ex["ticker"] in cached_tickers:
                n_cached_bars = expr_cache.get_ticker_bar_count(ex["ticker"])
                if ex["scan_idx"] < n_cached_bars:
                    filtered_dfs.append(ex)
                else:
                    excluded.append(f"{ex['ticker']} (scan_idx {ex['scan_idx']} >= {n_cached_bars} cached bars)")
            else:
                excluded.append(f"{ex['ticker']} (not in expr cache)")
        example_dfs = filtered_dfs
        if excluded:
            print(f"  ⚠ Excluded {len(excluded)} examples not in expr cache:")
            for e in excluded:
                print(f"    - {e}")
            print(f"  Examples: {before_count} → {len(example_dfs)}")
    else:
        raise RuntimeError(
            "Expression series cache not found or invalid.\n"
            "Run: python local_runner/expr_cache_builder.py --build"
        )

    # ══════════════════════════════════════════════════════════════
    # MULTI-PASS MODE
    # ══════════════════════════════════════════════════════════════
    if multi_pass:
        all_conditions = []
        all_tier_results = {}
        pass_summaries = []

        for pass_def in MULTI_PASS_DEFS:
            pass_name, timeframe, pass_tier_defs = pass_def
            pass_exprs = _filter_expressions_by_timeframe(all_expressions, timeframe)

            t_pass = time.time()
            new_conds, tier_results = _run_single_pass(
                pass_name=pass_name,
                pass_expressions=pass_exprs,
                pass_tiers=pass_tier_defs,
                universe_cache=universe_cache,
                example_dfs=example_dfs,
                all_expressions=all_expressions,
                example_ranges_full=None,  # computed inside _run_single_pass
                example_matrix_full=None,
                locked_conditions=list(all_conditions),  # copy — prior passes locked
                expr_cache=expr_cache,
                beam_width=beam_width,
                depth=depth,
                peak_target=peak_target,
                d1_beam=d1_beam,
                d1_depth=d1_depth,
                blackout_map=blackout_map,
                whitelist_map=whitelist_map,
            )

            if new_conds is None:
                # Should not happen — _run_single_pass no longer aborts
                # but guard defensively: skip this pass, continue
                print(f"  WARNING: pass returned None — skipping, continuing with next pass")
                new_conds = []
                tier_results = {}

            pass_time = time.time() - t_pass
            all_conditions.extend(new_conds)
            if tier_results:
                all_tier_results.update({
                    f"{timeframe}_{k}": v for k, v in tier_results.items()
                })

            pass_summaries.append({
                "pass_name": pass_name,
                "timeframe": timeframe,
                "conditions_added": len(new_conds),
                "total_conditions": len(all_conditions),
                "time_s": round(pass_time, 1),
                "conditions": [c["name"] for c in new_conds],
            })

            print(f"\n  ═══ {pass_name} complete: +{len(new_conds)} conditions "
                  f"({len(all_conditions)} total) [{pass_time:.0f}s] ═══")

        tier_results = all_tier_results

    # ══════════════════════════════════════════════════════════════
    # SINGLE-PASS MODE (legacy)
    # ══════════════════════════════════════════════════════════════
    else:
        expressions = all_expressions

        # Compute example ranges for all expressions
        print(f"\n  Computing example ranges...")
        t0 = time.time()
        example_ranges, example_matrix = compute_example_ranges(
            example_dfs, expressions, expr_cache=expr_cache)
        print(f"  {len(example_ranges)} expressions have valid ranges ({time.time()-t0:.0f}s)")

        all_conditions = []
        tier_results = {}
        pass_summaries = None

        # D1 tier
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

        if all_conditions:
            print(f"\n  Validating examples after D1...")
            if not validate_examples(example_dfs, all_conditions, expr_cache=expr_cache):
                print(f"\n{'!'*80}")
                print(f"VALIDATION FAILED after D1 — dropping D1 conditions, continuing.")
                print(f"{'!'*80}")
                all_conditions = []  # discard D1 conditions that broke validation

        # Historical tiers
        for tier_name, n_bars, description in TIERS[1:]:
            print(f"\n{'─'*70}")
            print(f"  TIER: {tier_name} — {description}")
            print(f"  Locked: {len(all_conditions)} conditions")
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
                blackout_map=blackout_map,
                whitelist_map=whitelist_map,
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
                "final_signals": tier_result.get("final_signals", []),
                "n_unique_tickers": tier_result.get("n_unique_tickers", 0),
            }

            if new_conds:
                print(f"\n  {tier_name} added {len(new_conds)} conditions:")
                for c in new_conds:
                    print(f"    + [{c['category']:>18}] {c['name']}")
                print(f"  Peak: {tier_result.get('baseline_peak')} → "
                      f"{tier_result.get('final_peak')}/day")
                print(f"\n  Validating examples after {tier_name}...")
                if not validate_examples(example_dfs, all_conditions, expr_cache=expr_cache):
                    print(f"\n{'!'*80}")
                    print(f"VALIDATION FAILED after {tier_name} — dropping {tier_name} conditions, continuing.")
                    print(f"{'!'*80}")
                    # Roll back only the conditions added by this tier
                    all_conditions = all_conditions[:-len(new_conds)]
            else:
                if tier_result.get("skipped"):
                    print(f"  {tier_name}: Skipped ({tier_result.get('reason', 'no candidates')})")
                else:
                    print(f"  {tier_name}: No conditions added")

            final_peak = tier_result.get("final_peak")
            if final_peak is not None and final_peak == 0:
                print(f"  Zero signals — stopping early.")
                break

    # ══════════════════════════════════════════════════════════════
    # FINAL SUMMARY (shared by both modes)
    # ══════════════════════════════════════════════════════════════
    total_time = time.time() - t_total

    print(f"\n{'='*70}")
    print(f"  PYRAMID COMPLETE" + (" — MULTI-PASS" if multi_pass else ""))
    print(f"{'='*70}")
    print(f"  Total conditions: {len(all_conditions)}")
    print(f"  Total time: {total_time:.0f}s ({total_time/60:.1f} min)")

    if multi_pass and pass_summaries:
        print(f"\n  Pass summary:")
        for ps in pass_summaries:
            print(f"    {ps['pass_name']}: +{ps['conditions_added']} conditions "
                  f"[{ps['time_s']:.0f}s]")

    # Count conditions by tier
    tier_counts = defaultdict(int)
    for c in all_conditions:
        tier_counts[c.get("tier", "?")] += 1
    if tier_counts:
        print(f"\n  Conditions by tier:")
        for tier, cnt in sorted(tier_counts.items()):
            print(f"    {tier:>4}: {cnt} conditions")

    # Count by timeframe category
    cat_counts = defaultdict(int)
    for c in all_conditions:
        cat = c.get("category", "unknown")
        if cat == "htf_weekly":
            cat_counts["weekly"] += 1
        elif cat == "htf_monthly":
            cat_counts["monthly"] += 1
        elif cat == "lsp":
            cat_counts["lsp"] += 1
        else:
            cat_counts["daily"] += 1
    print(f"\n  Conditions by timeframe:")
    for tf, cnt in sorted(cat_counts.items()):
        print(f"    {tf:>8}: {cnt}")

    print(f"\n  All conditions:")
    for i, c in enumerate(all_conditions, 1):
        tier = c.get("tier", "?")
        cat = c.get("category", "unknown")
        print(f"    {i:2d}. [{tier:>4}] [{cat:>18}] {c['name']:35s} "
              f"[{c['low']:.4f} — {c['high']:.4f}]")

    # ── Get real signal count from last 5yr tier (full history, all conditions) ──
    # In multi-pass: monthly_5yr > weekly_5yr > daily_5yr (last pass has all conditions)
    # In single-pass: just "5yr"
    final_total = 0
    final_peak = 0
    final_avg = 0.0
    final_deduped_signals = []
    for pass_prefix in ["monthly_5yr", "weekly_5yr", "daily_5yr", "5yr"]:
        if pass_prefix in tier_results:
            tr = tier_results[pass_prefix]
            sigs = tr.get("final_signals", [])
            if sigs:
                # Dedupe by (ticker, date)
                seen = set()
                for s in sigs:
                    key = (s["ticker"], s["date"])
                    if key not in seen:
                        seen.add(key)
                        final_deduped_signals.append(s)
                final_total = len(final_deduped_signals)
                final_peak = tr.get("final_peak", 0)
                final_avg = tr.get("final_avg", 0.0)
                # Compute real peak from deduped signals
                from collections import Counter as _Counter
                date_counts = _Counter(s["date"] for s in final_deduped_signals)
                if date_counts:
                    final_peak = max(date_counts.values())
                    final_avg = round(sum(date_counts.values()) / len(date_counts), 1)
                print(f"\n  Signal count (from {pass_prefix}): "
                      f"{len(sigs)} raw → {final_total} deduped, peak {final_peak}/day")
                break

    if final_total == 0:
        print("\n  WARNING: No 5yr tier data — signal count unavailable")

    # ── Build example signal bars ──
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

    # ── Final validation ──
    print(f"\n  Final example validation:")
    if not validate_examples(example_dfs, all_conditions, expr_cache=expr_cache):
        print(f"\n{'!'*80}")
        print(f"VALIDATION FAILED — Results NOT saved. All examples must pass. No exceptions.")
        print(f"{'!'*80}")
        return None

    examples_passing = len([ex for ex in example_dfs if ex["scan_idx"] is not None])
    examples_failing = 0
    print(f"    {examples_passing}/{examples_passing} examples pass all conditions")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_tag = "mp" if multi_pass else "sp"
    is_refinement = whitelist_map is not None
    refinement_tag = "_refinement" if is_refinement else ""
    desc_name = f"pyramid_{setup_type}_{mode_tag}{refinement_tag}_sig{final_total}_pk{final_peak}_{ts}"

    result = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "peak_target": peak_target,
        "multi_pass": multi_pass,
        "refinement": is_refinement,
        "n_conditions": len(all_conditions),
        "all_conditions": all_conditions,
        "tier_results": tier_results,
        "pass_summaries": pass_summaries,
        "params": {
            "beam_width": beam_width,
            "depth": depth,
            "d1_beam": d1_beam,
            "d1_depth": d1_depth,
            "peak_target": peak_target,
            "multi_pass": multi_pass,
            "refinement": is_refinement,
            "source": "pyramid_grinder",
        },
        "summary": {
            "final_total": final_total,
            "final_peak": final_peak,
            "final_avg": final_avg,
        },
        "example_signals": example_signals,
        "examples_passing": examples_passing,
        "examples_failing": examples_failing,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)

    # Timestamped archive — always unique, never overwrites anything
    # This is the local backup. Railway is the permanent record.
    out_path = os.path.join(CACHE_DIR, f"{desc_name}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # ── Mirror to Railway ──
    from file_mirror import mirror_file
    mirror_file(out_path)

    # ── Upload to Railway ──
    step_type = "refinement_grind" if is_refinement else "signal_grind"
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
        print(f"\n  [pyramid_grinder] WARNING: Railway upload failed: {e}")
        print(f"  [pyramid_grinder] Local files are saved. Upload manually or retry later.")

    return result


# ══════════════════════════════════════════════════════════════
# REFINEMENT HELPERS
# ══════════════════════════════════════════════════════════════

def _load_signal_conditions(setup_type):
    """Load step 1 signal conditions from latest pyramid result (local).

    Returns (conditions_list, source_filename) or ([], None) if not found.
    """
    import glob as _glob

    search_dirs = [
        os.path.join(REPO_ROOT, "local_runner", "cache"),
        os.path.join(REPO_ROOT, "data"),
    ]
    candidates = []
    for d in search_dirs:
        for p in _glob.glob(os.path.join(d, f"pyramid_{setup_type}_*.json")):
            bn = os.path.basename(p)
            if "blackout" in bn or "refinement" in bn:
                continue
            candidates.append(p)

    def _extract_ts(path):
        bn = os.path.basename(path).replace(".json", "")
        parts = bn.split("_")
        if len(parts) >= 2:
            ts = parts[-2] + parts[-1]
            if len(ts) == 14 and ts.isdigit():
                return ts
        return "0"

    if not candidates:
        return [], None

    candidates.sort(key=_extract_ts, reverse=True)
    with open(candidates[0]) as f:
        data = json.load(f)
    conditions = data.get("all_conditions", [])
    return conditions, os.path.basename(candidates[0])


def _load_exit_cond(setup_type):
    """Load step 2 exit condition from local signal_exit_grind output.

    Returns exit_cond dict or None if not found.
    """
    exit_path = os.path.join(
        REPO_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup_type}.json"
    )
    if not os.path.exists(exit_path):
        return None
    with open(exit_path) as f:
        data = json.load(f)
    if data.get("grinder_type") == "signal_exit" and data.get("top_conditions"):
        return data["top_conditions"][0]
    return None


def _gather_raw_signal_clusters(setup_type):
    """Gather raw pre-dedup signal clusters for the refinement grinder.

    Scans the full universe with step 1 signal conditions, groups consecutive
    bars into clusters, applies exit condition on rightmost bars, classifies
    each cluster as AUTO_WIN or AUTO_LOSS.

    Output saved to local_runner/cache/raw_signal_clusters_{setup}.json.
    The existing refinement grinder does NOT use this yet — it's gathered
    here for later steps to wire in.

    Returns path to saved file, or None on error.
    """
    import gc

    print(f"\n  ── GATHERING RAW SIGNAL CLUSTERS ──")

    # ── Load signal conditions ──
    signal_conditions, cond_source = _load_signal_conditions(setup_type)
    if not signal_conditions:
        print(f"  ERROR: No signal conditions found for {setup_type}")
        print(f"  Run step 1 first: python local_runner/pyramid_grinder.py --setup {setup_type}")
        return None
    print(f"  Signal conditions: {len(signal_conditions)} from {cond_source}")

    # ── Load exit condition ──
    exit_cond = _load_exit_cond(setup_type)
    if exit_cond is None:
        print(f"  ERROR: No exit condition found for {setup_type}")
        print(f"  Run step 2 first: python scripts/signal_exit_grinder.py --setup {setup_type}")
        return None
    print(f"  Exit condition: {exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}")

    # ── Load examples from local SQLite ──
    print(f"  Loading examples...")
    import sqlite3
    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, entry_date FROM examples WHERE setup_type=?",
            (setup_type,)
        ).fetchall()
        conn.close()
        examples_raw = [{"ticker": r["ticker"], "entry_date": r["entry_date"]} for r in rows]
    except Exception as e:
        print(f"  ERROR: Could not load examples from {db_path}: {e}")
        return None
    print(f"  {len(examples_raw)} examples loaded")

    # ── Load expression cache ──
    print(f"  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print(f"  ERROR: Expression cache not found or invalid.")
        return None
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # ── Load 5yr cache → build slim → free full cache → scan ──
    print(f"  Loading 5yr cache...")
    universe_cache = load_5yr_cache()

    # Build example lookup: {ticker: list of entry_date strings}
    # Used to tag clusters as examples by date proximity.
    # entry_date is the hardcoded truth from the examples table.
    example_date_lookup = {}
    for ex in examples_raw:
        ticker = ex.get("ticker")
        entry_date = ex.get("entry_date")
        if ticker is None or entry_date is None:
            continue
        if ticker not in example_date_lookup:
            example_date_lookup[ticker] = []
        example_date_lookup[ticker].append(entry_date)
    print(f"  Example date lookup: {sum(len(v) for v in example_date_lookup.values())} entries across {len(example_date_lookup)} tickers")

    # Build date→bar_idx mapping per ticker for matching clusters to examples
    # This converts the OHLCV date column to {date_str: bar_idx} once per ticker
    _ticker_date_to_idx = {}
    for ticker in example_date_lookup:
        df = universe_cache.get(ticker)
        if df is None:
            continue
        dates_str = [str(d)[:10] for d in df["date"].values]
        _ticker_date_to_idx[ticker] = {d: i for i, d in enumerate(dates_str)}

    # Build slim cache for scan workers
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    from signal_filter import scan_all_signals as _scan_all, _build_slim_cache

    slim = _build_slim_cache(universe_cache)
    # Keep universe_cache — needed for exit classification below
    # (previously deleted here and reloaded, wasting ~1s)

    # ── Scan ──
    n_workers = max(cpu_count() - 1, 1)
    raw_signals = _scan_all(slim, signal_conditions, n_workers, expr_cache)
    del slim
    gc.collect()

    if not raw_signals:
        print(f"  ERROR: Scan produced 0 raw signals")
        return None

    # ── Cluster consecutive bars ──
    print(f"\n  Clustering {len(raw_signals):,} raw signals...")
    raw_signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))

    clusters = []
    cluster_id = 0
    i = 0
    while i < len(raw_signals):
        j = i + 1
        ticker = raw_signals[i]["ticker"]
        while j < len(raw_signals):
            if raw_signals[j]["ticker"] != ticker:
                break
            if raw_signals[j]["bar_idx"] != raw_signals[j - 1]["bar_idx"] + 1:
                break
            j += 1

        # raw_signals[i:j] is one cluster
        rightmost = raw_signals[j - 1]
        leftward = [raw_signals[k] for k in range(i, j - 1)]

        clusters.append({
            "cluster_id": cluster_id,
            "ticker": ticker,
            "rightmost": {
                "bar_idx": rightmost["bar_idx"],
                "date": rightmost["date"],
                "close": rightmost.get("close"),
            },
            "leftward": [
                {
                    "bar_idx": s["bar_idx"],
                    "date": s["date"],
                    "close": s.get("close"),
                }
                for s in leftward
            ],
            "size": j - i,
        })
        cluster_id += 1
        i = j

    n_single = sum(1 for c in clusters if c["size"] == 1)
    n_multi = sum(1 for c in clusters if c["size"] > 1)
    total_leftward = sum(len(c["leftward"]) for c in clusters)
    print(f"  {len(clusters):,} clusters ({n_single:,} single-bar, {n_multi:,} multi-bar)")
    print(f"  {total_leftward:,} leftward (sacrificial) bars")

    # ── Exit + classify on rightmost bars ──

    # Build cluster-to-example matching by date proximity.
    # The example has a hardcoded entry_date. The cluster has signal bars
    # that fire BEFORE the entry. Match if ANY bar in the cluster is within
    # max_distance bars before the example's entry_date.
    #
    # Two-pass:
    #   Pass 1: seed distance of 3 bars → compute forward_window
    #   Pass 2: use forward_window as match distance for classification

    def _match_cluster_to_example(cluster, max_distance=3):
        """Check if this cluster corresponds to a known example.
        Returns (is_example, entry_date, entry_bar_idx) or (False, None, None).

        Match if ANY bar in the cluster (rightmost or leftward) is within
        max_distance bars before the example's entry_date.
        """
        tk = cluster["ticker"]
        if tk not in example_date_lookup:
            return False, None, None
        all_bar_idxs = [cluster["rightmost"]["bar_idx"]] + [
            b["bar_idx"] for b in cluster.get("leftward", [])]
        date_map = _ticker_date_to_idx.get(tk, {})
        for entry_date in example_date_lookup[tk]:
            entry_idx = date_map.get(entry_date)
            if entry_idx is None:
                continue
            # Check if ANY cluster bar is within max_distance before entry
            for bi in all_bar_idxs:
                distance = entry_idx - bi
                if 0 < distance <= max_distance:
                    return True, entry_date, entry_idx
        return False, None, None

    print(f"\n  Applying exit condition on rightmost bars...")
    # universe_cache already loaded above (not freed after slim build)

    exit_expr = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]
    exit_col = expr_cache.expr_index(exit_expr)
    # Look up trade direction from the setups table
    import sqlite3 as _sqlite3
    _db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
    _conn = _sqlite3.connect(_db_path)
    _row = _conn.execute("SELECT direction FROM setups WHERE setup_type=?", (setup_type,)).fetchone()
    _conn.close()
    if not _row:
        print(f"  ERROR: Setup '{setup_type}' not found in setups table")
        return None
    direction = _row[0]
    print(f"  Trade direction: {direction}")
    MAX_FWD = 120  # Ceiling for example exit search (informational only)

    if exit_col is None:
        print(f"  ERROR: Exit expression '{exit_expr}' not in expr cache")
        return None

    # Pass 1: Match examples to clusters by strict containment only (scan bar inside cluster).
    # This computes the forward window — distance from leftmost signal bar to entry bar.
    example_adrs = []
    example_exit_bars = []
    example_fwd_windows = []  # leftmost signal bar to entry bar distance
    pass1_matched = 0
    for c in clusters:
        ticker = c["ticker"]
        bar_idx = c["rightmost"]["bar_idx"]
        all_bar_idxs = [bar_idx] + [b["bar_idx"] for b in c["leftward"]]
        is_ex, entry_date, entry_bar_idx = _match_cluster_to_example(c, max_distance=3)
        if not is_ex:
            continue
        pass1_matched += 1

        # Forward window: leftmost signal bar to entry bar
        leftmost_bar = min(all_bar_idxs)
        fwd_window_bars = entry_bar_idx - leftmost_bar
        example_fwd_windows.append(fwd_window_bars)

        # Informational: measure exit distance from entry bar
        pre_entry_bar = entry_bar_idx - 1  # bar before entry, for ADR measurement
        df = universe_cache.get(ticker)
        if df is None or pre_entry_bar >= len(df) - 1:
            continue
        try:
            cached_dates, cached_data = expr_cache.get_ticker(ticker)
            if cached_dates is None or len(cached_dates) != len(df):
                continue
            adr = float(np.mean(df["high"].values[max(0, pre_entry_bar-13):pre_entry_bar+1] - df["low"].values[max(0, pre_entry_bar-13):pre_entry_bar+1]))
            if adr <= 0 or np.isnan(adr):
                continue
            sc = float(df["close"].values[pre_entry_bar])
            fwd = min(120, len(df) - pre_entry_bar - 1)
            if fwd < 5:
                continue
            es = cached_data[:, exit_col]
            for f_i in range(1, fwd + 1):
                v = es[pre_entry_bar + f_i]
                if np.isnan(v):
                    continue
                if exit_dir in (">=", "above") and v >= exit_thresh:
                    ec = float(df["close"].values[pre_entry_bar + f_i])
                    move = (sc - ec) / adr if direction == "short" else (ec - sc) / adr
                    example_adrs.append(move)
                    example_exit_bars.append(f_i)
                    print(f"    {ticker}: {f_i} bars, {move:.1f} ADR, fwd_window={fwd_window_bars}")
                    break
                elif exit_dir in ("<=", "below") and v <= exit_thresh:
                    ec = float(df["close"].values[pre_entry_bar + f_i])
                    move = (sc - ec) / adr if direction == "short" else (ec - sc) / adr
                    example_adrs.append(move)
                    example_exit_bars.append(f_i)
                    print(f"    {ticker}: {f_i} bars, {move:.1f} ADR, fwd_window={fwd_window_bars}")
                    break
        except Exception:
            continue

    if not example_fwd_windows:
        print(f"  ERROR: No example forward windows computed (pass 1 matched {pass1_matched} examples)")
        return None

    # Forward window: max leftmost-to-entry distance + 10%
    max_fwd_window = max(example_fwd_windows)
    forward_window = round(max_fwd_window * 1.1)
    if forward_window < 1:
        forward_window = 1
    n_total_examples = sum(len(v) for v in example_date_lookup.values())
    print(f"\n  Pass 1 (containment): {pass1_matched}/{n_total_examples} examples matched")
    print(f"  Forward window: max={max_fwd_window} bars, using {forward_window} bars (110%)")
    if example_adrs:
        print(f"  Example floor (informational): {min(example_adrs):.1f} ADR")

    # ── Classify each cluster ──
    # Pass 2: use forward_window as match distance for ALL example matching.
    # For each cluster:
    #   1. Find highest high across all signal bars in cluster
    #   2. From rightmost bar, look forward by forward_window → find max high → ceiling
    #   3. After rightmost + forward_window, scan forward:
    #      - close > ceiling → AUTO_LOSS
    #      - exit condition fires → AUTO_WIN
    #      - end of data → AUTO_WIN
    #   4. Example clusters → AUTO_WIN
    print(f"\n  Classifying clusters (ceiling + exit race, match distance={forward_window})...")
    tcache = {}
    prev_tk = None
    n_win = 0
    n_loss = 0

    for c in clusters:
        ticker = c["ticker"]
        bar_idx = c["rightmost"]["bar_idx"]
        all_bar_idxs = [bar_idx] + [b["bar_idx"] for b in c["leftward"]]
        is_ex, ex_entry_date, ex_entry_idx = _match_cluster_to_example(c, max_distance=forward_window)

        c["is_example"] = 1 if is_ex else 0
        if is_ex:
            c["example_entry_date"] = ex_entry_date
            c["example_entry_idx"] = ex_entry_idx

        df = universe_cache.get(ticker)
        if df is None or bar_idx >= len(df) - 1:
            if is_ex:
                c["classification"] = "AUTO_WIN"
                c["classification_reason"] = "example_no_data"
                n_win += 1
            else:
                c["classification"] = "AUTO_LOSS"
                c["classification_reason"] = "no_data"
                n_loss += 1
            continue

        try:
            if ticker not in tcache:
                if prev_tk and prev_tk != ticker and prev_tk in tcache:
                    del tcache[prev_tk]
                tcache[ticker] = expr_cache.get_ticker(ticker)
            prev_tk = ticker
            cd, cdata = tcache[ticker]
            if cd is None or len(cd) != len(df):
                if is_ex:
                    c["classification"] = "AUTO_WIN"
                    c["classification_reason"] = "example_cache_mismatch"
                    n_win += 1
                else:
                    c["classification"] = "AUTO_LOSS"
                    c["classification_reason"] = "cache_mismatch"
                    n_loss += 1
                continue

            highs = df["high"].values
            closes = df["close"].values

            # Step 1: highest high across all signal bars in cluster
            cluster_high = max(float(highs[bi]) for bi in all_bar_idxs if bi < len(highs))

            # Step 2: from rightmost bar, look forward by forward_window, find max high → ceiling
            fw_end = min(bar_idx + forward_window, len(df) - 1)
            if fw_end > bar_idx:
                entry_window_high = float(np.max(highs[bar_idx + 1:fw_end + 1]))
                ceiling = max(cluster_high, entry_window_high)
            else:
                ceiling = cluster_high

            c["ceiling"] = round(ceiling, 4)

            # Step 3: after forward window, race exit vs ceiling breach
            scan_start = fw_end + 1
            remaining = len(df) - scan_start
            if remaining < 1:
                # Not enough data after forward window — setup held, count as win
                c["classification"] = "AUTO_WIN"
                c["classification_reason"] = "no_data_after_window"
                n_win += 1
                continue

            es = cdata[:, exit_col]
            classified = False
            for f_i in range(scan_start, len(df)):
                # Check ceiling breach (close above ceiling for shorts)
                if direction == "short" and float(closes[f_i]) > ceiling:
                    c["classification"] = "AUTO_LOSS"
                    c["classification_reason"] = "ceiling_breach"
                    c["breach_bar"] = f_i - bar_idx
                    n_loss += 1
                    classified = True
                    break
                elif direction == "long" and float(closes[f_i]) < ceiling:
                    c["classification"] = "AUTO_LOSS"
                    c["classification_reason"] = "ceiling_breach"
                    c["breach_bar"] = f_i - bar_idx
                    n_loss += 1
                    classified = True
                    break

                # Check exit condition
                v = es[f_i]
                if not np.isnan(v):
                    if exit_dir in (">=", "above") and v >= exit_thresh:
                        c["classification"] = "AUTO_WIN"
                        c["classification_reason"] = "exit_fired"
                        c["exit_bar"] = f_i - bar_idx
                        n_win += 1
                        classified = True
                        break
                    elif exit_dir in ("<=", "below") and v <= exit_thresh:
                        c["classification"] = "AUTO_WIN"
                        c["classification_reason"] = "exit_fired"
                        c["exit_bar"] = f_i - bar_idx
                        n_win += 1
                        classified = True
                        break

            if not classified:
                # Never breached ceiling, exit never fired — setup held
                c["classification"] = "AUTO_WIN"
                c["classification_reason"] = "held_to_end"
                n_win += 1

        except Exception:
            c["classification"] = "AUTO_LOSS"
            c["classification_reason"] = "error"
            n_loss += 1

        # Examples are always winners regardless of race outcome.
        # The race still runs so they get ceiling, exit_bar, etc.
        if is_ex and c.get("classification") != "AUTO_WIN":
            c["classification"] = "AUTO_WIN"
            c["classification_reason"] = "example"
            n_loss -= 1
            n_win += 1

    # ── Compute move_adr for each cluster ──
    # entry_high → exit_close, measured in ADR at signal bar.
    # Examples: entry_high = high of entry candle (scan_bar + 1)
    # Non-examples: entry_high = max high in forward window after rightmost bar
    # Only clusters with exit_bar get a value; others get null.
    print(f"\n  Computing move_adr for each cluster...")
    adr_col = expr_cache.expr_index("adr14")
    n_move_ok = 0
    n_move_skip = 0

    for c in clusters:
        ticker = c["ticker"]
        bar_idx = c["rightmost"]["bar_idx"]

        if c.get("exit_bar") is None:
            c["move_adr"] = None
            c["adr_at_signal"] = None
            c["entry_high"] = None
            n_move_skip += 1
            continue

        df = universe_cache.get(ticker)
        if df is None or bar_idx >= len(df) - 1:
            c["move_adr"] = None
            c["adr_at_signal"] = None
            c["entry_high"] = None
            n_move_skip += 1
            continue

        try:
            highs = df["high"].values
            closes = df["close"].values

            # ADR at signal bar (prefer expr cache, fallback to manual)
            adr = None
            if adr_col is not None:
                cd_t, cdata_t = expr_cache.get_ticker(ticker)
                if cd_t is not None and bar_idx < len(cdata_t):
                    adr = float(cdata_t[bar_idx, adr_col])
            if adr is None or adr <= 0 or np.isnan(adr):
                start = max(0, bar_idx - 13)
                lows = df["low"].values
                adr = float(np.mean(highs[start:bar_idx+1] - lows[start:bar_idx+1]))
            if adr <= 0 or np.isnan(adr):
                c["move_adr"] = None
                c["adr_at_signal"] = None
                c["entry_high"] = None
                n_move_skip += 1
                continue

            c["adr_at_signal"] = round(adr, 4)

            # Entry high: examples use entry candle, non-examples use forward window max high
            if c.get("is_example") == 1:
                # Use the hardcoded entry_date → entry bar is the entry candle
                ex_entry_idx = c.get("example_entry_idx")
                if ex_entry_idx is not None and ex_entry_idx < len(highs):
                    entry_high = float(highs[ex_entry_idx])
                else:
                    # Fallback: forward window max high
                    fw_end = min(bar_idx + forward_window, len(df) - 1)
                    entry_high = float(np.max(highs[bar_idx + 1:fw_end + 1])) if fw_end > bar_idx else float(highs[bar_idx])
            else:
                # Non-example: max high in forward window
                fw_end = min(bar_idx + forward_window, len(df) - 1)
                entry_high = float(np.max(highs[bar_idx + 1:fw_end + 1])) if fw_end > bar_idx else float(highs[bar_idx])

            c["entry_high"] = round(entry_high, 4)

            # Exit close
            exit_idx = bar_idx + c["exit_bar"]
            if exit_idx >= len(closes):
                c["move_adr"] = None
                c["exit_date"] = None
                n_move_skip += 1
                continue
            exit_close = float(closes[exit_idx])

            # Save exit date
            dates = df["date"].values
            if exit_idx < len(dates):
                ed = dates[exit_idx]
                c["exit_date"] = str(ed)[:10] if ed is not None else None
            else:
                c["exit_date"] = None

            # move_adr: entry_high to exit_close in ADR
            if direction == "short":
                move_adr = (entry_high - exit_close) / adr
            else:
                move_adr = (exit_close - entry_high) / adr

            c["move_adr"] = round(move_adr, 4)
            n_move_ok += 1

        except Exception:
            c["move_adr"] = None
            c["adr_at_signal"] = None
            c["entry_high"] = None
            n_move_skip += 1

    print(f"  move_adr computed: {n_move_ok} ok, {n_move_skip} skipped")
    win_moves = [c["move_adr"] for c in clusters
                 if "WIN" in c.get("classification", "") and c.get("move_adr") is not None]
    if win_moves:
        win_moves_sorted = sorted(win_moves)
        print(f"  Winner move_adr: median {win_moves_sorted[len(win_moves_sorted)//2]:.1f}, "
              f"mean {sum(win_moves)/len(win_moves):.1f}, "
              f"floor {win_moves_sorted[0]:.1f}, "
              f"ceiling {win_moves_sorted[-1]:.1f} "
              f"({len(win_moves)} winners with data)")

    # Free caches
    del universe_cache, tcache
    gc.collect()

    wr = n_win / max(n_win + n_loss, 1) * 100
    print(f"\n  Cluster classification:")
    print(f"    AUTO_WIN:  {n_win}")
    print(f"    AUTO_LOSS: {n_loss}")
    print(f"    Win rate:  {wr:.1f}%")

    # ── Save ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signal_conditions_file": cond_source,
        "exit_condition": {
            "expression": exit_cond["expression"],
            "threshold": exit_cond["threshold"],
            "direction": exit_cond["direction"],
        },
        "forward_window": forward_window,
        "direction": direction,
        "n_raw": len(raw_signals),
        "n_clusters": len(clusters),
        "n_winning_clusters": n_win,
        "n_losing_clusters": n_loss,
        "n_leftward_bars": total_leftward,
        "clusters": clusters,
    }

    # Timestamped file
    ts_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}_{ts}.json")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {ts_path}")

    # Latest pointer
    latest_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved: {latest_path}")

    # Mirror to Railway
    try:
        from file_mirror import mirror_file
        mirror_file(ts_path)
        mirror_file(latest_path)
        print(f"  Mirrored to Railway")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    # Free remaining memory
    del raw_signals, clusters, output
    gc.collect()

    print(f"  ── RAW SIGNAL CLUSTERS COMPLETE ──\n")
    return latest_path


# ══════════════════════════════════════════════════════════════
# BLACKOUT MAP LOADER
# ══════════════════════════════════════════════════════════════

def _load_refinement_piles(setup_type):
    """Load win/lose piles from raw_signal_clusters file (produced by _gather_raw_signal_clusters).

    Reads local_runner/cache/raw_signal_clusters_{setup}.json.
    Win pile (must-pass): rightmost bars of AUTO_WIN clusters → example_dfs format
    Lose pile (expendable): ALL bars from losing clusters + leftward bars from winning clusters → whitelist_map
    Cluster map: list of lists, each inner list = all bar indices for one losing cluster

    Returns:
        (win_example_dfs, whitelist_map, raw_winners, raw_losers, universe_cache, adr_threshold,
         losing_cluster_bars, win_leftward_bars)
    """
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(cluster_path):
        print(f"  ERROR: No cluster file found:")
        print(f"    {cluster_path}")
        print(f"  Run refinement grind first (it gathers clusters at the top).")
        return None, None, None, None, None, None, None, None

    with open(cluster_path) as f:
        data = json.load(f)

    clusters = data.get("clusters", [])
    if not clusters:
        print(f"  ERROR: No clusters in {cluster_path}")
        return None, None, None, None, None, None, None, None

    print(f"\n  ── REFINEMENT GRIND: Loading piles from {os.path.basename(cluster_path)} ──")
    print(f"  Timestamp: {data.get('timestamp')}")
    print(f"  Total clusters: {len(clusters)}")

    # Split by classification
    win_clusters = [c for c in clusters if c.get("classification") == "AUTO_WIN"]
    lose_clusters = [c for c in clusters if c.get("classification") == "AUTO_LOSS"]
    print(f"  Win clusters: {len(win_clusters)}")
    print(f"  Lose clusters: {len(lose_clusters)}")

    if not win_clusters:
        print(f"  ERROR: No winning clusters — nothing to use as must-pass set")
        return None, None, None, None, None, None, None, None
    if not lose_clusters:
        print(f"  WARNING: No losing clusters — nothing to filter. Refinement is a no-op.")
        return None, None, None, None, None, None, None, None

    # Load 5yr cache
    universe_cache = load_5yr_cache()

    # Build win_example_dfs from winning cluster rightmost bars
    win_example_dfs = []
    skipped_win = []
    for c in win_clusters:
        ticker = c["ticker"]
        bar_idx = c["rightmost"]["bar_idx"]

        df = universe_cache.get(ticker)
        if df is None:
            skipped_win.append(f"{ticker} (not in 5yr cache)")
            continue

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        if bar_idx >= len(df):
            skipped_win.append(f"{ticker} (bar_idx {bar_idx} >= {len(df)} bars)")
            continue

        win_example_dfs.append({
            "ticker": ticker,
            "entry_date": c["rightmost"].get("date"),
            "scan_idx": bar_idx,
            "df": df,
        })

    if skipped_win:
        print(f"  ⚠ Skipped {len(skipped_win)} winners: {', '.join(skipped_win[:10])}")
    print(f"  Win pile loaded: {len(win_example_dfs)} example_dfs")

    # Build whitelist_map: ALL bars from losing clusters + leftward bars from winning clusters
    # This is the full expendable set the engine loads into its matrix.
    whitelist_map = {}
    skipped_lose = 0

    # Losing clusters: rightmost + all leftward bars
    for c in lose_clusters:
        ticker = c["ticker"]
        if ticker not in universe_cache:
            skipped_lose += 1
            continue
        if ticker not in whitelist_map:
            whitelist_map[ticker] = set()
        whitelist_map[ticker].add(c["rightmost"]["bar_idx"])
        for lw in c.get("leftward", []):
            whitelist_map[ticker].add(lw["bar_idx"])

    # Winning clusters: leftward bars only (rightmost are must-pass, not expendable)
    win_leftward_bars = {}
    for c in win_clusters:
        ticker = c["ticker"]
        if ticker not in universe_cache:
            continue
        for lw in c.get("leftward", []):
            if ticker not in whitelist_map:
                whitelist_map[ticker] = set()
            whitelist_map[ticker].add(lw["bar_idx"])
            if ticker not in win_leftward_bars:
                win_leftward_bars[ticker] = set()
            win_leftward_bars[ticker].add(lw["bar_idx"])

    total_expendable_bars = sum(len(v) for v in whitelist_map.values())
    total_win_leftward = sum(len(v) for v in win_leftward_bars.values())
    print(f"  Expendable set: {total_expendable_bars} bars across {len(whitelist_map)} tickers")
    print(f"    (losing cluster bars + {total_win_leftward} winning leftward bars)")
    if skipped_lose:
        print(f"  ⚠ Skipped {skipped_lose} losers (not in cache)")

    # Build losing_cluster_bars: list of lists, each = [(ticker, bar_idx), ...] for one losing cluster
    # Engine uses this to check whole-cluster elimination
    losing_cluster_bars = []
    for c in lose_clusters:
        ticker = c["ticker"]
        if ticker not in universe_cache:
            continue
        bars = [(ticker, c["rightmost"]["bar_idx"])]
        for lw in c.get("leftward", []):
            bars.append((ticker, lw["bar_idx"]))
        losing_cluster_bars.append(bars)

    print(f"  Losing cluster map: {len(losing_cluster_bars)} clusters")

    # Build raw_winners / raw_losers as flat signal dicts (used by save phase)
    raw_winners = []
    for c in win_clusters:
        raw_winners.append({
            "ticker": c["ticker"],
            "signal_date": c["rightmost"].get("date"),
            "bar_idx": c["rightmost"]["bar_idx"],
            "close": c["rightmost"].get("close"),
            "is_example": c.get("is_example", 0),
            "classification": "AUTO_WIN",
            "move_adr": c.get("move_adr"),
            "adr_at_signal": c.get("adr_at_signal"),
            "entry_high": c.get("entry_high"),
            "exit_bar": c.get("exit_bar"),
            "exit_date": c.get("exit_date"),
        })

    raw_losers = []
    for c in lose_clusters:
        raw_losers.append({
            "ticker": c["ticker"],
            "signal_date": c["rightmost"].get("date"),
            "bar_idx": c["rightmost"]["bar_idx"],
            "close": c["rightmost"].get("close"),
            "is_example": c.get("is_example", 0),
            "classification": "AUTO_LOSS",
            "move_adr": c.get("move_adr"),
            "adr_at_signal": c.get("adr_at_signal"),
            "entry_high": c.get("entry_high"),
            "exit_bar": c.get("exit_bar"),
            "exit_date": c.get("exit_date"),
        })

    # adr_threshold = 0.0 — classification handled by ceiling+exit race in clusters,
    # not ADR floor. 0.0 means any positive move counts in re-scan classify phase.
    adr_threshold = 0.0

    return win_example_dfs, whitelist_map, raw_winners, raw_losers, universe_cache, adr_threshold, losing_cluster_bars, win_leftward_bars


def run_refinement(setup_type, beam_width=10000, depth=100, peak_target=3):
    """Refinement grind: cluster-aware beam search, winners must-pass, minimize losing clusters.

    Gathers raw signal clusters (Phase 1), loads full expendable set,
    runs cluster-aware beam search (Phase 2), combines conditions and
    filters signal lists by whole-cluster elimination (Phase 3).
    No re-scan, no re-classify — Phase 1 classification is truth.
    """
    print("\n" + "=" * 70)
    print("  REFINEMENT GRINDER")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Beam: {beam_width}, Depth: {depth}, Peak target: {peak_target}")

    t_total = time.time()

    # ── Phase 1: Gather raw signal clusters (pre-dedup) ──
    # Scans the universe, groups consecutive bars into clusters,
    # applies exit + classifies. Output saved and used by Phase 2 below.
    cluster_path = _gather_raw_signal_clusters(setup_type)
    if cluster_path:
        print(f"  Raw signal clusters saved: {os.path.basename(cluster_path)}")
    else:
        print(f"  WARNING: Raw signal cluster gathering failed")
        return None

    # ── Load classified signals ──
    win_dfs, loser_whitelist, raw_winners, raw_losers, universe_cache, adr_threshold, losing_cluster_bars, win_leftward_bars = _load_refinement_piles(setup_type)
    if win_dfs is None:
        print("  ABORT: Could not load refinement piles.")
        return None

    # ── Load expression cache ──
    print(f"\n  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression series cache not found or invalid.")
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # Filter winners to those in expr cache
    cached_tickers = expr_cache.get_available_tickers()
    filtered_win = []
    for ex in win_dfs:
        if ex["ticker"] in cached_tickers:
            n_bars = expr_cache.get_ticker_bar_count(ex["ticker"])
            if ex["scan_idx"] < n_bars:
                filtered_win.append(ex)
    print(f"  Winners in expr cache: {len(filtered_win)}/{len(win_dfs)}")
    win_dfs = filtered_win

    if not win_dfs:
        print("  ABORT: No winners in expr cache.")
        return None

    # ── Load expressions ──
    print(f"\n  Loading expressions...")
    all_expressions = generate_all()
    print(f"  {len(all_expressions)} expressions")

    # ── Compute winner ranges (must-pass bounding box) ──
    print(f"\n  Computing winner ranges...")
    example_ranges, example_matrix = compute_example_ranges(
        win_dfs, all_expressions, expr_cache=expr_cache)

    # Tighten to exact min/max — no 5% margin for refinement.
    # The winner set is fixed from step 3, no new winners will be added.
    expr_name_list = [e["name"] for e in all_expressions]
    for j, name in enumerate(expr_name_list):
        if name not in example_ranges:
            continue
        vals = example_matrix[:, j]
        valid = vals[~np.isnan(vals)]
        if len(valid) == 0:
            continue
        example_ranges[name] = (float(np.min(valid)), float(np.max(valid)))

    print(f"  {len(example_ranges)} expressions with valid ranges across all {len(win_dfs)} winners (exact min/max, no margin)")

    # ── Build loser matrix from expr cache (vectorized) ──
    print(f"\n  Building loser matrix...")
    t_lm = time.time()
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    expr_names = [e["name"] for e in all_expressions]
    expr_col_map = [cache_name_to_idx.get(n) for n in expr_names]

    # Build vectorized column remapping array
    valid_cache_cols = np.array(
        [c if c is not None else -1 for c in expr_col_map], dtype=np.int32)
    has_mapping = valid_cache_cols >= 0
    mapped_our_indices = np.where(has_mapping)[0]
    mapped_cache_indices = valid_cache_cols[has_mapping]

    # Build reverse lookup: (ticker, bar_idx) → losing cluster index
    _bar_to_cluster = {}
    for ci, cluster_bars in enumerate(losing_cluster_bars):
        for (tk, bi) in cluster_bars:
            _bar_to_cluster[(tk, bi)] = ci

    # Pre-allocate output arrays — we know the upper bound is total whitelist bars
    total_bars = sum(len(v) for v in loser_whitelist.values())
    loser_matrix = np.full((total_bars, len(all_expressions)), np.nan, dtype=np.float32)
    loser_dates = []
    loser_tickers = []
    row_cluster_ids = []  # >=0 = losing cluster index, -1 = winning leftward bar
    skipped = 0
    row_idx = 0

    for ticker, bar_indices in loser_whitelist.items():
        dates, data = expr_cache.get_ticker(ticker)
        if dates is None:
            skipped += len(bar_indices)
            continue

        n_cache_cols = data.shape[1]
        # Filter column mapping to what exists in this ticker's cache
        col_mask = mapped_cache_indices < n_cache_cols
        our_idx = mapped_our_indices[col_mask]
        cache_idx = mapped_cache_indices[col_mask]

        for bar_idx in bar_indices:
            if bar_idx >= len(data):
                skipped += 1
                continue
            # Vectorized row extraction — one numpy fancy index instead of 15K Python iterations
            loser_matrix[row_idx, our_idx] = data[bar_idx, cache_idx]
            loser_dates.append(str(dates[bar_idx])[:10])
            loser_tickers.append(ticker)
            row_cluster_ids.append(_bar_to_cluster.get((ticker, bar_idx), -1))
            row_idx += 1

    if row_idx == 0:
        print("  ABORT: No loser bars loaded from expr cache.")
        return None

    # Trim pre-allocated matrix to actual row count
    loser_matrix = loser_matrix[:row_idx]
    print(f"  Loser matrix: {loser_matrix.shape[0]} bars x {loser_matrix.shape[1]} expressions ({time.time()-t_lm:.1f}s)")
    if skipped:
        print(f"  Skipped {skipped} loser bars (not in cache)")

    # ── Filter to candidate expressions (have valid winner ranges) ──
    candidate_indices = []
    for i, name in enumerate(expr_names):
        if name in example_ranges:
            candidate_indices.append(i)

    candidate_names = [expr_names[i] for i in candidate_indices]
    candidate_categories = [all_expressions[i].get("category", "unknown") for i in candidate_indices]
    candidate_values = loser_matrix[:, candidate_indices]
    candidate_ranges = {name: example_ranges[name] for name in candidate_names if name in example_ranges}

    print(f"  Candidates: {len(candidate_indices)} expressions with valid winner ranges")

    # ── Run cluster-aware beam search ──
    print(f"\n  Running cluster-aware beam search...")
    search = ClusterAwareRefinementSearch(
        candidate_values=candidate_values,
        row_dates=loser_dates,
        row_tickers=loser_tickers,
        example_ranges=candidate_ranges,
        candidate_names=candidate_names,
        candidate_categories=candidate_categories,
        row_cluster_ids=row_cluster_ids,
        n_losing_clusters=len(losing_cluster_bars),
    )

    result = search.run(depth=depth, beam_width=beam_width, peak_target=peak_target)

    # ── Extract conditions ──
    all_conditions = []
    for cond in result.get("conditions", []):
        name = cond["name"]
        expr_spec = None
        for e in all_expressions:
            if e["name"] == name:
                expr_spec = e
                break
        if expr_spec is None:
            continue
        low, high = candidate_ranges[name]
        all_conditions.append({
            "name": name,
            "expr": name,
            "category": cond.get("category", "unknown"),
            "compute": expr_spec["compute"],
            "low": low,
            "high": high,
            "tier": "refinement",
            "filter_power": cond.get("filter_power"),
            "signals_with_all": cond.get("signals_with_all"),
            "signals_without": cond.get("signals_without"),
        })

    # ── Stats ──
    final_clusters = result.get("final_total", 0)  # surviving losing clusters
    final_peak = result.get("final_peak", 0)
    final_avg = result.get("final_avg", 0.0)
    eliminated_cluster_indices = result.get("eliminated_cluster_indices", set())
    surviving_cluster_indices = result.get("surviving_cluster_indices", set())

    # ── Build depth progression from beam search levels ──
    # Each level records the best path's conditions + cluster elimination stats.
    # The Settings Lock slider reads this to let you choose refinement depth.
    beam_levels = result.get("levels", [])
    n_winners = len(raw_winners)
    n_losing_input = len(losing_cluster_bars)
    depth_progression = []
    for lv in beam_levels:
        cond_names = lv.get("best_condition_names", [])
        surviving = lv.get("best_cluster_score", 0)
        eliminated = n_losing_input - surviving
        total_signals = n_winners + surviving
        wr = round(n_winners / total_signals, 4) if total_signals > 0 else 0.0
        # Resolve full condition details (name, low, high, category)
        cond_details = []
        for cn in cond_names:
            rng = candidate_ranges.get(cn)
            cat = "unknown"
            for e in all_expressions:
                if e["name"] == cn:
                    cat = e.get("category", "unknown")
                    break
            cond_details.append({
                "name": cn,
                "low": rng[0] if rng else None,
                "high": rng[1] if rng else None,
                "category": cat,
            })
        depth_progression.append({
            "depth": lv["level"],
            "conditions": cond_details,
            "losing_clusters_surviving": surviving,
            "losing_clusters_eliminated": eliminated,
            "winners": n_winners,
            "total_signals": total_signals,
            "wr": wr,
            "elapsed_s": lv.get("elapsed_s", 0),
        })
    if depth_progression:
        print(f"\n  Depth progression: {len(depth_progression)} levels saved")
        print(f"    Depth  1: {depth_progression[0]['losing_clusters_eliminated']} eliminated, "
              f"WR {depth_progression[0]['wr']:.1%}")
        print(f"    Depth {depth_progression[-1]['depth']:2d}: {depth_progression[-1]['losing_clusters_eliminated']} eliminated, "
              f"WR {depth_progression[-1]['wr']:.1%}")

    total_time = time.time() - t_total

    print(f"\n  -- REFINEMENT RESULTS --")
    print(f"  Conditions found: {len(all_conditions)}")
    print(f"  Losing clusters remaining: {final_clusters}/{len(losing_cluster_bars)}")
    print(f"  Losing clusters eliminated: {len(eliminated_cluster_indices)}")
    print(f"  Peak/day: {final_peak}")
    print(f"  Time: {total_time:.0f}s")

    # ── Validate winners still pass ──
    print(f"\n  Validating all {len(win_dfs)} winners pass...")
    if not validate_examples(win_dfs, all_conditions, expr_cache=expr_cache):
        print(f"\n{'!'*80}")
        print(f"VALIDATION FAILED — winners don't all pass. Results NOT saved.")
        print(f"{'!'*80}")
        return None
    print(f"  All {len(win_dfs)} winners pass all {len(all_conditions)} refinement conditions")

    # ── Build signal lists from cluster-level elimination ──
    # Phase 1 classification is truth. No re-scan, no re-classify.
    # Eliminated cluster → its rightmost bar removed from loser list.
    # Surviving cluster → its rightmost bar stays in loser list.
    # Winner signals unchanged.

    # Build lookup: cluster index → raw_losers entry (rightmost bar signal dict)
    # losing_cluster_bars[i][0] is always (ticker, rightmost_bar_idx) — first entry
    _cluster_to_raw_loser = {}
    for i, cluster_bars in enumerate(losing_cluster_bars):
        tk, bi = cluster_bars[0]  # rightmost bar is first in list
        for sig in raw_losers:
            if sig["ticker"] == tk and sig.get("bar_idx") == bi:
                _cluster_to_raw_loser[i] = sig
                break

    surviving_losers = []
    eliminated_losers = []
    for i in range(len(losing_cluster_bars)):
        sig = _cluster_to_raw_loser.get(i)
        if sig is None:
            continue
        if i in eliminated_cluster_indices:
            eliminated_losers.append(sig)
        else:
            surviving_losers.append(sig)

    print(f"\n  Signal lists:")
    print(f"    Winners (unchanged): {len(raw_winners)}")
    print(f"    Losers surviving:    {len(surviving_losers)}")
    print(f"    Losers eliminated:   {len(eliminated_losers)}")

    # ── Combine signal + refinement conditions ──
    signal_conditions, _cond_src = _load_signal_conditions(setup_type)
    if signal_conditions:
        print(f"\n  Signal conditions: {len(signal_conditions)} from {_cond_src}")
    else:
        print(f"\n  WARNING: No pyramid result found.")

    exit_cond = _load_exit_cond(setup_type)
    if exit_cond:
        print(f"  Exit condition: {exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}")
    else:
        print(f"  WARNING: No exit condition found.")

    combined_conditions = None
    if signal_conditions:
        sig_names = {c["name"] for c in signal_conditions}
        ref_names = {c["name"] for c in all_conditions}
        overlap = sig_names & ref_names

        combined_conditions = list(signal_conditions)
        for rc in all_conditions:
            if rc["name"] in overlap:
                combined_conditions = [c for c in combined_conditions if c["name"] != rc["name"]]
            combined_conditions.append(rc)

        print(f"  Combined: {len(signal_conditions)} signal + {len(all_conditions)} refinement "
              f"({len(overlap)} overlap) = {len(combined_conditions)} total")

        # Validate winners pass combined set
        print(f"  Validating winners pass combined conditions...")
        if not validate_examples(win_dfs, combined_conditions, expr_cache=expr_cache):
            print(f"  WARNING: Winners fail combined set — falling back to refinement only.")
            combined_conditions = None
        else:
            print(f"  ✓ All winners pass combined conditions")

    # ── Save ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    desc_name = f"refinement_{setup_type}_cl{final_clusters}_pk{final_peak}_{ts}"

    result_data = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "refinement": True,
        "n_conditions": len(combined_conditions) if combined_conditions else len(all_conditions),
        "all_conditions": combined_conditions if combined_conditions else all_conditions,
        "refinement_conditions_only": all_conditions,
        "signal_conditions": signal_conditions if signal_conditions else [],
        "exit_condition": {
            "expression": exit_cond["expression"],
            "threshold": exit_cond["threshold"],
            "direction": exit_cond["direction"],
        } if exit_cond else None,
        "params": {
            "beam_width": beam_width,
            "depth": depth,
            "peak_target": peak_target,
            "source": "cluster_aware_refinement_grinder",
        },
        "summary": {
            "losing_clusters_input": len(losing_cluster_bars),
            "losing_clusters_eliminated": len(eliminated_cluster_indices),
            "losing_clusters_surviving": final_clusters,
            "final_peak": final_peak,
            "final_avg": round(final_avg, 1) if final_avg else 0,
            "winners_input": len(win_dfs),
            "winners_passing": len(win_dfs),
        },
        "winner_signals": raw_winners,
        "loser_signals": surviving_losers,
        "eliminated_signals": eliminated_losers,
        "depth_progression": depth_progression,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"{desc_name}.json")
    with open(out_path, "w") as f:
        json.dump(result_data, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Mirror to Railway
    from file_mirror import mirror_file
    mirror_file(out_path)

    # Upload to Railway cycle
    try:
        from grind_uploader import upload as railway_upload
        railway_upload(
            result=result_data,
            result_path=out_path,
            step_type="refinement_grind",
            setup_type=setup_type,
            activate=True,
        )
    except Exception as e:
        print(f"\n  WARNING: Railway upload failed: {e}")
        print(f"  Local file saved. Upload manually or retry later.")

    return result_data


def main():
    parser = argparse.ArgumentParser(description="Pyramidal Grinder")
    parser.add_argument("--setup", required=True, help="Setup type")
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
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of times to repeat the grinder (default: 1)")
    parser.add_argument("--single-pass", action="store_true",
                        help="Legacy single-pass mode (all 12K expressions in one pass)")
    parser.add_argument("--blackout", action="store_true",
                        help="Refinement grind: winners as must-pass, losers as universe "
                             "(Step 4 — loads classified signals from step 3)")
    args = parser.parse_args()

    # ── Refinement grind: separate path ──
    if args.blackout:
        # Use aggressive defaults for refinement unless explicitly overridden
        ref_beam = args.beam if args.beam != 50 else 10000
        ref_depth = args.depth if args.depth != 10 else 100
        ref_peak = args.peak_target if args.peak_target != 15 else 3
        result = run_refinement(
            setup_type=args.setup,
            beam_width=ref_beam,
            depth=ref_depth,
            peak_target=ref_peak,
        )
        if result is None:
            sys.exit(1)
        sys.exit(0)

    multi_pass = not args.single_pass

    n_runs = max(1, args.runs)
    results = []

    for run_i in range(n_runs):
        if n_runs > 1:
            print(f"\n{'#'*70}")
            print(f"  RUN {run_i + 1} of {n_runs}")
            print(f"{'#'*70}")

        result = run_pyramid(
            setup_type=args.setup,
            peak_target=args.peak_target,
            beam_width=args.beam,
            depth=args.depth,
            d1_depth=args.d1_depth,
            d1_beam=args.d1_beam,
            multi_pass=multi_pass,
        )
        results.append(result)

    # ── Multi-run summary table ──
    if n_runs > 1:
        print(f"\n\n{'='*80}")
        print(f"  MULTI-RUN SUMMARY  ({n_runs} runs)")
        print(f"  Setup: {args.setup.upper()}  peak={args.peak_target}  beam={args.beam}  depth={args.depth}")
        print(f"{'='*80}")
        print(f"  {'Run':>4}  {'Conditions':>11}  {'5yr Total':>10}  {'5yr Peak/d':>11}  {'5yr Avg/d':>10}  {'Time':>8}")
        print(f"  {'-'*4}  {'-'*11}  {'-'*10}  {'-'*11}  {'-'*10}  {'-'*8}")

        for i, r in enumerate(results, 1):
            n_conds = r.get("n_conditions", 0)
            t = r.get("total_time_s", 0)
            s = r.get("summary", {})
            total = s.get("final_total", "—")
            peak = s.get("final_peak", "—")
            avg = s.get("final_avg", "—")

            # Fallback to tier_results if summary not populated
            if total == "—" or total is None or total == 0:
                tr = r.get("tier_results", {})
                for tier_name in reversed(["D1", "1wk", "1mo", "6mo", "1yr", "5yr"]):
                    ti = tr.get(tier_name, {})
                    if ti.get("final_total") is not None and ti["final_total"] > 0:
                        total = ti["final_total"]
                        peak = ti.get("final_peak", "—")
                        avg = ti.get("final_avg", "—")
                        break

            total_s = f"{total:,}" if isinstance(total, (int, float)) else str(total)
            peak_s = str(peak) if peak != "—" else "—"
            avg_s = f"{avg:.1f}" if isinstance(avg, (int, float)) else str(avg)
            time_s = f"{t:.0f}s"

            print(f"  {i:>4}  {n_conds:>11}  {total_s:>10}  {peak_s:>11}  {avg_s:>10}  {time_s:>8}")

        # Best run
        best_total = None
        best_i = None
        for i, r in enumerate(results, 1):
            s = r.get("summary", {})
            t = s.get("final_total")
            if t is not None and t > 0 and (best_total is None or t < best_total):
                best_total = t
                best_i = i
        if best_i:
            print(f"\n  ★ Best: Run {best_i} with {best_total:,} total signals")
        print()


if __name__ == "__main__":
    main()
