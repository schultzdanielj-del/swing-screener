"""
Proximity Grinder — Trim losers by finding conditions that separate
win pile from lose pile, then re-scan with full combined condition set.

Post-convergence step (Step 6). Finds conditions that ALL win pile signals
pass but that eliminate lose pile signals. Pure EV gain — every loser removed
raises win rate and profit factor.

DATA SOURCE:
  Reads from the refinement grinder's local output file:
    local_runner/cache/refinement_{setup}_*.json
  The refinement grinder outputs:
    - all_conditions: combined signal + refinement conditions
    - winner_signals / loser_signals: classified signal lists
    - sacrificial_signals: leftward dedup duplicates
    - exit_condition: exit condition used for classification

COMPUTATION PATH:
  Uses expr cache as single computation path — same as pyramid_grinder.py
  and all other grinders. No live ExpressionEngine.compute().

  NaN handling matches pyramid_grinder exactly:
    - Win pile range computation: require ALL win signals non-NaN per expression
    - Lose pile filtering: NaN = FAIL (does not pass the condition)
    - Validation: NaN = FAIL

  Parallelized matrix extraction via ProcessPoolExecutor (full CPU usage).

OUTPUT:
  Local JSON with:
    - all_conditions: combined signal + refinement + proximity
    - proximity_conditions_only: just the new proximity conditions
    - winner_signals / loser_signals: freshly classified from re-scan
    - sacrificial_signals: leftward dedup duplicates from re-scan
    - exit_condition, metrics
  Uploaded to Railway via file_mirror + grind_uploader.

Usage:
    python scripts/proximity_grinder.py --setup dtss
    python scripts/proximity_grinder.py --setup dtss --beam 10000 --depth 100
    python scripts/proximity_grinder.py --setup dtss --dry-run
"""

import argparse
import glob
import os
import sys
import time
import json
import pickle
import numpy as np
import pandas as pd
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache

MAX_FORWARD = 120

SETUP_CONFIGS = {
    "dtss": {"direction": "short"},
}


# ══════════════════════════════════════════════════════════════
# DATA LOADING — reads from refinement grinder local output
# ══════════════════════════════════════════════════════════════

def load_refinement_output(setup_type):
    """Load the refinement grinder's output file.

    Reads the latest refinement_*.json from local_runner/cache/.

    Returns the full data dict containing:
      all_conditions, winner_signals, loser_signals,
      sacrificial_signals, exit_condition, etc.
    """
    pattern = os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json")
    files = sorted(glob.glob(pattern))

    if not files:
        raise RuntimeError(
            f"No refinement grinder output found: {pattern}\n"
            f"Run step 4 first: python local_runner/pyramid_grinder.py --setup {setup_type} --blackout"
        )

    latest = files[-1]
    print(f"  Loading: {os.path.basename(latest)}")

    with open(latest) as f:
        data = json.load(f)

    win_pile = data.get("winner_signals", [])
    lose_pile = data.get("loser_signals", [])
    conditions = data.get("all_conditions", [])
    exit_cond = data.get("exit_condition", {})

    if not win_pile:
        raise RuntimeError(
            f"No winner_signals in {latest}.\n"
            f"Re-run the refinement grinder to get updated output."
        )
    if not conditions:
        raise RuntimeError(
            f"No all_conditions in {latest}.\n"
            f"Re-run the refinement grinder to get updated output."
        )
    if not exit_cond:
        raise RuntimeError(
            f"No exit_condition in {latest}.\n"
            f"Re-run the refinement grinder to get updated output."
        )

    print(f"  Pre-proximity conditions: {len(conditions)}")
    print(f"  Win pile:  {len(win_pile):,} (winners — untouchable)")
    print(f"  Lose pile: {len(lose_pile):,} (surviving losers — target to trim)")
    print(f"  Exit: {exit_cond.get('expression')} {exit_cond.get('direction')} {exit_cond.get('threshold')}")

    return data


