"""Diagnose which HTF examples are missing from F1 and Location cluster CSVs,
and why (history length, date, etc).
"""
from __future__ import annotations

import os
import pickle
import sqlite3

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
F1_CSV = os.path.join(WORKTREE, "research", "visual_shape_compare", "F1_clusters.csv")
LOC_CSV = os.path.join(WORKTREE, "research", "location_axis", "location_clusters.csv")
N_BARS = 39
DATE_CUTOFF = "2020-01-02"


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
    universe = pickle.load(f)

conn = sqlite3.connect(DB)
exs = conn.execute("SELECT ticker, entry_date FROM examples WHERE setup_type=?", ("htf",)).fetchall()
conn.close()

f1 = pd.read_csv(F1_CSV)
loc = pd.read_csv(LOC_CSV)
f1_keys = set(zip(f1["ticker"].astype(str), f1["E_idx"].astype(int)))
loc_keys = set(zip(loc["ticker"].astype(str), loc["E_idx"].astype(int)))

print(f"{'ticker':<8}{'entry_date':<14}{'E_idx':>7}{'hist':>6}  {'date>=cut':<10}{'inF1':<6}{'inLoc':<6}{'note'}")
for t, d in exs:
    df = universe.get(t)
    if df is None:
        print(f"{t:<8}{d:<14}{'--':>7}{'--':>6}  {'--':<10}{'--':<6}{'--':<6}no df in universe")
        continue
    E = lookup_idx(df, d)
    if E < 0:
        print(f"{t:<8}{d:<14}{'--':>7}{'--':>6}  {'--':<10}{'--':<6}{'--':<6}entry_date not in df")
        continue
    hist = E  # bars before E (since E is 0-indexed, there are E bars before it)
    date_ok = str(d) >= DATE_CUTOFF
    in_f1 = (t, E) in f1_keys
    in_loc = (t, E) in loc_keys
    note = ""
    if hist < N_BARS:
        note = f"hist<N={N_BARS}"
    elif not date_ok:
        note = "pre-cutoff"
    elif not in_f1 and not in_loc:
        note = "MISSING from both"
    elif in_f1 and not in_loc:
        note = "in F1, NOT in Loc"
    elif not in_f1 and in_loc:
        note = "in Loc, NOT in F1"
    print(f"{t:<8}{str(d):<14}{E:>7}{hist:>6}  {str(date_ok):<10}{str(in_f1):<6}{str(in_loc):<6}{note}")
