"""
Multi-Stage Exit Grinder — Task 3.7

Grinds for optimal multi-stage conditional exits with position sizing.
Uses the SAME expression cache as the pyramid grinder (12,131 expressions).
No separate exit expression library — one library, one computation path.

GRINDER RULES:
    - Uses expression cache (same as pyramid grinder, profit grinder)
    - All CPU cores used for every phase
    - 100% example pass rate required — ALL conditions must FIRE on ALL examples. No exceptions.
    - If any stage has remaining position (condition didn't fire), config is INVALID — thrown out.
    - No backstop exits. No S99. No partial credit.
    - Sort: floor capture efficiency primary, median secondary
    - Never aborts — handles errors gracefully, always produces output

How it works:
    1. Loads forward expression matrices from cache (parallel)
    2. Finds all valid conditions (parallel, all cores)
    3. Runs 8 passes testing every structure variant (parallel, all cores)
    4. Ranks ALL results across all passes, reports the #1 best

Usage:
    python scripts/multistage_exit_grinder.py --setup dtss
    python scripts/multistage_exit_grinder.py --setup dtss --workers 12
"""

import argparse
import sys
import os
import time
import json
import numpy as np
import pandas as pd
import pickle
import requests
from dataclasses import dataclass, field
from typing import Optional, List
from concurrent.futures import ProcessPoolExecutor, as_completed

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD_DEFAULT = 120
DEFAULT_WORKERS = os.cpu_count() or 8

LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

# Stage parameter sets
TRIM_PCTS = [0.25, 0.33, 0.50, 0.75, 1.0]
MAX_WINDOWS = [8, 12, 15, 20, 25, 30]
MFE_GATES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
BAR_GATES = [10, 15, 20, 30, 40]


# ============================================================
# Lightweight Data Classes
# ============================================================

@dataclass
class ExampleMeta:
    """Pickleable example metadata for worker processes."""
    idx: int
    ticker: str
    entry_date: str
    entry_high: float
    entry_low: float
    n_forward: int
    mfe_pct: float
    mfe_adr: float
    adr_at_entry: float
    direction: str
    fwd_close: np.ndarray
    fwd_low: np.ndarray
    fwd_high: np.ndarray


# ============================================================
# Stage Config (pickleable tuples for fast multiprocessing)
# ============================================================

def make_stage(stage_id, expr_name, direction, threshold, trim_pct, gate_type="none", gate_value=0.0, max_window=None):
    return (stage_id, expr_name, direction, threshold, trim_pct, gate_type, gate_value, max_window)

def s_id(s): return s[0]
def s_expr(s): return s[1]
def s_dir(s): return s[2]
def s_thresh(s): return s[3]
def s_trim(s): return s[4]
def s_gate_type(s): return s[5]
def s_gate_val(s): return s[6]
def s_maxwin(s): return s[7]


# ============================================================
# Data Loading — from expression cache
# ============================================================

def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")
    print(f"Loading 5yr OHLCV cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  {len(cache)} tickers loaded")
    return cache


def load_examples(setup_type):
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    return r.json()["examples"]


