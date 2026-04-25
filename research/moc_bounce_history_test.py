"""Test whether adding per-level touch/bounce-history features strengthens
MOC signal.

Adds 3 new per-level features (on top of the existing 7):
  - contact_count: bars since birth whose HL range intersected the level
  - bounce_rate: contacts where close stayed on the approach-side 5 bars later
  - avg_bounce_atr: mean distance of close from level at t+5 on bounces (ATR14)

Bounce detection uses approach side defined by close[t-1] vs level,
outcome side defined by close[t+5] vs level, same-side = bounce. No forward
leak — contact statistics only include contacts with t <= snapshot_bar - 5.

10 features x 3 slots x 2 sides = 60 expressions (vs current 42).

Compares full-60 feature set against the old 42-only feature set on the
pooled breakout usefulness test, and also reports top metrics for the
NEW-18 features alone.
"""
import os
import pickle
import sys
import time
import random
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import ks_2samp, spearmanr

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from moc_usefulness_test import (
    CACHE_OHLCV, SETUP_CLASSES, FORWARD_WINDOWS, RANDOM_SEED,
    load_examples, resolve_signal_bar, get_dates, sample_universe_nulls,
    compute_forward_outcomes, compute_adrp20, compute_atr14, compute_rvol,
)

BOUNCE_FWD = 5  # bars forward to measure bounce / cross

# New feature set
EXPR_FEATURES = [
    "distance", "stack_weight", "stack_count",
    "max_contributor_rvol", "cross_count",
    "bars_since_birth", "bars_since_last_contribution",
    "contact_count", "bounce_rate", "avg_bounce_atr",
]
EXPR_SLOTS = [f"above_{i}" for i in (1, 2, 3)] + [f"below_{i}" for i in (1, 2, 3)]
EXPR_NAMES = [f"{slot}_{feat}" for slot in EXPR_SLOTS for feat in EXPR_FEATURES]
assert len(EXPR_NAMES) == 60

NEW_FEATURE_INDICES = [i for i, name in enumerate(EXPR_NAMES)
                       if any(name.endswith(s) for s in
                              ("contact_count", "bounce_rate", "avg_bounce_atr"))]
OLD_FEATURE_INDICES = [i for i in range(60) if i not in NEW_FEATURE_INDICES]
assert len(OLD_FEATURE_INDICES) == 42
assert len(NEW_FEATURE_INDICES) == 18


def build_moc_with_bounces(high, low, close, volume, target_bars,
                           tolerance_frac, weight_fn, rvol_birth_min,
                           fwd_window=BOUNCE_FWD):
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

        # Birth / stack
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
                        "contact_count": 0, "bounce_count": 0,
                        "bounce_mags_sum": 0.0,
                    })
                else:
                    matched_lvl["stack_weight"] += w
                    matched_lvl["stack_count"] += 1
                    if rv > matched_lvl["max_contributor_rvol"]:
                        matched_lvl["max_contributor_rvol"] = rv
                    matched_lvl["last_contribution_bar"] = t

        # Cross tracking (unchanged)
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

        # Bounce/contact statistics — resolved retroactively at t for contact
        # that happened at check_bar = t - fwd_window
        check_bar = t - fwd_window
        if check_bar >= 1:
            hc, lc, cc_prev = high[check_bar], low[check_bar], close[check_bar - 1]
            for lvl in levels:
                if check_bar <= lvl["birth_bar"]:
                    continue
                p = lvl["price"]
                # Contact at check_bar?
                if not (lc <= p <= hc):
                    continue
                # Approach side from close[check_bar - 1]
                prev_dist = cc_prev - p
                if abs(prev_dist) <= 1e-9:
                    continue  # ambiguous
                approach_side = 1 if prev_dist > 0 else -1
                lvl["contact_count"] += 1
                # Outcome at bar t (t = check_bar + fwd_window)
                out_dist = ct - p
                if abs(out_dist) <= 1e-9:
                    continue
                out_side = 1 if out_dist > 0 else -1
                if out_side == approach_side:
                    lvl["bounce_count"] += 1
                    if atr[t] and atr[t] > 0 and np.isfinite(atr[t]):
                        lvl["bounce_mags_sum"] += abs(out_dist) / atr[t]

        if t in target_set:
            snapshots[t] = snapshot_60(levels, ct, atr[t], t)

    return snapshots