def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")
    print(f"  Loading 5yr cache...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


# ══════════════════════════════════════════════════════════════
# PARALLEL MATRIX EXTRACTION — matches pyramid_grinder pattern
# ══════════════════════════════════════════════════════════════

_w_signals = None
_w_expr_to_cache_col = None
_w_n_expr = None


def _init_extract_worker(signals, expr_to_cache_col, n_expr):
    global _w_signals, _w_expr_to_cache_col, _w_n_expr
    _w_signals = signals
    _w_expr_to_cache_col = expr_to_cache_col
    _w_n_expr = n_expr


def _extract_batch(sig_indices):
    """Worker: extract expression values for a batch of signal indices."""
    from expr_cache_builder import load_ticker_cache

    results = []
    ticker_cache = {}

    for si in sig_indices:
        sig = _w_signals[si]
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]

        if ticker not in ticker_cache:
            dates, data = load_ticker_cache(ticker)
            ticker_cache[ticker] = (dates, data)
        cached_dates, cached_data = ticker_cache[ticker]

        if cached_dates is None or cached_data is None:
            continue
        if bar_idx >= len(cached_data):
            continue

        row = cached_data[bar_idx, :]
        values = np.full(_w_n_expr, np.nan, dtype=np.float32)
        for j, cache_col in enumerate(_w_expr_to_cache_col):
            if cache_col is not None and cache_col < len(row):
                values[j] = row[cache_col]
        results.append((si, values))

    return results


def extract_signal_values_parallel(signals, expressions, expr_cache):
    """Pull expression values from expr cache for each signal bar.

    Parallelized across CPU cores.
    Returns np.array (n_signals, n_expressions) float32.
    """
    n_sig = len(signals)
    n_expr = len(expressions)
    matrix = np.full((n_sig, n_expr), np.nan, dtype=np.float32)

    if n_sig == 0:
        return matrix

    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    expr_to_cache_col = []
    for e in expressions:
        expr_to_cache_col.append(cache_name_to_idx.get(e["name"]))

    n_workers = max(cpu_count() - 1, 1)
    batch_size = max(n_sig // (n_workers * 4), 10)
    all_indices = list(range(n_sig))
    batches = [all_indices[i:i + batch_size]
               for i in range(0, len(all_indices), batch_size)]

    t0 = time.time()
    n_loaded = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_extract_worker,
        initargs=(signals, expr_to_cache_col, n_expr)
    ) as pool:
        futures = {pool.submit(_extract_batch, batch): batch for batch in batches}
        completed = 0
        for future in as_completed(futures):
            for si, values in future.result():
                matrix[si, :] = values
                n_loaded += 1
            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches "
                      f"[{elapsed:.0f}s, {n_loaded:,} loaded]")

    pct = n_loaded / max(n_sig, 1) * 100
    elapsed = time.time() - t0
    print(f"  Extracted {n_loaded}/{n_sig} signals ({pct:.0f}%) "
          f"x {n_expr:,} expressions in {elapsed:.1f}s "
          f"({n_workers} workers)")
    return matrix


# ══════════════════════════════════════════════════════════════
# WIN PILE RANGES — matches pyramid_grinder.compute_example_ranges
# ══════════════════════════════════════════════════════════════

def compute_win_ranges(win_matrix, expressions):
    """Compute [min, max] range with 5% margin for each expression across win pile.

    Require ALL win pile signals to have valid (non-NaN) values.
    5% margin on each side, same as pyramid_grinder.
    """
    n_win = win_matrix.shape[0]
    ranges = {}

    for j, expr in enumerate(expressions):
        vals = win_matrix[:, j]
        valid = vals[~np.isnan(vals)]
        if len(valid) < n_win:
            continue
        lo, hi = float(np.min(valid)), float(np.max(valid))
        margin = (hi - lo) * 0.05
        ranges[expr["name"]] = (lo - margin, hi + margin)

    print(f"  Win ranges: {len(ranges):,} expressions with full coverage "
          f"(out of {len(expressions):,})")
    return ranges


# ══════════════════════════════════════════════════════════════
# BEAM SEARCH — minimize remaining trimmable signals
# ══════════════════════════════════════════════════════════════

