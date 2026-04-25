"""Two diagnostics on the current presignal F1+Location architecture:

1. Per-offset F1 hull contributor counts per setup on BOTH scales.
   - How many examples contribute (finite values at both offsets) to each
     adjacent-offset 2D hull?
   - How many pairs have hull=None (<3 contributors -> auto-pass for all
     candidates)?
   - Rationale: if many deep-offset pairs have no effective hull, the
     nominal N is larger than the filter's effective depth.

2. Per-descriptor keep rates on WEEKLY Location axis across the full
   universe, mirroring §15.5's daily table.
   - Per setup, per descriptor, fraction of universe bars admitted.
   - Rationale: identify whether a specific descriptor is acting as
     near-pass-through (~100%) and contributing nothing to carve.

Daily keep rates are read from existing summary JSONs.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import location_axis as loc
import presignal_weekly_aggregate as wagg

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
DAILY_DIR = os.path.join(WORKTREE, "research", "presignal_grinder_all")
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_diagnostics")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
DATE_CUTOFF = "2020-01-02"
TSH_CAP_W = 104


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def daily_hulls_for(setup, N, universe):
    examples = get_examples(setup)
    ex_paths = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        p = vsc.extract_example_path(df, E, N)
        if p is None:
            continue
        ex_paths.append(p)
    if not ex_paths:
        return None, 0
    return vsc.build_filters(np.array(ex_paths)), len(ex_paths)


def weekly_hulls_for(setup, W_N, universe):
    examples = get_examples(setup)
    ex_paths = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        wb = wagg.compute_weekly_back(df, E, W_max_target=None)
        if wb is None:
            continue
        path = np.full(W_N, np.nan, dtype=np.float64)
        reach = min(W_N, len(wb) - 1)
        if reach > 0:
            prev = wb[1:reach + 1]
            ok = np.isfinite(prev) & (prev > 0)
            with np.errstate(invalid='ignore', divide='ignore'):
                path[:reach] = np.where(ok, np.log(np.where(ok, prev, 1.0)) - np.log(wb[0]), np.nan)
        ex_paths.append(path)
    if not ex_paths:
        return None, 0
    return vsc.build_filters(np.array(ex_paths)), len(ex_paths)


def part1_hull_contribs(universe):
    """Rebuild hulls per setup per scale, report contributor counts per
    offset pair and fraction of pairs with hull=None."""
    print("\n" + "=" * 90, flush=True)
    print("=== PART 1 — F1 hull contributor counts per offset pair", flush=True)
    print("=" * 90, flush=True)

    # Daily N from §15.4
    daily_N = {"htf": 39, "bf": 49, "base": 32, "dtss": 86, "3-4db": 64}

    results = {}
    for setup in SETUPS:
        # Daily
        dN = daily_N[setup]
        d_filters, d_nex = daily_hulls_for(setup, dN, universe)
        d_contrib = d_filters["hull_n_contrib"] if d_filters else np.array([])
        d_none = int((d_contrib < 3).sum())
        # Weekly
        stage_a = json.load(open(os.path.join(STAGE_A_DIR, f"{setup}_stage_a.json")))
        wN = stage_a["W_N"]
        w_filters, w_nex = weekly_hulls_for(setup, wN, universe)
        w_contrib = w_filters["hull_n_contrib"] if w_filters else np.array([])
        w_none = int((w_contrib < 3).sum())

        results[setup] = {
            "daily_N": dN, "daily_n_ex": d_nex, "daily_pairs": len(d_contrib),
            "daily_hull_none": d_none, "daily_hull_none_frac": d_none / max(len(d_contrib), 1),
            "daily_contrib_min": int(d_contrib.min()) if len(d_contrib) else 0,
            "daily_contrib_max": int(d_contrib.max()) if len(d_contrib) else 0,
            "daily_contrib_median": int(np.median(d_contrib)) if len(d_contrib) else 0,
            "daily_contrib_per_pair": [int(x) for x in d_contrib],
            "weekly_N": wN, "weekly_n_ex": w_nex, "weekly_pairs": len(w_contrib),
            "weekly_hull_none": w_none, "weekly_hull_none_frac": w_none / max(len(w_contrib), 1),
            "weekly_contrib_min": int(w_contrib.min()) if len(w_contrib) else 0,
            "weekly_contrib_max": int(w_contrib.max()) if len(w_contrib) else 0,
            "weekly_contrib_median": int(np.median(w_contrib)) if len(w_contrib) else 0,
            "weekly_contrib_per_pair": [int(x) for x in w_contrib],
        }

        print(f"\n{setup.upper()}", flush=True)
        print(f"  DAILY  N={dN}  n_ex={d_nex}  pairs={len(d_contrib)}  "
              f"hull=None: {d_none} ({results[setup]['daily_hull_none_frac']*100:.1f}%)  "
              f"contrib range [{results[setup]['daily_contrib_min']}, {results[setup]['daily_contrib_max']}], "
              f"median {results[setup]['daily_contrib_median']}", flush=True)
        print(f"  WEEKLY W_N={wN}  n_ex={w_nex}  pairs={len(w_contrib)}  "
              f"hull=None: {w_none} ({results[setup]['weekly_hull_none_frac']*100:.1f}%)  "
              f"contrib range [{results[setup]['weekly_contrib_min']}, {results[setup]['weekly_contrib_max']}], "
              f"median {results[setup]['weekly_contrib_median']}", flush=True)

    with open(os.path.join(OUT_DIR, "hull_contribs.json"), "w") as f:
        json.dump(results, f, indent=2)

    return results


def part2_weekly_keep_rates(universe, tickers_sorted):
    """Per-setup full-universe scan of weekly Location only — report per-
    descriptor keep rates."""
    print("\n" + "=" * 90, flush=True)
    print("=== PART 2 — per-descriptor keep rates on WEEKLY Location axis", flush=True)
    print("=" * 90, flush=True)

    results = {}
    for setup in SETUPS:
        stage_a = json.load(open(os.path.join(STAGE_A_DIR, f"{setup}_stage_a.json")))
        W_N = stage_a["W_N"]
        W_M1 = stage_a["horizons"]["W_M1_pos"]
        W_M2 = stage_a["horizons"]["W_M2_trend"]
        W_M5 = stage_a["horizons"]["W_M5_vol_ratio"]
        bounds = {k: tuple(v) for k, v in stage_a["bounds"].items()}

        print(f"\n{setup.upper()}  W_N={W_N}  W_M1={W_M1}  W_M2={W_M2}  W_M5={W_M5}", flush=True)
        print(f"  bounds: {bounds}", flush=True)

        # We need F1 hulls too so scan_ticker_weekly can run; build them.
        f1_filters, _ = weekly_hulls_for(setup, W_N, universe)
        f1_hulls = f1_filters["hulls"] if f1_filters else []

        total = 0
        k1 = k2 = k3 = k4a = k4b = k5 = 0
        t0 = time.time()
        for i, ticker in enumerate(tickers_sorted):
            df = universe.get(ticker)
            if df is None:
                continue
            try:
                idx = wagg.build_ticker_weekly_indexing(df)
            except Exception:
                continue
            if idx["L"] < 2:
                continue
            ds = vsc.dates_as_str(df)
            res = wagg.scan_ticker_weekly(
                idx, W_N, W_M1, W_M2, W_M5, TSH_CAP_W,
                f1_hulls, bounds, DATE_CUTOFF, ds,
            )
            if res is None:
                continue
            total += len(res["E"])
            k1 += int(res["p1"].sum())
            k2 += int(res["p2"].sum())
            k3 += int(res["p3"].sum())
            k4a += int(res["p4a"].sum())
            k4b += int(res["p4b"].sum())
            k5 += int(res["p5"].sum())
            if (i + 1) % 2000 == 0:
                print(f"  scan {i+1}/{len(tickers_sorted)} ({time.time()-t0:.0f}s)", flush=True)

        def pct(n):
            return (n / total * 100.0) if total > 0 else 0.0

        results[setup] = {
            "W_N": W_N, "W_M1": W_M1, "W_M2": W_M2, "W_M5": W_M5,
            "total_candidates": int(total),
            "keep": {"D1": int(k1), "D2": int(k2), "D3": int(k3),
                     "D4a": int(k4a), "D4b": int(k4b), "D5": int(k5)},
            "keep_rate": {
                "D1": pct(k1), "D2": pct(k2), "D3": pct(k3),
                "D4a": pct(k4a), "D4b": pct(k4b), "D5": pct(k5),
            },
        }
        print(f"  total candidates: {total:,}", flush=True)
        print(f"  D1  pos       {pct(k1):6.2f}%   D2  trend   {pct(k2):6.2f}%   D3  tsh   {pct(k3):6.2f}%", flush=True)
        print(f"  D4a log_ath   {pct(k4a):6.2f}%   D4b log_atl {pct(k4b):6.2f}%   D5  vol   {pct(k5):6.2f}%", flush=True)

    with open(os.path.join(OUT_DIR, "weekly_keep_rates.json"), "w") as f:
        json.dump(results, f, indent=2)

    return results


def part3_daily_keep_from_summary():
    """Read §15.5 daily keep rates from existing summary JSONs."""
    print("\n" + "=" * 90, flush=True)
    print("=== PART 3 — per-descriptor keep rates on DAILY (from existing summaries)", flush=True)
    print("=" * 90, flush=True)
    results = {}
    for setup in SETUPS:
        path = os.path.join(DAILY_DIR, f"{setup}_summary.json")
        if not os.path.exists(path):
            continue
        s = json.load(open(path))
        keep = s.get("Location_per_desc_keep", {})
        total = s.get("total_candidates") or s.get("Location_joint_bars")
        # Location_per_desc_keep is BAR counts, not bar vs total. Compute rates.
        # Actually we need the total location-side candidate count for denominator.
        # Use total_candidates from the setup summary.
        denom = s.get("total_candidates", 0)
        rates = {k: (v / denom * 100.0) if denom else 0.0 for k, v in keep.items()}
        results[setup] = {"total": denom, "keep": keep, "keep_rate": rates}
        print(f"\n{setup.upper()}  total={denom:,}", flush=True)
        print(f"  D1  pos       {rates.get('D1', 0):6.2f}%   D2  trend   {rates.get('D2', 0):6.2f}%   D3  tsh   {rates.get('D3', 0):6.2f}%", flush=True)
        print(f"  D4a log_ath   {rates.get('D4a', 0):6.2f}%   D4b log_atl {rates.get('D4b', 0):6.2f}%   D5  vol   {rates.get('D5', 0):6.2f}%", flush=True)

    with open(os.path.join(OUT_DIR, "daily_keep_rates.json"), "w") as f:
        json.dump(results, f, indent=2)
    return results


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    print(f"Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    tickers_sorted = sorted(universe.keys())

    hull_results = part1_hull_contribs(universe)
    daily_results = part3_daily_keep_from_summary()
    weekly_results = part2_weekly_keep_rates(universe, tickers_sorted)

    print(f"\nWrote outputs to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
