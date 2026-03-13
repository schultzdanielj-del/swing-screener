"""
Setup Grinder — Setup-Specific Correlation Engine.

Answers: which characteristics of the individual stock correlate with
higher win rate and move size?

Unlike the market grinder (which looks at broad market conditions on the
signal date), this looks at traits of the stock itself: price level, ADR,
dollar volume, relative strength vs SPY, days since IPO, etc.

These are NOT price-action/volume patterns (the signal grind already captured
those). These are "what kind of stock is this" traits that the causative
filters can't see.

Phase 1 (this file): Feature extraction — compute setup-specific traits
for every signal in the refinement output. Runs on both pre-refinement
(full signal set) and post-refinement (surviving signals only) for
redundancy analysis.

All data is local. Reads refinement grind output from local_runner/cache/.
Reads 5yr OHLCV cache from local_runner/cache/.

Usage:
    python scripts/setup_grinder.py --setup dtss
    python scripts/setup_grinder.py --setup dtss --dry-run
    python scripts/setup_grinder.py --refinement local_runner/cache/refinement_dtss_cl102_pk5_20260313_122818.json
"""

import os
import sys
import json
import argparse
import pickle
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")

WIN_CLASSES = {"AUTO_WIN", "AI_WIN", "MANUAL_WIN"}

ALL_FEATURES = ["feat_price", "feat_adr", "feat_dollar_volume_20d",
                "feat_days_since_ipo", "feat_rs_d1", "feat_rs_w1"]


# ══════════════════════════════════════════════════════════════
# DATA LOADING (all local)
# ══════════════════════════════════════════════════════════════

def find_latest_refinement(setup_type):
    """Find the most recent refinement_*.json for a setup type in local cache."""
    import glob
    pattern = os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json")
    matches = glob.glob(pattern)
    if not matches:
        raise FileNotFoundError(
            f"No refinement files found for {setup_type} in {CACHE_DIR}\n"
            f"Pattern: {pattern}\n"
            f"Run the refinement grind first: python -m local_runner.pyramid_grinder --setup {setup_type} --blackout"
        )

    def extract_timestamp(path):
        name = os.path.basename(path).replace(".json", "")
        parts = name.split("_")
        if len(parts) >= 2:
            try:
                ts_str = parts[-2] + "_" + parts[-1]
                return datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            except ValueError:
                pass
        return datetime.min

    matches.sort(key=extract_timestamp)
    return matches[-1]


def load_signals_from_refinement(refinement_path, mode="pre"):
    """Load signals from a local refinement grind JSON file.

    Args:
        refinement_path: Path to refinement_*.json in local_runner/cache/
        mode: "pre" = all clusters (winners + losers + eliminated)
              "post" = surviving only (winners + losers, no eliminated)

    Returns:
        list of signal dicts (not a DataFrame — we'll augment them in place)
    """
    with open(refinement_path) as f:
        data = json.load(f)

    winners = data.get("winner_signals", [])
    losers = data.get("loser_signals", [])
    eliminated = data.get("eliminated_signals", [])

    if mode == "pre":
        all_signals = winners + losers + eliminated
    elif mode == "post":
        all_signals = winners + losers
    else:
        raise ValueError(f"Unknown mode: {mode!r} (expected 'pre' or 'post')")

    if not all_signals:
        raise ValueError(f"No signals found in {refinement_path} (mode={mode})")

    return all_signals