def run_beam_search(trim_matrix, win_ranges, expressions,
                    beam_width=10000, depth=100):
    """Beam search to minimize remaining trimmable signals.

    NaN handling: NaN = FAIL (does not pass). Matches pyramid_grinder.
    Score = number of remaining rows (minimize).
    """
    n_rows, n_expr = trim_matrix.shape
    expr_names = [e["name"] for e in expressions]

    print(f"\n  Beam Search:")
    print(f"    Trimmable rows: {n_rows:,}")
    print(f"    Candidate expressions: {len(win_ranges):,}")
    print(f"    Beam: {beam_width:,}, Depth: {depth}")

    # Precompute: for each candidate, which trim rows pass?
    cand_indices = []
    cand_passes = []

    for j, name in enumerate(expr_names):
        if name not in win_ranges:
            continue
        lo, hi = win_ranges[name]
        vals = trim_matrix[:, j]
        passes = (vals >= lo) & (vals <= hi)
        passes[np.isnan(vals)] = False
        n_pass = int(np.sum(passes))
        if n_pass < n_rows * 0.99:
            cand_indices.append(j)
            cand_passes.append(passes)

    n_useful = len(cand_indices)
    print(f"    Useful candidates (filter >= 1%): {n_useful}")

    if n_useful == 0:
        print("    No useful candidates. Cannot trim further.")
        return [], n_rows

    cand_passes_arr = np.array(cand_passes, dtype=bool)

    # Score each candidate individually
    base_mask = np.ones(n_rows, dtype=bool)
    scored = []
    for ci in range(n_useful):
        mask = base_mask & cand_passes_arr[ci]
        remaining = int(np.sum(mask))
        scored.append((ci, remaining, mask))

    scored.sort(key=lambda x: x[1])

    # Build initial beam
    class Node:
        __slots__ = ['conditions', 'row_mask', 'remaining']
        def __init__(self, conditions, row_mask, remaining):
            self.conditions = conditions
            self.row_mask = row_mask
            self.remaining = remaining

    n_seeds = min(beam_width * 2, len(scored))
    current_level = []
    for ci, remaining, mask in scored[:n_seeds]:
        current_level.append(Node(conditions=(ci,), row_mask=mask, remaining=remaining))

    current_level.sort(key=lambda n: n.remaining)
    current_level = current_level[:beam_width]

    best = current_level[0]
    trimmed = n_rows - best.remaining
    print(f"    Level  1: {best.remaining:,} remaining "
          f"(-{trimmed:,}, {trimmed / max(n_rows, 1) * 100:.1f}%)")

    # Deepen
    stall_count = 0
    for lv in range(2, depth + 1):
        next_level = []
        seen = set()

        for node in current_level:
            used = set(node.conditions)
            if not np.any(node.row_mask):
                continue

            for ci in range(n_useful):
                if ci in used:
                    continue
                combo = tuple(sorted(node.conditions + (ci,)))
                if combo in seen:
                    continue
                seen.add(combo)

                mask = node.row_mask & cand_passes_arr[ci]
                remaining = int(np.sum(mask))
                next_level.append(Node(conditions=combo, row_mask=mask,
                                       remaining=remaining))

                if len(next_level) >= beam_width * 8:
                    break
            if len(next_level) >= beam_width * 8:
                break

        if not next_level:
            print(f"    Level {lv:2d}: ceiling (no new combos)")
            break

        next_level.sort(key=lambda n: n.remaining)
        current_level = next_level[:beam_width]

        if current_level[0].remaining < best.remaining:
            best = current_level[0]
            stall_count = 0
        else:
            stall_count += 1

        trimmed = n_rows - best.remaining
        print(f"    Level {lv:2d}: {best.remaining:,} remaining "
              f"(-{trimmed:,}, {trimmed / max(n_rows, 1) * 100:.1f}%) "
              f"[{len(best.conditions)} conds]")

        if best.remaining == 0:
            break

        if stall_count >= 2:
            print(f"    Ceiling at level {lv} (no improvement in 2 levels)")
            break

    return _extract_conditions(best, cand_indices, expressions, win_ranges), best.remaining


def _extract_conditions(node, cand_indices, expressions, win_ranges):
    """Convert beam search result to condition dicts."""
    conditions = []
    for ci in node.conditions:
        expr_idx = cand_indices[ci]
        expr = expressions[expr_idx]
        lo, hi = win_ranges[expr["name"]]
        conditions.append({
            "name": expr["name"],
            "expression_name": expr["name"],
            "expr": expr.get("expr", expr["name"]),
            "category": expr.get("category", "unknown"),
            "compute": expr.get("compute", {}),
            "low": lo,
            "high": hi,
            "tier": "proximity",
            "filter_power": None,
        })
    return conditions


