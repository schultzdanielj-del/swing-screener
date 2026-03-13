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

warnings.filterwarnings("ignore")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")

# RS vs SPY lookback windows (trading days)
RS_WINDOWS = [10, 20, 50, 63]

WIN_CLASSES = {"AUTO_WIN", "AI_WIN", "MANUAL_WIN"}


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
# FEATURE COMPUTATION
# ══════════════════════════════════════════════════════════════

def _find_bar_idx(dates, signal_date_str):
    """Find the index of signal_date in a date array/series.

    Returns index or -1 if not found.
    """
    # dates could be a pandas Series of datetime, or strings
    for i, d in enumerate(dates):
        d_str = str(d)[:10]
        if d_str == signal_date_str:
            return i
    return -1


def compute_dollar_volume_20d(df, bar_idx):
    """Average daily dollar volume over 20 bars ending at bar_idx."""
    start = max(0, bar_idx - 19)
    window = df.iloc[start:bar_idx + 1]
    if len(window) < 5:  # need at least 5 bars
        return None
    dv = window["close"].values * window["volume"].values
    return float(np.nanmean(dv))


def compute_rs_vs_spy(df, bar_idx, spy_df, spy_date_idx, window):
    """Stock's % change over N days minus SPY's % change over same N days.

    Positive = stock outperforming SPY. Negative = underperforming.
    """
    if bar_idx < window or spy_date_idx < window:
        return None

    stock_now = df.iloc[bar_idx]["close"]
    stock_then = df.iloc[bar_idx - window]["close"]
    if stock_then <= 0 or np.isnan(stock_then) or np.isnan(stock_now):
        return None
    stock_roc = (stock_now - stock_then) / stock_then

    spy_now = spy_df.iloc[spy_date_idx]["close"]
    spy_then = spy_df.iloc[spy_date_idx - window]["close"]
    if spy_then <= 0 or np.isnan(spy_then) or np.isnan(spy_now):
        return None
    spy_roc = (spy_now - spy_then) / spy_then

    return float(stock_roc - spy_roc)


def compute_days_since_ipo(df, bar_idx):
    """Trading days from first bar in OHLCV to signal bar.

    This is a rough proxy — capped at whatever the cache holds (5yr = ~1260 bars).
    """
    return bar_idx  # first bar in cache = index 0, so bar_idx IS the count


