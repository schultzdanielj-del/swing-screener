"""Run all three MOC feature-expansion tests back to back.

Test 1 — BOUNCE history (per-level): contact_count, bounce_rate, avg_bounce_atr.
Test 2 — BEYOND (per-level):  max_abs_beyond_atr = max over all bars since
         birth of abs(close[t] - level_price) / atr14[t]. "How decisively
         has close moved past this level ever."
Test 3 — COMPOSITE (per-bar): 7 zone-level composites computed from the
         full levels list: total stack_weight above/below, level counts,
         density near close, top-1 zone width, weight asymmetry.

One unified builder emits 73 features per bar:
  66 per-level: 11 features x 6 slots (top 3 above/below by stack_weight)
  7 per-bar composites.

Scored against the baseline OLD-42 (the shippable feature set the usefulness
test was run on). Slices compared:
  OLD 42
  +BEYOND only (48)
  +BOUNCE only (60)
  +COMPOSITE only (49)
  ALL 73

All uses linear w=rvol, birth>1, tol=sqrt(1/78). Bounce forward-window = 5.
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

BOUNCE_FWD = 5
TOP_N_PER_SIDE = 3
FEATS_PER_LEVEL = 11  # 7 old + 1 beyond + 3 bounce
N_COMPOSITE = 7
N_SLOTS = 2 * TOP_N_PER_SIDE  # 6
N_TOTAL = N_SLOTS * FEATS_PER_LEVEL + N_COMPOSITE  # 66 + 7 = 73

PER_LEVEL_NAMES = [
    "distance", "stack_weight", "stack_count",
    "max_contributor_rvol", "cross_count",
    "bars_since_birth", "bars_since_last_contribution",
    "max_abs_beyond_atr",
    "contact_count", "bounce_rate", "avg_bounce_atr",
]
SLOT_NAMES = [f"above_{i}" for i in (1, 2, 3)] + [f"below_{i}" for i in (1, 2, 3)]
PER_LEVEL_EXPR_NAMES = [f"{s}_{f}" for s in SLOT_NAMES for f in PER_LEVEL_NAMES]

COMPOSITE_NAMES = [
    "total_weight_above", "total_weight_below",
    "n_levels_above", "n_levels_below",
    "n_levels_within_2atr", "top1_spread_atr", "weight_asymmetry",
]
EXPR_NAMES = PER_LEVEL_EXPR_NAMES + [f"composite_{c}" for c in COMPOSITE_NAMES]
assert len(EXPR_NAMES) == N_TOTAL

# Index sets for slicing
OLD_FEAT_IN_LEVEL = list(range(7))       # feats 0..6 are the original 7
BEYOND_FEAT_IN_LEVEL = [7]
BOUNCE_FEATS_IN_LEVEL = [8, 9, 10]


def slot_indices(feats_in_level):
    idx = []
    for s in range(N_SLOTS):
        base = s * FEATS_PER_LEVEL
        for f in feats_in_level:
            idx.append(base + f)
    return idx


OLD_IDX = slot_indices(OLD_FEAT_IN_LEVEL)               # 42
BEYOND_IDX = slot_indices(BEYOND_FEAT_IN_LEVEL)         # 6
BOUNCE_IDX = slot_indices(BOUNCE_FEATS_IN_LEVEL)        # 18
COMPOSITE_IDX = list(range(N_SLOTS * FEATS_PER_LEVEL, N_TOTAL))  # 7
assert len(OLD_IDX) == 42
assert len(BEYOND_IDX) == 6
assert len(BOUNCE_IDX) == 18
assert len(COMPOSITE_IDX) == 7


def build_moc_expanded(high, low, close, volume, target_bars,
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
        atr_t = atr[t]
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
                        "max_abs_beyond_atr": 0.0,
                        "contact_count": 0, "bounce_count": 0,
                        "bounce_mags_sum": 0.0,
                    })
                else:
                    matched_lvl["stack_weight"] += w
                    matched_lvl["stack_count"] += 1
                    if rv > matched_lvl["max_contributor_rvol"]:
                        matched_lvl["max_contributor_rvol"] = rv
                    matched_lvl["last_contribution_bar"] = t

        # Cross tracking + max-beyond update
        for lvl in levels:
            if t <= lvl["birth_bar"]:
                continue
            dist = ct - lvl["price"]
            # Max absolute beyond-level distance in ATR units
            if atr_t and atr_t > 0 and np.isfinite(atr_t):
                beyond_atr = abs(dist) / atr_t
                if beyond_atr > lvl["max_abs_beyond_atr"]:
                    lvl["max_abs_beyond_atr"] = beyond_atr
            # Cross-count (same as before)
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

        # Bounce / contact statistics (resolved at t for contact at t-fwd_window)
        check_bar = t - fwd_window
        if check_bar >= 1:
            hc, lc, cc_prev = high[check_bar], low[check_bar], close[check_bar - 1]
            for lvl in levels:
                if check_bar <= lvl["birth_bar"]:
                    continue
                p = lvl["price"]
                if not (lc <= p <= hc):
                    continue
                prev_dist = cc_prev - p
                if abs(prev_dist) <= 1e-9:
                    continue
                approach_side = 1 if prev_dist > 0 else -1
                lvl["contact_count"] += 1
                out_dist = ct - p
                if abs(out_dist) <= 1e-9:
                    continue
                out_side = 1 if out_dist > 0 else -1
                if out_side == approach_side:
                    lvl["bounce_count"] += 1
                    if atr_t and atr_t > 0 and np.isfinite(atr_t):
                        lvl["bounce_mags_sum"] += abs(out_dist) / atr_t

        if t in target_set:
            snapshots[t] = snapshot_vec(levels, ct, atr_t, t)

    return snapshots


def snapshot_vec(levels, close_t, atr_t, bar_idx):
    vec = np.full(N_TOTAL, np.nan, dtype=np.float64)
    atr_safe = atr_t if atr_t and atr_t > 0 else np.nan

    above = [l for l in levels if l["price"] > close_t]
    below = [l for l in levels if l["price"] < close_t]
    above.sort(key=lambda l: -l["stack_weight"])
    below.sort(key=lambda l: -l["stack_weight"])
    top = (above[:TOP_N_PER_SIDE] + [None] * TOP_N_PER_SIDE)[:TOP_N_PER_SIDE] \
          + (below[:TOP_N_PER_SIDE] + [None] * TOP_N_PER_SIDE)[:TOP_N_PER_SIDE]

    for i, lvl in enumerate(top):
        if lvl is None:
            continue
        base = i * FEATS_PER_LEVEL
        vec[base + 0] = (lvl["price"] - close_t) / atr_safe
        vec[base + 1] = lvl["stack_weight"]
        vec[base + 2] = lvl["stack_count"]
        vec[base + 3] = lvl["max_contributor_rvol"]
        vec[base + 4] = lvl["cross_count"]
        vec[base + 5] = bar_idx - lvl["birth_bar"]
        vec[base + 6] = bar_idx - lvl["last_contribution_bar"]
        vec[base + 7] = lvl["max_abs_beyond_atr"]
        vec[base + 8] = lvl["contact_count"]
        if lvl["contact_count"] > 0:
            vec[base + 9] = lvl["bounce_count"] / lvl["contact_count"]
            if lvl["bounce_count"] > 0:
                vec[base + 10] = lvl["bounce_mags_sum"] / lvl["bounce_count"]

    # Composites — compute from the FULL levels list, not just top-6
    composite_base = N_SLOTS * FEATS_PER_LEVEL
    active_above = [l for l in levels if l["price"] > close_t]
    active_below = [l for l in levels if l["price"] < close_t]
    total_w_above = sum(l["stack_weight"] for l in active_above)
    total_w_below = sum(l["stack_weight"] for l in active_below)
    n_above = len(active_above)
    n_below = len(active_below)
    if np.isfinite(atr_safe):
        thresh = 2.0 * atr_safe
        n_within_2atr = sum(1 for l in levels if abs(l["price"] - close_t) < thresh)
    else:
        n_within_2atr = np.nan
    top_above_price = active_above[0]["price"] if (active_above and
        active_above == sorted(active_above, key=lambda l: -l["stack_weight"])
        [:len(active_above)]) else (active_above[0]["price"] if active_above else np.nan)
    # Already sorted by stack_weight via earlier sort — but levels list itself isn't
    # sorted. Sort quickly here.
    if active_above:
        top_above_price = max(active_above, key=lambda l: l["stack_weight"])["price"]
    else:
        top_above_price = np.nan
    if active_below:
        top_below_price = max(active_below, key=lambda l: l["stack_weight"])["price"]
    else:
        top_below_price = np.nan
    if np.isfinite(top_above_price) and np.isfinite(top_below_price) and np.isfinite(atr_safe):
        top1_spread = (top_above_price - top_below_price) / atr_safe
    else:
        top1_spread = np.nan
    total_weight = total_w_above + total_w_below
    weight_asym = ((total_w_above - total_w_below) / total_weight) if total_weight > 0 else np.nan

    vec[composite_base + 0] = total_w_above
    vec[composite_base + 1] = total_w_below
    vec[composite_base + 2] = n_above
    vec[composite_base + 3] = n_below
    vec[composite_base + 4] = n_within_2atr
    vec[composite_base + 5] = top1_spread
    vec[composite_base + 6] = weight_asym

    return vec


def build_features(daily_cache, ticker_to_bars, tol, wfn, birth_min):
    out = {}
    for ticker, bars in ticker_to_bars.items():
        if ticker not in daily_cache:
            continue
        df = daily_cache[ticker]
        snaps = build_moc_expanded(
            df["high"].values.astype(float),
            df["low"].values.astype(float),
            df["close"].values.astype(float),
            df["volume"].values.astype(float),
            bars, tol, wfn, birth_min,
        )
        for b, vec in snaps.items():
            out[(ticker, b)] = vec
    return out


def score_feature_slice(X_ex, X_null, Y_out, indices, outcome_names,
                        top_ks_n=5, top_rho_n=10):
    idx_arr = np.array(indices, dtype=int)
    ks_stats = np.full(len(idx_arr), np.nan)
    for ii, j in enumerate(idx_arr):
        ev = X_ex[:, j]
        nv = X_null[:, j]
        ev = ev[np.isfinite(ev)]
        nv = nv[np.isfinite(nv)]
        if len(ev) >= 10 and len(nv) >= 50:
            ks_stats[ii] = ks_2samp(ev, nv).statistic
    rho_mat = np.full((len(idx_arr), len(outcome_names)), np.nan)
    for ii, j in enumerate(idx_arr):
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
    top_k = ks_sorted[-top_ks_n:].mean() if len(ks_sorted) >= top_ks_n else np.nan
    top_r = rho_sorted[-top_rho_n:].mean() if len(rho_sorted) >= top_rho_n else np.nan
    return ks_stats, rho_mat, top_k, top_r


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
    tol = float(np.sqrt(1 / 78))
    wfn = lambda r: r
    birth_min = 1.0

    ex_t2b = defaultdict(list)
    for r in resolved:
        ex_t2b[r["ticker"]].append(r["signal_bar"])
    null_t2b = defaultdict(list)
    for t, b in null_pairs:
        null_t2b[t].append(b)

    print(f"\nBuilding 73-feature vectors (tol={tol:.4f}, w=rvol, birth>1)")
    t0 = time.time()
    ex_features = build_features(daily_cache, ex_t2b, tol, wfn, birth_min)
    t1 = time.time()
    null_features = build_features(daily_cache, null_t2b, tol, wfn, birth_min)
    t2 = time.time()
    print(f"  examples: {t1-t0:.1f}s, null: {t2-t1:.1f}s")

    X_ex = np.array([ex_features[(r["ticker"], r["signal_bar"])]
                     for r in resolved
                     if (r["ticker"], r["signal_bar"]) in ex_features])
    Y_out = {k: np.array([r["outcomes"][k] for r in resolved
                          if (r["ticker"], r["signal_bar"]) in ex_features],
                         dtype=float)
             for k in outcome_names}
    X_null = np.array(list(null_features.values()))
    print(f"X_ex: {X_ex.shape}, X_null: {X_null.shape}")

    # Scoring slices
    slices = {
        "OLD 42 (baseline)":                      OLD_IDX,
        "+BOUNCE only (42+18=60)":                OLD_IDX + BOUNCE_IDX,
        "+BEYOND only (42+6=48)":                 OLD_IDX + BEYOND_IDX,
        "+COMPOSITE only (42+7=49)":              OLD_IDX + COMPOSITE_IDX,
        "BOUNCE-only features (18)":              BOUNCE_IDX,
        "BEYOND-only features (6)":               BEYOND_IDX,
        "COMPOSITE-only features (7)":            COMPOSITE_IDX,
        "ALL 73":                                 list(range(N_TOTAL)),
    }

    print("\nScoring slices:")
    print(f"  {'slice':<36s}  {'top5 KS':>9s}  {'top10 |rho|':>12s}  {'sum':>6s}")
    results = {}
    for name, idx in slices.items():
        ks, rho_mat, top_k, top_r = score_feature_slice(
            X_ex, X_null, Y_out, idx, outcome_names,
        )
        results[name] = (ks, rho_mat, top_k, top_r, idx)
        print(f"  {name:<36s}  {top_k:>9.3f}  {top_r:>12.3f}  "
              f"{(top_k+top_r):>6.3f}")

    # Rank all 73 features by combined (max KS + max |rho|)
    all_ks, all_rho, _, _, _ = results["ALL 73"]
    combined = np.nan_to_num(all_ks, nan=-1) + \
        np.nanmax(np.abs(all_rho), axis=1, initial=0)
    order = np.argsort(-combined)

    print("\nTop 20 features overall (among all 73):")
    for i, local_idx in enumerate(order[:20]):
        global_idx = slices["ALL 73"][local_idx]
        name = EXPR_NAMES[global_idx]
        cat = ("BOUNCE" if global_idx in BOUNCE_IDX else
               "BEYOND" if global_idx in BEYOND_IDX else
               "COMPOSITE" if global_idx in COMPOSITE_IDX else "OLD")
        k = all_ks[local_idx]
        mr = np.nanmax(np.abs(all_rho[local_idx]))
        print(f"  {i+1:>2d}. [{cat:<9s}] {name:<48s}  "
              f"KS={k:.3f}  max|rho|={mr:.3f}")

    # Summary plot — ranked 73 features, colored by category
    fig, axes = plt.subplots(1, 2, figsize=(18, 16),
                             gridspec_kw={"width_ratios": [1, 1.3]})
    ax = axes[0]
    y = np.arange(N_TOTAL)
    category_colors = []
    for local_idx in order:
        global_idx = slices["ALL 73"][local_idx]
        if global_idx in BOUNCE_IDX:
            category_colors.append("crimson")
        elif global_idx in BEYOND_IDX:
            category_colors.append("darkorange")
        elif global_idx in COMPOSITE_IDX:
            category_colors.append("purple")
        else:
            category_colors.append("steelblue")
    ax.barh(y, all_ks[order], color=category_colors, edgecolor="black",
            linewidth=0.25)
    ax.set_yticks(y)
    labels = [EXPR_NAMES[slices["ALL 73"][o]] for o in order]
    ax.set_yticklabels(labels, fontsize=5.5)
    ax.invert_yaxis()
    ax.set_xlabel("KS statistic", fontsize=9)
    ax.set_title(f"All 73 features ranked\nOLD=blue, BOUNCE=red, BEYOND=orange, COMPOSITE=purple",
                 fontsize=10)
    ax.grid(True, axis="x", alpha=0.2)

    ax = axes[1]
    mat = all_rho[order, :]
    vmax = max(0.45, np.nanmax(np.abs(all_rho)))
    im = ax.imshow(mat, cmap="RdBu_r", vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(np.arange(len(outcome_names)))
    ax.set_xticklabels(outcome_names, rotation=30, ha="right", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=5.5)
    ax.set_title("Forward-outcome Spearman rho", fontsize=10)
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:+.2f}", ha="center", va="center",
                        fontsize=4.5,
                        color="white" if abs(v) > 0.25 else "black")
    fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02, label="Spearman rho")

    old_r = results["OLD 42 (baseline)"]
    bounce_r = results["+BOUNCE only (42+18=60)"]
    beyond_r = results["+BEYOND only (42+6=48)"]
    comp_r = results["+COMPOSITE only (42+7=49)"]
    all_r = results["ALL 73"]
    fig.suptitle(
        "MOC feature-expansion: bounce-history + beyond-ATR + composite\n"
        f"OLD 42:   KS={old_r[2]:.3f}, |rho|={old_r[3]:.3f}   "
        f"| +BOUNCE:  {bounce_r[2]:.3f}, {bounce_r[3]:.3f}   "
        f"| +BEYOND:  {beyond_r[2]:.3f}, {beyond_r[3]:.3f}   "
        f"| +COMP:    {comp_r[2]:.3f}, {comp_r[3]:.3f}   "
        f"| ALL:  {all_r[2]:.3f}, {all_r[3]:.3f}",
        fontsize=10, y=0.997,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    out_path = os.path.join(HERE, "moc_feature_expansion_test.png")
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
