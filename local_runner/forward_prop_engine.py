"""
Forward-Propagation Engine — Compute one new bar per ticker using state + lookback.

Replaces _compute_ticker_full() in the nightly append path for existing tickers.
Instead of recomputing all ~1,500 bars x 15,805 expressions, this computes only
the new bar's values using stored .state, .lookback, and .append files.

Projected: ~0.87s/ticker x 11,200 tickers / 14 workers ~ 12 minutes
(vs ~124 minutes for full recompute)

Authoritative spec: FORWARD_PROP_SPEC.md
"""

import os
import sys
import json
import math
import numpy as np
import pandas as pd

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import (
    EXPR_CACHE_DIR, BOOL_OPS, SLOW_OPS,
)
from scripts.setup_forward_prop import (
    N_INTERMEDIATES, N_BASE_INTERMEDIATES,
    LOOKBACK_ROWS, INTERMEDIATE_COLUMNS, INTERMEDIATE_COL_INDEX,
    SMA_CLOSE_PERIODS, EMA_CLOSE_PERIODS, SMA_VOL_PERIODS,
    MAXH_PERIODS, MINL_PERIODS, RSI_PERIODS, ADX_PERIODS,
    STOCH_PERIODS, CCI_PERIODS, BOP_PERIODS, MACD_PAIRS,
    BOLL_PERIODS, AROON_PERIODS, CMF_PERIODS, KAUF_PERIODS,
    MAXC_PERIODS, MACD_SIGNAL_CONFIGS,
    ON_SERIES_RSI_PERIODS, ON_SERIES_ADX_PERIODS, EXT_SERIES_NAMES,
)

# Cumsum column indices — these may overflow in float16 .lookback
_CUMSUM_COL_NAMES = {
    "cumsum_close", "cumsum_volume", "cumsum_hl", "cumsum_tr",
    "cumsum_bop_raw", "cumsum_mfv", "cumsum_abs_diff",
    "cumsum_tp", "cumsum_c2", "cumsum_gains", "cumsum_losses",
}
_CUMSUM_COL_INDICES = {INTERMEDIATE_COL_INDEX[n] for n in _CUMSUM_COL_NAMES}

# Map cumsum column to its source raw column for overflow fallback
_CUMSUM_SOURCE_MAP = {
    "cumsum_close": "close",
    "cumsum_volume": "volume",
    "cumsum_hl": None,         # H - L, computed inline
    "cumsum_tr": "true_range",
    "cumsum_bop_raw": "bop_raw",
    "cumsum_mfv": "mfv",
    "cumsum_abs_diff": "abs_diff",
    "cumsum_tp": "tp",
    "cumsum_c2": None,         # close^2, computed inline
    "cumsum_gains": "gains",
    "cumsum_losses": "losses",
}


# ══════════════════════════════════════════════════════════════
# WORKER GLOBALS
# ══════════════════════════════════════════════════════════════

_fp_expressions = None
_fp_n_exprs = 0
_fp_daily_dispatch_indices = []
_fp_daily_slow_indices = []
_fp_daily_fallback_indices = []
_fp_daily_bool_ct = []
_fp_daily_bool_st = []
_fp_daily_bool_tir = []
_fp_ext_struct_indices = []
_fp_ext_series_name_to_idx = {}
_fp_lsp_indices = []
_fp_algo_indices = []
_fp_moc_indices = []
_fp_reversal_profile_by_source = {}  # input_column -> [(constant_name, j), ...]
_fp_ext50_trendline_indices = []
_fp_htf_weekly_indices = []
_fp_htf_monthly_indices = []
_fp_htf_weekly_base = []
_fp_htf_monthly_base = []


def _init_fp_worker(expressions):
    """Initialize worker with expression list and pre-classify indices.

    Uses identical classification logic to _init_worker in expr_cache_builder.py
    so that the same expressions route to the same computation paths.
    """
    global _fp_expressions, _fp_n_exprs
    global _fp_daily_dispatch_indices, _fp_daily_slow_indices, _fp_daily_fallback_indices
    global _fp_daily_bool_ct, _fp_daily_bool_st, _fp_daily_bool_tir
    global _fp_ext_struct_indices, _fp_ext_series_name_to_idx
    global _fp_lsp_indices, _fp_algo_indices, _fp_moc_indices
    global _fp_reversal_profile_by_source, _fp_ext50_trendline_indices
    global _fp_htf_weekly_indices, _fp_htf_monthly_indices
    global _fp_htf_weekly_base, _fp_htf_monthly_base

    _fp_expressions = expressions
    _fp_n_exprs = len(expressions)

    daily_indices = []
    _fp_ext_struct_indices = []
    _fp_lsp_indices = []
    _fp_algo_indices = []
    _fp_moc_indices = []
    _fp_reversal_profile_by_source = {}
    _fp_ext50_trendline_indices = []
    _fp_htf_weekly_indices = []
    _fp_htf_monthly_indices = []
    _fp_htf_weekly_base = []
    _fp_htf_monthly_base = []

    # Build name -> index map for finding extension series columns
    name_to_idx = {}
    for j, expr in enumerate(expressions):
        name_to_idx[expr["name"]] = j

    # Map extension series names to their column indices
    _fp_ext_series_name_to_idx = {}
    for series_name in ["ext_avgc50_adr14", "ext_avgc200_adr14"]:
        if series_name in name_to_idx:
            _fp_ext_series_name_to_idx[series_name] = name_to_idx[series_name]

    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    for j, expr in enumerate(expressions):
        compute = expr["compute"]
        if compute.get("op") == "precomputed":
            source = compute.get("source")
            if source == "lsp":
                _fp_lsp_indices.append(j)
            elif source == "algo":
                _fp_algo_indices.append(j)
            elif source == "moc":
                _fp_moc_indices.append(j)
            elif source == "reversal_profile":
                src_col = compute.get("input_column")
                cname = compute.get("constant")
                _fp_reversal_profile_by_source.setdefault(src_col, []).append((cname, j))
            elif source == "ext50_trendlines":
                _fp_ext50_trendline_indices.append(j)
            elif source == "htf":
                tf = compute.get("timeframe")
                if tf == "w":
                    _fp_htf_weekly_indices.append(j)
                    _fp_htf_weekly_base.append(compute.get("base_compute"))
                elif tf == "m":
                    _fp_htf_monthly_indices.append(j)
                    _fp_htf_monthly_base.append(compute.get("base_compute"))
        elif compute.get("op") in _ON_SERIES_OPS:
            _fp_ext_struct_indices.append(j)
        else:
            daily_indices.append(j)

    # Sub-classify daily into slow/dispatch/fallback/bool
    # Same DISPATCH_OPS set as expr_cache_builder.py:398-410
    DISPATCH_OPS = {
        "ma_slope", "ma_spread", "extension", "distance_to_maxh", "ratio_c_maxh",
        "distance_to_minl", "ratio_c_minl", "extension_slope", "extension_peak_ratio",
        "extension_ceiling_ratio", "ext_adr_multiples", "spread_slope", "pullback",
        "range_position", "range_width", "roc", "roc_delta", "adx", "adx_slope",
        "rsi", "rsi_slope", "stochastic", "cci", "di_spread", "volume_ratio",
        "candle_range_ratio", "body_range_ratio", "upper_wick_ratio", "lower_wick_ratio",
        "bop", "obv_slope", "macd_histogram", "macd_histogram_slope", "macd_line_norm",
        "bollinger_pctb", "bollinger_bandwidth", "bollinger_bandwidth_rank",
        "aroon_up_val", "aroon_down_val", "aroon_oscillator", "cmf", "cmf_slope",
        "kaufman_efficiency_ratio", "atr_ratio", "slope_ratio", "ma_undercut_depth",
        "channel_slope", "retrace_high", "retrace_low",
    }

    _fp_daily_slow_indices = []
    _fp_daily_dispatch_indices = []
    _fp_daily_fallback_indices = []
    _fp_daily_bool_ct = []
    _fp_daily_bool_st = []
    _fp_daily_bool_tir = []

    for j in daily_indices:
        op = expressions[j]["compute"].get("op", "")
        if op in SLOW_OPS:
            _fp_daily_slow_indices.append(j)
        elif op in BOOL_OPS:
            if op == "count_true":
                _fp_daily_bool_ct.append(j)
            elif op == "since_true":
                _fp_daily_bool_st.append(j)
            elif op == "true_in_row":
                _fp_daily_bool_tir.append(j)
        elif op in DISPATCH_OPS:
            _fp_daily_dispatch_indices.append(j)
        else:
            _fp_daily_fallback_indices.append(j)


# ══════════════════════════════════════════════════════════════
# FILE LOADING HELPERS
# ══════════════════════════════════════════════════════════════

def _safe_ticker(ticker):
    """Sanitize ticker for filesystem — matches expr_cache_builder convention."""
    return ticker.replace("/", "_").replace(".", "_")


def _load_state(ticker):
    """Load .state JSON file. Returns dict or None."""
    path = os.path.join(EXPR_CACHE_DIR, f"{_safe_ticker(ticker)}.state")
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def _load_lookback(ticker):
    """Load .lookback binary file. Returns float32 array (lb_rows, 196) or None."""
    path = os.path.join(EXPR_CACHE_DIR, f"{_safe_ticker(ticker)}.lookback")
    if not os.path.exists(path):
        return None
    raw = np.fromfile(path, dtype=np.float16)
    if raw.size == 0 or raw.size % N_INTERMEDIATES != 0:
        return None
    return raw.reshape(-1, N_INTERMEDIATES).astype(np.float32)


def _load_append(ticker, n_exprs):
    """Load .append binary file. Returns float32 array (n_rows, n_exprs + 196) or None."""
    path = os.path.join(EXPR_CACHE_DIR, f"{_safe_ticker(ticker)}.append")
    if not os.path.exists(path):
        return None
    file_size = os.path.getsize(path)
    if file_size == 0:
        return None
    total_cols = n_exprs + N_INTERMEDIATES
    row_bytes = total_cols * 2  # float16
    if file_size % row_bytes != 0:
        return None
    raw = np.fromfile(path, dtype=np.float16)
    return raw.reshape(-1, total_cols).astype(np.float32)


def _load_npz_tail(ticker, tail_rows=1260):
    """Load last tail_rows of .npz expression data.

    Returns (npz_tail_data, npz_n_bars) or (None, 0).
    npz_tail_data: float32 array (min(tail_rows, n_bars), n_exprs)

    Uses expr_cache_builder._open_npz so both the legacy zlib-compressed
    .npz format and the new zstd-wrapped format are supported.
    """
    path = os.path.join(EXPR_CACHE_DIR, f"{_safe_ticker(ticker)}.npz")
    if not os.path.exists(path):
        return None, 0
    try:
        from expr_cache_builder import _open_npz
        loaded = _open_npz(path)
        data = loaded["data"]
        n_bars = data.shape[0]
        tail = data[-tail_rows:] if n_bars > tail_rows else data
        if tail.dtype != np.float32:
            tail = tail.astype(np.float32)
        return tail, n_bars
    except Exception:
        return None, 0


def _load_append_dates(ticker):
    """Load .append_dates file. Returns list of date strings."""
    path = os.path.join(EXPR_CACHE_DIR, f"{_safe_ticker(ticker)}.append_dates")
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [line.strip() for line in f if line.strip()]


# ══════════════════════════════════════════════════════════════
# HISTORICAL VALUE READERS
# ══════════════════════════════════════════════════════════════

def _read_intermediate(col_idx, abs_bar_idx, lookback, append_data,
                       npz_end_bar, lb_rows, n_exprs):
    """Read one intermediate value at a historical bar index.

    Sources:
    - lookback covers bars [npz_end_bar - lb_rows + 1, npz_end_bar]
    - append covers bars [npz_end_bar + 1, ...]

    If the value is inf/NaN AND it's a cumsum column, returns NaN (caller
    should use _read_intermediate_cumsum_fallback for cumsum window sums).

    Returns: float32 value or NaN if out of range.
    """
    # Check lookback range
    lb_start_bar = npz_end_bar - lb_rows + 1
    if lb_start_bar <= abs_bar_idx <= npz_end_bar:
        row = abs_bar_idx - lb_start_bar
        val = lookback[row, col_idx]
        # Check cumsum overflow (float16 inf)
        if col_idx in _CUMSUM_COL_INDICES and (np.isinf(val) or np.isnan(val)):
            return np.nan  # Caller must use fallback
        return val

    # Check append range
    if append_data is not None and abs_bar_idx > npz_end_bar:
        row = abs_bar_idx - npz_end_bar - 1
        if 0 <= row < append_data.shape[0]:
            return append_data[row, n_exprs + col_idx]

    return np.nan


def _read_intermediate_window(col_idx, start_bar, end_bar, lookback, append_data,
                              npz_end_bar, lb_rows, n_exprs):
    """Read a window of intermediate values [start_bar, end_bar] inclusive.

    Returns: numpy float32 array of length (end_bar - start_bar + 1).
    Out-of-range bars return NaN.
    """
    length = end_bar - start_bar + 1
    result = np.full(length, np.nan, dtype=np.float32)

    lb_start_bar = npz_end_bar - lb_rows + 1

    for i in range(length):
        bar = start_bar + i
        if lb_start_bar <= bar <= npz_end_bar:
            result[i] = lookback[bar - lb_start_bar, col_idx]
        elif append_data is not None and bar > npz_end_bar:
            row = bar - npz_end_bar - 1
            if 0 <= row < append_data.shape[0]:
                result[i] = append_data[row, n_exprs + col_idx]

    return result


def _read_expr(expr_idx, abs_bar_idx, npz_tail, append_data,
               npz_end_bar, npz_tail_start_bar, n_exprs):
    """Read one expression value at a historical bar index.

    Sources:
    - npz_tail covers bars [npz_tail_start_bar, npz_end_bar]
    - append covers bars [npz_end_bar + 1, ...] (expression columns only = first n_exprs)

    Returns: float32 value or NaN if out of range.
    """
    if npz_tail is not None and npz_tail_start_bar <= abs_bar_idx <= npz_end_bar:
        row = abs_bar_idx - npz_tail_start_bar
        if 0 <= row < npz_tail.shape[0]:
            return npz_tail[row, expr_idx]

    if append_data is not None and abs_bar_idx > npz_end_bar:
        row = abs_bar_idx - npz_end_bar - 1
        if 0 <= row < append_data.shape[0]:
            return append_data[row, expr_idx]

    return np.nan


def _read_expr_window(expr_idx, start_bar, end_bar, npz_tail, append_data,
                      npz_end_bar, npz_tail_start_bar, n_exprs):
    """Read a window of expression values [start_bar, end_bar] inclusive.

    Returns: numpy float32 array.
    """
    length = end_bar - start_bar + 1
    result = np.full(length, np.nan, dtype=np.float32)

    for i in range(length):
        bar = start_bar + i
        if npz_tail is not None and npz_tail_start_bar <= bar <= npz_end_bar:
            row = bar - npz_tail_start_bar
            if 0 <= row < npz_tail.shape[0]:
                result[i] = npz_tail[row, expr_idx]
        elif append_data is not None and bar > npz_end_bar:
            row = bar - npz_end_bar - 1
            if 0 <= row < append_data.shape[0]:
                result[i] = append_data[row, expr_idx]

    return result


