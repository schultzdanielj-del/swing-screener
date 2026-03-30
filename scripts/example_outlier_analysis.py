"""
Example Outlier Analysis — Leave-one-out range impact scoring.

For each example in the library, removes it and recomputes expression
ranges for all conditions used in the latest grind. Scores each example
by how much its removal would tighten the scan (reduce total range width).

Examples that disproportionately widen critical condition ranges are
suspects — possibly marginal quality setups that got approved during vetting.

Usage:
    python scripts/example_outlier_analysis.py --setup dtss
    python scripts/example_outlier_analysis.py --setup dtss --grind-file <path_on_railway>

Output:
    data/example_outlier_dtss_<timestamp>.json  (local + mirrored to Railway)
"""

import os
import sys
import json
import argparse
import numpy as np
import requests
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache
from pyramid_grinder import load_example_data, load_daily_cache
from file_mirror import mirror_file

API_BASE = "https://web-production-e3025.up.railway.app"


def find_latest_grind(setup_type):
    """Find the newest pyramid grind file on Railway for this setup."""
    prefix = f"local_runner/cache/pyramid_{setup_type}_mp_"
    resp = requests.get(f"{API_BASE}/api/v2/files", params={"prefix": prefix}, timeout=30)
    resp.raise_for_status()
    files = resp.json()["files"]
    # Exclude blackout grinds and prefilter experiments
    files = [f for f in files if "blackout" not in f["path"]]
    if not files:
        raise FileNotFoundError(f"No grind files found with prefix: {prefix}")
    # Sort by created_at descending
    files.sort(key=lambda x: x["created_at"], reverse=True)
    return files[0]["path"]


def load_grind_result(path):
    """Load a grind result from Railway file mirror."""
    resp = requests.get(f"{API_BASE}/api/v2/files/{path}", timeout=60)
    resp.raise_for_status()
    return resp.json()


def compute_range_width(example_matrix, expr_idx, exclude_idx=None):
    """Compute the range width for one expression across examples.
    
    Returns (low, high, width) with 5% margin applied (same as grinder).
    If all remaining values are NaN, returns None.
    """
    vals = example_matrix[:, expr_idx].copy()
    if exclude_idx is not None:
        vals = np.delete(vals, exclude_idx)
    valid = vals[~np.isnan(vals)]
    if len(valid) == 0:
        return None
    ex_min, ex_max = float(np.min(valid)), float(np.max(valid))
    margin = (ex_max - ex_min) * 0.05
    low = ex_min - margin
    high = ex_max + margin
    width = high - low
    return (low, high, width)


