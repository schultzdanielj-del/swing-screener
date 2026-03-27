"""
Vectorized 2D Daily Expression Dispatcher.

Takes precomputed 2D intermediates (MAs, ATR, ADR, RSI, etc.) and computes
all ~1,604 daily expressions as 2D arrays (n_tickers × n_bars).

Usage:
    intermediates = build_intermediates(O, H, L, C, V)
    result = compute_expr_2d(comp, intermediates, O, H, L, C, V)
"""

import numpy as np
from local_runner.vectorized_indicators import (
    sma_2d, ema_2d, hma_2d, rolling_max_2d, rolling_min_2d, rolling_sum_2d,
    rolling_std_2d, true_range_2d, atr_2d, adr_2d, rsi_2d, stochastic_2d,
    adx_2d, di_plus_2d, di_minus_2d, cci_2d, macd_2d,
    bollinger_top_2d, bollinger_bot_2d, obv_2d, bop_2d,
    aroon_up_2d, aroon_down_2d, cmf_2d, kaufman_eff_2d,
)


def _safe_div(a, b):
    """a / b, returning NaN where b == 0 or either is NaN."""
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(b != 0, a / b, np.nan)
    result[np.isnan(a) | np.isnan(b)] = np.nan
    return result


def _shift(arr, n):
    """Shift 2D array right by n bars (axis=1). Fill with NaN."""
    result = np.full_like(arr, np.nan)
    if n > 0 and n < arr.shape[1]:
        result[:, n:] = arr[:, :-n]
    return result


def build_intermediates(O, H, L, C, V):
    """Precompute all unique intermediates needed by daily expressions.
    
    Returns dict of name → 2D array.
    """
    im = {}
    
    # MAs on close
    sma_periods = [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]
    ema_periods = [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200]
    
    for p in sma_periods:
        im[f"avgc{p}"] = sma_2d(C, p)
    for p in ema_periods:
        im[f"xavgc{p}"] = ema_2d(C, p)
    
    # MAs on volume
    for p in [10, 20, 50]:
        im[f"avgv{p}"] = sma_2d(V, p)
    
    # ATR, ADR
    im["atr14"] = atr_2d(H, L, C, 14)
    im["adr14"] = adr_2d(H, L, 14)
    
    # Normalizers (pct = C / 100)
    im["norm_atr14"] = im["atr14"]
    im["norm_adr14"] = im["adr14"]
    im["norm_pct"] = C / 100.0
    
    # Rolling max of high
    maxh_periods = sorted(set(
        list(range(5, 125, 5)) + [2, 3, 7, 10, 15, 63, 65, 126]
    ))
    for p in maxh_periods:
        im[f"maxh{p}"] = rolling_max_2d(H, p)
    
    # Rolling min of low
    minl_periods = sorted(set(
        list(range(5, 65, 5)) + [2, 3, 7, 65, 90, 120, 126]
    ))
    for p in minl_periods:
        im[f"minl{p}"] = rolling_min_2d(L, p)
    
    # RSI
    for p in [5, 7, 9, 14, 21, 28]:
        im[f"rsi{p}"] = rsi_2d(C, p)
    
    # ADX, DI+, DI-
    for p in [7, 10, 14, 20]:
        im[f"adx{p}"] = adx_2d(H, L, C, p)
        im[f"diplus{p}"] = di_plus_2d(H, L, C, p)
        im[f"diminus{p}"] = di_minus_2d(H, L, C, p)
    
    # Stochastic
    for p in [3, 5, 7, 9, 10, 14, 21, 28, 50]:
        im[f"stoch{p}"] = stochastic_2d(H, L, C, p)
    
    # CCI
    for p in [5, 7, 10, 14, 20, 30, 50]:
        im[f"cci{p}"] = cci_2d(H, L, C, p)
    
    # BOP
    for p in [5, 10, 14, 20]:
        im[f"bop{p}"] = bop_2d(O, H, L, C, p)
    
    # OBV
    im["obv"] = obv_2d(C, V)
    
    # MACD
    for fast, slow in [(12, 26), (8, 17), (5, 35), (5, 13), (6, 19)]:
        im[f"macd_{fast}_{slow}"] = macd_2d(C, fast, slow)
    
    # Bollinger
    for p in [5, 10, 15, 20, 30, 50]:
        im[f"bbtop_{p}"] = bollinger_top_2d(C, p)
        im[f"bbbot_{p}"] = bollinger_bot_2d(C, p)
        im[f"stddev_{p}"] = rolling_std_2d(C, p)
    
    # Aroon
    for p in [7, 10, 14, 20, 25, 50, 100]:
        im[f"aroon_up_{p}"] = aroon_up_2d(H, p)
        im[f"aroon_down_{p}"] = aroon_down_2d(L, p)
    
    # CMF
    for p in [10, 14, 20, 30, 50]:
        im[f"cmf_{p}"] = cmf_2d(H, L, C, V, p)
    
    # Kaufman efficiency
    for p in [5, 7, 10, 15, 20, 30, 50, 65, 100]:
        im[f"kauf_eff_{p}"] = kaufman_eff_2d(C, p)
    
    # Rolling max of close (for some breakout booleans)
    for p in [10]:
        im[f"maxc{p}"] = rolling_max_2d(C, p)
    
    return im


