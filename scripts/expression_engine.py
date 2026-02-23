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
        """Evaluate a named boolean condition across the full series."""
        def _compute():
            c, o, h, l, v = self.c, self.o, self.h, self.l, self.v
            mapping = {
                "c_gt_xavgc8":       c > self._ma("xavgc8"),
                "c_gt_xavgc21":      c > self._ma("xavgc21"),
                "c_gt_xavgc50":      c > self._ma("xavgc50"),
                "c_gt_xavgc100":     c > self._ma("xavgc100"),
                "c_gt_avgc50":       c > self._ma("avgc50"),
                "c_gt_avgc200":      c > self._ma("avgc200"),
                "c_gt_c1":           c > c.shift(1),
                "c_lt_c1":           c < c.shift(1),
                "h_gt_h1":           h > h.shift(1),
                "l_lt_l1":           l < l.shift(1),
                "v_gt_avgv20":       v > self._ma("avgv20"),
                "v_gt_2x_avgv20":    v > 2 * self._ma("avgv20"),
                "c_gt_o":            c > o,
                "xavgc8_gt_xavgc21": self._ma("xavgc8") > self._ma("xavgc21"),
                "xavgc50_gt_xavgc200": self._ma("xavgc50") > self._ma("xavgc200"),
                "avgc50_gt_avgc200": self._ma("avgc50") > self._ma("avgc200"),
                "avgc50_rising":     self._ma("avgc50") > self._ma("avgc50").shift(1),
                "avgc200_rising":    self._ma("avgc200") > self._ma("avgc200").shift(1),
                "xavgc50_rising":    self._ma("xavgc50") > self._ma("xavgc50").shift(1),
                "h_gt_maxh5_1":      h > self._maxh(5).shift(1),
                "l_lt_minl5_1":      l < self._minl(5).shift(1),
                "c_gt_maxc10_1":     c > rolling_max(c, 10).shift(1),
                "range_gt_atr":      (h - l) > self._atr(14),
                "body_gt_half_range": abs(c - o) > 0.5 * (h - l),
                "c_upper_half":      c > (h + l) / 2,
                "c_lower_half":      c < (h + l) / 2,
                "diplus_gt_diminus": self._diplus(14) > self._diminus(14),
                "rsi14_gt_50":       self._rsi(14) > 50,
                "rsi14_gt_70":       self._rsi(14) > 70,
                "rsi14_lt_30":       self._rsi(14) < 30,
                "adx14_gt_25":       self._adx(14) > 25,
                "c_gt_bbtop":        c > (self._ma("avgc20") + 2 * self.c.rolling(20).std()),
                "c_lt_bbbot":        c < (self._ma("avgc20") - 2 * self.c.rolling(20).std()),
            }
            if cond_name not in mapping:
                raise ValueError(f"Unknown boolean condition: {cond_name}")
            return mapping[cond_name].astype(bool)
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