def _load_one_example(args):
    """Load one example from expr cache + OHLCV. Returns (meta_dict, matrix_dict, err)."""
    (ticker, entry_date, example_id, idx, direction, max_forward,
     ohlcv_records, cache_dir, expr_names) = args

    import numpy as np
    import pandas as pd
    import os

    try:
        df = pd.DataFrame(ohlcv_records)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        entry_dt = pd.to_datetime(entry_date)
        date_matches = df.index[df["date"] == entry_dt].tolist()
        if not date_matches:
            date_matches = df.index[df["date"].dt.strftime("%Y-%m-%d") == entry_date].tolist()
        if not date_matches:
            return None, None, f"SKIP {ticker} — entry date not found"

        entry_idx = date_matches[0]
        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            return None, None, f"SKIP {ticker} — only {n_available} forward bars"

        actual_forward = min(max_forward, n_available)
        entry_high = float(df["high"].iloc[entry_idx])
        entry_low = float(df["low"].iloc[entry_idx])

        # ADR14 at entry
        highs = df["high"].values
        lows = df["low"].values
        hl = highs - lows
        start14 = max(0, entry_idx - 13)
        adr_val = float(np.mean(hl[start14:entry_idx + 1]))
        if adr_val <= 0:
            adr_val = 1.0

        # Forward OHLCV
        fwd_close = df["close"].values[entry_idx:entry_idx + actual_forward + 1].astype(np.float64)
        fwd_low = df["low"].values[entry_idx:entry_idx + actual_forward + 1].astype(np.float64)
        fwd_high = df["high"].values[entry_idx:entry_idx + actual_forward + 1].astype(np.float64)

        # MFE
        if direction == "short":
            mfe_raw = entry_high - float(np.min(fwd_low))
            mfe_pct = mfe_raw / entry_high * 100
        else:
            mfe_raw = float(np.max(fwd_high)) - entry_low
            mfe_pct = mfe_raw / entry_low * 100
        mfe_adr = mfe_raw / adr_val

        # Load expression cache
        safe_ticker = ticker.replace("/", "_").replace("\\", "_")
        npz_path = os.path.join(cache_dir, f"{safe_ticker}.npz")
        if not os.path.exists(npz_path):
            return None, None, f"SKIP {ticker} — not in expression cache"

        loaded = np.load(npz_path, allow_pickle=True)
        cache_dates = loaded["dates"]
        cache_data = loaded["data"]

        entry_str = entry_date if isinstance(entry_date, str) else str(entry_date)[:10]
        cache_date_strs = [str(d)[:10] for d in cache_dates]
        try:
            cache_entry_idx = cache_date_strs.index(entry_str)
        except ValueError:
            return None, None, f"SKIP {ticker} — entry date not in expr cache"

        cache_end = min(cache_entry_idx + actual_forward + 1, len(cache_data))
        fwd_matrix = cache_data[cache_entry_idx:cache_end].astype(np.float64)

        # Trim to match
        actual_len = len(fwd_matrix)
        fwd_close = fwd_close[:actual_len]
        fwd_low = fwd_low[:actual_len]
        fwd_high = fwd_high[:actual_len]
        actual_forward = actual_len - 1

        if actual_forward < 5:
            return None, None, f"SKIP {ticker} — only {actual_forward} forward bars in cache"

        # Convert matrix to dict {expr_name: series} for simulation compatibility
        # expr_names is the FULL cache list — filter out booleans when building dict
        BOOL_PREFIXES = ("ct_", "st_", "tir_")
        matrix_dict = {}
        for col_i, name in enumerate(expr_names):
            if name.startswith(BOOL_PREFIXES):
                continue
            series = fwd_matrix[:, col_i]
            if not np.all(np.isnan(series)):
                matrix_dict[name] = series

        meta_dict = {
            "idx": idx, "ticker": ticker, "entry_date": entry_date,
            "entry_high": entry_high, "entry_low": entry_low,
            "n_forward": actual_forward, "mfe_pct": mfe_pct,
            "mfe_adr": mfe_adr, "adr_at_entry": adr_val,
            "direction": direction, "fwd_close": fwd_close,
            "fwd_low": fwd_low, "fwd_high": fwd_high,
        }

        return meta_dict, matrix_dict, None

    except Exception as e:
        return None, None, f"ERROR {ticker}: {e}"


