"""
Historical Scorer — Phase 2 of the Grinder.

After Phase 1 (spiderweb) finds the single-day ceiling, Phase 2 scores
remaining candidate expressions by how many HISTORICAL signals they eliminate.

The key insight: a condition useless today (doesn't drop 3→2 tickers) might
eliminate 300/day of historical noise without losing any examples.

Algorithm:
  1. Load Phase 1 winning conditions + thresholds
  2. Compute base signal mask: run conditions across all tickers × 5yr → boolean signal array
  3. For every remaining valid expression that passes all examples:
     a. Compute its series across all tickers × 5yr
     b. Apply example-derived thresholds
     c. AND with base signal mask → count remaining signals
  4. Greedily add the expression that eliminates the most historical signals
  5. Repeat until signals/day < target threshold
  6. Constraint: 100% of setup examples must ALWAYS pass all conditions

Usage:
    python local_runner/historical_scorer.py --setup dtss --target 10
    
    --target: target avg signals per day (default: 10)

Requires:
  - Phase 1 grinder results (local_runner/cache/grinder_results_{setup}.json)
  - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
  - Example OHLCV data (via Railway API or local cache)
"""

import os
import sys
import time
import json
import pickle
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series, _get_normalizer
from scripts.profiling_engine import count_true, since_true, true_in_row
from brute_expressions import generate_all

API_BASE = "https://web-production-e3025.up.railway.app"


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_phase1_results(setup_type):
    """Load Phase 1 grinder results and enrich with compute specs."""
    path = os.path.join(CACHE_DIR, f"grinder_results_{setup_type}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No Phase 1 results found at {path}. Run the grinder first.")
    with open(path) as f:
        results = json.load(f)
    
    # Enrich best_thresholds with compute specs from expression library
    from local_runner.brute_expressions import generate_all
    expr_lookup = {e["name"]: e["compute"] for e in generate_all()}
    for cond in results.get("best_thresholds", []):
        if "compute" not in cond:
            name = cond["expr"]
            if name not in expr_lookup:
                raise KeyError(
                    f"Expression '{name}' from Phase 1 results not found "
                    f"in expression library. Was the library changed?")
            cond["compute"] = expr_lookup[name]
    
    return results


