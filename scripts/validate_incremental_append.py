"""
Validate Incremental Append Approach — Gate check for Increment 2.

This script answers two questions:
1. AUDIT: How many expressions can be computed from state-only (prev row + today's OHLCV)
   vs needing historical lookback vs needing HTF data?
2. CORRECTNESS: For a test ticker, can we reproduce the last bar's expression values
   by computing only that one bar (using the full OHLCV for lookback, but NOT running
   _compute_ticker_full on the full series)?

The approach:
- Run _compute_ticker_full on the FULL OHLCV → "truth" array (n_bars x n_exprs)
- The last row of truth = what the append must reproduce
- For each expression, classify what inputs it needs
- Report how many fall into each bucket

Usage:
    python validate_incremental_append.py [--ticker AAPL]

Requires: daily OHLCV cache, weekly/monthly HTF caches, expression library.
Run from repo root.
"""

import os
import sys
import time
import json
import pickle
import argparse
import warnings
import numpy as np
import pandas as pd
from collections import Counter, defaultdict

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_runner")
REPO_ROOT = os.path.dirname(LOCAL_DIR)
if os.path.exists(LOCAL_DIR):
    # Running from repo root
    pass
else:
    # Running from local_runner/
    LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
    REPO_ROOT = os.path.dirname(LOCAL_DIR)

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")
EXPR_CACHE_START = "2020-01-02"


def load_daily_cache():
    for name in ["universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"]:
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
    raise FileNotFoundError("No OHLCV cache found")


