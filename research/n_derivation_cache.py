"""Derive N per setup using full expression cache divergence.

Per setup, for each offset k in [1..K_MAX] (k=1 is E-1, k=K_MAX is E-K_MAX):
  - For every cache feature c, compute range_k_c = ex_max - ex_min across
    labeled examples (NaN-skipped). Require >= MIN_EX_MEASURED examples at
    (k, c) to record a range.
  - Relevance filter per feature: keep feature iff Spearman rank correlation
    between offset and range is strictly positive (range grows going back
    in time). Requires at least MIN_VALID_OFFSETS measurable offsets.
  - Normalize each surviving feature's range curve by its own max.
  - Aggregate = nanmean of normalized curves across surviving features.
  - Kneedle elbow on aggregate = N (in bars).

Writes per-setup envelope and summary JSON to research/n_derivation_cache/.
Nothing is written to the main repo cache.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
K_MAX = 120
MIN_EX_MEASURED = 10
MIN_VALID_OFFSETS = 10


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def dates_as_str(dates):
    if pd.api.types.is_datetime64_any_dtype(dates):
        return pd.to_datetime(dates).strftime('%Y-%m-%d').values.astype('<U10')
    return np.array([str(d)[:10] for d in dates], dtype='<U10')


def accumulate_stats(examples, ec, n_feat):
    """Stream through examples; update per-(offset, feature) min/max/count.
    Offset 0 = E-1 (closest), offset K_MAX-1 = E-K_MAX (farthest back).
    """
    ex_min = np.full((K_MAX, n_feat), np.inf, dtype=np.float32)
    ex_max = np.full((K_MAX, n_feat), -np.inf, dtype=np.float32)
    ex_count = np.zeros((K_MAX, n_feat), dtype=np.int32)
    n_valid = 0

    by_ticker = defaultdict(list)
    for ex in examples:
        by_ticker[ex["ticker"]].append(ex["entry_date"])

    n_tickers_loaded = 0
    for ticker, dates_list in by_ticker.items():
        t0 = time.time()
        dates, data = ec.get_ticker(ticker)
        if dates is None:
            continue
        ds = dates_as_str(dates)
        n_tickers_loaded += 1
        for date in dates_list:
            m = np.where(ds == date)[0]
            if len(m) == 0:
                continue
            E_idx = int(m[0])
            if E_idx < K_MAX:
                continue
            # Rows E_idx-K_MAX .. E_idx-1 (K_MAX bars preceding entry, exclusive of E)
            window = data[E_idx - K_MAX:E_idx, :].astype(np.float32, copy=False)
            # Reverse so row 0 = E-1, row K_MAX-1 = E-K_MAX
            window = window[::-1, :]
            measured = ~np.isnan(window)
            # Running min/max with NaN-safe update
            np.minimum(ex_min, np.where(measured, window, np.inf), out=ex_min)
            np.maximum(ex_max, np.where(measured, window, -np.inf), out=ex_max)
            ex_count += measured.astype(np.int32)
            n_valid += 1
        del data, dates, ds
    return ex_min, ex_max, ex_count, n_valid, n_tickers_loaded


def compute_range_curve(ex_min, ex_max, ex_count):
    """range[k, c] = ex_max - ex_min where count >= MIN_EX_MEASURED, else NaN."""
    coverage_ok = ex_count >= MIN_EX_MEASURED
    range_curve = np.where(coverage_ok, ex_max - ex_min, np.nan).astype(np.float32)
    # If a feature is fully below coverage, we get inf-(-inf) = inf where coverage
    # fails — but `np.where` already masks those to NaN. Just ensure no rogue inf:
    range_curve[~np.isfinite(range_curve)] = np.nan
    return range_curve


def relevance_filter(range_curve):
    """For each feature, compute Spearman rank corr between offset and range
    over measured offsets. Keep features with rho > 0.
    Returns (relevant_mask, spearman_rhos, n_valid_offsets_per_feature).
    """
    K, n_feat = range_curve.shape
    relevant = np.zeros(n_feat, dtype=bool)
    rhos = np.full(n_feat, np.nan, dtype=np.float32)
    n_valid = np.zeros(n_feat, dtype=np.int32)
    offsets_all = np.arange(K, dtype=np.float32)

    for c in range(n_feat):
        rc = range_curve[:, c]
        valid = ~np.isnan(rc)
        nv = int(valid.sum())
        n_valid[c] = nv
        if nv < MIN_VALID_OFFSETS:
            continue
        # If the feature is constant across measured offsets, skip.
        ys = rc[valid]
        if ys.max() == ys.min():
            continue
        xs = offsets_all[valid]
        rho, _ = spearmanr(xs, ys)
        if np.isnan(rho):
            continue
        rhos[c] = rho
        if rho > 0:
            relevant[c] = True
    return relevant, rhos, n_valid


def aggregate_curve(range_curve, relevant):
    """Normalize each relevant feature's curve by its own max (ignoring NaN),
    then nanmean across relevant features per offset.
    """
    if not relevant.any():
        return np.full(range_curve.shape[0], np.nan, dtype=np.float32)
    sub = range_curve[:, relevant]  # (K_MAX, n_rel)
    # Normalize each column by its nanmax
    col_max = np.nanmax(sub, axis=0, keepdims=True)  # (1, n_rel)
    # Avoid div by zero
    col_max = np.where(col_max > 0, col_max, np.nan)
    norm = sub / col_max  # NaN propagates where col_max is NaN or sub is NaN
    agg = np.nanmean(norm, axis=1)
    return agg.astype(np.float32)


def kneedle_idx(y):
    """Return offset index in y with maximum perpendicular distance from
    the straight line connecting (0, y[0]) and (n-1, y[n-1]).
    NaN-handled: replace NaN with linear interpolation; if endpoints are NaN,
    fall back to the first/last finite values.
    """
    n = len(y)
    if n < 3:
        return 0
    y = np.array(y, dtype=np.float64, copy=True)
    # Fill NaN by linear interpolation
    finite = np.isfinite(y)
    if not finite.any():
        return 0
    idxs = np.arange(n)
    y[~finite] = np.interp(idxs[~finite], idxs[finite], y[finite])
    x0, y0 = 0.0, float(y[0])
    x1, y1 = float(n - 1), float(y[-1])
    a = y1 - y0
    b = -(x1 - x0)
    c = x1 * y0 - x0 * y1
    denom = np.sqrt(a * a + b * b)
    if denom == 0:
        return 0
    xs = np.arange(n, dtype=np.float64)
    dist = np.abs(a * xs + b * y + c) / denom
    return int(np.argmax(dist))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    # Preflight: OHLCV (just ticker count sanity) + expr cache.
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV (for preflight count): {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"OHLCV tickers: {len(universe)}", flush=True)
    if len(universe) < 11000:
        print("FAIL: OHLCV ticker count too low")
        sys.exit(1)
    del universe  # free; we only needed the preflight

    print("Opening expression cache...", flush=True)
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expr cache invalid")
        sys.exit(1)
    expr_names = ec.expr_names
    n_feat = ec.n_expressions
    n_ec_tickers = len(ec.get_available_tickers())
    print(f"Expr cache: {n_feat} features × {n_ec_tickers} tickers", flush=True)
    if n_ec_tickers < 11000:
        print("FAIL: expr cache ticker count too low")
        sys.exit(1)

    global_summary = []

    for setup in SETUP_ORDER:
        print(f"\n=== {setup.upper()} ===", flush=True)
        t_setup = time.time()
        examples = get_examples(setup)
        print(f"  DB examples: {len(examples)}", flush=True)

        print(f"  accumulating per-(offset, feature) stats...", flush=True)
        t0 = time.time()
        ex_min, ex_max, ex_count, n_ex_valid, n_tickers_loaded = accumulate_stats(
            examples, ec, n_feat)
        print(f"  loaded {n_tickers_loaded} tickers, valid examples: {n_ex_valid} "
              f"({time.time() - t0:.1f}s)", flush=True)

        print(f"  computing range curves with coverage gate >= {MIN_EX_MEASURED}...", flush=True)
        range_curve = compute_range_curve(ex_min, ex_max, ex_count)
        total_cells = range_curve.size
        covered_cells = int(np.sum(~np.isnan(range_curve)))
        print(f"  covered cells: {covered_cells:,} / {total_cells:,} "
              f"({covered_cells / total_cells * 100:.1f}%)", flush=True)

        print(f"  relevance filter (Spearman rho > 0)...", flush=True)
        t0 = time.time()
        relevant, rhos, n_valid = relevance_filter(range_curve)
        n_rel = int(relevant.sum())
        print(f"  relevant features: {n_rel} / {n_feat} "
              f"({time.time() - t0:.1f}s)", flush=True)
        if n_rel == 0:
            print(f"  no relevant features — skipping setup", flush=True)
            continue

        agg = aggregate_curve(range_curve, relevant)
        k_idx = kneedle_idx(agg)
        N_bars = k_idx + 1
        print(f"  kneedle offset idx: {k_idx}  ->  N_bars = {N_bars}", flush=True)

        # Slice envelope to first N_bars of relevant features
        rel_indices = np.where(relevant)[0]
        env_min = ex_min[:N_bars, :][:, rel_indices].astype(np.float32)
        env_max = ex_max[:N_bars, :][:, rel_indices].astype(np.float32)
        env_count = ex_count[:N_bars, :][:, rel_indices].astype(np.int32)
        # Where count < MIN_EX_MEASURED, null out the band so scanner treats as pass-through
        under_cov = env_count < MIN_EX_MEASURED
        env_min = np.where(under_cov, np.nan, env_min).astype(np.float32)
        env_max = np.where(under_cov, np.nan, env_max).astype(np.float32)

        # Write envelope npz
        env_path = os.path.join(OUT_DIR, f"{setup}_envelope.npz")
        np.savez_compressed(
            env_path,
            feature_indices=rel_indices.astype(np.int32),
            ex_min=env_min,
            ex_max=env_max,
            ex_count=env_count,
        )

        # Write summary json
        summary = {
            "setup": setup,
            "K_MAX": K_MAX,
            "min_ex_measured": MIN_EX_MEASURED,
            "min_valid_offsets": MIN_VALID_OFFSETS,
            "n_ex_db": len(examples),
            "n_ex_valid": int(n_ex_valid),
            "n_tickers_loaded": int(n_tickers_loaded),
            "n_features_total": int(n_feat),
            "n_features_relevant": int(n_rel),
            "kneedle_offset_idx": int(k_idx),
            "N_bars": int(N_bars),
            "window_semantics": "bars at offsets 1..N_bars from entry, i.e. [E-N_bars, E-1]",
            "aggregate_curve": [float(x) for x in agg],
            "envelope_file": f"{setup}_envelope.npz",
            "relevant_features_top20_by_rho": sorted(
                [{"idx": int(i), "name": expr_names[i], "rho": float(rhos[i]),
                  "n_valid_offsets": int(n_valid[i])}
                 for i in np.where(relevant)[0]],
                key=lambda r: -r["rho"])[:20],
        }
        summary_path = os.path.join(OUT_DIR, f"{setup}_summary.json")
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2)

        global_summary.append({
            "setup": setup,
            "n_ex_valid": int(n_ex_valid),
            "n_features_relevant": int(n_rel),
            "N_bars": int(N_bars),
            "kneedle_offset_idx": int(k_idx),
            "elapsed_s": round(time.time() - t_setup, 1),
        })
        print(f"  wrote {env_path} and {summary_path}", flush=True)
        print(f"  setup elapsed: {time.time() - t_setup:.1f}s", flush=True)

    # Global rollup
    rollup_path = os.path.join(OUT_DIR, "summary.txt")
    lines = []
    lines.append("=" * 80)
    lines.append("N DERIVATION — full expression cache divergence (relevance: Spearman rho > 0)")
    lines.append("=" * 80)
    lines.append(f"K_MAX={K_MAX}  MIN_EX_MEASURED={MIN_EX_MEASURED}  "
                 f"MIN_VALID_OFFSETS={MIN_VALID_OFFSETS}")
    lines.append("")
    lines.append(f"{'setup':<10}{'n_ex_valid':<12}{'n_rel':<10}{'N_bars':<10}{'kneedle_idx':<14}{'s':<6}")
    for r in global_summary:
        lines.append(f"{r['setup']:<10}{r['n_ex_valid']:<12}{r['n_features_relevant']:<10}"
                     f"{r['N_bars']:<10}{r['kneedle_offset_idx']:<14}{r['elapsed_s']:<6}")
    lines.append("")
    lines.append("Per-setup files: {setup}_summary.json, {setup}_envelope.npz")
    with open(rollup_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote rollup to {rollup_path}", flush=True)


if __name__ == "__main__":
    main()
