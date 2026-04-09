"""
Scan Engine — Evaluate locked conditions against intermediate cache.

Derives scan-needed expressions on-the-fly from .im files (196 intermediates).
No full expression cache required. Supports late-eval for LSP conditions.

Usage:
    # Scan one setup:
    python local_runner/scan_engine.py --setup dtss

    # Scan all setups:
    python local_runner/scan_engine.py --all

    # Validate against signal_filter.py results:
    python local_runner/scan_engine.py --validate dtss
"""

import os
import sys
import json
import time
import pickle
import warnings
import argparse
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
IM_CACHE_DIR = os.path.join(CACHE_DIR, "intermediate_series")
EXPR_CACHE_START = "2020-01-02"

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


def _find_main_repo():
    """Find the main repo root, even from a worktree."""
    git_common = os.path.join(REPO_ROOT, ".git")
    if os.path.isfile(git_common):
        with open(git_common) as f:
            line = f.read().strip()
        if line.startswith("gitdir:"):
            gitdir = line.split(":", 1)[1].strip()
            return os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
    return REPO_ROOT


# ══════════════════════════════════════════════════════════════
# CONDITION LOADING
# ══════════════════════════════════════════════════════════════

def load_conditions(setup_type):
    """Load locked conditions for a setup type from pyramid_results file.

    Returns list of {name, low, high, compute} dicts.
    """
    main_repo = _find_main_repo()
    # Search for condition files
    search_paths = [
        os.path.join(main_repo, "data", f"pyramid_results_{setup_type}.json"),
        os.path.join(main_repo, "local_runner", "cache", f"pyramid_results_{setup_type}.json"),
        os.path.join(main_repo, "cache", f"pyramid_results_{setup_type}.json"),
    ]
    for path in search_paths:
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            conds = data.get("all_conditions", [])
            print(f"  Loaded {len(conds)} conditions from {os.path.basename(path)}")
            return conds

    raise FileNotFoundError(f"No condition file found for setup '{setup_type}'")


def classify_conditions(conditions):
    """Classify conditions by computation type for optimal evaluation order.

    Returns: (dispatch_conds, other_conds, lsp_conds)
    - dispatch_conds: can be derived from intermediates (fast)
    - other_conds: need ExpressionEngine or historical data (slower)
    - lsp_conds: need LSP detector (expensive, late-eval)
    """
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
    LSP_SOURCES = {"lsp"}

    dispatch_conds = []
    other_conds = []
    lsp_conds = []

    for cond in conditions:
        compute = cond.get("compute", {})
        op = compute.get("op", "")
        source = compute.get("source", "daily")

        if source in LSP_SOURCES:
            lsp_conds.append(cond)
        elif op in DISPATCH_OPS:
            dispatch_conds.append(cond)
        else:
            other_conds.append(cond)

    return dispatch_conds, other_conds, lsp_conds


# ══════════════════════════════════════════════════════════════
# EXPRESSION DERIVATION FROM INTERMEDIATES
# ══════════════════════════════════════════════════════════════

def derive_expression_from_im(im_dict, compute, n_bars):
    """Derive one expression value array from intermediate dict.

    Args:
        im_dict: {col_name: float64 array} from .im file
        compute: expression compute spec dict
        n_bars: number of bars

    Returns: float64 array of expression values, or None on failure.
    """
    from expr_cache_builder import dispatch_arith_numpy
    try:
        result = dispatch_arith_numpy(compute, im_dict)
        if result is not None:
            return result[:n_bars]
    except Exception:
        pass
    return None


def derive_expression_via_engine(engine, compute, series_registry=None):
    """Derive one expression via ExpressionEngine fallback.

    For ops not handled by dispatch_arith_numpy (bool_aggs, SLOW_OPS, etc.)
    """
    from scripts.backtest_conditions import compute_series
    try:
        s = compute_series(engine, compute, series_registry=series_registry or {})
        if s is not None:
            return np.asarray(s, dtype=np.float64)
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════
# SCAN WORKER
# ══════════════════════════════════════════════════════════════

_w_daily_cache = None
_w_dispatch_conds = None
_w_other_conds = None
_w_lsp_conds = None


def _init_scan_worker(daily_cache, dispatch_conds, other_conds, lsp_conds):
    global _w_daily_cache, _w_dispatch_conds, _w_other_conds, _w_lsp_conds
    _w_daily_cache = daily_cache
    _w_dispatch_conds = dispatch_conds
    _w_other_conds = other_conds
    _w_lsp_conds = lsp_conds


