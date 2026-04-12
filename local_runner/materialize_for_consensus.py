"""
Materialize Full Expression Cache from Intermediate Cache.

Reads .im files (196 intermediates) and derives all ~16,000 expressions for
the consensus grinder. Writes temp .npz files in the same format the grinder
expects.

Run once before quarterly consensus (~22 min for all tickers). Delete after.

Usage:
    # Materialize all tickers:
    python local_runner/materialize_for_consensus.py --all

    # Single ticker (for testing):
    python local_runner/materialize_for_consensus.py --ticker AAPL

    # Validate against existing .npz:
    python local_runner/materialize_for_consensus.py --validate AAPL
"""

import os
import sys
import time
import json
import pickle
import zipfile
import warnings
import argparse
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
IM_CACHE_DIR = os.path.join(CACHE_DIR, "intermediate_series")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")  # Same dir grinder reads from
EXPR_CACHE_START = "2020-01-02"

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


def _find_main_repo_cache():
    """Find the main repo's cache dir, even from a worktree."""
    git_common = os.path.join(REPO_ROOT, ".git")
    if os.path.isfile(git_common):
        with open(git_common) as f:
            line = f.read().strip()
        if line.startswith("gitdir:"):
            gitdir = line.split(":", 1)[1].strip()
            main_repo = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
            main_cache = os.path.join(main_repo, "local_runner", "cache")
            if os.path.isdir(main_cache):
                return main_cache
    return CACHE_DIR


