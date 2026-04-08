"""
Expression Series Cache Builder — Pre-compute all expression series for all tickers.

Eliminates the ~40 min recompute bottleneck in pyramid_grinder.py.
After building, the grinder loads pre-computed arrays instead of running
compute_series() from scratch.

Storage: One .npz file per ticker in local_runner/cache/expr_series/
  - "data": float16 array (n_bars, n_expressions) — computed in float32, stored as float16
  - "dates": date strings array
  Data is cast back to float32 on load — all consumers see float32 transparently.

  History window: EXPR_CACHE_START (2020-01-02) onward. OHLCV data before this date
  is truncated before computing. ~6 years of history.

Manifest: local_runner/cache/expr_series/_manifest.json
  - expression names + order (to verify cache matches current library)
  - per-ticker bar counts
  - build timestamp
  - dtype and start_date for auditability

Usage:
    # First build (full, ~90 min):
    python local_runner/expr_cache_builder.py --build

    # Nightly append (1 bar per ticker, ~5-8 min):
    python local_runner/expr_cache_builder.py --append

    # Force full rebuild:
    python local_runner/expr_cache_builder.py --build --force

    # Check status:
    python local_runner/expr_cache_builder.py --status

Requires: daily OHLCV cache (universe_ohlcv_daily.pkl)
"""

import os
import sys
import time
import json
import pickle
import hashlib
import argparse
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")
MANIFEST_PATH = os.path.join(EXPR_CACHE_DIR, "_manifest.json")

# Expression cache only stores data from this date onward (~6 years).
# OHLCV data before this is truncated before computing expressions.
# This keeps cache size manageable (~163 GB at float16) and grinder
# scan times reasonable for consensus pipeline (10-15 passes overnight).
EXPR_CACHE_START = "2020-01-02"

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# HTF RESAMPLING HELPERS
# ══════════════════════════════════════════════════════════════

def resample_ohlcv(daily_df, freq='W'):
    """Resample daily OHLCV to weekly ('W') or monthly ('ME').

    Returns a new DataFrame with OHLCV columns + 'date' column.
    Each row represents one week/month period.
    Returns None if too few bars to resample.
    """
    if len(daily_df) < 10:
        return None

    df = daily_df.copy()
    df = df.set_index('date')

    resampled = df.resample(freq).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna(subset=['close'])

    if len(resampled) < 5:
        return None

    resampled = resampled.reset_index()
    resampled.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    return resampled


def build_htf_to_daily_map(daily_dates, htf_df, freq='W'):
    """Build mapping from daily bar index → HTF bar index.

    For each daily bar, find which HTF period it belongs to.
    The HTF value is constant within each period (step function).

    Args:
        daily_dates: pd.Series or array of daily date values (datetime64)
        htf_df: resampled DataFrame with 'date' column (period end dates)
        freq: 'W' for weekly, 'ME' for monthly

    Returns:
        np.array of shape (n_daily_bars,) with HTF bar indices.
        -1 means no HTF bar available (before first HTF period).
    """
    n_daily = len(daily_dates)
    mapping = np.full(n_daily, -1, dtype=np.int32)

    # HTF dates are period-end dates. A daily bar belongs to the HTF period
    # whose end date is >= the daily date and whose start is <= the daily date.
    # Simplest: for each daily bar, find the latest HTF date that is >= daily date.
    # With sorted dates, we can use searchsorted.

    htf_dates = pd.to_datetime(htf_df['date']).values
    daily_dt = pd.to_datetime(daily_dates).values

    # For each daily bar, find the HTF period it falls into.
    # searchsorted gives us the insertion point — the HTF bar at that index
    # is the first one whose date >= daily date (using 'left' side).
    indices = np.searchsorted(htf_dates, daily_dt, side='left')

    # Clamp to valid range
    indices = np.clip(indices, 0, len(htf_dates) - 1)

    # Verify: the HTF date should be >= the daily date
    # If not (daily bar is after last HTF bar), map to last HTF bar
    for i in range(n_daily):
        idx = indices[i]
        if idx < len(htf_dates):
            mapping[i] = idx
        else:
            mapping[i] = len(htf_dates) - 1

    return mapping


def map_htf_series_to_daily(htf_series, htf_to_daily_map):
    """Map an HTF expression series back to daily bar indices.

    Args:
        htf_series: np.array of expression values at HTF frequency
        htf_to_daily_map: np.array from build_htf_to_daily_map()

    Returns:
        np.array of shape (n_daily_bars,) with HTF values stepped to daily.
    """
    n_daily = len(htf_to_daily_map)
    result = np.full(n_daily, np.nan, dtype=np.float32)

    valid = htf_to_daily_map >= 0
    valid_indices = htf_to_daily_map[valid]

    # Clamp to actual HTF series length
    in_range = valid_indices < len(htf_series)
    daily_mask = np.where(valid)[0][in_range]
    htf_indices = valid_indices[in_range]

    if len(daily_mask) > 0:
        result[daily_mask] = htf_series[htf_indices]

    return result


# ══════════════════════════════════════════════════════════════
# EXPRESSION LIBRARY FINGERPRINT
# ══════════════════════════════════════════════════════════════

def _expr_fingerprint(expressions):
    """Hash the expression library to detect changes."""
    # Hash names + compute specs — if either changes, cache is stale
    data = json.dumps(
        [(e["name"], e["compute"]) for e in expressions],
        sort_keys=True
    ).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def _load_expressions():
    """Load full expression library: signal + exit expressions."""
    from brute_expressions import generate_all
    signal_exprs = generate_all()

    # Add generic exit expressions (those that compute per-bar, no entry context)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(LOCAL_DIR)))
        from scripts.exit_expressions import generate_exit_expressions
        exit_exprs = generate_exit_expressions()

        # Only include generic ops (no entry-relative or context-dependent ones)
        ENTRY_RELATIVE_OPS = {
            'atr_ratio_vs_entry', 'bars_since_mfe', 'bars_since_reclaim',
            'bars_since_signal', 'bars_since_touch_ma', 'below_signal_bar_low',
            'capture_efficiency', 'delta_from_entry', 'mfe', 'mfe_expanding',
            'move_captured', 'move_pct', 'move_per_bar', 'position_in_post_range',
            'ratio_to_entry', 'reclaim_then_lost', 'retrace_from_mfe',
            'retrace_from_mfe_pct', 'sequential_reclaim', 'velocity_change',
            'vol_rank_post_signal', 'vol_vs_signal_bar', 'inside_bar_count',
        }
        CONTEXT_OPS = {
            'algo_broken', 'algo_distance', 'algo_shallowest_distance',
            'algo_shallowest_slope', 'algo_touch_count',
            'lsp_broken', 'lsp_congestion', 'lsp_distance',
            'lsp_nearest_unbroken', 'rs_vs_spy', 'rs_vs_spy_slope',
        }
        EXCLUDE_OPS = ENTRY_RELATIVE_OPS | CONTEXT_OPS

        # Deduplicate by name (signal exprs take priority)
        existing_names = {e["name"] for e in signal_exprs}
        added = 0
        for e in exit_exprs:
            if e["compute"]["op"] in EXCLUDE_OPS:
                continue
            if e["name"] not in existing_names:
                signal_exprs.append(e)
                existing_names.add(e["name"])
                added += 1
        if added > 0:
            print(f"  Added {added} generic exit expressions to cache library")
    except ImportError:
        pass  # exit_expressions not available

    return signal_exprs


def _truncate_to_cache_window(df):
    """Truncate a DataFrame to EXPR_CACHE_START onward.

    Returns the truncated DataFrame, or the original if it starts after
    EXPR_CACHE_START. Returns None if no bars remain after truncation.
    """
    if df is None or len(df) == 0:
        return None
    start = pd.Timestamp(EXPR_CACHE_START)
    dates = pd.to_datetime(df["date"])
    mask = dates >= start
    if not mask.any():
        return None
    return df[mask].reset_index(drop=True)


def _load_daily_cache():
    """Load daily OHLCV cache."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py --daily first.")
    with open(path, "rb") as f:
        return pickle.load(f)


def _load_htf_cache(timeframe):
    """Load weekly or monthly HTF OHLCV cache.

    Args:
        timeframe: 'weekly' or 'monthly'

    Returns: dict of {ticker: DataFrame} or None if cache doesn't exist.
    """
    filename = f"universe_ohlcv_{timeframe}.pkl"
    path = os.path.join(CACHE_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def _df_to_dict(df):
    """Convert a DataFrame to a dict of arrays for IPC serialization.

    Returns None if df is None or too short.
    """
    if df is None or len(df) < 5:
        return None
    return {
        "date": df["date"].values,
        "open": df["open"].values,
        "high": df["high"].values,
        "low": df["low"].values,
        "close": df["close"].values,
        "volume": df["volume"].values,
    }


# ══════════════════════════════════════════════════════════════
# MANIFEST
# ══════════════════════════════════════════════════════════════

def load_manifest():
    """Load manifest, return None if doesn't exist."""
    if not os.path.exists(MANIFEST_PATH):
        return None
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(manifest):
    """Save manifest to disk."""
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


# ══════════════════════════════════════════════════════════════
# PER-TICKER COMPUTATION (worker functions)
# ══════════════════════════════════════════════════════════════

_w_expressions = None
_w_daily_indices = None      # indices of daily (non-precomputed) expressions
_w_ext_struct_indices = None # indices of on_series/on_series_bool_agg (2nd pass)
_w_ext_series_name_to_idx = None  # map ext series name → column index in data
_w_lsp_indices = None        # indices of LSP precomputed expressions
_w_algo_indices = None       # indices of algo line precomputed expressions
_w_htf_weekly_indices = None  # indices of weekly HTF expressions
_w_htf_monthly_indices = None # indices of monthly HTF expressions
_w_htf_weekly_base = None    # base compute specs for weekly HTF expressions
_w_htf_monthly_base = None   # base compute specs for monthly HTF expressions

