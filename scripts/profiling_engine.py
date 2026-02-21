"""
Profiling Engine — Computes thousands of PCF-equivalent measurements for any ticker/date.

This is Step 3 of the Analysis System. Given a set of tickers with dates (examples or signals),
it builds a wide numerical fingerprint of what each setup looks like on its scan bar.

All indicators faithfully replicate TC2000 PCF functions using raw OHLCV data.
"""

import numpy as np
import pandas as pd
from typing import Optional


# =============================================================================
# LAYER 0: Core indicator functions (PCF-equivalent implementations)
# =============================================================================

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average — equivalent to AVGCx in PCF."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average — equivalent to XAVGCx in PCF."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def fwma(series: pd.Series, period: int) -> pd.Series:
    """Front-Weighted Moving Average — equivalent to FAVGCx in PCF.
    Weights: period, period-1, ..., 1 (most recent gets highest weight)."""
    weights = np.arange(1, period + 1, dtype=float)
    def _calc(window):
        return np.dot(window, weights) / weights.sum()
    return series.rolling(window=period, min_periods=period).apply(_calc, raw=True)


def hma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average — equivalent to HAVGCx in PCF.
    HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))"""
    half = max(int(period / 2), 1)
    sqrt_p = max(int(np.sqrt(period)), 1)
    # WMA using linearly weighted
    wma_half = series.rolling(window=half, min_periods=half).apply(
        lambda x: np.dot(x, np.arange(1, half + 1)) / np.arange(1, half + 1).sum(), raw=True)
    wma_full = series.rolling(window=period, min_periods=period).apply(
        lambda x: np.dot(x, np.arange(1, period + 1)) / np.arange(1, period + 1).sum(), raw=True)
    diff = 2 * wma_half - wma_full
    return diff.rolling(window=sqrt_p, min_periods=sqrt_p).apply(
        lambda x: np.dot(x, np.arange(1, sqrt_p + 1)) / np.arange(1, sqrt_p + 1).sum(), raw=True)


def rolling_max(series: pd.Series, period: int) -> pd.Series:
    """Rolling max — equivalent to MAXHx / MAXCx in PCF."""
    return series.rolling(window=period, min_periods=period).max()


def rolling_min(series: pd.Series, period: int) -> pd.Series:
    """Rolling min — equivalent to MINLx / MINCx in PCF."""
    return series.rolling(window=period, min_periods=period).min()


def rolling_sum(series: pd.Series, period: int) -> pd.Series:
    """Rolling sum — equivalent to SUM(x, period) in PCF."""
    return series.rolling(window=period, min_periods=period).sum()


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range — TC2000 uses SMA smoothing (not Wilder's).
    Equivalent to ATRx in PCF."""
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return sma(tr, period)


