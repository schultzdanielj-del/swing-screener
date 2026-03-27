"""
Benchmark v6: Batch numpy dispatch for daily arithmetic + HTF

All prior optimizations (numpy bools, vectorized linreg, ext bool_agg cache) PLUS:
- Precompute all needed intermediates as numpy arrays once via ExpressionEngine
- Dispatch daily arithmetic expressions as direct numpy ops (no compute_series)
- Same approach for HTF arithmetic on resampled data

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
from scripts.backtest_conditions import compute_series, compute_on_series

BOOL_OPS = {"count_true", "since_true", "true_in_row"}


# ══════════════════════════════════════════════════════════════
# NUMPY HELPERS (from prior benchmarks)
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


# ══════════════════════════════════════════════════════════════
# NUMPY INTERMEDIATE CACHE — extract from ExpressionEngine
# ══════════════════════════════════════════════════════════════

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
    n = len(arr)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        result[i] = np.nanmax(arr[i - period + 1:i + 1])
    return result

def _rolling_min(arr, period):
    n = len(arr)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        result[i] = np.nanmin(arr[i - period + 1:i + 1])
    return result

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
    """Vectorized percentile rank matching pandas rolling().apply(pct_rank, raw=False).
    Original uses min_periods=2, returns (count <= current) / window_size * 100."""
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(arr)
    result = np.full(n, np.nan)
    if n < 2:
        return result
    # Use min_periods=2 to match original — start producing values at index 1
    # For bars before full window: use available bars
    for i in range(1, min(period - 1, n)):
        window = arr[:i + 1]
        if np.any(np.isnan(window)):
            continue
        result[i] = np.sum(window <= arr[i]) / len(window) * 100.0
    # For bars with full window: vectorized
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
    """Vectorized roc_percentile_rank matching pandas rolling().rank(pct=True)."""
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(close)
    result = np.full(n, np.nan)
    # Compute ROC series
    roc = np.full(n, np.nan)
    roc[roc_period:] = (close[roc_period:] / close[:-roc_period] - 1.0) * 100.0
    # Need lookback window of non-NaN roc values
    # Start index: roc_period + lookback - 1
    start = roc_period + lookback - 1
    if start >= n:
        return result
    # Use sliding_window_view on the roc array starting from roc_period
    roc_valid = roc[roc_period:]
    if len(roc_valid) < lookback:
        return result
    windows = sliding_window_view(roc_valid, lookback)
    # For each window, compute rank of last element (average method, pct=True)
    last_vals = windows[:, -1:]
    # rank = (count_less + 0.5 * count_equal) / n  ... but pandas rank(pct=True) uses
    # rank/count where rank is 1-based average. Simplify: for each window,
    # sort and find position
    lt_count = np.sum(windows < last_vals, axis=1).astype(np.float64)
    eq_count = np.sum(windows == last_vals, axis=1).astype(np.float64)
    rank = lt_count + (eq_count + 1.0) / 2.0  # 1-based average rank
    pct = rank / lookback
    # Handle NaN windows
    nan_mask = np.any(np.isnan(windows), axis=1)
    pct[nan_mask] = np.nan
    result[start:] = pct
    return result


def np_bars_since_cross(close, ma, max_lb):
    """Bars since close crossed above/below MA. Pure numpy."""
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
    """Count swing points in rolling window. Precompute swing bool, then cumsum."""
    result = np.full(n_bars, np.nan)
    swing_f = swing_arr.astype(np.float64)
    cs = np.cumsum(swing_f)
    # Window is [i-p+2, i) exclusive of endpoints — matches original
    # Original: for j in range(i - p + 2, i) means indices i-p+2 to i-1 inclusive
    # That's a window of size p-2 starting at i-p+2
    win_size = period - 2
    if win_size <= 0:
        return result
    for i in range(period + 1, n_bars):
        start = i - period + 2
        end = i  # exclusive (range doesn't include i)
        result[i] = cs[end - 1] - (cs[start - 1] if start > 0 else 0)
    return result


def np_trend_swing_count(swing_arr, values, period, direction, n_bars):
    """Count consecutive higher-highs / lower-lows etc in rolling window."""
    result = np.full(n_bars, np.nan)
    for i in range(period + 1, n_bars):
        # Find swing points in window
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
    """Extract all cached intermediates from ExpressionEngine as numpy arrays.
    
    Calls each engine method once (triggering lazy compute + cache),
    then takes .values to get numpy. After this, all intermediates are
    numpy arrays and we never touch pandas again.
    """
    im = {}
    
    # MAs on close
    for p in [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]:
        im[f"avgc{p}"] = engine._ma(f"avgc{p}").values.astype(np.float64)
    for p in [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200]:
        im[f"xavgc{p}"] = engine._ma(f"xavgc{p}").values.astype(np.float64)
    
    # MAs on volume
    for p in [10, 20, 50]:
        im[f"avgv{p}"] = engine._ma(f"avgv{p}").values.astype(np.float64)
    
    # Core
    im["atr14"] = engine._atr(14).values.astype(np.float64)
    im["adr14"] = engine._adr(14).values.astype(np.float64)
    im["pct"] = engine.c.values.astype(np.float64) / 100.0
    im["close"] = engine.c.values.astype(np.float64)
    im["open"] = engine.o.values.astype(np.float64)
    im["high"] = engine.h.values.astype(np.float64)
    im["low"] = engine.l.values.astype(np.float64)
    im["volume"] = engine.v.values.astype(np.float64)
    
    # Rolling max/min
    for p in sorted(set(list(range(5, 125, 5)) + [2, 3, 7, 10, 15, 63, 65, 126])):
        im[f"maxh{p}"] = engine._maxh(p).values.astype(np.float64)
    for p in sorted(set(list(range(5, 65, 5)) + [2, 3, 7, 65, 90, 120, 126])):
        im[f"minl{p}"] = engine._minl(p).values.astype(np.float64)
    
    # RSI
    for p in [5, 7, 9, 14, 21, 28]:
        im[f"rsi{p}"] = engine._rsi(p).values.astype(np.float64)
    
    # ADX, DI
    for p in [7, 10, 14, 20]:
        im[f"adx{p}"] = engine._adx(p).values.astype(np.float64)
        im[f"diplus{p}"] = engine._diplus(p).values.astype(np.float64)
        im[f"diminus{p}"] = engine._diminus(p).values.astype(np.float64)
    
    # Stochastic, CCI, BOP
    for p in [3, 5, 7, 9, 10, 14, 21, 28, 50]:
        im[f"stoch{p}"] = engine._stoch(p).values.astype(np.float64)
    for p in [5, 7, 10, 14, 20, 30, 50]:
        im[f"cci{p}"] = engine._cci(p).values.astype(np.float64)
    for p in [5, 10, 14, 20]:
        im[f"bop{p}"] = engine._bop(p).values.astype(np.float64)
    
    # OBV, MACD, Bollinger, Aroon, CMF, Kaufman
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
    
    # Rolling max of close (for some ops)
    from scripts.profiling_engine import rolling_max as pd_rolling_max
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
            return None  # Fallback — unknown op
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════

def run_optimized_bools(engine, expressions, indices_by_op, n_bars, data):
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
    import json
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
        ck = (comp["series"], json.dumps(comp["bool_op"], sort_keys=True))
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
        ck = (comp["series"], json.dumps(comp["bool_op"], sort_keys=True))
        if ck in ba_bs_cache or indicator_bool_cache.get(ck) is None: continue
        b = indicator_bool_cache[ck]
        bs = np.full(n_bars, n_bars, dtype=np.float64)
        for i in range(n_bars):
            if b[i]: bs[i] = 0.0
            elif i > 0: bs[i] = bs[i-1] + 1.0
        ba_bs_cache[ck] = bs
    for j in bool_agg_indices:
        comp = expressions[j]["compute"]
        ck = (comp["series"], json.dumps(comp["bool_op"], sort_keys=True))
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


# ══════════════════════════════════════════════════════════════
# BENCHMARK: ORIGINAL
# ══════════════════════════════════════════════════════════════

def benchmark_original(ticker, df, expressions):
    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices, _w_ext_series_name_to_idx,
        _w_lsp_indices, _w_algo_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
        _w_htf_weekly_base, _w_htf_monthly_base,
    )
    n_bars = len(df)
    n_exprs = len(expressions)
    results = {}
    engine = ExpressionEngine(df)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)
    arith_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") not in BOOL_OPS]
    t0 = time.perf_counter()
    op_times = {}
    for j in arith_indices:
        op = expressions[j]["compute"]["op"]
        t_before = time.perf_counter()
        try:
            s = compute_series(engine, expressions[j]["compute"])
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars: data[:, j] = arr
        except: pass
        elapsed = time.perf_counter() - t_before
        op_times[op] = op_times.get(op, 0.0) + elapsed
    results["daily_arith"] = time.perf_counter() - t0
    # Print top 10 slowest ops
    print(f"\n  Daily arith breakdown (top 15 by time):")
    for op, t in sorted(op_times.items(), key=lambda x: -x[1])[:15]:
        cnt = sum(1 for j in arith_indices if expressions[j]["compute"]["op"] == op)
        print(f"    {op:<30} {t*1000:>7.1f}ms  ({cnt} exprs, {t/cnt*1000:.1f}ms each)")
    bool_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") in BOOL_OPS]
    t0 = time.perf_counter()
    for j in bool_indices:
        try:
            s = compute_series(engine, expressions[j]["compute"])
            if s is not None:
                arr = np.asarray(s, dtype=np.float32)
                if len(arr) == n_bars: data[:, j] = arr
        except: pass
    results["daily_bools"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    if _w_lsp_indices:
        try:
            from scripts.lsp_detector_v2 import compute_all_lsp_series
            lsp_dict = compute_all_lsp_series(df)
            for j in _w_lsp_indices:
                col = expressions[j]["compute"]["column"]
                if col in lsp_dict and len(lsp_dict[col]) == n_bars:
                    data[:, j] = lsp_dict[col].astype(np.float32)
        except: pass
    if _w_algo_indices:
        try:
            from scripts.algo_line_detector import compute_all_algo_series
            algo_dict = compute_all_algo_series(df)
            for j in _w_algo_indices:
                col = expressions[j]["compute"]["column"]
                if col in algo_dict and len(algo_dict[col]) == n_bars:
                    data[:, j] = algo_dict[col].astype(np.float32)
        except: pass
    results["lsp_algo"] = time.perf_counter() - t0
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
                            data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                    except: pass
        results[label] = time.perf_counter() - t0
    t0 = time.perf_counter()
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        sr = {}
        for sn, si in _w_ext_series_name_to_idx.items():
            cd = data[:, si]
            if not np.all(np.isnan(cd)): sr[sn] = cd.astype(np.float64)
        if sr:
            for j in _w_ext_struct_indices:
                try:
                    s = compute_series(engine, expressions[j]["compute"], series_registry=sr)
                    if s is not None:
                        arr = np.asarray(s, dtype=np.float32)
                        if len(arr) == n_bars: data[:, j] = arr
                except: pass
    results["ext_struct"] = time.perf_counter() - t0
    return data, results, sum(results.values())


# ══════════════════════════════════════════════════════════════
# BENCHMARK: OPTIMIZED
# ══════════════════════════════════════════════════════════════

def benchmark_optimized(ticker, df, expressions):
    from expr_cache_builder import (
        _w_daily_indices, _w_ext_struct_indices, _w_ext_series_name_to_idx,
        _w_lsp_indices, _w_algo_indices,
        _w_htf_weekly_indices, _w_htf_monthly_indices,
        _w_htf_weekly_base, _w_htf_monthly_base,
    )
    n_bars = len(df)
    n_exprs = len(expressions)
    results = {}
    engine = ExpressionEngine(df)
    data = np.full((n_bars, n_exprs), np.nan, dtype=np.float32)

    # ── Daily arithmetic: numpy replacements for slow ops, compute_series for rest ──
    arith_indices = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") not in BOOL_OPS]
    
    SLOW_OPS = {"percentile_rank", "roc_percentile_rank", "bars_since_ma_cross",
                "swing_high_count", "swing_low_count",
                "higher_high_count", "higher_low_count", "lower_high_count", "lower_low_count"}
    
    t0 = time.perf_counter()
    
    # Pre-extract numpy arrays for slow op replacements
    c_vals = engine.c.values.astype(np.float64)
    h_vals = engine.h.values.astype(np.float64)
    l_vals = engine.l.values.astype(np.float64)
    v_vals = engine.v.values.astype(np.float64)
    
    # Cache for percentile_rank sources and swing point arrays
    pct_rank_cache = {}
    swing_high_arr = None  # bool array: is this bar a swing high?
    swing_low_arr = None
    
    opt_op_times = {}
    
    for j in arith_indices:
        comp = expressions[j]["compute"]
        op = comp["op"]
        t_op = time.perf_counter()
        
        if op == "percentile_rank":
            source = comp["source"]
            period = comp["period"]
            # Get source array (cached)
            if source not in pct_rank_cache:
                if source == "close": pct_rank_cache[source] = c_vals
                elif source == "volume": pct_rank_cache[source] = v_vals
                elif source == "range": pct_rank_cache[source] = h_vals - l_vals
                elif source == "atr14": pct_rank_cache[source] = engine._atr(14).values.astype(np.float64)
                elif source == "rsi14": pct_rank_cache[source] = engine._rsi(14).values.astype(np.float64)
                else: pct_rank_cache[source] = c_vals
            s = pct_rank_cache[source]
            data[:, j] = np_percentile_rank(s, period).astype(np.float32)
        
        elif op == "roc_percentile_rank":
            roc_p = comp["roc_period"]
            lb = comp["lookback"]
            data[:, j] = np_roc_percentile_rank(c_vals, roc_p, lb).astype(np.float32)
        
        elif op == "bars_since_ma_cross":
            ma = engine._ma(comp["ma"]).values.astype(np.float64)
            max_lb = comp.get("max_lookback", 120)
            data[:, j] = np_bars_since_cross(c_vals, ma, max_lb).astype(np.float32)
        
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
        
        else:
            # All other ops: use original compute_series (still fast with engine cache)
            try:
                s = compute_series(engine, comp)
                if s is not None:
                    arr = np.asarray(s, dtype=np.float32)
                    if len(arr) == n_bars: data[:, j] = arr
            except: pass
        
        opt_op_times[op] = opt_op_times.get(op, 0.0) + (time.perf_counter() - t_op)
    
    results["daily_arith"] = time.perf_counter() - t0
    
    # Print optimized daily arith breakdown
    print(f"\n  Optimized daily arith breakdown (top 15 by time):")
    for opn, t in sorted(opt_op_times.items(), key=lambda x: -x[1])[:15]:
        cnt = sum(1 for j in arith_indices if expressions[j]["compute"]["op"] == opn)
        print(f"    {opn:<30} {t*1000:>7.1f}ms  ({cnt} exprs, {t/cnt*1000:.1f}ms each)")

    # ── Daily booleans: numpy optimized ──
    ct = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "count_true"]
    st = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "since_true"]
    tir = [j for j in _w_daily_indices if expressions[j]["compute"].get("op") == "true_in_row"]
    t0 = time.perf_counter()
    run_optimized_bools(engine, expressions, (ct, st, tir), n_bars, data)
    results["daily_bools"] = time.perf_counter() - t0

    # ── LSP + Algo ──
    t0 = time.perf_counter()
    if _w_lsp_indices:
        try:
            from scripts.lsp_detector_v2 import compute_all_lsp_series
            lsp_dict = compute_all_lsp_series(df)
            for j in _w_lsp_indices:
                col = expressions[j]["compute"]["column"]
                if col in lsp_dict and len(lsp_dict[col]) == n_bars:
                    data[:, j] = lsp_dict[col].astype(np.float32)
        except: pass
    if _w_algo_indices:
        try:
            from scripts.algo_line_detector import compute_all_algo_series
            algo_dict = compute_all_algo_series(df)
            for j in _w_algo_indices:
                col = expressions[j]["compute"]["column"]
                if col in algo_dict and len(algo_dict[col]) == n_bars:
                    data[:, j] = algo_dict[col].astype(np.float32)
        except: pass
    results["lsp_algo"] = time.perf_counter() - t0

    # ── HTF: targeted slow-op interception + bool optimization ──
    ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}
    for freq, label, htf_indices, htf_base in [
        ("W", "htf_weekly", _w_htf_weekly_indices, _w_htf_weekly_base),
        ("ME", "htf_monthly", _w_htf_monthly_indices, _w_htf_monthly_base),
    ]:
        t0 = time.perf_counter()
        if htf_indices:
            htf_df = resample_ohlcv(df, freq)
            if htf_df is not None and len(htf_df) >= 5:
                t_resample = time.perf_counter() - t0
                
                htf_map = build_htf_to_daily_map(df["date"], htf_df, freq)
                t_eng0 = time.perf_counter()
                htf_engine = ExpressionEngine(htf_df)
                t_eng = time.perf_counter() - t_eng0
                htf_n = len(htf_df)
                
                # Extract HTF numpy arrays for slow-op replacements
                htf_c = htf_df["close"].values.astype(np.float64)
                htf_h = htf_df["high"].values.astype(np.float64)
                htf_l = htf_df["low"].values.astype(np.float64)
                htf_v = htf_df["volume"].values.astype(np.float64)
                
                # Classify HTF expressions
                htf_ct = []
                htf_st = []
                htf_tir = []
                htf_ext = []
                htf_slow = []  # slow ops that get numpy replacement
                htf_fast = []  # everything else → compute_series
                
                for k, j in enumerate(htf_indices):
                    base_op = htf_base[k].get("op", "")
                    if base_op == "count_true": htf_ct.append((k, j))
                    elif base_op == "since_true": htf_st.append((k, j))
                    elif base_op == "true_in_row": htf_tir.append((k, j))
                    elif base_op in ON_SERIES_OPS: htf_ext.append((k, j))
                    elif base_op in SLOW_OPS: htf_slow.append((k, j))
                    else: htf_fast.append((k, j))
                
                # HTF slow ops — numpy replacements
                htf_pct_cache = {}
                htf_swing_high = None
                htf_swing_low = None
                
                for k, j in htf_slow:
                    comp = htf_base[k]
                    op = comp["op"]
                    try:
                        if op == "percentile_rank":
                            source = comp["source"]
                            period = comp["period"]
                            if source not in htf_pct_cache:
                                if source == "close": htf_pct_cache[source] = htf_c
                                elif source == "volume": htf_pct_cache[source] = htf_v
                                elif source == "range": htf_pct_cache[source] = htf_h - htf_l
                                elif source == "atr14": htf_pct_cache[source] = htf_engine._atr(14).values.astype(np.float64)
                                elif source == "rsi14": htf_pct_cache[source] = htf_engine._rsi(14).values.astype(np.float64)
                                else: htf_pct_cache[source] = htf_c
                            arr = np_percentile_rank(htf_pct_cache[source], period)
                            data[:, j] = map_htf_series_to_daily(arr.astype(np.float32), htf_map)
                        elif op == "roc_percentile_rank":
                            arr = np_roc_percentile_rank(htf_c, comp["roc_period"], comp["lookback"])
                            data[:, j] = map_htf_series_to_daily(arr.astype(np.float32), htf_map)
                        elif op == "bars_since_ma_cross":
                            ma = htf_engine._ma(comp["ma"]).values.astype(np.float64)
                            arr = np_bars_since_cross(htf_c, ma, comp.get("max_lookback", 120))
                            data[:, j] = map_htf_series_to_daily(arr.astype(np.float32), htf_map)
                        elif op in ("swing_high_count", "swing_low_count"):
                            p = comp["period"]
                            if op == "swing_high_count":
                                if htf_swing_high is None:
                                    htf_swing_high = np.zeros(htf_n, dtype=bool)
                                    for i in range(1, htf_n - 1):
                                        htf_swing_high[i] = htf_h[i] > htf_h[i-1] and htf_h[i] > htf_h[i+1]
                                arr = np_swing_count_rolling(htf_swing_high, p, htf_n)
                            else:
                                if htf_swing_low is None:
                                    htf_swing_low = np.zeros(htf_n, dtype=bool)
                                    for i in range(1, htf_n - 1):
                                        htf_swing_low[i] = htf_l[i] < htf_l[i-1] and htf_l[i] < htf_l[i+1]
                                arr = np_swing_count_rolling(htf_swing_low, p, htf_n)
                            data[:, j] = map_htf_series_to_daily(arr.astype(np.float32), htf_map)
                        elif op in ("higher_high_count", "lower_high_count"):
                            p = comp["period"]
                            if htf_swing_high is None:
                                htf_swing_high = np.zeros(htf_n, dtype=bool)
                                for i in range(1, htf_n - 1):
                                    htf_swing_high[i] = htf_h[i] > htf_h[i-1] and htf_h[i] > htf_h[i+1]
                            direction = "higher" if op == "higher_high_count" else "lower"
                            arr = np_trend_swing_count(htf_swing_high, htf_h, p, direction, htf_n)
                            data[:, j] = map_htf_series_to_daily(arr.astype(np.float32), htf_map)
                        elif op in ("higher_low_count", "lower_low_count"):
                            p = comp["period"]
                            if htf_swing_low is None:
                                htf_swing_low = np.zeros(htf_n, dtype=bool)
                                for i in range(1, htf_n - 1):
                                    htf_swing_low[i] = htf_l[i] < htf_l[i-1] and htf_l[i] < htf_l[i+1]
                            direction = "higher" if op == "higher_low_count" else "lower"
                            arr = np_trend_swing_count(htf_swing_low, htf_l, p, direction, htf_n)
                            data[:, j] = map_htf_series_to_daily(arr.astype(np.float32), htf_map)
                        else:
                            s = compute_series(htf_engine, comp)
                            if s is not None:
                                data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                    except: pass
                
                # HTF fast ops — compute_series (already fast with engine cache)
                for k, j in htf_fast:
                    try:
                        s = compute_series(htf_engine, htf_base[k])
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                    except: pass
                
                # HTF booleans — numpy
                htf_bool_cache = {}
                for k, j in htf_ct + htf_st + htf_tir:
                    cond = htf_base[k]["condition"]
                    if cond not in htf_bool_cache:
                        try:
                            htf_bool_cache[cond] = htf_engine._bool_series(cond).values.astype(bool)
                        except:
                            htf_bool_cache[cond] = np.zeros(htf_n, dtype=bool)
                for k, j in htf_ct:
                    b = htf_bool_cache[htf_base[k]["condition"]]
                    data[:, j] = map_htf_series_to_daily(np_count_true(b, htf_base[k]["period"]).astype(np.float32), htf_map)
                htf_bs = {}
                for k, j in htf_st:
                    cond = htf_base[k]["condition"]
                    if cond not in htf_bs:
                        b = htf_bool_cache[cond]
                        bs = np.full(htf_n, htf_n, dtype=np.float64)
                        for i in range(htf_n):
                            if b[i]: bs[i] = 0.0
                            elif i > 0: bs[i] = bs[i-1] + 1.0
                        htf_bs[cond] = bs
                for k, j in htf_st:
                    bs = htf_bs[htf_base[k]["condition"]]
                    p = htf_base[k]["period"]
                    r = np.full(htf_n, np.nan)
                    for i in range(p-1, htf_n):
                        r[i] = bs[i] if bs[i] < p else -1.0
                    data[:, j] = map_htf_series_to_daily(r.astype(np.float32), htf_map)
                for k, j in htf_tir:
                    b = htf_bool_cache[htf_base[k]["condition"]]
                    data[:, j] = map_htf_series_to_daily(np_true_in_row(b, htf_base[k]["period"]).astype(np.float32), htf_map)
                
                # HTF ext struct — compute_series (small arrays, not worth optimizing)
                for k, j in htf_ext:
                    try:
                        s = compute_series(htf_engine, htf_base[k])
                        if s is not None:
                            data[:, j] = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                    except: pass
                
                print(f"\n  HTF {freq} sub-phases:")
                print(f"    resample+map:  {t_resample*1000:.0f}ms")
                print(f"    engine init:   {t_eng*1000:.0f}ms")
                print(f"    slow ops:      {len(htf_slow)} exprs")
                print(f"    fast ops:      {len(htf_fast)} exprs")
                print(f"    bools:         {len(htf_ct)+len(htf_st)+len(htf_tir)} exprs")
                print(f"    ext struct:    {len(htf_ext)} exprs")
        results[label] = time.perf_counter() - t0

    # ── Extension structure ──
    t0 = time.perf_counter()
    if _w_ext_struct_indices and _w_ext_series_name_to_idx:
        run_optimized_ext_struct(engine, expressions, _w_ext_struct_indices,
                                _w_ext_series_name_to_idx, n_bars, data)
    results["ext_struct"] = time.perf_counter() - t0

    total = sum(results.values())
    return data, results, total


# ══════════════════════════════════════════════════════════════
# COMPARISON + MAIN
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
        if nan_mm + val_mm == 0: n_match += 1
        else:
            n_mismatch += 1
            if len(examples) < 10:
                examples.append(f"{expressions[j]['name']}: {nan_mm} NaN, {val_mm} val")
    return n_match, n_mismatch, max_diff, worst, examples


def main():
    print("\n" + "=" * 70)
    print("  EXPRESSION CACHE — BENCHMARK v8b (slow op fix + HTF slow ops)")
    print("=" * 70)
    expressions = _load_expressions()
    _init_worker(expressions)
    print(f"\n  {len(expressions)} expressions")
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
        print(f"  {phase:<25} {secs:>7.3f}   {secs/total_orig*100:>5.1f}%")
    print(f"  {'─' * 50}")
    print(f"  {'TOTAL':<25} {total_orig:>7.3f}   100.0%")

    # ═══ OPTIMIZED ═══
    print(f"\n  {'─' * 60}")
    print(f"  OPTIMIZED (batch numpy dispatch + all prior optimizations)")
    print(f"  {'─' * 60}")
    data_opt, res_opt, total_opt = benchmark_optimized(ticker, df, expressions)
    print(f"\n  Phase                     Time (s)    %")
    print(f"  {'─' * 50}")
    for phase, secs in res_opt.items():
        print(f"  {phase:<25} {secs:>7.3f}   {secs/total_opt*100:>5.1f}%")
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
    for phase in res_orig:
        if phase in res_opt:
            o, n = res_orig[phase], res_opt[phase]
            saved = o - n
            if saved > 0.1:
                print(f"  {phase}: {o:.3f}s -> {n:.3f}s ({o/n:.1f}x, saved {saved:.1f}s)")

    # ═══ CORRECTNESS ═══
    print(f"\n  {'─' * 60}")
    print(f"  CORRECTNESS")
    print(f"  {'─' * 60}")
    m, mm, md, w, ex = compare_outputs(data_orig, data_opt, expressions, list(range(len(expressions))))
    print(f"\n  All expressions ({len(expressions)}):")
    print(f"    Match: {m}, Mismatch: {mm}")
    print(f"    Max diff: {md:.6f} ({w})")
    for e in ex[:10]:
        print(f"      {e}")

    # ═══ ESTIMATES ═══
    print(f"\n  {'=' * 60}")
    print(f"  FULL BUILD + NIGHTLY ESTIMATES")
    print(f"  {'=' * 60}")
    print(f"  Per ticker: {total_orig:.1f}s original, {total_opt:.1f}s optimized")
    for label, n_tickers in [("Nightly (10,856 tickers)", 10856)]:
        orig_est = total_orig * n_tickers / 8
        opt_est = total_opt * n_tickers / 8
        print(f"\n  {label}:")
        print(f"    Original:   {orig_est:.0f}s ({orig_est/60:.1f} min)")
        print(f"    Optimized:  {opt_est:.0f}s ({opt_est/60:.1f} min)")


if __name__ == "__main__":
    main()
