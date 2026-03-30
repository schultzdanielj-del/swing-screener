"""
Expression discriminative power analysis.

For every expression in the library, computes:
  - Example range (min-max across examples + 5% margin)
  - What % of the universe falls within that range
  - Classification: useful (<85%), marginal (85-95%), junk (>95%)

Usage (from repo root):
    python scripts/analyze_expression_power.py --setup dtss

Outputs: data/expression_power_{setup}.json (auto-mirrored to Railway)
"""

import os
import sys
import json
import time
import argparse
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache
from pyramid_grinder import load_daily_cache, load_example_data, compute_example_ranges


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", required=True, help="Setup type (e.g. dtss)")
    parser.add_argument("--threshold", type=float, default=0.85,
                        help="Pass rate above which expression is 'junk' (default: 0.85)")
    args = parser.parse_args()

    t0 = time.time()

    # Load data
    print("Loading OHLCV cache...")
    universe_cache = load_daily_cache()
    print(f"  {len(universe_cache)} tickers")

    print("Loading examples...")
    example_dfs = load_example_data(args.setup, universe_cache)
    print(f"  {len(example_dfs)} examples")

    print("Loading expressions...")
    all_expressions = generate_all()
    print(f"  {len(all_expressions)} expressions")

    print("Loading expr cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("ERROR: expr cache not found")
        return

    # Filter examples to those in cache
    cached_tickers = expr_cache.get_available_tickers()
    example_dfs = [ex for ex in example_dfs
                   if ex["ticker"] in cached_tickers
                   and ex["scan_idx"] < expr_cache.get_ticker_bar_count(ex["ticker"])]
    print(f"  {len(example_dfs)} examples after cache filter")

    # Compute example ranges for ALL expressions
    print("Computing example ranges...")
    example_ranges, example_matrix = compute_example_ranges(
        example_dfs, all_expressions, expr_cache=expr_cache)
    print(f"  {len(example_ranges)} expressions have valid ranges")

    # Now compute universe pass rate for each expression with a valid range.
    # Use the matrix_builder's universe matrix (D1 snapshot) for speed.
    # This is what D1 tier uses — pass rate on today's snapshot.
    print("Loading universe matrix...")
    from matrix_builder import get_universe_matrix
    uni_data = get_universe_matrix()
    uni_matrix = uni_data["universe_matrix"]
    uni_tickers = uni_data["universe_tickers"]
    uni_expr_names = uni_data["expr_names"]
    n_universe = len(uni_tickers)
    print(f"  {n_universe} tickers x {len(uni_expr_names)} expressions")

    # Map expression names to universe matrix columns
    uni_name_to_col = {name: i for i, name in enumerate(uni_expr_names)}

    # Also compute pass rate on the FULL full history (all ticker-day rows)
    # This is what historical tiers see.
    # Too expensive to do for all 15k expressions on full history.
    # Instead, sample: use the last bar per ticker (same as D1 matrix).
    # D1 pass rate is a reasonable proxy.

    print("Computing pass rates...")
    results = []
    expr_name_list = [e["name"] for e in all_expressions]
    expr_cat_list = [e["category"] for e in all_expressions]

    for j, expr in enumerate(all_expressions):
        name = expr["name"]
        cat = expr["category"]

        if name not in example_ranges:
            results.append({
                "name": name,
                "category": cat,
                "has_range": False,
                "reason": "NaN in examples",
            })
            continue

        low, high = example_ranges[name]
        width = high - low

        # Get universe column
        col = uni_name_to_col.get(name)
        if col is None:
            results.append({
                "name": name,
                "category": cat,
                "has_range": True,
                "low": round(float(low), 6),
                "high": round(float(high), 6),
                "width": round(float(width), 6),
                "pass_rate": None,
                "reason": "not in universe matrix",
            })
            continue

        vals = uni_matrix[:, col]
        passes = ((vals >= low) & (vals <= high)) | np.isnan(vals)
        pass_rate = float(np.sum(passes)) / n_universe

        results.append({
            "name": name,
            "category": cat,
            "has_range": True,
            "low": round(float(low), 6),
            "high": round(float(high), 6),
            "width": round(float(width), 6),
            "pass_rate": round(pass_rate, 6),
        })

    # Classify
    threshold = args.threshold
    with_range = [r for r in results if r.get("has_range")]
    with_rate = [r for r in with_range if r.get("pass_rate") is not None]

    junk = [r for r in with_rate if r["pass_rate"] >= 0.95]
    marginal = [r for r in with_rate if 0.85 <= r["pass_rate"] < 0.95]
    useful = [r for r in with_rate if r["pass_rate"] < 0.85]

    # Summary by category
    cat_summary = {}
    for r in with_rate:
        cat = r["category"]
        if cat not in cat_summary:
            cat_summary[cat] = {"total": 0, "useful": 0, "marginal": 0, "junk": 0,
                                "avg_pass_rate": 0, "avg_width": 0}
        cat_summary[cat]["total"] += 1
        cat_summary[cat]["avg_pass_rate"] += r["pass_rate"]
        cat_summary[cat]["avg_width"] += r["width"]
        if r["pass_rate"] >= 0.95:
            cat_summary[cat]["junk"] += 1
        elif r["pass_rate"] >= 0.85:
            cat_summary[cat]["marginal"] += 1
        else:
            cat_summary[cat]["useful"] += 1

    for cat in cat_summary:
        n = cat_summary[cat]["total"]
        if n > 0:
            cat_summary[cat]["avg_pass_rate"] = round(cat_summary[cat]["avg_pass_rate"] / n, 4)
            cat_summary[cat]["avg_width"] = round(cat_summary[cat]["avg_width"] / n, 2)

    elapsed = time.time() - t0

    # Print summary
    print(f"\n{'='*80}")
    print(f"EXPRESSION DISCRIMINATIVE POWER — {args.setup.upper()}")
    print(f"{'='*80}")
    print(f"Total expressions: {len(all_expressions)}")
    print(f"With valid range: {len(with_range)}")
    print(f"With pass rate: {len(with_rate)}")
    print(f"")
    print(f"Useful (<85% pass): {len(useful)} ({len(useful)/len(with_rate)*100:.0f}%)")
    print(f"Marginal (85-95%):  {len(marginal)} ({len(marginal)/len(with_rate)*100:.0f}%)")
    print(f"Junk (>95% pass):   {len(junk)} ({len(junk)/len(with_rate)*100:.0f}%)")
    print(f"")
    print(f"If we pre-filter at 85%: {len(useful)} candidates (drop {len(marginal)+len(junk)})")
    print(f"If we pre-filter at 95%: {len(useful)+len(marginal)} candidates (drop {len(junk)})")

    print(f"\n{'='*80}")
    print(f"BY CATEGORY (sorted by avg pass rate, worst first)")
    print(f"{'='*80}")
    print(f"{'Category':<25} {'Total':>6} {'Useful':>6} {'Marg':>6} {'Junk':>6} {'AvgPR':>7} {'AvgW':>10}")
    print("-" * 80)
    for cat, s in sorted(cat_summary.items(), key=lambda x: -x[1]["avg_pass_rate"]):
        print(f"{cat:<25} {s['total']:>6} {s['useful']:>6} {s['marginal']:>6} {s['junk']:>6} "
              f"{s['avg_pass_rate']:>6.1%} {s['avg_width']:>10.1f}")

    print(f"\nTop 30 worst (highest pass rate):")
    worst = sorted(with_rate, key=lambda r: -r["pass_rate"])[:30]
    for r in worst:
        print(f"  {r['pass_rate']:>5.1%}  w={r['width']:>10.1f}  {r['category']:<25} {r['name']}")

    # Save
    output = {
        "setup_type": args.setup,
        "n_examples": len(example_dfs),
        "n_expressions": len(all_expressions),
        "n_with_range": len(with_range),
        "n_with_rate": len(with_rate),
        "n_useful": len(useful),
        "n_marginal": len(marginal),
        "n_junk": len(junk),
        "threshold": threshold,
        "category_summary": cat_summary,
        "expressions": sorted(with_rate, key=lambda r: -r["pass_rate"]),
        "elapsed_s": round(elapsed, 1),
    }

    os.makedirs("data", exist_ok=True)
    out_path = f"data/expression_power_{args.setup}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved: {out_path}")

    from file_mirror import mirror_file
    mirror_file(out_path)


if __name__ == "__main__":
    main()
