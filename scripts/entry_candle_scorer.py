"""
Entry Candle Scorer — Post-refinement pipeline step.

Scores the refinement winner pile by how similar each signal's forward window
bars are to validated example entry candles. Runs automatically after
refinement grind; output consumed by vetting workspace for sort ordering.

Usage:
    python scripts/entry_candle_scorer.py --setup dtss
"""

import os
import sys
import json
import glob
import argparse
import sqlite3
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import ExprSeriesCache

# Minimum fraction of examples that must have a valid value for an expression
# to be included in the centroid. Below this, the expression is NaN'd out.
MIN_VALID_FRACTION = 0.5


def build_entry_candle_centroid(setup_type, expr_cache):
    """Build the centroid vector from example entry candle expression values.

    Returns:
        centroid: numpy array shape (n_expressions,) — mean entry candle profile.
                  Expressions with < MIN_VALID_FRACTION coverage are set to NaN.
        n_examples_used: how many examples had valid expr cache lookups
    """
    # ── Load examples from local SQLite ──
    print("\n  Loading examples from local DB...")
    examples = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, entry_date, chart_date FROM examples WHERE setup_type=?",
            (setup_type,)
        ).fetchall()
        conn.close()
        examples = [{"ticker": r["ticker"], "entry_date": r["entry_date"],
                      "chart_date": r["chart_date"]} for r in rows]
    except Exception as e:
        print(f"  ERROR loading examples: {e}")
        return None, 0, None
    print(f"  {len(examples)} examples loaded")

    # ── Look up entry candle expression vectors ──
    print("  Looking up entry candle expression vectors...")
    vectors = []
    skipped = []

    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entry_date")
        if not ticker or not entry_date:
            skipped.append(f"{ticker} (no entry_date)")
            continue

        dates, data = expr_cache.get_ticker(ticker)
        if dates is None:
            skipped.append(f"{ticker} (not in expr cache)")
            continue

        dates_str = [str(d)[:10] for d in dates]
        if entry_date not in dates_str:
            skipped.append(f"{ticker} (entry_date {entry_date} not in cache)")
            continue

        entry_idx = dates_str.index(entry_date)
        vec = data[entry_idx, :].astype(np.float64)
        vectors.append(vec)

    if skipped:
        print(f"  Skipped {len(skipped)} examples: {', '.join(skipped[:5])}")
        if len(skipped) > 5:
            print(f"    ... and {len(skipped) - 5} more")

    n_used = len(vectors)
    print(f"  Entry candle vectors: {n_used}/{len(examples)}")

    if n_used == 0:
        print("  ERROR: No valid entry candle vectors")
        return None, 0

    # ── Stack into matrix and compute centroid ──
    matrix = np.array(vectors)  # shape: (n_examples, n_expressions)

    # Count valid (non-NaN) values per expression
    valid_counts = np.sum(~np.isnan(matrix), axis=0)  # shape: (n_expressions,)
    min_required = int(n_used * MIN_VALID_FRACTION)

    # Compute centroid (nanmean), then NaN out expressions below coverage threshold
    centroid = np.nanmean(matrix, axis=0)  # shape: (n_expressions,)
    low_coverage_mask = valid_counts < min_required
    centroid[low_coverage_mask] = np.nan

    n_valid = int(np.sum(~np.isnan(centroid)))
    n_masked = int(np.sum(low_coverage_mask))
    n_total = len(centroid)

    print(f"\n  Centroid built:")
    print(f"    Shape: {centroid.shape}")
    print(f"    Valid expressions: {n_valid}/{n_total}")
    print(f"    Masked (< {MIN_VALID_FRACTION*100:.0f}% coverage): {n_masked}")
    print(f"    Sample values: [{centroid[0]:.4f}, {centroid[1]:.4f}, {centroid[2]:.4f}]")

    # ── Coverage distribution ──
    pct_coverage = valid_counts / n_used * 100
    for threshold in [100, 90, 75, 50, 25]:
        count = int(np.sum(pct_coverage >= threshold))
        print(f"    Expressions with >= {threshold}% coverage: {count}")

    return centroid, n_used, matrix


