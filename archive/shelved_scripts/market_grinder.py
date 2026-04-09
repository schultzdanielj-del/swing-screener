"""
Market Grinder — Win Rate Time Series Correlation Engine.

Answers: which market conditions correlate with high win rate over time?

Method:
  1. Build win rate time series — for each trading day in the 5yr window,
     compute win rate across all signals in a ±5 trading day rolling window.
     Each day is weighted by signal count in its window (density weighting).

  2. For each (instrument, expression): extract that instrument's expression
     value on each day in the win rate series. This produces a feature time
     series aligned to the same dates.

  3. Compute weighted Pearson correlation between each feature time series
     and the win rate series. Weight = signal count in that day's window.

  4. Rank all features by |correlation|. Keep top N.

  5. For each top feature: bucket into quartiles, compute win rate per quartile
     (what win rate do we see when this indicator is in Q1/Q2/Q3/Q4?).

  6. Compute composite regime score per signal date (0-1) — weighted dot
     product of top features. Used by watchlist to rank incoming signals.

All data is local. Reads refinement grind output from local_runner/cache/.
Saves results to local_runner/cache/ and mirrors to Railway via file_mirror.

Usage:
    python scripts/market_grinder.py --setup dtss
    python scripts/market_grinder.py --setup dtss --mode post
    python scripts/market_grinder.py --refinement local_runner/cache/refinement_dtss_cl102_pk5_20260312_150704.json
    python scripts/market_grinder.py --setup dtss --window 5 --top-n 50 --dry-run
"""

import os
import sys
import json
import argparse
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed

warnings.filterwarnings("ignore")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
MKT_DIR   = os.path.join(CACHE_DIR, "market_series")
MANIFEST  = os.path.join(MKT_DIR, "_manifest.json")

# Defaults
DEFAULT_WINDOW    = 5    # ±N trading days for rolling win rate
DEFAULT_TOP_N     = 50   # top features to keep in model
MIN_WEIGHT        = 1    # min signal weight to include a day (always 1 — keep everything)
MIN_COVERAGE_FRAC = 0.20 # feature must have valid values on ≥20% of win rate series days


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
        """Extract YYYYMMDD_HHMMSS from end of filename."""
        name = os.path.basename(path).replace(".json", "")
        parts = name.split("_")
        if len(parts) >= 2:
            try:
                ts_str = parts[-2] + "_" + parts[-1]  # e.g. "20260312_150704"
                return datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            except ValueError:
                pass
        return datetime.min  # fallback: sort to beginning

    matches.sort(key=extract_timestamp)
    return matches[-1]


