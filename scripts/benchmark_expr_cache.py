"""
Benchmark v3: Targeted Optimization Test

Tests specific optimizations against the original path:
1. numpy since_true / true_in_row (replacing pandas rolling().apply())  
2. Vectorized trendline_deviation / channel_position (replacing per-bar Python loops)
3. HTF: use vectorized_indicators 2D functions on resampled arrays

Usage:
    python scripts/benchmark_expr_cache.py
"""

import os
import sys
import time
import pickle
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "local_runner")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from expr_cache_builder import (
    _load_expressions, _init_worker, resample_ohlcv,
    build_htf_to_daily_map, map_htf_series_to_daily,
)
from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series

BOOL_OPS = {"count_true", "since_true", "true_in_row"}


# ══════════════════════════════════════════════════════════════
# OPTIMIZED NUMPY REPLACEMENTS
# ══════════════════════════════════════════════════════════════

def np_count_true(bool_arr, period):
    """count_true: rolling sum of boolean array. Pure numpy."""
    n = len(bool_arr)
    f = bool_arr.astype(np.float64)
    result = np.full(n, np.nan)
    cs = np.cumsum(f)
    result[period - 1] = cs[period - 1]
    result[period:] = cs[period:] - cs[:-period]
    return result


def np_since_true(bool_arr, period):
    """since_true: bars since last True within window. Pure numpy.
    Returns float array. -1 if never true in window."""
    n = len(bool_arr)
    result = np.full(n, np.nan)
    
    # Build "bars since last True" running counter
    bars_since = np.full(n, n, dtype=np.float64)  # large default = never seen
    for i in range(n):
        if bool_arr[i]:
            bars_since[i] = 0.0
        elif i > 0:
            bars_since[i] = bars_since[i - 1] + 1.0
    
    # Apply window constraint
    for i in range(period - 1, n):
        bs = bars_since[i]
        if bs < period:
            result[i] = bs
        else:
            result[i] = -1.0
    
    return result


def np_true_in_row(bool_arr, max_look):
    """true_in_row: consecutive True count from current bar back. Pure numpy."""
    n = len(bool_arr)
    result = np.zeros(n, dtype=np.float64)
    
    for i in range(n):
        if bool_arr[i]:
            count = 1
            limit = min(max_look, i + 1)
            for j in range(1, limit):
                if bool_arr[i - j]:
                    count += 1
                else:
                    break
            result[i] = float(count)
    
    return result


def np_trendline_deviation(series, lookback):
    """Vectorized trendline deviation using numpy stride tricks."""
    n = len(series)
    result = np.full(n, np.nan)
    
    if n < lookback:
        return result
    
    # Precompute x constants (same for all windows)
    x = np.arange(lookback, dtype=np.float64)
    x_mean = np.mean(x)
    x_var = np.mean(x * x) - x_mean ** 2 + 1e-10
    
    # Create sliding windows using stride tricks
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(series, lookback)  # shape: (n - lookback + 1, lookback)
    
    # Check for NaN windows
    has_nan = np.any(np.isnan(windows), axis=1)
    
    # Vectorized linear regression across ALL windows at once
    w_mean = np.mean(windows, axis=1)  # (n_windows,)
    xw_mean = np.mean(x[np.newaxis, :] * windows, axis=1)  # (n_windows,)
    slope = (xw_mean - x_mean * w_mean) / x_var
    intercept = w_mean - slope * x_mean
    projected = slope * (lookback - 1) + intercept
    
    # trendline deviation = actual - projected
    actual = windows[:, -1]
    dev = actual - projected
    
    # Apply NaN mask
    dev[has_nan] = np.nan
    
    # Place results at correct indices
    result[lookback - 1:] = dev
    
    return result