def build_expression_weights(entry_matrix, setup_type, expr_cache):
    """Compute per-expression discrimination weights.

    For each expression, compares how tightly the entry candles cluster
    (entry_stdev) vs how spread out the winner pile forward window bars are
    (fw_stdev). Weight = fw_stdev / entry_stdev, capped at the 95th percentile
    to prevent extreme outliers from dominating the similarity score.

    Returns:
        weights: numpy array shape (n_expressions,) — 0 for unusable expressions
    """
    n_entry = entry_matrix.shape[0]
    n_exprs = entry_matrix.shape[1]

    # ── Collect forward window bar vectors from non-example winners ──
    print("\n  Building expression weights...")
    print("  Collecting forward window bars for weight computation...")

    ref_files = glob.glob(os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json"))
    ref_files.sort(key=os.path.getmtime, reverse=True)
    with open(ref_files[0]) as f:
        ref_data = json.load(f)
    winners = ref_data.get("winner_signals", [])

    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    with open(cluster_path) as f:
        cluster_data = json.load(f)
    forward_window = cluster_data.get("forward_window")
    clusters = cluster_data.get("clusters", [])

    cluster_lookup = {}
    for c in clusters:
        key = (c["ticker"], c["rightmost"]["bar_idx"])
        cluster_lookup[key] = c

    fw_vectors = []
    prev_ticker = None
    cached_data = None

    for w in winners:
        if w.get("is_example") == 1:
            continue

        ticker = w["ticker"]
        bar_idx = w["bar_idx"]
        key = (ticker, bar_idx)
        c = cluster_lookup.get(key)
        if c is None:
            continue

        leftward = c.get("leftward", [])
        all_bars = [bar_idx] + [lw["bar_idx"] for lw in leftward]
        leftmost = min(all_bars)
        scan_start = leftmost
        scan_end = bar_idx + forward_window

        if ticker != prev_ticker:
            dates, data = expr_cache.get_ticker(ticker)
            cached_data = (dates, data)
            prev_ticker = ticker
        else:
            dates, data = cached_data

        if dates is None:
            continue

        scan_end = min(scan_end, len(data) - 1)
        if scan_start > scan_end:
            continue

        for bi in range(scan_start, scan_end + 1):
            fw_vectors.append(data[bi, :].astype(np.float64))

    print(f"  Forward window bars (non-example): {len(fw_vectors)}")
    fw_matrix = np.array(fw_vectors)

    # ── Compute discrimination ratio per expression ──
    min_entry = int(n_entry * MIN_VALID_FRACTION)
    min_fw = int(len(fw_vectors) * 0.1)

    entry_valid_counts = np.sum(~np.isnan(entry_matrix), axis=0)
    fw_valid_counts = np.sum(~np.isnan(fw_matrix), axis=0)

    with np.errstate(all='ignore'):
        entry_stdev = np.nanstd(entry_matrix, axis=0)
        fw_stdev = np.nanstd(fw_matrix, axis=0)

    weights = np.full(n_exprs, 0.0)
    usable = 0

    for j in range(n_exprs):
        if entry_valid_counts[j] < min_entry:
            continue
        if fw_valid_counts[j] < min_fw:
            continue
        if np.isnan(entry_stdev[j]) or np.isnan(fw_stdev[j]):
            continue
        if entry_stdev[j] < 1e-10:
            # Entry candles identical on this expression — maximally diagnostic
            # but capped later by percentile, so use a placeholder high value
            weights[j] = 1e6
            usable += 1
            continue
        if fw_stdev[j] < 1e-10:
            continue

        weights[j] = fw_stdev[j] / entry_stdev[j]
        usable += 1

    # ── Cap at 95th percentile of real ratios (exclude 1e6 placeholders) ──
    real_weights = weights[(weights > 0) & (weights < 1e6)]
    if len(real_weights) == 0:
        print("  ERROR: No expressions with real positive weight")
        return weights

    cap = float(np.percentile(real_weights, 95))

    # Apply cap to everything including the 1e6 placeholders
    n_capped = int(np.sum(weights > cap))
    weights = np.minimum(weights, cap)

    print(f"  Usable expressions: {usable}/{n_exprs}")
    print(f"  95th percentile cap: {cap:.3f}")
    print(f"  Expressions capped: {n_capped}")
    nonzero = weights[weights > 0]
    print(f"  Weight range after cap: {np.min(nonzero):.4f} — {cap:.3f}")

    return weights


def weighted_cosine_similarity(vec, centroid, valid_mask, weights):
    """Weighted cosine similarity between vec and centroid.

    Multiplies each expression dimension by its weight before computing
    the dot product. Expressions with higher weights contribute more to
    the similarity score.

    valid_mask: boolean array — True where centroid is non-NaN.
    weights: array shape (n_expressions,) — per-expression importance weights.
    Returns similarity in [-1, 1], or -999 if insufficient shared values.
    """
    a = vec[valid_mask]
    b = centroid[valid_mask]
    w = weights[valid_mask]

    # Need shared non-NaN values in both
    both_valid = ~np.isnan(a) & ~np.isnan(b)
    a = a[both_valid]
    b = b[both_valid]
    w = w[both_valid]

    if len(a) < 100:
        return -999.0, 0

    # Apply weights
    aw = a * w
    bw = b * w

    dot = np.dot(aw, bw)
    norm_a = np.linalg.norm(aw)
    norm_b = np.linalg.norm(bw)

    if norm_a == 0 or norm_b == 0:
        return -999.0, len(a)

    return float(dot / (norm_a * norm_b)), len(a)


def score_winner_pile(setup_type, expr_cache, centroid, weights):
    """Score each winner signal's forward window bars against the centroid.

    For each winner cluster:
      - Find scan range: leftmost bar through rightmost bar + forward_window
      - Score every bar in that range against the centroid
      - Keep the best-matching bar as the cluster's entry candle score

    Returns list of scored signals (same as winner_signals with added fields).
    """
    # ── Load refinement output ──
    print("\n  Loading refinement output...")
    ref_files = glob.glob(os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json"))
    if not ref_files:
        print("  ERROR: No refinement output found")
        return None
    ref_files.sort(key=os.path.getmtime, reverse=True)
    ref_path = ref_files[0]
    print(f"  File: {os.path.basename(ref_path)}")
    with open(ref_path) as f:
        ref_data = json.load(f)
    winners = ref_data.get("winner_signals", [])
    print(f"  Winner signals: {len(winners)}")

    # ── Load raw_signal_clusters for forward_window and cluster structure ──
    print("  Loading raw_signal_clusters...")
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(cluster_path):
        print("  ERROR: No raw_signal_clusters file found")
        return None
    with open(cluster_path) as f:
        cluster_data = json.load(f)
    forward_window = cluster_data.get("forward_window")
    clusters = cluster_data.get("clusters", [])
    print(f"  forward_window: {forward_window} bars")
    print(f"  Clusters: {len(clusters)}")

    # ── Build cluster lookup: (ticker, rightmost_bar_idx) → cluster ──
    cluster_lookup = {}
    for c in clusters:
        key = (c["ticker"], c["rightmost"]["bar_idx"])
        cluster_lookup[key] = c

    # ── Precompute centroid valid mask (non-NaN positions) ──
    centroid_valid = ~np.isnan(centroid)

    # ── Score each winner ──
    print(f"\n  Scoring {len(winners)} winners...")
    scored = []
    skipped = 0
    prev_ticker = None
    cached_ticker_data = None

    for i, w in enumerate(winners):
        ticker = w["ticker"]
        bar_idx = w["bar_idx"]

        # Look up cluster to get leftward bars
        key = (ticker, bar_idx)
        c = cluster_lookup.get(key)
        if c is None:
            skipped += 1
            continue

        # Compute scan range: leftmost through rightmost + forward_window
        leftward = c.get("leftward", [])
        all_bars = [bar_idx] + [lw["bar_idx"] for lw in leftward]
        leftmost = min(all_bars)
        scan_start = leftmost
        scan_end = bar_idx + forward_window

        # Load expr cache for this ticker (reuse if same ticker)
        if ticker != prev_ticker:
            dates, data = expr_cache.get_ticker(ticker)
            cached_ticker_data = (dates, data)
            prev_ticker = ticker
        else:
            dates, data = cached_ticker_data

        if dates is None:
            skipped += 1
            continue

        n_bars = len(data)
        scan_end = min(scan_end, n_bars - 1)

        if scan_start >= n_bars or scan_start > scan_end:
            skipped += 1
            continue

        # Score each bar in scan range, keep best
        best_score = -999.0
        best_offset = None
        best_date = None
        best_shared = 0

        for bi in range(scan_start, scan_end + 1):
            vec = data[bi, :].astype(np.float64)
            sim, n_shared = weighted_cosine_similarity(vec, centroid, centroid_valid, weights)
            if sim > best_score:
                best_score = sim
                best_offset = bi - bar_idx  # negative = before rightmost, positive = after
                best_date = str(dates[bi])[:10]
                best_shared = n_shared

        # Build scored signal
        scored_signal = dict(w)
        scored_signal["entry_candle_score"] = round(best_score, 6) if best_score > -999 else None
        scored_signal["entry_candle_bar_offset"] = best_offset
        scored_signal["entry_candle_date"] = best_date
        scored_signal["entry_candle_shared_exprs"] = best_shared
        scored.append(scored_signal)

        if (i + 1) % 50 == 0:
            print(f"    {i + 1}/{len(winners)} scored...")

    print(f"  Scored: {len(scored)}, Skipped: {skipped}")

    # ── Compute combined score: percentile rank of entry_candle_score × percentile rank of move_adr ──
    # Normalize both to 0-1 via percentile rank, then multiply
    ec_scores = np.array([s.get("entry_candle_score") or -999 for s in scored])
    adr_scores = np.array([s.get("move_adr") or 0 for s in scored])

    # Percentile rank: fraction of values that are <= this value
    def percentile_ranks(arr):
        from scipy.stats import rankdata
        ranks = rankdata(arr, method='average')
        return (ranks - 1) / max(len(ranks) - 1, 1)  # 0 to 1

    ec_pct = percentile_ranks(ec_scores)
    adr_pct = percentile_ranks(adr_scores)
    combined = ec_pct * adr_pct

    for i, s in enumerate(scored):
        s["entry_candle_pct"] = round(float(ec_pct[i]), 4)
        s["move_adr_pct"] = round(float(adr_pct[i]), 4)
        s["combined_score"] = round(float(combined[i]), 4)

    # ── Sort by combined score descending ──
    scored.sort(key=lambda s: s.get("combined_score") or -999, reverse=True)

    # ── Summary stats ──
    valid_scores = [s["entry_candle_score"] for s in scored if s.get("entry_candle_score") is not None]
    if valid_scores:
        print(f"\n  Score distribution:")
        print(f"    Min:    {min(valid_scores):.4f}")
        print(f"    25th:   {np.percentile(valid_scores, 25):.4f}")
        print(f"    Median: {np.percentile(valid_scores, 50):.4f}")
        print(f"    75th:   {np.percentile(valid_scores, 75):.4f}")
        print(f"    Max:    {max(valid_scores):.4f}")

        # Top 10
        print(f"\n  Top 10 by combined score (entry candle × move_adr):")
        print(f"    {'Ticker':<8} {'Combined':>8} {'EC_pct':>7} {'ADR_pct':>8} {'EC_score':>9} {'move_adr':>9} {'Offset':>7}")
        print(f"    {'-'*62}")
        for s in scored[:10]:
            print(f"    {s['ticker']:<8} {s.get('combined_score', 0):>8.4f} "
                  f"{s.get('entry_candle_pct', 0):>7.3f} "
                  f"{s.get('move_adr_pct', 0):>8.3f} "
                  f"{s.get('entry_candle_score', 0):>9.4f} "
                  f"{s.get('move_adr', 0):>9.3f} "
                  f"{s.get('entry_candle_bar_offset', '?'):>7}")

    return scored


def save_scores(setup_type, scored):
    """Save scored signals to local cache and mirror to Railway."""
    from datetime import datetime, timezone

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_scored": len(scored),
        "scored_signals": scored,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"entry_scores_{setup_type}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Latest pointer
    latest_path = os.path.join(CACHE_DIR, f"entry_scores_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved: {latest_path}")

    # Mirror to Railway
    try:
        from file_mirror import mirror_file
        mirror_file(out_path)
        mirror_file(latest_path)
        print(f"  Mirrored to Railway")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    return out_path


def main():
    parser = argparse.ArgumentParser(description="Entry Candle Scorer")
    parser.add_argument("--setup", required=True, help="Setup type")
    args = parser.parse_args()

    print("=" * 60)
    print("  ENTRY CANDLE SCORER")
    print("=" * 60)

    # ── Load expr cache ──
    print("\n  Loading expr cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expr cache not valid")
        return
    print(f"  {expr_cache.n_expressions} expressions")

    # ── Build centroid ──
    centroid, n_used, entry_matrix = build_entry_candle_centroid(args.setup, expr_cache)
    if centroid is None:
        return

    # ── Build expression weights ──
    weights = build_expression_weights(entry_matrix, args.setup, expr_cache)

    # ── Score winner pile ──
    scored = score_winner_pile(args.setup, expr_cache, centroid, weights)
    if scored is None:
        return

    # ── Save ──
    save_scores(args.setup, scored)

    print("\n  ✓ Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