def snapshot_60(levels, close_t, atr_t, bar_idx):
    above = [l for l in levels if l["price"] > close_t]
    below = [l for l in levels if l["price"] < close_t]
    above.sort(key=lambda l: -l["stack_weight"])
    below.sort(key=lambda l: -l["stack_weight"])
    top = (above[:3] + [None] * 3)[:3] + (below[:3] + [None] * 3)[:3]
    vec = np.full(60, np.nan, dtype=np.float64)
    for i, lvl in enumerate(top):
        if lvl is None:
            continue
        base = i * 10
        atr_safe = atr_t if atr_t and atr_t > 0 else np.nan
        vec[base + 0] = (lvl["price"] - close_t) / atr_safe
        vec[base + 1] = lvl["stack_weight"]
        vec[base + 2] = lvl["stack_count"]
        vec[base + 3] = lvl["max_contributor_rvol"]
        vec[base + 4] = lvl["cross_count"]
        vec[base + 5] = bar_idx - lvl["birth_bar"]
        vec[base + 6] = bar_idx - lvl["last_contribution_bar"]
        vec[base + 7] = lvl["contact_count"]
        if lvl["contact_count"] > 0:
            vec[base + 8] = lvl["bounce_count"] / lvl["contact_count"]
            if lvl["bounce_count"] > 0:
                vec[base + 9] = lvl["bounce_mags_sum"] / lvl["bounce_count"]
    return vec


def build_features(daily_cache, ticker_to_bars, tol, wfn, birth_min):
    out = {}
    for ticker, bars in ticker_to_bars.items():
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        snaps = build_moc_with_bounces(
            df["high"].values.astype(float),
            df["low"].values.astype(float),
            df["close"].values.astype(float),
            df["volume"].values.astype(float),
            bars, tol, wfn, birth_min,
        )
        for b, vec in snaps.items():
            out[(ticker, b)] = vec
    return out


def score_feature_set(X_ex, X_null, Y_out, indices, outcome_names):
    """Return top-5 KS mean and top-10 |rho| mean restricted to given
    feature indices."""
    ks_stats = np.full(len(indices), np.nan)
    for ii, j in enumerate(indices):
        ev = X_ex[:, j]
        nv = X_null[:, j]
        ev = ev[np.isfinite(ev)]
        nv = nv[np.isfinite(nv)]
        if len(ev) >= 10 and len(nv) >= 50:
            ks_stats[ii] = ks_2samp(ev, nv).statistic
    rho_mat = np.full((len(indices), len(outcome_names)), np.nan)
    for ii, j in enumerate(indices):
        ev = X_ex[:, j]
        for ki, k in enumerate(outcome_names):
            y = Y_out[k]
            m = np.isfinite(ev) & np.isfinite(y)
            if m.sum() >= 10:
                rho, _ = spearmanr(ev[m], y[m])
                if np.isfinite(rho):
                    rho_mat[ii, ki] = rho
    ks_sorted = np.sort(ks_stats[np.isfinite(ks_stats)])
    rho_sorted = np.sort(np.abs(rho_mat[np.isfinite(rho_mat)]))
    top5_ks = ks_sorted[-5:].mean() if len(ks_sorted) >= 5 else np.nan
    top10_rho = rho_sorted[-10:].mean() if len(rho_sorted) >= 10 else np.nan
    return ks_stats, rho_mat, top5_ks, top10_rho