def np_channel_position(series, lookback):
    """Vectorized channel position using numpy stride tricks."""
    n = len(series)
    result = np.full(n, np.nan)
    
    if n < lookback:
        return result
    
    x = np.arange(lookback, dtype=np.float64)
    x_mean = np.mean(x)
    x_var = np.mean(x * x) - x_mean ** 2 + 1e-10
    
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(series, lookback)
    
    has_nan = np.any(np.isnan(windows), axis=1)
    
    w_mean = np.mean(windows, axis=1)
    xw_mean = np.mean(x[np.newaxis, :] * windows, axis=1)
    slope = (xw_mean - x_mean * w_mean) / x_var
    intercept = w_mean - slope * x_mean
    
    # Compute residual std for each window
    projected_lines = slope[:, np.newaxis] * x[np.newaxis, :] + intercept[:, np.newaxis]
    residuals = windows - projected_lines
    std_resid = np.std(residuals, axis=1)
    
    # Channel position = (actual - projected) / std_resid
    actual = windows[:, -1]
    projected = slope * (lookback - 1) + intercept
    
    with np.errstate(divide='ignore', invalid='ignore'):
        pos = np.where(std_resid > 0, (actual - projected) / std_resid, np.nan)
    
    pos[has_nan] = np.nan
    result[lookback - 1:] = pos
    
    return result


# ══════════════════════════════════════════════════════════════
# BENCHMARK: ORIGINAL PATH
# ══════════════════════════════════════════════════════════════

def benchmark_original(ticker, df, expressions):
    """Run current path, with sub-phase timing for booleans and ext_struct."""
    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_ext_series_name_to_idx,
        _w_lsp_indices, _w_algo_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
        _w_htf_weekly_base, _w_htf_monthly_base,
    )

    n_bars = len(df)
    n_exprs = len(expressions)
    results = {}

    engine = ExpressionEngine(df)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

    # Daily arithmetic
    arith_indices = [j for j in _w_daily_indices
                     if expressions[j]["compute"].get("op") not in BOOL_OPS]
    t0 = time.perf_counter()
    for j in arith_indices:
        try:
            s = compute_series(engine, expressions[j]["compute"])
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
        except:
            pass
    results["1_daily_arith"] = time.perf_counter() - t0

    # Daily booleans — split by op type
    ct_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "count_true"]
    st_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "since_true"]
    tir_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "true_in_row"]

    for label, indices in [("2a_count_true", ct_indices), ("2b_since_true", st_indices), ("2c_true_in_row", tir_indices)]:
        t0 = time.perf_counter()
        for j in indices:
            try:
                s = compute_series(engine, expressions[j]["compute"])
                if s is not None:
                    arr = np.asarray(s, dtype=np.float32)
                    if len(arr) == n_bars:
                        data[:, j] = arr
            except:
                pass
        results[label] = time.perf_counter() - t0

    # LSP + Algo
    t0 = time.perf_counter()
    if _w_lsp_indices:
        try:
            from scripts.lsp_detector_v2 import compute_all_lsp_series
            lsp_dict = compute_all_lsp_series(df)
            for j in _w_lsp_indices:
                col = expressions[j]["compute"]["column"]
                if col in lsp_dict and len(lsp_dict[col]) == n_bars:
                    data[:, j] = lsp_dict[col].astype(np.float32)
        except:
            pass
    if _w_algo_indices:
        try:
            from scripts.algo_line_detector import compute_all_algo_series
            algo_dict = compute_all_algo_series(df)
            for j in _w_algo_indices:
                col = expressions[j]["compute"]["column"]
                if col in algo_dict and len(algo_dict[col]) == n_bars:
                    data[:, j] = algo_dict[col].astype(np.float32)
        except:
            pass
    results["3_lsp_algo"] = time.perf_counter() - t0

    # HTF
    for freq, label, htf_indices, htf_base in [
        ("W", "4a_htf_weekly", _w_htf_weekly_indices, _w_htf_weekly_base),
        ("ME", "4b_htf_monthly", _w_htf_monthly_indices, _w_htf_monthly_base),
    ]:
        t0 = time.perf_counter()
        if htf_indices:
            htf_df = resample_ohlcv(df, freq)
            if htf_df is not None and len(htf_df) >= 5:
                htf_map = build_htf_to_daily_map(df["date"], htf_df, freq)
                htf_engine = ExpressionEngine(htf_df)
                for k, j in enumerate(htf_indices):
                    try:
                        s = compute_series(htf_engine, htf_base[k])
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(
                                np.asarray(s, dtype=np.float32), htf_map)
                    except:
                        pass
        results[label] = time.perf_counter() - t0

    # Extension structure — split trendline/channel from the rest
    t0_ext_fast = time.perf_counter()
    t_ext_linreg = 0.0
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        series_registry = {}
        for sname, sidx in _w_ext_series_name_to_idx.items():
            col_data = data[:, sidx]
            if not np.all(np.isnan(col_data)):
                series_registry[sname] = col_data.astype(np.float64)
        if series_registry:
            LINREG_OPS = {"trendline_deviation", "channel_position"}
            for j in _w_ext_struct_indices:
                comp = expressions[j]["compute"]
                inner_op = comp.get("inner_op", {}).get("op", "")
                is_linreg = inner_op in LINREG_OPS
                t_before = time.perf_counter()
                try:
                    s = compute_series(engine, comp, series_registry=series_registry)
                    if s is not None:
                        arr = np.asarray(s, dtype=np.float32)
                        if len(arr) == n_bars:
                            data[:, j] = arr
                except:
                    pass
                if is_linreg:
                    t_ext_linreg += time.perf_counter() - t_before

    total_ext = time.perf_counter() - t0_ext_fast
    results["5a_ext_linreg"] = t_ext_linreg
    results["5b_ext_other"] = total_ext - t_ext_linreg

    total = sum(results.values())
    return data, results, total


