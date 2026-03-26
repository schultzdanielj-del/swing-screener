"""
Backtest Conditions — Evaluate grinder-discovered conditions across historical days.

Computes full indicator series once per ticker, then checks thresholds across
all bars with numpy vectorization. ~17s for 4,100 tickers × 200 days.

Usage:
    python scripts/backtest_conditions.py --days 200

Input: grinder result conditions (hardcoded below — update after each grind)
Output: list of (date, ticker) signals where ALL conditions passed
"""

import sys
import os
import time
import pickle
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings("ignore", message="All-NaN slice encountered")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.expression_engine import ExpressionEngine
from scripts.profiling_engine import (
    count_true, since_true, true_in_row, ema, obv,
    aroon_up, aroon_down, chaikin_money_flow, kaufman_efficiency,
    bollinger_top, bollinger_bot, macd,
)


# ══════════════════════════════════════════════════════════
# GRINDER CONDITIONS — Update these after each grind run
# ══════════════════════════════════════════════════════════
CONDITIONS = [
    {
        "name": "slope_avgc100_off1_adr14",
        "compute": {"op": "ma_slope", "ma": "avgc100", "offset": 1, "normalizer": "adr14"},
        "low": 0.024, "high": 0.132,
    },
    {
        "name": "spread_xavgc13_xavgc50_adr14",
        "compute": {"op": "ma_spread", "ma_fast": "xavgc13", "ma_slow": "xavgc50", "normalizer": "adr14"},
        "low": 0.101, "high": 2.824,
    },
    {
        "name": "adx_7",
        "compute": {"op": "adx", "period": 7},
        "low": 29.731, "high": 85.461,
    },
    {
        "name": "ct_c_gt_avgc200_50",
        "compute": {"op": "count_true", "condition": "c_gt_avgc200", "period": 50},
        "low": 0.000, "high": 0.000,
    },
    {
        "name": "ct_c_gt_c1_15",
        "compute": {"op": "count_true", "condition": "c_gt_c1", "period": 15},
        "low": 6.700, "high": 13.300,
    },
]