def rsi(series: pd.Series, period: int) -> pd.Series:
    """RSI — standard (not Wilder's). Equivalent to RSIx.1.0 in PCF."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = sma(gain, period)
    avg_loss = sma(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def wrsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI — equivalent to WRSIx in PCF."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int, slow: int) -> pd.Series:
    """MACD oscillator — equivalent to MACDs.l.0 in PCF."""
    return ema(series, fast) - ema(series, slow)


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int, smooth: int = 3) -> pd.Series:
    """Stochastic %K — equivalent to STOCx.y.0 in PCF."""
    lowest = rolling_min(low, period)
    highest = rolling_max(high, period)
    raw_k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    return sma(raw_k, smooth)


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Commodity Channel Index — equivalent to CCIx in PCF."""
    tp = (high + low + close) / 3
    sma_tp = sma(tp, period)
    mad = tp.rolling(window=period, min_periods=period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    return (tp - sma_tp) / (0.015 * mad).replace(0, np.nan)


def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        di_period: int, smooth: int = 14) -> tuple:
    """ADX with DI+ and DI- — equivalent to ADXd.s.0, DIPLUSx, DIMINUSx in PCF.
    Returns (adx_series, diplus_series, diminus_series)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)
    minus_dm = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr_val = tr.ewm(alpha=1.0/di_period, adjust=False, min_periods=di_period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0/di_period, adjust=False, min_periods=di_period).mean() / atr_val.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1.0/di_period, adjust=False, min_periods=di_period).mean() / atr_val.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1.0/smooth, adjust=False, min_periods=smooth).mean()
    return adx_val, plus_di, minus_di


def bollinger_bands(series: pd.Series, period: int, num_std: float = 2.0) -> tuple:
    """Bollinger Bands — equivalent to BBTOPd.x.0 and BBBOTd.x.0 in PCF.
    Returns (upper, lower)."""
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    return mid + num_std * std, mid - num_std * std


def aroon(high: pd.Series, low: pd.Series, period: int) -> tuple:
    """Aroon Up/Down — equivalent to AROONUPx and AROONDOWNx in PCF.
    Returns (aroon_up, aroon_down)."""
    def _bars_since_highest(window):
        return period - np.argmax(window)
    def _bars_since_lowest(window):
        return period - np.argmin(window)

    aroon_up = high.rolling(window=period + 1, min_periods=period + 1).apply(
        lambda x: 100 * _bars_since_highest(x) / period, raw=True)
    aroon_down = low.rolling(window=period + 1, min_periods=period + 1).apply(
        lambda x: 100 * _bars_since_lowest(x) / period, raw=True)
    return aroon_up, aroon_down


def obv(close: pd.Series, volume: pd.Series, smooth: int = 1) -> pd.Series:
    """On Balance Volume — equivalent to OBVy.0 in PCF."""
    direction = np.sign(close.diff())
    raw_obv = (direction * volume).cumsum()
    if smooth > 1:
        return sma(raw_obv, smooth)
    return raw_obv


def bop(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series,
        smooth: int = 1) -> pd.Series:
    """Balance of Power — equivalent to BOPy.0 in PCF."""
    raw = (close - open_) / (high - low).replace(0, np.nan)
    if smooth > 1:
        return sma(raw, smooth)
    return raw


def stddev(series: pd.Series, period: int) -> pd.Series:
    """Standard deviation — equivalent to STDDEVx in PCF."""
    return series.rolling(window=period, min_periods=period).std()


# Boolean-to-numeric helpers (PCF equivalents)
def count_true(condition: pd.Series, period: int) -> pd.Series:
    """CountTrue(b, x) in PCF — number of True in last x bars."""
    return condition.astype(float).rolling(window=period, min_periods=period).sum()


def since_true(condition: pd.Series, period: int) -> pd.Series:
    """SinceTrue(b, x) in PCF — bars since last True (0=current). -1 if never."""
    result = pd.Series(np.nan, index=condition.index)
    for i in range(len(condition)):
        if i < period - 1:
            continue
        window = condition.iloc[max(0, i - period + 1):i + 1]
        true_positions = np.where(window.values)[0]
        if len(true_positions) == 0:
            result.iloc[i] = -1
        else:
            result.iloc[i] = len(window) - 1 - true_positions[-1]
    return result


def true_in_row(condition: pd.Series, max_period: int) -> pd.Series:
    """TrueInRow(b, x) in PCF — consecutive True count from current bar back."""
    result = pd.Series(0, index=condition.index, dtype=float)
    for i in range(len(condition)):
        count = 0
        for j in range(min(max_period, i + 1)):
            if condition.iloc[i - j]:
                count += 1
            else:
                break
        result.iloc[i] = count
    return result


# =============================================================================
# LAYER 1: Raw indicators with period sweeps
# =============================================================================

# Period sets — balanced coverage without explosion
MA_PERIODS = [5, 8, 10, 13, 15, 20, 21, 30, 40, 50, 65, 100, 150, 200]
ATR_PERIODS = [5, 7, 10, 14, 20, 30, 50]
RSI_PERIODS = [7, 10, 14, 21, 30]
STOCH_PERIODS = [5, 10, 14, 21]
CCI_PERIODS = [10, 14, 20, 30]
ADX_PERIODS = [10, 14, 20]
BB_PERIODS = [10, 20, 30, 50]
AROON_PERIODS = [10, 14, 20, 25, 50]
MAXMIN_PERIODS = [3, 5, 10, 15, 20, 30, 50, 65, 100]
VOLUME_SMOOTH = [1, 5, 10, 14, 20]
BOP_SMOOTH = [1, 5, 10, 14]
OFFSET_BARS = [1, 2, 3, 5, 10]


def compute_layer1(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all Layer 1 raw indicators. Returns DataFrame with indicator columns added."""
    out = df.copy()
    c, o, h, l, v = out['close'], out['open'], out['high'], out['low'], out['volume']

    # -- Moving averages of Close --
    for p in MA_PERIODS:
        out[f'sma_{p}'] = sma(c, p)
        out[f'ema_{p}'] = ema(c, p)
    # FWMA and HMA for select periods (expensive)
    for p in [10, 20, 50]:
        out[f'fwma_{p}'] = fwma(c, p)
        out[f'hma_{p}'] = hma(c, p)

    # -- Moving averages of High/Low (for channel analysis) --
    for p in [10, 20, 50]:
        out[f'sma_h_{p}'] = sma(h, p)
        out[f'sma_l_{p}'] = sma(l, p)
        out[f'ema_h_{p}'] = ema(h, p)
        out[f'ema_l_{p}'] = ema(l, p)

    # -- Rolling Max/Min --
    for p in MAXMIN_PERIODS:
        out[f'maxh_{p}'] = rolling_max(h, p)
        out[f'maxc_{p}'] = rolling_max(c, p)
        out[f'minl_{p}'] = rolling_min(l, p)
        out[f'minc_{p}'] = rolling_min(c, p)

    # -- ATR --
    for p in ATR_PERIODS:
        out[f'atr_{p}'] = atr(out, p)

    # -- RSI variants --
    for p in RSI_PERIODS:
        out[f'rsi_{p}'] = rsi(c, p)
        out[f'wrsi_{p}'] = wrsi(c, p)

    # -- MACD --
    for fast, slow in [(8, 17), (12, 26), (5, 35)]:
        out[f'macd_{fast}_{slow}'] = macd(c, fast, slow)

    # -- Stochastics --
    for p in STOCH_PERIODS:
        out[f'stoch_{p}'] = stochastic(h, l, c, p, smooth=3)

    # -- CCI --
    for p in CCI_PERIODS:
        out[f'cci_{p}'] = cci(h, l, c, p)

    # -- ADX, DI+, DI- --
    for p in ADX_PERIODS:
        adx_val, diplus, diminus = adx(h, l, c, p)
        out[f'adx_{p}'] = adx_val
        out[f'diplus_{p}'] = diplus
        out[f'diminus_{p}'] = diminus

    # -- Bollinger Bands --
    for p in BB_PERIODS:
        upper, lower = bollinger_bands(c, p)
        out[f'bbtop_{p}'] = upper
        out[f'bbbot_{p}'] = lower

    # -- Aroon --
    for p in AROON_PERIODS:
        up, down = aroon(h, l, p)
        out[f'aroonup_{p}'] = up
        out[f'aroondn_{p}'] = down

    # -- Volume indicators --
    for sm in VOLUME_SMOOTH:
        out[f'obv_{sm}'] = obv(c, v, sm)
    for sm in BOP_SMOOTH:
        out[f'bop_{sm}'] = bop(o, h, l, c, sm)

    # -- Rolling sums --
    for p in [5, 10, 20, 50]:
        out[f'sumv_{p}'] = rolling_sum(v, p)

    # -- Standard deviation --
    for p in [10, 20, 50]:
        out[f'stddev_{p}'] = stddev(c, p)

    # -- Price offsets (C1 through C10, etc.) --
    for offset in OFFSET_BARS:
        out[f'c_{offset}'] = c.shift(offset)
        out[f'o_{offset}'] = o.shift(offset)
        out[f'h_{offset}'] = h.shift(offset)
        out[f'l_{offset}'] = l.shift(offset)
        out[f'v_{offset}'] = v.shift(offset)

    return out