def main():
    rng = random.Random(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)

    print(f"Loading {CACHE_OHLCV}")
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
    print(f"resolved examples: {len(resolved)}")

    signal_dates = []
    for r in resolved:
        dates = get_dates(daily_cache[r["ticker"]])
        if dates is not None:
            signal_dates.append(dates[r["signal_bar"]])
    date_range = (min(signal_dates), max(signal_dates))
    example_tickers = {r["ticker"] for r in resolved}
    null_pairs = sample_universe_nulls(daily_cache, example_tickers,
                                       date_range, rng)
    print(f"null bars: {len(null_pairs)}")

    outcome_names = list(FORWARD_WINDOWS) + ["mfe_10bar", "mae_10bar",
                                             "cap_eff_10bar"]

    # Single build with best weight config (raw rvol, punt tolerance).
    tol = float(np.sqrt(1 / 78))
    wfn = lambda r: r
    birth_min = 1.0
    print(f"\nBuilding 60-feature vectors (tol={tol:.4f}, w=rvol, birth>1, "
          f"bounce_fwd={BOUNCE_FWD})")

    ex_t2b = defaultdict(list)
    for r in resolved:
        ex_t2b[r["ticker"]].append(r["signal_bar"])
    null_t2b = defaultdict(list)
    for t, b in null_pairs:
        null_t2b[t].append(b)

    t0 = time.time()
    ex_features = build_features(daily_cache, ex_t2b, tol, wfn, birth_min)
    t1 = time.time()
    null_features = build_features(daily_cache, null_t2b, tol, wfn, birth_min)
    t2 = time.time()
    print(f"  examples: {t1 - t0:.1f}s,  null: {t2 - t1:.1f}s")

    X_ex = np.array([ex_features[(r["ticker"], r["signal_bar"])]
                     for r in resolved
                     if (r["ticker"], r["signal_bar"]) in ex_features])
    Y_out = {k: np.array([r["outcomes"][k] for r in resolved
                          if (r["ticker"], r["signal_bar"]) in ex_features],
                         dtype=float)
             for k in outcome_names}
    X_null = np.array(list(null_features.values()))
    print(f"X_ex: {X_ex.shape}, X_null: {X_null.shape}")

    # Score three feature slices
    slices = {
        "OLD 42 features only": OLD_FEATURE_INDICES,
        "NEW 18 features only": NEW_FEATURE_INDICES,
        "FULL 60 features": list(range(60)),
    }
    print("\nScoring feature slices:")
    print(f"  {'slice':<28s}  {'top5 KS':>9s}  {'top10 |rho|':>12s}  {'sum':>6s}")
    slice_results = {}
    for name, idx in slices.items():
        ks, rho_mat, top5_ks, top10_rho = score_feature_set(
            X_ex, X_null, Y_out, idx, outcome_names,
        )
        slice_results[name] = (ks, rho_mat, top5_ks, top10_rho, idx)
        print(f"  {name:<28s}  {top5_ks:>9.3f}  {top10_rho:>12.3f}  "
              f"{(top5_ks + top10_rho):>6.3f}")

    # Identify where the NEW features rank in the FULL-60 list
    full_ks, full_rho, _, _, _ = slice_results["FULL 60 features"]
    combined = np.nan_to_num(full_ks, nan=-1) + \
        np.nanmax(np.abs(full_rho), axis=1, initial=0)
    order = np.argsort(-combined)
    new_in_top15 = [idx for idx in order[:15] if idx in NEW_FEATURE_INDICES]
    print(f"\nNew features in top 15 (by KS + max|rho|):")
    for i, idx in enumerate(order[:15]):
        tag = "  <NEW>" if idx in NEW_FEATURE_INDICES else ""
        ks_v = full_ks[idx]
        max_rho = np.nanmax(np.abs(full_rho[idx]))
        print(f"  {i+1:>2d}. {EXPR_NAMES[idx]:<44s}  KS={ks_v:.3f}  "
              f"max|rho|={max_rho:.3f}{tag}")

    # Visualize all 60 features in a combined heatmap
    fig, axes = plt.subplots(1, 2, figsize=(17, 15),
                             gridspec_kw={"width_ratios": [1, 1.3]})
    order_plot = order
    ax = axes[0]
    y = np.arange(60)
    colors = ["crimson" if i in NEW_FEATURE_INDICES else "steelblue"
              for i in order_plot]
    ax.barh(y, full_ks[order_plot], color=colors, edgecolor="black",
            linewidth=0.3)
    ax.set_yticks(y)
    ax.set_yticklabels([EXPR_NAMES[i] for i in order_plot], fontsize=6)
    ax.invert_yaxis()
    ax.set_xlabel("KS statistic", fontsize=9)
    ax.set_title("Signal separation (KS)\nred = NEW bounce-history feature",
                 fontsize=10)
    ax.grid(True, axis="x", alpha=0.25)

    ax = axes[1]
    mat = full_rho[order_plot, :]
    vmax = max(0.45, np.nanmax(np.abs(full_rho)))
    im = ax.imshow(mat, cmap="RdBu_r", vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(np.arange(len(outcome_names)))
    ax.set_xticklabels(outcome_names, rotation=30, ha="right", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels([EXPR_NAMES[i] for i in order_plot], fontsize=6)
    ax.set_title("Forward-outcome Spearman rho\npositive (red) = higher MOC value -> higher outcome",
                 fontsize=10)
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:+.2f}", ha="center", va="center",
                        fontsize=5,
                        color="white" if abs(v) > 0.25 else "black")
    fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02, label="Spearman rho")

    old_k = slice_results["OLD 42 features only"]
    new_k = slice_results["NEW 18 features only"]
    full_k = slice_results["FULL 60 features"]
    fig.suptitle(
        f"MOC bounce-history feature test  (n_ex={X_ex.shape[0]}, "
        f"n_null={X_null.shape[0]})\n"
        f"OLD 42: top5 KS={old_k[2]:.3f}, top10 |rho|={old_k[3]:.3f}   |   "
        f"NEW 18: top5 KS={new_k[2]:.3f}, top10 |rho|={new_k[3]:.3f}   |   "
        f"FULL 60: top5 KS={full_k[2]:.3f}, top10 |rho|={full_k[3]:.3f}",
        fontsize=11, y=0.997,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    out_path = os.path.join(HERE, "moc_bounce_history_test.png")
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