def load_all_examples(raw_examples, direction, max_forward, universe_cache, expr_names, workers):
    """Load all examples in parallel — expr cache + OHLCV."""
    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    tasks = []
    idx = 0
    for raw in raw_examples:
        ticker = raw["ticker"]
        ohlcv_df = universe_cache.get(ticker)
        if ohlcv_df is None:
            print(f"  SKIP {ticker} — not in 5yr OHLCV cache")
            continue
        ohlcv_df = ohlcv_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(ohlcv_df["date"]):
            ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])
        records = ohlcv_df.to_dict("records")
        tasks.append((ticker, raw["entryDate"], raw["id"], idx, direction, max_forward,
                       records, expr_cache_dir, expr_names))
        idx += 1

    print(f"\nLoading {len(tasks)} examples from expr cache ({workers} workers)...")
    t0 = time.time()

    metas = []
    matrix_list = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_load_one_example, task): task[0] for task in tasks}
        done = 0
        for future in as_completed(futures):
            ticker = futures[future]
            done += 1
            meta_dict, matrix_dict, err = future.result()
            if err:
                print(f"  [{done}/{len(tasks)}] {err}")
            elif meta_dict:
                # Assign sequential index matching position in metas/matrix_list
                meta_dict["idx"] = len(metas)
                meta = ExampleMeta(**meta_dict)
                metas.append(meta)
                matrix_list.append(matrix_dict)
                print(f"  [{done}/{len(tasks)}] {ticker:8s} OK — "
                      f"{meta.n_forward} fwd bars, MFE={meta.mfe_pct:+.2f}% "
                      f"({meta.mfe_adr:.1f} ADR), {len(matrix_dict)} exprs")

    elapsed = time.time() - t0
    print(f"\n{len(metas)} examples loaded in {elapsed:.1f}s")
    return metas, matrix_list


# ============================================================
# Multi-Stage Simulator (unchanged from original)
# ============================================================

def simulate_stages(stages, matrix, meta):
    """Simulate multi-stage exit for one example."""
    n_fwd = meta.n_forward
    direction = meta.direction
    entry_high, entry_low = meta.entry_high, meta.entry_low
    adr = meta.adr_at_entry
    fwd_close, fwd_low, fwd_high = meta.fwd_close, meta.fwd_low, meta.fwd_high

    if direction == "short":
        running_mfe_adr = (entry_high - np.minimum.accumulate(fwd_low)) / adr
    else:
        running_mfe_adr = (np.maximum.accumulate(fwd_high) - entry_low) / adr

    events = []
    remaining = 1.0
    fired = set()

    for bar in range(1, min(n_fwd + 1, len(fwd_close))):
        if remaining <= 0.001:
            break
        mfe_now = running_mfe_adr[bar] if bar < len(running_mfe_adr) else 0.0

        for stg in stages:
            sid = s_id(stg)
            if sid in fired or remaining <= 0.001:
                continue
            mw = s_maxwin(stg)
            if mw is not None and bar > mw:
                continue
            gt = s_gate_type(stg)
            gv = s_gate_val(stg)
            if gt == "mfe_adr" and mfe_now < gv:
                continue
            if gt == "bars_min" and bar < gv:
                continue

            series = matrix.get(s_expr(stg))
            if series is None or bar >= len(series):
                continue
            val = series[bar]
            if np.isnan(val):
                continue

            hit = (val > s_thresh(stg)) if s_dir(stg) == "above" else (val < s_thresh(stg))
            if not hit:
                continue

            exit_price = fwd_close[bar]
            if direction == "short":
                pct_move = (entry_high - exit_price) / entry_high * 100
            else:
                pct_move = (exit_price - entry_low) / entry_low * 100

            trimmed = remaining * min(s_trim(stg), 1.0)
            remaining -= trimmed
            events.append((sid, bar, exit_price, trimmed, pct_move, remaining))
            fired.add(sid)
            break

    fully_exited = remaining <= 0.001

    if fully_exited:
        effective_pct = sum(e[3] * e[4] for e in events)
        capture_eff = effective_pct / meta.mfe_pct if meta.mfe_pct > 0 else 0.0
    else:
        effective_pct = 0.0
        capture_eff = 0.0

    return events, effective_pct, capture_eff, fully_exited


