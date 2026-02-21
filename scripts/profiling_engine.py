"""
Profiling Engine — Step 3 of ANALYSIS_SYSTEM.md

Computes thousands of measurements for every example on its scan bar.
Builds a complete numerical profile ("fingerprint") of what a setup looks
like at the moment it's ripe.

All indicators replicate TC2000 PCF semantics:
  - ATR = SMA of True Range (not Wilder's)
  - EMA = standard exponential (XAVG in PCF)
  - RSI = standard (not Wilder's) unless WRSI
  - Bollinger Bands = SMA +/- N*stddev

Usage:
  from scripts.profiling_engine import ProfilingEngine

  engine = ProfilingEngine(api_base="https://web-production-e3025.up.railway.app")
  # Profile examples
  result = engine.profile_examples("dtss")
  # Profile universe sample
  result = engine.profile_universe_sample(date="2026-02-19", n=500)
"""

import requests
import numpy as np
import pandas as pd
from typing import Optional


# ============================================================
# PERIOD SWEEPS — meaningful steps, not every integer
# ============================================================

# Moving averages: key periods traders actually use + sweep coverage
MA_PERIODS = [5, 8, 10, 13, 15, 20, 21, 25, 30, 35, 40, 50, 65, 100, 150, 200]
# Shorter set for EMA (traders use fewer long EMAs)
EMA_PERIODS = [5, 8, 10, 13, 15, 20, 21, 25, 30, 35, 40, 50, 65, 100, 150, 200]
# Rolling max/min: focused on lookback windows that matter
MAXMIN_PERIODS = [3, 5, 10, 15, 20, 25, 30, 40, 50, 60, 65, 90, 120, 150, 200]
# ATR periods
ATR_PERIODS = [5, 7, 10, 14, 20, 30, 50]
# RSI periods
RSI_PERIODS = [5, 7, 9, 14, 21, 30]
# Stochastic periods
STOC_PERIODS = [5, 9, 14, 21]
# CCI periods
CCI_PERIODS = [10, 14, 20, 30, 50]
# ADX periods
ADX_PERIODS = [7, 14, 21]
# Bollinger periods
BB_PERIODS = [10, 20, 30, 50]
# Aroon periods
AROON_PERIODS = [14, 25, 50]
# Volume average periods
VOL_PERIODS = [5, 10, 15, 20, 30, 50, 65]
# BOP smoothing
BOP_PERIODS = [5, 10, 14, 20]
# MACD combos (fast, slow)
MACD_COMBOS = [(8, 17), (12, 26), (5, 35)]
# Offset bars for price primitives
OFFSETS = [1, 2, 3, 4, 5, 10, 15, 20]
# Periods for CountTrue / TrueInRow / SinceTrue
COUNT_PERIODS = [3, 5, 7, 10, 15, 20]

# Maximum lookback needed (for data fetching)
MAX_LOOKBACK = 250


# ============================================================
# INDICATOR COMPUTATION — vectorized pandas/numpy
# ============================================================

def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average (AVGCx in PCF)."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (XAVGCx in PCF)."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def fwma(series: pd.Series, period: int) -> pd.Series:
    """Front-Weighted Moving Average (FAVGCx in PCF).
    Weights: period, period-1, ..., 1 (most recent gets highest weight)."""
    weights = np.arange(1, period + 1, dtype=float)
    weights = weights / weights.sum()

    def _calc(window):
        return np.dot(window, weights)

    return series.rolling(window=period, min_periods=period).apply(_calc, raw=True)