# =============================================================================
# LAYER 2: Derived measurements
# =============================================================================

def compute_layer2(df: pd.DataFrame) -> pd.DataFrame:
    """Compute derived measurements that encode history into scan-bar values."""
    out = df.copy()
    c, o, h, l, v = out['close'], out['open'], out['high'], out['low'], out['volume']
    atr14 = out.get('atr_14')
    if atr14 is None:
        atr14 = atr(out[['high', 'low', 'close']].rename(columns={'high':'high','low':'low','close':'close'}), 14)

    # -- Distance from MA (in ATR units) --
    for p in MA_PERIODS:
        for atr_p in [14]:
            atr_col = out.get(f'atr_{atr_p}', atr14)
            if f'sma_{p}' in out.columns:
                out[f'c_dist_sma{p}_atr{atr_p}'] = (c - out[f'sma_{p}']) / atr_col.replace(0, np.nan)
            if f'ema_{p}' in out.columns:
                out[f'c_dist_ema{p}_atr{atr_p}'] = (c - out[f'ema_{p}']) / atr_col.replace(0, np.nan)

    # -- Distance from MA (in %) --
    for p in [8, 10, 20, 21, 50, 200]:
        if f'sma_{p}' in out.columns:
            out[f'c_dist_sma{p}_pct'] = (c - out[f'sma_{p}']) / out[f'sma_{p}'].replace(0, np.nan) * 100
        if f'ema_{p}' in out.columns:
            out[f'c_dist_ema{p}_pct'] = (c - out[f'ema_{p}']) / out[f'ema_{p}'].replace(0, np.nan) * 100

    # -- Extension: (MaxH - SMA) / ATR --
    for maxp in [10, 20, 30, 50, 65]:
        for sma_p in [20, 50, 200]:
            if f'maxh_{maxp}' in out.columns and f'sma_{sma_p}' in out.columns:
                out[f'ext_maxh{maxp}_sma{sma_p}_atr14'] = (
                    (out[f'maxh_{maxp}'] - out[f'sma_{sma_p}']) / atr14.replace(0, np.nan)
                )

    # -- Pullback depth: (MaxH - C) / ATR --
    for maxp in [10, 20, 30, 50, 65]:
        if f'maxh_{maxp}' in out.columns:
            out[f'pullback_maxh{maxp}_atr14'] = (out[f'maxh_{maxp}'] - c) / atr14.replace(0, np.nan)

    # -- Pullback as % of move: (MaxH - C) / (MaxH - MinL) --
    for maxp in [20, 30, 50]:
        for minp in [50, 65, 100]:
            maxh = out.get(f'maxh_{maxp}')
            minl = out.get(f'minl_{minp}')
            if maxh is not None and minl is not None:
                move = (maxh - minl).replace(0, np.nan)
                out[f'pb_pct_maxh{maxp}_minl{minp}'] = (maxh - c) / move

    # -- MA relationships: differences and ratios --
    ma_pairs = [(8, 21), (10, 20), (20, 50), (50, 200), (8, 50), (21, 50)]
    for fast_p, slow_p in ma_pairs:
        for ma_type in ['sma', 'ema']:
            fast_col = f'{ma_type}_{fast_p}'
            slow_col = f'{ma_type}_{slow_p}'
            if fast_col in out.columns and slow_col in out.columns:
                out[f'{ma_type}{fast_p}_minus_{ma_type}{slow_p}_atr14'] = (
                    (out[fast_col] - out[slow_col]) / atr14.replace(0, np.nan)
                )
                out[f'{ma_type}{fast_p}_div_{ma_type}{slow_p}'] = (
                    out[fast_col] / out[slow_col].replace(0, np.nan)
                )

    # -- MA slope: SMA now vs N bars ago (in ATR units) --
    for p in [20, 50, 200]:
        for offset in [5, 10, 20]:
            col = f'sma_{p}'
            if col in out.columns:
                out[f'sma{p}_slope_{offset}d_atr14'] = (
                    (out[col] - out[col].shift(offset)) / atr14.replace(0, np.nan)
                )
    for p in [8, 21]:
        for offset in [5, 10]:
            col = f'ema_{p}'
            if col in out.columns:
                out[f'ema{p}_slope_{offset}d_atr14'] = (
                    (out[col] - out[col].shift(offset)) / atr14.replace(0, np.nan)
                )

    # -- Range ratios --
    for maxp in [10, 20, 50]:
        for minp in [10, 20, 50]:
            maxh = out.get(f'maxh_{maxp}')
            minl = out.get(f'minl_{minp}')
            if maxh is not None and minl is not None:
                out[f'range_maxh{maxp}_minl{minp}_atr14'] = (maxh - minl) / atr14.replace(0, np.nan)

    # -- Candle shape --
    body = (c - o).abs()
    wick_upper = h - pd.concat([c, o], axis=1).max(axis=1)
    wick_lower = pd.concat([c, o], axis=1).min(axis=1) - l
    bar_range = (h - l).replace(0, np.nan)

    out['candle_body_pct'] = body / bar_range
    out['candle_upper_wick_pct'] = wick_upper / bar_range
    out['candle_lower_wick_pct'] = wick_lower / bar_range
    out['candle_close_position'] = (c - l) / bar_range  # 1 = closed at high, 0 = at low
    out['candle_range_atr14'] = bar_range / atr14.replace(0, np.nan)
    out['candle_body_atr14'] = body / atr14.replace(0, np.nan)
    out['candle_is_green'] = (c > o).astype(float)

    # -- Volume ratios --
    for short_p in [1, 3, 5]:
        for long_p in [10, 20, 50]:
            short_avg = sma(v, short_p) if short_p > 1 else v
            long_avg = out.get(f'sma_{long_p}') if f'sma_{long_p}' in out.columns else sma(v, long_p)
            # For volume we need vol sma, not close sma
            vol_long = sma(v, long_p)
            if short_p == 1:
                out[f'vol_ratio_1d_avg{long_p}'] = v / vol_long.replace(0, np.nan)
            else:
                vol_short = sma(v, short_p)
                out[f'vol_ratio_avg{short_p}_avg{long_p}'] = vol_short / vol_long.replace(0, np.nan)

    # -- Price position in range: (C - MinL) / (MaxH - MinL) --
    for p in [10, 20, 50]:
        maxh = out.get(f'maxh_{p}')
        minl = out.get(f'minl_{p}')
        if maxh is not None and minl is not None:
            rng = (maxh - minl).replace(0, np.nan)
            out[f'price_position_{p}d'] = (c - minl) / rng

    # -- Bollinger %b --
    for p in BB_PERIODS:
        top = out.get(f'bbtop_{p}')
        bot = out.get(f'bbbot_{p}')
        if top is not None and bot is not None:
            bw = (top - bot).replace(0, np.nan)
            out[f'bb_pctb_{p}'] = (c - bot) / bw
            out[f'bb_width_{p}'] = bw / out.get(f'sma_{p}', sma(c, p)).replace(0, np.nan)

    # -- CountTrue patterns --
    up_close = c > c.shift(1)
    green_candle = c > o
    above_ema8 = c > out.get('ema_8', pd.Series(np.nan, index=c.index))
    above_ema21 = c > out.get('ema_21', pd.Series(np.nan, index=c.index))
    above_sma50 = c > out.get('sma_50', pd.Series(np.nan, index=c.index))

    for p in [3, 5, 10, 14, 20]:
        out[f'ct_up_close_{p}'] = count_true(up_close, p)
        out[f'ct_green_{p}'] = count_true(green_candle, p)
    for p in [5, 10, 20]:
        out[f'ct_above_ema8_{p}'] = count_true(above_ema8, p)
        out[f'ct_above_ema21_{p}'] = count_true(above_ema21, p)
        out[f'ct_above_sma50_{p}'] = count_true(above_sma50, p)

    # -- TrueInRow patterns --
    down_close = c < c.shift(1)
    out['tir_up_close'] = true_in_row(up_close, 15)
    out['tir_down_close'] = true_in_row(down_close, 15)
    out['tir_green'] = true_in_row(green_candle, 15)
    out['tir_red'] = true_in_row(~green_candle, 15)

    # -- ROC % at various lookbacks --
    for p in [1, 3, 5, 10, 20, 50]:
        out[f'roc_pct_{p}'] = (c / c.shift(p) - 1) * 100

    return out