_w_daily_slow_indices = None     # SLOW_OPS (custom numpy)
_w_daily_dispatch_indices = None # ops dispatch_arith_numpy handles
_w_daily_fallback_indices = None # ops that go through compute_series
_w_daily_bool_ct = None          # count_true indices
_w_daily_bool_st = None          # since_true indices
_w_daily_bool_tir = None         # true_in_row indices


def _init_worker(expressions):
    """Initialize worker with expression list and pre-classify indices."""
    global _w_expressions, _w_daily_indices, _w_ext_struct_indices
    global _w_ext_series_name_to_idx
    global _w_lsp_indices, _w_algo_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base
    global _w_daily_slow_indices, _w_daily_dispatch_indices, _w_daily_fallback_indices
    global _w_daily_bool_ct, _w_daily_bool_st, _w_daily_bool_tir

    _w_expressions = expressions

    _w_daily_indices = []
    _w_ext_struct_indices = []
    _w_lsp_indices = []
    _w_algo_indices = []
    _w_htf_weekly_indices = []
    _w_htf_monthly_indices = []
    _w_htf_weekly_base = []
    _w_htf_monthly_base = []

    # Build name→index map for finding extension series columns
    _name_to_idx = {}
    for j, expr in enumerate(expressions):
        _name_to_idx[expr["name"]] = j

    # Map extension series names to their column indices
    _w_ext_series_name_to_idx = {}
    for series_name in ["ext_avgc50_adr14", "ext_avgc200_adr14"]:
        if series_name in _name_to_idx:
            _w_ext_series_name_to_idx[series_name] = _name_to_idx[series_name]

    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    for j, expr in enumerate(expressions):
        compute = expr["compute"]
        if compute.get("op") == "precomputed":
            source = compute.get("source")
            if source == "lsp":
                _w_lsp_indices.append(j)
            elif source == "algo":
                _w_algo_indices.append(j)
            elif source == "htf":
                tf = compute.get("timeframe")
                if tf == "w":
                    _w_htf_weekly_indices.append(j)
                    _w_htf_weekly_base.append(compute.get("base_compute"))
                elif tf == "m":
                    _w_htf_monthly_indices.append(j)
                    _w_htf_monthly_base.append(compute.get("base_compute"))
        elif compute.get("op") in _ON_SERIES_OPS:
            _w_ext_struct_indices.append(j)
        else:
            _w_daily_indices.append(j)

    # Pre-classify daily indices into slow/dispatch/fallback/bool
    # This runs once per worker init, not per ticker
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

    _w_daily_slow_indices = []
    _w_daily_dispatch_indices = []
    _w_daily_fallback_indices = []
    _w_daily_bool_ct = []
    _w_daily_bool_st = []
    _w_daily_bool_tir = []

    for j in _w_daily_indices:
        op = expressions[j]["compute"].get("op", "")
        if op in SLOW_OPS:
            _w_daily_slow_indices.append(j)
        elif op in BOOL_OPS:
            if op == "count_true": _w_daily_bool_ct.append(j)
            elif op == "since_true": _w_daily_bool_st.append(j)
            elif op == "true_in_row": _w_daily_bool_tir.append(j)
        elif op in DISPATCH_OPS:
            _w_daily_dispatch_indices.append(j)
        else:
            _w_daily_fallback_indices.append(j)


# ══════════════════════════════════════════════════════════════
# OPTIMIZED NUMPY HELPERS (from benchmark v9c)
# ══════════════════════════════════════════════════════════════

BOOL_OPS = {"count_true", "since_true", "true_in_row"}

SLOW_OPS = {"percentile_rank", "roc_percentile_rank", "bars_since_ma_cross",
            "swing_high_count", "swing_low_count",
            "higher_high_count", "higher_low_count", "lower_high_count", "lower_low_count"}


def np_count_true(bool_arr, period):
    n = len(bool_arr)
    if period > n:
        return np.full(n, np.nan)
    f = bool_arr.astype(np.float64)
    result = np.full(n, np.nan)
    cs = np.cumsum(f)
    result[period - 1] = cs[period - 1]
    result[period:] = cs[period:] - cs[:-period]
    return result


def np_since_true(bool_arr, period):
    n = len(bool_arr)
    if period > n:
        return np.full(n, np.nan)
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
    if n == 0:
        return np.zeros(0, dtype=np.float64)
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


def _safe_div(a, b):
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(b != 0, a / b, np.nan)
    result[np.isnan(a) | np.isnan(b)] = np.nan
    return result


def _shift(arr, n):
    result = np.full_like(arr, np.nan)
    if n > 0 and n < len(arr):
        result[n:] = arr[:-n]
    return result


def _rolling_max(arr, period):
    return pd.Series(arr).rolling(period, min_periods=period).max().values


def _rolling_min(arr, period):
    return pd.Series(arr).rolling(period, min_periods=period).min().values


def _rolling_sum(arr, period):
    n = len(arr)
    result = np.full(n, np.nan)
    cs = np.nancumsum(arr)
    result[period - 1] = cs[period - 1]
    if period < n:
        result[period:] = cs[period:] - cs[:-period]
    return result


def _rolling_mean(arr, period):
    s = _rolling_sum(arr, period)
    return s / period


def np_percentile_rank(arr, period):
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(arr)
    result = np.full(n, np.nan)
    if n < 2:
        return result
    for i in range(1, min(period - 1, n)):
        window = arr[:i + 1]
        if np.any(np.isnan(window)):
            continue
        result[i] = np.sum(window <= arr[i]) / len(window) * 100.0
    if n >= period:
        windows = sliding_window_view(arr, period)
        last_vals = windows[:, -1:]
        counts = np.sum(windows <= last_vals, axis=1).astype(np.float64)
        vals = counts / period * 100.0
        nan_mask = np.any(np.isnan(windows), axis=1)
        vals[nan_mask] = np.nan
        result[period - 1:] = vals
    return result


def np_roc_percentile_rank(close, roc_period, lookback):
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(close)
    result = np.full(n, np.nan)
    roc = np.full(n, np.nan)
    roc[roc_period:] = (close[roc_period:] / close[:-roc_period] - 1.0) * 100.0
    start = roc_period + lookback - 1
    if start >= n:
        return result
    roc_valid = roc[roc_period:]
    if len(roc_valid) < lookback:
        return result
    windows = sliding_window_view(roc_valid, lookback)
    last_vals = windows[:, -1:]
    lt_count = np.sum(windows < last_vals, axis=1).astype(np.float64)
    eq_count = np.sum(windows == last_vals, axis=1).astype(np.float64)
    rank = lt_count + (eq_count + 1.0) / 2.0
    pct = rank / lookback
    nan_mask = np.any(np.isnan(windows), axis=1)
    pct[nan_mask] = np.nan
    result[start:] = pct
    return result


def np_bars_since_cross(close, ma, max_lb):
    n = len(close)
    above = close > ma
    result = np.full(n, float(max_lb))
    for i in range(1, n):
        above_now = above[i]
        limit = min(max_lb, i)
        for back in range(1, limit + 1):
            if above[i - back] != above_now:
                result[i] = float(back)
                break
    return result


def np_swing_count_rolling(swing_arr, period, n_bars):
    result = np.full(n_bars, np.nan)
    if period + 1 >= n_bars:
        return result
    swing_f = swing_arr.astype(np.float64)
    cs = np.cumsum(swing_f)
    win_size = period - 2
    if win_size <= 0:
        return result
    for i in range(period + 1, n_bars):
        start = i - period + 2
        end = i
        result[i] = cs[end - 1] - (cs[start - 1] if start > 0 else 0)
    return result


def np_trend_swing_count(swing_arr, values, period, direction, n_bars):
    result = np.full(n_bars, np.nan)
    if period + 1 >= n_bars:
        return result
    for i in range(period + 1, n_bars):
        points = []
        for j in range(i - period + 2, i):
            if swing_arr[j]:
                points.append(values[j])
        if len(points) < 2:
            result[i] = 0.0
            continue
        count = 0
        for k in range(len(points) - 1, 0, -1):
            if direction == "higher" and points[k] > points[k - 1]:
                count += 1
            elif direction == "lower" and points[k] < points[k - 1]:
                count += 1
            else:
                break
        result[i] = float(count)
    return result


def build_numpy_intermediates(engine):
    """Extract all cached intermediates from ExpressionEngine as numpy arrays."""
    from scripts.profiling_engine import rolling_max as pd_rolling_max
    im = {}
    for p in [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]:
        im[f"avgc{p}"] = engine._ma(f"avgc{p}").values.astype(np.float64)
    for p in [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200]:
        im[f"xavgc{p}"] = engine._ma(f"xavgc{p}").values.astype(np.float64)
    for p in [10, 20, 50]:
        im[f"avgv{p}"] = engine._ma(f"avgv{p}").values.astype(np.float64)
    im["atr14"] = engine._atr(14).values.astype(np.float64)
    im["adr14"] = engine._adr(14).values.astype(np.float64)
    im["pct"] = engine.c.values.astype(np.float64) / 100.0
    im["close"] = engine.c.values.astype(np.float64)
    im["open"] = engine.o.values.astype(np.float64)
    im["high"] = engine.h.values.astype(np.float64)
    im["low"] = engine.l.values.astype(np.float64)
    im["volume"] = engine.v.values.astype(np.float64)
    for p in sorted(set(list(range(5, 125, 5)) + [2, 3, 7, 10, 15, 63, 65, 126])):
        im[f"maxh{p}"] = engine._maxh(p).values.astype(np.float64)
    for p in sorted(set(list(range(5, 65, 5)) + [2, 3, 7, 65, 90, 120, 126])):
        im[f"minl{p}"] = engine._minl(p).values.astype(np.float64)
    for p in [5, 7, 9, 14, 21, 28]:
        im[f"rsi{p}"] = engine._rsi(p).values.astype(np.float64)
    for p in [7, 10, 14, 20]:
        im[f"adx{p}"] = engine._adx(p).values.astype(np.float64)
        im[f"diplus{p}"] = engine._diplus(p).values.astype(np.float64)
        im[f"diminus{p}"] = engine._diminus(p).values.astype(np.float64)
    for p in [3, 5, 7, 9, 10, 14, 21, 28, 50]:
        im[f"stoch{p}"] = engine._stoch(p).values.astype(np.float64)
    for p in [5, 7, 10, 14, 20, 30, 50]:
        im[f"cci{p}"] = engine._cci(p).values.astype(np.float64)
    for p in [5, 10, 14, 20]:
        im[f"bop{p}"] = engine._bop(p).values.astype(np.float64)
    im["obv"] = engine._obv().values.astype(np.float64)
    for fast, slow in [(12, 26), (8, 17), (5, 35), (5, 13), (6, 19)]:
        im[f"macd_{fast}_{slow}"] = engine._macd(fast, slow).values.astype(np.float64)
    for p in [5, 10, 15, 20, 30, 50]:
        im[f"bbtop_{p}"] = engine._bbtop(p).values.astype(np.float64)
        im[f"bbbot_{p}"] = engine._bbbot(p).values.astype(np.float64)
        im[f"stddev_{p}"] = engine._stddev(p).values.astype(np.float64)
    for p in [7, 10, 14, 20, 25, 50, 100]:
        im[f"aroon_up_{p}"] = engine._aroon_up(p).values.astype(np.float64)
        im[f"aroon_down_{p}"] = engine._aroon_down(p).values.astype(np.float64)
    for p in [10, 14, 20, 30, 50]:
        im[f"cmf_{p}"] = engine._cmf(p).values.astype(np.float64)
    for p in [5, 7, 10, 15, 20, 30, 50, 65, 100]:
        im[f"kauf_eff_{p}"] = engine._kaufman_eff(p).values.astype(np.float64)
    for p in [10, 20, 50]:
        im[f"maxc{p}"] = pd_rolling_max(engine.c, p).values.astype(np.float64)
    return im


