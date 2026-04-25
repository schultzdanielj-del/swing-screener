"""Derive tolerance and test alternative weight formulas for MOC.

Phase 1 — tolerance:
  Pool every high-RVOL D1 H/L print across breakout-example tickers.
  For each ticker, sort prints by price, compute consecutive-neighbor
  distances in ATR14 units (at the time of the later print). Pool.
  Identify the natural scale at which neighboring prints separate into
  distinct levels (elbow / mode-separation), then propose a tolerance.

Phase 2 — weight formulas:
  Using the derived tolerance, re-run the pooled-breakout usefulness test
  with several alternative weight formulas. Compare mean KS and mean
  |Spearman rho| of the top-K expressions across formulas.

No cache rebuild. No code punts that pick thresholds by eye — thresholds
come from elbow detection on the empirical distribution.
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Reuse machinery from the usefulness test for loading + outcomes
from moc_usefulness_test import (
    CACHE_OHLCV, EXAMPLES_DB, SETUP_CLASSES, EXPR_NAMES, FORWARD_WINDOWS,
    MFE_MAE_WINDOW, ADRP_WINDOW, ATR_WINDOW, RVOL_WINDOW,
    MIN_TRADABLE_ADRP, MIN_TRADABLE_DOLLAR_VOL, MIN_CLOSE,
    NULL_BARS_PER_TICKER, NULL_N_TICKERS, RANDOM_SEED,
    compute_rvol, compute_atr14, compute_adrp20, compute_dollar_vol20,
    load_examples, resolve_signal_bar, get_dates, sample_universe_nulls,
    compute_forward_outcomes, snapshot_42,
)

import random


def collect_hl_prints(daily_cache, tickers, rvol_min=1.0):
    """Return dict[ticker] -> list of (price, atr14_at_bar)."""
    per_ticker = {}
    for ticker in tickers:
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        vol = df["volume"].values.astype(float)
        rvol = compute_rvol(vol)
        atr = compute_atr14(high, low, close)
        mask = rvol > rvol_min
        idx = np.where(mask)[0]
        if len(idx) == 0:
            continue
        prints = []
        for t in idx:
            if atr[t] and atr[t] > 0 and np.isfinite(atr[t]):
                prints.append((high[t], atr[t]))
                prints.append((low[t], atr[t]))
        per_ticker[ticker] = prints
    return per_ticker


def pairwise_neighbor_distances(per_ticker_prints):
    """For each ticker, sort prints by price; compute consecutive distances
    normalized by the LATER print's ATR14. Pool across tickers."""
    pooled = []
    for ticker, prints in per_ticker_prints.items():
        if len(prints) < 2:
            continue
        prints_sorted = sorted(prints, key=lambda p: p[0])
        for (p1, a1), (p2, a2) in zip(prints_sorted[:-1], prints_sorted[1:]):
            if a2 > 0 and np.isfinite(a2):
                dist_atr = (p2 - p1) / a2
                if np.isfinite(dist_atr) and dist_atr >= 0:
                    pooled.append(dist_atr)
    return np.array(pooled)


def find_elbow(x_sorted, y_sorted):
    """Maximum perpendicular distance from the straight line joining
    (x[0], y[0]) -> (x[-1], y[-1]). Returns the index of the elbow."""
    p1 = np.array([x_sorted[0], y_sorted[0]])
    p2 = np.array([x_sorted[-1], y_sorted[-1]])
    line = p2 - p1
    line_len = np.linalg.norm(line)
    if line_len == 0:
        return 0
    line_unit = line / line_len
    perp_distances = []
    for xi, yi in zip(x_sorted, y_sorted):
        v = np.array([xi, yi]) - p1
        proj = np.dot(v, line_unit)
        perp = v - proj * line_unit
        perp_distances.append(np.linalg.norm(perp))
    return int(np.argmax(perp_distances))