def _get_ma(im, name):
    """Get MA from intermediates by name like 'avgc50', 'xavgc21'."""
    return im[name]


def _get_norm(im, name):
    """Get normalizer from intermediates."""
    if name == "atr14":
        return im["norm_atr14"]
    elif name == "adr14":
        return im["norm_adr14"]
    elif name == "pct":
        return im["norm_pct"]
    elif name == "close":
        # Some ops use "close" as normalizer
        return im.get("_close")
    return im["norm_atr14"]


def compute_expr_2d(comp, im, O, H, L, C, V):
    """Compute one expression across all tickers and bars.
    
    comp: expression compute spec dict (from brute_expressions)
    im: precomputed intermediates dict
    O, H, L, C, V: 2D OHLCV arrays (n_tickers, n_bars)
    
    Returns: 2D array (n_tickers, n_bars)
    """
    op = comp["op"]
    
    if op == "ma_slope":
        ma = _get_ma(im, comp["ma"])
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(ma - _shift(ma, comp["offset"]), norm)
    
    elif op == "ma_spread":
        fast = _get_ma(im, comp["ma_fast"])
        slow = _get_ma(im, comp["ma_slow"])
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(fast - slow, norm)
    
    elif op == "extension":
        ma = _get_ma(im, comp["ma"])
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(C - ma, norm)
    
    elif op == "low_vs_ma":
        ma = _get_ma(im, comp["ma"])
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(L - ma, norm)
    
    elif op == "high_vs_ma":
        ma = _get_ma(im, comp["ma"])
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(H - ma, norm)
    
    elif op == "distance_to_maxh":
        price = C if comp["price_ref"] == "C" else H
        maxh = _shift(im[f"maxh{comp['maxh_period']}"], 1)
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(maxh - price, norm)
    
    elif op == "ratio_c_maxh":
        maxh = _shift(im[f"maxh{comp['maxh_period']}"], 1)
        return _safe_div(C, maxh)
    
    elif op == "distance_to_minl":
        price = C if comp["price_ref"] == "C" else L
        minl = _shift(im[f"minl{comp['minl_period']}"], 1)
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(price - minl, norm)
    
    elif op == "ratio_c_minl":
        minl = _shift(im[f"minl{comp['minl_period']}"], 1)
        return _safe_div(C, minl)
    
    elif op == "pullback":
        maxh = im[f"maxh{comp['period']}"]
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(maxh - C, norm)
    
    elif op == "range_position":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        minl = im[f"minl{p}"] if f"minl{p}" in im else rolling_min_2d(L, p)
        rng = maxh - minl
        return _safe_div(C - minl, rng)
    
    elif op == "range_width":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        minl = im[f"minl{p}"] if f"minl{p}" in im else rolling_min_2d(L, p)
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(maxh - minl, norm)
    
    elif op == "roc":
        p = comp["period"]
        shifted = _shift(C, p)
        return _safe_div(C, shifted) * 100.0 - 100.0
    
    elif op == "roc_delta":
        p = comp["period"]
        co = comp["compare_offset"]
        roc_now = _safe_div(C, _shift(C, p)) - 1.0
        c_co = _shift(C, co)
        c_cop = _shift(C, co + p)
        roc_prev = _safe_div(c_co, c_cop) - 1.0
        return 100.0 * (roc_now - roc_prev)
    
    elif op == "roc_acceleration":
        outer = comp["outer_period"]
        inner = comp["inner_period"]
        roc_s = (_safe_div(C, _shift(C, outer)) - 1.0) * 100.0
        return roc_s - _shift(roc_s, inner)
    
    elif op == "roc_percentile_rank":
        roc_p = comp["roc_period"]
        lb = comp["lookback"]
        roc_s = (_safe_div(C, _shift(C, roc_p)) - 1.0) * 100.0
        return _rolling_rank_2d(roc_s, lb)
    
    elif op == "rsi":
        return im[f"rsi{comp['period']}"]
    
    elif op == "rsi_slope":
        r = im[f"rsi{comp['period']}"]
        return r - _shift(r, comp["offset"])
    
    elif op == "adx":
        return im[f"adx{comp['period']}"]
    
    elif op == "adx_slope":
        a = im[f"adx{comp['period']}"]
        return a - _shift(a, comp["offset"])
    
    elif op == "di_spread":
        p = comp["period"]
        return im[f"diplus{p}"] - im[f"diminus{p}"]
    
    elif op == "stochastic":
        return im[f"stoch{comp['period']}"]
    
    elif op == "cci":
        return im[f"cci{comp['period']}"]
    
    elif op == "volume_ratio":
        p = comp['avg_period']
        avg = im.get(f"avgv{p}", sma_2d(V, p))
        return _safe_div(V, avg)
    
    elif op == "extension_slope":
        ma = _get_ma(im, comp["ma"])
        norm = _get_norm(im, comp["normalizer"])
        ext = C - ma
        return _safe_div(ext - _shift(ext, comp["offset"]), norm)
    
    elif op == "extension_peak_ratio":
        ma = _get_ma(im, comp["ma"])
        lb = comp["lookback"]
        ext = C - ma
        max_ext = _rolling_max_1sided(ext, lb)
        return _safe_div(ext, max_ext)
    
    elif op == "extension_ceiling_ratio":
        ma = _get_ma(im, comp["ma"])
        lb = comp["lookback"]
        norm = _get_norm(im, comp["normalizer"])
        ext_norm = _safe_div(C - ma, norm)
        max_ext = _rolling_max_1sided(ext_norm, lb)
        return _safe_div(ext_norm, max_ext)
    
    elif op == "ext_adr_multiples":
        ma = _get_ma(im, comp["ma"])
        adr = im["adr14"]
        return _safe_div(C - ma, adr)
    
    elif op == "spread_slope":
        fast = _get_ma(im, comp["ma_fast"])
        slow = _get_ma(im, comp["ma_slow"])
        norm = _get_norm(im, comp["normalizer"])
        spread = _safe_div(fast - slow, norm)
        return spread - _shift(spread, comp["offset"])
    
    elif op == "slope_ratio":
        fast_ma = _get_ma(im, comp["fast_ma"])
        slow_ma = _get_ma(im, comp["slow_ma"])
        offset = comp["offset"]
        fast_slope = fast_ma - _shift(fast_ma, offset)
        slow_slope = slow_ma - _shift(slow_ma, offset)
        return _safe_div(fast_slope, slow_slope)
    
    elif op == "ma_cross_count":
        if "ma_fast" in comp:
            fast = _get_ma(im, comp["ma_fast"])
            slow = _get_ma(im, comp["ma_slow"])
        else:
            # Single MA: cross = close crossing above/below MA
            fast = C
            slow = _get_ma(im, comp["ma"])
        above = (fast > slow).astype(np.float64)
        cross = np.abs(above - _shift(above, 1))
        cross[:, 0] = np.nan
        return _rolling_sum_minperiods1(cross, comp["period"])
    
    elif op == "bars_since_ma_cross":
        ma = _get_ma(im, comp["ma"])
        max_lb = comp.get("max_lookback", 120)
        above = C > ma
        # Sequential scan
        n_t, n_b = C.shape
        result = np.full((n_t, n_b), float(max_lb))
        for j in range(1, n_b):
            for back in range(1, min(max_lb, j + 1)):
                changed = above[:, j] != above[:, j - back]
                not_yet_found = result[:, j] == float(max_lb)
                found_now = changed & not_yet_found
                result[found_now, j] = float(back)
        return result
    
    elif op == "ma_undercut_depth":
        ma = _get_ma(im, comp["ma"])
        p = comp["period"]
        norm = _get_norm(im, comp["normalizer"])
        diff = L - ma
        min_diff = _rolling_min_minperiods1(diff, p)
        return _safe_div(min_diff, norm)
    
    elif op == "ma_stack_score":
        mas_list = comp["mas"]
        mas = [_get_ma(im, name) for name in mas_list]
        n_mas = len(mas)
        score = np.zeros_like(C)
        for a in range(n_mas):
            for b in range(a + 1, n_mas):
                score = score + (mas[a] > mas[b]).astype(np.float64)
        return score
    
    elif op == "channel_slope":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(maxh - _shift(maxh, p), norm)
    
    elif op == "candle_range_ratio":
        rng = H - L
        return _safe_div(rng, im["atr14"])
    
    elif op == "body_range_ratio":
        rng = H - L
        body = np.abs(C - O)
        return _safe_div(body, rng)
    
    elif op == "upper_wick_ratio":
        rng = H - L
        upper = H - np.maximum(C, O)
        return _safe_div(upper, rng)
    
    elif op == "lower_wick_ratio":
        rng = H - L
        lower = np.minimum(C, O) - L
        return _safe_div(lower, rng)
    
    elif op == "close_vs_open_ratio":
        p = comp["period"]
        bullish = (C > O).astype(np.float64)
        return _safe_div(_rolling_sum_minperiods1(bullish, p), float(p))
    
    elif op == "avg_candle_body_ratio":
        p = comp["period"]
        rng = H - L
        body = np.abs(C - O)
        ratio = _safe_div(body, rng)
        return _rolling_mean_minperiods1(ratio, p)
    
    elif op == "close_position_in_bar":
        p = comp["period"]
        rng = H - L
        pos = _safe_div(C - L, rng)
        return sma_2d(pos, p)
    
    elif op == "inside_bar_count":
        p = comp["period"]
        is_inside = ((H < _shift(H, 1)) & (L > _shift(L, 1))).astype(np.float64)
        return _rolling_sum_minperiods1(is_inside, p)
    
    elif op == "outside_bar_count":
        p = comp["period"]
        is_outside = ((H > _shift(H, 1)) & (L < _shift(L, 1))).astype(np.float64)
        return _rolling_sum_minperiods1(is_outside, p)
    
    elif op == "nr_ratio":
        p = comp["period"]
        today_range = H - L
        max_range = _rolling_max_minperiods1(today_range, p)
        return _safe_div(today_range, max_range)
    
    elif op == "gap_size":
        norm = _get_norm(im, comp["normalizer"])
        gap = np.full_like(C, np.nan)
        gap[:, 1:] = O[:, 1:] - C[:, :-1]
        return _safe_div(gap, norm)
    
    elif op == "gap_count":
        p = comp["period"]
        threshold = comp.get("threshold", 0.5)
        atr_s = im["atr14"]
        gaps = np.full_like(C, 0.0)
        gaps[:, 1:] = np.abs(O[:, 1:] - C[:, :-1])
        gap_atr = _safe_div(gaps, atr_s)
        # bar 0: gap=0, gap_atr may be NaN (atr not ready), treat as no gap
        gap_atr_safe = np.nan_to_num(gap_atr, nan=0.0)
        is_gap = (gap_atr_safe > threshold).astype(np.float64)
        # Use manual loop to match pandas exactly
        n_t, n_b = C.shape
        result = np.full((n_t, n_b), np.nan)
        for i in range(p - 1, n_b):
            result[:, i] = np.sum(is_gap[:, i - p + 1:i + 1], axis=1)
        return result
    
    elif op == "unfilled_gap_up_count":
        # This is expensive — sequential per-bar. Keep as loop.
        p = comp["period"]
        n_t, n_b = C.shape
        result = np.full((n_t, n_b), np.nan)
        for i in range(p, n_b):
            count = np.zeros(n_t)
            for j_off in range(p):
                j = i - p + 1 + j_off
                if j < 1:
                    continue
                gap_up = O[:, j] > H[:, j - 1]
                filled = np.zeros(n_t, dtype=bool)
                for k in range(j, i + 1):
                    filled |= L[:, k] <= H[:, j - 1]
                count += (gap_up & ~filled).astype(np.float64)
            result[:, i] = count
        return result
    
    elif op == "retracement_level":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        minl = im[f"minl{p}"] if f"minl{p}" in im else rolling_min_2d(L, p)
        rng = maxh - minl
        return _safe_div(C - minl, rng)
    
    elif op == "retrace_high":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        minl = im[f"minl{p}"] if f"minl{p}" in im else rolling_min_2d(L, p)
        rng = maxh - minl
        return _safe_div(H - minl, rng)
    
    elif op == "retrace_low":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        minl = im[f"minl{p}"] if f"minl{p}" in im else rolling_min_2d(L, p)
        rng = maxh - minl
        return _safe_div(L - minl, rng)
    
    elif op == "range_contraction_ratio":
        p = comp["period"]
        maxh = im[f"maxh{p}"] if f"maxh{p}" in im else rolling_max_2d(H, p)
        minl = im[f"minl{p}"] if f"minl{p}" in im else rolling_min_2d(L, p)
        curr_width = maxh - minl
        prev_width = _shift(curr_width, p)
        return _safe_div(curr_width, prev_width)
    
    elif op == "atr_ratio":
        a = im[f"atr14"] if comp.get("period", 14) == 14 else atr_2d(H, L, C, comp["period"])
        offset = comp["offset"]
        return _safe_div(a, _shift(a, offset))
    
    elif op == "bop":
        return im[f"bop{comp['period']}"]
    
    elif op == "obv_slope":
        o = im["obv"]
        offset = comp["offset"]
        vol_period = comp.get("vol_period", 20)
        avg_vol = im.get(f"avgv{vol_period}", sma_2d(V, vol_period))
        return _safe_div(o - _shift(o, offset), avg_vol * offset)
    
    elif op == "macd_histogram":
        fast = comp.get("fast", 12)
        slow = comp.get("slow", 26)
        signal_p = comp.get("signal", 9)
        macd_line = im.get(f"macd_{fast}_{slow}", macd_2d(C, fast, slow))
        signal_line = ema_2d(macd_line, signal_p)
        return macd_line - signal_line
    
    elif op == "macd_histogram_slope":
        fast = comp.get("fast", 12)
        slow = comp.get("slow", 26)
        signal_p = comp.get("signal", 9)
        offset = comp["offset"]
        macd_line = im.get(f"macd_{fast}_{slow}", macd_2d(C, fast, slow))
        signal_line = ema_2d(macd_line, signal_p)
        hist = macd_line - signal_line
        return hist - _shift(hist, offset)
    
    elif op == "macd_line_norm":
        fast = comp.get("fast", 12)
        slow = comp.get("slow", 26)
        macd_line = im.get(f"macd_{fast}_{slow}", macd_2d(C, fast, slow))
        norm = _get_norm(im, comp["normalizer"])
        return _safe_div(macd_line, norm)
    
    elif op == "cmf":
        return im[f"cmf_{comp['period']}"]
    
    elif op == "cmf_slope":
        c = im[f"cmf_{comp['period']}"]
        return c - _shift(c, comp["offset"])
    
    elif op == "kaufman_efficiency_ratio":
        return im[f"kauf_eff_{comp['period']}"]
    
    elif op == "bollinger_pctb":
        p = comp["period"]
        top = im.get(f"bbtop_{p}", bollinger_top_2d(C, p))
        bot = im.get(f"bbbot_{p}", bollinger_bot_2d(C, p))
        bw = top - bot
        return _safe_div(C - bot, bw)
    
    elif op == "bollinger_bandwidth":
        p = comp["period"]
        top = im.get(f"bbtop_{p}", bollinger_top_2d(C, p))
        bot = im.get(f"bbbot_{p}", bollinger_bot_2d(C, p))
        mid = im.get(f"avgc{p}", sma_2d(C, p))
        return _safe_div(top - bot, mid)
    
    elif op == "bollinger_bandwidth_rank":
        p = comp["period"]
        lb = comp["lookback"]
        top = im.get(f"bbtop_{p}", bollinger_top_2d(C, p))
        bot = im.get(f"bbbot_{p}", bollinger_bot_2d(C, p))
        mid = im.get(f"avgc{p}", sma_2d(C, p))
        bw = _safe_div(top - bot, mid)
        bw_min = _rolling_min_minperiods1(bw, lb)
        bw_max = _rolling_max_minperiods1(bw, lb)
        bw_range = bw_max - bw_min
        return _safe_div(bw - bw_min, bw_range)
    
    elif op == "vwap_distance":
        p = comp["period"]
        norm = _get_norm(im, comp["normalizer"])
        tp = (H + L + C) / 3.0
        cum_tpv = _rolling_sum_minperiods1(tp * V, p)
        cum_vol = _rolling_sum_minperiods1(V, p)
        vwap_s = _safe_div(cum_tpv, cum_vol)
        return _safe_div(C - vwap_s, norm)
    
    elif op == "vwap_slope":
        p = comp["period"]
        offset = comp["offset"]
        norm = _get_norm(im, comp["normalizer"])
        tp = (H + L + C) / 3.0
        cum_tpv = _rolling_sum_minperiods1(tp * V, p)
        cum_vol = _rolling_sum_minperiods1(V, p)
        vwap_s = _safe_div(cum_tpv, cum_vol)
        return _safe_div(vwap_s - _shift(vwap_s, offset), norm)
    
    elif op == "percentile_rank":
        source = comp["source"]
        period = comp["period"]
        if source == "close":
            s = C
        elif source == "volume":
            s = V
        elif source == "range":
            s = H - L
        elif source == "atr14":
            s = im["atr14"]
        elif source == "rsi14":
            s = im["rsi14"]
        else:
            s = C
        return _rolling_pct_rank_2d(s, period, min_periods=2)
    
    elif op == "rvol_continuous":
        period = comp["period"]
        avg_period = comp["avg_period"]
        avg_vol = _sma_half_minperiods(V, avg_period)
        rvol = _safe_div(V, avg_vol)
        return _rolling_mean_minperiods1(rvol, period)
    
    elif op == "cumulative_rvol":
        period = comp["period"]
        avg_period = comp["avg_period"]
        avg_vol = _sma_half_minperiods(V, avg_period)
        rvol = _safe_div(V, avg_vol)
        return _rolling_sum_minperiods1(rvol, period)
    
    elif op == "high_volume_bar_pct":
        p = comp["period"]
        mult = comp.get("multiplier", 1.5)
        avg_period = comp.get("avg_period", 50)
        avg_v = im.get(f"avgv{avg_period}", sma_2d(V, avg_period))
        is_high = (V > mult * avg_v).astype(np.float64)
        return _safe_div(_rolling_sum_minperiods1(is_high, p), float(p))
    
    elif op == "up_volume_ratio":
        p = comp["period"]
        up_mask = (C > O).astype(np.float64)
        up_vol = _rolling_sum_minperiods1(V * up_mask, p)
        total_vol = _rolling_sum_minperiods1(V, p)
        return _safe_div(up_vol, total_vol)
    
    elif op == "volume_price_divergence":
        p = comp["period"]
        price_roc = _safe_div(C, _shift(C, p)) - 1.0
        vol_avg = sma_2d(V, p)
        vol_roc = _safe_div(vol_avg, _shift(vol_avg, p)) - 1.0
        return price_roc - vol_roc
    
    elif op == "aroon_oscillator":
        p = comp["period"]
        return im.get(f"aroon_up_{p}", aroon_up_2d(H, p)) - im.get(f"aroon_down_{p}", aroon_down_2d(L, p))
    
    elif op == "aroon_up_val":
        return im.get(f"aroon_up_{comp['period']}", aroon_up_2d(H, comp["period"]))
    
    elif op == "aroon_down_val":
        return im.get(f"aroon_down_{comp['period']}", aroon_down_2d(L, comp["period"]))
    
    elif op == "consecutive_up_days":
        n_t, n_b = C.shape
        result = np.zeros((n_t, n_b))
        for j in range(1, n_b):
            up = C[:, j] > C[:, j - 1]
            result[:, j] = np.where(up, result[:, j - 1] + 1, 0)
        return result
    
    elif op == "consecutive_down_days":
        n_t, n_b = C.shape
        result = np.zeros((n_t, n_b))
        for j in range(1, n_b):
            dn = C[:, j] < C[:, j - 1]
            result[:, j] = np.where(dn, result[:, j - 1] + 1, 0)
        return result
    
    elif op == "consecutive_up_roc":
        n_t, n_b = C.shape
        result = np.zeros((n_t, n_b))
        for j in range(1, n_b):
            up = C[:, j] > C[:, j - 1]
            roc = _safe_div(C[:, j], C[:, j - 1]) - 1.0
            result[:, j] = np.where(up, result[:, j - 1] + roc * 100, 0)
        return result
    
    elif op == "consecutive_down_roc":
        n_t, n_b = C.shape
        result = np.zeros((n_t, n_b))
        for j in range(1, n_b):
            dn = C[:, j] < C[:, j - 1]
            roc = _safe_div(C[:, j], C[:, j - 1]) - 1.0
            result[:, j] = np.where(dn, result[:, j - 1] + roc * 100, 0)
        return result
    
    elif op == "swing_high_count" or op == "swing_low_count":
        return _swing_count_2d(H if "high" in op else L, comp["period"], op == "swing_high_count")
    
    elif op in ("higher_high_count", "higher_low_count", "lower_high_count", "lower_low_count"):
        return _trend_swing_count_2d(H, L, comp["period"], op)
    
    else:
        # Unknown op — return NaN
        return np.full_like(C, np.nan)


