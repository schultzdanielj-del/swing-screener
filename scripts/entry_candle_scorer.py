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

    print("\n  ✓ Centroid ready.")
    print("=" * 60)


if __name__ == "__main__":
    main()
