"""
One-Time Setup Script — Generate .lookback and .state files for forward-propagation.

Reads existing .npz files + OHLCV pickles, extracts intermediate history and
running computation state, writes per-ticker .lookback and .state files.

Run once after a full expr cache rebuild. After this, nightly appends use
forward-propagation instead of full recompute.

Usage:
    python setup_forward_prop.py [--workers N] [--ticker AAPL] [--verify]
"""

import os
import sys
import json
import time
import pickle
import struct
import warnings
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore", category=RuntimeWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)  # scripts/ -> repo root
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")

EXPR_CACHE_START = "2020-01-02"
LOOKBACK_ROWS = 504
N_BASE_INTERMEDIATES = 178
N_ADDITIONAL_INTERMEDIATES = 18
N_INTERMEDIATES = N_BASE_INTERMEDIATES + N_ADDITIONAL_INTERMEDIATES  # 196

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

# ══════════════════════════════════════════════════════════════
# INTERMEDIATE COLUMN DEFINITIONS — must match build_numpy_intermediates order
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

# MACD configs that use signal lines (from brute_expressions.py macd_configs)
MACD_SIGNAL_CONFIGS = [(12, 26, 9), (8, 17, 9), (5, 13, 8), (6, 19, 9)]

# On-series state periods
ON_SERIES_RSI_PERIODS = [5, 7, 9, 14, 21, 28]
ON_SERIES_ADX_PERIODS = [7, 10, 14, 20]
EXT_SERIES_NAMES = ["ext_avgc50_adr14", "ext_avgc200_adr14"]


def build_intermediate_column_order():
    """Build the ordered list of intermediate column names.

    Must match the order used in build_numpy_intermediates + additional columns.
    Returns list of 196 column name strings.
    """
    cols = []

    # From build_numpy_intermediates — 178 columns
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

    assert len(cols) == N_BASE_INTERMEDIATES, f"Expected {N_BASE_INTERMEDIATES}, got {len(cols)}"

    # Additional 18 columns for forward-prop
    # 11 cumsums
    for name in ["cumsum_close", "cumsum_volume", "cumsum_hl", "cumsum_tr",
                  "cumsum_bop_raw", "cumsum_mfv", "cumsum_abs_diff",
                  "cumsum_tp", "cumsum_c2", "cumsum_gains", "cumsum_losses"]:
        cols.append(name)
    # 7 raw per-bar values
    for name in ["true_range", "gains", "losses", "tp", "bop_raw", "mfv", "abs_diff"]:
        cols.append(name)

    assert len(cols) == N_INTERMEDIATES, f"Expected {N_INTERMEDIATES}, got {len(cols)}"
    return cols


INTERMEDIATE_COLUMNS = build_intermediate_column_order()
INTERMEDIATE_COL_INDEX = {name: i for i, name in enumerate(INTERMEDIATE_COLUMNS)}


# ══════════════════════════════════════════════════════════════
# WORKER GLOBALS
# ══════════════════════════════════════════════════════════════

_w_expressions = None
_w_ext_name_to_idx = None


def _init_worker(expressions, ext_name_to_idx):
    global _w_expressions, _w_ext_name_to_idx
    _w_expressions = expressions
    _w_ext_name_to_idx = ext_name_to_idx


# ══════════════════════════════════════════════════════════════
# SETUP ONE TICKER
# ══════════════════════════════════════════════════════════════

