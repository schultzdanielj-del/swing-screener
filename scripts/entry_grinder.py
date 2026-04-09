"""
Entry Grinder — Brute-force stop placement for breakout/breakdown setups.

Finds the most aggressive stop placement and stop-raise strategy that all
examples survive, plus the breakeven window. Output feeds into the
classification system for non-example signals.

Three parts:
  1. Static stop test (8 candidates, 100% survival)
  2. Ratcheting stop test (10 candidates × (fw+1) positions, monotonic, 100% survival)
  3. Breakeven window (max bars until price permanently clears entry zone)

Scoring: lowest avg/median ADR risk during forward window.

Usage:
    python scripts/entry_grinder.py --setup brko
"""

import argparse
import os
import sys
import time
import json
import pickle

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from itertools import product as iter_product

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_daily_cache():
    """Load daily OHLCV cache."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")
    with open(path, "rb") as f:
        return pickle.load(f)


def load_setup_direction(setup_type):
    """Get trade direction from setups table."""
    import sqlite3
    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Setup '{setup_type}' not found in setups table")
    return row[0]


def load_exit_condition(setup_type):
    """Load exit condition from signal_exit_grind output."""
    exit_path = os.path.join(
        REPO_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup_type}.json"
    )
    if not os.path.exists(exit_path):
        raise FileNotFoundError(f"No exit condition file: {exit_path}")
    with open(exit_path) as f:
        data = json.load(f)
    if data.get("grinder_type") == "signal_exit" and data.get("top_conditions"):
        return data["top_conditions"][0]
    raise ValueError(f"Invalid exit condition file: {exit_path}")


def load_clusters(setup_type):
    """Load raw signal clusters file."""
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(cluster_path):
        raise FileNotFoundError(f"No cluster file: {cluster_path}")
    with open(cluster_path) as f:
        data = json.load(f)
    return data


def date_to_idx(df, target_date_str):
    """Find the OHLCV row index for a given date string.

    Returns index or None if not found.
    """
    target = str(target_date_str)[:10]
    dates_str = [str(d)[:10] for d in df["date"].values]
    for i, d in enumerate(dates_str):
        if d == target:
            return i
    return None


def compute_adr14(highs, lows, bar_idx):
    """Compute 14-period ADR at a given bar index.

    Uses bars [bar_idx-13 .. bar_idx] inclusive (14 bars).
    """
    start = max(0, bar_idx - 13)
    if start >= bar_idx:
        return float(highs[bar_idx] - lows[bar_idx])
    return float(np.mean(highs[start:bar_idx + 1] - lows[start:bar_idx + 1]))


# ══════════════════════════════════════════════════════════════
# EXAMPLE EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_examples(cluster_data, universe_cache, exit_cond, direction):
    """Extract per-example data from clusters + OHLCV.

    For each example cluster:
      - Resolve signal bar, entry bar, pre-signal bar by DATE
      - Validate signal_date < entry_date
      - Extract reference bar prices
      - Compute ADR at entry bar
      - Find exit bar by walking forward and evaluating exit condition

    Returns list of example dicts, each containing all data needed for Parts 1-3.
    """
    from expr_cache_builder import ExprSeriesCache

    forward_window = cluster_data["forward_window"]
    clusters = cluster_data["clusters"]
    example_clusters = [c for c in clusters if c.get("is_example") == 1]

    print(f"  Example clusters in file: {len(example_clusters)}")

    # Load expr cache for exit condition evaluation
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found or invalid.")

    exit_expr = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]
    exit_col = expr_cache.expr_index(exit_expr)
    if exit_col is None:
        raise ValueError(f"Exit expression '{exit_expr}' not in expr cache")

    examples = []
    skipped = []

    for c in example_clusters:
        ticker = c["ticker"]
        signal_date = str(c["rightmost"]["date"])[:10]
        entry_date = str(c.get("example_entry_date", ""))[:10]

        if not entry_date:
            skipped.append(f"{ticker}: no entry_date on cluster")
            continue

        # Filter: signal must be BEFORE entry
        if signal_date >= entry_date:
            skipped.append(f"{ticker} {entry_date}: signal_date {signal_date} >= entry_date")
            continue

        # Load OHLCV
        df = universe_cache.get(ticker)
        if df is None:
            skipped.append(f"{ticker}: not in OHLCV cache")
            continue

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        highs = df["high"].values.astype(np.float64)
        lows = df["low"].values.astype(np.float64)
        closes = df["close"].values.astype(np.float64)

        # Resolve bar indices by date
        signal_idx = date_to_idx(df, signal_date)
        entry_idx = date_to_idx(df, entry_date)

        if signal_idx is None:
            skipped.append(f"{ticker} {entry_date}: signal_date {signal_date} not in OHLCV")
            continue
        if entry_idx is None:
            skipped.append(f"{ticker} {entry_date}: entry_date not in OHLCV")
            continue

        # Double-check with resolved indices
        if signal_idx >= entry_idx:
            skipped.append(f"{ticker} {entry_date}: signal_idx {signal_idx} >= entry_idx {entry_idx}")
            continue

        pre_signal_idx = signal_idx - 1
        if pre_signal_idx < 0:
            skipped.append(f"{ticker} {entry_date}: no bar before signal")
            continue

        # Forward window bars (from signal bar)
        fw_end_idx = min(signal_idx + forward_window, len(df) - 1)
        if fw_end_idx <= signal_idx:
            skipped.append(f"{ticker} {entry_date}: no forward window bars")
            continue

        fw_highs = highs[signal_idx:fw_end_idx + 1]  # signal bar through fw end
        fw_lows = lows[signal_idx:fw_end_idx + 1]

        # Reference bars
        fw_highest_high_offset = int(np.argmax(fw_highs))
        fw_lowest_low_offset = int(np.argmin(fw_lows))
        fw_hh_idx = signal_idx + fw_highest_high_offset
        fw_ll_idx = signal_idx + fw_lowest_low_offset

        ref_bars = {
            "pre_signal": {"idx": pre_signal_idx, "high": float(highs[pre_signal_idx]), "low": float(lows[pre_signal_idx])},
            "signal": {"idx": signal_idx, "high": float(highs[signal_idx]), "low": float(lows[signal_idx])},
            "fw_highest_high": {"idx": fw_hh_idx, "high": float(highs[fw_hh_idx]), "low": float(lows[fw_hh_idx])},
            "fw_lowest_low": {"idx": fw_ll_idx, "high": float(highs[fw_ll_idx]), "low": float(lows[fw_ll_idx])},
        }

        # ADR at entry bar (using bars up to and including entry bar)
        adr_at_entry = compute_adr14(highs, lows, entry_idx)
        if adr_at_entry <= 0 or np.isnan(adr_at_entry):
            skipped.append(f"{ticker} {entry_date}: invalid ADR {adr_at_entry}")
            continue

        # Worst-case entry price (for scoring only)
        if direction == "long":
            worst_entry = float(lows[entry_idx]) + adr_at_entry
        else:
            worst_entry = float(highs[entry_idx]) - adr_at_entry

        # Find exit bar: walk forward from signal bar, evaluate exit expression
        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None or len(cached_dates) != len(df):
            skipped.append(f"{ticker} {entry_date}: expr cache mismatch")
            continue

        exit_series = cached_data[:, exit_col]
        exit_bar_idx = None
        max_search = len(df) - 1

        for fi in range(1, max_search - signal_idx + 1):
            check_idx = signal_idx + fi
            if check_idx > max_search:
                break
            v = exit_series[check_idx]
            if np.isnan(v):
                continue
            if exit_dir in (">=", "above") and v >= exit_thresh:
                exit_bar_idx = check_idx
                break
            elif exit_dir in ("<=", "below") and v <= exit_thresh:
                exit_bar_idx = check_idx
                break

        if exit_bar_idx is None:
            # No exit found — use end of data
            exit_bar_idx = len(df) - 1

        exit_bars_from_signal = exit_bar_idx - signal_idx

        # Store complete OHLCV slice from pre-signal through exit
        slice_start = pre_signal_idx
        slice_end = exit_bar_idx + 1  # exclusive

        examples.append({
            "ticker": ticker,
            "entry_date": entry_date,
            "signal_date": signal_date,
            "direction": direction,
            "forward_window": forward_window,
            # Bar indices (within this ticker's OHLCV)
            "pre_signal_idx": pre_signal_idx,
            "signal_idx": signal_idx,
            "entry_idx": entry_idx,
            "fw_end_idx": fw_end_idx,
            "exit_bar_idx": exit_bar_idx,
            "exit_bars_from_signal": exit_bars_from_signal,
            # Reference bars
            "ref_bars": ref_bars,
            # OHLCV arrays (full ticker, not sliced — indices are absolute)
            "highs": highs,
            "lows": lows,
            "closes": closes,
            # ADR and scoring
            "adr_at_entry": adr_at_entry,
            "worst_entry": worst_entry,
            "entry_low": float(lows[entry_idx]),
            "entry_high": float(highs[entry_idx]),
        })

    print(f"  Examples extracted: {len(examples)}")
    if skipped:
        print(f"  Skipped {len(skipped)}:")
        for s in skipped:
            print(f"    {s}")

    return examples


# ══════════════════════════════════════════════════════════════
# PART 1: STATIC STOP TEST
# ══════════════════════════════════════════════════════════════

# 8 static candidates: high and low of each of 4 reference bars
STATIC_LABELS = [
    "pre_signal_low", "pre_signal_high",
    "signal_low", "signal_high",
    "fw_highest_high_low", "fw_highest_high_high",
    "fw_lowest_low_low", "fw_lowest_low_high",
]

STATIC_REF_MAP = [
    ("pre_signal", "low"), ("pre_signal", "high"),
    ("signal", "low"), ("signal", "high"),
    ("fw_highest_high", "low"), ("fw_highest_high", "high"),
    ("fw_lowest_low", "low"), ("fw_lowest_low", "high"),
]


def run_static_stop_test(examples, direction, forward_window):
    """Part 1: Test 8 static stop candidates across all examples.

    For each candidate, check if the close ever breaches the stop from
    signal bar through exit bar. 100% survival required.

    Returns list of result dicts sorted by aggressiveness (tightest first).
    """
    print(f"\n  ── PART 1: STATIC STOP TEST ──")
    n_ex = len(examples)
    results = []

    for ci, (ref_bar_name, price_field) in enumerate(STATIC_REF_MAP):
        label = STATIC_LABELS[ci]
        n_survive = 0
        worst_margin_pct = float("inf")
        risk_adrs = []

        for ex in examples:
            # Stop level for this example
            stop_level = ex["ref_bars"][ref_bar_name][price_field]

            signal_idx = ex["signal_idx"]
            exit_bar_idx = ex["exit_bar_idx"]
            closes = ex["closes"]

            # Check survival: signal bar through exit bar
            check_closes = closes[signal_idx:exit_bar_idx + 1]

            if direction == "long":
                # Breach = close below stop
                min_close = float(np.min(check_closes))
                if min_close >= stop_level:
                    n_survive += 1
                margin = (min_close - stop_level) / stop_level if stop_level != 0 else float("inf")
            else:
                # Breach = close above stop
                max_close = float(np.max(check_closes))
                if max_close <= stop_level:
                    n_survive += 1
                margin = (stop_level - max_close) / stop_level if stop_level != 0 else float("inf")

            worst_margin_pct = min(worst_margin_pct, margin)

            # Risk-ADR scoring: distance from worst-case entry to stop, in ADR
            # Computed across forward window only (signal through fw end)
            fw_end_idx = ex["fw_end_idx"]
            if direction == "long":
                risk = (ex["worst_entry"] - stop_level) / ex["adr_at_entry"]
            else:
                risk = (stop_level - ex["worst_entry"]) / ex["adr_at_entry"]
            risk_adrs.append(risk)

        survival_rate = n_survive / n_ex
        avg_risk = float(np.mean(risk_adrs)) if risk_adrs else 0
        median_risk = float(np.median(risk_adrs)) if risk_adrs else 0

        results.append({
            "label": label,
            "survival_rate": round(survival_rate, 4),
            "n_survive": n_survive,
            "n_total": n_ex,
            "worst_margin_pct": round(worst_margin_pct * 100, 2) if worst_margin_pct != float("inf") else None,
            "avg_risk_adr": round(avg_risk, 4),
            "median_risk_adr": round(median_risk, 4),
        })

        status = "✓" if n_survive == n_ex else "✗"
        print(f"    {status} {label:25s}  survive={n_survive}/{n_ex}  "
              f"worst_margin={worst_margin_pct*100:.2f}%  "
              f"avg_risk={avg_risk:.2f} ADR  median_risk={median_risk:.2f} ADR")

    # Sort: 100% survivors first, then by avg_risk_adr ascending (tightest)
    results.sort(key=lambda r: (0 if r["survival_rate"] == 1.0 else 1, r["avg_risk_adr"]))

    n_pass = sum(1 for r in results if r["survival_rate"] == 1.0)
    print(f"\n  Static: {n_pass}/8 candidates with 100% survival")

    return results


# ══════════════════════════════════════════════════════════════
# PART 2: RATCHETING STOP TEST
# ══════════════════════════════════════════════════════════════

# 10 stop level functions
RATCHET_LABELS = [
    "pre_signal_low", "pre_signal_high",
    "signal_low", "signal_high",
    "cur_bar_low", "cur_bar_high",
    "prev_bar_low", "prev_bar_high",
    "min_low_so_far", "max_high_so_far",
]


def _compute_stop_level(func_label, ex, position_idx, direction):
    """Compute the stop price for a given function label at a given position.

    position_idx: 0 = signal bar, 1 = signal+1, ..., fw = signal+fw
    The bar at this position is signal_idx + position_idx.
    """
    signal_idx = ex["signal_idx"]
    cur_idx = signal_idx + position_idx
    highs = ex["highs"]
    lows = ex["lows"]

    # Bounds check
    max_idx = len(highs) - 1
    cur_idx = min(cur_idx, max_idx)

    if func_label == "pre_signal_low":
        return float(lows[ex["pre_signal_idx"]])
    elif func_label == "pre_signal_high":
        return float(highs[ex["pre_signal_idx"]])
    elif func_label == "signal_low":
        return float(lows[signal_idx])
    elif func_label == "signal_high":
        return float(highs[signal_idx])
    elif func_label == "cur_bar_low":
        return float(lows[cur_idx])
    elif func_label == "cur_bar_high":
        return float(highs[cur_idx])
    elif func_label == "prev_bar_low":
        prev_idx = max(0, cur_idx - 1)
        return float(lows[prev_idx])
    elif func_label == "prev_bar_high":
        prev_idx = max(0, cur_idx - 1)
        return float(highs[prev_idx])
    elif func_label == "min_low_so_far":
        return float(np.min(lows[signal_idx:cur_idx + 1]))
    elif func_label == "max_high_so_far":
        return float(np.max(highs[signal_idx:cur_idx + 1]))
    else:
        raise ValueError(f"Unknown stop function: {func_label}")


def run_ratchet_stop_test(examples, direction, forward_window):
    """Part 2: Test all ratcheting stop paths across all examples.

    10 functions × (fw+1) positions = 10^(fw+1) combos.
    Monotonic constraint: stop can only move favorably (up for long, down for short).
    100% survival required.

    Vectorized approach:
      1. Pre-compute a (n_examples, n_positions, 10) price matrix
      2. Enumerate all combos, check monotonicity, check survival
      3. Score survivors by risk-ADR

    Returns (top50_list, total_valid, total_tested).
    """
    print(f"\n  ── PART 2: RATCHETING STOP TEST ──")

    n_ex = len(examples)
    n_positions = forward_window + 1  # 0=signal bar, 1..fw
    n_funcs = len(RATCHET_LABELS)
    total_combos = n_funcs ** n_positions

    print(f"  {n_funcs} functions × {n_positions} positions = {total_combos:,} combos")

    # Step 1: Pre-compute price matrix (n_examples, n_positions, n_funcs)
    print(f"  Building price matrix...")
    price_matrix = np.zeros((n_ex, n_positions, n_funcs), dtype=np.float64)

    for ei, ex in enumerate(examples):
        for pos in range(n_positions):
            for fi, label in enumerate(RATCHET_LABELS):
                price_matrix[ei, pos, fi] = _compute_stop_level(label, ex, pos, direction)

    # Pre-compute closes at each position for each example (for breach check)
    # Shape: (n_examples, n_positions)
    close_at_pos = np.zeros((n_ex, n_positions), dtype=np.float64)
    for ei, ex in enumerate(examples):
        for pos in range(n_positions):
            bar_idx = ex["signal_idx"] + pos
            bar_idx = min(bar_idx, len(ex["closes"]) - 1)
            close_at_pos[ei, pos] = ex["closes"][bar_idx]

    # Pre-compute: for post-fw survival, we need closes from fw_end+1 through exit
    # For each example, check if the FINAL stop level holds through exit
    # We'll store the worst close (min for long, max for short) post-fw
    post_fw_worst = np.zeros(n_ex, dtype=np.float64)
    has_post_fw = np.ones(n_ex, dtype=bool)
    for ei, ex in enumerate(examples):
        fw_end_bar = ex["signal_idx"] + forward_window
        exit_bar = ex["exit_bar_idx"]
        if fw_end_bar >= exit_bar:
            has_post_fw[ei] = False
            continue
        post_closes = ex["closes"][fw_end_bar + 1:exit_bar + 1]
        if len(post_closes) == 0:
            has_post_fw[ei] = False
            continue
        if direction == "long":
            post_fw_worst[ei] = float(np.min(post_closes))
        else:
            post_fw_worst[ei] = float(np.max(post_closes))

    # Pre-compute worst-case entry and ADR arrays for scoring
    worst_entries = np.array([ex["worst_entry"] for ex in examples], dtype=np.float64)
    adrs = np.array([ex["adr_at_entry"] for ex in examples], dtype=np.float64)

    # Step 2: Enumerate combos in batches
    # For fw=3: 10^4 = 10,000 combos — small enough to do all at once
    print(f"  Testing {total_combos:,} combos...")
    t0 = time.time()

    # Generate all combos as array of shape (total_combos, n_positions)
    # Each value is a function index 0..9
    combo_indices = np.array(list(iter_product(range(n_funcs), repeat=n_positions)),
                             dtype=np.int32)
    # Shape: (total_combos, n_positions)

    # For each combo, extract stop levels per example per position
    # stop_levels shape: (total_combos, n_examples, n_positions)
    # price_matrix shape: (n_examples, n_positions, n_funcs)
    # combo_indices shape: (total_combos, n_positions)

    # Vectorized gather: for each combo c and position p, select function combo_indices[c, p]
    # from price_matrix[:, p, :]
    stop_levels = np.zeros((len(combo_indices), n_ex, n_positions), dtype=np.float64)
    for pos in range(n_positions):
        func_indices = combo_indices[:, pos]  # (total_combos,)
        # price_matrix[:, pos, :] shape: (n_ex, n_funcs)
        # Gather: for each combo, pick the func_index column
        pos_prices = price_matrix[:, pos, :]  # (n_ex, n_funcs)
        stop_levels[:, :, pos] = pos_prices[:, func_indices].T
        # pos_prices[:, func_indices] shape: (n_ex, total_combos) → transpose

    # Step 3: Monotonicity check
    # For longs: each step must be >= previous (stop moves up or stays flat)
    # For shorts: each step must be <= previous (stop moves down or stays flat)
    if n_positions > 1:
        diffs = np.diff(stop_levels, axis=2)  # (total_combos, n_ex, n_positions-1)
        if direction == "long":
            # Every diff must be >= 0 for ALL examples
            mono_ok = np.all(diffs >= -1e-10, axis=(1, 2))  # (total_combos,)
        else:
            mono_ok = np.all(diffs <= 1e-10, axis=(1, 2))
    else:
        mono_ok = np.ones(len(combo_indices), dtype=bool)

    n_mono = int(np.sum(mono_ok))
    print(f"  Monotonic paths: {n_mono:,} / {total_combos:,}")

    if n_mono == 0:
        print(f"  WARNING: No monotonic paths found")
        return [], 0, total_combos

    # Filter to monotonic only
    mono_indices = np.where(mono_ok)[0]
    mono_stops = stop_levels[mono_indices]  # (n_mono, n_ex, n_positions)
    mono_combos = combo_indices[mono_indices]  # (n_mono, n_positions)

    # Step 4: Survival check — breach at each position
    # close_at_pos shape: (n_ex, n_positions)
    # mono_stops shape: (n_mono, n_ex, n_positions)
    close_expanded = close_at_pos[np.newaxis, :, :]  # (1, n_ex, n_positions)

    if direction == "long":
        # Breach = close < stop at any position for any example
        breach_mask = close_expanded < mono_stops  # (n_mono, n_ex, n_positions)
    else:
        breach_mask = close_expanded > mono_stops

    # Any breach at any position for any example → fail
    fw_survive = ~np.any(breach_mask, axis=(1, 2))  # (n_mono,)

    # Post-fw survival: final stop must hold through exit
    final_stops = mono_stops[:, :, -1]  # (n_mono, n_ex)
    if direction == "long":
        post_breach = post_fw_worst[np.newaxis, :] < final_stops  # (n_mono, n_ex)
    else:
        post_breach = post_fw_worst[np.newaxis, :] > final_stops

    # Only check examples that have post-fw data
    post_breach[:, ~has_post_fw] = False
    post_survive = ~np.any(post_breach, axis=1)  # (n_mono,)

    # Combined survival
    full_survive = fw_survive & post_survive
    n_valid = int(np.sum(full_survive))
    print(f"  100% survival paths: {n_valid:,} / {n_mono:,} monotonic")

    if n_valid == 0:
        print(f"  WARNING: No paths with 100% survival")
        return [], 0, total_combos

    # Step 5: Score surviving paths by risk-ADR
    valid_indices = np.where(full_survive)[0]
    valid_stops = mono_stops[valid_indices]  # (n_valid, n_ex, n_positions)
    valid_combos = mono_combos[valid_indices]  # (n_valid, n_positions)

    # Risk = distance from worst-case entry to stop, in ADR
    # worst_entries shape: (n_ex,), adrs shape: (n_ex,)
    # valid_stops shape: (n_valid, n_ex, n_positions)
    if direction == "long":
        risk_per_bar = (worst_entries[np.newaxis, :, np.newaxis] -
                        valid_stops) / adrs[np.newaxis, :, np.newaxis]
    else:
        risk_per_bar = (valid_stops -
                        worst_entries[np.newaxis, :, np.newaxis]) / adrs[np.newaxis, :, np.newaxis]

    # Average risk across all examples and all positions
    avg_risk = np.mean(risk_per_bar, axis=(1, 2))  # (n_valid,)
    median_risk = np.median(risk_per_bar.reshape(n_valid, -1), axis=1)  # (n_valid,)

    # Worst margin: minimum margin across all examples at all positions
    if direction == "long":
        margins = (close_at_pos[np.newaxis, :, :] - valid_stops) / valid_stops
    else:
        margins = (valid_stops - close_at_pos[np.newaxis, :, :]) / valid_stops
    worst_margins = np.min(margins, axis=(1, 2))  # (n_valid,)

    # Sort by avg_risk ascending (tightest stop = lowest risk)
    sort_idx = np.argsort(avg_risk)

    top_n = min(50, n_valid)
    top50 = []
    for rank in range(top_n):
        vi = sort_idx[rank]
        combo = valid_combos[vi]
        path_labels = [RATCHET_LABELS[combo[p]] for p in range(n_positions)]
        top50.append({
            "path": path_labels,
            "worst_margin_pct": round(float(worst_margins[vi]) * 100, 2),
            "avg_risk_adr": round(float(avg_risk[vi]), 4),
            "median_risk_adr": round(float(median_risk[vi]), 4),
            "n_survive": n_ex,
        })

    elapsed = time.time() - t0
    print(f"  Ratchet test completed in {elapsed:.1f}s")
    print(f"\n  Top 5 ratchet paths:")
    for i, r in enumerate(top50[:5]):
        print(f"    {i+1}. {r['path']}  avg_risk={r['avg_risk_adr']:.2f}  "
              f"median_risk={r['median_risk_adr']:.2f}  "
              f"worst_margin={r['worst_margin_pct']:.2f}%")

    return top50, n_valid, total_combos


# ══════════════════════════════════════════════════════════════
# PART 3: BREAKEVEN WINDOW
# ══════════════════════════════════════════════════════════════

def run_breakeven_window(examples, direction):
    """Part 3: Find the breakeven window.

    For each example:
      1. Breakeven level = entry bar low + 1 ADR (long) or entry bar high - 1 ADR (short)
         Using ADR at entry bar.
      2. Walk forward from signal bar. Find the first bar after which price
         never revisits the breakeven level.
      3. Breakeven window = max across all examples.

    Returns (breakeven_window_bars, per_example_details).
    """
    print(f"\n  ── PART 3: BREAKEVEN WINDOW ──")

    per_example = []
    max_window = 0

    for ex in examples:
        ticker = ex["ticker"]
        signal_idx = ex["signal_idx"]
        exit_bar_idx = ex["exit_bar_idx"]
        closes = ex["closes"]
        adr = ex["adr_at_entry"]

        if direction == "long":
            breakeven = ex["entry_low"] + adr
        else:
            breakeven = ex["entry_high"] - adr

        # Walk forward from signal bar to exit bar
        # Find the LAST bar where close revisits the breakeven level
        last_revisit = signal_idx  # default: signal bar itself
        for bi in range(signal_idx, exit_bar_idx + 1):
            if direction == "long":
                if closes[bi] < breakeven:
                    last_revisit = bi
            else:
                if closes[bi] > breakeven:
                    last_revisit = bi

        # Breakeven window = bars from signal to last revisit
        be_window = last_revisit - signal_idx

        per_example.append({
            "ticker": ticker,
            "entry_date": ex["entry_date"],
            "breakeven_level": round(breakeven, 4),
            "last_revisit_bar": be_window,
        })

        if be_window > max_window:
            max_window = be_window

    print(f"  Breakeven window: {max_window} bars (max across {len(examples)} examples)")
    print(f"  Per-example breakdown:")
    for pe in sorted(per_example, key=lambda x: -x["last_revisit_bar"])[:10]:
        print(f"    {pe['ticker']:6s} {pe['entry_date']}  "
              f"be_level={pe['breakeven_level']:.2f}  "
              f"window={pe['last_revisit_bar']} bars")
    if len(per_example) > 10:
        print(f"    ... and {len(per_example) - 10} more")

    return max_window, per_example


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Entry Grinder — brute-force stop placement")
    parser.add_argument("--setup", required=True, help="Setup type (e.g. brko)")
    args = parser.parse_args()
    setup_type = args.setup

    print(f"\n{'='*60}")
    print(f"  Entry Grinder — {setup_type}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    t_start = time.time()

    # ── Load inputs ──
    print(f"\n  Loading inputs...")
    direction = load_setup_direction(setup_type)
    print(f"  Direction: {direction}")

    exit_cond = load_exit_condition(setup_type)
    print(f"  Exit condition: {exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}")

    cluster_data = load_clusters(setup_type)
    forward_window = cluster_data["forward_window"]
    print(f"  Forward window: {forward_window}")
    print(f"  Total clusters: {len(cluster_data['clusters'])}")

    print(f"\n  Loading OHLCV cache...")
    universe_cache = load_daily_cache()
    print(f"  OHLCV cache: {len(universe_cache)} tickers")

    # ── Extract examples ──
    print(f"\n  Extracting examples...")
    examples = extract_examples(cluster_data, universe_cache, exit_cond, direction)

    if not examples:
        print(f"  ERROR: No valid examples extracted. Cannot proceed.")
        return

    # Free OHLCV cache — examples hold their own arrays
    del universe_cache

    # ── Print example summary ──
    print(f"\n  ── EXAMPLE SUMMARY ──")
    print(f"  {'Ticker':6s} {'Entry':12s} {'Signal':12s} {'ADR':>8s} "
          f"{'EntryLow':>10s} {'WrstEntry':>10s} {'ExitBars':>8s}")
    for ex in examples:
        print(f"  {ex['ticker']:6s} {ex['entry_date']:12s} {ex['signal_date']:12s} "
              f"{ex['adr_at_entry']:8.2f} "
              f"{ex['entry_low']:10.2f} {ex['worst_entry']:10.2f} "
              f"{ex['exit_bars_from_signal']:8d}")

    # ── Run Parts ──
    static_results = run_static_stop_test(examples, direction, forward_window)
    ratchet_top50, ratchet_valid, ratchet_tested = run_ratchet_stop_test(
        examples, direction, forward_window)
    breakeven_window, be_details = run_breakeven_window(examples, direction)

    # ── Build output ──
    elapsed = time.time() - t_start
    output = {
        "setup_type": setup_type,
        "direction": direction,
        "forward_window": forward_window,
        "n_examples": len(examples),
        "elapsed_s": round(elapsed, 1),
        "breakeven_window": breakeven_window,
        "exit_condition": {
            "expression": exit_cond["expression"],
            "threshold": exit_cond["threshold"],
            "direction": exit_cond["direction"],
        },
        "static_stops": static_results,
        "ratchet_stops_top50": ratchet_top50,
        "ratchet_stops_total_valid": ratchet_valid,
        "ratchet_stops_total_tested": ratchet_tested,
    }

    # ── Save ──
    os.makedirs(CACHE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Timestamped
    ts_path = os.path.join(CACHE_DIR, f"entry_grinder_{setup_type}_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {ts_path}")

    # Latest
    latest_path = os.path.join(CACHE_DIR, f"entry_grinder_{setup_type}.json")
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

    print(f"\n  Entry grinder completed in {elapsed:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
