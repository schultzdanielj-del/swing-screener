"""
Exit Grinder — Step 6 of the Analysis System

Brute force ~2,100 post-signal expressions against validated examples'
forward paths to find optimal TA-driven exit conditions.

Scoring:
    Primary:   Floor capture efficiency (worst example's captured/MFE)
    Secondary: Median capture efficiency
    Constraint: Every example must capture > 0 ADR

Benchmark: Entry bar high → exit bar close = captured move (in ADR)

Usage:
    python scripts/exit_grinder.py --setup dtss [--max-forward 120] [--top 50]
    python scripts/exit_grinder.py --setup dtss --profile-only   # just show MFE stats

Requires:
    - Railway API (examples + OHLCV data)
    - scripts/exit_expressions.py (expression library)
    - scripts/exit_compute.py (expression compute engine)
"""

import os
import sys
import json
import time
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.exit_expressions import generate_exit_expressions, generate_all_exit_expressions
from scripts.exit_compute import ExitExprEngine

API_BASE = "https://web-production-e3025.up.railway.app"

# Setup direction mapping
SETUP_DIRECTION = {
    "dtss": "short",
    "3-4db": "short",
    "htf": "long",
}


# ═══════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════

def load_examples(setup_type):
    """Load examples from Railway API with full OHLCV."""
    import requests
    
    resp = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=30)
    data = resp.json()
    examples = data.get("examples", [])
    
    result = []
    for ex in examples:
        eid = ex["id"]
        ticker = ex["ticker"]
        entry_date = ex.get("entryDate") or ex.get("entry_date")
        if not entry_date:
            print(f"  SKIP {ticker} (id={eid}): no entry date")
            continue
        
        r = requests.get(f"{API_BASE}/api/ohlcv/local/{setup_type}/{eid}", timeout=30)
        candles = r.json().get("candles", [])
        if not candles:
            print(f"  SKIP {ticker} (id={eid}): no OHLCV data")
            continue
        
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        
        # Find entry bar index
        entry_dt = pd.to_datetime(entry_date)
        entry_matches = df[df["date"] == entry_dt]
        if len(entry_matches) == 0:
            # Try finding closest date on or after
            entry_matches = df[df["date"] >= entry_dt]
            if len(entry_matches) == 0:
                print(f"  SKIP {ticker} (id={eid}): entry date {entry_date} not in OHLCV")
                continue
        
        entry_idx = entry_matches.index[0]
        
        result.append({
            "id": eid,
            "ticker": ticker,
            "entry_date": entry_date,
            "entry_idx": entry_idx,
            "df": df,
        })
    
    return result


def load_spy_data():
    """Load SPY OHLCV from Railway for relative strength calculations."""
    import requests
    try:
        resp = requests.get(f"{API_BASE}/api/ohlcv/universe/SPY", timeout=30)
        if resp.status_code != 200:
            return None
        candles = resp.json().get("candles", [])
        if not candles:
            return None
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# MFE PROFILING
# ═══════════════════════════════════════════════════════════

def profile_examples(examples, direction, max_forward):
    """Profile MFE for each example. Returns list of profile dicts."""
    profiles = []
    
    for ex in examples:
        df = ex["df"]
        entry_idx = ex["entry_idx"]
        n_forward = min(max_forward, len(df) - entry_idx)
        
        if n_forward < 5:
            print(f"  SKIP {ex['ticker']}: only {n_forward} forward bars")
            continue
        
        entry_high = df["high"].iloc[entry_idx]
        entry_low = df["low"].iloc[entry_idx]
        
        fwd_slice = slice(entry_idx, entry_idx + n_forward)
        fwd_c = df["close"].values[fwd_slice]
        fwd_l = df["low"].values[fwd_slice]
        fwd_h = df["high"].values[fwd_slice]
        
        # ADR at entry
        adr_series = (df["high"] - df["low"]).rolling(14).mean()
        adr_at_entry = adr_series.iloc[entry_idx]
        if adr_at_entry <= 0 or np.isnan(adr_at_entry):
            adr_at_entry = (df["high"] - df["low"]).iloc[max(0, entry_idx-14):entry_idx].mean()
        
        if direction == 'short':
            # MFE = entry high - lowest price reached
            mfe_by_close = entry_high - np.minimum.accumulate(fwd_c)
            mfe_by_low = entry_high - np.minimum.accumulate(fwd_l)
            max_mfe_close = np.max(mfe_by_close)
            max_mfe_low = np.max(mfe_by_low)
            mfe_bar_close = np.argmax(mfe_by_close)
            mfe_bar_low = np.argmax(mfe_by_low)
        else:
            mfe_by_close = np.maximum.accumulate(fwd_c) - entry_low
            mfe_by_low = np.maximum.accumulate(fwd_h) - entry_low
            max_mfe_close = np.max(mfe_by_close)
            max_mfe_low = np.max(mfe_by_low)
            mfe_bar_close = np.argmax(mfe_by_close)
            mfe_bar_low = np.argmax(mfe_by_low)
        
        profiles.append({
            "id": ex["id"],
            "ticker": ex["ticker"],
            "entry_date": ex["entry_date"],
            "entry_idx": entry_idx,
            "n_forward": n_forward,
            "adr_at_entry": adr_at_entry,
            "mfe_close_raw": max_mfe_close,
            "mfe_close_adr": max_mfe_close / adr_at_entry if adr_at_entry > 0 else 0,
            "mfe_bar_close": int(mfe_bar_close),
            "mfe_low_raw": max_mfe_low,
            "mfe_low_adr": max_mfe_low / adr_at_entry if adr_at_entry > 0 else 0,
            "mfe_bar_low": int(mfe_bar_low),
            "df": ex["df"],
        })
    
    return profiles


