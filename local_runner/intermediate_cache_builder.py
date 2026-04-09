"""
Intermediate Cache Builder — Pure numpy computation of 196 indicator intermediates.

Replaces the 16,000-column expression cache with a compact 196-column intermediate cache.
All 16,000 expressions can be derived from these 196 values via dispatch_arith_numpy.

Storage: One .im file per ticker in local_runner/cache/intermediate_series/
  - Header: 4 bytes (uint32 row count)
  - Data: row_count x 196 x float16 (392 bytes per row)
  - Dates: row_count x 10 bytes (YYYY-MM-DD strings)
  Total per ticker: ~470 KB for 1,200 bars.

Usage:
    # Full rebuild (~14 min):
    python local_runner/intermediate_cache_builder.py --build

    # Single ticker validation:
    python local_runner/intermediate_cache_builder.py --validate AAPL

    # Compare against pandas ExpressionEngine:
    python local_runner/intermediate_cache_builder.py --compare AAPL
"""

import os
import sys
import time
import json
import struct
import pickle
import warnings
import argparse
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
IM_CACHE_DIR = os.path.join(CACHE_DIR, "intermediate_series")
EXPR_CACHE_START = "2020-01-02"

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

# ══════════════════════════════════════════════════════════════
# PERIOD CONSTANTS — must match setup_forward_prop.py exactly
# ══════════════════════════════════════════════════════════════

SMA_CLOSE_PERIODS = [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]
EMA_CLOSE_PERIODS = [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200]
SMA_VOL_PERIODS = [10, 20, 50]
MAXH_PERIODS = sorted(set(list(range(5, 125, 5)) + [2, 3, 7, 10, 15, 63, 65, 126]))
MINL_PERIODS = sorted(set(list(range(5, 65, 5)) + [2, 3, 7, 65, 90, 120, 126]))
RSI_PERIODS = [5, 7, 9, 14, 21, 28]
ADX_PERIODS = [7, 10, 14, 20]
STOCH_PERIODS = [3, 5, 7, 9, 10, 14, 21, 28, 50]
CCI_PERIODS = [5, 7, 10, 14, 20, 30, 50]
BOP_PERIODS = [5, 10, 14, 20]
MACD_PAIRS = [(12, 26), (8, 17), (5, 35), (5, 13), (6, 19)]
BOLL_PERIODS = [5, 10, 15, 20, 30, 50]
AROON_PERIODS = [7, 10, 14, 20, 25, 50, 100]
CMF_PERIODS = [10, 14, 20, 30, 50]
KAUF_PERIODS = [5, 7, 10, 15, 20, 30, 50, 65, 100]
MAXC_PERIODS = [10, 20, 50]
MACD_SIGNAL_CONFIGS = [(12, 26, 9), (8, 17, 9), (5, 13, 8), (6, 19, 9)]

N_BASE_INTERMEDIATES = 178
N_ADDITIONAL = 18
N_INTERMEDIATES = N_BASE_INTERMEDIATES + N_ADDITIONAL  # 196


