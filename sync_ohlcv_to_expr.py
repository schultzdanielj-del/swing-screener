"""
One-time fix: Trim 5yr OHLCV pickle to match expr cache start dates.

The expr cache has the correct start dates from when the 65/68 example
match happened. The 5yr OHLCV was rebuilt without a limit and now has
extra bars at the front. This script trims each ticker's OHLCV to start
at the same date as the expr cache, re-syncing bar indices.

Usage:
    python sync_ohlcv_to_expr.py

Run from repo root. Overwrites universe_ohlcv_5yr.pkl in place.
"""

import os
import sys
import pickle
import numpy as np
import pandas as pd

LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
OHLCV_PATH = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")

sys.path.insert(0, LOCAL_DIR)
from expr_cache_builder import load_ticker_cache, load_manifest

print("=" * 60)
print("  SYNC OHLCV TO EXPR CACHE START DATES")
print("=" * 60)

# Load expr cache manifest
manifest = load_manifest()
if manifest is None:
    print("ERROR: No expr cache manifest found.")
    sys.exit(1)
print(f"\n  Expr cache: {manifest['n_tickers']} tickers, {manifest['n_expressions']} expressions")

# Load 5yr OHLCV
print(f"  Loading 5yr OHLCV...")
with open(OHLCV_PATH, "rb") as f:
    ohlcv = pickle.load(f)
print(f"  OHLCV: {len(ohlcv)} tickers")

trimmed = 0
matched = 0
skipped = 0
not_in_expr = 0

for ticker, df in list(ohlcv.items()):
    # Load expr cache first date for this ticker
    dates, data = load_ticker_cache(ticker)
    if dates is None:
        not_in_expr += 1
        continue

    expr_first_date = str(dates[0])[:10]

    # Get OHLCV dates
    ohlcv_dates = [str(d)[:10] for d in df["date"].values]
    if not ohlcv_dates:
        skipped += 1
        continue

    ohlcv_first_date = ohlcv_dates[0]

    if ohlcv_first_date == expr_first_date:
        matched += 1
        continue

    # Find the expr start date in the OHLCV
    if expr_first_date in ohlcv_dates:
        trim_idx = ohlcv_dates.index(expr_first_date)
        ohlcv[ticker] = df.iloc[trim_idx:].reset_index(drop=True)
        trimmed += 1
    else:
        # Expr cache starts at a date not in OHLCV — skip
        skipped += 1

print(f"\n  Results:")
print(f"    Already matched: {matched}")
print(f"    Trimmed to match: {trimmed}")
print(f"    Not in expr cache: {not_in_expr}")
print(f"    Skipped (date not found): {skipped}")

# Verify a few tickers
print(f"\n  Verifying...")
check_tickers = ["AAOI", "ACHR", "APP", "MSFT", "AAPL"]
for ticker in check_tickers:
    if ticker not in ohlcv:
        continue
    dates_e, _ = load_ticker_cache(ticker)
    if dates_e is None:
        continue
    df = ohlcv[ticker]
    ohlcv_first = str(df["date"].iloc[0])[:10]
    expr_first = str(dates_e[0])[:10]
    ohlcv_n = len(df)
    expr_n = len(dates_e)
    match = "OK" if ohlcv_first == expr_first else "MISMATCH"
    bars_match = "OK" if ohlcv_n == expr_n else f"DRIFT ({ohlcv_n} vs {expr_n})"
    print(f"    {ticker}: first={ohlcv_first} expr={expr_first} [{match}] bars={ohlcv_n} expr={expr_n} [{bars_match}]")

# Save
print(f"\n  Saving trimmed OHLCV...")
with open(OHLCV_PATH, "wb") as f:
    pickle.dump(ohlcv, f, protocol=pickle.HIGHEST_PROTOCOL)

size_mb = os.path.getsize(OHLCV_PATH) / 1024 / 1024
bar_counts = [len(df) for df in ohlcv.values()]
print(f"  Saved: {OHLCV_PATH} ({size_mb:.1f} MB)")
print(f"  Tickers: {len(ohlcv)}")
print(f"  Min/Max bars: {min(bar_counts)}/{max(bar_counts)}")
print(f"\n  Done. OHLCV start dates now match expr cache.")
print(f"  Run the refinement grind to verify 65/68 matches.")