def load_5yr_ohlcv():
    """Load 5yr OHLCV cache. Returns dict: ticker -> DataFrame."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py --5yr first.")
    with open(path, "rb") as f:
        return pickle.load(f)


# ══════════════════════════════════════════════════════════════
# RELATIVE STRENGTH FORMULA (vectorized)
# ══════════════════════════════════════════════════════════════
#
# TC2000 PCF formula (for one ticker):
#   ((((C4/O4)-1)*100) + (((C3/O3)-1)*100) + (((C2/O2)-1)*100)
#    + (((C1/O1)-1)*100) + (((C/O)-1)*100)) / 5
#   * (((C+C50)/2) / ATR50)
#
# Part 1: 5-day average intraday % move (open to close)
# Part 2: price-to-volatility scaling factor
#   ((current close + close 50 bars ago) / 2) / 50-period ATR
#
# RS vs SPY = stock's value - SPY's value on same date.
# Positive = stock has stronger vol-adjusted momentum than SPY.


def compute_rs_series_vectorized(opens, highs, lows, closes):
    """Compute the RS formula value for every bar using numpy vectorization.

    Returns np.array of length n, NaN where insufficient data.
    """
    n = len(closes)
    rs = np.full(n, np.nan)

    if n < 55:  # need at least 50 bars + 5 bar rolling window
        return rs

    # Part 1: intraday % move per bar = ((C/O) - 1) * 100
    with np.errstate(divide='ignore', invalid='ignore'):
        intraday_pct = np.where(opens > 0, ((closes / opens) - 1.0) * 100.0, np.nan)

    # 5-bar rolling average of intraday_pct
    cumsum = np.nancumsum(intraday_pct)
    avg_pct = np.full(n, np.nan)
    for i in range(4, n):
        if i == 4:
            avg_pct[i] = cumsum[i] / 5.0
        else:
            avg_pct[i] = (cumsum[i] - cumsum[i - 5]) / 5.0

    # Part 2: ATR50 (SMA of true range, matching TC2000)
    tr = np.full(n, np.nan)
    tr[1:] = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        )
    )
    tr_cumsum = np.nancumsum(tr)
    atr50 = np.full(n, np.nan)
    for i in range(50, n):
        atr50[i] = (tr_cumsum[i] - tr_cumsum[i - 50]) / 50.0

    # C50 = close from 50 bars ago
    c50 = np.full(n, np.nan)
    c50[50:] = closes[:-50]

    # Price-vol scaling: ((C + C50) / 2) / ATR50
    with np.errstate(divide='ignore', invalid='ignore'):
        avg_price = (closes + c50) / 2.0
        scaling = np.where(atr50 > 0, avg_price / atr50, np.nan)

    rs[50:] = avg_pct[50:] * scaling[50:]
    return rs


def _resample_to_weekly(df):
    """Resample daily OHLCV to weekly. Returns DataFrame or None."""
    if len(df) < 10:
        return None
    tmp = df.copy()
    tmp["date"] = pd.to_datetime(tmp["date"])
    tmp = tmp.set_index("date")
    weekly = tmp.resample("W").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])
    if len(weekly) < 55:
        return None
    weekly = weekly.reset_index()
    weekly.columns = ["date", "open", "high", "low", "close", "volume"]
    return weekly


def build_rs_lookup(df, weekly_df=None):
    """Pre-compute RS formula values for every bar of a ticker (vectorized).

    Returns:
        d1_values: dict of date_str -> RS value (daily)
        w1_values: dict of date_str -> RS value (weekly, keyed by daily date)
    """
    opens = df["open"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    closes = df["close"].values.astype(np.float64)
    dates = [str(d)[:10] for d in df["date"].values]

    d1_arr = compute_rs_series_vectorized(opens, highs, lows, closes)
    d1_values = {}
    for i in range(len(dates)):
        if not np.isnan(d1_arr[i]):
            d1_values[dates[i]] = float(d1_arr[i])

    w1_values = {}
    if weekly_df is not None and len(weekly_df) >= 55:
        w_opens = weekly_df["open"].values.astype(np.float64)
        w_highs = weekly_df["high"].values.astype(np.float64)
        w_lows = weekly_df["low"].values.astype(np.float64)
        w_closes = weekly_df["close"].values.astype(np.float64)
        w_dates = [str(d)[:10] for d in weekly_df["date"].values]

        w1_arr = compute_rs_series_vectorized(w_opens, w_highs, w_lows, w_closes)
        weekly_rs = {}
        for i in range(len(w_dates)):
            if not np.isnan(w1_arr[i]):
                weekly_rs[w_dates[i]] = float(w1_arr[i])

        if weekly_rs:
            sorted_w_dates = sorted(weekly_rs.keys())
            sorted_w_vals = [weekly_rs[d] for d in sorted_w_dates]
            for daily_date in dates:
                idx = _bisect_right_str(sorted_w_dates, daily_date) - 1
                if idx >= 0:
                    w1_values[daily_date] = sorted_w_vals[idx]

    return d1_values, w1_values


def _bisect_right_str(sorted_list, target):
    """Binary search for rightmost insertion point in sorted string list."""
    lo, hi = 0, len(sorted_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_list[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ══════════════════════════════════════════════════════════════
# PARALLEL RS PRE-COMPUTATION
# ══════════════════════════════════════════════════════════════

def _compute_ticker_rs(args):
    """Worker: compute RS lookup for one ticker. Runs in subprocess."""
    ticker, df_dict = args
    try:
        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if len(df) < 55:
            return (ticker, {}, {})

        weekly_df = None
        if len(df) >= 10:
            tmp = df.copy().set_index("date")
            weekly = tmp.resample("W").agg({
                "open": "first", "high": "max",
                "low": "min", "close": "last", "volume": "sum",
            }).dropna(subset=["close"])
            if len(weekly) >= 55:
                weekly_df = weekly.reset_index()
                weekly_df.columns = ["date", "open", "high", "low", "close", "volume"]

        d1, w1 = build_rs_lookup(df, weekly_df)
        return (ticker, d1, w1)
    except Exception:
        return (ticker, {}, {})


def precompute_all_rs(ohlcv_cache, tickers_needed):
    """Compute RS lookups for all needed tickers + SPY in parallel."""
    import time

    all_tickers = set(tickers_needed) | {"SPY"}
    work_items = []
    for ticker in all_tickers:
        df = ohlcv_cache.get(ticker)
        if df is None or len(df) < 55:
            continue
        df_dict = {
            "date": df["date"].values, "open": df["open"].values,
            "high": df["high"].values, "low": df["low"].values,
            "close": df["close"].values, "volume": df["volume"].values,
        }
        work_items.append((ticker, df_dict))

    n_workers = min(os.cpu_count() or 4, len(work_items))
    print(f"  Computing RS for {len(work_items)} tickers ({n_workers} workers)...")

    t0 = time.time()
    rs_cache = {}
    completed = 0

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_compute_ticker_rs, item): item[0]
                   for item in work_items}
        for future in as_completed(futures):
            ticker, d1, w1 = future.result()
            rs_cache[ticker] = (d1, w1)
            completed += 1
            if completed % 50 == 0 or completed == len(work_items):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(work_items)} tickers [{elapsed:.1f}s]")

    elapsed = time.time() - t0
    print(f"  RS pre-computation done: {len(rs_cache)} tickers in {elapsed:.1f}s")
    return rs_cache


# ══════════════════════════════════════════════════════════════
# FEATURE COMPUTATION
# ══════════════════════════════════════════════════════════════

def _find_bar_idx(dates, signal_date_str):
    """Find the index of signal_date in a date array/series."""
    for i, d in enumerate(dates):
        d_str = str(d)[:10]
        if d_str == signal_date_str:
            return i
    return -1


def compute_adr_14(df, bar_idx):
    """14-bar Average Daily Range at bar_idx. Computed from OHLCV directly."""
    start = max(0, bar_idx - 13)
    window = df.iloc[start:bar_idx + 1]
    if len(window) < 5:
        return None
    ranges = window["high"].values - window["low"].values
    adr = float(np.nanmean(ranges))
    if adr <= 0 or np.isnan(adr):
        return None
    return adr


def compute_dollar_volume_20d(df, bar_idx):
    """Average daily dollar volume over 20 bars ending at bar_idx."""
    start = max(0, bar_idx - 19)
    window = df.iloc[start:bar_idx + 1]
    if len(window) < 5:
        return None
    dv = window["close"].values * window["volume"].values
    return float(np.nanmean(dv))


def compute_days_since_ipo(df, bar_idx):
    """Trading days from first bar in OHLCV to signal bar."""
    return bar_idx


def compute_features_for_signals(signals, ohlcv_cache, rs_cache):
    """Compute setup-specific features for every signal.

    Augments each signal dict in place with new feature fields.
    Returns a stats dict with coverage and summary info.
    """
    spy_d1_rs, spy_w1_rs = rs_cache.get("SPY", ({}, {}))

    n_total = len(signals)
    n_ohlcv_found = 0
    n_bar_found = 0
    feature_counts = {f.replace("feat_", ""): 0 for f in ALL_FEATURES}

    # Cache: ticker -> (dates_list, df) to avoid repeated string conversion
    ticker_date_cache = {}

    for sig in signals:
        ticker = sig.get("ticker", "")
        signal_date = str(sig.get("signal_date", ""))[:10]

        for f in ALL_FEATURES:
            sig[f] = None

        df = ohlcv_cache.get(ticker)
        if df is None or len(df) < 20:
            continue
        n_ohlcv_found += 1

        # Cache date string conversion per ticker
        if ticker not in ticker_date_cache:
            ticker_date_cache[ticker] = [str(d)[:10] for d in df["date"].values]
        dates = ticker_date_cache[ticker]

        bar_idx = _find_bar_idx(dates, signal_date)
        if bar_idx < 0:
            continue
        n_bar_found += 1

        # 1. Price
        price = float(df.iloc[bar_idx]["close"])
        if not np.isnan(price):
            sig["feat_price"] = price
            feature_counts["price"] += 1

        # 2. ADR (14-bar average daily range, computed from OHLCV)
        adr = compute_adr_14(df, bar_idx)
        if adr is not None:
            sig["feat_adr"] = adr
            feature_counts["adr"] += 1

        # 3. Dollar volume (20-day avg)
        dv = compute_dollar_volume_20d(df, bar_idx)
        if dv is not None:
            sig["feat_dollar_volume_20d"] = dv
            feature_counts["dollar_volume_20d"] += 1

        # 4. Days since IPO
        sig["feat_days_since_ipo"] = compute_days_since_ipo(df, bar_idx)
        feature_counts["days_since_ipo"] += 1

        # 5. RS vs SPY (D1 and W1)
        d1_rs, w1_rs = rs_cache.get(ticker, ({}, {}))

        stock_d1 = d1_rs.get(signal_date)
        spy_d1 = spy_d1_rs.get(signal_date)
        if stock_d1 is not None and spy_d1 is not None:
            sig["feat_rs_d1"] = stock_d1 - spy_d1
            feature_counts["rs_d1"] += 1

        stock_w1 = w1_rs.get(signal_date)
        spy_w1 = spy_w1_rs.get(signal_date)
        if stock_w1 is not None and spy_w1 is not None:
            sig["feat_rs_w1"] = stock_w1 - spy_w1
            feature_counts["rs_w1"] += 1

    # Summary stats
    feature_stats = {}
    for fk in ALL_FEATURES:
        vals = [s[fk] for s in signals if s[fk] is not None]
        if vals:
            arr = np.array(vals, dtype=np.float64)
            feature_stats[fk] = {
                "count": len(vals),
                "min": float(np.nanmin(arr)),
                "median": float(np.nanmedian(arr)),
                "mean": float(np.nanmean(arr)),
                "max": float(np.nanmax(arr)),
            }
        else:
            feature_stats[fk] = {"count": 0}

    return {
        "n_total": n_total,
        "n_ohlcv_found": n_ohlcv_found,
        "n_bar_found": n_bar_found,
        "feature_counts": feature_counts,
        "feature_stats": feature_stats,
    }


# ══════════════════════════════════════════════════════════════
# QUARTILE ANALYSIS + REDUNDANCY
# ══════════════════════════════════════════════════════════════

def compute_quartile_win_rates(signals, feature_key):
    """Compute win rate per quartile for one feature on a signal set.

    Returns dict with q1-q4 win rates and Q4-Q1 spread, or None if
    insufficient data.
    """
    vals = [(s[feature_key], 1 if str(s.get("classification", "")).upper() in WIN_CLASSES else 0)
            for s in signals if s[feature_key] is not None]
    if len(vals) < 20:
        return None

    arr = np.array([v[0] for v in vals])
    wins = np.array([v[1] for v in vals])

    q25, q50, q75 = np.percentile(arr, [25, 50, 75])

    bands = {
        "Q1": arr <= q25,
        "Q2": (arr > q25) & (arr <= q50),
        "Q3": (arr > q50) & (arr <= q75),
        "Q4": arr > q75,
    }

    result = {"n": len(vals), "q25": float(q25), "q50": float(q50), "q75": float(q75)}
    for band_name, mask in bands.items():
        n = int(mask.sum())
        wr = float(np.mean(wins[mask])) if n > 0 else None
        result[f"wr_{band_name.lower()}"] = wr
        result[f"n_{band_name.lower()}"] = n

    # Q4-Q1 spread
    if result["wr_q1"] is not None and result["wr_q4"] is not None:
        result["spread"] = result["wr_q4"] - result["wr_q1"]
    else:
        result["spread"] = None

    return result


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(setup_type, refinement_path, dry_run=False):
    print("\n" + "=" * 70)
    print("  SETUP GRINDER — Feature Extraction + Redundancy Analysis")
    print("=" * 70)
    print(f"  Setup:      {setup_type}")
    print(f"  Source:     {os.path.basename(refinement_path)}")

    # ── 1. Load signals ──────────────────────────────────────
    print(f"\n  Loading signals (pre-refinement)...")
    pre_signals = load_signals_from_refinement(refinement_path, mode="pre")
    pre_wins = sum(1 for s in pre_signals if str(s.get("classification", "")).upper() in WIN_CLASSES)
    pre_losses = len(pre_signals) - pre_wins
    print(f"  PRE:  {len(pre_signals)} signals  |  {pre_wins} wins  {pre_losses} losses  "
          f"|  baseline WR: {pre_wins/len(pre_signals):.3f}")

    print(f"\n  Loading signals (post-refinement)...")
    post_signals = load_signals_from_refinement(refinement_path, mode="post")
    post_wins = sum(1 for s in post_signals if str(s.get("classification", "")).upper() in WIN_CLASSES)
    post_losses = len(post_signals) - post_wins
    print(f"  POST: {len(post_signals)} signals  |  {post_wins} wins  {post_losses} losses  "
          f"|  baseline WR: {post_wins/len(post_signals):.3f}")

    # ── 2. Load OHLCV ────────────────────────────────────────
    print(f"\n  Loading 5yr OHLCV cache...")
    ohlcv_cache = load_5yr_ohlcv()
    print(f"  {len(ohlcv_cache)} tickers loaded")

    spy_df = ohlcv_cache.get("SPY")
    if spy_df is None:
        raise ValueError("SPY not found in OHLCV cache — needed for RS computation")
    print(f"  SPY: {len(spy_df)} bars ({str(spy_df['date'].iloc[0])[:10]} to {str(spy_df['date'].iloc[-1])[:10]})")

    # ── 3. Pre-compute RS ────────────────────────────────────
    all_tickers = set()
    for s in pre_signals:
        all_tickers.add(s.get("ticker", ""))
    for s in post_signals:
        all_tickers.add(s.get("ticker", ""))
    all_tickers.discard("")

    print(f"\n  {len(all_tickers)} unique tickers in signal set")
    rs_cache = precompute_all_rs(ohlcv_cache, all_tickers)

    # ── 4. Compute features (pre-refinement) ─────────────────
    print(f"\n  Computing features (pre-refinement, {len(pre_signals)} signals)...")
    pre_stats = compute_features_for_signals(pre_signals, ohlcv_cache, rs_cache)

    print(f"\n  PRE-REFINEMENT coverage:")
    print(f"    OHLCV found:  {pre_stats['n_ohlcv_found']}/{pre_stats['n_total']}")
    print(f"    Bar matched:  {pre_stats['n_bar_found']}/{pre_stats['n_total']}")
    print(f"\n  {'Feature':<30} {'Count':>6}  {'Min':>12}  {'Median':>12}  {'Max':>12}")
    print(f"  {'-'*78}")
    for fk, fs in pre_stats["feature_stats"].items():
        label = fk.replace("feat_", "")
        if fs["count"] == 0:
            print(f"  {label:<30} {0:>6}  {'—':>12}  {'—':>12}  {'—':>12}")
        else:
            print(f"  {label:<30} {fs['count']:>6}  {fs['min']:>12.2f}  {fs['median']:>12.2f}  {fs['max']:>12.2f}")

    # ── 5. Compute features (post-refinement) ────────────────
    print(f"\n  Computing features (post-refinement, {len(post_signals)} signals)...")
    post_stats = compute_features_for_signals(post_signals, ohlcv_cache, rs_cache)

    print(f"\n  POST-REFINEMENT coverage:")
    print(f"    OHLCV found:  {post_stats['n_ohlcv_found']}/{post_stats['n_total']}")
    print(f"    Bar matched:  {post_stats['n_bar_found']}/{post_stats['n_total']}")
    print(f"\n  {'Feature':<30} {'Count':>6}  {'Min':>12}  {'Median':>12}  {'Max':>12}")
    print(f"  {'-'*78}")
    for fk, fs in post_stats["feature_stats"].items():
        label = fk.replace("feat_", "")
        if fs["count"] == 0:
            print(f"  {label:<30} {0:>6}  {'—':>12}  {'—':>12}  {'—':>12}")
        else:
            print(f"  {label:<30} {fs['count']:>6}  {fs['min']:>12.2f}  {fs['median']:>12.2f}  {fs['max']:>12.2f}")

    # ── 6. Quartile win rates: pre and post ──────────────────
    print(f"\n  {'='*70}")
    print(f"  QUARTILE WIN RATES")
    print(f"  {'='*70}")

    pre_quartiles = {}
    post_quartiles = {}

    for fk in ALL_FEATURES:
        label = fk.replace("feat_", "")
        pre_q = compute_quartile_win_rates(pre_signals, fk)
        post_q = compute_quartile_win_rates(post_signals, fk)
        pre_quartiles[fk] = pre_q
        post_quartiles[fk] = post_q

        if pre_q is None:
            continue

        print(f"\n  {label}:")

        # Pre-refinement line
        parts = []
        for qname in ["q1", "q2", "q3", "q4"]:
            wr = pre_q[f"wr_{qname}"]
            n = pre_q[f"n_{qname}"]
            wr_str = f"{wr:.1%}" if wr is not None else "N/A"
            parts.append(f"{qname.upper()}={wr_str}({n})")
        spread_str = f"  spread={pre_q['spread']:+.1%}" if pre_q["spread"] is not None else ""
        print(f"    PRE:  {' | '.join(parts)}{spread_str}")

        # Post-refinement line
        if post_q is not None:
            parts = []
            for qname in ["q1", "q2", "q3", "q4"]:
                wr = post_q[f"wr_{qname}"]
                n = post_q[f"n_{qname}"]
                wr_str = f"{wr:.1%}" if wr is not None else "N/A"
                parts.append(f"{qname.upper()}={wr_str}({n})")
            spread_str = f"  spread={post_q['spread']:+.1%}" if post_q["spread"] is not None else ""
            print(f"    POST: {' | '.join(parts)}{spread_str}")
        else:
            print(f"    POST: insufficient data")

    # ── 7. Redundancy analysis ───────────────────────────────
    print(f"\n  {'='*70}")
    print(f"  REDUNDANCY ANALYSIS (post spread / pre spread)")
    print(f"  {'='*70}")
    print(f"  ratio >= 0.5 = genuine (feature has signal beyond refinement)")
    print(f"  ratio < 0.5  = redundant (refinement already captures it)")
    print(f"\n  {'Feature':<25} {'Pre spread':>11} {'Post spread':>12} {'Ratio':>7}  {'Verdict'}")
    print(f"  {'-'*70}")

    n_genuine = 0
    n_redundant = 0

    for fk in ALL_FEATURES:
        label = fk.replace("feat_", "")
        pre_q = pre_quartiles.get(fk)
        post_q = post_quartiles.get(fk)

        pre_spread = pre_q["spread"] if pre_q and pre_q.get("spread") is not None else None
        post_spread = post_q["spread"] if post_q and post_q.get("spread") is not None else None

        ratio = None
        if pre_spread is not None and abs(pre_spread) > 0.01 and post_spread is not None:
            ratio = post_spread / pre_spread

        if ratio is not None:
            verdict = "GENUINE" if ratio >= 0.5 else "redundant"
            if ratio >= 0.5:
                n_genuine += 1
            else:
                n_redundant += 1
            print(f"  {label:<25} {pre_spread:>+10.1%} {post_spread:>+11.1%} {ratio:>7.2f}  {verdict}")
        elif pre_spread is not None:
            print(f"  {label:<25} {pre_spread:>+10.1%} {'—':>11} {'—':>7}  no post data")
        else:
            print(f"  {label:<25} {'—':>10} {'—':>11} {'—':>7}  no pre data")

    print(f"\n  Genuine: {n_genuine}  |  Redundant: {n_redundant}")

    print(f"\n  ✓ Setup grinder complete.")
    print(f"  Pre:  {pre_stats['n_bar_found']}/{pre_stats['n_total']} signals with features")
    print(f"  Post: {post_stats['n_bar_found']}/{post_stats['n_total']} signals with features")

    return pre_signals, post_signals, pre_stats, post_stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup Grinder — setup-specific correlation features")
    parser.add_argument("--setup", default="dtss", help="Setup type (finds latest refinement file)")
    parser.add_argument("--refinement", help="Path to specific refinement JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't save results")
    args = parser.parse_args()

    setup_type = args.setup

    if args.refinement:
        refinement_path = args.refinement
        basename = os.path.basename(refinement_path)
        if basename.startswith("refinement_") and args.setup == "dtss":
            parts = basename.split("_")
            if len(parts) >= 2:
                setup_type = parts[1]
    else:
        refinement_path = find_latest_refinement(setup_type)
        print(f"  Latest refinement: {os.path.basename(refinement_path)}")

    run(setup_type, refinement_path, dry_run=args.dry_run)