def print_profile_summary(profiles):
    """Print MFE summary table."""
    print(f"\n{'='*80}")
    print(f"MFE PROFILE — {len(profiles)} examples")
    print(f"{'='*80}")
    print(f"{'Ticker':<8} {'Entry Date':<12} {'MFE Close':>10} {'Bar':>5} {'MFE Low':>10} {'Bar':>5} {'ADR':>8}")
    print(f"{'-'*8} {'-'*12} {'-'*10} {'-'*5} {'-'*10} {'-'*5} {'-'*8}")
    
    for p in sorted(profiles, key=lambda x: -x["mfe_close_adr"]):
        print(f"{p['ticker']:<8} {p['entry_date']:<12} "
              f"{p['mfe_close_adr']:>9.1f}x {p['mfe_bar_close']:>5d} "
              f"{p['mfe_low_adr']:>9.1f}x {p['mfe_bar_low']:>5d} "
              f"${p['adr_at_entry']:>7.2f}")
    
    mfe_adrs = [p["mfe_close_adr"] for p in profiles]
    mfe_bars = [p["mfe_bar_close"] for p in profiles]
    print(f"\n  MFE (close, ADR): min={min(mfe_adrs):.1f}x  median={np.median(mfe_adrs):.1f}x  "
          f"mean={np.mean(mfe_adrs):.1f}x  max={max(mfe_adrs):.1f}x")
    print(f"  MFE bar:          min={min(mfe_bars)}  median={int(np.median(mfe_bars))}  "
          f"mean={np.mean(mfe_bars):.0f}  max={max(mfe_bars)}")


# ═══════════════════════════════════════════════════════════
# EXIT GRINDER CORE
# ═══════════════════════════════════════════════════════════

def compute_example_exit_matrix(profile, expressions, direction, spy_df=None):
    """Compute all exit expressions for one example's forward path.
    
    Returns: numpy array of shape (n_forward, n_expressions)
    """
    df = profile["df"]
    entry_idx = profile["entry_idx"]
    n_forward = profile["n_forward"]
    
    engine = ExitExprEngine(
        df, entry_idx, 
        direction=direction,
        spy_df=spy_df,
        max_forward=n_forward
    )
    
    n_expr = len(expressions)
    matrix = np.full((engine.n_forward, n_expr), np.nan)
    
    for j, expr in enumerate(expressions):
        try:
            series = engine.compute(expr["compute"])
            if len(series) == engine.n_forward:
                matrix[:, j] = series
        except Exception as e:
            pass  # Leave as NaN
    
    return matrix