# ══════════════════════════════════════════════════════════════
# BENCHMARK: OPTIMIZED PATH
# ══════════════════════════════════════════════════════════════

def benchmark_optimized(ticker, df, expressions):
    """Run optimized path: same engine + compute_series for arithmetic,
    but numpy replacements for booleans and ext_struct linreg."""
    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_ext_series_name_to_idx,
        _w_lsp_indices, _w_algo_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
        _w_htf_weekly_base, _w_htf_monthly_base,
    )

    n_bars = len(df)
    n_exprs = len(expressions)
    results = {}

    engine = ExpressionEngine(df)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

    # Daily arithmetic — SAME AS ORIGINAL (no change)
    arith_indices = [j for j in _w_daily_indices
                     if expressions[j]["compute"].get("op") not in BOOL_OPS]
    t0 = time.perf_counter()
    for j in arith_indices:
        try:
            s = compute_series(engine, expressions[j]["compute"])
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
        except:
            pass
    results["1_daily_arith"] = time.perf_counter() - t0

    # Daily booleans — OPTIMIZED: compute bool conditions via engine (cached),
    # then use numpy count/since/true_in_row instead of pandas rolling
    ct_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "count_true"]
    st_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "since_true"]
    tir_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "true_in_row"]

    # Pre-compute all unique boolean conditions via the engine (uses its cache)
    t0 = time.perf_counter()
    bool_cache = {}
    all_bool_indices = ct_indices + st_indices + tir_indices
    for j in all_bool_indices:
        cond = expressions[j]["compute"]["condition"]
        if cond not in bool_cache:
            try:
                b = engine._bool_series(cond)
                bool_cache[cond] = b.values.astype(bool)
            except:
                bool_cache[cond] = np.zeros(n_bars, dtype=bool)
    results["2a_bool_conditions"] = time.perf_counter() - t0

    # count_true — numpy cumsum
    t0 = time.perf_counter()
    for j in ct_indices:
        comp = expressions[j]["compute"]
        cond = comp["condition"]
        period = comp["period"]
        b = bool_cache[cond]
        data[:, j] = np_count_true(b, period).astype(np.float32)
    results["2b_count_true"] = time.perf_counter() - t0

    # since_true — numpy loop (still a loop but no pandas overhead)
    t0 = time.perf_counter()
    # Pre-compute "bars_since" for each unique condition (reusable across periods)
    bars_since_cache = {}
    for j in st_indices:
        cond = expressions[j]["compute"]["condition"]
        if cond not in bars_since_cache:
            b = bool_cache[cond]
            bs = np.full(n_bars, n_bars, dtype=np.float64)
            for i in range(n_bars):
                if b[i]:
                    bs[i] = 0.0
                elif i > 0:
                    bs[i] = bs[i - 1] + 1.0
            bars_since_cache[cond] = bs
    
    for j in st_indices:
        comp = expressions[j]["compute"]
        cond = comp["condition"]
        period = comp["period"]
        bs = bars_since_cache[cond]
        result = np.full(n_bars, np.nan)
        for i in range(period - 1, n_bars):
            if bs[i] < period:
                result[i] = bs[i]
            else:
                result[i] = -1.0
        data[:, j] = result.astype(np.float32)
    results["2c_since_true"] = time.perf_counter() - t0

    # true_in_row — numpy loop
    t0 = time.perf_counter()
    for j in tir_indices:
        comp = expressions[j]["compute"]
        cond = comp["condition"]
        period = comp["period"]
        b = bool_cache[cond]
        data[:, j] = np_true_in_row(b, period).astype(np.float32)
    results["2d_true_in_row"] = time.perf_counter() - t0

    # LSP + Algo — SAME AS ORIGINAL
    t0 = time.perf_counter()
    if _w_lsp_indices:
        try:
            from scripts.lsp_detector_v2 import compute_all_lsp_series
            lsp_dict = compute_all_lsp_series(df)
            for j in _w_lsp_indices:
                col = expressions[j]["compute"]["column"]
                if col in lsp_dict and len(lsp_dict[col]) == n_bars:
                    data[:, j] = lsp_dict[col].astype(np.float32)
        except:
            pass
    if _w_algo_indices:
        try:
            from scripts.algo_line_detector import compute_all_algo_series
            algo_dict = compute_all_algo_series(df)
            for j in _w_algo_indices:
                col = expressions[j]["compute"]["column"]
                if col in algo_dict and len(algo_dict[col]) == n_bars:
                    data[:, j] = algo_dict[col].astype(np.float32)
        except:
            pass
    results["3_lsp_algo"] = time.perf_counter() - t0

    # HTF — SAME AS ORIGINAL (optimize in next increment)
    for freq, label, htf_indices, htf_base in [
        ("W", "4a_htf_weekly", _w_htf_weekly_indices, _w_htf_weekly_base),
        ("ME", "4b_htf_monthly", _w_htf_monthly_indices, _w_htf_monthly_base),
    ]:
        t0 = time.perf_counter()
        if htf_indices:
            htf_df = resample_ohlcv(df, freq)
            if htf_df is not None and len(htf_df) >= 5:
                htf_map = build_htf_to_daily_map(df["date"], htf_df, freq)
                htf_engine = ExpressionEngine(htf_df)
                for k, j in enumerate(htf_indices):
                    try:
                        s = compute_series(htf_engine, htf_base[k])
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(
                                np.asarray(s, dtype=np.float32), htf_map)
                    except:
                        pass
        results[label] = time.perf_counter() - t0

    # Extension structure — OPTIMIZED: numpy linreg for trendline/channel
    t0_ext = time.perf_counter()
    t_linreg = 0.0
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        series_registry = {}
        for sname, sidx in _w_ext_series_name_to_idx.items():
            col_data = data[:, sidx]
            if not np.all(np.isnan(col_data)):
                series_registry[sname] = col_data.astype(np.float64)
        
        if series_registry:
            LINREG_OPS = {"trendline_deviation", "channel_position"}
            for j in _w_ext_struct_indices:
                comp = expressions[j]["compute"]
                inner_op_spec = comp.get("inner_op", {})
                inner_op_name = inner_op_spec.get("op", "")
                
                if inner_op_name in LINREG_OPS:
                    # Use vectorized numpy version
                    t_before = time.perf_counter()
                    try:
                        # Get the extension series this operates on
                        series_name = comp.get("series", "")
                        if series_name in series_registry:
                            s = series_registry[series_name]
                            lb = inner_op_spec["lookback"]
                            if inner_op_name == "trendline_deviation":
                                arr = np_trendline_deviation(s, lb)
                            else:
                                arr = np_channel_position(s, lb)
                            data[:, j] = arr.astype(np.float32)
                    except:
                        pass
                    t_linreg += time.perf_counter() - t_before
                else:
                    # Use original compute_series for non-linreg ops
                    try:
                        s = compute_series(engine, comp, series_registry=series_registry)
                        if s is not None:
                            arr = np.asarray(s, dtype=np.float32)
                            if len(arr) == n_bars:
                                data[:, j] = arr
                    except:
                        pass

    total_ext = time.perf_counter() - t0_ext
    results["5a_ext_linreg"] = t_linreg
    results["5b_ext_other"] = total_ext - t_linreg

    total = sum(results.values())
    return data, results, total