# ══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════

def _sma_half_minperiods(arr, period):
    """SMA with min_periods = period // 2. Matches pandas rolling(p, min_periods=p//2).mean()."""
    min_p = period // 2
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    valid = ~np.isnan(arr)
    safe = np.where(valid, arr, 0.0)
    cs = np.cumsum(safe, axis=1)
    cv = np.cumsum(valid.astype(np.float64), axis=1)
    for j in range(n_b):
        start = max(0, j - period + 1)
        if start == 0:
            s = cs[:, j]
            c = cv[:, j]
        else:
            s = cs[:, j] - cs[:, start - 1]
            c = cv[:, j] - cv[:, start - 1]
        vals = np.where(c >= min_p, s / c, np.nan)
        result[:, j] = vals
    return result


def _rolling_max_1sided(arr, period):
    """Rolling max with min_periods=1."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    for j in range(n_b):
        start = max(0, j - period + 1)
        window = arr[:, start:j + 1]
        result[:, j] = np.nanmax(window, axis=1)
    return result


def _rolling_min_minperiods1(arr, period):
    """Rolling min with min_periods=1."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    for j in range(n_b):
        start = max(0, j - period + 1)
        window = arr[:, start:j + 1]
        result[:, j] = np.nanmin(window, axis=1)
    return result