def _get_ma(im, name):
    return im.get(name)


def _get_norm(im, name):
    if name == "atr14": return im["atr14"]
    elif name == "adr14": return im["adr14"]
    elif name == "pct": return im["pct"]
    elif name == "close": return im["close"]
    return im["atr14"]


def dispatch_arith_numpy(comp, im):
    """Dispatch a single arithmetic expression using precomputed numpy intermediates.
    Returns numpy array or None on failure."""
    op = comp["op"]
    C = im["close"]
    O = im["open"]
    H = im["high"]
    L = im["low"]
    V = im["volume"]
    try:
        if op == "ma_slope":
            ma = _get_ma(im, comp["ma"])
            norm = _get_norm(im, comp["normalizer"])
            return _safe_div(ma - _shift(ma, comp["offset"]), norm)
        elif op == "ma_spread":
            return _safe_div(_get_ma(im, comp["ma_fast"]) - _get_ma(im, comp["ma_slow"]),
                            _get_norm(im, comp["normalizer"]))
        elif op == "extension":
            return _safe_div(C - _get_ma(im, comp["ma"]), _get_norm(im, comp["normalizer"]))
        elif op == "distance_to_maxh":
            price = C if comp["price_ref"] == "C" else H
            maxh = _shift(im[f"maxh{comp['maxh_period']}"], 1)
            return _safe_div(maxh - price, _get_norm(im, comp["normalizer"]))
        elif op == "ratio_c_maxh":
            maxh = _shift(im[f"maxh{comp['maxh_period']}"], 1)
            return _safe_div(C, maxh)
        elif op == "distance_to_minl":
            price = C if comp["price_ref"] == "C" else L
            minl = _shift(im[f"minl{comp['minl_period']}"], 1)
            return _safe_div(price - minl, _get_norm(im, comp["normalizer"]))
        elif op == "ratio_c_minl":
            minl = _shift(im[f"minl{comp['minl_period']}"], 1)
            return _safe_div(C, minl)
        elif op == "extension_slope":
            ma = _get_ma(im, comp["ma"])
            norm = _get_norm(im, comp["normalizer"])
            ext = C - ma
            return _safe_div(ext - _shift(ext, comp["offset"]), norm)
        elif op == "extension_peak_ratio":
            ma = _get_ma(im, comp["ma"])
            ext = C - ma
            max_ext = _rolling_max(ext, comp["lookback"])
            return _safe_div(ext, max_ext)
        elif op == "extension_ceiling_ratio":
            ma = _get_ma(im, comp["ma"])
            norm = _get_norm(im, comp["normalizer"])
            ext_norm = _safe_div(C - ma, norm)
            max_ext = _rolling_max(ext_norm, comp["lookback"])
            return _safe_div(ext_norm, max_ext)
        elif op == "ext_adr_multiples":
            return _safe_div(C - _get_ma(im, comp["ma"]), im["adr14"])
        elif op == "spread_slope":
            fast = _get_ma(im, comp["ma_fast"])
            slow = _get_ma(im, comp["ma_slow"])
            norm = _get_norm(im, comp["normalizer"])
            spread = _safe_div(fast - slow, norm)
            return spread - _shift(spread, comp["offset"])
        elif op == "pullback":
            return _safe_div(im[f"maxh{comp['period']}"] - C, _get_norm(im, comp["normalizer"]))
        elif op == "range_position":
            p = comp["period"]
            maxh = im.get(f"maxh{p}")
            minl = im.get(f"minl{p}")
            if maxh is None: maxh = _rolling_max(H, p)
            if minl is None: minl = _rolling_min(L, p)
            return _safe_div(C - minl, maxh - minl)
        elif op == "range_width":
            p = comp["period"]
            maxh = im.get(f"maxh{p}")
            minl = im.get(f"minl{p}")
            if maxh is None: maxh = _rolling_max(H, p)
            if minl is None: minl = _rolling_min(L, p)
            return _safe_div(maxh - minl, _get_norm(im, comp["normalizer"]))
        elif op == "roc":
            return _safe_div(C, _shift(C, comp["period"])) * 100.0 - 100.0
        elif op == "roc_delta":
            p = comp["period"]
            co = comp["compare_offset"]
            roc_now = _safe_div(C, _shift(C, p)) - 1.0
            roc_prev = _safe_div(_shift(C, co), _shift(C, co + p)) - 1.0
            return 100.0 * (roc_now - roc_prev)
        elif op == "adx": return im[f"adx{comp['period']}"]
        elif op == "adx_slope":
            a = im[f"adx{comp['period']}"]
            return a - _shift(a, comp["offset"])
        elif op == "rsi": return im[f"rsi{comp['period']}"]
        elif op == "rsi_slope":
            r = im[f"rsi{comp['period']}"]
            return r - _shift(r, comp["offset"])
        elif op == "stochastic": return im[f"stoch{comp['period']}"]
        elif op == "cci": return im[f"cci{comp['period']}"]
        elif op == "di_spread":
            return im[f"diplus{comp['period']}"] - im[f"diminus{comp['period']}"]
        elif op == "volume_ratio":
            avg = im.get(f"avgv{comp['avg_period']}")
            return _safe_div(V, avg) if avg is not None else None
        elif op == "candle_range_ratio":
            return _safe_div(H - L, im["atr14"])
        elif op == "body_range_ratio":
            return _safe_div(np.abs(C - O), H - L)
        elif op == "upper_wick_ratio":
            return _safe_div(H - np.maximum(C, O), H - L)
        elif op == "lower_wick_ratio":
            return _safe_div(np.minimum(C, O) - L, H - L)
        elif op == "bop": return im[f"bop{comp['period']}"]
        elif op == "obv_slope":
            o = im["obv"]
            offset = comp["offset"]
            avg = im.get(f"avgv{comp.get('vol_period', 20)}")
            return _safe_div(o - _shift(o, offset), avg * offset) if avg is not None else None
        elif op == "macd_histogram":
            from scripts.profiling_engine import ema as pd_ema
            macd_line = im.get(f"macd_{comp.get('fast',12)}_{comp.get('slow',26)}")
            if macd_line is None: return None
            sig = pd_ema(pd.Series(macd_line), comp.get("signal", 9)).values
            return macd_line - sig
        elif op == "macd_histogram_slope":
            from scripts.profiling_engine import ema as pd_ema
            macd_line = im.get(f"macd_{comp.get('fast',12)}_{comp.get('slow',26)}")
            if macd_line is None: return None
            sig = pd_ema(pd.Series(macd_line), comp.get("signal", 9)).values
            hist = macd_line - sig
            return hist - _shift(hist, comp["offset"])
        elif op == "macd_line_norm":
            macd_line = im.get(f"macd_{comp.get('fast',12)}_{comp.get('slow',26)}")
            if macd_line is None: return None
            return _safe_div(macd_line, _get_norm(im, comp["normalizer"]))
        elif op == "bollinger_pctb":
            p = comp["period"]
            top = im.get(f"bbtop_{p}")
            bot = im.get(f"bbbot_{p}")
            if top is None or bot is None: return None
            return _safe_div(C - bot, top - bot)
        elif op == "bollinger_bandwidth":
            p = comp["period"]
            top = im.get(f"bbtop_{p}")
            bot = im.get(f"bbbot_{p}")
            mid = im.get(f"avgc{p}")
            if top is None or bot is None or mid is None: return None
            return _safe_div(top - bot, mid)
        elif op == "bollinger_bandwidth_rank":
            p = comp["period"]
            lb = comp["lookback"]
            top = im.get(f"bbtop_{p}")
            bot = im.get(f"bbbot_{p}")
            mid = im.get(f"avgc{p}")
            if top is None or bot is None or mid is None: return None
            bw = _safe_div(top - bot, mid)
            bw_min = _rolling_min(bw, lb)
            bw_max = _rolling_max(bw, lb)
            return _safe_div(bw - bw_min, bw_max - bw_min)
        elif op == "aroon_up_val":
            return im.get(f"aroon_up_{comp['period']}")
        elif op == "aroon_down_val":
            return im.get(f"aroon_down_{comp['period']}")
        elif op == "aroon_oscillator":
            p = comp["period"]
            up = im.get(f"aroon_up_{p}")
            dn = im.get(f"aroon_down_{p}")
            if up is None or dn is None: return None
            return up - dn
        elif op == "cmf": return im.get(f"cmf_{comp['period']}")
        elif op == "cmf_slope":
            c = im.get(f"cmf_{comp['period']}")
            if c is None: return None
            return c - _shift(c, comp["offset"])
        elif op == "kaufman_efficiency_ratio":
            return im.get(f"kauf_eff_{comp['period']}")
        elif op == "atr_ratio":
            a = im["atr14"]
            return _safe_div(a, _shift(a, comp["offset"]))
        elif op == "slope_ratio":
            fast_ma = _get_ma(im, comp["fast_ma"])
            slow_ma = _get_ma(im, comp["slow_ma"])
            offset = comp["offset"]
            return _safe_div(fast_ma - _shift(fast_ma, offset),
                           slow_ma - _shift(slow_ma, offset))
        elif op == "ma_undercut_depth":
            ma = _get_ma(im, comp["ma"])
            p = comp["period"]
            norm = _get_norm(im, comp["normalizer"])
            diff = L - ma
            min_diff = _rolling_min(diff, p)
            return _safe_div(min_diff, norm)
        elif op == "channel_slope":
            p = comp["period"]
            maxh = im.get(f"maxh{p}")
            if maxh is None: maxh = _rolling_max(H, p)
            norm = _get_norm(im, comp["normalizer"])
            return _safe_div(maxh - _shift(maxh, p), norm)
        elif op == "retrace_high":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", _rolling_max(H, p))
            minl = im.get(f"minl{p}", _rolling_min(L, p))
            return _safe_div(H - minl, maxh - minl)
        elif op == "retrace_low":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", _rolling_max(H, p))
            minl = im.get(f"minl{p}", _rolling_min(L, p))
            return _safe_div(L - minl, maxh - minl)
        else:
            return None
    except Exception:
        return None


