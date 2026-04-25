"""Compute each location descriptor value for CHEF, CR, XPEV at their entry
bars. Compare single-eval results (sanity path) with what the scan would
produce. Pinpoint why the scan rejects these examples.
"""
from __future__ import annotations

import os
import pickle
import sqlite3

import numpy as np
import pandas as pd

import sys
sys.path.insert(0, os.path.dirname(__file__))
from location_axis import (
    precompute_ticker, scan_ticker, desc_1_pos, desc_2_trend,
    desc_3_tsh_single, desc_4_ath_atl, desc_5_vol_ratio,
    dates_as_str, lookup_idx,
)

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
N_BARS = 39
M1, M2, M5 = 80, 190, 71
BOUNDS = {
    "D1":  (0.8115, 1.7184),
    "D2":  (-2.7546, 1.3621),
    "D3":  (3, 504),
    "D4a": (-2.9054, 0.0000),
    "D4b": (0.3561, 4.7335),
    "D5":  (0.4616, 2.1202),
}

with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
    universe = pickle.load(f)

for ticker, entry_date in [("CHEF", "2024-12-03"), ("CR", "2023-10-31"), ("XPEV", "2020-11-12")]:
    df = universe[ticker]
    E = lookup_idx(df, entry_date)
    close = df["close"].values.astype(np.float64)
    log_returns = np.diff(np.log(np.where(close > 0, close, np.nan)))
    print(f"\n=== {ticker}  E={E}  entry={entry_date}  L={len(close)} ===")

    # Single-eval
    v1 = desc_1_pos(close, E, M1)
    v2 = desc_2_trend(close, E, N_BARS, M2)
    v3 = desc_3_tsh_single(close, E)
    v4a, v4b = desc_4_ath_atl(close, E)
    v5 = desc_5_vol_ratio(log_returns, E, M5)
    for name, v, bnd in [
        ("D1", v1, BOUNDS["D1"]),
        ("D2", v2, BOUNDS["D2"]),
        ("D3", v3, BOUNDS["D3"]),
        ("D4a", v4a, BOUNDS["D4a"]),
        ("D4b", v4b, BOUNDS["D4b"]),
        ("D5", v5, BOUNDS["D5"]),
    ]:
        nanpass = v is None or (isinstance(v, float) and not np.isfinite(v))
        inb = nanpass or (bnd[0] <= v <= bnd[1])
        mark = "PASS" if inb else "FAIL"
        print(f"  single {name}: value={v}   bounds={bnd}   {mark}")

    # Scan-eval via scan_ticker
    pre = precompute_ticker(df)
    if pre is None:
        print("  precompute_ticker returned None")
        continue
    ds = dates_as_str(df)
    res = scan_ticker(pre, N_BARS, M1, M2, M5, BOUNDS, "2020-01-02", ds)
    if res is None:
        print("  scan_ticker returned None")
        continue
    # find the index where E matches
    match = np.where(res["E"] == E)[0]
    if len(match) == 0:
        print(f"  E={E} NOT in scan's E_range (date filter or history issue)")
        continue
    idx = match[0]
    for name in ["D1", "D2", "D3", "D4a", "D4b", "D5"]:
        val = res[name][idx]
        p = res["p" + name[1:]][idx]  # p1, p2, p3, p4a, p4b, p5
        print(f"  scan   {name}: value={val:.4f}" if np.isfinite(val) else f"  scan   {name}: value=NaN",
              f"   pass={p}")
    print(f"  scan all_pass = {res['all_pass'][idx]}")