def build_intermediate_column_order():
    """Build the ordered list of 196 intermediate column names."""
    cols = []
    for p in SMA_CLOSE_PERIODS:
        cols.append(f"avgc{p}")
    for p in EMA_CLOSE_PERIODS:
        cols.append(f"xavgc{p}")
    for p in SMA_VOL_PERIODS:
        cols.append(f"avgv{p}")
    for name in ["close", "open", "high", "low", "volume", "atr14", "adr14", "pct"]:
        cols.append(name)
    for p in MAXH_PERIODS:
        cols.append(f"maxh{p}")
    for p in MINL_PERIODS:
        cols.append(f"minl{p}")
    for p in RSI_PERIODS:
        cols.append(f"rsi{p}")
    for p in ADX_PERIODS:
        cols.append(f"adx{p}")
        cols.append(f"diplus{p}")
        cols.append(f"diminus{p}")
    for p in STOCH_PERIODS:
        cols.append(f"stoch{p}")
    for p in CCI_PERIODS:
        cols.append(f"cci{p}")
    for p in BOP_PERIODS:
        cols.append(f"bop{p}")
    cols.append("obv")
    for fast, slow in MACD_PAIRS:
        cols.append(f"macd_{fast}_{slow}")
    for p in BOLL_PERIODS:
        cols.append(f"bbtop_{p}")
        cols.append(f"bbbot_{p}")
        cols.append(f"stddev_{p}")
    for p in AROON_PERIODS:
        cols.append(f"aroon_up_{p}")
        cols.append(f"aroon_down_{p}")
    for p in CMF_PERIODS:
        cols.append(f"cmf_{p}")
    for p in KAUF_PERIODS:
        cols.append(f"kauf_eff_{p}")
    for p in MAXC_PERIODS:
        cols.append(f"maxc{p}")
    # Additional 18 for forward-prop
    for name in ["cumsum_close", "cumsum_volume", "cumsum_hl", "cumsum_tr",
                  "cumsum_bop_raw", "cumsum_mfv", "cumsum_abs_diff",
                  "cumsum_tp", "cumsum_c2", "cumsum_gains", "cumsum_losses"]:
        cols.append(name)
    for name in ["true_range", "gains", "losses", "tp", "bop_raw", "mfv", "abs_diff"]:
        cols.append(name)
    assert len(cols) == N_INTERMEDIATES, f"Expected {N_INTERMEDIATES}, got {len(cols)}"
    return cols


INTERMEDIATE_COLUMNS = build_intermediate_column_order()
INTERMEDIATE_COL_INDEX = {name: i for i, name in enumerate(INTERMEDIATE_COLUMNS)}


# ══════════════════════════════════════════════════════════════
# PURE NUMPY INDICATOR FUNCTIONS
# ══════════════════════════════════════════════════════════════

def np_sma(values, period):
    """Simple moving average matching pandas rolling(period, min_periods=period).mean().

    Uses cumsum fast path when input has no NaN. Falls back to vectorized
    sliding_window_view when NaN is present (any window with a NaN = NaN output).
    """
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result

    if np.any(np.isnan(values)):
        # NaN-safe path: windows containing any NaN produce NaN
        from numpy.lib.stride_tricks import sliding_window_view
        windows = sliding_window_view(values, period)
        valid = ~np.any(np.isnan(windows), axis=1)
        result[period - 1:][valid] = np.mean(windows[valid], axis=1)
        return result

    # Fast path: no NaN, use cumsum
    cs = np.cumsum(values)
    result[period - 1] = cs[period - 1] / period
    if n > period:
        result[period:] = (cs[period:] - cs[:-period]) / period
    return result


def np_ema(values, period):
    """EMA matching pandas ewm(span=period, adjust=False, min_periods=period).

    Handles leading NaN by finding the first valid value and seeding from there.
    Counts non-NaN values for min_periods.
    """
    n = len(values)
    alpha = 2.0 / (period + 1)
    result = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return result

    # Find first non-NaN value to seed EMA
    first_valid = -1
    for i in range(n):
        if not np.isnan(values[i]):
            first_valid = i
            break
    if first_valid < 0:
        return result  # all NaN

    ema = values[first_valid]
    count = 1
    if count >= period:
        result[first_valid] = ema

    for i in range(first_valid + 1, n):
        if np.isnan(values[i]):
            # NaN input: output NaN, don't update EMA state
            continue
        ema = alpha * values[i] + (1.0 - alpha) * ema
        count += 1
        if count >= period:
            result[i] = ema

    return result


def np_rolling_max(values, period):
    """Rolling max. NaN for first period-1 bars."""
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    # Use stride_tricks for efficiency
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(values, period)
    result[period - 1:] = np.nanmax(windows, axis=1)
    return result


