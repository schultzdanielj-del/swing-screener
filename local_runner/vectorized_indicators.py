"""
Vectorized 2D Indicator Library — Numpy implementations of all base intermediates.

All functions operate on 2D arrays with shape (n_tickers, n_bars).
NaN padding is preserved — tickers with fewer bars have leading NaNs.

These must produce identical results to the pandas implementations in
scripts/profiling_engine.py (within float32 tolerance).
"""

import numpy as np


# ══════════════════════════════════════════════════════════════
# MOVING AVERAGES
# ══════════════════════════════════════════════════════════════

def sma_2d(arr, period):
    """SMA via cumsum trick. Shape: (n_tickers, n_bars) → same.
    
    First (period-1) valid bars are NaN (matches pandas min_periods=period).
    NaN-containing windows produce NaN (matches pandas default).
    """
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    
    # Replace NaN with 0 for cumsum, track valid counts
    valid = ~np.isnan(arr)
    safe = np.where(valid, arr, 0.0)
    
    cs = np.cumsum(safe, axis=1)
    cv = np.cumsum(valid.astype(np.float64), axis=1)
    
    # SMA = (cumsum[i] - cumsum[i-period]) / period
    result[:, period-1:] = (cs[:, period-1:] - np.concatenate([np.zeros((n_t, 1)), cs[:, :-period]], axis=1)) / period
    
    # Count valid bars in each window
    counts = cv[:, period-1:] - np.concatenate([np.zeros((n_t, 1)), cv[:, :-period]], axis=1)
    
    # NaN where fewer than period valid bars
    mask = counts < period
    result[:, period-1:][mask] = np.nan
    
    return result


def ema_2d(arr, period):
    """EMA via sequential loop over bars, vectorized across tickers.
    
    Matches pandas ewm(span=period, adjust=False, min_periods=period).
    Seed: ema[first_valid] = x[first_valid], then apply formula.
    Output NaN until min_periods valid bars have been seen.
    """
    n_t, n_b = arr.shape
    alpha = 2.0 / (period + 1)
    result = np.full_like(arr, np.nan)
    
    valid = ~np.isnan(arr)
    
    ema_val = np.zeros(n_t)
    started = np.zeros(n_t, dtype=bool)  # have we seen first valid bar?
    count = np.zeros(n_t, dtype=np.int64)  # valid bars seen so far
    
    for j in range(n_b):
        v = valid[:, j]
        vals = arr[:, j]
        
        # First valid bar: seed EMA
        just_starting = v & ~started
        ema_val[just_starting] = vals[just_starting]
        started[just_starting] = True
        count[just_starting] = 1
        
        # Already started and current bar is valid: apply formula
        continuing = v & started & ~just_starting
        ema_val[continuing] = alpha * vals[continuing] + (1 - alpha) * ema_val[continuing]
        count[continuing] += 1
        
        # Already started and current bar is NaN: carry forward
        # (ema_val stays the same, count doesn't increment)
        
        # Output where we've seen >= min_periods valid bars
        output_mask = started & (count >= period)
        result[output_mask, j] = ema_val[output_mask]
    
    return result


def hma_2d(arr, period):
    """Hull Moving Average = WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    half = max(int(period / 2), 1)
    sqrt_p = max(int(np.sqrt(period)), 1)
    wma_half = wma_2d(arr, half)
    wma_full = wma_2d(arr, period)
    diff = 2 * wma_half - wma_full
    return wma_2d(diff, sqrt_p)


def wma_2d(arr, period):
    """Weighted Moving Average (linear weights). Matches profiling_engine._wma."""
    n_t, n_b = arr.shape
    weights = np.arange(1, period + 1, dtype=np.float64)
    weights = weights / weights.sum()
    
    result = np.full_like(arr, np.nan)
    
    for j in range(period - 1, n_b):
        window = arr[:, j - period + 1:j + 1]  # (n_t, period)
        # Check for NaN in any position
        has_nan = np.any(np.isnan(window), axis=1)
        dot = np.dot(window, weights)  # (n_t,)
        dot[has_nan] = np.nan
        result[:, j] = dot
    
    return result


# ══════════════════════════════════════════════════════════════
# ROLLING MAX / MIN / SUM
# ══════════════════════════════════════════════════════════════

def rolling_max_2d(arr, period):
    """Rolling maximum. Matches pandas rolling(period, min_periods=period).max()."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    
    for j in range(period - 1, n_b):
        window = arr[:, j - period + 1:j + 1]
        # nanmax but NaN if any NaN present (to match pandas min_periods=period)
        has_nan = np.any(np.isnan(window), axis=1)
        mx = np.nanmax(window, axis=1)
        mx[has_nan] = np.nan
        result[:, j] = mx
    
    return result


