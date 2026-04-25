"""Derive forward_window per setup from current example data.

Per SIGNAL_FILTER.md (main repo) and pyramid_grinder.py §3395-3478:
  forward_window = max(example_fwd_windows)
  example_fwd_window = entry_bar_idx - leftmost_cluster_bar

This IS the entry-discovery window. Per SIGNAL_FILTER.md §57:
  Race (trade lifetime) starts at rightmost_bar + forward_window + 1
  Therefore entry must be in (rightmost, rightmost + forward_window], i.e. the
  forward_window-bar window after the signal bar.

For each example, the cluster's leftmost is the earliest consecutive-bar signal ending
at the example's signal bar. Cluster size = signal_bar_idx - leftmost_idx + 1. For an
example (entry_bar = signal_bar + 1), example_fwd_window = entry_bar - leftmost =
cluster_size.
"""

from __future__ import annotations

import os, json, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")

LATEST_PYRAMID = {
    "htf":   "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":    "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":  "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":  "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db": "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def main():
    with sqlite3.connect(DB) as c:
        examples = [
            {"setup": s, "ticker": t, "entry_date": d}
            for s, t, d in c.execute(
                "SELECT setup_type, ticker, entry_date FROM examples "
                "WHERE setup_type IN ('htf','bf','base','dtss','3-4db') ORDER BY setup_type, ticker, entry_date"
            ).fetchall()
        ]
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    signals_by_setup = {}
    for setup, fn in LATEST_PYRAMID.items():
        with open(os.path.join(CACHE_DIR, fn)) as f:
            pyr = json.load(f)
        tiers = pyr["tier_results"]
        last = tiers[list(tiers.keys())[-1]]
        # Per-ticker sorted signal dates
        by_ticker = defaultdict(list)
        for s in last["final_signals"]:
            by_ticker[s["ticker"]].append(s["date"])
        signals_by_setup[setup] = {t: sorted(set(ds)) for t, ds in by_ticker.items()}

    per_setup = defaultdict(list)  # {setup: [cluster_size per example]}
    per_setup_details = defaultdict(list)

    for e in examples:
        setup = e["setup"]; ticker = e["ticker"]; entry_date = e["entry_date"]
        df = universe.get(ticker)
        if df is None: continue
        dates = ohlcv_dates_str(df)
        if entry_date not in set(dates): continue
        entry_idx = int(np.where(dates == entry_date)[0][0])
        signal_idx = entry_idx - 1
        if signal_idx < 0: continue
        signal_date = dates[signal_idx]

        tsig = signals_by_setup[setup].get(ticker, [])
        if signal_date not in set(tsig): continue

        # Map signal dates to bar indices
        date_to_idx = {d: i for i, d in enumerate(dates)}
        sig_idxs = sorted(date_to_idx[d] for d in tsig if d in date_to_idx)
        # Find consecutive run ending at signal_idx
        if signal_idx not in sig_idxs: continue
        pos = sig_idxs.index(signal_idx)
        lo = pos
        while lo > 0 and sig_idxs[lo] - sig_idxs[lo - 1] == 1:
            lo -= 1
        leftmost_idx = sig_idxs[lo]
        cluster_size = signal_idx - leftmost_idx + 1
        fwd_window = entry_idx - leftmost_idx  # = cluster_size (since entry=signal+1)

        per_setup[setup].append(fwd_window)
        per_setup_details[setup].append({
            "ticker": ticker, "entry_date": entry_date, "fwd_window": fwd_window,
        })

    print("Derived forward_window per setup (max across examples)")
    print("=" * 66)
    for setup in LATEST_PYRAMID:
        sizes = per_setup[setup]
        if not sizes:
            print(f"  {setup:<7}  no resolved examples")
            continue
        max_fw = max(sizes)
        mean_fw = np.mean(sizes)
        dist = defaultdict(int)
        for s in sizes:
            dist[s] += 1
        dist_str = " ".join(f"{k}:{v}" for k, v in sorted(dist.items()))
        print(f"  {setup:<7}  forward_window = {max_fw}    "
              f"(n={len(sizes)}, mean={mean_fw:.2f}, distribution: {dist_str})")
    print()

    # Show the driving examples (max fwd_window) per setup
    print("Driving examples (longest cluster ending at example signal bar):")
    for setup in LATEST_PYRAMID:
        rows = per_setup_details[setup]
        if not rows: continue
        rows_sorted = sorted(rows, key=lambda r: -r["fwd_window"])
        max_fw = rows_sorted[0]["fwd_window"]
        drivers = [r for r in rows_sorted if r["fwd_window"] == max_fw]
        print(f"  {setup:<7}  max fwd_window = {max_fw}, {len(drivers)} example(s):")
        for d in drivers[:8]:
            print(f"    {d['ticker']:<6}  {d['entry_date']}  fwd_window={d['fwd_window']}")


if __name__ == "__main__":
    main()