def find_exit_conditions(profiles, expressions, direction, spy_df=None, 
                          n_thresholds=20, top_n=50):
    """Grind all expressions against all examples to find optimal exit conditions.
    
    For each expression:
        1. Compute series across all examples' forward paths
        2. Try multiple threshold values
        3. For each threshold, find the first bar where condition triggers per example
        4. Compute captured move at that bar
        5. Score by floor capture efficiency
    
    Returns list of top exit conditions sorted by score.
    """
    n_examples = len(profiles)
    n_expr = len(expressions)
    
    print(f"\nComputing exit matrices for {n_examples} examples × {n_expr} expressions...")
    
    # Build all example matrices
    all_matrices = []
    all_mfe_adr = []
    all_entry_high = []
    all_adr = []
    all_fwd_close = []
    
    for i, prof in enumerate(profiles):
        t0 = time.time()
        matrix = compute_example_exit_matrix(prof, expressions, direction, spy_df)
        all_matrices.append(matrix)
        all_mfe_adr.append(prof["mfe_close_adr"])
        all_entry_high.append(prof["df"]["high"].iloc[prof["entry_idx"]])
        all_adr.append(prof["adr_at_entry"])
        all_fwd_close.append(
            prof["df"]["close"].values[prof["entry_idx"]:prof["entry_idx"] + prof["n_forward"]]
        )
        elapsed = time.time() - t0
        print(f"  [{i+1}/{n_examples}] {prof['ticker']:<8} {matrix.shape[0]:>4} bars × {matrix.shape[1]:>5} exprs  ({elapsed:.1f}s)")
    
    print(f"\nScoring {n_expr} expressions × {n_thresholds} thresholds...")
    
    # For each expression, try thresholds and score
    results = []
    
    for j in range(n_expr):
        expr = expressions[j]
        
        # Collect all values across all examples for this expression
        all_vals = []
        for matrix in all_matrices:
            col = matrix[:, j]
            valid = col[~np.isnan(col)]
            all_vals.extend(valid.tolist())
        
        if len(all_vals) < n_examples:
            continue
        
        # Generate threshold candidates from percentiles
        all_vals_arr = np.array(all_vals)
        percentiles = np.linspace(5, 95, n_thresholds)
        thresholds = np.percentile(all_vals_arr, percentiles)
        thresholds = np.unique(thresholds)
        
        for thresh in thresholds:
            # For each example, find first bar where expression crosses threshold
            capture_efficiencies = []
            captured_adrs = []
            all_captured_positive = True
            
            for i, (matrix, mfe_adr, entry_h, adr_val, fwd_c) in enumerate(
                zip(all_matrices, all_mfe_adr, all_entry_high, all_adr, all_fwd_close)
            ):
                col = matrix[:, j]
                
                # Find first bar where value crosses threshold
                # For "above" threshold: first bar where val >= thresh
                # Try both directions
                above_mask = col >= thresh
                
                exit_bar = None
                # Skip bar 0 (entry bar itself)
                for b in range(1, len(col)):
                    if above_mask[b] and not np.isnan(col[b]):
                        exit_bar = b
                        break
                
                if exit_bar is None:
                    # Condition never triggered — this example gets 0 capture
                    all_captured_positive = False
                    capture_efficiencies.append(0.0)
                    captured_adrs.append(0.0)
                    continue
                
                # Captured move: entry high → exit bar close
                if direction == 'short':
                    captured_raw = entry_h - fwd_c[exit_bar]
                else:
                    captured_raw = fwd_c[exit_bar] - entry_h  # entry_low for longs TODO
                
                captured_adr = captured_raw / adr_val if adr_val > 0 else 0
                captured_adrs.append(captured_adr)
                
                # Capture efficiency
                if mfe_adr > 0:
                    eff = captured_adr / mfe_adr
                else:
                    eff = 0.0
                capture_efficiencies.append(eff)
                
                if captured_adr <= 0:
                    all_captured_positive = False
            
            if not capture_efficiencies:
                continue
            
            eff_arr = np.array(capture_efficiencies)
            adr_arr = np.array(captured_adrs)
            
            floor_eff = np.min(eff_arr)
            median_eff = np.median(eff_arr)
            mean_eff = np.mean(eff_arr)
            floor_adr = np.min(adr_arr)
            median_adr = np.median(adr_arr)
            
            # Hard constraint: every example must capture > 0
            if floor_adr <= 0:
                continue
            
            results.append({
                "expression": expr["name"],
                "category": expr["category"],
                "threshold": float(thresh),
                "direction": ">=",
                "floor_efficiency": float(floor_eff),
                "median_efficiency": float(median_eff),
                "mean_efficiency": float(mean_eff),
                "floor_adr": float(floor_adr),
                "median_adr": float(median_adr),
                "mean_adr": float(np.mean(adr_arr)),
                "n_examples_triggered": int(np.sum(np.array(captured_adrs) > 0)),
                "per_example_efficiency": eff_arr.tolist(),
                "per_example_adr": adr_arr.tolist(),
            })
        
        # Also try "below" threshold (first bar where val <= thresh)
        for thresh in thresholds:
            capture_efficiencies = []
            captured_adrs = []
            
            for i, (matrix, mfe_adr, entry_h, adr_val, fwd_c) in enumerate(
                zip(all_matrices, all_mfe_adr, all_entry_high, all_adr, all_fwd_close)
            ):
                col = matrix[:, j]
                below_mask = col <= thresh
                
                exit_bar = None
                for b in range(1, len(col)):
                    if below_mask[b] and not np.isnan(col[b]):
                        exit_bar = b
                        break
                
                if exit_bar is None:
                    capture_efficiencies.append(0.0)
                    captured_adrs.append(0.0)
                    continue
                
                if direction == 'short':
                    captured_raw = entry_h - fwd_c[exit_bar]
                else:
                    captured_raw = fwd_c[exit_bar] - entry_h
                
                captured_adr = captured_raw / adr_val if adr_val > 0 else 0
                captured_adrs.append(captured_adr)
                
                if mfe_adr > 0:
                    eff = captured_adr / mfe_adr
                else:
                    eff = 0.0
                capture_efficiencies.append(eff)
            
            if not capture_efficiencies:
                continue
            
            eff_arr = np.array(capture_efficiencies)
            adr_arr = np.array(captured_adrs)
            
            if np.min(adr_arr) <= 0:
                continue
            
            results.append({
                "expression": expr["name"],
                "category": expr["category"],
                "threshold": float(thresh),
                "direction": "<=",
                "floor_efficiency": float(np.min(eff_arr)),
                "median_efficiency": float(np.median(eff_arr)),
                "mean_efficiency": float(np.mean(eff_arr)),
                "floor_adr": float(np.min(adr_arr)),
                "median_adr": float(np.median(adr_arr)),
                "mean_adr": float(np.mean(adr_arr)),
                "n_examples_triggered": int(np.sum(adr_arr > 0)),
                "per_example_efficiency": eff_arr.tolist(),
                "per_example_adr": adr_arr.tolist(),
            })
        
        if (j + 1) % 50 == 0:
            print(f"  Scored {j+1}/{n_expr} expressions... ({len(results)} valid conditions so far)")
    
    # Sort by floor efficiency (primary), median efficiency (secondary)
    results.sort(key=lambda x: (-x["floor_efficiency"], -x["median_efficiency"]))
    
    return results[:top_n]