def run_optimized_bools(engine, expressions, indices_by_op, n_bars, data):
    """Compute all boolean aggregate expressions using numpy."""
    ct_indices, st_indices, tir_indices = indices_by_op
    bool_cache = {}
    for j in ct_indices + st_indices + tir_indices:
        cond = expressions[j]["compute"]["condition"]
        if cond not in bool_cache:
            try:
                bool_cache[cond] = engine._bool_series(cond).values.astype(bool)
            except:
                bool_cache[cond] = np.zeros(n_bars, dtype=bool)
    for j in ct_indices:
        c = expressions[j]["compute"]
        data[:, j] = np_count_true(bool_cache[c["condition"]], c["period"]).astype(np.float32)
    bars_since_cache = {}
    for j in st_indices:
        cond = expressions[j]["compute"]["condition"]
        if cond not in bars_since_cache:
            b = bool_cache[cond]
            bs = np.full(n_bars, n_bars, dtype=np.float64)
            for i in range(n_bars):
                if b[i]: bs[i] = 0.0
                elif i > 0: bs[i] = bs[i-1] + 1.0
            bars_since_cache[cond] = bs
    for j in st_indices:
        c = expressions[j]["compute"]
        bs = bars_since_cache[c["condition"]]
        r = np.full(n_bars, np.nan)
        for i in range(c["period"] - 1, n_bars):
            r[i] = bs[i] if bs[i] < c["period"] else -1.0
        data[:, j] = r.astype(np.float32)
    for j in tir_indices:
        c = expressions[j]["compute"]
        data[:, j] = np_true_in_row(bool_cache[c["condition"]], c["period"]).astype(np.float32)


def run_optimized_ext_struct(engine, expressions, ext_indices, ext_name_to_idx, n_bars, data):
    """Compute extension structure expressions using vectorized linreg + cached bool_agg."""
    import json as _json
    from scripts.backtest_conditions import compute_on_series, compute_series
    LINREG_OPS = {"trendline_deviation", "channel_position"}
    series_registry = {}
    for sname, sidx in ext_name_to_idx.items():
        col_data = data[:, sidx]
        if not np.all(np.isnan(col_data)):
            series_registry[sname] = col_data.astype(np.float64)
    if not series_registry:
        return
    linreg_indices = []
    bool_agg_indices = []
    other_indices = []
    for j in ext_indices:
        comp = expressions[j]["compute"]
        if comp.get("op") == "on_series":
            if comp.get("inner_op", {}).get("op", "") in LINREG_OPS:
                linreg_indices.append(j)
            else:
                other_indices.append(j)
        elif comp.get("op") == "on_series_bool_agg":
            bool_agg_indices.append(j)
        else:
            other_indices.append(j)
    for j in linreg_indices:
        try:
            comp = expressions[j]["compute"]
            sn = comp.get("series", "")
            if sn in series_registry:
                s = series_registry[sn]
                lb = comp["inner_op"]["lookback"]
                fn = np_trendline_deviation if comp["inner_op"]["op"] == "trendline_deviation" else np_channel_position
                data[:, j] = fn(s, lb).astype(np.float32)
        except:
            pass
    indicator_bool_cache = {}
    for j in bool_agg_indices:
        comp = expressions[j]["compute"]
        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
        if ck not in indicator_bool_cache:
            try:
                sd = series_registry.get(comp["series"])
                if sd is None:
                    indicator_bool_cache[ck] = None
                    continue
                indicator = compute_on_series(np.asarray(sd, dtype=np.float64), comp["bool_op"])
                threshold = comp["bool_op"].get("threshold", 0)
                direction = comp["bool_op"].get("direction", "gt")
                if direction == "gt": b = indicator > threshold
                elif direction == "lt": b = indicator < threshold
                elif direction == "positive": b = indicator > 0
                elif direction == "negative": b = indicator < 0
                else: b = indicator > threshold
                b[np.isnan(indicator)] = False
                indicator_bool_cache[ck] = b.astype(bool)
            except:
                indicator_bool_cache[ck] = None
    ba_bs_cache = {}
    for j in bool_agg_indices:
        comp = expressions[j]["compute"]
        if comp["agg_op"] != "since_true": continue
        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
        if ck in ba_bs_cache or indicator_bool_cache.get(ck) is None: continue
        b = indicator_bool_cache[ck]
        bs = np.full(n_bars, n_bars, dtype=np.float64)
        for i in range(n_bars):
            if b[i]: bs[i] = 0.0
            elif i > 0: bs[i] = bs[i-1] + 1.0
        ba_bs_cache[ck] = bs
    for j in bool_agg_indices:
        comp = expressions[j]["compute"]
        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
        b = indicator_bool_cache.get(ck)
        if b is None: continue
        ap = comp["agg_period"]
        if comp["agg_op"] == "count_true":
            data[:, j] = np_count_true(b, ap).astype(np.float32)
        elif comp["agg_op"] == "since_true":
            bs = ba_bs_cache.get(ck)
            if bs is not None:
                r = np.full(n_bars, np.nan)
                for i in range(ap - 1, n_bars):
                    r[i] = bs[i] if bs[i] < ap else -1.0
                data[:, j] = r.astype(np.float32)
        elif comp["agg_op"] == "true_in_row":
            data[:, j] = np_true_in_row(b, ap).astype(np.float32)
    for j in other_indices:
        try:
            s = compute_series(engine, expressions[j]["compute"], series_registry=series_registry)
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars:
                    data[:, j] = arr
        except:
            pass