def load_signals_from_refinement(refinement_path, mode="pre"):
    """Load signals from a local refinement grind JSON file.

    Args:
        refinement_path: Path to refinement_*.json in local_runner/cache/
        mode: "pre" = all clusters (winners + losers + eliminated)
              "post" = surviving only (winners + losers, no eliminated)

    Returns:
        DataFrame with columns: signal_date, classification, ticker
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

    df = pd.DataFrame(all_signals)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    return df


def load_market_manifest():
    if not os.path.exists(MANIFEST):
        raise FileNotFoundError(
            f"Market cache not found at {MKT_DIR}\n"
            "Run: python local_runner/market_cache_builder.py --build"
        )
    with open(MANIFEST) as f:
        return json.load(f)


def load_instrument_cache(instrument_id):
    from local_runner.market_cache_builder import instrument_filename
    path = os.path.join(MKT_DIR, instrument_filename(instrument_id))
    if not os.path.exists(path):
        return None, None
    with np.load(path, allow_pickle=True) as f:
        return f["dates"], f["data"]


# ══════════════════════════════════════════════════════════════
# STEP 1 — WIN RATE TIME SERIES
# ══════════════════════════════════════════════════════════════

WIN_CLASSES = {"AUTO_WIN", "AI_WIN", "MANUAL_WIN"}


def build_win_rate_series(signals_df, window=DEFAULT_WINDOW):
    """
    Build a daily win rate time series over the full signal date range.

    For each trading day T that has at least 1 signal within ±window days:
      - win_rate[T]  = wins_in_window / total_in_window
      - weight[T]    = total signals in window  (density weighting)

    Returns DataFrame with columns:
        date, win_rate, weight, n_wins, n_signals
    Indexed by trading days that have sufficient signal coverage.
    """
    # Classify each signal as win (1) or loss (0)
    signals_df = signals_df.copy()
    signals_df["is_win"] = signals_df["classification"].apply(
        lambda c: 1 if str(c).upper() in WIN_CLASSES else 0
    )
    signals_df = signals_df.sort_values("signal_date").reset_index(drop=True)

    # Get all unique signal dates — these are the candidate days
    signal_dates = signals_df["signal_date"].values  # numpy datetime64 array
    is_win_arr   = signals_df["is_win"].values

    # Build all trading days in the range
    date_min = signals_df["signal_date"].min()
    date_max = signals_df["signal_date"].max()
    all_trading_days = pd.bdate_range(start=date_min, end=date_max)

    rows = []
    for day in all_trading_days:
        day_np = np.datetime64(day, "ns")

        # Window: ±window trading days around this day
        window_start = day - pd.tseries.offsets.BDay(window)
        window_end   = day + pd.tseries.offsets.BDay(window)

        mask = (
            (signals_df["signal_date"] >= window_start) &
            (signals_df["signal_date"] <= window_end)
        )
        n_signals = int(mask.sum())
        if n_signals == 0:
            continue

        n_wins   = int(is_win_arr[mask.values].sum())
        win_rate = n_wins / n_signals

        rows.append({
            "date":      day,
            "win_rate":  win_rate,
            "weight":    n_signals,
            "n_wins":    n_wins,
            "n_signals": n_signals,
        })

    wr_df = pd.DataFrame(rows)
    wr_df["date"] = pd.to_datetime(wr_df["date"])
    return wr_df


# ══════════════════════════════════════════════════════════════
# STEP 2 — FEATURE ALIGNMENT
# ══════════════════════════════════════════════════════════════

def align_feature_to_win_rate(dates_cache, data_cache, wr_dates_str, expr_idx):
    """
    For one (instrument, expression): extract values on each win rate date.
    Returns np.array of length len(wr_dates_str), NaN where date not in cache.
    """
    date_to_idx = {d: i for i, d in enumerate(dates_cache)}
    n = len(wr_dates_str)
    col = np.full(n, np.nan, dtype=np.float32)
    for i, d in enumerate(wr_dates_str):
        idx = date_to_idx.get(d)
        if idx is not None:
            col[i] = data_cache[idx, expr_idx]
    return col


# ══════════════════════════════════════════════════════════════
# STEP 3 — WEIGHTED PEARSON CORRELATION
# ══════════════════════════════════════════════════════════════

def weighted_pearson(x, y, w):
    """
    Weighted Pearson correlation between x and y with weights w.
    All arrays length N. NaN values in x are excluded (y and w too).
    Returns (r, n_valid) or (nan, 0) if insufficient data.
    """
    valid = ~np.isnan(x) & ~np.isnan(y) & (w > 0)
    n = int(valid.sum())
    if n < 10:
        return np.nan, n

    xv = x[valid].astype(np.float64)
    yv = y[valid].astype(np.float64)
    wv = w[valid].astype(np.float64)
    wv = wv / wv.sum()  # normalize weights to sum to 1

    x_mean = np.dot(wv, xv)
    y_mean = np.dot(wv, yv)

    dx = xv - x_mean
    dy = yv - y_mean

    cov  = np.dot(wv, dx * dy)
    var_x = np.dot(wv, dx ** 2)
    var_y = np.dot(wv, dy ** 2)

    if var_x <= 0 or var_y <= 0:
        return np.nan, n

    r = cov / np.sqrt(var_x * var_y)
    return float(np.clip(r, -1.0, 1.0)), n


# ══════════════════════════════════════════════════════════════
# STEP 3 — COMPUTE ALL CORRELATIONS
# ══════════════════════════════════════════════════════════════


def _correlate_instrument(args):
    """
    Worker: compute weighted Pearson correlations for one instrument.
    Runs in a subprocess. Returns list of result dicts (may be empty).
    """
    inst_id, wr_dates_str, y, w, n_days, min_coverage_days, mkt_dir, expr_names = args

    import os, sys, numpy as np
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from local_runner.market_cache_builder import instrument_filename

    path = os.path.join(mkt_dir, instrument_filename(inst_id))
    if not os.path.exists(path):
        return []

    with np.load(path, allow_pickle=True) as f:
        dates_cache = f["dates"]
        data_cache  = f["data"]

    date_to_idx = {d: idx for idx, d in enumerate(dates_cache)}
    row_indices = np.array([date_to_idx.get(d, -1) for d in wr_dates_str])
    valid_days  = row_indices >= 0

    if valid_days.sum() < 10:
        return []

    inst_label = (inst_id.replace("^", "").replace("=F", "_F")
                  .replace(":", "_").replace("$", "").replace("-", "_"))

    valid_rows = row_indices[valid_days]
    results = []

    for j in range(data_cache.shape[1]):
        x = np.full(n_days, np.nan, dtype=np.float64)
        x[valid_days] = data_cache[valid_rows, j]

        x_valid = x[~np.isnan(x)]
        if len(x_valid) < min_coverage_days or np.std(x_valid) == 0:
            continue

        # Inline weighted Pearson
        valid = ~np.isnan(x) & ~np.isnan(y) & (w > 0)
        n_valid = int(valid.sum())
        if n_valid < 10:
            continue
        xv = x[valid].astype(np.float64)
        yv = y[valid].astype(np.float64)
        wv = w[valid].astype(np.float64)
        wv = wv / wv.sum()
        x_mean = np.dot(wv, xv)
        y_mean = np.dot(wv, yv)
        dx = xv - x_mean
        dy = yv - y_mean
        cov   = np.dot(wv, dx * dy)
        var_x = np.dot(wv, dx ** 2)
        var_y = np.dot(wv, dy ** 2)
        if var_x <= 0 or var_y <= 0:
            continue
        r = float(np.clip(cov / np.sqrt(var_x * var_y), -1.0, 1.0))

        results.append({
            "instrument":      inst_id,
            "expr_name":       expr_names[j],
            "feature_name":    f"{inst_label}__{expr_names[j]}",
            "correlation":     r,
            "abs_correlation": abs(r),
            "n_valid":         n_valid,
        })

    return results


def compute_all_correlations(wr_df, manifest):
    """
    For every (instrument, expression) pair, compute weighted Pearson
    correlation with the win rate series.  Parallelized per instrument.

    Returns DataFrame: [instrument, expr_name, feature_name, correlation,
                        abs_correlation, n_valid]
    sorted by abs_correlation descending.
    """
    import time
    instruments = manifest["instruments"]
    n_exprs     = len(manifest["expr_names"])

    wr_dates_str = wr_df["date"].dt.strftime("%Y-%m-%d").values
    y  = wr_df["win_rate"].values.astype(np.float64)
    w  = wr_df["weight"].values.astype(np.float64)
    n_days = len(wr_dates_str)
    min_coverage_days = max(10, int(n_days * MIN_COVERAGE_FRAC))

    total_instruments = len(instruments)
    n_workers = os.cpu_count() or 4

    print(f"\n  Win rate series: {n_days} days  "
          f"(mean win rate: {np.average(y, weights=w):.3f}  "
          f"mean weight: {w.mean():.1f})")
    print(f"\n  Computing correlations: {total_instruments} instruments x "
          f"{n_exprs:,} expressions  ({n_workers} workers)...")
    print(f"  Min coverage: {min_coverage_days} days "
          f"({MIN_COVERAGE_FRAC*100:.0f}% of {n_days} series days)")

    args_list = [
        (inst_id, wr_dates_str, y, w, n_days, min_coverage_days, MKT_DIR, manifest["expr_names"])
        for inst_id in instruments
    ]

    all_results = []
    t0 = time.time()
    completed = 0

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_correlate_instrument, a): a[0] for a in args_list}
        for future in as_completed(futures):
            all_results.extend(future.result())
            completed += 1
            if completed % 25 == 0 or completed == total_instruments:
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 1
                eta  = (total_instruments - completed) / rate if rate > 0 else 0
                print(f"    {completed}/{total_instruments} instruments  "
                      f"[{elapsed:.0f}s, ~{eta:.0f}s left]  "
                      f"{len(all_results):,} features so far")

    elapsed = time.time() - t0
    print(f"\n  Done. {len(all_results):,} valid features in {elapsed:.1f}s")

    if not all_results:
        print("  WARNING: No valid features found. Check market cache date alignment.")
        return pd.DataFrame(columns=["instrument","expr_name","feature_name","correlation","abs_correlation","n_valid"])

    df = pd.DataFrame(all_results).sort_values("abs_correlation", ascending=False)
    return df.reset_index(drop=True)

# ══════════════════════════════════════════════════════════════
# STEP 3b — DEDUPLICATION
# ══════════════════════════════════════════════════════════════

def deduplicate_features(corr_df, manifest, wr_df, top_n, max_inter_corr=0.95):
    """
    Greedy deduplication: iterate candidates ranked by |corr|, select a
    feature only if its time series correlates < max_inter_corr with all
    already-selected features.  Stops when top_n independent features found
    or candidates exhausted.

    Returns a filtered DataFrame of up to top_n rows.
    """
    wr_dates_str = wr_df["date"].dt.strftime("%Y-%m-%d").values
    n_days = len(wr_dates_str)

    # Cache: feature_name → aligned time series (length n_days, NaN where missing)
    series_cache = {}

    def get_series(row):
        fname = row["feature_name"]
        if fname in series_cache:
            return series_cache[fname]
        dates_cache, data_cache = load_instrument_cache(row["instrument"])
        if dates_cache is None:
            series_cache[fname] = None
            return None
        date_to_idx = {d: idx for idx, d in enumerate(dates_cache)}
        row_indices = np.array([date_to_idx.get(d, -1) for d in wr_dates_str])
        valid_days = row_indices >= 0
        expr_idx = manifest["expr_names"].index(row["expr_name"])
        x = np.full(n_days, np.nan, dtype=np.float64)
        x[valid_days] = data_cache[row_indices[valid_days], expr_idx]
        series_cache[fname] = x
        return x

    selected_rows = []
    selected_series = []

    print(f"\n  Deduplicating features (max inter-corr: {max_inter_corr})...")
    n_candidates_checked = 0

    for _, row in corr_df.iterrows():
        if len(selected_rows) >= top_n:
            break

        x = get_series(row)
        if x is None:
            continue

        n_candidates_checked += 1
        valid_x = ~np.isnan(x)

        # Check against all already-selected series
        is_dup = False
        for sel_x in selected_series:
            valid_both = valid_x & ~np.isnan(sel_x)
            if valid_both.sum() < 10:
                continue
            r = np.corrcoef(x[valid_both], sel_x[valid_both])[0, 1]
            if abs(r) >= max_inter_corr:
                is_dup = True
                break

        if not is_dup:
            selected_rows.append(row)
            selected_series.append(x)

    print(f"  Checked {n_candidates_checked} candidates → "
          f"selected {len(selected_rows)} independent features")

    return pd.DataFrame(selected_rows).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
# STEP 4 — QUARTILE WIN RATES
# ══════════════════════════════════════════════════════════════

def compute_quartile_win_rates(wr_df, manifest, top_features_df):
    """
    For each top feature, bucket win rate days into quartiles by feature value.
    Computes weighted win rate per quartile.

    Returns dict: {feature_name: {q1: wr, q2: wr, q3: wr, q4: wr, ...}}
    """
    wr_dates_str = wr_df["date"].dt.strftime("%Y-%m-%d").values
    y = wr_df["win_rate"].values.astype(np.float64)
    w = wr_df["weight"].values.astype(np.float64)

    # Build instrument → (dates, data) cache — load each instrument once
    inst_cache = {}

    quartile_stats = {}

    for _, row in top_features_df.iterrows():
        inst_id   = row["instrument"]
        expr_name = row["expr_name"]
        fname     = row["feature_name"]

        if inst_id not in inst_cache:
            dates_c, data_c = load_instrument_cache(inst_id)
            inst_cache[inst_id] = (dates_c, data_c)
        dates_c, data_c = inst_cache[inst_id]
        if dates_c is None:
            continue

        # Get expression column index from manifest
        try:
            j = manifest["expr_names"].index(expr_name)
        except ValueError:
            continue

        date_to_idx = {d: idx for idx, d in enumerate(dates_c)}
        n_days = len(wr_dates_str)
        x = np.full(n_days, np.nan, dtype=np.float64)
        for k, d in enumerate(wr_dates_str):
            idx = date_to_idx.get(d)
            if idx is not None:
                x[k] = data_c[idx, j]

        valid = ~np.isnan(x)
        if valid.sum() < 10:
            continue

        xv = x[valid]
        yv = y[valid]
        wv = w[valid]

        q25, q50, q75 = np.percentile(xv, [25, 50, 75])

        bands = {
            "q1": xv <= q25,
            "q2": (xv > q25) & (xv <= q50),
            "q3": (xv > q50) & (xv <= q75),
            "q4": xv > q75,
        }

        stats = {"q25": float(q25), "q50": float(q50), "q75": float(q75)}
        for band, mask in bands.items():
            n = int(mask.sum())
            if n > 0:
                # Weighted win rate within band
                wt = wv[mask]
                wr = float(np.average(yv[mask], weights=wt))
                stats[f"wr_{band}"] = wr
                stats[f"n_{band}"]  = n
                stats[f"w_{band}"]  = float(wt.sum())
            else:
                stats[f"wr_{band}"] = None
                stats[f"n_{band}"]  = 0
                stats[f"w_{band}"]  = 0.0

        quartile_stats[fname] = stats

    return quartile_stats


# ══════════════════════════════════════════════════════════════
# STEP 5 — REGIME SCORES PER SIGNAL DATE
# ══════════════════════════════════════════════════════════════

def compute_regime_scores(signals_df, manifest, top_features_df):
    """
    For each signal, compute a composite regime score (0-1).

    Method:
      1. For each top feature, z-score its value on the signal date
      2. Weighted dot product: score = sum(z_i × corr_i) / sum(|corr_i|)
         (positive corr → high value = better; negative corr → high value = worse)
      3. Normalize all scores to 0-1 across the signal population

    Returns np.array of length len(signals_df).
    """
    n_signals  = len(signals_df)
    n_features = len(top_features_df)
    dates_str  = signals_df["signal_date"].dt.strftime("%Y-%m-%d").values
    corrs      = top_features_df["correlation"].values

    # score_matrix: (n_signals, n_features) — z-scored feature values
    score_matrix = np.full((n_signals, n_features), np.nan, dtype=np.float64)

    # Load each instrument once, compute z-score across the full instrument history
    inst_cache = {}

    for k, (_, row) in enumerate(top_features_df.iterrows()):
        inst_id   = row["instrument"]
        expr_name = row["expr_name"]

        if inst_id not in inst_cache:
            dates_c, data_c = load_instrument_cache(inst_id)
            inst_cache[inst_id] = (dates_c, data_c)
        dates_c, data_c = inst_cache[inst_id]
        if dates_c is None:
            continue

        try:
            j = manifest["expr_names"].index(expr_name)
        except ValueError:
            continue

        # Full series for z-score normalization
        full_col = data_c[:, j].astype(np.float64)
        valid    = ~np.isnan(full_col)
        if valid.sum() < 2:
            continue
        mu  = np.nanmean(full_col)
        std = np.nanstd(full_col)
        if std == 0:
            continue

        date_to_idx = {d: idx for idx, d in enumerate(dates_c)}
        for i, d in enumerate(dates_str):
            idx = date_to_idx.get(d)
            if idx is not None:
                val = data_c[idx, j]
                if not np.isnan(val):
                    score_matrix[i, k] = (val - mu) / std

    # Weighted dot product per signal
    raw_scores = np.full(n_signals, np.nan, dtype=np.float64)
    weight_sum = np.sum(np.abs(corrs))

    for i in range(n_signals):
        row    = score_matrix[i]
        valid  = ~np.isnan(row)
        if valid.sum() < 5:  # need at least 5 features to score
            continue
        raw_scores[i] = np.dot(row[valid], corrs[valid]) / np.sum(np.abs(corrs[valid]))

    # Normalize to 0-1
    valid_mask = ~np.isnan(raw_scores)
    if valid_mask.sum() < 2:
        return raw_scores

    mn  = np.nanmin(raw_scores)
    mx  = np.nanmax(raw_scores)
    rng = mx - mn
    if rng > 0:
        raw_scores[valid_mask] = (raw_scores[valid_mask] - mn) / rng

    return raw_scores


def compute_expected_win_rates(regime_scores, signals_df, n_buckets=10):
    """
    Bucket signals by regime score into deciles.
    Compute actual win rate per bucket.
    Returns dict of decile stats.
    """
    is_win = signals_df["classification"].apply(
        lambda c: 1 if str(c).upper() in WIN_CLASSES else 0
    ).values

    valid = ~np.isnan(regime_scores)
    scores = regime_scores[valid]
    wins   = is_win[valid]

    if len(scores) < n_buckets:
        return {}

    thresholds = np.percentile(scores, np.linspace(0, 100, n_buckets + 1))
    buckets = {}

    for b in range(n_buckets):
        lo = thresholds[b]
        hi = thresholds[b + 1]
        mask = (scores >= lo) & (scores <= hi) if b == n_buckets - 1 else (scores >= lo) & (scores < hi)
        n  = int(mask.sum())
        wr = float(np.mean(wins[mask])) if n > 0 else None
        buckets[f"d{b+1}"] = {
            "score_min": float(lo),
            "score_max": float(hi),
            "win_rate":  wr,
            "n":         n,
        }

    return buckets


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(setup_type, refinement_path, window=DEFAULT_WINDOW, top_n=DEFAULT_TOP_N, dry_run=False):
    print("\n" + "=" * 70)
    print("  MARKET GRINDER")
    print("=" * 70)
    print(f"  Setup:      {setup_type}")
    print(f"  Source:     {os.path.basename(refinement_path)}")
    print(f"  Window:     ±{window} trading days")
    print(f"  Top N:      {top_n} features")

    # ── 1. Load both signal sets ─────────────────────────────
    print(f"\n  Loading signals (pre-refinement)...")
    pre_df = load_signals_from_refinement(refinement_path, mode="pre")
    pre_wins   = int((pre_df["classification"].apply(
        lambda c: str(c).upper() in WIN_CLASSES)).sum())
    pre_losses = len(pre_df) - pre_wins
    print(f"  PRE:  {len(pre_df)} signals  |  {pre_wins} wins  {pre_losses} losses  "
          f"|  baseline WR: {pre_wins/len(pre_df):.3f}")

    print(f"\n  Loading signals (post-refinement)...")
    post_df = load_signals_from_refinement(refinement_path, mode="post")
    post_wins   = int((post_df["classification"].apply(
        lambda c: str(c).upper() in WIN_CLASSES)).sum())
    post_losses = len(post_df) - post_wins
    print(f"  POST: {len(post_df)} signals  |  {post_wins} wins  {post_losses} losses  "
          f"|  baseline WR: {post_wins/len(post_df):.3f}")

    if pre_wins < 10 or pre_losses < 10:
        raise ValueError(f"Insufficient pre-refinement signals: {pre_wins} wins, {pre_losses} losses")

    # ── 2. Build win rate time series (pre-refinement) ───────
    print(f"\n  Building win rate time series (±{window} trading days, pre-refinement)...")
    pre_wr_df = build_win_rate_series(pre_df, window=window)
    print(f"  {len(pre_wr_df)} days in series")
    print(f"  Win rate range: {pre_wr_df['win_rate'].min():.3f} – {pre_wr_df['win_rate'].max():.3f}")
    print(f"  Mean weighted win rate: "
          f"{np.average(pre_wr_df['win_rate'], weights=pre_wr_df['weight']):.3f}")

    # ── 3. Load market manifest ──────────────────────────────
    manifest = load_market_manifest()
    print(f"\n  Market cache: {manifest['n_instruments']} instruments  "
          f"{manifest['n_expressions']:,} expressions  "
          f"built {manifest.get('built_at','?')[:10]}")

    # ── 4. Compute correlations (pre-refinement) ─────────────
    corr_df = compute_all_correlations(pre_wr_df, manifest)

    if corr_df.empty:
        raise ValueError("No correlations computed — check market cache coverage of signal dates")

    print(f"\n  Top 20 features by |weighted correlation|:")
    print(f"  {'Feature':<65} {'Corr':>8}  {'N':>5}")
    print(f"  {'-'*82}")
    for _, row in corr_df.head(20).iterrows():
        sign = "+" if row["correlation"] > 0 else ""
        print(f"  {row['feature_name']:<65} "
              f"{sign}{row['correlation']:>7.4f}  {row['n_valid']:>5}")

    # ── 5. Select top N (deduplicated) ───────────────────────
    top_df = deduplicate_features(corr_df, manifest, pre_wr_df, top_n)

    # ── 6. Quartile win rates (pre-refinement) ──────────────
    print(f"\n  Computing quartile win rates (pre-refinement)...")
    pre_quartiles = compute_quartile_win_rates(pre_wr_df, manifest, top_df)

    # ── 7. Regime scores + deciles (pre-refinement) ──────────
    print(f"\n  Scoring {len(pre_df)} signals (pre-refinement)...")
    pre_scores = compute_regime_scores(pre_df, manifest, top_df)
    pre_n_scored = int(np.sum(~np.isnan(pre_scores)))
    print(f"  Scored: {pre_n_scored}/{len(pre_df)}")
    print(f"  Score range: {np.nanmin(pre_scores):.3f} – {np.nanmax(pre_scores):.3f}")

    pre_deciles = compute_expected_win_rates(pre_scores, pre_df)
    if pre_deciles:
        print(f"\n  PRE-REFINEMENT win rate by regime score decile:")
        print(f"  {'Decile':<8} {'Score range':<20} {'Win rate':>10}  {'N':>5}")
        for label, stats in pre_deciles.items():
            wr_str = f"{stats['win_rate']:.3f}" if stats["win_rate"] is not None else "  N/A"
            print(f"  {label:<8} "
                  f"{stats['score_min']:.3f}–{stats['score_max']:.3f}   "
                  f"{wr_str:>10}  {stats['n']:>5}")

    # ── 8. Post-refinement pass (same features) ──────────────
    print(f"\n  {'='*60}")
    print(f"  POST-REFINEMENT COMPARISON")
    print(f"  {'='*60}")

    print(f"\n  Building win rate time series (post-refinement)...")
    post_wr_df = build_win_rate_series(post_df, window=window)
    print(f"  {len(post_wr_df)} days in series")
    print(f"  Mean weighted win rate: "
          f"{np.average(post_wr_df['win_rate'], weights=post_wr_df['weight']):.3f}")

    print(f"\n  Computing quartile win rates (post-refinement, same {len(top_df)} features)...")
    post_quartiles = compute_quartile_win_rates(post_wr_df, manifest, top_df)

    print(f"\n  Scoring {len(post_df)} signals (post-refinement)...")
    post_scores = compute_regime_scores(post_df, manifest, top_df)
    post_n_scored = int(np.sum(~np.isnan(post_scores)))
    print(f"  Scored: {post_n_scored}/{len(post_df)}")

    post_deciles = compute_expected_win_rates(post_scores, post_df)
    if post_deciles:
        print(f"\n  POST-REFINEMENT win rate by regime score decile:")
        print(f"  {'Decile':<8} {'Score range':<20} {'Win rate':>10}  {'N':>5}")
        for label, stats in post_deciles.items():
            wr_str = f"{stats['win_rate']:.3f}" if stats["win_rate"] is not None else "  N/A"
            print(f"  {label:<8} "
                  f"{stats['score_min']:.3f}–{stats['score_max']:.3f}   "
                  f"{wr_str:>10}  {stats['n']:>5}")

    # ── 9. Redundancy scores ─────────────────────────────────
    print(f"\n  {'='*60}")
    print(f"  REDUNDANCY ANALYSIS")
    print(f"  {'='*60}")

    redundancy = {}
    for fname in pre_quartiles:
        pre_q = pre_quartiles[fname]
        post_q = post_quartiles.get(fname)

        pre_spread = None
        post_spread = None

        if pre_q and pre_q.get("wr_q4") is not None and pre_q.get("wr_q1") is not None:
            pre_spread = pre_q["wr_q4"] - pre_q["wr_q1"]

        if post_q and post_q.get("wr_q4") is not None and post_q.get("wr_q1") is not None:
            post_spread = post_q["wr_q4"] - post_q["wr_q1"]

        ratio = None
        if pre_spread and abs(pre_spread) > 0.01 and post_spread is not None:
            ratio = post_spread / pre_spread

        redundancy[fname] = {
            "pre_spread":  round(pre_spread, 4) if pre_spread is not None else None,
            "post_spread": round(post_spread, 4) if post_spread is not None else None,
            "ratio":       round(ratio, 3) if ratio is not None else None,
        }

    # Print sorted by ratio descending (strongest regime signals first)
    scored_features = [(f, r) for f, r in redundancy.items() if r["ratio"] is not None]
    scored_features.sort(key=lambda x: x[1]["ratio"], reverse=True)

    print(f"\n  {'Feature':<55} {'Pre Q4-Q1':>10} {'Post Q4-Q1':>11} {'Ratio':>7}")
    print(f"  {'-'*87}")
    for fname, r in scored_features[:30]:
        print(f"  {fname:<55} {r['pre_spread']:>+10.3f} {r['post_spread']:>+11.3f} {r['ratio']:>7.2f}")

    n_genuine = sum(1 for _, r in scored_features if r["ratio"] and r["ratio"] >= 0.5)
    n_redundant = sum(1 for _, r in scored_features if r["ratio"] is not None and r["ratio"] < 0.5)
    print(f"\n  Genuine (ratio >= 0.5): {n_genuine}  |  Redundant (ratio < 0.5): {n_redundant}")

    # ── 10. Build result ─────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    feature_weights = {}
    for _, row in top_df.iterrows():
        fname = row["feature_name"]
        feature_weights[fname] = {
            "instrument":      row["instrument"],
            "expr_name":       row["expr_name"],
            "correlation":     row["correlation"],
            "abs_correlation": row["abs_correlation"],
            "n_valid":         int(row["n_valid"]),
            "pre_quartiles":   pre_quartiles.get(fname),
            "post_quartiles":  post_quartiles.get(fname),
            "redundancy":      redundancy.get(fname),
        }

    # Per-signal scores (pre-refinement — the full set)
    signal_scores = []
    for i, (_, sig_row) in enumerate(pre_df.iterrows()):
        score = float(pre_scores[i]) if not np.isnan(pre_scores[i]) else None

        expected_wr = None
        if score is not None and pre_deciles:
            for stats in pre_deciles.values():
                if stats["score_min"] <= score <= stats["score_max"]:
                    expected_wr = stats["win_rate"]
                    break

        signal_scores.append({
            "ticker":           sig_row.get("ticker"),
            "signal_date":      str(sig_row["signal_date"])[:10],
            "classification":   sig_row["classification"],
            "regime_score":     score,
            "expected_win_rate": expected_wr,
        })

    result_data = {
        "setup_type":          setup_type,
        "refinement_source":   os.path.basename(refinement_path),
        "timestamp":           now,
        "window":              window,
        "top_n":               top_n,
        "pre": {
            "n_signals":       int(len(pre_df)),
            "n_wins":          pre_wins,
            "n_losses":        pre_losses,
            "baseline_win_rate": float(pre_wins / len(pre_df)),
            "win_rate_by_decile": pre_deciles,
        },
        "post": {
            "n_signals":       int(len(post_df)),
            "n_wins":          post_wins,
            "n_losses":        post_losses,
            "baseline_win_rate": float(post_wins / len(post_df)),
            "win_rate_by_decile": post_deciles,
        },
        "n_features_tested":   int(len(corr_df)),
        "n_features_selected": int(len(top_df)),
        "feature_weights":     feature_weights,
        "top_features":        top_df["feature_name"].head(5).tolist(),
        "redundancy_summary": {
            "n_genuine":   n_genuine,
            "n_redundant": n_redundant,
        },
        "signal_scores":       signal_scores,
    }

    if dry_run:
        print(f"\n  DRY RUN — not saving.")
        d1 = pre_deciles.get("d1", {}).get("win_rate")
        d10 = pre_deciles.get("d10", {}).get("win_rate")
        if d1 is not None and d10 is not None:
            print(f"  Pre-refinement lift D10 vs D1: {d10:.3f} vs {d1:.3f} "
                  f"(+{(d10-d1)*100:.1f}pp)")
        return result_data

    # ── 11. Save locally + mirror to Railway ─────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"regime_{setup_type}_{ts}.json"
    out_path = os.path.join(CACHE_DIR, filename)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result_data, f, indent=2)
    print(f"\n  Saved: {out_path}")

    try:
        from file_mirror import mirror_file
        mirror_file(out_path)
        print(f"  Mirrored to Railway.")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  ✓ Market grinder complete.")
    if pre_deciles:
        d1  = pre_deciles.get("d1",  {}).get("win_rate")
        d10 = pre_deciles.get("d10", {}).get("win_rate")
        if d1 is not None and d10 is not None:
            print(f"  Pre-refinement:  D1 {d1:.3f} → D10 {d10:.3f} "
                  f"(+{(d10-d1)*100:.1f}pp)")
    if post_deciles:
        d1  = post_deciles.get("d1",  {}).get("win_rate")
        d10 = post_deciles.get("d10", {}).get("win_rate")
        if d1 is not None and d10 is not None:
            print(f"  Post-refinement: D1 {d1:.3f} → D10 {d10:.3f} "
                  f"(+{(d10-d1)*100:.1f}pp)")
    print(f"  Genuine features: {n_genuine}/{len(scored_features)}  "
          f"Redundant: {n_redundant}/{len(scored_features)}")
    print(f"  Top feature: {top_df.iloc[0]['feature_name']}")

    return result_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Grinder — win rate time series correlation")
    parser.add_argument("--setup",       default="dtss", help="Setup type (finds latest refinement file)")
    parser.add_argument("--refinement",  help="Path to specific refinement JSON file")
    parser.add_argument("--window",      type=int, default=DEFAULT_WINDOW,
                        help=f"Rolling window ±N trading days (default: {DEFAULT_WINDOW})")
    parser.add_argument("--top-n",       type=int, default=DEFAULT_TOP_N,
                        help=f"Top N features to keep (default: {DEFAULT_TOP_N})")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Compute but don't save results")
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

    run(setup_type, refinement_path,
        window=args.window, top_n=args.top_n, dry_run=args.dry_run)
