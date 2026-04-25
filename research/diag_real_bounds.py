"""Rebuild bounds at full precision, scan CR+CHEF+XPEV+CRCL through
scan_ticker, print exactly which examples fail. Pinpoint the 2 missing.
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from location_axis import (
    precompute_ticker, scan_ticker,
    desc_1_pos, desc_2_trend, desc_3_tsh_single,
    desc_4_ath_atl, desc_5_vol_ratio,
    dates_as_str, lookup_idx, get_examples,
    derive_horizon, bounds_at_M, bounds_no_M,
    M_SWEEP, N_BARS,
)

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DATE_CUTOFF = "2020-01-02"

with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
    universe = pickle.load(f)

examples = get_examples("htf")
ex_rows = []
for ex in examples:
    df = universe.get(ex["ticker"])
    if df is None:
        continue
    E = lookup_idx(df, ex["entry_date"])
    if E < 0:
        continue
    close = df["close"].values.astype(np.float64)
    log_returns = np.diff(np.log(np.where(close > 0, close, np.nan)))
    ex_rows.append({"ticker": ex["ticker"], "entry_date": ex["entry_date"],
                    "E_idx": E, "close": close, "log_returns": log_returns})

print(f"Loaded {len(ex_rows)} examples")

# Derive M*
fn_d1 = lambda r, M: desc_1_pos(r["close"], r["E_idx"], M)
fn_d2 = lambda r, M: desc_2_trend(r["close"], r["E_idx"], N_BARS, M)
fn_d5 = lambda r, M: desc_5_vol_ratio(r["log_returns"], r["E_idx"], M)
M1, _, _ = derive_horizon(ex_rows, fn_d1, "D1")
M2, _, _ = derive_horizon(ex_rows, fn_d2, "D2")
M5, _, _ = derive_horizon(ex_rows, fn_d5, "D5")

# Bounds at full precision
D1_lo, D1_hi, _ = bounds_at_M(ex_rows, fn_d1, M1)
D2_lo, D2_hi, _ = bounds_at_M(ex_rows, fn_d2, M2)
D3_lo, D3_hi, _ = bounds_no_M(ex_rows, lambda r: desc_3_tsh_single(r["close"], r["E_idx"]))
D4_vals = [desc_4_ath_atl(r["close"], r["E_idx"]) for r in ex_rows]
D4a_vals = np.array([v[0] for v in D4_vals])
D4b_vals = np.array([v[1] for v in D4_vals])
D4a_lo = float(np.nanmin(D4a_vals)); D4a_hi = float(np.nanmax(D4a_vals))
D4b_lo = float(np.nanmin(D4b_vals)); D4b_hi = float(np.nanmax(D4b_vals))
D5_lo, D5_hi, _ = bounds_at_M(ex_rows, fn_d5, M5)

bounds = {
    "D1": (D1_lo, D1_hi), "D2": (D2_lo, D2_hi), "D3": (D3_lo, D3_hi),
    "D4a": (D4a_lo, D4a_hi), "D4b": (D4b_lo, D4b_hi), "D5": (D5_lo, D5_hi),
}
print(f"\nFull-precision bounds:")
for k, (lo, hi) in bounds.items():
    print(f"  {k}: lo={lo!r}  hi={hi!r}")

# Scan every example's ticker and check if its E appears in passed set
print(f"\nChecking each of {len(ex_rows)} examples:")
missing = []
for r in ex_rows:
    df = universe[r["ticker"]]
    pre = precompute_ticker(df)
    if pre is None:
        print(f"  {r['ticker']:<8} {r['entry_date']:<12} E={r['E_idx']:<5}  PRECOMPUTE=None")
        missing.append(r)
        continue
    ds = dates_as_str(df)
    res = scan_ticker(pre, N_BARS, M1, M2, M5, bounds, DATE_CUTOFF, ds)
    if res is None:
        print(f"  {r['ticker']:<8} {r['entry_date']:<12} E={r['E_idx']:<5}  SCAN=None")
        missing.append(r)
        continue
    idx = np.where(res["E"] == r["E_idx"])[0]
    if len(idx) == 0:
        print(f"  {r['ticker']:<8} {r['entry_date']:<12} E={r['E_idx']:<5}  E-not-in-E_range")
        missing.append(r)
        continue
    i = idx[0]
    ap = bool(res["all_pass"][i])
    note = ""
    if not ap:
        fails = [n for n in ["1", "2", "3", "4a", "4b", "5"] if not bool(res["p" + n][i])]
        note = "FAIL on " + ",".join(fails)
        for n in fails:
            v = res["D" + n][i]
            print(f"    {r['ticker']} D{n}={v}  bound={bounds['D'+n]}")
        missing.append(r)
    print(f"  {r['ticker']:<8} {r['entry_date']:<12} E={r['E_idx']:<5}  all_pass={ap}  {note}")

print(f"\nMissing total: {len(missing)}")
for r in missing:
    print(f"  {r['ticker']} {r['entry_date']}")