def compute_series(engine, comp, **kwargs):
    """Compute full indicator series for one expression.
    
    Returns numpy array of values across all bars. Each series is computed
    once per ticker — not per-bar.
    
    Covers all ops that brute_expressions.py can generate.
    
    kwargs:
        series_registry: dict mapping series name → numpy array, used by
                        on_series ops to access pre-computed extension series.
    """
    op = comp["op"]
    
    if op == "ma_slope":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((ma - ma.shift(comp["offset"])) / norm).values
    
    elif op == "ma_spread":
        fast = engine._ma(comp["ma_fast"])
        slow = engine._ma(comp["ma_slow"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((fast - slow) / norm).values
    
    elif op == "extension":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((engine.c - ma) / norm).values
    
    elif op == "adx":
        return engine._adx(comp["period"]).values
    
    elif op == "rsi":
        return engine._rsi(comp["period"]).values
    
    elif op == "roc":
        p = comp["period"]
        return (100 * (engine.c / engine.c.shift(p) - 1)).values
    
    elif op == "stochastic":
        return engine._stoch(comp["period"]).values
    
    elif op == "cci":
        return engine._cci(comp["period"]).values
    
    elif op == "volume_ratio":
        avg = engine._ma(f"avgv{comp['avg_period']}")
        return (engine.v / avg).values
    
    elif op == "count_true":
        b = engine._bool_series(comp["condition"])
        return count_true(b, comp["period"]).values
    
    elif op == "since_true":
        b = engine._bool_series(comp["condition"])
        return since_true(b, comp["period"]).values
    
    elif op == "true_in_row":
        b = engine._bool_series(comp["condition"])
        return true_in_row(b, comp["period"]).values
    
    elif op == "range_position":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.c - minl) / rng.replace(0, np.nan)).values
    
    elif op == "pullback":
        maxh = engine._maxh(comp["period"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((maxh - engine.c) / norm).values
    
    elif op == "candle_range_ratio":
        rng = engine.h - engine.l
        atr_val = engine._atr(14)
        return (rng / atr_val).values
    
    elif op == "body_range_ratio":
        rng = engine.h - engine.l
        body = (engine.c - engine.o).abs()
        return (body / rng.replace(0, np.nan)).values
    
    elif op == "di_spread":
        return (engine._diplus(comp["period"]) - engine._diminus(comp["period"])).values
    
    elif op == "adx_slope":
        a = engine._adx(comp["period"])
        return (a - a.shift(comp["offset"])).values
    
    elif op == "rsi_slope":
        r = engine._rsi(comp["period"])
        return (r - r.shift(comp["offset"])).values
    
    elif op == "extension_slope":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        ext = engine.c - ma
        return ((ext - ext.shift(comp["offset"])) / norm).values
    
    elif op == "extension_peak_ratio":
        ma = engine._ma(comp["ma"])
        lb = comp["lookback"]
        ext = engine.c - ma
        max_ext = ext.rolling(lb, min_periods=1).max()
        return (ext / max_ext.replace(0, np.nan)).values
    
    elif op == "extension_ceiling_ratio":
        ma = engine._ma(comp["ma"])
        lb = comp["lookback"]
        norm = _get_normalizer(engine, comp["normalizer"])
        ext_norm = (engine.c - ma) / norm
        max_ext = ext_norm.rolling(lb, min_periods=1).max()
        return (ext_norm / max_ext.replace(0, np.nan)).values
    
    elif op == "ext_adr_multiples":
        ma_val = engine._ma(comp["ma"])
        adr_val = engine._adr(14)
        return ((engine.c - ma_val) / adr_val.replace(0, np.nan)).values
    
    elif op == "range_width":
        p = comp["period"]
        rng = engine._maxh(p) - engine._minl(p)
        norm = _get_normalizer(engine, comp["normalizer"])
        return (rng / norm).values
    
    elif op == "bop":
        return engine._bop(comp["period"]).values
    
    elif op == "roc_delta":
        p = comp["period"]
        co = comp["compare_offset"]
        roc_now = engine.c / engine.c.shift(p) - 1
        roc_prev = engine.c.shift(co) / engine.c.shift(co + p) - 1
        return (100 * (roc_now - roc_prev)).values
    
    elif op == "channel_slope":
        p = comp["period"]
        maxh = engine._maxh(p)
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((maxh - maxh.shift(p)) / norm).values
    
    elif op == "distance_to_maxh":
        price_ref = comp["price_ref"]
        price = engine.c if price_ref == "C" else engine.h
        maxh = engine._maxh(comp["maxh_period"]).shift(1)
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((maxh - price) / norm).values
    
    elif op == "ratio_c_maxh":
        maxh = engine._maxh(comp["maxh_period"]).shift(1)
        return (engine.c / maxh.replace(0, np.nan)).values
    
    elif op == "upper_wick_ratio":
        rng = engine.h - engine.l
        upper = engine.h - pd.concat([engine.c, engine.o], axis=1).max(axis=1)
        return (upper / rng.replace(0, np.nan)).values
    
    elif op == "ma_cross_count":
        fast = engine._ma(comp["ma_fast"])
        slow = engine._ma(comp["ma_slow"])
        above = (fast > slow).astype(int)
        cross = above.diff().abs()
        return cross.rolling(comp["period"], min_periods=1).sum().values

    # ── NEW OPS (Step 3 expansion) ──

    elif op == "distance_to_minl":
        price_ref = comp["price_ref"]
        price = engine.c if price_ref == "C" else engine.l
        minl = engine._minl(comp["minl_period"]).shift(1)
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((price - minl) / norm).values

    elif op == "ratio_c_minl":
        minl = engine._minl(comp["minl_period"]).shift(1)
        return (engine.c / minl.replace(0, np.nan)).values

    elif op == "percentile_rank":
        source = comp["source"]
        period = comp["period"]
        if source == "close":
            s = engine.c
        elif source == "volume":
            s = engine.v
        elif source == "range":
            s = engine.h - engine.l
        elif source == "atr14":
            s = engine._atr(14)
        elif source == "rsi14":
            s = engine._rsi(14)
        else:
            s = engine.c
        # Vectorized percentile rank via rolling
        def pct_rank(window):
            if len(window) < 2:
                return np.nan
            return (window <= window.iloc[-1]).sum() / len(window) * 100
        return s.rolling(period, min_periods=2).apply(pct_rank, raw=False).values

    elif op == "spread_slope":
        fast = engine._ma(comp["ma_fast"])
        slow = engine._ma(comp["ma_slow"])
        norm = _get_normalizer(engine, comp["normalizer"])
        spread = (fast - slow) / norm
        offset = comp["offset"]
        return (spread - spread.shift(offset)).values

    elif op == "rvol_continuous":
        period = comp["period"]
        avg_period = comp["avg_period"]
        avg_vol = engine.v.rolling(avg_period, min_periods=avg_period // 2).mean()
        rvol = engine.v / avg_vol.replace(0, np.nan)
        return rvol.rolling(period, min_periods=1).mean().values

    elif op == "cumulative_rvol":
        period = comp["period"]
        avg_period = comp["avg_period"]
        avg_vol = engine.v.rolling(avg_period, min_periods=avg_period // 2).mean()
        rvol = engine.v / avg_vol.replace(0, np.nan)
        return rvol.rolling(period, min_periods=1).sum().values

    elif op == "slope_ratio":
        fast_ma = engine._ma(comp["fast_ma"])
        slow_ma = engine._ma(comp["slow_ma"])
        offset = comp["offset"]
        fast_slope = fast_ma - fast_ma.shift(offset)
        slow_slope = slow_ma - slow_ma.shift(offset)
        return (fast_slope / slow_slope.replace(0, np.nan)).values

    elif op == "retrace_high":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.h - minl) / rng.replace(0, np.nan)).values

    elif op == "retrace_low":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.l - minl) / rng.replace(0, np.nan)).values

    elif op == "vwap_slope":
        p = comp["period"]
        offset = comp["offset"]
        norm = _get_normalizer(engine, comp["normalizer"])
        tp = (engine.h + engine.l + engine.c) / 3
        vwap = (tp * engine.v).rolling(p, min_periods=1).sum() / \
               engine.v.rolling(p, min_periods=1).sum().replace(0, np.nan)
        return ((vwap - vwap.shift(offset)) / norm).values

    elif op == "bars_since_ma_cross":
        ma = engine._ma(comp["ma"])
        max_lb = comp.get("max_lookback", 120)
        above = (engine.c > ma).values
        n = len(above)
        result = np.full(n, float(max_lb))
        for i in range(1, n):
            above_now = above[i]
            for back in range(1, min(max_lb, i + 1)):
                if above[i - back] != above_now:
                    result[i] = float(back)
                    break
        return result

    elif op == "gap_count":
        p = comp["period"]
        threshold = comp.get("threshold", 0.5)
        atr_s = engine._atr(14).values
        gaps = np.abs(engine.o.values[1:] - engine.c.values[:-1])
        # Pad first element
        gaps = np.concatenate([[0.0], gaps])
        # Gap in ATR units
        with np.errstate(divide='ignore', invalid='ignore'):
            gap_atr = np.where(atr_s > 0, gaps / atr_s, 0.0)
        is_gap = (gap_atr > threshold).astype(float)
        # Rolling count over period
        result = np.full(len(is_gap), np.nan)
        for i in range(p - 1, len(is_gap)):
            result[i] = np.sum(is_gap[i - p + 1:i + 1])
        return result

    # ══════════════════════════════════════════════════════════
    # NEW OPS — Step 5a: Ported from expression_engine.compute()
    # ══════════════════════════════════════════════════════════

    elif op == "aroon_up_val":
        return engine._aroon_up(comp["period"]).values

    elif op == "aroon_down_val":
        return engine._aroon_down(comp["period"]).values

    elif op == "aroon_oscillator":
        p = comp["period"]
        return (engine._aroon_up(p) - engine._aroon_down(p)).values

    elif op == "atr_ratio":
        atr_s = engine._atr(comp["period"])
        offset = comp["offset"]
        prev = atr_s.shift(offset)
        return (atr_s / prev.replace(0, np.nan)).values

    elif op == "bollinger_pctb":
        p = comp["period"]
        top = engine._bbtop(p)
        bot = engine._bbbot(p)
        bw = top - bot
        return ((engine.c - bot) / bw.replace(0, np.nan)).values

    elif op == "bollinger_bandwidth":
        p = comp["period"]
        top = engine._bbtop(p)
        bot = engine._bbbot(p)
        mid = engine._ma(f"avgc{p}")
        return ((top - bot) / mid.replace(0, np.nan)).values

    elif op == "bollinger_bandwidth_rank":
        p = comp["period"]
        lb = comp["lookback"]
        top = engine._bbtop(p)
        bot = engine._bbbot(p)
        mid = engine._ma(f"avgc{p}")
        bw = (top - bot) / mid.replace(0, np.nan)
        bw_min = bw.rolling(lb, min_periods=1).min()
        bw_max = bw.rolling(lb, min_periods=1).max()
        bw_range = bw_max - bw_min
        return ((bw - bw_min) / bw_range.replace(0, np.nan)).values

    elif op == "macd_histogram":
        fast = comp.get("fast", 12)
        slow = comp.get("slow", 26)
        signal_p = comp.get("signal", 9)
        macd_line = engine._macd(fast, slow)
        signal_line = ema(macd_line, signal_p)
        return (macd_line - signal_line).values

    elif op == "macd_histogram_slope":
        fast = comp.get("fast", 12)
        slow = comp.get("slow", 26)
        signal_p = comp.get("signal", 9)
        offset = comp["offset"]
        macd_line = engine._macd(fast, slow)
        signal_line = ema(macd_line, signal_p)
        hist = macd_line - signal_line
        return (hist - hist.shift(offset)).values

    elif op == "macd_line_norm":
        fast = comp.get("fast", 12)
        slow = comp.get("slow", 26)
        norm = _get_normalizer(engine, comp["normalizer"])
        return (engine._macd(fast, slow) / norm).values

    elif op == "cmf":
        return engine._cmf(comp["period"]).values

    elif op == "cmf_slope":
        c = engine._cmf(comp["period"])
        offset = comp["offset"]
        return (c - c.shift(offset)).values

    elif op == "kaufman_efficiency_ratio":
        return engine._kaufman_eff(comp["period"]).values

    elif op == "ma_stack_score":
        # Vectorized: count ordered pairs across all bars
        mas = [engine._ma(name) for name in comp["mas"]]
        n_mas = len(mas)
        score = pd.Series(0.0, index=engine.c.index)
        for a in range(n_mas):
            for b in range(a + 1, n_mas):
                score += (mas[a] > mas[b]).astype(float)
        return score.values

    elif op == "ma_undercut_depth":
        ma = engine._ma(comp["ma"])
        p = comp["period"]
        norm = _get_normalizer(engine, comp["normalizer"])
        diff = engine.l - ma
        min_diff = diff.rolling(p, min_periods=1).min()
        return (min_diff / norm).values

    elif op == "obv_slope":
        o = engine._obv()
        offset = comp["offset"]
        vol_period = comp.get("vol_period", 20)
        avg_vol = engine._ma(f"avgv{vol_period}")
        return ((o - o.shift(offset)) / (avg_vol * offset).replace(0, np.nan)).values

    elif op == "vwap_distance":
        p = comp["period"]
        norm = _get_normalizer(engine, comp["normalizer"])
        tp = (engine.h + engine.l + engine.c) / 3
        cum_tpv = (tp * engine.v).rolling(p, min_periods=1).sum()
        cum_vol = engine.v.rolling(p, min_periods=1).sum()
        vwap_s = cum_tpv / cum_vol.replace(0, np.nan)
        return ((engine.c - vwap_s) / norm).values

    elif op == "gap_size":
        norm = _get_normalizer(engine, comp["normalizer"])
        gap = pd.Series(np.nan, index=engine.c.index)
        gap.iloc[1:] = engine.o.values[1:] - engine.c.values[:-1]
        return (gap / norm).values

    elif op == "retracement_level":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.c - minl) / rng.replace(0, np.nan)).values

    elif op == "range_contraction_ratio":
        p = comp["period"]
        curr_width = engine._maxh(p) - engine._minl(p)
        prev_width = (engine._maxh(p) - engine._minl(p)).shift(p)
        return (curr_width / prev_width.replace(0, np.nan)).values

    elif op == "nr_ratio":
        p = comp["period"]
        today_range = engine.h - engine.l
        max_range = (engine.h - engine.l).rolling(p, min_periods=1).max()
        return (today_range / max_range.replace(0, np.nan)).values

    elif op == "lower_wick_ratio":
        rng = engine.h - engine.l
        lower = pd.concat([engine.c, engine.o], axis=1).min(axis=1) - engine.l
        return (lower / rng.replace(0, np.nan)).values

    elif op == "close_vs_open_ratio":
        p = comp["period"]
        bullish = (engine.c > engine.o).astype(float)
        return (bullish.rolling(p, min_periods=1).sum() / p).values

    elif op == "avg_candle_body_ratio":
        p = comp["period"]
        rng = engine.h - engine.l
        body = (engine.c - engine.o).abs()
        ratio = body / rng.replace(0, np.nan)
        return ratio.rolling(p, min_periods=1).mean().values

    elif op == "inside_bar_count":
        p = comp["period"]
        is_inside = ((engine.h < engine.h.shift(1)) & (engine.l > engine.l.shift(1))).astype(float)
        return is_inside.rolling(p, min_periods=1).sum().values

    elif op == "outside_bar_count":
        p = comp["period"]
        is_outside = ((engine.h > engine.h.shift(1)) & (engine.l < engine.l.shift(1))).astype(float)
        return is_outside.rolling(p, min_periods=1).sum().values

    elif op == "high_volume_bar_pct":
        p = comp["period"]
        mult = comp.get("multiplier", 1.5)
        avg_period = comp.get("avg_period", 50)
        avg_v = engine._ma(f"avgv{avg_period}")
        is_high = (engine.v > mult * avg_v).astype(float)
        return (is_high.rolling(p, min_periods=1).sum() / p).values

    elif op == "up_volume_ratio":
        p = comp["period"]
        up_mask = (engine.c > engine.o).astype(float)
        up_vol = (engine.v * up_mask).rolling(p, min_periods=1).sum()
        total_vol = engine.v.rolling(p, min_periods=1).sum()
        return (up_vol / total_vol.replace(0, np.nan)).values

    elif op == "consecutive_up_days":
        c_vals = engine.c.values
        n = len(c_vals)
        result = np.zeros(n)
        for i in range(1, n):
            if c_vals[i] > c_vals[i - 1]:
                result[i] = result[i - 1] + 1
        return result

    elif op == "consecutive_down_days":
        c_vals = engine.c.values
        n = len(c_vals)
        result = np.zeros(n)
        for i in range(1, n):
            if c_vals[i] < c_vals[i - 1]:
                result[i] = result[i - 1] + 1
        return result

    elif op == "consecutive_up_roc":
        c_vals = engine.c.values
        n = len(c_vals)
        result = np.zeros(n)
        for i in range(1, n):
            if c_vals[i] > c_vals[i - 1]:
                result[i] = result[i - 1] + (c_vals[i] / c_vals[i - 1] - 1) * 100
        return result

    elif op == "consecutive_down_roc":
        c_vals = engine.c.values
        n = len(c_vals)
        result = np.zeros(n)
        for i in range(1, n):
            if c_vals[i] < c_vals[i - 1]:
                result[i] = result[i - 1] + (c_vals[i] / c_vals[i - 1] - 1) * 100
        return result

    elif op == "unfilled_gap_up_count":
        p = comp["period"]
        h_vals = engine.h.values
        l_vals = engine.l.values
        o_vals = engine.o.values
        n = len(h_vals)
        result = np.full(n, np.nan)
        for i in range(p, n):
            count = 0
            for j in range(i - p + 1, i + 1):
                if j < 1:
                    continue
                if o_vals[j] > h_vals[j - 1]:
                    # Check if any bar from j to i filled it
                    filled = False
                    for k in range(j, i + 1):
                        if l_vals[k] <= h_vals[j - 1]:
                            filled = True
                            break
                    if not filled:
                        count += 1
            result[i] = count
        return result

    elif op == "swing_high_count":
        p = comp["period"]
        h_vals = engine.h.values
        n = len(h_vals)
        result = np.full(n, np.nan)
        for i in range(p + 1, n):
            count = 0
            for j in range(i - p + 2, i):
                if h_vals[j] > h_vals[j - 1] and h_vals[j] > h_vals[j + 1]:
                    count += 1
            result[i] = count
        return result

    elif op == "swing_low_count":
        p = comp["period"]
        l_vals = engine.l.values
        n = len(l_vals)
        result = np.full(n, np.nan)
        for i in range(p + 1, n):
            count = 0
            for j in range(i - p + 2, i):
                if l_vals[j] < l_vals[j - 1] and l_vals[j] < l_vals[j + 1]:
                    count += 1
            result[i] = count
        return result

    elif op == "higher_high_count":
        p = comp["period"]
        h_vals = engine.h.values
        n = len(h_vals)
        result = np.full(n, np.nan)
        for i in range(p + 1, n):
            # Find swing highs in window
            highs = []
            for j in range(i - p + 2, i):
                if h_vals[j] > h_vals[j - 1] and h_vals[j] > h_vals[j + 1]:
                    highs.append(h_vals[j])
            if len(highs) < 2:
                result[i] = 0.0
                continue
            count = 0
            for k in range(len(highs) - 1, 0, -1):
                if highs[k] > highs[k - 1]:
                    count += 1
                else:
                    break
            result[i] = count
        return result

    elif op == "higher_low_count":
        p = comp["period"]
        l_vals = engine.l.values
        n = len(l_vals)
        result = np.full(n, np.nan)
        for i in range(p + 1, n):
            lows = []
            for j in range(i - p + 2, i):
                if l_vals[j] < l_vals[j - 1] and l_vals[j] < l_vals[j + 1]:
                    lows.append(l_vals[j])
            if len(lows) < 2:
                result[i] = 0.0
                continue
            count = 0
            for k in range(len(lows) - 1, 0, -1):
                if lows[k] > lows[k - 1]:
                    count += 1
                else:
                    break
            result[i] = count
        return result

    elif op == "lower_high_count":
        p = comp["period"]
        h_vals = engine.h.values
        n = len(h_vals)
        result = np.full(n, np.nan)
        for i in range(p + 1, n):
            highs = []
            for j in range(i - p + 2, i):
                if h_vals[j] > h_vals[j - 1] and h_vals[j] > h_vals[j + 1]:
                    highs.append(h_vals[j])
            if len(highs) < 2:
                result[i] = 0.0
                continue
            count = 0
            for k in range(len(highs) - 1, 0, -1):
                if highs[k] < highs[k - 1]:
                    count += 1
                else:
                    break
            result[i] = count
        return result

    elif op == "lower_low_count":
        p = comp["period"]
        l_vals = engine.l.values
        n = len(l_vals)
        result = np.full(n, np.nan)
        for i in range(p + 1, n):
            lows = []
            for j in range(i - p + 2, i):
                if l_vals[j] < l_vals[j - 1] and l_vals[j] < l_vals[j + 1]:
                    lows.append(l_vals[j])
            if len(lows) < 2:
                result[i] = 0.0
                continue
            count = 0
            for k in range(len(lows) - 1, 0, -1):
                if lows[k] < lows[k - 1]:
                    count += 1
                else:
                    break
            result[i] = count
        return result

    elif op == "low_vs_ma":
        ma_s = engine._ma(comp["ma"])
        norm_s = _get_normalizer(engine, comp["normalizer"])
        return ((engine.l - ma_s) / norm_s).values

    elif op == "high_vs_ma":
        ma_s = engine._ma(comp["ma"])
        norm_s = _get_normalizer(engine, comp["normalizer"])
        return ((engine.h - ma_s) / norm_s).values

    elif op == "close_position_in_bar":
        p = comp["period"]
        rng = engine.h - engine.l
        pos = (engine.c - engine.l) / rng.replace(0, np.nan)
        return pos.rolling(p).mean().values

    elif op == "roc_acceleration":
        outer = comp["outer_period"]
        inner = comp["inner_period"]
        roc_s = engine.c.pct_change(outer) * 100
        return roc_s.diff(inner).values

    elif op == "roc_percentile_rank":
        roc_p = comp["roc_period"]
        lb = comp["lookback"]
        roc_s = engine.c.pct_change(roc_p) * 100
        return roc_s.rolling(lb).rank(pct=True).values

    elif op == "volume_price_divergence":
        p = comp["period"]
        price_roc = engine.c.pct_change(p)
        vol_roc = engine.v.rolling(p).mean().pct_change(p)
        return (price_roc - vol_roc).values

    # ══════════════════════════════════════════════════════════════
    # EXIT GRINDER OPS — generic (per-bar, no entry context needed)
    # These must match exit_compute.py ExitExprEngine implementations.
    # ══════════════════════════════════════════════════════════════

    elif op == "avg_bar_range_rolling":
        w = comp.get("window", comp.get("period", 10))
        norm = _get_normalizer(engine, comp.get("normalizer", "adr14"))
        rng = (engine.h - engine.l) / norm
        return rng.rolling(w, min_periods=w).mean().values

    elif op == "avg_body_ratio_rolling":
        w = comp.get("window", comp.get("period", 10))
        rng = engine.h - engine.l
        body = (engine.c - engine.o).abs()
        rng_safe = rng.replace(0, np.nan)
        ratio = body / rng_safe
        return ratio.rolling(w, min_periods=w).mean().values

    elif op == "avg_rvol_rolling":
        w = comp.get("window", comp.get("period", 10))
        avg_p = comp.get("avg_period", 20)
        avg_vol = engine.v.rolling(avg_p).mean()
        avg_vol_safe = avg_vol.replace(0, np.nan)
        rvol = engine.v / avg_vol_safe
        return rvol.rolling(w, min_periods=w).mean().values

    elif op == "bar_range":
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((engine.h - engine.l) / norm).values

    elif op == "close_above_ma":
        ma = engine._ma(comp["ma"])
        return (engine.c > ma).astype(float).values

    elif op == "closed_below_ma":
        ma = engine._ma(comp["ma"])
        return (engine.c < ma).astype(float).values

    elif op == "consecutive_green":
        green = (engine.c > engine.o).values.astype(float)
        result = np.zeros(len(green))
        for i in range(len(green)):
            if green[i]:
                result[i] = result[i-1] + 1 if i > 0 else 1
        return result

    elif op == "consecutive_red":
        red = (engine.c < engine.o).values.astype(float)
        result = np.zeros(len(red))
        for i in range(len(red)):
            if red[i]:
                result[i] = result[i-1] + 1 if i > 0 else 1
        return result

    elif op == "distance_from_ma":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((engine.c - ma) / norm).values

    elif op == "down_vol_ratio_rolling":
        w = comp.get("window", comp.get("period", 10))
        c_arr = engine.c.values
        o_arr = engine.o.values
        v_arr = engine.v.values.astype(float)
        down = c_arr < o_arr
        n = len(c_arr)
        result = np.full(n, np.nan)
        for i in range(w - 1, n):
            s = i - w + 1
            total = np.sum(v_arr[s:i+1])
            if total > 0:
                result[i] = np.sum(v_arr[s:i+1][down[s:i+1]]) / total
        return result

    elif op == "ext_accel":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        ext = ((engine.c - ma) / norm).values
        slope = np.full(len(ext), np.nan)
        slope[1:] = ext[1:] - ext[:-1]
        accel = np.full(len(ext), np.nan)
        accel[2:] = slope[2:] - slope[1:-1]
        return accel

    elif op == "ext_ceiling_ratio":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        ext = ((engine.c - ma) / norm).values
        lb = comp["lookback"]
        n = len(ext)
        result = np.full(n, np.nan)
        for i in range(lb, n):
            ceiling = np.nanmax(ext[max(0, i - lb):i])
            if ceiling != 0 and not np.isnan(ceiling):
                result[i] = ext[i] / ceiling
        return result

    elif op == "ext_retrace_from_peak":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        ext = ((engine.c - ma) / norm).values
        # Running peak of extension (most extreme), then retrace from it
        running_peak = np.minimum.accumulate(ext)  # for shorts, most negative
        return ext - running_peak

    elif op == "ext_slope":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        ext = ((engine.c - ma) / norm).values
        offset = comp["offset"]
        result = np.full(len(ext), np.nan)
        result[offset:] = ext[offset:] - ext[:-offset]
        return result

    elif op == "gap_from_prior":
        norm = _get_normalizer(engine, comp["normalizer"])
        o_arr = engine.o.values
        c_arr = engine.c.values
        result = np.full(len(o_arr), np.nan)
        result[1:] = (o_arr[1:] - c_arr[:-1]) / norm.values[1:]
        return result

    elif op == "higher_low_formed":
        l_arr = engine.l.values
        n = len(l_arr)
        result = np.zeros(n)
        running_min = l_arr[0]
        for i in range(1, n):
            if l_arr[i] < running_min:
                running_min = l_arr[i]
            elif l_arr[i] > running_min and i >= 2:
                result[i] = 1.0
        return result

    elif op == "is_doji":
        rng = engine.h - engine.l
        body = (engine.c - engine.o).abs()
        rng_safe = rng.replace(0, np.nan)
        ratio = body / rng_safe
        return (ratio < 0.1).astype(float).values

    elif op == "is_green":
        return (engine.c > engine.o).astype(float).values

    elif op == "lower_low_sequence":
        l_arr = engine.l.values
        n = len(l_arr)
        result = np.zeros(n)
        count = 0
        for i in range(1, n):
            if l_arr[i] < l_arr[i-1]:
                count += 1
            else:
                count = 0
            result[i] = count
        return result

    elif op == "new_high_count":
        p = comp["period"]
        h_arr = engine.h.values
        n = len(h_arr)
        hits = np.zeros(n)
        for i in range(1, n):
            s = max(0, i - p)
            if h_arr[i] > np.max(h_arr[s:i]):
                hits[i] = 1.0
        return np.cumsum(hits)

    elif op == "new_low_count":
        p = comp["period"]
        l_arr = engine.l.values
        n = len(l_arr)
        hits = np.zeros(n)
        for i in range(1, n):
            s = max(0, i - p)
            if l_arr[i] < np.min(l_arr[s:i]):
                hits[i] = 1.0
        return np.cumsum(hits)

    elif op == "pct_green_rolling":
        w = comp.get("window", comp.get("period", 10))
        green = (engine.c > engine.o).astype(float)
        return green.rolling(w, min_periods=w).mean().values

    elif op == "range_contracting":
        w = comp.get("window", comp.get("period", 10))
        rng = (engine.h - engine.l).values
        n = len(rng)
        result = np.zeros(n)
        for i in range(2 * w - 1, n):
            recent = np.mean(rng[i - w + 1:i + 1])
            prior = np.mean(rng[i - 2 * w + 1:i - w + 1])
            if prior > 0:
                result[i] = float(recent < prior)
        return result

    elif op == "rvol":
        avg_p = comp.get("avg_period", 20)
        avg_vol = engine.v.rolling(avg_p).mean()
        avg_vol_safe = avg_vol.replace(0, np.nan)
        return (engine.v / avg_vol_safe).values

    elif op == "touched_ma":
        ma = engine._ma(comp["ma"])
        # Low touched or crossed below MA
        return (engine.l <= ma).astype(float).values

    elif op == "up_vol_ratio_rolling":
        w = comp.get("window", comp.get("period", 10))
        c_arr = engine.c.values
        o_arr = engine.o.values
        v_arr = engine.v.values.astype(float)
        up = c_arr > o_arr
        n = len(c_arr)
        result = np.full(n, np.nan)
        for i in range(w - 1, n):
            s = i - w + 1
            total = np.sum(v_arr[s:i+1])
            if total > 0:
                result[i] = np.sum(v_arr[s:i+1][up[s:i+1]]) / total
        return result

    elif op == "vol_trend_rolling":
        w = comp.get("window", comp.get("period", 10))
        v_arr = engine.v.values.astype(float)
        n = len(v_arr)
        result = np.full(n, np.nan)
        for i in range(w - 1, n):
            s = i - w + 1
            if v_arr[s] > 0:
                result[i] = v_arr[i] / v_arr[s] - 1
        return result

    elif op == "on_series":
        # Extension structure op: run an inner op on a named series as if it were price.
        # series_data must be passed via kwargs or pre-injected on the engine.
        series_name = comp["series"]
        inner_op = comp["inner_op"]
        # Get the series data — passed via series_registry kwarg
        s_data = kwargs.get("series_registry", {}).get(series_name)
        if s_data is None:
            # Try to compute from engine (e.g. ext_avgc50_adr14)
            try:
                base_comp = _EXT_SERIES_COMPUTE.get(series_name)
                if base_comp:
                    s_data = compute_series(engine, base_comp)
            except:
                pass
        if s_data is None:
            return np.full(len(engine.c), np.nan)
        return compute_on_series(np.asarray(s_data, dtype=np.float64), inner_op)

    elif op == "on_series_bool_agg":
        # Boolean aggregation on extension structure series.
        # Computes a boolean condition from the series, then applies ct_/st_/tir_.
        series_name = comp["series"]
        bool_spec = comp["bool_op"]  # {op, period, threshold, direction}
        agg_op = comp["agg_op"]      # "count_true", "since_true", "true_in_row"
        agg_period = comp["agg_period"]

        s_data = kwargs.get("series_registry", {}).get(series_name)
        if s_data is None:
            try:
                base_comp = _EXT_SERIES_COMPUTE.get(series_name)
                if base_comp:
                    s_data = compute_series(engine, base_comp)
            except:
                pass
        if s_data is None:
            return np.full(len(engine.c), np.nan)

        # Compute the indicator on the series
        indicator = compute_on_series(
            np.asarray(s_data, dtype=np.float64), bool_spec
        )
        # Apply threshold to get boolean
        threshold = bool_spec.get("threshold", 0)
        direction = bool_spec.get("direction", "gt")
        if direction == "gt":
            bool_s = pd.Series(indicator > threshold)
        elif direction == "lt":
            bool_s = pd.Series(indicator < threshold)
        elif direction == "positive":
            bool_s = pd.Series(indicator > 0)
        elif direction == "negative":
            bool_s = pd.Series(indicator < 0)
        else:
            bool_s = pd.Series(indicator > threshold)

        # Apply aggregation
        if agg_op == "count_true":
            return count_true(bool_s, agg_period).values
        elif agg_op == "since_true":
            return since_true(bool_s, agg_period).values
        elif agg_op == "true_in_row":
            return true_in_row(bool_s, agg_period).values
        else:
            return np.full(len(engine.c), np.nan)

    else:
        raise ValueError(f"Unsupported op for backtest series: {op}")