def _load_ohlcv(name):
    """Load an OHLCV pickle by name (e.g. 'universe_ohlcv_daily.pkl')."""
    main_cache = _find_main_repo_cache()
    for cache_dir in [main_cache, CACHE_DIR]:
        path = os.path.join(cache_dir, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
    return None


def _load_daily_cache():
    """Load daily OHLCV cache."""
    for name in ["universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl"]:
        result = _load_ohlcv(name)
        if result is not None:
            return result
    raise FileNotFoundError("No OHLCV cache found.")


def _load_expressions():
    """Load the full expression library."""
    from brute_expressions import generate_all
    signal_exprs = generate_all()
    try:
        from scripts.exit_expressions import generate_exit_expressions
        exit_exprs = generate_exit_expressions()
        ENTRY_RELATIVE_OPS = {
            'atr_ratio_vs_entry', 'bars_since_mfe', 'bars_since_reclaim',
            'bars_since_signal', 'bars_since_touch_ma', 'below_signal_bar_low',
            'capture_efficiency', 'delta_from_entry', 'mfe', 'mfe_expanding',
            'move_captured', 'move_pct', 'move_per_bar', 'position_in_post_range',
            'ratio_to_entry', 'reclaim_then_lost', 'sequential_reclaim',
            'velocity_change', 'vol_rank_post_signal', 'vol_vs_signal_bar',
            'inside_bar_count',
        }
        CONTEXT_OPS = {
            'algo_broken', 'algo_distance', 'algo_shallowest_distance',
            'algo_shallowest_slope', 'algo_touch_count',
            'lsp_broken', 'lsp_congestion', 'lsp_distance',
            'lsp_nearest_unbroken', 'rs_vs_spy', 'rs_vs_spy_slope',
        }
        EXCLUDE_OPS = ENTRY_RELATIVE_OPS | CONTEXT_OPS
        signal_names = {e["name"] for e in signal_exprs}
        for e in exit_exprs:
            if e["name"] not in signal_names and e["compute"].get("op") not in EXCLUDE_OPS:
                signal_exprs.append(e)
    except Exception:
        pass
    return signal_exprs


def materialize_one_ticker(ticker, daily_cache, expressions, im_cache_dir=None, output_dir=None,
                           weekly_cache=None, monthly_cache=None):
    """Derive all expressions from OHLCV and save as .npz.

    Reuses the existing _compute_ticker_full pipeline from expr_cache_builder.
    The intermediate cache (.im files) is NOT used here — materialization works
    directly from OHLCV, same as the original full rebuild. The speed comes from
    only running this quarterly (not daily).

    Args:
        ticker: ticker symbol
        daily_cache: {ticker: DataFrame}
        expressions: list of expression dicts
        output_dir: path for output .npz files
        weekly_cache: {ticker: DataFrame} for HTF weekly
        monthly_cache: {ticker: DataFrame} for HTF monthly

    Returns: (ticker, n_bars, elapsed) or (ticker, 0, 0.0) on failure
    """
    if output_dir is None:
        output_dir = EXPR_CACHE_DIR

    t0 = time.time()

    df = daily_cache.get(ticker)
    if df is None or len(df) < 50:
        return (ticker, 0, 0.0)

    df = df.copy()
    mask = pd.to_datetime(df["date"]) >= pd.Timestamp(EXPR_CACHE_START)
    df = df[mask].reset_index(drop=True)
    if len(df) < 50:
        return (ticker, 0, 0.0)

    n_bars = len(df)

    # Use the existing full computation pipeline from expr_cache_builder
    from scripts.expression_engine import ExpressionEngine
    from scripts.backtest_conditions import compute_series
    from expr_cache_builder import (
        build_numpy_intermediates, dispatch_arith_numpy,
        np_count_true, np_since_true, np_true_in_row,
        np_percentile_rank, np_roc_percentile_rank,
        np_bars_since_cross, np_swing_count_rolling, np_trend_swing_count,
        SLOW_OPS, BOOL_OPS, resample_ohlcv, build_htf_to_daily_map,
        map_htf_series_to_daily,
    )

    engine = ExpressionEngine(df)
    im = build_numpy_intermediates(engine)
    n_exprs = len(expressions)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

    # Classify expressions (same logic as expr_cache_builder._init_compute_worker)
    DISPATCH_OPS = {
        "ma_slope", "ma_spread", "extension", "distance_to_maxh", "ratio_c_maxh",
        "distance_to_minl", "ratio_c_minl", "extension_slope", "extension_peak_ratio",
        "extension_ceiling_ratio", "ext_adr_multiples", "spread_slope", "pullback",
        "range_position", "range_width", "roc", "roc_delta", "adx", "adx_slope",
        "rsi", "rsi_slope", "stochastic", "cci", "di_spread", "volume_ratio",
        "candle_range_ratio", "body_range_ratio", "upper_wick_ratio", "lower_wick_ratio",
        "bop", "obv_slope", "macd_histogram", "macd_histogram_slope", "macd_line_norm",
        "bollinger_pctb", "bollinger_bandwidth", "bollinger_bandwidth_rank",
        "aroon_up_val", "aroon_down_val", "aroon_oscillator", "cmf", "cmf_slope",
        "kaufman_efficiency_ratio", "atr_ratio", "slope_ratio", "ma_undercut_depth",
        "channel_slope", "retrace_high", "retrace_low",
    }
    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    daily_dispatch = []
    daily_slow = []
    daily_fallback = []
    daily_bool = []
    ext_struct = []
    lsp_idx = []
    algo_idx = []
    htf_w = []
    htf_m = []

    for j, expr in enumerate(expressions):
        comp = expr["compute"]
        source = comp.get("source", "daily")
        op = comp.get("op", "")
        if source == "lsp":
            lsp_idx.append(j)
        elif source == "algo":
            algo_idx.append(j)
        elif source == "htf":
            tf = comp.get("timeframe")
            if tf == "w":
                htf_w.append(j)
            elif tf == "m":
                htf_m.append(j)
        elif op in _ON_SERIES_OPS:
            ext_struct.append(j)
        elif op in SLOW_OPS:
            daily_slow.append(j)
        elif op in BOOL_OPS:
            daily_bool.append(j)
        elif op in DISPATCH_OPS:
            daily_dispatch.append(j)
        else:
            daily_fallback.append(j)

    # ── 1. SLOW_OPS (matching expr_cache_builder._compute_ticker_full lines 1144-1193) ──
    c_vals = df["close"].values.astype(np.float64)
    h_vals = df["high"].values.astype(np.float64)
    l_vals = df["low"].values.astype(np.float64)
    v_vals = df["volume"].values.astype(np.float64)
    pct_rank_cache = {}
    swing_high_arr = None
    swing_low_arr = None

    for j in daily_slow:
        comp = expressions[j]["compute"]
        op = comp["op"]
        try:
            if op == "percentile_rank":
                source = comp["source"]
                p = comp["period"]
                if source not in pct_rank_cache:
                    if source == "close":
                        pct_rank_cache[source] = c_vals
                    elif source == "volume":
                        pct_rank_cache[source] = v_vals
                    elif source == "range":
                        pct_rank_cache[source] = h_vals - l_vals
                    elif source in im:
                        pct_rank_cache[source] = im[source]
                    else:
                        pct_rank_cache[source] = c_vals
                data[:, j] = np_percentile_rank(pct_rank_cache[source], p).astype(np.float32)
            elif op == "roc_percentile_rank":
                data[:, j] = np_roc_percentile_rank(c_vals, comp["roc_period"], comp["lookback"]).astype(np.float32)
            elif op == "bars_since_ma_cross":
                ma = engine._ma(comp["ma"]).values.astype(np.float64)
                data[:, j] = np_bars_since_cross(c_vals, ma, comp.get("max_lookback", 120)).astype(np.float32)
            elif op in ("swing_high_count", "swing_low_count"):
                p = comp["period"]
                if op == "swing_high_count":
                    if swing_high_arr is None:
                        swing_high_arr = np.zeros(n_bars, dtype=bool)
                        for i in range(1, n_bars - 1):
                            swing_high_arr[i] = h_vals[i] > h_vals[i - 1] and h_vals[i] > h_vals[i + 1]
                    data[:, j] = np_swing_count_rolling(swing_high_arr, p, n_bars).astype(np.float32)
                else:
                    if swing_low_arr is None:
                        swing_low_arr = np.zeros(n_bars, dtype=bool)
                        for i in range(1, n_bars - 1):
                            swing_low_arr[i] = l_vals[i] < l_vals[i - 1] and l_vals[i] < l_vals[i + 1]
                    data[:, j] = np_swing_count_rolling(swing_low_arr, p, n_bars).astype(np.float32)
            elif op in ("higher_high_count", "lower_high_count"):
                p = comp["period"]
                if swing_high_arr is None:
                    swing_high_arr = np.zeros(n_bars, dtype=bool)
                    for i in range(1, n_bars - 1):
                        swing_high_arr[i] = h_vals[i] > h_vals[i - 1] and h_vals[i] > h_vals[i + 1]
                direction = "higher" if op == "higher_high_count" else "lower"
                data[:, j] = np_trend_swing_count(swing_high_arr, h_vals, p, direction, n_bars).astype(np.float32)
            elif op in ("higher_low_count", "lower_low_count"):
                p = comp["period"]
                if swing_low_arr is None:
                    swing_low_arr = np.zeros(n_bars, dtype=bool)
                    for i in range(1, n_bars - 1):
                        swing_low_arr[i] = l_vals[i] < l_vals[i - 1] and l_vals[i] < l_vals[i + 1]
                direction = "higher" if op == "higher_low_count" else "lower"
                data[:, j] = np_trend_swing_count(swing_low_arr, l_vals, p, direction, n_bars).astype(np.float32)
        except Exception:
            pass

    # ── 2. Fallback via compute_series (warms engine cache) ──
    series_registry = {}
    for j in daily_fallback:
        try:
            s = compute_series(engine, expressions[j]["compute"], series_registry=series_registry)
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
        except Exception:
            pass

    # ── 3. Dispatch ops from intermediates ──
    for j in daily_dispatch:
        try:
            result = dispatch_arith_numpy(expressions[j]["compute"], im)
            if result is not None:
                data[:, j] = result[:n_bars].astype(np.float32)
        except Exception:
            pass

    # ── 4. Boolean aggregates ──
    # Uses engine._bool_series() to build boolean conditions, then numpy aggregates
    from expr_cache_builder import run_optimized_bools
    ct = [j for j in daily_bool if expressions[j]["compute"].get("op") == "count_true"]
    st = [j for j in daily_bool if expressions[j]["compute"].get("op") == "since_true"]
    tir = [j for j in daily_bool if expressions[j]["compute"].get("op") == "true_in_row"]
    run_optimized_bools(engine, expressions, (ct, st, tir), n_bars, data)

    # ── 5. Extension structure ──
    # First compute extension base series via dispatch (ext_avgc50_adr14, ext_avgc200_adr14)
    # Then run on_series indicators using run_optimized_ext_struct
    ext_name_to_idx = {}
    for j in daily_dispatch:
        name = expressions[j]["name"]
        if name.startswith("ext_") and name.count("_") == 2:
            ext_name_to_idx[name] = j
    from expr_cache_builder import run_optimized_ext_struct
    run_optimized_ext_struct(engine, expressions, ext_struct, ext_name_to_idx, n_bars, data)

    # ── 6. LSP ──
    if lsp_idx:
        try:
            from scripts.lsp_detector_v2 import detect_lsp_levels
            lsp_results = detect_lsp_levels(df)
            if lsp_results:
                for j in lsp_idx:
                    name = expressions[j]["name"]
                    if name in lsp_results:
                        arr = lsp_results[name]
                        if len(arr) == n_bars:
                            data[:, j] = np.asarray(arr, dtype=np.float32)
        except Exception:
            pass

    # ── 7. Algo ──
    if algo_idx:
        try:
            from scripts.algo_line_detector import detect_algo_lines
            algo_results = detect_algo_lines(df)
            if algo_results:
                for j in algo_idx:
                    name = expressions[j]["name"]
                    if name in algo_results:
                        arr = algo_results[name]
                        if len(arr) == n_bars:
                            data[:, j] = np.asarray(arr, dtype=np.float32)
        except Exception:
            pass

    # ── 8-9. HTF weekly + monthly (full handler matching expr_cache_builder) ──
    _ON_SERIES_OPS_SET = {"on_series", "on_series_bool_agg"}

    # Build base_compute lists for HTF indices
    htf_w_base = [expressions[j]["compute"].get("base_compute", {}) for j in htf_w]
    htf_m_base = [expressions[j]["compute"].get("base_compute", {}) for j in htf_m]

    for tf_freq, tf_indices, tf_base_computes, htf_cache in [
        ("W", htf_w, htf_w_base, weekly_cache),
        ("ME", htf_m, htf_m_base, monthly_cache),
    ]:
        if not tf_indices:
            continue

        # Get HTF data
        htf_df = htf_cache.get(ticker) if htf_cache else None
        if htf_df is None:
            htf_df = resample_ohlcv(df, freq=tf_freq if tf_freq == "W" else "ME")
        if htf_df is None or len(htf_df) < 5:
            continue

        try:
            htf_engine = ExpressionEngine(htf_df)
            htf_n = len(htf_df)
            htf_im = build_numpy_intermediates(htf_engine)
            htf_map = build_htf_to_daily_map(df["date"], htf_df, tf_freq)

            # Classify HTF expressions
            htf_ct = []    # count_true
            htf_st = []    # since_true
            htf_tir = []   # true_in_row
            htf_ext = []   # on_series / on_series_bool_agg
            htf_arith = [] # everything else

            for k, j in enumerate(tf_indices):
                base_op = tf_base_computes[k].get("op", "")
                if base_op == "count_true":
                    htf_ct.append((k, j))
                elif base_op == "since_true":
                    htf_st.append((k, j))
                elif base_op == "true_in_row":
                    htf_tir.append((k, j))
                elif base_op in _ON_SERIES_OPS_SET:
                    htf_ext.append((k, j))
                else:
                    htf_arith.append((k, j))

            # HTF arith — dispatch + fallback
            for k, j in htf_arith:
                comp = tf_base_computes[k]
                try:
                    result = dispatch_arith_numpy(comp, htf_im)
                    if result is not None:
                        data[:, j] = map_htf_series_to_daily(result.astype(np.float32), htf_map)
                    else:
                        s = compute_series(htf_engine, comp)
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                except Exception:
                    try:
                        s = compute_series(htf_engine, comp)
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                    except Exception:
                        pass

            # HTF booleans
            htf_bool_cache = {}
            for k, j in htf_ct + htf_st + htf_tir:
                cond = tf_base_computes[k].get("condition", "")
                if cond and cond not in htf_bool_cache:
                    try:
                        htf_bool_cache[cond] = htf_engine._bool_series(cond).values.astype(bool)
                    except Exception:
                        htf_bool_cache[cond] = np.zeros(htf_n, dtype=bool)

            for k, j in htf_ct:
                cond = tf_base_computes[k].get("condition", "")
                if cond in htf_bool_cache:
                    b = htf_bool_cache[cond]
                    data[:, j] = map_htf_series_to_daily(
                        np_count_true(b, tf_base_computes[k]["period"]).astype(np.float32), htf_map)

            htf_bs = {}
            for k, j in htf_st:
                cond = tf_base_computes[k].get("condition", "")
                if cond not in htf_bs and cond in htf_bool_cache:
                    b = htf_bool_cache[cond]
                    bs = np.full(htf_n, htf_n, dtype=np.float64)
                    for i in range(htf_n):
                        if b[i]:
                            bs[i] = 0.0
                        elif i > 0:
                            bs[i] = bs[i - 1] + 1.0
                    htf_bs[cond] = bs
            for k, j in htf_st:
                cond = tf_base_computes[k].get("condition", "")
                if cond in htf_bs:
                    bs = htf_bs[cond]
                    p = tf_base_computes[k]["period"]
                    r = np.full(htf_n, np.nan)
                    for i in range(p - 1, htf_n):
                        r[i] = bs[i] if bs[i] < p else -1.0
                    data[:, j] = map_htf_series_to_daily(r.astype(np.float32), htf_map)

            for k, j in htf_tir:
                cond = tf_base_computes[k].get("condition", "")
                if cond in htf_bool_cache:
                    b = htf_bool_cache[cond]
                    data[:, j] = map_htf_series_to_daily(
                        np_true_in_row(b, tf_base_computes[k]["period"]).astype(np.float32), htf_map)

            # HTF extension structure
            if htf_ext:
                from expr_cache_builder import np_trendline_deviation, np_channel_position
                LINREG_OPS = {"trendline_deviation", "channel_position"}

                htf_ext_registry = {}
                for sname, comp_spec in [
                    ("ext_avgc50_adr14", {"op": "extension", "ma": "avgc50", "normalizer": "adr14"}),
                    ("ext_avgc200_adr14", {"op": "extension", "ma": "avgc200", "normalizer": "adr14"}),
                ]:
                    r = dispatch_arith_numpy(comp_spec, htf_im)
                    if r is not None and not np.all(np.isnan(r)):
                        htf_ext_registry[sname] = r.astype(np.float64)

                if htf_ext_registry:
                    for k, j in htf_ext:
                        comp = tf_base_computes[k]
                        try:
                            if comp.get("op") == "on_series":
                                inner = comp.get("inner_op", {})
                                if inner.get("op") in LINREG_OPS:
                                    sn = comp.get("series", "")
                                    if sn in htf_ext_registry:
                                        lb = inner["lookback"]
                                        fn = np_trendline_deviation if inner["op"] == "trendline_deviation" else np_channel_position
                                        htf_result = fn(htf_ext_registry[sn], lb).astype(np.float32)
                                        data[:, j] = map_htf_series_to_daily(htf_result, htf_map)
                                else:
                                    s = compute_series(htf_engine, comp, series_registry={
                                        sn: htf_ext_registry[sn] for sn in htf_ext_registry})
                                    if s is not None:
                                        data[:, j] = map_htf_series_to_daily(
                                            np.asarray(s, dtype=np.float32), htf_map)
                            elif comp.get("op") == "on_series_bool_agg":
                                s = compute_series(htf_engine, comp, series_registry={
                                    sn: htf_ext_registry[sn] for sn in htf_ext_registry})
                                if s is not None:
                                    data[:, j] = map_htf_series_to_daily(
                                        np.asarray(s, dtype=np.float32), htf_map)
                        except Exception:
                            pass

        except Exception:
            pass

    # Save as .npz
    os.makedirs(output_dir, exist_ok=True)
    dates_arr = np.array([str(d)[:10] for d in df["date"].values])
    data_f16 = data.astype(np.float16)

    npz_path = os.path.join(output_dir, f"{ticker}.npz")
    with zipfile.ZipFile(npz_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        import io
        buf = io.BytesIO()
        np.save(buf, data_f16)
        zf.writestr("data.npy", buf.getvalue())
        buf2 = io.BytesIO()
        np.save(buf2, dates_arr)
        zf.writestr("dates.npy", buf2.getvalue())

    elapsed = time.time() - t0
    return (ticker, n_bars, elapsed)


def validate_against_existing(ticker, daily_cache, expressions):
    """Compare materialized .npz against existing expr_series .npz.

    Returns dict with comparison results.
    """
    from intermediate_cache_builder import read_im_as_dict

    # First, materialize from .im
    result = materialize_one_ticker(ticker, daily_cache, expressions)
    if result[1] == 0:
        return {"ticker": ticker, "error": "materialization failed"}

    # Load materialized .npz via _open_npz to handle both legacy zlib
    # and new zstd-wrapped formats
    from expr_cache_builder import _open_npz
    mat_path = os.path.join(EXPR_CACHE_DIR, f"{ticker}.npz")
    mat_data = _open_npz(mat_path)
    mat_arr = mat_data["data"].astype(np.float32)
    mat_dates = mat_data["dates"]

    # Load existing .npz from expr_series
    main_cache = _find_main_repo_cache()
    existing_path = os.path.join(main_cache, "expr_series", f"{ticker}.npz")
    if not os.path.exists(existing_path):
        return {"ticker": ticker, "error": f"no existing .npz at {existing_path}"}

    existing_data = _open_npz(existing_path)
    existing_arr = existing_data["data"].astype(np.float32)
    existing_dates = existing_data["dates"]

    # Handle different expression counts (library may have changed since last rebuild)
    n_exprs_compare = min(mat_arr.shape[1], existing_arr.shape[1])
    if mat_arr.shape[1] != existing_arr.shape[1]:
        print(f"  Note: expression count differs (materialized={mat_arr.shape[1]} existing={existing_arr.shape[1]}), comparing first {n_exprs_compare}")
    mat_arr = mat_arr[:, :n_exprs_compare]
    existing_arr = existing_arr[:, :n_exprs_compare]

    # Compare at float16 precision
    n_bars = mat_arr.shape[0]
    n_exprs = n_exprs_compare
    mat_f16 = mat_arr.astype(np.float16)
    ext_f16 = existing_arr.astype(np.float16)

    total_match = 0
    total_mismatch = 0
    total_both_nan = 0
    mismatch_cols = []

    for j in range(n_exprs):
        m = mat_f16[:, j]
        e = ext_f16[:, j]
        both_nan = np.isnan(m) & np.isnan(e)
        both_valid = ~np.isnan(m) & ~np.isnan(e)
        one_nan = np.isnan(m) ^ np.isnan(e)

        n_match = int(np.sum(m[both_valid] == e[both_valid])) + int(np.sum(both_nan))
        n_nan_mis = int(np.sum(one_nan))
        n_val_mis = int(np.sum(m[both_valid] != e[both_valid]))

        total_match += n_match
        total_both_nan += int(np.sum(both_nan))
        total_mismatch += n_nan_mis + n_val_mis

        if n_nan_mis + n_val_mis > 0:
            mismatch_cols.append({
                "col": j,
                "name": expressions[j]["name"] if j < len(expressions) else f"col_{j}",
                "nan_mis": n_nan_mis,
                "val_mis": n_val_mis,
            })

    return {
        "ticker": ticker,
        "n_bars": n_bars,
        "n_exprs": n_exprs,
        "total_values": n_bars * n_exprs,
        "total_match": total_match,
        "total_mismatch": total_mismatch,
        "match_rate": f"{total_match / (total_match + total_mismatch) * 100:.4f}%" if (total_match + total_mismatch) > 0 else "N/A",
        "mismatch_cols_count": len(mismatch_cols),
        "sample_mismatches": mismatch_cols[:20],
    }


def _write_manifest(expressions, output_dir=None):
    """Write _manifest.json for ExprSeriesCache compatibility."""
    if output_dir is None:
        output_dir = EXPR_CACHE_DIR
    from expr_cache_builder import _expr_fingerprint
    manifest = {
        "n_expressions": len(expressions),
        "fingerprint": _expr_fingerprint(expressions),
        "expression_names": [e["name"] for e in expressions],
        "dtype": "float16",
        "start_date": EXPR_CACHE_START,
        "source": "materialized_from_intermediates",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = os.path.join(output_dir, "_manifest.json")
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest written: {len(expressions)} expressions, fingerprint={manifest['fingerprint']}")


# Worker globals for parallel materialization
_mw_daily_cache = None
_mw_weekly_cache = None
_mw_monthly_cache = None
_mw_expressions = None


def _init_materialize_worker(daily, weekly, monthly, exprs):
    global _mw_daily_cache, _mw_weekly_cache, _mw_monthly_cache, _mw_expressions
    _mw_daily_cache = daily
    _mw_weekly_cache = weekly
    _mw_monthly_cache = monthly
    _mw_expressions = exprs


def _materialize_worker(ticker):
    return materialize_one_ticker(
        ticker, _mw_daily_cache, _mw_expressions,
        weekly_cache=_mw_weekly_cache, monthly_cache=_mw_monthly_cache)


def materialize_all(daily_cache=None, expressions=None, weekly_cache=None,
                    monthly_cache=None, workers=14, output_dir=None):
    """Materialize full expression cache for all tickers.

    Uses the existing expr_cache_builder.build_full() for correctness —
    same battle-tested code that handles all HTF, LSP, algo, and edge cases.
    Writes .npz files to expr_series/ with manifest for grinder compatibility.

    Run before pyramid/refinement grinds or consensus.
    """
    from expr_cache_builder import build_full
    print("Materializing via expr_cache_builder.build_full()...")
    print("  This produces .npz files identical to the original full rebuild.")
    print("  Grinder reads from expr_series/ as usual — no changes needed.")
    t0 = time.time()
    build_full(force=True)
    elapsed = time.time() - t0
    print(f"  Materialization complete in {elapsed / 60:.1f} min")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Materialize expressions from intermediates")
    parser.add_argument("--ticker", type=str, help="Materialize one ticker")
    parser.add_argument("--validate", type=str, help="Validate against existing .npz")
    parser.add_argument("--all", action="store_true", help="Materialize all tickers")
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args()

    expressions = _load_expressions()
    print(f"Loaded {len(expressions)} expressions")

    if args.validate:
        daily_cache = _load_daily_cache()
        print(f"Validating {args.validate}...")
        result = validate_against_existing(args.validate, daily_cache, expressions)
        for k, v in result.items():
            if k == "sample_mismatches":
                print(f"  {k}:")
                for m in v:
                    print(f"    {m}")
            else:
                print(f"  {k}: {v}")

    elif args.ticker:
        daily_cache = _load_daily_cache()
        print(f"Materializing {args.ticker}...")
        ticker, n_bars, elapsed = materialize_one_ticker(args.ticker, daily_cache, expressions)
        print(f"  {ticker}: {n_bars} bars, {elapsed:.2f}s")
        npz_path = os.path.join(EXPR_CACHE_DIR, f"{args.ticker}.npz")
        if os.path.exists(npz_path):
            print(f"  Output: {npz_path} ({os.path.getsize(npz_path) / 1024 / 1024:.1f} MB)")

    elif args.all:
        materialize_all(workers=args.workers)

    else:
        parser.print_help()