# ══════════════════════════════════════════════════════════════
# VALIDATION — NaN = FAIL, matches pyramid_grinder
# ══════════════════════════════════════════════════════════════

def validate_win_pile(win_matrix, proximity_conditions, expressions):
    """Verify every win pile signal passes all proximity conditions.

    NaN = FAIL. This MUST pass 100%. If it fails, the grinder has a bug.
    """
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    n_win = win_matrix.shape[0]
    failures = []

    for cond in proximity_conditions:
        j = expr_name_to_idx.get(cond["name"])
        if j is None:
            failures.append(f"  {cond['name']}: not in expression list")
            continue
        vals = win_matrix[:, j]
        in_range = (vals >= cond["low"]) & (vals <= cond["high"])
        in_range[np.isnan(vals)] = False
        n_fail = int(np.sum(~in_range))
        if n_fail > 0:
            failures.append(f"  {cond['name']}: {n_fail}/{n_win} win signals FAIL")

    if failures:
        print("\n  VALIDATION FAILED — ABORTING:")
        for f in failures:
            print(f)
        return False

    print(f"\n  Validation passed: {n_win} win pile signals x "
          f"{len(proximity_conditions)} proximity conditions = 100% pass")
    return True


# ══════════════════════════════════════════════════════════════
# RE-SCAN — full universe with combined conditions (parallel)
# ══════════════════════════════════════════════════════════════

_rscan_cache = None
_rscan_conditions = None
_rscan_expr_cache_dir = None
_rscan_cond_col_indices = None


def _init_rscan_worker(cache, conditions, expr_cache_dir, cond_col_indices):
    global _rscan_cache, _rscan_conditions, _rscan_expr_cache_dir, _rscan_cond_col_indices
    _rscan_cache = cache
    _rscan_conditions = conditions
    _rscan_expr_cache_dir = expr_cache_dir
    _rscan_cond_col_indices = cond_col_indices


def _rscan_load_npz(ticker):
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = os.path.join(_rscan_expr_cache_dir, f"{safe}.npz")
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        return loaded["dates"], loaded["data"]
    except Exception:
        return None, None


def _rscan_batch(tickers):
    signals = []
    for ticker in tickers:
        df = _rscan_cache.get(ticker)
        if df is None or len(df) < 100:
            continue
        try:
            dates_cache, data_cache = _rscan_load_npz(ticker)
            if dates_cache is None or len(dates_cache) != len(df):
                continue
            n_bars = len(df)
            mask = np.ones(n_bars, dtype=bool)
            mask[:50] = False
            for i, cond in enumerate(_rscan_conditions):
                col_idx = _rscan_cond_col_indices[i]
                if col_idx is None:
                    mask[:] = False
                    break
                series = data_cache[:, col_idx]
                in_range = (series >= cond["low"]) & (series <= cond["high"])
                in_range[np.isnan(series)] = False
                mask &= in_range
            for idx in np.where(mask)[0]:
                signals.append({
                    "ticker": ticker,
                    "date": str(df["date"].values[idx])[:10],
                    "bar_idx": int(idx),
                    "close": float(df["close"].values[idx]),
                })
        except Exception:
            pass
    return signals


