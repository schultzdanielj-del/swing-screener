"""
Entry Candle Scorer (Full Population) — Standalone diagnostic.

Scores ALL signals in the refinement output (winners + surviving losers +
eliminated) against the example entry candle centroid. Not a pipeline step.
Used to determine whether entry candle quality varies by signal classification
and whether a correlative grinder could predict it.

Usage:
    python scripts/entry_candle_scorer_full.py --setup dtss
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

MIN_VALID_FRACTION = 0.5


def build_entry_candle_centroid(setup_type, expr_cache):
    """Build centroid from example entry candle expression values.
    Identical to the pipeline scorer — examples define the reference point."""
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
        return None, 0, None

    matrix = np.array(vectors)
    valid_counts = np.sum(~np.isnan(matrix), axis=0)
    min_required = int(n_used * MIN_VALID_FRACTION)

    centroid = np.nanmean(matrix, axis=0)
    low_coverage_mask = valid_counts < min_required
    centroid[low_coverage_mask] = np.nan

    n_valid = int(np.sum(~np.isnan(centroid)))
    n_masked = int(np.sum(low_coverage_mask))

    print(f"\n  Centroid built:")
    print(f"    Valid expressions: {n_valid}/{len(centroid)}")
    print(f"    Masked (< {MIN_VALID_FRACTION*100:.0f}% coverage): {n_masked}")

    return centroid, n_used, matrix


def build_expression_weights(entry_matrix, setup_type, expr_cache):
    """Compute per-expression discrimination weights.
    Identical to the pipeline scorer."""
    n_entry = entry_matrix.shape[0]
    n_exprs = entry_matrix.shape[1]

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
            weights[j] = 1e6
            usable += 1
            continue
        if fw_stdev[j] < 1e-10:
            continue

        weights[j] = fw_stdev[j] / entry_stdev[j]
        usable += 1

    real_weights = weights[(weights > 0) & (weights < 1e6)]
    if len(real_weights) == 0:
        print("  ERROR: No expressions with real positive weight")
        return weights

    cap = float(np.percentile(real_weights, 95))
    n_capped = int(np.sum(weights > cap))
    weights = np.minimum(weights, cap)

    print(f"  Usable expressions: {usable}/{n_exprs}")
    print(f"  95th percentile cap: {cap:.3f}")
    print(f"  Expressions capped: {n_capped}")

    return weights


def weighted_cosine_similarity(vec, centroid, valid_mask, weights):
    """Weighted cosine similarity. Identical to pipeline scorer."""
    a = vec[valid_mask]
    b = centroid[valid_mask]
    w = weights[valid_mask]

    both_valid = ~np.isnan(a) & ~np.isnan(b)
    a = a[both_valid]
    b = b[both_valid]
    w = w[both_valid]

    if len(a) < 100:
        return -999.0, 0

    aw = a * w
    bw = b * w

    dot = np.dot(aw, bw)
    norm_a = np.linalg.norm(aw)
    norm_b = np.linalg.norm(bw)

    if norm_a == 0 or norm_b == 0:
        return -999.0, len(a)

    return float(dot / (norm_a * norm_b)), len(a)


def score_all_signals(setup_type, expr_cache, centroid, weights):
    """Score every signal in the refinement output — winners, losers, eliminated."""

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
    losers = ref_data.get("loser_signals", [])
    eliminated = ref_data.get("eliminated_signals", [])
    print(f"  Winners: {len(winners)}, Losers: {len(losers)}, Eliminated: {len(eliminated)}")

    # Tag each signal with its population
    all_signals = []
    for s in winners:
        s2 = dict(s)
        s2["population"] = "winner"
        all_signals.append(s2)
    for s in losers:
        s2 = dict(s)
        s2["population"] = "loser_surviving"
        all_signals.append(s2)
    for s in eliminated:
        s2 = dict(s)
        s2["population"] = "loser_eliminated"
        all_signals.append(s2)

    print(f"  Total signals to score: {len(all_signals)}")

    # ── Load cluster structure for scan ranges ──
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

    cluster_lookup = {}
    for c in clusters:
        key = (c["ticker"], c["rightmost"]["bar_idx"])
        cluster_lookup[key] = c

    # ── Score every signal ──
    centroid_valid = ~np.isnan(centroid)
    print(f"\n  Scoring {len(all_signals)} signals...")
    scored = []
    skipped = 0
    prev_ticker = None
    cached_ticker_data = None

    for i, sig in enumerate(all_signals):
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]

        key = (ticker, bar_idx)
        c = cluster_lookup.get(key)
        if c is None:
            skipped += 1
            continue

        leftward = c.get("leftward", [])
        all_bars = [bar_idx] + [lw["bar_idx"] for lw in leftward]
        leftmost = min(all_bars)
        scan_start = leftmost
        scan_end = bar_idx + forward_window

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

        best_score = -999.0
        best_offset = None
        best_date = None
        best_shared = 0

        for bi in range(scan_start, scan_end + 1):
            vec = data[bi, :].astype(np.float64)
            sim, n_shared = weighted_cosine_similarity(vec, centroid, centroid_valid, weights)
            if sim > best_score:
                best_score = sim
                best_offset = bi - bar_idx
                best_date = str(dates[bi])[:10]
                best_shared = n_shared

        scored_signal = dict(sig)
        scored_signal["entry_candle_score"] = round(best_score, 6) if best_score > -999 else None
        scored_signal["entry_candle_bar_offset"] = best_offset
        scored_signal["entry_candle_date"] = best_date
        scored_signal["entry_candle_shared_exprs"] = best_shared
        scored.append(scored_signal)

        if (i + 1) % 100 == 0:
            print(f"    {i + 1}/{len(all_signals)} scored...")

    print(f"  Scored: {len(scored)}, Skipped: {skipped}")
    return scored


def print_diagnostics(scored):
    """Print distribution analysis comparing winners vs losers."""

    print("\n" + "=" * 60)
    print("  DISTRIBUTION ANALYSIS")
    print("=" * 60)

    winners = [s for s in scored if s["population"] == "winner"]
    losers_surv = [s for s in scored if s["population"] == "loser_surviving"]
    losers_elim = [s for s in scored if s["population"] == "loser_eliminated"]
    all_losers = losers_surv + losers_elim

    def stats(signals, label):
        scores = [s["entry_candle_score"] for s in signals
                  if s.get("entry_candle_score") is not None]
        if not scores:
            print(f"\n  {label}: no scores")
            return
        arr = np.array(scores)
        print(f"\n  {label} (n={len(arr)}):")
        print(f"    Min:    {arr.min():.4f}")
        print(f"    P10:    {np.percentile(arr, 10):.4f}")
        print(f"    P25:    {np.percentile(arr, 25):.4f}")
        print(f"    Median: {np.median(arr):.4f}")
        print(f"    P75:    {np.percentile(arr, 75):.4f}")
        print(f"    P90:    {np.percentile(arr, 90):.4f}")
        print(f"    Max:    {arr.max():.4f}")
        print(f"    Mean:   {np.mean(arr):.4f}")
        print(f"    Std:    {arr.std():.4f}")

    stats(winners, "WINNERS")
    stats(all_losers, "ALL LOSERS (surviving + eliminated)")
    stats(losers_surv, "LOSERS — surviving refinement")
    stats(losers_elim, "LOSERS — eliminated by refinement")

    # ── Decile breakdown: do winners concentrate in higher score deciles? ──
    all_with_scores = [s for s in scored if s.get("entry_candle_score") is not None]
    if len(all_with_scores) < 20:
        print("\n  Too few scored signals for decile analysis")
        return

    all_scores = np.array([s["entry_candle_score"] for s in all_with_scores])
    decile_edges = np.percentile(all_scores, np.arange(10, 100, 10))

    print(f"\n  DECILE ANALYSIS (all {len(all_with_scores)} signals, bucketed by entry_candle_score):")
    print(f"    {'Decile':<8} {'Range':<22} {'Total':>6} {'Win':>5} {'Loss':>6} {'WR':>7}")
    print(f"    {'-'*57}")

    for d in range(10):
        if d == 0:
            lo = all_scores.min() - 0.001
        else:
            lo = decile_edges[d - 1]
        if d == 9:
            hi = all_scores.max() + 0.001
        else:
            hi = decile_edges[d]

        in_decile = [s for s in all_with_scores if lo < s["entry_candle_score"] <= hi]
        if d == 0:
            in_decile = [s for s in all_with_scores if s["entry_candle_score"] <= hi]

        n_total = len(in_decile)
        n_win = sum(1 for s in in_decile if s["population"] == "winner")
        n_loss = n_total - n_win
        wr = n_win / n_total * 100 if n_total > 0 else 0

        print(f"    D{d+1:<6} {lo:>8.4f} — {hi:<8.4f}  {n_total:>6} {n_win:>5} {n_loss:>6} {wr:>6.1f}%")

    # ── Examples vs non-examples ──
    examples = [s for s in scored if s.get("is_example") and s.get("entry_candle_score") is not None]
    non_ex = [s for s in scored if not s.get("is_example") and s.get("entry_candle_score") is not None]
    if examples:
        ex_scores = np.array([s["entry_candle_score"] for s in examples])
        ne_scores = np.array([s["entry_candle_score"] for s in non_ex])
        print(f"\n  Examples (n={len(ex_scores)}):     median={np.median(ex_scores):.4f}")
        print(f"  Non-examples (n={len(ne_scores)}): median={np.median(ne_scores):.4f}")


def save_scores(setup_type, scored):
    """Save full-population scores to local cache."""
    from datetime import datetime, timezone

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_scored": len(scored),
        "populations": {
            "winner": sum(1 for s in scored if s["population"] == "winner"),
            "loser_surviving": sum(1 for s in scored if s["population"] == "loser_surviving"),
            "loser_eliminated": sum(1 for s in scored if s["population"] == "loser_eliminated"),
        },
        "scored_signals": scored,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"entry_scores_full_{setup_type}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    latest_path = os.path.join(CACHE_DIR, f"entry_scores_full_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved: {latest_path}")

    return out_path


def main():
    parser = argparse.ArgumentParser(description="Entry Candle Scorer — Full Population")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    args = parser.parse_args()

    print("=" * 60)
    print("  ENTRY CANDLE SCORER — FULL POPULATION (diagnostic)")
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

    # ── Score all signals ──
    scored = score_all_signals(args.setup, expr_cache, centroid, weights)
    if scored is None:
        return

    # ── Diagnostics ──
    print_diagnostics(scored)

    # ── Save ──
    save_scores(args.setup, scored)

    print("\n  Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()
