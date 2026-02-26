"""Parity Test — verify all grinders compute expressions identically.

Picks a random sample of tickers, computes the exit condition through
each grinder's code path, and asserts the values are exactly equal.

Usage:
    python scripts/parity_test.py --setup dtss
"""
import pickle, sys, os, json, numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.expression_engine import ExpressionEngine
from scripts.exit_compute import ExitExprEngine
from scripts.outcome_grinder import _compute_exit_series


def test_parity(setup="dtss", n_tickers=20):
    # Load cache
    cache_path = os.path.join("local_runner", "cache", "universe_ohlcv_5yr.pkl")
    cache = pickle.load(open(cache_path, "rb"))
    
    # Load exit grind to get the winning condition
    from local_runner.grind_storage import GrindStorage
    gs = GrindStorage(setup)
    exit_data = gs.load("exit")
    expr_name = exit_data["results"][0]["expr_name"]
    
    # Get example tickers from signal grind
    signal_data = gs.load("signal")
    # Signal grind stores examples under "example_signals"
    example_entries = signal_data.get("example_signals", signal_data.get("examples", []))
    example_tickers = list(set(
        ex.get("ticker", ex.get("Ticker", "")) for ex in example_entries
    ))
    
    # Test examples first, then pad with random tickers
    test_tickers = list(example_tickers)
    remaining = [t for t in cache.keys() if t not in set(test_tickers)]
    np.random.seed(42)
    extra = np.random.choice(remaining, min(n_tickers, len(remaining)), replace=False)
    test_tickers.extend(extra)
    
    print(f"Testing parity for: {expr_name}")
    print(f"Testing {len(example_tickers)} example tickers + {len(extra)} random tickers\n")
    
    mismatches = 0
    tested = 0
    
    for ticker in test_tickers:
        df = cache[ticker]
        if len(df) < 100:
            continue
        
        # Ensure numeric
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        
        # === PATH 1: Outcome grinder's _compute_exit_series ===
        engine1 = ExpressionEngine(df)
        outcome_vals = _compute_exit_series(engine1, expr_name)
        
        # === PATH 2: ExitExprEngine (exit grinder's path) ===
        # Use midpoint as fake entry to get full-ish forward window
        entry_idx = 50
        exit_engine = ExitExprEngine(df, entry_idx, direction="short",
                                      max_forward=len(df) - entry_idx - 1)
        
        # We need to build the compute dict for this expression
        # Parse the expression name to get the compute dict
        import re
        m = re.match(r'^(\w+?)_(\d+)_(\w+?)_(count_true|pct_true|since_true|true_in_row)_(\d+)b$', expr_name)
        if not m:
            print(f"  Cannot parse expression: {expr_name}")
            return
        
        indicator = m.group(1)
        period = int(m.group(2))
        bool_cond = m.group(3)
        agg_type = m.group(4)
        agg_window = int(m.group(5))
        
        compute_dict = {
            "op": f"bool_{agg_type}",
            "base_op": {"op": f"{indicator}_{bool_cond}", "period": period},
            "window": agg_window,
        }
        
        exit_vals_fwd = exit_engine.compute(compute_dict)
        
        # exit_vals_fwd covers [entry_idx : end], outcome_vals covers [0 : end]
        # Compare the overlapping region
        outcome_slice = outcome_vals[entry_idx:entry_idx + len(exit_vals_fwd)]
        
        # Compare (ignoring NaN == NaN)
        both_nan = np.isnan(outcome_slice) & np.isnan(exit_vals_fwd)
        both_valid = ~np.isnan(outcome_slice) & ~np.isnan(exit_vals_fwd)
        
        if both_valid.sum() == 0:
            print(f"  {ticker:8s} — no valid overlapping values, skip")
            continue
        
        max_diff = np.max(np.abs(outcome_slice[both_valid] - exit_vals_fwd[both_valid]))
        match = max_diff < 1e-10
        
        tested += 1
        if not match:
            mismatches += 1
            print(f"  {ticker:8s} — MISMATCH! max diff = {max_diff:.6f}")
            # Show first few divergent values
            diffs = np.where(both_valid & (np.abs(outcome_slice - exit_vals_fwd) > 1e-10))[0]
            for idx in diffs[:3]:
                abs_idx = entry_idx + idx
                print(f"    bar {abs_idx}: outcome={outcome_slice[idx]:.4f} vs exit={exit_vals_fwd[idx]:.4f}")
        else:
            print(f"  {ticker:8s} — ✓ exact match ({both_valid.sum()} bars compared)")
    
    print(f"\n{'='*60}")
    if mismatches == 0:
        print(f"✅ PARITY CONFIRMED: {tested}/{tested} tickers match exactly")
    else:
        print(f"❌ PARITY FAILED: {mismatches}/{tested} tickers have mismatches")
    print(f"{'='*60}")
    
    return mismatches == 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--n-tickers", type=int, default=20)
    args = parser.parse_args()
    
    success = test_parity(args.setup, args.n_tickers)
    sys.exit(0 if success else 1)
