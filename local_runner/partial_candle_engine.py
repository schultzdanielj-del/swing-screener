"""
Partial Candle Engine — Eliminates HTF look-ahead bias in expression cache.

Instead of mapping closed weekly/monthly candle values to all daily bars
within the period (giving Monday the benefit of Friday's close), this engine
computes HTF expression values using partial candles that reflect only the
data available on each day.

Architecture:
1. Compute full intermediate arrays on the CLOSED HTF series (once per ticker)
2. Build partial candle OHLCV arrays at daily resolution
3. Extend each intermediate to daily resolution using vectorized update rules
4. Dispatch all expressions from the daily-resolution intermediates
5. Booleans and extension structure handled with window lookups
"""

import numpy as np
import pandas as pd
import warnings

warnings.filterwarnings("ignore", category=RuntimeWarning)


# ══════════════════════════════════════════════════════════════
# 1. DAILY → HTF PERIOD MAPPING + PARTIAL CANDLE CONSTRUCTION
# ══════════════════════════════════════════════════════════════

def build_partial_candle_mapping(daily_df, htf_df, freq):
    """Map each daily bar to its HTF period and build partial candles.

    Returns:
        lci: int32 array (n_daily,) — last closed HTF index before each bar.
             -1 if no closed period exists yet.
        partial: dict with 'open','high','low','close','volume' arrays (n_daily,)
        prev_close: float64 array (n_daily,) — close of last completed HTF bar
    """
    daily_dates = pd.to_datetime(daily_df['date']).values
    htf_dates = pd.to_datetime(htf_df['date']).values
    n_daily = len(daily_dates)

    d_open = daily_df['open'].values.astype(np.float64)
    d_high = daily_df['high'].values.astype(np.float64)
    d_low = daily_df['low'].values.astype(np.float64)
    d_close = daily_df['close'].values.astype(np.float64)
    d_volume = daily_df['volume'].values.astype(np.float64)
    htf_close = htf_df['close'].values.astype(np.float64)

    # Assign each daily bar to a period using year+week or year+month
    daily_pd = pd.DatetimeIndex(daily_dates)
    if freq == 'W':
        iso = daily_pd.isocalendar()
        period_ids = iso.year.values * 100 + iso.week.values
    else:
        period_ids = daily_pd.year * 100 + daily_pd.month

    # For each period, find the date of its first daily bar
    unique_periods, first_indices = np.unique(period_ids, return_index=True)
    period_start_map = {}
    for up, fi in zip(unique_periods, first_indices):
        period_start_map[up] = daily_dates[fi]

    first_daily_in_period = np.array([period_start_map[pid] for pid in period_ids])

    # Last closed HTF index: the last HTF bar whose date < first daily date of the period
    lci = np.searchsorted(htf_dates, first_daily_in_period, side='left').astype(np.int32) - 1

    # Build partial candles by accumulating daily OHLCV within each period
    p_open = np.empty(n_daily, dtype=np.float64)
    p_high = np.empty(n_daily, dtype=np.float64)
    p_low = np.empty(n_daily, dtype=np.float64)
    p_close = np.empty(n_daily, dtype=np.float64)
    p_volume = np.empty(n_daily, dtype=np.float64)

    cur_period = -1
    cur_o = cur_h = cur_l = cur_c = cur_v = np.nan

    for i in range(n_daily):
        pid = period_ids[i]
        if pid != cur_period:
            cur_period = pid
            cur_o = d_open[i]
            cur_h = d_high[i]
            cur_l = d_low[i]
            cur_c = d_close[i]
            cur_v = d_volume[i]
        else:
            if d_high[i] > cur_h:
                cur_h = d_high[i]
            if d_low[i] < cur_l:
                cur_l = d_low[i]
            cur_c = d_close[i]
            cur_v += d_volume[i]

        p_open[i] = cur_o
        p_high[i] = cur_h
        p_low[i] = cur_l
        p_close[i] = cur_c
        p_volume[i] = cur_v

    # Previous close: close of the last completed HTF bar
    prev_close = np.full(n_daily, np.nan, dtype=np.float64)
    valid = lci >= 0
    prev_close[valid] = htf_close[lci[valid]]

    return lci, {
        'open': p_open, 'high': p_high, 'low': p_low,
        'close': p_close, 'volume': p_volume,
    }, prev_close


