"""Test whether amplifying-contrast weight functions strengthen MOC signal.

Under-tested direction: weight functions that disproportionately reward
high-RVOL contributors (exponentials + power laws). Prior comparison only
tried linear/mild formulas and one shifted-quadratic.

Tested at two tolerances so contrast effect isn't masked by tolerance.
"""
import os
import pickle
import sys
import random
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from moc_usefulness_test import (
    CACHE_OHLCV, SETUP_CLASSES, EXPR_NAMES, FORWARD_WINDOWS, RANDOM_SEED,
    load_examples, resolve_signal_bar, get_dates, sample_universe_nulls,
    compute_forward_outcomes, compute_adrp20,
)
from moc_parameter_tuning import evaluate_formula


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

    variants = [
        ("BASELINE raw rvol        (no contrast)", lambda r: r),
        ("mild      rvol^1.5",                      lambda r: r ** 1.5),
        ("contrast  rvol^2",                        lambda r: r ** 2),
        ("strong    rvol^3",                        lambda r: r ** 3),
        ("exp       e^(rvol-1)",                    lambda r: np.exp(r - 1)),
        ("exp-soft  e^(rvol/2)",                    lambda r: np.exp(r / 2)),
        ("exp-sharp e^(rvol/1)",                    lambda r: np.exp(r)),
    ]

    punt_tol = float(np.sqrt(1 / 78))
    tols_to_test = [("tol=0.113(punt)", punt_tol),
                    ("tol=0.005(derived)", 0.005)]

    print(f"n_examples={len(resolved)}, n_null={len(null_pairs)}\n")
    all_results = []
    for tol_name, tol in tols_to_test:
        print(f"=== {tol_name} (tol={tol:.4f}) ===")
        for v_name, wfn in variants:
            res = evaluate_formula(f"{v_name} @ {tol_name}",
                                   tol, wfn, 1.0,
                                   daily_cache, resolved, null_pairs,
                                   outcome_names)
            res["variant"] = v_name
            res["tol_name"] = tol_name
            res["tol"] = tol
            all_results.append(res)
        print()

    # Summary table
    print("\n=== RANKED BY COMBINED (top5 KS + top10 |rho|) ===")
    print(f"  {'variant':<40s}  {'tol':>8s}  {'KS':>6s}  {'|rho|':>7s}  {'sum':>6s}")
    for r in sorted(all_results,
                    key=lambda x: -(x["top5_ks"] + x["top10_abs_rho"])):
        print(f"  {r['variant']:<40s}  {r['tol']:>8.4f}  "
              f"{r['top5_ks']:>6.3f}  {r['top10_abs_rho']:>7.3f}  "
              f"{r['top5_ks'] + r['top10_abs_rho']:>6.3f}")

    # Summary plot: lines for KS and |rho| vs variant, one series per tolerance
    fig, ax = plt.subplots(figsize=(13, 6))
    variant_names = [v for v, _ in variants]
    x = np.arange(len(variant_names))

    for tol_name, tol in tols_to_test:
        ks = [r["top5_ks"] for r in all_results
              if r["tol_name"] == tol_name]
        rho = [r["top10_abs_rho"] for r in all_results
               if r["tol_name"] == tol_name]
        marker = "o" if "punt" in tol_name else "s"
        ax.plot(x, ks, marker=marker, linestyle="-",
                label=f"top-5 KS @ {tol_name}")
        ax.plot(x, rho, marker=marker, linestyle="--",
                label=f"top-10 |rho| @ {tol_name}")

    ax.set_xticks(x)
    ax.set_xticklabels(variant_names, rotation=25, ha="right", fontsize=9)
    ax.set_ylabel("signal metric")
    ax.set_title("MOC contrast-amplification weight-function sweep\n"
                 f"pooled breakouts (n={len(resolved)}), "
                 f"null bars n={len(null_pairs)}")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9, loc="best")
    fig.tight_layout()
    out_path = os.path.join(HERE, "moc_contrast_weight.png")
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
