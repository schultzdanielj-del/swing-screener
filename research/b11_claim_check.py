"""Verify the §6.1 claim attributed to B11:
   'On 100% of breakout examples, entry_close = max(close over [leftmost, entry_idx])'

Leftmost = earliest signal bar in the cluster that contains the example.
entry_idx = example signal bar + 1 (per §3.1 strict rule).

For each breakout example (htf/bf/base), reconstruct its cluster per §5.2 from the
latest pyramid JSON, locate leftmost signal bar, then test the claim against real
OHLCV data. Report hits, misses, and the shape of the misses.
"""

from __future__ import annotations

import os, sys, json, pickle, sqlite3
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
}
BREAKOUT_SETUPS = list(LATEST_PYRAMID.keys())


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def reconstruct_clusters(raw_signals):
    """Apply §5.2 dedupe: consecutive-bar per-ticker dedupe (rightmost wins).
    Returns a dict ticker -> list of clusters, each cluster = sorted list of dates.
    A "cluster" here is a run of consecutive-bar signals (no gap); gaps split clusters.
    Consecutive = date-adjacent in the ticker's trading calendar.
    """
    by_ticker = defaultdict(list)
    for s in raw_signals:
        by_ticker[s["ticker"]].append(s["date"])
    return {t: sorted(set(ds)) for t, ds in by_ticker.items()}


def find_cluster_containing(ticker_signal_dates, ticker_ohlcv_dates, target_date):
    """Find the consecutive-bar run of ticker_signal_dates that contains target_date,
    where 'consecutive' means adjacent in ticker_ohlcv_dates.
    Returns (leftmost_date, rightmost_date) or None if not found.
    """
    if target_date not in set(ticker_signal_dates):
        return None
    date_to_idx = {d: i for i, d in enumerate(ticker_ohlcv_dates)}
    sig_idxs = sorted(date_to_idx[d] for d in ticker_signal_dates if d in date_to_idx)
    if not sig_idxs:
        return None
    t_idx = date_to_idx.get(target_date)
    if t_idx is None or t_idx not in sig_idxs:
        return None
    pos = sig_idxs.index(t_idx)
    lo = pos
    while lo > 0 and sig_idxs[lo] - sig_idxs[lo - 1] == 1:
        lo -= 1
    hi = pos
    while hi < len(sig_idxs) - 1 and sig_idxs[hi + 1] - sig_idxs[hi] == 1:
        hi += 1
    return sig_idxs[lo], sig_idxs[hi]


def main():
    with sqlite3.connect(DB) as c:
        examples = [
            {"setup": s, "ticker": t, "entry_date": d}
            for s, t, d in c.execute(
                "SELECT setup_type, ticker, entry_date FROM examples "
                "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, ticker, entry_date"
            ).fetchall()
        ]

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    # Per-setup signal lists
    signals_by_setup = {}
    for setup, fn in LATEST_PYRAMID.items():
        with open(os.path.join(CACHE_DIR, fn)) as f:
            data = json.load(f)
        tiers = data["tier_results"]
        last = tiers[list(tiers.keys())[-1]]
        signals_by_setup[setup] = reconstruct_clusters(last["final_signals"])

    hits = 0
    misses = 0
    unresolvable = 0
    miss_details = []
    per_setup = defaultdict(lambda: {"hit": 0, "miss": 0, "unresolvable": 0})

    for e in examples:
        setup = e["setup"]; ticker = e["ticker"]; entry_date = e["entry_date"]
        df = universe.get(ticker)
        if df is None:
            per_setup[setup]["unresolvable"] += 1; unresolvable += 1; continue
        dates_str = ohlcv_dates_str(df)
        if entry_date not in set(dates_str):
            per_setup[setup]["unresolvable"] += 1; unresolvable += 1; continue
        entry_idx = int(np.where(dates_str == entry_date)[0][0])
        signal_idx = entry_idx - 1
        if signal_idx < 0:
            per_setup[setup]["unresolvable"] += 1; unresolvable += 1; continue

        ticker_signals = signals_by_setup[setup].get(ticker, [])
        signal_date = dates_str[signal_idx]
        bounds = find_cluster_containing(ticker_signals, dates_str, signal_date)
        if bounds is None:
            per_setup[setup]["unresolvable"] += 1; unresolvable += 1; continue
        leftmost_idx, rightmost_idx = bounds

        closes = df["close"].values[leftmost_idx:entry_idx + 1]
        entry_close = closes[-1]
        max_close = float(np.max(closes))

        if entry_close >= max_close - 1e-9:
            # claim hits — entry_close is the max (or tied for max)
            hits += 1
            per_setup[setup]["hit"] += 1
            # Is this a strict max or a tie? And how does entry_close compare to signal_close?
            signal_close = float(df["close"].values[signal_idx])
            strict = entry_close > max_close - 1e-9 and entry_close > signal_close + 1e-9
            miss_details.append({  # repurpose: record hit structure too
                "hit_type": "strict" if strict else "tied_or_flat",
                "setup": setup, "ticker": ticker, "entry_date": entry_date,
                "cluster_bars": entry_idx - leftmost_idx,
                "entry_close": round(float(entry_close), 4),
                "signal_close": round(signal_close, 4),
                "max_prior_close": round(float(np.max(df["close"].values[leftmost_idx:entry_idx])), 4),
            })
        else:
            misses += 1
            per_setup[setup]["miss"] += 1
            argmax_offset = int(np.argmax(closes))
            miss_details.append({
                "setup": setup, "ticker": ticker, "entry_date": entry_date,
                "leftmost_date": dates_str[leftmost_idx],
                "signal_date": signal_date,
                "cluster_bars": entry_idx - leftmost_idx,
                "entry_close": round(float(entry_close), 4),
                "max_close": round(max_close, 4),
                "gap_pct": round(100 * (max_close - entry_close) / max_close, 2),
                "argmax_bar_from_leftmost": argmax_offset,
            })

    print("=" * 70)
    print("B11 claim check: entry_close == max(close over [leftmost, entry_idx])")
    print("=" * 70)
    total_resolved = hits + misses
    print(f"Total resolved: {total_resolved}  (unresolvable: {unresolvable})")
    print(f"Hits:  {hits}  ({100*hits/max(total_resolved,1):.1f}%)")
    print(f"Misses: {misses}  ({100*misses/max(total_resolved,1):.1f}%)")
    print()
    print("Per setup:")
    for setup in BREAKOUT_SETUPS:
        d = per_setup[setup]
        tot = d["hit"] + d["miss"]
        pct = 100 * d["hit"] / max(tot, 1)
        print(f"  {setup:<6}  hit={d['hit']:>3}  miss={d['miss']:>3}  unresolvable={d['unresolvable']:>3}  hit_pct={pct:.1f}%")
    print()

    if miss_details:
        print(f"First {min(30, len(miss_details))} misses:")
        print(f"  {'setup':<6} {'ticker':<6} {'entry_date':<11} {'cluster_bars':<12} "
              f"{'entry_close':>11} {'max_close':>10} {'gap%':>6} {'argmax_bar':>10}")
        for m in miss_details[:30]:
            print(f"  {m['setup']:<6} {m['ticker']:<6} {m['entry_date']:<11} {m['cluster_bars']:<12} "
                  f"{m['entry_close']:>11} {m['max_close']:>10} {m['gap_pct']:>6} {m['argmax_bar_from_leftmost']:>10}")


if __name__ == "__main__":
    main()
