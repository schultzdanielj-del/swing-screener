"""
Post-Signal Expression Compute Engine

Computes exit expression series for a single example's forward path.
Each expression is evaluated at every bar after the signal, producing
a numpy array indexed by forward bar offset.

Usage:
    engine = ExitExprEngine(df, entry_idx, direction='short')
    series = engine.compute(expr_compute_spec)
    # series[0] = value at entry bar, series[1] = value at bar+1, etc.

The engine reuses ExpressionEngine's indicator cache for MAs, RSI, etc.
and adds post-signal-specific computations (move captured, MFE, reclaims).
"""

import numpy as np
import pandas as pd
from scripts.expression_engine import ExpressionEngine
from scripts.profiling_engine import (
    sma, ema, hma, rolling_max, rolling_min, rolling_sum,
    atr, rsi, stochastic_k, cci, adx, di_plus, di_minus,
    bop, obv, macd, bollinger_top, bollinger_bot, stddev,
)


class ExitExprEngine:
    """Compute post-signal expression series for an example's forward path.
    
    Args:
        df: Full OHLCV DataFrame for the ticker (needs history before entry for MAs)
        entry_idx: Index of the entry bar in df
        direction: 'short' or 'long'
        spy_df: Optional SPY DataFrame aligned by date for relative strength
        max_forward: Maximum bars forward to compute (None = all available)
    """
    
    def __init__(self, df, entry_idx, direction='short', spy_df=None, max_forward=None):
        self.df = df
        self.entry_idx = entry_idx
        self.direction = direction
        self.spy_df = spy_df
        self.sign = -1.0 if direction == 'short' else 1.0
        
        # Forward slice bounds
        last_idx = len(df) - 1
        if max_forward is not None:
            last_idx = min(entry_idx + max_forward, last_idx)
        self.n_forward = last_idx - entry_idx + 1  # includes entry bar
        
        if self.n_forward < 2:
            raise ValueError(f"Not enough forward bars: {self.n_forward}")
        
        # Use ExpressionEngine for indicator computations
        # We give it the full df so MAs have full history
        self._base = ExpressionEngine(df)
        
        # Pre-extract key arrays (full df length)
        self.c = df["close"].values
        self.o = df["open"].values
        self.h = df["high"].values
        self.l = df["low"].values
        self.v = df["volume"].values
        
        # Entry bar reference values
        self.entry_high = self.h[entry_idx]
        self.entry_low = self.l[entry_idx]
        self.entry_close = self.c[entry_idx]
        self.entry_vol = self.v[entry_idx]
        
        # Forward slice indices (absolute indices into df)
        self.fwd_start = entry_idx
        self.fwd_end = entry_idx + self.n_forward
        
        # Pre-compute normalizers at each forward bar
        self._adr14 = self._get_normalizer_array("adr14")
        self._atr14 = self._get_normalizer_array("atr14")
        
        # Pre-compute MFE (running best move for the direction)
        self._mfe_close = self._compute_mfe("close")
        self._mfe_low = self._compute_mfe("low")  # low for shorts, high for longs
        
        # Cache
        self._cache = {}
    
    def _get_normalizer_array(self, norm):
        """Get normalizer values for forward bars."""
        if norm == "adr14":
            series = self._base._adr(14).values
        elif norm == "atr14":
            series = self._base._atr(14).values
        else:
            raise ValueError(f"Unknown normalizer: {norm}")
        result = series[self.fwd_start:self.fwd_end].copy()
        result[result == 0] = np.nan
        return result
    
    def _get_norm(self, norm):
        """Get pre-computed normalizer array."""
        if norm == "adr14":
            return self._adr14
        elif norm == "atr14":
            return self._atr14
        else:
            raise ValueError(f"Unknown normalizer: {norm}")
    
    def _compute_mfe(self, price_ref):
        """Compute running Maximum Favorable Excursion from entry high.
        
        For shorts: entry_high - running_min(price) (want price to go down)
        For longs: running_max(price) - entry_low (want price to go up)
        """
        if price_ref == "close":
            prices = self.c[self.fwd_start:self.fwd_end]
        elif price_ref == "low":
            if self.direction == 'short':
                prices = self.l[self.fwd_start:self.fwd_end]
            else:
                prices = self.h[self.fwd_start:self.fwd_end]
        else:
            prices = self.c[self.fwd_start:self.fwd_end]
        
        if self.direction == 'short':
            running_extreme = np.minimum.accumulate(prices)
            mfe = self.entry_high - running_extreme
        else:
            running_extreme = np.maximum.accumulate(prices)
            mfe = running_extreme - self.entry_low
        
        return mfe
    
    def _fwd(self, full_array):
        """Slice a full-df-length array to forward bars."""
        return full_array[self.fwd_start:self.fwd_end]
    
    def _ma_values(self, ma_name):
        """Get MA series for forward bars."""
        key = f"ma_{ma_name}"
        if key not in self._cache:
            self._cache[key] = self._base._ma(ma_name).values[self.fwd_start:self.fwd_end]
        return self._cache[key]
    
    def _cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]
    
    def compute(self, comp):
        """Compute a post-signal expression series.
        
        Args:
            comp: dict with 'op' key and parameters
            
        Returns:
            numpy array of length n_forward (one value per forward bar)
        """
        # ──── OP NAME ALIASING ─────────────────────────────
        # Maps expression library op names → compute engine op names
        _OP_ALIASES = {
            "extension_ceiling_ratio": "ext_ceiling_ratio",
            "extension_slope": "ext_slope",
            "ext_acceleration": "ext_accel",
            "pct_green_bars": "pct_green_rolling",
            "avg_body_ratio": "avg_body_ratio_rolling",
            "avg_range_adr": "avg_bar_range_rolling",
            "avg_rvol": "avg_rvol_rolling",
            "up_vol_ratio": "up_vol_ratio_rolling",
            "obv_slope_exit": "obv_slope",
            "atr_ratio": "atr_ratio_vs_entry",
            "candle_range_ratio": "bar_range",
            "volume_ratio": "rvol",
            "below_signal_low": "below_signal_bar_low",
        }
        comp = dict(comp)  # copy to avoid mutating original
        op = _OP_ALIASES.get(comp["op"], comp["op"])
        comp["op"] = op
        
        # ──── PARAM ALIASING ──────────────────────────────
        # Fix parameter mismatches between expression lib and compute engine
        if op == "avg_rvol_rolling" and "avg_period" not in comp:
            comp["avg_period"] = 20
        if op == "avg_bar_range_rolling" and "normalizer" not in comp:
            comp["normalizer"] = "adr14"
        if op in ("pct_green_rolling", "avg_body_ratio_rolling", "avg_bar_range_rolling",
                   "up_vol_ratio_rolling", "obv_slope") and "window" not in comp:
            comp["window"] = comp.get("period", 10)
        if op == "rvol" and "avg_period" not in comp:
            comp["avg_period"] = comp.get("avg_period", 20)
        if op == "atr_ratio_vs_entry" and "window" not in comp:
            comp["window"] = comp.get("offset", 5)
        # General: map 'period' → 'window' for ops that expect 'window'
        if op in ("avg_rvol_rolling", "inside_bar_count", "mfe_expanding",
                   "rs_vs_spy", "rs_vs_spy_slope") and "window" not in comp:
            comp["window"] = comp.get("period", 10)
        
        # ──── MOVE CAPTURED ────────────────────────────────
        if op == "move_captured":
            pr = comp["price_ref"]
            norm_name = comp.get("normalizer", "adr14")
            if pr == "close":
                prices = self.c[self.fwd_start:self.fwd_end]
            else:
                prices = self.l[self.fwd_start:self.fwd_end] if self.direction == 'short' \
                    else self.h[self.fwd_start:self.fwd_end]
            if self.direction == 'short':
                raw = self.entry_high - prices
            else:
                raw = prices - self.entry_low
            if norm_name == "pct":
                ref_price = self.entry_high if self.direction == 'short' else self.entry_low
                return raw / ref_price * 100
            norm = self._get_norm(norm_name)
            return raw / norm
        
        elif op == "mfe":
            pr = comp.get("price_ref", "close")
            norm_name = comp.get("normalizer", "adr14")
            if norm_name == "pct":
                mfe = self._mfe_close if pr == "close" else self._mfe_low
                return mfe / self.entry_high * 100 if self.direction == 'short' else mfe / self.df["low"].iloc[self.entry_idx] * 100
            norm = self._get_norm(norm_name)
            mfe = self._mfe_close if pr == "close" else self._mfe_low
            return mfe / norm
        
        elif op == "capture_efficiency":
            # Current captured (close) / MFE (close), clamped [0, 1]
            if self.direction == 'short':
                captured = self.entry_high - self.c[self.fwd_start:self.fwd_end]
            else:
                captured = self.c[self.fwd_start:self.fwd_end] - self.entry_low
            mfe = self._mfe_close.copy()
            mfe[mfe <= 0] = np.nan
            return captured / mfe
        
        elif op == "move_pct":
            prices = self.c[self.fwd_start:self.fwd_end]
            if self.direction == 'short':
                return (self.entry_high - prices) / self.entry_high * 100
            else:
                return (prices - self.entry_low) / self.entry_low * 100
        
        # ──── EXTENSION FROM MA ────────────────────────────
        elif op == "extension":
            ma_vals = self._ma_values(comp["ma"])
            norm = self._get_norm(comp["normalizer"])
            prices = self.c[self.fwd_start:self.fwd_end]
            return (prices - ma_vals) / norm
        
        elif op == "ext_ceiling_ratio":
            ma_vals = self._ma_values(comp["ma"])
            norm = self._get_norm(comp["normalizer"])
            prices = self.c[self.fwd_start:self.fwd_end]
            ext_norm = (prices - ma_vals) / norm
            lb = comp["lookback"]
            # Use full history to get the ceiling
            full_ma = self._base._ma(comp["ma"]).values
            full_norm_vals = self._base._adr(14).values if comp["normalizer"] == "adr14" else self._base._atr(14).values
            full_norm_vals = np.where(full_norm_vals == 0, np.nan, full_norm_vals)
            full_ext = (self.c - full_ma) / full_norm_vals
            
            result = np.full(self.n_forward, np.nan)
            for i in range(self.n_forward):
                abs_idx = self.fwd_start + i
                start = max(0, abs_idx - lb)
                window = full_ext[start:abs_idx+1]
                if self.direction == 'short':
                    extreme = np.nanmin(window)  # most negative extension
                    result[i] = ext_norm[i] / extreme if extreme != 0 and not np.isnan(extreme) else np.nan
                else:
                    extreme = np.nanmax(window)
                    result[i] = ext_norm[i] / extreme if extreme != 0 and not np.isnan(extreme) else np.nan
            return result
        
        # ──── EXTENSION DYNAMICS ───────────────────────────
        elif op == "ext_slope":
            ma_vals = self._ma_values(comp["ma"])
            norm = self._get_norm(comp["normalizer"])
            prices = self.c[self.fwd_start:self.fwd_end]
            ext = (prices - ma_vals) / norm
            offset = comp["offset"]
            result = np.full(self.n_forward, np.nan)
            result[offset:] = ext[offset:] - ext[:-offset]
            return result
        
        elif op == "ext_retrace_from_peak":
            ma_vals = self._ma_values(comp["ma"])
            norm = self._get_norm(comp["normalizer"])
            prices = self.c[self.fwd_start:self.fwd_end]
            ext = (prices - ma_vals) / norm
            if self.direction == 'short':
                running_peak = np.minimum.accumulate(ext)  # most negative = peak for shorts
                retrace = ext - running_peak  # positive = giving back
            else:
                running_peak = np.maximum.accumulate(ext)
                retrace = running_peak - ext
            return retrace
        
        elif op == "ext_accel":
            ma_vals = self._ma_values(comp["ma"])
            norm = self._get_norm(comp["normalizer"])
            prices = self.c[self.fwd_start:self.fwd_end]
            ext = (prices - ma_vals) / norm
            slope = np.full(self.n_forward, np.nan)
            slope[1:] = ext[1:] - ext[:-1]
            accel = np.full(self.n_forward, np.nan)
            accel[2:] = slope[2:] - slope[1:-1]
            return accel
        
        # ──── MA RECLAIM ───────────────────────────────────
        elif op == "close_above_ma":
            ma_vals = self._ma_values(comp["ma"])
            prices = self.c[self.fwd_start:self.fwd_end]
            return (prices > ma_vals).astype(float)
        
        elif op == "bars_since_reclaim":
            ma_vals = self._ma_values(comp["ma"])
            prices = self.c[self.fwd_start:self.fwd_end]
            above = prices > ma_vals
            result = np.full(self.n_forward, np.nan)
            bars = 0
            reclaimed = False
            for i in range(self.n_forward):
                if above[i] and not reclaimed:
                    reclaimed = True
                    bars = 0
                if reclaimed:
                    result[i] = float(bars)
                    bars += 1
            return result
        
        elif op == "reclaim_then_lost":
            ma_vals = self._ma_values(comp["ma"])
            prices = self.c[self.fwd_start:self.fwd_end]
            above = prices > ma_vals
            result = np.zeros(self.n_forward)
            ever_reclaimed = False
            for i in range(self.n_forward):
                if above[i]:
                    ever_reclaimed = True
                elif ever_reclaimed:
                    result[i] = 1.0  # was above, now below again
            return result
        
        elif op == "distance_from_ma":
            ma_vals = self._ma_values(comp["ma"])
            norm = self._get_norm(comp["normalizer"])
            prices = self.c[self.fwd_start:self.fwd_end]
            return (prices - ma_vals) / norm
        
        elif op == "sequential_reclaim":
            fast_vals = self._ma_values(comp["ma_fast"])
            slow_vals = self._ma_values(comp["ma_slow"])
            prices = self.c[self.fwd_start:self.fwd_end]
            above_fast = prices > fast_vals
            above_slow = prices > slow_vals
            mode = comp.get("mode", "both")
            if mode == "both":
                return (above_fast & above_slow).astype(float)
            else:  # fast_only
                return (above_fast & ~above_slow).astype(float)
        
        # ──── MOMENTUM REVERSAL ────────────────────────────
        elif op == "rsi":
            return self._fwd(self._base._rsi(comp["period"]).values)
        
        elif op == "rsi_slope":
            vals = self._fwd(self._base._rsi(comp["period"]).values)
            offset = comp["offset"]
            result = np.full(self.n_forward, np.nan)
            result[offset:] = vals[offset:] - vals[:-offset]
            return result
        
        elif op == "rsi_above":
            vals = self._fwd(self._base._rsi(comp["period"]).values)
            return (vals > comp["threshold"]).astype(float)
        
        elif op == "roc":
            p = comp["period"]
            fwd_c = self.c[self.fwd_start:self.fwd_end]
            result = np.full(self.n_forward, np.nan)
            if p < self.n_forward:
                # Use absolute indices for lookback
                for i in range(self.n_forward):
                    abs_idx = self.fwd_start + i
                    if abs_idx >= p:
                        result[i] = (self.c[abs_idx] / self.c[abs_idx - p] - 1) * 100
            return result
        
        elif op == "macd_histogram":
            f, s, sig = comp["fast"], comp["slow"], comp["signal"]
            macd_line = self._base._macd(f, s).values
            signal_line = ema(pd.Series(macd_line), sig).values
            hist = macd_line - signal_line
            return hist[self.fwd_start:self.fwd_end]
        
        elif op == "macd_histogram_slope":
            f, s, sig = comp["fast"], comp["slow"], comp["signal"]
            macd_line = self._base._macd(f, s).values
            signal_line = ema(pd.Series(macd_line), sig).values
            hist = macd_line - signal_line
            fwd_hist = hist[self.fwd_start:self.fwd_end]
            result = np.full(self.n_forward, np.nan)
            result[1:] = fwd_hist[1:] - fwd_hist[:-1]
            return result
        
        elif op == "macd_hist_positive":
            f, s, sig = comp["fast"], comp["slow"], comp["signal"]
            macd_line = self._base._macd(f, s).values
            signal_line = ema(pd.Series(macd_line), sig).values
            hist = macd_line - signal_line
            return (hist[self.fwd_start:self.fwd_end] > 0).astype(float)
        
        elif op == "stochastic":
            return self._fwd(self._base._stoch(comp["period"]).values)
        
        elif op == "stoch_above":
            vals = self._fwd(self._base._stoch(comp["period"]).values)
            return (vals > comp["threshold"]).astype(float)
        
        elif op == "adx":
            return self._fwd(self._base._adx(comp["period"]).values)
        
        elif op == "adx_slope":
            vals = self._fwd(self._base._adx(comp["period"]).values)
            offset = comp["offset"]
            result = np.full(self.n_forward, np.nan)
            result[offset:] = vals[offset:] - vals[:-offset]
            return result
        
        elif op == "adx_declining":
            vals = self._fwd(self._base._adx(comp["period"]).values)
            result = np.zeros(self.n_forward)
            result[3:] = (vals[3:] < vals[:-3]).astype(float)
            return result
        
        elif op == "di_spread":
            dp = self._fwd(self._base._diplus(comp["period"]).values)
            dm = self._fwd(self._base._diminus(comp["period"]).values)
            return dp - dm
        
        # ──── CANDLE CHARACTER ─────────────────────────────
        elif op == "bar_range":
            norm = self._get_norm(comp["normalizer"])
            rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            return rng / norm
        
        elif op == "body_range_ratio":
            rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            body = np.abs(self.c[self.fwd_start:self.fwd_end] - self.o[self.fwd_start:self.fwd_end])
            rng_safe = np.where(rng == 0, np.nan, rng)
            return body / rng_safe
        
        elif op == "upper_wick_ratio":
            rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            oc_max = np.maximum(self.c[self.fwd_start:self.fwd_end], self.o[self.fwd_start:self.fwd_end])
            upper = self.h[self.fwd_start:self.fwd_end] - oc_max
            rng_safe = np.where(rng == 0, np.nan, rng)
            return upper / rng_safe
        
        elif op == "lower_wick_ratio":
            rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            oc_min = np.minimum(self.c[self.fwd_start:self.fwd_end], self.o[self.fwd_start:self.fwd_end])
            lower = oc_min - self.l[self.fwd_start:self.fwd_end]
            rng_safe = np.where(rng == 0, np.nan, rng)
            return lower / rng_safe
        
        elif op == "is_green":
            return (self.c[self.fwd_start:self.fwd_end] > self.o[self.fwd_start:self.fwd_end]).astype(float)
        
        elif op == "is_doji":
            rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            body = np.abs(self.c[self.fwd_start:self.fwd_end] - self.o[self.fwd_start:self.fwd_end])
            rng_safe = np.where(rng == 0, np.nan, rng)
            ratio = body / rng_safe
            return (ratio < 0.1).astype(float)
        
        elif op == "gap_from_prior":
            norm = self._get_norm(comp["normalizer"])
            fwd_o = self.o[self.fwd_start:self.fwd_end]
            # Prior close: shift back 1 in absolute terms
            prior_c = np.full(self.n_forward, np.nan)
            for i in range(self.n_forward):
                abs_idx = self.fwd_start + i
                if abs_idx > 0:
                    prior_c[i] = self.c[abs_idx - 1]
            return (fwd_o - prior_c) / norm
        
        elif op == "pct_green_rolling":
            w = comp["window"]
            green = (self.c[self.fwd_start:self.fwd_end] > self.o[self.fwd_start:self.fwd_end]).astype(float)
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                result[i] = np.mean(green[max(0,i-w+1):i+1])
            return result
        
        elif op == "avg_body_ratio_rolling":
            w = comp["window"]
            rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            body = np.abs(self.c[self.fwd_start:self.fwd_end] - self.o[self.fwd_start:self.fwd_end])
            rng_safe = np.where(rng == 0, np.nan, rng)
            ratio = body / rng_safe
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                result[i] = np.nanmean(ratio[max(0,i-w+1):i+1])
            return result
        
        elif op == "avg_bar_range_rolling":
            w = comp["window"]
            norm = self._get_norm(comp["normalizer"])
            rng = (self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]) / norm
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                result[i] = np.nanmean(rng[max(0,i-w+1):i+1])
            return result
        
        elif op == "consecutive_green":
            green = self.c[self.fwd_start:self.fwd_end] > self.o[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            for i in range(self.n_forward):
                if green[i]:
                    result[i] = result[i-1] + 1 if i > 0 else 1
            return result
        
        elif op == "consecutive_red":
            red = self.c[self.fwd_start:self.fwd_end] < self.o[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            for i in range(self.n_forward):
                if red[i]:
                    result[i] = result[i-1] + 1 if i > 0 else 1
            return result
        
        # ──── VOLUME CHARACTER ─────────────────────────────
        elif op == "rvol":
            avg_p = comp["avg_period"]
            avg_vol = sma(pd.Series(self.v), avg_p).values
            avg_vol_safe = np.where(avg_vol == 0, np.nan, avg_vol)
            return self.v[self.fwd_start:self.fwd_end] / avg_vol_safe[self.fwd_start:self.fwd_end]
        
        elif op == "avg_rvol_rolling":
            w = comp["window"]
            avg_p = comp["avg_period"]
            avg_vol = sma(pd.Series(self.v), avg_p).values
            avg_vol_safe = np.where(avg_vol == 0, np.nan, avg_vol)
            rvol = self.v / avg_vol_safe
            fwd_rvol = rvol[self.fwd_start:self.fwd_end]
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                result[i] = np.nanmean(fwd_rvol[max(0,i-w+1):i+1])
            return result
        
        elif op == "up_vol_ratio_rolling":
            w = comp["window"]
            fwd_c = self.c[self.fwd_start:self.fwd_end]
            fwd_o = self.o[self.fwd_start:self.fwd_end]
            fwd_v = self.v[self.fwd_start:self.fwd_end]
            up = fwd_c > fwd_o
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                s = max(0, i-w+1)
                total = np.sum(fwd_v[s:i+1])
                if total > 0:
                    result[i] = np.sum(fwd_v[s:i+1][up[s:i+1]]) / total
            return result
        
        elif op == "down_vol_ratio_rolling":
            w = comp["window"]
            fwd_c = self.c[self.fwd_start:self.fwd_end]
            fwd_o = self.o[self.fwd_start:self.fwd_end]
            fwd_v = self.v[self.fwd_start:self.fwd_end]
            down = fwd_c < fwd_o
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                s = max(0, i-w+1)
                total = np.sum(fwd_v[s:i+1])
                if total > 0:
                    result[i] = np.sum(fwd_v[s:i+1][down[s:i+1]]) / total
            return result
        
        elif op == "vol_trend_rolling":
            w = comp["window"]
            fwd_v = self.v[self.fwd_start:self.fwd_end].astype(float)
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                s = max(0, i-w+1)
                seg = fwd_v[s:i+1]
                if len(seg) >= 2 and seg[0] > 0:
                    result[i] = seg[-1] / seg[0] - 1  # simple ratio
            return result
        
        elif op == "obv_slope":
            w = comp["window"]
            full_obv = self._base._obv().values
            fwd_obv = full_obv[self.fwd_start:self.fwd_end]
            result = np.full(self.n_forward, np.nan)
            for i in range(w, self.n_forward):
                result[i] = fwd_obv[i] - fwd_obv[i-w]
            return result
        
        elif op == "vol_vs_signal_bar":
            fwd_v = self.v[self.fwd_start:self.fwd_end]
            if self.entry_vol > 0:
                return fwd_v / self.entry_vol
            return np.full(self.n_forward, np.nan)
        
        elif op == "vol_rank_post_signal":
            fwd_v = self.v[self.fwd_start:self.fwd_end].astype(float)
            result = np.full(self.n_forward, np.nan)
            for i in range(1, self.n_forward):
                window = fwd_v[:i+1]
                result[i] = (window <= fwd_v[i]).sum() / len(window)
            return result
        
        # ──── STRUCTURAL ──────────────────────────────────
        elif op == "touched_ma":
            ma_vals = self._ma_values(comp["ma"])
            # For shorts: low touched below MA (price went through it)
            if self.direction == 'short':
                touched = self.l[self.fwd_start:self.fwd_end] <= ma_vals
            else:
                touched = self.h[self.fwd_start:self.fwd_end] >= ma_vals
            # Running: once touched, stays 1
            result = np.zeros(self.n_forward)
            for i in range(self.n_forward):
                if touched[i] or (i > 0 and result[i-1]):
                    result[i] = 1.0
            return result
        
        elif op == "closed_below_ma":
            ma_vals = self._ma_values(comp["ma"])
            prices = self.c[self.fwd_start:self.fwd_end]
            if self.direction == 'short':
                return (prices < ma_vals).astype(float)
            else:
                return (prices > ma_vals).astype(float)
        
        elif op == "bars_since_touch_ma":
            ma_vals = self._ma_values(comp["ma"])
            if self.direction == 'short':
                touched = self.l[self.fwd_start:self.fwd_end] <= ma_vals
            else:
                touched = self.h[self.fwd_start:self.fwd_end] >= ma_vals
            result = np.full(self.n_forward, np.nan)
            bars = 0
            first_touch = False
            for i in range(self.n_forward):
                if touched[i]:
                    first_touch = True
                    bars = 0
                if first_touch:
                    result[i] = float(bars)
                    bars += 1
            return result
        
        elif op == "new_low_count":
            p = comp["period"]
            fwd_l = self.l[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            for i in range(1, self.n_forward):
                s = max(0, i - p)
                if fwd_l[i] < np.min(fwd_l[s:i]):
                    result[i] = 1.0
            # Cumulative
            return np.cumsum(result)
        
        elif op == "new_high_count":
            p = comp["period"]
            fwd_h = self.h[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            for i in range(1, self.n_forward):
                s = max(0, i - p)
                if fwd_h[i] > np.max(fwd_h[s:i]):
                    result[i] = 1.0
            return np.cumsum(result)
        
        elif op == "lower_low_sequence":
            fwd_l = self.l[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            count = 0
            for i in range(1, self.n_forward):
                if fwd_l[i] < fwd_l[i-1]:
                    count += 1
                else:
                    count = 0
                result[i] = float(count)
            return result
        
        elif op == "higher_low_formed":
            fwd_l = self.l[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            running_min = fwd_l[0]
            for i in range(1, self.n_forward):
                if fwd_l[i] < running_min:
                    running_min = fwd_l[i]
                elif fwd_l[i] > running_min and i >= 2:
                    # Check if this is a higher low vs the running min
                    result[i] = 1.0
            return result
        
        elif op == "below_signal_bar_low":
            fwd_l = self.l[self.fwd_start:self.fwd_end]
            return (fwd_l < self.entry_low).astype(float)
        
        # ──── RANGE COMPRESSION ───────────────────────────
        elif op == "atr_ratio_vs_entry":
            w = comp["window"]
            fwd_rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            entry_range = self.h[self.entry_idx] - self.l[self.entry_idx]
            if entry_range <= 0:
                return np.full(self.n_forward, np.nan)
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                s = max(0, i-w+1)
                result[i] = np.mean(fwd_rng[s:i+1]) / entry_range
            return result
        
        elif op == "range_contracting":
            w = comp["window"]
            fwd_rng = self.h[self.fwd_start:self.fwd_end] - self.l[self.fwd_start:self.fwd_end]
            result = np.zeros(self.n_forward)
            for i in range(w, self.n_forward):
                recent = np.mean(fwd_rng[i-w+1:i+1])
                prior = np.mean(fwd_rng[max(0,i-2*w+1):i-w+1]) if i >= 2*w-1 else np.mean(fwd_rng[:i-w+1])
                if prior > 0:
                    result[i] = float(recent < prior)
            return result
        
        elif op == "inside_bar_count":
            w = comp.get("window", comp.get("period", 5))
            fwd_h = self.h[self.fwd_start:self.fwd_end]
            fwd_l = self.l[self.fwd_start:self.fwd_end]
            inside = np.zeros(self.n_forward)
            for i in range(1, self.n_forward):
                if fwd_h[i] <= fwd_h[i-1] and fwd_l[i] >= fwd_l[i-1]:
                    inside[i] = 1.0
            result = np.full(self.n_forward, np.nan)
            for i in range(w-1, self.n_forward):
                s = max(0, i-w+1)
                result[i] = np.sum(inside[s:i+1])
            return result
        
        elif op == "bollinger_bandwidth":
            p = comp["period"]
            top = self._fwd(self._base._bbtop(p).values)
            bot = self._fwd(self._base._bbbot(p).values)
            mid = self._fwd(self._base._ma(f"avgc{p}").values)
            mid_safe = np.where(mid == 0, np.nan, mid)
            return (top - bot) / mid_safe
        
        elif op == "bollinger_pctb":
            p = comp["period"]
            top = self._fwd(self._base._bbtop(p).values)
            bot = self._fwd(self._base._bbbot(p).values)
            bw = top - bot
            bw_safe = np.where(bw == 0, np.nan, bw)
            fwd_c = self.c[self.fwd_start:self.fwd_end]
            return (fwd_c - bot) / bw_safe
        
        elif op == "bollinger_bandwidth_rank":
            p = comp["period"]
            lb = comp["lookback"]
            top = self._base._bbtop(p).values
            bot = self._base._bbbot(p).values
            mid = self._base._ma(f"avgc{p}").values
            mid_safe = np.where(mid == 0, np.nan, mid)
            bw = (top - bot) / mid_safe
            fwd_bw = bw[self.fwd_start:self.fwd_end]
            result = np.full(self.n_forward, np.nan)
            for i in range(self.n_forward):
                abs_idx = self.fwd_start + i
                start = max(0, abs_idx - lb)
                window = bw[start:abs_idx+1]
                w_min = np.nanmin(window)
                w_max = np.nanmax(window)
                w_range = w_max - w_min
                if w_range > 0:
                    result[i] = (fwd_bw[i] - w_min) / w_range
            return result
        
        # ──── RETRACEMENT ─────────────────────────────────
        elif op == "retrace_from_mfe_pct":
            mfe = self._mfe_close.copy()
            if self.direction == 'short':
                captured = self.entry_high - self.c[self.fwd_start:self.fwd_end]
            else:
                captured = self.c[self.fwd_start:self.fwd_end] - self.entry_low
            mfe_safe = np.where(mfe <= 0, np.nan, mfe)
            return 1.0 - (captured / mfe_safe)  # 0 = at MFE, 1 = back to entry
        
        elif op == "retrace_from_mfe":
            norm_name = comp.get("normalizer", "adr14")
            mfe = self._mfe_close
            if self.direction == 'short':
                captured = self.entry_high - self.c[self.fwd_start:self.fwd_end]
            else:
                captured = self.c[self.fwd_start:self.fwd_end] - self.entry_low
            giveback = mfe - captured
            if norm_name == "pct":
                ref_price = self.entry_high if self.direction == 'short' else self.entry_low
                return giveback / ref_price * 100
            norm = self._get_norm(norm_name)
            return giveback / norm
        
        elif op == "position_in_post_range":
            fwd_h = self.h[self.fwd_start:self.fwd_end]
            fwd_l = self.l[self.fwd_start:self.fwd_end]
            fwd_c = self.c[self.fwd_start:self.fwd_end]
            running_high = np.maximum.accumulate(fwd_h)
            running_low = np.minimum.accumulate(fwd_l)
            rng = running_high - running_low
            rng_safe = np.where(rng == 0, np.nan, rng)
            return (fwd_c - running_low) / rng_safe
        
        elif op == "bars_since_mfe":
            mfe = self._mfe_close
            result = np.zeros(self.n_forward)
            current_mfe = 0
            mfe_bar = 0
            for i in range(self.n_forward):
                if mfe[i] > current_mfe:
                    current_mfe = mfe[i]
                    mfe_bar = i
                result[i] = float(i - mfe_bar)
            return result
        
        elif op == "mfe_expanding":
            w = comp.get("window", comp.get("period", 5))
            mfe = self._mfe_close
            result = np.zeros(self.n_forward)
            for i in range(w, self.n_forward):
                if mfe[i] > mfe[i-w]:
                    result[i] = 1.0
            return result
        
        # ──── TIME ────────────────────────────────────────
        elif op == "bars_since_signal":
            return np.arange(self.n_forward, dtype=float)
        
        elif op == "move_per_bar":
            norm_name = comp.get("normalizer", "adr14")
            if self.direction == 'short':
                captured = self.entry_high - self.c[self.fwd_start:self.fwd_end]
            else:
                captured = self.c[self.fwd_start:self.fwd_end] - self.entry_low
            bars = np.arange(self.n_forward, dtype=float)
            bars[0] = np.nan  # avoid div by zero at bar 0
            if norm_name == "pct":
                ref_price = self.entry_high if self.direction == 'short' else self.entry_low
                return (captured / ref_price * 100) / bars
            norm = self._get_norm(norm_name)
            return (captured / norm) / bars
        
        elif op == "velocity_change":
            if self.direction == 'short':
                captured = self.entry_high - self.c[self.fwd_start:self.fwd_end]
            else:
                captured = self.c[self.fwd_start:self.fwd_end] - self.entry_low
            velocity = np.full(self.n_forward, np.nan)
            velocity[1:] = captured[1:] - captured[:-1]
            accel = np.full(self.n_forward, np.nan)
            accel[2:] = velocity[2:] - velocity[1:-1]
            if comp["direction"] == "increasing":
                return (accel > 0).astype(float)
            else:
                return (accel < 0).astype(float)
        
        # ──── RELATIVE STRENGTH ───────────────────────────
        elif op == "rs_vs_spy":
            w = comp.get("window", comp.get("period", 10))
            if self.spy_df is None:
                return np.full(self.n_forward, np.nan)
            # Align SPY by date
            fwd_dates = self.df["date"].values[self.fwd_start:self.fwd_end]
            spy_c = self.spy_df.set_index("date")["close"]
            result = np.full(self.n_forward, np.nan)
            for i in range(w, self.n_forward):
                abs_idx = self.fwd_start + i
                d_now = self.df["date"].iloc[abs_idx]
                d_prev = self.df["date"].iloc[abs_idx - w]
                if d_now in spy_c.index and d_prev in spy_c.index:
                    stock_ret = self.c[abs_idx] / self.c[abs_idx - w] - 1
                    spy_ret = spy_c[d_now] / spy_c[d_prev] - 1
                    result[i] = stock_ret - spy_ret
            return result
        
        elif op == "rs_vs_spy_slope":
            w = comp.get("window", comp.get("period", 10))
            rs = self.compute({"op": "rs_vs_spy", "window": w})
            result = np.full(self.n_forward, np.nan)
            result[1:] = rs[1:] - rs[:-1]
            return result
        
        # ──── BOOLEAN AGGREGATIONS ────────────────────────
        elif op.startswith("bool_"):
            base_series = self.compute(comp["base_op"])
            window = comp["window"]
            agg_type = op[5:]  # strip "bool_"
            
            result = np.full(self.n_forward, np.nan)
            bool_vals = base_series > 0.5  # convert to boolean
            
            if agg_type == "count_true":
                for i in range(self.n_forward):
                    s = max(0, i - window + 1)
                    result[i] = float(np.sum(bool_vals[s:i+1]))
            
            elif agg_type == "since_true":
                bars = 0
                found = False
                for i in range(self.n_forward):
                    if bool_vals[i]:
                        found = True
                        bars = 0
                    elif found:
                        bars += 1
                    if found:
                        result[i] = float(bars)
            
            elif agg_type == "true_in_row":
                streak = 0
                for i in range(self.n_forward):
                    if bool_vals[i]:
                        streak += 1
                    else:
                        streak = 0
                    result[i] = float(streak)
            
            elif agg_type == "pct_true":
                for i in range(self.n_forward):
                    s = max(0, i - window + 1)
                    seg = bool_vals[s:i+1]
                    if len(seg) > 0:
                        result[i] = float(np.sum(seg)) / len(seg)
            
            return result
        
        else:
            raise ValueError(f"Unknown exit expression op: {op}")