def np_rolling_min(values, period):
    """Rolling min. NaN for first period-1 bars."""
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(values, period)
    result[period - 1:] = np.nanmin(windows, axis=1)
    return result


def np_rolling_sum(values, period):
    """Rolling sum via cumsum. NaN for first period-1 bars."""
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    cs = np.cumsum(values)
    result[period - 1] = cs[period - 1]
    if n > period:
        result[period:] = cs[period:] - cs[:-period]
    return result


def np_rolling_std(values, period, ddof=1):
    """Rolling standard deviation (ddof=1 matches pandas default). NaN for first period-1 bars."""
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(values, period)
    result[period - 1:] = np.std(windows, axis=1, ddof=ddof)
    return result


def np_rolling_mean_dev(values, period):
    """Rolling mean absolute deviation from mean. For CCI computation."""
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(values, period)
    means = np.mean(windows, axis=1)
    result[period - 1:] = np.mean(np.abs(windows - means[:, np.newaxis]), axis=1)
    return result


def np_aroon(high_vals, low_vals, period):
    """Aroon Up and Aroon Down. NaN for first period-1 bars."""
    n = len(high_vals)
    up = np.full(n, np.nan, dtype=np.float64)
    down = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return up, down
    from numpy.lib.stride_tricks import sliding_window_view
    h_windows = sliding_window_view(high_vals, period)
    l_windows = sliding_window_view(low_vals, period)
    max_pos = np.argmax(h_windows, axis=1)  # position in window (0-based from left)
    min_pos = np.argmin(l_windows, axis=1)
    bars_since_max = period - 1 - max_pos
    bars_since_min = period - 1 - min_pos
    up[period - 1:] = (period - bars_since_max) / period * 100.0
    down[period - 1:] = (period - bars_since_min) / period * 100.0
    return up, down


def _safe_div_arr(num, denom):
    """Element-wise division, returning NaN where denom is 0 or NaN."""
    result = np.full_like(num, np.nan, dtype=np.float64)
    mask = (denom != 0) & ~np.isnan(denom) & ~np.isnan(num)
    result[mask] = num[mask] / denom[mask]
    return result


# ══════════════════════════════════════════════════════════════
# MAIN COMPUTATION: ALL 196 INTERMEDIATES FROM OHLCV
# ══════════════════════════════════════════════════════════════