def _compute_ticker_full(args):
    """Compute all expression series for one ticker (daily + LSP + algo + HTF).

    Args: (ticker, df_dict, weekly_df_dict, monthly_df_dict)
        df_dict: daily OHLCV columns + date
        weekly_df_dict: weekly OHLCV from HTF pickle (or None to resample)
        monthly_df_dict: monthly OHLCV from HTF pickle (or None to resample)

    Returns: (ticker, dates_array, data_array) or (ticker, None, None)
    """
    ticker, df_dict, weekly_df_dict, monthly_df_dict = args
    global _w_expressions, _w_daily_indices, _w_ext_struct_indices
    global _w_ext_series_name_to_idx
    global _w_lsp_indices, _w_algo_indices
    global _w_htf_weekly_indices, _w_htf_monthly_indices
    global _w_htf_weekly_base, _w_htf_monthly_base

    try:
        from scripts.expression_engine import ExpressionEngine
        from scripts.backtest_conditions import compute_series

        # Reconstruct DataFrame from dict (avoids pickle overhead of full DataFrame)
        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        n_bars = len(df)
        if n_bars < 50:
            return (ticker, None, None)

        n_exprs = len(_w_expressions)
        engine = ExpressionEngine(df)

        # Allocate output array
        data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

        # ── 1. Daily expressions ──
        # Order: SLOW_OPS (numpy) → fallback ops via compute_series (warms engine)
        #      → build intermediates from warm cache → dispatch remaining arith → bools

        # SLOW_OPS — custom numpy replacements
        c_vals = engine.c.values.astype(np.float64)
        h_vals = engine.h.values.astype(np.float64)
        l_vals = engine.l.values.astype(np.float64)
        v_vals = engine.v.values.astype(np.float64)

        pct_rank_cache = {}
        swing_high_arr = None
        swing_low_arr = None

        for j in _w_daily_slow_indices:
            comp = _w_expressions[j]["compute"]
            op = comp["op"]
            try:
                if op == "percentile_rank":
                    source = comp["source"]
                    period = comp["period"]
                    if source not in pct_rank_cache:
                        if source == "close": pct_rank_cache[source] = c_vals
                        elif source == "volume": pct_rank_cache[source] = v_vals
                        elif source == "range": pct_rank_cache[source] = h_vals - l_vals
                        elif source == "atr14": pct_rank_cache[source] = engine._atr(14).values.astype(np.float64)
                        elif source == "rsi14": pct_rank_cache[source] = engine._rsi(14).values.astype(np.float64)
                        else: pct_rank_cache[source] = c_vals
                    data[:, j] = np_percentile_rank(pct_rank_cache[source], period).astype(np.float32)
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
                                swing_high_arr[i] = h_vals[i] > h_vals[i-1] and h_vals[i] > h_vals[i+1]
                        data[:, j] = np_swing_count_rolling(swing_high_arr, p, n_bars).astype(np.float32)
                    else:
                        if swing_low_arr is None:
                            swing_low_arr = np.zeros(n_bars, dtype=bool)
                            for i in range(1, n_bars - 1):
                                swing_low_arr[i] = l_vals[i] < l_vals[i-1] and l_vals[i] < l_vals[i+1]
                        data[:, j] = np_swing_count_rolling(swing_low_arr, p, n_bars).astype(np.float32)
                elif op in ("higher_high_count", "lower_high_count"):
                    p = comp["period"]
                    if swing_high_arr is None:
                        swing_high_arr = np.zeros(n_bars, dtype=bool)
                        for i in range(1, n_bars - 1):
                            swing_high_arr[i] = h_vals[i] > h_vals[i-1] and h_vals[i] > h_vals[i+1]
                    direction = "higher" if op == "higher_high_count" else "lower"
                    data[:, j] = np_trend_swing_count(swing_high_arr, h_vals, p, direction, n_bars).astype(np.float32)
                elif op in ("higher_low_count", "lower_low_count"):
                    p = comp["period"]
                    if swing_low_arr is None:
                        swing_low_arr = np.zeros(n_bars, dtype=bool)
                        for i in range(1, n_bars - 1):
                            swing_low_arr[i] = l_vals[i] < l_vals[i-1] and l_vals[i] < l_vals[i+1]
                    direction = "higher" if op == "higher_low_count" else "lower"
                    data[:, j] = np_trend_swing_count(swing_low_arr, l_vals, p, direction, n_bars).astype(np.float32)
            except:
                pass

        # Fallback ops — compute_series (warms engine cache for intermediates build)
        for j in _w_daily_fallback_indices:
            try:
                series = compute_series(engine, _w_expressions[j]["compute"])
                if series is not None:
                    arr = np.asarray(series, dtype=np.float32)
                    if len(arr) == n_bars:
                        data[:, j] = arr
                    elif len(arr) < n_bars:
                        data[n_bars - len(arr):, j] = arr
            except:
                pass

        # Dispatch ops — build intermediates from now-warm engine, pure numpy
        daily_im = build_numpy_intermediates(engine)
        for j in _w_daily_dispatch_indices:
            try:
                result = dispatch_arith_numpy(_w_expressions[j]["compute"], daily_im)
                if result is not None:
                    data[:, j] = result.astype(np.float32)
            except:
                pass
        del daily_im

        # Daily booleans — numpy optimized
        run_optimized_bools(engine, _w_expressions,
                           (_w_daily_bool_ct, _w_daily_bool_st, _w_daily_bool_tir),
                           n_bars, data)

        # ── 2. LSP expressions (precomputed by lsp_detector_v2) ──
        if _w_lsp_indices:
            try:
                from scripts.lsp_detector_v2 import compute_all_lsp_series
                lsp_dict = compute_all_lsp_series(df)
                for j in _w_lsp_indices:
                    col_name = _w_expressions[j]["compute"]["column"]
                    if col_name in lsp_dict:
                        arr = lsp_dict[col_name]
                        if len(arr) == n_bars:
                            data[:, j] = arr.astype(np.float32)
            except Exception:
                pass  # LSP fails silently — columns stay NaN

        # ── 2b. Algo line expressions (precomputed by algo_line_detector) ──
        if _w_algo_indices:
            try:
                from scripts.algo_line_detector import compute_all_algo_series
                algo_dict = compute_all_algo_series(df)
                for j in _w_algo_indices:
                    col_name = _w_expressions[j]["compute"]["column"]
                    if col_name in algo_dict:
                        arr = algo_dict[col_name]
                        if len(arr) == n_bars:
                            data[:, j] = arr.astype(np.float32)
            except Exception:
                pass  # Algo lines fail silently — columns stay NaN

        # ── 3. HTF expressions (weekly + monthly) ──
        # Use pre-fetched HTF OHLCV from pickles when available.
        # Fall back to resampling from daily if HTF data not provided.
        _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

        # Build HTF DataFrames from provided dicts (or resample as fallback)
        _htf_sources = {}
        if weekly_df_dict is not None:
            try:
                wdf = pd.DataFrame(weekly_df_dict)
                wdf["date"] = pd.to_datetime(wdf["date"])
                for col in ["open", "high", "low", "close", "volume"]:
                    wdf[col] = pd.to_numeric(wdf[col], errors="coerce")
                if len(wdf) >= 5:
                    _htf_sources["W"] = wdf
            except Exception:
                pass
        if "W" not in _htf_sources:
            _htf_sources["W"] = resample_ohlcv(df, "W")

        if monthly_df_dict is not None:
            try:
                mdf = pd.DataFrame(monthly_df_dict)
                mdf["date"] = pd.to_datetime(mdf["date"])
                for col in ["open", "high", "low", "close", "volume"]:
                    mdf[col] = pd.to_numeric(mdf[col], errors="coerce")
                if len(mdf) >= 5:
                    _htf_sources["ME"] = mdf
            except Exception:
                pass
        if "ME" not in _htf_sources:
            _htf_sources["ME"] = resample_ohlcv(df, "ME")

        for tf_freq, tf_indices, tf_base_computes in [
            ("W", _w_htf_weekly_indices, _w_htf_weekly_base),
            ("ME", _w_htf_monthly_indices, _w_htf_monthly_base),
        ]:
            if not tf_indices:
                continue

            htf_df = _htf_sources.get(tf_freq)
            if htf_df is None or len(htf_df) < 5:
                continue  # too few bars — HTF columns stay NaN

            # Build daily→HTF mapping once per timeframe
            htf_map = build_htf_to_daily_map(df["date"], htf_df, tf_freq)

            # Create engine for HTF data
            htf_engine = ExpressionEngine(htf_df)
            htf_n = len(htf_df)

            # Build numpy intermediates from HTF engine (cheap on small arrays)
            htf_im = build_numpy_intermediates(htf_engine)

            # Classify HTF expressions
            htf_ct = []
            htf_st = []
            htf_tir = []
            htf_ext = []
            htf_arith = []

            for k, j in enumerate(tf_indices):
                base_op = tf_base_computes[k].get("op", "")
                if base_op == "count_true": htf_ct.append((k, j))
                elif base_op == "since_true": htf_st.append((k, j))
                elif base_op == "true_in_row": htf_tir.append((k, j))
                elif base_op in _ON_SERIES_OPS: htf_ext.append((k, j))
                else: htf_arith.append((k, j))

            # HTF arith — dispatch through numpy intermediates, fallback to compute_series
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
                except:
                    try:
                        s = compute_series(htf_engine, comp)
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                    except: pass

            # HTF booleans — numpy
            htf_bool_cache = {}
            for k, j in htf_ct + htf_st + htf_tir:
                cond = tf_base_computes[k]["condition"]
                if cond not in htf_bool_cache:
                    try:
                        htf_bool_cache[cond] = htf_engine._bool_series(cond).values.astype(bool)
                    except:
                        htf_bool_cache[cond] = np.zeros(htf_n, dtype=bool)
            for k, j in htf_ct:
                b = htf_bool_cache[tf_base_computes[k]["condition"]]
                data[:, j] = map_htf_series_to_daily(np_count_true(b, tf_base_computes[k]["period"]).astype(np.float32), htf_map)
            htf_bs = {}
            for k, j in htf_st:
                cond = tf_base_computes[k]["condition"]
                if cond not in htf_bs:
                    b = htf_bool_cache[cond]
                    bs = np.full(htf_n, htf_n, dtype=np.float64)
                    for i in range(htf_n):
                        if b[i]: bs[i] = 0.0
                        elif i > 0: bs[i] = bs[i-1] + 1.0
                    htf_bs[cond] = bs
            for k, j in htf_st:
                bs = htf_bs[tf_base_computes[k]["condition"]]
                p = tf_base_computes[k]["period"]
                r = np.full(htf_n, np.nan)
                for i in range(p-1, htf_n):
                    r[i] = bs[i] if bs[i] < p else -1.0
                data[:, j] = map_htf_series_to_daily(r.astype(np.float32), htf_map)
            for k, j in htf_tir:
                b = htf_bool_cache[tf_base_computes[k]["condition"]]
                data[:, j] = map_htf_series_to_daily(np_true_in_row(b, tf_base_computes[k]["period"]).astype(np.float32), htf_map)

            # HTF ext struct — optimized at HTF resolution, then mapped to daily
            if htf_ext:
                import json as _json
                from scripts.backtest_conditions import compute_on_series
                LINREG_OPS = {"trendline_deviation", "channel_position"}

                # Build HTF-resolution extension series from intermediates
                htf_ext_registry = {}
                for sname, comp_spec in [
                    ("ext_avgc50_adr14", {"op": "extension", "ma": "avgc50", "normalizer": "adr14"}),
                    ("ext_avgc200_adr14", {"op": "extension", "ma": "avgc200", "normalizer": "adr14"}),
                ]:
                    result = dispatch_arith_numpy(comp_spec, htf_im)
                    if result is not None and not np.all(np.isnan(result)):
                        htf_ext_registry[sname] = result.astype(np.float64)

                if htf_ext_registry:
                    # Classify HTF ext struct expressions
                    ext_linreg = []
                    ext_bool_agg = []
                    ext_other = []
                    for k, j in htf_ext:
                        comp = tf_base_computes[k]
                        if comp.get("op") == "on_series":
                            if comp.get("inner_op", {}).get("op", "") in LINREG_OPS:
                                ext_linreg.append((k, j))
                            else:
                                ext_other.append((k, j))
                        elif comp.get("op") == "on_series_bool_agg":
                            ext_bool_agg.append((k, j))
                        else:
                            ext_other.append((k, j))

                    # Linreg — vectorized numpy at HTF resolution
                    for k, j in ext_linreg:
                        try:
                            comp = tf_base_computes[k]
                            sn = comp.get("series", "")
                            if sn in htf_ext_registry:
                                s = htf_ext_registry[sn]
                                lb = comp["inner_op"]["lookback"]
                                fn = np_trendline_deviation if comp["inner_op"]["op"] == "trendline_deviation" else np_channel_position
                                htf_result = fn(s, lb).astype(np.float32)
                                data[:, j] = map_htf_series_to_daily(htf_result, htf_map)
                        except: pass

                    # Bool agg — numpy at HTF resolution
                    htf_ind_bool_cache = {}
                    for k, j in ext_bool_agg:
                        comp = tf_base_computes[k]
                        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
                        if ck not in htf_ind_bool_cache:
                            try:
                                sd = htf_ext_registry.get(comp["series"])
                                if sd is None:
                                    htf_ind_bool_cache[ck] = None
                                    continue
                                indicator = compute_on_series(np.asarray(sd, dtype=np.float64), comp["bool_op"])
                                threshold = comp["bool_op"].get("threshold", 0)
                                direction = comp["bool_op"].get("direction", "gt")
                                if direction == "gt": b = indicator > threshold
                                elif direction == "lt": b = indicator < threshold
                                elif direction == "positive": b = indicator > 0
                                elif direction == "negative": b = indicator < 0
                                else: b = indicator > threshold
                                b[np.isnan(indicator)] = False
                                htf_ind_bool_cache[ck] = b.astype(bool)
                            except:
                                htf_ind_bool_cache[ck] = None

                    htf_ba_bs_cache = {}
                    for k, j in ext_bool_agg:
                        comp = tf_base_computes[k]
                        if comp["agg_op"] != "since_true": continue
                        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
                        if ck in htf_ba_bs_cache or htf_ind_bool_cache.get(ck) is None: continue
                        b = htf_ind_bool_cache[ck]
                        bs = np.full(htf_n, htf_n, dtype=np.float64)
                        for i in range(htf_n):
                            if b[i]: bs[i] = 0.0
                            elif i > 0: bs[i] = bs[i-1] + 1.0
                        htf_ba_bs_cache[ck] = bs

                    for k, j in ext_bool_agg:
                        comp = tf_base_computes[k]
                        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
                        b = htf_ind_bool_cache.get(ck)
                        if b is None: continue
                        ap = comp["agg_period"]
                        if comp["agg_op"] == "count_true":
                            htf_result = np_count_true(b, ap).astype(np.float32)
                            data[:, j] = map_htf_series_to_daily(htf_result, htf_map)
                        elif comp["agg_op"] == "since_true":
                            bs = htf_ba_bs_cache.get(ck)
                            if bs is not None:
                                r = np.full(htf_n, np.nan)
                                for i in range(ap - 1, htf_n):
                                    r[i] = bs[i] if bs[i] < ap else -1.0
                                data[:, j] = map_htf_series_to_daily(r.astype(np.float32), htf_map)
                        elif comp["agg_op"] == "true_in_row":
                            htf_result = np_true_in_row(b, ap).astype(np.float32)
                            data[:, j] = map_htf_series_to_daily(htf_result, htf_map)

                    # Other on_series ops — compute_series fallback at HTF resolution
                    for k, j in ext_other:
                        try:
                            s = compute_series(htf_engine, tf_base_computes[k], series_registry=htf_ext_registry)
                            if s is not None:
                                data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                        except: pass
                else:
                    # No base series available — fallback all to compute_series
                    for k, j in htf_ext:
                        try:
                            s = compute_series(htf_engine, tf_base_computes[k])
                            if s is not None:
                                data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                        except: pass

            del htf_im  # free HTF intermediates

        # ── 4. Extension structure (on_series — optimized) ──
        # These require the extension series (ext_avgc50_adr14, ext_avgc200_adr14)
        # to be already computed in step 1 above.
        if _w_ext_struct_indices and _w_ext_series_name_to_idx:
            run_optimized_ext_struct(engine, _w_expressions, _w_ext_struct_indices,
                                    _w_ext_series_name_to_idx, n_bars, data)

        dates = df["date"].dt.strftime("%Y-%m-%d").values
        return (ticker, dates, data)

    except Exception as e:
        import traceback
        print(f"  FAIL {ticker}: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return (ticker, None, None)


def _compute_and_save_ticker(args):
    """Compute all expressions and save to disk — all inside the worker.

    Same as _compute_ticker_full but saves the .npz file in-process,
    so compression is parallelized across all workers instead of
    bottlenecking on the main thread.

    Args: (ticker, df_dict, weekly_df_dict, monthly_df_dict)
    Returns: (ticker, n_bars, last_date) or (None, None, None)
    """
    ticker = args[0]
    ticker_out, dates, data = _compute_ticker_full(args)
    if dates is not None and data is not None:
        save_ticker_cache(ticker_out, dates, data)
        return (ticker_out, len(dates), str(dates[-1]))
    return (None, None, None)


# ══════════════════════════════════════════════════════════════
# CACHE I/O
# ══════════════════════════════════════════════════════════════

def _ticker_cache_path(ticker):
    """Path for a ticker's cached expression series."""
    # Handle tickers with special chars
    safe = ticker.replace("/", "_").replace(".", "_")
    return os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")


def save_ticker_cache(ticker, dates, data):
    """Save one ticker's expression series to disk.

    Data is computed in float32, stored as float16 to halve disk usage.
    load_ticker_cache() casts back to float32 — consumers see float32.
    Uses fast compression (level 1).
    """
    import zipfile
    import io
    path = _ticker_cache_path(ticker)
    data_f16 = data.astype(np.float16)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for name, arr in [("data", data_f16), ("dates", dates)]:
            buf = io.BytesIO()
            np.save(buf, arr)
            zf.writestr(name + ".npy", buf.getvalue())


def load_ticker_cache(ticker):
    """Load one ticker's cached expression series.

    Data is stored as float16 on disk, cast to float32 on load.
    If .append file exists (from forward-prop nightly appends), its rows are
    vstacked onto the base .npz data. The .append file may be wider than .npz
    (16,001 cols vs 15,805) — only the first 15,805 expression columns are
    returned to consumers.

    Returns: (dates, data) or (None, None)
    """
    path = _ticker_cache_path(ticker)
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        data = loaded["data"]
        dates = loaded["dates"]
        if data.dtype != np.float32:
            data = data.astype(np.float32)
        n_exprs = data.shape[1]

        # Check for .append file (forward-prop appended rows)
        safe = ticker.replace("/", "_").replace(".", "_")
        append_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.append")
        dates_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.append_dates")

        if os.path.exists(append_path):
            file_size = os.path.getsize(append_path)
            if file_size > 0:
                # Detect width: forward-prop format (16,001) or legacy narrow (15,805)
                from scripts.setup_forward_prop import N_INTERMEDIATES
                wide_cols = n_exprs + N_INTERMEDIATES  # 16,001
                narrow_cols = n_exprs  # 15,805

                if file_size % (wide_cols * 2) == 0:
                    total_cols = wide_cols
                elif file_size % (narrow_cols * 2) == 0:
                    total_cols = narrow_cols
                else:
                    return dates, data  # Corrupt .append — skip

                raw = np.fromfile(append_path, dtype=np.float16)
                append_data = raw.reshape(-1, total_cols)
                # Slice to expression columns only
                append_exprs = append_data[:, :n_exprs].astype(np.float32)

                # Vstack with base
                data = np.vstack([data, append_exprs])

                # Load append dates
                if os.path.exists(dates_path):
                    with open(dates_path, "r") as f:
                        append_dates = np.array([line.strip() for line in f if line.strip()])
                    if len(append_dates) == append_exprs.shape[0]:
                        dates = np.concatenate([dates, append_dates])

        return dates, data
    except:
        return None, None


# ══════════════════════════════════════════════════════════════
# FULL BUILD
# ══════════════════════════════════════════════════════════════

def build_full(force=False):
    """Build the complete expression series cache from scratch."""
    print("\n" + "=" * 70)
    print("  EXPRESSION SERIES CACHE — FULL BUILD")
    print("=" * 70)

    # Load expression library
    print("\n  Loading expressions...")
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)
    print(f"  {len(expressions)} expressions (fingerprint: {fingerprint})")

    # Check if cache is fresh
    if not force:
        manifest = load_manifest()
        if manifest and manifest.get("fingerprint") == fingerprint:
            n_cached = len(manifest.get("tickers", {}))
            print(f"\n  Cache is fresh ({n_cached} tickers, fingerprint matches).")
            print(f"  Use --force to rebuild, or --append to add new bars.")
            return manifest

    # Load OHLCV
    print("\n  Loading daily OHLCV cache...")
    universe_cache = _load_daily_cache()
    print(f"  {len(universe_cache)} tickers loaded")

    # Truncate daily data to EXPR_CACHE_START
    truncated = 0
    for ticker in list(universe_cache.keys()):
        df = _truncate_to_cache_window(universe_cache[ticker])
        if df is None or len(df) < 50:
            del universe_cache[ticker]
        else:
            if len(df) < len(universe_cache[ticker]):
                truncated += 1
            universe_cache[ticker] = df
    print(f"  After truncation to {EXPR_CACHE_START}: {len(universe_cache)} tickers "
          f"({truncated} truncated, ≥50 bars required)")

    # Load HTF OHLCV caches (weekly + monthly from EODHD)
    weekly_cache = _load_htf_cache("weekly")
    monthly_cache = _load_htf_cache("monthly")
    if weekly_cache:
        # Truncate HTF caches to same window
        for ticker in list(weekly_cache.keys()):
            df = _truncate_to_cache_window(weekly_cache[ticker])
            if df is not None and len(df) >= 5:
                weekly_cache[ticker] = df
            else:
                del weekly_cache[ticker]
        print(f"  Weekly HTF cache: {len(weekly_cache)} tickers")
    else:
        print(f"  Weekly HTF cache: not found (will resample from daily)")
    if monthly_cache:
        for ticker in list(monthly_cache.keys()):
            df = _truncate_to_cache_window(monthly_cache[ticker])
            if df is not None and len(df) >= 5:
                monthly_cache[ticker] = df
            else:
                del monthly_cache[ticker]
        print(f"  Monthly HTF cache: {len(monthly_cache)} tickers")
    else:
        print(f"  Monthly HTF cache: not found (will resample from daily)")

    # Prepare work items — convert DataFrames to dicts for cheaper serialization
    print("\n  Preparing work items...")
    work_items = []
    for ticker, df in universe_cache.items():
        df_dict = {
            "date": df["date"].values,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values,
        }
        weekly_df_dict = _df_to_dict(weekly_cache.get(ticker)) if weekly_cache else None
        monthly_df_dict = _df_to_dict(monthly_cache.get(ticker)) if monthly_cache else None
        work_items.append((ticker, df_dict, weekly_df_dict, monthly_df_dict))

    # Sort by bar count descending — big tickers first, short ones fill gaps
    work_items.sort(key=lambda x: len(x[1]["date"]), reverse=True)

    # Free the large caches — work_items has everything we need now
    del universe_cache, weekly_cache, monthly_cache
    import gc; gc.collect()

    # Create output directory
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)

    # Clean up forward-prop files — full rebuild makes them stale
    _fp_extensions = [".append", ".append_dates", ".lookback", ".state"]
    fp_cleaned = 0
    for fname in os.listdir(EXPR_CACHE_DIR):
        if any(fname.endswith(ext) for ext in _fp_extensions):
            os.remove(os.path.join(EXPR_CACHE_DIR, fname))
            fp_cleaned += 1
    if fp_cleaned:
        print(f"  Cleaned {fp_cleaned} forward-prop files (.append/.lookback/.state)")

    # Parallel computation — 14 workers, fast compression frees CPU headroom
    n_workers = int(os.environ.get("EXPR_CACHE_WORKERS", 14))
    print(f"\n  Computing {len(work_items)} tickers × {len(expressions)} expressions")
    print(f"  Workers: {n_workers}")

    t0 = time.time()
    ticker_info = {}
    completed = 0
    failed = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_worker,
        initargs=(expressions,)
    ) as pool:
        # Workers save to disk in-process — only small metadata returns through IPC.
        # Higher in-flight keeps workers busy without waiting for dispatch.
        max_in_flight = n_workers * 4
        pending = {}
        work_idx = 0
        first_errors = []

        def _submit_next():
            """Submit one work item if available."""
            nonlocal work_idx
            if work_idx < len(work_items):
                item = work_items[work_idx]
                future = pool.submit(_compute_and_save_ticker, item)
                pending[future] = item[0]  # ticker name
                work_idx += 1
                return True
            return False

        def _collect_one(future):
            """Collect one result metadata, free memory."""
            nonlocal completed, failed
            ticker = pending.pop(future)
            try:
                ticker_out, n_bars, last_date = future.result()
                if ticker_out is not None:
                    ticker_info[ticker_out] = {
                        "n_bars": n_bars,
                        "last_date": last_date,
                    }
                else:
                    failed += 1
                    if len(first_errors) < 5:
                        first_errors.append(f"{ticker}: returned None")
            except Exception as e:
                failed += 1
                if len(first_errors) < 5:
                    first_errors.append(f"{ticker}: {type(e).__name__}: {str(e)[:200]}")
            del future  # free the result memory immediately
            completed += 1
            if completed % 25 == 0 or completed == len(work_items):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(work_items) - completed) / rate if rate > 0 else 0
                pct = completed / len(work_items) * 100
                per_ticker = elapsed * n_workers / completed if completed > 0 else 0
                print(f"    {completed:,}/{len(work_items):,} ({pct:.0f}%) "
                      f"[{elapsed/60:.1f}m elapsed, ~{eta/60:.1f}m left] "
                      f"({len(ticker_info)} ok, {failed} failed) "
                      f"[{per_ticker:.1f}s/ticker, {rate:.1f} tickers/s]")

        # Seed the initial batch
        for _ in range(min(max_in_flight, len(work_items))):
            _submit_next()

        # Process as completed, immediately submit replacements
        while pending:
            # Wait for ANY one future to complete
            done_iter = as_completed(pending)
            future = next(done_iter)
            _collect_one(future)
            # Submit replacement to keep pipeline full
            _submit_next()

    total_time = time.time() - t0

    if first_errors:
        print(f"\n  First {len(first_errors)} errors:")
        for err in first_errors:
            print(f"    ✗ {err}")

    # Save manifest
    manifest = {
        "fingerprint": fingerprint,
        "n_expressions": len(expressions),
        "expr_names": [e["name"] for e in expressions],
        "n_tickers": len(ticker_info),
        "tickers": ticker_info,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "build_time_s": round(total_time, 1),
        "dtype": "float16",
        "start_date": EXPR_CACHE_START,
    }
    save_manifest(manifest)

    # Disk usage
    total_bytes = sum(
        os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
        for f in os.listdir(EXPR_CACHE_DIR)
        if f.endswith(".npz")
    )
    total_gb = total_bytes / (1024 ** 3)

    print(f"\n  {'=' * 50}")
    print(f"  BUILD COMPLETE")
    print(f"  {'=' * 50}")
    print(f"  Tickers cached: {len(ticker_info):,}")
    print(f"  Failed: {failed}")
    print(f"  Total disk: {total_gb:.1f} GB")
    print(f"  Time: {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  Manifest: {MANIFEST_PATH}")

    return manifest