def _scan_ticker(ticker):
    """Scan one ticker against all conditions. Returns signal dict or None."""
    from intermediate_cache_builder import read_im_as_dict
    from expr_cache_builder import build_numpy_intermediates

    # Load .im file
    im_path = os.path.join(IM_CACHE_DIR, f"{ticker}.im")
    im_dict, im_dates = read_im_as_dict(im_path)
    if im_dict is None or len(im_dates) < 50:
        return None

    n_bars = len(im_dates)
    last_idx = n_bars - 1
    df = None  # Initialized here so LSP block can reference it even if other_conds is empty

    # ── Pass 1: Dispatch conditions (from intermediates, fast) ──
    # If dispatch fails for a condition (returns None), defer it to Pass 2
    # rather than failing the ticker. This handles dispatch gaps (e.g. volume_ratio
    # with avg_period=3 when only avgv10/avgv20/avgv50 intermediates exist).
    deferred_dispatch = []
    for cond in _w_dispatch_conds:
        values = derive_expression_from_im(im_dict, cond["compute"], n_bars)
        if values is None:
            deferred_dispatch.append(cond)  # Defer to ExpressionEngine
            continue
        val = values[last_idx]
        if np.isnan(val) or val < cond["low"] or val > cond["high"]:
            return None  # Failed condition

    # ── Pass 2: Other conditions + deferred dispatch (ExpressionEngine, slower) ──
    combined_other = list(_w_other_conds) + deferred_dispatch
    if combined_other:
        df = _w_daily_cache.get(ticker)
        if df is None:
            return None
        df = df.copy()
        mask = pd.to_datetime(df["date"]) >= pd.Timestamp(EXPR_CACHE_START)
        df = df[mask].reset_index(drop=True)
        if len(df) != n_bars:
            return None

        from scripts.expression_engine import ExpressionEngine
        engine = ExpressionEngine(df)
        series_registry = {}

        for cond in combined_other:
            compute = cond["compute"]
            op = compute.get("op", "")

            # Try dispatch first (some ops might work)
            values = derive_expression_from_im(im_dict, compute, n_bars)

            # Fallback to ExpressionEngine
            if values is None:
                values = derive_expression_via_engine(engine, compute, series_registry)

            if values is None:
                return None

            val = float(values[last_idx]) if last_idx < len(values) else np.nan
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                return None

    # ── Pass 3: LSP conditions (late eval — only if passes 1 & 2) ──
    if _w_lsp_conds:
        if df is None:
            df = _w_daily_cache.get(ticker)
            if df is None:
                return None
            df = df.copy()
            mask = pd.to_datetime(df["date"]) >= pd.Timestamp(EXPR_CACHE_START)
            df = df[mask].reset_index(drop=True)

        try:
            from scripts.lsp_detector_v2 import detect_lsp_levels
            lsp_results = detect_lsp_levels(df)
            if lsp_results is None:
                return None

            for cond in _w_lsp_conds:
                name = cond["name"]
                if name not in lsp_results:
                    return None
                arr = lsp_results[name]
                val = float(arr[last_idx]) if last_idx < len(arr) else np.nan
                if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                    return None
        except Exception:
            return None

    # All conditions passed — this is a signal
    close = float(im_dict["close"][last_idx])
    return {
        "ticker": ticker,
        "date": im_dates[last_idx],
        "close": close,
    }


def _scan_batch(tickers):
    """Scan a batch of tickers. Returns list of signal dicts."""
    signals = []
    for ticker in tickers:
        result = _scan_ticker(ticker)
        if result is not None:
            signals.append(result)
    return signals


