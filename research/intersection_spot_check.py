"""Spot-check overlay for F1 INTERSECT Location clusters on the HTF example fan.
Loads both cluster CSVs, computes intersection, samples 40 non-example
survivors, plots their log(close/close_E) paths in red over the blue
example fan, and also prints aggregate path stats.
"""
from __future__ import annotations

import os
import pickle
import random
import sqlite3

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
F1_CSV = os.path.join(WORKTREE, "research", "visual_shape_compare", "F1_clusters.csv")
LOC_CSV = os.path.join(WORKTREE, "research", "location_axis", "location_clusters.csv")
OUT_DIR = os.path.join(WORKTREE, "research", "location_axis")

SETUP = "htf"
N_BARS = 39
SAMPLE = 40
RANDOM_SEED = 7


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def extract_path(df, E_idx, N):
    if E_idx < N:
        return None
    close = df["close"].values.astype(np.float64)
    close_E = close[E_idx]
    if not np.isfinite(close_E) or close_E <= 0:
        return None
    prev = close[E_idx - N:E_idx]
    if not np.all(np.isfinite(prev)) or np.any(prev <= 0):
        return None
    return (np.log(prev[::-1]) - np.log(close_E)).astype(np.float64)


def stats(paths):
    ranges = []
    mins = []
    maxs = []
    for p in paths:
        ranges.append(p.max() - p.min())
        mins.append(p.min())
        maxs.append(p.max())
    return {
        "n": len(paths),
        "range_median": float(np.median(ranges)),
        "range_p10": float(np.percentile(ranges, 10)),
        "range_p90": float(np.percentile(ranges, 90)),
        "min_median": float(np.median(mins)),
        "min_p10": float(np.percentile(mins, 10)),
        "max_median": float(np.median(maxs)),
        "max_p90": float(np.percentile(maxs, 90)),
    }


def main():
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
        universe = pickle.load(f)

    # Load cluster lists
    f1 = pd.read_csv(F1_CSV)
    loc = pd.read_csv(LOC_CSV)
    print(f"F1 clusters: {len(f1):,}", flush=True)
    print(f"Location clusters: {len(loc):,}", flush=True)

    f1_keys = set(zip(f1["ticker"].astype(str), f1["E_idx"].astype(int)))
    loc_keys = set(zip(loc["ticker"].astype(str), loc["E_idx"].astype(int)))
    inter = f1_keys & loc_keys
    print(f"F1 INTERSECT Location (bar-level): {len(inter):,}", flush=True)

    # Separate example-matched vs non-example
    # Build example (ticker, E_idx) set
    examples = get_examples(SETUP)
    ex_keys = set()
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = lookup_idx(df, ex["entry_date"])
        if E >= 0:
            ex_keys.add((ex["ticker"], E))

    inter_nonex = inter - ex_keys
    inter_ex = inter & ex_keys
    print(f"  example-matched in INTERSECT: {len(inter_ex)} / {len(ex_keys)}", flush=True)
    print(f"  non-example in INTERSECT:     {len(inter_nonex):,}", flush=True)

    # Load paths
    def to_paths(keys):
        paths = []
        for ticker, E in keys:
            df = universe.get(ticker)
            if df is None:
                continue
            p = extract_path(df, int(E), N_BARS)
            if p is not None:
                paths.append(p)
        return paths

    # All F1 paths (for F1-only reference)
    f1_paths_all = to_paths(f1_keys)
    # All F1 INTERSECT Location paths
    inter_paths_all = to_paths(inter)
    # example paths
    ex_paths = to_paths(ex_keys)

    print(f"\n=== PATH STATS ===", flush=True)
    s_ex = stats(ex_paths)
    s_f1 = stats(f1_paths_all)
    s_in = stats(inter_paths_all)
    print(f"                      n    range(p10/med/p90)    min(p10/med)     max(med/p90)", flush=True)
    print(f"  Examples:         {s_ex['n']:>4}   "
          f"{s_ex['range_p10']:.3f} / {s_ex['range_median']:.3f} / {s_ex['range_p90']:.3f}    "
          f"{s_ex['min_p10']:+.3f} / {s_ex['min_median']:+.3f}    "
          f"{s_ex['max_median']:+.3f} / {s_ex['max_p90']:+.3f}", flush=True)
    print(f"  F1-only:          {s_f1['n']:>4}   "
          f"{s_f1['range_p10']:.3f} / {s_f1['range_median']:.3f} / {s_f1['range_p90']:.3f}    "
          f"{s_f1['min_p10']:+.3f} / {s_f1['min_median']:+.3f}    "
          f"{s_f1['max_median']:+.3f} / {s_f1['max_p90']:+.3f}", flush=True)
    print(f"  F1 INTERSECT Loc: {s_in['n']:>4}   "
          f"{s_in['range_p10']:.3f} / {s_in['range_median']:.3f} / {s_in['range_p90']:.3f}    "
          f"{s_in['min_p10']:+.3f} / {s_in['min_median']:+.3f}    "
          f"{s_in['max_median']:+.3f} / {s_in['max_p90']:+.3f}", flush=True)

    # Build overlay: sample SAMPLE non-example intersection survivors
    rng = random.Random(RANDOM_SEED)
    nonex_list = sorted(inter_nonex)
    if len(nonex_list) > SAMPLE:
        nonex_list = rng.sample(nonex_list, SAMPLE)
    sample_paths = to_paths(nonex_list)
    print(f"\nSampled {len(sample_paths)} non-example intersection clusters for overlay", flush=True)

    # Plot
    ex_arr = np.array(ex_paths)
    n_ex, N = ex_arr.shape
    xs = np.arange(-N, 0)  # -N .. -1
    # reshape so index 0 (E-1) goes at x=-1; reverse for plotting
    def flip(p):
        return p[::-1]

    fig, ax = plt.subplots(1, 1, figsize=(14, 7))
    fig.suptitle(
        f"HTF — example fan (blue, n={n_ex}) vs F1INTERSECTLocation survivors (red, n={len(sample_paths)}) "
        f"— log(close/close_E), N={N_BARS}",
        fontsize=13
    )
    for i in range(n_ex):
        ax.plot(xs, flip(ex_arr[i]), color='steelblue', linewidth=1.1, alpha=0.6, zorder=2)
    emin = ex_arr.min(axis=0)[::-1]
    emax = ex_arr.max(axis=0)[::-1]
    ax.fill_between(xs, emin, emax, color='steelblue', alpha=0.10, zorder=1,
                    label="example envelope")

    for p in sample_paths:
        ax.plot(xs, flip(p), color='crimson', linewidth=0.9, alpha=0.6, zorder=3)

    ax.axvline(x=0, color='black', linewidth=1.0, alpha=0.8)
    ax.axhline(y=0, color='black', linewidth=0.6, alpha=0.5)
    ax.set_xlabel("Bars before entry (-N to -1)", fontsize=11)
    ax.set_ylabel("log(close / close_E)", fontsize=11)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='lower left', fontsize=10)
    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, "overlay_F1_INTERSECT_Location.png")
    plt.savefig(out_path, dpi=140, bbox_inches='tight')
    plt.close(fig)
    print(f"  wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