def compute_features_for_signals(signals, ohlcv_cache, spy_df):
    """Compute setup-specific features for every signal.

    Augments each signal dict in place with new feature fields.
    Returns a stats dict with coverage and summary info.
    """
    # Build SPY date lookup: date_str -> index
    spy_dates = [str(d)[:10] for d in spy_df["date"].values]
    spy_date_to_idx = {d: i for i, d in enumerate(spy_dates)}

    # Track coverage
    n_total = len(signals)
    n_ohlcv_found = 0
    n_bar_found = 0
    feature_counts = {
        "price": 0,
        "adr": 0,
        "dollar_volume_20d": 0,
        "days_since_ipo": 0,
    }
    for w in RS_WINDOWS:
        feature_counts[f"rs_vs_spy_{w}d"] = 0

    for sig in signals:
        ticker = sig.get("ticker", "")
        signal_date = str(sig.get("signal_date", ""))[:10]

        # Initialize all feature fields to None
        sig["feat_price"] = None
        sig["feat_adr"] = None
        sig["feat_dollar_volume_20d"] = None
        sig["feat_days_since_ipo"] = None
        for w in RS_WINDOWS:
            sig[f"feat_rs_vs_spy_{w}d"] = None

        # Look up ticker OHLCV
        df = ohlcv_cache.get(ticker)
        if df is None or len(df) < 20:
            continue
        n_ohlcv_found += 1

        # Ensure date column is string for matching
        dates = [str(d)[:10] for d in df["date"].values]
        bar_idx = _find_bar_idx(dates, signal_date)
        if bar_idx < 0:
            continue
        n_bar_found += 1

        # 1. Price
        price = float(df.iloc[bar_idx]["close"])
        if not np.isnan(price):
            sig["feat_price"] = price
            feature_counts["price"] += 1

        # 2. ADR (carry from signal if available, otherwise skip)
        adr = sig.get("adr_at_signal")
        if adr is not None and not np.isnan(adr):
            sig["feat_adr"] = float(adr)
            feature_counts["adr"] += 1

        # 3. Dollar volume (20-day avg)
        dv = compute_dollar_volume_20d(df, bar_idx)
        if dv is not None:
            sig["feat_dollar_volume_20d"] = dv
            feature_counts["dollar_volume_20d"] += 1

        # 4. RS vs SPY (multiple windows)
        spy_idx = spy_date_to_idx.get(signal_date, -1)
        if spy_idx >= 0:
            for w in RS_WINDOWS:
                rs = compute_rs_vs_spy(df, bar_idx, spy_df, spy_idx, w)
                if rs is not None:
                    sig[f"feat_rs_vs_spy_{w}d"] = rs
                    feature_counts[f"rs_vs_spy_{w}d"] += 1

        # 5. Days since IPO
        sig["feat_days_since_ipo"] = compute_days_since_ipo(df, bar_idx)
        feature_counts["days_since_ipo"] += 1

    # Compute summary stats for each feature
    feature_stats = {}
    feature_keys = ["feat_price", "feat_adr", "feat_dollar_volume_20d", "feat_days_since_ipo"]
    feature_keys += [f"feat_rs_vs_spy_{w}d" for w in RS_WINDOWS]

    for fk in feature_keys:
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

    stats = {
        "n_total": n_total,
        "n_ohlcv_found": n_ohlcv_found,
        "n_bar_found": n_bar_found,
        "feature_counts": feature_counts,
        "feature_stats": feature_stats,
    }
    return stats


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(setup_type, refinement_path, dry_run=False):
    print("\n" + "=" * 70)
    print("  SETUP GRINDER — Feature Extraction")
    print("=" * 70)
    print(f"  Setup:      {setup_type}")
    print(f"  Source:     {os.path.basename(refinement_path)}")

    # ── 1. Load signals ──────────────────────────────────────
    print(f"\n  Loading signals (pre-refinement)...")
    pre_signals = load_signals_from_refinement(refinement_path, mode="pre")
    pre_wins = sum(1 for s in pre_signals if str(s.get("classification", "")).upper() in WIN_CLASSES)
    pre_losses = len(pre_signals) - pre_wins
    print(f"  PRE:  {len(pre_signals)} signals  |  {pre_wins} wins  {pre_losses} losses")

    print(f"\n  Loading signals (post-refinement)...")
    post_signals = load_signals_from_refinement(refinement_path, mode="post")
    post_wins = sum(1 for s in post_signals if str(s.get("classification", "")).upper() in WIN_CLASSES)
    post_losses = len(post_signals) - post_wins
    print(f"  POST: {len(post_signals)} signals  |  {post_wins} wins  {post_losses} losses")

    # ── 2. Load OHLCV ────────────────────────────────────────
    print(f"\n  Loading 5yr OHLCV cache...")
    ohlcv_cache = load_5yr_ohlcv()
    print(f"  {len(ohlcv_cache)} tickers loaded")

    # Extract SPY
    spy_df = ohlcv_cache.get("SPY")
    if spy_df is None:
        raise ValueError("SPY not found in OHLCV cache — needed for RS computation")
    print(f"  SPY: {len(spy_df)} bars ({str(spy_df['date'].iloc[0])[:10]} to {str(spy_df['date'].iloc[-1])[:10]})")

    # ── 3. Compute features (pre-refinement) ─────────────────
    print(f"\n  Computing features (pre-refinement, {len(pre_signals)} signals)...")
    pre_stats = compute_features_for_signals(pre_signals, ohlcv_cache, spy_df)

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

    # ── 4. Compute features (post-refinement) ────────────────
    print(f"\n  Computing features (post-refinement, {len(post_signals)} signals)...")
    post_stats = compute_features_for_signals(post_signals, ohlcv_cache, spy_df)

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

    # ── 5. Win rate by quartile (quick sanity check) ─────────
    print(f"\n  {'='*60}")
    print(f"  QUICK WIN RATE BY QUARTILE (pre-refinement)")
    print(f"  {'='*60}")

    feature_keys = ["feat_price", "feat_adr", "feat_dollar_volume_20d", "feat_days_since_ipo"]
    feature_keys += [f"feat_rs_vs_spy_{w}d" for w in RS_WINDOWS]

    for fk in feature_keys:
        vals = [(s[fk], 1 if str(s.get("classification", "")).upper() in WIN_CLASSES else 0)
                for s in pre_signals if s[fk] is not None]
        if len(vals) < 20:
            continue

        arr = np.array([v[0] for v in vals])
        wins = np.array([v[1] for v in vals])

        q25, q50, q75 = np.percentile(arr, [25, 50, 75])
        label = fk.replace("feat_", "")

        bands = {
            "Q1": arr <= q25,
            "Q2": (arr > q25) & (arr <= q50),
            "Q3": (arr > q50) & (arr <= q75),
            "Q4": arr > q75,
        }

        parts = []
        for band_name, mask in bands.items():
            n = int(mask.sum())
            wr = float(np.mean(wins[mask])) if n > 0 else 0
            parts.append(f"{band_name}={wr:.1%}({n})")

        spread = None
        n_q1 = int((arr <= q25).sum())
        n_q4 = int((arr > q75).sum())
        if n_q1 > 0 and n_q4 > 0:
            wr_q1 = float(np.mean(wins[arr <= q25]))
            wr_q4 = float(np.mean(wins[arr > q75]))
            spread = wr_q4 - wr_q1

        spread_str = f"  spread={spread:+.1%}" if spread is not None else ""
        print(f"\n  {label}:")
        print(f"    {' | '.join(parts)}{spread_str}")

    print(f"\n  ✓ Feature extraction complete.")
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
