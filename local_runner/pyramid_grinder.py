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

    Examples metadata (ticker, entryDate) comes from Railway API.
    OHLCV data comes from the same 5yr cache used by the backtest scanner,
    ensuring identical indicator values and history depth.
    """
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

        # Extract the scan bar row and map to our expression order
        cached_row = data[scan_idx, :]
        for j, cache_col in enumerate(expr_to_cache_col):
            if cache_col is not None and cache_col < len(cached_row):
                val = cached_row[cache_col]
                if not np.isnan(val):
                    example_matrix[i, j] = val

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

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_tier_worker,
        initargs=(universe_cache, locked_conditions, expressions,
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
# CLI
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# BLACKOUT MAP LOADER
# ══════════════════════════════════════════════════════════════

def _load_refinement_piles(setup_type):
    """Load cycle signals from Railway and split into win/lose piles.

    Win pile (must-pass): examples + AUTO_WIN signals → returned as example_dfs format
    Lose pile (count signals in): AUTO_LOSS signals → returned as whitelist_map

    Returns:
        (win_example_dfs, whitelist_map) or (None, None) if no cycle signals found.
    """
    import requests

    # Find current cycle
    try:
        r = requests.get(f"{API_BASE}/api/v2/cycles/{setup_type}", timeout=30)
        r.raise_for_status()
        cycles = r.json().get("cycles", [])
        current = [c for c in cycles if c.get("is_current") == 1]
        if not current:
            print(f"  ERROR: No current cycle found for {setup_type}")
            return None, None
        cycle_id = current[0]["cycle_id"]
    except Exception as e:
        print(f"  ERROR: Failed to find current cycle: {e}")
        return None, None

    # Load cycle signals
    try:
        r = requests.get(f"{API_BASE}/api/v2/cycles/{cycle_id}/signals", timeout=60)
        r.raise_for_status()
        signals = r.json().get("signals", [])
    except Exception as e:
        print(f"  ERROR: Failed to load cycle signals: {e}")
        return None, None

    if not signals:
        print(f"  ERROR: No signals in cycle {cycle_id}")
        return None, None

    print(f"\n  ── REFINEMENT GRIND: Loading piles from cycle {cycle_id} ──")
    print(f"  Total signals: {len(signals)}")

    # Split into winners and losers
    winners = [s for s in signals if s.get("classification") == "AUTO_WIN"]
    losers = [s for s in signals if s.get("classification") == "AUTO_LOSS"]
    print(f"  Win pile: {len(winners)} (examples + exit-triggered winners)")
    print(f"  Lose pile: {len(losers)}")

    if not winners:
        print(f"  ERROR: No winners in cycle — nothing to use as must-pass set")
        return None, None
    if not losers:
        print(f"  WARNING: No losers in cycle — nothing to filter. Refinement is a no-op.")
        return None, None

    # Load 5yr cache to build example_dfs format for winners
    universe_cache = load_5yr_cache()

    win_example_dfs = []
    skipped_win = []
    for sig in winners:
        ticker = sig["ticker"]
        bar_idx = sig.get("bar_idx")
        if bar_idx is None:
            skipped_win.append(f"{ticker} (no bar_idx)")
            continue

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
            "entry_date": sig.get("signal_date"),
            "scan_idx": bar_idx,
            "df": df,
        })

    if skipped_win:
        print(f"  ⚠ Skipped {len(skipped_win)} winners: {', '.join(skipped_win[:10])}")
    print(f"  Win pile loaded: {len(win_example_dfs)} example_dfs")

    # Build whitelist_map from losers: {ticker: set(bar_idx)}
    whitelist_map = {}
    skipped_lose = 0
    for sig in losers:
        ticker = sig["ticker"]
        bar_idx = sig.get("bar_idx")
        if bar_idx is None:
            skipped_lose += 1
            continue
        if ticker not in universe_cache:
            skipped_lose += 1
            continue
        if ticker not in whitelist_map:
            whitelist_map[ticker] = set()
        whitelist_map[ticker].add(bar_idx)

    total_loser_bars = sum(len(v) for v in whitelist_map.values())
    print(f"  Lose pile whitelist: {total_loser_bars} bars across {len(whitelist_map)} tickers")
    if skipped_lose:
        print(f"  ⚠ Skipped {skipped_lose} losers (no bar_idx or not in cache)")

    return win_example_dfs, whitelist_map


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
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of times to repeat the grinder (default: 1)")
    parser.add_argument("--single-pass", action="store_true",
                        help="Legacy single-pass mode (all 12K expressions in one pass)")
    parser.add_argument("--blackout", action="store_true",
                        help="Refinement grind: winners as must-pass, losers as universe "
                             "(Step 4 — loads cycle signals from Railway)")
    args = parser.parse_args()

    multi_pass = not args.single_pass

    # ── Refinement grind: load win/lose piles from cycle signals ──
    blackout_map = None
    whitelist_map = None
    override_example_dfs = None
    if args.blackout:
        win_dfs, wl_map = _load_refinement_piles(args.setup)
        if win_dfs is None:
            print("  ABORT: Could not load refinement piles.")
            sys.exit(1)
        override_example_dfs = win_dfs
        whitelist_map = wl_map

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
            blackout_map=blackout_map,
            whitelist_map=whitelist_map,
            override_example_dfs=override_example_dfs,
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