def rolling_min_2d(arr, period):
    """Rolling minimum. Matches pandas rolling(period, min_periods=period).min()."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    
    for j in range(period - 1, n_b):
        window = arr[:, j - period + 1:j + 1]
        has_nan = np.any(np.isnan(window), axis=1)
        mn = np.nanmin(window, axis=1)
        mn[has_nan] = np.nan
        result[:, j] = mn
    
    return result


def rolling_sum_2d(arr, period):
    """Rolling sum via cumsum trick. Matches pandas rolling(period, min_periods=period).sum()."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    
    valid = ~np.isnan(arr)
    safe = np.where(valid, arr, 0.0)
    
    cs = np.cumsum(safe, axis=1)
    cv = np.cumsum(valid.astype(np.float64), axis=1)
    
    result[:, period-1:] = cs[:, period-1:] - np.concatenate([np.zeros((n_t, 1)), cs[:, :-period]], axis=1)
    
    counts = cv[:, period-1:] - np.concatenate([np.zeros((n_t, 1)), cv[:, :-period]], axis=1)
    mask = counts < period
    result[:, period-1:][mask] = np.nan
    
    return result


def rolling_std_2d(arr, period):
    """Rolling standard deviation. Matches pandas rolling(period, min_periods=period).std()."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    
    for j in range(period - 1, n_b):
        window = arr[:, j - period + 1:j + 1]
        has_nan = np.any(np.isnan(window), axis=1)
        # ddof=1 to match pandas default
        sd = np.std(window, axis=1, ddof=1)
        sd[has_nan] = np.nan
        result[:, j] = sd
    
    return result


# ══════════════════════════════════════════════════════════════
# TRUE RANGE / ATR / ADR
# ══════════════════════════════════════════════════════════════

def true_range_2d(h, l, c):
    """True Range = max(H-L, |H-C1|, |L-C1|). Shape: (n_t, n_b)."""
    c1 = np.full_like(c, np.nan)
    c1[:, 1:] = c[:, :-1]
    
    tr1 = h - l
    tr2 = np.abs(h - c1)
    tr3 = np.abs(l - c1)
    
    return np.fmax(np.fmax(tr1, tr2), tr3)


def atr_2d(h, l, c, period):
    """ATR = SMA(TrueRange, period). TC2000 style, not Wilder's."""
    tr = true_range_2d(h, l, c)
    return sma_2d(tr, period)


def adr_2d(h, l, period):
    """Average Daily Range = SMA(H - L, period)."""
    return sma_2d(h - l, period)


# ══════════════════════════════════════════════════════════════
# RSI
# ══════════════════════════════════════════════════════════════

def rsi_2d(c, period):
    """RSI using SMA of gains/losses. Matches profiling_engine.rsi().
    
    delta = close.diff(). First bar of valid data produces NaN diff.
    gains = max(delta, 0), losses = max(-delta, 0).
    RSI = 100 - 100/(1 + SMA(gains)/SMA(losses)).
    """
    delta = np.full_like(c, np.nan)
    delta[:, 1:] = c[:, 1:] - c[:, :-1]
    
    gains = np.where(np.isnan(delta), np.nan, np.maximum(delta, 0.0))
    losses = np.where(np.isnan(delta), np.nan, np.maximum(-delta, 0.0))
    
    avg_gain = sma_2d(gains, period)
    avg_loss = sma_2d(losses, period)
    
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where((avg_loss != 0) & ~np.isnan(avg_loss), avg_gain / avg_loss, np.nan)
        result = 100.0 - (100.0 / (1.0 + rs))
    
    result[np.isnan(avg_gain) | np.isnan(avg_loss)] = np.nan
    return result


# ══════════════════════════════════════════════════════════════
# STOCHASTIC
# ══════════════════════════════════════════════════════════════

def stochastic_2d(h, l, c, period, smooth=3):
    """Stochastic %K = SMA(smooth) of raw %K. Matches profiling_engine.stochastic_k()."""
    highest = rolling_max_2d(h, period)
    lowest = rolling_min_2d(l, period)
    rng = highest - lowest
    
    with np.errstate(divide='ignore', invalid='ignore'):
        raw_k = np.where(rng != 0, (c - lowest) / rng * 100, np.nan)
    raw_k[np.isnan(rng)] = np.nan
    
    return sma_2d(raw_k, smooth)


# ══════════════════════════════════════════════════════════════
# ADX / DI+ / DI-
# ══════════════════════════════════════════════════════════════

