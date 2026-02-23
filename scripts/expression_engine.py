"""
Expression Compute Engine — Evaluates expression definitions against OHLCV data.

Takes a precomputed indicator cache (MAs, ATR, RSI etc.) and an expression
definition, returns a single numeric value at the target index.

Usage:
    from scripts.expression_engine import ExpressionEngine

    engine = ExpressionEngine(df)  # df = OHLCV DataFrame
    engine.set_target(idx)         # which bar to evaluate at
    value = engine.compute(expr)   # expr = expression dict from expressions.json
"""

import numpy as np
import pandas as pd
from scripts.profiling_engine import (
    sma, ema, hma, rolling_max, rolling_min, rolling_sum,
    atr, rsi, wrsi, stochastic_k, cci, adx, di_plus, di_minus,
    bop, obv, count_true, since_true, true_in_row,
    macd, bollinger_top, bollinger_bot, stddev,
    aroon_up, aroon_down, chaikin_money_flow, kaufman_efficiency,
)


class ExpressionEngine:
    """Compute expressions against OHLCV data at a target index."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.c = df["close"]
        self.o = df["open"]
        self.h = df["high"]
        self.l = df["low"]
        self.v = df["volume"]
        self._cache = {}
        self._target = len(df) - 1
        self._lsp = None  # Set via set_lsp_context for DTSS expressions

    def set_target(self, idx: int):
        self._target = idx

    def set_lsp_context(self, lsp: dict):
        """
        Inject LSP context for DTSS-specific expressions.
        lsp dict keys: date, price, bars_lookback, volume_ratio
        """
        self._lsp = lsp

    # ── Indicator cache ──────────────────────────────────────
    def _get(self, key, compute_fn):
        if key not in self._cache:
            self._cache[key] = compute_fn()
        return self._cache[key]

    def _ma(self, name: str) -> pd.Series:
        """Get a moving average by name like 'avgc50', 'xavgc21', 'havgc9'."""
        def _compute():
            n = name.lower()
            if n.startswith("xavgc"):
                return ema(self.c, int(n[5:]))
            elif n.startswith("avgc"):
                return sma(self.c, int(n[4:]))
            elif n.startswith("avgv"):
                return sma(self.v, int(n[4:]))
            elif n.startswith("havgc"):
                return hma(self.c, int(n[5:]))
            else:
                raise ValueError(f"Unknown MA: {name}")
        return self._get(f"ma_{name}", _compute)

    def _atr(self, period: int) -> pd.Series:
        return self._get(f"atr{period}", lambda: atr(self.df, period))

    def _adr(self, period: int) -> pd.Series:
        """Average Daily Range = SMA of (H - L)."""
        return self._get(f"adr{period}", lambda: sma(self.h - self.l, period))

    def _maxh(self, period: int) -> pd.Series:
        return self._get(f"maxh{period}", lambda: rolling_max(self.h, period))

    def _minl(self, period: int) -> pd.Series:
        return self._get(f"minl{period}", lambda: rolling_min(self.l, period))

    def _rsi(self, period: int) -> pd.Series:
        return self._get(f"rsi{period}", lambda: rsi(self.c, period))

    def _adx(self, period: int) -> pd.Series:
        return self._get(f"adx{period}", lambda: adx(self.df, period))

    def _diplus(self, period: int) -> pd.Series:
        return self._get(f"diplus{period}", lambda: di_plus(self.df, period))

    def _diminus(self, period: int) -> pd.Series:
        return self._get(f"diminus{period}", lambda: di_minus(self.df, period))

    def _stoch(self, period: int) -> pd.Series:
        return self._get(f"stoch{period}", lambda: stochastic_k(self.df, period))

    def _cci(self, period: int) -> pd.Series:
        return self._get(f"cci{period}", lambda: cci(self.df, period))

    def _bop(self, period: int) -> pd.Series:
        return self._get(f"bop{period}", lambda: bop(self.df, period))

    def _obv(self) -> pd.Series:
        return self._get("obv", lambda: obv(self.df))

    def _macd(self, fast: int, slow: int) -> pd.Series:
        return self._get(f"macd_{fast}_{slow}", lambda: macd(self.c, fast, slow))

    def _bbtop(self, period: int, nstd: float = 2.0) -> pd.Series:
        return self._get(f"bbtop_{period}_{nstd}", lambda: bollinger_top(self.c, period, nstd))

    def _bbbot(self, period: int, nstd: float = 2.0) -> pd.Series:
        return self._get(f"bbbot_{period}_{nstd}", lambda: bollinger_bot(self.c, period, nstd))

    def _stddev(self, period: int) -> pd.Series:
        return self._get(f"stddev_{period}", lambda: stddev(self.c, period))

    def _aroon_up(self, period: int) -> pd.Series:
        return self._get(f"aroon_up_{period}", lambda: aroon_up(self.df, period))

    def _aroon_down(self, period: int) -> pd.Series:
        return self._get(f"aroon_down_{period}", lambda: aroon_down(self.df, period))

    def _cmf(self, period: int) -> pd.Series:
        return self._get(f"cmf_{period}", lambda: chaikin_money_flow(self.df, period))

    def _kaufman_eff(self, period: int) -> pd.Series:
        return self._get(f"kauf_eff_{period}", lambda: kaufman_efficiency(self.c, period))

    def _normalizer(self, name: str) -> float:
        """Get normalizer value at target index."""
        i = self._target
        if name == "atr14":
            v = self._atr(14).iloc[i]
        elif name == "adr14":
            v = self._adr(14).iloc[i]
        elif name == "pct":
            v = self.c.iloc[i] / 100  # so result is in percent
        else:
            raise ValueError(f"Unknown normalizer: {name}")
        return v if v != 0 and not np.isnan(v) else np.nan

    # ── Boolean condition evaluation ─────────────────────────
    def _bool_series(self, cond_name: str) -> pd.Series:
        """Evaluate a named boolean condition across the full series.

        Uses if/elif dispatch so only the requested boolean is computed.
        The old dict-literal approach eagerly evaluated ALL ~55 booleans
        every time ANY single one was requested (~245ms wasted per ticker).
        """
        def _compute():
            c, o, h, l, v = self.c, self.o, self.h, self.l, self.v
            n = cond_name
            # --- Price vs MA ---
            if   n == "c_gt_xavgc8":       s = c > self._ma("xavgc8")
            elif n == "c_gt_xavgc21":      s = c > self._ma("xavgc21")
            elif n == "c_gt_xavgc50":      s = c > self._ma("xavgc50")
            elif n == "c_gt_xavgc100":     s = c > self._ma("xavgc100")
            elif n == "c_gt_avgc50":       s = c > self._ma("avgc50")
            elif n == "c_gt_avgc200":      s = c > self._ma("avgc200")
            elif n == "c_lt_xavgc8":       s = c < self._ma("xavgc8")
            elif n == "c_lt_xavgc21":      s = c < self._ma("xavgc21")
            elif n == "c_lt_avgc50":       s = c < self._ma("avgc50")
            elif n == "c_lt_avgc200":      s = c < self._ma("avgc200")
            # --- Price vs prior bar ---
            elif n == "c_gt_c1":           s = c > c.shift(1)
            elif n == "c_lt_c1":           s = c < c.shift(1)
            elif n == "h_gt_h1":           s = h > h.shift(1)
            elif n == "l_lt_l1":           s = l < l.shift(1)
            elif n == "c_gt_o":            s = c > o
            # --- Volume ---
            elif n == "v_gt_avgv20":       s = v > self._ma("avgv20")
            elif n == "v_gt_2x_avgv20":    s = v > 2 * self._ma("avgv20")
            elif n == "v_gt_avgv50":       s = v > self._ma("avgv50")
            elif n == "v_lt_avgv20":       s = v < self._ma("avgv20")
            elif n == "v_lt_half_avgv20":  s = v < 0.5 * self._ma("avgv20")
            # --- MA vs MA ---
            elif n == "xavgc8_gt_xavgc21": s = self._ma("xavgc8") > self._ma("xavgc21")
            elif n == "xavgc50_gt_xavgc200": s = self._ma("xavgc50") > self._ma("xavgc200")
            elif n == "avgc50_gt_avgc200": s = self._ma("avgc50") > self._ma("avgc200")
            elif n == "xavgc21_gt_avgc50": s = self._ma("xavgc21") > self._ma("avgc50")
            elif n == "xavgc8_gt_avgc50":  s = self._ma("xavgc8") > self._ma("avgc50")
            # --- MA direction ---
            elif n == "avgc50_rising":     s = self._ma("avgc50") > self._ma("avgc50").shift(1)
            elif n == "avgc50_falling":    s = self._ma("avgc50") < self._ma("avgc50").shift(1)
            elif n == "avgc200_rising":    s = self._ma("avgc200") > self._ma("avgc200").shift(1)
            elif n == "xavgc50_rising":    s = self._ma("xavgc50") > self._ma("xavgc50").shift(1)
            elif n == "xavgc21_rising":    s = self._ma("xavgc21") > self._ma("xavgc21").shift(1)
            elif n == "xavgc21_falling":   s = self._ma("xavgc21") < self._ma("xavgc21").shift(1)
            elif n == "xavgc8_rising":     s = self._ma("xavgc8") > self._ma("xavgc8").shift(1)
            elif n == "xavgc8_falling":    s = self._ma("xavgc8") < self._ma("xavgc8").shift(1)
            # --- Breakout/breakdown ---
            elif n == "h_gt_maxh5_1":      s = h > self._maxh(5).shift(1)
            elif n == "h_gt_maxh10_1":     s = h > self._maxh(10).shift(1)
            elif n == "h_gt_maxh20_1":     s = h > self._maxh(20).shift(1)
            elif n == "l_lt_minl5_1":      s = l < self._minl(5).shift(1)
            elif n == "l_lt_minl10_1":     s = l < self._minl(10).shift(1)
            elif n == "l_lt_minl20_1":     s = l < self._minl(20).shift(1)
            elif n == "c_gt_maxc10_1":     s = c > rolling_max(c, 10).shift(1)
            # --- Range/candle ---
            elif n == "range_gt_atr":      s = (h - l) > self._atr(14)
            elif n == "body_gt_half_range": s = abs(c - o) > 0.5 * (h - l)
            elif n == "c_upper_half":      s = c > (h + l) / 2
            elif n == "c_lower_half":      s = c < (h + l) / 2
            elif n == "inside_bar":        s = (h < h.shift(1)) & (l > l.shift(1))
            elif n == "outside_bar":       s = (h > h.shift(1)) & (l < l.shift(1))
            # --- Gap ---
            elif n == "gap_up":            s = o > c.shift(1)
            elif n == "gap_down":          s = o < c.shift(1)
            elif n == "big_gap_up":        s = (o - c.shift(1)) > self._atr(14)
            elif n == "big_gap_down":      s = (c.shift(1) - o) > self._atr(14)
            # --- Directional/momentum ---
            elif n == "diplus_gt_diminus": s = self._diplus(14) > self._diminus(14)
            elif n == "rsi14_gt_50":       s = self._rsi(14) > 50
            elif n == "rsi14_gt_60":       s = self._rsi(14) > 60
            elif n == "rsi14_gt_70":       s = self._rsi(14) > 70
            elif n == "rsi14_lt_30":       s = self._rsi(14) < 30
            elif n == "rsi14_lt_40":       s = self._rsi(14) < 40
            elif n == "rsi14_lt_50":       s = self._rsi(14) < 50
            elif n == "adx14_gt_20":       s = self._adx(14) > 20
            elif n == "adx14_gt_25":       s = self._adx(14) > 25
            elif n == "adx14_gt_30":       s = self._adx(14) > 30
            elif n == "adx14_lt_20":       s = self._adx(14) < 20
            # --- Bollinger ---
            elif n == "c_gt_bbtop":        s = c > (self._ma("avgc20") + 2 * self.c.rolling(20).std())
            elif n == "c_lt_bbbot":        s = c < (self._ma("avgc20") - 2 * self.c.rolling(20).std())
            # --- Chaikin Money Flow ---
            elif n == "cmf20_positive":    s = self._cmf(20) > 0
            elif n == "cmf20_negative":    s = self._cmf(20) < 0
            else:
                raise ValueError(f"Unknown boolean condition: {cond_name}")
            return s.astype(bool)
        return self._get(f"bool_{cond_name}", _compute)

    # ── Main compute dispatch ────────────────────────────────
    def compute(self, expr: dict) -> float:
        """Compute a single expression, return value at target index."""
        comp = expr["compute"]
        op = comp["op"]
        i = self._target

        try:
            if op == "distance_to_maxh":
                price = self.c.iloc[i] if comp["price_ref"] == "C" else self.h.iloc[i]
                # shift(1) — prior period's high, excludes today so new highs != at resistance
                maxh = self._maxh(comp["maxh_period"]).shift(1).iloc[i]
                norm = self._normalizer(comp["normalizer"])
                return (maxh - price) / norm if norm else np.nan

            elif op == "ratio_c_maxh":
                maxh = self._maxh(comp["maxh_period"]).shift(1).iloc[i]
                return self.c.iloc[i] / maxh if maxh else np.nan

            elif op == "extension":
                ma_val = self._ma(comp["ma"]).iloc[i]
                norm = self._normalizer(comp["normalizer"])
                return (self.c.iloc[i] - ma_val) / norm if norm else np.nan

            elif op == "ma_slope":
                ma = self._ma(comp["ma"])
                offset = comp["offset"]
                norm = self._normalizer(comp["normalizer"])
                val = ma.iloc[i] - ma.iloc[i - offset] if i >= offset else np.nan
                return val / norm if norm else np.nan

            elif op == "ma_spread":
                fast = self._ma(comp["ma_fast"]).iloc[i]
                slow = self._ma(comp["ma_slow"]).iloc[i]
                norm = self._normalizer(comp["normalizer"])
                return (fast - slow) / norm if norm else np.nan

            elif op == "roc":
                p = comp["period"]
                if i < p:
                    return np.nan
                return 100 * (self.c.iloc[i] / self.c.iloc[i - p] - 1)

            elif op == "roc_delta":
                p = comp["period"]
                co = comp["compare_offset"]
                if i < p + co:
                    return np.nan
                roc_now = self.c.iloc[i] / self.c.iloc[i - p] - 1
                roc_prev = self.c.iloc[i - co] / self.c.iloc[i - co - p] - 1
                return 100 * (roc_now - roc_prev)

            elif op == "rsi":
                return self._rsi(comp["period"]).iloc[i]

            elif op == "rsi_slope":
                r = self._rsi(comp["period"])
                offset = comp["offset"]
                return r.iloc[i] - r.iloc[i - offset] if i >= offset else np.nan

            elif op == "volume_ratio":
                avg = self._ma(f"avgv{comp['avg_period']}").iloc[i]
                return self.v.iloc[i] / avg if avg else np.nan

            elif op == "adx":
                return self._adx(comp["period"]).iloc[i]

            elif op == "adx_slope":
                a = self._adx(comp["period"])
                offset = comp["offset"]
                return a.iloc[i] - a.iloc[i - offset] if i >= offset else np.nan

            elif op == "di_spread":
                return self._diplus(comp["period"]).iloc[i] - self._diminus(comp["period"]).iloc[i]

            elif op == "stochastic":
                return self._stoch(comp["period"]).iloc[i]

            elif op == "cci":
                return self._cci(comp["period"]).iloc[i]

            elif op == "bop":
                return self._bop(comp["period"]).iloc[i]

            elif op == "range_position":
                p = comp["period"]
                maxh = self._maxh(p).iloc[i]
                minl = self._minl(p).iloc[i]
                rng = maxh - minl
                return (self.c.iloc[i] - minl) / rng if rng > 0 else np.nan

            elif op == "pullback":
                maxh = self._maxh(comp["period"]).iloc[i]
                norm = self._normalizer(comp["normalizer"])
                return (maxh - self.c.iloc[i]) / norm if norm else np.nan

            elif op == "range_width":
                p = comp["period"]
                rng = self._maxh(p).iloc[i] - self._minl(p).iloc[i]
                norm = self._normalizer(comp["normalizer"])
                return rng / norm if norm else np.nan

            elif op == "channel_slope":
                p = comp["period"]
                maxh = self._maxh(p)
                norm = self._normalizer(comp["normalizer"])
                val = maxh.iloc[i] - maxh.iloc[i - p] if i >= p else np.nan
                return val / norm if norm else np.nan

            elif op == "candle_range_ratio":
                rng = self.h.iloc[i] - self.l.iloc[i]
                atr_val = self._atr(14).iloc[i]
                return rng / atr_val if atr_val else np.nan

            elif op == "body_range_ratio":
                rng = self.h.iloc[i] - self.l.iloc[i]
                body = abs(self.c.iloc[i] - self.o.iloc[i])
                return body / rng if rng > 0 else np.nan

            elif op == "upper_wick_ratio":
                rng = self.h.iloc[i] - self.l.iloc[i]
                upper = self.h.iloc[i] - max(self.c.iloc[i], self.o.iloc[i])
                return upper / rng if rng > 0 else np.nan

            elif op == "extension_slope":
                ma = self._ma(comp["ma"])
                offset = comp["offset"]
                norm = self._normalizer(comp["normalizer"])
                if i < offset:
                    return np.nan
                ext_now = self.c.iloc[i] - ma.iloc[i]
                ext_prev = self.c.iloc[i - offset] - ma.iloc[i - offset]
                return (ext_now - ext_prev) / norm if norm else np.nan

            elif op == "extension_peak_ratio":
                ma = self._ma(comp["ma"])
                lb = comp["lookback"]
                ext_now = self.c.iloc[i] - ma.iloc[i]
                start = max(0, i - lb + 1)
                ext_series = self.c.iloc[start:i+1] - ma.iloc[start:i+1]
                max_ext = ext_series.max()
                return ext_now / max_ext if max_ext > 0 else np.nan

            elif op == "extension_ceiling_ratio":
                # Current extension / max extension in lookback = how close to statistical ceiling
                # 1.0 = at ceiling, 0.5 = halfway, >1.0 = new high extension
                ma = self._ma(comp["ma"])
                lb = comp["lookback"]
                ext_now = (self.c.iloc[i] - ma.iloc[i])
                norm = self._normalizer(comp["normalizer"])
                if not norm:
                    return np.nan
                ext_now_norm = ext_now / norm
                start = max(0, i - lb + 1)
                ext_series = (self.c.iloc[start:i+1] - ma.iloc[start:i+1]) / norm
                max_ext = ext_series.max()
                return ext_now_norm / max_ext if max_ext > 0 else np.nan

            elif op == "ext_adr_multiples":
                # Extension from MA in multiples of ADR — the core ta_knowledge metric
                ma_val = self._ma(comp["ma"]).iloc[i]
                adr_val = self._adr(14).iloc[i]
                return (self.c.iloc[i] - ma_val) / adr_val if adr_val and adr_val > 0 else np.nan

            elif op == "ma_cross_count":
                # How many times price crossed this MA in last N bars (high = choppy/stage3)
                ma = self._ma(comp["ma"])
                p = comp["period"]
                if i < p:
                    return np.nan
                above = self.c.iloc[i-p+1:i+1] > ma.iloc[i-p+1:i+1]
                crosses = (above != above.shift(1)).sum()
                return float(crosses)

            elif op == "bars_since_ma_cross":
                # Bars since price last crossed this MA
                ma = self._ma(comp["ma"])
                max_lb = comp.get("max_lookback", 120)
                above_now = self.c.iloc[i] > ma.iloc[i]
                for back in range(1, min(max_lb, i + 1)):
                    was_above = self.c.iloc[i - back] > ma.iloc[i - back]
                    if was_above != above_now:
                        return float(back)
                return float(max_lb)

            elif op == "ma_undercut_depth":
                # Max depth below MA in last N bars, in ATR
                # Measures severity of recent correction relative to this MA
                ma = self._ma(comp["ma"])
                p = comp["period"]
                if i < p:
                    return np.nan
                norm = self._normalizer(comp["normalizer"])
                if not norm:
                    return np.nan
                diffs = self.l.iloc[i-p+1:i+1] - ma.iloc[i-p+1:i+1]
                min_diff = diffs.min()
                return min_diff / norm  # negative = below MA

            elif op == "swing_high_count":
                # Count of swing highs (H > H[-1] and H > H[+1]) in last N bars
                p = comp["period"]
                if i < p + 1:
                    return np.nan
                count = 0
                for j in range(i - p + 2, i):  # exclude endpoints
                    if self.h.iloc[j] > self.h.iloc[j-1] and self.h.iloc[j] > self.h.iloc[j+1]:
                        count += 1
                return float(count)

            elif op == "swing_low_count":
                p = comp["period"]
                if i < p + 1:
                    return np.nan
                count = 0
                for j in range(i - p + 2, i):
                    if self.l.iloc[j] < self.l.iloc[j-1] and self.l.iloc[j] < self.l.iloc[j+1]:
                        count += 1
                return float(count)

            elif op == "higher_high_count":
                # Count of consecutive higher swing highs looking back
                p = comp["period"]
                if i < p + 1:
                    return np.nan
                # Find swing highs
                highs = []
                for j in range(i - p + 2, i):
                    if self.h.iloc[j] > self.h.iloc[j-1] and self.h.iloc[j] > self.h.iloc[j+1]:
                        highs.append(self.h.iloc[j])
                if len(highs) < 2:
                    return 0.0
                count = 0
                for k in range(len(highs) - 1, 0, -1):
                    if highs[k] > highs[k-1]:
                        count += 1
                    else:
                        break
                return float(count)

            elif op == "higher_low_count":
                p = comp["period"]
                if i < p + 1:
                    return np.nan
                lows = []
                for j in range(i - p + 2, i):
                    if self.l.iloc[j] < self.l.iloc[j-1] and self.l.iloc[j] < self.l.iloc[j+1]:
                        lows.append(self.l.iloc[j])
                if len(lows) < 2:
                    return 0.0
                count = 0
                for k in range(len(lows) - 1, 0, -1):
                    if lows[k] > lows[k-1]:
                        count += 1
                    else:
                        break
                return float(count)

            elif op == "lower_high_count":
                p = comp["period"]
                if i < p + 1:
                    return np.nan
                highs = []
                for j in range(i - p + 2, i):
                    if self.h.iloc[j] > self.h.iloc[j-1] and self.h.iloc[j] > self.h.iloc[j+1]:
                        highs.append(self.h.iloc[j])
                if len(highs) < 2:
                    return 0.0
                count = 0
                for k in range(len(highs) - 1, 0, -1):
                    if highs[k] < highs[k-1]:
                        count += 1
                    else:
                        break
                return float(count)

            elif op == "lower_low_count":
                p = comp["period"]
                if i < p + 1:
                    return np.nan
                lows = []
                for j in range(i - p + 2, i):
                    if self.l.iloc[j] < self.l.iloc[j-1] and self.l.iloc[j] < self.l.iloc[j+1]:
                        lows.append(self.l.iloc[j])
                if len(lows) < 2:
                    return 0.0
                count = 0
                for k in range(len(lows) - 1, 0, -1):
                    if lows[k] < lows[k-1]:
                        count += 1
                    else:
                        break
                return float(count)

            elif op == "retracement_level":
                # Where is current price as % retracement of the last N-bar move
                # 0 = at the low, 1 = at the high
                p = comp["period"]
                if i < p:
                    return np.nan
                maxh = self.h.iloc[i-p+1:i+1].max()
                minl = self.l.iloc[i-p+1:i+1].min()
                rng = maxh - minl
                return (self.c.iloc[i] - minl) / rng if rng > 0 else np.nan

            elif op == "gap_size":
                # Today's gap as ATR multiple (positive = gap up, negative = gap down)
                if i < 1:
                    return np.nan
                gap = self.o.iloc[i] - self.c.iloc[i-1]
                norm = self._normalizer(comp["normalizer"])
                return gap / norm if norm else np.nan

            elif op == "gap_count":
                # Count of gaps > threshold ATR in last N bars
                p = comp["period"]
                threshold = comp.get("threshold", 0.5)
                if i < p:
                    return np.nan
                atr_s = self._atr(14)
                count = 0
                for j in range(i - p + 1, i + 1):
                    if j < 1:
                        continue
                    gap = abs(self.o.iloc[j] - self.c.iloc[j-1])
                    if atr_s.iloc[j] > 0 and gap / atr_s.iloc[j] > threshold:
                        count += 1
                return float(count)

            elif op == "unfilled_gap_up_count":
                # Count of unfilled gap-ups in last N bars
                # Gap-up = open > prior high. Unfilled = low never came back to prior high
                p = comp["period"]
                if i < p:
                    return np.nan
                count = 0
                for j in range(i - p + 1, i + 1):
                    if j < 1:
                        continue
                    if self.o.iloc[j] > self.h.iloc[j-1]:
                        # Check if any subsequent bar filled it
                        filled = False
                        for k in range(j, i + 1):
                            if self.l.iloc[k] <= self.h.iloc[j-1]:
                                filled = True
                                break
                        if not filled:
                            count += 1
                return float(count)

            elif op == "consecutive_up_roc":
                # Cumulative ROC over consecutive up-close days ending at target
                cum = 0.0
                j = i
                while j > 0 and self.c.iloc[j] > self.c.iloc[j-1]:
                    cum += (self.c.iloc[j] / self.c.iloc[j-1] - 1) * 100
                    j -= 1
                return cum

            elif op == "consecutive_down_roc":
                cum = 0.0
                j = i
                while j > 0 and self.c.iloc[j] < self.c.iloc[j-1]:
                    cum += (self.c.iloc[j] / self.c.iloc[j-1] - 1) * 100
                    j -= 1
                return cum  # will be negative

            elif op == "consecutive_up_days":
                count = 0
                j = i
                while j > 0 and self.c.iloc[j] > self.c.iloc[j-1]:
                    count += 1
                    j -= 1
                return float(count)

            elif op == "consecutive_down_days":
                count = 0
                j = i
                while j > 0 and self.c.iloc[j] < self.c.iloc[j-1]:
                    count += 1
                    j -= 1
                return float(count)

            elif op == "inside_bar_count":
                # Inside bar = H < H[-1] and L > L[-1]
                p = comp["period"]
                if i < p:
                    return np.nan
                count = 0
                for j in range(i - p + 1, i + 1):
                    if j < 1:
                        continue
                    if self.h.iloc[j] < self.h.iloc[j-1] and self.l.iloc[j] > self.l.iloc[j-1]:
                        count += 1
                return float(count)

            elif op == "outside_bar_count":
                # Outside bar = H > H[-1] and L < L[-1]
                p = comp["period"]
                if i < p:
                    return np.nan
                count = 0
                for j in range(i - p + 1, i + 1):
                    if j < 1:
                        continue
                    if self.h.iloc[j] > self.h.iloc[j-1] and self.l.iloc[j] < self.l.iloc[j-1]:
                        count += 1
                return float(count)

            elif op == "nr_ratio":
                # NR7-style: today's range / max range of last N bars
                # Low value = range compression / squeeze
                p = comp["period"]
                if i < p:
                    return np.nan
                today_range = self.h.iloc[i] - self.l.iloc[i]
                max_range = max(self.h.iloc[j] - self.l.iloc[j] for j in range(i - p + 1, i + 1))
                return today_range / max_range if max_range > 0 else np.nan

            elif op == "obv_slope":
                # OBV slope over N bars, normalized by avg volume
                o = self._obv()
                offset = comp["offset"]
                if i < offset:
                    return np.nan
                avg_vol = self._ma(f"avgv{comp.get('vol_period', 20)}").iloc[i]
                if not avg_vol or avg_vol <= 0:
                    return np.nan
                return (o.iloc[i] - o.iloc[i - offset]) / (avg_vol * offset)

            elif op == "up_volume_ratio":
                # Sum of volume on up days / total volume over N bars
                p = comp["period"]
                if i < p:
                    return np.nan
                up_vol = 0.0
                total_vol = 0.0
                for j in range(i - p + 1, i + 1):
                    vol_j = self.v.iloc[j]
                    total_vol += vol_j
                    if self.c.iloc[j] > self.o.iloc[j]:
                        up_vol += vol_j
                return up_vol / total_vol if total_vol > 0 else np.nan

            elif op == "cmf":
                return self._cmf(comp["period"]).iloc[i]

            elif op == "cmf_slope":
                c = self._cmf(comp["period"])
                offset = comp["offset"]
                return c.iloc[i] - c.iloc[i - offset] if i >= offset else np.nan

            elif op == "bollinger_pctb":
                # %B = (price - lower band) / (upper - lower)
                # >1 = above upper band, <0 = below lower band
                p = comp["period"]
                top = self._bbtop(p).iloc[i]
                bot = self._bbbot(p).iloc[i]
                bw = top - bot
                return (self.c.iloc[i] - bot) / bw if bw > 0 else np.nan

            elif op == "bollinger_bandwidth":
                # Bandwidth = (upper - lower) / middle — squeeze indicator
                p = comp["period"]
                top = self._bbtop(p).iloc[i]
                bot = self._bbbot(p).iloc[i]
                mid = self._ma(f"avgc{p}").iloc[i]
                return (top - bot) / mid if mid > 0 else np.nan

            elif op == "bollinger_bandwidth_rank":
                # Where is current bandwidth relative to its own range over lookback
                p = comp["period"]
                lb = comp["lookback"]
                if i < lb:
                    return np.nan
                top_s = self._bbtop(p)
                bot_s = self._bbbot(p)
                mid_s = self._ma(f"avgc{p}")
                bw_now = (top_s.iloc[i] - bot_s.iloc[i]) / mid_s.iloc[i] if mid_s.iloc[i] > 0 else np.nan
                if np.isnan(bw_now):
                    return np.nan
                bws = []
                for j in range(i - lb + 1, i + 1):
                    m = mid_s.iloc[j]
                    if m > 0:
                        bws.append((top_s.iloc[j] - bot_s.iloc[j]) / m)
                if not bws:
                    return np.nan
                return (bw_now - min(bws)) / (max(bws) - min(bws)) if max(bws) > min(bws) else 0.5

            elif op == "macd_histogram":
                fast = comp.get("fast", 12)
                slow = comp.get("slow", 26)
                signal_p = comp.get("signal", 9)
                macd_line = self._macd(fast, slow)
                signal_line = ema(macd_line, signal_p)
                return macd_line.iloc[i] - signal_line.iloc[i]

            elif op == "macd_histogram_slope":
                fast = comp.get("fast", 12)
                slow = comp.get("slow", 26)
                signal_p = comp.get("signal", 9)
                offset = comp["offset"]
                macd_line = self._macd(fast, slow)
                signal_line = ema(macd_line, signal_p)
                hist = macd_line - signal_line
                return hist.iloc[i] - hist.iloc[i - offset] if i >= offset else np.nan

            elif op == "macd_line_norm":
                fast = comp.get("fast", 12)
                slow = comp.get("slow", 26)
                norm = self._normalizer(comp["normalizer"])
                return self._macd(fast, slow).iloc[i] / norm if norm else np.nan

            elif op == "aroon_oscillator":
                p = comp["period"]
                return self._aroon_up(p).iloc[i] - self._aroon_down(p).iloc[i]

            elif op == "aroon_up_val":
                return self._aroon_up(comp["period"]).iloc[i]

            elif op == "aroon_down_val":
                return self._aroon_down(comp["period"]).iloc[i]

            elif op == "kaufman_efficiency_ratio":
                return self._kaufman_eff(comp["period"]).iloc[i]

            elif op == "ma_stack_score":
                # How bullishly stacked are the MAs? Count of ordered pairs
                # Full bull stack (8>21>50>200) = 6, full bear = 0
                vals = []
                for ma_name in comp["mas"]:
                    vals.append(self._ma(ma_name).iloc[i])
                score = 0
                for a in range(len(vals)):
                    for b in range(a + 1, len(vals)):
                        if vals[a] > vals[b]:
                            score += 1
                return float(score)

            elif op == "range_contraction_ratio":
                # Current N-bar range width / prior N-bar range width
                # <1 = contracting (squeeze), >1 = expanding
                p = comp["period"]
                if i < 2 * p:
                    return np.nan
                curr_width = self.h.iloc[i-p+1:i+1].max() - self.l.iloc[i-p+1:i+1].min()
                prev_width = self.h.iloc[i-2*p+1:i-p+1].max() - self.l.iloc[i-2*p+1:i-p+1].min()
                return curr_width / prev_width if prev_width > 0 else np.nan

            elif op == "atr_ratio":
                # Current ATR / ATR N bars ago — volatility expansion/contraction
                p1 = comp["period"]
                offset = comp["offset"]
                atr_s = self._atr(p1)
                if i < offset:
                    return np.nan
                prev = atr_s.iloc[i - offset]
                return atr_s.iloc[i] / prev if prev > 0 else np.nan

            elif op == "vwap_distance":
                # Distance to rolling VWAP over N bars
                p = comp["period"]
                if i < p:
                    return np.nan
                tp = (self.h.iloc[i-p+1:i+1] + self.l.iloc[i-p+1:i+1] + self.c.iloc[i-p+1:i+1]) / 3
                vol = self.v.iloc[i-p+1:i+1]
                cum_vol = vol.sum()
                if cum_vol <= 0:
                    return np.nan
                vwap_val = (tp * vol).sum() / cum_vol
                norm = self._normalizer(comp["normalizer"])
                return (self.c.iloc[i] - vwap_val) / norm if norm else np.nan

            elif op == "close_vs_open_ratio":
                # Over N bars, what fraction close above open (bullish bars)
                p = comp["period"]
                if i < p:
                    return np.nan
                bullish = sum(1 for j in range(i-p+1, i+1) if self.c.iloc[j] > self.o.iloc[j])
                return bullish / p

            elif op == "lower_wick_ratio":
                rng = self.h.iloc[i] - self.l.iloc[i]
                lower = min(self.c.iloc[i], self.o.iloc[i]) - self.l.iloc[i]
                return lower / rng if rng > 0 else np.nan

            elif op == "avg_candle_body_ratio":
                # Average body/range ratio over N bars — trend clarity
                p = comp["period"]
                if i < p:
                    return np.nan
                total = 0.0
                valid = 0
                for j in range(i - p + 1, i + 1):
                    rng = self.h.iloc[j] - self.l.iloc[j]
                    if rng > 0:
                        total += abs(self.c.iloc[j] - self.o.iloc[j]) / rng
                        valid += 1
                return total / valid if valid > 0 else np.nan

            elif op == "high_volume_bar_pct":
                # Pct of bars in last N with volume > X times average
                p = comp["period"]
                mult = comp.get("multiplier", 1.5)
                if i < p:
                    return np.nan
                avg_v = self._ma(f"avgv{comp.get('avg_period', 50)}").iloc[i]
                if not avg_v or avg_v <= 0:
                    return np.nan
                count = sum(1 for j in range(i-p+1, i+1) if self.v.iloc[j] > mult * avg_v)
                return count / p

            elif op == "lsp_distance":
                # Distance from price_ref to LSP price, normalized
                # Positive = LSP above price (approaching from below)
                # Negative = price above LSP (breakout failure variant)
                if self._lsp is None:
                    return np.nan
                lsp_price = self._lsp["price"]
                price = self.c.iloc[i] if comp.get("price_ref", "C") == "C" else self.h.iloc[i]
                norm = self._normalizer(comp["normalizer"])
                return (lsp_price - price) / norm if norm else np.nan

            elif op == "lsp_bounce_recovery":
                # How far has bounce retraced from post-LSP low back toward LSP
                # 0.0 = price at post-LSP low, 1.0 = price back at LSP
                if self._lsp is None:
                    return np.nan
                lsp_price = self._lsp["price"]
                lsp_bars_back = self._lsp["bars_lookback"]
                lsp_idx = i - lsp_bars_back
                if lsp_idx < 0:
                    return np.nan
                # Post-LSP low: minimum low from LSP bar to scan bar
                post_lsp_low = self.l.iloc[lsp_idx:i + 1].min()
                rng = lsp_price - post_lsp_low
                if rng <= 0:
                    return np.nan
                return (self.c.iloc[i] - post_lsp_low) / rng

            elif op == "lsp_right_peak_ratio":
                # Current high / LSP price — how close is right peak to left peak
                if self._lsp is None:
                    return np.nan
                lsp_price = self._lsp["price"]
                return self.h.iloc[i] / lsp_price if lsp_price > 0 else np.nan

            elif op == "lsp_volume_ratio":
                # Recent avg volume / volume at LSP bar
                # Low ratio = volume drying up on second approach (bearish)
                if self._lsp is None:
                    return np.nan
                lsp_bars_back = self._lsp["bars_lookback"]
                lsp_idx = i - lsp_bars_back
                if lsp_idx < 0 or lsp_idx >= len(self.v):
                    return np.nan
                lsp_vol = self.v.iloc[lsp_idx]
                if lsp_vol <= 0:
                    return np.nan
                avg_period = comp.get("avg_period", 5)
                recent_start = max(0, i - avg_period + 1)
                recent_avg = self.v.iloc[recent_start:i + 1].mean()
                return recent_avg / lsp_vol

            elif op == "avwap_lsp_distance":
                # AVWAP anchored at LSP bar — distance from scan close to AVWAP
                # Negative = price below AVWAP (trapped buyers — short fuel)
                # Positive = price above AVWAP
                if self._lsp is None:
                    return np.nan
                lsp_bars_back = self._lsp["bars_lookback"]
                lsp_idx = i - lsp_bars_back
                if lsp_idx < 0:
                    return np.nan
                # AVWAP = cumsum(typical_price * volume) / cumsum(volume) from LSP bar
                segment = self.df.iloc[lsp_idx:i + 1]
                tp = (segment["high"] + segment["low"] + segment["close"]) / 3
                vol = segment["volume"]
                cum_vol = vol.cumsum()
                if cum_vol.iloc[-1] <= 0:
                    return np.nan
                avwap = (tp * vol).cumsum() / cum_vol
                avwap_val = avwap.iloc[-1]
                norm = self._normalizer(comp["normalizer"])
                return (self.c.iloc[i] - avwap_val) / norm if norm else np.nan

            elif op == "lsp_bars_back":
                if self._lsp is None:
                    return np.nan
                return float(self._lsp["bars_lookback"])

            elif op == "lsp_prominence":
                if self._lsp is None:
                    return np.nan
                return float(self._lsp.get("prominence_score", np.nan))

            elif op == "lsp_pullback_depth":
                # How deep was the pullback after LSP in ATR units
                if self._lsp is None:
                    return np.nan
                return float(self._lsp.get("pullback_depth_atr", np.nan))

            elif op == "count_true":
                b = self._bool_series(comp["condition"])
                result = count_true(b, comp["period"])
                return result.iloc[i]

            elif op == "since_true":
                b = self._bool_series(comp["condition"])
                result = since_true(b, comp["period"])
                return result.iloc[i]

            elif op == "true_in_row":
                b = self._bool_series(comp["condition"])
                result = true_in_row(b, comp["period"])
                return result.iloc[i]

            else:
                raise ValueError(f"Unknown op: {op}")

        except (IndexError, KeyError, ZeroDivisionError):
            return np.nan