def _setup_one_ticker(args):
    """Generate .lookback and .state for one ticker.

    Args: (ticker, df_dict, weekly_df_dict, monthly_df_dict)
    Returns: (ticker, True, msg) on success, (ticker, False, error_msg) on failure
    """
    ticker, df_dict, weekly_df_dict, monthly_df_dict = args
    global _w_expressions, _w_ext_name_to_idx

    try:
        from scripts.expression_engine import ExpressionEngine
        from scripts.profiling_engine import (
            ema as pd_ema, rolling_max as pd_rolling_max,
            rolling_min as pd_rolling_min, true_range as pd_true_range,
        )
        from local_runner.expr_cache_builder import (
            build_numpy_intermediates, resample_ohlcv,
        )

        # Reconstruct DataFrame
        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Load .npz to get its bar count — state must reflect the .npz end,
        # not the daily OHLCV end (which may have extra bars appended since last rebuild)
        safe_ticker = ticker.replace("/", "_").replace(".", "_")
        npz_path = os.path.join(EXPR_CACHE_DIR, f"{safe_ticker}.npz")
        if not os.path.exists(npz_path):
            return (ticker, False, "no .npz file")

        from expr_cache_builder import _open_npz
        loaded_npz = _open_npz(npz_path)
        npz_n_bars = loaded_npz["data"].shape[0]
        npz_dates = loaded_npz["dates"]

        # Truncate daily OHLCV to match .npz bar count
        if len(df) > npz_n_bars:
            df = df.iloc[:npz_n_bars].reset_index(drop=True)
        elif len(df) < npz_n_bars:
            return (ticker, False, f"OHLCV has {len(df)} bars but .npz has {npz_n_bars}")

        n_bars = len(df)
        if n_bars < 50:
            return (ticker, False, "too few bars")

        # ── Build ExpressionEngine and extract 178 intermediates ──
        engine = ExpressionEngine(df)
        im = build_numpy_intermediates(engine)

        # ── Compute 18 additional intermediate columns ──
        close = im["close"]
        high = im["high"]
        low = im["low"]
        volume = im["volume"]
        open_ = im["open"]

        # True range
        tr = np.empty(n_bars, dtype=np.float64)
        tr[0] = high[0] - low[0]
        for i in range(1, n_bars):
            tr[i] = max(high[i] - low[i],
                        abs(high[i] - close[i - 1]),
                        abs(low[i] - close[i - 1]))

        # Gains and losses (for RSI cumsum)
        delta = np.diff(close, prepend=np.nan)
        gains = np.maximum(0.0, delta)
        losses = np.maximum(0.0, -delta)
        gains[0] = 0.0
        losses[0] = 0.0

        # Typical price
        tp = (high + low + close) / 3.0

        # BOP raw (unsmoothed)
        hl = high - low
        hl_safe = np.where(hl == 0, np.nan, hl)
        bop_raw = (close - open_) / hl_safe

        # Money flow volume (for CMF)
        mfm = ((close - low) - (high - close)) / hl_safe
        mfv = mfm * volume

        # Absolute diff of close (for Kaufman efficiency)
        abs_diff = np.abs(delta)
        abs_diff[0] = 0.0

        # Close squared (for Bollinger variance)
        c2 = close ** 2

        # Cumsums (NaN-safe — replace NaN with 0 before cumsum)
        def _safe_cumsum(arr):
            a = arr.copy()
            a[np.isnan(a)] = 0.0
            return np.cumsum(a)

        cumsum_close = _safe_cumsum(close)
        cumsum_volume = _safe_cumsum(volume)
        cumsum_hl = _safe_cumsum(high - low)
        cumsum_tr = _safe_cumsum(tr)
        cumsum_bop_raw = _safe_cumsum(np.nan_to_num(bop_raw, nan=0.0))
        cumsum_mfv = _safe_cumsum(np.nan_to_num(mfv, nan=0.0))
        cumsum_abs_diff = _safe_cumsum(abs_diff)
        cumsum_tp = _safe_cumsum(tp)
        cumsum_c2 = _safe_cumsum(c2)
        cumsum_gains = _safe_cumsum(gains)
        cumsum_losses = _safe_cumsum(losses)

        # ── Build full 196-column intermediate array ──
        im_array = np.full((n_bars, N_INTERMEDIATES), np.nan, dtype=np.float64)

        # Fill 178 base columns from im dict
        for col_name in INTERMEDIATE_COLUMNS[:N_BASE_INTERMEDIATES]:
            idx = INTERMEDIATE_COL_INDEX[col_name]
            arr = im.get(col_name)
            if arr is not None:
                im_array[:, idx] = arr

        # Fill 18 additional columns
        additional = {
            "cumsum_close": cumsum_close, "cumsum_volume": cumsum_volume,
            "cumsum_hl": cumsum_hl, "cumsum_tr": cumsum_tr,
            "cumsum_bop_raw": cumsum_bop_raw, "cumsum_mfv": cumsum_mfv,
            "cumsum_abs_diff": cumsum_abs_diff, "cumsum_tp": cumsum_tp,
            "cumsum_c2": cumsum_c2, "cumsum_gains": cumsum_gains,
            "cumsum_losses": cumsum_losses,
            "true_range": tr, "gains": gains, "losses": losses,
            "tp": tp, "bop_raw": np.nan_to_num(bop_raw, nan=0.0),
            "mfv": np.nan_to_num(mfv, nan=0.0), "abs_diff": abs_diff,
        }
        for col_name, arr in additional.items():
            idx = INTERMEDIATE_COL_INDEX[col_name]
            im_array[:, idx] = arr

        # ── Write .lookback file ──
        # Last 504 rows (or fewer if ticker has < 504 bars)
        lb_rows = min(LOOKBACK_ROWS, n_bars)
        lookback_data = im_array[-lb_rows:, :].astype(np.float16)

        lookback_path = os.path.join(EXPR_CACHE_DIR, f"{safe_ticker}.lookback")
        lookback_data.tofile(lookback_path)

        # ── Build .state file ──
        state = {}
        last = n_bars - 1  # index of last bar

        # --- Daily EMA close states (14) ---
        for p in EMA_CLOSE_PERIODS:
            val = im[f"xavgc{p}"][last]
            state[f"xavgc{p}"] = float(val) if not np.isnan(val) else 0.0

        # --- MACD EMA states for periods not in EMA_CLOSE_PERIODS ---
        # MACD line = EMA(close, fast) - EMA(close, slow). Some MACD periods
        # (6, 17, 19, 26, 35) are not in EMA_CLOSE_PERIODS and need separate state.
        _macd_extra_periods = set()
        for fast, slow in MACD_PAIRS:
            if fast not in EMA_CLOSE_PERIODS:
                _macd_extra_periods.add(fast)
            if slow not in EMA_CLOSE_PERIODS:
                _macd_extra_periods.add(slow)
        close_series = pd.Series(im["close"])
        for p in sorted(_macd_extra_periods):
            ema_vals = pd_ema(close_series, p).values
            state[f"macd_ema_{p}"] = float(ema_vals[last]) if not np.isnan(ema_vals[last]) else 0.0

        # --- MACD signal line EMA states (4) ---
        for fast, slow, sig_period in MACD_SIGNAL_CONFIGS:
            macd_line = im[f"macd_{fast}_{slow}"]
            # Recompute signal line to extract last EMA state
            macd_series = pd.Series(macd_line)
            signal = pd_ema(macd_series, sig_period).values
            val = signal[last]
            state[f"macd_signal_{fast}_{slow}"] = float(val) if not np.isnan(val) else 0.0

        # --- ADX EMA chain states (12) ---
        # ADX uses: ema(dm_plus, period), ema(dm_minus, period), ema(dx, period)
        # DM+/DM- raw arrays are the same for all periods
        up = high[1:] - high[:-1]
        down = low[:-1] - low[1:]
        dm_plus_raw = np.where((up > down) & (up > 0), up, 0.0)
        dm_minus_raw = np.where((down > up) & (down > 0), down, 0.0)
        dm_plus_full = np.empty(n_bars, dtype=np.float64)
        dm_minus_full = np.empty(n_bars, dtype=np.float64)
        dm_plus_full[0] = np.nan
        dm_minus_full[0] = np.nan
        dm_plus_full[1:] = dm_plus_raw
        dm_minus_full[1:] = dm_minus_raw

        for p in ADX_PERIODS:
            # EMA of DM+ and DM-
            ema_dmp = pd_ema(pd.Series(dm_plus_full), p).values
            ema_dmm = pd_ema(pd.Series(dm_minus_full), p).values

            state[f"ema_dmp_{p}"] = float(ema_dmp[last]) if not np.isnan(ema_dmp[last]) else 0.0
            state[f"ema_dmm_{p}"] = float(ema_dmm[last]) if not np.isnan(ema_dmm[last]) else 0.0

            # adx = ema(dx, P) — read from im (already computed by ExpressionEngine)
            adx_val = im[f"adx{p}"][last]
            state[f"ema_dx_{p}"] = float(adx_val) if not np.isnan(adx_val) else 0.0

        # --- Rolling max/min tracking indices (51) ---
        # For each rolling max/min period, find the bar index of the current max/min
        # within the last P bars
        for p in MAXH_PERIODS:
            window = im["high"][max(0, last - p + 1):last + 1]
            if len(window) > 0:
                local_idx = np.nanargmax(window)
                abs_idx = max(0, last - p + 1) + local_idx
                state[f"maxh_idx_{p}"] = int(abs_idx)
            else:
                state[f"maxh_idx_{p}"] = last

        for p in MINL_PERIODS:
            window = im["low"][max(0, last - p + 1):last + 1]
            if len(window) > 0:
                local_idx = np.nanargmin(window)
                abs_idx = max(0, last - p + 1) + local_idx
                state[f"minl_idx_{p}"] = int(abs_idx)
            else:
                state[f"minl_idx_{p}"] = last

        for p in MAXC_PERIODS:
            window = im["close"][max(0, last - p + 1):last + 1]
            if len(window) > 0:
                local_idx = np.nanargmax(window)
                abs_idx = max(0, last - p + 1) + local_idx
                state[f"maxc_idx_{p}"] = int(abs_idx)
            else:
                state[f"maxc_idx_{p}"] = last

        # --- Aroon tracking indices (14) ---
        for p in AROON_PERIODS:
            # Aroon uses rolling window of H and L
            window_h = im["high"][max(0, last - p + 1):last + 1]
            window_l = im["low"][max(0, last - p + 1):last + 1]
            if len(window_h) > 0:
                state[f"aroon_maxh_idx_{p}"] = int(max(0, last - p + 1) + np.nanargmax(window_h))
                state[f"aroon_minl_idx_{p}"] = int(max(0, last - p + 1) + np.nanargmin(window_l))
            else:
                state[f"aroon_maxh_idx_{p}"] = last
                state[f"aroon_minl_idx_{p}"] = last

        # --- Stochastic raw_k history (18) ---
        # Need prev1 and prev2 raw_k values for each stochastic period
        # raw_k = (close - rolling_min(low,p)) / (rolling_max(high,p) - rolling_min(low,p)) * 100
        # Stochastic periods don't all exist in maxh/minl intermediates, so compute directly
        for p in STOCH_PERIODS:
            maxh_arr = pd_rolling_max(pd.Series(high), p).values
            minl_arr = pd_rolling_min(pd.Series(low), p).values
            denom = maxh_arr - minl_arr
            denom_safe = np.where(denom == 0, np.nan, denom)
            raw_k_all = (close - minl_arr) / denom_safe * 100.0

            # prev1 = raw_k at last bar, prev2 = raw_k at last-1
            if last >= 1:
                state[f"raw_k_{p}_prev1"] = float(raw_k_all[last]) if not np.isnan(raw_k_all[last]) else 0.0
                state[f"raw_k_{p}_prev2"] = float(raw_k_all[last - 1]) if not np.isnan(raw_k_all[last - 1]) else 0.0
            else:
                state[f"raw_k_{p}_prev1"] = 0.0
                state[f"raw_k_{p}_prev2"] = 0.0

        # --- OBV (1) ---
        state["obv_prev"] = float(im["obv"][last]) if not np.isnan(im["obv"][last]) else 0.0

        # --- Previous OHLCV (3) ---
        state["prev_close"] = float(close[last])
        state["prev_high"] = float(high[last])
        state["prev_low"] = float(low[last])

        # --- Bar index (1) ---
        state["bar_index"] = int(last)

        # --- Previous bar intermediates at float64 (196) ---
        # Stored so forward-prop can read shift(1) at full precision
        # instead of reading float16 from .lookback
        prev_im = {}
        for col_name in INTERMEDIATE_COLUMNS:
            idx = INTERMEDIATE_COL_INDEX[col_name]
            val = float(im_array[last, idx])
            prev_im[col_name] = val if not np.isnan(val) else 0.0
        state["prev_im"] = prev_im

        # --- Float64 source history for SMA computation (last 200 bars) ---
        # Avoids float16 precision loss when computing SMAs from lookback.
        # Stores source values used by cumsum-based SMAs at full precision.
        _src_len = min(200, n_bars)
        _src_start = n_bars - _src_len
        state["src_history"] = {
            "close": [float(x) for x in close[_src_start:]],
            "volume": [float(x) for x in volume[_src_start:]],
            "true_range": [float(x) for x in tr[_src_start:]],
            "hl": [float(high[i] - low[i]) for i in range(_src_start, n_bars)],
            "gains": [float(x) for x in gains[_src_start:]],
            "losses": [float(x) for x in losses[_src_start:]],
            "bop_raw": [float(x) if not np.isnan(x) else 0.0 for x in np.nan_to_num(bop_raw[_src_start:], nan=0.0)],
            "mfv": [float(x) if not np.isnan(x) else 0.0 for x in np.nan_to_num(mfv[_src_start:], nan=0.0)],
            "abs_diff": [float(x) for x in abs_diff[_src_start:]],
            "tp": [float(x) for x in tp[_src_start:]],
            "c2": [float(x) for x in c2[_src_start:]],
            "high": [float(x) for x in high[_src_start:]],
            "low": [float(x) for x in low[_src_start:]],
        }

        # --- Cumsum states (11) at last bar ---
        state["cumsum_close"] = float(cumsum_close[last])
        state["cumsum_volume"] = float(cumsum_volume[last])
        state["cumsum_hl"] = float(cumsum_hl[last])
        state["cumsum_tr"] = float(cumsum_tr[last])
        state["cumsum_bop_raw"] = float(cumsum_bop_raw[last])
        state["cumsum_mfv"] = float(cumsum_mfv[last])
        state["cumsum_abs_diff"] = float(cumsum_abs_diff[last])
        state["cumsum_tp"] = float(cumsum_tp[last])
        state["cumsum_c2"] = float(cumsum_c2[last])
        state["cumsum_gains"] = float(cumsum_gains[last])
        state["cumsum_losses"] = float(cumsum_losses[last])

        # ════════════════════════════════════════════════════
        # HTF STATE (weekly + monthly)
        # ════════════════════════════════════════════════════
        for tf_key, tf_df_dict, tf_freq in [
            ("w", weekly_df_dict, "W"),
            ("m", monthly_df_dict, "ME"),
        ]:
            htf_state = _build_htf_state(
                df, tf_df_dict, tf_freq, tf_key, engine, im, n_bars
            )
            for k, v in htf_state.items():
                state[k] = v

        # ════════════════════════════════════════════════════
        # EXTENSION STRUCTURE STATE
        # ════════════════════════════════════════════════════
        # Use the .npz already loaded above for extension series columns
        npz_data = loaded_npz["data"].astype(np.float32)
        npz_last = npz_data.shape[0] - 1  # same as last since we truncated df to match

        for ext_series_name in EXT_SERIES_NAMES:
            ext_idx = _w_ext_name_to_idx.get(ext_series_name)
            if ext_idx is None:
                continue

            ext_values = npz_data[:, ext_idx].astype(np.float64)
            ext_label = "ext50" if "50" in ext_series_name else "ext200"

            # Previous extension value
            state[f"ext_prev_{ext_label}"] = float(ext_values[npz_last]) if not np.isnan(ext_values[npz_last]) else 0.0

            # On-series RSI EMA states (12 per series)
            delta_ext = np.diff(ext_values, prepend=np.nan)
            gain_ext = np.maximum(0.0, delta_ext)
            loss_ext = np.maximum(0.0, -delta_ext)

            for p in ON_SERIES_RSI_PERIODS:
                # EMA smoothing: ewm(span=p, adjust=False)
                avg_gain = pd.Series(gain_ext).ewm(span=p, adjust=False).mean().values
                avg_loss = pd.Series(loss_ext).ewm(span=p, adjust=False).mean().values
                state[f"ext_{ext_label}_rsi_avg_gain_{p}"] = float(avg_gain[npz_last]) if not np.isnan(avg_gain[npz_last]) else 0.0
                state[f"ext_{ext_label}_rsi_avg_loss_{p}"] = float(avg_loss[npz_last]) if not np.isnan(avg_loss[npz_last]) else 0.0

            # On-series ADX EMA states (12 per series + 4 ATR proxy EMA per series)
            # Must match compute_on_series() in backtest_conditions.py:1119-1126
            # DM+/DM- use simple clip (NOT traditional mutual-exclusion formula)
            # ATR proxy uses EMA (NOT SMA)
            ext_diff = np.diff(ext_values, prepend=np.nan)
            ext_dm_plus = np.clip(ext_diff, 0, None)    # backtest_conditions.py:1120
            ext_dm_minus = np.clip(-ext_diff, 0, None)   # backtest_conditions.py:1121
            ext_dm_plus[0] = np.nan
            ext_dm_minus[0] = np.nan
            ext_tr = np.abs(ext_diff)                    # backtest_conditions.py:1122
            ext_tr[0] = np.nan

            for p in ON_SERIES_ADX_PERIODS:
                ema_dmp_ext = pd_ema(pd.Series(ext_dm_plus), p).values
                ema_dmm_ext = pd_ema(pd.Series(ext_dm_minus), p).values

                # ATR proxy: EMA, matching backtest_conditions.py:1122
                ext_atr = pd_ema(pd.Series(ext_tr), p).values

                ext_atr_safe = np.where(ext_atr == 0, np.nan, ext_atr)
                di_plus_ext = 100.0 * ema_dmp_ext / ext_atr_safe
                di_minus_ext = 100.0 * ema_dmm_ext / ext_atr_safe
                di_sum = di_plus_ext + di_minus_ext
                di_sum_safe = np.where(di_sum == 0, np.nan, di_sum)
                dx_ext = np.abs(di_plus_ext - di_minus_ext) / di_sum_safe * 100.0
                adx_ext = pd_ema(pd.Series(dx_ext), p).values

                state[f"ext_{ext_label}_adx_ema_dmp_{p}"] = float(ema_dmp_ext[npz_last]) if not np.isnan(ema_dmp_ext[npz_last]) else 0.0
                state[f"ext_{ext_label}_adx_ema_dmm_{p}"] = float(ema_dmm_ext[npz_last]) if not np.isnan(ema_dmm_ext[npz_last]) else 0.0
                state[f"ext_{ext_label}_adx_ema_dx_{p}"] = float(adx_ext[npz_last]) if not np.isnan(adx_ext[npz_last]) else 0.0
                # ATR proxy EMA state — needed for forward-prop DI normalization
                state[f"ext_{ext_label}_adx_atr_proxy_{p}"] = float(ext_atr[npz_last]) if not np.isnan(ext_atr[npz_last]) else 0.0

        # ── MOC level state (variable-length list, JSON-friendly dicts) ──
        # Walks the same df the .npz was built from; final state reflects end of .npz.
        try:
            from scripts.moc_detector import bootstrap_moc_state
            moc_state = bootstrap_moc_state(df)
            state["moc_levels"] = moc_state["levels"]
        except Exception:
            state["moc_levels"] = []

        # ── Reversal profile state per ext source (Extension Chart Levels) ──
        # Walks the same .npz ext columns the cache holds; final tally state
        # reflects the .npz end-bar. Forward-prop steps the running tallies
        # one bar at a time using state["ext_prev_*"] + new ext value.
        try:
            from scripts.reversal_profile import bootstrap_reversal_state, EXT_SOURCE_COLUMNS
            rp_state = {}
            for src_col in EXT_SOURCE_COLUMNS:
                ext_idx = (_w_ext_name_to_idx or {}).get(src_col)
                if ext_idx is None:
                    rp_state[src_col] = {}
                    continue
                ext_arr = loaded_npz["data"][:, ext_idx].astype(np.float64)
                rp_state[src_col] = bootstrap_reversal_state(ext_arr)
            state["reversal_profile_state"] = rp_state
        except Exception:
            state["reversal_profile_state"] = {}

        # ── Trendline state: full ext50 history (for path-pure forward-prop) ──
        try:
            from scripts.ext50_trendlines import bootstrap_ext50_trendline_state
            ext50_idx = (_w_ext_name_to_idx or {}).get("ext_avgc50_adr14")
            if ext50_idx is not None:
                ext50_arr = loaded_npz["data"][:, ext50_idx].astype(np.float64)
                state["ext50_trendline_state"] = bootstrap_ext50_trendline_state(ext50_arr)
            else:
                state["ext50_trendline_state"] = {"ext50_history": []}
        except Exception:
            state["ext50_trendline_state"] = {"ext50_history": []}

        # ── Write .state file ──
        state_path = os.path.join(EXPR_CACHE_DIR, f"{safe_ticker}.state")
        with open(state_path, "w") as f:
            json.dump(state, f)

        # ── Verify ──
        lb_size = os.path.getsize(lookback_path)
        expected_lb_size = lb_rows * N_INTERMEDIATES * 2  # float16
        if lb_size != expected_lb_size:
            return (ticker, False, f"lookback size mismatch: {lb_size} vs {expected_lb_size}")

        return (ticker, True, f"ok (n_bars={n_bars}, lb_rows={lb_rows}, state_keys={len(state)})")

    except Exception as e:
        import traceback
        return (ticker, False, f"{type(e).__name__}: {str(e)[:300]}\n{traceback.format_exc()[-500:]}")


