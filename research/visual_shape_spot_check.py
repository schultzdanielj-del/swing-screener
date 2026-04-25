"""Spot-check: overlay a random sample of each family's survivors on the
HTF example fan. Produces one chart per family showing example lead-ups in
blue and survivors in red.
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
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
COMPARE_DIR = os.path.join(WORKTREE, "research", "visual_shape_compare")

SETUP = "htf"
N_BARS = 39
SAMPLE_SURVIVORS = 40  # plot this many survivors per family (random if more exist)
RANDOM_SEED = 42


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


def build_example_paths(universe):
    examples = get_examples(SETUP)
    paths = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E_idx = lookup_idx(df, ex["entry_date"])
        if E_idx < 0:
            continue
        p = extract_path(df, E_idx, N_BARS)
        if p is not None:
            paths.append(p)
    return np.array(paths)


def load_survivors(name):
    path = os.path.join(COMPARE_DIR, f"{name}_survivors.csv")
    if not os.path.exists(path):
        return []
    return pd.read_csv(path).to_dict("records")


def plot_family(name, ex_paths, surv_paths, out_path):
    n_ex, N = ex_paths.shape
    # X: index 0 = E-1 (closest), index N-1 = E-N. Plot with x going from -N to -1.
    xs = np.arange(-N, 0)  # -N ... -1
    # path layout: ex_paths[i, 0] is p at E-1 (index 0). To put that at x=-1 on the chart,
    # reverse so x[-1] corresponds to path[0].
    def reshape_for_plot(p):
        # p is length N. p[0] = E-1, p[N-1] = E-N. Plot with x=-(k+1) for k in 0..N-1.
        # That means x = -1, -2, ..., -N. Reverse so leftmost point is E-N.
        return xs, p[::-1]  # xs[0]=-N corresponds to p[N-1]=E-N ✓

    fig, ax = plt.subplots(1, 1, figsize=(14, 7))
    fig.suptitle(
        f"HTF — example fan (blue, n={n_ex}) vs {name} survivors (red, n={len(surv_paths)}) "
        f"— log(close/close_E), N={N_BARS}",
        fontsize=13
    )

    for i in range(n_ex):
        x, y = reshape_for_plot(ex_paths[i])
        ax.plot(x, y, color='steelblue', linewidth=1.1, alpha=0.6, zorder=2)

    # envelope min/max
    emin = ex_paths.min(axis=0)[::-1]
    emax = ex_paths.max(axis=0)[::-1]
    ax.fill_between(xs, emin, emax, color='steelblue', alpha=0.10, zorder=1,
                    label="example envelope")

    for p in surv_paths:
        x, y = reshape_for_plot(p)
        ax.plot(x, y, color='crimson', linewidth=0.9, alpha=0.55, zorder=3)

    ax.axvline(x=0, color='black', linewidth=1.0, alpha=0.8)
    ax.axhline(y=0, color='black', linewidth=0.6, alpha=0.5)
    ax.set_xlabel("Bars before entry (-N to -1)", fontsize=11)
    ax.set_ylabel("log(close / close_E)", fontsize=11)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='lower left', fontsize=10)
    plt.tight_layout()
    plt.savefig(out_path, dpi=140, bbox_inches='tight')
    plt.close(fig)
    print(f"  wrote {out_path}", flush=True)


def main():
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe tickers: {len(universe)}", flush=True)

    ex_paths = build_example_paths(universe)
    print(f"Example paths: {ex_paths.shape}", flush=True)

    rng = random.Random(RANDOM_SEED)

    for name in ["F1", "F2", "F3"]:
        rows = load_survivors(name)
        if not rows:
            print(f"{name}: no survivors to plot", flush=True)
            continue
        if len(rows) > SAMPLE_SURVIVORS:
            rows = rng.sample(rows, SAMPLE_SURVIVORS)
        surv_paths = []
        for r in rows:
            df = universe.get(r["ticker"])
            if df is None:
                continue
            p = extract_path(df, int(r["E_idx"]), N_BARS)
            if p is not None:
                surv_paths.append(p)
        print(f"{name}: loaded {len(surv_paths)} survivor paths for plotting", flush=True)
        out_path = os.path.join(COMPARE_DIR, f"overlay_{name}.png")
        plot_family(name, ex_paths, surv_paths, out_path)


if __name__ == "__main__":
    main()