def run_outlier_analysis(setup_type, grind_path=None):
    """Main analysis: leave-one-out range impact for every example."""
    
    print(f"═══ Example Outlier Analysis: {setup_type.upper()} ═══\n")
    
    # 1. Load grind result
    if grind_path is None:
        grind_path = find_latest_grind(setup_type)
    print(f"Grind: {grind_path}")
    grind = load_grind_result(grind_path)
    conditions = grind["all_conditions"]
    print(f"Conditions: {len(conditions)}")
    
    # Get condition expression names
    cond_names = set(c["expr"] for c in conditions)
    print(f"Unique expressions in conditions: {len(cond_names)}")
    
    # 2. Load examples + expression cache
    print("\nLoading daily cache...")
    universe_cache = load_daily_cache()
    print(f"  {len(universe_cache)} tickers")
    
    print("Loading examples...")
    example_dfs = load_example_data(setup_type, universe_cache)
    n_examples = len(example_dfs)
    print(f"  {n_examples} examples loaded")
    
    print("Loading expression library...")
    all_expressions = generate_all()
    
    # Filter to only expressions used in conditions
    expr_lookup = {e["name"]: e for e in all_expressions}
    cond_expressions = [expr_lookup[name] for name in cond_names if name in expr_lookup]
    print(f"  {len(cond_expressions)} condition expressions found in library")
    
    missing = cond_names - set(e["name"] for e in cond_expressions)
    if missing:
        print(f"  WARNING: {len(missing)} condition expressions not found: {missing}")
    
    # 3. Build example matrix for condition expressions only
    print("\nLoading expr cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not valid. Run expr_cache_builder.py --build")
    
    n_expr = len(cond_expressions)
    example_matrix = np.full((n_examples, n_expr), np.nan)
    
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    expr_name_list = [e["name"] for e in cond_expressions]
    expr_to_cache_col = [cache_name_to_idx.get(name) for name in expr_name_list]
    
    for i, ex in enumerate(example_dfs):
        if ex["scan_idx"] is None:
            continue
        ticker = ex["ticker"]
        scan_idx = ex["scan_idx"]
        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            print(f"  WARNING: {ticker} not in expr cache")
            continue
        if scan_idx >= len(data):
            print(f"  WARNING: {ticker} scan_idx {scan_idx} >= cached bars {len(data)}")
            continue
        cached_row = data[scan_idx, :]
        for j, cache_col in enumerate(expr_to_cache_col):
            if cache_col is not None and cache_col < len(cached_row):
                val = cached_row[cache_col]
                if not np.isnan(val):
                    example_matrix[i, j] = val
    
    n_valid = int(np.sum(~np.isnan(example_matrix)))
    print(f"  Matrix: {n_examples} examples × {n_expr} expressions, "
          f"{n_valid:,} values ({n_valid / max(n_examples * n_expr, 1) * 100:.1f}% fill)")
    
    # 4. Compute baseline ranges (all examples)
    print("\nComputing baseline ranges...")
    baseline_widths = {}
    for j, name in enumerate(expr_name_list):
        result = compute_range_width(example_matrix, j)
        if result is not None:
            baseline_widths[name] = result[2]  # width only
    print(f"  {len(baseline_widths)} expressions with valid ranges")
    
    total_baseline_width = sum(baseline_widths.values())
    
    # 5. Leave-one-out: for each example, compute total width reduction
    print(f"\nRunning leave-one-out for {n_examples} examples...")
    
    example_scores = []
    for i, ex in enumerate(example_dfs):
        per_expr_impact = {}
        total_width_without = 0.0
        
        for j, name in enumerate(expr_name_list):
            if name not in baseline_widths:
                continue
            result = compute_range_width(example_matrix, j, exclude_idx=i)
            if result is not None:
                width_without = result[2]
                total_width_without += width_without
                reduction = baseline_widths[name] - width_without
                if reduction > 0:
                    per_expr_impact[name] = {
                        "baseline_width": round(float(baseline_widths[name]), 6),
                        "width_without": round(float(width_without), 6),
                        "reduction": round(float(reduction), 6),
                        "pct_reduction": round(float(reduction / baseline_widths[name] * 100), 2),
                    }
            else:
                total_width_without += baseline_widths.get(name, 0)
        
        total_reduction = total_baseline_width - total_width_without
        pct_reduction = total_reduction / total_baseline_width * 100 if total_baseline_width > 0 else 0
        
        # Count how many conditions this example defines the min or max for
        n_boundary = 0
        for j, name in enumerate(expr_name_list):
            vals = example_matrix[:, j]
            valid = vals[~np.isnan(vals)]
            if len(valid) == 0:
                continue
            v = example_matrix[i, j]
            if np.isnan(v):
                continue
            if v == np.min(valid) or v == np.max(valid):
                n_boundary += 1
        
        score = {
            "idx": i,
            "ticker": ex["ticker"],
            "entry_date": ex["entry_date"],
            "total_width_reduction": round(float(total_reduction), 4),
            "pct_total_reduction": round(float(pct_reduction), 2),
            "n_conditions_impacted": len(per_expr_impact),
            "n_boundary_conditions": n_boundary,
            "top_impacts": sorted(
                per_expr_impact.items(),
                key=lambda x: x[1]["reduction"],
                reverse=True
            )[:10],
        }
        example_scores.append(score)
        
        if (i + 1) % 10 == 0 or i == n_examples - 1:
            print(f"  {i + 1}/{n_examples} done")
    
    # 6. Rank by total width reduction and flag outliers
    example_scores.sort(key=lambda x: x["total_width_reduction"], reverse=True)
    
    reductions = [s["total_width_reduction"] for s in example_scores]
    mean_red = float(np.mean(reductions))
    std_red = float(np.std(reductions))
    threshold = mean_red + 2 * std_red
    
    for s in example_scores:
        s["z_score"] = round(float((s["total_width_reduction"] - mean_red) / std_red), 2) if std_red > 0 else 0
        s["flagged"] = s["total_width_reduction"] > threshold
        # Convert top_impacts from list of tuples to list of dicts for JSON
        s["top_impacts"] = [
            {"expr": name, **impact} for name, impact in s["top_impacts"]
        ]
    
    n_flagged = sum(1 for s in example_scores if s["flagged"])
    
    # 7. Summary
    print(f"\n═══ RESULTS ═══")
    print(f"Total baseline width: {total_baseline_width:.2f}")
    print(f"Mean reduction per example: {mean_red:.4f}")
    print(f"Std: {std_red:.4f}")
    print(f"Outlier threshold (mean + 2σ): {threshold:.4f}")
    print(f"Flagged outliers: {n_flagged}")
    print()
    
    for rank, s in enumerate(example_scores[:10]):
        flag = " *** OUTLIER ***" if s["flagged"] else ""
        print(f"  #{rank+1}: {s['ticker']} ({s['entry_date']}) — "
              f"reduction={s['total_width_reduction']:.4f} "
              f"({s['pct_total_reduction']:.1f}%), "
              f"z={s['z_score']}, "
              f"boundary={s['n_boundary_conditions']}, "
              f"impacted={s['n_conditions_impacted']}"
              f"{flag}")
    
    # 8. Build output
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "setup_type": setup_type,
        "timestamp": timestamp,
        "grind_file": grind_path,
        "n_examples": n_examples,
        "n_conditions": len(conditions),
        "total_baseline_width": round(float(total_baseline_width), 4),
        "mean_reduction": round(float(mean_red), 6),
        "std_reduction": round(float(std_red), 6),
        "outlier_threshold": round(float(threshold), 6),
        "n_flagged": n_flagged,
        "examples_ranked": example_scores,
    }
    
    # 9. Save + mirror
    out_dir = os.path.join(REPO_ROOT, "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"example_outlier_{setup_type}_{timestamp}.json")
    
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nSaved: {out_path}")
    
    mirror_file(out_path)
    
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Leave-one-out example outlier analysis")
    parser.add_argument("--setup", required=True, help="Setup type (e.g. dtss)")
    parser.add_argument("--grind-file", default=None, help="Specific grind file path on Railway (default: latest)")
    args = parser.parse_args()
    
    run_outlier_analysis(args.setup, args.grind_file)