def rescan_universe(cache, conditions, expr_cache, workers):
    """Scan full universe with combined conditions. Returns raw signal list."""
    tickers = list(cache.keys())
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    cond_col_indices = [expr_cache.expr_index(c["name"]) for c in conditions]
    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    print(f"\n  Re-scanning {len(tickers):,} tickers x {len(conditions)} conditions "
          f"({workers} workers)...")
    t0 = time.time()
    all_signals = []

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_rscan_worker,
        initargs=(cache, conditions, expr_cache_dir, cond_col_indices)
    ) as pool:
        futures = [pool.submit(_rscan_batch, b) for b in batches]
        done = 0
        for f in as_completed(futures):
            all_signals.extend(f.result())
            done += 1
            if done % max(len(batches) // 5, 1) == 0 or done == len(batches):
                pct = done / len(batches) * 100
                print(f"    {pct:.0f}%  {len(all_signals):,} signals  [{time.time()-t0:.0f}s]")

    print(f"  Raw signals: {len(all_signals):,}  ({time.time()-t0:.0f}s)")
    return all_signals


# ══════════════════════════════════════════════════════════════
# DEDUP + EXIT + CLASSIFY
# ══════════════════════════════════════════════════════════════

def dedup_with_sacrificial(signals):
    """Consecutive signal bars per ticker → keep rightmost.

    Returns (deduped, sacrificial).
    """
    signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))
    deduped = []
    sacrificial = []
    i = 0
    while i < len(signals):
        j = i + 1
        ticker = signals[i]["ticker"]
        while j < len(signals):
            if signals[j]["ticker"] != ticker:
                break
            if signals[j]["bar_idx"] != signals[j-1]["bar_idx"] + 1:
                break
            j += 1
        rightmost = signals[j-1]
        rightmost["cluster_size"] = j - i
        rightmost["cluster_start_date"] = signals[i]["date"]
        deduped.append(rightmost)
        for k in range(i, j - 1):
            sacrificial.append(signals[k])
        i = j
    print(f"  Deduped: {len(signals):,} → {len(deduped):,} + "
          f"{len(sacrificial):,} sacrificial")
    return deduped, sacrificial


def apply_exit(signals, cache, exit_cond, direction, expr_cache):
    """Apply exit condition to signals. Returns (with_exit, no_exit)."""
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]

    exit_col_idx = expr_cache.expr_index(expr_name)
    if exit_col_idx is None:
        raise RuntimeError(f"Exit expression '{expr_name}' not in expression cache")
    adr_col_idx = expr_cache.expr_index("adr14")

    print(f"\n  Applying exit: {expr_name} {exit_dir} {exit_thresh}  "
          f"(direction={direction}, max_forward={MAX_FORWARD})")

    with_exit = []
    no_exit = []
    _ticker_cache = {}

    for sig in signals:
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        df = cache.get(ticker)
        if df is None or bar_idx >= len(df) - 1:
            no_exit.append({**sig, "exit_triggered": False})
            continue
        try:
            if ticker not in _ticker_cache:
                _ticker_cache[ticker] = expr_cache.get_ticker(ticker)
            cached_dates, cached_data = _ticker_cache[ticker]
            if cached_dates is None or len(cached_dates) != len(df):
                no_exit.append({**sig, "exit_triggered": False})
                continue

            adr = (float(cached_data[bar_idx, adr_col_idx])
                   if adr_col_idx is not None else None)
            if adr is None or adr <= 0 or np.isnan(adr):
                h = df["high"].values
                l = df["low"].values
                s = max(0, bar_idx - 13)
                adr = float(np.mean(h[s:bar_idx+1] - l[s:bar_idx+1]))
            if adr <= 0:
                no_exit.append({**sig, "exit_triggered": False})
                continue

            signal_close = float(df["close"].values[bar_idx])
            actual_forward = min(MAX_FORWARD, len(df) - bar_idx - 1)
            if actual_forward < 5:
                no_exit.append({**sig, "exit_triggered": False})
                continue

            exit_series = cached_data[:, exit_col_idx]
            exit_bar = None
            exit_close = None
            for fwd in range(1, actual_forward + 1):
                idx = bar_idx + fwd
                val = exit_series[idx]
                if np.isnan(val):
                    continue
                is_below = exit_dir in ("<=", "below")
                is_above = exit_dir in (">=", "above")
                if is_above and val >= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[idx])
                    break
                elif is_below and val <= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[idx])
                    break

            if exit_bar is None:
                no_exit.append({**sig, "exit_triggered": False,
                                "signal_close": round(signal_close, 2),
                                "adr_at_signal": round(adr, 2)})
                continue

            if direction == "short":
                move_adr = (signal_close - exit_close) / adr
                mfe_price = float(df["low"].values[bar_idx+1:bar_idx+exit_bar+1].min())
                mfe_adr = (signal_close - mfe_price) / adr
            else:
                move_adr = (exit_close - signal_close) / adr
                mfe_price = float(df["high"].values[bar_idx+1:bar_idx+exit_bar+1].max())
                mfe_adr = (mfe_price - signal_close) / adr

            with_exit.append({
                **sig,
                "exit_triggered": True,
                "signal_close": round(signal_close, 2),
                "adr_at_signal": round(adr, 2),
                "exit_bar": exit_bar,
                "exit_date": str(df["date"].values[bar_idx + exit_bar])[:10],
                "exit_close": round(exit_close, 2),
                "move_adr": round(move_adr, 2),
                "mfe_adr": round(mfe_adr, 2),
                "capture_eff": round(move_adr / mfe_adr, 3) if mfe_adr > 0 else 0,
            })
        except Exception:
            no_exit.append({**sig, "exit_triggered": False})

    print(f"  Exit applied: {len(with_exit)} triggered, {len(no_exit)} no exit")
    return with_exit, no_exit