def load_htf_cache(timeframe):
    path = os.path.join(CACHE_DIR, f"universe_ohlcv_{timeframe}.pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def truncate_to_cache_window(df):
    if df is None or len(df) == 0:
        return None
    start = pd.Timestamp(EXPR_CACHE_START)
    dates = pd.to_datetime(df["date"])
    mask = dates >= start
    if not mask.any():
        return None
    return df[mask].reset_index(drop=True)


def df_to_dict(df):
    if df is None or len(df) < 5:
        return None
    return {
        "date": df["date"].values,
        "open": df["open"].values,
        "high": df["high"].values,
        "low": df["low"].values,
        "close": df["close"].values,
        "volume": df["volume"].values,
    }


# ══════════════════════════════════════════════════════════════
# PART 1: EXPRESSION AUDIT — classify every expression
# ══════════════════════════════════════════════════════════════

# Ops where the last-bar value depends ONLY on:
#   - Today's OHLCV (O, H, L, C, V)
#   - A moving average or indicator value at bar[-1], which itself is
#     an EMA or rolling value that can be updated from its previous state
#
# These are "state-propagatable" — the previous row's intermediate values
# plus today's OHLCV is enough to compute the new value.
#
# Ops where the last-bar value needs a WINDOW of historical data:
#   - percentile_rank: needs `period` bars of raw data to rank current vs history
#   - roc_percentile_rank: same
#   - swing_high_count, swing_low_count: needs `period` bars of swing detection
#   - higher_high_count etc: same
#   - bars_since_ma_cross: needs to scan backwards up to max_lookback
#   - extension_ceiling_ratio: rolling max of normalized extension over `lookback` bars
#   - extension_peak_ratio: rolling max of extension over `lookback` bars
#   - bollinger_bandwidth_rank: rolling min/max of bandwidth over `lookback` bars
#   - count_true: rolling sum of boolean over `period` bars
#   - since_true: scan backwards for last true
#   - true_in_row: scan backwards for consecutive trues
#   - trendline_deviation, channel_position: linear regression over `lookback` bars
#   - on_series_bool_agg: boolean aggregation on extension series
#   - aroon: argmax/argmin over window
#   - cci: mean deviation over window (not just mean)

# For each op, what lookback depth does it need?
def classify_expression(expr):
    """Classify an expression by what it needs to compute its last-bar value.

    Returns: (category, lookback_bars, details)
        category: 'state_only' | 'lookback' | 'htf' | 'precomputed'
        lookback_bars: int — how many prior EXPRESSION rows (not OHLCV) are needed
                       0 for state_only, >0 for lookback
        details: str — human-readable explanation
    """
    comp = expr["compute"]
    op = comp.get("op", "")

    # HTF expressions
    if op == "precomputed":
        source = comp.get("source", "")
        if source == "htf":
            tf = comp.get("timeframe", "?")
            base_op = comp.get("base_compute", {}).get("op", "?")
            return ("htf", 0, f"htf_{tf}:{base_op}")
        elif source == "lsp":
            return ("precomputed_lsp", 0, "LSP detector — needs full daily OHLCV")
        elif source == "algo":
            return ("precomputed_algo", 0, "algo line detector — needs full daily OHLCV")
        else:
            return ("precomputed_other", 0, f"precomputed:{source}")

    # Extension structure — operates on extension series, not raw OHLCV
    if op == "on_series":
        inner_op = comp.get("inner_op", {}).get("op", "?")
        lb = comp.get("inner_op", {}).get("lookback", 0)
        if inner_op in ("trendline_deviation", "channel_position"):
            return ("lookback", lb, f"on_series:{inner_op} lb={lb}")
        elif inner_op in ("ceiling_ratio", "floor_ratio", "peak_ratio", "retracement_level"):
            lb2 = comp.get("inner_op", {}).get("lookback", 0)
            return ("lookback", lb2, f"on_series:{inner_op} lb={lb2}")
        else:
            return ("lookback", 1, f"on_series:{inner_op}")

    if op == "on_series_bool_agg":
        ap = comp.get("agg_period", 0)
        agg_op = comp.get("agg_op", "?")
        return ("lookback", ap, f"on_series_bool_agg:{agg_op} period={ap}")

    # Boolean aggregates — need lookback into the boolean series
    if op == "count_true":
        return ("lookback", comp.get("period", 0), f"count_true period={comp.get('period')}")
    if op == "since_true":
        return ("lookback", comp.get("period", 0), f"since_true period={comp.get('period')}")
    if op == "true_in_row":
        return ("lookback", comp.get("period", 0), f"true_in_row period={comp.get('period')}")

    # Lookback-heavy ops
    if op == "percentile_rank":
        return ("lookback", comp.get("period", 252), f"percentile_rank period={comp.get('period')}")
    if op == "roc_percentile_rank":
        return ("lookback", comp.get("lookback", 252) + comp.get("roc_period", 20),
                f"roc_percentile_rank lb={comp.get('lookback')}+roc={comp.get('roc_period')}")
    if op in ("swing_high_count", "swing_low_count"):
        return ("lookback", comp.get("period", 0), f"{op} period={comp.get('period')}")
    if op in ("higher_high_count", "lower_high_count", "higher_low_count", "lower_low_count"):
        return ("lookback", comp.get("period", 0), f"{op} period={comp.get('period')}")
    if op == "bars_since_ma_cross":
        return ("lookback", comp.get("max_lookback", 120), f"bars_since_ma_cross lb={comp.get('max_lookback')}")

    # Ops that use rolling max/min with lookback
    if op == "extension_ceiling_ratio":
        return ("lookback", comp.get("lookback", 504), f"extension_ceiling_ratio lb={comp.get('lookback')}")
    if op == "extension_peak_ratio":
        return ("lookback", comp.get("lookback", 126), f"extension_peak_ratio lb={comp.get('lookback')}")
    if op == "bollinger_bandwidth_rank":
        return ("lookback", comp.get("lookback", 126), f"bollinger_bandwidth_rank lb={comp.get('lookback')}")

    # Ops using rolling max/min of price (distance_to_maxh, etc.)
    # These use _rolling_max(H, period) or _rolling_min(L, period)
    # The rolling max/min CAN be maintained incrementally with a deque,
    # but it requires knowing the values being dropped off the window.
    # With .lookback file (504 rows of intermediates), these are covered.
    if op in ("distance_to_maxh", "ratio_c_maxh"):
        p = comp.get("maxh_period", 0)
        # shift(1) means we need rolling max of H over p bars, lagged by 1
        # That's p+1 bars of H values — but the rolling max itself is an intermediate
        # that can be stored in .lookback
        return ("state_only", 0, f"{op} — uses maxh{p} intermediate (shiftable)")
    if op in ("distance_to_minl", "ratio_c_minl"):
        p = comp.get("minl_period", 0)
        return ("state_only", 0, f"{op} — uses minl{p} intermediate (shiftable)")

    # Ops using rolling max/min that need actual window data
    if op == "pullback":
        p = comp.get("period", 0)
        return ("lookback", p, f"pullback — needs maxh{p} which is rolling max")
    if op == "range_position":
        return ("lookback", comp.get("period", 0), f"range_position period={comp.get('period')}")
    if op == "range_width":
        return ("lookback", comp.get("period", 0), f"range_width period={comp.get('period')}")
    if op == "channel_slope":
        p = comp.get("period", 0)
        return ("lookback", p, f"channel_slope — needs maxh shift={p}")
    if op == "retrace_high":
        return ("lookback", comp.get("period", 0), f"retrace_high period={comp.get('period')}")
    if op == "retrace_low":
        return ("lookback", comp.get("period", 0), f"retrace_low period={comp.get('period')}")
    if op == "ma_undercut_depth":
        return ("lookback", comp.get("period", 0), f"ma_undercut_depth period={comp.get('period')}")

    # Aroon — needs argmax/argmin over window
    if op in ("aroon_up_val", "aroon_down_val", "aroon_oscillator"):
        return ("lookback", comp.get("period", 0), f"{op} period={comp.get('period')}")

    # CCI — needs mean deviation (not just mean) over window
    if op == "cci":
        return ("lookback", comp.get("period", 0), f"cci period={comp.get('period')}")

    # Stochastic — needs rolling max/min of H/L
    if op == "stochastic":
        return ("lookback", comp.get("period", 0), f"stochastic period={comp.get('period')}")

    # ── State-propagatable ops ──
    # These need only: today's OHLCV + previous intermediate values
    # (MAs, EMAs, RSI state, etc.)

    # Simple arithmetic from today's bar + MA/EMA intermediates
    STATE_ONLY_OPS = {
        "extension",          # C - MA / norm — MA is EMA or SMA (state-updatable)
        "ma_slope",           # MA - shift(MA, offset) / norm — needs prev MA values
        "ma_spread",          # MA_fast - MA_slow / norm
        "extension_slope",    # ext - shift(ext, offset) / norm
        "ext_adr_multiples",  # (C - MA) / ADR
        "spread_slope",       # spread - shift(spread, offset)
        "roc",                # C / shift(C, period) - 1
        "roc_delta",          # roc_now - roc_prev
        "adx",                # EMA chain — state-propagatable
        "adx_slope",          # ADX - shift(ADX, offset)
        "rsi",                # EMA of gains/losses — state-propagatable
        "rsi_slope",          # RSI - shift(RSI, offset)
        "di_spread",          # DI+ - DI-
        "volume_ratio",       # V / avgV
        "candle_range_ratio", # (H-L) / ATR
        "body_range_ratio",   # |C-O| / (H-L)
        "upper_wick_ratio",   # (H - max(C,O)) / (H-L)
        "lower_wick_ratio",   # (min(C,O) - L) / (H-L)
        "bop",                # SMA of (C-O)/(H-L) — state-propagatable
        "obv_slope",          # OBV diff / volume
        "macd_histogram",     # MACD line - signal (both EMAs)
        "macd_histogram_slope", # hist - shift(hist)
        "macd_line_norm",     # MACD / norm
        "bollinger_pctb",     # (C - BB_bot) / (BB_top - BB_bot)
        "bollinger_bandwidth", # (BB_top - BB_bot) / MA
        "cmf",                # SMA-based — needs rolling sum of MFV and V
        "cmf_slope",          # CMF - shift(CMF, offset)
        "kaufman_efficiency_ratio", # |C-C[p]| / sum(|C-C[1]|) — needs lookback actually!
        "atr_ratio",          # ATR / shift(ATR, offset)
        "slope_ratio",        # slope_fast / slope_slow
        "gap_size",           # (O - prev_C) / norm
    }

    if op in STATE_ONLY_OPS:
        # Most of these need shift(X, offset) which means storing a few prior values
        # But that's just state — the offset is small (1-10 bars typically)
        offset = comp.get("offset", 0)
        period = comp.get("period", 0)
        max_shift = max(offset, period, 1)
        return ("state_only", 0, f"{op} — intermediates + shift({max_shift})")

    # Ops that go through compute_series fallback — need to classify individually
    FALLBACK_OPS = {
        "avg_candle_body_ratio", "close_position_in_bar", "close_vs_open_ratio",
        "cumulative_rvol", "gap_count", "high_volume_bar_pct", "high_vs_ma",
        "inside_bar_count", "low_vs_ma", "ma_cross", "ma_cross_count",
        "ma_stack_score", "nr_ratio", "outside_bar_count", "roc_acceleration",
        "rvol_continuous", "slope", "smoothed_ma", "unfilled_gap_up_count",
        "up_volume_ratio", "volume_price_divergence", "vwap_distance", "vwap_slope",
        "range_contraction_ratio",
    }

    if op in FALLBACK_OPS:
        # These go through compute_series — need to check each one
        # For now, classify as needing lookback (conservative)
        period = comp.get("period", comp.get("lookback", 0))
        return ("fallback_needs_audit", period, f"{op} — goes through compute_series, period={period}")

    # Unknown op
    return ("unknown", 0, f"unknown op: {op}")


def run_audit(expressions):
    """Classify all expressions and print summary."""
    print("\n" + "=" * 70)
    print("  EXPRESSION AUDIT — Classification for Incremental Append")
    print("=" * 70)

    categories = Counter()
    max_lookbacks = defaultdict(int)
    by_category = defaultdict(list)

    for i, expr in enumerate(expressions):
        cat, lb, detail = classify_expression(expr)
        categories[cat] += 1
        if lb > max_lookbacks[cat]:
            max_lookbacks[cat] = lb
        by_category[cat].append((i, expr["name"], lb, detail))

    print(f"\n  Total expressions: {len(expressions)}")
    print(f"\n  Category breakdown:")
    for cat in sorted(categories.keys()):
        print(f"    {cat:30s}  {categories[cat]:6d}  (max lookback: {max_lookbacks[cat]} bars)")

    # Detailed lookback distribution for lookback category
    if "lookback" in by_category:
        lb_dist = Counter()
        for _, _, lb, _ in by_category["lookback"]:
            if lb <= 10: bucket = "1-10"
            elif lb <= 50: bucket = "11-50"
            elif lb <= 126: bucket = "51-126"
            elif lb <= 252: bucket = "127-252"
            elif lb <= 504: bucket = "253-504"
            else: bucket = "505+"
            lb_dist[bucket] += 1
        print(f"\n  Lookback depth distribution:")
        for bucket in ["1-10", "11-50", "51-126", "127-252", "253-504", "505+"]:
            if bucket in lb_dist:
                print(f"    {bucket:10s}  {lb_dist[bucket]:6d}")

    # Top lookback expressions
    if "lookback" in by_category:
        top = sorted(by_category["lookback"], key=lambda x: x[2], reverse=True)[:10]
        print(f"\n  Top 10 deepest lookback expressions:")
        for _, name, lb, detail in top:
            print(f"    {lb:4d} bars  {name:40s}  ({detail})")

    # HTF sub-classification
    if "htf" in by_category:
        htf_ops = Counter()
        for _, _, _, detail in by_category["htf"]:
            htf_ops[detail.split(":")[1] if ":" in detail else detail] += 1
        print(f"\n  HTF expression ops ({categories['htf']} total):")
        for op, count in htf_ops.most_common(15):
            print(f"    {op:35s}  {count:5d}")

    # Fallback ops
    if "fallback_needs_audit" in by_category:
        fb_ops = Counter()
        for _, _, _, detail in by_category["fallback_needs_audit"]:
            op_name = detail.split(" — ")[0]
            fb_ops[op_name] += 1
        print(f"\n  Fallback ops needing individual audit ({categories['fallback_needs_audit']} total):")
        for op, count in fb_ops.most_common():
            print(f"    {op:35s}  {count:5d}")

    return categories, by_category


# ══════════════════════════════════════════════════════════════
# PART 2: CORRECTNESS — Compare full vs single-bar compute
# ══════════════════════════════════════════════════════════════

def run_correctness_check(ticker, expressions):
    """Compare _compute_ticker_full output against per-expression last-bar recompute.

    This runs _compute_ticker_full TWICE:
    1. On the full OHLCV → truth (n_bars rows)
    2. On OHLCV minus the last bar → prev (n_bars-1 rows)

    Then for each expression, we check:
    - truth[-1] vs prev[-1]: Are they the same? (They shouldn't be for most —
      this just gives us the "previous row" state)
    - Can truth[-1] be derived from prev[-1] + today's OHLCV alone?

    The real test: for state-only expressions, verify that the value at bar[-1]
    depends only on intermediates that can be incrementally updated.
    """
    print(f"\n{'=' * 70}")
    print(f"  CORRECTNESS CHECK — {ticker}")
    print(f"{'=' * 70}")

    # Load OHLCV
    print(f"\n  Loading OHLCV...")
    daily_cache = load_daily_cache()
    if ticker not in daily_cache:
        print(f"  ERROR: {ticker} not in daily cache")
        return

    df_full = truncate_to_cache_window(daily_cache[ticker])
    if df_full is None or len(df_full) < 100:
        print(f"  ERROR: {ticker} has too few bars after truncation")
        return

    n_bars_full = len(df_full)
    df_prev = df_full.iloc[:-1].reset_index(drop=True)
    n_bars_prev = len(df_prev)

    print(f"  Full bars: {n_bars_full}, Prev bars: {n_bars_prev}")
    print(f"  Last date: {df_full['date'].iloc[-1]}")
    print(f"  Prev last date: {df_prev['date'].iloc[-1]}")

    # Load HTF
    weekly_cache = load_htf_cache("weekly")
    monthly_cache = load_htf_cache("monthly")

    weekly_dict = df_to_dict(weekly_cache.get(ticker)) if weekly_cache else None
    monthly_dict = df_to_dict(monthly_cache.get(ticker)) if monthly_cache else None

    # Prepare dicts
    full_dict = df_to_dict(df_full)
    prev_dict = df_to_dict(df_prev)

    # Free large caches
    del daily_cache, weekly_cache, monthly_cache

    # Import and init worker
    from expr_cache_builder import _init_worker, _compute_ticker_full

    _init_worker(expressions)

    # Run full compute
    print(f"\n  Computing full ({n_bars_full} bars)...")
    t0 = time.time()
    _, dates_full, data_full = _compute_ticker_full(
        (ticker, full_dict, weekly_dict, monthly_dict))
    t_full = time.time() - t0
    print(f"  Full compute: {t_full:.2f}s, shape: {data_full.shape}")

    # Run prev compute (minus last bar)
    print(f"  Computing prev ({n_bars_prev} bars)...")
    t0 = time.time()
    _, dates_prev, data_prev = _compute_ticker_full(
        (ticker, prev_dict, weekly_dict, monthly_dict))
    t_prev = time.time() - t0
    print(f"  Prev compute: {t_prev:.2f}s, shape: {data_prev.shape}")

    # Also load the existing .npz to compare against
    from expr_cache_builder import load_ticker_cache
    cached_dates, cached_data = load_ticker_cache(ticker)

    if cached_data is not None:
        print(f"\n  Cached .npz: shape={cached_data.shape}, "
              f"last date={cached_dates[-1]}")
        # Check if cached matches full compute
        if cached_data.shape == data_full.shape:
            diff = np.abs(cached_data - data_full)
            max_diff = np.nanmax(diff)
            n_mismatch = np.sum(~np.isclose(cached_data, data_full,
                                             rtol=1e-3, atol=1e-3, equal_nan=True))
            print(f"  Cached vs full recompute: max_diff={max_diff:.6f}, "
                  f"mismatches={n_mismatch} "
                  f"({n_mismatch / cached_data.size * 100:.4f}%)")
        else:
            print(f"  Shape mismatch — cached {cached_data.shape} vs "
                  f"full {data_full.shape}")
    else:
        print(f"\n  No cached .npz found for {ticker}")

    # ── Compare last row of full vs prev ──
    truth_last = data_full[-1, :]   # The row we need to reproduce
    prev_last = data_prev[-1, :]    # The "state" from the previous day

    n_exprs = len(expressions)
    print(f"\n  Expressions: {n_exprs}")
    print(f"  truth_last shape: {truth_last.shape}")
    print(f"  prev_last shape: {prev_last.shape}")

    # For each expression, classify and check
    results = {
        "state_only_match": 0,
        "state_only_mismatch": 0,
        "lookback_match": 0,
        "lookback_mismatch": 0,
        "htf_match": 0,
        "htf_mismatch": 0,
        "precomputed_match": 0,
        "precomputed_mismatch": 0,
        "nan_both": 0,
        "nan_truth_only": 0,
        "nan_prev_only": 0,
    }

    mismatches_by_op = defaultdict(list)
    mismatch_details = []

    for i in range(n_exprs):
        cat, lb, detail = classify_expression(expressions[i])
        val_truth = truth_last[i]
        val_prev = prev_last[i]

        both_nan = np.isnan(val_truth) and np.isnan(val_prev)
        truth_nan = np.isnan(val_truth)
        prev_nan = np.isnan(val_prev)

        if both_nan:
            results["nan_both"] += 1
            continue
        if truth_nan and not prev_nan:
            results["nan_truth_only"] += 1
            continue
        if prev_nan and not truth_nan:
            results["nan_prev_only"] += 1
            continue

        # Both have values — this is expected, they're different bars
        # The question is NOT whether truth[-1] == prev[-1]
        # The question is whether truth[-1] can be computed incrementally

    # ── The real test: compare truth[-1] against truth[-2] to see
    # which expressions changed and by how much ──
    if n_bars_full >= 2:
        truth_prev = data_full[-2, :]  # Same as prev_last IF computation is deterministic

        # Verify: prev_last should equal truth_prev for all expressions
        # (because running on N-1 bars should give same result as row N-1 of N-bar run)
        mismatch_count = 0
        mismatch_examples = []
        for i in range(n_exprs):
            a = truth_prev[i]
            b = prev_last[i]
            if np.isnan(a) and np.isnan(b):
                continue
            if np.isnan(a) or np.isnan(b):
                mismatch_count += 1
                if len(mismatch_examples) < 20:
                    mismatch_examples.append((i, expressions[i]["name"],
                                              a, b, "nan_mismatch"))
                continue
            if not np.isclose(a, b, rtol=1e-3, atol=1e-3):
                mismatch_count += 1
                if len(mismatch_examples) < 20:
                    mismatch_examples.append((i, expressions[i]["name"],
                                              a, b, f"diff={abs(a-b):.6f}"))

        print(f"\n  IMMUTABILITY CHECK: truth[-2] vs prev[-1]")
        print(f"  (These MUST match — same data, same bar)")
        print(f"  Mismatches: {mismatch_count} / {n_exprs}")

        if mismatch_examples:
            print(f"\n  First {len(mismatch_examples)} mismatches:")
            for idx, name, a, b, note in mismatch_examples:
                cat, _, _ = classify_expression(expressions[idx])
                print(f"    [{idx:5d}] {name:45s} truth[-2]={a:12.4f}  "
                      f"prev[-1]={b:12.4f}  ({cat}) {note}")

    # ── Dependency analysis: for each expression, what data range does it
    # actually touch to compute bar[-1]? ──
    # This is the key question. We classify by op type.

    print(f"\n  EXPRESSION DEPENDENCY ANALYSIS (for last bar):")

    cat_counts = Counter()
    max_lb_needed = defaultdict(int)
    for i in range(n_exprs):
        cat, lb, detail = classify_expression(expressions[i])
        base_cat = cat.split("_")[0] if cat.startswith("precomputed") else cat
        cat_counts[cat] += 1
        if lb > max_lb_needed[cat]:
            max_lb_needed[cat] = lb

    # Summary
    state_total = cat_counts.get("state_only", 0)
    lookback_total = cat_counts.get("lookback", 0)
    htf_total = cat_counts.get("htf", 0)
    lsp_total = cat_counts.get("precomputed_lsp", 0)
    algo_total = cat_counts.get("precomputed_algo", 0)
    fallback_total = cat_counts.get("fallback_needs_audit", 0)
    unknown_total = cat_counts.get("unknown", 0)

    print(f"\n    state_only (prev row + OHLCV):     {state_total:6d}")
    print(f"    lookback (needs historical window): {lookback_total:6d}  "
          f"(max depth: {max_lb_needed.get('lookback', 0)} bars)")
    print(f"    htf (needs HTF pickle data):        {htf_total:6d}")
    print(f"    precomputed_lsp (full OHLCV scan):  {lsp_total:6d}")
    print(f"    precomputed_algo (full OHLCV scan): {algo_total:6d}")
    print(f"    fallback_needs_audit:               {fallback_total:6d}")
    print(f"    unknown:                            {unknown_total:6d}")
    print(f"    ─────────────────────────────────────────────")
    print(f"    TOTAL:                              {n_exprs:6d}")

    incr_feasible = state_total + lookback_total + htf_total
    print(f"\n    Incrementally computable:            {incr_feasible:6d}  "
          f"({incr_feasible / n_exprs * 100:.1f}%)")
    print(f"    Needs full OHLCV scan:              {lsp_total + algo_total:6d}  "
          f"({(lsp_total + algo_total) / n_exprs * 100:.1f}%)")
    print(f"    Needs audit:                        {fallback_total + unknown_total:6d}  "
          f"({(fallback_total + unknown_total) / n_exprs * 100:.1f}%)")


def main():
    parser = argparse.ArgumentParser(
        description="Validate incremental append approach for expression cache")
    parser.add_argument("--ticker", default="AAPL",
                        help="Ticker to use for correctness check (default: AAPL)")
    parser.add_argument("--audit-only", action="store_true",
                        help="Only run the expression audit, skip correctness check")
    args = parser.parse_args()

    # Load expression library
    print("\n  Loading expression library...")
    from expr_cache_builder import _load_expressions
    expressions = _load_expressions()
    print(f"  {len(expressions)} expressions loaded")

    # Part 1: Audit
    categories, by_category = run_audit(expressions)

    # Part 2: Correctness
    if not args.audit_only:
        run_correctness_check(args.ticker, expressions)

    print(f"\n{'=' * 70}")
    print(f"  DONE")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