def _sma_from_cumsum(cumsum_name, cumsum_today, period, bar_idx,
                     lookback, append_data, npz_end_bar, lb_rows, n_exprs,
                     prev_im=None, today_source=None, src_history=None):
    """Compute SMA for a rolling window of P bars.

    Uses float64 source history from state when available (avoids float16
    precision loss from lookback). Falls back to lookback reads otherwise.
    today_source: current bar's raw source value at float64.
    src_history: dict of source name → list of float64 values (last N bars).
    """
    # Map cumsum name to src_history key
    _SRC_HIST_KEY = {
        "cumsum_close": "close", "cumsum_volume": "volume",
        "cumsum_tr": "true_range", "cumsum_hl": "hl",
        "cumsum_gains": "gains", "cumsum_losses": "losses",
        "cumsum_bop_raw": "bop_raw", "cumsum_mfv": "mfv",
        "cumsum_abs_diff": "abs_diff", "cumsum_tp": "tp",
        "cumsum_c2": "c2",
    }

    hist_key = _SRC_HIST_KEY.get(cumsum_name)
    if src_history is not None and hist_key in src_history and today_source is not None:
        hist = src_history[hist_key]
        # hist has the last N bars (N ≤ 200) ending at bar_idx-1
        # We need bars [bar_idx-period+1, bar_idx] = P values
        # hist[-k] = value at bar_idx-1-k+1 = bar_idx-k
        if len(hist) >= period - 1:
            # Take last (period-1) values from hist + today's value
            window = hist[-(period - 1):] + [today_source]
            return sum(window) / period
        # Not enough history — fall below to lookback

    # Fallback: read from lookback (float16) + today's value
    cidx = INTERMEDIATE_COL_INDEX
    past_bar = bar_idx - period
    source_name = _CUMSUM_SOURCE_MAP.get(cumsum_name)

    if source_name is not None and source_name in cidx:
        source_col_idx = cidx[source_name]
        window = _read_intermediate_window(source_col_idx, past_bar + 1, bar_idx - 1,
                                           lookback, append_data, npz_end_bar,
                                           lb_rows, n_exprs)
        if today_source is not None:
            window = np.append(window, today_source)
    elif cumsum_name == "cumsum_hl":
        h_win = _read_intermediate_window(cidx["high"], past_bar + 1, bar_idx - 1,
                                          lookback, append_data, npz_end_bar,
                                          lb_rows, n_exprs)
        l_win = _read_intermediate_window(cidx["low"], past_bar + 1, bar_idx - 1,
                                          lookback, append_data, npz_end_bar,
                                          lb_rows, n_exprs)
        window = h_win - l_win
        if today_source is not None:
            window = np.append(window, today_source)
    elif cumsum_name == "cumsum_c2":
        c_win = _read_intermediate_window(cidx["close"], past_bar + 1, bar_idx - 1,
                                          lookback, append_data, npz_end_bar,
                                          lb_rows, n_exprs)
        window = c_win ** 2
        if today_source is not None:
            window = np.append(window, today_source)
    else:
        return np.nan

    if np.all(np.isnan(window)):
        return np.nan
    return float(np.nanmean(window))


def _sum_from_cumsum(cumsum_name, cumsum_today, period, bar_idx,
                     lookback, append_data, npz_end_bar, lb_rows, n_exprs,
                     prev_im=None, today_source=None, src_history=None):
    """Compute rolling SUM for a window of P bars.

    Same as _sma_from_cumsum but returns raw sum (not divided by P).
    """
    _SRC_HIST_KEY = {
        "cumsum_close": "close", "cumsum_volume": "volume",
        "cumsum_mfv": "mfv",
    }

    hist_key = _SRC_HIST_KEY.get(cumsum_name)
    if src_history is not None and hist_key in src_history and today_source is not None:
        hist = src_history[hist_key]
        if len(hist) >= period - 1:
            window = hist[-(period - 1):] + [today_source]
            return sum(window)

    cidx = INTERMEDIATE_COL_INDEX
    past_bar = bar_idx - period
    source_name = _CUMSUM_SOURCE_MAP.get(cumsum_name)
    if source_name is not None and source_name in cidx:
        source_col_idx = cidx[source_name]
        window = _read_intermediate_window(source_col_idx, past_bar + 1, bar_idx - 1,
                                           lookback, append_data, npz_end_bar,
                                           lb_rows, n_exprs)
        if today_source is not None:
            window = np.append(window, today_source)
    elif cumsum_name == "cumsum_hl":
        h_win = _read_intermediate_window(cidx["high"], past_bar + 1, bar_idx - 1,
                                          lookback, append_data, npz_end_bar,
                                          lb_rows, n_exprs)
        l_win = _read_intermediate_window(cidx["low"], past_bar + 1, bar_idx - 1,
                                          lookback, append_data, npz_end_bar,
                                          lb_rows, n_exprs)
        window = h_win - l_win
        if today_source is not None:
            window = np.append(window, today_source)
    else:
        return np.nan

    if np.all(np.isnan(window)):
        return np.nan
    return float(np.nansum(window))


def _safe_div(a, b):
    """Safe scalar division. Returns NaN if b is 0 or NaN."""
    if b == 0 or np.isnan(b) or np.isnan(a):
        return np.nan
    return a / b


# ══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════

