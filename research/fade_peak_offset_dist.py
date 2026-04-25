"""Derive fade entry-discovery window from peak-offset distribution on WILD signals.

For each wild (non-example) fade signal, find the argmax offset of high in
[signal+1, signal+40]. Histogram the result per setup. A natural entry-discovery
window shows as a mode (cluster) at early offsets with a tail-off, after which
offsets become flat or dropoff is just from the windowing artifact.
"""

from __future__ import annotations

import os, json, pickle, sqlite3
from collections import defaultdict, Counter

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
MAX_OFFSET = 40
FADE_SETUPS = {
    "dtss":  "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db": "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def main():
    with sqlite3.connect(DB) as c:
        examples = {
            (setup, ticker, entry_date)
            for setup, ticker, entry_date in c.execute(
                "SELECT setup_type, ticker, entry_date FROM examples "
                "WHERE setup_type IN ('dtss','3-4db')"
            ).fetchall()
        }
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    for setup, fn in FADE_SETUPS.items():
        with open(os.path.join(CACHE_DIR, fn)) as f:
            pyr = json.load(f)
        tiers = pyr["tier_results"]
        last = tiers[list(tiers.keys())[-1]]
        signals = last["final_signals"]

        # Build set of example signal dates per ticker (signal = entry_date - 1 bar)
        ex_signal_dates = defaultdict(set)
        for s, t, d in examples:
            if s != setup: continue
            df = universe.get(t)
            if df is None: continue
            dates = ohlcv_dates_str(df)
            if d not in set(dates): continue
            eidx = int(np.where(dates == d)[0][0])
            if eidx - 1 >= 0:
                ex_signal_dates[t].add(dates[eidx - 1])

        peak_offsets = []
        ex_peak_offsets = []  # For comparison
        for s in signals:
            ticker, date = s["ticker"], s["date"]
            df = universe.get(ticker)
            if df is None: continue
            dates = ohlcv_dates_str(df)
            if date not in set(dates): continue
            sidx = int(np.where(dates == date)[0][0])
            end = min(sidx + MAX_OFFSET, len(df) - 1)
            if end - sidx < 1: continue
            fwd_highs = df["high"].values[sidx + 1: end + 1]
            if len(fwd_highs) == 0: continue
            argmax_offset = int(np.argmax(fwd_highs)) + 1  # 1-indexed from signal
            is_example = (date in ex_signal_dates.get(ticker, set()))
            if is_example:
                ex_peak_offsets.append(argmax_offset)
            else:
                peak_offsets.append(argmax_offset)

        # Histogram
        print(f"\n=== {setup} ===  (non-example signals: n={len(peak_offsets)}, examples: n={len(ex_peak_offsets)})")
        print(f"  {'offset':>6}  {'wild':>8} {'wild_cum':>10} {'example':>8} {'ex_cum':>8}")
        wild_counter = Counter(peak_offsets)
        ex_counter = Counter(ex_peak_offsets)
        wild_total = len(peak_offsets)
        ex_total = len(ex_peak_offsets)
        wild_cum = 0
        ex_cum = 0
        for n in range(1, MAX_OFFSET + 1):
            w = wild_counter.get(n, 0)
            e = ex_counter.get(n, 0)
            wild_cum += w
            ex_cum += e
            w_pct = 100 * w / wild_total if wild_total else 0
            wc_pct = 100 * wild_cum / wild_total if wild_total else 0
            e_pct = 100 * e / ex_total if ex_total else 0
            ec_pct = 100 * ex_cum / ex_total if ex_total else 0
            bar = '#' * int(w_pct)
            print(f"  {n:>6}  {w:>3} {w_pct:>3.0f}% {wc_pct:>4.0f}%cum  {e:>3} {e_pct:>3.0f}%  {ec_pct:>4.0f}%cum  {bar}")


if __name__ == "__main__":
    main()
