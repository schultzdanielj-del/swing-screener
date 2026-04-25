"""Visualize P(bounce | cross_count = N) curves for MOC retest-survival research.

Imports compute_survival from both existing research modules (loose v1 and
strict v3-decisive) so no level-building or bounce-detection logic is
duplicated. One PNG per ticker, two side-by-side panels (loose | strict).
Wilson 95% CI band, dot size proportional to sqrt(n), dashed baseline at
the N=0 bucket bounce rate, sample count annotated per dot. No sample-size
floor — every observed N is shown.
"""
import os
import pickle
import sys
import time
from math import sqrt

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import retest_survival_extended as loose_mod
import retest_survival_v3_decisive as strict_mod

CACHE_OHLCV = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl"
TICKERS = ["F", "SPY", "TSLA"]


def wilson_ci(n_success, n_total, z=1.96):
    if n_total == 0:
        return (np.nan, np.nan, np.nan)
    p = n_success / n_total
    denom = 1 + z * z / n_total
    center = (p + z * z / (2 * n_total)) / denom
    half = z * sqrt(p * (1 - p) / n_total + z * z / (4 * n_total * n_total)) / denom
    return (p, max(0.0, center - half), min(1.0, center + half))


def buckets_to_curve(buckets):
    if not buckets:
        return np.array([]), np.array([]), np.array([]), np.array([]), np.array([])
    sorted_cc = sorted(buckets.keys())
    cc_arr = np.array(sorted_cc, dtype=float)
    p_arr = np.zeros(len(sorted_cc))
    lo_arr = np.zeros(len(sorted_cc))
    hi_arr = np.zeros(len(sorted_cc))
    n_arr = np.zeros(len(sorted_cc), dtype=int)
    for i, cc in enumerate(sorted_cc):
        nb, nk = buckets[cc]
        n = nb + nk
        p, lo, hi = wilson_ci(nb, n)
        p_arr[i], lo_arr[i], hi_arr[i], n_arr[i] = p, lo, hi, n
    return cc_arr, p_arr, lo_arr, hi_arr, n_arr


def plot_panel(ax, buckets, title):
    cc, p, lo, hi, n = buckets_to_curve(buckets)
    if len(cc) == 0:
        ax.text(0.5, 0.5, "no data", ha="center", va="center",
                transform=ax.transAxes, color="gray")
        ax.set_title(title, fontsize=11)
        return
    if 0 in buckets:
        nb0, nk0 = buckets[0]
        n0 = nb0 + nk0
        base_p = nb0 / n0 if n0 > 0 else np.nan
        if not np.isnan(base_p):
            ax.axhline(base_p, color="red", linestyle="--", linewidth=1.2,
                       alpha=0.75, label=f"N=0 baseline ({base_p:.2f}, n={n0})")

    ax.fill_between(cc, lo, hi, alpha=0.22, color="steelblue",
                    step="mid", label="95% CI")
    sizes = np.clip(np.sqrt(n) * 7.0, 18, 260)
    ax.scatter(cc, p, s=sizes, color="steelblue", edgecolor="navy",
               linewidth=0.6, zorder=3)
    for xi, yi, ni in zip(cc, p, n):
        ax.annotate(str(ni), (xi, yi), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=7, color="dimgray")

    ax.set_ylim(0, 1)
    ax.set_xlim(-0.5, cc.max() + 0.5)
    ax.set_xlabel("cross_count N", fontsize=10)
    ax.set_ylabel("P(bounce)", fontsize=10)
    ax.set_title(title, fontsize=11)
    ax.tick_params(axis="both", labelsize=9)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8, loc="lower left", framealpha=0.85)


def run_ticker(ticker, df):
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    volume = df["volume"].values.astype(float)

    t0 = time.time()
    loose_buckets, _ = loose_mod.compute_survival(high, low, close, volume)
    t1 = time.time()
    strict_buckets, _ = strict_mod.compute_survival(high, low, close, volume)
    t2 = time.time()

    loose_total = sum(nb + nk for nb, nk in loose_buckets.values())
    strict_total = sum(nb + nk for nb, nk in strict_buckets.values())
    print(f"  {ticker} LOOSE : {len(loose_buckets):3d} buckets, "
          f"{loose_total:5d} retests, max_N={max(loose_buckets) if loose_buckets else 0}, "
          f"{t1 - t0:.1f}s")
    print(f"  {ticker} STRICT: {len(strict_buckets):3d} buckets, "
          f"{strict_total:5d} retests, max_N={max(strict_buckets) if strict_buckets else 0}, "
          f"{t2 - t1:.1f}s")
    return loose_buckets, strict_buckets


def main():
    print("Loading universe_ohlcv_daily.pkl...")
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)

    for ticker in TICKERS:
        if ticker not in daily_cache:
            print(f"\n{ticker}: MISSING in cache — skipped")
            continue
        print(f"\n{ticker}:")
        df = daily_cache[ticker]
        loose_buckets, strict_buckets = run_ticker(ticker, df)

        fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
        plot_panel(axes[0], loose_buckets, f"{ticker} — LOOSE (v1)")
        plot_panel(axes[1], strict_buckets, f"{ticker} — STRICT (v3 decisive)")
        fig.suptitle(
            f"{ticker}  MOC retest-survival: P(bounce) vs cross_count\n"
            "Wilson 95% CI band | dot size ∝ √n | dashed = N=0 baseline | n annotated per dot",
            fontsize=11,
        )
        fig.tight_layout(rect=[0, 0, 1, 0.94])
        out_path = os.path.join(HERE, f"retest_survival_{ticker}.png")
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        print(f"  saved: {out_path}")


if __name__ == "__main__":
    main()