def compute_intermediates(close, open_, high, low, volume):
    """Compute all 196 intermediate values from OHLCV arrays.

    Args:
        close, open_, high, low, volume: 1D numpy float64 arrays, same length.

    Returns:
        dict of {column_name: numpy float64 array} with all 196 intermediates.
    """
    n = len(close)
    im = {}

    # ── OHLCV copies ──
    im["close"] = close.copy()
    im["open"] = open_.copy()
    im["high"] = high.copy()
    im["low"] = low.copy()
    im["volume"] = volume.copy()
    im["pct"] = close / 100.0

    # ── Raw per-bar values ──
    prev_c = np.empty(n, dtype=np.float64)
    prev_c[0] = np.nan
    prev_c[1:] = close[:-1]

    prev_h = np.empty(n, dtype=np.float64)
    prev_h[0] = np.nan
    prev_h[1:] = high[:-1]

    prev_l = np.empty(n, dtype=np.float64)
    prev_l[0] = np.nan
    prev_l[1:] = low[:-1]

    # True range
    tr1 = high - low
    tr2 = np.abs(high - prev_c)
    tr3 = np.abs(low - prev_c)
    true_range = np.fmax(tr1, np.fmax(tr2, tr3))
    true_range[0] = tr1[0]  # no prev_c for first bar
    im["true_range"] = true_range

    # Gains/losses for RSI
    delta_c = np.empty(n, dtype=np.float64)
    delta_c[0] = 0.0
    delta_c[1:] = close[1:] - close[:-1]
    gains = np.maximum(delta_c, 0.0)
    losses = np.maximum(-delta_c, 0.0)
    im["gains"] = gains
    im["losses"] = losses

    # Typical price
    tp = (high + low + close) / 3.0
    im["tp"] = tp

    # BOP raw
    hl = high - low
    hl_safe = np.where(hl == 0, np.nan, hl)
    bop_raw = (close - open_) / hl_safe
    bop_raw = np.nan_to_num(bop_raw, nan=0.0)
    im["bop_raw"] = bop_raw

    # MFV for CMF
    mfm = ((close - low) - (high - close)) / hl_safe
    mfm = np.nan_to_num(mfm, nan=0.0)
    mfv = mfm * volume
    im["mfv"] = mfv

    # Abs diff for Kaufman
    abs_diff = np.abs(delta_c)
    im["abs_diff"] = abs_diff

    # ── Cumsums ──
    im["cumsum_close"] = np.cumsum(close)
    im["cumsum_volume"] = np.cumsum(volume)
    im["cumsum_hl"] = np.cumsum(hl)
    im["cumsum_tr"] = np.cumsum(true_range)
    im["cumsum_bop_raw"] = np.cumsum(bop_raw)
    im["cumsum_mfv"] = np.cumsum(mfv)
    im["cumsum_abs_diff"] = np.cumsum(abs_diff)
    im["cumsum_tp"] = np.cumsum(tp)
    im["cumsum_c2"] = np.cumsum(close * close)
    im["cumsum_gains"] = np.cumsum(gains)
    im["cumsum_losses"] = np.cumsum(losses)

    # ── SMA of close ──
    for p in SMA_CLOSE_PERIODS:
        im[f"avgc{p}"] = np_sma(close, p)

    # ── EMA of close ──
    for p in EMA_CLOSE_PERIODS:
        im[f"xavgc{p}"] = np_ema(close, p)

    # ── SMA of volume ──
    for p in SMA_VOL_PERIODS:
        im[f"avgv{p}"] = np_sma(volume, p)

    # ── ATR(14): SMA of true_range ──
    im["atr14"] = np_sma(true_range, 14)

    # ── ADR(14): SMA of (H-L) ──
    im["adr14"] = np_sma(hl, 14)

    # ── Rolling max of high ──
    for p in MAXH_PERIODS:
        im[f"maxh{p}"] = np_rolling_max(high, p)

    # ── Rolling min of low ──
    for p in MINL_PERIODS:
        im[f"minl{p}"] = np_rolling_min(low, p)

    # ── Rolling max of close ──
    for p in MAXC_PERIODS:
        im[f"maxc{p}"] = np_rolling_max(close, p)

    # ── RSI: SMA-based (not Wilder's EMA) ──
    # gains/losses start from bar 1 (bar 0 has no previous close).
    # pandas: delta = series.diff() → first element is NaN → gains[0]=NaN → SMA shifts by 1.
    # Our gains[0]=0 (from delta_c[0]=0), so SMA starts producing 1 bar earlier.
    # Fix: set gains[0] and losses[0] to NaN to match pandas diff() behavior.
    gains_rsi = gains.copy()
    losses_rsi = losses.copy()
    gains_rsi[0] = np.nan
    losses_rsi[0] = np.nan
    for p in RSI_PERIODS:
        avg_gain = np_sma(gains_rsi, p)
        avg_loss = np_sma(losses_rsi, p)
        rs = _safe_div_arr(avg_gain, avg_loss)
        rsi = 100.0 - 100.0 / (1.0 + rs)
        im[f"rsi{p}"] = rsi

    # ── ADX + DI+/DI- ──
    # pandas uses shift(1) which produces NaN at bar 0
    up_move = np.empty(n, dtype=np.float64)
    up_move[0] = np.nan
    up_move[1:] = high[1:] - high[:-1]

    down_move = np.empty(n, dtype=np.float64)
    down_move[0] = np.nan
    down_move[1:] = low[:-1] - low[1:]

    # NaN comparisons evaluate to False, so bar 0 correctly gets DM=0.
    # This matches pandas: np.where with NaN conditions → else branch (0.0).
    dm_plus = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    for p in ADX_PERIODS:
        ema_dmp = np_ema(dm_plus, p)
        ema_dmm = np_ema(dm_minus, p)
        atr_p = np_sma(true_range, p)
        atr_safe = np.where((atr_p == 0) | np.isnan(atr_p), np.nan, atr_p)
        di_plus = 100.0 * ema_dmp / atr_safe
        di_minus = 100.0 * ema_dmm / atr_safe
        im[f"diplus{p}"] = di_plus
        im[f"diminus{p}"] = di_minus

        di_sum = di_plus + di_minus
        dx = _safe_div_arr(np.abs(di_plus - di_minus), di_sum) * 100.0
        adx = np_ema(dx, p)
        im[f"adx{p}"] = adx

    # ── Stochastic: (C - minL) / (maxH - minL) * 100, smoothed SMA(3) ──
    for p in STOCH_PERIODS:
        highest = np_rolling_max(high, p)
        lowest = np_rolling_min(low, p)
        denom = highest - lowest
        denom_safe = np.where(denom == 0, np.nan, denom)
        raw_k = (close - lowest) / denom_safe * 100.0
        stoch = np_sma(raw_k, 3)
        im[f"stoch{p}"] = stoch

    # ── CCI: (TP - SMA(TP)) / (0.015 * mean_dev) ──
    for p in CCI_PERIODS:
        tp_sma = np_sma(tp, p)
        mean_dev = np_rolling_mean_dev(tp, p)
        denom = 0.015 * mean_dev
        denom_safe = np.where((denom == 0) | np.isnan(denom), np.nan, denom)
        im[f"cci{p}"] = (tp - tp_sma) / denom_safe

    # ── BOP: SMA of (C-O)/(H-L) ──
    for p in BOP_PERIODS:
        im[f"bop{p}"] = np_sma(bop_raw, p)

    # ── OBV: cumsum(sign(delta_c) * volume) ──
    # pandas: sign = np.sign(df['close'].diff()) → bar 0 is NaN
    # cumsum of [NaN, v1, v2, ...] → [NaN, v1, v1+v2, ...]
    sign = np.sign(delta_c)
    sign[0] = np.nan  # match pandas diff() producing NaN at bar 0
    obv_raw = sign * volume
    obv = np.nancumsum(obv_raw)  # treat NaN as 0 in cumsum
    obv[0] = np.nan  # but first bar is NaN
    im["obv"] = obv

    # ── MACD: EMA(fast) - EMA(slow) ──
    # Pre-compute all needed EMAs (some may not be in EMA_CLOSE_PERIODS)
    all_ema_periods = set(EMA_CLOSE_PERIODS)
    for fast, slow in MACD_PAIRS:
        all_ema_periods.add(fast)
        all_ema_periods.add(slow)

    ema_cache = {}
    for p in all_ema_periods:
        key = f"xavgc{p}"
        if key in im:
            ema_cache[p] = im[key]
        else:
            ema_cache[p] = np_ema(close, p)

    for fast, slow in MACD_PAIRS:
        im[f"macd_{fast}_{slow}"] = ema_cache[fast] - ema_cache[slow]

    # ── Bollinger bands: SMA +/- 2*std ──
    for p in BOLL_PERIODS:
        sma_p = np_sma(close, p)
        std_p = np_rolling_std(close, p, ddof=1)
        im[f"bbtop_{p}"] = sma_p + 2.0 * std_p
        im[f"bbbot_{p}"] = sma_p - 2.0 * std_p
        im[f"stddev_{p}"] = std_p

    # ── Aroon up/down ──
    for p in AROON_PERIODS:
        au, ad = np_aroon(high, low, p)
        im[f"aroon_up_{p}"] = au
        im[f"aroon_down_{p}"] = ad

    # ── CMF: sum(MFV, P) / sum(V, P) ──
    for p in CMF_PERIODS:
        sum_mfv = np_rolling_sum(mfv, p)
        sum_vol = np_rolling_sum(volume, p)
        im[f"cmf_{p}"] = _safe_div_arr(sum_mfv, sum_vol)

    # ── Kaufman efficiency: |C - C_P_ago| / sum(|diff(C)|, P) ──
    for p in KAUF_PERIODS:
        direction = np.full(n, np.nan, dtype=np.float64)
        direction[p:] = np.abs(close[p:] - close[:-p])
        volatility = np_rolling_sum(abs_diff, p)
        im[f"kauf_eff_{p}"] = _safe_div_arr(direction, volatility)

    # Verify we have all columns
    for col in INTERMEDIATE_COLUMNS:
        assert col in im, f"Missing intermediate: {col}"

    return im


