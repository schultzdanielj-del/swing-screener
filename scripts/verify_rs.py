"""Quick verification: compute RS_D1 for MSFT on 2025-10-27 and print it.

Expected from manual calculation: approximately -13.13

Usage: python scripts/verify_rs.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.setup_grinder import (
    load_signals_from_refinement, load_5yr_ohlcv,
    find_latest_refinement, precompute_all_rs, compute_features_for_signals
)

print("Loading...")
ref_path = find_latest_refinement("dtss")
sigs = load_signals_from_refinement(ref_path, "pre")
ohlcv = load_5yr_ohlcv()

tickers = {s["ticker"] for s in sigs}
rs_cache = precompute_all_rs(ohlcv, tickers)
compute_features_for_signals(sigs, ohlcv, rs_cache)

# Find MSFT 2025-10-27
for s in sigs:
    if s["ticker"] == "MSFT" and str(s["signal_date"])[:10] == "2025-10-27":
        print(f"\nMSFT 2025-10-27:")
        print(f"  feat_rs_d1 = {s['feat_rs_d1']}")
        print(f"  feat_rs_w1 = {s['feat_rs_w1']}")
        print(f"  feat_price = {s['feat_price']}")
        print(f"  feat_adr   = {s['feat_adr']}")
        print(f"\nExpected rs_d1 ~ -13.13 (from manual calc)")
        if s['feat_rs_d1'] is not None:
            diff = abs(s['feat_rs_d1'] - (-13.1341))
            print(f"  Difference from manual: {diff:.4f}")
            if diff < 0.5:
                print("  PASS - matches manual calculation")
            else:
                print(f"  FAIL - off by {diff:.2f}")
        break
else:
    print("MSFT 2025-10-27 not found in signals")
