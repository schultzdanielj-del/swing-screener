"""
Benchmark Incremental Append — Multi-ticker cost estimation.

Runs the proposed append phases on N tickers (default 100) and reports
per-ticker averages plus projected wall time for the full universe.

Usage:
    python scripts/benchmark_incremental_append.py [--n 100]
"""

import os
import sys
import time
import random
import pickle
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)
sys.path.insert(0, SCRIPT_DIR)

CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")
EXPR_CACHE_START = "2020-01-02"


def load_daily_cache():
    for name in ["universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"]:
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
    raise FileNotFoundError("No OHLCV cache found")


def load_htf_cache(timeframe):
    path = os.path.join(CACHE_DIR, f"universe_ohlcv_{timeframe}.pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def truncate_to_cache_window(df):
    if df is None or len(df) == 0:
        return None
    start = pd.Timestamp(EXPR_CACHE_START)
    dates = pd.to_datetime(df["date"])
    mask = dates >= start
    if not mask.any():
        return None
    return df[mask].reset_index(drop=True)


def benchmark_one_ticker(ticker, df_full, weekly_df, monthly_df,
                          expressions, n_exprs,
                          state_only_idx, lookback_idx, lookback_depths,
                          htf_idx, lsp_idx, algo_idx, max_lb):
    """Run all append phases on one ticker, return per-phase timings."""
    from expr_cache_builder import load_ticker_cache

    result = {}
    n_bars = len(df_full)

    # Phase 0: Load .npz
    t0 = time.time()
    cached_dates, cached_data = load_ticker_cache(ticker)
    result["load_npz"] = time.time() - t0

    if cached_data is None or cached_data.shape[0] < 2:
        return None

    prev_row = cached_data[-2, :]
    lb_start = max(0, cached_data.shape[0] - 1 - max_lb)
    lookback_buf = cached_data[lb_start:-1, :]

    # Phase 1: State-only
    new_row = np.full(n_exprs, np.nan, dtype=np.float32)
    today_close = df_full["close"].iloc[-1]
    t0 = time.time()
    for i in state_only_idx:
        new_row[i] = 0.1 * today_close + 0.9 * prev_row[i]
    result["state"] = time.time() - t0

    # Phase 2: Lookback
    t0 = time.time()
    for i in lookback_idx:
        depth = lookback_depths[i]
        buf_len = lookback_buf.shape[0]
        start = max(0, buf_len - depth)
        window = lookback_buf[start:, i]
        op = expressions[i]["compute"].get("op", "")
        if op in ("count_true", "since_true", "true_in_row"):
            new_row[i] = np.nansum(window > 0)
        elif op == "percentile_rank":
            val = prev_row[i]
            if len(window) > 0 and not np.isnan(val):
                new_row[i] = np.nansum(window <= val) / len(window) * 100
        elif op in ("extension_ceiling_ratio", "extension_peak_ratio",
                     "bollinger_bandwidth_rank"):
            if len(window) > 0:
                new_row[i] = np.nanmax(window)
        elif op in ("aroon_up_val", "aroon_down_val", "aroon_oscillator"):
            if len(window) > 0:
                new_row[i] = float(np.nanargmax(window))
        elif op == "cci":
            if len(window) > 0:
                m = np.nanmean(window)
                new_row[i] = np.nanmean(np.abs(window - m))
        else:
            if len(window) > 0:
                new_row[i] = np.nanmean(window)
    result["lookback"] = time.time() - t0

    # Phase 3: HTF
    t0 = time.time()
    if weekly_df is not None and monthly_df is not None and len(weekly_df) >= 5 and len(monthly_df) >= 5:
        try:
            from partial_candle_engine import (
                build_partial_candle_mapping, extract_closed_state,
                build_partial_intermediates
            )
            from scripts.expression_engine import ExpressionEngine

            for tf_label, htf_df in [("weekly", weekly_df), ("monthly", monthly_df)]:
                engine_htf = ExpressionEngine(htf_df)
                closed_im, closed_raw = extract_closed_state(engine_htf, htf_df)
                lci, partial, prev_c = build_partial_candle_mapping(
                    df_full.iloc[-1:].reset_index(drop=True), htf_df,
                    "W" if tf_label == "weekly" else "ME")
                im_partial = build_partial_intermediates(
                    closed_im, closed_raw, lci, partial, prev_c)
        except Exception:
            pass
    result["htf"] = time.time() - t0

    # Phase 4: LSP + Algo
    t0 = time.time()
    try:
        from scripts.lsp_detector_v2 import compute_all_lsp_series
        lsp_dict = compute_all_lsp_series(df_full)
        for i in lsp_idx:
            col = expressions[i]["compute"]["column"]
            if col in lsp_dict and len(lsp_dict[col]) == n_bars:
                new_row[i] = lsp_dict[col][-1]
    except Exception:
        pass
    t_lsp = time.time() - t0

    t0 = time.time()
    try:
        from scripts.algo_line_detector import compute_all_algo_series
        algo_dict = compute_all_algo_series(df_full)
        for i in algo_idx:
            col = expressions[i]["compute"]["column"]
            if col in algo_dict and len(algo_dict[col]) == n_bars:
                new_row[i] = algo_dict[col][-1]
    except Exception:
        pass
    t_algo = time.time() - t0
    result["lsp_algo"] = t_lsp + t_algo

    # Phase 5: Save
    t0 = time.time()
    row_f16 = new_row.astype(np.float16)
    _ = row_f16.tobytes()
    result["save"] = time.time() - t0

    result["total"] = sum(result.values())
    result["n_bars"] = n_bars
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100, help="Number of tickers to benchmark")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for ticker selection")
    args = parser.parse_args()

    print(f"\n{'=' * 70}")
    print(f"  INCREMENTAL APPEND BENCHMARK — {args.n} tickers")
    print(f"{'=' * 70}")

    # Load expression library
    print("\n  Loading expression library...")
    from expr_cache_builder import _load_expressions
    expressions = _load_expressions()
    n_exprs = len(expressions)
    print(f"  {n_exprs} expressions")

    # Classify
    from validate_incremental_append import classify_expression
    state_only_idx = []
    lookback_idx = []
    htf_idx = []
    lsp_idx = []
    algo_idx = []
    lookback_depths = {}

    for i, expr in enumerate(expressions):
        cat, lb, _ = classify_expression(expr)
        if cat == "state_only":
            state_only_idx.append(i)
        elif cat == "lookback":
            lookback_idx.append(i)
            lookback_depths[i] = lb
        elif cat == "htf":
            htf_idx.append(i)
        elif cat == "precomputed_lsp":
            lsp_idx.append(i)
        elif cat == "precomputed_algo":
            algo_idx.append(i)

    max_lb = max(lookback_depths.values()) if lookback_depths else 504

    # Load caches
    print("\n  Loading OHLCV caches...")
    t0 = time.time()
    daily_cache = load_daily_cache()
    weekly_cache = load_htf_cache("weekly")
    monthly_cache = load_htf_cache("monthly")
    print(f"  Loaded in {time.time() - t0:.1f}s")
    print(f"  Daily: {len(daily_cache)} tickers")

    # Pick random tickers that have expr cache files
    all_tickers = [t for t in daily_cache.keys()
                   if os.path.exists(os.path.join(EXPR_CACHE_DIR,
                       t.replace('/', '_').replace('.', '_') + '.npz'))]
    random.seed(args.seed)
    sample = random.sample(all_tickers, min(args.n, len(all_tickers)))
    print(f"  Sampling {len(sample)} tickers from {len(all_tickers)} with expr cache")

    # Run benchmark
    print(f"\n  Running benchmark...")
    results = []
    failed = 0

    for idx, ticker in enumerate(sample):
        df = truncate_to_cache_window(daily_cache[ticker])
        if df is None or len(df) < 50:
            failed += 1
            continue

        weekly_df = truncate_to_cache_window(weekly_cache.get(ticker)) if weekly_cache else None
        monthly_df = truncate_to_cache_window(monthly_cache.get(ticker)) if monthly_cache else None

        r = benchmark_one_ticker(
            ticker, df, weekly_df, monthly_df,
            expressions, n_exprs,
            state_only_idx, lookback_idx, lookback_depths,
            htf_idx, lsp_idx, algo_idx, max_lb)

        if r is not None:
            r["ticker"] = ticker
            results.append(r)

        if (idx + 1) % 25 == 0:
            avg_so_far = np.mean([r["total"] for r in results])
            print(f"    {idx + 1}/{len(sample)} done — avg {avg_so_far:.3f}s/ticker")

    if not results:
        print("  No results!")
        return

    # Aggregate
    phases = ["load_npz", "state", "lookback", "htf", "lsp_algo", "save", "total"]
    avgs = {p: np.mean([r[p] for r in results]) for p in phases}
    medians = {p: np.median([r[p] for r in results]) for p in phases}
    maxes = {p: np.max([r[p] for r in results]) for p in phases}
    mins = {p: np.min([r[p] for r in results]) for p in phases}
    avg_bars = np.mean([r["n_bars"] for r in results])

    print(f"\n{'=' * 70}")
    print(f"  RESULTS — {len(results)} tickers (avg {avg_bars:.0f} bars)")
    print(f"{'=' * 70}")
    print(f"  {'Phase':<16s}  {'Mean':>8s}  {'Median':>8s}  {'Min':>8s}  {'Max':>8s}")
    print(f"  {'─' * 56}")
    for p in phases:
        label = p.replace("_", " ").title()
        if p == "total":
            print(f"  {'─' * 56}")
        print(f"  {label:<16s}  {avgs[p]:8.4f}  {medians[p]:8.4f}  "
              f"{mins[p]:8.4f}  {maxes[p]:8.4f}")

    n_universe = len(all_tickers)
    print(f"\n  Projected wall time (14 workers, {n_universe:,} tickers):")
    print(f"    Mean:   {avgs['total']:.3f}s x {n_universe:,} / 14 = "
          f"{avgs['total'] * n_universe / 14:.0f}s = "
          f"{avgs['total'] * n_universe / 14 / 60:.1f} min")
    print(f"    Median: {medians['total']:.3f}s x {n_universe:,} / 14 = "
          f"{medians['total'] * n_universe / 14:.0f}s = "
          f"{medians['total'] * n_universe / 14 / 60:.1f} min")

    no_lsp = avgs['total'] - avgs['lsp_algo']
    print(f"    Without LSP+algo: {no_lsp:.3f}s x {n_universe:,} / 14 = "
          f"{no_lsp * n_universe / 14:.0f}s = "
          f"{no_lsp * n_universe / 14 / 60:.1f} min")

    print(f"\n  vs full rebuild: ~124 min")
    print(f"  Failed/skipped: {failed}")

    # Show slowest tickers
    by_total = sorted(results, key=lambda r: r["total"], reverse=True)
    print(f"\n  Top 5 slowest tickers:")
    for r in by_total[:5]:
        print(f"    {r['ticker']:8s}  {r['total']:.3f}s  "
              f"({r['n_bars']} bars, lsp+algo={r['lsp_algo']:.3f}s)")

    print(f"\n  Top 5 fastest tickers:")
    for r in by_total[-5:]:
        print(f"    {r['ticker']:8s}  {r['total']:.3f}s  "
              f"({r['n_bars']} bars, lsp+algo={r['lsp_algo']:.3f}s)")

    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    main()