# ══════════════════════════════════════════════════════════════
# COMPARISON: NUMPY vs PANDAS ExpressionEngine
# ══════════════════════════════════════════════════════════════

def compare_against_pandas(ticker, daily_cache):
    """Compare numpy intermediates against pandas ExpressionEngine for one ticker.

    Returns dict with comparison results.
    """
    import pandas as pd
    from scripts.expression_engine import ExpressionEngine
    from expr_cache_builder import build_numpy_intermediates

    df = daily_cache.get(ticker)
    if df is None or len(df) < 100:
        return {"ticker": ticker, "error": "no data or too few bars"}

    # Truncate to EXPR_CACHE_START
    df = df.copy()
    mask = pd.to_datetime(df["date"]) >= pd.Timestamp(EXPR_CACHE_START)
    df = df[mask].reset_index(drop=True)
    if len(df) < 100:
        return {"ticker": ticker, "error": "too few bars after truncation"}

    n = len(df)

    # --- Pandas path ---
    engine = ExpressionEngine(df)
    pandas_im = build_numpy_intermediates(engine)

    # --- Numpy path ---
    c = df["close"].values.astype(np.float64)
    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    v = df["volume"].values.astype(np.float64)
    numpy_im = compute_intermediates(c, o, h, l, v)

    # --- Compare base 178 intermediates ---
    results = {"ticker": ticker, "n_bars": n, "mismatches": [], "total_compared": 0}

    base_cols = INTERMEDIATE_COLUMNS[:N_BASE_INTERMEDIATES]
    for col in base_cols:
        if col not in pandas_im:
            results["mismatches"].append({"col": col, "error": "missing from pandas"})
            continue

        pd_vals = pandas_im[col]
        np_vals = numpy_im[col]

        if len(pd_vals) != len(np_vals):
            results["mismatches"].append({
                "col": col, "error": f"length mismatch: pandas={len(pd_vals)} numpy={len(np_vals)}"
            })
            continue

        # Compare at float16 precision (both rounded to float16)
        pd_f16 = pd_vals.astype(np.float16)
        np_f16 = np_vals[:len(pd_vals)].astype(np.float16)

        # Both NaN = match. One NaN = mismatch. Both finite = compare.
        both_nan = np.isnan(pd_f16) & np.isnan(np_f16)
        both_finite = ~np.isnan(pd_f16) & ~np.isnan(np_f16)
        one_nan = np.isnan(pd_f16) ^ np.isnan(np_f16)

        n_nan_mismatch = int(np.sum(one_nan))
        n_value_mismatch = 0
        if np.any(both_finite):
            diffs = np.abs(pd_f16[both_finite].astype(np.float32) - np_f16[both_finite].astype(np.float32))
            n_value_mismatch = int(np.sum(diffs > 0))

        results["total_compared"] += int(np.sum(both_finite)) + int(np.sum(both_nan))

        if n_nan_mismatch > 0 or n_value_mismatch > 0:
            # Find first mismatch for debugging
            first_idx = -1
            if n_nan_mismatch > 0:
                first_idx = int(np.where(one_nan)[0][0])
            elif n_value_mismatch > 0:
                finite_indices = np.where(both_finite)[0]
                diff_at_finite = np.abs(pd_f16[both_finite].astype(np.float32) - np_f16[both_finite].astype(np.float32))
                diff_idx = np.where(diff_at_finite > 0)[0][0]
                first_idx = int(finite_indices[diff_idx])

            results["mismatches"].append({
                "col": col,
                "nan_mismatches": n_nan_mismatch,
                "value_mismatches": n_value_mismatch,
                "total_bars": n,
                "first_mismatch_bar": first_idx,
                "pandas_val": float(pd_vals[first_idx]) if first_idx >= 0 else None,
                "numpy_val": float(np_vals[first_idx]) if first_idx >= 0 else None,
            })

    return results


