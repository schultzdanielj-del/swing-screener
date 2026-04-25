"""Derive fade entry-discovery window from examples.

For each fade example, the mechanic picks the highest-high bar in window [signal+1, signal+N].
Sweep N from 1 upward; for each N, count how many examples have DB entry_idx == that highest bar.
Report the match-rate curve and identify the plateau (smallest N where further increases
stop gaining misses — i.e. the natural entry window size).
"""
from __future__ import annotations

import os, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
FADE_SETUPS = ("dtss", "3-4db")
MAX_N = 40


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
                "WHERE setup_type IN ('dtss','3-4db') ORDER BY setup_type, ticker, entry_date"
            ).fetchall()
        ]
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    # Pre-load each example's signal+1 high and the subsequent forward highs
    per_setup = defaultdict(list)  # {setup: [ (entry_high, [forward_highs_offset_2..N]) ]}
    for e in examples:
        setup = e["setup"]; ticker = e["ticker"]; entry_date = e["entry_date"]
        df = universe.get(ticker)
        if df is None: continue
        dates = ohlcv_dates_str(df)
        if entry_date not in set(dates): continue
        entry_idx = int(np.where(dates == entry_date)[0][0])
        signal_idx = entry_idx - 1
        if signal_idx < 0: continue
        # Forward-highs array: index 0 = signal+1 (= entry bar by DB) = offset 1 from signal
        max_end = min(signal_idx + MAX_N, len(df) - 1)
        forward_highs = df["high"].values[signal_idx + 1: max_end + 1]
        if len(forward_highs) == 0: continue
        per_setup[setup].append(forward_highs)

    # For each setup and each N, compute match rate
    print("Fade entry window sweep: fraction of examples where DB entry bar (signal+1) is the")
    print("highest-high bar in window [signal+1, signal+N]")
    print("=" * 78)
    print(f"{'N':>3}  " + "  ".join(f"{s:>13}" for s in FADE_SETUPS) + f"  {'combined':>13}")
    results_by_setup = {}
    combined_hits = [0] * (MAX_N + 1)
    combined_totals = [0] * (MAX_N + 1)
    for setup in FADE_SETUPS:
        rows = per_setup[setup]
        hits_by_N = []
        for N in range(1, MAX_N + 1):
            hits = 0
            total = 0
            for fh in rows:
                if len(fh) < 1: continue
                window = fh[:N]  # offsets 1..N from signal
                if len(window) == 0: continue
                total += 1
                if window[0] >= window.max() - 1e-9:  # signal+1 is (tied for) max
                    hits += 1
            hits_by_N.append((hits, total))
        results_by_setup[setup] = hits_by_N

    for N in range(1, MAX_N + 1):
        parts = []
        total_hits = 0
        total_n = 0
        for setup in FADE_SETUPS:
            h, t = results_by_setup[setup][N - 1]
            pct = 100 * h / t if t else 0
            parts.append(f"{h:>3}/{t:<3} {pct:>4.1f}%")
            total_hits += h; total_n += t
        comb = f"{total_hits:>3}/{total_n:<3} {100*total_hits/total_n if total_n else 0:>4.1f}%"
        print(f"{N:>3}  " + "  ".join(parts) + f"  {comb}")

    # Plateau detection: smallest N where match rate stops dropping
    # i.e. results_by_setup[...][N] == results_by_setup[...][N-1]
    print()
    print("Plateau (smallest N where adding another bar doesn't introduce a new miss):")
    for setup in FADE_SETUPS:
        hits_by_N = results_by_setup[setup]
        plateau = None
        for i in range(1, len(hits_by_N)):
            if hits_by_N[i][0] == hits_by_N[i - 1][0]:
                plateau = i + 1  # the N at index i-1 (smallest unchanged)
                break
        if plateau is not None:
            h, t = hits_by_N[plateau - 1]
            print(f"  {setup:<7}  plateau at N={plateau}  ({h}/{t} = {100*h/t:.1f}%)")
        else:
            print(f"  {setup:<7}  no plateau within sweep N<={MAX_N}")


if __name__ == "__main__":
    main()