def classify_signals(deduped, with_exit, example_bar_lookup):
    """Classify all deduped signals into AUTO_WIN / AUTO_LOSS.

    example_bar_lookup: {ticker: set(bar_idx)} — example markers from
    the refinement grinder's input (step 3 classified signals).
    """
    exit_lookup = {}
    for sig in with_exit:
        exit_lookup[(sig["ticker"], sig["bar_idx"])] = sig

    exit_adrs = [s["move_adr"] for s in with_exit if s.get("move_adr") is not None]
    median_adr = sorted(exit_adrs)[len(exit_adrs) // 2] if exit_adrs else 5.0

    classified = []
    for sig in deduped:
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]

        is_example = 0
        if ticker in example_bar_lookup and bar_idx in example_bar_lookup[ticker]:
            is_example = 1

        exit_data = exit_lookup.get((ticker, bar_idx))

        if is_example:
            classification = "AUTO_WIN"
            classification_source = "example"
        elif exit_data and exit_data.get("move_adr", 0) >= median_adr:
            classification = "AUTO_WIN"
            classification_source = "exit_filter"
        elif exit_data:
            classification = "AUTO_LOSS"
            classification_source = "exit_filter"
        else:
            classification = "AUTO_LOSS"
            classification_source = "exit_filter"

        row = {
            "ticker": ticker,
            "signal_date": sig["date"],
            "bar_idx": bar_idx,
            "close": sig.get("close"),
            "adr": exit_data.get("adr_at_signal") if exit_data else None,
            "is_example": is_example,
            "classification": classification,
            "classification_source": classification_source,
            "exit_triggered": 1 if exit_data else 0,
            "exit_date": exit_data.get("exit_date") if exit_data else None,
            "move_adr": exit_data.get("move_adr") if exit_data else None,
            "mfe_adr": exit_data.get("mfe_adr") if exit_data else None,
            "capture_eff": exit_data.get("capture_eff") if exit_data else None,
        }
        classified.append(row)

    n_win = sum(1 for s in classified if s["classification"] == "AUTO_WIN")
    n_loss = sum(1 for s in classified if s["classification"] == "AUTO_LOSS")
    n_ex = sum(1 for s in classified if s["is_example"])
    n_exit = sum(1 for s in classified if s["exit_triggered"])

    print(f"\n  Classification:")
    print(f"    Total: {len(classified)}")
    print(f"    AUTO_WIN: {n_win} (examples: {n_ex}, exit_filter: {n_win - n_ex})")
    print(f"    AUTO_LOSS: {n_loss}")
    print(f"    Exit triggered: {n_exit}/{len(classified)}")
    print(f"    Win rate: {n_win/len(classified)*100:.1f}%")
    print(f"    Median ADR threshold: {median_adr:.1f}")

    return classified, median_adr


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run_proximity_grind(setup_type, beam_width=10000, depth=100, dry_run=False):
    """Proximity grind: find conditions that trim losers without touching winners,
    then re-scan full universe with all conditions combined.

    Full output for downstream (profit grind, regime model):
      - all_conditions: combined signal + refinement + proximity
      - proximity_conditions_only: just the new proximity conditions
      - winner_signals / loser_signals: re-classified from fresh scan
      - sacrificial_signals: leftward dedup duplicates from fresh scan
    """
    print(f"\n{'=' * 70}")
    print(f"  PROXIMITY GRINDER — {setup_type.upper()}")
    print(f"{'=' * 70}\n")

    direction = SETUP_CONFIGS.get(setup_type, {}).get("direction", "short")
    workers = max(cpu_count() - 1, 1)
    t0 = time.time()

    # ── 1. Load refinement grinder output (all local) ──
    print("Phase 1: Loading refinement grinder output...")
    ref_data = load_refinement_output(setup_type)

    win_pile = ref_data["winner_signals"]
    lose_pile = ref_data["loser_signals"]
    pre_conditions = ref_data["all_conditions"]
    exit_cond = ref_data["exit_condition"]

    # Extract example bar lookup from winner signals
    example_bar_lookup = {}
    for sig in win_pile:
        if sig.get("is_example"):
            ticker = sig["ticker"]
            bar_idx = sig.get("bar_idx")
            if bar_idx is not None:
                if ticker not in example_bar_lookup:
                    example_bar_lookup[ticker] = set()
                example_bar_lookup[ticker].add(bar_idx)

    if dry_run:
        print(f"\n  DRY RUN — stopping here. ({time.time() - t0:.0f}s)")
        return

    if len(win_pile) == 0:
        print("\n  ERROR: Win pile is empty. Cannot grind.")
        return
    if len(lose_pile) == 0:
        print("\n  Nothing to trim — lose pile is empty.")
        return

    # ── 2. Load expr cache + expressions ──
    print("\nPhase 2: Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found. "
                           "Run: python local_runner/expr_cache_builder.py --build")

    expressions = generate_all()
    print(f"  Expressions: {len(expressions):,}")
    print(f"  Expr cache: {expr_cache.n_expressions:,} expressions")

    # ── 3. Extract values (parallelized) ──
    print("\nPhase 3a: Extracting win pile expression values...")
    win_matrix = extract_signal_values_parallel(win_pile, expressions, expr_cache)

    print("\nPhase 3b: Extracting lose pile expression values...")
    trim_matrix = extract_signal_values_parallel(lose_pile, expressions, expr_cache)

    # ── 4. Compute win ranges ──
    print("\nPhase 4: Computing win pile ranges...")
    win_ranges = compute_win_ranges(win_matrix, expressions)

    if not win_ranges:
        print("  ERROR: No expressions with full win pile coverage.")
        return

    # ── 5. Beam search ──
    print("\nPhase 5: Beam search...")
    proximity_conditions, remaining = run_beam_search(
        trim_matrix, win_ranges, expressions,
        beam_width=beam_width, depth=depth,
    )

    if not proximity_conditions:
        print("\n  No conditions found. Cannot trim further.")
        return

    # ── 6. Validate (NaN = FAIL, hard abort on failure) ──
    print("\nPhase 6: Validating win pile (NaN = FAIL)...")
    if not validate_win_pile(win_matrix, proximity_conditions, expressions):
        print("\n  CRITICAL: Validation failed. Aborting — grinder has a bug.")
        return

    # ── 7. Combine conditions ──
    print(f"\nPhase 7: Combining conditions...")

    pre_names = {c["name"] for c in pre_conditions}
    prox_names = {c["name"] for c in proximity_conditions}
    overlap = pre_names & prox_names

    combined_conditions = list(pre_conditions)
    for pc in proximity_conditions:
        if pc["name"] in overlap:
            combined_conditions = [c for c in combined_conditions if c["name"] != pc["name"]]
        combined_conditions.append(pc)

    n_pre = len(pre_conditions)
    n_prox = len(proximity_conditions)
    n_overlap = len(overlap)
    print(f"  Pre-proximity conditions:  {n_pre}")
    print(f"  Proximity conditions:      {n_prox}")
    print(f"  Overlap (replaced):        {n_overlap}")
    print(f"  Combined total:            {len(combined_conditions)}")

    # ── 8. Re-scan full universe with combined conditions ──
    print(f"\nPhase 8: Re-scanning universe with {len(combined_conditions)} combined conditions...")
    universe_cache = load_5yr_cache()
    raw_signals = rescan_universe(universe_cache, combined_conditions, expr_cache, workers)

    # ── 9. Re-dedup + classify ──
    print(f"\nPhase 9: Dedup + classify...")
    deduped, sacrificial = dedup_with_sacrificial(raw_signals)
    with_exit, no_exit = apply_exit(deduped, universe_cache, exit_cond, direction, expr_cache)
    classified, median_adr = classify_signals(deduped, with_exit, example_bar_lookup)

    winner_signals = [s for s in classified if s["classification"] == "AUTO_WIN"]
    loser_signals = [s for s in classified if s["classification"] == "AUTO_LOSS"]

    # ── 10. Metrics ──
    old_win = len(win_pile)
    old_lose = len(lose_pile)
    old_total = old_win + old_lose
    new_total = len(winner_signals) + len(loser_signals)
    old_wr = old_win / max(old_total, 1) * 100
    new_wr = len(winner_signals) / max(new_total, 1) * 100

    n_deduped = len(deduped)
    final_peak = 0
    if deduped:
        date_counts = Counter(s["date"] for s in deduped)
        final_peak = max(date_counts.values()) if date_counts else 0

    metrics = {
        "win_pile_before": old_win,
        "lose_pile_before": old_lose,
        "winner_signals": len(winner_signals),
        "loser_signals": len(loser_signals),
        "old_total": old_total,
        "new_total": new_total,
        "old_win_rate_pct": round(old_wr, 1),
        "new_win_rate_pct": round(new_wr, 1),
        "n_proximity_conditions": n_prox,
        "n_raw": len(raw_signals),
        "n_deduped": n_deduped,
        "n_sacrificial": len(sacrificial),
        "final_peak": final_peak,
        "median_adr_threshold": median_adr,
    }

    total_time = time.time() - t0

    # ── 11. Print results ──
    print(f"\n{'=' * 70}")
    print(f"  PROXIMITY GRIND RESULTS — {setup_type.upper()}")
    print(f"{'=' * 70}")
    print(f"  Proximity conditions: {n_prox}")
    for c in proximity_conditions:
        print(f"    {c['name']:40s}  [{c['low']:.4f}, {c['high']:.4f}]  ({c['category']})")
    print(f"\n  Conditions: {n_pre} pre + {n_prox} proximity = {len(combined_conditions)} combined")
    print(f"  Signals: {len(raw_signals):,} raw → {n_deduped:,} deduped → "
          f"{len(winner_signals):,} winners / {len(loser_signals):,} losers")
    print(f"  Sacrificial: {len(sacrificial):,}")
    print(f"  Win rate: {old_wr:.1f}% → {new_wr:.1f}%")
    print(f"  Time: {total_time:.0f}s")

    # ── 12. Save locally ──
    print(f"\nPhase 10: Save + upload...")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    result_data = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "step": "proximity_grind",
        "n_conditions": len(combined_conditions),
        "n_pre_conditions": n_pre,
        "n_proximity_conditions": n_prox,
        "all_conditions": combined_conditions,
        "proximity_conditions_only": proximity_conditions,
        "exit_condition": exit_cond,
        "params": {
            "beam_width": beam_width,
            "depth": depth,
            "source": "proximity_grinder",
        },
        "summary": metrics,
        "winner_signals": winner_signals,
        "loser_signals": loser_signals,
        "sacrificial_signals": sacrificial,
    }

    out_dir = os.path.join(REPO_ROOT, "data", "proximity_grind")
    os.makedirs(out_dir, exist_ok=True)

    fname = f"proximity_{setup_type}_{n_prox}cond_{ts}.json"
    out_path = os.path.join(out_dir, fname)
    with open(out_path, "w") as f:
        json.dump(result_data, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    latest = os.path.join(out_dir, f"proximity_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(result_data, f, indent=2, default=str)
    print(f"  Latest: {latest}")

    # Mirror to Railway
    from file_mirror import mirror_file
    mirror_file(out_path)
    mirror_file(latest)

    # Upload to Railway cycle
    try:
        from grind_uploader import upload as railway_upload
        railway_upload(
            result=result_data,
            result_path=out_path,
            step_type="proximity_grind",
            setup_type=setup_type,
            activate=True,
        )
    except Exception as e:
        print(f"\n  WARNING: Railway upload failed: {e}")
        print(f"  Local file saved. Upload manually or retry later.")

    print(f"\n  {'='*70}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  {'='*70}\n")

    return result_data


def main():
    parser = argparse.ArgumentParser(description="Proximity Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--beam", type=int, default=10000, help="Beam width")
    parser.add_argument("--depth", type=int, default=100, help="Search depth")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pile stats only, don't grind")
    args = parser.parse_args()

    run_proximity_grind(
        setup_type=args.setup,
        beam_width=args.beam,
        depth=args.depth,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
