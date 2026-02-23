"""
Signal Distribution Analyzer — Check peak/avg/median signals per day.

Loads Phase 2 results + 5yr cache, applies all conditions, counts signals
per calendar day. Fast (~60s) since it's just 12 conditions, no candidate search.

Usage:
    python scripts/signal_distribution.py [--setup dtss] [--top 20]
"""

import os
import sys
import time
import json
import pickle
import numpy as np
import pandas as pd
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner"))

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series
from scripts.profiling_engine import count_true, since_true, true_in_row

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner", "cache")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Signal Distribution Analyzer")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--top", type=int, default=20, help="Show top N worst days")
    args = parser.parse_args()

    # Load results
    results_path = os.path.join(CACHE_DIR, f"historical_results_{args.setup}.json")
    if not os.path.exists(results_path):
        print(f"No results found at {results_path}")
        return
    with open(results_path) as f:
        results = json.load(f)

    conditions = results["all_conditions"]
    print(f"Loaded {len(conditions)} conditions for {args.setup.upper()}")

    # Load 5yr cache
    cache_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    print(f"Loading cache...")
    with open(cache_path, "rb") as f:
        universe_cache = pickle.load(f)
    print(f"  {len(universe_cache)} tickers loaded")

    # Run all conditions across all tickers, collect (date, ticker) signals
    print(f"\nScanning {len(universe_cache)} tickers × {len(conditions)} conditions...")
    t0 = time.time()

    daily_counts = defaultdict(int)
    daily_tickers = defaultdict(list)
    total_signals = 0
    skipped = 0
    done = 0

    for ticker, df in universe_cache.items():
        done += 1
        if df is None or len(df) < 100:
            skipped += 1
            continue

        try:
            engine = ExpressionEngine(df)
            n_bars = len(df)
            pass_mask = np.ones(n_bars, dtype=bool)
            pass_mask[:50] = False

            for cond in conditions:
                series = compute_series(engine, cond["compute"])
                low, high = cond["low"], cond["high"]
                in_range = (series >= low) & (series <= high)
                in_range[np.isnan(series)] = False
                pass_mask &= in_range

            signal_indices = np.where(pass_mask)[0]
            if len(signal_indices) > 0:
                dates = df["date"].values
                for idx in signal_indices:
                    d = str(dates[idx])[:10]
                    daily_counts[d] += 1
                    daily_tickers[d].append(ticker)
                total_signals += len(signal_indices)
        except:
            skipped += 1

        if done % 500 == 0:
            elapsed = time.time() - t0
            rate = done / elapsed
            eta = (len(universe_cache) - done) / rate
            print(f"  {done}/{len(universe_cache)} [{elapsed:.0f}s, ~{eta:.0f}s left] "
                  f"{total_signals:,} signals so far")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"Skipped: {skipped} tickers")

    if not daily_counts:
        print("No signals found.")
        return

    # Analyze distribution
    counts = np.array(list(daily_counts.values()))
    dates_sorted = sorted(daily_counts.keys(), key=lambda d: daily_counts[d], reverse=True)

    n_days = len(counts)
    print(f"\n{'='*60}")
    print(f"  SIGNAL DISTRIBUTION — {args.setup.upper()}")
    print(f"{'='*60}")
    print(f"  Total signals:  {total_signals:,}")
    print(f"  Days with signals: {n_days}")
    print(f"  Average/day:    {np.mean(counts):.1f}")
    print(f"  Median/day:     {np.median(counts):.1f}")
    print(f"  Max (peak):     {np.max(counts)}")
    print(f"  Min:            {np.min(counts)}")
    print(f"  Std dev:        {np.std(counts):.1f}")
    print(f"  P90:            {np.percentile(counts, 90):.0f}")
    print(f"  P95:            {np.percentile(counts, 95):.0f}")
    print(f"  P99:            {np.percentile(counts, 99):.0f}")

    # Days with 0 signals (approximate — only days in the data)
    # Count total trading days from the data
    all_dates = set()
    for ticker, df in universe_cache.items():
        if df is not None and len(df) > 0:
            for d in df["date"].values:
                all_dates.add(str(d)[:10])
            break  # just need one ticker's date range as reference
    # Actually get all unique dates across all tickers for accuracy
    # But that's slow — use the signal days vs total estimate
    zero_days = max(0, len(all_dates) - n_days)

    print(f"\n  Bracket distribution:")
    brackets = [(0, 0), (1, 5), (6, 10), (11, 15), (16, 20), (21, 30), (31, 50), (51, 100), (101, 9999)]
    for lo, hi in brackets:
        n = np.sum((counts >= lo) & (counts <= hi))
        if n > 0:
            label = f"{lo}-{hi}" if hi < 9999 else f"{lo}+"
            print(f"    {label:>8} signals/day: {n:>4} days ({n/n_days*100:.1f}%)")

    print(f"\n  Top {args.top} worst days:")
    for i, d in enumerate(dates_sorted[:args.top]):
        tickers = daily_tickers[d]
        ticker_str = ", ".join(tickers[:8])
        if len(tickers) > 8:
            ticker_str += f" +{len(tickers)-8} more"
        print(f"    {i+1:3d}. {d}  {daily_counts[d]:>3} signals  [{ticker_str}]")


if __name__ == "__main__":
    main()
