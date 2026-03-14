"""
Entry Candle Scorer — Standalone vetting utility.

Scores the refinement winner pile by how similar each signal's forward window
bars are to validated example entry candles. Not a pipeline step — a vetting
tool you run on demand to rank-order charts for review.

Usage:
    python scripts/entry_candle_scorer.py --setup dtss
"""

import os
import sys
import json
import glob
import argparse
import numpy as np
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import ExprSeriesCache

API_BASE = "https://web-production-e3025.up.railway.app"

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
    # ── Load examples from Railway API ──
    print("\n  Loading examples from Railway API...")
    resp = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=30)
    resp.raise_for_status()
    examples = resp.json().get("examples", [])
    print(f"  {len(examples)} examples loaded")

    # ── Look up entry candle expression vectors ──
    print("  Looking up entry candle expression vectors...")
    vectors = []
    skipped = []

    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
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

    return centroid, n_used


def cosine_similarity(vec, centroid, valid_mask):
    """Cosine similarity between vec and centroid using only valid_mask positions.

    valid_mask: boolean array — True where both vec and centroid are non-NaN.
    Returns similarity in [-1, 1], or -999 if insufficient shared values.
    """
    a = vec[valid_mask]
    b = centroid[valid_mask]

    # Need shared non-NaN values in both
    both_valid = ~np.isnan(a) & ~np.isnan(b)
    a = a[both_valid]
    b = b[both_valid]

    if len(a) < 100:
        return -999.0, 0

    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0 or norm_b == 0:
        return -999.0, len(a)

    return float(dot / (norm_a * norm_b)), len(a)


def score_winner_pile(setup_type, expr_cache, centroid):
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
            sim, n_shared = cosine_similarity(vec, centroid, centroid_valid)
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

    # ── Sort by score descending ──
    scored.sort(key=lambda s: s.get("entry_candle_score") or -999, reverse=True)

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
        print(f"\n  Top 10 by entry candle score:")
        print(f"    {'Ticker':<8} {'Score':>8} {'Offset':>7} {'Date':<12} {'move_adr':>9} {'Shared':>7}")
        print(f"    {'-'*55}")
        for s in scored[:10]:
            print(f"    {s['ticker']:<8} {s.get('entry_candle_score', 0):>8.4f} "
                  f"{s.get('entry_candle_bar_offset', '?'):>7} "
                  f"{s.get('entry_candle_date', '?'):<12} "
                  f"{s.get('move_adr', 0):>9.3f} "
                  f"{s.get('entry_candle_shared_exprs', 0):>7}")

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
    parser.add_argument("--setup", default="dtss", help="Setup type")
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
    centroid, n_used = build_entry_candle_centroid(args.setup, expr_cache)
    if centroid is None:
        return

    # ── Score winner pile ──
    scored = score_winner_pile(args.setup, expr_cache, centroid)
    if scored is None:
        return

    # ── Save ──
    save_scores(args.setup, scored)

    print("\n  ✓ Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