def _rolling_max_minperiods1(arr, period):
    """Rolling max with min_periods=1."""
    return _rolling_max_1sided(arr, period)


def _rolling_mean_minperiods1(arr, period):
    """Rolling mean with min_periods=1."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    for j in range(n_b):
        start = max(0, j - period + 1)
        window = arr[:, start:j + 1]
        result[:, j] = np.nanmean(window, axis=1)
    return result


def _rolling_sum_minperiods1(arr, period):
    """Rolling sum with min_periods=1. NaN if no valid values in window."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    valid = ~np.isnan(arr)
    safe = np.where(valid, arr, 0.0)
    cs = np.cumsum(safe, axis=1)
    cv = np.cumsum(valid.astype(np.float64), axis=1)
    for j in range(n_b):
        start = max(0, j - period + 1)
        if start == 0:
            s = cs[:, j]
            c = cv[:, j]
        else:
            s = cs[:, j] - cs[:, start - 1]
            c = cv[:, j] - cv[:, start - 1]
        result[:, j] = np.where(c > 0, s, np.nan)
    return result


def _rolling_pct_rank_2d(arr, period, min_periods=None):
    """Percentile rank: what % of values in window are <= current value.
    
    Matches pandas rolling(period, min_periods).apply(pct_rank, raw=False).
    NaN values in window count toward denominator but not numerator.
    Requires at least min_periods non-NaN values in window.
    """
    if min_periods is None:
        min_periods = period
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    for j in range(min_periods - 1, n_b):
        start = max(0, j - period + 1)
        window = arr[:, start:j + 1]
        current = arr[:, j]
        wlen = window.shape[1]
        current_nan = np.isnan(current)
        # Count non-NaN values in window
        n_valid = np.sum(~np.isnan(window), axis=1)
        # NaN <= x is False in numpy, matching pandas
        count = np.nansum(window <= current[:, np.newaxis], axis=1).astype(np.float64)
        val = count / wlen * 100.0
        val[current_nan] = np.nan
        val[n_valid < min_periods] = np.nan
        result[:, j] = val
    return result