def derive_tolerance(daily_cache, tickers, out_path):
    print("Phase 1 — tolerance derivation")
    print(f"  collecting H/L prints across {len(tickers)} tickers")
    per_ticker = collect_hl_prints(daily_cache, tickers, rvol_min=1.0)
    total_prints = sum(len(v) for v in per_ticker.values())
    print(f"  total RVOL>1 H/L prints: {total_prints} across {len(per_ticker)} tickers")

    pairwise = pairwise_neighbor_distances(per_ticker)
    print(f"  pairwise-neighbor distances: {len(pairwise)}")
    if len(pairwise) == 0:
        print("  no distances — aborting")
        return None

    pairwise = np.sort(pairwise)
    quantiles = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]
    print("  distance distribution (ATR14 units):")
    for q in quantiles:
        print(f"    p{int(q*100):>2d} = {np.quantile(pairwise, q):.4f}")

    # Elbow on the cumulative distribution (log-x)
    sub = pairwise[(pairwise >= 1e-4) & (pairwise <= 1.0)]
    sub = np.sort(sub)
    cdf = np.arange(1, len(sub) + 1) / len(sub)
    x = np.log10(sub)
    elbow_idx = find_elbow(x, cdf)
    elbow_tol = sub[elbow_idx]
    elbow_cdf = cdf[elbow_idx]
    print(f"  CDF elbow (log-x): tolerance = {elbow_tol:.4f} ATR14  "
          f"(cumulative = {elbow_cdf:.3f})")
    print(f"  current punt:      tolerance = {np.sqrt(1/78):.4f} ATR14  "
          f"(sqrt(1/78))")

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Histogram on log scale
    ax = axes[0]
    bins = np.logspace(np.log10(max(1e-4, pairwise.min())),
                       np.log10(max(1.0, pairwise.max())), 80)
    ax.hist(pairwise, bins=bins, color="steelblue", edgecolor="white",
            linewidth=0.3)
    ax.set_xscale("log")
    ax.axvline(np.sqrt(1/78), color="red", linestyle="--",
               label=f"current punt ({np.sqrt(1/78):.3f})")
    ax.axvline(elbow_tol, color="limegreen", linestyle="--",
               label=f"CDF-elbow ({elbow_tol:.3f})")
    ax.set_xlabel("consecutive H/L price distance / ATR14", fontsize=10)
    ax.set_ylabel("count", fontsize=10)
    ax.set_title(f"Distribution of neighboring-print distances\n"
                 f"n={len(pairwise)} pairs, {len(per_ticker)} tickers",
                 fontsize=11)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.25)

    # CDF with elbow
    ax = axes[1]
    ax.plot(sub, cdf, color="steelblue")
    ax.axvline(elbow_tol, color="limegreen", linestyle="--",
               label=f"elbow = {elbow_tol:.3f} ATR14\n(cdf = {elbow_cdf:.3f})")
    ax.axvline(np.sqrt(1/78), color="red", linestyle="--",
               label=f"current punt = {np.sqrt(1/78):.3f}")
    ax.set_xscale("log")
    ax.set_xlabel("distance / ATR14 (log scale)", fontsize=10)
    ax.set_ylabel("cumulative fraction of pairs", fontsize=10)
    ax.set_title("CDF of pairwise distances + elbow", fontsize=11)
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, alpha=0.25)

    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved: {out_path}")
    return elbow_tol


# ---------- Phase 2: parameterized MOC builder ----------

def build_moc_parameterized(high, low, close, volume, target_bars,
                            tolerance_frac, weight_fn, rvol_birth_min):
    """Parameterized MOC builder. weight_fn(rvol) -> scalar weight."""
    n = len(close)
    target_set = set(int(b) for b in target_bars if 0 <= b < n)
    if not target_set:
        return {}

    rvol = compute_rvol(volume)
    atr = compute_atr14(high, low, close)
    tol = atr * tolerance_frac

    levels = []
    snapshots = {}

    for t in range(n):
        rv = rvol[t]
        tol_t = tol[t]
        ct = close[t]

        if rv > rvol_birth_min:
            w = weight_fn(rv)
            for price in (high[t], low[t]):
                matched_lvl = None
                for lvl in levels:
                    if abs(lvl["price"] - price) <= tol_t:
                        matched_lvl = lvl
                        break
                if matched_lvl is None:
                    levels.append({
                        "price": price, "birth_bar": t,
                        "stack_weight": w, "stack_count": 1,
                        "max_contributor_rvol": rv, "cross_count": 0,
                        "last_contribution_bar": t, "last_close_side": None,
                    })
                else:
                    matched_lvl["stack_weight"] += w
                    matched_lvl["stack_count"] += 1
                    if rv > matched_lvl["max_contributor_rvol"]:
                        matched_lvl["max_contributor_rvol"] = rv
                    matched_lvl["last_contribution_bar"] = t

        for lvl in levels:
            if t <= lvl["birth_bar"]:
                continue
            dist = ct - lvl["price"]
            if dist > tol_t:
                curr_side = 1
            elif dist < -tol_t:
                curr_side = -1
            else:
                curr_side = 0
            if lvl["last_close_side"] is not None and curr_side != 0:
                if curr_side != lvl["last_close_side"] and lvl["last_close_side"] != 0:
                    lvl["cross_count"] += 1
            if curr_side != 0:
                lvl["last_close_side"] = curr_side

        if t in target_set:
            snapshots[t] = snapshot_42(levels, ct, atr[t], t)

    return snapshots