# ══════════════════════════════════════════════════════════════
# NIGHTLY APPEND
# ══════════════════════════════════════════════════════════════

def append_new_bars():
    """Append new bars to existing cache.

    Compares current OHLCV cache bar counts against manifest,
    then computes and appends only new bars for each ticker.
    Also handles brand new tickers (full compute).
    """
    print("\n" + "=" * 70)
    print("  EXPRESSION SERIES CACHE — NIGHTLY APPEND")
    print("=" * 70)

    # Load manifest
    manifest = load_manifest()
    if manifest is None:
        print("\n  No existing cache. Running full build instead.")
        return build_full()

    # Load expression library and verify fingerprint
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)

    if fingerprint != manifest.get("fingerprint"):
        print(f"\n  Expression library changed!")
        print(f"    Cached: {manifest.get('fingerprint')}")
        print(f"    Current: {fingerprint}")
        print(f"  Running full rebuild...")
        return build_full(force=True)

    print(f"\n  Fingerprint OK: {fingerprint}")
    print(f"  Cached tickers: {manifest['n_tickers']}")

    # Load OHLCV
    print("\n  Loading daily OHLCV cache...")
    universe_cache = _load_daily_cache()
    print(f"  {len(universe_cache)} tickers in OHLCV cache")

    # Load HTF OHLCV caches (weekly + monthly from EODHD)
    weekly_cache = _load_htf_cache("weekly")
    monthly_cache = _load_htf_cache("monthly")
    if weekly_cache:
        print(f"  Weekly HTF cache: {len(weekly_cache)} tickers")
    else:
        print(f"  Weekly HTF cache: not found (will resample from daily)")
    if monthly_cache:
        print(f"  Monthly HTF cache: {len(monthly_cache)} tickers")
    else:
        print(f"  Monthly HTF cache: not found (will resample from daily)")

    # Delisting cleanup — remove .npz files for tickers no longer in OHLCV cache
    cached_tickers = manifest.get("tickers", {})
    ohlcv_tickers = set(universe_cache.keys())
    delisted = [t for t in cached_tickers if t not in ohlcv_tickers]
    if delisted:
        print(f"\n  Removing {len(delisted)} delisted tickers from expr cache...")
        for ticker in delisted:
            path = _ticker_cache_path(ticker)
            if os.path.exists(path):
                os.remove(path)
            del cached_tickers[ticker]
        manifest["tickers"] = cached_tickers
        manifest["n_tickers"] = len(cached_tickers)
        save_manifest(manifest)
        print(f"  Removed: {', '.join(delisted[:10])}{'...' if len(delisted) > 10 else ''}")

    # Find tickers that need updating
    work_append = []  # (ticker, df_dict, existing_n_bars) — extend
    work_new = []     # (ticker, df_dict) — full compute

    for ticker, df in universe_cache.items():
        # Truncate to EXPR_CACHE_START for new tickers
        if ticker not in cached_tickers:
            df = _truncate_to_cache_window(df)
            if df is None:
                continue

        if len(df) < 50:
            continue

        df_dict = {
            "date": df["date"].values,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values,
        }

        if ticker in cached_tickers:
            existing_n = cached_tickers[ticker]["n_bars"]
            if len(df) > existing_n:
                work_append.append((ticker, df_dict, existing_n))
        else:
            work_new.append((ticker, df_dict))

    print(f"\n  Tickers to append: {len(work_append)}")
    print(f"  New tickers (full compute): {len(work_new)}")

    if not work_append and not work_new:
        print("  Nothing to do — cache is up to date.")
        del universe_cache, weekly_cache, monthly_cache
        import gc; gc.collect()
        return manifest

    t0 = time.time()
    n_workers = int(os.environ.get("EXPR_CACHE_WORKERS", 14))
    updated = 0
    failed = 0

    # Route tickers: forward-prop (fast) vs full recompute (slow)
    # Forward-prop requires .state + .lookback files to exist.
    fp_work = []     # Forward-prop tickers
    full_work = []   # Full recompute tickers (no .state/.lookback, or new)

    for t, d, existing_n in work_append:
        safe = t.replace("/", "_").replace(".", "_")
        state_exists = os.path.exists(os.path.join(EXPR_CACHE_DIR, f"{safe}.state"))
        lb_exists = os.path.exists(os.path.join(EXPR_CACHE_DIR, f"{safe}.lookback"))
        weekly_df_dict = _df_to_dict(weekly_cache.get(t)) if weekly_cache else None
        monthly_df_dict = _df_to_dict(monthly_cache.get(t)) if monthly_cache else None

        if state_exists and lb_exists:
            # Forward-prop: extract today's OHLCV as single bar
            n_bars = len(d["date"])
            today_ohlcv = {
                "open": float(d["open"][n_bars - 1]),
                "high": float(d["high"][n_bars - 1]),
                "low": float(d["low"][n_bars - 1]),
                "close": float(d["close"][n_bars - 1]),
                "volume": float(d["volume"][n_bars - 1]),
                "date": d["date"][n_bars - 1],
            }
            fp_work.append((t, today_ohlcv, d, weekly_df_dict, monthly_df_dict))
        else:
            full_work.append((t, d, weekly_df_dict, monthly_df_dict))

    for t, d in work_new:
        weekly_df_dict = _df_to_dict(weekly_cache.get(t)) if weekly_cache else None
        monthly_df_dict = _df_to_dict(monthly_cache.get(t)) if monthly_cache else None
        full_work.append((t, d, weekly_df_dict, monthly_df_dict))

    print(f"  Forward-prop: {len(fp_work)} tickers")
    print(f"  Full recompute: {len(full_work)} tickers")

    # Free the large caches
    del universe_cache, weekly_cache, monthly_cache
    import gc; gc.collect()

    # ── Forward-prop batch ──
    if fp_work:
        from forward_prop_engine import _init_fp_worker, _forward_prop_one_ticker
        print(f"\n  Forward-prop {len(fp_work)} tickers ({n_workers} workers)...")
        fp_t0 = time.time()
        max_in_flight = n_workers * 4
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_fp_worker,
            initargs=(expressions,)
        ) as pool:
            pending = {}
            idx = 0
            for _ in range(min(max_in_flight, len(fp_work))):
                if idx < len(fp_work):
                    f = pool.submit(_forward_prop_one_ticker, fp_work[idx])
                    pending[f] = fp_work[idx][0]
                    idx += 1
            while pending:
                done = next(iter(as_completed(pending)))
                ticker_name = pending.pop(done)
                try:
                    t_out, n_bars_out, last_date_out = done.result()
                    if t_out is not None:
                        cached_tickers[t_out] = {
                            "n_bars": n_bars_out,
                            "last_date": last_date_out,
                        }
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                if idx < len(fp_work):
                    f = pool.submit(_forward_prop_one_ticker, fp_work[idx])
                    pending[f] = fp_work[idx][0]
                    idx += 1
                total_done = updated + failed
                if total_done % 100 == 0 or total_done == len(fp_work):
                    elapsed = time.time() - fp_t0
                    rate = total_done / elapsed if elapsed > 0 else 0
                    print(f"    FP: {total_done}/{len(fp_work)} "
                          f"[{elapsed:.0f}s, {rate:.1f}/s]")
        fp_elapsed = time.time() - fp_t0
        print(f"  Forward-prop done: {fp_elapsed:.0f}s")

    # ── Full recompute batch ──
    all_work = full_work
    if all_work:
        label = "Recomputing" if work_append else "Computing"
        print(f"\n  {label} {len(all_work)} tickers ({n_workers} workers)...")
        max_in_flight = n_workers * 4
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_worker,
            initargs=(expressions,)
        ) as pool:
            pending = {}
            work_idx = 0

            def _submit_next():
                nonlocal work_idx
                if work_idx < len(all_work):
                    item = all_work[work_idx]
                    future = pool.submit(_compute_and_save_ticker, item)
                    pending[future] = item[0]
                    work_idx += 1
                    return True
                return False

            def _collect_one(future):
                nonlocal updated, failed
                ticker = pending.pop(future)
                try:
                    ticker_out, n_bars, last_date = future.result()
                    if ticker_out is not None:
                        cached_tickers[ticker_out] = {
                            "n_bars": n_bars,
                            "last_date": last_date,
                        }
                        updated += 1
                    else:
                        failed += 1
                except:
                    failed += 1
                del future
                total_done = updated + failed
                if total_done % 25 == 0 or total_done == len(all_work):
                    elapsed = time.time() - t0
                    rate = total_done / elapsed if elapsed > 0 else 0
                    eta = (len(all_work) - total_done) / rate if rate > 0 else 0
                    pct = total_done / len(all_work) * 100
                    per_ticker = elapsed * n_workers / total_done if total_done > 0 else 0
                    print(f"    {total_done}/{len(all_work)} ({pct:.0f}%) "
                          f"[{elapsed/60:.1f}m elapsed, ~{eta/60:.1f}m left] "
                          f"({updated} ok, {failed} failed) "
                          f"[{per_ticker:.1f}s/ticker, {rate:.1f} tickers/s]")

            # Seed initial batch
            for _ in range(min(max_in_flight, len(all_work))):
                _submit_next()

            # Process as completed, submit replacements
            while pending:
                done_iter = as_completed(pending)
                future = next(done_iter)
                _collect_one(future)
                _submit_next()

    total_time = time.time() - t0

    # Update manifest
    manifest["tickers"] = cached_tickers
    manifest["n_tickers"] = len(cached_tickers)
    manifest["last_append"] = datetime.now(timezone.utc).isoformat()
    manifest["last_append_time_s"] = round(total_time, 1)
    save_manifest(manifest)

    print(f"\n  Append complete: {updated} updated, {failed} failed ({total_time:.0f}s)")
    return manifest