# ══════════════════════════════════════════════════════════════
# MAIN SCAN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def scan_setup(setup_type, daily_cache=None, workers=14):
    """Run a full scan for one setup type.

    Args:
        setup_type: e.g. "dtss"
        daily_cache: {ticker: DataFrame}, loaded if None
        workers: number of parallel workers

    Returns: list of signal dicts [{ticker, date, close}, ...]
    """
    t0 = time.time()

    # Load conditions
    conditions = load_conditions(setup_type)
    dispatch_conds, other_conds, lsp_conds = classify_conditions(conditions)
    print(f"  Conditions: {len(dispatch_conds)} dispatch, {len(other_conds)} other, {len(lsp_conds)} LSP")

    # Get ticker list from .im files
    if not os.path.isdir(IM_CACHE_DIR):
        raise FileNotFoundError(f"Intermediate cache not found at {IM_CACHE_DIR}")

    im_tickers = [f[:-3] for f in os.listdir(IM_CACHE_DIR) if f.endswith(".im")]
    print(f"  Scanning {len(im_tickers)} tickers...")

    # Always load daily cache — dispatch conditions may defer to ExpressionEngine
    # when intermediates don't cover the required period (e.g. volume_ratio avg_period=3)
    if daily_cache is None:
        from intermediate_cache_builder import load_daily_cache
        daily_cache = load_daily_cache()

    # Build slim daily cache (only tickers with .im files, only needed columns)
    slim_cache = {}
    if daily_cache:
        for t in im_tickers:
            if t in daily_cache:
                slim_cache[t] = daily_cache[t]

    # Batch tickers for workers
    batch_size = max(1, len(im_tickers) // (workers * 4))
    batches = [im_tickers[i:i + batch_size] for i in range(0, len(im_tickers), batch_size)]

    all_signals = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_scan_worker,
        initargs=(slim_cache, dispatch_conds, other_conds, lsp_conds)
    ) as pool:
        futures = {pool.submit(_scan_batch, batch): batch for batch in batches}
        for fut in as_completed(futures):
            signals = fut.result()
            all_signals.extend(signals)

    elapsed = time.time() - t0
    print(f"  Found {len(all_signals)} signals in {elapsed:.2f}s")
    return all_signals


def validate_scan(setup_type, daily_cache=None):
    """Validate scan_engine results against signal_filter.py.

    Compares today's signals from both approaches.
    """
    t0 = time.time()

    # Run scan engine
    signals_new = scan_setup(setup_type, daily_cache, workers=1)
    new_tickers = {s["ticker"] for s in signals_new}
    new_dates = {(s["ticker"], s["date"]) for s in signals_new}

    # Run signal_filter for comparison
    from intermediate_cache_builder import load_daily_cache
    if daily_cache is None:
        daily_cache = load_daily_cache()

    conditions = load_conditions(setup_type)
    main_repo = _find_main_repo()
    main_cache_dir = os.path.join(main_repo, "local_runner", "cache")

    # Use the existing signal_filter scan logic
    from expr_cache_builder import ExprSeriesCache
    sys.path.insert(0, os.path.join(main_repo, "scripts"))
    from signal_filter import scan_all_signals, _build_slim_cache

    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  WARNING: expression cache not valid, can't validate")
        return

    slim_cache = _build_slim_cache(daily_cache)

    signals_old = scan_all_signals(slim_cache, conditions, 1, expr_cache)

    old_today = {(s["ticker"], s["date"]) for s in signals_old
                 if s["date"] == signals_new[0]["date"]} if signals_new else set()

    old_today_tickers = {t for t, d in old_today}
    only_scan = new_tickers - old_today_tickers
    only_filter = old_today_tickers - new_tickers

    print(f"\n  Validation results:")
    print(f"    Scan engine signals (last bar): {len(new_tickers)} tickers")
    print(f"    Signal filter signals (last bar): {len(old_today_tickers)} tickers")
    print(f"    In both: {len(new_tickers & old_today_tickers)} tickers")
    print(f"    Only in scan engine: {only_scan}")
    print(f"    Only in signal filter: {only_filter}")
    print(f"    Elapsed: {time.time() - t0:.2f}s")


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan Engine")
    parser.add_argument("--setup", type=str, help="Scan one setup type (e.g. dtss)")
    parser.add_argument("--all", action="store_true", help="Scan all setups")
    parser.add_argument("--validate", type=str, help="Validate against signal_filter.py")
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args()

    if args.setup:
        signals = scan_setup(args.setup, workers=args.workers)
        for s in signals:
            print(f"  {s['ticker']:>6s}  {s['date']}  ${s['close']:.2f}")

    elif args.validate:
        validate_scan(args.validate)

    elif args.all:
        # Scan all known setups
        from intermediate_cache_builder import load_daily_cache
        daily_cache = load_daily_cache()
        for setup in ["dtss"]:  # Add more setups as they exist
            print(f"\n{'='*60}")
            print(f"  Setup: {setup}")
            print(f"{'='*60}")
            signals = scan_setup(setup, daily_cache, workers=args.workers)
            for s in signals:
                print(f"    {s['ticker']:>6s}  {s['date']}  ${s['close']:.2f}")

    else:
        parser.print_help()
