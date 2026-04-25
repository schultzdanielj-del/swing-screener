"""Verify Dan's fade entry mechanic: entry bar = highest-high bar in forward window.

For each fade example, forward window = [signal+1, min(earnings-1, +120 bars)].
Check: is entry_idx (from DB) the bar with max(high) in that window?
Tie policy: if multiple bars tie for max, check if entry_idx is among them.
"""
from __future__ import annotations

import os, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
MAX_FORWARD = 120
FADE_SETUPS = {"dtss", "3-4db"}


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
        ern = defaultdict(list)
        for tk, d in c.execute("SELECT ticker, earnings_date FROM earnings_dates"):
            ern[tk].append(str(d)[:10])
    ern = {t: np.array(sorted(set(v))) for t, v in ern.items()}

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    per_setup = defaultdict(lambda: {"exact_max": 0, "tied_for_max": 0, "not_max": 0, "unresolvable": 0})
    misses = []

    for e in examples:
        setup = e["setup"]; ticker = e["ticker"]; entry_date = e["entry_date"]
        df = universe.get(ticker)
        if df is None:
            per_setup[setup]["unresolvable"] += 1; continue
        dates = ohlcv_dates_str(df)
        if entry_date not in set(dates):
            per_setup[setup]["unresolvable"] += 1; continue
        entry_idx = int(np.where(dates == entry_date)[0][0])
        signal_idx = entry_idx - 1
        if signal_idx < 0:
            per_setup[setup]["unresolvable"] += 1; continue

        # Forward window: [signal+1, min(earnings-1, signal+MAX_FORWARD)]
        ern_arr = ern.get(ticker)
        window_end = min(signal_idx + MAX_FORWARD, len(df) - 1)
        if ern_arr is not None and len(ern_arr) > 0:
            signal_date = dates[signal_idx]
            pos = int(np.searchsorted(ern_arr, signal_date, side="right"))
            if pos < len(ern_arr):
                ern_date = ern_arr[pos]
                # Find bar index of earnings date
                ern_bar = int(np.searchsorted(dates, ern_date, side="left"))
                if 0 < ern_bar - 1 <= window_end:
                    window_end = ern_bar - 1
        window_start = signal_idx + 1
        if window_end < window_start:
            per_setup[setup]["unresolvable"] += 1; continue

        highs = df["high"].values[window_start:window_end + 1]
        max_high = float(np.max(highs))
        entry_high = float(df["high"].values[entry_idx])

        # Is entry bar the (or tied for) highest-high bar?
        if entry_high == max_high:
            # Check if strictly unique or tied
            n_ties = int((highs == max_high).sum())
            if n_ties == 1:
                per_setup[setup]["exact_max"] += 1
            else:
                per_setup[setup]["tied_for_max"] += 1
        else:
            per_setup[setup]["not_max"] += 1
            # Where is the actual max?
            arg = int(np.argmax(highs))
            peak_idx = window_start + arg
            peak_off = peak_idx - signal_idx  # offset from signal bar (entry_idx = signal+1 by DB)
            entry_off = 1  # by DB convention
            gap_pct = 100 * (max_high - entry_high) / max_high
            misses.append({
                "setup": setup, "ticker": ticker, "entry_date": entry_date,
                "entry_high": round(entry_high, 4),
                "max_high": round(max_high, 4),
                "gap_pct": round(gap_pct, 2),
                "peak_offset_from_signal": peak_off,
                "window_len_bars": window_end - window_start + 1,
            })

    print("Fade entry-mechanic verify — is DB entry_idx the highest-high bar in forward window?")
    print("=" * 84)
    print(f"{'setup':<8} {'exact':>7} {'tied':>6} {'not_max':>8} {'unresolvable':>13}")
    for setup, d in per_setup.items():
        tot = d["exact_max"] + d["tied_for_max"] + d["not_max"]
        print(f"{setup:<8} {d['exact_max']:>7} {d['tied_for_max']:>6} {d['not_max']:>8} {d['unresolvable']:>13}    (n_resolved={tot})")
    print()

    if misses:
        print(f"Misses — entry bar is NOT the highest-high bar ({len(misses)} examples):")
        print(f"  {'setup':<7} {'ticker':<6} {'entry_date':<11} {'entry_high':>11} {'max_high':>10} {'gap%':>5} {'peak_off':>8} {'window':>7}")
        for m in misses[:40]:
            print(f"  {m['setup']:<7} {m['ticker']:<6} {m['entry_date']:<11} {m['entry_high']:>11} {m['max_high']:>10} "
                  f"{m['gap_pct']:>5} {m['peak_offset_from_signal']:>8} {m['window_len_bars']:>7}")


if __name__ == "__main__":
    main()
