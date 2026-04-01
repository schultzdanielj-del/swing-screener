"""
Benchmark Incremental Append — Per-ticker cost estimation.

Times each phase of the proposed append approach for a single ticker:
1. Load existing .npz (prev row + lookback rows)
2. State-only expressions: scalar math from prev row + today's OHLCV
3. Lookback expressions: window ops on lookback buffer  
4. HTF expressions: partial candle update from HTF pickles
5. LSP + algo: full detectors on daily OHLCV
6. Save: write one row as raw binary (not .npz rewrite)

Usage:
    python scripts/benchmark_incremental_append.py [--ticker AAPL]
"""

import os
import sys
import time
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


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="AAPL")
    args = parser.parse_args()
    ticker = args.ticker

    print(f"\n{'=' * 70}")
    print(f"  INCREMENTAL APPEND BENCHMARK — {ticker}")
    print(f"{'=' * 70}")

    # ── Load expression library ──
    print("\n  Loading expression library...")
    from expr_cache_builder import _load_expressions, _init_worker, load_ticker_cache
    expressions = _load_expressions()
    n_exprs = len(expressions)
    print(f"  {n_exprs} expressions")

    # ── Classify expressions ──
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

    print(f"  state_only: {len(state_only_idx)}, lookback: {len(lookback_idx)}, "
          f"htf: {len(htf_idx)}, lsp: {len(lsp_idx)}, algo: {len(algo_idx)}")

    # ── Load data ──
    print("\n  Loading OHLCV caches...")
    t0 = time.time()
    daily_cache = load_daily_cache()
    weekly_cache = load_htf_cache("weekly")
    monthly_cache = load_htf_cache("monthly")
    print(f"  Caches loaded in {time.time() - t0:.1f}s")

    df_full = truncate_to_cache_window(daily_cache[ticker])
    n_bars = len(df_full)
    print(f"  {ticker}: {n_bars} bars")

    weekly_df = truncate_to_cache_window(weekly_cache.get(ticker)) if weekly_cache else None
    monthly_df = truncate_to_cache_window(monthly_cache.get(ticker)) if monthly_cache else None

    # Free large caches
    del daily_cache, weekly_cache, monthly_cache

    # ── Phase 0: Load existing .npz ──
    print(f"\n  Phase 0: Load existing .npz...")
    t0 = time.time()
    cached_dates, cached_data = load_ticker_cache(ticker)
    t_load_npz = time.time() - t0
    print(f"  {t_load_npz:.4f}s — shape: {cached_data.shape}")

    # The prev row (what we'd have from previous night)
    prev_row = cached_data[-2, :]  # second to last = yesterday's append result
    # Lookback buffer: last 1260 rows of the cache
    max_lb = max(lookback_depths.values()) if lookback_depths else 504
    lb_start = max(0, cached_data.shape[0] - 1 - max_lb)
    lookback_buf = cached_data[lb_start:-1, :]  # everything except last row
    print(f"  Lookback buffer: {lookback_buf.shape[0]} rows (max depth: {max_lb})")

    # ── Phase 1: State-only expressions ──
    # These just need prev_row values + today's OHLCV for simple arithmetic.
    # In the real append, this is scalar math. Here we simulate by copying
    # prev_row values (since we can't do the actual scalar math without
    # building the forward-propagation engine). The timing measures the
    # overhead of indexing and array allocation.
    print(f"\n  Phase 1: State-only expressions ({len(state_only_idx)})...")
    new_row = np.full(n_exprs, np.nan, dtype=np.float32)
    t0 = time.time()
    # Simulate: for state-only, the cost is reading prev values + a few
    # arithmetic ops. Worst case is something like EMA update:
    #   new_val = alpha * today_close + (1-alpha) * prev_ema
    # That's 3 multiplies + 1 add per expression.
    # Simulate with actual numpy ops on arrays of that size.
    today_close = df_full["close"].iloc[-1]
    for i in state_only_idx:
        # Simulate EMA-like update (worst case for state-only)
        new_row[i] = 0.1 * today_close + 0.9 * prev_row[i]
    t_state = time.time() - t0
    print(f"  {t_state:.4f}s")

    # ── Phase 2: Lookback expressions ──
    # These need to look back into the buffer. Simulate with actual
    # window operations (rolling max, percentile rank, etc.)
    print(f"\n  Phase 2: Lookback expressions ({len(lookback_idx)})...")
    t0 = time.time()
    for i in lookback_idx:
        depth = lookback_depths[i]
        # Get the lookback window from buffer
        buf_len = lookback_buf.shape[0]
        start = max(0, buf_len - depth)
        window = lookback_buf[start:, i]  # column i, last `depth` rows

        # Simulate different ops based on what's common:
        # Most are count_true (rolling sum), since_true (scan back),
        # percentile_rank (comparison), rolling max/min
        op = expressions[i]["compute"].get("op", "")
        if op in ("count_true", "since_true", "true_in_row"):
            # Boolean scan — cheap
            new_row[i] = np.nansum(window > 0)
        elif op == "percentile_rank":
            # Rank current value in window
            val = prev_row[i]
            if len(window) > 0 and not np.isnan(val):
                new_row[i] = np.nansum(window <= val) / len(window) * 100
        elif op in ("extension_ceiling_ratio", "extension_peak_ratio",
                     "bollinger_bandwidth_rank"):
            # Rolling max
            if len(window) > 0:
                new_row[i] = np.nanmax(window)
        elif op in ("aroon_up_val", "aroon_down_val", "aroon_oscillator"):
            # Argmax/argmin
            if len(window) > 0:
                new_row[i] = float(np.nanargmax(window))
        elif op == "cci":
            # Mean deviation
            if len(window) > 0:
                m = np.nanmean(window)
                new_row[i] = np.nanmean(np.abs(window - m))
        else:
            # Generic: rolling sum or similar
            if len(window) > 0:
                new_row[i] = np.nanmean(window)
    t_lookback = time.time() - t0
    print(f"  {t_lookback:.4f}s")

    # ── Phase 3: HTF expressions ──
    # In real append: run partial candle engine on HTF pickles for last bar.
    # This builds ExpressionEngine on HTF df, extracts closed state,
    # builds partial intermediates for ONE daily bar, dispatches.
    # Simulate by running the actual partial candle path for the last bar only.
    print(f"\n  Phase 3: HTF expressions ({len(htf_idx)})...")
    t0 = time.time()

    if weekly_df is not None and monthly_df is not None:
        from partial_candle_engine import (
            build_partial_candle_mapping, extract_closed_state,
            build_partial_intermediates, dispatch_partial_arith
        )
        from scripts.expression_engine import ExpressionEngine

        htf_time_detail = {}
        for tf_label, htf_df, prefix in [("weekly", weekly_df, "w_"),
                                          ("monthly", monthly_df, "m_")]:
            t_htf0 = time.time()

            # Build engine on closed HTF series
            engine_htf = ExpressionEngine(htf_df)

            # Extract closed state (intermediates + raw arrays)
            closed_im, closed_raw = extract_closed_state(engine_htf, htf_df)

            # Build partial candle mapping for just the last daily bar
            # (In real append, we only need the last bar's partial candle)
            lci, partial, prev_c = build_partial_candle_mapping(
                df_full.iloc[-1:].reset_index(drop=True), htf_df, 
                "W" if tf_label == "weekly" else "ME")

            # Build partial intermediates
            im_partial = build_partial_intermediates(
                closed_im, closed_raw, lci, partial, prev_c)

            htf_time_detail[tf_label] = time.time() - t_htf0

        t_htf = time.time() - t0
        print(f"  {t_htf:.4f}s (weekly: {htf_time_detail.get('weekly', 0):.4f}s, "
              f"monthly: {htf_time_detail.get('monthly', 0):.4f}s)")
    else:
        t_htf = 0
        print(f"  SKIPPED — no HTF caches")

    # ── Phase 4: LSP + Algo ──
    print(f"\n  Phase 4: LSP ({len(lsp_idx)}) + Algo ({len(algo_idx)})...")
    t0 = time.time()
    from scripts.lsp_detector_v2 import compute_all_lsp_series
    lsp_dict = compute_all_lsp_series(df_full)
    t_lsp = time.time() - t0

    t0 = time.time()
    from scripts.algo_line_detector import compute_all_algo_series
    algo_dict = compute_all_algo_series(df_full)
    t_algo = time.time() - t0

    # Extract last-bar values
    for i in lsp_idx:
        col = expressions[i]["compute"]["column"]
        if col in lsp_dict and len(lsp_dict[col]) == n_bars:
            new_row[i] = lsp_dict[col][-1]
    for i in algo_idx:
        col = expressions[i]["compute"]["column"]
        if col in algo_dict and len(algo_dict[col]) == n_bars:
            new_row[i] = algo_dict[col][-1]

    t_precomputed = t_lsp + t_algo
    print(f"  {t_precomputed:.4f}s (LSP: {t_lsp:.4f}s, Algo: {t_algo:.4f}s)")

    # ── Phase 5: Save ──
    # In real append: write 1 row as raw binary float16 (~31 KB)
    # NOT rewriting the full .npz
    print(f"\n  Phase 5: Save (raw binary append)...")
    t0 = time.time()
    row_f16 = new_row.astype(np.float16)
    test_path = os.path.join(CACHE_DIR, "_benchmark_append_test.bin")
    with open(test_path, "wb") as f:
        f.write(row_f16.tobytes())
    t_save = time.time() - t0
    row_bytes = len(row_f16.tobytes())
    os.remove(test_path)
    print(f"  {t_save:.4f}s ({row_bytes:,} bytes = {row_bytes/1024:.1f} KB)")

    # ── Summary ──
    t_total = t_load_npz + t_state + t_lookback + t_htf + t_precomputed + t_save
    print(f"\n{'=' * 70}")
    print(f"  PER-TICKER COST BREAKDOWN")
    print(f"{'=' * 70}")
    print(f"  Load .npz:         {t_load_npz:8.4f}s")
    print(f"  State-only:        {t_state:8.4f}s  ({len(state_only_idx)} exprs)")
    print(f"  Lookback:          {t_lookback:8.4f}s  ({len(lookback_idx)} exprs)")
    print(f"  HTF:               {t_htf:8.4f}s  ({len(htf_idx)} exprs)")
    print(f"  LSP + Algo:        {t_precomputed:8.4f}s  ({len(lsp_idx) + len(algo_idx)} exprs)")
    print(f"  Save:              {t_save:8.4f}s")
    print(f"  ─────────────────────────────")
    print(f"  TOTAL:             {t_total:8.4f}s")

    print(f"\n  Projected wall time (14 workers, 11,201 tickers):")
    print(f"  Total:  {t_total:.2f}s × 11,201 / 14 = "
          f"{t_total * 11201 / 14:.0f}s = {t_total * 11201 / 14 / 60:.1f} min")

    # Without LSP+algo (for comparison)
    t_no_precomputed = t_total - t_precomputed
    print(f"  Without LSP+algo: {t_no_precomputed:.2f}s × 11,201 / 14 = "
          f"{t_no_precomputed * 11201 / 14:.0f}s = "
          f"{t_no_precomputed * 11201 / 14 / 60:.1f} min")

    print(f"\n  vs full rebuild: ~124 min")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