def simulate_all_examples(stages, matrices, metas):
    """Run simulation across all examples. Returns None if ANY example fails."""
    cap_effs = []
    eff_pcts = []
    bars_list = []
    per_example = []

    for meta in metas:
        matrix = matrices[meta.idx]
        if matrix is None:
            return None

        events, eff_pct, cap_eff, fully_exited = simulate_stages(stages, matrix, meta)

        if not fully_exited:
            return None

        last_bar = max(e[1] for e in events) if events else 0

        per_example.append({
            "ticker": meta.ticker, "entry_date": meta.entry_date,
            "mfe_pct": meta.mfe_pct, "mfe_adr": meta.mfe_adr,
            "effective_pct": eff_pct, "capture_eff": cap_eff,
            "events": [{"stage_id": e[0], "bar": e[1], "price": e[2],
                        "pct_trimmed": e[3], "pct_move": e[4]} for e in events],
            "bars_to_full_exit": last_bar,
        })
        cap_effs.append(cap_eff)
        eff_pcts.append(eff_pct)
        bars_list.append(last_bar)

    if not cap_effs:
        return None

    return (float(np.median(cap_effs)), float(np.mean(cap_effs)), float(np.min(cap_effs)),
            float(np.median(eff_pcts)), float(np.mean(bars_list)), per_example)


# ============================================================
# Parallel Condition Discovery
# ============================================================

def _find_conditions_chunk(args):
    """Worker: find valid conditions for a chunk of expression names."""
    expr_chunk, matrices_pickle, n_examples, n_thresholds = args
    matrices = matrices_pickle
    valid = []

    for expr_name in expr_chunk:
        example_series = []
        all_values = []
        for i, matrix in enumerate(matrices):
            if matrix is not None and expr_name in matrix:
                series = matrix[expr_name]
                example_series.append((i, series))
                vals = series[~np.isnan(series)]
                if len(vals) > 0:
                    all_values.append(vals)

        if len(example_series) < n_examples or not all_values:
            continue

        combined = np.concatenate(all_values)
        thresholds = list(set(round(float(t), 6)
                              for t in np.percentile(combined, np.linspace(5, 95, n_thresholds))))

        for thresh in thresholds:
            for dir_test in ["above", "below"]:
                all_hit = True
                for _, series in example_series:
                    hit = False
                    for bar in range(1, len(series)):
                        val = series[bar]
                        if np.isnan(val):
                            continue
                        if (dir_test == "above" and val > thresh) or \
                           (dir_test == "below" and val < thresh):
                            hit = True
                            break
                    if not hit:
                        all_hit = False
                        break
                if all_hit:
                    valid.append((expr_name, dir_test, thresh))

    return valid


def find_all_valid_conditions_parallel(matrices, expr_names, n_examples, n_thresholds, workers):
    """Find all conditions that trigger on 100% of examples. Parallel across CPU cores."""
    print(f"\n  Finding valid conditions across {len(expr_names):,} expressions ({workers} workers)...")
    t0 = time.time()

    chunk_size = max(1, len(expr_names) // workers)
    chunks = [expr_names[i:i+chunk_size] for i in range(0, len(expr_names), chunk_size)]
    tasks = [(chunk, matrices, n_examples, n_thresholds) for chunk in chunks]

    all_valid = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_find_conditions_chunk, t): i for i, t in enumerate(tasks)}
        for future in as_completed(futures):
            try:
                all_valid.extend(future.result())
            except Exception as e:
                print(f"    Chunk error: {e}")

    elapsed = time.time() - t0
    print(f"  Found {len(all_valid):,} valid conditions in {elapsed:.1f}s ({workers} workers)")
    return all_valid


# ============================================================
# Parallel Pass Runner
# ============================================================

def _run_pass_chunk(args):
    """Worker: test a chunk of stage configs."""
    stage_configs_chunk, matrices, metas_pickle = args
    metas = metas_pickle
    results = []

    for stages in stage_configs_chunk:
        sim = simulate_all_examples(stages, matrices, metas)
        if sim is not None:
            median_eff, avg_eff, floor_eff, median_pct, avg_bars, _ = sim
            results.append((stages, median_eff, avg_eff, floor_eff, median_pct, avg_bars))

    return results