# ══════════════════════════════════════════════════════════════
# 2. CLOSED SERIES STATE EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_closed_state(engine, htf_df):
    """Extract intermediate arrays + additional raw arrays from closed HTF series.

    Returns:
        closed_im: dict of intermediate arrays (from build_numpy_intermediates)
        closed_raw: dict of additional raw arrays for incremental computation
    """
    from local_runner.expr_cache_builder import build_numpy_intermediates

    closed_im = build_numpy_intermediates(engine)
    n = len(htf_df)

    c = closed_im['close']
    h = closed_im['high']
    l = closed_im['low']
    o = closed_im['open']
    v = closed_im['volume']

    raw = {}

    # Cumulative sums for SMA-based lookups
    raw['cumsum_c'] = np.nancumsum(c)
    raw['cumsum_v'] = np.nancumsum(v)

    hl = h - l
    raw['hl'] = hl
    raw['cumsum_hl'] = np.nancumsum(hl)

    # True range for ATR
    prev_c = np.full(n, np.nan)
    prev_c[1:] = c[:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    raw['tr'] = tr
    raw['cumsum_tr'] = np.nancumsum(tr)

    # Gains/losses for RSI
    delta = np.full(n, np.nan)
    delta[1:] = c[1:] - c[:-1]
    gains = np.maximum(0, delta)
    losses = np.maximum(0, -delta)
    raw['gains'] = gains
    raw['losses'] = losses
    raw['cumsum_gains'] = np.nancumsum(gains)
    raw['cumsum_losses'] = np.nancumsum(losses)

    # DM+/DM- for ADX chain
    prev_h = np.full(n, np.nan); prev_h[1:] = h[:-1]
    prev_l = np.full(n, np.nan); prev_l[1:] = l[:-1]
    up = h - prev_h
    down = prev_l - l
    dm_plus = np.where((up > down) & (up > 0), up, 0.0).astype(np.float64)
    dm_minus = np.where((down > up) & (down > 0), down, 0.0).astype(np.float64)
    dm_plus[0] = np.nan; dm_minus[0] = np.nan

    for p in [7, 10, 14, 20]:
        ema_dmp = pd.Series(dm_plus).ewm(span=p, adjust=False, min_periods=p).mean().values
        ema_dmm = pd.Series(dm_minus).ewm(span=p, adjust=False, min_periods=p).mean().values
        raw[f'ema_dmp_{p}'] = ema_dmp
        raw[f'ema_dmm_{p}'] = ema_dmm

        atr_sma = closed_im['atr14']
        with np.errstate(divide='ignore', invalid='ignore'):
            dip = np.where(atr_sma != 0, 100 * ema_dmp / atr_sma, np.nan)
            dim = np.where(atr_sma != 0, 100 * ema_dmm / atr_sma, np.nan)
        di_sum = dip + dim
        with np.errstate(divide='ignore', invalid='ignore'):
            dx = np.where(di_sum != 0, np.abs(dip - dim) / di_sum * 100, np.nan)
        raw[f'dx_{p}'] = dx
        raw[f'ema_dx_{p}'] = pd.Series(dx).ewm(span=p, adjust=False, min_periods=p).mean().values

    # BOP raw
    with np.errstate(divide='ignore', invalid='ignore'):
        hl_safe = np.where(hl != 0, hl, np.nan)
        bop_raw = (c - o) / hl_safe
    raw['bop_raw'] = bop_raw
    raw['cumsum_bop_raw'] = np.nancumsum(np.nan_to_num(bop_raw, nan=0.0))

    # CMF components
    with np.errstate(divide='ignore', invalid='ignore'):
        mfm = ((c - l) - (h - c)) / hl_safe
    mfv = mfm * v
    raw['mfv'] = mfv
    raw['cumsum_mfv'] = np.nancumsum(np.nan_to_num(mfv, nan=0.0))

    # OBV
    sign = np.sign(delta)
    sign = np.nan_to_num(sign, nan=0.0)
    raw['obv_series'] = np.cumsum(sign * v)

    # Kaufman: |diff| for volatility
    abs_diff = np.abs(delta)
    abs_diff = np.nan_to_num(abs_diff, nan=0.0)
    raw['abs_diff'] = abs_diff
    raw['cumsum_abs_diff'] = np.cumsum(abs_diff)

    # Typical price for CCI
    tp = (h + l + c) / 3.0
    raw['tp'] = tp
    raw['cumsum_tp'] = np.nancumsum(tp)

    # Sum of squares of close for Bollinger stddev
    raw['cumsum_c2'] = np.nancumsum(c ** 2)

    # MACD signal line (EMA of MACD line)
    for fast, slow in [(12, 26), (8, 17), (5, 35), (5, 13), (6, 19)]:
        macd_line = closed_im.get(f'macd_{fast}_{slow}')
        if macd_line is not None:
            raw[f'macd_signal_{fast}_{slow}'] = pd.Series(macd_line).ewm(
                span=9, adjust=False, min_periods=9).mean().values

    # CMF closed series (for CMF slope)
    for p in [10, 14, 20, 30, 50]:
        sum_mfv_c = pd.Series(np.nan_to_num(mfv, nan=0.0)).rolling(p, min_periods=p).sum().values
        sum_v_c = pd.Series(v).rolling(p, min_periods=p).sum().values
        with np.errstate(divide='ignore', invalid='ignore'):
            raw[f'cmf_closed_{p}'] = np.where(sum_v_c != 0, sum_mfv_c / sum_v_c, np.nan)

    # Stochastic raw_K for smoothing
    for p in [3, 5, 7, 9, 10, 14, 21, 28, 50]:
        maxh_key = f'maxh{p}'
        minl_key = f'minl{p}'
        if maxh_key in closed_im and minl_key in closed_im:
            maxh_c = closed_im[maxh_key]
            minl_c = closed_im[minl_key]
            diff_c = maxh_c - minl_c
            with np.errstate(divide='ignore', invalid='ignore'):
                raw[f'raw_k_{p}'] = np.where(diff_c != 0,
                                              (c - minl_c) / diff_c * 100, np.nan)

    return closed_im, raw


# ══════════════════════════════════════════════════════════════
# 3. VECTORIZED HELPERS
# ══════════════════════════════════════════════════════════════

def _partial_sma(cumsum_closed, lci, period, partial_value):
    """SMA at partial position: window of (period-1) closed + partial, / period."""
    n = len(lci)
    result = np.full(n, np.nan, dtype=np.float64)
    valid = lci >= period - 2  # need at least period-1 closed values
    if not np.any(valid):
        return result
    end = lci[valid]
    start = end - (period - 1)
    # Sum of closed values in [start+1 .. end] = cumsum[end] - cumsum[start]
    cs = cumsum_closed
    window_sum = np.where(start >= 0, cs[end] - cs[start], cs[end])
    # Verify window has enough values
    has_enough = (end - np.maximum(start, -1)) >= period - 1
    window_sum = np.where(has_enough, window_sum, np.nan)
    result[valid] = (window_sum + partial_value[valid]) / period
    return result


def _partial_ema(ema_closed, lci, alpha, partial_value):
    """EMA at partial: alpha * partial + (1-alpha) * ema_closed[lci]."""
    n = len(lci)
    result = np.full(n, np.nan, dtype=np.float64)
    valid = lci >= 0
    if np.any(valid):
        result[valid] = alpha * partial_value[valid] + (1 - alpha) * ema_closed[lci[valid]]
    return result


def _partial_rolling_max(closed_arr, lci, period, partial_value):
    """Rolling max: max of (period-1) closed ending at lci + partial."""
    if period <= 1:
        return partial_value.copy()
    rmax = pd.Series(closed_arr).rolling(period - 1, min_periods=1).max().values
    n = len(lci)
    result = np.full(n, np.nan, dtype=np.float64)
    valid = lci >= 0
    if np.any(valid):
        result[valid] = np.maximum(rmax[lci[valid]], partial_value[valid])
    result[lci < period - 2] = np.nan
    return result


def _partial_rolling_min(closed_arr, lci, period, partial_value):
    """Rolling min: min of (period-1) closed ending at lci + partial."""
    if period <= 1:
        return partial_value.copy()
    rmin = pd.Series(closed_arr).rolling(period - 1, min_periods=1).min().values
    n = len(lci)
    result = np.full(n, np.nan, dtype=np.float64)
    valid = lci >= 0
    if np.any(valid):
        result[valid] = np.minimum(rmin[lci[valid]], partial_value[valid])
    result[lci < period - 2] = np.nan
    return result


def _partial_shift(closed_arr, lci, offset):
    """Value `offset` HTF bars before partial position = closed_arr[lci - offset + 1]."""
    n = len(lci)
    result = np.full(n, np.nan, dtype=np.float64)
    idx = lci - offset + 1
    valid = (idx >= 0) & (idx < len(closed_arr))
    if np.any(valid):
        result[valid] = closed_arr[idx[valid]]
    return result


def _safe_div(a, b):
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(b != 0, a / b, np.nan)
    mask = np.isnan(a) | np.isnan(b)
    result[mask] = np.nan
    return result


# ══════════════════════════════════════════════════════════════
# 4. BUILD PARTIAL INTERMEDIATES
# ══════════════════════════════════════════════════════════════

def build_partial_intermediates(closed_im, closed_raw, lci, partial, prev_close):
    """Build daily-resolution intermediate arrays for partial candle values."""
    pc = partial['close']
    ph = partial['high']
    pl = partial['low']
    po = partial['open']
    pv = partial['volume']
    n_daily = len(pc)

    im = {}
    im['close'] = pc.copy()
    im['open'] = po.copy()
    im['high'] = ph.copy()
    im['low'] = pl.copy()
    im['volume'] = pv.copy()
    im['pct'] = pc / 100.0

    # ── SMA of close ──
    for p in [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]:
        im[f'avgc{p}'] = _partial_sma(closed_raw['cumsum_c'], lci, p, pc)

    # ── EMA of close ──
    for p in [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200]:
        alpha = 2.0 / (p + 1)
        im[f'xavgc{p}'] = _partial_ema(closed_im[f'xavgc{p}'], lci, alpha, pc)

    # ── SMA of volume ──
    for p in [10, 20, 50]:
        im[f'avgv{p}'] = _partial_sma(closed_raw['cumsum_v'], lci, p, pv)

    # ── ATR (SMA of TR) ──
    partial_tr = np.maximum(ph - pl,
                            np.maximum(np.abs(ph - prev_close),
                                       np.abs(pl - prev_close)))
    im['atr14'] = _partial_sma(closed_raw['cumsum_tr'], lci, 14, partial_tr)

    # ── ADR (SMA of H-L) ──
    partial_hl = ph - pl
    im['adr14'] = _partial_sma(closed_raw['cumsum_hl'], lci, 14, partial_hl)

    # ── MaxH / MinL ──
    for p in sorted(set(list(range(5, 125, 5)) + [2, 3, 7, 10, 15, 63, 65, 126])):
        im[f'maxh{p}'] = _partial_rolling_max(closed_im['high'], lci, p, ph)
    for p in sorted(set(list(range(5, 65, 5)) + [2, 3, 7, 65, 90, 120, 126])):
        im[f'minl{p}'] = _partial_rolling_min(closed_im['low'], lci, p, pl)

    # ── RSI (SMA-based) ──
    partial_delta = pc - prev_close
    partial_gain = np.maximum(0, partial_delta)
    partial_loss = np.maximum(0, -partial_delta)
    for p in [5, 7, 9, 14, 21, 28]:
        avg_gain = _partial_sma(closed_raw['cumsum_gains'], lci, p, partial_gain)
        avg_loss = _partial_sma(closed_raw['cumsum_losses'], lci, p, partial_loss)
        with np.errstate(divide='ignore', invalid='ignore'):
            rs = np.where(avg_loss != 0, avg_gain / avg_loss, np.nan)
            im[f'rsi{p}'] = 100.0 - (100.0 / (1.0 + rs))

    # ── ADX / DI+ / DI- ──
    for p in [7, 10, 14, 20]:
        alpha = 2.0 / (p + 1)
        prev_h_arr = np.full(n_daily, np.nan)
        prev_l_arr = np.full(n_daily, np.nan)
        v = lci >= 0
        if np.any(v):
            prev_h_arr[v] = closed_im['high'][lci[v]]
            prev_l_arr[v] = closed_im['low'][lci[v]]
        up = ph - prev_h_arr
        down = prev_l_arr - pl
        pdm_plus = np.where((up > down) & (up > 0), up, 0.0)
        pdm_minus = np.where((down > up) & (down > 0), down, 0.0)

        ema_dmp_p = _partial_ema(closed_raw[f'ema_dmp_{p}'], lci, alpha, pdm_plus)
        ema_dmm_p = _partial_ema(closed_raw[f'ema_dmm_{p}'], lci, alpha, pdm_minus)
        atr_p = im['atr14']
        with np.errstate(divide='ignore', invalid='ignore'):
            dip = np.where(atr_p != 0, 100 * ema_dmp_p / atr_p, np.nan)
            dim = np.where(atr_p != 0, 100 * ema_dmm_p / atr_p, np.nan)
        im[f'diplus{p}'] = dip
        im[f'diminus{p}'] = dim
        di_sum = dip + dim
        with np.errstate(divide='ignore', invalid='ignore'):
            dx_p = np.where(di_sum != 0, np.abs(dip - dim) / di_sum * 100, np.nan)
        im[f'adx{p}'] = _partial_ema(closed_raw[f'ema_dx_{p}'], lci, alpha, dx_p)

    # ── Stochastic ──
    for p in [3, 5, 7, 9, 10, 14, 21, 28, 50]:
        hh = _partial_rolling_max(closed_im['high'], lci, p, ph)
        ll = _partial_rolling_min(closed_im['low'], lci, p, pl)
        diff = hh - ll
        with np.errstate(divide='ignore', invalid='ignore'):
            raw_k = np.where(diff != 0, (pc - ll) / diff * 100, np.nan)
        rk_key = f'raw_k_{p}'
        if rk_key in closed_raw:
            rk_closed = closed_raw[rk_key]
            rk_m1 = _partial_shift(rk_closed, lci, 1)
            rk_m2 = _partial_shift(rk_closed, lci, 2)
            im[f'stoch{p}'] = (rk_m2 + rk_m1 + raw_k) / 3.0
        else:
            im[f'stoch{p}'] = raw_k  # fallback: no smoothing

    # ── CCI ──
    partial_tp = (ph + pl + pc) / 3.0
    tp_closed = closed_raw['tp']
    for p in [5, 7, 10, 14, 20, 30, 50]:
        tp_sma = _partial_sma(closed_raw['cumsum_tp'], lci, p, partial_tp)
        result = np.full(n_daily, np.nan, dtype=np.float64)
        valid = lci >= p - 1
        if np.any(valid):
            vi = np.where(valid)[0]
            for idx in vi:
                k = lci[idx]
                start_k = k - p + 2
                if start_k < 0:
                    continue
                window_tp = np.empty(p, dtype=np.float64)
                window_tp[:p-1] = tp_closed[start_k:k+1]
                window_tp[p-1] = partial_tp[idx]
                mean_tp = np.mean(window_tp)
                mean_dev = np.mean(np.abs(window_tp - mean_tp))
                if mean_dev != 0:
                    result[idx] = (partial_tp[idx] - mean_tp) / (0.015 * mean_dev)
        im[f'cci{p}'] = result

    # ── BOP ──
    with np.errstate(divide='ignore', invalid='ignore'):
        p_hl_safe = np.where(partial_hl != 0, partial_hl, np.nan)
        partial_bop_raw = (pc - po) / p_hl_safe
    for p in [5, 10, 14, 20]:
        im[f'bop{p}'] = _partial_sma(closed_raw['cumsum_bop_raw'], lci, p, partial_bop_raw)

    # ── OBV ──
    obv_closed = closed_raw['obv_series']
    obv_p = np.full(n_daily, np.nan, dtype=np.float64)
    v_mask = lci >= 0
    if np.any(v_mask):
        obv_base = obv_closed[lci[v_mask]]
        pdelta = pc[v_mask] - prev_close[v_mask]
        obv_p[v_mask] = obv_base + np.sign(pdelta) * pv[v_mask]
    im['obv'] = obv_p

    # ── MACD ──
    for fast, slow in [(12, 26), (8, 17), (5, 35), (5, 13), (6, 19)]:
        ef = im.get(f'xavgc{fast}')
        es = im.get(f'xavgc{slow}')
        if ef is not None and es is not None:
            im[f'macd_{fast}_{slow}'] = ef - es
        else:
            im[f'macd_{fast}_{slow}'] = np.full(n_daily, np.nan)

    # ── Bollinger ──
    cumsum_c2 = closed_raw['cumsum_c2']
    c_closed = closed_im['close']
    for p in [5, 10, 15, 20, 30, 50]:
        sma_p = im[f'avgc{p}']
        sum_sq = np.full(n_daily, np.nan, dtype=np.float64)
        vld = lci >= p - 2
        if np.any(vld):
            end = lci[vld]
            start = end - (p - 1)
            s = np.where(start >= 0, cumsum_c2[end] - cumsum_c2[start], cumsum_c2[end])
            has = (end - np.maximum(start, -1)) >= p - 1
            sum_sq[vld] = np.where(has, s, np.nan)
        mean_sq = (sum_sq + pc ** 2) / p
        variance = np.maximum(mean_sq - sma_p ** 2, 0)
        std_p = np.sqrt(variance)
        im[f'stddev_{p}'] = std_p
        im[f'bbtop_{p}'] = sma_p + 2.0 * std_p
        im[f'bbbot_{p}'] = sma_p - 2.0 * std_p

    # ── Aroon ──
    h_closed = closed_im['high']
    l_closed = closed_im['low']
    for p in [7, 10, 14, 20, 25, 50, 100]:
        aroon_up = np.full(n_daily, np.nan, dtype=np.float64)
        aroon_dn = np.full(n_daily, np.nan, dtype=np.float64)
        valid = lci >= p - 2
        if np.any(valid):
            vi = np.where(valid)[0]
            for idx in vi:
                k = lci[idx]
                sk = k - p + 2
                if sk < 0:
                    continue
                wh = np.empty(p, dtype=np.float64)
                wl = np.empty(p, dtype=np.float64)
                wh[:p-1] = h_closed[sk:k+1]
                wh[p-1] = ph[idx]
                wl[:p-1] = l_closed[sk:k+1]
                wl[p-1] = pl[idx]
                aroon_up[idx] = ((p - (p - 1 - np.argmax(wh))) / p) * 100
                aroon_dn[idx] = ((p - (p - 1 - np.argmin(wl))) / p) * 100
        im[f'aroon_up_{p}'] = aroon_up
        im[f'aroon_down_{p}'] = aroon_dn

    # ── CMF ──
    with np.errstate(divide='ignore', invalid='ignore'):
        p_hl_safe2 = np.where(partial_hl != 0, partial_hl, np.nan)
        partial_mfm = ((pc - pl) - (ph - pc)) / p_hl_safe2
    partial_mfv = partial_mfm * pv
    for p in [10, 14, 20, 30, 50]:
        sum_mfv = _partial_sma(closed_raw['cumsum_mfv'], lci, p, partial_mfv) * p
        sum_vol = _partial_sma(closed_raw['cumsum_v'], lci, p, pv) * p
        with np.errstate(divide='ignore', invalid='ignore'):
            im[f'cmf_{p}'] = np.where(sum_vol != 0, sum_mfv / sum_vol, np.nan)

    # ── Kaufman Efficiency ──
    partial_abs_diff = np.abs(pc - prev_close)
    for p in [5, 7, 10, 15, 20, 30, 50, 65, 100]:
        c_shifted_p = _partial_shift(closed_im['close'], lci, p)
        direction = np.abs(pc - c_shifted_p)
        volatility = _partial_sma(closed_raw['cumsum_abs_diff'], lci, p, partial_abs_diff) * p
        with np.errstate(divide='ignore', invalid='ignore'):
            im[f'kauf_eff_{p}'] = np.where(volatility != 0, direction / volatility, np.nan)

    # ── MaxC ──
    for p in [10, 20, 50]:
        im[f'maxc{p}'] = _partial_rolling_max(closed_im['close'], lci, p, pc)

    return im


# ══════════════════════════════════════════════════════════════
# 5. PARTIAL-AWARE EXPRESSION DISPATCH
# ══════════════════════════════════════════════════════════════

def dispatch_partial_arith(comp, im_p, closed_im, closed_raw, lci):
    """Dispatch arithmetic expression using partial intermediates.

    Same ops as dispatch_arith_numpy but shift/rolling are HTF-aware.
    """
    op = comp.get("op", "")
    C = im_p["close"]; O = im_p["open"]; H = im_p["high"]
    L = im_p["low"]; V = im_p["volume"]

    def _gm(name):
        return im_p.get(name)
    def _gn(name):
        if name == "atr14": return im_p["atr14"]
        elif name == "adr14": return im_p["adr14"]
        elif name == "pct": return im_p["pct"]
        elif name == "close": return im_p["close"]
        return im_p.get("atr14")
    def _sp(key, off):
        ca = closed_im.get(key)
        if ca is None: return np.full_like(C, np.nan)
        return _partial_shift(ca, lci, off)

    try:
        if op == "ma_slope":
            ma = _gm(comp["ma"])
            if ma is None: return None
            return _safe_div(ma - _sp(comp["ma"], comp["offset"]),
                             _gn(comp["normalizer"]))

        elif op == "ma_spread":
            f = _gm(comp["ma_fast"]); s = _gm(comp["ma_slow"])
            if f is None or s is None: return None
            return _safe_div(f - s, _gn(comp["normalizer"]))

        elif op == "extension":
            ma = _gm(comp["ma"])
            if ma is None: return None
            return _safe_div(C - ma, _gn(comp["normalizer"]))

        elif op == "distance_to_maxh":
            price = C if comp["price_ref"] == "C" else H
            ms = _sp(f"maxh{comp['maxh_period']}", 1)
            return _safe_div(ms - price, _gn(comp["normalizer"]))

        elif op == "ratio_c_maxh":
            return _safe_div(C, _sp(f"maxh{comp['maxh_period']}", 1))

        elif op == "distance_to_minl":
            price = C if comp["price_ref"] == "C" else L
            return _safe_div(price - _sp(f"minl{comp['minl_period']}", 1),
                             _gn(comp["normalizer"]))

        elif op == "ratio_c_minl":
            return _safe_div(C, _sp(f"minl{comp['minl_period']}", 1))

        elif op == "extension_slope":
            ma = _gm(comp["ma"])
            if ma is None: return None
            ext = C - ma
            ext_s = _sp("close", comp["offset"]) - _sp(comp["ma"], comp["offset"])
            return _safe_div(ext - ext_s, _gn(comp["normalizer"]))

        elif op == "extension_peak_ratio":
            ma = _gm(comp["ma"])
            if ma is None: return None
            ext = C - ma
            ma_c = closed_im.get(comp["ma"])
            if ma_c is None: return None
            ext_c = closed_im["close"] - ma_c
            mx = _partial_rolling_max(ext_c, lci, comp["lookback"], ext)
            return _safe_div(ext, mx)

        elif op == "extension_ceiling_ratio":
            ma = _gm(comp["ma"])
            if ma is None: return None
            norm = _gn(comp["normalizer"])
            ext_norm = _safe_div(C - ma, norm)
            ma_c = closed_im.get(comp["ma"])
            nc = closed_im.get(comp.get("normalizer", "atr14"),
                               closed_im.get("atr14"))
            if ma_c is None or nc is None: return None
            ext_c = _safe_div(closed_im["close"] - ma_c, nc)
            mx = _partial_rolling_max(ext_c, lci, comp["lookback"], ext_norm)
            return _safe_div(ext_norm, mx)

        elif op == "ext_adr_multiples":
            ma = _gm(comp["ma"])
            if ma is None: return None
            return _safe_div(C - ma, im_p["adr14"])

        elif op == "spread_slope":
            f = _gm(comp["ma_fast"]); s = _gm(comp["ma_slow"])
            if f is None or s is None: return None
            n = _gn(comp["normalizer"])
            spread = _safe_div(f - s, n)
            fs = _sp(comp["ma_fast"], comp["offset"])
            ss = _sp(comp["ma_slow"], comp["offset"])
            ns = _partial_shift(closed_im.get(comp.get("normalizer", "atr14"),
                                              closed_im.get("atr14", np.zeros(1))),
                                lci, comp["offset"])
            return spread - _safe_div(fs - ss, ns)

        elif op == "pullback":
            mh = im_p.get(f"maxh{comp['period']}")
            if mh is None: return None
            return _safe_div(mh - C, _gn(comp["normalizer"]))

        elif op == "range_position":
            p = comp["period"]
            mh = im_p.get(f"maxh{p}", _partial_rolling_max(closed_im["high"], lci, p, H))
            ml = im_p.get(f"minl{p}", _partial_rolling_min(closed_im["low"], lci, p, L))
            return _safe_div(C - ml, mh - ml)

        elif op == "range_width":
            p = comp["period"]
            mh = im_p.get(f"maxh{p}", _partial_rolling_max(closed_im["high"], lci, p, H))
            ml = im_p.get(f"minl{p}", _partial_rolling_min(closed_im["low"], lci, p, L))
            return _safe_div(mh - ml, _gn(comp["normalizer"]))

        elif op == "roc":
            return _safe_div(C, _sp("close", comp["period"])) * 100.0 - 100.0

        elif op == "roc_delta":
            p = comp["period"]; co = comp["compare_offset"]
            rn = _safe_div(C, _sp("close", p)) - 1.0
            rp = _safe_div(_sp("close", co), _sp("close", co + p)) - 1.0
            return 100.0 * (rn - rp)

        elif op == "adx": return im_p.get(f"adx{comp['period']}")
        elif op == "adx_slope":
            a = im_p.get(f"adx{comp['period']}")
            if a is None: return None
            return a - _sp(f"adx{comp['period']}", comp["offset"])

        elif op == "rsi": return im_p.get(f"rsi{comp['period']}")
        elif op == "rsi_slope":
            r = im_p.get(f"rsi{comp['period']}")
            if r is None: return None
            return r - _sp(f"rsi{comp['period']}", comp["offset"])

        elif op == "stochastic": return im_p.get(f"stoch{comp['period']}")
        elif op == "cci": return im_p.get(f"cci{comp['period']}")
        elif op == "di_spread":
            dp = im_p.get(f"diplus{comp['period']}")
            dm = im_p.get(f"diminus{comp['period']}")
            if dp is None or dm is None: return None
            return dp - dm

        elif op == "volume_ratio":
            av = im_p.get(f"avgv{comp['avg_period']}")
            return _safe_div(V, av) if av is not None else None

        elif op == "candle_range_ratio": return _safe_div(H - L, im_p["atr14"])
        elif op == "body_range_ratio": return _safe_div(np.abs(C - O), H - L)
        elif op == "upper_wick_ratio": return _safe_div(H - np.maximum(C, O), H - L)
        elif op == "lower_wick_ratio": return _safe_div(np.minimum(C, O) - L, H - L)
        elif op == "bop": return im_p.get(f"bop{comp['period']}")

        elif op == "obv_slope":
            obv = im_p.get("obv")
            if obv is None: return None
            off = comp["offset"]
            avg = im_p.get(f"avgv{comp.get('vol_period', 20)}")
            obv_s = _partial_shift(closed_raw.get('obv_series', np.zeros(1)), lci, off)
            if avg is not None:
                return _safe_div(obv - obv_s, avg * off)
            return None

        elif op == "macd_histogram":
            f = comp.get("fast", 12); s = comp.get("slow", 26)
            ml = im_p.get(f"macd_{f}_{s}")
            if ml is None: return None
            sk = f'macd_signal_{f}_{s}'
            sc = closed_raw.get(sk)
            if sc is None: return None
            alpha_s = 2.0 / (comp.get("signal", 9) + 1)
            sig = _partial_ema(sc, lci, alpha_s, ml)
            return ml - sig

        elif op == "macd_histogram_slope":
            f = comp.get("fast", 12); s = comp.get("slow", 26)
            ml = im_p.get(f"macd_{f}_{s}")
            if ml is None: return None
            sk = f'macd_signal_{f}_{s}'
            sc = closed_raw.get(sk)
            if sc is None: return None
            alpha_s = 2.0 / (comp.get("signal", 9) + 1)
            sig = _partial_ema(sc, lci, alpha_s, ml)
            hist = ml - sig
            mc = closed_im.get(f"macd_{f}_{s}")
            if mc is None: return None
            hist_c = mc - sc
            return hist - _partial_shift(hist_c, lci, comp["offset"])

        elif op == "macd_line_norm":
            f = comp.get("fast", 12); s = comp.get("slow", 26)
            ml = im_p.get(f"macd_{f}_{s}")
            if ml is None: return None
            return _safe_div(ml, _gn(comp["normalizer"]))

        elif op == "bollinger_pctb":
            p = comp["period"]
            t = im_p.get(f"bbtop_{p}"); b = im_p.get(f"bbbot_{p}")
            if t is None or b is None: return None
            return _safe_div(C - b, t - b)

        elif op == "bollinger_bandwidth":
            p = comp["period"]
            t = im_p.get(f"bbtop_{p}"); b = im_p.get(f"bbbot_{p}")
            m = im_p.get(f"avgc{p}")
            if t is None or b is None or m is None: return None
            return _safe_div(t - b, m)

        elif op == "bollinger_bandwidth_rank":
            p = comp["period"]; lb = comp["lookback"]
            t = im_p.get(f"bbtop_{p}"); b = im_p.get(f"bbbot_{p}")
            m = im_p.get(f"avgc{p}")
            if t is None or b is None or m is None: return None
            bw = _safe_div(t - b, m)
            tc = closed_im.get(f"bbtop_{p}"); bc = closed_im.get(f"bbbot_{p}")
            mc = closed_im.get(f"avgc{p}")
            if tc is None or bc is None or mc is None: return None
            bwc = _safe_div(tc - bc, mc)
            return _safe_div(bw - _partial_rolling_min(bwc, lci, lb, bw),
                             _partial_rolling_max(bwc, lci, lb, bw) -
                             _partial_rolling_min(bwc, lci, lb, bw))

        elif op == "aroon_up_val": return im_p.get(f"aroon_up_{comp['period']}")
        elif op == "aroon_down_val": return im_p.get(f"aroon_down_{comp['period']}")
        elif op == "aroon_oscillator":
            p = comp["period"]
            u = im_p.get(f"aroon_up_{p}"); d = im_p.get(f"aroon_down_{p}")
            if u is None or d is None: return None
            return u - d

        elif op == "cmf": return im_p.get(f"cmf_{comp['period']}")
        elif op == "cmf_slope":
            cv = im_p.get(f"cmf_{comp['period']}")
            if cv is None: return None
            cc = closed_raw.get(f"cmf_closed_{comp['period']}")
            if cc is None: return None
            return cv - _partial_shift(cc, lci, comp["offset"])

        elif op == "kaufman_efficiency_ratio":
            return im_p.get(f"kauf_eff_{comp['period']}")

        elif op == "atr_ratio":
            return _safe_div(im_p["atr14"], _sp("atr14", comp["offset"]))

        elif op == "slope_ratio":
            fm = _gm(comp["fast_ma"]); sm = _gm(comp["slow_ma"])
            if fm is None or sm is None: return None
            off = comp["offset"]
            fs = _sp(comp["fast_ma"], off); ss = _sp(comp["slow_ma"], off)
            den = sm - ss
            with np.errstate(divide='ignore', invalid='ignore'):
                return np.where(den != 0, (fm - fs) / den, np.nan)

        elif op == "ma_undercut_depth":
            ma = _gm(comp["ma"])
            if ma is None: return None
            p = comp["period"]; n = _gn(comp["normalizer"])
            diff = L - ma
            mac = closed_im.get(comp["ma"])
            if mac is None: return None
            dc = closed_im["low"] - mac
            return _safe_div(_partial_rolling_min(dc, lci, p, diff), n)

        elif op == "channel_slope":
            p = comp["period"]
            mh = im_p.get(f"maxh{p}", _partial_rolling_max(closed_im["high"], lci, p, H))
            return _safe_div(mh - _sp(f"maxh{p}", p), _gn(comp["normalizer"]))

        elif op == "retrace_high":
            p = comp["period"]
            mh = im_p.get(f"maxh{p}", _partial_rolling_max(closed_im["high"], lci, p, H))
            ml = im_p.get(f"minl{p}", _partial_rolling_min(closed_im["low"], lci, p, L))
            return _safe_div(H - ml, mh - ml)

        elif op == "retrace_low":
            p = comp["period"]
            mh = im_p.get(f"maxh{p}", _partial_rolling_max(closed_im["high"], lci, p, H))
            ml = im_p.get(f"minl{p}", _partial_rolling_min(closed_im["low"], lci, p, L))
            return _safe_div(L - ml, mh - ml)

        else:
            return None

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# 6. BOOLEAN AGGREGATES FOR PARTIAL CANDLES
# ══════════════════════════════════════════════════════════════

def compute_partial_booleans(engine_closed, im_partial, closed_im, lci,
                             tf_base_computes, htf_ct, htf_st, htf_tir,
                             n_daily, data):
    """Boolean aggregates with partial candle awareness."""
    n_htf = len(closed_im['close'])
    bool_cache = {}

    def _get_cond(cond):
        if cond in bool_cache:
            return bool_cache[cond]
        try:
            b = engine_closed._bool_series(cond).values.astype(bool)
        except Exception:
            b = np.zeros(n_htf, dtype=bool)
        cs = np.cumsum(b.astype(np.float64))
        bs = np.full(n_htf, n_htf, dtype=np.float64)
        for i in range(n_htf):
            if b[i]: bs[i] = 0.0
            elif i > 0: bs[i] = bs[i - 1] + 1.0
        ct = np.zeros(n_htf, dtype=np.float64)
        for i in range(n_htf):
            if b[i]: ct[i] = (ct[i - 1] if i > 0 else 0) + 1.0
        bool_cache[cond] = (b, cs, bs, ct)
        return bool_cache[cond]

    def _eval_partial(cond):
        """Evaluate bool at partial bar. Uses last closed bar's value as proxy."""
        b, _, _, _ = _get_cond(cond)
        result = np.full(n_daily, False, dtype=bool)
        v = lci >= 0
        result[v] = b[np.minimum(lci[v], n_htf - 1)]
        return result

    # count_true
    for k, j in htf_ct:
        cond = tf_base_computes[k]["condition"]
        period = tf_base_computes[k]["period"]
        _, cs, _, _ = _get_cond(cond)
        bp = _eval_partial(cond)
        result = np.full(n_daily, np.nan, dtype=np.float32)
        valid = lci >= period - 2
        if np.any(valid):
            vi = np.where(valid)[0]
            end = lci[vi]; start = end - (period - 1)
            wc = np.where(start >= 0, cs[end] - cs[start], cs[end])
            has = (end - np.maximum(start, -1)) >= period - 1
            wc = np.where(has, wc, np.nan)
            result[vi] = (wc + bp[vi].astype(np.float64)).astype(np.float32)
        data[:, j] = result

    # since_true
    for k, j in htf_st:
        cond = tf_base_computes[k]["condition"]
        period = tf_base_computes[k]["period"]
        _, _, bs, _ = _get_cond(cond)
        bp = _eval_partial(cond)
        result = np.full(n_daily, np.nan, dtype=np.float32)
        valid = lci >= period - 2
        if np.any(valid):
            vi = np.where(valid)[0]
            for idx in vi:
                k_lci = lci[idx]
                if bp[idx]:
                    result[idx] = 0.0
                else:
                    bars = bs[k_lci] + 1.0
                    result[idx] = bars if bars < period else -1.0
        data[:, j] = result

    # true_in_row
    for k, j in htf_tir:
        cond = tf_base_computes[k]["condition"]
        period = tf_base_computes[k]["period"]
        _, _, _, ct = _get_cond(cond)
        bp = _eval_partial(cond)
        result = np.full(n_daily, np.nan, dtype=np.float32)
        valid = lci >= period - 2
        if np.any(valid):
            vi = np.where(valid)[0]
            for idx in vi:
                k_lci = lci[idx]
                if bp[idx]:
                    consec = ct[k_lci] + 1.0 if k_lci >= 0 else 1.0
                else:
                    consec = 0.0
                result[idx] = min(consec, period)
        data[:, j] = result


# ══════════════════════════════════════════════════════════════
# 7. EXTENSION STRUCTURE FOR PARTIAL CANDLES
# ══════════════════════════════════════════════════════════════

def compute_partial_ext_struct(closed_im, closed_raw, im_partial, lci,
                               htf_ext, tf_base_computes, n_daily, data):
    """Extension structure with partial candle awareness."""
    import json as _json
    from scripts.backtest_conditions import compute_on_series

    LINREG_OPS = {"trendline_deviation", "channel_position"}

    # Build partial + closed extension series
    ext_partial = {}
    ext_closed = {}
    for sname, ma_name, norm_name in [
        ("ext_avgc50_adr14", "avgc50", "adr14"),
        ("ext_avgc200_adr14", "avgc200", "adr14"),
    ]:
        ma_p = im_partial.get(ma_name)
        norm_p = im_partial.get(norm_name)
        ma_c = closed_im.get(ma_name)
        norm_c = closed_im.get(norm_name)
        if ma_p is not None and norm_p is not None:
            ext_partial[sname] = _safe_div(im_partial['close'] - ma_p, norm_p)
        if ma_c is not None and norm_c is not None:
            ext_closed[sname] = _safe_div(closed_im['close'] - ma_c, norm_c)

    if not ext_partial:
        return

    # Classify
    ext_linreg = []; ext_bool_agg = []; ext_other = []
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

    # ── Linreg ──
    for k, j in ext_linreg:
        comp = tf_base_computes[k]
        sn = comp.get("series", "")
        if sn not in ext_partial or sn not in ext_closed:
            continue
        try:
            lb = comp["inner_op"]["lookback"]
            is_td = comp["inner_op"]["op"] == "trendline_deviation"
            ec = ext_closed[sn]; ep = ext_partial[sn]

            result = np.full(n_daily, np.nan, dtype=np.float32)
            valid = lci >= lb - 2
            if np.any(valid):
                vi = np.where(valid)[0]
                for idx in vi:
                    k_lci = lci[idx]
                    sk = k_lci - lb + 2
                    if sk < 0: continue
                    window = np.empty(lb, dtype=np.float64)
                    window[:lb-1] = ec[sk:k_lci+1]
                    window[lb-1] = ep[idx]
                    if np.any(np.isnan(window)): continue
                    x = np.arange(lb, dtype=np.float64)
                    xm = (lb - 1) / 2.0
                    ym = np.mean(window)
                    slope = np.sum((x - xm) * (window - ym)) / np.sum((x - xm) ** 2)
                    pred = ym + slope * (x[-1] - xm)
                    res = window - (ym + slope * (x - xm))
                    std_r = np.std(res)
                    if std_r != 0:
                        result[idx] = (window[-1] - pred) / std_r
                    else:
                        result[idx] = 0.0
            data[:, j] = result
        except Exception:
            pass

    # ── Bool agg ──
    n_htf = len(closed_im['close'])
    htf_ind_bool_cache = {}
    for k, j in ext_bool_agg:
        comp = tf_base_computes[k]
        sn = comp.get("series", "")
        ck = (sn, _json.dumps(comp["bool_op"], sort_keys=True))
        if ck in htf_ind_bool_cache: continue
        if sn not in ext_closed:
            htf_ind_bool_cache[ck] = None; continue
        try:
            sd = ext_closed[sn]
            indicator = compute_on_series(sd, comp["bool_op"])
            threshold = comp["bool_op"].get("threshold", 0)
            direction = comp["bool_op"].get("direction", "gt")
            if direction == "gt": b = indicator > threshold
            elif direction == "lt": b = indicator < threshold
            elif direction == "positive": b = indicator > 0
            elif direction == "negative": b = indicator < 0
            else: b = indicator > threshold
            b[np.isnan(indicator)] = False
            htf_ind_bool_cache[ck] = b.astype(bool)
        except Exception:
            htf_ind_bool_cache[ck] = None

    htf_ba_bs = {}
    for k, j in ext_bool_agg:
        comp = tf_base_computes[k]
        if comp["agg_op"] != "since_true": continue
        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
        if ck in htf_ba_bs or htf_ind_bool_cache.get(ck) is None: continue
        b = htf_ind_bool_cache[ck]
        bs = np.full(n_htf, n_htf, dtype=np.float64)
        for i in range(n_htf):
            if b[i]: bs[i] = 0.0
            elif i > 0: bs[i] = bs[i - 1] + 1.0
        htf_ba_bs[ck] = bs

    for k, j in ext_bool_agg:
        comp = tf_base_computes[k]
        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
        bc = htf_ind_bool_cache.get(ck)
        if bc is None: continue
        ap = comp["agg_period"]

        # Partial bar boolean: use last closed bar's value
        bp = np.full(n_daily, False, dtype=bool)
        v = lci >= 0
        if np.any(v):
            bp[v] = bc[np.minimum(lci[v], n_htf - 1)]

        if comp["agg_op"] == "count_true":
            cs = np.cumsum(bc.astype(np.float64))
            result = np.full(n_daily, np.nan, dtype=np.float32)
            vld = lci >= ap - 2
            if np.any(vld):
                vi = np.where(vld)[0]
                end = lci[vi]; start = end - (ap - 1)
                wc = np.where(start >= 0, cs[end] - cs[start], cs[end])
                has = (end - np.maximum(start, -1)) >= ap - 1
                wc = np.where(has, wc, np.nan)
                result[vi] = (wc + bp[vi].astype(np.float64)).astype(np.float32)
            data[:, j] = result

        elif comp["agg_op"] == "since_true":
            bs = htf_ba_bs.get(ck)
            if bs is None: continue
            result = np.full(n_daily, np.nan, dtype=np.float32)
            vld = lci >= ap - 2
            if np.any(vld):
                for idx in np.where(vld)[0]:
                    if bp[idx]: result[idx] = 0.0
                    else:
                        bars = bs[lci[idx]] + 1.0
                        result[idx] = bars if bars < ap else -1.0
            data[:, j] = result

        elif comp["agg_op"] == "true_in_row":
            ct = np.zeros(n_htf, dtype=np.float64)
            for i in range(n_htf):
                if bc[i]: ct[i] = (ct[i-1] if i > 0 else 0) + 1.0
            result = np.full(n_daily, np.nan, dtype=np.float32)
            vld = lci >= ap - 2
            if np.any(vld):
                for idx in np.where(vld)[0]:
                    if bp[idx]: consec = ct[lci[idx]] + 1.0
                    else: consec = 0.0
                    result[idx] = min(consec, ap)
            data[:, j] = result


# ══════════════════════════════════════════════════════════════
# 8. MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════

def compute_htf_partial(daily_df, htf_df, freq, tf_indices, tf_base_computes,
                        n_bars, data, expressions):
    """Compute HTF expression values with partial candle reconstruction.

    Replaces section 3 of _compute_ticker_full. For each daily bar,
    HTF values reflect only data available on that day.
    """
    if htf_df is None or len(htf_df) < 5:
        return

    from scripts.expression_engine import ExpressionEngine
    from scripts.backtest_conditions import compute_series
    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    # 1. Build period mapping and partial candles
    lci, partial, prev_close = build_partial_candle_mapping(daily_df, htf_df, freq)

    # 2. Build engine on closed HTF series + extract state
    engine_closed = ExpressionEngine(htf_df)
    closed_im, closed_raw = extract_closed_state(engine_closed, htf_df)

    # 3. Build daily-resolution partial intermediates
    im_partial = build_partial_intermediates(closed_im, closed_raw, lci, partial, prev_close)

    # 4. Classify HTF expressions
    htf_ct = []; htf_st = []; htf_tir = []; htf_ext = []; htf_arith = []
    for k, j in enumerate(tf_indices):
        base_op = tf_base_computes[k].get("op", "")
        if base_op == "count_true": htf_ct.append((k, j))
        elif base_op == "since_true": htf_st.append((k, j))
        elif base_op == "true_in_row": htf_tir.append((k, j))
        elif base_op in _ON_SERIES_OPS: htf_ext.append((k, j))
        else: htf_arith.append((k, j))

    # 5. Dispatch arith expressions
    for k, j in htf_arith:
        comp = tf_base_computes[k]
        try:
            result = dispatch_partial_arith(comp, im_partial, closed_im, closed_raw, lci)
            if result is not None:
                data[:, j] = result.astype(np.float32)
            else:
                # Fallback to closed-candle mapping for unhandled ops
                s = compute_series(engine_closed, comp)
                if s is not None:
                    from local_runner.expr_cache_builder import (
                        build_htf_to_daily_map, map_htf_series_to_daily)
                    htf_map = build_htf_to_daily_map(daily_df['date'], htf_df, freq)
                    data[:, j] = map_htf_series_to_daily(
                        np.asarray(s, dtype=np.float32), htf_map)
        except Exception:
            try:
                s = compute_series(engine_closed, comp)
                if s is not None:
                    from local_runner.expr_cache_builder import (
                        build_htf_to_daily_map, map_htf_series_to_daily)
                    htf_map = build_htf_to_daily_map(daily_df['date'], htf_df, freq)
                    data[:, j] = map_htf_series_to_daily(
                        np.asarray(s, dtype=np.float32), htf_map)
            except Exception:
                pass

    # 6. Booleans
    compute_partial_booleans(engine_closed, im_partial, closed_im, lci,
                             tf_base_computes, htf_ct, htf_st, htf_tir,
                             n_daily=n_bars, data=data)

    # 7. Extension structure
    if htf_ext:
        compute_partial_ext_struct(closed_im, closed_raw, im_partial, lci,
                                   htf_ext, tf_base_computes,
                                   n_daily=n_bars, data=data)