# ══════════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════════

def show_status():
    """Show cache status."""
    print("\n  Expression Series Cache Status")
    print("  " + "─" * 40)

    manifest = load_manifest()
    if manifest is None:
        print("  No cache exists. Run --build first.")
        return

    print(f"  Expressions: {manifest['n_expressions']}")
    print(f"  Fingerprint: {manifest['fingerprint']}")
    print(f"  Tickers: {manifest['n_tickers']}")
    print(f"  Dtype: {manifest.get('dtype', 'float32 (legacy)')}")
    print(f"  Start date: {manifest.get('start_date', 'unknown (legacy)')}")
    print(f"  Built: {manifest.get('built_at', 'unknown')}")
    print(f"  Build time: {manifest.get('build_time_s', '?')}s")

    if manifest.get("last_append"):
        print(f"  Last append: {manifest['last_append']}")
        print(f"  Append time: {manifest.get('last_append_time_s', '?')}s")

    # Check current library match
    try:
        expressions = _load_expressions()
        fp = _expr_fingerprint(expressions)
        if fp == manifest["fingerprint"]:
            print(f"\n  ✓ Expression library matches cache")
        else:
            print(f"\n  ⚠ Expression library CHANGED — rebuild needed")
            print(f"    Cached: {manifest['fingerprint']}")
            print(f"    Current: {fp}")
    except:
        print(f"\n  ? Could not verify expression library")

    # Disk usage
    if os.path.exists(EXPR_CACHE_DIR):
        total_bytes = sum(
            os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
            for f in os.listdir(EXPR_CACHE_DIR)
            if f.endswith(".npz")
        )
        print(f"\n  Disk usage: {total_bytes / (1024**3):.1f} GB "
              f"({total_bytes / (1024**2):.0f} MB)")

    # Sample ticker stats
    tickers = manifest.get("tickers", {})
    if tickers:
        bar_counts = [t["n_bars"] for t in tickers.values()]
        print(f"\n  Bar counts: min={min(bar_counts)}, "
              f"avg={sum(bar_counts)/len(bar_counts):.0f}, "
              f"max={max(bar_counts)}")


