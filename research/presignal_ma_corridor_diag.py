"""Diagnostic: why is the MA-corridor filter admitting so many results?

For each setup:
  1. Load the selection JSON (cell labels + corridor lo/hi).
  2. Group cells by MA type; report count per group.
  3. Rebuild the per-example feature matrix at selected cells only.
  4. SVD → effective rank at various energy thresholds (50%, 90%, 99%).
     Reveals how many independent constraints the strict-AND really applies.
  5. Corridor width stats:
     - slacked width hi-lo
     - raw ex_min-ex_max (reconstructed from examples)
     - slack fraction = (slacked - raw) / raw
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import presignal_ma_corridor as pmc
import visual_shape_compare as vsc

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
OUT_DIR = os.path.join(HERE, "presignal_ma_corridor")
MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")


def decode(label, kind):
    head, k = label.split(f"@{'E' if kind=='daily' else 'W'}-")
    return pmc.MA_TYPES.index(head), int(k), head


def effective_rank(X, energy_thresholds=(0.5, 0.9, 0.99)):
    """SVD of (n_ex, n_cells) matrix X; return number of singular values
    needed to reach each energy threshold."""
    Xc = X - np.nanmean(X, axis=0, keepdims=True)
    Xc = np.where(np.isfinite(Xc), Xc, 0.0)
    if Xc.shape[1] == 0 or Xc.shape[0] < 2:
        return {t: 0 for t in energy_thresholds}, np.array([])
    U, s, Vt = np.linalg.svd(Xc, full_matrices=False)
    s2 = s ** 2
    total = s2.sum()
    if total == 0:
        return {t: 0 for t in energy_thresholds}, s
    cum = np.cumsum(s2) / total
    out = {}
    for t in energy_thresholds:
        idx = int(np.searchsorted(cum, t) + 1)
        out[t] = min(idx, len(s))
    return out, s


def analyze_setup(setup, universe, tcache):
    print(f"\n{'='*70}\n{setup.upper()}\n{'='*70}", flush=True)
    sel = json.load(open(os.path.join(OUT_DIR, f"{setup}_selection.json")))
    W_N = int(sel["W_N"])
    n_ex_expected = sel["n_ex_kept"]
    d_lbls = sel["daily_cells"]
    w_lbls = sel["weekly_cells"]
    d_lo = np.asarray(sel["d_lo"], dtype=np.float64)
    d_hi = np.asarray(sel["d_hi"], dtype=np.float64)
    w_lo = np.asarray(sel["w_lo"], dtype=np.float64)
    w_hi = np.asarray(sel["w_hi"], dtype=np.float64)

    sel_d = [decode(l, "daily") for l in d_lbls]
    sel_w = [decode(l, "weekly") for l in w_lbls]

    print(f"n_ex_kept: {n_ex_expected}  W_N: {W_N}")
    print(f"daily cells: {len(sel_d)}  weekly cells: {len(sel_w)}")

    d_by_ma = defaultdict(list)
    for m, k, head in sel_d:
        d_by_ma[head].append(k)
    w_by_ma = defaultdict(list)
    for m, k, head in sel_w:
        w_by_ma[head].append(k)

    def span(offs):
        return f"E-{min(offs)}..E-{max(offs)}" if offs else "-"
    print("\nDAILY cells per MA type (offset range):")
    for head in sorted(d_by_ma.keys(), key=lambda h: (h[:3], int(h[3:]))):
        offs = sorted(d_by_ma[head])
        print(f"  {head:>8s}  {len(offs):3d} cells   offsets {span(offs)}")
    print("\nWEEKLY cells per MA type:")
    for head in sorted(w_by_ma.keys(), key=lambda h: (h[:3], int(h[3:]))):
        offs = sorted(w_by_ma[head])
        rng = f"W-{min(offs)}..W-{max(offs)}" if offs else "-"
        print(f"  {head:>8s}  {len(offs):3d} cells   offsets {rng}")

    # Rebuild per-example feature matrices at selected cells
    examples = sel["kept_examples"]
    ex_d = np.full((len(examples), len(sel_d)), np.nan)
    ex_w = np.full((len(examples), len(sel_w)), np.nan)
    for ei, ex in enumerate(examples):
        t = ex["ticker"]
        df = universe.get(t)
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        if t not in tcache:
            tcache[t] = pmc.build_ticker_cache(df)
        tc = tcache[t]
        dff = pmc.daily_feat_at(tc, E)
        wff = pmc.weekly_feat_at(tc, E, W_N)
        if dff is None or wff is None:
            continue
        for ci, (m, k, _) in enumerate(sel_d):
            ex_d[ei, ci] = dff[m, k]
        for ci, (m, k, _) in enumerate(sel_w):
            ex_w[ei, ci] = wff[m, k]

    # Corridor widths and slack fractions
    def width_stats(lo, hi, ex_mat, tag):
        slacked = hi - lo
        raw_lo = np.nanmin(ex_mat, axis=0)
        raw_hi = np.nanmax(ex_mat, axis=0)
        raw = raw_hi - raw_lo
        slack = slacked - raw
        with np.errstate(divide='ignore', invalid='ignore'):
            frac = np.where(raw > 0, slack / raw, np.nan)
        q = lambda a, p: np.nanpercentile(a, p) if np.any(np.isfinite(a)) else np.nan
        print(f"\n{tag} corridor widths (slacked hi-lo):")
        print(f"  n: {len(slacked)}  min: {q(slacked, 0):.4f}  p50: {q(slacked, 50):.4f}  max: {q(slacked, 100):.4f}")
        print(f"{tag} raw ex range (max-min of examples):")
        print(f"  min: {q(raw, 0):.4f}  p50: {q(raw, 50):.4f}  max: {q(raw, 100):.4f}")
        print(f"{tag} slack fraction (slack / raw):")
        print(f"  p10: {q(frac, 10):.2f}  p50: {q(frac, 50):.2f}  p90: {q(frac, 90):.2f}")

    width_stats(d_lo, d_hi, ex_d, "DAILY")
    width_stats(w_lo, w_hi, ex_w, "WEEKLY")

    # SVD effective rank
    d_ranks, d_sing = effective_rank(ex_d)
    w_ranks, w_sing = effective_rank(ex_w)
    print(f"\nEFFECTIVE RANK  (how many independent dims the cells really form)")
    print(f"  DAILY  n_cells={len(sel_d)}   rank@50%={d_ranks[0.5]}   rank@90%={d_ranks[0.9]}   rank@99%={d_ranks[0.99]}")
    print(f"  WEEKLY n_cells={len(sel_w)}   rank@50%={w_ranks[0.5]}   rank@90%={w_ranks[0.9]}   rank@99%={w_ranks[0.99]}")
    top = 8
    if len(d_sing) >= 1:
        s = d_sing / d_sing[0]
        print(f"  daily singular values (rel to s0): " + ", ".join(f"{v:.3f}" for v in s[:top]))
    if len(w_sing) >= 1:
        s = w_sing / w_sing[0]
        print(f"  weekly singular values (rel to s0): " + ", ".join(f"{v:.3f}" for v in s[:top]))


def main():
    print("Loading universe...", flush=True)
    pkl = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    with open(pkl, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe):,} tickers", flush=True)
    tcache = {}
    for s in SETUPS:
        analyze_setup(s, universe, tcache)


if __name__ == "__main__":
    main()