def _build_htf_state(daily_df, htf_df_dict, freq, tf_key, engine, im, n_bars):
    """Build HTF state for one timeframe (weekly or monthly).

    Returns dict of state keys prefixed with tf_key (e.g., "w_" or "m_").
    """
    from scripts.expression_engine import ExpressionEngine
    from scripts.profiling_engine import ema as pd_ema, sma as pd_sma
    from local_runner.expr_cache_builder import resample_ohlcv

    state = {}

    # Get HTF DataFrame
    if htf_df_dict is not None:
        htf_df = pd.DataFrame(htf_df_dict)
        htf_df["date"] = pd.to_datetime(htf_df["date"])
    else:
        htf_df = resample_ohlcv(daily_df, freq)

    if htf_df is None or len(htf_df) < 5:
        # Not enough data — store zeros
        _fill_htf_zeros(state, tf_key)
        return state

    # Truncate to cache window
    htf_df = htf_df[htf_df["date"] >= pd.Timestamp(EXPR_CACHE_START)]
    if len(htf_df) < 5:
        _fill_htf_zeros(state, tf_key)
        return state

    htf_close = htf_df["close"].values.astype(np.float64)
    htf_high = htf_df["high"].values.astype(np.float64)
    htf_low = htf_df["low"].values.astype(np.float64)
    htf_open = htf_df["open"].values.astype(np.float64)
    htf_volume = htf_df["volume"].values.astype(np.float64)
    n_htf = len(htf_df)
    htf_last = n_htf - 1

    # Determine current partial candle state from the last daily bar
    daily_dates = daily_df["date"]
    last_daily = daily_dates.iloc[-1]

    if freq == "W":
        # ISO week: (year, week)
        iso = last_daily.isocalendar()
        period_id = iso[0] * 100 + iso[1]
    else:
        period_id = last_daily.year * 100 + last_daily.month

    # The partial candle = the LAST closed HTF bar's values
    # But actually the last bar in the HTF pickle IS the current (possibly partial) period
    # For the state file, we store the current partial candle OHLCV
    # The "closed" intermediates are computed from all bars EXCEPT the last one
    state[f"{tf_key}_period_id"] = int(period_id)
    state[f"{tf_key}_partial_open"] = float(htf_open[htf_last])
    state[f"{tf_key}_partial_high"] = float(htf_high[htf_last])
    state[f"{tf_key}_partial_low"] = float(htf_low[htf_last])
    state[f"{tf_key}_partial_close"] = float(htf_close[htf_last])
    state[f"{tf_key}_partial_volume"] = float(htf_volume[htf_last])

    # Previous HTF bar OHLCV (for TR and DM+/DM- on period boundaries)
    if htf_last >= 1:
        state[f"{tf_key}_prev_high"] = float(htf_high[htf_last - 1])
        state[f"{tf_key}_prev_low"] = float(htf_low[htf_last - 1])
        state[f"{tf_key}_prev_close"] = float(htf_close[htf_last - 1])
    else:
        state[f"{tf_key}_prev_high"] = float(htf_high[0])
        state[f"{tf_key}_prev_low"] = float(htf_low[0])
        state[f"{tf_key}_prev_close"] = float(htf_close[0])

    # Build ExpressionEngine on CLOSED HTF bars (all but last)
    # to extract EMA / ADX / MACD signal states
    if n_htf >= 6:
        closed_htf = htf_df.iloc[:-1].copy().reset_index(drop=True)
    else:
        closed_htf = htf_df.copy().reset_index(drop=True)

    closed_close = closed_htf["close"].values.astype(np.float64)
    closed_high = closed_htf["high"].values.astype(np.float64)
    closed_low = closed_htf["low"].values.astype(np.float64)
    n_closed = len(closed_htf)
    cl = n_closed - 1  # last closed bar index

    # HTF EMA close states (14)
    for p in EMA_CLOSE_PERIODS:
        ema_vals = pd_ema(pd.Series(closed_close), p).values
        val = ema_vals[cl] if cl >= 0 and not np.isnan(ema_vals[cl]) else 0.0
        state[f"{tf_key}_xavgc{p}"] = float(val)

    # HTF true range (computed once, used by ADX and cumsums)
    tr_htf = np.empty(n_closed, dtype=np.float64)
    if n_closed >= 1:
        tr_htf[0] = closed_high[0] - closed_low[0]
    for i in range(1, n_closed):
        tr_htf[i] = max(closed_high[i] - closed_low[i],
                       abs(closed_high[i] - closed_close[i - 1]),
                       abs(closed_low[i] - closed_close[i - 1]))

    # HTF ADX EMA chain (12)
    # DM+/DM- computed once, shared across periods
    if n_closed >= 3:
        up = closed_high[1:] - closed_high[:-1]
        down = closed_low[:-1] - closed_low[1:]
        dm_plus = np.where((up > down) & (up > 0), up, 0.0)
        dm_minus = np.where((down > up) & (down > 0), down, 0.0)
        dm_plus_full = np.empty(n_closed, dtype=np.float64)
        dm_minus_full = np.empty(n_closed, dtype=np.float64)
        dm_plus_full[0] = np.nan
        dm_minus_full[0] = np.nan
        dm_plus_full[1:] = dm_plus
        dm_minus_full[1:] = dm_minus

    for p in ADX_PERIODS:
        if n_closed < 3:
            state[f"{tf_key}_ema_dmp_{p}"] = 0.0
            state[f"{tf_key}_ema_dmm_{p}"] = 0.0
            state[f"{tf_key}_ema_dx_{p}"] = 0.0
            continue

        ema_dmp = pd_ema(pd.Series(dm_plus_full), p).values
        ema_dmm = pd_ema(pd.Series(dm_minus_full), p).values

        state[f"{tf_key}_ema_dmp_{p}"] = float(ema_dmp[cl]) if not np.isnan(ema_dmp[cl]) else 0.0
        state[f"{tf_key}_ema_dmm_{p}"] = float(ema_dmm[cl]) if not np.isnan(ema_dmm[cl]) else 0.0

        # ADX = ema(dx, p) — compute DX from DI+/DI-
        atr_htf = pd_sma(pd.Series(tr_htf), p).values
        atr_safe = np.where(atr_htf == 0, np.nan, atr_htf)
        di_p = 100.0 * ema_dmp / atr_safe
        di_m = 100.0 * ema_dmm / atr_safe
        di_sum = di_p + di_m
        di_sum_safe = np.where(di_sum == 0, np.nan, di_sum)
        dx = np.abs(di_p - di_m) / di_sum_safe * 100.0
        adx_vals = pd_ema(pd.Series(dx), p).values

        state[f"{tf_key}_ema_dx_{p}"] = float(adx_vals[cl]) if not np.isnan(adx_vals[cl]) else 0.0

    # HTF MACD signal states (5)
    for fast, slow, sig_period in MACD_SIGNAL_CONFIGS:
        if n_closed < slow + 5:
            state[f"{tf_key}_macd_signal_{fast}_{slow}"] = 0.0
            continue
        ema_fast = pd_ema(pd.Series(closed_close), fast).values
        ema_slow = pd_ema(pd.Series(closed_close), slow).values
        macd_line = ema_fast - ema_slow
        signal = pd_ema(pd.Series(macd_line), sig_period).values
        state[f"{tf_key}_macd_signal_{fast}_{slow}"] = float(signal[cl]) if not np.isnan(signal[cl]) else 0.0

    # HTF OBV (1)
    if n_closed >= 2:
        sign = np.sign(np.diff(closed_close, prepend=0.0))
        htf_obv = np.cumsum(sign * closed_htf["volume"].values.astype(np.float64))
        state[f"{tf_key}_obv"] = float(htf_obv[cl])
    else:
        state[f"{tf_key}_obv"] = 0.0

    # HTF cumsums (11)
    def _htf_cumsum(arr):
        a = arr.copy()
        a[np.isnan(a)] = 0.0
        return np.cumsum(a)

    closed_vol = closed_htf["volume"].values.astype(np.float64)
    closed_open_vals = closed_htf["open"].values.astype(np.float64)
    htf_hl = closed_high - closed_low
    htf_hl_safe = np.where(htf_hl == 0, np.nan, htf_hl)
    htf_tp = (closed_high + closed_low + closed_close) / 3.0
    htf_bop_raw = (closed_close - closed_open_vals) / htf_hl_safe
    htf_mfm = ((closed_close - closed_low) - (closed_high - closed_close)) / htf_hl_safe
    htf_mfv_arr = htf_mfm * closed_vol
    htf_delta = np.diff(closed_close, prepend=np.nan)
    htf_abs_diff_arr = np.abs(htf_delta)
    htf_abs_diff_arr[0] = 0.0
    htf_gains = np.maximum(0.0, htf_delta)
    htf_losses = np.maximum(0.0, -htf_delta)
    htf_gains[0] = 0.0
    htf_losses[0] = 0.0

    cs = {
        "cumsum_close": _htf_cumsum(closed_close),
        "cumsum_volume": _htf_cumsum(closed_vol),
        "cumsum_hl": _htf_cumsum(htf_hl),
        "cumsum_tr": _htf_cumsum(tr_htf),
        "cumsum_bop_raw": _htf_cumsum(np.nan_to_num(htf_bop_raw, nan=0.0)),
        "cumsum_mfv": _htf_cumsum(np.nan_to_num(htf_mfv_arr, nan=0.0)),
        "cumsum_abs_diff": _htf_cumsum(htf_abs_diff_arr),
        "cumsum_tp": _htf_cumsum(htf_tp),
        "cumsum_c2": _htf_cumsum(closed_close ** 2),
        "cumsum_gains": _htf_cumsum(htf_gains),
        "cumsum_losses": _htf_cumsum(htf_losses),
    }
    for name, arr in cs.items():
        state[f"{tf_key}_{name}"] = float(arr[cl]) if cl >= 0 else 0.0

    # HTF stochastic raw_k history (18 per TF)
    for p in STOCH_PERIODS:
        from scripts.profiling_engine import rolling_max as pd_rm, rolling_min as pd_rmin
        htf_maxh = pd_rm(pd.Series(closed_high), p).values
        htf_minl = pd_rmin(pd.Series(closed_low), p).values
        denom = htf_maxh - htf_minl
        denom_safe = np.where(denom == 0, np.nan, denom)
        raw_k_htf = (closed_close - htf_minl) / denom_safe * 100.0

        if cl >= 1:
            state[f"{tf_key}_raw_k_{p}_prev1"] = float(raw_k_htf[cl]) if not np.isnan(raw_k_htf[cl]) else 0.0
            state[f"{tf_key}_raw_k_{p}_prev2"] = float(raw_k_htf[cl - 1]) if not np.isnan(raw_k_htf[cl - 1]) else 0.0
        else:
            state[f"{tf_key}_raw_k_{p}_prev1"] = 0.0
            state[f"{tf_key}_raw_k_{p}_prev2"] = 0.0

    return state