def _di_plus_minus_2d(h, l, c, period):
    """Compute DI+ and DI- together. Matches profiling_engine._di_plus_minus()."""
    up = np.full_like(h, np.nan)
    down = np.full_like(h, np.nan)
    up[:, 1:] = h[:, 1:] - h[:, :-1]
    down[:, 1:] = l[:, :-1] - l[:, 1:]
    
    # Pandas behavior: np.where with NaN comparisons yields 0.0 (condition is False).
    # The resulting pd.Series keeps 0.0 at bar 0, not NaN.
    # We must match this so EMA seeds at the same bar.
    up_safe = np.nan_to_num(up, nan=0.0)
    down_safe = np.nan_to_num(down, nan=0.0)
    
    dm_plus = np.where((up_safe > down_safe) & (up_safe > 0), up_safe, 0.0)
    dm_minus = np.where((down_safe > up_safe) & (down_safe > 0), down_safe, 0.0)
    
    atr_val = atr_2d(h, l, c, period)
    with np.errstate(divide='ignore', invalid='ignore'):
        safe_atr = np.where(atr_val != 0, atr_val, np.nan)
    
    dip = 100 * ema_2d(dm_plus, period) / safe_atr
    dim = 100 * ema_2d(dm_minus, period) / safe_atr
    
    return dip, dim


def adx_2d(h, l, c, period):
    """ADX = EMA(DX, period). Matches profiling_engine.adx()."""
    dip, dim = _di_plus_minus_2d(h, l, c, period)
    
    with np.errstate(divide='ignore', invalid='ignore'):
        denom = dip + dim
        safe_denom = np.where(denom != 0, denom, np.nan)
        dx = np.abs(dip - dim) / safe_denom * 100
    
    return ema_2d(dx, period)


def di_plus_2d(h, l, c, period):
    """DI+."""
    dip, _ = _di_plus_minus_2d(h, l, c, period)
    return dip


def di_minus_2d(h, l, c, period):
    """DI-."""
    _, dim = _di_plus_minus_2d(h, l, c, period)
    return dim


# ══════════════════════════════════════════════════════════════
# CCI
# ══════════════════════════════════════════════════════════════

def cci_2d(h, l, c, period):
    """CCI. Matches profiling_engine.cci()."""
    tp = (h + l + c) / 3.0
    tp_sma = sma_2d(tp, period)
    
    n_t, n_b = tp.shape
    mean_dev = np.full_like(tp, np.nan)
    
    for j in range(period - 1, n_b):
        window = tp[:, j - period + 1:j + 1]  # (n_t, period)
        has_nan = np.any(np.isnan(window), axis=1)
        window_mean = np.nanmean(window, axis=1, keepdims=True)
        md = np.mean(np.abs(window - window_mean), axis=1)
        md[has_nan] = np.nan
        mean_dev[:, j] = md
    
    with np.errstate(divide='ignore', invalid='ignore'):
        safe_md = np.where(mean_dev != 0, mean_dev, np.nan)
        result = (tp - tp_sma) / (0.015 * safe_md)
    
    return result


# ══════════════════════════════════════════════════════════════
# MACD
# ══════════════════════════════════════════════════════════════

def macd_2d(c, fast, slow):
    """MACD = EMA(fast) - EMA(slow)."""
    return ema_2d(c, fast) - ema_2d(c, slow)


# ══════════════════════════════════════════════════════════════
# BOLLINGER BANDS
# ══════════════════════════════════════════════════════════════

def bollinger_top_2d(c, period, nstd=2.0):
    """Bollinger top = SMA + nstd * stddev."""
    mid = sma_2d(c, period)
    sd = rolling_std_2d(c, period)
    return mid + nstd * sd


def bollinger_bot_2d(c, period, nstd=2.0):
    """Bollinger bottom = SMA - nstd * stddev."""
    mid = sma_2d(c, period)
    sd = rolling_std_2d(c, period)
    return mid - nstd * sd


# ══════════════════════════════════════════════════════════════
# OBV
# ══════════════════════════════════════════════════════════════

def obv_2d(c, v):
    """On Balance Volume. Matches profiling_engine.obv().
    
    sign = sign(close.diff()), then cumsum(sign * volume).
    Pandas cumsum skips NaN (treats as 0 for accumulation, preserves NaN in output).
    """
    diff = np.full_like(c, np.nan)
    diff[:, 1:] = c[:, 1:] - c[:, :-1]
    sign = np.sign(diff)
    sv = sign * v
    
    # Match pandas cumsum: NaN positions get 0 contribution, output stays NaN
    nan_mask = np.isnan(sv)
    safe_sv = np.where(nan_mask, 0.0, sv)
    result = np.cumsum(safe_sv, axis=1)
    result[nan_mask] = np.nan
    
    return result


# ══════════════════════════════════════════════════════════════
# BOP (Balance of Power)
# ══════════════════════════════════════════════════════════════

def bop_2d(o, h, l, c, smooth):
    """BOP = SMA((C-O)/(H-L), smooth). Matches profiling_engine.bop()."""
    hl = h - l
    with np.errstate(divide='ignore', invalid='ignore'):
        raw = np.where(hl != 0, (c - o) / hl, np.nan)
    return sma_2d(raw, smooth)