def find_plateaus(results, top_n=20):
    """Find robust parameter regions where many conditions score similarly.
    
    Groups by expression category and identifies clusters.
    """
    if not results:
        return []
    
    # Group by category
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    
    plateaus = []
    for cat, items in by_cat.items():
        if len(items) < 2:
            continue
        
        floor_effs = [r["floor_efficiency"] for r in items]
        median_effs = [r["median_efficiency"] for r in items]
        
        plateaus.append({
            "category": cat,
            "n_conditions": len(items),
            "floor_eff_range": [min(floor_effs), max(floor_effs)],
            "median_eff_range": [min(median_effs), max(median_effs)],
            "best_expression": items[0]["expression"],
            "best_floor_eff": items[0]["floor_efficiency"],
        })
    
    plateaus.sort(key=lambda x: -x["best_floor_eff"])
    return plateaus[:top_n]


def print_results(results, profiles, top_n=30):
    """Print top exit conditions."""
    print(f"\n{'='*100}")
    print(f"TOP {min(top_n, len(results))} EXIT CONDITIONS (by floor capture efficiency)")
    print(f"{'='*100}")
    print(f"{'#':>3} {'Expression':<40} {'Dir':>3} {'Thresh':>8} {'Floor%':>7} {'Med%':>6} {'FloorADR':>9} {'MedADR':>8} {'Cat':<20}")
    print(f"{'-'*3} {'-'*40} {'-'*3} {'-'*8} {'-'*7} {'-'*6} {'-'*9} {'-'*8} {'-'*20}")
    
    for i, r in enumerate(results[:top_n]):
        print(f"{i+1:>3} {r['expression']:<40} {r['direction']:>3} {r['threshold']:>8.3f} "
              f"{r['floor_efficiency']*100:>6.1f}% {r['median_efficiency']*100:>5.1f}% "
              f"{r['floor_adr']:>8.1f}x {r['median_adr']:>7.1f}x "
              f"{r['category']:<20}")