def evaluate_formula(name, tolerance_frac, weight_fn, rvol_birth_min,
                     daily_cache, resolved, null_pairs, outcome_names):
    t0 = time.time()
    # Example features
    ex_t2b = defaultdict(list)
    for r in resolved:
        ex_t2b[r["ticker"]].append(r["signal_bar"])
    ex_features = {}
    for ticker, bars in ex_t2b.items():
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        snaps = build_moc_parameterized(
            df["high"].values.astype(float),
            df["low"].values.astype(float),
            df["close"].values.astype(float),
            df["volume"].values.astype(float),
            bars, tolerance_frac, weight_fn, rvol_birth_min,
        )
        for b, vec in snaps.items():
            ex_features[(ticker, b)] = vec

    # Null features
    null_t2b = defaultdict(list)
    for t, b in null_pairs:
        null_t2b[t].append(b)
    null_features = {}
    for ticker, bars in null_t2b.items():
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        snaps = build_moc_parameterized(
            df["high"].values.astype(float),
            df["low"].values.astype(float),
            df["close"].values.astype(float),
            df["volume"].values.astype(float),
            bars, tolerance_frac, weight_fn, rvol_birth_min,
        )
        for b, vec in snaps.items():
            null_features[(ticker, b)] = vec

    # Stack
    X_ex = np.array([ex_features[(r["ticker"], r["signal_bar"])]
                     for r in resolved
                     if (r["ticker"], r["signal_bar"]) in ex_features])
    Y_out = {}
    for k in outcome_names:
        Y_out[k] = np.array([r["outcomes"][k] for r in resolved
                             if (r["ticker"], r["signal_bar"]) in ex_features],
                            dtype=float)
    X_null = np.array(list(null_features.values()))

    ks_stats = np.full(42, np.nan)
    for j in range(42):
        ev = X_ex[:, j]
        nv = X_null[:, j]
        ev = ev[np.isfinite(ev)]
        nv = nv[np.isfinite(nv)]
        if len(ev) >= 10 and len(nv) >= 50:
            ks_stats[j] = ks_2samp(ev, nv).statistic

    rho_mat = np.full((42, len(outcome_names)), np.nan)
    for j in range(42):
        ev = X_ex[:, j]
        for ki, k in enumerate(outcome_names):
            y = Y_out[k]
            m = np.isfinite(ev) & np.isfinite(y)
            if m.sum() >= 10:
                rho, _ = spearmanr(ev[m], y[m])
                if np.isfinite(rho):
                    rho_mat[j, ki] = rho
    dt = time.time() - t0

    # Summary metrics
    top5_ks = np.sort(ks_stats[np.isfinite(ks_stats)])[-5:].mean() \
        if np.isfinite(ks_stats).any() else np.nan
    top10_abs_rho = np.sort(np.abs(rho_mat[np.isfinite(rho_mat)]))[-10:].mean() \
        if np.isfinite(rho_mat).any() else np.nan

    print(f"  [{name:<28s}] top-5 KS mean = {top5_ks:.3f}   "
          f"top-10 |rho| mean = {top10_abs_rho:.3f}   ({dt:.1f}s)")
    return {
        "name": name, "ks_stats": ks_stats, "rho_mat": rho_mat,
        "top5_ks": top5_ks, "top10_abs_rho": top10_abs_rho,
    }