# =============================================================================
# LAYER 3: Market context (SPY/QQQ relative measurements)
# =============================================================================

def compute_layer3(df: pd.DataFrame, spy_df: pd.DataFrame, qqq_df: pd.DataFrame) -> pd.DataFrame:
    """Add market context columns. Requires SPY and QQQ DataFrames with same date index."""
    out = df.copy()

    # Align SPY/QQQ to stock dates
    for prefix, mkt_df in [('spy', spy_df), ('qqq', qqq_df)]:
        if mkt_df is None or mkt_df.empty:
            continue
        # Merge on date
        mkt = mkt_df[['date', 'close', 'high', 'low', 'volume']].copy()
        mkt.columns = ['date'] + [f'{prefix}_{c}' for c in ['close', 'high', 'low', 'volume']]
        out = out.merge(mkt, on='date', how='left')

        mkt_c = out[f'{prefix}_close']
        mkt_h = out[f'{prefix}_high']
        mkt_l = out[f'{prefix}_low']

        # Market MAs
        for p in [20, 50, 200]:
            out[f'{prefix}_sma_{p}'] = sma(mkt_c, p)
            out[f'{prefix}_c_dist_sma{p}_pct'] = (mkt_c - out[f'{prefix}_sma_{p}']) / out[f'{prefix}_sma_{p}'].replace(0, np.nan) * 100

        # Market ATR
        mkt_tr = pd.concat([
            mkt_h - mkt_l,
            (mkt_h - mkt_c.shift(1)).abs(),
            (mkt_l - mkt_c.shift(1)).abs()
        ], axis=1).max(axis=1)
        out[f'{prefix}_atr_14'] = sma(mkt_tr, 14)

        # Market RSI
        out[f'{prefix}_rsi_14'] = rsi(mkt_c, 14)

        # Market ROC
        for p in [5, 10, 20]:
            out[f'{prefix}_roc_{p}'] = (mkt_c / mkt_c.shift(p) - 1) * 100

        # Stock vs market relative
        if 'rsi_14' in out.columns:
            out[f'rsi14_minus_{prefix}_rsi14'] = out['rsi_14'] - out[f'{prefix}_rsi_14']
        for p in [50, 200]:
            stock_dist = out.get(f'c_dist_sma{p}_pct')
            mkt_dist = out.get(f'{prefix}_c_dist_sma{p}_pct')
            if stock_dist is not None and mkt_dist is not None:
                out[f'rel_dist_sma{p}_vs_{prefix}'] = stock_dist - mkt_dist

        # Market MA above/below flags
        for p in [50, 200]:
            mkt_sma = out.get(f'{prefix}_sma_{p}')
            if mkt_sma is not None:
                out[f'{prefix}_above_sma{p}'] = (mkt_c > mkt_sma).astype(float)

        # Market MA slope
        for p in [50]:
            col = f'{prefix}_sma_{p}'
            if col in out.columns:
                out[f'{prefix}_sma{p}_slope_10d'] = (out[col] - out[col].shift(10))

    return out