# ══════════════════════════════════════════════════════════════
# LOADER — Used by pyramid_grinder.py
# ══════════════════════════════════════════════════════════════

class ExprSeriesCache:
    """Interface for the pyramid grinder to load cached expression series.

    Usage:
        cache = ExprSeriesCache()
        if cache.is_valid():
            dates, data = cache.get_ticker("AAPL")
            # data shape: (n_bars, n_expressions)
            # To get expression j's series: data[:, j]
    """

    def __init__(self):
        self.manifest = load_manifest()
        self._expr_name_to_idx = {}
        if self.manifest:
            for i, name in enumerate(self.manifest.get("expr_names", [])):
                self._expr_name_to_idx[name] = i

    def is_valid(self, expressions=None):
        """Check if cache exists and matches current expression library."""
        if self.manifest is None:
            return False
        if expressions is not None:
            fp = _expr_fingerprint(expressions)
            return fp == self.manifest.get("fingerprint")
        return True

    @property
    def expr_names(self):
        return self.manifest.get("expr_names", []) if self.manifest else []

    @property
    def n_expressions(self):
        return self.manifest.get("n_expressions", 0) if self.manifest else 0

    def expr_index(self, name):
        """Get column index for an expression name."""
        return self._expr_name_to_idx.get(name)

    def get_ticker(self, ticker):
        """Load cached series for a ticker.

        Returns: (dates, data) where data is (n_bars, n_expressions) float32
                 or (None, None) if not cached.
        """
        return load_ticker_cache(ticker)

    def get_ticker_series(self, ticker, expr_indices):
        """Load specific expression columns for a ticker.

        More memory-efficient than loading all columns.

        Args:
            ticker: ticker symbol
            expr_indices: list of expression column indices to load

        Returns: (dates, data) where data is (n_bars, len(expr_indices)) float32
                 or (None, None) if not cached.
        """
        dates, full_data = load_ticker_cache(ticker)
        if dates is None:
            return None, None
        return dates, full_data[:, expr_indices]

    def get_available_tickers(self):
        """Return set of tickers in cache."""
        if self.manifest is None:
            return set()
        return set(self.manifest.get("tickers", {}).keys())

    def get_ticker_bar_count(self, ticker):
        """Get cached bar count for a ticker."""
        if self.manifest is None:
            return 0
        return self.manifest.get("tickers", {}).get(ticker, {}).get("n_bars", 0)


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Expression Series Cache Builder")
    parser.add_argument("--build", action="store_true", help="Full build from scratch")
    parser.add_argument("--append", action="store_true", help="Append new bars (nightly)")
    parser.add_argument("--status", action="store_true", help="Show cache status")
    parser.add_argument("--force", action="store_true", help="Force rebuild even if fresh")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.append:
        append_new_bars()
    elif args.build:
        build_full(force=args.force)
    else:
        parser.print_help()
        print("\n  Hint: Run --build for first time, --append for nightly updates.")


if __name__ == "__main__":
    main()