# ══════════════════════════════════════════════════════════════
# AROON
# ══════════════════════════════════════════════════════════════

def aroon_up_2d(h, period):
    """Aroon Up. Matches profiling_engine.aroon_up()."""
    n_t, n_b = h.shape
    result = np.full_like(h, np.nan)
    
    for j in range(period - 1, n_b):
        window = h[:, j - period + 1:j + 1]  # (n_t, period)
        has_nan = np.any(np.isnan(window), axis=1)
        idx_max = np.argmax(window, axis=1)  # index within window
        val = ((period - (period - 1 - idx_max)) / period) * 100
        val = val.astype(np.float64)
        val[has_nan] = np.nan
        result[:, j] = val
    
    return result


def aroon_down_2d(l, period):
    """Aroon Down. Matches profiling_engine.aroon_down()."""
    n_t, n_b = l.shape
    result = np.full_like(l, np.nan)
    
    for j in range(period - 1, n_b):
        window = l[:, j - period + 1:j + 1]
        has_nan = np.any(np.isnan(window), axis=1)
        idx_min = np.argmin(window, axis=1)
        val = ((period - (period - 1 - idx_min)) / period) * 100
        val = val.astype(np.float64)
        val[has_nan] = np.nan
        result[:, j] = val
    
    return result


# ══════════════════════════════════════════════════════════════
# CMF (Chaikin Money Flow)
# ══════════════════════════════════════════════════════════════

def cmf_2d(h, l, c, v, period):
    """CMF. Matches profiling_engine.chaikin_money_flow()."""
    hl = h - l
    with np.errstate(divide='ignore', invalid='ignore'):
        mfm = np.where(hl != 0, ((c - l) - (h - c)) / hl, np.nan)
    mfv = mfm * v
    
    sum_mfv = rolling_sum_2d(mfv, period)
    sum_v = rolling_sum_2d(v, period)
    
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(sum_v != 0, sum_mfv / sum_v, np.nan)
    result[np.isnan(sum_mfv) | np.isnan(sum_v)] = np.nan
    
    return result


# ══════════════════════════════════════════════════════════════
# KAUFMAN EFFICIENCY RATIO
# ══════════════════════════════════════════════════════════════

def kaufman_eff_2d(c, period):
    """Kaufman Efficiency Ratio. Matches profiling_engine.kaufman_efficiency()."""
    direction = np.full_like(c, np.nan)
    direction[:, period:] = np.abs(c[:, period:] - c[:, :-period])
    
    diff_abs = np.full_like(c, np.nan)
    diff_abs[:, 1:] = np.abs(c[:, 1:] - c[:, :-1])
    
    volatility = rolling_sum_2d(diff_abs, period)
    
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(volatility != 0, direction / volatility, np.nan)
    result[np.isnan(direction) | np.isnan(volatility)] = np.nan
    
    return result


# ══════════════════════════════════════════════════════════════
# BOOLEAN AGGREGATES
# ══════════════════════════════════════════════════════════════

def count_true_2d(bool_arr, period):
    """Rolling count of True in last period bars. Uses cumsum trick."""
    int_arr = bool_arr.astype(np.float64)
    return rolling_sum_2d(int_arr, period)


def since_true_2d(bool_arr, period):
    """Bars since last True within period bars.
    Returns -1 if never true in window. Matches profiling_engine.since_true()."""
    n_t, n_b = bool_arr.shape
    result = np.full((n_t, n_b), np.nan)
    
    for j in range(period - 1, n_b):
        window = bool_arr[:, j - period + 1:j + 1]  # (n_t, period)
        # For each ticker, find last True index
        # Flip window so we search from the end
        flipped = window[:, ::-1]  # (n_t, period)
        # argmax on bool finds first True (from end, since flipped)
        has_any = np.any(flipped, axis=1)
        first_true = np.argmax(flipped, axis=1)  # 0 = current bar was True
        val = np.where(has_any, first_true.astype(np.float64), -1.0)
        result[:, j] = val
    
    return result


def true_in_row_2d(bool_arr, max_look):
    """Consecutive True count from current bar back (max max_look).
    Matches profiling_engine.true_in_row()."""
    n_t, n_b = bool_arr.shape
    result = np.zeros((n_t, n_b), dtype=np.float64)
    
    for j in range(n_b):
        if j == 0:
            result[:, 0] = bool_arr[:, 0].astype(np.float64)
        else:
            is_true = bool_arr[:, j]
            result[:, j] = np.where(is_true, result[:, j-1] + 1, 0)
    
    # Cap at max_look
    result = np.minimum(result, max_look)
    
    return result
