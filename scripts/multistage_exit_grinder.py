"""
Multi-Stage Exit Grinder — Task 3.7

Grinds for optimal multi-stage conditional exits with position sizing.
Separate from single-stage exit_grinder.py — both systems coexist.

GRINDER RULES:
    - Uses exact same computation methods as exit_grinder.py (ExitExprEngine, same expressions)
    - All CPU cores used for every phase (matrix build, condition discovery, all passes)
    - 100% example pass rate required — ALL conditions must FIRE on ALL examples. No exceptions.
    - If any stage has remaining position (condition didn't fire), config is INVALID — thrown out.
    - No backstop exits. No S99. Condition fires or config fails.
    - Sort: floor capture efficiency primary, median secondary (matches exit_grinder.py)
    - Never aborts — handles errors gracefully, always produces output

How it works:
    1. Builds expression matrices (parallel, all cores)
    2. Finds all valid conditions (parallel, all cores)
    3. Runs 8 passes testing every structure variant (parallel, all cores)
    4. Ranks ALL results across all passes, reports the best

Usage:
    python scripts/multistage_exit_grinder.py --setup dtss
    python scripts/multistage_exit_grinder.py --setup dtss --workers 12
    python scripts/multistage_exit_grinder.py --setup dtss --base-only
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.exit_expressions import (
    generate_exit_expressions, generate_exit_boolean_conditions,
    WINDOWS,
)

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
# Lightweight Data Classes (pickleable for multiprocessing)
# ============================================================

@dataclass
class ExampleMeta:
    """Pickleable example metadata for worker processes (no DataFrame)."""
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


@dataclass
class ExampleData:
    """Full example with DataFrame (for matrix building only)."""
    id: int
    ticker: str
    entry_date: str
    df: pd.DataFrame
    entry_idx: int
    entry_high: float
    entry_low: float
    n_forward: int
    mfe_pct: float
    mfe_adr: float
    adr_at_entry: float
    direction: str
    fwd_close: np.ndarray = field(default=None, repr=False)
    fwd_low: np.ndarray = field(default=None, repr=False)
    fwd_high: np.ndarray = field(default=None, repr=False)

    def to_meta(self, idx):
        return ExampleMeta(
            idx=idx, ticker=self.ticker, entry_date=self.entry_date,
            entry_high=self.entry_high, entry_low=self.entry_low,
            n_forward=self.n_forward, mfe_pct=self.mfe_pct,
            mfe_adr=self.mfe_adr, adr_at_entry=self.adr_at_entry,
            direction=self.direction, fwd_close=self.fwd_close,
            fwd_low=self.fwd_low, fwd_high=self.fwd_high,
        )


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
# Data Loading
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


def build_example_data(example, direction, max_forward, universe_cache):
    ticker = example["ticker"]
    entry_date = example["entryDate"]
    try:
        df = universe_cache.get(ticker)
        if df is None:
            return None
        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        entry_dt = pd.to_datetime(entry_date)
        date_matches = df.index[df["date"] == entry_dt].tolist()
        if not date_matches:
            date_matches = df.index[df["date"].dt.strftime("%Y-%m-%d") == entry_date].tolist()
        if not date_matches:
            return None
        entry_idx = date_matches[0]
        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            return None

        actual_forward = min(max_forward, n_available)
        entry_high = df["high"].iloc[entry_idx]
        entry_low = df["low"].iloc[entry_idx]

        from scripts.expression_engine import ExpressionEngine
        adr_at_entry = ExpressionEngine(df)._adr(14).values[entry_idx]
        if np.isnan(adr_at_entry) or adr_at_entry <= 0:
            adr_at_entry = 1.0

        fwd_close = df["close"].values[entry_idx:entry_idx + actual_forward + 1]
        fwd_low = df["low"].values[entry_idx:entry_idx + actual_forward + 1]
        fwd_high = df["high"].values[entry_idx:entry_idx + actual_forward + 1]

        if direction == "short":
            mfe_raw = entry_high - np.min(fwd_low)
            mfe_pct = mfe_raw / entry_high * 100
        else:
            mfe_raw = np.max(fwd_high) - entry_low
            mfe_pct = mfe_raw / entry_low * 100

        return ExampleData(
            id=example["id"], ticker=ticker, entry_date=entry_date, df=df,
            entry_idx=entry_idx, entry_high=entry_high, entry_low=entry_low,
            n_forward=actual_forward, mfe_pct=mfe_pct,
            mfe_adr=mfe_raw / adr_at_entry, adr_at_entry=adr_at_entry,
            direction=direction, fwd_close=fwd_close,
            fwd_low=fwd_low, fwd_high=fwd_high,
        )
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")
        return None


# ============================================================
# Expression Matrix Building (parallel — same as exit_grinder.py)
# ============================================================

def _build_one_example_matrix(args):
    ex_dict, expressions, direction, spy_pickle = args
    import pandas as pd, numpy as np, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.exit_compute import ExitExprEngine

    df = pd.DataFrame(ex_dict["df_records"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    spy_df = None
    if spy_pickle is not None:
        spy_df = pd.DataFrame(spy_pickle)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in spy_df.columns:
                spy_df[col] = pd.to_numeric(spy_df[col], errors="coerce")

    engine = ExitExprEngine(df, ex_dict["entry_idx"], direction=direction,
                            spy_df=spy_df, max_forward=ex_dict["n_forward"])
    result = {}
    failed = 0
    for expr in expressions:
        try:
            result[expr["name"]] = engine.compute(expr["compute"])
        except Exception:
            failed += 1
    return ex_dict["ticker"], result, failed, len(expressions)


def build_all_matrices(examples, expressions, direction, spy_df, workers):
    spy_records = spy_df.to_dict("records") if spy_df is not None else None
    tasks = [({
        "ticker": ex.ticker, "entry_idx": ex.entry_idx,
        "n_forward": ex.n_forward, "df_records": ex.df.to_dict("records"),
    }, expressions, direction, spy_records) for ex in examples]

    print(f"\nComputing {len(expressions)} expressions × {len(examples)} examples "
          f"({workers} workers)...")
    t0 = time.time()
    matrices = [None] * len(examples)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_build_one_example_matrix, t): i for i, t in enumerate(tasks)}
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                ticker, matrix, failed, total = future.result()
                matrices[idx] = matrix
                done += 1
                print(f"  [{done}/{len(examples)}] {ticker:8s} — "
                      f"{total-failed}/{total}" + (f" ({failed} failed)" if failed else ""))
            except Exception as e:
                done += 1
                print(f"  [{done}/{len(examples)}] ERROR: {e}")
    print(f"Matrix build: {time.time()-t0:.1f}s")
    return matrices


# ============================================================
# Multi-Stage Simulator
# ============================================================

def simulate_stages(stages, matrix, meta):
    """Simulate multi-stage exit for one example.
    
    Returns: (events_list, effective_capture_pct, capture_efficiency, fully_exited)
    
    fully_exited is True only if ALL position was exited by conditions firing.
    No backstop. No S99. If conditions don't fire, fully_exited=False.
    """
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

    # NO BACKSTOP. If remaining > 0, the config FAILED on this example.
    fully_exited = remaining <= 0.001

    if fully_exited:
        effective_pct = sum(e[3] * e[4] for e in events)
        capture_eff = effective_pct / meta.mfe_pct if meta.mfe_pct > 0 else 0.0
    else:
        effective_pct = 0.0
        capture_eff = 0.0

    return events, effective_pct, capture_eff, fully_exited


def simulate_all_examples(stages, matrices, metas):
    """Run simulation across all examples.
    
    Returns None if ANY example fails (condition didn't fire / position not fully exited).
    This enforces the 100% pass rule — no exceptions.
    """
    cap_effs = []
    eff_pcts = []
    bars_list = []
    per_example = []

    for meta in metas:
        matrix = matrices[meta.idx]
        if matrix is None:
            return None  # missing matrix = fail

        events, eff_pct, cap_eff, fully_exited = simulate_stages(stages, matrix, meta)

        if not fully_exited:
            return None  # HARD FAIL: condition didn't fire on this example

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
    print(f"\n  Finding valid conditions across {len(expr_names)} expressions ({workers} workers)...")
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

    # Sort: floor primary, median secondary (matches exit_grinder.py)
    all_results.sort(key=lambda r: (r[3], r[1]), reverse=True)

    elapsed = time.time() - t0
    best_floor = all_results[0][3] if all_results else 0
    best_med = all_results[0][1] if all_results else 0
    print(f"    {n_configs:,} configs in {elapsed:.1f}s → "
          f"{len(all_results)} passed 100% rule, best floor={best_floor:.3f} median={best_med:.3f}")

    return all_results[:top_n]


# ============================================================
# Stage Config Generators
# ============================================================

def gen_single_stage(conditions):
    """Pass 1: single condition exits 100%."""
    return [[make_stage(1, e, d, t, 1.0)] for e, d, t in conditions]


def gen_early_trim(conditions):
    """Pass 2: early partial trim + full exit."""
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
    """Pass 3: MFE-gated partial + full exit."""
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
    """Pass 4: max_window early trim + MFE-gated full exit."""
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
    """Pass 5: bar-gated exit."""
    configs = []
    for expr, d, t in conditions:
        for bar_min in BAR_GATES:
            configs.append([make_stage(1, expr, d, t, 1.0, "bars_min", float(bar_min))])
    return configs


def gen_cross_condition(best_conds):
    """Pass 6: different conditions per stage."""
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
    """Pass 7: 3-stage with different conditions."""
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
    """Pass 8: finer thresholds around best results."""
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

def rebuild_full_results(top_results, matrices, metas, top_n=50):
    """Re-simulate top results to get full per-example data for reporting."""
    full = []
    for stages, med_eff, avg_eff, floor_eff, med_pct, avg_bars in top_results[:top_n]:
        sim = simulate_all_examples(stages, matrices, metas)
        if sim:
            _, _, _, _, _, per_example = sim
            full.append({
                "stages": stages, "per_example": per_example,
                "median_capture_eff": med_eff, "avg_capture_eff": avg_eff,
                "floor_capture_eff": floor_eff, "median_effective_pct": med_pct,
                "avg_bars_to_full_exit": avg_bars,
            })
    return full


def print_results(results, top_n=20):
    if not results:
        print("\nNo valid configurations found.")
        return

    print(f"\n{'='*120}")
    print(f"TOP {min(top_n, len(results))} EXIT CONFIGURATIONS (all passes)")
    print(f"Sorted by: floor capture efficiency (primary), median (secondary)")
    print(f"Rule: ALL conditions must fire on ALL examples. No backstops. No exceptions.")
    print(f"{'='*120}")

    for rank, r in enumerate(results[:top_n], 1):
        stages = r["stages"]
        stage_ids = [s_id(s) for s in stages]
        print(f"\n{'─'*120}")
        print(f"#{rank}  [{len(stages)}-stage: {stage_ids}]  "
              f"floor_eff={r['floor_capture_eff']:.3f}  "
              f"median_eff={r['median_capture_eff']:.3f}  "
              f"median_pct={r['median_effective_pct']:+.2f}%  "
              f"avg_bars={r['avg_bars_to_full_exit']:.1f}")

        for s in stages:
            parts = [f"S{s_id(s)}: {s_expr(s)} {s_dir(s)} {s_thresh(s):.4f} "
                     f"trim={s_trim(s):.0%}"]
            if s_gate_type(s) == "mfe_adr":
                parts.append(f"gate:MFE≥{s_gate_val(s):.1f}ADR")
            elif s_gate_type(s) == "bars_min":
                parts.append(f"gate:bars≥{int(s_gate_val(s))}")
            if s_maxwin(s):
                parts.append(f"maxwin={s_maxwin(s)}")
            print(f"      {'  '.join(parts)}")

        print(f"      {'Ticker':8s} {'Entry':12s} {'MFE%':>7s} {'Eff%':>7s} "
              f"{'CapEff':>7s}  Events")
        for pe in r["per_example"]:
            evts = " → ".join(
                f"S{e['stage_id']}@b{e['bar']}({e['pct_trimmed']:.0%},{e['pct_move']:+.1f}%)"
                for e in pe["events"])
            print(f"      {pe['ticker']:8s} {pe['entry_date']:12s} "
                  f"{pe['mfe_pct']:+6.2f}% {pe['effective_pct']:+6.2f}% "
                  f"{pe['capture_eff']:6.3f}  {evts}")


def save_results(results, examples, setup_type):
    from datetime import datetime
    os.makedirs("data/multistage_exit", exist_ok=True)

    best = results[0] if results else None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    data = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "n_examples": len(examples),
        "examples": [{"ticker": ex.ticker, "entry_date": ex.entry_date,
                       "mfe_pct": ex.mfe_pct, "mfe_adr": ex.mfe_adr} for ex in examples],
        "results": [],
    }
    for rank, r in enumerate(results[:50]):
        data["results"].append({
            "rank": rank + 1,
            "n_stages": len(r["stages"]),
            "median_capture_eff": r["median_capture_eff"],
            "avg_capture_eff": r["avg_capture_eff"],
            "floor_capture_eff": r["floor_capture_eff"],
            "median_effective_pct": r["median_effective_pct"],
            "avg_bars_to_full_exit": r["avg_bars_to_full_exit"],
            "stages": [{"stage_id": s_id(s), "expr_name": s_expr(s),
                         "direction": s_dir(s), "threshold": s_thresh(s),
                         "trim_pct": s_trim(s), "gate_type": s_gate_type(s),
                         "gate_value": s_gate_val(s), "max_window": s_maxwin(s)}
                        for s in r["stages"]],
            "per_example": r["per_example"],
        })

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


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Multi-Stage Exit Grinder")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--n-thresholds", type=int, default=20)
    parser.add_argument("--direction", default="short")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--base-only", action="store_true",
                        help="Skip boolean aggregations (faster)")
    args = parser.parse_args()

    print(f"Multi-Stage Exit Grinder")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}, Workers: {args.workers}")
    print(f"RULE: ALL conditions must fire on ALL examples. No backstops. No exceptions.")

    # --- Load ---
    raw_examples = load_examples(args.setup)
    print(f"Loaded {len(raw_examples)} {args.setup.upper()} examples")
    universe_cache = load_5yr_cache()

    spy_df = universe_cache.get("SPY")
    if spy_df is not None:
        spy_df = spy_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(spy_df["date"]):
            spy_df["date"] = pd.to_datetime(spy_df["date"])
        spy_df = spy_df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            spy_df[col] = pd.to_numeric(spy_df[col], errors="coerce")

    examples = []
    for raw in raw_examples:
        ex = build_example_data(raw, args.direction, args.max_forward, universe_cache)
        if ex:
            examples.append(ex)
            print(f"  {ex.ticker:8s} {ex.entry_date} — {ex.n_forward} bars, "
                  f"MFE={ex.mfe_pct:+.2f}% ({ex.mfe_adr:.1f} ADR)")
        else:
            print(f"  {raw['ticker']:8s} SKIP")

    if len(examples) < 3:
        print(f"Only {len(examples)} examples. Aborting.")
        return

    mfes = [ex.mfe_pct for ex in examples]
    print(f"\n{len(examples)} examples | MFE: floor={min(mfes):+.2f}% "
          f"median={np.median(mfes):+.2f}% avg={np.mean(mfes):+.2f}%")

    # --- Build expression matrices (parallel, all cores) ---
    base_exprs = generate_exit_expressions()
    native_bools, threshold_bools = generate_exit_boolean_conditions(base_exprs)
    print(f"\n{len(base_exprs)} base expressions, "
          f"{len(native_bools)} native bools, {len(threshold_bools)} threshold bools")

    base_matrices = build_all_matrices(examples, base_exprs, args.direction, spy_df, args.workers)

    if not args.base_only:
        from scripts.exit_grinder import compute_boolean_aggregations
        agg_matrices = compute_boolean_aggregations(
            base_matrices, native_bools, threshold_bools,
            [ex.n_forward for ex in examples])
        all_matrices = []
        for i in range(len(examples)):
            merged = {}
            if base_matrices[i]: merged.update(base_matrices[i])
            if agg_matrices[i]: merged.update(agg_matrices[i])
            all_matrices.append(merged)
        all_expr_names = [e["name"] for e in base_exprs]
        for m in agg_matrices:
            if m:
                all_expr_names.extend(sorted(m.keys()))
                break
    else:
        all_matrices = base_matrices
        all_expr_names = [e["name"] for e in base_exprs]

    print(f"Total expressions: {len(all_expr_names)}")

    # Build lightweight metas
    metas = [ex.to_meta(i) for i, ex in enumerate(examples)]

    # --- Find valid conditions (parallel, all cores) ---
    conditions = find_all_valid_conditions_parallel(
        all_matrices, all_expr_names, len(examples), args.n_thresholds, args.workers)

    if not conditions:
        print("No valid conditions found.")
        return

    # --- Run all passes (parallel, all cores) ---
    all_top = []
    t_total = time.time()

    print(f"\n{'='*80}")
    print(f"RUNNING PASSES ({args.workers} workers per pass)")
    print(f"{'='*80}")

    # Pass 1: single-stage baseline
    r = run_pass_parallel("P1:single", gen_single_stage(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    # Pass 2: early trim + full exit
    r = run_pass_parallel("P2:early-trim", gen_early_trim(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    # Pass 3: MFE-gated
    r = run_pass_parallel("P3:mfe-gated", gen_mfe_gated(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    # Pass 4: protection + trail
    r = run_pass_parallel("P4:protect+trail", gen_protection_trail(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    # Pass 5: bar-gated
    r = run_pass_parallel("P5:bar-gated", gen_bar_gated(conditions), all_matrices, metas, args.workers)
    all_top.extend(r)

    # Pass 6: cross-condition
    best_conds_set = set()
    for stages, *_ in all_top[:100]:
        for stg in stages:
            best_conds_set.add((s_expr(stg), s_dir(stg), s_thresh(stg)))
    best_conds = list(best_conds_set)

    if len(best_conds) >= 2:
        r = run_pass_parallel("P6:cross-cond", gen_cross_condition(best_conds), all_matrices, metas, args.workers)
        all_top.extend(r)

    # Pass 7: 3-stage cross
    if len(best_conds) >= 3:
        r = run_pass_parallel("P7:3stg-cross", gen_3stage_cross(best_conds), all_matrices, metas, args.workers)
        all_top.extend(r)

    # Pass 8: refinement
    refine_configs = gen_refinement(all_top)
    if refine_configs:
        r = run_pass_parallel("P8:refine", refine_configs, all_matrices, metas, args.workers)
        all_top.extend(r)

    # --- Final ranking: floor primary, median secondary ---
    all_top.sort(key=lambda r: (r[3], r[1]), reverse=True)

    # Dedup
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

    # Rebuild full results for reporting
    full_results = rebuild_full_results(deduped, all_matrices, metas)
    print_results(full_results)

    if full_results:
        save_results(full_results, examples, args.setup)
        best = full_results[0]
        print(f"\n{'='*80}")
        print(f"BEST: floor={best['floor_capture_eff']:.1%} "
              f"median={best['median_capture_eff']:.1%} "
              f"({best['median_effective_pct']:+.2f}% median move)")
        print(f"ALL conditions fired on ALL examples. No backstops.")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
