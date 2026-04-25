"""Run F1 (joint consecutive-offset hulls) + Location (5-descriptor axis)
pipeline for all 5 setups: HTF, BF, BASE, DTSS, 3-4DB.

Per setup: 100% EX pass sanity on both stages, full-universe post-2020 scan
with CLASSIFIER_SPEC §5.2 cluster dedup, and F1 ∩ Location intersection.

Outputs to research/presignal_grinder_all/.
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

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_grinder_all")

SETUPS = [
    ("htf", 39),
    ("bf", 49),
    ("base", 32),
    ("dtss", 86),
    ("3-4db", 64),
]
DATE_CUTOFF = "2020-01-02"
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def process_setup(setup, N_BARS, universe, tickers_sorted):
    t_setup = time.time()
    print(f"\n{'=' * 70}", flush=True)
    print(f"=== {setup.upper()}  N={N_BARS}", flush=True)
    print(f"{'=' * 70}", flush=True)

    examples = get_examples(setup)
    print(f"DB examples: {len(examples)}", flush=True)

    # Load examples, build N-bar anchored paths
    ex_meta = []
    ex_paths = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        p = vsc.extract_example_path(df, E, N_BARS)
        if p is None:
            continue
        ex_paths.append(p)
        ex_meta.append({"ticker": ex["ticker"], "entry_date": ex["entry_date"], "E_idx": E})

    n_ex = len(ex_paths)
    if n_ex < 3:
        print(f"  SKIP: only {n_ex} valid examples", flush=True)
        return None
    ex_paths = np.array(ex_paths)
    print(f"Valid example paths: {n_ex} / {len(examples)}  (shape {ex_paths.shape})", flush=True)

    # --- F1 filter ---
    f1_filters = vsc.build_filters(ex_paths)

    # Sanity: 100% EX pass on F1
    f1_ex = vsc.check_F1_batch(ex_paths, f1_filters["hulls"])
    if not f1_ex.all():
        fails = int((~f1_ex).sum())
        print(f"  F1 SANITY FAIL: {fails} example(s) don't pass own F1 filter", flush=True)
        return None
    print(f"F1 sanity OK: {f1_ex.sum()}/{n_ex}", flush=True)

    # F1 full-universe scan
    per_ticker_f1 = {}
    total_cand = 0
    t_f1 = time.time()
    for i, ticker in enumerate(tickers_sorted):
        df = universe.get(ticker)
        if df is None:
            continue
        n_cand, f1_E, _, _ = vsc.scan_ticker(df, N_BARS, f1_filters, DATE_CUTOFF)
        total_cand += n_cand
        if len(f1_E) > 0:
            per_ticker_f1[ticker] = [int(e) for e in f1_E]
        if (i + 1) % 3000 == 0:
            print(f"  F1 scan {i + 1}/{len(tickers_sorted)}  ({time.time() - t_f1:.0f}s)",
                  flush=True)
    print(f"F1 scan complete: {time.time() - t_f1:.1f}s, {total_cand:,} candidates", flush=True)

    # Dedupe F1 §5.2
    per_ticker_ex = {}
    for m in ex_meta:
        per_ticker_ex.setdefault(m["ticker"], set()).add(int(m["E_idx"]))
    f1_clusters = []
    for ticker, E_list in per_ticker_f1.items():
        ex_set = per_ticker_ex.get(ticker, set())
        reps = vsc.dedupe_clusters(E_list, ex_set)
        for r in reps:
            f1_clusters.append((ticker, r, r in ex_set))
    f1_ex_match = sum(1 for _, _, m in f1_clusters if m)
    f1_non_ex = len(f1_clusters) - f1_ex_match
    f1_bars = sum(len(v) for v in per_ticker_f1.values())
    print(f"F1 bars: {f1_bars:,}  clusters: {len(f1_clusters):,}  "
          f"(ex: {f1_ex_match}/{n_ex}, non-ex: {f1_non_ex:,})", flush=True)

    # --- Location stage ---
    ex_rows = []
    for m in ex_meta:
        df = universe[m["ticker"]]
        close = df["close"].values.astype(np.float64)
        log_returns = np.diff(np.log(np.where(close > 0, close, np.nan)))
        ex_rows.append({
            "ticker": m["ticker"], "entry_date": m["entry_date"],
            "E_idx": m["E_idx"], "close": close, "log_returns": log_returns,
        })

    fn_d1 = lambda r, M: loc.desc_1_pos(r["close"], r["E_idx"], M)
    fn_d2 = lambda r, M: loc.desc_2_trend(r["close"], r["E_idx"], N_BARS, M)
    fn_d5 = lambda r, M: loc.desc_5_vol_ratio(r["log_returns"], r["E_idx"], M)

    M1, _, _ = loc.derive_horizon(ex_rows, fn_d1, "D1")
    M2, _, _ = loc.derive_horizon(ex_rows, fn_d2, "D2")
    M5, _, _ = loc.derive_horizon(ex_rows, fn_d5, "D5")

    D1_lo, D1_hi, _ = loc.bounds_at_M(ex_rows, fn_d1, M1)
    D2_lo, D2_hi, _ = loc.bounds_at_M(ex_rows, fn_d2, M2)
    D3_lo, D3_hi, _ = loc.bounds_no_M(ex_rows, lambda r: loc.desc_3_tsh_single(r["close"], r["E_idx"]))
    D4_vals = [loc.desc_4_ath_atl(r["close"], r["E_idx"]) for r in ex_rows]
    D4a_arr = np.array([v[0] for v in D4_vals])
    D4b_arr = np.array([v[1] for v in D4_vals])
    D4a_lo = float(np.nanmin(D4a_arr)); D4a_hi = float(np.nanmax(D4a_arr))
    D4b_lo = float(np.nanmin(D4b_arr)); D4b_hi = float(np.nanmax(D4b_arr))
    D5_lo, D5_hi, _ = loc.bounds_at_M(ex_rows, fn_d5, M5)

    bounds = {
        "D1": (D1_lo, D1_hi), "D2": (D2_lo, D2_hi), "D3": (D3_lo, D3_hi),
        "D4a": (D4a_lo, D4a_hi), "D4b": (D4b_lo, D4b_hi), "D5": (D5_lo, D5_hi),
    }
    print(f"Location horizons: M1={M1} M2={M2} M5={M5}", flush=True)

    # Sanity: 100% EX pass on location (single-eval)
    sanity_ok = 0
    for r in ex_rows:
        v1 = loc.desc_1_pos(r["close"], r["E_idx"], M1)
        v2 = loc.desc_2_trend(r["close"], r["E_idx"], N_BARS, M2)
        v3 = loc.desc_3_tsh_single(r["close"], r["E_idx"])
        v4a, v4b = loc.desc_4_ath_atl(r["close"], r["E_idx"])
        v5 = loc.desc_5_vol_ratio(r["log_returns"], r["E_idx"], M5)
        def ok(x, lo, hi):
            if x is None or not np.isfinite(x):
                return True
            return lo <= x <= hi
        if (ok(v1, *bounds["D1"]) and ok(v2, *bounds["D2"]) and ok(v3, *bounds["D3"])
            and ok(v4a, *bounds["D4a"]) and ok(v4b, *bounds["D4b"]) and ok(v5, *bounds["D5"])):
            sanity_ok += 1
    print(f"Location sanity: {sanity_ok}/{n_ex}", flush=True)

    # Location full-universe scan
    per_ticker_loc = {}
    t_loc = time.time()
    total_joint_bars = 0
    per_desc_keep = {"D1": 0, "D2": 0, "D3": 0, "D4a": 0, "D4b": 0, "D5": 0}
    for i, ticker in enumerate(tickers_sorted):
        df = universe.get(ticker)
        if df is None:
            continue
        pre = loc.precompute_ticker(df)
        if pre is None:
            continue
        ds = vsc.dates_as_str(df)
        res = loc.scan_ticker(pre, N_BARS, M1, M2, M5, bounds, DATE_CUTOFF, ds)
        if res is None:
            continue
        ap = res["all_pass"]
        total_joint_bars += int(ap.sum())
        for k in per_desc_keep:
            per_desc_keep[k] += int(res["p" + k[1:]].sum())
        if ap.any():
            per_ticker_loc[ticker] = [int(res["E"][j]) for j in np.where(ap)[0]]
        if (i + 1) % 3000 == 0:
            print(f"  Loc scan {i + 1}/{len(tickers_sorted)}  ({time.time() - t_loc:.0f}s)",
                  flush=True)
    print(f"Location scan complete: {time.time() - t_loc:.1f}s", flush=True)

    loc_clusters = []
    for ticker, E_list in per_ticker_loc.items():
        ex_set = per_ticker_ex.get(ticker, set())
        reps = vsc.dedupe_clusters(E_list, ex_set)
        for r in reps:
            loc_clusters.append((ticker, r, r in ex_set))
    loc_ex_match = sum(1 for _, _, m in loc_clusters if m)
    loc_non_ex = len(loc_clusters) - loc_ex_match
    print(f"Location clusters: {len(loc_clusters):,}  "
          f"(ex: {loc_ex_match}/{n_ex}, non-ex: {loc_non_ex:,})", flush=True)

    # Intersection
    f1_keys = set((t, e) for (t, e, _) in f1_clusters)
    loc_keys = set((t, e) for (t, e, _) in loc_clusters)
    inter = f1_keys & loc_keys
    ex_keys_all = set((m["ticker"], int(m["E_idx"])) for m in ex_meta)
    inter_ex = inter & ex_keys_all
    print(f"F1 INTERSECT Location: {len(inter):,} clusters  "
          f"(ex-matched: {len(inter_ex)}/{n_ex})", flush=True)
    print(f"Setup elapsed: {time.time() - t_setup:.1f}s", flush=True)

    # Persist per-setup outputs
    setup_summary = {
        "setup": setup,
        "N_bars": N_BARS,
        "n_ex_db": len(examples),
        "n_ex_valid": n_ex,
        "total_candidates": int(total_cand),
        "F1_bars": f1_bars,
        "F1_clusters": len(f1_clusters),
        "F1_ex_matched": f1_ex_match,
        "F1_non_example": f1_non_ex,
        "horizons": {"M1_pos": M1, "M2_trend": M2, "M5_vol_ratio": M5},
        "bounds": {k: [float(b[0]), float(b[1])] for k, b in bounds.items()},
        "Location_sanity_pass": sanity_ok,
        "Location_joint_bars": int(total_joint_bars),
        "Location_clusters": len(loc_clusters),
        "Location_ex_matched": loc_ex_match,
        "Location_non_example": loc_non_ex,
        "Location_per_desc_keep": per_desc_keep,
        "Intersection_clusters": len(inter),
        "Intersection_ex_matched": len(inter_ex),
    }
    with open(os.path.join(OUT_DIR, f"{setup}_summary.json"), 'w') as f:
        json.dump(setup_summary, f, indent=2)

    # F1 clusters CSV
    _save_clusters_csv(os.path.join(OUT_DIR, f"{setup}_F1_clusters.csv"),
                       f1_clusters, universe)
    # Intersection clusters CSV
    inter_with_meta = sorted([(t, e, (t, e) in ex_keys_all) for (t, e) in inter])
    _save_clusters_csv(os.path.join(OUT_DIR, f"{setup}_F1_Loc_clusters.csv"),
                       inter_with_meta, universe)

    return setup_summary


def _save_clusters_csv(path, clusters, universe):
    if not clusters:
        pd.DataFrame(columns=["ticker", "E_idx", "date", "example_matched"]).to_csv(path, index=False)
        return
    rows = []
    date_cache = {}
    for ticker, E, is_ex in sorted(clusters):
        df = universe.get(ticker)
        if df is None:
            continue
        ds = date_cache.get(ticker)
        if ds is None:
            ds = vsc.dates_as_str(df)
            date_cache[ticker] = ds
        rows.append({
            "ticker": ticker,
            "E_idx": E,
            "date": ds[E] if E < len(ds) else "",
            "example_matched": is_ex,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


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
    tickers_sorted = sorted(universe.keys())

    all_results = []
    for setup, N_BARS in SETUPS:
        r = process_setup(setup, N_BARS, universe, tickers_sorted)
        if r is not None:
            all_results.append(r)

    # Master summary
    with open(os.path.join(OUT_DIR, "all_setups_summary.json"), 'w') as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'=' * 90}", flush=True)
    print(f"=== MASTER SUMMARY (post-2020, 100% EX pass F1 + Location)", flush=True)
    print(f"{'=' * 90}", flush=True)
    hdr = f"{'setup':<8}{'N':<5}{'ex':<5}{'candidates':>14}{'F1_cl':>10}{'Loc_cl':>12}{'F1_Loc':>10}{'ex_match':>10}"
    print(hdr, flush=True)
    for r in all_results:
        print(f"{r['setup']:<8}{r['N_bars']:<5}{r['n_ex_valid']:<5}"
              f"{r['total_candidates']:>14,}{r['F1_clusters']:>10,}"
              f"{r['Location_clusters']:>12,}{r['Intersection_clusters']:>10,}"
              f"{r['Intersection_ex_matched']:>6}/{r['n_ex_valid']:<3}", flush=True)

    print(f"\nWrote summaries + CSVs to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
