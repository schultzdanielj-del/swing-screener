"""Sweep tolerance values to find the one that maximizes MOC usefulness
signal in the pooled breakout test. Weight formula held at w=rvol (raw)
to remove formula-interaction noise.

Output: PNG with top5 KS mean and top10 |rho| mean as functions of
tolerance (log x-axis), plus a bar chart of per-expression KS at the best
tolerance.
"""
import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import ks_2samp, spearmanr

import random

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from moc_usefulness_test import (
    CACHE_OHLCV, EXAMPLES_DB, SETUP_CLASSES, EXPR_NAMES, FORWARD_WINDOWS,
    RANDOM_SEED,
    load_examples, resolve_signal_bar, get_dates, sample_universe_nulls,
    compute_forward_outcomes, compute_adrp20,
)
from moc_parameter_tuning import (
    build_moc_parameterized, evaluate_formula,
)


def main():
    rng = random.Random(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)

    all_examples = load_examples()
    breakouts = [(s, t, d) for s, t, d in all_examples if s in SETUP_CLASSES]

    resolved = []
    for setup, ticker, entry_date in breakouts:
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        sb = resolve_signal_bar(df, entry_date)
        if sb is None:
            continue
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        adrp = compute_adrp20(high, low)
        outcomes = compute_forward_outcomes(high, low, close, sb, adrp)
        resolved.append({"setup": setup, "ticker": ticker,
                         "entry_date": entry_date, "signal_bar": sb,
                         "outcomes": outcomes})

    signal_dates = []
    for r in resolved:
        dates = get_dates(daily_cache[r["ticker"]])
        if dates is not None:
            signal_dates.append(dates[r["signal_bar"]])
    date_range = (min(signal_dates), max(signal_dates))
    example_tickers = {r["ticker"] for r in resolved}
    null_pairs = sample_universe_nulls(daily_cache, example_tickers,
                                       date_range, rng)
    outcome_names = list(FORWARD_WINDOWS) + ["mfe_10bar", "mae_10bar",
                                             "cap_eff_10bar"]

    tols = [0.005, 0.015, 0.030, 0.050, 0.075, 0.100, 0.113, 0.150,
            0.200, 0.300, 0.500]
    weight_fn = lambda r: r  # raw rvol

    results = []
    print(f"Sweeping tolerance, weight=rvol (raw), birth>1, n_ex={len(resolved)}, n_null={len(null_pairs)}")
    for tol in tols:
        res = evaluate_formula(f"tol={tol:.3f}", tol, weight_fn, 1.0,
                               daily_cache, resolved, null_pairs,
                               outcome_names)
        res["tol"] = tol
        results.append(res)

    # Summary plot
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    tol_arr = np.array([r["tol"] for r in results])
    ks_arr = np.array([r["top5_ks"] for r in results])
    rho_arr = np.array([r["top10_abs_rho"] for r in results])

    ax = axes[0]
    ax.plot(tol_arr, ks_arr, marker="o", color="steelblue",
            label="top-5 KS mean")
    ax.plot(tol_arr, rho_arr, marker="s", color="crimson",
            label="top-10 |rho| mean")
    ax.axvline(np.sqrt(1/78), color="gray", linestyle="--",
               label=f"punt = {np.sqrt(1/78):.3f}")
    best_ks_tol = tol_arr[np.argmax(ks_arr)]
    best_rho_tol = tol_arr[np.argmax(rho_arr)]
    ax.axvline(best_ks_tol, color="steelblue", linestyle=":", alpha=0.7,
               label=f"best KS at tol={best_ks_tol:.3f}")
    ax.axvline(best_rho_tol, color="crimson", linestyle=":", alpha=0.7,
               label=f"best rho at tol={best_rho_tol:.3f}")
    ax.set_xscale("log")
    ax.set_xlabel("tolerance (ATR14 units)")
    ax.set_ylabel("signal metric")
    ax.set_title("MOC signal strength vs tolerance\n"
                 "weight = raw rvol, birth > 1")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    best = results[np.argmax(ks_arr + rho_arr)]  # combined best
    ks = best["ks_stats"]
    order = np.argsort(-np.nan_to_num(ks, nan=-1))
    y = np.arange(len(EXPR_NAMES))
    ax.barh(y, ks[order], color="steelblue", edgecolor="black", linewidth=0.3)
    ax.set_yticks(y)
    ax.set_yticklabels([EXPR_NAMES[i] for i in order], fontsize=6)
    ax.invert_yaxis()
    ax.set_xlabel("KS stat")
    ax.set_title(f"Per-expression KS at best tol={best['tol']:.3f}\n"
                 f"top-5 KS mean = {best['top5_ks']:.3f}, "
                 f"top-10 |rho| mean = {best['top10_abs_rho']:.3f}")
    ax.grid(True, axis="x", alpha=0.3)

    fig.tight_layout()
    out_path = os.path.join(HERE, "moc_tolerance_sweep.png")
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved: {out_path}")

    print("\nSweep results:")
    print(f"  {'tol':>7s}  {'top5 KS':>9s}  {'top10 |rho|':>12s}  {'combined':>10s}")
    for r in sorted(results, key=lambda x: -(x["top5_ks"] + x["top10_abs_rho"])):
        print(f"  {r['tol']:>7.3f}  {r['top5_ks']:>9.3f}  "
              f"{r['top10_abs_rho']:>12.3f}  "
              f"{r['top5_ks'] + r['top10_abs_rho']:>10.3f}")


if __name__ == "__main__":
    main()