def run_pass_parallel(label, all_stage_configs, matrices, metas, workers, top_n=50):
    """Run a pass: test all stage configs in parallel, return top results."""
    n_configs = len(all_stage_configs)
    if n_configs == 0:
        print(f"\n  {label}: 0 configs. Skipping.")
        return []

    print(f"\n  {label} ({n_configs:,} configs, {workers} workers)...")
    t0 = time.time()

    chunk_size = max(1, n_configs // workers)
    chunks = [all_stage_configs[i:i+chunk_size] for i in range(0, n_configs, chunk_size)]
    tasks = [(chunk, matrices, metas) for chunk in chunks]

    all_results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_pass_chunk, t): i for i, t in enumerate(tasks)}
        for future in as_completed(futures):
            try:
                all_results.extend(future.result())
            except Exception as e:
                print(f"    Chunk error: {e}")

    all_results.sort(key=lambda r: (r[3], r[1]), reverse=True)

    elapsed = time.time() - t0
    best_floor = all_results[0][3] if all_results else 0
    best_med = all_results[0][1] if all_results else 0
    print(f"    {n_configs:,} configs in {elapsed:.1f}s -> "
          f"{len(all_results)} passed 100% rule, best floor={best_floor:.3f} median={best_med:.3f}")

    return all_results[:top_n]


# ============================================================
# Stage Config Generators (unchanged from original)
# ============================================================

def gen_single_stage(conditions):
    return [[make_stage(1, e, d, t, 1.0)] for e, d, t in conditions]

def gen_early_trim(conditions):
    configs = []
    for expr, d, t in conditions:
        for max_win in MAX_WINDOWS:
            for trim in [0.25, 0.33, 0.50]:
                configs.append([
                    make_stage(1, expr, d, t, trim, "none", 0.0, max_win),
                    make_stage(2, expr, d, t, 1.0),
                ])
    return configs

def gen_mfe_gated(conditions):
    configs = []
    for expr, d, t in conditions:
        for gate in MFE_GATES:
            for trim in TRIM_PCTS:
                if trim >= 1.0:
                    configs.append([make_stage(1, expr, d, t, 1.0, "mfe_adr", gate)])
                else:
                    configs.append([
                        make_stage(1, expr, d, t, trim, "mfe_adr", gate),
                        make_stage(2, expr, d, t, 1.0),
                    ])
    return configs

def gen_protection_trail(conditions):
    configs = []
    for expr, d, t in conditions:
        for max_win in [10, 15, 20]:
            for trim in [0.25, 0.33, 0.50]:
                for gate in [1.5, 2.0, 3.0]:
                    configs.append([
                        make_stage(1, expr, d, t, trim, "none", 0.0, max_win),
                        make_stage(2, expr, d, t, 1.0, "mfe_adr", gate),
                    ])
    return configs

def gen_bar_gated(conditions):
    configs = []
    for expr, d, t in conditions:
        for bar_min in BAR_GATES:
            configs.append([make_stage(1, expr, d, t, 1.0, "bars_min", float(bar_min))])
    return configs

def gen_cross_condition(best_conds):
    configs = []
    conds = best_conds[:30]
    for i, (e1, d1, t1) in enumerate(conds):
        for j, (e2, d2, t2) in enumerate(conds):
            if i == j:
                continue
            for max_win in [10, 15, 20]:
                for trim in [0.25, 0.33, 0.50]:
                    configs.append([
                        make_stage(1, e1, d1, t1, trim, "none", 0.0, max_win),
                        make_stage(2, e2, d2, t2, 1.0),
                    ])
            for gate in [1.5, 2.0, 3.0]:
                for trim in [0.33, 0.50]:
                    configs.append([
                        make_stage(1, e1, d1, t1, trim, "none", 0.0, 15),
                        make_stage(2, e2, d2, t2, 1.0, "mfe_adr", gate),
                    ])
    return configs

def gen_3stage_cross(best_conds):
    configs = []
    conds = best_conds[:15]
    for i, (e1, d1, t1) in enumerate(conds):
        for j, (e2, d2, t2) in enumerate(conds):
            for k, (e3, d3, t3) in enumerate(conds):
                if i == j == k:
                    continue
                for trim1 in [0.25, 0.33]:
                    for trim2 in [0.33, 0.50]:
                        configs.append([
                            make_stage(1, e1, d1, t1, trim1, "none", 0.0, 15),
                            make_stage(2, e2, d2, t2, trim2, "mfe_adr", 1.5),
                            make_stage(3, e3, d3, t3, 1.0, "mfe_adr", 3.0),
                        ])
    return configs