def _rolling_rank_2d(arr, period):
    """Rolling rank (pct=True). Matches pandas .rolling(lb).rank(pct=True)."""
    n_t, n_b = arr.shape
    result = np.full_like(arr, np.nan)
    for j in range(period - 1, n_b):
        window = arr[:, j - period + 1:j + 1]
        current = arr[:, j:j + 1]
        has_nan = np.any(np.isnan(window), axis=1)
        rank = np.sum(window <= current, axis=1).astype(np.float64)
        val = rank / period
        val[has_nan] = np.nan
        result[:, j] = val
    return result


def _swing_count_2d(price, period, is_high):
    """Count swing highs or lows in period window."""
    n_t, n_b = price.shape
    result = np.full((n_t, n_b), np.nan)
    for i in range(period + 1, n_b):
        count = np.zeros(n_t)
        for j in range(i - period + 2, i):
            if is_high:
                is_swing = (price[:, j] > price[:, j - 1]) & (price[:, j] > price[:, j + 1])
            else:
                is_swing = (price[:, j] < price[:, j - 1]) & (price[:, j] < price[:, j + 1])
            count += is_swing.astype(np.float64)
        result[:, i] = count
    return result


def _trend_swing_count_2d(H, L, period, op):
    """Count higher highs, higher lows, lower highs, lower lows."""
    n_t, n_b = H.shape
    result = np.full((n_t, n_b), np.nan)
    # "higher_high_count" / "lower_high_count" → swing highs on H
    # "higher_low_count" / "lower_low_count" → swing lows on L
    is_high_type = op.endswith("high_count")
    is_higher = op.startswith("higher")
    price = H if is_high_type else L
    
    for i in range(period + 1, n_b):
        for t in range(n_t):
            vals = []
            for j in range(i - period + 2, i):
                if is_high_type:
                    if price[t, j] > price[t, j - 1] and price[t, j] > price[t, j + 1]:
                        vals.append(price[t, j])
                else:
                    if price[t, j] < price[t, j - 1] and price[t, j] < price[t, j + 1]:
                        vals.append(price[t, j])
            if len(vals) < 2:
                result[t, i] = 0.0
                continue
            c = 0
            for k in range(len(vals) - 1, 0, -1):
                if is_higher:
                    if vals[k] > vals[k - 1]:
                        c += 1
                    else:
                        break
                else:
                    if vals[k] < vals[k - 1]:
                        c += 1
                    else:
                        break
            result[t, i] = float(c)
    
    return result