# ══════════════════════════════════════════════════════════════
# COMPARISON
# ══════════════════════════════════════════════════════════════

def compare_outputs(data_orig, data_opt, expressions, indices):
    """Compare outputs, report mismatches."""
    n_match = 0
    n_mismatch = 0
    max_diff = 0.0
    worst = ""
    examples = []

    for j in indices:
        orig = data_orig[:, j]
        opt = data_opt[:, j]
        one_nan = np.isnan(orig) != np.isnan(opt)
        both_val = ~np.isnan(orig) & ~np.isnan(opt)
        nan_mm = one_nan.sum()
        val_mm = 0
        if both_val.any():
            d = np.abs(orig[both_val] - opt[both_val])
            mag = np.maximum(np.abs(orig[both_val]), np.abs(opt[both_val]))
            mag = np.maximum(mag, 1e-8)
            val_mm = (d / mag > 1e-4).sum()
            if d.max() > max_diff:
                max_diff = d.max()
                worst = expressions[j]["name"]
        if nan_mm + val_mm == 0:
            n_match += 1
        else:
            n_mismatch += 1
            if len(examples) < 10:
                examples.append(f"{expressions[j]['name']}: {nan_mm} NaN, {val_mm} val")

    return n_match, n_mismatch, max_diff, worst, examples


