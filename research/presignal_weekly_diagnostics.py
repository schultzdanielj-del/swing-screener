"""Diagnostics on DW (daily-and-weekly) survivors per setup:

1. anchor_pos distribution — measures how many survivors are short-reach
   (recent IPOs etc.) vs. full-history.
2. temporal distribution of survivor dates — by year and by month.

Also reports the same stats for the labeled examples, as a comparison.
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import presignal_weekly_aggregate as wagg
import visual_shape_compare as vsc

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
STAGE_B_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_b")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
REACH_BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 40), (40, 1_000_000)]


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def anchor_pos_for(df, E_idx, idx_cache):
    """Return weekly anchor_pos for daily E_idx on this df."""
    # cache build_ticker_weekly_indexing per-df
    if idx_cache.get("id") is not id(df):
        idx_cache["id"] = id(df)
        idx_cache["data"] = wagg.build_ticker_weekly_indexing(df)
    idx = idx_cache["data"]
    if E_idx < 1 or E_idx - 1 >= idx["L"]:
        return None
    return int(idx["w_pos_all"][E_idx - 1])


def bucket_label(ap, buckets=REACH_BUCKETS):
    for lo, hi in buckets:
        if lo <= ap < hi:
            return f"[{lo},{hi})" if hi < 1_000_000 else f"[{lo},INF)"
    return "unknown"


def year_month(date_str):
    # date_str like "2024-06-03"
    if not isinstance(date_str, str) or len(date_str) < 7:
        return None, None
    return date_str[:4], date_str[5:7]


def analyze_setup(setup, universe):
    print(f"\n{'=' * 80}", flush=True)
    print(f"=== {setup.upper()}", flush=True)
    print(f"{'=' * 80}", flush=True)

    dw_path = os.path.join(STAGE_B_DIR, f"{setup}_DW_F1_Loc_clusters.csv")
    if not os.path.exists(dw_path):
        print(f"  no DW CSV at {dw_path}", flush=True)
        return
    dw = pd.read_csv(dw_path)
    print(f"DW survivors: {len(dw):,}", flush=True)

    # anchor_pos per survivor
    reaches = []
    years = []
    months = []
    idx_cache = {}
    cur_ticker = None
    for _, row in dw.iterrows():
        t = str(row["ticker"])
        E = int(row["E_idx"])
        date = row["date"] if "date" in row else None
        df = universe.get(t)
        if df is None:
            continue
        if t != cur_ticker:
            idx_cache = {}
            cur_ticker = t
        ap = anchor_pos_for(df, E, idx_cache)
        if ap is None:
            continue
        reaches.append(ap)
        y, m = year_month(date)
        if y is not None:
            years.append(y)
            months.append(m)

    reaches = np.array(reaches)
    n = len(reaches)
    print(f"  reach-distribution over {n:,} survivors:", flush=True)
    for lo, hi in REACH_BUCKETS:
        mask = (reaches >= lo) & (reaches < hi)
        frac = mask.sum() / n if n > 0 else 0
        label = f"[{lo},{hi})" if hi < 1_000_000 else f"[{lo},INF)"
        print(f"    {label:<12} {int(mask.sum()):>10,} ({frac*100:5.1f}%)", flush=True)

    # Temporal: year
    year_counts = Counter(years)
    print(f"  year distribution:", flush=True)
    for y in sorted(year_counts):
        frac = year_counts[y] / n if n > 0 else 0
        print(f"    {y}  {year_counts[y]:>10,} ({frac*100:5.1f}%)", flush=True)

    # Examples: reach distribution
    examples = get_examples(setup)
    ex_reaches = []
    ex_years = []
    ex_months = []
    idx_cache_ex = {}
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        if id(df) not in idx_cache_ex:
            idx_cache_ex[id(df)] = wagg.build_ticker_weekly_indexing(df)
        idx = idx_cache_ex[id(df)]
        if E < 1:
            continue
        ap = int(idx["w_pos_all"][E - 1])
        ex_reaches.append(ap)
        y, m = year_month(ex["entry_date"])
        if y is not None:
            ex_years.append(y)
            ex_months.append(m)

    ex_reaches = np.array(ex_reaches)
    n_ex = len(ex_reaches)
    print(f"  EXAMPLES reach-distribution over {n_ex} examples:", flush=True)
    for lo, hi in REACH_BUCKETS:
        mask = (ex_reaches >= lo) & (ex_reaches < hi)
        frac = mask.sum() / n_ex if n_ex > 0 else 0
        label = f"[{lo},{hi})" if hi < 1_000_000 else f"[{lo},INF)"
        print(f"    {label:<12} {int(mask.sum()):>4,} ({frac*100:5.1f}%)", flush=True)

    ex_year_counts = Counter(ex_years)
    print(f"  EXAMPLES year distribution:", flush=True)
    for y in sorted(ex_year_counts):
        frac = ex_year_counts[y] / n_ex if n_ex > 0 else 0
        print(f"    {y}  {ex_year_counts[y]:>3,} ({frac*100:5.1f}%)", flush=True)


def main():
    print(f"Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)

    for setup in SETUPS:
        analyze_setup(setup, universe)


if __name__ == "__main__":
    main()