# ══════════════════════════════════════════════════════════════
# .im FILE I/O
# ══════════════════════════════════════════════════════════════

def write_im_file(filepath, data_dict, dates):
    """Write intermediate cache to .im file.

    Args:
        filepath: path to .im file
        data_dict: dict of {col_name: numpy float64 array}
        dates: list/array of date strings (YYYY-MM-DD)
    """
    n_rows = len(dates)
    # Build matrix in column order
    matrix = np.empty((n_rows, N_INTERMEDIATES), dtype=np.float16)
    for i, col in enumerate(INTERMEDIATE_COLUMNS):
        vals = data_dict[col][:n_rows].astype(np.float16)
        matrix[:, i] = vals

    with open(filepath, "wb") as f:
        # Header: row count (uint32)
        f.write(struct.pack("<I", n_rows))
        # Data: n_rows x 196 x float16
        f.write(matrix.tobytes())
        # Dates: n_rows x 10 bytes (YYYY-MM-DD, padded)
        for d in dates:
            date_str = str(d)[:10]
            f.write(date_str.encode("ascii").ljust(10, b"\x00"))


def read_im_file(filepath):
    """Read intermediate cache from .im file.

    Returns: (data_matrix_float16, dates_list) or (None, None) if file missing.
    """
    if not os.path.exists(filepath):
        return None, None

    with open(filepath, "rb") as f:
        n_rows = struct.unpack("<I", f.read(4))[0]
        data_bytes = f.read(n_rows * N_INTERMEDIATES * 2)
        matrix = np.frombuffer(data_bytes, dtype=np.float16).reshape(n_rows, N_INTERMEDIATES)
        dates = []
        for _ in range(n_rows):
            d = f.read(10).decode("ascii").strip("\x00")
            dates.append(d)

    return matrix, dates