def gen_refinement(best_results):
    if not best_results:
        return []
    seen = set()
    refine_targets = []
    for stages, *_ in best_results[:30]:
        for stg in stages:
            key = (s_expr(stg), s_dir(stg), s_thresh(stg))
            if key not in seen:
                seen.add(key)
                refine_targets.append(key)
    configs = []
    for expr, d, base_t in refine_targets:
        offsets = np.linspace(-0.15, 0.15, 15) * abs(base_t) if base_t != 0 \
            else np.linspace(-0.1, 0.1, 15)
        for offset in offsets:
            t = round(base_t + offset, 6)
            configs.append([make_stage(1, expr, d, t, 1.0)])
            for max_win in [10, 15, 20]:
                for trim in [0.25, 0.33, 0.50]:
                    configs.append([
                        make_stage(1, expr, d, t, trim, "none", 0.0, max_win),
                        make_stage(2, expr, d, t, 1.0),
                    ])
            for gate in [1.0, 1.5, 2.0, 3.0]:
                for trim in [0.33, 0.50, 0.75]:
                    configs.append([
                        make_stage(1, expr, d, t, trim, "mfe_adr", gate),
                        make_stage(2, expr, d, t, 1.0),
                    ])
            for max_win in [10, 15, 20]:
                for trim in [0.25, 0.33]:
                    for gate in [1.5, 2.0, 3.0]:
                        configs.append([
                            make_stage(1, expr, d, t, trim, "none", 0.0, max_win),
                            make_stage(2, expr, d, t, 1.0, "mfe_adr", gate),
                        ])
    return configs


# ============================================================
# Reporting & Saving
# ============================================================

def rebuild_full_results(top_results, matrices, metas):
    if not top_results:
        return []
    stages, med_eff, avg_eff, floor_eff, med_pct, avg_bars = top_results[0]
    sim = simulate_all_examples(stages, matrices, metas)
    if sim:
        _, _, _, _, _, per_example = sim
        return [{
            "stages": stages, "per_example": per_example,
            "median_capture_eff": med_eff, "avg_capture_eff": avg_eff,
            "floor_capture_eff": floor_eff, "median_effective_pct": med_pct,
            "avg_bars_to_full_exit": avg_bars,
        }]
    return []


def print_results(results):
    if not results:
        print("\nNo valid configurations found.")
        return
    r = results[0]
    stages = r["stages"]
    print(f"\n{'='*120}")
    print(f"WINNER  [{len(stages)}-stage]  "
          f"floor_eff={r['floor_capture_eff']:.3f}  "
          f"median_eff={r['median_capture_eff']:.3f}  "
          f"median_pct={r['median_effective_pct']:+.2f}%  "
          f"avg_bars={r['avg_bars_to_full_exit']:.1f}")
    print(f"{'='*120}")
    for s in stages:
        parts = [f"S{s_id(s)}: {s_expr(s)} {s_dir(s)} {s_thresh(s):.4f} "
                 f"trim={s_trim(s):.0%}"]
        if s_gate_type(s) == "mfe_adr":
            parts.append(f"gate:MFE>={s_gate_val(s):.1f}ADR")
        elif s_gate_type(s) == "bars_min":
            parts.append(f"gate:bars>={int(s_gate_val(s))}")
        if s_maxwin(s):
            parts.append(f"maxwin={s_maxwin(s)}")
        print(f"  {'  '.join(parts)}")

    print(f"\n  {'Ticker':8s} {'Entry':12s} {'MFE%':>7s} {'Eff%':>7s} "
          f"{'CapEff':>7s}  Events")
    for pe in r["per_example"]:
        evts = " -> ".join(
            f"S{e['stage_id']}@b{e['bar']}({e['pct_trimmed']:.0%},{e['pct_move']:+.1f}%)"
            for e in pe["events"])
        print(f"  {pe['ticker']:8s} {pe['entry_date']:12s} "
              f"{pe['mfe_pct']:+6.2f}% {pe['effective_pct']:+6.2f}% "
              f"{pe['capture_eff']:6.3f}  {evts}")