def main():
    print("\n" + "=" * 70)
    print("  EXPRESSION CACHE — TARGETED OPTIMIZATION BENCHMARK")
    print("=" * 70)

    expressions = _load_expressions()
    _init_worker(expressions)

    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
    )

    ct = sum(1 for j in _w_daily_indices if expressions[j]["compute"].get("op") == "count_true")
    st = sum(1 for j in _w_daily_indices if expressions[j]["compute"].get("op") == "since_true")
    tir = sum(1 for j in _w_daily_indices if expressions[j]["compute"].get("op") == "true_in_row")
    
    print(f"\n  {len(expressions)} expressions")
    print(f"  Booleans: {ct} count_true + {st} since_true + {tir} true_in_row = {ct+st+tir}")
    print(f"  Extension structure: {len(_w_ext_struct_indices)}")

    # Load OHLCV
    print("\n  Loading 5yr OHLCV cache...")
    cache_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    with open(cache_path, "rb") as f:
        universe = pickle.load(f)

    valid = {t: df for t, df in universe.items() if df is not None and len(df) >= 50}
    by_bars = sorted(valid.items(), key=lambda x: len(x[1]))
    ticker, df = by_bars[-1]
    del universe, valid, by_bars
    import gc; gc.collect()

    print(f"  Benchmark ticker: {ticker} ({len(df)} bars)")

    # ═══ ORIGINAL ═══
    print(f"\n  {'─' * 60}")
    print(f"  ORIGINAL")
    print(f"  {'─' * 60}")
    data_orig, res_orig, total_orig = benchmark_original(ticker, df, expressions)

    print(f"\n  Phase                     Time (s)    %")
    print(f"  {'─' * 50}")
    for phase, secs in res_orig.items():
        pct = secs / total_orig * 100
        phase_name = phase.split("_", 1)[1]
        print(f"  {phase_name:<25} {secs:>7.3f}   {pct:>5.1f}%")
    print(f"  {'─' * 50}")
    print(f"  {'TOTAL':<25} {total_orig:>7.3f}   100.0%")

    # ═══ OPTIMIZED ═══
    print(f"\n  {'─' * 60}")
    print(f"  OPTIMIZED (numpy bools + vectorized linreg)")
    print(f"  {'─' * 60}")
    data_opt, res_opt, total_opt = benchmark_optimized(ticker, df, expressions)

    print(f"\n  Phase                     Time (s)    %")
    print(f"  {'─' * 50}")
    for phase, secs in res_opt.items():
        pct = secs / total_opt * 100
        phase_name = phase.split("_", 1)[1]
        print(f"  {phase_name:<25} {secs:>7.3f}   {pct:>5.1f}%")
    print(f"  {'─' * 50}")
    print(f"  {'TOTAL':<25} {total_opt:>7.3f}   100.0%")

    # ═══ COMPARISON ═══
    print(f"\n  {'─' * 60}")
    print(f"  COMPARISON")
    print(f"  {'─' * 60}")
    speedup = total_orig / total_opt if total_opt > 0 else 0
    print(f"\n  Original:   {total_orig:.3f}s")
    print(f"  Optimized:  {total_opt:.3f}s")
    print(f"  Speedup:    {speedup:.2f}x")

    # Per-phase comparison
    phases = [
        ("Booleans (ct+st+tir)", 
         sum(res_orig.get(k, 0) for k in ["2a_count_true", "2b_since_true", "2c_true_in_row"]),
         sum(res_opt.get(k, 0) for k in ["2a_bool_conditions", "2b_count_true", "2c_since_true", "2d_true_in_row"])),
        ("Ext linreg",
         res_orig.get("5a_ext_linreg", 0),
         res_opt.get("5a_ext_linreg", 0)),
    ]
    for name, orig_t, opt_t in phases:
        sp = orig_t / opt_t if opt_t > 0 else 0
        print(f"\n  {name}:")
        print(f"    Original:  {orig_t:.3f}s")
        print(f"    Optimized: {opt_t:.3f}s")
        print(f"    Speedup:   {sp:.2f}x")

    # ═══ CORRECTNESS ═══
    print(f"\n  {'─' * 60}")
    print(f"  CORRECTNESS")
    print(f"  {'─' * 60}")

    # Check booleans
    bool_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") in BOOL_OPS]
    m, mm, md, w, ex = compare_outputs(data_orig, data_opt, expressions, bool_indices)
    print(f"\n  Booleans ({len(bool_indices)}):")
    print(f"    Match: {m}, Mismatch: {mm}, Max diff: {md:.6f} ({w})")
    for e in ex[:5]:
        print(f"      {e}")

    # Check ext struct
    m, mm, md, w, ex = compare_outputs(data_orig, data_opt, expressions, list(_w_ext_struct_indices))
    print(f"\n  ExtStruct ({len(_w_ext_struct_indices)}):")
    print(f"    Match: {m}, Mismatch: {mm}, Max diff: {md:.6f} ({w})")
    for e in ex[:5]:
        print(f"      {e}")

    # ═══ FULL BUILD ESTIMATE ═══
    print(f"\n  {'=' * 60}")
    print(f"  FULL BUILD ESTIMATE (8 workers, {4100} tickers)")
    print(f"  {'=' * 60}")
    est = 4100
    print(f"  Original:   {total_orig * est / 8:.0f}s ({total_orig * est / 8 / 60:.1f} min)")
    print(f"  Optimized:  {total_opt * est / 8:.0f}s ({total_opt * est / 8 / 60:.1f} min)")


if __name__ == "__main__":
    main()
