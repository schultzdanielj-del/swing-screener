"""
Benchmark: Per-Ticker Expression Cache Build Cost

Measures time spent in each phase of _compute_ticker_full() to identify
where optimization effort should go.

Phase A: Run current path (compute_series per expression)
Phase B: Run vectorized path (build_intermediates + compute_expr_2d)
Compare: timing + output correctness

Usage:
    python scripts/benchmark_expr_cache.py

Output: Per-phase timing breakdown, vectorized comparison, correctness check.
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


def benchmark_original(ticker, df, expressions):
    """Run the current compute_series path — same as _compute_ticker_full."""

    from expr_cache_builder import (
        _w_expressions, _w_daily_indices, _w_ext_struct_indices,
        _w_ext_series_name_to_idx,
        _w_lsp_indices, _w_algo_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
        _w_htf_weekly_base, _w_htf_monthly_base,
    )

    n_bars = len(df)
    n_exprs = len(expressions)
    results = {}

    # Phase 0: Engine init
    t0 = time.perf_counter()
    engine = ExpressionEngine(df)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)
    results["0_engine_init"] = time.perf_counter() - t0

    # Phase 1a: Daily arithmetic
    arith_indices = [j for j in _w_daily_indices
                     if expressions[j]["compute"].get("op") not in BOOL_OPS]
    bool_indices = [j for j in _w_daily_indices
                    if expressions[j]["compute"].get("op") in BOOL_OPS]

    t0 = time.perf_counter()
    for j in arith_indices:
        try:
            series = compute_series(engine, expressions[j]["compute"])
            if series is not None:
                arr = np.asarray(series, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
                elif len(arr) < n_bars:
                    data[n_bars - len(arr):, j] = arr
        except:
            pass
    results["1a_daily_arithmetic"] = time.perf_counter() - t0

    # Phase 1b: Daily booleans
    t0 = time.perf_counter()
    for j in bool_indices:
        try:
            series = compute_series(engine, expressions[j]["compute"])
            if series is not None:
                arr = np.asarray(series, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
                elif len(arr) < n_bars:
                    data[n_bars - len(arr):, j] = arr
        except:
            pass
    results["1b_daily_booleans"] = time.perf_counter() - t0

    # Phase 2a: LSP
    t0 = time.perf_counter()
    if _w_lsp_indices:
        try:
            from scripts.lsp_detector_v2 import compute_all_lsp_series
            lsp_dict = compute_all_lsp_series(df)
            for j in _w_lsp_indices:
                col_name = expressions[j]["compute"]["column"]
                if col_name in lsp_dict:
                    arr = lsp_dict[col_name]
                    if len(arr) == n_bars:
                        data[:, j] = arr.astype(np.float32)
        except Exception:
            pass
    results["2a_lsp"] = time.perf_counter() - t0

    # Phase 2b: Algo
    t0 = time.perf_counter()
    if _w_algo_indices:
        try:
            from scripts.algo_line_detector import compute_all_algo_series
            algo_dict = compute_all_algo_series(df)
            for j in _w_algo_indices:
                col_name = expressions[j]["compute"]["column"]
                if col_name in algo_dict:
                    arr = algo_dict[col_name]
                    if len(arr) == n_bars:
                        data[:, j] = arr.astype(np.float32)
        except Exception:
            pass
    results["2b_algo"] = time.perf_counter() - t0

    # Phase 3a: HTF weekly
    t0 = time.perf_counter()
    if _w_htf_weekly_indices:
        htf_df = resample_ohlcv(df, "W")
        if htf_df is not None and len(htf_df) >= 5:
            htf_map = build_htf_to_daily_map(df["date"], htf_df, "W")
            htf_engine = ExpressionEngine(htf_df)
            for k, j in enumerate(_w_htf_weekly_indices):
                try:
                    htf_series = compute_series(htf_engine, _w_htf_weekly_base[k])
                    if htf_series is not None:
                        htf_arr = np.asarray(htf_series, dtype=np.float32)
                        data[:, j] = map_htf_series_to_daily(htf_arr, htf_map)
                except:
                    pass
    results["3a_htf_weekly"] = time.perf_counter() - t0

    # Phase 3b: HTF monthly
    t0 = time.perf_counter()
    if _w_htf_monthly_indices:
        htf_df = resample_ohlcv(df, "ME")
        if htf_df is not None and len(htf_df) >= 5:
            htf_map = build_htf_to_daily_map(df["date"], htf_df, "ME")
            htf_engine = ExpressionEngine(htf_df)
            for k, j in enumerate(_w_htf_monthly_indices):
                try:
                    htf_series = compute_series(htf_engine, _w_htf_monthly_base[k])
                    if htf_series is not None:
                        htf_arr = np.asarray(htf_series, dtype=np.float32)
                        data[:, j] = map_htf_series_to_daily(htf_arr, htf_map)
                except:
                    pass
    results["3b_htf_monthly"] = time.perf_counter() - t0

    # Phase 4: Extension structure
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
                    series = compute_series(
                        engine, expressions[j]["compute"],
                        series_registry=series_registry
                    )
                    if series is not None:
                        arr = np.asarray(series, dtype=np.float32)
                        if len(arr) == n_bars:
                            data[:, j] = arr
                except:
                    pass
    results["4_ext_structure"] = time.perf_counter() - t0

    total = sum(results.values())
    return data, results, total


def benchmark_vectorized(ticker, df, expressions):
    """Run the vectorized path: build_intermediates + compute_expr_2d.

    Treats one ticker as a (1, n_bars) 2D array so we can reuse
    the existing vectorized_dispatch.py functions directly.
    """
    from vectorized_dispatch import build_intermediates, compute_expr_2d
    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
        _w_htf_weekly_base, _w_htf_monthly_base,
    )
    from vectorized_cache_builder import _resample_2d, _build_htf_map

    n_bars = len(df)
    n_exprs = len(expressions)
    results = {}

    # Reshape OHLCV to (1, n_bars) for 2D dispatch
    O = df["open"].values.astype(np.float64).reshape(1, -1)
    H = df["high"].values.astype(np.float64).reshape(1, -1)
    L = df["low"].values.astype(np.float64).reshape(1, -1)
    C = df["close"].values.astype(np.float64).reshape(1, -1)
    V = df["volume"].values.astype(np.float64).reshape(1, -1)

    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

    # ── Phase 0: Build intermediates ──
    t0 = time.perf_counter()
    im = build_intermediates(O, H, L, C, V)
    im["_close"] = C
    results["0_intermediates"] = time.perf_counter() - t0

    # ── Phase 1: Daily arithmetic + booleans + ext structure ──
    # All of these go through compute_expr_2d which handles all ops
    all_daily_indices = list(_w_daily_indices) + list(_w_ext_struct_indices)

    t0 = time.perf_counter()
    ok = 0
    fail = 0
    for j in all_daily_indices:
        try:
            r = compute_expr_2d(expressions[j]["compute"], im, O, H, L, C, V)
            arr = r.squeeze(axis=0).astype(np.float32)  # (1, n_bars) -> (n_bars,)
            if len(arr) == n_bars:
                data[:, j] = arr
                ok += 1
        except Exception:
            fail += 1
    results["1_daily_all"] = time.perf_counter() - t0

    # ── Phase 2: HTF weekly (vectorized) ──
    t0 = time.perf_counter()
    if _w_htf_weekly_indices:
        dates = pd.to_datetime(df["date"]).values
        Ow, Hw, Lw, Cw, Vw, w_dates = _resample_2d(O, H, L, C, V, dates, "W")
        if Cw.shape[1] >= 5:
            im_w = build_intermediates(Ow, Hw, Lw, Cw, Vw)
            im_w["_close"] = Cw
            htf_w_map = _build_htf_map(dates, w_dates)
            for k, j in enumerate(_w_htf_weekly_indices):
                try:
                    wr = compute_expr_2d(_w_htf_weekly_base[k], im_w, Ow, Hw, Lw, Cw, Vw)
                    mapped = wr[0, htf_w_map].astype(np.float32)
                    data[:, j] = mapped
                except Exception:
                    pass
    results["2_htf_weekly"] = time.perf_counter() - t0

    # ── Phase 3: HTF monthly (vectorized) ──
    t0 = time.perf_counter()
    if _w_htf_monthly_indices:
        dates = pd.to_datetime(df["date"]).values
        Om, Hm, Lm, Cm, Vm, m_dates = _resample_2d(O, H, L, C, V, dates, "ME")
        if Cm.shape[1] >= 5:
            im_m = build_intermediates(Om, Hm, Lm, Cm, Vm)
            im_m["_close"] = Cm
            htf_m_map = _build_htf_map(dates, m_dates)
            for k, j in enumerate(_w_htf_monthly_indices):
                try:
                    mr = compute_expr_2d(_w_htf_monthly_base[k], im_m, Om, Hm, Lm, Cm, Vm)
                    mapped = mr[0, htf_m_map].astype(np.float32)
                    data[:, j] = mapped
                except Exception:
                    pass
    results["3_htf_monthly"] = time.perf_counter() - t0

    total = sum(results.values())
    return data, results, total, {"ok": ok, "fail": fail}


def compare_outputs(data_orig, data_vec, expressions, indices_to_check):
    """Compare two output arrays, report mismatches."""
    n_checked = 0
    n_match = 0
    n_both_nan = 0
    n_mismatch = 0
    max_diff = 0.0
    worst_expr = ""
    mismatch_examples = []

    for j in indices_to_check:
        orig = data_orig[:, j]
        vec = data_vec[:, j]

        # Both NaN = match
        both_nan = np.isnan(orig) & np.isnan(vec)
        # One NaN, other not = mismatch
        one_nan = np.isnan(orig) != np.isnan(vec)
        # Both have values = compare
        both_val = ~np.isnan(orig) & ~np.isnan(vec)

        nan_mismatches = one_nan.sum()

        if both_val.any():
            diffs = np.abs(orig[both_val] - vec[both_val])
            # float32 tolerance: values can differ by up to ~1e-6 relative
            # Use absolute tolerance scaled by value magnitude
            magnitudes = np.maximum(np.abs(orig[both_val]), np.abs(vec[both_val]))
            magnitudes = np.maximum(magnitudes, 1e-8)  # floor for near-zero
            rel_diffs = diffs / magnitudes
            val_mismatches = (rel_diffs > 1e-4).sum()  # 0.01% relative tolerance
            if diffs.max() > max_diff:
                max_diff = diffs.max()
                worst_expr = expressions[j]["name"]
        else:
            val_mismatches = 0

        total_mismatches = nan_mismatches + val_mismatches

        if total_mismatches == 0:
            n_match += 1
        else:
            n_mismatch += 1
            if len(mismatch_examples) < 10:
                mismatch_examples.append(
                    f"{expressions[j]['name']}: {nan_mismatches} NaN mismatches, "
                    f"{val_mismatches} value mismatches"
                )

        n_checked += 1

    return {
        "checked": n_checked,
        "match": n_match,
        "mismatch": n_mismatch,
        "max_abs_diff": max_diff,
        "worst_expr": worst_expr,
        "examples": mismatch_examples,
    }


def main():
    print("\n" + "=" * 70)
    print("  EXPRESSION CACHE — VECTORIZED vs ORIGINAL BENCHMARK")
    print("=" * 70)

    # Load expressions and init worker
    print("\n  Loading expressions...")
    expressions = _load_expressions()
    _init_worker(expressions)

    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
    )

    n_arith = sum(1 for j in _w_daily_indices
                  if expressions[j]["compute"].get("op") not in BOOL_OPS)
    n_bool = sum(1 for j in _w_daily_indices
                 if expressions[j]["compute"].get("op") in BOOL_OPS)

    print(f"  {len(expressions)} expressions")
    print(f"    Daily arithmetic:    {n_arith}")
    print(f"    Daily booleans:      {n_bool}")
    print(f"    Extension structure: {len(_w_ext_struct_indices)}")
    print(f"    HTF weekly:          {len(_w_htf_weekly_indices)}")
    print(f"    HTF monthly:         {len(_w_htf_monthly_indices)}")

    # Load OHLCV
    print("\n  Loading 5yr OHLCV cache...")
    cache_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    with open(cache_path, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe)} tickers loaded")

    # Pick the longest ticker for worst-case benchmark
    valid = {t: df for t, df in universe.items() if df is not None and len(df) >= 50}
    by_bars = sorted(valid.items(), key=lambda x: len(x[1]))
    ticker, df = by_bars[-1]

    del universe, valid, by_bars
    import gc; gc.collect()

    print(f"\n  Benchmark ticker: {ticker} ({len(df)} bars)")

    # ════════════════════════════════════════════════════════
    # RUN A: Original path
    # ════════════════════════════════════════════════════════
    print(f"\n  {'─' * 60}")
    print(f"  ORIGINAL PATH (compute_series per expression)")
    print(f"  {'─' * 60}")

    data_orig, results_orig, total_orig = benchmark_original(
        ticker, df, expressions
    )

    print(f"\n  Phase                     Time (s)    %")
    print(f"  {'─' * 45}")
    for phase, secs in results_orig.items():
        pct = secs / total_orig * 100 if total_orig > 0 else 0
        phase_name = phase.split("_", 1)[1]
        print(f"  {phase_name:<25} {secs:>7.3f}   {pct:>5.1f}%")
    print(f"  {'─' * 45}")
    print(f"  {'TOTAL':<25} {total_orig:>7.3f}   100.0%")

    # ════════════════════════════════════════════════════════
    # RUN B: Vectorized path
    # ════════════════════════════════════════════════════════
    print(f"\n  {'─' * 60}")
    print(f"  VECTORIZED PATH (build_intermediates + compute_expr_2d)")
    print(f"  {'─' * 60}")

    data_vec, results_vec, total_vec, vec_stats = benchmark_vectorized(
        ticker, df, expressions
    )

    print(f"\n  Phase                     Time (s)    %")
    print(f"  {'─' * 45}")
    for phase, secs in results_vec.items():
        pct = secs / total_vec * 100 if total_vec > 0 else 0
        phase_name = phase.split("_", 1)[1]
        print(f"  {phase_name:<25} {secs:>7.3f}   {pct:>5.1f}%")
    print(f"  {'─' * 45}")
    print(f"  {'TOTAL':<25} {total_vec:>7.3f}   100.0%")
    print(f"  Dispatch: {vec_stats['ok']} ok, {vec_stats['fail']} failed")

    # ════════════════════════════════════════════════════════
    # COMPARISON
    # ════════════════════════════════════════════════════════
    print(f"\n  {'─' * 60}")
    print(f"  COMPARISON")
    print(f"  {'─' * 60}")

    speedup = total_orig / total_vec if total_vec > 0 else 0
    print(f"\n  Original:   {total_orig:.3f}s")
    print(f"  Vectorized: {total_vec:.3f}s")
    print(f"  Speedup:    {speedup:.2f}x")

    # Compare per-phase where possible
    orig_daily = (results_orig.get("1a_daily_arithmetic", 0) +
                  results_orig.get("1b_daily_booleans", 0) +
                  results_orig.get("4_ext_structure", 0))
    vec_daily = results_vec.get("1_daily_all", 0)
    if vec_daily > 0:
        print(f"\n  Daily+Bool+ExtStruct:")
        print(f"    Original:   {orig_daily:.3f}s")
        print(f"    Vectorized: {vec_daily:.3f}s")
        print(f"    Speedup:    {orig_daily / vec_daily:.2f}x")

    orig_htf = (results_orig.get("3a_htf_weekly", 0) +
                results_orig.get("3b_htf_monthly", 0))
    vec_htf = (results_vec.get("2_htf_weekly", 0) +
               results_vec.get("3_htf_monthly", 0))
    if vec_htf > 0:
        print(f"\n  HTF (weekly + monthly):")
        print(f"    Original:   {orig_htf:.3f}s")
        print(f"    Vectorized: {vec_htf:.3f}s")
        print(f"    Speedup:    {orig_htf / vec_htf:.2f}x")

    # ════════════════════════════════════════════════════════
    # CORRECTNESS CHECK
    # ════════════════════════════════════════════════════════
    print(f"\n  {'─' * 60}")
    print(f"  CORRECTNESS CHECK")
    print(f"  {'─' * 60}")

    # Check daily + bool + ext struct
    check_indices = list(_w_daily_indices) + list(_w_ext_struct_indices)
    daily_cmp = compare_outputs(data_orig, data_vec, expressions, check_indices)
    print(f"\n  Daily+Bool+ExtStruct ({daily_cmp['checked']} expressions):")
    print(f"    Match:     {daily_cmp['match']}")
    print(f"    Mismatch:  {daily_cmp['mismatch']}")
    print(f"    Max diff:  {daily_cmp['max_abs_diff']:.6f} ({daily_cmp['worst_expr']})")
    if daily_cmp['examples']:
        print(f"    Examples:")
        for ex in daily_cmp['examples']:
            print(f"      {ex}")

    # Check HTF
    htf_indices = list(_w_htf_weekly_indices) + list(_w_htf_monthly_indices)
    htf_cmp = compare_outputs(data_orig, data_vec, expressions, htf_indices)
    print(f"\n  HTF weekly+monthly ({htf_cmp['checked']} expressions):")
    print(f"    Match:     {htf_cmp['match']}")
    print(f"    Mismatch:  {htf_cmp['mismatch']}")
    print(f"    Max diff:  {htf_cmp['max_abs_diff']:.6f} ({htf_cmp['worst_expr']})")
    if htf_cmp['examples']:
        print(f"    Examples:")
        for ex in htf_cmp['examples'][:5]:
            print(f"      {ex}")

    # ════════════════════════════════════════════════════════
    # FULL BUILD ESTIMATE
    # ════════════════════════════════════════════════════════
    print(f"\n  {'=' * 60}")
    print(f"  FULL BUILD ESTIMATE (8 workers)")
    print(f"  {'=' * 60}")

    # Original estimate (without LSP/algo — those stay the same)
    orig_compute = total_orig - results_orig.get("2a_lsp", 0) - results_orig.get("2b_algo", 0)
    vec_compute = total_vec

    est_tickers = 4100
    lsp_algo_per_ticker = results_orig.get("2a_lsp", 0) + results_orig.get("2b_algo", 0)

    orig_total_est = (orig_compute + lsp_algo_per_ticker) * est_tickers / 8
    vec_total_est = (vec_compute + lsp_algo_per_ticker) * est_tickers / 8

    print(f"  Per ticker (compute only, no LSP/algo):")
    print(f"    Original:   {orig_compute:.1f}s")
    print(f"    Vectorized: {vec_compute:.1f}s")
    print(f"  LSP+Algo per ticker: {lsp_algo_per_ticker:.1f}s")
    print(f"\n  Full build ({est_tickers} tickers, 8 workers):")
    print(f"    Original:   {orig_total_est:.0f}s ({orig_total_est/60:.1f} min)")
    print(f"    Vectorized: {vec_total_est:.0f}s ({vec_total_est/60:.1f} min)")


if __name__ == "__main__":
    main()