# ══════════════════════════════════════════════════════════════
# ON_SERIES — Compute inner ops on a 1D series (extension structure)
# ══════════════════════════════════════════════════════════════

# Map extension series names to their compute specs (for live backtest fallback)
_EXT_SERIES_COMPUTE = {
    "ext_avgc50_adr14": {"op": "extension", "ma": "avgc50", "normalizer": "adr14"},
    "ext_avgc200_adr14": {"op": "extension", "ma": "avgc200", "normalizer": "adr14"},
}


def compute_on_series(series, inner_op):
    """Run an inner op on a 1D float64 numpy array treated as 'close price'.

    This enables the full price-structure expression suite to be applied
    to extension series (ext_avgc50_adr14, ext_avgc200_adr14) or any
    other derived series.

    The series is treated as a standalone price chart — no OHLCV required.
    High=Low=Close=series for ops that reference H/L.
    """
    s = pd.Series(series)
    n = len(s)
    op = inner_op["op"]

    if op == "slope":
        offset = inner_op["offset"]
        return (s - s.shift(offset)).values

    elif op == "roc":
        p = inner_op["period"]
        shifted = s.shift(p)
        return (100 * (s / shifted.replace(0, np.nan) - 1)).values

    elif op == "rsi":
        p = inner_op["period"]
        delta = s.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(span=p, adjust=False).mean()
        avg_loss = loss.ewm(span=p, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).values

    elif op == "rsi_slope":
        p = inner_op["period"]
        offset = inner_op["offset"]
        delta = s.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(span=p, adjust=False).mean()
        avg_loss = loss.ewm(span=p, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_s = 100 - 100 / (1 + rs)
        return (rsi_s - rsi_s.shift(offset)).values

    elif op == "stochastic":
        p = inner_op["period"]
        min_s = s.rolling(p, min_periods=1).min()
        max_s = s.rolling(p, min_periods=1).max()
        rng = max_s - min_s
        return ((s - min_s) / rng.replace(0, np.nan) * 100).values

    elif op == "cci":
        p = inner_op["period"]
        # CCI on single series: typical = series itself (no H/L)
        sma_s = s.rolling(p, min_periods=1).mean()
        # MAD must use each window's own mean, not the per-element rolling mean.
        arr = s.values.astype(np.float64)
        result_mad = np.full(n, np.nan)
        if p <= n:
            from numpy.lib.stride_tricks import sliding_window_view
            windows = sliding_window_view(arr, p)
            wmeans = windows.mean(axis=1, keepdims=True)
            mads = np.mean(np.abs(windows - wmeans), axis=1)
            result_mad[p - 1:] = mads
        mad = pd.Series(result_mad, index=s.index)
        return ((s - sma_s) / (0.015 * mad).replace(0, np.nan)).values

    elif op == "adx":
        p = inner_op["period"]
        # ADX on single series: use absolute changes as +DM/-DM proxy
        diff = s.diff()
        plus_dm = diff.clip(lower=0)
        minus_dm = (-diff).clip(lower=0)
        atr_proxy = diff.abs().ewm(span=p, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=p, adjust=False).mean() / atr_proxy.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(span=p, adjust=False).mean() / atr_proxy.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(span=p, adjust=False).mean().values

    elif op == "adx_slope":
        p = inner_op["period"]
        offset = inner_op["offset"]
        diff = s.diff()
        plus_dm = diff.clip(lower=0)
        minus_dm = (-diff).clip(lower=0)
        atr_proxy = diff.abs().ewm(span=p, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=p, adjust=False).mean() / atr_proxy.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(span=p, adjust=False).mean() / atr_proxy.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_s = dx.ewm(span=p, adjust=False).mean()
        return (adx_s - adx_s.shift(offset)).values

    elif op == "range_position":
        p = inner_op["period"]
        max_s = s.rolling(p, min_periods=1).max()
        min_s = s.rolling(p, min_periods=1).min()
        rng = max_s - min_s
        return ((s - min_s) / rng.replace(0, np.nan)).values

    elif op == "pullback":
        p = inner_op["period"]
        max_s = s.rolling(p, min_periods=1).max()
        return (max_s - s).values

    elif op == "floor_ratio":
        lb = inner_op["lookback"]
        min_s = s.rolling(lb, min_periods=1).min()
        rng = s.rolling(lb, min_periods=1).max() - min_s
        return ((s - min_s) / rng.replace(0, np.nan)).values

    elif op == "peak_ratio":
        lb = inner_op["lookback"]
        max_s = s.rolling(lb, min_periods=1).max()
        return (s / max_s.replace(0, np.nan)).values

    elif op == "ceiling_ratio":
        lb = inner_op["lookback"]
        max_s = s.rolling(lb, min_periods=1).max()
        min_s = s.rolling(lb, min_periods=1).min()
        rng = max_s - min_s
        return ((max_s - s) / rng.replace(0, np.nan)).values

    elif op == "trendline_deviation":
        lb = inner_op["lookback"]
        # Vectorized linear regression residual using rolling sums.
        arr = s.values.astype(np.float64)
        result = np.full(n, np.nan)
        if lb > n:
            return result
        x = np.arange(lb, dtype=np.float64)
        sum_x = x.sum()
        sum_x2 = (x * x).sum()
        mean_x = sum_x / lb
        denom = sum_x2 - sum_x * sum_x / lb

        # Rolling Σy via cumsum
        cs = np.nancumsum(arr)
        cs = np.insert(cs, 0, 0.0)
        sum_y = cs[lb:] - cs[:-lb]

        # Rolling Σxy via convolution: for window ending at i,
        # positions 0..lb-1 map to arr[i-lb+1..i], so weight arr[k] by (k - (i-lb+1))
        # Equivalent to convolving arr with reversed x weights
        x_weights = x[::-1].copy()
        sum_xy_full = np.convolve(arr, x_weights, mode='full')
        # Valid entries start at index lb-1
        sum_xy = sum_xy_full[lb - 1: lb - 1 + n - lb + 1]

        slope_val = (sum_xy - sum_x * sum_y / lb) / (denom + 1e-10)
        intercept = sum_y / lb - slope_val * mean_x
        projected = slope_val * (lb - 1) + intercept
        # y at window end = arr[lb-1], arr[lb], ..., arr[n-1]
        y_end = arr[lb - 1:]
        result[lb - 1:] = y_end - projected
        # NaN out windows that had NaN input
        nan_count = np.convolve(np.isnan(s.values).astype(float), np.ones(lb), mode='full')
        nan_count = nan_count[lb - 1: lb - 1 + n - lb + 1]
        result[lb - 1:][nan_count > 0] = np.nan
        return result

    elif op == "channel_position":
        lb = inner_op["lookback"]
        # Vectorized: position within linear regression channel
        arr = s.values.astype(np.float64)
        result = np.full(n, np.nan)
        if lb > n:
            return result
        x = np.arange(lb, dtype=np.float64)
        sum_x = x.sum()
        sum_x2 = (x * x).sum()
        mean_x = sum_x / lb
        denom = sum_x2 - sum_x * sum_x / lb

        # Rolling Σy via cumsum
        cs = np.nancumsum(arr)
        cs = np.insert(cs, 0, 0.0)
        sum_y = cs[lb:] - cs[:-lb]

        # Rolling Σxy via convolution
        x_weights = x[::-1].copy()
        sum_xy_full = np.convolve(arr, x_weights, mode='full')
        sum_xy = sum_xy_full[lb - 1: lb - 1 + n - lb + 1]

        slope_val = (sum_xy - sum_x * sum_y / lb) / (denom + 1e-10)
        intercept = sum_y / lb - slope_val * mean_x

        # Rolling Σy² for std of residuals: var(resid) = var(y) - slope²·var(x)
        # var(y) = Σy²/n - (Σy/n)²
        cs2 = np.nancumsum(arr * arr)
        cs2 = np.insert(cs2, 0, 0.0)
        sum_y2 = cs2[lb:] - cs2[:-lb]
        var_y = sum_y2 / lb - (sum_y / lb) ** 2
        var_x = denom / lb  # constant
        var_resid = var_y - slope_val ** 2 * var_x
        # Clamp negative (numerical noise)
        var_resid = np.maximum(var_resid, 0.0)
        std_resid = np.sqrt(var_resid)

        y_end = arr[lb - 1:]
        projected = slope_val * (lb - 1) + intercept
        valid = std_resid > 0
        vals = np.where(valid, (y_end - projected) / std_resid, np.nan)
        result[lb - 1:] = vals
        # NaN out windows with NaN input
        nan_count = np.convolve(np.isnan(s.values).astype(float), np.ones(lb), mode='full')
        nan_count = nan_count[lb - 1: lb - 1 + n - lb + 1]
        result[lb - 1:][nan_count > 0] = np.nan
        return result

    elif op == "bollinger_pctb":
        p = inner_op["period"]
        std_mult = inner_op.get("std_mult", 2.0)
        sma = s.rolling(p, min_periods=1).mean()
        std = s.rolling(p, min_periods=1).std()
        upper = sma + std_mult * std
        lower = sma - std_mult * std
        bw = upper - lower
        return ((s - lower) / bw.replace(0, np.nan)).values

    elif op == "smoothed_ma":
        p = inner_op["period"]
        return s.rolling(p, min_periods=1).mean().values

    elif op == "ma_cross":
        fast_p = inner_op["fast_period"]
        slow_p = inner_op["slow_period"]
        fast = s.rolling(fast_p, min_periods=1).mean()
        slow = s.rolling(slow_p, min_periods=1).mean()
        return (fast - slow).values

    elif op == "roc_delta":
        p = inner_op["period"]
        co = inner_op["compare_offset"]
        shifted = s.shift(p)
        roc_now = s / shifted.replace(0, np.nan) - 1
        shifted_co = s.shift(co)
        shifted_cop = s.shift(co + p)
        roc_prev = shifted_co / shifted_cop.replace(0, np.nan) - 1
        return (100 * (roc_now - roc_prev)).values

    elif op == "roc_acceleration":
        outer = inner_op["outer_period"]
        inner_p = inner_op["inner_period"]
        roc_s = s.pct_change(outer) * 100
        return roc_s.diff(inner_p).values

    else:
        raise ValueError(f"Unsupported inner op for on_series: {op}")


def _get_normalizer(engine, norm_name):
    """Get normalizer series by name."""
    if norm_name == "adr14":
        return engine._adr(14)
    elif norm_name == "atr14":
        return engine._atr(14)
    elif norm_name == "pct":
        return engine.c / 100
    elif norm_name == "close":
        return engine.c
    else:
        return engine._atr(14)


def backtest(universe_cache, conditions, lookback_days=200):
    """Run backtest across all tickers and historical days.
    
    For each ticker:
      1. Create ExpressionEngine (computes indicator series once)
      2. For each condition, get the full series via compute_series()
      3. Vectorized threshold check across all bars at once
      4. Collect signals in the lookback window
    
    Returns list of signal dicts: {date, ticker, bar_idx}
    """
    signals = []
    n_tickers = len(universe_cache)
    t0 = time.time()
    skipped = 0
    
    for idx, (ticker, df) in enumerate(universe_cache.items()):
        if df is None or len(df) < 100:
            skipped += 1
            continue
        
        n_bars = len(df)
        start_bar = max(50, n_bars - lookback_days)
        
        try:
            engine = ExpressionEngine(df)
            
            # Compute all series and check thresholds in one pass
            pass_mask = np.ones(n_bars, dtype=bool)
            for cond in conditions:
                series = compute_series(engine, cond["compute"])
                low, high = cond["low"], cond["high"]
                in_range = (series >= low) & (series <= high)
                in_range[np.isnan(series)] = False
                pass_mask &= in_range
            
            # Only look at backtest window
            pass_mask[:start_bar] = False
            
            # Collect signals
            signal_bars = np.where(pass_mask)[0]
            dates = df["date"]
            for bar_idx in signal_bars:
                d = dates.iloc[bar_idx]
                signals.append({
                    "date": str(d.date()) if hasattr(d, 'date') else str(d)[:10],
                    "ticker": ticker,
                    "bar_idx": int(bar_idx),
                })
        except Exception as e:
            skipped += 1
            continue
        
        if (idx + 1) % 500 == 0 or (idx + 1) == n_tickers:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed
            eta = (n_tickers - idx - 1) / rate
            print(f"  {idx+1:,}/{n_tickers:,} tickers | {len(signals)} signals | "
                  f"{elapsed:.1f}s elapsed | ~{eta:.0f}s remaining")
    
    elapsed = time.time() - t0
    print(f"\nBacktest complete: {len(signals)} signals from "
          f"{n_tickers - skipped:,} tickers in {elapsed:.1f}s "
          f"({skipped} skipped)")
    return signals


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backtest grinder conditions across historical days")
    parser.add_argument("--days", type=int, default=200, help="Trading days to look back")
    args = parser.parse_args()
    
    # Load OHLCV cache
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "local_runner", "cache", "universe_ohlcv.pkl")
    if not os.path.exists(cache_file):
        print(f"Cache not found at {cache_file}")
        print("Run: python local_runner/cache_builder.py first")
        sys.exit(1)
    
    print(f"Loading OHLCV cache...")
    t0 = time.time()
    with open(cache_file, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe):,} tickers loaded in {time.time()-t0:.1f}s")
    
    print(f"\nConditions ({len(CONDITIONS)}):")
    for c in CONDITIONS:
        print(f"  {c['name']}: [{c['low']:.3f} — {c['high']:.3f}]")
    
    print(f"\nRunning {args.days}-day backtest...")
    signals = backtest(universe, CONDITIONS, lookback_days=args.days)
    
    # Summary
    if signals:
        df = pd.DataFrame(signals)
        print(f"\n{'='*60}")
        print(f"RESULTS: {len(signals)} total signals over {args.days} days")
        print(f"{'='*60}")
        
        # Signals by date
        by_date = df.groupby("date").size().sort_index()
        print(f"\nSignals by date:")
        for date, count in by_date.items():
            tickers = sorted(df[df["date"] == date]["ticker"].tolist())
            ticker_str = ", ".join(tickers[:15])
            if len(tickers) > 15:
                ticker_str += f"... (+{len(tickers)-15} more)"
            print(f"  {date}: {count:>3} — {ticker_str}")
        
        print(f"\n--- Summary ---")
        print(f"Total signals: {len(signals)}")
        print(f"Unique dates with signals: {df['date'].nunique()}")
        print(f"Unique tickers: {df['ticker'].nunique()}")
        print(f"Avg signals/day (signal days only): {len(signals)/df['date'].nunique():.1f}")
        print(f"Avg signals/day (all {args.days} days): {len(signals)/args.days:.1f}")
    else:
        print("\nNo signals found.")


if __name__ == "__main__":
    main()
