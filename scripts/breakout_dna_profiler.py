"""
Breakout DNA Profiler — Quick feasibility test for consolidated breakout scan.

Takes 57 breakout examples (HTF, bullflag, base break), loads their expression
values at the scan bar, and checks which expressions show consensus across
all examples. If many expressions cluster tightly across all 3 subtypes,
one unified breakout scan is feasible.

Usage (on Dan's desktop):
    python scripts/breakout_dna_profiler.py

Requires:
    - 5yr OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
    - Expression series cache (local_runner/cache/expr_series/)
    - breakouts_test01.txt in repo root (or scripts/ dir)
"""

import os
import sys
import json
import pickle
import numpy as np
import pandas as pd
from collections import defaultdict

# ── Paths ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import ExprSeriesCache, load_manifest


# ══════════════════════════════════════════════════════════════
# JSON HELPER
# ══════════════════════════════════════════════════════════════

class NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types."""
    def default(self, obj):
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# ══════════════════════════════════════════════════════════════
# PARSE EXAMPLE FILE
# ══════════════════════════════════════════════════════════════

def parse_examples(filepath):
    """Parse breakout examples file.
    
    Format:
        HTF
        TSLA 12/5/2024
        ...
        
        BULLFLAG
        ANF 11/12/2023
        ...
        
        BASE BREAK
        ERO 11/25/2025
        ...
    
    Returns: list of dicts with ticker, entry_date, subtype
    """
    examples = []
    current_type = None
    
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            # Check if this is a type header
            upper = line.upper()
            if upper in ("HTF", "BULLFLAG", "BASE BREAK", "BASEBREAK"):
                current_type = upper.replace("BASEBREAK", "BASE BREAK")
                continue
            
            # Parse ticker + date
            parts = line.split()
            if len(parts) >= 2 and current_type:
                ticker = parts[0].upper()
                date_str = parts[1]
                
                # Parse date — handle M/D/YY and M/D/YYYY
                try:
                    dt = pd.to_datetime(date_str)
                    examples.append({
                        "ticker": ticker,
                        "entry_date": dt.strftime("%Y-%m-%d"),
                        "subtype": current_type,
                    })
                except Exception as e:
                    print(f"  SKIP: Can't parse date '{date_str}' for {ticker}: {e}")
    
    return examples


# ══════════════════════════════════════════════════════════════
# LOAD EXPRESSION VALUES AT SCAN BARS
# ══════════════════════════════════════════════════════════════

def load_example_expr_values(examples, universe_cache, expr_cache):
    """For each example, find the scan bar and pull expression values.
    
    Scan bar = last bar BEFORE entry_date (same as pyramid_grinder).
    
    Returns:
        values: np.array (n_valid_examples, n_expressions) — expr values at scan bars
        valid_examples: list of example dicts that were successfully loaded
        skipped: list of (ticker, reason) for examples that couldn't be loaded
    """
    n_exprs = expr_cache.n_expressions
    values_list = []
    valid_examples = []
    skipped = []
    
    for ex in examples:
        ticker = ex["ticker"]
        entry_date = ex["entry_date"]
        
        # Get OHLCV data
        df = universe_cache.get(ticker)
        if df is None:
            skipped.append((ticker, entry_date, ex["subtype"], "not in 5yr cache"))
            continue
        
        # Find scan bar: last bar before entry_date
        entry_dt = pd.to_datetime(entry_date)
        match = df[df["date"] < entry_dt]
        if len(match) == 0:
            skipped.append((ticker, entry_date, ex["subtype"], "no bar before entry_date"))
            continue
        scan_idx = match.index[-1]
        
        # Load expr cache for this ticker
        dates, data = expr_cache.get_ticker(ticker)
        if dates is None or data is None:
            skipped.append((ticker, entry_date, ex["subtype"], "not in expr cache"))
            continue
        
        # Map scan_idx to expr cache row
        # The expr cache dates are strings like "2024-01-15"
        # The 5yr cache df has a date column. scan_idx is the df index.
        scan_date = df.iloc[scan_idx]["date"]
        if hasattr(scan_date, "strftime"):
            scan_date_str = scan_date.strftime("%Y-%m-%d")
        else:
            scan_date_str = str(scan_date)[:10]
        
        # Find this date in the expr cache dates
        date_matches = np.where(dates == scan_date_str)[0]
        if len(date_matches) == 0:
            # Try matching by position — expr cache should have same bar count
            if scan_idx < len(data):
                expr_row = scan_idx
            else:
                skipped.append((ticker, entry_date, ex["subtype"], 
                              f"scan_date {scan_date_str} not in expr cache dates"))
                continue
        else:
            expr_row = date_matches[0]
        
        # Pull expression values at scan bar
        row_values = data[expr_row, :]
        values_list.append(row_values)
        valid_examples.append(ex)
    
    if values_list:
        values = np.array(values_list, dtype=np.float32)
    else:
        values = np.empty((0, n_exprs), dtype=np.float32)
    
    return values, valid_examples, skipped


# ══════════════════════════════════════════════════════════════
# CONSENSUS ANALYSIS
# ══════════════════════════════════════════════════════════════

def analyze_consensus(values, valid_examples, expr_names):
    """For each expression, check how tightly the examples cluster.
    
    Metrics per expression:
    1. Range ratio: (max - min) / median — how spread out are the examples?
       Lower = tighter clustering.
    2. IQR capture: what % of examples fall within the IQR (25th-75th) of the group?
       Higher = more consensus.
    3. Per-subtype agreement: do all 3 subtypes land in the same region?
    
    Returns: list of dicts sorted by consensus score
    """
    n_examples, n_exprs = values.shape
    
    # Build subtype indices
    subtypes = sorted(set(ex["subtype"] for ex in valid_examples))
    subtype_indices = {}
    for st in subtypes:
        subtype_indices[st] = [i for i, ex in enumerate(valid_examples) if ex["subtype"] == st]
    
    results = []
    
    for j in range(n_exprs):
        col = values[:, j]
        
        # Skip expressions with too many NaN
        valid_mask = ~np.isnan(col)
        n_valid = valid_mask.sum()
        if n_valid < n_examples * 0.7:  # need at least 70% coverage
            continue
        
        valid_vals = col[valid_mask]
        
        # Global stats
        median = np.median(valid_vals)
        q25 = np.percentile(valid_vals, 25)
        q75 = np.percentile(valid_vals, 75)
        iqr = q75 - q25
        
        if iqr == 0 or median == 0:
            continue
        
        # Range ratio: how tight is the spread relative to median?
        val_range = np.max(valid_vals) - np.min(valid_vals)
        range_ratio = val_range / abs(median) if median != 0 else float('inf')
        
        # What fraction of examples fall within a tight band?
        # Use the 10th-90th percentile range of the examples themselves
        p10 = np.percentile(valid_vals, 10)
        p90 = np.percentile(valid_vals, 90)
        in_band = np.sum((valid_vals >= p10) & (valid_vals <= p90)) / n_valid
        
        # Per-subtype: compute median per subtype, check if they agree (same side, similar magnitude)
        subtype_medians = {}
        subtype_coverage = {}
        for st, indices in subtype_indices.items():
            st_vals = col[indices]
            st_valid = st_vals[~np.isnan(st_vals)]
            if len(st_valid) > 0:
                subtype_medians[st] = float(np.median(st_valid))
                # What % of this subtype falls within the global IQR?
                subtype_coverage[st] = float(np.mean((st_valid >= q25) & (st_valid <= q75)))
        
        # Cross-subtype agreement: how close are the subtype medians?
        if len(subtype_medians) >= 2:
            st_med_vals = list(subtype_medians.values())
            st_spread = max(st_med_vals) - min(st_med_vals)
            cross_agreement = 1.0 - min(st_spread / (iqr + 1e-9), 1.0)
        else:
            cross_agreement = 0.0
        
        # Consensus score: combines tightness + cross-subtype agreement
        # Higher = better candidate for unified scan condition
        tightness = 1.0 / (1.0 + range_ratio)
        consensus_score = (tightness * 0.3) + (cross_agreement * 0.5) + (in_band * 0.2)
        
        results.append({
            "expr_idx": int(j),
            "name": expr_names[j] if j < len(expr_names) else f"expr_{j}",
            "consensus_score": round(float(consensus_score), 4),
            "cross_agreement": round(float(cross_agreement), 4),
            "tightness": round(float(tightness), 4),
            "range_ratio": round(float(range_ratio), 3),
            "median": round(float(median), 4),
            "q25": round(float(q25), 4),
            "q75": round(float(q75), 4),
            "n_valid": int(n_valid),
            "subtype_medians": {k: round(float(v), 4) for k, v in subtype_medians.items()},
            "subtype_iqr_coverage": {k: round(float(v), 3) for k, v in subtype_coverage.items()},
        })
    
    # Sort by consensus score descending
    results.sort(key=lambda x: x["consensus_score"], reverse=True)
    return results


# ══════════════════════════════════════════════════════════════
# DIVERGENCE ANALYSIS — where do subtypes disagree?
# ══════════════════════════════════════════════════════════════

def find_divergences(values, valid_examples, expr_names):
    """Find expressions where subtypes clearly separate.
    
    If an expression has high consensus within each subtype but the subtypes
    are in different regions, that expression distinguishes them — meaning
    a unified scan might struggle with that dimension.
    """
    n_examples, n_exprs = values.shape
    subtypes = sorted(set(ex["subtype"] for ex in valid_examples))
    subtype_indices = {}
    for st in subtypes:
        subtype_indices[st] = [i for i, ex in enumerate(valid_examples) if ex["subtype"] == st]
    
    divergences = []
    
    for j in range(n_exprs):
        col = values[:, j]
        valid_mask = ~np.isnan(col)
        if valid_mask.sum() < n_examples * 0.7:
            continue
        
        # Compute per-subtype stats
        st_stats = {}
        for st, indices in subtype_indices.items():
            st_vals = col[indices]
            st_valid = st_vals[~np.isnan(st_vals)]
            if len(st_valid) >= 3:
                st_stats[st] = {
                    "median": round(float(np.median(st_valid)), 4),
                    "q25": round(float(np.percentile(st_valid, 25)), 4),
                    "q75": round(float(np.percentile(st_valid, 75)), 4),
                    "std": round(float(np.std(st_valid)), 4),
                }
        
        if len(st_stats) < 2:
            continue
        
        # Check if subtype medians are far apart relative to within-subtype spread
        medians = [s["median"] for s in st_stats.values()]
        avg_std = np.mean([s["std"] for s in st_stats.values()])
        
        if avg_std == 0:
            continue
        
        separation = (max(medians) - min(medians)) / avg_std
        
        if separation > 1.5:  # subtypes are >1.5 std apart
            divergences.append({
                "expr_idx": int(j),
                "name": expr_names[j] if j < len(expr_names) else f"expr_{j}",
                "separation": round(float(separation), 3),
                "subtype_stats": st_stats,
            })
    
    divergences.sort(key=lambda x: x["separation"], reverse=True)
    return divergences


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 70)
    print("  BREAKOUT DNA PROFILER")
    print("  Testing consolidated breakout scan feasibility")
    print("=" * 70)
    
    # ── Find example file ──
    example_file = None
    for candidate in [
        os.path.join(REPO_ROOT, "breakouts_test01.txt"),
        os.path.join(SCRIPT_DIR, "breakouts_test01.txt"),
        os.path.join(REPO_ROOT, "data", "breakouts_test01.txt"),
    ]:
        if os.path.exists(candidate):
            example_file = candidate
            break
    
    if example_file is None:
        print("\n  ERROR: breakouts_test01.txt not found.")
        print("  Place it in the repo root or scripts/ directory.")
        return
    
    print(f"\n  Example file: {example_file}")
    
    # ── Parse examples ──
    examples = parse_examples(example_file)
    print(f"  Parsed {len(examples)} examples")
    
    subtypes = defaultdict(int)
    for ex in examples:
        subtypes[ex["subtype"]] += 1
    for st, count in sorted(subtypes.items()):
        print(f"    {st}: {count}")
    
    # ── Deduplicate ──
    seen = set()
    unique_examples = []
    dupes = 0
    for ex in examples:
        key = (ex["ticker"], ex["entry_date"])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        unique_examples.append(ex)
    if dupes:
        print(f"  Removed {dupes} duplicates → {len(unique_examples)} unique examples")
    examples = unique_examples
    
    # ── Load 5yr cache ──
    print("\n  Loading 5yr OHLCV cache...")
    cache_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(cache_path):
        print("  ERROR: No OHLCV cache found. Run cache_builder.py first.")
        return
    
    with open(cache_path, "rb") as f:
        universe_cache = pickle.load(f)
    print(f"  {len(universe_cache)} tickers loaded")
    
    # ── Load expr cache ──
    print("  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache not valid. Run expr_cache_builder.py --build")
        return
    
    expr_names = expr_cache.expr_names
    print(f"  {expr_cache.n_expressions} expressions")
    
    # ── Load expression values at scan bars ──
    print("\n  Loading expression values at scan bars...")
    values, valid_examples, skipped = load_example_expr_values(
        examples, universe_cache, expr_cache
    )
    
    print(f"  ✓ {len(valid_examples)} examples loaded successfully")
    if skipped:
        print(f"  ✗ {len(skipped)} skipped:")
        for ticker, date, subtype, reason in skipped:
            print(f"      {ticker} {date} ({subtype}): {reason}")
    
    # Free memory
    del universe_cache
    
    if len(valid_examples) < 10:
        print("\n  ERROR: Too few valid examples to analyze.")
        return
    
    # Recount subtypes
    valid_subtypes = defaultdict(int)
    for ex in valid_examples:
        valid_subtypes[ex["subtype"]] += 1
    print(f"\n  Valid examples by subtype:")
    for st, count in sorted(valid_subtypes.items()):
        print(f"    {st}: {count}")
    
    # ══════════════════════════════════════════════════════════
    # CONSENSUS ANALYSIS
    # ══════════════════════════════════════════════════════════
    
    print("\n" + "=" * 70)
    print("  CONSENSUS ANALYSIS — Top expressions shared across all subtypes")
    print("=" * 70)
    
    results = analyze_consensus(values, valid_examples, expr_names)
    
    print(f"\n  {len(results)} expressions analyzed (after NaN/zero filtering)")
    
    # Show top 50
    print(f"\n  TOP 50 by consensus score:")
    print(f"  {'Rank':<5} {'Expression':<45} {'Score':<7} {'XAgree':<8} {'Tight':<7} {'RngRatio':<9} {'Median':<10} ", end="")
    for st in sorted(valid_subtypes.keys()):
        abbrev = st[:3]
        print(f" {abbrev+'Med':<10}", end="")
    print()
    print("  " + "─" * 140)
    
    for i, r in enumerate(results[:50]):
        print(f"  {i+1:<5} {r['name']:<45} {r['consensus_score']:<7.4f} {r['cross_agreement']:<8.4f} "
              f"{r['tightness']:<7.4f} {r['range_ratio']:<9.3f} {r['median']:<10.4f} ", end="")
        for st in sorted(valid_subtypes.keys()):
            med = r['subtype_medians'].get(st, float('nan'))
            print(f" {med:<10.4f}", end="")
        print()
    
    # ══════════════════════════════════════════════════════════
    # DIVERGENCE ANALYSIS
    # ══════════════════════════════════════════════════════════
    
    print("\n" + "=" * 70)
    print("  DIVERGENCE ANALYSIS — Where subtypes disagree most")
    print("=" * 70)
    
    divergences = find_divergences(values, valid_examples, expr_names)
    
    print(f"\n  {len(divergences)} expressions with subtype separation > 1.5 std")
    
    # Show top 30 divergences
    print(f"\n  TOP 30 divergences:")
    print(f"  {'Rank':<5} {'Expression':<45} {'Separation':<12}", end="")
    for st in sorted(valid_subtypes.keys()):
        abbrev = st[:3]
        print(f" {abbrev+'Med':<10} {abbrev+'Std':<10}", end="")
    print()
    print("  " + "─" * 140)
    
    for i, d in enumerate(divergences[:30]):
        print(f"  {i+1:<5} {d['name']:<45} {d['separation']:<12.3f}", end="")
        for st in sorted(valid_subtypes.keys()):
            stats = d['subtype_stats'].get(st, {})
            med = stats.get('median', float('nan'))
            std = stats.get('std', float('nan'))
            print(f" {med:<10.4f} {std:<10.4f}", end="")
        print()
    
    # ══════════════════════════════════════════════════════════
    # SUMMARY VERDICT
    # ══════════════════════════════════════════════════════════
    
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    
    # Count expressions with high consensus
    high_consensus = [r for r in results if r["consensus_score"] >= 0.6]
    medium_consensus = [r for r in results if 0.4 <= r["consensus_score"] < 0.6]
    high_divergence = [d for d in divergences if d["separation"] >= 3.0]
    
    print(f"\n  High consensus expressions (≥0.6):   {len(high_consensus)}")
    print(f"  Medium consensus (0.4-0.6):          {len(medium_consensus)}")
    print(f"  High divergence expressions (≥3.0σ): {len(high_divergence)}")
    
    if len(high_consensus) >= 20 and len(high_divergence) < 50:
        print("\n  ✓ PROMISING — Many shared traits, limited divergence.")
        print("    A consolidated breakout scan looks feasible.")
    elif len(high_consensus) >= 10:
        print("\n  ~ MIXED — Some shared traits but also significant divergence.")
        print("    Consolidated scan possible but may need subtype-aware conditions.")
    else:
        print("\n  ✗ CHALLENGING — Few shared traits across subtypes.")
        print("    Subtypes may need separate scans.")
    
    # ── Save full results to JSON ──
    output_path = os.path.join(CACHE_DIR, "breakout_dna_results.json")
    output = {
        "n_examples": len(valid_examples),
        "subtypes": dict(valid_subtypes),
        "skipped": [{"ticker": t, "date": d, "subtype": s, "reason": r} 
                    for t, d, s, r in skipped],
        "all_consensus": results,
        "all_divergences": divergences,
        "summary": {
            "high_consensus_count": len(high_consensus),
            "medium_consensus_count": len(medium_consensus),
            "high_divergence_count": len(high_divergence),
            "total_analyzed": len(results),
            "total_divergent": len(divergences),
        }
    }
    
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, cls=NumpyEncoder)
    print(f"\n  Full results saved to: {output_path}")
    
    # ── Mirror to Railway ──
    try:
        from file_mirror import mirror_file
        mirror_file(output_path)
    except Exception as e:
        print(f"  [mirror] WARNING: Could not mirror to Railway: {e}")


if __name__ == "__main__":
    main()