def hma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average (HAVGCx in PCF).
    HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))"""
    half = max(int(period / 2), 1)
    sqrt_p = max(int(np.sqrt(period)), 1)
    wma_half = _wma(series, half)
    wma_full = _wma(series, period)
    diff = 2 * wma_half - wma_full
    return _wma(diff, sqrt_p)


def _wma(series: pd.Series, period: int) -> pd.Series:
    """Weighted Moving Average (linear weights)."""
    weights = np.arange(1, period + 1, dtype=float)
    weights = weights / weights.sum()

    def _calc(window):
        return np.dot(window, weights)

    return series.rolling(window=period, min_periods=period).apply(_calc, raw=True)


def rolling_max(series: pd.Series, period: int) -> pd.Series:
    """Rolling maximum (MAXwx in PCF)."""
    return series.rolling(window=period, min_periods=period).max()


def rolling_min(series: pd.Series, period: int) -> pd.Series:
    """Rolling minimum (MINwx in PCF)."""
    return series.rolling(window=period, min_periods=period).min()


def rolling_sum(series: pd.Series, period: int) -> pd.Series:
    """Rolling sum (SUM(w, x) in PCF)."""
    return series.rolling(window=period, min_periods=period).sum()


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range = max(H-L, |H-C1|, |L-C1|)."""
    c1 = df['close'].shift(1)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - c1).abs()
    tr3 = (df['low'] - c1).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range — SMA of TR (TC2000 style, NOT Wilder's)."""
    tr = true_range(df)
    return sma(tr, period)


def rsi(series: pd.Series, period: int) -> pd.Series:
    """RSI — standard (not Wilder's). Uses SMA of gains/losses."""
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    avg_gain = sma(gains, period)
    avg_loss = sma(losses, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def wrsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI — uses exponential smoothing for gains/losses."""
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    # Wilder's smoothing = EMA with alpha = 1/period
    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int, slow: int) -> pd.Series:
    """MACD oscillator = EMA(fast) - EMA(slow)."""
    return ema(series, fast) - ema(series, slow)


def stochastic_k(df: pd.DataFrame, period: int, smooth: int = 3) -> pd.Series:
    """Stochastic %K = SMA(smooth) of raw %K."""
    highest = rolling_max(df['high'], period)
    lowest = rolling_min(df['low'], period)
    raw_k = (df['close'] - lowest) / (highest - lowest).replace(0, np.nan) * 100
    return sma(raw_k, smooth)


def cci(df: pd.DataFrame, period: int) -> pd.Series:
    """Commodity Channel Index."""
    tp = (df['high'] + df['low'] + df['close']) / 3
    tp_sma = sma(tp, period)
    mean_dev = tp.rolling(window=period, min_periods=period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )
    return (tp - tp_sma) / (0.015 * mean_dev.replace(0, np.nan))


def adx(df: pd.DataFrame, di_period: int, smooth: int = None) -> pd.Series:
    """ADX — Average Directional Index."""
    if smooth is None:
        smooth = di_period
    dip, dim = _di_plus_minus(df, di_period)
    dx = (dip - dim).abs() / (dip + dim).replace(0, np.nan) * 100
    return ema(dx, smooth)


def di_plus(df: pd.DataFrame, period: int) -> pd.Series:
    """DI+ (Directional Indicator Plus)."""
    dip, _ = _di_plus_minus(df, period)
    return dip


def di_minus(df: pd.DataFrame, period: int) -> pd.Series:
    """DI- (Directional Indicator Minus)."""
    _, dim = _di_plus_minus(df, period)
    return dim


def _di_plus_minus(df: pd.DataFrame, period: int):
    """Compute DI+ and DI- together."""
    up = df['high'] - df['high'].shift(1)
    down = df['low'].shift(1) - df['low']
    dm_plus = np.where((up > down) & (up > 0), up, 0.0)
    dm_minus = np.where((down > up) & (down > 0), down, 0.0)
    dm_plus = pd.Series(dm_plus, index=df.index)
    dm_minus = pd.Series(dm_minus, index=df.index)
    atr_val = atr(df, period).replace(0, np.nan)
    dip = 100 * ema(dm_plus, period) / atr_val
    dim = 100 * ema(dm_minus, period) / atr_val
    return dip, dim


def bollinger_top(series: pd.Series, period: int, num_std: float = 2.0) -> pd.Series:
    """Bollinger Band Top."""
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    return mid + num_std * std


def bollinger_bot(series: pd.Series, period: int, num_std: float = 2.0) -> pd.Series:
    """Bollinger Band Bottom."""
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    return mid - num_std * std


def stddev(series: pd.Series, period: int) -> pd.Series:
    """Rolling standard deviation."""
    return series.rolling(window=period, min_periods=period).std()


def aroon_up(df: pd.DataFrame, period: int) -> pd.Series:
    """Aroon Up = ((period - bars since highest high) / period) * 100."""
    def _calc(window):
        return ((period - (period - 1 - np.argmax(window))) / period) * 100
    return df['high'].rolling(window=period, min_periods=period).apply(_calc, raw=True)


def aroon_down(df: pd.DataFrame, period: int) -> pd.Series:
    """Aroon Down = ((period - bars since lowest low) / period) * 100."""
    def _calc(window):
        return ((period - (period - 1 - np.argmin(window))) / period) * 100
    return df['low'].rolling(window=period, min_periods=period).apply(_calc, raw=True)


def obv(df: pd.DataFrame) -> pd.Series:
    """On Balance Volume (cumulative)."""
    sign = np.sign(df['close'].diff())
    return (sign * df['volume']).cumsum()


def bop(df: pd.DataFrame, smooth: int) -> pd.Series:
    """Balance of Power = SMA((C-O)/(H-L), smooth)."""
    hl = (df['high'] - df['low']).replace(0, np.nan)
    raw = (df['close'] - df['open']) / hl
    return sma(raw, smooth)


def count_true(cond: pd.Series, period: int) -> pd.Series:
    """CountTrue(b, x) — number of True in last x bars."""
    return cond.astype(float).rolling(window=period, min_periods=period).sum()


def since_true(cond: pd.Series, period: int) -> pd.Series:
    """SinceTrue(b, x) — bars since b was last True within x bars.
    Returns -1 if never true in the window."""
    def _calc(window):
        arr = np.array(window, dtype=bool)
        true_idx = np.where(arr)[0]
        if len(true_idx) == 0:
            return -1
        return len(arr) - 1 - true_idx[-1]
    return cond.rolling(window=period, min_periods=period).apply(_calc, raw=True)


def true_in_row(cond: pd.Series, max_look: int) -> pd.Series:
    """TrueInRow(b, x) — consecutive True count from current bar back (max x)."""
    def _calc(window):
        arr = np.array(window, dtype=bool)
        count = 0
        for i in range(len(arr) - 1, -1, -1):
            if arr[i]:
                count += 1
            else:
                break
        return count
    return cond.rolling(window=max_look, min_periods=1).apply(_calc, raw=True)


def vwap_rolling(df: pd.DataFrame, period: int) -> pd.Series:
    """Moving VWAP = SUM(C*V, period) / SUM(V, period)."""
    return rolling_sum(df['close'] * df['volume'], period) / rolling_sum(df['volume'], period).replace(0, np.nan)


def chaikin_money_flow(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """CMF = SUM(((C-L)-(H-C))/(H-L)*V, period) / SUM(V, period)."""
    hl = (df['high'] - df['low']).replace(0, np.nan)
    mfm = ((df['close'] - df['low']) - (df['high'] - df['close'])) / hl
    mfv = mfm * df['volume']
    return rolling_sum(mfv, period) / rolling_sum(df['volume'], period).replace(0, np.nan)


def kaufman_efficiency(series: pd.Series, period: int) -> pd.Series:
    """Kaufman Efficiency Ratio = |C - Cx| / SUM(|C - C1|, x)."""
    direction = (series - series.shift(period)).abs()
    volatility = series.diff().abs().rolling(window=period, min_periods=period).sum()
    return direction / volatility.replace(0, np.nan)


# ============================================================
# PROFILING ENGINE
# ============================================================

class ProfilingEngine:
    """Computes Layer 1-4 measurements for any set of ticker/date pairs."""

    def __init__(self, api_base: str = "https://web-production-e3025.up.railway.app"):
        self.api_base = api_base.rstrip("/")

    # ----------------------------------------------------------
    # Data fetching
    # ----------------------------------------------------------

    def _query(self, sql: str) -> list[dict]:
        """Run read-only SQL against the Railway DB."""
        resp = requests.post(f"{self.api_base}/api/query", json={"sql": sql}, timeout=30)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"DB query error: {data['error']}")
        return data.get("results", [])

    def _fetch_ohlcv(self, ticker: str, end_date: str, lookback: int = MAX_LOOKBACK) -> pd.DataFrame:
        """Fetch OHLCV data for a ticker ending on end_date with enough lookback.
        Tries bulk endpoint first, falls back to chunked queries."""
        # Try bulk endpoint first
        try:
            resp = requests.get(
                f"{self.api_base}/api/ohlcv/bulk/{ticker}",
                params={"end_date": end_date, "lookback": lookback},
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                rows = data.get("results", [])
                if rows:
                    df = pd.DataFrame(rows)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date').reset_index(drop=True)
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                    return df
        except Exception:
            pass

        # Fallback: use /api/query with chunked approach
        # The query endpoint returns max 100 rows, so we need multiple queries
        all_rows = []
        current_end = end_date
        remaining = lookback
        max_attempts = 5  # Prevent infinite loops

        for _ in range(max_attempts):
            if remaining <= 0:
                break
            batch_size = min(remaining, 100)
            rows = self._query(
                f"SELECT date, open, high, low, close, volume "
                f"FROM universe_ohlcv "
                f"WHERE ticker='{ticker}' AND date<='{current_end}' "
                f"ORDER BY date DESC LIMIT {batch_size}"
            )
            if not rows:
                break
            all_rows.extend(rows)
            remaining -= len(rows)
            if len(rows) < batch_size:
                break  # No more data
            # Next chunk starts before the oldest date in this batch
            current_end = rows[-1]['date']
            # Shift back 1 day to avoid overlap
            from datetime import datetime, timedelta
            dt = datetime.strptime(current_end, '%Y-%m-%d') - timedelta(days=1)
            current_end = dt.strftime('%Y-%m-%d')

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        # Deduplicate (overlapping dates from chunks)
        df = df.drop_duplicates(subset=['date'])
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df

    def _get_examples(self, setup_type: str) -> list[dict]:
        """Get examples for a setup type."""
        rows = self._query(
            f"SELECT ticker, entry_date FROM examples "
            f"WHERE setup_type='{setup_type}' ORDER BY ticker"
        )
        return rows

    def _get_tradable_sample(self, date: str, n: int = 500) -> list[str]:
        """Get a random sample of tradable tickers."""
        rows = self._query(
            f"SELECT ticker FROM tradable_universe ORDER BY RANDOM() LIMIT {n}"
        )
        return [r['ticker'] for r in rows]

    # ----------------------------------------------------------
    # Layer 1: Raw indicators at swept periods
    # ----------------------------------------------------------

    def _compute_layer1(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        """Compute all Layer 1 indicators. Returns dict of name -> series."""
        out = {}
        c, o, h, l, v = df['close'], df['open'], df['high'], df['low'], df['volume']

        # Price offsets
        for offset in OFFSETS:
            out[f'C{offset}'] = c.shift(offset)
            out[f'O{offset}'] = o.shift(offset)
            out[f'H{offset}'] = h.shift(offset)
            out[f'L{offset}'] = l.shift(offset)
            out[f'V{offset}'] = v.shift(offset)

        # SMA of close
        for p in MA_PERIODS:
            out[f'AVGC{p}'] = sma(c, p)
        # SMA of high, low
        for p in [10, 20, 50]:
            out[f'AVGH{p}'] = sma(h, p)
            out[f'AVGL{p}'] = sma(l, p)
        # SMA of volume
        for p in VOL_PERIODS:
            out[f'AVGV{p}'] = sma(v, p)

        # EMA of close
        for p in EMA_PERIODS:
            out[f'XAVGC{p}'] = ema(c, p)
        # EMA of volume (for PVO etc)
        for p in [12, 26]:
            out[f'XAVGV{p}'] = ema(v, p)

        # FWMA of close (select periods)
        for p in [10, 20, 50]:
            out[f'FAVGC{p}'] = fwma(c, p)

        # HMA of close (select periods)
        for p in [10, 20, 50]:
            out[f'HAVGC{p}'] = hma(c, p)

        # Rolling MAX
        for p in MAXMIN_PERIODS:
            out[f'MAXH{p}'] = rolling_max(h, p)
            out[f'MAXC{p}'] = rolling_max(c, p)
        # Rolling MIN
        for p in MAXMIN_PERIODS:
            out[f'MINL{p}'] = rolling_min(l, p)
            out[f'MINC{p}'] = rolling_min(c, p)

        # ATR
        for p in ATR_PERIODS:
            out[f'ATR{p}'] = atr(df, p)

        # RSI
        for p in RSI_PERIODS:
            out[f'RSI{p}'] = rsi(c, p)
        # Wilder's RSI
        for p in [14, 21]:
            out[f'WRSI{p}'] = wrsi(c, p)

        # MACD
        for fast_p, slow_p in MACD_COMBOS:
            out[f'MACD{fast_p}_{slow_p}'] = macd(c, fast_p, slow_p)

        # Stochastics
        for p in STOC_PERIODS:
            out[f'STOC{p}'] = stochastic_k(df, p)

        # CCI
        for p in CCI_PERIODS:
            out[f'CCI{p}'] = cci(df, p)

        # ADX and DI+/DI-
        for p in ADX_PERIODS:
            out[f'ADX{p}'] = adx(df, p)
            out[f'DIPLUS{p}'] = di_plus(df, p)
            out[f'DIMINUS{p}'] = di_minus(df, p)

        # Bollinger Bands
        for p in BB_PERIODS:
            out[f'BBTOP2_{p}'] = bollinger_top(c, p, 2.0)
            out[f'BBBOT2_{p}'] = bollinger_bot(c, p, 2.0)

        # StdDev
        for p in [10, 20, 30, 50]:
            out[f'STDDEV{p}'] = stddev(c, p)

        # Aroon
        for p in AROON_PERIODS:
            out[f'AROONUP{p}'] = aroon_up(df, p)
            out[f'AROONDOWN{p}'] = aroon_down(df, p)

        # OBV (just the raw cumulative — we'll use derived ratios in Layer 2)
        out['OBV'] = obv(df)

        # BOP
        for p in BOP_PERIODS:
            out[f'BOP{p}'] = bop(df, p)

        # Volume sums
        for p in VOL_PERIODS:
            out[f'SUMV{p}'] = rolling_sum(v, p)

        # SMA offsets (for slope calculation) — key MAs 1-5 bars ago
        for p in [8, 21, 50, 200]:
            key = f'AVGC{p}'
            if key in out:
                for offset in [1, 2, 3, 5]:
                    out[f'AVGC{p}_{offset}'] = out[key].shift(offset)
        for p in [8, 21, 50]:
            key = f'XAVGC{p}'
            if key in out:
                for offset in [1, 2, 3, 5]:
                    out[f'XAVGC{p}_{offset}'] = out[key].shift(offset)

        return out

    # ----------------------------------------------------------
    # Layer 2: Derived measurements
    # ----------------------------------------------------------

    def _compute_layer2(self, df: pd.DataFrame, l1: dict[str, pd.Series]) -> dict[str, pd.Series]:
        """Compute all Layer 2 derived measurements from Layer 1."""
        out = {}
        c, o, h, l_col, v = df['close'], df['open'], df['high'], df['low'], df['volume']
        atr14 = l1.get('ATR14', pd.Series(dtype=float))

        # Guard against zero ATR
        atr14_safe = atr14.replace(0, np.nan)

        # --- Distance from MA (in ATR units) ---
        for ma_p in [8, 10, 20, 21, 50, 100, 200]:
            sma_key = f'AVGC{ma_p}'
            ema_key = f'XAVGC{ma_p}'
            if sma_key in l1:
                out[f'dist_sma{ma_p}_atr'] = (c - l1[sma_key]) / atr14_safe
            if ema_key in l1:
                out[f'dist_ema{ma_p}_atr'] = (c - l1[ema_key]) / atr14_safe

        # --- Distance from MA (as % of price) ---
        for ma_p in [20, 50, 200]:
            sma_key = f'AVGC{ma_p}'
            if sma_key in l1:
                out[f'dist_sma{ma_p}_pct'] = (c - l1[sma_key]) / c * 100

        # --- Extension: (MAXHx - AVGCy) / ATR ---
        for maxh_p in [20, 30, 50, 65, 120]:
            maxh_key = f'MAXH{maxh_p}'
            if maxh_key not in l1:
                continue
            for ma_p in [20, 50]:
                sma_key = f'AVGC{ma_p}'
                if sma_key in l1:
                    out[f'ext_maxh{maxh_p}_sma{ma_p}_atr'] = (l1[maxh_key] - l1[sma_key]) / atr14_safe

        # --- Pullback depth: (MAXHx - C) / ATR ---
        for maxh_p in [10, 15, 20, 30, 50, 65]:
            maxh_key = f'MAXH{maxh_p}'
            if maxh_key in l1:
                out[f'pullback_h{maxh_p}_atr'] = (l1[maxh_key] - c) / atr14_safe

        # --- Pullback as % of move: (MAXHx - C) / (MAXHx - MINLy) ---
        for maxh_p in [20, 30, 50, 65]:
            for minl_p in [50, 65, 120]:
                maxh_key = f'MAXH{maxh_p}'
                minl_key = f'MINL{minl_p}'
                if maxh_key in l1 and minl_key in l1:
                    rng = (l1[maxh_key] - l1[minl_key]).replace(0, np.nan)
                    out[f'pullback_pctmove_h{maxh_p}_l{minl_p}'] = (l1[maxh_key] - c) / rng

        # --- MA relationships (spreads) ---
        for fast, slow in [(8, 21), (8, 50), (21, 50), (50, 200), (10, 30), (20, 50)]:
            for kind in ['sma', 'ema']:
                prefix = 'AVGC' if kind == 'sma' else 'XAVGC'
                fk = f'{prefix}{fast}'
                sk = f'{prefix}{slow}'
                if fk in l1 and sk in l1:
                    out[f'{kind}{fast}_{kind}{slow}_spread_atr'] = (l1[fk] - l1[sk]) / atr14_safe
                    denom = l1[sk].replace(0, np.nan)
                    out[f'{kind}{fast}_{kind}{slow}_ratio'] = l1[fk] / denom

        # --- MA slope: MA now vs MA N bars ago (in ATR) ---
        for p in [8, 21, 50, 200]:
            for offset in [1, 3, 5]:
                off_key = f'AVGC{p}_{offset}'
                base_key = f'AVGC{p}'
                if base_key in l1 and off_key in l1:
                    out[f'slope_sma{p}_{offset}bar_atr'] = (l1[base_key] - l1[off_key]) / atr14_safe
        for p in [8, 21, 50]:
            for offset in [1, 3, 5]:
                off_key = f'XAVGC{p}_{offset}'
                base_key = f'XAVGC{p}'
                if base_key in l1 and off_key in l1:
                    out[f'slope_ema{p}_{offset}bar_atr'] = (l1[base_key] - l1[off_key]) / atr14_safe

        # --- Range ratios ---
        for maxh_p in [20, 30, 50, 65]:
            for minl_p in [20, 30, 50, 65]:
                maxh_key = f'MAXH{maxh_p}'
                minl_key = f'MINL{minl_p}'
                if maxh_key in l1 and minl_key in l1:
                    denom = l1[minl_key].replace(0, np.nan)
                    out[f'range_ratio_h{maxh_p}_l{minl_p}'] = l1[maxh_key] / denom
                    out[f'range_atr_h{maxh_p}_l{minl_p}'] = (l1[maxh_key] - l1[minl_key]) / atr14_safe

        # --- Candle shape ---
        hl = (h - l_col).replace(0, np.nan)
        out['upper_wick_pct'] = (h - pd.concat([c, o], axis=1).max(axis=1)) / hl
        out['lower_wick_pct'] = (pd.concat([c, o], axis=1).min(axis=1) - l_col) / hl
        out['body_pct'] = (c - o).abs() / hl
        out['bar_range_atr'] = (h - l_col) / atr14_safe
        out['is_bullish'] = (c > o).astype(float)
        out['close_position'] = (c - l_col) / hl  # 1=closed at high, 0=closed at low

        # --- Volume ratios ---
        for fast_p in [5, 10]:
            for slow_p in [20, 50, 65]:
                fk = f'AVGV{fast_p}'
                sk = f'AVGV{slow_p}'
                if fk in l1 and sk in l1:
                    out[f'volratio_v{fast_p}_v{slow_p}'] = l1[fk] / l1[sk].replace(0, np.nan)
        # Current volume vs average
        for p in [10, 20, 50]:
            vk = f'AVGV{p}'
            if vk in l1:
                out[f'vol_vs_avg{p}'] = v / l1[vk].replace(0, np.nan)

        # --- Price position in range ---
        for p in [20, 30, 50, 65, 120]:
            maxh_key = f'MAXH{p}'
            minl_key = f'MINL{p}'
            if maxh_key in l1 and minl_key in l1:
                rng = (l1[maxh_key] - l1[minl_key]).replace(0, np.nan)
                out[f'price_pos_{p}'] = (c - l1[minl_key]) / rng

        # --- Bollinger %b ---
        for p in BB_PERIODS:
            top_key = f'BBTOP2_{p}'
            bot_key = f'BBBOT2_{p}'
            if top_key in l1 and bot_key in l1:
                bw = (l1[top_key] - l1[bot_key]).replace(0, np.nan)
                out[f'bb_pctb_{p}'] = (c - l1[bot_key]) / bw
                out[f'bb_width_{p}'] = bw / sma(c, p).replace(0, np.nan)

        # --- CountTrue patterns ---
        for p in COUNT_PERIODS:
            out[f'count_up_days_{p}'] = count_true(c > c.shift(1), p)
            out[f'count_above_ema21_{p}'] = count_true(c > l1.get('XAVGC21', c), p)
            out[f'count_above_sma50_{p}'] = count_true(c > l1.get('AVGC50', c), p)

        # --- TrueInRow ---
        out['consec_down_days'] = true_in_row(c < c.shift(1), 20)
        out['consec_up_days'] = true_in_row(c > c.shift(1), 20)
        out['consec_below_ema8'] = true_in_row(c < l1.get('XAVGC8', c), 20)
        out['consec_above_ema8'] = true_in_row(c > l1.get('XAVGC8', c), 20)

        # --- SinceTrue ---
        for p in [20, 50]:
            maxh_key = f'MAXH{p}'
            if maxh_key in l1:
                out[f'bars_since_high_{p}'] = since_true(h >= l1[maxh_key].shift(1), p)
            minl_key = f'MINL{p}'
            if minl_key in l1:
                out[f'bars_since_low_{p}'] = since_true(l_col <= l1[minl_key].shift(1), p)

        # --- ROC % (rate of change) ---
        for p in [5, 10, 20, 50]:
            out[f'roc_pct_{p}'] = (c / c.shift(p) - 1) * 100

        # --- Chaikin Money Flow ---
        out['cmf_20'] = chaikin_money_flow(df, 20)

        # --- Kaufman Efficiency ---
        for p in [10, 20]:
            out[f'kaufman_eff_{p}'] = kaufman_efficiency(c, p)

        # --- Moving VWAP distance ---
        for p in [20, 50]:
            vw = vwap_rolling(df, p)
            out[f'dist_vwap{p}_atr'] = (c - vw) / atr14_safe

        # --- Elder Force / Bull Bear Power ---
        out['elder_force'] = (c - c.shift(1)) * v
        for p in [13]:
            ema_key = f'XAVGC{p}'
            if ema_key in l1:
                out[f'bull_power_{p}'] = h - l1[ema_key]
                out[f'bear_power_{p}'] = l_col - l1[ema_key]

        # --- PPO / PVO ---
        ema12 = l1.get('XAVGC12')
        ema26 = l1.get('XAVGC26')
        if ema12 is not None and ema26 is not None:
            out['ppo'] = (ema12 - ema26) / ema26.replace(0, np.nan) * 100
        ev12 = l1.get('XAVGV12')
        ev26 = l1.get('XAVGV26')
        if ev12 is not None and ev26 is not None:
            out['pvo'] = (ev12 - ev26) / ev26.replace(0, np.nan) * 100

        # --- Williams %R ---
        for p in [14]:
            maxh = l1.get(f'MAXH{p}', rolling_max(h, p))
            minl = l1.get(f'MINL{p}', rolling_min(l_col, p))
            denom = (maxh - minl).replace(0, np.nan)
            out[f'williams_r_{p}'] = (maxh - c) / denom * -100

        # --- Historical Volatility ---
        for p in [20, 50]:
            sd = l1.get(f'STDDEV{p}')
            avg = l1.get(f'AVGC{p}')
            if sd is not None and avg is not None:
                out[f'histvol_{p}'] = sd / avg.replace(0, np.nan) * np.sqrt(252) * 100

        # --- MACD Histogram ---
        macd_key = 'MACD12_26'
        if macd_key in l1:
            signal = ema(l1[macd_key], 9)
            out['macd_hist_12_26'] = l1[macd_key] - signal

        return out

    # ----------------------------------------------------------
    # Layer 3: Market context (SPY/QQQ)
    # ----------------------------------------------------------

    def _compute_layer3(self, stock_row: dict, market_rows: dict[str, dict]) -> dict[str, float]:
        """Compute stock-vs-market relative measurements.
        stock_row and market_rows are single-row dicts of measurements."""
        out = {}
        for mkt_ticker in ['SPY', 'QQQ']:
            mkt = market_rows.get(mkt_ticker, {})
            prefix = mkt_ticker.lower()

            # Market's own key indicators
            for key in ['RSI14', 'dist_sma50_atr', 'dist_sma200_atr',
                        'slope_sma50_5bar_atr', 'bb_pctb_20', 'roc_pct_20',
                        'STOC14', 'ADX14', 'price_pos_50', 'price_pos_120',
                        'cmf_20', 'consec_down_days', 'consec_up_days']:
                if key in mkt:
                    out[f'{prefix}_{key}'] = mkt[key]

            # Relative: stock minus market
            for key in ['RSI14', 'dist_sma50_atr', 'roc_pct_20',
                        'bb_pctb_20', 'price_pos_50']:
                if key in stock_row and key in mkt:
                    s_val = stock_row[key]
                    m_val = mkt[key]
                    if s_val is not None and m_val is not None and not (np.isnan(s_val) or np.isnan(m_val)):
                        out[f'rel_{prefix}_{key}'] = s_val - m_val

        return out

    # ----------------------------------------------------------
    # Layer 4: Rate of change of indicators
    # ----------------------------------------------------------

    def _compute_layer4(self, l1: dict[str, pd.Series], l2: dict[str, pd.Series]) -> dict[str, pd.Series]:
        """Indicator rate of change — value now vs N bars ago."""
        out = {}
        # Key indicators to track changes
        roc_targets = {
            'RSI14': [3, 5, 10],
            'ADX14': [3, 5],
            'STOC14': [3, 5],
            'ATR14': [5, 10],
            'MACD12_26': [3, 5],
        }
        for key, offsets in roc_targets.items():
            series = l1.get(key)
            if series is None:
                continue
            for off in offsets:
                out[f'{key}_chg_{off}bar'] = series - series.shift(off)

        # Derived indicator changes
        for key in ['dist_sma50_atr', 'dist_ema21_atr', 'pullback_h30_atr',
                     'vol_vs_avg20', 'bb_pctb_20', 'cmf_20']:
            series = l2.get(key)
            if series is None:
                continue
            for off in [3, 5]:
                out[f'{key}_chg_{off}bar'] = series - series.shift(off)

        # MA acceleration (second derivative)
        for p in [21, 50]:
            slope_key = f'slope_sma{p}_1bar_atr'
            series = l2.get(slope_key)
            if series is None:
                continue
            out[f'accel_sma{p}_3bar'] = series - series.shift(3)

        return out

    # ----------------------------------------------------------
    # Full profile for one ticker on one date
    # ----------------------------------------------------------

    def profile_ticker_date(self, ticker: str, target_date: str,
                            market_dfs: Optional[dict] = None) -> Optional[dict]:
        """Compute the full profile for one ticker on one date.

        Args:
            ticker: Stock ticker
            target_date: The scan bar date (1 day BEFORE entry)
            market_dfs: Pre-fetched {ticker: DataFrame} for SPY/QQQ

        Returns:
            dict of measurement_name -> value, or None if data insufficient
        """
        df = self._fetch_ohlcv(ticker, target_date)
        if len(df) < MAX_LOOKBACK * 0.6:  # Need at least ~150 bars
            return None

        # Find the target date row
        target_dt = pd.Timestamp(target_date)
        # Find closest date <= target_date
        mask = df['date'] <= target_dt
        if not mask.any():
            return None
        target_idx = df.loc[mask].index[-1]

        # Compute layers
        l1 = self._compute_layer1(df)
        l2 = self._compute_layer2(df, l1)
        l4 = self._compute_layer4(l1, l2)

        # Extract single row at target_idx
        result = {'ticker': ticker, 'date': target_date}

        # Add price primitives
        result['C'] = df.at[target_idx, 'close']
        result['O'] = df.at[target_idx, 'open']
        result['H'] = df.at[target_idx, 'high']
        result['L'] = df.at[target_idx, 'low']
        result['V'] = df.at[target_idx, 'volume']

        # Extract all indicator values at target_idx
        for name, series in l1.items():
            val = series.iloc[target_idx] if target_idx < len(series) else np.nan
            result[name] = float(val) if not pd.isna(val) else None

        for name, series in l2.items():
            val = series.iloc[target_idx] if target_idx < len(series) else np.nan
            result[name] = float(val) if not pd.isna(val) else None

        for name, series in l4.items():
            val = series.iloc[target_idx] if target_idx < len(series) else np.nan
            result[name] = float(val) if not pd.isna(val) else None

        # Layer 3: market context
        if market_dfs:
            market_rows = {}
            for mkt_ticker, mkt_df in market_dfs.items():
                if mkt_df is None or mkt_df.empty:
                    continue
                mkt_l1 = self._compute_layer1(mkt_df)
                mkt_l2 = self._compute_layer2(mkt_df, mkt_l1)
                mkt_mask = mkt_df['date'] <= target_dt
                if not mkt_mask.any():
                    continue
                mkt_idx = mkt_df.loc[mkt_mask].index[-1]
                mkt_row = {}
                for name, series in {**mkt_l1, **mkt_l2}.items():
                    val = series.iloc[mkt_idx] if mkt_idx < len(series) else np.nan
                    mkt_row[name] = float(val) if not pd.isna(val) else None
                market_rows[mkt_ticker] = mkt_row

            l3 = self._compute_layer3(result, market_rows)
            result.update(l3)

        return result

    # ----------------------------------------------------------
    # Profile all examples for a setup type
    # ----------------------------------------------------------

    def profile_examples(self, setup_type: str, include_market: bool = True,
                         progress_callback=None) -> pd.DataFrame:
        """Profile all examples for a setup type.

        Args:
            setup_type: e.g. "dtss", "3-4db"
            include_market: Whether to compute Layer 3 market context
            progress_callback: Optional fn(current, total, ticker) for progress

        Returns:
            DataFrame with one row per example, thousands of columns
        """
        examples = self._get_examples(setup_type)
        if not examples:
            raise ValueError(f"No examples found for setup type '{setup_type}'")

        # Pre-fetch market data for the date range
        market_dfs = None
        if include_market:
            all_dates = [e['entry_date'] for e in examples]
            max_date = max(all_dates)
            market_dfs = {}
            for mkt in ['SPY', 'QQQ']:
                market_dfs[mkt] = self._fetch_ohlcv(mkt, max_date)

        rows = []
        total = len(examples)
        for i, ex in enumerate(examples):
            ticker = ex['ticker']
            entry_date = ex['entry_date']

            # CRITICAL: scan bar = 1 trading day BEFORE entry
            # We fetch data up to entry_date, then the scan bar is the row before entry
            scan_date = self._get_scan_date(ticker, entry_date)
            if scan_date is None:
                if progress_callback:
                    progress_callback(i + 1, total, f"{ticker} — SKIPPED (no scan date)")
                continue

            if progress_callback:
                progress_callback(i + 1, total, f"{ticker} ({scan_date})")

            row = self.profile_ticker_date(ticker, scan_date, market_dfs)
            if row is not None:
                row['entry_date'] = entry_date
                row['scan_date'] = scan_date
                row['is_example'] = True
                rows.append(row)

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows)

    def _get_scan_date(self, ticker: str, entry_date: str) -> Optional[str]:
        """Get the trading day before entry_date for a ticker."""
        rows = self._query(
            f"SELECT date FROM universe_ohlcv "
            f"WHERE ticker='{ticker}' AND date<'{entry_date}' "
            f"ORDER BY date DESC LIMIT 1"
        )
        if rows:
            return rows[0]['date']
        return None

    # ----------------------------------------------------------
    # Profile a universe sample on a given date
    # ----------------------------------------------------------

    def profile_universe_sample(self, date: str, n: int = 500,
                                include_market: bool = True,
                                progress_callback=None) -> pd.DataFrame:
        """Profile a random sample of the tradable universe on a date.

        Args:
            date: The date to profile (should be a trading day)
            n: Number of tickers to sample
            include_market: Whether to compute Layer 3 market context
            progress_callback: Optional fn(current, total, ticker)

        Returns:
            DataFrame with one row per ticker, thousands of columns
        """
        tickers = self._get_tradable_sample(date, n)
        if not tickers:
            raise ValueError("No tradable tickers found")

        market_dfs = None
        if include_market:
            market_dfs = {}
            for mkt in ['SPY', 'QQQ']:
                market_dfs[mkt] = self._fetch_ohlcv(mkt, date)

        rows = []
        total = len(tickers)
        for i, ticker in enumerate(tickers):
            if progress_callback:
                progress_callback(i + 1, total, ticker)

            row = self.profile_ticker_date(ticker, date, market_dfs)
            if row is not None:
                row['is_example'] = False
                rows.append(row)

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows)

    # ----------------------------------------------------------
    # Combined: examples + universe sample side by side
    # ----------------------------------------------------------

    def profile_combined(self, setup_type: str, universe_date: str = None,
                         universe_n: int = 500,
                         progress_callback=None) -> pd.DataFrame:
        """Profile examples AND a universe sample, combined into one DataFrame.

        Args:
            setup_type: Setup type for examples
            universe_date: Date for universe profiling (defaults to most recent)
            universe_n: Number of universe tickers to sample
            progress_callback: Optional fn(current, total, ticker)

        Returns:
            Combined DataFrame with 'is_example' column to distinguish
        """
        if universe_date is None:
            # Get most recent trading date
            rows = self._query(
                "SELECT MAX(date) as d FROM universe_ohlcv WHERE ticker='SPY'"
            )
            universe_date = rows[0]['d'] if rows else None

        if progress_callback:
            progress_callback(0, 0, "Profiling examples...")

        examples_df = self.profile_examples(
            setup_type, include_market=True, progress_callback=progress_callback
        )

        if progress_callback:
            progress_callback(0, 0, "Profiling universe sample...")

        universe_df = self.profile_universe_sample(
            universe_date, n=universe_n, include_market=True,
            progress_callback=progress_callback
        )

        combined = pd.concat([examples_df, universe_df], ignore_index=True)
        return combined


# ============================================================
# CLI for standalone testing
# ============================================================

if __name__ == "__main__":
    import sys
    import time

    engine = ProfilingEngine()

    if len(sys.argv) > 1:
        setup_type = sys.argv[1]
    else:
        setup_type = "dtss"

    def progress(cur, total, msg):
        if total > 0:
            print(f"  [{cur}/{total}] {msg}")
        else:
            print(f"  {msg}")

    print(f"\nProfiling examples for setup: {setup_type}")
    t0 = time.time()
    df = engine.profile_examples(setup_type, include_market=True, progress_callback=progress)
    elapsed = time.time() - t0

    if df.empty:
        print("No results!")
    else:
        print(f"\nDone in {elapsed:.1f}s")
        print(f"Rows: {len(df)}")
        print(f"Columns: {len(df.columns)}")
        # Count non-null measurements
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        print(f"Numeric measurement columns: {len(numeric_cols)}")
        print(f"\nTickers profiled: {', '.join(df['ticker'].tolist())}")

        # Show a sample of column names
        cols = sorted(numeric_cols.tolist())
        print(f"\nSample columns ({len(cols)} total):")
        for c in cols[:30]:
            print(f"  {c}")
        print(f"  ... and {len(cols)-30} more")
