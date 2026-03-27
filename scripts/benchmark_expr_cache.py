"""
Benchmark v4: Targeted Optimization Test

Optimizations applied:
1. numpy count_true / since_true / true_in_row (replacing pandas rolling().apply())
2. Vectorized trendline_deviation / channel_position (replacing per-bar Python loops)
3. HTF: apply same bool + linreg optimizations to HTF engines
4. Ext_other: pre-compute extension series once, dispatch non-linreg ops with numpy

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
    n = len(bool_arr)
    f = bool_arr.astype(np.float64)
    result = np.full(n, np.nan)
    cs = np.cumsum(f)
    result[period - 1] = cs[period - 1]
    result[period:] = cs[period:] - cs[:-period]
    return result


def np_since_true(bool_arr, period):
    n = len(bool_arr)
    result = np.full(n, np.nan)
    bars_since = np.full(n, n, dtype=np.float64)
    for i in range(n):
        if bool_arr[i]:
            bars_since[i] = 0.0
        elif i > 0:
            bars_since[i] = bars_since[i - 1] + 1.0
    for i in range(period - 1, n):
        if bars_since[i] < period:
            result[i] = bars_since[i]
        else:
            result[i] = -1.0
    return result


def np_true_in_row(bool_arr, max_look):
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
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(series)
    result = np.full(n, np.nan)
    if n < lookback:
        return result
    x = np.arange(lookback, dtype=np.float64)
    x_mean = np.mean(x)
    x_var = np.mean(x * x) - x_mean ** 2 + 1e-10
    windows = sliding_window_view(series, lookback)
    has_nan = np.any(np.isnan(windows), axis=1)
    w_mean = np.mean(windows, axis=1)
    xw_mean = np.mean(x[np.newaxis, :] * windows, axis=1)
    slope = (xw_mean - x_mean * w_mean) / x_var
    intercept = w_mean - slope * x_mean
    projected = slope * (lookback - 1) + intercept
    dev = windows[:, -1] - projected
    dev[has_nan] = np.nan
    result[lookback - 1:] = dev
    return result


def np_channel_position(series, lookback):
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(series)
    result = np.full(n, np.nan)
    if n < lookback:
        return result
    x = np.arange(lookback, dtype=np.float64)
    x_mean = np.mean(x)
    x_var = np.mean(x * x) - x_mean ** 2 + 1e-10
    windows = sliding_window_view(series, lookback)
    has_nan = np.any(np.isnan(windows), axis=1)
    w_mean = np.mean(windows, axis=1)
    xw_mean = np.mean(x[np.newaxis, :] * windows, axis=1)
    slope = (xw_mean - x_mean * w_mean) / x_var
    intercept = w_mean - slope * x_mean
    projected_lines = slope[:, np.newaxis] * x[np.newaxis, :] + intercept[:, np.newaxis]
    residuals = windows - projected_lines
    std_resid = np.std(residuals, axis=1)
    projected = slope * (lookback - 1) + intercept
    with np.errstate(divide='ignore', invalid='ignore'):
        pos = np.where(std_resid > 0, (windows[:, -1] - projected) / std_resid, np.nan)
    pos[has_nan] = np.nan
    result[lookback - 1:] = pos
    return result


def run_optimized_bools(engine, expressions, indices_by_op, n_bars, data):
    """Run boolean expressions with numpy optimizations.
    Returns time breakdown dict."""
    ct_indices, st_indices, tir_indices = indices_by_op
    results = {}

    # Pre-compute all unique boolean conditions
    t0 = time.perf_counter()
    bool_cache = {}
    for j in ct_indices + st_indices + tir_indices:
        cond = expressions[j]["compute"]["condition"]
        if cond not in bool_cache:
            try:
                b = engine._bool_series(cond)
                bool_cache[cond] = b.values.astype(bool)
            except:
                bool_cache[cond] = np.zeros(n_bars, dtype=bool)
    results["bool_conditions"] = time.perf_counter() - t0

    # count_true
    t0 = time.perf_counter()
    for j in ct_indices:
        comp = expressions[j]["compute"]
        b = bool_cache[comp["condition"]]
        data[:, j] = np_count_true(b, comp["period"]).astype(np.float32)
    results["count_true"] = time.perf_counter() - t0

    # since_true
    t0 = time.perf_counter()
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
        bs = bars_since_cache[comp["condition"]]
        period = comp["period"]
        r = np.full(n_bars, np.nan)
        for i in range(period - 1, n_bars):
            r[i] = bs[i] if bs[i] < period else -1.0
        data[:, j] = r.astype(np.float32)
    results["since_true"] = time.perf_counter() - t0

    # true_in_row
    t0 = time.perf_counter()
    for j in tir_indices:
        comp = expressions[j]["compute"]
        b = bool_cache[comp["condition"]]
        data[:, j] = np_true_in_row(b, comp["period"]).astype(np.float32)
    results["true_in_row"] = time.perf_counter() - t0

    return results


def run_optimized_ext_struct(engine, expressions, ext_indices, ext_name_to_idx, n_bars, data):
    """Run extension structure with vectorized linreg. Returns time breakdown."""
    LINREG_OPS = {"trendline_deviation", "channel_position"}
    results = {}

    series_registry = {}
    for sname, sidx in ext_name_to_idx.items():
        col_data = data[:, sidx]
        if not np.all(np.isnan(col_data)):
            series_registry[sname] = col_data.astype(np.float64)

    if not series_registry:
        results["ext_linreg"] = 0.0
        results["ext_other"] = 0.0
        return results

    t_linreg = 0.0
    t_other = 0.0

    for j in ext_indices:
        comp = expressions[j]["compute"]
        inner_op_spec = comp.get("inner_op", {})
        inner_op_name = inner_op_spec.get("op", "")

        if inner_op_name in LINREG_OPS:
            t_before = time.perf_counter()
            try:
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
            t_before = time.perf_counter()
            try:
                s = compute_series(engine, comp, series_registry=series_registry)
                if s is not None:
                    arr = np.asarray(s, dtype=np.float32)
                    if len(arr) == n_bars:
                        data[:, j] = arr
            except:
                pass
            t_other += time.perf_counter() - t_before

    results["ext_linreg"] = t_linreg
    results["ext_other"] = t_other
    return results


# ══════════════════════════════════════════════════════════════
# BENCHMARK: ORIGINAL PATH
# ══════════════════════════════════════════════════════════════

def benchmark_original(ticker, df, expressions):
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
    results["daily_arith"] = time.perf_counter() - t0

    # Daily booleans
    bool_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") in BOOL_OPS]
    t0 = time.perf_counter()
    for j in bool_indices:
        try:
            s = compute_series(engine, expressions[j]["compute"])
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
        except:
            pass
    results["daily_bools"] = time.perf_counter() - t0

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
    results["lsp_algo"] = time.perf_counter() - t0

    # HTF
    for freq, label, htf_indices, htf_base in [
        ("W", "htf_weekly", _w_htf_weekly_indices, _w_htf_weekly_base),
        ("ME", "htf_monthly", _w_htf_monthly_indices, _w_htf_monthly_base),
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

    # Extension structure
    t0 = time.perf_counter()
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        series_registry = {}
        for sname, sidx in _w_ext_series_name_to_idx.items():
            col_data = data[:, sidx]
            if not np.all(np.isnan(col_data)):
                series_registry[sname] = col_data.astype(np.float64)
        if series_registry:
            for j in _w_ext_struct_indices:
                try:
                    s = compute_series(engine, expressions[j]["compute"],
                                       series_registry=series_registry)
                    if s is not None:
                        arr = np.asarray(s, dtype=np.float32)
                        if len(arr) == n_bars:
                            data[:, j] = arr
                except:
                    pass
    results["ext_struct"] = time.perf_counter() - t0

    total = sum(results.values())
    return data, results, total


# ══════════════════════════════════════════════════════════════
# BENCHMARK: OPTIMIZED PATH
# ══════════════════════════════════════════════════════════════

def benchmark_optimized(ticker, df, expressions):
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

    # Daily arithmetic — SAME
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
    results["daily_arith"] = time.perf_counter() - t0

    # Daily booleans — OPTIMIZED
    ct_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "count_true"]
    st_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "since_true"]
    tir_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "true_in_row"]
    
    t0 = time.perf_counter()
    bool_results = run_optimized_bools(engine, expressions, (ct_indices, st_indices, tir_indices), n_bars, data)
    results["daily_bools"] = time.perf_counter() - t0

    # LSP + Algo — SAME
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
    results["lsp_algo"] = time.perf_counter() - t0

    # HTF — OPTIMIZED: apply bool + linreg optimizations to HTF engines
    for freq, label, htf_indices, htf_base in [
        ("W", "htf_weekly", _w_htf_weekly_indices, _w_htf_weekly_base),
        ("ME", "htf_monthly", _w_htf_monthly_indices, _w_htf_monthly_base),
    ]:
        t0 = time.perf_counter()
        if htf_indices:
            htf_df = resample_ohlcv(df, freq)
            if htf_df is not None and len(htf_df) >= 5:
                htf_map = build_htf_to_daily_map(df["date"], htf_df, freq)
                htf_engine = ExpressionEngine(htf_df)
                htf_n_bars = len(htf_df)

                # Classify HTF expressions
                htf_ct = []
                htf_st = []
                htf_tir = []
                htf_arith = []
                htf_ext = []
                ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

                for k, j in enumerate(htf_indices):
                    base_op = htf_base[k].get("op", "")
                    if base_op == "count_true":
                        htf_ct.append((k, j))
                    elif base_op == "since_true":
                        htf_st.append((k, j))
                    elif base_op == "true_in_row":
                        htf_tir.append((k, j))
                    elif base_op in ON_SERIES_OPS:
                        htf_ext.append((k, j))
                    else:
                        htf_arith.append((k, j))

                # HTF arithmetic — use compute_series (unchanged)
                for k, j in htf_arith:
                    try:
                        s = compute_series(htf_engine, htf_base[k])
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(
                                np.asarray(s, dtype=np.float32), htf_map)
                    except:
                        pass

                # HTF booleans — numpy optimized
                htf_bool_cache = {}
                for k, j in htf_ct + htf_st + htf_tir:
                    cond = htf_base[k]["condition"]
                    if cond not in htf_bool_cache:
                        try:
                            b = htf_engine._bool_series(cond)
                            htf_bool_cache[cond] = b.values.astype(bool)
                        except:
                            htf_bool_cache[cond] = np.zeros(htf_n_bars, dtype=bool)

                for k, j in htf_ct:
                    b = htf_bool_cache[htf_base[k]["condition"]]
                    htf_arr = np_count_true(b, htf_base[k]["period"])
                    data[:, j] = map_htf_series_to_daily(htf_arr.astype(np.float32), htf_map)

                # HTF since_true: pre-compute bars_since per condition
                htf_bs_cache = {}
                for k, j in htf_st:
                    cond = htf_base[k]["condition"]
                    if cond not in htf_bs_cache:
                        b = htf_bool_cache[cond]
                        bs = np.full(htf_n_bars, htf_n_bars, dtype=np.float64)
                        for i in range(htf_n_bars):
                            if b[i]:
                                bs[i] = 0.0
                            elif i > 0:
                                bs[i] = bs[i - 1] + 1.0
                        htf_bs_cache[cond] = bs

                for k, j in htf_st:
                    bs = htf_bs_cache[htf_base[k]["condition"]]
                    period = htf_base[k]["period"]
                    r = np.full(htf_n_bars, np.nan)
                    for i in range(period - 1, htf_n_bars):
                        r[i] = bs[i] if bs[i] < period else -1.0
                    data[:, j] = map_htf_series_to_daily(r.astype(np.float32), htf_map)

                for k, j in htf_tir:
                    b = htf_bool_cache[htf_base[k]["condition"]]
                    htf_arr = np_true_in_row(b, htf_base[k]["period"])
                    data[:, j] = map_htf_series_to_daily(htf_arr.astype(np.float32), htf_map)

                # HTF ext struct — use compute_series (small arrays, not worth optimizing)
                for k, j in htf_ext:
                    try:
                        s = compute_series(htf_engine, htf_base[k])
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(
                                np.asarray(s, dtype=np.float32), htf_map)
                    except:
                        pass

        results[label] = time.perf_counter() - t0

    # Extension structure — OPTIMIZED
    t0 = time.perf_counter()
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        ext_results = run_optimized_ext_struct(
            engine, expressions, _w_ext_struct_indices,
            _w_ext_series_name_to_idx, n_bars, data
        )
    results["ext_struct"] = time.perf_counter() - t0

    total = sum(results.values())
    return data, results, total


# ══════════════════════════════════════════════════════════════
# COMPARISON
# ══════════════════════════════════════════════════════════════

def compare_outputs(data_orig, data_opt, expressions, indices):
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
    print("  EXPRESSION CACHE — TARGETED OPTIMIZATION BENCHMARK v4")
    print("=" * 70)

    expressions = _load_expressions()
    _init_worker(expressions)

    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
    )

    print(f"\n  {len(expressions)} expressions")

    # Load OHLCV
    print("  Loading 5yr OHLCV cache...")
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
        print(f"  {phase:<25} {secs:>7.3f}   {pct:>5.1f}%")
    print(f"  {'─' * 50}")
    print(f"  {'TOTAL':<25} {total_orig:>7.3f}   100.0%")

    # ═══ OPTIMIZED ═══
    print(f"\n  {'─' * 60}")
    print(f"  OPTIMIZED (numpy bools + vectorized linreg + HTF bools)")
    print(f"  {'─' * 60}")
    data_opt, res_opt, total_opt = benchmark_optimized(ticker, df, expressions)

    print(f"\n  Phase                     Time (s)    %")
    print(f"  {'─' * 50}")
    for phase, secs in res_opt.items():
        pct = secs / total_opt * 100
        print(f"  {phase:<25} {secs:>7.3f}   {pct:>5.1f}%")
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

    # Per-phase
    for phase in res_orig:
        if phase in res_opt:
            o = res_orig[phase]
            n = res_opt[phase]
            sp = o / n if n > 0 else 0
            saved = o - n
            if saved > 0.1:
                print(f"  {phase}: {o:.3f}s -> {n:.3f}s ({sp:.1f}x, saved {saved:.1f}s)")

    # ═══ CORRECTNESS ═══
    print(f"\n  {'─' * 60}")
    print(f"  CORRECTNESS")
    print(f"  {'─' * 60}")

    all_indices = list(range(len(expressions)))
    m, mm, md, w, ex = compare_outputs(data_orig, data_opt, expressions, all_indices)
    print(f"\n  All expressions ({len(all_indices)}):")
    print(f"    Match: {m}, Mismatch: {mm}")
    print(f"    Max diff: {md:.6f} ({w})")
    if ex:
        print(f"    Examples:")
        for e in ex[:10]:
            print(f"      {e}")

    # ═══ FULL BUILD ESTIMATE ═══
    print(f"\n  {'=' * 60}")
    print(f"  FULL BUILD ESTIMATE (8 workers, 4100 tickers)")
    print(f"  {'=' * 60}")
    est = 4100
    orig_est = total_orig * est / 8
    opt_est = total_opt * est / 8
    print(f"  Original:   {orig_est:.0f}s ({orig_est/60:.1f} min)")
    print(f"  Optimized:  {opt_est:.0f}s ({opt_est/60:.1f} min)")
    print(f"  Savings:    {(orig_est - opt_est)/60:.1f} min")


if __name__ == "__main__":
    main()
