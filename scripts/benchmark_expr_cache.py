"""
Benchmark: Per-Ticker Expression Cache Build Cost

Measures time spent in each phase of _compute_ticker_full() to identify
where optimization effort should go.

Usage:
    python scripts/benchmark_expr_cache.py

Output: Per-phase timing breakdown for 3 tickers (short/medium/long).
"""

import os
import sys
import time
import pickle
import numpy as np
import pandas as pd

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


def benchmark_ticker(ticker, df, expressions):
    """Run the same computation as _compute_ticker_full but time each phase."""

    # Access worker globals set by _init_worker
    global _w_expressions, _w_daily_indices, _w_ext_struct_indices
    global _w_ext_series_name_to_idx
    global _w_lsp_indices, _w_algo_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base

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

    # ── Phase 0: Engine construction ──
    t0 = time.perf_counter()
    engine = ExpressionEngine(df)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)
    results["0_engine_init"] = time.perf_counter() - t0

    # ── Phase 1a: Daily arithmetic (non-bool, non-on_series) ──
    # Separate bool ops from pure arithmetic
    BOOL_OPS = {"count_true", "since_true", "true_in_row"}
    arith_indices = [j for j in _w_daily_indices
                     if expressions[j]["compute"].get("op") not in BOOL_OPS]
    bool_indices = [j for j in _w_daily_indices
                    if expressions[j]["compute"].get("op") in BOOL_OPS]

    t0 = time.perf_counter()
    arith_ok = 0
    arith_fail = 0
    for j in arith_indices:
        try:
            series = compute_series(engine, expressions[j]["compute"])
            if series is not None:
                arr = np.asarray(series, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
                elif len(arr) < n_bars:
                    data[n_bars - len(arr):, j] = arr
                arith_ok += 1
        except:
            arith_fail += 1
    results["1a_daily_arithmetic"] = time.perf_counter() - t0

    # ── Phase 1b: Daily boolean aggregates ──
    t0 = time.perf_counter()
    bool_ok = 0
    bool_fail = 0
    for j in bool_indices:
        try:
            series = compute_series(engine, expressions[j]["compute"])
            if series is not None:
                arr = np.asarray(series, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
                elif len(arr) < n_bars:
                    data[n_bars - len(arr):, j] = arr
                bool_ok += 1
        except:
            bool_fail += 1
    results["1b_daily_booleans"] = time.perf_counter() - t0

    # ── Phase 2a: LSP expressions ──
    t0 = time.perf_counter()
    lsp_ok = 0
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
                        lsp_ok += 1
        except Exception as e:
            print(f"    LSP error: {e}")
    results["2a_lsp"] = time.perf_counter() - t0

    # ── Phase 2b: Algo line expressions ──
    t0 = time.perf_counter()
    algo_ok = 0
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
                        algo_ok += 1
        except Exception as e:
            print(f"    Algo error: {e}")
    results["2b_algo"] = time.perf_counter() - t0

    # ── Phase 3a: HTF Weekly ──
    t0 = time.perf_counter()
    htf_w_ok = 0
    if _w_htf_weekly_indices:
        htf_df = resample_ohlcv(df, "W")
        if htf_df is not None and len(htf_df) >= 5:
            htf_map = build_htf_to_daily_map(df["date"], htf_df, "W")
            htf_engine = ExpressionEngine(htf_df)
            for k, j in enumerate(_w_htf_weekly_indices):
                try:
                    base_compute = _w_htf_weekly_base[k]
                    htf_series = compute_series(htf_engine, base_compute)
                    if htf_series is not None:
                        htf_arr = np.asarray(htf_series, dtype=np.float32)
                        daily_arr = map_htf_series_to_daily(htf_arr, htf_map)
                        data[:, j] = daily_arr
                        htf_w_ok += 1
                except:
                    pass
    results["3a_htf_weekly"] = time.perf_counter() - t0

    # ── Phase 3b: HTF Monthly ──
    t0 = time.perf_counter()
    htf_m_ok = 0
    if _w_htf_monthly_indices:
        htf_df = resample_ohlcv(df, "ME")
        if htf_df is not None and len(htf_df) >= 5:
            htf_map = build_htf_to_daily_map(df["date"], htf_df, "ME")
            htf_engine = ExpressionEngine(htf_df)
            for k, j in enumerate(_w_htf_monthly_indices):
                try:
                    base_compute = _w_htf_monthly_base[k]
                    htf_series = compute_series(htf_engine, base_compute)
                    if htf_series is not None:
                        htf_arr = np.asarray(htf_series, dtype=np.float32)
                        daily_arr = map_htf_series_to_daily(htf_arr, htf_map)
                        data[:, j] = daily_arr
                        htf_m_ok += 1
                except:
                    pass
    results["3b_htf_monthly"] = time.perf_counter() - t0

    # ── Phase 4: Extension structure (on_series — second pass) ──
    t0 = time.perf_counter()
    ext_ok = 0
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        from scripts.backtest_conditions import compute_on_series

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
                            ext_ok += 1
                except:
                    pass
    results["4_ext_structure"] = time.perf_counter() - t0

    # Summary
    total = sum(results.values())
    filled = (1 - np.isnan(data).sum() / data.size) * 100

    return results, total, {
        "n_bars": n_bars,
        "arith": f"{arith_ok}/{len(arith_indices)}",
        "bools": f"{bool_ok}/{len(bool_indices)}",
        "lsp": f"{lsp_ok}/{len(_w_lsp_indices)}",
        "algo": f"{algo_ok}/{len(_w_algo_indices)}",
        "htf_w": f"{htf_w_ok}/{len(_w_htf_weekly_indices)}",
        "htf_m": f"{htf_m_ok}/{len(_w_htf_monthly_indices)}",
        "ext": f"{ext_ok}/{len(_w_ext_struct_indices)}",
        "filled_pct": filled,
    }


def main():
    print("\n" + "=" * 70)
    print("  EXPRESSION CACHE — PER-TICKER BENCHMARK")
    print("=" * 70)

    # Load expression library
    print("\n  Loading expressions...")
    expressions = _load_expressions()
    print(f"  {len(expressions)} expressions loaded")

    # Initialize worker globals (same as ProcessPoolExecutor initializer)
    _init_worker(expressions)

    # Reimport after init
    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices,
        _w_lsp_indices, _w_algo_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
    )

    BOOL_OPS = {"count_true", "since_true", "true_in_row"}
    n_arith = sum(1 for j in _w_daily_indices
                  if expressions[j]["compute"].get("op") not in BOOL_OPS)
    n_bool = sum(1 for j in _w_daily_indices
                 if expressions[j]["compute"].get("op") in BOOL_OPS)

    print(f"\n  Expression breakdown:")
    print(f"    Daily arithmetic:    {n_arith}")
    print(f"    Daily booleans:      {n_bool}")
    print(f"    Extension structure: {len(_w_ext_struct_indices)}")
    print(f"    LSP precomputed:     {len(_w_lsp_indices)}")
    print(f"    Algo precomputed:    {len(_w_algo_indices)}")
    print(f"    HTF weekly:          {len(_w_htf_weekly_indices)}")
    print(f"    HTF monthly:         {len(_w_htf_monthly_indices)}")
    print(f"    Total:               {len(expressions)}")

    # Load OHLCV
    print("\n  Loading 5yr OHLCV cache...")
    cache_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    with open(cache_path, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe)} tickers loaded")

    # Pick 3 tickers: longest, ~800 bars, ~200 bars
    valid = {t: df for t, df in universe.items() if df is not None and len(df) >= 50}
    by_bars = sorted(valid.items(), key=lambda x: len(x[1]))

    targets = []

    # Longest
    longest_ticker, longest_df = by_bars[-1]
    targets.append((longest_ticker, longest_df, "LONG"))

    # ~800 bars
    mid_target = 800
    mid_ticker, mid_df = min(by_bars, key=lambda x: abs(len(x[1]) - mid_target))
    targets.append((mid_ticker, mid_df, "MID"))

    # ~200 bars
    short_target = 200
    short_ticker, short_df = min(by_bars, key=lambda x: abs(len(x[1]) - short_target))
    targets.append((short_ticker, short_df, "SHORT"))

    # Free memory
    del universe, valid, by_bars
    import gc; gc.collect()

    # Run benchmarks
    for ticker, df, label in targets:
        print(f"\n  {'─' * 60}")
        print(f"  {label}: {ticker} ({len(df)} bars)")
        print(f"  {'─' * 60}")

        results, total, stats = benchmark_ticker(ticker, df, expressions)

        # Print breakdown
        print(f"\n  Phase                     Time (s)    %     ")
        print(f"  {'─' * 50}")
        for phase, secs in results.items():
            pct = secs / total * 100 if total > 0 else 0
            bar = "█" * int(pct / 2) + "░" * (25 - int(pct / 2))
            phase_name = phase.split("_", 1)[1] if "_" in phase else phase
            print(f"  {phase_name:<25} {secs:>7.3f}   {pct:>5.1f}%  {bar}")
        print(f"  {'─' * 50}")
        print(f"  {'TOTAL':<25} {total:>7.3f}   100.0%")

        print(f"\n  Success counts:")
        for k, v in stats.items():
            print(f"    {k}: {v}")

    # Extrapolation
    print(f"\n  {'=' * 60}")
    print(f"  FULL BUILD ESTIMATE (8 workers)")
    print(f"  {'=' * 60}")

    # Use the longest ticker as worst-case
    longest_results, longest_total, _ = benchmark_ticker(
        targets[0][0], targets[0][1], expressions
    )
    avg_time = longest_total  # worst case per ticker
    est_tickers = 4100
    sequential_time = est_tickers * avg_time
    parallel_time = sequential_time / 8

    print(f"  Worst-case per ticker: {avg_time:.1f}s")
    print(f"  Est. tickers: ~{est_tickers}")
    print(f"  Sequential total: {sequential_time:.0f}s ({sequential_time/60:.1f} min)")
    print(f"  With 8 workers: {parallel_time:.0f}s ({parallel_time/60:.1f} min)")

    # Phase cost at scale
    print(f"\n  Phase cost at full scale ({est_tickers} tickers, 8 workers):")
    for phase, secs in longest_results.items():
        phase_total = secs * est_tickers / 8
        phase_name = phase.split("_", 1)[1] if "_" in phase else phase
        print(f"    {phase_name:<25} {phase_total:>6.0f}s  ({phase_total/60:>5.1f} min)")


if __name__ == "__main__":
    main()