def read_im_as_dict(filepath):
    """Read .im file and return as {col_name: float32 array} dict."""
    matrix, dates = read_im_file(filepath)
    if matrix is None:
        return None, None
    result = {}
    matrix_f32 = matrix.astype(np.float32)
    for i, col in enumerate(INTERMEDIATE_COLUMNS):
        result[col] = matrix_f32[:, i]
    return result, dates


# ══════════════════════════════════════════════════════════════
# FULL REBUILD
# ══════════════════════════════════════════════════════════════

_w_cache = None


def _init_build_worker(daily_cache):
    global _w_cache
    _w_cache = daily_cache


def _build_one_ticker(ticker):
    """Build .im file for one ticker. Returns (ticker, n_bars, elapsed)."""
    import pandas as pd

    df = _w_cache.get(ticker)
    if df is None or len(df) < 50:
        return ticker, 0, 0.0

    t0 = time.time()

    df = df.copy()
    mask = pd.to_datetime(df["date"]) >= pd.Timestamp(EXPR_CACHE_START)
    df = df[mask].reset_index(drop=True)
    if len(df) < 50:
        return ticker, 0, 0.0

    c = df["close"].values.astype(np.float64)
    o = df["open"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    v = df["volume"].values.astype(np.float64)

    im = compute_intermediates(c, o, h, l, v)

    dates = [str(d)[:10] for d in df["date"].values]
    filepath = os.path.join(IM_CACHE_DIR, f"{ticker}.im")
    write_im_file(filepath, im, dates)

    elapsed = time.time() - t0
    return ticker, len(df), elapsed


def build_full(daily_cache, workers=14):
    """Full rebuild of intermediate cache for all tickers.

    Args:
        daily_cache: dict {ticker: DataFrame}
        workers: number of parallel workers
    """
    os.makedirs(IM_CACHE_DIR, exist_ok=True)

    tickers = sorted(daily_cache.keys())
    print(f"Building intermediate cache for {len(tickers)} tickers ({workers} workers)...")

    t0 = time.time()
    done = 0
    total_bars = 0

    with ProcessPoolExecutor(max_workers=workers,
                             initializer=_init_build_worker,
                             initargs=(daily_cache,)) as pool:
        futures = {pool.submit(_build_one_ticker, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker, n_bars, elapsed = fut.result()
            done += 1
            total_bars += n_bars
            if done % 500 == 0 or done == len(tickers):
                rate = done / (time.time() - t0)
                print(f"  {done}/{len(tickers)} tickers ({rate:.1f}/s, {total_bars:,} bars)")

    elapsed = time.time() - t0
    print(f"Done. {done} tickers, {total_bars:,} bars in {elapsed:.1f}s ({elapsed/60:.1f} min)")


# ══════════════════════════════════════════════════════════════
# OHLCV LOADING
# ══════════════════════════════════════════════════════════════

def _find_main_repo_cache():
    """Find the main repo's cache dir, even from a worktree."""
    # Worktree: .git is a file pointing to the real repo
    git_common = os.path.join(REPO_ROOT, ".git")
    if os.path.isfile(git_common):
        with open(git_common) as f:
            line = f.read().strip()
        if line.startswith("gitdir:"):
            gitdir = line.split(":", 1)[1].strip()
            # gitdir points to .git/worktrees/<name>, go up to repo root
            main_repo = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
            main_cache = os.path.join(main_repo, "local_runner", "cache")
            if os.path.isdir(main_cache):
                return main_cache
    return CACHE_DIR


def load_daily_cache():
    """Load daily OHLCV cache. Searches main repo cache first (for worktrees), then local."""
    main_cache = _find_main_repo_cache()
    search_dirs = [main_cache, CACHE_DIR] if main_cache != CACHE_DIR else [CACHE_DIR]

    for cache_dir in search_dirs:
        for name in ["universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"]:
            path = os.path.join(cache_dir, name)
            if os.path.exists(path):
                print(f"  Loading OHLCV from: {path}")
                with open(path, "rb") as f:
                    return pickle.load(f)
    raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py --daily first.")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intermediate Cache Builder")
    parser.add_argument("--build", action="store_true", help="Full rebuild")
    parser.add_argument("--compare", type=str, help="Compare numpy vs pandas for a ticker")
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args()

    if args.compare:
        print(f"Comparing numpy vs pandas for {args.compare}...")
        cache = load_daily_cache()
        result = compare_against_pandas(args.compare, cache)
        print(f"Ticker: {result['ticker']}, Bars: {result.get('n_bars', 0)}")
        print(f"Total values compared: {result.get('total_compared', 0)}")
        if result.get("mismatches"):
            print(f"MISMATCHES: {len(result['mismatches'])}")
            for m in result["mismatches"]:
                print(f"  {m}")
        else:
            print("ALL MATCH at float16 precision.")

    elif args.build:
        cache = load_daily_cache()
        build_full(cache, workers=args.workers)

    else:
        parser.print_help()