def _fill_htf_zeros(state, tf_key):
    """Fill all HTF state keys with zeros when insufficient data."""
    state[f"{tf_key}_period_id"] = 0
    for name in ["partial_open", "partial_high", "partial_low",
                  "partial_close", "partial_volume",
                  "prev_high", "prev_low", "prev_close"]:
        state[f"{tf_key}_{name}"] = 0.0
    for p in EMA_CLOSE_PERIODS:
        state[f"{tf_key}_xavgc{p}"] = 0.0
    for p in ADX_PERIODS:
        state[f"{tf_key}_ema_dmp_{p}"] = 0.0
        state[f"{tf_key}_ema_dmm_{p}"] = 0.0
        state[f"{tf_key}_ema_dx_{p}"] = 0.0
    for fast, slow, _ in MACD_SIGNAL_CONFIGS:
        state[f"{tf_key}_macd_signal_{fast}_{slow}"] = 0.0
    state[f"{tf_key}_obv"] = 0.0
    for name in ["cumsum_close", "cumsum_volume", "cumsum_hl", "cumsum_tr",
                  "cumsum_bop_raw", "cumsum_mfv", "cumsum_abs_diff",
                  "cumsum_tp", "cumsum_c2", "cumsum_gains", "cumsum_losses"]:
        state[f"{tf_key}_{name}"] = 0.0
    for p in STOCH_PERIODS:
        state[f"{tf_key}_raw_k_{p}_prev1"] = 0.0
        state[f"{tf_key}_raw_k_{p}_prev2"] = 0.0


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def _truncate_to_cache_window(df):
    """Truncate DataFrame to EXPR_CACHE_START."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    mask = df["date"] >= pd.Timestamp(EXPR_CACHE_START)
    df = df[mask].reset_index(drop=True)
    return df if len(df) >= 50 else None


def _df_to_dict(df):
    """Convert DataFrame to dict of arrays for cheap serialization."""
    if df is None:
        return None
    return {
        "date": df["date"].values,
        "open": df["open"].values,
        "high": df["high"].values,
        "low": df["low"].values,
        "close": df["close"].values,
        "volume": df["volume"].values,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate .lookback and .state files")
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--ticker", type=str, default=None, help="Single ticker for testing")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N tickers")
    parser.add_argument("--verify", action="store_true", help="Verify existing files")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  FORWARD-PROP SETUP — Generate .lookback + .state files")
    print("=" * 70)

    # Load expression library to get extension series column indices
    from local_runner.expr_cache_builder import _load_expressions
    expressions = _load_expressions()
    print(f"\n  {len(expressions)} expressions loaded")

    # Find extension series indices in expression list
    ext_name_to_idx = {}
    for i, expr in enumerate(expressions):
        if expr["name"] in ("ext_avgc50_adr14", "ext_avgc200_adr14"):
            ext_name_to_idx[expr["name"]] = i
    print(f"  Extension series indices: {ext_name_to_idx}")
    assert len(ext_name_to_idx) == 2, "Must find both extension series"

    # Load OHLCV caches — worktree-aware (falls back to main repo's cache).
    print("\n  Loading daily OHLCV cache...")
    from local_runner.expr_cache_builder import _load_daily_cache, _load_htf_cache
    universe_cache = _load_daily_cache()
    print(f"  {len(universe_cache)} tickers in daily cache")

    # Load HTF caches via worktree-aware loader
    weekly_cache = _load_htf_cache("weekly")
    monthly_cache = _load_htf_cache("monthly")
    if weekly_cache:
        print(f"  Weekly HTF: {len(weekly_cache)} tickers")
    if monthly_cache:
        print(f"  Monthly HTF: {len(monthly_cache)} tickers")

    # Find tickers with existing .npz files
    existing_npz = set()
    for fname in os.listdir(EXPR_CACHE_DIR):
        if fname.endswith(".npz") and not fname.startswith("_"):
            existing_npz.add(fname[:-4])  # strip .npz

    print(f"  Existing .npz files: {len(existing_npz)}")

    # Build work items
    work_items = []
    for ticker, df in universe_cache.items():
        safe = ticker.replace("/", "_").replace(".", "_")
        if safe not in existing_npz:
            continue  # No .npz — skip (will get full compute on next build)

        if args.ticker and ticker != args.ticker:
            continue

        df = _truncate_to_cache_window(df)
        if df is None:
            continue

        df_dict = _df_to_dict(df)
        weekly_df_dict = _df_to_dict(weekly_cache.get(ticker)) if weekly_cache else None
        monthly_df_dict = _df_to_dict(monthly_cache.get(ticker)) if monthly_cache else None
        work_items.append((ticker, df_dict, weekly_df_dict, monthly_df_dict))

    print(f"\n  Tickers to process: {len(work_items)}")

    if args.limit and not args.ticker:
        import random
        random.seed(42)  # reproducible sample
        if args.limit < len(work_items):
            work_items = random.sample(work_items, args.limit)
        print(f"  Random sample of {len(work_items)} tickers (seed=42)")

    if not work_items:
        print("  Nothing to do.")
        return

    # Free large caches
    del universe_cache, weekly_cache, monthly_cache
    import gc; gc.collect()

    # Process
    t0 = time.time()
    n_workers = args.workers if not args.ticker else 1
    ok = 0
    failed = 0
    first_errors = []

    if args.ticker:
        # Single-ticker mode — no multiprocessing
        _init_worker(expressions, ext_name_to_idx)
        result = _setup_one_ticker(work_items[0])
        ticker, success, msg = result
        if success:
            ok += 1
            print(f"  ✓ {ticker}: {msg}")
        else:
            failed += 1
            print(f"  ✗ {ticker}: {msg}")
    else:
        print(f"  Processing with {n_workers} workers...")
        max_in_flight = n_workers * 4

        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_worker,
            initargs=(expressions, ext_name_to_idx),
        ) as pool:
            pending = {}
            work_idx = 0

            def _submit_next():
                nonlocal work_idx
                if work_idx < len(work_items):
                    future = pool.submit(_setup_one_ticker, work_items[work_idx])
                    pending[future] = work_items[work_idx][0]
                    work_idx += 1
                    return True
                return False

            def _collect_one(future):
                nonlocal ok, failed
                ticker = pending.pop(future)
                try:
                    _, success, msg = future.result()
                    if success:
                        ok += 1
                    else:
                        failed += 1
                        if len(first_errors) < 10:
                            first_errors.append(f"{ticker}: {msg}")
                except Exception as e:
                    failed += 1
                    if len(first_errors) < 10:
                        first_errors.append(f"{ticker}: {type(e).__name__}: {str(e)[:200]}")
                del future
                total = ok + failed
                if total % 100 == 0 or total == len(work_items):
                    elapsed = time.time() - t0
                    rate = total / elapsed if elapsed > 0 else 0
                    eta = (len(work_items) - total) / rate if rate > 0 else 0
                    per_ticker = elapsed * n_workers / total if total > 0 else 0
                    print(f"    {total}/{len(work_items)} "
                          f"[{elapsed/60:.1f}m elapsed, ~{eta/60:.1f}m left] "
                          f"({ok} ok, {failed} failed) "
                          f"[{per_ticker:.1f}s/ticker]")

            for _ in range(min(max_in_flight, len(work_items))):
                _submit_next()

            while pending:
                done_iter = as_completed(pending)
                future = next(done_iter)
                _collect_one(future)
                _submit_next()

    total_time = time.time() - t0

    if first_errors:
        print(f"\n  First {len(first_errors)} errors:")
        for err in first_errors:
            print(f"    ✗ {err[:200]}")

    # Disk usage
    lb_bytes = sum(
        os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
        for f in os.listdir(EXPR_CACHE_DIR) if f.endswith(".lookback")
    )
    state_bytes = sum(
        os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
        for f in os.listdir(EXPR_CACHE_DIR) if f.endswith(".state")
    )

    print(f"\n  {'=' * 50}")
    print(f"  SETUP COMPLETE")
    print(f"  {'=' * 50}")
    print(f"  OK: {ok}, Failed: {failed}")
    print(f"  .lookback files: {lb_bytes / (1024**3):.2f} GB")
    print(f"  .state files: {state_bytes / (1024**2):.1f} MB")
    print(f"  Time: {total_time:.0f}s ({total_time/60:.1f} min)")

    # ── Validation spot-check ──
    # Pick up to 3 tickers from what we just processed and validate
    check_tickers = [t for t, _, _, _ in work_items[:3]]
    if check_tickers:
        print(f"\n  Validating {len(check_tickers)} tickers...")
        issues = []
        for ticker in check_tickers:
            safe = ticker.replace("/", "_").replace(".", "_")
            lb_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.lookback")
            st_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.state")

            if not os.path.exists(lb_path):
                issues.append(f"{ticker}: .lookback missing")
                continue
            if not os.path.exists(st_path):
                issues.append(f"{ticker}: .state missing")
                continue

            # Check .lookback dimensions
            lb_size = os.path.getsize(lb_path)
            row_bytes = N_INTERMEDIATES * 2  # float16
            if lb_size % row_bytes != 0:
                issues.append(f"{ticker}: .lookback size {lb_size} not divisible by row size {row_bytes}")
                continue
            lb_rows = lb_size // row_bytes
            if lb_rows > LOOKBACK_ROWS:
                issues.append(f"{ticker}: .lookback has {lb_rows} rows, expected <= {LOOKBACK_ROWS}")
                continue

            # Load and check for all-NaN columns
            lb_data = np.fromfile(lb_path, dtype=np.float16).reshape(lb_rows, N_INTERMEDIATES)
            nan_cols = np.all(np.isnan(lb_data), axis=0).sum()
            inf_cols = np.any(np.isinf(lb_data), axis=0).sum()

            # Check .state keys
            with open(st_path) as f:
                state = json.load(f)
            n_keys = len(state)

            # Check critical state keys exist
            missing_keys = []
            for k in ["xavgc50", "prev_close", "bar_index", "cumsum_close",
                       "obv_prev", "w_period_id", "m_period_id"]:
                if k not in state:
                    missing_keys.append(k)

            status = "OK"
            details = []
            if nan_cols > 0:
                details.append(f"{nan_cols} all-NaN cols in .lookback")
            if inf_cols > 0:
                details.append(f"{inf_cols} inf cols in .lookback")
                status = "WARN"
            if missing_keys:
                details.append(f"missing state keys: {missing_keys}")
                status = "FAIL"

            detail_str = f" ({', '.join(details)})" if details else ""
            print(f"    {ticker}: {status} — {lb_rows} lb rows, {n_keys} state keys{detail_str}")

            if missing_keys:
                issues.append(f"{ticker}: missing keys {missing_keys}")

        if issues:
            print(f"\n  VALIDATION ISSUES:")
            for issue in issues:
                print(f"    ✗ {issue}")
        else:
            print(f"  All validation checks passed.")


if __name__ == "__main__":
    main()
