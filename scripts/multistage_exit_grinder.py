"""
Multi-Stage Exit Grinder — Task 3.7

Grinds for optimal multi-stage conditional exits with position sizing.
Separate from single-stage exit_grinder.py — both systems coexist.

How it works:
    1. Builds expression matrices (same as single-stage grinder)
    2. Finds all conditions that trigger on 100% of examples (one grind)
    3. Runs multiple passes testing every structure variant:
       - Pass 1: single-stage (baseline, same as exit_grinder)
       - Pass 2: 2-stage early trim + full exit
       - Pass 3: 2-stage MFE-gated partial + full exit
       - Pass 4: 3-stage protection + partial + trailing
       - Pass 5: re-grind tighter around best results from passes 1-4
    4. Ranks ALL results across all passes, reports the best

Each pass is cheap — just simulation over pre-built matrices.
Expression matrix build is the only expensive step (done once).

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
from typing import Optional, List, Tuple
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
# Data Classes
# ============================================================

@dataclass
class ExampleData:
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


@dataclass
class StageCondition:
    expr_name: str
    direction: str
    threshold: float


@dataclass
class StageConfig:
    stage_id: int
    condition: StageCondition
    trim_pct: float
    gate_type: str           # "none", "mfe_adr", "bars_min"
    gate_value: float
    max_window: Optional[int]


@dataclass
class StageExitEvent:
    stage_id: int
    bar: int
    price: float
    pct_trimmed: float
    pct_move: float
    remaining_after: float


@dataclass
class MultiStageResult:
    label: str               # which pass produced this
    stages: List[StageConfig]
    per_example: List[dict]
    median_capture_eff: float
    avg_capture_eff: float
    floor_capture_eff: float
    median_effective_pct: float
    avg_bars_to_full_exit: float


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
# Expression Matrix Building
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

def simulate_example(stages, matrix, example):
    """Walk forward bar-by-bar, apply stages in order with position tracking."""
    n_fwd = example.n_forward
    direction = example.direction
    entry_high, entry_low = example.entry_high, example.entry_low
    adr = example.adr_at_entry
    fwd_close, fwd_low, fwd_high = example.fwd_close, example.fwd_low, example.fwd_high

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

        for stage in stages:
            if stage.stage_id in fired or remaining <= 0.001:
                continue
            if stage.max_window is not None and bar > stage.max_window:
                continue
            if stage.gate_type == "mfe_adr" and mfe_now < stage.gate_value:
                continue
            if stage.gate_type == "bars_min" and bar < stage.gate_value:
                continue

            series = matrix.get(stage.condition.expr_name)
            if series is None or bar >= len(series):
                continue
            val = series[bar]
            if np.isnan(val):
                continue

            hit = (val > stage.condition.threshold if stage.condition.direction == "above"
                   else val < stage.condition.threshold)
            if not hit:
                continue

            exit_price = fwd_close[bar]
            if direction == "short":
                pct_move = (entry_high - exit_price) / entry_high * 100
            else:
                pct_move = (exit_price - entry_low) / entry_low * 100

            trimmed = remaining * min(stage.trim_pct, 1.0)
            remaining -= trimmed
            events.append(StageExitEvent(
                stage.stage_id, bar, exit_price, trimmed, pct_move, remaining))
            fired.add(stage.stage_id)
            break

    # Backstop
    if remaining > 0.001:
        last_bar = min(n_fwd, len(fwd_close) - 1)
        exit_price = fwd_close[last_bar]
        pct_move = ((entry_high - exit_price) / entry_high * 100 if direction == "short"
                    else (exit_price - entry_low) / entry_low * 100)
        events.append(StageExitEvent(99, last_bar, exit_price, remaining, pct_move, 0.0))

    effective_pct = sum(e.pct_trimmed * e.pct_move for e in events)
    capture_eff = effective_pct / example.mfe_pct if example.mfe_pct > 0 else 0.0
    return events, effective_pct, capture_eff


def simulate_all(stages, matrices, examples):
    """Run simulation across all examples."""
    per_example = []
    cap_effs, eff_pcts, bars_list = [], [], []

    for i, ex in enumerate(examples):
        if matrices[i] is None:
            continue
        events, eff_pct, cap_eff = simulate_example(stages, matrices[i], ex)
        last_bar = max(e.bar for e in events) if events else 0
        per_example.append({
            "ticker": ex.ticker, "entry_date": ex.entry_date,
            "mfe_pct": ex.mfe_pct, "mfe_adr": ex.mfe_adr,
            "effective_pct": eff_pct, "capture_eff": cap_eff,
            "events": [{"stage_id": e.stage_id, "bar": e.bar, "price": e.price,
                        "pct_trimmed": e.pct_trimmed, "pct_move": e.pct_move}
                       for e in events],
            "bars_to_full_exit": last_bar,
        })
        cap_effs.append(cap_eff)
        eff_pcts.append(eff_pct)
        bars_list.append(last_bar)

    if not cap_effs:
        return None
    return MultiStageResult(
        label="", stages=stages, per_example=per_example,
        median_capture_eff=float(np.median(cap_effs)),
        avg_capture_eff=float(np.mean(cap_effs)),
        floor_capture_eff=float(np.min(cap_effs)),
        median_effective_pct=float(np.median(eff_pcts)),
        avg_bars_to_full_exit=float(np.mean(bars_list)),
    )


# ============================================================
# Condition Discovery (one grind for all passes)
# ============================================================

def find_all_valid_conditions(examples, matrices, expr_names, n_thresholds):
    """Find all (expr, direction, threshold) combos that trigger on 100% of examples.
    
    This is the expensive step — done ONCE, then reused by all passes.
    Returns list of StageCondition objects.
    """
    n_examples = len(examples)
    valid = []

    print(f"\n  Finding valid conditions across {len(expr_names)} expressions...")
    t0 = time.time()

    for expr_i, expr_name in enumerate(expr_names):
        if (expr_i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"    [{expr_i+1}/{len(expr_names)}] {rate:.0f}/s, {len(valid)} valid")

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
                    valid.append(StageCondition(expr_name, dir_test, thresh))

    elapsed = time.time() - t0
    print(f"  Found {len(valid):,} valid conditions in {elapsed:.1f}s")
    return valid


# ============================================================
# Pass Functions — each tests a different structural variant
# ============================================================

def run_pass(label, stage_builders, conditions, matrices, examples, top_n=50):
    """Generic pass runner.
    
    stage_builders: list of functions, each takes a StageCondition and returns
                    list of list[StageConfig] (one or more stage combos to test)
    
    For each condition, calls each builder to get stage configs, simulates, collects.
    """
    print(f"\n  {label} ({len(conditions)} conditions)...")
    t0 = time.time()
    results = []
    tested = 0

    for cond in conditions:
        for builder in stage_builders:
            for stages in builder(cond):
                tested += 1
                result = simulate_all(stages, matrices, examples)
                if result:
                    result.label = label
                    results.append(result)

    elapsed = time.time() - t0
    results.sort(key=lambda r: r.median_capture_eff, reverse=True)
    best_eff = results[0].median_capture_eff if results else 0
    print(f"    {tested:,} configs tested in {elapsed:.1f}s → "
          f"{len(results)} valid, best={best_eff:.3f}")
    return results[:top_n]


# --- Pass 1: Single stage (baseline) ---
def _single_stage_builder(cond):
    """Same as current exit_grinder: one condition exits 100%."""
    configs = []
    for trim in [1.0]:  # single stage always exits 100%
        configs.append([StageConfig(1, cond, trim, "none", 0.0, None)])
    return configs


# --- Pass 2: Early trim + full exit (same condition for both) ---
def _early_trim_builder(cond):
    """Stage 1: early partial trim (max_window). Stage 2: same condition exits rest."""
    configs = []
    for max_win in MAX_WINDOWS:
        for trim in [0.25, 0.33, 0.50]:
            configs.append([
                StageConfig(1, cond, trim, "none", 0.0, max_win),
                StageConfig(2, cond, 1.0, "none", 0.0, None),  # exit rest anytime
            ])
    return configs


# --- Pass 3: MFE-gated partial + full exit ---
def _mfe_gated_builder(cond):
    """Stage 1: MFE-gated partial. Stage 2: same condition exits rest later."""
    configs = []
    for gate in MFE_GATES:
        for trim in TRIM_PCTS:
            if trim >= 1.0:
                # Single stage with MFE gate
                configs.append([
                    StageConfig(1, cond, 1.0, "mfe_adr", gate, None),
                ])
            else:
                configs.append([
                    StageConfig(1, cond, trim, "mfe_adr", gate, None),
                    StageConfig(2, cond, 1.0, "none", 0.0, None),
                ])
    return configs


# --- Pass 4: 2-stage with different gates ---
def _protection_trail_builder(cond):
    """Stage 1: early trim with max_window. Stage 2: MFE-gated full exit."""
    configs = []
    for max_win in [10, 15, 20]:
        for trim in [0.25, 0.33, 0.50]:
            for gate in [1.5, 2.0, 3.0]:
                configs.append([
                    StageConfig(1, cond, trim, "none", 0.0, max_win),
                    StageConfig(2, cond, 1.0, "mfe_adr", gate, None),
                ])
    return configs


# --- Pass 5: bar-gated trailing ---
def _bar_gated_builder(cond):
    """Stage 1: exit rest after minimum bars elapsed."""
    configs = []
    for bar_min in BAR_GATES:
        configs.append([
            StageConfig(1, cond, 1.0, "bars_min", float(bar_min), None),
        ])
    return configs


# ============================================================
# Cross-condition combination pass
# ============================================================

def run_cross_condition_pass(label, all_results, conditions, matrices, examples, top_n=50):
    """Take best conditions from prior passes, try mixing different conditions per stage.
    
    This is the combinatorial pass: best S1 condition × best S2 condition.
    """
    # Collect unique best conditions from prior results
    seen_conds = {}
    for r in all_results:
        for s in r.stages:
            if s.stage_id == 99:
                continue
            key = (s.condition.expr_name, s.condition.direction, s.condition.threshold)
            if key not in seen_conds:
                seen_conds[key] = s.condition
    
    best_conds = list(seen_conds.values())
    if len(best_conds) < 2:
        print(f"\n  {label}: need 2+ unique conditions, have {len(best_conds)}. Skipping.")
        return []

    # Cap to top conditions to keep combinatorial manageable
    best_conds = best_conds[:30]
    n = len(best_conds)
    
    print(f"\n  {label} ({n} unique conditions, testing cross-combos)...")
    t0 = time.time()
    results = []
    tested = 0

    for i, cond_a in enumerate(best_conds):
        for j, cond_b in enumerate(best_conds):
            if i == j:
                continue

            # 2-stage: different condition per stage
            for max_win in [10, 15, 20]:
                for trim in [0.25, 0.33, 0.50]:
                    tested += 1
                    stages = [
                        StageConfig(1, cond_a, trim, "none", 0.0, max_win),
                        StageConfig(2, cond_b, 1.0, "none", 0.0, None),
                    ]
                    result = simulate_all(stages, matrices, examples)
                    if result:
                        result.label = label
                        results.append(result)

            # MFE-gated cross
            for gate in [1.5, 2.0, 3.0]:
                for trim in [0.33, 0.50]:
                    tested += 1
                    stages = [
                        StageConfig(1, cond_a, trim, "none", 0.0, 15),
                        StageConfig(2, cond_b, 1.0, "mfe_adr", gate, None),
                    ]
                    result = simulate_all(stages, matrices, examples)
                    if result:
                        result.label = label
                        results.append(result)

    elapsed = time.time() - t0
    results.sort(key=lambda r: r.median_capture_eff, reverse=True)
    best_eff = results[0].median_capture_eff if results else 0
    print(f"    {tested:,} cross-combos in {elapsed:.1f}s → "
          f"{len(results)} valid, best={best_eff:.3f}")
    return results[:top_n]


# ============================================================
# 3-stage cross-condition pass
# ============================================================

def run_3stage_cross_pass(label, all_results, matrices, examples, top_n=50):
    """3-stage with potentially different conditions per stage."""
    seen_conds = {}
    for r in all_results:
        for s in r.stages:
            if s.stage_id == 99:
                continue
            key = (s.condition.expr_name, s.condition.direction, s.condition.threshold)
            if key not in seen_conds:
                seen_conds[key] = s.condition

    best_conds = list(seen_conds.values())[:20]
    if len(best_conds) < 3:
        print(f"\n  {label}: need 3+ conditions, have {len(best_conds)}. Skipping.")
        return []

    n = len(best_conds)
    print(f"\n  {label} ({n} conditions, 3-stage cross-combos)...")
    t0 = time.time()
    results = []
    tested = 0

    # Sample: don't test all n³, test top conditions with variety
    for i, c1 in enumerate(best_conds[:10]):
        for j, c2 in enumerate(best_conds[:10]):
            for k, c3 in enumerate(best_conds[:10]):
                if i == j == k:
                    continue  # at least one different

                for trim1 in [0.25, 0.33]:
                    for trim2 in [0.33, 0.50]:
                        tested += 1
                        stages = [
                            StageConfig(1, c1, trim1, "none", 0.0, 15),
                            StageConfig(2, c2, trim2, "mfe_adr", 1.5, None),
                            StageConfig(3, c3, 1.0, "mfe_adr", 3.0, None),
                        ]
                        result = simulate_all(stages, matrices, examples)
                        if result:
                            result.label = label
                            results.append(result)

    elapsed = time.time() - t0
    results.sort(key=lambda r: r.median_capture_eff, reverse=True)
    best_eff = results[0].median_capture_eff if results else 0
    print(f"    {tested:,} 3-stage combos in {elapsed:.1f}s → "
          f"{len(results)} valid, best={best_eff:.3f}")
    return results[:top_n]


# ============================================================
# Refinement pass
# ============================================================

def run_refinement_pass(label, best_results, matrices, examples, n_thresholds=40, top_n=50):
    """Re-grind around the best results with finer thresholds and more parameter combos."""
    if not best_results:
        return []

    # Collect the conditions from top results
    refine_conds = set()
    for r in best_results[:20]:
        for s in r.stages:
            if s.stage_id == 99:
                continue
            refine_conds.add((s.condition.expr_name, s.condition.direction, s.condition.threshold))

    if not refine_conds:
        return []

    print(f"\n  {label} (refining {len(refine_conds)} conditions with finer params)...")
    t0 = time.time()
    results = []
    tested = 0

    for expr_name, direction, base_thresh in refine_conds:
        # Generate finer thresholds around the winning value
        offsets = np.linspace(-0.15, 0.15, 15) * abs(base_thresh) if base_thresh != 0 \
            else np.linspace(-0.1, 0.1, 15)
        fine_thresholds = [round(base_thresh + o, 6) for o in offsets]

        for thresh in fine_thresholds:
            cond = StageCondition(expr_name, direction, thresh)

            # Test all structural variants with this refined condition
            for builder in [_single_stage_builder, _early_trim_builder,
                            _mfe_gated_builder, _protection_trail_builder,
                            _bar_gated_builder]:
                for stages in builder(cond):
                    tested += 1
                    result = simulate_all(stages, matrices, examples)
                    if result:
                        result.label = label
                        results.append(result)

    elapsed = time.time() - t0
    results.sort(key=lambda r: r.median_capture_eff, reverse=True)
    best_eff = results[0].median_capture_eff if results else 0
    print(f"    {tested:,} refined configs in {elapsed:.1f}s → "
          f"{len(results)} valid, best={best_eff:.3f}")
    return results[:top_n]


# ============================================================
# Reporting & Saving
# ============================================================

def print_results(results, top_n=20):
    if not results:
        print("\nNo valid configurations found.")
        return

    print(f"\n{'='*120}")
    print(f"TOP {min(top_n, len(results))} EXIT CONFIGURATIONS (all passes)")
    print(f"{'='*120}")

    for rank, r in enumerate(results[:top_n], 1):
        stage_ids = [s.stage_id for s in r.stages]
        print(f"\n{'─'*120}")
        print(f"#{rank}  [{r.label}] [{len(r.stages)}-stage: {stage_ids}]  "
              f"median_eff={r.median_capture_eff:.3f}  "
              f"floor={r.floor_capture_eff:.3f}  "
              f"median_pct={r.median_effective_pct:+.2f}%  "
              f"avg_bars={r.avg_bars_to_full_exit:.1f}")

        for s in r.stages:
            parts = [f"S{s.stage_id}: {s.condition.expr_name} "
                     f"{s.condition.direction} {s.condition.threshold:.4f} "
                     f"trim={s.trim_pct:.0%}"]
            if s.gate_type == "mfe_adr":
                parts.append(f"gate:MFE≥{s.gate_value:.1f}ADR")
            elif s.gate_type == "bars_min":
                parts.append(f"gate:bars≥{int(s.gate_value)}")
            if s.max_window:
                parts.append(f"maxwin={s.max_window}")
            print(f"      {'  '.join(parts)}")

        print(f"      {'Ticker':8s} {'Entry':12s} {'MFE%':>7s} {'Eff%':>7s} "
              f"{'CapEff':>7s}  Events")
        for pe in r.per_example:
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
            "rank": rank + 1, "label": r.label,
            "n_stages": len(r.stages),
            "median_capture_eff": r.median_capture_eff,
            "avg_capture_eff": r.avg_capture_eff,
            "floor_capture_eff": r.floor_capture_eff,
            "median_effective_pct": r.median_effective_pct,
            "avg_bars_to_full_exit": r.avg_bars_to_full_exit,
            "stages": [{"stage_id": s.stage_id,
                         "expr_name": s.condition.expr_name,
                         "direction": s.condition.direction,
                         "threshold": s.condition.threshold,
                         "trim_pct": s.trim_pct,
                         "gate_type": s.gate_type,
                         "gate_value": s.gate_value,
                         "max_window": s.max_window} for s in r.stages],
            "per_example": r.per_example,
        })

    nan_fix = lambda x: None if isinstance(x, float) and np.isnan(x) else x
    n_stg = len(best.stages) if best else 0
    eff = f"{best.median_capture_eff:.3f}" if best else "na"

    ts_path = f"data/multistage_exit/ms_exit_{setup_type}_{n_stg}stg_{eff}eff_{ts}.json"
    with open(ts_path, "w") as f:
        json.dump(data, f, indent=2, default=nan_fix)
    latest = f"data/multistage_exit/ms_exit_{setup_type}.json"
    with open(latest, "w") as f:
        json.dump(data, f, indent=2, default=nan_fix)
    print(f"\n  Saved: {ts_path}")
    print(f"  Saved: {latest}")


# ============================================================
# Main — single command, multiple passes, best result wins
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

    # --- Load data ---
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

    # --- Build expression matrices (expensive, done once) ---
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

    # --- Find all valid conditions (done once) ---
    conditions = find_all_valid_conditions(examples, all_matrices, all_expr_names, args.n_thresholds)

    if not conditions:
        print("No valid conditions found. Aborting.")
        return

    # --- Run all passes ---
    all_results = []
    t_total = time.time()

    print(f"\n{'='*80}")
    print(f"RUNNING PASSES")
    print(f"{'='*80}")

    # Pass 1: single-stage baseline
    r1 = run_pass("P1:single", [_single_stage_builder], conditions, all_matrices, examples)
    all_results.extend(r1)

    # Pass 2: early trim + full exit
    r2 = run_pass("P2:early-trim", [_early_trim_builder], conditions, all_matrices, examples)
    all_results.extend(r2)

    # Pass 3: MFE-gated
    r3 = run_pass("P3:mfe-gated", [_mfe_gated_builder], conditions, all_matrices, examples)
    all_results.extend(r3)

    # Pass 4: protection + trail
    r4 = run_pass("P4:protect+trail", [_protection_trail_builder], conditions, all_matrices, examples)
    all_results.extend(r4)

    # Pass 5: bar-gated
    r5 = run_pass("P5:bar-gated", [_bar_gated_builder], conditions, all_matrices, examples)
    all_results.extend(r5)

    # Pass 6: cross-condition (different exprs per stage)
    r6 = run_cross_condition_pass("P6:cross-cond", all_results, conditions, all_matrices, examples)
    all_results.extend(r6)

    # Pass 7: 3-stage cross
    r7 = run_3stage_cross_pass("P7:3stg-cross", all_results, all_matrices, examples)
    all_results.extend(r7)

    # Pass 8: refinement around best
    all_results.sort(key=lambda r: r.median_capture_eff, reverse=True)
    r8 = run_refinement_pass("P8:refine", all_results[:30], all_matrices, examples)
    all_results.extend(r8)

    # --- Final ranking ---
    all_results.sort(key=lambda r: (r.median_capture_eff, r.floor_capture_eff), reverse=True)

    # Deduplicate (same stages = same result)
    seen = set()
    deduped = []
    for r in all_results:
        key = tuple((s.condition.expr_name, s.condition.direction, s.condition.threshold,
                      s.trim_pct, s.gate_type, s.gate_value, s.max_window)
                     for s in r.stages)
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    total_time = time.time() - t_total
    print(f"\n{'='*80}")
    print(f"ALL PASSES COMPLETE: {total_time:.1f}s total, "
          f"{len(deduped)} unique configs from {len(all_results)} total")
    print(f"{'='*80}")

    print_results(deduped)

    if deduped:
        save_results(deduped, examples, args.setup)
        best = deduped[0]
        print(f"\n{'='*80}")
        print(f"BEST: {best.median_capture_eff:.1%} median capture efficiency "
              f"({best.median_effective_pct:+.2f}% median move) [{best.label}]")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