def _forward_prop_one_ticker(args):
    """Forward-propagate one new bar for an existing ticker.

    Args: (ticker, today_ohlcv_dict, df_dict, weekly_df_dict, monthly_df_dict)
        today_ohlcv_dict: {open, high, low, close, volume, date} — single bar values
        df_dict: full daily OHLCV as dict of numpy arrays (for LSP/algo full scan)
        weekly_df_dict / monthly_df_dict: HTF OHLCV dicts or None

    Returns: (ticker, total_n_bars, last_date_str) on success
             (None, None, None) on failure

    Side effects:
        - Appends one row (16,001 float16 values) to {ticker}.append
        - Appends one date string to {ticker}.append_dates
        - Updates {ticker}.lookback (sliding window)
        - Overwrites {ticker}.state (JSON)
    """
    ticker, today_ohlcv, df_dict, weekly_df_dict, monthly_df_dict = args
    n_exprs = _fp_n_exprs

    try:
        # ── Load files ──
        state = _load_state(ticker)
        if state is None:
            return (None, None, None)

        lookback = _load_lookback(ticker)
        if lookback is None:
            return (None, None, None)

        append_data = _load_append(ticker, n_exprs)
        npz_tail, npz_n_bars = _load_npz_tail(ticker)

        lb_rows = lookback.shape[0]
        n_appended = append_data.shape[0] if append_data is not None else 0

        # Bar addressing
        # state["bar_index"] is the absolute bar index of the last computed bar
        bar_idx = int(state["bar_index"]) + 1  # today's bar index
        npz_end_bar = int(state["bar_index"]) - n_appended
        npz_tail_start_bar = npz_end_bar - (npz_tail.shape[0] - 1) if npz_tail is not None else 0

        # Today's OHLCV
        today_o = float(today_ohlcv["open"])
        today_h = float(today_ohlcv["high"])
        today_l = float(today_ohlcv["low"])
        today_c = float(today_ohlcv["close"])
        today_v = float(today_ohlcv["volume"])
        today_date = today_ohlcv["date"]

        # Allocate output row
        expr_row = np.full(n_exprs, np.nan, dtype=np.float32)

        # ── Phase 1: Daily intermediates (196 values) ──
        im, new_state = _compute_daily_intermediates(
            today_o, today_h, today_l, today_c, today_v,
            state, lookback, append_data, npz_end_bar,
            lb_rows, n_exprs, bar_idx,
        )

        # ── Build full-precision intermediates for shifted reads ──
        # Same as what _compute_ticker_full uses: ExpressionEngine + build_numpy_intermediates
        # on the full daily OHLCV. Provides float64 values for all shifted intermediate reads,
        # matching the truth exactly. Cost: ~0.3s/ticker.
        from scripts.expression_engine import ExpressionEngine as _EE
        from expr_cache_builder import build_numpy_intermediates as _bni
        _fp_df = pd.DataFrame(df_dict)
        _fp_df["date"] = pd.to_datetime(_fp_df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            _fp_df[col] = pd.to_numeric(_fp_df[col], errors="coerce")
        _fp_engine = _EE(_fp_df)
        _fp_full_im = _bni(_fp_engine)

        # ── Phase 2: Daily expressions ──
        _compute_daily_expressions(
            expr_row, im, state, new_state, bar_idx,
            lookback, append_data, npz_end_bar, lb_rows, n_exprs,
            npz_tail, npz_tail_start_bar, full_im=_fp_full_im,
            fp_engine=_fp_engine,
        )

        # ── Phase 3: HTF expressions (weekly + monthly) ──
        _compute_htf_expressions(
            expr_row, df_dict, weekly_df_dict, monthly_df_dict, today_date,
        )

        # ── Phase 4: Extension structure ──
        _compute_ext_struct_expressions(
            expr_row, im, state, new_state, bar_idx,
            lookback, append_data, npz_end_bar, lb_rows, n_exprs,
            npz_tail, npz_tail_start_bar, full_im=_fp_full_im,
        )

        # ── Phase 5: LSP + Algo + MOC + Reversal Profile + Trendlines ──
        _compute_lsp_algo_expressions(expr_row, df_dict)
        _compute_moc_expressions(expr_row, df_dict, state, new_state)
        # Reversal profile must run BEFORE ext-struct's prev_ext update would
        # overwrite state["ext_prev_*"] (which it already did in Phase 4 by
        # writing to new_state, not state — so reading from state is still
        # the previous bar's value here). And it must run AFTER Phase 2 wrote
        # the new ext value into expr_row.
        _compute_reversal_profile_expressions(expr_row, state, new_state, bar_idx)
        # Trendlines depends on Levels (just filled into expr_row) + ext50
        # history (in state). Must run AFTER reversal_profile.
        _compute_ext50_trendline_expressions(expr_row, state, new_state)

        # Save prev_im for next forward-prop's shifted reads
        new_state["prev_im"] = {name: float(im.get(name, 0.0)) for name in INTERMEDIATE_COLUMNS}

        # Update src_history: append today's values, drop oldest if > 200
        _src_hist = state.get("src_history", {})
        _new_src = {
            "close": _src_hist.get("close", []) + [today_c],
            "volume": _src_hist.get("volume", []) + [today_v],
            "true_range": _src_hist.get("true_range", []) + [im["true_range"]],
            "hl": _src_hist.get("hl", []) + [today_h - today_l],
            "gains": _src_hist.get("gains", []) + [im["gains"]],
            "losses": _src_hist.get("losses", []) + [im["losses"]],
            "bop_raw": _src_hist.get("bop_raw", []) + [im["bop_raw"]],
            "mfv": _src_hist.get("mfv", []) + [im["mfv"]],
            "abs_diff": _src_hist.get("abs_diff", []) + [im["abs_diff"]],
            "tp": _src_hist.get("tp", []) + [im["tp"]],
            "c2": _src_hist.get("c2", []) + [today_c * today_c],
            "high": _src_hist.get("high", []) + [today_h],
            "low": _src_hist.get("low", []) + [today_l],
        }
        for k in _new_src:
            if len(_new_src[k]) > 200:
                _new_src[k] = _new_src[k][-200:]
        new_state["src_history"] = _new_src

        # ── Phase 6: Save ──
        _save_forward_prop(
            ticker, expr_row, im, state, new_state, bar_idx,
            lookback, append_data, today_date, n_exprs,
        )

        # Compute total bars after this append
        total_n_bars = npz_n_bars + n_appended + 1
        date_str = str(today_date)[:10] if hasattr(today_date, 'isoformat') else str(today_date)[:10]
        return (ticker, total_n_bars, date_str)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return (None, None, None)


# ══════════════════════════════════════════════════════════════
# PHASE 1: DAILY INTERMEDIATES
# ══════════════════════════════════════════════════════════════

def _compute_daily_intermediates(today_o, today_h, today_l, today_c, today_v,
                                  state, lookback, append_data, npz_end_bar,
                                  lb_rows, n_exprs, bar_idx):
    """Compute all 196 intermediate values for today's bar.

    Every formula cites its profiling_engine.py source for audit.

    Returns: (im_dict, new_state_dict)
        im_dict: {name: float} for all 196 intermediates
        new_state_dict: updated state values (merged into state by caller)
    """
    cidx = INTERMEDIATE_COL_INDEX
    im = {}
    ns = {}  # new_state
    _prev_im = state.get("prev_im")  # float64 intermediates from previous bar
    _src_hist = state.get("src_history")  # float64 source history for SMA

    prev_c = state["prev_close"]
    prev_h = state["prev_high"]
    prev_l = state["prev_low"]

    # Helper: read one intermediate value at a past bar
    def _ri(name, abs_bar):
        return _read_intermediate(cidx[name], abs_bar, lookback, append_data,
                                  npz_end_bar, lb_rows, n_exprs)

    # Helper: read a window of intermediate values
    def _riw(name, start_bar, end_bar):
        return _read_intermediate_window(cidx[name], start_bar, end_bar,
                                         lookback, append_data, npz_end_bar,
                                         lb_rows, n_exprs)

    # ── Step 1: OHLCV copies + raw per-bar values ──
    im["close"] = today_c
    im["open"] = today_o
    im["high"] = today_h
    im["low"] = today_l
    im["volume"] = today_v
    im["pct"] = today_c / 100.0

    # True range: max(H-L, |H-prevC|, |L-prevC|) — profiling_engine.py:132-138
    tr1 = today_h - today_l
    tr2 = abs(today_h - prev_c)
    tr3 = abs(today_l - prev_c)
    im["true_range"] = max(tr1, tr2, tr3)

    # Gains/losses for RSI — profiling_engine.py:149-151
    delta_c = today_c - prev_c
    im["gains"] = max(0.0, delta_c)
    im["losses"] = max(0.0, -delta_c)

    # Typical price — profiling_engine.py:185
    im["tp"] = (today_h + today_l + today_c) / 3.0

    # BOP raw — profiling_engine.py:268-270
    hl = today_h - today_l
    im["bop_raw"] = (today_c - today_o) / hl if hl != 0 else 0.0

    # MFV for CMF — profiling_engine.py:311-314
    mfm = ((today_c - today_l) - (today_h - today_c)) / hl if hl != 0 else 0.0
    im["mfv"] = mfm * today_v

    # Abs diff for Kaufman — profiling_engine.py:321
    im["abs_diff"] = abs(delta_c)

    # ── Step 2: Update cumsums ──
    im["cumsum_close"] = state["cumsum_close"] + today_c
    im["cumsum_volume"] = state["cumsum_volume"] + today_v
    im["cumsum_hl"] = state["cumsum_hl"] + hl
    im["cumsum_tr"] = state["cumsum_tr"] + im["true_range"]
    im["cumsum_bop_raw"] = state["cumsum_bop_raw"] + im["bop_raw"]
    im["cumsum_mfv"] = state["cumsum_mfv"] + im["mfv"]
    im["cumsum_abs_diff"] = state["cumsum_abs_diff"] + im["abs_diff"]
    im["cumsum_tp"] = state["cumsum_tp"] + im["tp"]
    im["cumsum_c2"] = state["cumsum_c2"] + today_c * today_c
    im["cumsum_gains"] = state["cumsum_gains"] + im["gains"]
    im["cumsum_losses"] = state["cumsum_losses"] + im["losses"]

    # Copy cumsums to new_state
    for cs_name in _CUMSUM_COL_NAMES:
        ns[cs_name] = im[cs_name]

    # ── Step 3: SMA intermediates via cumsum ──

    # avgc{P}: SMA of close — profiling_engine.py:73-75
    for p in SMA_CLOSE_PERIODS:
        im[f"avgc{p}"] = _sma_from_cumsum("cumsum_close", im["cumsum_close"], p,
                                           bar_idx, lookback, append_data,
                                           npz_end_bar, lb_rows, n_exprs,
                                           prev_im=_prev_im, today_source=today_c, src_history=_src_hist)

    # avgv{P}: SMA of volume — profiling_engine.py:73-75
    for p in SMA_VOL_PERIODS:
        im[f"avgv{p}"] = _sma_from_cumsum("cumsum_volume", im["cumsum_volume"], p,
                                           bar_idx, lookback, append_data,
                                           npz_end_bar, lb_rows, n_exprs,
                                           prev_im=_prev_im, today_source=today_v, src_history=_src_hist)

    # atr14: SMA of true_range, period=14 — profiling_engine.py:141-144
    im["atr14"] = _sma_from_cumsum("cumsum_tr", im["cumsum_tr"], 14,
                                    bar_idx, lookback, append_data,
                                    npz_end_bar, lb_rows, n_exprs,
                                    prev_im=_prev_im, today_source=im["true_range"], src_history=_src_hist)

    # adr14: SMA of (H-L), period=14
    im["adr14"] = _sma_from_cumsum("cumsum_hl", im["cumsum_hl"], 14,
                                    bar_idx, lookback, append_data,
                                    npz_end_bar, lb_rows, n_exprs,
                                    prev_im=_prev_im, today_source=today_h - today_l, src_history=_src_hist)

    # ── Step 4: RSI — SMA-based, NOT EMA — profiling_engine.py:147-155 ──
    for p in RSI_PERIODS:
        avg_gain = _sma_from_cumsum("cumsum_gains", im["cumsum_gains"], p,
                                     bar_idx, lookback, append_data,
                                     npz_end_bar, lb_rows, n_exprs,
                                     prev_im=_prev_im, today_source=im["gains"], src_history=_src_hist)
        avg_loss = _sma_from_cumsum("cumsum_losses", im["cumsum_losses"], p,
                                     bar_idx, lookback, append_data,
                                     npz_end_bar, lb_rows, n_exprs,
                                     prev_im=_prev_im, today_source=im["losses"], src_history=_src_hist)
        rs = _safe_div(avg_gain, avg_loss)
        if np.isnan(rs):
            im[f"rsi{p}"] = np.nan
        else:
            im[f"rsi{p}"] = 100.0 - 100.0 / (1.0 + rs)

    # ── Step 5: EMA intermediates ──

    # xavgc{P}: EMA of close — profiling_engine.py:78-80
    for p in EMA_CLOSE_PERIODS:
        alpha = 2.0 / (p + 1)
        prev_ema = state[f"xavgc{p}"]
        val = alpha * today_c + (1.0 - alpha) * prev_ema
        im[f"xavgc{p}"] = val
        ns[f"xavgc{p}"] = val

    # MACD extra EMAs: periods (6, 17, 19, 26, 35) not in EMA_CLOSE_PERIODS
    _macd_extra_periods = set()
    for fast, slow in MACD_PAIRS:
        if fast not in EMA_CLOSE_PERIODS:
            _macd_extra_periods.add(fast)
        if slow not in EMA_CLOSE_PERIODS:
            _macd_extra_periods.add(slow)
    for p in _macd_extra_periods:
        alpha = 2.0 / (p + 1)
        prev_ema = state.get(f"macd_ema_{p}", 0.0)
        val = alpha * today_c + (1.0 - alpha) * prev_ema
        ns[f"macd_ema_{p}"] = val
        # Store for MACD line computation below
        im[f"_macd_ema_{p}"] = val

    # MACD lines: EMA(fast) - EMA(slow) — profiling_engine.py:170-172
    # Use xavgc for periods in EMA_CLOSE_PERIODS, macd_ema for others
    def _ema_val(p):
        if p in EMA_CLOSE_PERIODS:
            return im[f"xavgc{p}"]
        return im.get(f"_macd_ema_{p}", np.nan)

    for fast, slow in MACD_PAIRS:
        im[f"macd_{fast}_{slow}"] = _ema_val(fast) - _ema_val(slow)

    # MACD signal lines and histogram (EMA of MACD line)
    # Used by dispatch for macd_histogram expression
    for fast, slow, signal_p in MACD_SIGNAL_CONFIGS:
        macd_line = im[f"macd_{fast}_{slow}"]
        alpha_s = 2.0 / (signal_p + 1)
        prev_sig = state[f"macd_signal_{fast}_{slow}"]
        sig_val = alpha_s * macd_line + (1.0 - alpha_s) * prev_sig
        ns[f"macd_signal_{fast}_{slow}"] = sig_val

    # OBV — profiling_engine.py:261-264
    sign = 1.0 if delta_c > 0 else (-1.0 if delta_c < 0 else 0.0)
    obv_val = state["obv_prev"] + sign * today_v
    im["obv"] = obv_val
    ns["obv_prev"] = obv_val

    # ── Step 6: ADX chain — profiling_engine.py:193-225 ──
    # DM uses mutual-exclusion formula (up > down AND up > 0)
    # DM smoothing uses EMA. ATR is SMA, period-MATCHED. ADX uses EMA.
    up_move = today_h - prev_h
    down_move = prev_l - today_l
    dm_plus_raw = up_move if (up_move > down_move and up_move > 0) else 0.0
    dm_minus_raw = down_move if (down_move > up_move and down_move > 0) else 0.0

    for p in ADX_PERIODS:
        alpha = 2.0 / (p + 1)

        # EMA of DM+ — profiling_engine.py:223
        prev_dmp = state[f"ema_dmp_{p}"]
        ema_dmp = alpha * dm_plus_raw + (1.0 - alpha) * prev_dmp
        ns[f"ema_dmp_{p}"] = ema_dmp

        # EMA of DM- — profiling_engine.py:224
        prev_dmm = state[f"ema_dmm_{p}"]
        ema_dmm = alpha * dm_minus_raw + (1.0 - alpha) * prev_dmm
        ns[f"ema_dmm_{p}"] = ema_dmm

        # ATR(P) — SMA of true_range, period-matched — profiling_engine.py:222
        atr_p = _sma_from_cumsum("cumsum_tr", im["cumsum_tr"], p,
                                  bar_idx, lookback, append_data,
                                  npz_end_bar, lb_rows, n_exprs,
                                  prev_im=_prev_im, today_source=im["true_range"])

        # DI+, DI- — profiling_engine.py:223-224
        di_p = _safe_div(100.0 * ema_dmp, atr_p)
        di_m = _safe_div(100.0 * ema_dmm, atr_p)
        im[f"diplus{p}"] = di_p
        im[f"diminus{p}"] = di_m

        # DX and ADX — profiling_engine.py:198-199
        di_sum = di_p + di_m
        dx = _safe_div(abs(di_p - di_m), di_sum) * 100.0 if not np.isnan(di_sum) else np.nan
        prev_dx = state[f"ema_dx_{p}"]
        adx_val = alpha * dx + (1.0 - alpha) * prev_dx if not np.isnan(dx) else prev_dx
        im[f"adx{p}"] = adx_val
        ns[f"ema_dx_{p}"] = adx_val

    # ── Step 7: BOP — SMA of (C-O)/(H-L) — profiling_engine.py:267-271 ──
    for p in BOP_PERIODS:
        im[f"bop{p}"] = _sma_from_cumsum("cumsum_bop_raw", im["cumsum_bop_raw"], p,
                                          bar_idx, lookback, append_data,
                                          npz_end_bar, lb_rows, n_exprs,
                                          prev_im=_prev_im, today_source=im["bop_raw"], src_history=_src_hist)

    # ── Step 8: Bollinger bands — profiling_engine.py:228-244 ──
    # stddev uses ddof=1 (sample std) — .rolling().std() default
    # Always use direct window approach to avoid float16 cumsum precision loss
    for p in BOLL_PERIODS:
        avgc_p = im.get(f"avgc{p}", np.nan)
        # Use src_history for close window if available (float64 precision)
        if _src_hist and "close" in _src_hist and len(_src_hist["close"]) >= p - 1:
            window = np.array(_src_hist["close"][-(p - 1):] + [today_c], dtype=np.float64)
        else:
            past_bar = bar_idx - p
            window = _riw("close", past_bar + 1, bar_idx - 1)
            window = np.append(window, today_c)
        if np.all(np.isnan(window)):
            im[f"stddev_{p}"] = np.nan
            im[f"bbtop_{p}"] = np.nan
            im[f"bbbot_{p}"] = np.nan
        else:
            sd = float(np.nanstd(window, ddof=1))
            im[f"stddev_{p}"] = sd
            im[f"bbtop_{p}"] = avgc_p + 2.0 * sd
            im[f"bbbot_{p}"] = avgc_p - 2.0 * sd

    # ── Step 9: Rolling max/min — state-tracked with rescan fallback ──

    def _rolling_extreme(kind, periods, value_name, today_val, state_prefix, idx_prefix):
        """Compute rolling max or min for a set of periods.

        kind: "max" or "min"
        """
        cmp = (lambda a, b: a >= b) if kind == "max" else (lambda a, b: a <= b)
        find_fn = np.nanargmax if kind == "max" else np.nanargmin

        for p in periods:
            idx_key = f"{idx_prefix}{p}"
            prev_idx = int(state.get(idx_key, bar_idx - 1))
            name = f"{state_prefix}{p}"

            if cmp(today_val, _ri(value_name, prev_idx) if prev_idx != bar_idx else today_val):
                # Today beats or ties the current extreme
                im[name] = today_val
                ns[idx_key] = bar_idx
            elif prev_idx >= bar_idx - p + 1:
                # Current extreme still in window
                im[name] = _ri(value_name, prev_idx)
                ns[idx_key] = prev_idx
            else:
                # Old extreme fell out — rescan
                window = _riw(value_name, bar_idx - p + 1, bar_idx - 1)
                # Append today's value
                window = np.append(window, today_val)
                if np.all(np.isnan(window)):
                    im[name] = np.nan
                    ns[idx_key] = bar_idx
                else:
                    best_offset = int(find_fn(window))
                    best_bar = bar_idx - p + 1 + best_offset
                    im[name] = float(window[best_offset])
                    ns[idx_key] = best_bar

    # maxh{P} — rolling max of high
    _rolling_extreme("max", MAXH_PERIODS, "high", today_h, "maxh", "maxh_idx_")
    # minl{P} — rolling min of low
    _rolling_extreme("min", MINL_PERIODS, "low", today_l, "minl", "minl_idx_")
    # maxc{P} — rolling max of close
    _rolling_extreme("max", MAXC_PERIODS, "close", today_c, "maxc", "maxc_idx_")

    # ── Step 10: Stochastic — profiling_engine.py:175-180 ──
    # raw_k from rolling max/min computed directly per stoch period
    # stoch = SMA(3) of raw_k
    for p in STOCH_PERIODS:
        # Compute rolling max(high, P) and rolling min(low, P) directly
        h_window = _riw("high", bar_idx - p + 1, bar_idx - 1)
        h_window = np.append(h_window, today_h)
        l_window = _riw("low", bar_idx - p + 1, bar_idx - 1)
        l_window = np.append(l_window, today_l)

        max_h = float(np.nanmax(h_window))
        min_l = float(np.nanmin(l_window))
        denom = max_h - min_l
        raw_k = (today_c - min_l) / denom * 100.0 if denom != 0 else np.nan

        # SMA(3) of raw_k using prev1, prev2 from state
        prev1 = state[f"raw_k_{p}_prev1"]
        prev2 = state[f"raw_k_{p}_prev2"]
        stoch_val = (prev2 + prev1 + raw_k) / 3.0 if not np.isnan(raw_k) else np.nan
        im[f"stoch{p}"] = stoch_val

        # Update state: shift raw_k history
        ns[f"raw_k_{p}_prev2"] = prev1
        ns[f"raw_k_{p}_prev1"] = raw_k if not np.isnan(raw_k) else prev1

    # ── Step 11: CCI — profiling_engine.py:183-190 ──
    for p in CCI_PERIODS:
        tp_window = _riw("tp", bar_idx - p + 1, bar_idx - 1)
        tp_window = np.append(tp_window, im["tp"])

        if np.all(np.isnan(tp_window)):
            im[f"cci{p}"] = np.nan
        else:
            tp_sma = float(np.nanmean(tp_window))
            mean_dev = float(np.nanmean(np.abs(tp_window - tp_sma)))
            im[f"cci{p}"] = _safe_div(im["tp"] - tp_sma, 0.015 * mean_dev)

    # ── Step 12: Aroon — profiling_engine.py:247-258 ──
    for p in AROON_PERIODS:
        h_window = _riw("high", bar_idx - p + 1, bar_idx - 1)
        h_window = np.append(h_window, today_h)
        l_window = _riw("low", bar_idx - p + 1, bar_idx - 1)
        l_window = np.append(l_window, today_l)

        if np.all(np.isnan(h_window)):
            im[f"aroon_up_{p}"] = np.nan
            im[f"aroon_down_{p}"] = np.nan
        else:
            # argmax returns first (leftmost) occurrence — matches profiling_engine
            max_pos = int(np.nanargmax(h_window))
            min_pos = int(np.nanargmin(l_window))
            bars_since_max = p - 1 - max_pos
            bars_since_min = p - 1 - min_pos
            im[f"aroon_up_{p}"] = (p - bars_since_max) / p * 100.0
            im[f"aroon_down_{p}"] = (p - bars_since_min) / p * 100.0

        # Track indices for Aroon state
        ns[f"aroon_maxh_idx_{p}"] = bar_idx - p + 1 + int(np.nanargmax(h_window)) if not np.all(np.isnan(h_window)) else bar_idx
        ns[f"aroon_minl_idx_{p}"] = bar_idx - p + 1 + int(np.nanargmin(l_window)) if not np.all(np.isnan(l_window)) else bar_idx

    # ── Step 13: CMF — profiling_engine.py:310-315 ──
    # CMF = sum(MFV, P) / sum(V, P) — uses raw sums, not means
    for p in CMF_PERIODS:
        sum_mfv = _sum_from_cumsum("cumsum_mfv", im["cumsum_mfv"], p,
                                    bar_idx, lookback, append_data,
                                    npz_end_bar, lb_rows, n_exprs,
                                    prev_im=_prev_im, today_source=im["mfv"], src_history=_src_hist)
        sum_vol = _sum_from_cumsum("cumsum_volume", im["cumsum_volume"], p,
                                    bar_idx, lookback, append_data,
                                    npz_end_bar, lb_rows, n_exprs,
                                    prev_im=_prev_im, today_source=today_v)
        im[f"cmf_{p}"] = _safe_div(sum_mfv, sum_vol)

    # ── Step 14: Kaufman efficiency — profiling_engine.py:318-322 ──
    for p in KAUF_PERIODS:
        close_p_ago = _ri("close", bar_idx - p)
        direction = abs(today_c - close_p_ago) if not np.isnan(close_p_ago) else np.nan

        # Sum of abs diffs over P bars = cumsum_abs_diff[i] - cumsum_abs_diff[i-P]
        past_cumsum_ad = _read_intermediate(cidx["cumsum_abs_diff"], bar_idx - p,
                                             lookback, append_data, npz_end_bar,
                                             lb_rows, n_exprs)
        if not np.isnan(past_cumsum_ad) and not np.isinf(past_cumsum_ad):
            volatility = im["cumsum_abs_diff"] - past_cumsum_ad
        else:
            # Fallback: read P abs_diff values and sum
            ad_window = _riw("abs_diff", bar_idx - p + 1, bar_idx)
            volatility = float(np.nansum(ad_window)) if not np.all(np.isnan(ad_window)) else np.nan

        im[f"kauf_eff_{p}"] = _safe_div(direction, volatility)

    # ── Step 15: Update state for next bar ──
    ns["prev_close"] = today_c
    ns["prev_high"] = today_h
    ns["prev_low"] = today_l
    ns["bar_index"] = bar_idx

    return im, ns


# ══════════════════════════════════════════════════════════════
# PHASE 2: DAILY EXPRESSIONS (scalar dispatch + booleans + fallbacks)
# ══════════════════════════════════════════════════════════════

def _get_im(im, key):
    """Get intermediate value, return NaN if missing."""
    return im.get(key, np.nan)


def _get_ma_scalar(im, ma_name):
    """Resolve MA name to scalar value from intermediates."""
    return im.get(ma_name, np.nan)


def _get_norm_scalar(im, norm_name):
    """Resolve normalizer name to scalar value."""
    if norm_name == "atr14":
        return im.get("atr14", np.nan)
    elif norm_name == "adr14":
        return im.get("adr14", np.nan)
    elif norm_name == "pct":
        return im.get("pct", np.nan)
    elif norm_name == "close":
        return im.get("close", np.nan)
    return np.nan


def _ri_for_dispatch(name, offset, bar_idx, lookback, append_data,
                     npz_end_bar, lb_rows, n_exprs, prev_im=None):
    """Read intermediate value at bar_idx - offset (shifted).

    Uses prev_im (float64 from state) for the previous bar to avoid
    float16 precision loss from lookback.
    """
    # For offset reaching the previous bar (bar_idx - 1), use float64 from state
    if prev_im is not None and bar_idx - offset == bar_idx - 1 and name in prev_im:
        return prev_im[name]

    cidx = INTERMEDIATE_COL_INDEX
    if name not in cidx:
        return np.nan
    return _read_intermediate(cidx[name], bar_idx - offset, lookback, append_data,
                              npz_end_bar, lb_rows, n_exprs)


def _dispatch_scalar(comp, im, bar_idx, lookback, append_data,
                     npz_end_bar, lb_rows, n_exprs,
                     state, new_state, npz_tail, npz_tail_start_bar,
                     full_im=None):
    """Scalar version of dispatch_arith_numpy — same ops, single values.

    Returns float value or NaN.
    """
    op = comp.get("op", "")
    C = im["close"]
    O = im["open"]
    H = im["high"]
    L = im["low"]
    V = im["volume"]
    def _ri(name, offset):
        # Use full_im (float64 ExpressionEngine intermediates) when available
        if full_im is not None and name in full_im:
            idx = bar_idx - offset
            arr = full_im[name]
            if 0 <= idx < len(arr):
                return float(arr[idx])
            return np.nan
        _prev_im = state.get("prev_im") if state else None
        return _ri_for_dispatch(name, offset, bar_idx, lookback, append_data,
                                npz_end_bar, lb_rows, n_exprs, prev_im=_prev_im)

    try:
        if op == "ma_slope":
            ma_now = _get_ma_scalar(im, comp["ma"])
            ma_prev = _ri(comp["ma"], comp["offset"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(ma_now - ma_prev, norm)

        elif op == "ma_spread":
            fast = _get_ma_scalar(im, comp["ma_fast"])
            slow = _get_ma_scalar(im, comp["ma_slow"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(fast - slow, norm)

        elif op == "extension":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(C - ma, norm)

        elif op == "distance_to_maxh":
            price = C if comp["price_ref"] == "C" else H
            maxh_prev = _ri(f"maxh{comp['maxh_period']}", 1)
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(maxh_prev - price, norm)

        elif op == "ratio_c_maxh":
            maxh_prev = _ri(f"maxh{comp['maxh_period']}", 1)
            return _safe_div(C, maxh_prev)

        elif op == "distance_to_minl":
            price = C if comp["price_ref"] == "C" else L
            minl_prev = _ri(f"minl{comp['minl_period']}", 1)
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(price - minl_prev, norm)

        elif op == "ratio_c_minl":
            minl_prev = _ri(f"minl{comp['minl_period']}", 1)
            return _safe_div(C, minl_prev)

        elif op == "extension_slope":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            ext_now = C - ma
            ma_prev = _ri(comp["ma"], comp["offset"])
            c_prev = _ri("close", comp["offset"])
            ext_prev = c_prev - ma_prev if not np.isnan(c_prev) and not np.isnan(ma_prev) else np.nan
            return _safe_div(ext_now - ext_prev, norm)

        elif op == "extension_peak_ratio":
            ma = _get_ma_scalar(im, comp["ma"])
            ext_now = C - ma
            lb = comp["lookback"]
            # Read extension values over lookback window from expression columns
            # The extension expr index is the one with matching ma/norm
            # Simpler: compute ext for each past bar from close-MA intermediates
            window = []
            cidx = INTERMEDIATE_COL_INDEX
            ma_name = comp["ma"]
            for off in range(1, lb):
                c_past = _read_intermediate(cidx["close"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                ma_past = _read_intermediate(cidx.get(ma_name, -1), bar_idx - off, lookback,
                                             append_data, npz_end_bar, lb_rows, n_exprs) if ma_name in cidx else np.nan
                if not np.isnan(c_past) and not np.isnan(ma_past):
                    window.append(c_past - ma_past)
            if window:
                max_ext = max(max(window), ext_now) if not np.isnan(ext_now) else max(window)
            else:
                max_ext = ext_now
            return _safe_div(ext_now, max_ext)

        elif op == "extension_ceiling_ratio":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            ext_norm_now = _safe_div(C - ma, norm)
            lb = comp["lookback"]
            # Need historical normalized extension values — read from .npz tail + .append
            # Find the expression index for this exact expression
            # Fallback: compute from intermediates over window
            window = []
            cidx = INTERMEDIATE_COL_INDEX
            ma_name = comp["ma"]
            norm_name = comp["normalizer"]
            for off in range(1, min(lb, 1260)):
                c_past = _read_intermediate(cidx["close"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                ma_past = _read_intermediate(cidx.get(ma_name, -1), bar_idx - off, lookback,
                                             append_data, npz_end_bar, lb_rows, n_exprs) if ma_name in cidx else np.nan
                norm_past = _read_intermediate(cidx.get(norm_name, -1), bar_idx - off, lookback,
                                               append_data, npz_end_bar, lb_rows, n_exprs) if norm_name in cidx else np.nan
                if not np.isnan(c_past) and not np.isnan(ma_past) and not np.isnan(norm_past) and norm_past != 0:
                    window.append((c_past - ma_past) / norm_past)
            if window:
                max_ext = max(window)
            else:
                max_ext = ext_norm_now
            return _safe_div(ext_norm_now, max_ext)

        elif op == "ext_adr_multiples":
            ma = _get_ma_scalar(im, comp["ma"])
            adr = im.get("adr14", np.nan)
            return _safe_div(C - ma, adr)

        elif op == "spread_slope":
            fast = _get_ma_scalar(im, comp["ma_fast"])
            slow = _get_ma_scalar(im, comp["ma_slow"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            spread_now = _safe_div(fast - slow, norm)
            fast_prev = _ri(comp["ma_fast"], comp["offset"])
            slow_prev = _ri(comp["ma_slow"], comp["offset"])
            norm_prev = _ri(comp["normalizer"], comp["offset"]) if comp["normalizer"] in INTERMEDIATE_COL_INDEX else np.nan
            spread_prev = _safe_div(fast_prev - slow_prev, norm_prev)
            if np.isnan(spread_now) or np.isnan(spread_prev):
                return np.nan
            return spread_now - spread_prev

        elif op == "pullback":
            maxh_val = im.get(f"maxh{comp['period']}", np.nan)
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(maxh_val - C, norm)

        elif op == "range_position":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", np.nan)
            minl = im.get(f"minl{p}", np.nan)
            return _safe_div(C - minl, maxh - minl) if not np.isnan(maxh) and not np.isnan(minl) else np.nan

        elif op == "range_width":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", np.nan)
            minl = im.get(f"minl{p}", np.nan)
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(maxh - minl, norm) if not np.isnan(maxh) and not np.isnan(minl) else np.nan

        elif op == "roc":
            c_past = _ri("close", comp["period"])
            return (_safe_div(C, c_past) * 100.0 - 100.0) if not np.isnan(c_past) else np.nan

        elif op == "roc_delta":
            p = comp["period"]
            co = comp["compare_offset"]
            c_p = _ri("close", p)
            c_co = _ri("close", co)
            c_cop = _ri("close", co + p)
            roc_now = _safe_div(C, c_p) - 1.0 if not np.isnan(c_p) else np.nan
            roc_prev = _safe_div(c_co, c_cop) - 1.0 if not np.isnan(c_co) and not np.isnan(c_cop) else np.nan
            if np.isnan(roc_now) or np.isnan(roc_prev):
                return np.nan
            return 100.0 * (roc_now - roc_prev)

        elif op == "adx":
            return im.get(f"adx{comp['period']}", np.nan)
        elif op == "adx_slope":
            a_now = im.get(f"adx{comp['period']}", np.nan)
            a_prev = _ri(f"adx{comp['period']}", comp["offset"])
            return a_now - a_prev if not np.isnan(a_now) and not np.isnan(a_prev) else np.nan
        elif op == "rsi":
            return im.get(f"rsi{comp['period']}", np.nan)
        elif op == "rsi_slope":
            r_now = im.get(f"rsi{comp['period']}", np.nan)
            r_prev = _ri(f"rsi{comp['period']}", comp["offset"])
            return r_now - r_prev if not np.isnan(r_now) and not np.isnan(r_prev) else np.nan
        elif op == "stochastic":
            return im.get(f"stoch{comp['period']}", np.nan)
        elif op == "cci":
            return im.get(f"cci{comp['period']}", np.nan)
        elif op == "di_spread":
            return im.get(f"diplus{comp['period']}", np.nan) - im.get(f"diminus{comp['period']}", np.nan)
        elif op == "volume_ratio":
            avg = im.get(f"avgv{comp['avg_period']}", np.nan)
            return _safe_div(V, avg)
        elif op == "candle_range_ratio":
            return _safe_div(H - L, im.get("atr14", np.nan))
        elif op == "body_range_ratio":
            return _safe_div(abs(C - O), H - L)
        elif op == "upper_wick_ratio":
            return _safe_div(H - max(C, O), H - L)
        elif op == "lower_wick_ratio":
            return _safe_div(min(C, O) - L, H - L)
        elif op == "bop":
            return im.get(f"bop{comp['period']}", np.nan)
        elif op == "obv_slope":
            obv_now = im.get("obv", np.nan)
            offset = comp["offset"]
            obv_prev = _ri("obv", offset)
            vol_p = comp.get("vol_period", 20)
            avg_v = im.get(f"avgv{vol_p}", np.nan)
            denom = avg_v * offset if not np.isnan(avg_v) else np.nan
            return _safe_div(obv_now - obv_prev, denom) if not np.isnan(obv_prev) else np.nan

        elif op == "macd_histogram":
            fast = comp.get("fast", 12)
            slow = comp.get("slow", 26)
            sig_p = comp.get("signal", 9)
            macd_line = im.get(f"macd_{fast}_{slow}", np.nan)
            if np.isnan(macd_line):
                return np.nan
            # Use MACD signal from state (already EMA-updated in Phase 1)
            sig_key = f"macd_signal_{fast}_{slow}"
            sig_val = new_state.get(sig_key, state.get(sig_key, np.nan))
            return macd_line - sig_val if not np.isnan(sig_val) else np.nan

        elif op == "macd_histogram_slope":
            fast = comp.get("fast", 12)
            slow = comp.get("slow", 26)
            sig_p = comp.get("signal", 9)
            offset = comp["offset"]
            macd_line = im.get(f"macd_{fast}_{slow}", np.nan)
            sig_key = f"macd_signal_{fast}_{slow}"
            sig_val = new_state.get(sig_key, state.get(sig_key, np.nan))
            hist_now = macd_line - sig_val if not np.isnan(macd_line) and not np.isnan(sig_val) else np.nan
            # Read previous histogram from expression column
            if prev_expr_row is not None:
                # We need the expression index for this exact expression
                # Simpler: compute from previous bar's MACD line and signal
                # The prev signal = state[sig_key] (before update in Phase 1)
                macd_prev = _ri(f"macd_{fast}_{slow}", offset)
                if not np.isnan(macd_prev):
                    # Approximate: we can't easily get historical signal value from intermediates
                    # Use the expression value from prev_expr_row if available
                    # This is a known limitation — use prev expression value
                    return np.nan  # TODO: implement via expression history read
                return np.nan
            return np.nan

        elif op == "macd_line_norm":
            fast = comp.get("fast", 12)
            slow = comp.get("slow", 26)
            macd_line = im.get(f"macd_{fast}_{slow}", np.nan)
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(macd_line, norm)

        elif op == "bollinger_pctb":
            p = comp["period"]
            top = im.get(f"bbtop_{p}", np.nan)
            bot = im.get(f"bbbot_{p}", np.nan)
            return _safe_div(C - bot, top - bot)

        elif op == "bollinger_bandwidth":
            p = comp["period"]
            top = im.get(f"bbtop_{p}", np.nan)
            bot = im.get(f"bbbot_{p}", np.nan)
            mid = im.get(f"avgc{p}", np.nan)
            return _safe_div(top - bot, mid)

        elif op == "bollinger_bandwidth_rank":
            p = comp["period"]
            lb = comp["lookback"]
            top_now = im.get(f"bbtop_{p}", np.nan)
            bot_now = im.get(f"bbbot_{p}", np.nan)
            mid_now = im.get(f"avgc{p}", np.nan)
            bw_now = _safe_div(top_now - bot_now, mid_now)
            if np.isnan(bw_now):
                return np.nan
            # Read historical bandwidth over lookback window
            cidx = INTERMEDIATE_COL_INDEX
            bws = [bw_now]
            for off in range(1, lb):
                t = _read_intermediate(cidx.get(f"bbtop_{p}", -1), bar_idx - off, lookback,
                                        append_data, npz_end_bar, lb_rows, n_exprs) if f"bbtop_{p}" in cidx else np.nan
                b = _read_intermediate(cidx.get(f"bbbot_{p}", -1), bar_idx - off, lookback,
                                        append_data, npz_end_bar, lb_rows, n_exprs) if f"bbbot_{p}" in cidx else np.nan
                m = _read_intermediate(cidx.get(f"avgc{p}", -1), bar_idx - off, lookback,
                                        append_data, npz_end_bar, lb_rows, n_exprs) if f"avgc{p}" in cidx else np.nan
                bw = _safe_div(t - b, m)
                if not np.isnan(bw):
                    bws.append(bw)
            if len(bws) < 2:
                return np.nan
            bw_min = min(bws)
            bw_max = max(bws)
            return _safe_div(bw_now - bw_min, bw_max - bw_min)

        elif op == "aroon_up_val":
            return im.get(f"aroon_up_{comp['period']}", np.nan)
        elif op == "aroon_down_val":
            return im.get(f"aroon_down_{comp['period']}", np.nan)
        elif op == "aroon_oscillator":
            p = comp["period"]
            up = im.get(f"aroon_up_{p}", np.nan)
            dn = im.get(f"aroon_down_{p}", np.nan)
            return up - dn if not np.isnan(up) and not np.isnan(dn) else np.nan
        elif op == "cmf":
            return im.get(f"cmf_{comp['period']}", np.nan)
        elif op == "cmf_slope":
            c_now = im.get(f"cmf_{comp['period']}", np.nan)
            c_prev = _ri(f"cmf_{comp['period']}", comp["offset"])
            return c_now - c_prev if not np.isnan(c_now) and not np.isnan(c_prev) else np.nan
        elif op == "kaufman_efficiency_ratio":
            return im.get(f"kauf_eff_{comp['period']}", np.nan)
        elif op == "atr_ratio":
            atr_now = im.get("atr14", np.nan)
            atr_prev = _ri("atr14", comp["offset"])
            return _safe_div(atr_now, atr_prev)
        elif op == "slope_ratio":
            fast_now = _get_ma_scalar(im, comp["fast_ma"])
            slow_now = _get_ma_scalar(im, comp["slow_ma"])
            offset = comp["offset"]
            fast_prev = _ri(comp["fast_ma"], offset)
            slow_prev = _ri(comp["slow_ma"], offset)
            fast_slope = fast_now - fast_prev if not np.isnan(fast_prev) else np.nan
            slow_slope = slow_now - slow_prev if not np.isnan(slow_prev) else np.nan
            return _safe_div(fast_slope, slow_slope)
        elif op == "ma_undercut_depth":
            ma = _get_ma_scalar(im, comp["ma"])
            p = comp["period"]
            norm = _get_norm_scalar(im, comp["normalizer"])
            # Read L-MA for last P bars, find min
            cidx = INTERMEDIATE_COL_INDEX
            ma_name = comp["ma"]
            diffs = [L - ma]
            for off in range(1, p):
                l_past = _read_intermediate(cidx["low"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                ma_past = _read_intermediate(cidx.get(ma_name, -1), bar_idx - off, lookback,
                                             append_data, npz_end_bar, lb_rows, n_exprs) if ma_name in cidx else np.nan
                if not np.isnan(l_past) and not np.isnan(ma_past):
                    diffs.append(l_past - ma_past)
            return _safe_div(min(diffs), norm) if diffs else np.nan
        elif op == "channel_slope":
            p = comp["period"]
            maxh_now = im.get(f"maxh{p}", np.nan)
            maxh_prev = _ri(f"maxh{p}", p)
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(maxh_now - maxh_prev, norm) if not np.isnan(maxh_prev) else np.nan
        elif op == "retrace_high":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", np.nan)
            minl = im.get(f"minl{p}", np.nan)
            return _safe_div(H - minl, maxh - minl)
        elif op == "retrace_low":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", np.nan)
            minl = im.get(f"minl{p}", np.nan)
            return _safe_div(L - minl, maxh - minl)
        else:
            return np.nan
    except Exception:
        return np.nan


def _dispatch_fallback_scalar(comp, im, bar_idx, state, new_state,
                               lookback, append_data, npz_end_bar, lb_rows, n_exprs,
                               prev_expr_row, prev_expr_idx, full_im=None):
    """Scalar forward-prop for fallback ops.

    Many use the pattern: read prev expression value, apply incremental update.
    """
    op = comp.get("op", "")
    C = im["close"]
    O = im["open"]
    H = im["high"]
    L = im["low"]
    V = im["volume"]
    prev_c = state["prev_close"]

    def _ri(name, offset):
        if full_im is not None and name in full_im:
            idx = bar_idx - offset
            arr = full_im[name]
            if 0 <= idx < len(arr):
                return float(arr[idx])
            return np.nan
        _prev_im = state.get("prev_im")
        return _ri_for_dispatch(name, offset, bar_idx, lookback, append_data,
                                npz_end_bar, lb_rows, n_exprs, prev_im=_prev_im)

    def _prev_expr():
        """Read this expression's value from the previous bar."""
        if prev_expr_row is not None and prev_expr_idx >= 0:
            return float(prev_expr_row[prev_expr_idx])
        return np.nan

    cidx = INTERMEDIATE_COL_INDEX
    try:
        # ── Trivial single-bar ops ──
        if op == "gap_size":
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(O - prev_c, norm)
        elif op == "gap_from_prior":
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(O - prev_c, norm)
        elif op == "bar_range":
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(H - L, norm)
        elif op == "is_green":
            return 1.0 if C > O else 0.0
        elif op == "is_doji":
            rng = H - L
            body = abs(C - O)
            return 1.0 if rng > 0 and body / rng < 0.1 else 0.0
        elif op == "close_above_ma":
            ma = _get_ma_scalar(im, comp["ma"])
            return 1.0 if C > ma else 0.0
        elif op == "closed_below_ma":
            ma = _get_ma_scalar(im, comp["ma"])
            return 1.0 if C < ma else 0.0
        elif op == "touched_ma":
            ma = _get_ma_scalar(im, comp["ma"])
            return 1.0 if L <= ma else 0.0
        elif op == "rvol":
            avg_p = comp.get("avg_period", 20)
            avg_v = im.get(f"avgv{avg_p}", np.nan)
            return _safe_div(V, avg_v)
        elif op == "nr_ratio":
            p = comp["period"]
            rng_today = H - L
            # Read H-L for last P bars, find max
            max_range = rng_today
            for off in range(1, p):
                h_p = _read_intermediate(cidx["high"], bar_idx - off, lookback,
                                         append_data, npz_end_bar, lb_rows, n_exprs)
                l_p = _read_intermediate(cidx["low"], bar_idx - off, lookback,
                                         append_data, npz_end_bar, lb_rows, n_exprs)
                if not np.isnan(h_p) and not np.isnan(l_p):
                    max_range = max(max_range, h_p - l_p)
            return _safe_div(rng_today, max_range)

        # ── Increment/reset ops ──
        elif op == "consecutive_up_days":
            prev = _prev_expr()
            return (prev + 1 if C > prev_c else 0.0) if not np.isnan(prev) else (1.0 if C > prev_c else 0.0)
        elif op == "consecutive_down_days":
            prev = _prev_expr()
            return (prev + 1 if C < prev_c else 0.0) if not np.isnan(prev) else (1.0 if C < prev_c else 0.0)
        elif op == "consecutive_up_roc":
            prev = _prev_expr()
            if C > prev_c:
                roc_today = (C / prev_c - 1) * 100 if prev_c != 0 else 0
                return (prev + roc_today) if not np.isnan(prev) else roc_today
            return 0.0
        elif op == "consecutive_down_roc":
            prev = _prev_expr()
            if C < prev_c:
                roc_today = (C / prev_c - 1) * 100 if prev_c != 0 else 0
                return (prev + roc_today) if not np.isnan(prev) else roc_today
            return 0.0
        elif op == "consecutive_green":
            prev = _prev_expr()
            return (prev + 1 if C > O else 0.0) if not np.isnan(prev) else (1.0 if C > O else 0.0)
        elif op == "consecutive_red":
            prev = _prev_expr()
            return (prev + 1 if C < O else 0.0) if not np.isnan(prev) else (1.0 if C < O else 0.0)
        elif op == "lower_low_sequence":
            prev = _prev_expr()
            prev_l = _ri("low", 1)
            return (prev + 1 if L < prev_l else 0.0) if not np.isnan(prev) and not np.isnan(prev_l) else 0.0
        elif op == "higher_low_formed":
            # 1.0 if today's low > running minimum so far
            prev = _prev_expr()
            prev_l = _ri("low", 1)
            if np.isnan(prev_l):
                return 0.0
            # Simplified: check if L > prev_low (approximate)
            return 1.0 if L > prev_l else 0.0

        # ── Window-scan ops ──
        elif op == "retracement_level":
            p = comp["period"]
            maxh = im.get(f"maxh{p}", np.nan)
            minl = im.get(f"minl{p}", np.nan)
            return _safe_div(C - minl, maxh - minl)

        elif op == "low_vs_ma":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(L - ma, norm)

        elif op == "high_vs_ma":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(H - ma, norm)

        elif op == "distance_from_ma":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            return _safe_div(C - ma, norm)

        elif op == "ext_slope":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            offset = comp["offset"]
            ext_now = _safe_div(C - ma, norm)
            c_prev = _ri("close", offset)
            ma_prev = _ri(comp["ma"], offset)
            norm_prev = _ri(comp["normalizer"], offset) if comp["normalizer"] in cidx else _get_norm_scalar(im, comp["normalizer"])
            ext_prev = _safe_div(c_prev - ma_prev, norm_prev) if not np.isnan(c_prev) and not np.isnan(ma_prev) else np.nan
            return ext_now - ext_prev if not np.isnan(ext_now) and not np.isnan(ext_prev) else np.nan

        elif op == "ext_accel":
            ma = _get_ma_scalar(im, comp["ma"])
            norm = _get_norm_scalar(im, comp["normalizer"])
            ext_now = _safe_div(C - ma, norm)
            c_1 = _ri("close", 1)
            ma_1 = _ri(comp["ma"], 1)
            norm_1 = _ri(comp["normalizer"], 1) if comp["normalizer"] in cidx else norm
            ext_1 = _safe_div(c_1 - ma_1, norm_1) if not np.isnan(c_1) and not np.isnan(ma_1) else np.nan
            c_2 = _ri("close", 2)
            ma_2 = _ri(comp["ma"], 2)
            norm_2 = _ri(comp["normalizer"], 2) if comp["normalizer"] in cidx else norm
            ext_2 = _safe_div(c_2 - ma_2, norm_2) if not np.isnan(c_2) and not np.isnan(ma_2) else np.nan
            if np.isnan(ext_now) or np.isnan(ext_1) or np.isnan(ext_2):
                return np.nan
            slope_now = ext_now - ext_1
            slope_prev = ext_1 - ext_2
            return slope_now - slope_prev

        elif op == "close_vs_open_ratio":
            p = comp["period"]
            # Count bullish bars in last P bars
            count = 1.0 if C > O else 0.0
            for off in range(1, p):
                c_p = _read_intermediate(cidx["close"], bar_idx - off, lookback,
                                         append_data, npz_end_bar, lb_rows, n_exprs)
                o_p = _read_intermediate(cidx["open"], bar_idx - off, lookback,
                                         append_data, npz_end_bar, lb_rows, n_exprs)
                if not np.isnan(c_p) and not np.isnan(o_p) and c_p > o_p:
                    count += 1.0
            return count / p

        elif op == "inside_bar_count":
            p = comp["period"]
            count = 0.0
            for off in range(0, p):
                if off == 0:
                    h_now, l_now = H, L
                    h_prev = _ri("high", 1)
                    l_prev = _ri("low", 1)
                else:
                    h_now = _read_intermediate(cidx["high"], bar_idx - off, lookback,
                                              append_data, npz_end_bar, lb_rows, n_exprs)
                    l_now = _read_intermediate(cidx["low"], bar_idx - off, lookback,
                                              append_data, npz_end_bar, lb_rows, n_exprs)
                    h_prev = _read_intermediate(cidx["high"], bar_idx - off - 1, lookback,
                                               append_data, npz_end_bar, lb_rows, n_exprs)
                    l_prev = _read_intermediate(cidx["low"], bar_idx - off - 1, lookback,
                                               append_data, npz_end_bar, lb_rows, n_exprs)
                if not np.isnan(h_now) and not np.isnan(l_now) and not np.isnan(h_prev) and not np.isnan(l_prev):
                    if h_now < h_prev and l_now > l_prev:
                        count += 1.0
            return count

        elif op == "outside_bar_count":
            p = comp["period"]
            count = 0.0
            for off in range(0, p):
                if off == 0:
                    h_now, l_now = H, L
                    h_prev = _ri("high", 1)
                    l_prev = _ri("low", 1)
                else:
                    h_now = _read_intermediate(cidx["high"], bar_idx - off, lookback,
                                              append_data, npz_end_bar, lb_rows, n_exprs)
                    l_now = _read_intermediate(cidx["low"], bar_idx - off, lookback,
                                              append_data, npz_end_bar, lb_rows, n_exprs)
                    h_prev = _read_intermediate(cidx["high"], bar_idx - off - 1, lookback,
                                               append_data, npz_end_bar, lb_rows, n_exprs)
                    l_prev = _read_intermediate(cidx["low"], bar_idx - off - 1, lookback,
                                               append_data, npz_end_bar, lb_rows, n_exprs)
                if not np.isnan(h_now) and not np.isnan(l_now) and not np.isnan(h_prev) and not np.isnan(l_prev):
                    if h_now > h_prev and l_now < l_prev:
                        count += 1.0
            return count

        elif op == "avg_candle_body_ratio":
            p = comp["period"]
            total = 0.0
            count = 0
            for off in range(0, p):
                if off == 0:
                    h_v, l_v, c_v, o_v = H, L, C, O
                else:
                    h_v = _read_intermediate(cidx["high"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                    l_v = _read_intermediate(cidx["low"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                    c_v = _read_intermediate(cidx["close"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                    o_v = _read_intermediate(cidx["open"], bar_idx - off, lookback,
                                            append_data, npz_end_bar, lb_rows, n_exprs)
                if not any(np.isnan(x) for x in [h_v, l_v, c_v, o_v]) and h_v != l_v:
                    total += abs(c_v - o_v) / (h_v - l_v)
                    count += 1
            return total / count if count > 0 else np.nan

        elif op == "pct_green_rolling":
            w = comp.get("window", comp.get("period", 10))
            count = 1.0 if C > O else 0.0
            for off in range(1, w):
                c_p = _read_intermediate(cidx["close"], bar_idx - off, lookback,
                                         append_data, npz_end_bar, lb_rows, n_exprs)
                o_p = _read_intermediate(cidx["open"], bar_idx - off, lookback,
                                         append_data, npz_end_bar, lb_rows, n_exprs)
                if not np.isnan(c_p) and not np.isnan(o_p) and c_p > o_p:
                    count += 1.0
            return count / w

        # ── Catch-all: return NaN for unhandled ops ──
        else:
            return np.nan

    except Exception:
        return np.nan


def _eval_bool_condition(cond_name, im, bar_idx, lookback, append_data,
                         npz_end_bar, lb_rows, n_exprs, state=None, full_im=None):
    """Evaluate one boolean condition at today's bar.

    Returns True/False. Matches expression_engine.py _bool_series().
    """
    cidx = INTERMEDIATE_COL_INDEX
    C = im["close"]
    O = im["open"]
    H = im["high"]
    L = im["low"]
    V = im["volume"]

    def _ri(name, offset):
        if full_im is not None and name in full_im:
            idx = bar_idx - offset
            arr = full_im[name]
            if 0 <= idx < len(arr):
                return float(arr[idx])
            return np.nan
        _prev_im = state.get("prev_im") if isinstance(state, dict) else None
        return _ri_for_dispatch(name, offset, bar_idx, lookback, append_data,
                                npz_end_bar, lb_rows, n_exprs, prev_im=_prev_im)

    n = cond_name

    # --- Price vs MA ---
    if   n == "c_gt_xavgc8":       return C > im.get("xavgc8", np.nan)
    elif n == "c_gt_xavgc21":      return C > im.get("xavgc21", np.nan)
    elif n == "c_gt_xavgc50":      return C > im.get("xavgc50", np.nan)
    elif n == "c_gt_xavgc100":     return C > im.get("xavgc100", np.nan)
    elif n == "c_gt_avgc50":       return C > im.get("avgc50", np.nan)
    elif n == "c_gt_avgc200":      return C > im.get("avgc200", np.nan)
    elif n == "c_lt_xavgc8":       return C < im.get("xavgc8", np.nan)
    elif n == "c_lt_xavgc21":      return C < im.get("xavgc21", np.nan)
    elif n == "c_lt_avgc50":       return C < im.get("avgc50", np.nan)
    elif n == "c_lt_avgc200":      return C < im.get("avgc200", np.nan)
    # --- Price vs prior bar ---
    elif n == "c_gt_c1":           return C > _ri("close", 1)
    elif n == "c_lt_c1":           return C < _ri("close", 1)
    elif n == "h_gt_h1":           return H > _ri("high", 1)
    elif n == "l_lt_l1":           return L < _ri("low", 1)
    elif n == "c_gt_o":            return C > O
    # --- Volume ---
    elif n == "v_gt_avgv20":       return V > im.get("avgv20", np.nan)
    elif n == "v_gt_2x_avgv20":    return V > 2 * im.get("avgv20", np.nan)
    elif n == "v_gt_avgv50":       return V > im.get("avgv50", np.nan)
    elif n == "v_lt_avgv20":       return V < im.get("avgv20", np.nan)
    elif n == "v_lt_half_avgv20":  return V < 0.5 * im.get("avgv20", np.nan)
    # --- MA vs MA ---
    elif n == "xavgc8_gt_xavgc21": return im.get("xavgc8", np.nan) > im.get("xavgc21", np.nan)
    elif n == "xavgc50_gt_xavgc200": return im.get("xavgc50", np.nan) > im.get("xavgc200", np.nan)
    elif n == "avgc50_gt_avgc200": return im.get("avgc50", np.nan) > im.get("avgc200", np.nan)
    elif n == "xavgc21_gt_avgc50": return im.get("xavgc21", np.nan) > im.get("avgc50", np.nan)
    elif n == "xavgc8_gt_avgc50":  return im.get("xavgc8", np.nan) > im.get("avgc50", np.nan)
    # --- MA direction ---
    elif n == "avgc50_rising":     return im.get("avgc50", np.nan) > _ri("avgc50", 1)
    elif n == "avgc50_falling":    return im.get("avgc50", np.nan) < _ri("avgc50", 1)
    elif n == "avgc200_rising":    return im.get("avgc200", np.nan) > _ri("avgc200", 1)
    elif n == "xavgc50_rising":    return im.get("xavgc50", np.nan) > _ri("xavgc50", 1)
    elif n == "xavgc21_rising":    return im.get("xavgc21", np.nan) > _ri("xavgc21", 1)
    elif n == "xavgc21_falling":   return im.get("xavgc21", np.nan) < _ri("xavgc21", 1)
    elif n == "xavgc8_rising":     return im.get("xavgc8", np.nan) > _ri("xavgc8", 1)
    elif n == "xavgc8_falling":    return im.get("xavgc8", np.nan) < _ri("xavgc8", 1)
    # --- Breakout/breakdown ---
    elif n == "h_gt_maxh5_1":      return H > _ri("maxh5", 1)
    elif n == "h_gt_maxh10_1":     return H > _ri("maxh10", 1)
    elif n == "h_gt_maxh20_1":     return H > _ri("maxh20", 1)
    elif n == "l_lt_minl5_1":      return L < _ri("minl5", 1)
    elif n == "l_lt_minl10_1":     return L < _ri("minl10", 1)
    elif n == "l_lt_minl20_1":     return L < _ri("minl20", 1)
    elif n == "c_gt_maxc10_1":     return C > _ri("maxc10", 1)
    # --- Range/candle ---
    elif n == "range_gt_atr":      return (H - L) > im.get("atr14", np.nan)
    elif n == "body_gt_half_range": return abs(C - O) > 0.5 * (H - L) if H != L else False
    elif n == "c_upper_half":      return C > (H + L) / 2
    elif n == "c_lower_half":      return C < (H + L) / 2
    elif n == "inside_bar":
        h1 = _ri("high", 1)
        l1 = _ri("low", 1)
        return H < h1 and L > l1 if not np.isnan(h1) and not np.isnan(l1) else False
    elif n == "outside_bar":
        h1 = _ri("high", 1)
        l1 = _ri("low", 1)
        return H > h1 and L < l1 if not np.isnan(h1) and not np.isnan(l1) else False
    # --- Gap ---
    elif n == "gap_up":            return O > _ri("close", 1)
    elif n == "gap_down":          return O < _ri("close", 1)
    elif n == "big_gap_up":
        atr = im.get("atr14", np.nan)
        return (O - _ri("close", 1)) > atr if not np.isnan(atr) else False
    elif n == "big_gap_down":
        atr = im.get("atr14", np.nan)
        return (_ri("close", 1) - O) > atr if not np.isnan(atr) else False
    # --- Directional/momentum ---
    elif n == "diplus_gt_diminus": return im.get("diplus14", np.nan) > im.get("diminus14", np.nan)
    elif n == "rsi14_gt_50":       return im.get("rsi14", np.nan) > 50
    elif n == "rsi14_gt_60":       return im.get("rsi14", np.nan) > 60
    elif n == "rsi14_gt_70":       return im.get("rsi14", np.nan) > 70
    elif n == "rsi14_lt_30":       return im.get("rsi14", np.nan) < 30
    elif n == "rsi14_lt_40":       return im.get("rsi14", np.nan) < 40
    elif n == "rsi14_lt_50":       return im.get("rsi14", np.nan) < 50
    elif n == "adx14_gt_20":       return im.get("adx14", np.nan) > 20
    elif n == "adx14_gt_25":       return im.get("adx14", np.nan) > 25
    elif n == "adx14_gt_30":       return im.get("adx14", np.nan) > 30
    elif n == "adx14_lt_20":       return im.get("adx14", np.nan) < 20
    # --- Bollinger ---
    elif n == "c_gt_bbtop":        return C > im.get("bbtop_20", np.nan)
    elif n == "c_lt_bbbot":        return C < im.get("bbbot_20", np.nan)
    # --- CMF ---
    elif n == "cmf20_positive":    return im.get("cmf_20", np.nan) > 0
    elif n == "cmf20_negative":    return im.get("cmf_20", np.nan) < 0
    # --- Expanded Price vs MA ---
    elif n == "c_gt_xavgc13":     return C > im.get("xavgc13", np.nan)
    elif n == "c_gt_xavgc200":    return C > im.get("xavgc200", np.nan)
    elif n == "c_gt_avgc100":     return C > im.get("avgc100", np.nan)
    elif n == "c_lt_xavgc13":     return C < im.get("xavgc13", np.nan)
    elif n == "c_lt_xavgc50":     return C < im.get("xavgc50", np.nan)
    elif n == "c_lt_avgc100":     return C < im.get("avgc100", np.nan)
    # --- Wick vs MA ---
    elif n == "l_gt_xavgc8":      return L > im.get("xavgc8", np.nan)
    elif n == "l_gt_xavgc21":     return L > im.get("xavgc21", np.nan)
    elif n == "l_gt_avgc50":      return L > im.get("avgc50", np.nan)
    elif n == "l_gt_avgc200":     return L > im.get("avgc200", np.nan)
    elif n == "h_lt_xavgc8":      return H < im.get("xavgc8", np.nan)
    elif n == "h_lt_xavgc21":     return H < im.get("xavgc21", np.nan)
    elif n == "h_lt_avgc50":      return H < im.get("avgc50", np.nan)
    elif n == "h_lt_avgc200":     return H < im.get("avgc200", np.nan)
    # --- Volume expanded ---
    elif n == "v_gt_avgv10":      return V > im.get("avgv10", np.nan)
    elif n == "v_gt_1_5x_avgv20": return V > 1.5 * im.get("avgv20", np.nan)
    elif n == "v_gt_3x_avgv20":   return V > 3 * im.get("avgv20", np.nan)
    # --- MA vs MA expanded ---
    elif n == "xavgc13_gt_xavgc21": return im.get("xavgc13", np.nan) > im.get("xavgc21", np.nan)
    elif n == "xavgc8_gt_xavgc50":  return im.get("xavgc8", np.nan) > im.get("xavgc50", np.nan)
    elif n == "xavgc21_gt_xavgc50": return im.get("xavgc21", np.nan) > im.get("xavgc50", np.nan)
    elif n == "xavgc21_gt_xavgc100": return im.get("xavgc21", np.nan) > im.get("xavgc100", np.nan)
    elif n == "avgc50_gt_avgc100":  return im.get("avgc50", np.nan) > im.get("avgc100", np.nan)
    elif n == "avgc100_gt_avgc200": return im.get("avgc100", np.nan) > im.get("avgc200", np.nan)
    # --- MA direction expanded ---
    elif n == "xavgc13_rising":   return im.get("xavgc13", np.nan) > _ri("xavgc13", 1)
    elif n == "xavgc13_falling":  return im.get("xavgc13", np.nan) < _ri("xavgc13", 1)
    elif n == "xavgc100_rising":  return im.get("xavgc100", np.nan) > _ri("xavgc100", 1)
    elif n == "xavgc100_falling": return im.get("xavgc100", np.nan) < _ri("xavgc100", 1)
    elif n == "xavgc50_falling":  return im.get("xavgc50", np.nan) < _ri("xavgc50", 1)
    elif n == "avgc100_rising":   return im.get("avgc100", np.nan) > _ri("avgc100", 1)
    elif n == "avgc100_falling":  return im.get("avgc100", np.nan) < _ri("avgc100", 1)
    elif n == "avgc200_falling":  return im.get("avgc200", np.nan) < _ri("avgc200", 1)
    # --- Breakout expanded ---
    elif n == "h_gt_maxh50_1":    return H > _ri("maxh50", 1)
    elif n == "h_gt_maxh65_1":    return H > _ri("maxh65", 1)
    elif n == "l_lt_minl50_1":    return L < _ri("minl50", 1)
    elif n == "l_lt_minl65_1":    return L < _ri("minl65", 1)
    elif n == "c_gt_maxc20_1":    return C > _ri("maxc20", 1)
    elif n == "c_gt_maxc50_1":    return C > _ri("maxc50", 1)
    # --- Range/candle expanded ---
    elif n == "range_gt_1_5_atr": return (H - L) > 1.5 * im.get("atr14", np.nan)
    elif n == "range_lt_half_atr": return (H - L) < 0.5 * im.get("atr14", np.nan)
    elif n == "close_near_high":  return (H - C) < 0.25 * (H - L) if H != L else False
    elif n == "close_near_low":   return (C - L) < 0.25 * (H - L) if H != L else False
    elif n == "narrow_range":     return (H - L) < 0.5 * im.get("atr14", np.nan)
    elif n == "wide_range":       return (H - L) > 1.5 * im.get("atr14", np.nan)
    # --- Gap expanded ---
    elif n == "gap_up_half_atr":
        atr = im.get("atr14", np.nan)
        return (O - _ri("close", 1)) > 0.5 * atr if not np.isnan(atr) else False
    elif n == "gap_down_half_atr":
        atr = im.get("atr14", np.nan)
        return (_ri("close", 1) - O) > 0.5 * atr if not np.isnan(atr) else False
    # --- Momentum expanded ---
    elif n == "rsi14_gt_80":      return im.get("rsi14", np.nan) > 80
    elif n == "rsi14_lt_20":      return im.get("rsi14", np.nan) < 20
    elif n == "stoch14_gt_50":    return im.get("stoch14", np.nan) > 50
    elif n == "stoch14_gt_80":    return im.get("stoch14", np.nan) > 80
    elif n == "stoch14_lt_20":    return im.get("stoch14", np.nan) < 20
    elif n == "stoch14_lt_50":    return im.get("stoch14", np.nan) < 50
    elif n == "cci14_gt_100":     return im.get("cci14", np.nan) > 100
    elif n == "cci14_lt_neg100":  return im.get("cci14", np.nan) < -100
    # --- Bollinger squeeze ---
    elif n == "bb_squeeze":
        sd = im.get("stddev_20", np.nan)
        avgc20 = im.get("avgc20", np.nan)
        bw = _safe_div(sd, avgc20)
        # Approximate: this needs percentile rank of bandwidth over 120 bars
        # Cannot compute accurately without full history. Use prev expression value.
        return False  # Conservative fallback
    # --- MACD ---
    elif n == "macd_positive":
        return im.get("macd_12_26", np.nan) > 0
    elif n == "macd_negative":
        return im.get("macd_12_26", np.nan) < 0
    # --- OBV ---
    elif n == "obv_rising":
        obv_now = im.get("obv", np.nan)
        obv_5 = _ri("obv", 5)
        return obv_now > obv_5 if not np.isnan(obv_now) and not np.isnan(obv_5) else False
    elif n == "obv_falling":
        obv_now = im.get("obv", np.nan)
        obv_5 = _ri("obv", 5)
        return obv_now < obv_5 if not np.isnan(obv_now) and not np.isnan(obv_5) else False
    # --- BOP ---
    elif n == "bop14_positive":   return im.get("bop14", np.nan) > 0
    elif n == "bop14_negative":   return im.get("bop14", np.nan) < 0
    # --- Aroon ---
    elif n == "aroon_up14_gt_70": return im.get("aroon_up_14", np.nan) > 70
    elif n == "aroon_down14_gt_70": return im.get("aroon_down_14", np.nan) > 70
    else:
        return False  # Unknown condition — conservative


def _compute_daily_expressions(expr_row, im, state, new_state, bar_idx,
                                lookback, append_data, npz_end_bar, lb_rows, n_exprs,
                                npz_tail, npz_tail_start_bar, full_im=None,
                                fp_engine=None):
    """Compute all daily expression values for today's bar.

    Fills expr_row in-place for daily dispatch, slow, fallback, and boolean indices.
    full_im: dict of intermediate name → numpy float64 array (full history).
    fp_engine: ExpressionEngine on full OHLCV, for compute_series fallback.
    """
    # Get the previous bar's expression row (from last .append row or .npz tail)
    prev_expr_row = None
    if append_data is not None and append_data.shape[0] > 0:
        prev_expr_row = append_data[-1, :n_exprs]
    elif npz_tail is not None and npz_tail.shape[0] > 0:
        prev_expr_row = npz_tail[-1, :]

    # ── Dispatch ops ──
    # Primary: use dispatch_arith_numpy on full_im (same path as truth).
    # Fallback: scalar dispatch (for when full_im is unavailable).
    from scripts.backtest_conditions import compute_series as _cs
    from expr_cache_builder import dispatch_arith_numpy as _dan

    for j in _fp_daily_dispatch_indices:
        comp = _fp_expressions[j]["compute"]
        if full_im is not None:
            try:
                result = _dan(comp, full_im)
                if result is not None and len(result) > 0:
                    expr_row[j] = float(result[-1])
                # If result is None, expression stays NaN — matches truth path
                # (dispatch_arith_numpy returns None for expressions with missing intermediates)
            except Exception:
                pass
        else:
            val = _dispatch_scalar(comp, im, bar_idx, lookback, append_data,
                                   npz_end_bar, lb_rows, n_exprs,
                                   state, new_state, npz_tail, npz_tail_start_bar,
                                   full_im=full_im)
            expr_row[j] = val

    # ── SLOW_OPS ──
    # When full_im is available, use it for window reads (float64 precision).
    # Define a helper that reads a window from full_im or lookback.
    cidx = INTERMEDIATE_COL_INDEX
    H = im["high"]
    L = im["low"]

    def _riw_full(name, start_bar, end_bar):
        """Read intermediate window, preferring full_im (float64)."""
        if full_im is not None and name in full_im:
            arr = full_im[name]
            s = max(0, start_bar)
            e = min(len(arr), end_bar + 1)
            if s >= e:
                return np.full(end_bar - start_bar + 1, np.nan, dtype=np.float32)
            result = np.full(end_bar - start_bar + 1, np.nan, dtype=np.float64)
            result[s - start_bar:e - start_bar] = arr[s:e]
            return result
        return _read_intermediate_window(cidx.get(name, -1), start_bar, end_bar,
                                          lookback, append_data, npz_end_bar, lb_rows, n_exprs)

    for j in _fp_daily_slow_indices:
        comp = _fp_expressions[j]["compute"]
        op = comp["op"]
        try:
            if op == "percentile_rank":
                source = comp["source"]
                period = comp["period"]
                # Get source intermediate name
                if source == "close": src_name = "close"
                elif source == "volume": src_name = "volume"
                elif source == "range": src_name = None  # H-L, computed
                elif source == "atr14": src_name = "atr14"
                elif source == "rsi14": src_name = "rsi14"
                else: src_name = "close"

                today_val = im.get(src_name, np.nan) if src_name else (H - L)
                if src_name:
                    window = _riw_full(src_name, bar_idx - period + 1, bar_idx - 1)
                else:
                    h_win = _riw_full("high", bar_idx - period + 1, bar_idx - 1)
                    l_win = _riw_full("low", bar_idx - period + 1, bar_idx - 1)
                    window = h_win - l_win
                full_window = np.append(window, today_val)
                valid = ~np.isnan(full_window)
                if np.sum(valid) >= 2:
                    expr_row[j] = float(np.sum(full_window[valid] <= today_val) / np.sum(valid) * 100.0)

            elif op == "roc_percentile_rank":
                roc_p = comp["roc_period"]
                lb = comp["lookback"]
                # Compute ROC at today
                c_past = _ri_for_dispatch("close", roc_p, bar_idx, lookback, append_data,
                                          npz_end_bar, lb_rows, n_exprs)
                roc_today = (C / c_past - 1) * 100 if not np.isnan(c_past) and c_past != 0 else np.nan
                # Compute ROC at last lb-1 bars
                rocs = []
                for off in range(1, lb):
                    c_off = _read_intermediate(cidx["close"], bar_idx - off, lookback,
                                              append_data, npz_end_bar, lb_rows, n_exprs)
                    c_off_p = _read_intermediate(cidx["close"], bar_idx - off - roc_p, lookback,
                                                 append_data, npz_end_bar, lb_rows, n_exprs)
                    if not np.isnan(c_off) and not np.isnan(c_off_p) and c_off_p != 0:
                        rocs.append((c_off / c_off_p - 1) * 100)
                if rocs and not np.isnan(roc_today):
                    all_rocs = rocs + [roc_today]
                    expr_row[j] = float(np.sum(np.array(all_rocs) <= roc_today) / len(all_rocs))

            elif op == "bars_since_ma_cross":
                ma_name = comp["ma"]
                max_lb = comp.get("max_lookback", 120)
                ma_now = _get_ma_scalar(im, ma_name)
                above_now = C > ma_now if not np.isnan(ma_now) else None
                if above_now is not None:
                    result = float(max_lb)
                    for back in range(1, max_lb):
                        c_back = _read_intermediate(cidx["close"], bar_idx - back, lookback,
                                                     append_data, npz_end_bar, lb_rows, n_exprs)
                        ma_back = _read_intermediate(cidx.get(ma_name, -1), bar_idx - back, lookback,
                                                      append_data, npz_end_bar, lb_rows, n_exprs) if ma_name in cidx else np.nan
                        if not np.isnan(c_back) and not np.isnan(ma_back):
                            above_back = c_back > ma_back
                            if above_back != above_now:
                                result = float(back)
                                break
                    expr_row[j] = result

            elif op in ("swing_high_count", "swing_low_count",
                        "higher_high_count", "higher_low_count",
                        "lower_high_count", "lower_low_count"):
                p = comp["period"]
                is_high = op in ("swing_high_count", "higher_high_count", "lower_high_count")
                src_name = "high" if is_high else "low"
                window = _riw_full(src_name, bar_idx - p, bar_idx)

                if op == "swing_high_count":
                    count = 0
                    for k in range(1, len(window) - 1):
                        if window[k] > window[k-1] and window[k] > window[k+1]:
                            count += 1
                    expr_row[j] = float(count)
                elif op == "swing_low_count":
                    count = 0
                    for k in range(1, len(window) - 1):
                        if window[k] < window[k-1] and window[k] < window[k+1]:
                            count += 1
                    expr_row[j] = float(count)
                elif op in ("higher_high_count", "lower_high_count",
                            "higher_low_count", "lower_low_count"):
                    # Find swings
                    swings = []
                    for k in range(1, len(window) - 1):
                        if is_high:
                            if window[k] > window[k-1] and window[k] > window[k+1]:
                                swings.append(float(window[k]))
                        else:
                            if window[k] < window[k-1] and window[k] < window[k+1]:
                                swings.append(float(window[k]))
                    if len(swings) < 2:
                        expr_row[j] = 0.0
                    else:
                        count = 0
                        ascending = "higher" in op
                        for k in range(len(swings) - 1, 0, -1):
                            if ascending and swings[k] > swings[k-1]:
                                count += 1
                            elif not ascending and swings[k] < swings[k-1]:
                                count += 1
                            else:
                                break
                        expr_row[j] = float(count)
        except Exception:
            pass

    # SLOW_OPS truth-path: use compute_series for ALL slow ops when fp_engine available
    # This replaces the scalar window approach above, which has float16 precision issues
    if fp_engine is not None:
        for j in _fp_daily_slow_indices:
            try:
                s = _cs(fp_engine, _fp_expressions[j]["compute"])
                if s is not None:
                    arr = np.asarray(s, dtype=np.float32)
                    if len(arr) > 0:
                        expr_row[j] = float(arr[-1])
            except Exception:
                pass

    # ── Fallback ops ──
    from scripts.backtest_conditions import compute_series as _cs
    n_bars_full = bar_idx + 1  # total bars including today

    for j in _fp_daily_fallback_indices:
        comp = _fp_expressions[j]["compute"]
        computed = False
        # Primary: use compute_series on ExpressionEngine (truth path)
        if fp_engine is not None:
            try:
                series = _cs(fp_engine, comp)
                if series is not None:
                    arr = np.asarray(series, dtype=np.float32)
                    if len(arr) > 0:
                        expr_row[j] = float(arr[-1])
                        computed = True
            except Exception:
                pass
        if not computed:
            val = _dispatch_fallback_scalar(comp, im, bar_idx, state, new_state,
                                             lookback, append_data, npz_end_bar,
                                             lb_rows, n_exprs,
                                             prev_expr_row, j, full_im=full_im)
            if not np.isnan(val):
                expr_row[j] = val

    # ── Boolean aggregates ──
    # Use truth path: ExpressionEngine._bool_series() + vectorized aggregation.
    # This matches _compute_ticker_full's run_optimized_bools exactly.
    if fp_engine is not None:
        from expr_cache_builder import np_count_true as _nct, np_true_in_row as _ntir
        n_bars_full = bar_idx + 1

        # Cache boolean series per condition
        bool_series_cache = {}

        def _get_bool_series(cond):
            if cond not in bool_series_cache:
                try:
                    bool_series_cache[cond] = fp_engine._bool_series(cond).values.astype(bool)
                except Exception:
                    bool_series_cache[cond] = np.zeros(n_bars_full, dtype=bool)
            return bool_series_cache[cond]

        # count_true
        for j in _fp_daily_bool_ct:
            comp = _fp_expressions[j]["compute"]
            b = _get_bool_series(comp["condition"])
            ct = _nct(b, comp["period"])
            if len(ct) > 0:
                expr_row[j] = float(ct[-1])

        # since_true
        bs_cache = {}
        for j in _fp_daily_bool_st:
            comp = _fp_expressions[j]["compute"]
            cond = comp["condition"]
            period = comp["period"]
            if cond not in bs_cache:
                b = _get_bool_series(cond)
                bs = np.full(n_bars_full, n_bars_full, dtype=np.float64)
                for i in range(n_bars_full):
                    if b[i]: bs[i] = 0.0
                    elif i > 0: bs[i] = bs[i-1] + 1.0
                bs_cache[cond] = bs
            bs = bs_cache[cond]
            if bar_idx < len(bs):
                expr_row[j] = bs[bar_idx] if bs[bar_idx] < period else -1.0

        # true_in_row
        for j in _fp_daily_bool_tir:
            comp = _fp_expressions[j]["compute"]
            b = _get_bool_series(comp["condition"])
            tir = _ntir(b, comp["period"])
            if len(tir) > 0:
                expr_row[j] = float(tir[-1])
    else:
        # Scalar fallback (no ExpressionEngine available)
        bool_cache = {}
        def _get_bool(cond):
            if cond not in bool_cache:
                bool_cache[cond] = _eval_bool_condition(cond, im, bar_idx, lookback,
                                                         append_data, npz_end_bar,
                                                         lb_rows, n_exprs, state=state)
            return bool_cache[cond]
        for j in _fp_daily_bool_ct:
            comp = _fp_expressions[j]["compute"]
            bool_today = 1.0 if _get_bool(comp["condition"]) else 0.0
            prev_count = prev_expr_row[j] if prev_expr_row is not None else np.nan
            if not np.isnan(prev_count):
                expr_row[j] = prev_count + bool_today  # Approximate: no drop bar
        for j in _fp_daily_bool_st:
            comp = _fp_expressions[j]["compute"]
            if _get_bool(comp["condition"]):
                expr_row[j] = 0.0
            else:
                prev_val = prev_expr_row[j] if prev_expr_row is not None else np.nan
                if not np.isnan(prev_val) and prev_val != -1.0:
                    new_val = prev_val + 1.0
                    expr_row[j] = new_val if new_val < comp["period"] else -1.0
        for j in _fp_daily_bool_tir:
            comp = _fp_expressions[j]["compute"]
            if _get_bool(comp["condition"]):
                prev_val = prev_expr_row[j] if prev_expr_row is not None else 0.0
                expr_row[j] = (prev_val + 1.0) if not np.isnan(prev_val) else 1.0
            else:
                expr_row[j] = 0.0


def _eval_bool_at_bar(cond_name, drop_im):
    """Evaluate a boolean condition using pre-loaded intermediate values at a historical bar.

    drop_im has keys like "close", "high", etc. for that bar,
    plus "_close_prev", "_high_prev", "_low_prev" for shift(1) values.
    """
    n = cond_name
    C = drop_im.get("close", np.nan)
    O = drop_im.get("open", np.nan)
    H = drop_im.get("high", np.nan)
    L = drop_im.get("low", np.nan)
    V = drop_im.get("volume", np.nan)

    def _val(key):
        return drop_im.get(key, np.nan)

    def _prev(key):
        return drop_im.get(f"_{key}_prev", np.nan)

    if any(np.isnan(x) for x in [C, O, H, L]):
        return False

    # Simplified condition evaluation — covers the most common conditions
    if   n == "c_gt_xavgc8":       return C > _val("xavgc8")
    elif n == "c_gt_xavgc21":      return C > _val("xavgc21")
    elif n == "c_gt_xavgc50":      return C > _val("xavgc50")
    elif n == "c_gt_xavgc100":     return C > _val("xavgc100")
    elif n == "c_gt_avgc50":       return C > _val("avgc50")
    elif n == "c_gt_avgc200":      return C > _val("avgc200")
    elif n == "c_lt_xavgc8":       return C < _val("xavgc8")
    elif n == "c_lt_xavgc21":      return C < _val("xavgc21")
    elif n == "c_lt_avgc50":       return C < _val("avgc50")
    elif n == "c_lt_avgc200":      return C < _val("avgc200")
    elif n == "c_gt_c1":           return C > _prev("close")
    elif n == "c_lt_c1":           return C < _prev("close")
    elif n == "h_gt_h1":           return H > _prev("high")
    elif n == "l_lt_l1":           return L < _prev("low")
    elif n == "c_gt_o":            return C > O
    elif n == "v_gt_avgv20":       return V > _val("avgv20")
    elif n == "v_gt_avgv50":       return V > _val("avgv50")
    elif n == "rsi14_gt_50":       return _val("rsi14") > 50
    elif n == "rsi14_gt_70":       return _val("rsi14") > 70
    elif n == "rsi14_lt_30":       return _val("rsi14") < 30
    elif n == "rsi14_lt_50":       return _val("rsi14") < 50
    elif n == "adx14_gt_20":       return _val("adx14") > 20
    elif n == "adx14_gt_25":       return _val("adx14") > 25
    elif n == "range_gt_atr":      return (H - L) > _val("atr14")
    elif n == "c_upper_half":      return C > (H + L) / 2
    elif n == "c_lower_half":      return C < (H + L) / 2
    elif n == "diplus_gt_diminus": return _val("diplus14") > _val("diminus14")
    elif n == "macd_positive":     return _val("macd_12_26") > 0
    elif n == "macd_negative":     return _val("macd_12_26") < 0
    elif n == "cmf20_positive":    return _val("cmf_20") > 0
    elif n == "cmf20_negative":    return _val("cmf_20") < 0
    # For conditions we can't evaluate at a historical bar, return False (conservative)
    else:
        return False


# ══════════════════════════════════════════════════════════════
# PHASE 3: HTF EXPRESSIONS (weekly + monthly)
# ══════════════════════════════════════════════════════════════

def _compute_htf_expressions(expr_row, df_dict, weekly_df_dict, monthly_df_dict, today_date):
    """Compute HTF expression values using ExpressionEngine on HTF data.

    Builds ExpressionEngine on full HTF DataFrames, dispatches expressions at the
    last bar (which includes today's partial candle). Cost: ~0.13s/ticker.
    """
    from scripts.expression_engine import ExpressionEngine
    from scripts.backtest_conditions import compute_series
    from expr_cache_builder import (
        build_numpy_intermediates, dispatch_arith_numpy, resample_ohlcv,
        build_htf_to_daily_map, map_htf_series_to_daily,
        np_count_true, np_since_true, np_true_in_row,
        _truncate_to_cache_window,
    )

    # Reconstruct daily DataFrame — truncate to same window as build_full
    df = pd.DataFrame(df_dict)
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = _truncate_to_cache_window(df)
    if df is None or len(df) < 50:
        return
    n_daily = len(df)

    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}

    for tf_freq, tf_indices, tf_base_computes, htf_df_dict in [
        ("W", _fp_htf_weekly_indices, _fp_htf_weekly_base, weekly_df_dict),
        ("ME", _fp_htf_monthly_indices, _fp_htf_monthly_base, monthly_df_dict),
    ]:
        if not tf_indices:
            continue

        # Match build_full: use truncated HTF pickle first, resample as fallback.
        htf_df = None
        if htf_df_dict is not None:
            try:
                htf_df = pd.DataFrame(htf_df_dict)
                htf_df["date"] = pd.to_datetime(htf_df["date"])
                for col in ["open", "high", "low", "close", "volume"]:
                    htf_df[col] = pd.to_numeric(htf_df[col], errors="coerce")
                htf_df = _truncate_to_cache_window(htf_df)
                if htf_df is None or len(htf_df) < 5:
                    htf_df = None
            except Exception:
                htf_df = None
        if htf_df is None:
            htf_df = resample_ohlcv(df, tf_freq)
        if htf_df is None or len(htf_df) < 5:
            continue

        try:
            # Build daily→HTF map
            htf_map = build_htf_to_daily_map(df["date"], htf_df, tf_freq)
            # Today's HTF index
            today_htf_idx = htf_map[-1] if len(htf_map) > 0 else len(htf_df) - 1

            # Create engine and intermediates
            htf_engine = ExpressionEngine(htf_df)
            htf_n = len(htf_df)
            htf_im = build_numpy_intermediates(htf_engine)

            # Classify HTF expressions
            htf_ct, htf_st, htf_tir, htf_ext, htf_arith = [], [], [], [], []
            for k, j in enumerate(tf_indices):
                base_op = tf_base_computes[k].get("op", "")
                if base_op == "count_true": htf_ct.append((k, j))
                elif base_op == "since_true": htf_st.append((k, j))
                elif base_op == "true_in_row": htf_tir.append((k, j))
                elif base_op in _ON_SERIES_OPS: htf_ext.append((k, j))
                else: htf_arith.append((k, j))

            # Dispatch arith — take last daily value from mapped series
            for k, j in htf_arith:
                comp = tf_base_computes[k]
                try:
                    result = dispatch_arith_numpy(comp, htf_im)
                    if result is not None:
                        mapped = map_htf_series_to_daily(result.astype(np.float32), htf_map)
                        expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                    else:
                        s = compute_series(htf_engine, comp)
                        if s is not None:
                            mapped = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                            expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                except Exception:
                    try:
                        s = compute_series(htf_engine, comp)
                        if s is not None:
                            mapped = map_htf_series_to_daily(np.asarray(s, dtype=np.float32), htf_map)
                            expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                    except Exception:
                        pass

            # HTF booleans
            htf_bool_cache = {}
            for k, j in htf_ct + htf_st + htf_tir:
                cond = tf_base_computes[k]["condition"]
                if cond not in htf_bool_cache:
                    try:
                        htf_bool_cache[cond] = htf_engine._bool_series(cond).values.astype(bool)
                    except Exception:
                        htf_bool_cache[cond] = np.zeros(htf_n, dtype=bool)

            for k, j in htf_ct:
                b = htf_bool_cache[tf_base_computes[k]["condition"]]
                mapped = map_htf_series_to_daily(
                    np_count_true(b, tf_base_computes[k]["period"]).astype(np.float32), htf_map)
                expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan

            # since_true
            htf_bs = {}
            for k, j in htf_st:
                cond = tf_base_computes[k]["condition"]
                if cond not in htf_bs:
                    b = htf_bool_cache[cond]
                    bs = np.full(htf_n, htf_n, dtype=np.float64)
                    for i in range(htf_n):
                        if b[i]: bs[i] = 0.0
                        elif i > 0: bs[i] = bs[i-1] + 1.0
                    htf_bs[cond] = bs
            for k, j in htf_st:
                bs = htf_bs[tf_base_computes[k]["condition"]]
                p = tf_base_computes[k]["period"]
                r = np.full(htf_n, np.nan)
                for i in range(p-1, htf_n):
                    r[i] = bs[i] if bs[i] < p else -1.0
                mapped = map_htf_series_to_daily(r.astype(np.float32), htf_map)
                expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan

            for k, j in htf_tir:
                b = htf_bool_cache[tf_base_computes[k]["condition"]]
                mapped = map_htf_series_to_daily(
                    np_true_in_row(b, tf_base_computes[k]["period"]).astype(np.float32), htf_map)
                expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan

            # HTF extension structure (on_series + on_series_bool_agg)
            if htf_ext:
                import json as _json
                from scripts.backtest_conditions import compute_on_series
                from expr_cache_builder import np_trendline_deviation, np_channel_position
                LINREG_OPS = {"trendline_deviation", "channel_position"}

                # Build HTF-resolution extension series from intermediates
                htf_ext_registry = {}
                for sname, comp_spec in [
                    ("ext_avgc50_adr14", {"op": "extension", "ma": "avgc50", "normalizer": "adr14"}),
                    ("ext_avgc200_adr14", {"op": "extension", "ma": "avgc200", "normalizer": "adr14"}),
                ]:
                    result = dispatch_arith_numpy(comp_spec, htf_im)
                    if result is not None and not np.all(np.isnan(result)):
                        htf_ext_registry[sname] = result.astype(np.float64)

                if htf_ext_registry:
                    ext_linreg, ext_bool_agg, ext_other = [], [], []
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

                    # Linreg at HTF resolution
                    for k, j in ext_linreg:
                        try:
                            comp = tf_base_computes[k]
                            sn = comp.get("series", "")
                            if sn in htf_ext_registry:
                                s = htf_ext_registry[sn]
                                lb = comp["inner_op"]["lookback"]
                                fn = np_trendline_deviation if comp["inner_op"]["op"] == "trendline_deviation" else np_channel_position
                                htf_result = fn(s, lb).astype(np.float32)
                                mapped = map_htf_series_to_daily(htf_result, htf_map)
                                expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                        except Exception:
                            pass

                    # Bool agg at HTF resolution
                    htf_ind_bool_cache = {}
                    for k, j in ext_bool_agg:
                        comp = tf_base_computes[k]
                        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
                        if ck not in htf_ind_bool_cache:
                            try:
                                sd = htf_ext_registry.get(comp["series"])
                                if sd is None:
                                    htf_ind_bool_cache[ck] = None
                                    continue
                                indicator = compute_on_series(np.asarray(sd, dtype=np.float64), comp["bool_op"])
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

                    htf_ba_bs_cache = {}
                    for k, j in ext_bool_agg:
                        comp = tf_base_computes[k]
                        if comp["agg_op"] != "since_true":
                            continue
                        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
                        if ck in htf_ba_bs_cache or htf_ind_bool_cache.get(ck) is None:
                            continue
                        b = htf_ind_bool_cache[ck]
                        bs = np.full(htf_n, htf_n, dtype=np.float64)
                        for i in range(htf_n):
                            if b[i]: bs[i] = 0.0
                            elif i > 0: bs[i] = bs[i-1] + 1.0
                        htf_ba_bs_cache[ck] = bs

                    for k, j in ext_bool_agg:
                        comp = tf_base_computes[k]
                        ck = (comp["series"], _json.dumps(comp["bool_op"], sort_keys=True))
                        b = htf_ind_bool_cache.get(ck)
                        if b is None:
                            continue
                        ap = comp["agg_period"]
                        if comp["agg_op"] == "count_true":
                            ct = np_count_true(b, ap)
                            mapped = map_htf_series_to_daily(ct.astype(np.float32), htf_map)
                            expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                        elif comp["agg_op"] == "since_true":
                            bs = htf_ba_bs_cache.get(ck)
                            if bs is not None:
                                r = np.full(htf_n, np.nan)
                                for i in range(ap - 1, htf_n):
                                    r[i] = bs[i] if bs[i] < ap else -1.0
                                mapped = map_htf_series_to_daily(r.astype(np.float32), htf_map)
                                expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                        elif comp["agg_op"] == "true_in_row":
                            tir = np_true_in_row(b, ap)
                            mapped = map_htf_series_to_daily(tir.astype(np.float32), htf_map)
                            expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan

                    # Other on_series ops — compute_on_series at HTF resolution
                    for k, j in ext_other:
                        try:
                            comp = tf_base_computes[k]
                            sn = comp.get("series", "")
                            if sn in htf_ext_registry:
                                s = htf_ext_registry[sn]
                                inner = comp.get("inner_op", comp)
                                result = compute_on_series(np.asarray(s, dtype=np.float64), inner)
                                if result is not None:
                                    mapped = map_htf_series_to_daily(np.asarray(result, dtype=np.float32), htf_map)
                                    expr_row[j] = mapped[-1] if len(mapped) > 0 else np.nan
                        except Exception:
                            pass

        except Exception:
            pass  # HTF fails silently — columns stay NaN


# ══════════════════════════════════════════════════════════════
# PHASE 4: EXTENSION STRUCTURE (on_series + on_series_bool_agg)
# ══════════════════════════════════════════════════════════════

def _compute_ext_struct_expressions(expr_row, im, state, new_state, bar_idx,
                                     lookback, append_data, npz_end_bar, lb_rows, n_exprs,
                                     npz_tail, npz_tail_start_bar, full_im=None):
    """Compute extension structure expressions.

    These are on_series and on_series_bool_agg ops that run indicators on
    extension series (ext_avgc50_adr14, ext_avgc200_adr14).
    """
    from scripts.backtest_conditions import compute_on_series
    from expr_cache_builder import np_count_true, np_true_in_row, dispatch_arith_numpy

    # Get the base extension series values (computed in Phase 2 as daily expressions)
    ext_values = {}
    for sname, sidx in _fp_ext_series_name_to_idx.items():
        ext_values[sname] = expr_row[sidx]

    # Build extension series history at float64 precision
    # Use full_im (from ExpressionEngine) to compute extension = (close - ma) / norm
    ext_history = {}
    _ext_specs = {
        "ext_avgc50_adr14": {"op": "extension", "ma": "avgc50", "normalizer": "adr14"},
        "ext_avgc200_adr14": {"op": "extension", "ma": "avgc200", "normalizer": "adr14"},
    }
    if full_im is not None:
        for sname in _fp_ext_series_name_to_idx:
            spec = _ext_specs.get(sname)
            if spec:
                result = dispatch_arith_numpy(spec, full_im)
                if result is not None:
                    ext_history[sname] = result.astype(np.float64)
    else:
        # Fallback: read from .npz expression columns (float16)
        for sname, sidx in _fp_ext_series_name_to_idx.items():
            max_lb = 504
            history = _read_expr_window(sidx, max(bar_idx - max_lb, 0), bar_idx - 1,
                                         npz_tail, append_data, npz_end_bar,
                                         npz_tail_start_bar, n_exprs)
            ext_history[sname] = np.append(history, ext_values[sname])

    # For on_series EMA-based ops (RSI, ADX), use state for forward-prop
    ext_label_map = {"ext_avgc50_adr14": "ext50", "ext_avgc200_adr14": "ext200"}

    # Process each extension structure expression
    for j in _fp_ext_struct_indices:
        comp = _fp_expressions[j]["compute"]
        op = comp.get("op", "")

        try:
            if op == "on_series":
                sname = comp["series"]
                inner_op = comp["inner_op"]
                inner_op_name = inner_op.get("op", "")

                if sname not in ext_history:
                    continue

                # All on_series ops: use compute_on_series on full history (truth path)
                full_series = ext_history[sname]
                if len(full_series) >= 10:
                    result = compute_on_series(full_series.astype(np.float64), inner_op)
                    if result is not None and len(result) > 0:
                        expr_row[j] = float(result[-1])

            elif op == "on_series_bool_agg":
                sname = comp["series"]
                bool_spec = comp["bool_op"]
                agg_op = comp["agg_op"]
                agg_period = comp["agg_period"]

                if sname not in ext_history:
                    continue

                full_series = ext_history[sname]
                if len(full_series) < 10:
                    continue

                # Compute indicator on series
                indicator = compute_on_series(full_series.astype(np.float64), bool_spec)
                if indicator is None:
                    continue

                # Apply threshold to get boolean
                threshold = bool_spec.get("threshold", 0)
                direction = bool_spec.get("direction", "gt")
                if direction == "gt": bools = indicator > threshold
                elif direction == "lt": bools = indicator < threshold
                elif direction == "positive": bools = indicator > 0
                elif direction == "negative": bools = indicator < 0
                else: bools = indicator > threshold
                bools[np.isnan(indicator)] = False

                # Apply aggregation on last value
                b_series = bools.astype(bool)
                if agg_op == "count_true":
                    ct = np_count_true(b_series, agg_period)
                    expr_row[j] = float(ct[-1]) if len(ct) > 0 else np.nan
                elif agg_op == "since_true":
                    n = len(b_series)
                    bs = np.full(n, n, dtype=np.float64)
                    for i in range(n):
                        if b_series[i]: bs[i] = 0.0
                        elif i > 0: bs[i] = bs[i-1] + 1.0
                    last_bs = bs[-1]
                    expr_row[j] = last_bs if last_bs < agg_period else -1.0
                elif agg_op == "true_in_row":
                    tir = np_true_in_row(b_series, agg_period)
                    expr_row[j] = float(tir[-1]) if len(tir) > 0 else np.nan

        except Exception:
            pass

    # Update ext_prev in state for next bar
    for sname, sidx in _fp_ext_series_name_to_idx.items():
        ext_label = ext_label_map.get(sname, "")
        new_state[f"ext_prev_{ext_label}"] = float(ext_values.get(sname, 0.0))


# ══════════════════════════════════════════════════════════════
# PHASE 5: LSP + ALGO EXPRESSIONS
# ══════════════════════════════════════════════════════════════

def _compute_lsp_algo_expressions(expr_row, df_dict):
    """Compute LSP and algo expressions via full OHLCV scan.

    No forward-prop shortcut — these scan the entire price history.
    Cost: ~0.64s/ticker (73% of total append cost).
    """
    df = pd.DataFrame(df_dict)
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # LSP
    if _fp_lsp_indices:
        try:
            from scripts.lsp_detector_v2 import compute_all_lsp_series
            lsp_dict = compute_all_lsp_series(df)
            for j in _fp_lsp_indices:
                col_name = _fp_expressions[j]["compute"]["column"]
                if col_name in lsp_dict:
                    arr = lsp_dict[col_name]
                    if len(arr) > 0:
                        expr_row[j] = float(arr[-1])
        except Exception:
            pass

    # Algo lines
    if _fp_algo_indices:
        try:
            from scripts.algo_line_detector import compute_all_algo_series
            algo_dict = compute_all_algo_series(df)
            for j in _fp_algo_indices:
                col_name = _fp_expressions[j]["compute"]["column"]
                if col_name in algo_dict:
                    arr = algo_dict[col_name]
                    if len(arr) > 0:
                        expr_row[j] = float(arr[-1])
        except Exception:
            pass


def _compute_ext50_trendline_expressions(expr_row, state, new_state):
    """Forward-prop Extension Chart Trendlines at the new bar.

    Path-pure primitive: cascade_at re-derives at the new bar using the full
    ext50 history from state['ext50_trendline_state']['ext50_history']. Levels
    at the new bar are read from expr_row (Phase 5d reversal_profile filled
    them earlier in this same forward-prop call).
    """
    if not _fp_ext50_trendline_indices:
        new_state["ext50_trendline_state"] = state.get("ext50_trendline_state",
                                                         {"ext50_history": []})
        return
    try:
        from scripts.ext50_trendlines import (
            step_ext50_trendline_one_bar, get_ext50_trendline_expression_names,
        )
        ext50_idx = _fp_ext_series_name_to_idx.get("ext_avgc50_adr14")
        if ext50_idx is None:
            new_state["ext50_trendline_state"] = state.get("ext50_trendline_state",
                                                             {"ext50_history": []})
            return
        new_ext = float(expr_row[ext50_idx])
        # Read Levels at new bar from expr_row by name lookup
        name_to_idx = {expr["name"]: jj for jj, expr in enumerate(_fp_expressions)}
        levels_at_new_bar = {}
        for level_key in ("upside_1", "upside_2", "downside_1", "downside_2", "chop_upper"):
            col_name = f"ext_avgc50_adr14_{level_key}"
            jj = name_to_idx.get(col_name)
            if jj is not None:
                levels_at_new_bar[level_key] = float(expr_row[jj])
            else:
                levels_at_new_bar[level_key] = float("nan")
        prior = state.get("ext50_trendline_state", {"ext50_history": []})
        vec, new_tl_state = step_ext50_trendline_one_bar(prior, new_ext, levels_at_new_bar)
        tl_names = get_ext50_trendline_expression_names()
        name_to_local_li = {nm: li for li, nm in enumerate(tl_names)}
        for j in _fp_ext50_trendline_indices:
            col_name = _fp_expressions[j]["compute"]["column"]
            li = name_to_local_li.get(col_name)
            if li is not None:
                expr_row[j] = float(vec[li])
        new_state["ext50_trendline_state"] = new_tl_state
    except Exception:
        new_state["ext50_trendline_state"] = state.get("ext50_trendline_state",
                                                         {"ext50_history": []})


def _compute_reversal_profile_expressions(expr_row, state, new_state, bar_idx):
    """Forward-prop Extension Chart Levels (reversal profile) at the new bar.

    Reads:
      - prev_ext per source from state[f'ext_prev_{label}'] (same key the
        ext-struct phase already maintains).
      - new_ext per source from expr_row at the source column index (filled
        by Phase 2 daily expression dispatch).
      - prior reversal_profile state per source from state['reversal_profile_state'][source].
    Writes:
      - 6 constants per source into expr_row at registered indices.
      - Updated state in new_state['reversal_profile_state'][source].
    """
    if not _fp_reversal_profile_by_source:
        new_state["reversal_profile_state"] = state.get("reversal_profile_state", {})
        return
    try:
        from scripts.reversal_profile import step_reversal_at_bar, CONSTANT_NAMES
        prior_all = state.get("reversal_profile_state", {})
        new_all = {}
        # Map source ext column → ext_label used in state keys (drops "ext_" prefix
        # and "_adr14" suffix; matches existing ext_struct convention).
        ext_label_map = {
            "ext_avgc50_adr14": "avgc50",
            "ext_avgc200_adr14": "avgc200",
        }
        cname_to_li = {nm: li for li, nm in enumerate(CONSTANT_NAMES)}
        for src_col, entries in _fp_reversal_profile_by_source.items():
            src_idx = _fp_ext_series_name_to_idx.get(src_col)
            if src_idx is None:
                new_all[src_col] = prior_all.get(src_col, {})
                continue
            new_ext = float(expr_row[src_idx])
            ext_label = ext_label_map.get(src_col, "")
            prev_ext = float(state.get(f"ext_prev_{ext_label}", 0.0))
            prior_src = prior_all.get(src_col, {})
            vec, new_src = step_reversal_at_bar(prior_src, prev_ext, new_ext, int(bar_idx))
            for cname, j in entries:
                li = cname_to_li.get(cname)
                if li is not None:
                    expr_row[j] = float(vec[li])
            new_all[src_col] = new_src
        new_state["reversal_profile_state"] = new_all
    except Exception:
        new_state["reversal_profile_state"] = state.get("reversal_profile_state", {})


def _compute_moc_expressions(expr_row, df_dict, state, new_state):
    """Forward-prop MOC level expressions for the new bar.

    Resumes from `state["moc_levels"]` (list of level dicts, possibly empty),
    applies one bar of MOC mechanics at the last bar of df_dict, writes the
    61-feature snapshot into expr_row at MOC indices, and stores the updated
    levels list in `new_state["moc_levels"]` for the next call.
    """
    if not _fp_moc_indices:
        new_state["moc_levels"] = state.get("moc_levels", [])
        return
    try:
        from scripts.moc_detector import step_moc_one_bar, get_moc_expression_names
        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        prior = {"levels": state.get("moc_levels", [])}
        vec, updated = step_moc_one_bar(prior, df)
        names = get_moc_expression_names()
        name_to_local_idx = {nm: i for i, nm in enumerate(names)}
        for j in _fp_moc_indices:
            col_name = _fp_expressions[j]["compute"]["column"]
            li = name_to_local_idx.get(col_name)
            if li is not None:
                expr_row[j] = float(vec[li])
        new_state["moc_levels"] = updated["levels"]
    except Exception:
        # Preserve prior state so the next call doesn't lose history
        new_state["moc_levels"] = state.get("moc_levels", [])


# ══════════════════════════════════════════════════════════════
# PHASE 6: SAVE
# ══════════════════════════════════════════════════════════════

def _save_forward_prop(ticker, expr_row, im, state, new_state, bar_idx,
                        lookback, append_data, today_date, n_exprs):
    """Save forward-prop results: append to files, update lookback and state.

    1. Append expression + intermediate row to .append file
    2. Append date to .append_dates
    3. Update .lookback (sliding window)
    4. Overwrite .state
    """
    safe = _safe_ticker(ticker)

    # Build intermediate row in INTERMEDIATE_COLUMNS order
    im_row = np.full(N_INTERMEDIATES, np.nan, dtype=np.float32)
    for i, col_name in enumerate(INTERMEDIATE_COLUMNS):
        im_row[i] = im.get(col_name, np.nan)

    # Concatenate: [expressions | intermediates] = 16,001 values
    full_row = np.concatenate([expr_row, im_row])

    # Cast to float16 for storage
    full_row_f16 = full_row.astype(np.float16)

    # ── Append to .append file ──
    append_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.append")
    with open(append_path, "ab") as f:
        full_row_f16.tofile(f)

    # ── Append date to .append_dates ──
    dates_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.append_dates")
    date_str = str(today_date)[:10] if hasattr(today_date, 'isoformat') else str(today_date)[:10]
    with open(dates_path, "a") as f:
        f.write(date_str + "\n")

    # ── Update .lookback (sliding window) ──
    # Drop oldest row, append new intermediate row
    im_row_f16 = im_row.astype(np.float16)
    if lookback is not None and lookback.shape[0] > 0:
        # Shift rows: drop row 0, move 1..N-1 to 0..N-2, write new at N-1
        new_lookback = np.empty_like(lookback)
        if lookback.shape[0] > 1:
            new_lookback[:-1] = lookback[1:]
        new_lookback[-1] = im_row  # Store as float32, will be written as float16
        lb_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.lookback")
        new_lookback.astype(np.float16).tofile(lb_path)

    # ── Overwrite .state ──
    # Merge new_state into state
    for k, v in new_state.items():
        state[k] = v

    # Convert all values to Python float for JSON serialization
    for k in state:
        v = state[k]
        if isinstance(v, (np.floating, np.integer)):
            state[k] = float(v)
        elif isinstance(v, float) and np.isnan(v):
            state[k] = 0.0

    state_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.state")
    with open(state_path, "w") as f:
        json.dump(state, f)
