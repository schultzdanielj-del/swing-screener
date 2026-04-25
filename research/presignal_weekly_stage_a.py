"""Presignal grinder — weekly pass, Stage A (example-only compute).

For each of the 5 setups, on weekly-aggregated data:
  - Derive W_N via kneedle elbow on weekly path spread vs offset.
  - Build F1 hulls from example weekly paths; sanity 100% EX pass.
  - Derive W_M1, W_M2, W_M5 via kneedle on descriptor spread vs M (weekly).
  - Build 5-descriptor Location bounds; sanity 100% EX pass.

No full-universe scan in this stage. Outputs per-setup JSON with derived
parameters (W_N, horizons, bounds, sanity counts).

Writes to research/presignal_weekly_stage_a/.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import location_axis as loc
import presignal_weekly_aggregate as wagg

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
W_K_SWEEP = 52         # offsets 1..52 used for N-derivation kneedle sweep
W_HISTORY_TARGET = 120 # target weekly history for Location descriptors (~2.3 years)
W_M_SWEEP = np.arange(4, 53)  # 4..52 weeks for D1/D2/D5 horizon sweep
TSH_CAP_W = 104        # ~2 years of weekly bars (matches daily's 504 wall-clock)
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def derive_weekly_N(example_paths_max):
    """example_paths_max: (n_ex, W_K_SWEEP) log-ratio weekly paths, possibly
    with NaN for offsets an example doesn't reach. Return W_N via kneedle
    elbow on NaN-aware spread vs offset. Offsets with <2 contributing examples
    have NaN spread (not measurable).
    W_N = kneedle_idx + 1 (so that the path window is offsets 1..W_N).
    """
    with np.errstate(invalid='ignore', all='ignore'):
        spread = np.nanmax(example_paths_max, axis=0) - np.nanmin(example_paths_max, axis=0)
    n_contrib = np.isfinite(example_paths_max).sum(axis=0)
    spread = np.where(n_contrib >= 2, spread, np.nan)
    idx = loc.kneedle_elbow_idx(spread)
    return int(idx + 1), spread, n_contrib


def process_setup(setup, universe):
    t_setup = time.time()
    print(f"\n{'=' * 70}", flush=True)
    print(f"=== {setup.upper()}", flush=True)
    print(f"{'=' * 70}", flush=True)

    examples = get_examples(setup)
    print(f"DB examples: {len(examples)}", flush=True)

    # Build weekly_back per example up to W_HISTORY_TARGET. Variable-length.
    # NO minimum-history drop — examples contribute to whatever offsets they
    # reach (NaN-lenient per §4.5). A recent-IPO example with only 4 weeks
    # of weekly history contributes to offsets 1..4 of the hull and is
    # skipped at deeper offsets.
    ex_meta = []
    ex_weekly_backs = []     # variable length, up to W_HISTORY_TARGET+1
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        wb = wagg.compute_weekly_back(df, E, W_max_target=None)
        if wb is None:
            continue  # only skip if weekly aggregation fully failed (bad anchor)
        ex_meta.append({"ticker": ex["ticker"], "entry_date": ex["entry_date"], "E_idx": E})
        ex_weekly_backs.append(wb)

    n_ex = len(ex_weekly_backs)
    if n_ex < 3:
        print(f"  SKIP: only {n_ex} valid examples after weekly aggregation", flush=True)
        return None

    # N-derivation paths: (n_ex, W_K_SWEEP) NaN-padded where examples don't reach.
    ex_paths_sweep = np.full((n_ex, W_K_SWEEP), np.nan, dtype=np.float64)
    hist_lens = np.zeros(n_ex, dtype=np.int64)
    for i, wb in enumerate(ex_weekly_backs):
        reach = min(W_K_SWEEP, len(wb) - 1)  # anchor at wb[0]; weeks back are wb[1:]
        hist_lens[i] = len(wb) - 1
        if reach > 0:
            prev = wb[1:reach + 1]
            ok = np.isfinite(prev) & (prev > 0)
            # path[k] = log(wb[k+1] / wb[0]) for k=0..reach-1
            with np.errstate(invalid='ignore', divide='ignore'):
                ex_paths_sweep[i, :reach] = np.where(ok, np.log(np.where(ok, prev, 1.0)) - np.log(wb[0]), np.nan)

    print(f"Valid weekly examples: {n_ex} / {len(examples)}  "
          f"(all kept; NaN-lenient per reach)", flush=True)
    print(f"  weekly-history depth: min={hist_lens.min()}  median={int(np.median(hist_lens))}  "
          f"max={hist_lens.max()}  (target={W_HISTORY_TARGET})", flush=True)

    # --- Weekly N derivation ---
    W_N, spread_curve, n_contrib = derive_weekly_N(ex_paths_sweep)
    print(f"Weekly N derivation: W_N={W_N} (NaN-aware kneedle on spread vs offset, "
          f"W_K_SWEEP={W_K_SWEEP})", flush=True)
    print(f"  spread curve head (offsets 1..10): {np.round(spread_curve[:10], 4)}", flush=True)
    print(f"  n_contrib head (offsets 1..10): {n_contrib[:10].tolist()}", flush=True)
    print(f"  spread curve at W_N-1 (k={W_N}): {spread_curve[W_N - 1]:.4f}  "
          f"(n_contrib={n_contrib[W_N - 1]}/{n_ex})", flush=True)

    # --- Weekly F1 hull ---
    ex_paths_N = ex_paths_sweep[:, :W_N]  # truncate to W_N
    f1_filters = vsc.build_filters(ex_paths_N)
    f1_ex = vsc.check_F1_batch(ex_paths_N, f1_filters["hulls"])
    f1_sanity = int(f1_ex.sum())
    print(f"Weekly F1 sanity: {f1_sanity}/{n_ex}", flush=True)
    if f1_sanity != n_ex:
        print(f"  F1 SANITY FAIL — aborting setup", flush=True)
        return None

    # --- Weekly Location ---
    # Build weekly ex_rows (chronological weekly close series, anchor at last index)
    ex_rows = []
    for m, wb in zip(ex_meta, ex_weekly_backs):
        ex_rows.append(wagg.weekly_ex_row(m["ticker"], m["entry_date"], wb))
    # Note: ex_rows have close of length W_K_MAX+1 (anchor at E_idx = W_K_MAX)

    # Override location_axis.M_SWEEP for weekly horizon derivation
    orig_M_SWEEP = loc.M_SWEEP
    loc.M_SWEEP = W_M_SWEEP
    try:
        fn_d1 = lambda r, M: loc.desc_1_pos(r["close"], r["E_idx"], M)
        fn_d2 = lambda r, M: loc.desc_2_trend(r["close"], r["E_idx"], W_N, M)
        fn_d5 = lambda r, M: loc.desc_5_vol_ratio(r["log_returns"], r["E_idx"], M)

        print("Deriving weekly Location horizons (W_M_SWEEP = 4..52 weeks)...", flush=True)
        W_M1, _, _ = loc.derive_horizon(ex_rows, fn_d1, "D1 pos")
        W_M2, _, _ = loc.derive_horizon(ex_rows, fn_d2, "D2 trend")
        W_M5, _, _ = loc.derive_horizon(ex_rows, fn_d5, "D5 vol_ratio")
    finally:
        loc.M_SWEEP = orig_M_SWEEP

    # Weekly bounds with TSH_CAP_W = 104
    D1_lo, D1_hi, n1 = loc.bounds_at_M(ex_rows, fn_d1, W_M1)
    D2_lo, D2_hi, n2 = loc.bounds_at_M(ex_rows, fn_d2, W_M2)
    D3_lo, D3_hi, n3 = loc.bounds_no_M(
        ex_rows, lambda r: loc.desc_3_tsh_single(r["close"], r["E_idx"], cap=TSH_CAP_W)
    )
    D4_vals = [loc.desc_4_ath_atl(r["close"], r["E_idx"]) for r in ex_rows]
    D4a_arr = np.array([v[0] for v in D4_vals])
    D4b_arr = np.array([v[1] for v in D4_vals])
    D4a_lo = float(np.nanmin(D4a_arr)); D4a_hi = float(np.nanmax(D4a_arr))
    D4b_lo = float(np.nanmin(D4b_arr)); D4b_hi = float(np.nanmax(D4b_arr))
    D5_lo, D5_hi, n5 = loc.bounds_at_M(ex_rows, fn_d5, W_M5)

    bounds = {
        "D1": (D1_lo, D1_hi), "D2": (D2_lo, D2_hi), "D3": (D3_lo, D3_hi),
        "D4a": (D4a_lo, D4a_hi), "D4b": (D4b_lo, D4b_hi), "D5": (D5_lo, D5_hi),
    }
    print(f"Weekly bounds:", flush=True)
    print(f"  D1  pos(M={W_M1}w)       [{D1_lo:.4f}, {D1_hi:.4f}]   n_ex={n1}/{n_ex}", flush=True)
    print(f"  D2  trend(M={W_M2}w)     [{D2_lo:.4f}, {D2_hi:.4f}]   n_ex={n2}/{n_ex}", flush=True)
    print(f"  D3  tsh (cap={TSH_CAP_W}w)    [{D3_lo:.0f}, {D3_hi:.0f}]       n_ex={n3}/{n_ex}", flush=True)
    print(f"  D4a log_ath              [{D4a_lo:.4f}, {D4a_hi:.4f}]", flush=True)
    print(f"  D4b log_atl              [{D4b_lo:.4f}, {D4b_hi:.4f}]", flush=True)
    print(f"  D5  vol_ratio(M={W_M5}w) [{D5_lo:.4f}, {D5_hi:.4f}]   n_ex={n5}/{n_ex}", flush=True)

    # Weekly Location 100% EX sanity
    loc_sanity = 0
    for r in ex_rows:
        v1 = loc.desc_1_pos(r["close"], r["E_idx"], W_M1)
        v2 = loc.desc_2_trend(r["close"], r["E_idx"], W_N, W_M2)
        v3 = loc.desc_3_tsh_single(r["close"], r["E_idx"], cap=TSH_CAP_W)
        v4a, v4b = loc.desc_4_ath_atl(r["close"], r["E_idx"])
        v5 = loc.desc_5_vol_ratio(r["log_returns"], r["E_idx"], W_M5)
        def ok(x, lo, hi):
            if x is None or not np.isfinite(x):
                return True
            return lo <= x <= hi
        if (ok(v1, *bounds["D1"]) and ok(v2, *bounds["D2"]) and ok(v3, *bounds["D3"])
            and ok(v4a, *bounds["D4a"]) and ok(v4b, *bounds["D4b"]) and ok(v5, *bounds["D5"])):
            loc_sanity += 1
    print(f"Weekly Location sanity: {loc_sanity}/{n_ex}", flush=True)
    if loc_sanity != n_ex:
        print(f"  LOCATION SANITY FAIL — aborting setup", flush=True)
        return None

    print(f"Setup elapsed: {time.time() - t_setup:.1f}s", flush=True)

    summary = {
        "setup": setup,
        "n_ex_db": len(examples),
        "n_ex_valid": n_ex,
        "n_dropped_short_history": 0,
        "n_contrib_at_W_N": int(n_contrib[W_N - 1]),
        "W_N": W_N,
        "W_K_SWEEP": W_K_SWEEP,
        "W_HISTORY_TARGET": W_HISTORY_TARGET,
        "history_depth_stats": {
            "min": int(hist_lens.min()),
            "median": int(np.median(hist_lens)),
            "max": int(hist_lens.max()),
        },
        "W_M_SWEEP_range": [int(W_M_SWEEP[0]), int(W_M_SWEEP[-1])],
        "TSH_CAP_W": TSH_CAP_W,
        "horizons": {
            "W_M1_pos": W_M1,
            "W_M2_trend": W_M2,
            "W_M5_vol_ratio": W_M5,
        },
        "bounds": {k: [float(v[0]), float(v[1])] for k, v in bounds.items()},
        "bounds_n_ex_finite": {
            "D1": n1, "D2": n2, "D3": n3,
            "D4a": int(np.isfinite(D4a_arr).sum()),
            "D4b": int(np.isfinite(D4b_arr).sum()),
            "D5": n5,
        },
        "F1_sanity_pass": f1_sanity,
        "Loc_sanity_pass": loc_sanity,
        "spread_curve_weekly_N": [float(x) for x in spread_curve],
    }
    with open(os.path.join(OUT_DIR, f"{setup}_stage_a.json"), 'w') as f:
        json.dump(summary, f, indent=2)
    return summary


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    t0 = time.time()
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)} tickers  ({time.time() - t0:.1f}s)", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print(f"FAIL: ticker count below {MIN_TICKER_COUNT}")
        sys.exit(1)

    results = []
    for setup in SETUPS:
        r = process_setup(setup, universe)
        if r is not None:
            results.append(r)

    with open(os.path.join(OUT_DIR, "all_setups_stage_a.json"), 'w') as f:
        json.dump(results, f, indent=2)

    # Master table
    print(f"\n{'=' * 90}", flush=True)
    print(f"=== WEEKLY STAGE A — derived params + sanity ({len(results)}/{len(SETUPS)} setups)", flush=True)
    print(f"{'=' * 90}", flush=True)
    hdr = f"{'setup':<8}{'n_ex':<6}{'W_N':<6}{'W_M1':<6}{'W_M2':<6}{'W_M5':<6}{'F1 EX':<8}{'Loc EX':<8}"
    print(hdr, flush=True)
    for r in results:
        print(f"{r['setup']:<8}{r['n_ex_valid']:<6}{r['W_N']:<6}"
              f"{r['horizons']['W_M1_pos']:<6}{r['horizons']['W_M2_trend']:<6}"
              f"{r['horizons']['W_M5_vol_ratio']:<6}"
              f"{r['F1_sanity_pass']}/{r['n_ex_valid']:<4}"
              f"{r['Loc_sanity_pass']}/{r['n_ex_valid']:<4}", flush=True)

    print(f"\nWrote Stage A summaries to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
