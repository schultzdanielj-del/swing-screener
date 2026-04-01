"""
Entry Candle Scorer — Expression Weight Diagnostic

For each expression, compares how tightly the 65 entry candles cluster
vs how spread out the winner pile forward window bars are.

Expressions where entry candles are tight and random bars are spread
are the most diagnostic — they're what makes entry candles distinctive.

Prints the top and bottom expressions by discrimination ratio so we
can see if the weighting finds real signal (e.g. LSP, extension, candle
pattern expressions should rank high).
"""

import os
import sys
import json
import glob
import numpy as np
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import ExprSeriesCache

MIN_VALID_FRACTION = 0.5


def main():
    setup = "dtss"

    print("=" * 60)
    print("  ENTRY CANDLE — EXPRESSION WEIGHT DIAGNOSTIC")
    print("=" * 60)

    # ── Load expr cache ──
    print("\n  Loading expr cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expr cache not valid")
        return
    n_exprs = expr_cache.n_expressions
    expr_names = expr_cache.expr_names
    print(f"  {n_exprs} expressions")

    # ── Load examples and build entry candle matrix ──
    print("\n  Loading examples from local DB...")
    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker",
        (setup,)
    ).fetchall()
    conn.close()
    examples = [{"ticker": r["ticker"], "entryDate": r["entry_date"]} for r in rows]
    print(f"  {len(examples)} examples loaded")

    print("  Building entry candle matrix...")
    entry_vectors = []
    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        if not ticker or not entry_date:
            continue
        dates, data = expr_cache.get_ticker(ticker)
        if dates is None:
            continue
        dates_str = [str(d)[:10] for d in dates]
        if entry_date not in dates_str:
            continue
        entry_idx = dates_str.index(entry_date)
        entry_vectors.append(data[entry_idx, :].astype(np.float64))

    n_entry = len(entry_vectors)
    print(f"  Entry candle vectors: {n_entry}")
    entry_matrix = np.array(entry_vectors)  # (n_entry, n_exprs)

    # ── Load winner pile forward window bars as "comparison" bars ──
    print("\n  Loading winner pile forward window bars...")
    ref_files = glob.glob(os.path.join(CACHE_DIR, f"refinement_{setup}_*.json"))
    if not ref_files:
        print("  ERROR: No refinement output found")
        return
    ref_files.sort(key=os.path.getmtime, reverse=True)
    with open(ref_files[0]) as f:
        ref_data = json.load(f)
    winners = ref_data.get("winner_signals", [])

    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup}.json")
    with open(cluster_path) as f:
        cluster_data = json.load(f)
    forward_window = cluster_data.get("forward_window")
    clusters = cluster_data.get("clusters", [])

    # Build cluster lookup
    cluster_lookup = {}
    for c in clusters:
        key = (c["ticker"], c["rightmost"]["bar_idx"])
        cluster_lookup[key] = c

    # Collect all forward window bar vectors from non-example winners
    fw_vectors = []
    prev_ticker = None
    cached_data = None

    for w in winners:
        if w.get("is_example") == 1:
            continue  # skip examples — we want comparison bars only

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

    n_fw = len(fw_vectors)
    print(f"  Forward window bars (non-example): {n_fw}")
    fw_matrix = np.array(fw_vectors)  # (n_fw, n_exprs)

    # ── Compute discrimination ratio per expression ──
    print("\n  Computing discrimination ratios...")

    # Stdev of entry candles vs stdev of forward window bars
    # Only use expressions with enough valid data in both sets
    min_entry = int(n_entry * MIN_VALID_FRACTION)
    min_fw = int(n_fw * 0.1)  # looser threshold for the larger set

    entry_valid_counts = np.sum(~np.isnan(entry_matrix), axis=0)
    fw_valid_counts = np.sum(~np.isnan(fw_matrix), axis=0)

    with np.errstate(all='ignore'):
        entry_stdev = np.nanstd(entry_matrix, axis=0)
        fw_stdev = np.nanstd(fw_matrix, axis=0)

    # Build weights: fw_stdev / entry_stdev
    # High ratio = entry candles are tight, random bars spread = diagnostic
    # Guard against division by zero or near-zero entry stdev
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
            # Entry candles are effectively identical on this expression
            # That's maximally diagnostic — give it a high but capped weight
            weights[j] = 100.0
            usable += 1
            continue
        if fw_stdev[j] < 1e-10:
            # Both are flat — not useful
            continue

        weights[j] = fw_stdev[j] / entry_stdev[j]
        usable += 1

    print(f"  Usable expressions: {usable}/{n_exprs}")

    # ── Distribution of weights ──
    nonzero_weights = weights[weights > 0]
    if len(nonzero_weights) == 0:
        print("  ERROR: No expressions with positive weight")
        return

    print(f"\n  Weight distribution (non-zero only):")
    print(f"    Min:    {np.min(nonzero_weights):.4f}")
    print(f"    25th:   {np.percentile(nonzero_weights, 25):.4f}")
    print(f"    Median: {np.percentile(nonzero_weights, 50):.4f}")
    print(f"    75th:   {np.percentile(nonzero_weights, 75):.4f}")
    print(f"    Max:    {np.max(nonzero_weights):.4f}")
    print(f"    Expressions with weight > 2.0: {int(np.sum(nonzero_weights > 2.0))}")
    print(f"    Expressions with weight > 5.0: {int(np.sum(nonzero_weights > 5.0))}")
    print(f"    Expressions with weight > 10.0: {int(np.sum(nonzero_weights > 10.0))}")

    # ── Top 30 most discriminating expressions ──
    ranked = np.argsort(weights)[::-1]

    print(f"\n  Top 30 most discriminating expressions:")
    print(f"    {'Rank':>4} {'Expression':<45} {'Weight':>8} {'Entry SD':>10} {'FW SD':>10}")
    print(f"    {'-'*80}")
    for rank, j in enumerate(ranked[:30], 1):
        name = expr_names[j] if j < len(expr_names) else f"expr_{j}"
        print(f"    {rank:>4} {name:<45} {weights[j]:>8.3f} {entry_stdev[j]:>10.4f} {fw_stdev[j]:>10.4f}")

    # ── Bottom 30 (lowest non-zero weight) ──
    nonzero_indices = np.where(weights > 0)[0]
    bottom_ranked = nonzero_indices[np.argsort(weights[nonzero_indices])]

    print(f"\n  Bottom 30 (least discriminating, weight > 0):")
    print(f"    {'Rank':>4} {'Expression':<45} {'Weight':>8} {'Entry SD':>10} {'FW SD':>10}")
    print(f"    {'-'*80}")
    for rank, j in enumerate(bottom_ranked[:30], 1):
        name = expr_names[j] if j < len(expr_names) else f"expr_{j}"
        print(f"    {rank:>4} {name:<45} {weights[j]:>8.3f} {entry_stdev[j]:>10.4f} {fw_stdev[j]:>10.4f}")

    # ── Category breakdown: avg weight per expression category ──
    # Try to extract category from expression name prefix
    print(f"\n  Average weight by expression name prefix (top 20):")
    from collections import defaultdict
    cat_weights = defaultdict(list)
    for j in range(n_exprs):
        if weights[j] <= 0:
            continue
        name = expr_names[j] if j < len(expr_names) else ""
        # Rough category from name prefix
        parts = name.split("_")
        if len(parts) >= 2:
            cat = parts[0]
            # Some have 2-part prefixes
            if cat in ("ext", "nr", "ns", "spread", "slope", "vwap", "bb",
                       "range", "roc", "vol", "obv", "cmf", "macd", "aroon",
                       "close", "up", "cross", "retrace", "channel"):
                if len(parts) >= 3:
                    cat = parts[0] + "_" + parts[1]
        else:
            cat = name
        cat_weights[cat].append(weights[j])

    cat_avg = [(cat, np.mean(ws), len(ws)) for cat, ws in cat_weights.items()]
    cat_avg.sort(key=lambda x: x[1], reverse=True)
    print(f"    {'Category':<30} {'Avg Weight':>10} {'Count':>6}")
    print(f"    {'-'*50}")
    for cat, avg, count in cat_avg[:20]:
        print(f"    {cat:<30} {avg:>10.3f} {count:>6}")

    print(f"\n  ✓ Diagnostic complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