# =============================================================================
# LAYER 4: Offset comparisons (rate of change of indicators)
# =============================================================================

def compute_layer4(df: pd.DataFrame) -> pd.DataFrame:
    """Compare indicator values now vs N bars ago."""
    out = df.copy()

    # Key indicators to compare across time
    key_indicators = []
    for p in [14]:
        key_indicators.append(f'rsi_{p}')
        key_indicators.append(f'atr_{p}')
    for p in [20, 50]:
        key_indicators.append(f'sma_{p}')
        key_indicators.append(f'bb_width_{p}')
    for p in [14]:
        key_indicators.append(f'adx_{p}')
    key_indicators.extend(['candle_range_atr14', 'vol_ratio_1d_avg20'])

    for col in key_indicators:
        if col not in out.columns:
            continue
        for offset in [3, 5, 10]:
            out[f'{col}_chg_{offset}d'] = out[col] - out[col].shift(offset)

    # Acceleration (2nd derivative) for MAs
    for p in [20, 50]:
        col = f'sma_{p}'
        if col in out.columns:
            slope = out[col] - out[col].shift(5)
            prev_slope = out[col].shift(5) - out[col].shift(10)
            out[f'sma{p}_accel_5d'] = slope - prev_slope

    return out


# =============================================================================
# MAIN PROFILING FUNCTION
# =============================================================================