def load_5yr_cache():
    """Load 5-year OHLCV cache."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        # Fall back to standard cache
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(
            "No OHLCV cache found. Run cache_builder.py first.")
    with open(path, "rb") as f:
        return pickle.load(f)


def load_example_data(setup_type):
    """Load example OHLCV data from Railway API."""
    import requests
    resp = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=30)
    data = resp.json()
    if "examples" not in data:
        print(f"    ⚠ API response keys: {list(data.keys())}")
        print(f"    ⚠ Response: {str(data)[:300]}")
        raise KeyError(f"API response missing 'examples' key. Status: {resp.status_code}")
    examples = data["examples"]
    
    example_dfs = []
    for ex in examples:
        eid = ex["id"]
        r = requests.get(
            f"{API_BASE}/api/ohlcv/local/{setup_type}/{eid}", timeout=30)
        data = r.json()
        candles = data.get("candles", [])
        if not candles:
            continue
        
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        
        # Entry date is the scan date (day before entry)
        entry_date = ex.get("entryDate") or ex.get("entry_date")
        scan_idx = None
        if entry_date:
            entry_dt = pd.to_datetime(entry_date)
            # Scan bar = bar before entry
            match = df[df["date"] < entry_dt]
            if len(match) > 0:
                scan_idx = match.index[-1]
        
        example_dfs.append({
            "ticker": ex["ticker"],
            "entry_date": entry_date,
            "scan_idx": scan_idx,
            "df": df,
        })
    
    return example_dfs


# ══════════════════════════════════════════════════════════════
# PARALLEL WORKER FUNCTIONS (must be top-level for pickling)
# ══════════════════════════════════════════════════════════════

# Module-level shared state for workers (set by initializer)
_worker_conditions = None
_worker_valid_candidates = None
_worker_candidate_ranges = None
_worker_n_cands = None
_worker_min_bars = None


def _init_base_worker(conditions, min_bars):
    """Initializer for base signal workers."""
    global _worker_conditions, _worker_min_bars
    _worker_conditions = conditions
    _worker_min_bars = min_bars


def _base_signal_worker(args):
    """Process one ticker for base signal computation."""
    ticker, df = args
    if df is None or len(df) < _worker_min_bars:
        return ticker, None, 0, True
    try:
        engine = ExpressionEngine(df)
        n_bars = len(df)
        pass_mask = np.ones(n_bars, dtype=bool)
        pass_mask[:50] = False
        
        for cond in _worker_conditions:
            series = compute_series(engine, cond["compute"])
            low, high = cond["low"], cond["high"]
            in_range = (series >= low) & (series <= high)
            in_range[np.isnan(series)] = False
            pass_mask &= in_range
        
        sig_count = int(np.sum(pass_mask))
        return ticker, pass_mask if sig_count > 0 else None, n_bars - 50, False
    except:
        return ticker, None, 0, True


def _init_precompute_worker(valid_candidates, candidate_ranges, n_cands):
    """Initializer for candidate precompute workers."""
    global _worker_valid_candidates, _worker_candidate_ranges, _worker_n_cands
    _worker_valid_candidates = valid_candidates
    _worker_candidate_ranges = candidate_ranges
    _worker_n_cands = n_cands


def _precompute_ticker_worker(args):
    """Process one ticker for candidate precomputation."""
    ticker, df = args
    if df is None:
        return ticker, None
    n_bars = len(df)
    try:
        engine = ExpressionEngine(df)
    except:
        return ticker, None
    
    masks = np.zeros((_worker_n_cands, n_bars), dtype=bool)
    for cidx, expr in enumerate(_worker_valid_candidates):
        try:
            series = compute_series(engine, expr["compute"])
            if series is None or len(series) != n_bars:
                continue
            low, high = _worker_candidate_ranges[expr["name"]]
            in_range = (series >= low) & (series <= high)
            in_range[np.isnan(series)] = False
            masks[cidx] = in_range
        except:
            pass
    return ticker, masks


# ══════════════════════════════════════════════════════════════
# CORE: COMPUTE SIGNAL MASKS
# ══════════════════════════════════════════════════════════════

def compute_base_signals(universe_cache, conditions, min_bars=100):
    """
    Run Phase 1 conditions across ALL tickers × ALL bars.
    Returns dict of {ticker: boolean_array} where True = signal.
    Uses ProcessPoolExecutor for true parallelism.
    """
    print(f"\n  Computing base signal mask ({len(conditions)} conditions × "
          f"{len(universe_cache)} tickers)...")
    t0 = time.time()
    
    signals = {}
    total_signals = 0
    total_bars = 0
    skipped = 0
    completed = 0
    
    n_workers = min(cpu_count(), 8)
    work_items = list(universe_cache.items())
    
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_base_worker,
        initargs=(conditions, min_bars)
    ) as pool:
        futures = {pool.submit(_base_signal_worker, item): item[0] 
                   for item in work_items}
        for future in as_completed(futures):
            ticker, mask, bars, was_skipped = future.result()
            if was_skipped:
                skipped += 1
            else:
                total_bars += bars
                if mask is not None:
                    signals[ticker] = mask
                    total_signals += int(np.sum(mask))
            completed += 1
            if completed % 500 == 0:
                elapsed = time.time() - t0
                rate = completed / elapsed
                eta = (len(universe_cache) - completed) / rate
                print(f"    {completed}/{len(universe_cache)} tickers "
                      f"[{elapsed:.0f}s, ~{eta:.0f}s left, "
                      f"{total_signals:,} signals so far] ({n_workers} workers)")
    
    elapsed = time.time() - t0
    n_days = total_bars / max(len(universe_cache) - skipped, 1)
    sig_per_day = total_signals / max(n_days, 1) if n_days > 0 else 0
    
    print(f"  Base signals: {total_signals:,} across {len(signals)} tickers "
          f"(~{sig_per_day:.1f}/day, {elapsed:.0f}s)")
    print(f"  Skipped: {skipped} tickers (too short or error)")
    
    return signals, total_signals


def compute_candidate_mask(engine, expr, n_bars):
    """
    Compute a single expression's boolean mask for one ticker.
    Returns boolean array of length n_bars, or None on error.
    """
    try:
        series = compute_series(engine, expr["compute"])
        if series is None or len(series) != n_bars:
            return None
        return series
    except:
        return None


def validate_examples(example_dfs, conditions):
    """
    Verify all examples pass all conditions at their scan bar.
    Returns list of example scan bar values for threshold derivation.
    """
    print(f"\n  Validating {len(example_dfs)} examples against conditions...")
    
    all_pass = True
    for ex in example_dfs:
        df = ex["df"]
        scan_idx = ex["scan_idx"]
        if scan_idx is None:
            print(f"    ⚠ {ex['ticker']}: no scan index, skipping validation")
            continue
        
        engine = ExpressionEngine(df)
        for cond in conditions:
            series = compute_series(engine, cond["compute"])
            val = series[scan_idx]
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                print(f"    ✗ {ex['ticker']} FAILS {cond.get('name', cond.get('expr'))}: "
                      f"{val:.4f} not in [{cond['low']:.4f}, {cond['high']:.4f}]")
                all_pass = False
    
    if all_pass:
        print(f"  ✓ All examples pass all conditions")
    return all_pass


def get_example_range(example_dfs, expr):
    """
    Compute the [min, max] range of an expression across all example scan bars.
    Returns (low, high) or None if too many NaNs.
    """
    values = []
    for ex in example_dfs:
        scan_idx = ex["scan_idx"]
        if scan_idx is None:
            continue
        try:
            engine = ExpressionEngine(ex["df"])
            series = compute_series(engine, expr["compute"])
            val = series[scan_idx]
            if not np.isnan(val):
                values.append(val)
        except:
            continue
    
    if len(values) < len(example_dfs) * 0.8:  # Need 80% of examples
        return None
    
    return (min(values), max(values))


# ══════════════════════════════════════════════════════════════
# PHASE 2: GREEDY HISTORICAL SCORING
# ══════════════════════════════════════════════════════════════

def score_candidates(universe_cache, base_signals, example_dfs,
                     phase1_conditions, target_per_day=10, max_rounds=20):
    """
    Greedy forward selection: at each round, find the expression that
    eliminates the most historical signals without losing any examples.
    
    Optimization: precompute all candidate series across signal-bearing
    tickers ONCE, then scoring each round is pure numpy AND operations.
    """
    # Generate all candidate expressions
    all_exprs = generate_all()
    
    # Get Phase 1 expression names to exclude (already applied)
    phase1_names = set(c.get("name", c.get("expr")) for c in phase1_conditions)
    candidates = [e for e in all_exprs if e["name"] not in phase1_names]
    print(f"\n  Phase 2 candidates: {len(candidates)} expressions "
          f"(excluded {len(phase1_names)} Phase 1 conditions)")
    
    # Pre-compute example ranges for all candidates
    print(f"  Computing example ranges...")
    t0 = time.time()
    candidate_ranges = {}
    
    for expr in candidates:
        rng = get_example_range(example_dfs, expr)
        if rng is not None:
            candidate_ranges[expr["name"]] = rng
    
    valid_candidates = [e for e in candidates if e["name"] in candidate_ranges]
    print(f"  {len(valid_candidates)} candidates have valid example ranges "
          f"({time.time()-t0:.0f}s)")
    
    # Tickers that have any signals
    signal_tickers = [t for t, m in base_signals.items() if np.any(m)]
    print(f"  {len(signal_tickers)} tickers have signals to score against")
    
    # ── PRECOMPUTE all candidate boolean masks per signal ticker ──
    # Shape per ticker: (n_candidates, n_bars) boolean
    # This is the expensive step but only done ONCE
    print(f"\n  Precomputing {len(valid_candidates)} candidate series "
          f"across {len(signal_tickers)} tickers...")
    t_pre = time.time()
    
    # Store as {ticker: np.array(n_candidates, n_bars)} boolean
    ticker_candidate_masks = {}
    candidate_names = [e["name"] for e in valid_candidates]
    candidate_idx = {name: i for i, name in enumerate(candidate_names)}
    n_cands = len(valid_candidates)
    
    # Use ProcessPoolExecutor for true parallelism
    n_workers = min(cpu_count(), 8)
    work_items = [(t, universe_cache[t]) for t in signal_tickers]
    completed = 0
    
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_precompute_worker,
        initargs=(valid_candidates, candidate_ranges, n_cands)
    ) as pool:
        futures = {pool.submit(_precompute_ticker_worker, item): item[0] 
                   for item in work_items}
        for future in as_completed(futures):
            ticker, masks = future.result()
            if masks is not None:
                ticker_candidate_masks[ticker] = masks
            completed += 1
            if completed % 200 == 0 or completed == len(signal_tickers):
                elapsed = time.time() - t_pre
                rate = completed / elapsed
                eta = (len(signal_tickers) - completed) / rate
                print(f"    {completed}/{len(signal_tickers)} tickers precomputed "
                      f"[{elapsed:.0f}s, ~{eta:.0f}s left] ({n_workers} workers)")
    
    print(f"  Precomputation done in {time.time()-t_pre:.0f}s")
    
    # Count current signals & estimate trading days
    current_total = sum(int(np.sum(m)) for m in base_signals.values())
    # Use median ticker bar count for days estimate (more robust)
    bar_counts = [len(m) - 50 for m in base_signals.values()]
    est_days = int(np.median(bar_counts)) if bar_counts else 250
    current_per_day = current_total / max(est_days, 1)
    
    print(f"\n  Starting: {current_total:,} signals (~{current_per_day:.1f}/day)")
    print(f"  Target: <{target_per_day}/day")
    
    if current_per_day <= target_per_day:
        print(f"  Already below target. No Phase 2 needed.")
        return []
    
    # Current signal masks (mutable)
    current_masks = {t: m.copy() for t, m in base_signals.items()
                     if t in ticker_candidate_masks}
    
    selected = []
    selected_indices = set()
    
    for round_num in range(1, max_rounds + 1):
        print(f"\n  ── Round {round_num} ──")
        t_round = time.time()
        
        # Score all remaining candidates — pure numpy
        reductions = np.zeros(n_cands, dtype=np.int64)
        
        for ticker, sig_mask in current_masks.items():
            if not np.any(sig_mask):
                continue
            cand_masks = ticker_candidate_masks.get(ticker)
            if cand_masks is None:
                continue
            
            # For each candidate: count signals that would be eliminated
            # eliminated = signals AND (NOT in_range)
            # = sum(sig_mask & ~cand_masks[c]) for each c
            # Vectorized: sig_mask is (n_bars,), cand_masks is (n_cands, n_bars)
            sig_expanded = sig_mask[np.newaxis, :]  # (1, n_bars)
            eliminated = np.sum(sig_expanded & ~cand_masks, axis=1)  # (n_cands,)
            reductions += eliminated
        
        # Zero out already-selected candidates
        for idx in selected_indices:
            reductions[idx] = 0
        
        best_idx = np.argmax(reductions)
        best_reduction = int(reductions[best_idx])
        
        if best_reduction == 0:
            print(f"  No more improving candidates. Stopping.")
            break
        
        # Apply the winner
        best_expr = valid_candidates[best_idx]
        best_name = best_expr["name"]
        low, high = candidate_ranges[best_name]
        
        new_total = 0
        for ticker, sig_mask in current_masks.items():
            cand_masks = ticker_candidate_masks.get(ticker)
            if cand_masks is not None:
                current_masks[ticker] = sig_mask & cand_masks[best_idx]
            new_total += int(np.sum(current_masks[ticker]))
        
        new_per_day = new_total / max(est_days, 1)
        
        selected.append({
            "name": best_name,
            "category": best_expr.get("category", "unknown"),
            "compute": best_expr["compute"],
            "low": low,
            "high": high,
        })
        selected_indices.add(best_idx)
        
        elapsed = time.time() - t_round
        print(f"  + {best_name}")
        print(f"    Range: [{low:.4f}, {high:.4f}]")
        print(f"    Eliminated: {best_reduction:,} signals")
        print(f"    Remaining: {new_total:,} (~{new_per_day:.1f}/day)")
        print(f"    Time: {elapsed:.1f}s")
        
        if new_per_day <= target_per_day:
            print(f"\n  ✓ Target reached: {new_per_day:.1f}/day < {target_per_day}/day")
            break
    
    return selected


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Historical Scorer — Phase 2")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--target", type=int, default=10,
                        help="Target avg signals per day")
    parser.add_argument("--max-rounds", type=int, default=20)
    args = parser.parse_args()
    
    print("\n" + "=" * 60)
    print("  HISTORICAL SCORER — Phase 2")
    print("=" * 60)
    print(f"  Setup: {args.setup.upper()}")
    print(f"  Target: <{args.target} signals/day")
    
    t0 = time.time()
    
    # Load Phase 1 results
    print(f"\n  Loading Phase 1 results...")
    phase1 = load_phase1_results(args.setup)
    phase1_conditions = phase1["best_thresholds"]
    print(f"  Phase 1: {len(phase1_conditions)} conditions, "
          f"{phase1['best_rate']:.2%} single-day pass rate")
    
    # Load OHLCV cache
    print(f"\n  Loading OHLCV cache...")
    universe_cache = load_5yr_cache()
    print(f"  {len(universe_cache)} tickers loaded")
    
    # Load examples
    print(f"\n  Loading examples...")
    example_dfs = load_example_data(args.setup)
    print(f"  {len(example_dfs)} examples loaded")
    
    # Validate examples against Phase 1 conditions
    validate_examples(example_dfs, phase1_conditions)
    
    # Compute base signal mask
    base_signals, total_base = compute_base_signals(
        universe_cache, phase1_conditions)
    
    # Run Phase 2 greedy selection
    phase2_additions = score_candidates(
        universe_cache, base_signals, example_dfs,
        phase1_conditions,
        target_per_day=args.target,
        max_rounds=args.max_rounds,
    )
    
    # Combine and save
    all_conditions = phase1_conditions + phase2_additions
    total_time = time.time() - t0
    
    print(f"\n{'='*60}")
    print(f"  FINAL RESULTS")
    print(f"{'='*60}")
    print(f"  Phase 1 conditions: {len(phase1_conditions)}")
    print(f"  Phase 2 additions:  {len(phase2_additions)}")
    print(f"  Total conditions:   {len(all_conditions)}")
    print(f"  Total time:         {total_time:.0f}s ({total_time/60:.1f} min)")
    
    print(f"\n  All conditions:")
    for i, c in enumerate(all_conditions, 1):
        phase = "P1" if i <= len(phase1_conditions) else "P2"
        cat = c.get("category", "unknown")
        print(f"    {i:2d}. [{phase}] [{cat:>18}] {c['name']:35s} "
              f"[{c['low']:.4f} — {c['high']:.4f}]")
    
    # Save results
    result = {
        "setup_type": args.setup,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "target_per_day": args.target,
        "phase1_conditions": phase1_conditions,
        "phase2_additions": phase2_additions,
        "all_conditions": all_conditions,
        "n_phase1": len(phase1_conditions),
        "n_phase2": len(phase2_additions),
    }
    
    out_path = os.path.join(CACHE_DIR, f"historical_results_{args.setup}.json")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
