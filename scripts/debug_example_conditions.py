"""Debug: check why examples don't pass pyramid conditions at their scan bar."""
import os, sys, json, pickle, numpy as np, pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series

# Load cache
cache_path = os.path.join(REPO_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
if not os.path.exists(cache_path):
    cache_path = os.path.join(REPO_ROOT, "local_runner", "cache", "universe_ohlcv_5yr.pkl")
print(f"Loading cache from {cache_path}...")
with open(cache_path, "rb") as f:
    cache = pickle.load(f)
print(f"Loaded {len(cache)} tickers")

# Load conditions
cond_path = os.path.join(REPO_ROOT, "data", "pyramid_results_dtss.json")
with open(cond_path) as f:
    data = json.load(f)
conditions = data.get("all_conditions", [])
print(f"Loaded {len(conditions)} conditions")

# Load examples from Railway
import requests
r = requests.get("https://web-production-e3025.up.railway.app/api/examples/dtss", timeout=30)
examples = r.json().get("examples", [])
print(f"Loaded {len(examples)} examples\n")

# Check first few examples
for ex in examples[:5]:
    ticker = ex.get("ticker")
    entry_date = ex.get("entryDate", ex.get("entry_date"))
    df = cache.get(ticker)
    if df is None:
        print(f"{ticker}: NOT IN CACHE")
        continue

    dates_str = [str(d)[:10] for d in df["date"].values]
    if entry_date not in dates_str:
        print(f"{ticker}: entry date {entry_date} not in cache dates")
        print(f"  Cache date range: {dates_str[0]} to {dates_str[-1]}")
        continue

    entry_idx = dates_str.index(entry_date)
    scan_idx = entry_idx - 1
    print(f"\n{ticker}: entry={entry_date} entry_idx={entry_idx} scan_idx={scan_idx}")
    print(f"  Cache has {len(df)} bars, date range {dates_str[0]} to {dates_str[-1]}")

    engine = ExpressionEngine(df)

    # Check each condition at scan bar
    n_pass = 0
    n_fail = 0
    for cond in conditions:
        try:
            series = compute_series(engine, cond["compute"])
            val = series[scan_idx] if scan_idx < len(series) else np.nan
            low, high = cond["low"], cond["high"]
            passes = not np.isnan(val) and low <= val <= high
            if not passes:
                if n_fail < 5:  # Show first 5 failures
                    print(f"  FAIL: {cond['name']} = {val:.6f}  range [{low:.6f}, {high:.6f}]")
                n_fail += 1
            else:
                n_pass += 1
        except Exception as e:
            n_fail += 1
            if n_fail <= 5:
                print(f"  ERROR: {cond['name']}: {e}")

    print(f"  Result: {n_pass}/{len(conditions)} pass, {n_fail} fail")
