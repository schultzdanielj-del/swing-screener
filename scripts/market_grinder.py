"""
Market Grinder — Win Rate Time Series Correlation Engine.

Answers: which market conditions correlate with high DTSS win rate over time?

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

Works for any setup type. Only requires: signal_date + classification.
No dependency on exit dates, exit grinder, or setup-specific logic.

Usage:
    python scripts/market_grinder.py --setup dtss
    python scripts/market_grinder.py --cycle dtss_20260306_170830
    python scripts/market_grinder.py --setup dtss --dry-run
    python scripts/market_grinder.py --setup dtss --window 5 --top-n 50
"""

import os
import sys
import json
import argparse
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
MKT_DIR   = os.path.join(CACHE_DIR, "market_series")
MANIFEST  = os.path.join(MKT_DIR, "_manifest.json")

API_BASE  = os.environ.get("RAILWAY_API", "https://web-production-e3025.up.railway.app")

# Defaults
DEFAULT_WINDOW  = 5    # ±N trading days for rolling win rate
DEFAULT_TOP_N   = 50   # top features to keep in model
MIN_WEIGHT      = 1    # min signal weight to include a day (always 1 — keep everything)


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_signals(cycle_id):
    import urllib.request
    url = f"{API_BASE}/api/v2/cycles/{cycle_id}/signals"
    req = urllib.request.Request(url, headers={"User-Agent": "market-grinder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    signals = data.get("signals", [])
    if not signals:
        raise ValueError(f"No signals found for cycle {cycle_id}")
    df = pd.DataFrame(signals)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    return df


def get_current_cycle(setup_type):
    import urllib.request
    url = f"{API_BASE}/api/v2/cycles/{setup_type}"
    req = urllib.request.Request(url, headers={"User-Agent": "market-grinder/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    for c in data.get("cycles", []):
        if c.get("is_current"):
            return c["cycle_id"]
    raise ValueError(f"No current cycle for: {setup_type}")


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

def compute_all_correlations(wr_df, manifest):
    """
    For every (instrument, expression) pair, compute weighted Pearson
    correlation with the win rate series.

    Returns DataFrame: [instrument, expr_name, feature_name, correlation,
                        abs_correlation, n_valid]
    sorted by abs_correlation descending.
    """
    expr_names  = manifest["expr_names"]
    instruments = manifest["instruments"]
    n_exprs     = len(expr_names)

    wr_dates_str = wr_df["date"].dt.strftime("%Y-%m-%d").values
    y  = wr_df["win_rate"].values.astype(np.float64)
    w  = wr_df["weight"].values.astype(np.float64)
    n_days = len(wr_dates_str)

    total_instruments = len(instruments)
    print(f"\n  Win rate series: {n_days} days  "
          f"(mean win rate: {np.average(y, weights=w):.3f}  "
          f"mean weight: {w.mean():.1f})")
    print(f"\n  Computing correlations: {total_instruments} instruments × "
          f"{n_exprs:,} expressions...")

    results = []
    t0 = __import__("time").time()

    for i, (inst_id, inst_info) in enumerate(instruments.items()):
        dates_cache, data_cache = load_instrument_cache(inst_id)
        if dates_cache is None:
            continue

        # Build date→index map once per instrument
        date_to_idx = {d: idx for idx, d in enumerate(dates_cache)}

        # Get row indices for all win rate dates at once
        row_indices = np.array([date_to_idx.get(d, -1) for d in wr_dates_str])
        valid_days  = row_indices >= 0

        if valid_days.sum() < 10:
            continue  # not enough date overlap

        inst_label = (inst_id.replace("^", "").replace("=F", "_F")
                      .replace(":", "_").replace("$", "").replace("-", "_"))

        # Process all expressions for this instrument
        for j in range(n_exprs):
            # Extract feature values on win rate dates
            x = np.full(n_days, np.nan, dtype=np.float64)
            valid_rows = row_indices[valid_days]
            x[valid_days] = data_cache[valid_rows, j]

            # Skip if all NaN or zero variance
            x_valid = x[~np.isnan(x)]
            if len(x_valid) < 10 or np.std(x_valid) == 0:
                continue

            r, n_valid = weighted_pearson(x, y, w)
            if np.isnan(r):
                continue

            results.append({
                "instrument":      inst_id,
                "expr_name":       expr_names[j],
                "feature_name":    f"{inst_label}__{expr_names[j]}",
                "correlation":     r,
                "abs_correlation": abs(r),
                "n_valid":         n_valid,
            })

        if (i + 1) % 25 == 0 or (i + 1) == total_instruments:
            elapsed = __import__("time").time() - t0
            rate    = (i + 1) / elapsed if elapsed > 0 else 1
            eta     = (total_instruments - i - 1) / rate if rate > 0 else 0
            print(f"    {i+1}/{total_instruments} instruments  "
                  f"[{elapsed:.0f}s, ~{eta:.0f}s left]  "
                  f"{len(results):,} features so far")

    elapsed = __import__("time").time() - t0
    print(f"\n  Done. {len(results):,} valid features in {elapsed:.1f}s")

    df = pd.DataFrame(results).sort_values("abs_correlation", ascending=False)
    return df.reset_index(drop=True)


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
# UPLOAD
# ══════════════════════════════════════════════════════════════

def _post(endpoint, payload):
    import urllib.request
    url  = f"{API_BASE}{endpoint}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "market-grinder/1.0"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(cycle_id, setup_type, window=DEFAULT_WINDOW, top_n=DEFAULT_TOP_N, dry_run=False):
    print("\n" + "=" * 70)
    print("  MARKET GRINDER")
    print("=" * 70)
    print(f"  Cycle:      {cycle_id}")
    print(f"  Setup:      {setup_type}")
    print(f"  Window:     ±{window} trading days")
    print(f"  Top N:      {top_n} features")

    # ── 1. Load signals ──────────────────────────────────────
    print(f"\n  Loading signals...")
    signals_df = load_signals(cycle_id)
    n_wins   = int((signals_df["classification"].apply(
        lambda c: str(c).upper() in WIN_CLASSES)).sum())
    n_losses = len(signals_df) - n_wins
    print(f"  {len(signals_df)} signals  |  {n_wins} wins  {n_losses} losses  "
          f"|  baseline win rate: {n_wins/len(signals_df):.3f}")

    if n_wins < 10 or n_losses < 10:
        raise ValueError(f"Insufficient labeled signals: {n_wins} wins, {n_losses} losses")

    # ── 2. Build win rate time series ────────────────────────
    print(f"\n  Building win rate time series (±{window} trading days)...")
    wr_df = build_win_rate_series(signals_df, window=window)
    print(f"  {len(wr_df)} days in series")
    print(f"  Win rate range: {wr_df['win_rate'].min():.3f} – {wr_df['win_rate'].max():.3f}")
    print(f"  Weight range:   {wr_df['weight'].min()} – {wr_df['weight'].max()} signals/window")
    print(f"  Mean weighted win rate: "
          f"{np.average(wr_df['win_rate'], weights=wr_df['weight']):.3f}")

    # ── 3. Load market manifest ──────────────────────────────
    manifest = load_market_manifest()
    print(f"\n  Market cache: {manifest['n_instruments']} instruments  "
          f"{manifest['n_expressions']:,} expressions  "
          f"built {manifest.get('built_at','?')[:10]}")

    # ── 4. Compute correlations ──────────────────────────────
    corr_df = compute_all_correlations(wr_df, manifest)

    if corr_df.empty:
        raise ValueError("No correlations computed — check market cache coverage of signal dates")

    print(f"\n  Top 20 features by |weighted correlation|:")
    print(f"  {'Feature':<65} {'Corr':>8}  {'N':>5}")
    print(f"  {'-'*82}")
    for _, row in corr_df.head(20).iterrows():
        sign = "+" if row["correlation"] > 0 else ""
        print(f"  {row['feature_name']:<65} "
              f"{sign}{row['correlation']:>7.4f}  {row['n_valid']:>5}")

    # ── 5. Select top N ──────────────────────────────────────
    top_df = corr_df.head(top_n).copy().reset_index(drop=True)

    # ── 6. Quartile win rates ────────────────────────────────
    print(f"\n  Computing quartile win rates for top {top_n} features...")
    quartile_stats = compute_quartile_win_rates(wr_df, manifest, top_df)

    # ── 7. Regime scores per signal ──────────────────────────
    print(f"\n  Scoring {len(signals_df)} signals...")
    regime_scores = compute_regime_scores(signals_df, manifest, top_df)
    n_scored = int(np.sum(~np.isnan(regime_scores)))
    print(f"  Scored: {n_scored}/{len(signals_df)}")
    print(f"  Score range: {np.nanmin(regime_scores):.3f} – {np.nanmax(regime_scores):.3f}")
    print(f"  Score mean:  {np.nanmean(regime_scores):.3f}")

    # ── 8. Win rate by decile ────────────────────────────────
    wr_by_decile = compute_expected_win_rates(regime_scores, signals_df)
    if wr_by_decile:
        print(f"\n  Win rate by regime score decile (D1=worst, D10=best):")
        print(f"  {'Decile':<8} {'Score range':<20} {'Win rate':>10}  {'N':>5}")
        for label, stats in wr_by_decile.items():
            wr_str = f"{stats['win_rate']:.3f}" if stats["win_rate"] is not None else "  N/A"
            print(f"  {label:<8} "
                  f"{stats['score_min']:.3f}–{stats['score_max']:.3f}   "
                  f"{wr_str:>10}  {stats['n']:>5}")

    # ── 9. Build payloads ────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    feature_weights = {}
    for _, row in top_df.iterrows():
        fname = row["feature_name"]
        feature_weights[fname] = {
            "instrument":      row["instrument"],
            "expr_name":       row["expr_name"],
            "correlation":     row["correlation"],
            "abs_correlation": row["abs_correlation"],
            "n_valid":         row["n_valid"],
            "quartiles":       quartile_stats.get(fname),
        }

    regime_model_payload = {
        "setup_type":         setup_type,
        "cycle_id":           cycle_id,
        "n_signals_used":     int(len(signals_df)),
        "n_features_tested":  int(len(corr_df)),
        "feature_weights":    json.dumps(feature_weights),
        "top_features":       json.dumps(top_df["feature_name"].head(5).tolist()),
        "win_rate_by_decile": json.dumps(wr_by_decile),
        "baseline_win_rate":  float(n_wins / len(signals_df)),
        "win_rate_series_window": window,
        "updated_at":         now,
    }

    # Per-signal scores
    signal_score_rows = []
    for i, (_, sig_row) in enumerate(signals_df.iterrows()):
        score = float(regime_scores[i]) if not np.isnan(regime_scores[i]) else None

        # Map score to expected win rate
        expected_wr = None
        if score is not None and wr_by_decile:
            for stats in wr_by_decile.values():
                if stats["score_min"] <= score <= stats["score_max"]:
                    expected_wr = stats["win_rate"]
                    break

        signal_score_rows.append({
            "cycle_signal_id":   sig_row.get("id"),
            "cycle_id":          cycle_id,
            "regime_score":      score,
            "expected_win_rate": expected_wr,
        })

    signal_scores_payload = {
        "cycle_id": cycle_id,
        "scores":   signal_score_rows,
    }

    if dry_run:
        print(f"\n  DRY RUN — not uploading.")
        print(f"  Would upload regime model + {len(signal_score_rows)} signal scores.")
        d1 = wr_by_decile.get("d1", {}).get("win_rate")
        d10 = wr_by_decile.get("d10", {}).get("win_rate")
        if d1 is not None and d10 is not None:
            print(f"  Win rate lift D10 vs D1: {d10:.3f} vs {d1:.3f} "
                  f"(+{(d10-d1)*100:.1f}pp)")
        return

    # ── 10. Upload ───────────────────────────────────────────
    print(f"\n  Uploading regime model...")
    result = _post("/api/v2/regime/model", regime_model_payload)
    print(f"  {result}")

    print(f"  Uploading {len(signal_score_rows)} signal scores...")
    result = _post("/api/v2/regime/scores", signal_scores_payload)
    print(f"  {result}")

    print(f"\n  ✓ Market grinder complete.")
    if wr_by_decile:
        d1  = wr_by_decile.get("d1",  {}).get("win_rate")
        d10 = wr_by_decile.get("d10", {}).get("win_rate")
        if d1 is not None and d10 is not None:
            print(f"  Win rate: D1 (worst regime) {d1:.3f}  →  "
                  f"D10 (best regime) {d10:.3f}  "
                  f"(+{(d10-d1)*100:.1f}pp lift)")
    print(f"  Top feature: {top_df.iloc[0]['feature_name']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Grinder — win rate time series correlation")
    parser.add_argument("--cycle",   help="Specific cycle_id")
    parser.add_argument("--setup",   default="dtss", help="Setup type (uses current cycle)")
    parser.add_argument("--window",  type=int, default=DEFAULT_WINDOW,
                        help=f"Rolling window ±N trading days (default: {DEFAULT_WINDOW})")
    parser.add_argument("--top-n",   type=int, default=DEFAULT_TOP_N,
                        help=f"Top N features to keep (default: {DEFAULT_TOP_N})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute but don't upload to Railway")
    args = parser.parse_args()

    if args.cycle:
        cycle_id   = args.cycle
        setup_type = cycle_id.split("_")[0]
    else:
        setup_type = args.setup
        print(f"  Fetching current cycle for {setup_type}...")
        cycle_id = get_current_cycle(setup_type)
        print(f"  Current cycle: {cycle_id}")

    run(cycle_id, setup_type,
        window=args.window, top_n=args.top_n, dry_run=args.dry_run)