def save_results(results, plateaus, profiles, setup_type, output_dir):
    """Save results to JSON."""
    os.makedirs(output_dir, exist_ok=True)
    
    # Main results
    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_examples": len(profiles),
        "n_conditions_found": len(results),
        "scoring": {
            "primary": "floor_capture_efficiency",
            "secondary": "median_capture_efficiency",
            "constraint": "all_examples_capture_positive_adr",
            "benchmark": "entry_high_to_exit_close_in_adr",
        },
        "example_profiles": [{
            "id": p["id"],
            "ticker": p["ticker"],
            "entry_date": p["entry_date"],
            "mfe_close_adr": p["mfe_close_adr"],
            "mfe_bar_close": p["mfe_bar_close"],
            "adr_at_entry": p["adr_at_entry"],
        } for p in profiles],
        "top_conditions": results,
        "plateaus": plateaus,
    }
    
    path = os.path.join(output_dir, f"exit_grind_{setup_type}.json")
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {path}")
    return path


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Exit Grinder — Step 6")
    parser.add_argument("--setup", required=True, help="Setup type (dtss, 3-4db, htf)")
    parser.add_argument("--max-forward", type=int, default=120, help="Max forward bars per example")
    parser.add_argument("--top", type=int, default=50, help="Number of top results to show/save")
    parser.add_argument("--thresholds", type=int, default=20, help="Number of threshold candidates per expression")
    parser.add_argument("--profile-only", action="store_true", help="Only show MFE profiles, don't grind")
    parser.add_argument("--base-only", action="store_true", help="Use base expressions only (skip boolean aggregations)")
    parser.add_argument("--output-dir", default="data/exit_grind", help="Output directory")
    args = parser.parse_args()
    
    direction = SETUP_DIRECTION.get(args.setup, "short")
    print(f"Exit Grinder — Setup: {args.setup} ({direction})")
    print(f"Max forward: {args.max_forward} bars | Top: {args.top} | Thresholds: {args.thresholds}")
    
    # Load examples
    print(f"\nLoading examples from Railway...")
    examples = load_examples(args.setup)
    print(f"  Loaded {len(examples)} examples")
    
    if not examples:
        print("ERROR: No examples loaded!")
        return
    
    # Profile MFE
    print(f"\nProfiling MFE...")
    profiles = profile_examples(examples, direction, args.max_forward)
    print_profile_summary(profiles)
    
    if args.profile_only:
        return
    
    # Load SPY for relative strength
    print(f"\nLoading SPY data...")
    spy_df = load_spy_data()
    if spy_df is not None:
        print(f"  SPY: {len(spy_df)} bars")
    else:
        print(f"  SPY: not available (relative strength expressions will be NaN)")
    
    # Generate expression library
    if args.base_only:
        expressions = generate_exit_expressions()
        print(f"\nExpression library: {len(expressions)} base expressions (no boolean aggs)")
    else:
        expressions = generate_all_exit_expressions()
        print(f"\nExpression library: {len(expressions)} total expressions")
    
    # Grind
    t0 = time.time()
    results = find_exit_conditions(
        profiles, expressions, direction,
        spy_df=spy_df,
        n_thresholds=args.thresholds,
        top_n=args.top * 2,  # get extra for plateau analysis
    )
    elapsed = time.time() - t0
    print(f"\nGrind complete in {elapsed:.1f}s — {len(results)} conditions passed all constraints")
    
    # Print results
    print_results(results, profiles, top_n=args.top)
    
    # Plateau analysis
    plateaus = find_plateaus(results)
    if plateaus:
        print(f"\n{'='*80}")
        print(f"PLATEAU ANALYSIS — Robust parameter regions")
        print(f"{'='*80}")
        for p in plateaus:
            print(f"  {p['category']:<25} {p['n_conditions']:>3} conditions  "
                  f"floor_eff: {p['floor_eff_range'][0]*100:.1f}%-{p['floor_eff_range'][1]*100:.1f}%  "
                  f"best: {p['best_expression']}")
    
    # Save
    path = save_results(results[:args.top], plateaus, profiles, args.setup, args.output_dir)
    
    print(f"\n{'='*80}")
    print(f"DONE — {len(results)} conditions found, top {min(args.top, len(results))} saved")
    print(f"Next: Review results, then run Step 7 (Outcome Grind) with these exit conditions")


if __name__ == "__main__":
    main()