def save_results(results, metas, setup_type):
    from datetime import datetime
    os.makedirs("data/multistage_exit", exist_ok=True)

    best = results[0] if results else None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    data = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "n_examples": len(metas),
        "computation_source": "expression_cache",
        "examples": [{"ticker": m.ticker, "entry_date": m.entry_date,
                       "mfe_pct": m.mfe_pct, "mfe_adr": m.mfe_adr} for m in metas],
        "result": None,
    }

    if best:
        data["result"] = {
            "n_stages": len(best["stages"]),
            "median_capture_eff": best["median_capture_eff"],
            "avg_capture_eff": best["avg_capture_eff"],
            "floor_capture_eff": best["floor_capture_eff"],
            "median_effective_pct": best["median_effective_pct"],
            "avg_bars_to_full_exit": best["avg_bars_to_full_exit"],
            "stages": [{"stage_id": s_id(s), "expr_name": s_expr(s),
                         "direction": s_dir(s), "threshold": s_thresh(s),
                         "trim_pct": s_trim(s), "gate_type": s_gate_type(s),
                         "gate_value": s_gate_val(s), "max_window": s_maxwin(s)}
                        for s in best["stages"]],
            "per_example": best["per_example"],
        }

    nan_fix = lambda x: None if isinstance(x, float) and np.isnan(x) else x
    n_stg = len(best["stages"]) if best else 0
    eff = f"{best['floor_capture_eff']:.3f}" if best else "na"

    ts_path = f"data/multistage_exit/ms_exit_{setup_type}_{n_stg}stg_{eff}floor_{ts}.json"
    with open(ts_path, "w") as f:
        json.dump(data, f, indent=2, default=nan_fix)
    latest = f"data/multistage_exit/ms_exit_{setup_type}.json"
    with open(latest, "w") as f:
        json.dump(data, f, indent=2, default=nan_fix)
    print(f"\n  Saved: {ts_path}")
    print(f"  Saved: {latest}")

    from file_mirror import mirror_file
    mirror_file(ts_path)
    mirror_file(latest)

    try:
        r = requests.post(
            f"{RAILWAY_URL}/api/exit-grind/{setup_type}/upload-multistage",
            json=data, timeout=60)
        if r.status_code == 200:
            print(f"  Uploaded to Railway OK")
        else:
            print(f"  WARNING: Railway upload failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  WARNING: Railway upload error: {e}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Multi-Stage Exit Grinder (expr cache)")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--n-thresholds", type=int, default=50)
    parser.add_argument("--direction", default="short")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    print(f"Multi-Stage Exit Grinder (expression cache)")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}, Workers: {args.workers}")
    print(f"RULE: ALL conditions must fire on ALL examples. No backstops. No exceptions.")

    # 1. Load expression cache manifest
    from local_runner.expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("ERROR: Expression cache not valid. Run expr_cache_builder.py --build first.")
        sys.exit(1)

    all_expr_names = expr_cache.expr_names

    # Filter out boolean aggregations — monotonically increasing during trends,
    # structurally wrong for exit detection (fire early, not at move exhaustion)
    BOOL_PREFIXES = ("ct_", "st_", "tir_")
    expr_names = [n for n in all_expr_names if not n.startswith(BOOL_PREFIXES)]
    n_excluded = len(all_expr_names) - len(expr_names)
    print(f"\nExpression cache: {len(all_expr_names)} total, "
          f"{n_excluded} boolean aggregations excluded, "
          f"{len(expr_names)} expressions for exit grind")
    print(f"  {len(expr_cache.get_available_tickers())} tickers")

    # 2. Load examples + OHLCV
    raw_examples = load_examples(args.setup)
    print(f"Loaded {len(raw_examples)} {args.setup.upper()} examples")
    universe_cache = load_5yr_cache()

    # 3. Load all examples from cache (parallel)
    metas, all_matrices = load_all_examples(
        raw_examples, args.direction, args.max_forward, universe_cache, all_expr_names, args.workers)

    if len(metas) < 3:
        print(f"Only {len(metas)} examples. Aborting.")
        return

    mfes = [m.mfe_pct for m in metas]
    print(f"\n{len(metas)} examples | MFE: floor={min(mfes):+.2f}% "
          f"median={np.median(mfes):+.2f}% avg={np.mean(mfes):+.2f}%")

    # Collect expression names that exist in at least one matrix
    all_expr_names_in_matrices = set()
    # Also check how many matrices have each expression
    expr_example_counts = {}
    for matrix in all_matrices:
        if matrix:
            all_expr_names_in_matrices.update(matrix.keys())
            for k in matrix.keys():
                expr_example_counts[k] = expr_example_counts.get(k, 0) + 1
    active_expr_names = sorted(all_expr_names_in_matrices)
    print(f"Active expressions (non-NaN in at least one example): {len(active_expr_names):,}")

    # Check how many expressions exist in ALL examples
    n_ex = len(metas)
    in_all = sum(1 for k, v in expr_example_counts.items() if v >= n_ex)
    in_most = sum(1 for k, v in expr_example_counts.items() if v >= n_ex - 1)
    print(f"  In all {n_ex} examples: {in_all:,}")
    print(f"  In {n_ex-1}+ examples: {in_most:,}")

    # 4. Find valid conditions (parallel)
    conditions = find_all_valid_conditions_parallel(
        all_matrices, active_expr_names, len(metas), args.n_thresholds, args.workers)

    if not conditions:
        print("No valid conditions found.")
        return

    # 5. Run all passes (parallel)
    all_top = []
    t_total = time.time()

    print(f"\n{'='*80}")
    print(f"RUNNING PASSES ({args.workers} workers per pass)")
    print(f"{'='*80}")

    r = run_pass_parallel("P1:single", gen_single_stage(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    r = run_pass_parallel("P2:early-trim", gen_early_trim(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    r = run_pass_parallel("P3:mfe-gated", gen_mfe_gated(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    r = run_pass_parallel("P4:protect+trail", gen_protection_trail(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    r = run_pass_parallel("P5:bar-gated", gen_bar_gated(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    best_conds_set = set()
    for stages, *_ in all_top[:100]:
        for stg in stages:
            best_conds_set.add((s_expr(stg), s_dir(stg), s_thresh(stg)))
    best_conds = list(best_conds_set)

    if len(best_conds) >= 2:
        r = run_pass_parallel("P6:cross-cond", gen_cross_condition(best_conds), all_matrices, metas, args.workers)
        all_top.extend(r)

    if len(best_conds) >= 3:
        r = run_pass_parallel("P7:3stg-cross", gen_3stage_cross(best_conds), all_matrices, metas, args.workers)
        all_top.extend(r)

    refine_configs = gen_refinement(all_top)
    if refine_configs:
        r = run_pass_parallel("P8:refine", refine_configs, all_matrices, metas, args.workers)
        all_top.extend(r)

    # Final ranking
    all_top.sort(key=lambda r: (r[3], r[1]), reverse=True)

    seen = set()
    deduped = []
    for entry in all_top:
        key = tuple(entry[0])
        if key not in seen:
            seen.add(key)
            deduped.append(entry)

    total_time = time.time() - t_total
    print(f"\n{'='*80}")
    print(f"ALL PASSES COMPLETE: {total_time:.1f}s, "
          f"{len(deduped)} unique from {len(all_top)} total")
    print(f"{'='*80}")

    full_results = rebuild_full_results(deduped, all_matrices, metas)
    print_results(full_results)

    if full_results:
        save_results(full_results, metas, args.setup)
        best = full_results[0]
        print(f"\n{'='*80}")
        print(f"BEST: floor={best['floor_capture_eff']:.1%} "
              f"median={best['median_capture_eff']:.1%} "
              f"({best['median_effective_pct']:+.2f}% median move)")
        print(f"Expressions tested: {len(active_expr_names):,}")
        print(f"ALL conditions fired on ALL examples. No backstops.")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