def profile_ticker(ohlcv_rows: list[dict],
                   scan_date: str,
                   spy_rows: Optional[list[dict]] = None,
                   qqq_rows: Optional[list[dict]] = None) -> dict:
    """
    Profile a single ticker on a specific scan date.

    Args:
        ohlcv_rows: List of dicts with keys: date, open, high, low, close, volume
        scan_date: The date to extract the profile for (the scan bar, NOT entry date)
        spy_rows: Optional SPY OHLCV for market context
        qqq_rows: Optional QQQ OHLCV for market context

    Returns:
        Dict of measurement_name -> value for the scan bar
    """
    df = pd.DataFrame(ohlcv_rows)
    df.columns = [c.lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    df = df.sort_values('date').reset_index(drop=True)

    # Ensure numeric
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Compute all layers
    df = compute_layer1(df)
    df = compute_layer2(df)

    # Layer 3: Market context
    spy_df, qqq_df = None, None
    if spy_rows:
        spy_df = pd.DataFrame(spy_rows)
        spy_df.columns = [c.lower() for c in spy_df.columns]
        spy_df['date'] = pd.to_datetime(spy_df['date']).dt.strftime('%Y-%m-%d')
        spy_df = spy_df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            spy_df[col] = pd.to_numeric(spy_df[col], errors='coerce')
    if qqq_rows:
        qqq_df = pd.DataFrame(qqq_rows)
        qqq_df.columns = [c.lower() for c in qqq_df.columns]
        qqq_df['date'] = pd.to_datetime(qqq_df['date']).dt.strftime('%Y-%m-%d')
        qqq_df = qqq_df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            qqq_df[col] = pd.to_numeric(qqq_df[col], errors='coerce')

    df = compute_layer3(df, spy_df, qqq_df)
    df = compute_layer4(df)

    # Extract the scan bar row
    scan_row = df[df['date'] == scan_date]
    if scan_row.empty:
        # Try closest prior date
        prior = df[df['date'] <= scan_date]
        if prior.empty:
            raise ValueError(f"No data at or before scan date {scan_date}")
        scan_row = prior.iloc[[-1]]

    # Convert to dict, dropping NaN and base OHLCV columns
    row = scan_row.iloc[0]
    skip_cols = {'date', 'open', 'high', 'low', 'close', 'volume'}
    result = {}
    for col in row.index:
        if col in skip_cols:
            continue
        val = row[col]
        if pd.notna(val) and np.isfinite(val):
            result[col] = round(float(val), 6)

    return result


def profile_examples(examples: list[dict],
                     fetch_ohlcv_fn,
                     fetch_market_fn=None) -> pd.DataFrame:
    """
    Profile a batch of examples.

    Args:
        examples: List of dicts with keys: ticker, entry_date (scan_date = entry_date - 1 trading day)
        fetch_ohlcv_fn: Callable(ticker) -> list of OHLCV dicts
        fetch_market_fn: Optional Callable(ticker) -> list of OHLCV dicts (for SPY/QQQ)

    Returns:
        DataFrame with one row per example, columns are measurements
    """
    spy_rows, qqq_rows = None, None
    if fetch_market_fn:
        spy_rows = fetch_market_fn('SPY')
        qqq_rows = fetch_market_fn('QQQ')

    rows = []
    for ex in examples:
        ticker = ex['ticker']
        entry_date = ex['entry_date']
        try:
            ohlcv = fetch_ohlcv_fn(ticker)
            if not ohlcv:
                print(f"  SKIP {ticker}: no OHLCV data")
                continue

            # Find scan date (trading day before entry)
            dates = sorted([r['date'] if isinstance(r['date'], str) else r['date'].strftime('%Y-%m-%d') for r in ohlcv])
            entry_str = entry_date if isinstance(entry_date, str) else entry_date.strftime('%Y-%m-%d')
            prior_dates = [d for d in dates if d < entry_str]
            if not prior_dates:
                print(f"  SKIP {ticker}: no trading day before entry {entry_str}")
                continue
            scan_date = prior_dates[-1]

            profile = profile_ticker(ohlcv, scan_date, spy_rows, qqq_rows)
            profile['_ticker'] = ticker
            profile['_entry_date'] = entry_str
            profile['_scan_date'] = scan_date
            rows.append(profile)
            print(f"  OK {ticker}: {len(profile)} measurements on {scan_date}")

        except Exception as e:
            print(f"  ERROR {ticker}: {e}")
            continue

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def profile_universe_sample(tickers: list[str],
                            scan_date: str,
                            fetch_ohlcv_fn,
                            fetch_market_fn=None,
                            max_tickers: int = 500) -> pd.DataFrame:
    """
    Profile a sample of the tradable universe on a given date.
    Used as the comparison baseline for discovery.
    """
    spy_rows, qqq_rows = None, None
    if fetch_market_fn:
        spy_rows = fetch_market_fn('SPY')
        qqq_rows = fetch_market_fn('QQQ')

    sample = tickers[:max_tickers]
    rows = []
    for i, ticker in enumerate(sample):
        try:
            ohlcv = fetch_ohlcv_fn(ticker)
            if not ohlcv:
                continue
            profile = profile_ticker(ohlcv, scan_date, spy_rows, qqq_rows)
            profile['_ticker'] = ticker
            profile['_scan_date'] = scan_date
            rows.append(profile)
        except Exception:
            continue

        if (i + 1) % 100 == 0:
            print(f"  Universe profiling: {i+1}/{len(sample)} done, {len(rows)} succeeded")

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


# =============================================================================
# STANDALONE TEST
# =============================================================================

if __name__ == '__main__':
    import json

    # Quick test with synthetic data
    np.random.seed(42)
    n = 300
    dates = pd.bdate_range('2024-01-01', periods=n)
    price = 50 + np.cumsum(np.random.randn(n) * 0.5)
    test_rows = [
        {
            'date': d.strftime('%Y-%m-%d'),
            'open': float(price[i] - np.random.rand() * 0.3),
            'high': float(price[i] + np.random.rand()),
            'low': float(price[i] - np.random.rand()),
            'close': float(price[i]),
            'volume': float(np.random.randint(100000, 1000000))
        }
        for i, d in enumerate(dates)
    ]

    scan_date = test_rows[-1]['date']
    result = profile_ticker(test_rows, scan_date)
    print(f"\nProfile computed: {len(result)} measurements")
    print(f"Sample keys: {list(result.keys())[:20]}")
    print(f"Sample values: {dict(list(result.items())[:10])}")
