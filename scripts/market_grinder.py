"""
Market Grinder — Correlate market conditions with DTSS win/loss outcomes.

For each signal in cycle_signals, looks up market instrument expression values
on that signal date, then computes point-biserial correlation between each
(instrument, expression) feature and the win/loss outcome.

Produces:
  - regime_model: top features ranked by correlation, per-quartile win rates
  - signal_regime_scores: composite regime score per signal
  - Updates cycle_signals.regime_score (denormalized)

Usage:
    python scripts/market_grinder.py --cycle dtss_20260306_170830
    python scripts/market_grinder.py --setup dtss          # uses current cycle

Output uploaded to Railway via API.
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

# How many top features to keep in the model
TOP_N_FEATURES = 50

# Minimum number of non-NaN values required to compute correlation for a feature
MIN_VALID = 30


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_signals(cycle_id):
    """Load cycle_signals from Railway API. Returns DataFrame."""
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


def load_market_manifest():
    if not os.path.exists(MANIFEST):
        raise FileNotFoundError(
            f"Market cache not found at {MKT_DIR}\n"
            "Run: python local_runner/market_cache_builder.py --build"
        )
    with open(MANIFEST) as f:
        return json.load(f)


def load_instrument_cache(instrument_id):
    """Load .npz for one instrument. Returns (dates_array, data_array) or (None, None)."""
    from local_runner.market_cache_builder import instrument_filename
    path = os.path.join(MKT_DIR, instrument_filename(instrument_id))
    if not os.path.exists(path):
        return None, None
    with np.load(path, allow_pickle=True) as f:
        return f["dates"], f["data"]


def get_current_cycle(setup_type):
    import urllib.request
    url = f"{API_BASE}/api/v2/cycles/{setup_type}"
    req = urllib.request.Request(url, headers={"User-Agent": "market-grinder/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    for c in data.get("cycles", []):
        if c.get("is_current"):
            return c["cycle_id"]
    raise ValueError(f"No current cycle for setup type: {setup_type}")


# ══════════════════════════════════════════════════════════════
# FEATURE MATRIX BUILDER
# ══════════════════════════════════════════════════════════════

def build_feature_matrix(signals_df, manifest):
    """
    For each signal, look up every (instrument, expression) value on signal_date.

    Returns:
        feature_matrix: np.ndarray (n_signals, n_features)
        feature_names:  list of str, e.g. "SPY__rsi14"
        signal_dates:   np.array of date strings
    """
    expr_names  = manifest["expr_names"]       # list of expression names in order
    instruments = manifest["instruments"]       # dict: inst_id -> info

    signal_dates_str = signals_df["signal_date"].dt.strftime("%Y-%m-%d").values
    n_signals = len(signals_df)

    # Build date → row-index map per instrument (built on demand)
    feature_cols   = []   # list of np.array (n_signals,) per feature
    feature_labels = []   # list of "INST__expr_name"

    total_instruments = len(instruments)
    print(f"\n  Building feature matrix: {n_signals} signals × {total_instruments} instruments")

    for i, (inst_id, inst_info) in enumerate(instruments.items()):
        if (i + 1) % 50 == 0 or (i + 1) == total_instruments:
            print(f"    {i+1}/{total_instruments} instruments processed...")

        dates, data = load_instrument_cache(inst_id)
        if dates is None:
            continue

        # Build date → index map for this instrument
        date_to_idx = {d: idx for idx, d in enumerate(dates)}

        # For each signal, look up the row index
        row_indices = np.array([date_to_idx.get(d, -1) for d in signal_dates_str])
        valid_mask  = row_indices >= 0

        if not np.any(valid_mask):
            continue  # no date overlap

        # Extract all expression columns for valid signals
        # data shape: (n_bars, n_exprs)
        n_exprs = data.shape[1]
        inst_prefix = _safe_inst_name(inst_id)

        for j in range(n_exprs):
            col = np.full(n_signals, np.nan, dtype=np.float32)
            valid_rows = row_indices[valid_mask]
            col[valid_mask] = data[valid_rows, j]

            # Skip if all NaN or no variance
            non_nan = col[~np.isnan(col)]
            if len(non_nan) < MIN_VALID:
                continue
            if np.std(non_nan) == 0:
                continue

            feature_cols.append(col)
            feature_labels.append(f"{inst_prefix}__{expr_names[j]}")

    if not feature_cols:
        raise ValueError("Feature matrix is empty — no instrument data overlaps with signal dates")

    feature_matrix = np.column_stack(feature_cols)
    print(f"\n  Feature matrix: {feature_matrix.shape[0]} signals × {feature_matrix.shape[1]} features")
    return feature_matrix, feature_labels, signal_dates_str


def _safe_inst_name(inst_id):
    """Convert instrument ID to a safe label for feature names."""
    return inst_id.replace("^", "").replace("=F", "_F").replace(
        ":", "_").replace("$", "").replace("-", "_").replace(".", "_")


# ══════════════════════════════════════════════════════════════
# CORRELATION ENGINE
# ══════════════════════════════════════════════════════════════

def compute_correlations(feature_matrix, y_binary, feature_labels):
    """
    Compute point-biserial correlation between each feature and win/loss outcome.

    Point-biserial is equivalent to Pearson for a binary Y variable.
    This is the right tool: interpretable, fast, no assumptions about feature distribution.

    Returns DataFrame: [feature_name, correlation, abs_correlation, n_valid]
    sorted by abs_correlation descending.
    """
    from scipy import stats as scipy_stats

    n_features = feature_matrix.shape[1]
    results = []

    print(f"\n  Computing correlations for {n_features:,} features...")
    t0 = __import__("time").time()

    for j in range(n_features):
        col = feature_matrix[:, j]
        valid = ~np.isnan(col)
        n_valid = int(np.sum(valid))

        if n_valid < MIN_VALID:
            continue

        x = col[valid].astype(np.float64)
        y = y_binary[valid].astype(np.float64)

        # Skip if y has no variance in valid subset (all wins or all losses)
        if np.std(y) == 0:
            continue

        try:
            r, p = scipy_stats.pearsonr(x, y)
            if np.isnan(r):
                continue
            results.append({
                "feature_name":    feature_labels[j],
                "correlation":     float(r),
                "abs_correlation": float(abs(r)),
                "n_valid":         n_valid,
                "p_value":         float(p),
            })
        except:
            continue

        if (j + 1) % 100000 == 0:
            elapsed = __import__("time").time() - t0
            print(f"    {j+1:,}/{n_features:,} ({elapsed:.0f}s)...")

    elapsed = __import__("time").time() - t0
    print(f"  Done. {len(results):,} valid features computed in {elapsed:.1f}s")

    df = pd.DataFrame(results).sort_values("abs_correlation", ascending=False)
    return df.reset_index(drop=True)


def compute_quartile_win_rates(feature_matrix, feature_labels, y_binary, top_feature_names):
    """
    For each top feature, bucket signals into quartiles and compute win rate per quartile.
    Returns dict: {feature_name: {q1: wr, q2: wr, q3: wr, q4: wr, n_q1: n, ...}}
    """
    label_to_idx = {name: j for j, name in enumerate(feature_labels)}
    quartile_stats = {}

    for fname in top_feature_names:
        j = label_to_idx.get(fname)
        if j is None:
            continue
        col = feature_matrix[:, j]
        valid = ~np.isnan(col)
        if np.sum(valid) < MIN_VALID:
            continue

        x = col[valid]
        y = y_binary[valid]

        q25, q50, q75 = np.percentile(x, [25, 50, 75])
        bands = {
            "q1": x <= q25,
            "q2": (x > q25) & (x <= q50),
            "q3": (x > q50) & (x <= q75),
            "q4": x > q75,
        }
        stats = {}
        for band_name, mask in bands.items():
            n = int(np.sum(mask))
            if n > 0:
                stats[f"wr_{band_name}"]  = float(np.mean(y[mask]))
                stats[f"n_{band_name}"]   = n
            else:
                stats[f"wr_{band_name}"]  = None
                stats[f"n_{band_name}"]   = 0
        stats["q25"] = float(q25)
        stats["q50"] = float(q50)
        stats["q75"] = float(q75)
        quartile_stats[fname] = stats

    return quartile_stats


# ══════════════════════════════════════════════════════════════
# REGIME SCORING
# ══════════════════════════════════════════════════════════════

def compute_regime_scores(feature_matrix, feature_labels, top_features_df, y_binary):
    """
    Compute a composite regime score for each signal.

    Method: weighted dot product of top features × their correlations,
    normalized to 0-1 range.

    A score near 1 means the market environment closely resembles historical winners.
    A score near 0 means it resembles historical losers.
    """
    top_names = top_features_df["feature_name"].tolist()
    top_corrs = top_features_df["correlation"].values
    label_to_idx = {name: j for j, name in enumerate(feature_labels)}

    n_signals = feature_matrix.shape[0]
    score_matrix = np.full((n_signals, len(top_names)), np.nan, dtype=np.float64)

    for k, fname in enumerate(top_names):
        j = label_to_idx.get(fname)
        if j is None:
            continue
        col = feature_matrix[:, j].astype(np.float64)

        # Standardize: z-score
        valid = ~np.isnan(col)
        if np.sum(valid) < 2:
            continue
        mu  = np.nanmean(col)
        std = np.nanstd(col)
        if std == 0:
            continue
        score_matrix[:, k] = (col - mu) / std

    # Weighted sum: correlation coefficient is the weight
    # Sign matters: positive correlation → high value = more wins
    # Multiply z-score by correlation sign so high score always = win-like
    weights = top_corrs  # already signed

    # Compute weighted sum, ignoring NaN
    raw_scores = np.full(n_signals, np.nan)
    for i in range(n_signals):
        row = score_matrix[i]
        valid_mask = ~np.isnan(row)
        if np.sum(valid_mask) < 5:
            continue
        raw_scores[i] = np.dot(row[valid_mask], weights[valid_mask]) / np.sum(np.abs(weights[valid_mask]))

    # Normalize to 0-1
    valid = ~np.isnan(raw_scores)
    if np.sum(valid) < 2:
        return raw_scores  # can't normalize

    mn  = np.nanmin(raw_scores)
    mx  = np.nanmax(raw_scores)
    rng = mx - mn
    if rng > 0:
        raw_scores = (raw_scores - mn) / rng

    return raw_scores


def compute_expected_win_rates(regime_scores, y_binary, n_buckets=10):
    """
    Bucket regime scores into deciles and compute win rate per bucket.
    Returns dict: {bucket_label: {score_min, score_max, win_rate, n}}
    """
    valid = ~np.isnan(regime_scores)
    scores = regime_scores[valid]
    wins   = y_binary[valid]

    percentiles = np.linspace(0, 100, n_buckets + 1)
    thresholds  = np.percentile(scores, percentiles)

    buckets = {}
    for b in range(n_buckets):
        lo = thresholds[b]
        hi = thresholds[b + 1]
        if b == n_buckets - 1:
            mask = (scores >= lo) & (scores <= hi)
        else:
            mask = (scores >= lo) & (scores < hi)
        n = int(np.sum(mask))
        wr = float(np.mean(wins[mask])) if n > 0 else None
        label = f"d{b+1}"
        buckets[label] = {
            "score_min": float(lo),
            "score_max": float(hi),
            "win_rate":  wr,
            "n":         n,
        }
    return buckets


# ══════════════════════════════════════════════════════════════
# UPLOAD
# ══════════════════════════════════════════════════════════════

def upload_regime_model(payload):
    """POST regime model to Railway."""
    import urllib.request
    url = f"{API_BASE}/api/v2/regime/model"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "market-grinder/1.0"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def upload_signal_scores(payload):
    """POST signal regime scores to Railway."""
    import urllib.request
    url = f"{API_BASE}/api/v2/regime/scores"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "market-grinder/1.0"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(cycle_id, setup_type, top_n=TOP_N_FEATURES, dry_run=False):
    print("\n" + "=" * 70)
    print("  MARKET GRINDER")
    print("=" * 70)
    print(f"  Cycle:      {cycle_id}")
    print(f"  Top N:      {top_n} features")

    # 1. Load signals
    print(f"\n  Loading signals from Railway...")
    signals_df = load_signals(cycle_id)
    print(f"  {len(signals_df)} signals loaded")

    # 2. Determine win/loss
    # Classification: AUTO_WIN, AI_WIN, MANUAL_WIN = win; everything else = loss
    WIN_CLASSES = {"AUTO_WIN", "AI_WIN", "MANUAL_WIN"}
    y_binary = np.array([
        1 if str(row.get("classification", "")).upper() in WIN_CLASSES else 0
        for _, row in signals_df.iterrows()
    ], dtype=np.float32)

    n_wins   = int(np.sum(y_binary))
    n_losses = int(len(y_binary) - n_wins)
    baseline_wr = float(np.mean(y_binary))
    print(f"  Wins: {n_wins}  Losses: {n_losses}  Baseline win rate: {baseline_wr:.3f}")

    if n_wins < 10 or n_losses < 10:
        raise ValueError(f"Insufficient labeled data: {n_wins} wins, {n_losses} losses. Need at least 10 of each.")

    # 3. Load market cache manifest
    manifest = load_market_manifest()
    print(f"  Market cache: {manifest['n_instruments']} instruments, "
          f"{manifest['n_expressions']} expressions, "
          f"built {manifest.get('built_at', 'unknown')[:10]}")

    # 4. Build feature matrix
    feature_matrix, feature_labels, signal_dates = build_feature_matrix(signals_df, manifest)

    # 5. Compute correlations
    corr_df = compute_correlations(feature_matrix, y_binary, feature_labels)

    print(f"\n  Top 20 features by |correlation|:")
    print(f"  {'Feature':<60} {'Corr':>8}  {'N':>6}")
    print(f"  {'-'*78}")
    for _, row in corr_df.head(20).iterrows():
        print(f"  {row['feature_name']:<60} {row['correlation']:>+8.4f}  {row['n_valid']:>6}")

    # 6. Select top N
    top_df = corr_df.head(top_n).copy()
    top_feature_names = top_df["feature_name"].tolist()

    # 7. Quartile win rates for top features
    print(f"\n  Computing quartile win rates for top {top_n} features...")
    quartile_stats = compute_quartile_win_rates(
        feature_matrix, feature_labels, y_binary, top_feature_names
    )

    # 8. Regime scores
    print(f"\n  Computing regime scores...")
    regime_scores = compute_regime_scores(feature_matrix, feature_labels, top_df, y_binary)
    valid_scores = regime_scores[~np.isnan(regime_scores)]
    print(f"  Scores computed: {len(valid_scores)}/{len(regime_scores)} signals")
    print(f"  Score range: {np.nanmin(regime_scores):.3f} – {np.nanmax(regime_scores):.3f}")
    print(f"  Score mean:  {np.nanmean(regime_scores):.3f}")

    # 9. Win rate by decile
    wr_by_decile = compute_expected_win_rates(regime_scores, y_binary)
    print(f"\n  Win rate by regime score decile:")
    print(f"  {'Decile':<8} {'Score range':<20} {'Win rate':>10}  {'N':>6}")
    for label, stats in wr_by_decile.items():
        wr_str = f"{stats['win_rate']:.3f}" if stats['win_rate'] is not None else "  N/A"
        print(f"  {label:<8} {stats['score_min']:.3f} – {stats['score_max']:.3f}   "
              f"{wr_str:>10}  {stats['n']:>6}")

    # 10. Build upload payloads
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Feature weights: top N features with their correlation + quartile stats
    feature_weights = {}
    for _, row in top_df.iterrows():
        fname = row["feature_name"]
        feature_weights[fname] = {
            "correlation": row["correlation"],
            "abs_correlation": row["abs_correlation"],
            "n_valid": row["n_valid"],
            "p_value": row.get("p_value"),
            "quartiles": quartile_stats.get(fname),
        }

    regime_model_payload = {
        "setup_type":        setup_type,
        "cycle_id":          cycle_id,
        "n_signals_used":    int(len(signals_df)),
        "n_features_tested": int(len(corr_df)),
        "feature_weights":   json.dumps(feature_weights),
        "top_features":      json.dumps(top_feature_names[:5]),
        "win_rate_by_decile": json.dumps(wr_by_decile),
        "baseline_win_rate": baseline_wr,
        "updated_at":        now,
    }

    # Signal scores
    signal_score_rows = []
    for i, (_, sig_row) in enumerate(signals_df.iterrows()):
        score = float(regime_scores[i]) if not np.isnan(regime_scores[i]) else None

        # Map score to expected win rate via decile
        expected_wr = None
        if score is not None:
            for stats in wr_by_decile.values():
                if stats["score_min"] <= score <= stats["score_max"]:
                    expected_wr = stats["win_rate"]
                    break

        signal_score_rows.append({
            "cycle_signal_id": sig_row.get("id"),
            "cycle_id":        cycle_id,
            "regime_score":    score,
            "expected_win_rate": expected_wr,
        })

    signal_scores_payload = {
        "cycle_id": cycle_id,
        "scores":   signal_score_rows,
    }

    if dry_run:
        print(f"\n  DRY RUN — not uploading.")
        print(f"  Would upload regime model + {len(signal_score_rows)} signal scores.")
        return

    # 11. Upload
    print(f"\n  Uploading regime model...")
    result = upload_regime_model(regime_model_payload)
    print(f"  {result}")

    print(f"  Uploading {len(signal_score_rows)} signal scores...")
    result = upload_signal_scores(signal_scores_payload)
    print(f"  {result}")

    print(f"\n  ✓ Market grinder complete.")
    print(f"  Top feature: {top_feature_names[0]}")
    print(f"  Regime score spread: {np.nanmin(regime_scores):.3f} – {np.nanmax(regime_scores):.3f}")
    print(f"  Win rate in top decile vs bottom: "
          f"{wr_by_decile.get('d10', {}).get('win_rate', 'N/A')} vs "
          f"{wr_by_decile.get('d1', {}).get('win_rate', 'N/A')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Market Grinder — regime correlation engine")
    parser.add_argument("--cycle",   help="Specific cycle_id to run on")
    parser.add_argument("--setup",   default="dtss", help="Setup type (uses current cycle)")
    parser.add_argument("--top-n",   type=int, default=TOP_N_FEATURES,
                        help=f"Number of top features to keep (default: {TOP_N_FEATURES})")
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't upload")
    args = parser.parse_args()

    if args.cycle:
        cycle_id   = args.cycle
        setup_type = cycle_id.split("_")[0]
    else:
        setup_type = args.setup
        print(f"  Fetching current cycle for {setup_type}...")
        cycle_id = get_current_cycle(setup_type)
        print(f"  Current cycle: {cycle_id}")

    run(cycle_id, setup_type, top_n=args.top_n, dry_run=args.dry_run)