def render_comparison(results, outcome_names, out_path):
    n_formulas = len(results)
    fig, axes = plt.subplots(2, n_formulas, figsize=(5 * n_formulas, 11))
    if n_formulas == 1:
        axes = axes.reshape(2, 1)

    max_ks = max((np.nanmax(r["ks_stats"]) for r in results if np.isfinite(r["ks_stats"]).any()), default=0.5)
    max_abs_rho = max((np.nanmax(np.abs(r["rho_mat"])) for r in results if np.isfinite(r["rho_mat"]).any()), default=0.5)
    vmin_rho, vmax_rho = -max_abs_rho, max_abs_rho

    for ci, res in enumerate(results):
        # KS bar (top row)
        ax = axes[0, ci]
        ks = res["ks_stats"]
        order = np.argsort(-np.nan_to_num(ks, nan=-1))
        y = np.arange(len(EXPR_NAMES))
        ax.barh(y, ks[order], color="steelblue", edgecolor="black",
                linewidth=0.3)
        ax.set_yticks(y)
        ax.set_yticklabels([EXPR_NAMES[i] for i in order], fontsize=5)
        ax.invert_yaxis()
        ax.set_xlim(0, max_ks * 1.05)
        ax.set_xlabel("KS stat", fontsize=8)
        ax.set_title(f"{res['name']}\ntop-5 KS mean = {res['top5_ks']:.3f}",
                     fontsize=9)
        ax.grid(True, axis="x", alpha=0.25)

        # Spearman heatmap (bottom row)
        ax = axes[1, ci]
        mat = res["rho_mat"][order, :]
        im = ax.imshow(mat, cmap="RdBu_r", vmin=vmin_rho, vmax=vmax_rho,
                       aspect="auto")
        ax.set_xticks(np.arange(len(outcome_names)))
        ax.set_xticklabels(outcome_names, rotation=45, ha="right",
                           fontsize=7)
        ax.set_yticks(y)
        ax.set_yticklabels([EXPR_NAMES[i] for i in order], fontsize=5)
        ax.set_title(f"top-10 |rho| mean = {res['top10_abs_rho']:.3f}",
                     fontsize=9)

    fig.suptitle("MOC weight-formula comparison  (pooled breakouts, n=111)",
                 fontsize=12, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def main():
    rng = random.Random(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    print(f"Loading {CACHE_OHLCV}")
    with open(CACHE_OHLCV, "rb") as f:
        daily_cache = pickle.load(f)
    print(f"  {len(daily_cache)} tickers in cache")

    # Examples
    all_examples = load_examples()
    breakouts = [(s, t, d) for s, t, d in all_examples if s in SETUP_CLASSES]
    breakout_tickers = sorted({t for _, t, _ in breakouts})

    # Phase 1: tolerance from breakout-example tickers
    tolerance_out = os.path.join(HERE, "moc_tolerance_derivation.png")
    derived_tol = derive_tolerance(daily_cache, breakout_tickers,
                                   tolerance_out)
    if derived_tol is None:
        print("Tolerance derivation failed; aborting.")
        return
    punt_tol = float(np.sqrt(1 / 78))

    # Phase 2 prep: resolved examples + outcomes + null set
    print("\nPhase 2 — weight-formula comparison")
    print("  resolving signal bars + forward outcomes")
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
    print(f"  resolved examples: {len(resolved)}")

    # Null sample (same date range)
    signal_dates = []
    for r in resolved:
        dates = get_dates(daily_cache[r["ticker"]])
        if dates is not None:
            signal_dates.append(dates[r["signal_bar"]])
    date_range = (min(signal_dates), max(signal_dates))
    example_tickers_set = {r["ticker"] for r in resolved}
    null_pairs = sample_universe_nulls(daily_cache, example_tickers_set,
                                       date_range, rng)
    print(f"  null bars: {len(null_pairs)}")

    outcome_names = list(FORWARD_WINDOWS) + ["mfe_10bar", "mae_10bar",
                                             "cap_eff_10bar"]

    # Formula matrix: (tolerance, weight_fn, birth_min, name)
    combos = [
        (punt_tol,    lambda r: max(0.0, r - 1.0), 1.0,
         "PUNT: tol=0.113, w=max(0,rvol-1), birth>1"),
        (derived_tol, lambda r: max(0.0, r - 1.0), 1.0,
         f"DERIVED TOL ({derived_tol:.3f}), same w, same birth"),
        (derived_tol, lambda r: 1.0,              1.0,
         f"DERIVED TOL, w=1 (count-only), birth>1"),
        (derived_tol, lambda r: r,                1.0,
         f"DERIVED TOL, w=rvol (raw), birth>1"),
        (derived_tol, lambda r: np.log(r),        1.0,
         f"DERIVED TOL, w=log(rvol), birth>1"),
        (derived_tol, lambda r: (r - 1.0) ** 2 if r > 1 else 0.0, 1.0,
         f"DERIVED TOL, w=(rvol-1)^2, birth>1"),
        (derived_tol, lambda r: r,                1.5,
         f"DERIVED TOL, w=rvol, birth>1.5"),
    ]

    results = []
    for tol, wfn, birth_min, name in combos:
        res = evaluate_formula(name, tol, wfn, birth_min, daily_cache,
                               resolved, null_pairs, outcome_names)
        results.append(res)

    # Summary ranking
    print("\nSummary:")
    print(f"  {'formula':<48s}  {'top5 KS':>9s}  {'top10 |rho|':>12s}")
    for r in sorted(results, key=lambda r: -(r["top5_ks"] + r["top10_abs_rho"])):
        print(f"  {r['name']:<48s}  {r['top5_ks']:>9.3f}  {r['top10_abs_rho']:>12.3f}")

    out_path = os.path.join(HERE, "moc_weight_formula_comparison.png")
    render_comparison(results, outcome_names, out_path)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
