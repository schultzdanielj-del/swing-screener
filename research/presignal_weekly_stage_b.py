"""Presignal grinder - weekly Stage B: full-universe weekly scan per setup,
cluster dedup, and daily-and-weekly intersection.

Consumes Stage A outputs (W_N, W_M1, W_M2, W_M5, weekly Location bounds).
Reads daily F1_and_Loc cluster CSVs from research/presignal_grinder_all/ and
intersects at (ticker, E_idx) - both scales share the daily bar index
domain, so intersection is a set-intersect on keys.

Outputs per setup to research/presignal_weekly_stage_b/:
  - {setup}_W_F1_clusters.csv           - weekly F1 survivors
  - {setup}_W_Loc_clusters.csv          - weekly Location survivors
  - {setup}_W_F1_Loc_clusters.csv       - weekly F1_and_Loc survivors
  - {setup}_DW_F1_Loc_clusters.csv      - dailyANDweekly final
  - {setup}_stage_b.json                - cluster counts, EX-pass sanity
And all_setups_stage_b.json master summary.

100% EX end-to-end sanity: every example's (ticker, E_idx) must appear in
daily F1_and_Loc, weekly F1_and_Loc, AND dailyANDweekly. Any missing example is a
semantic failure (not a volume issue) and is reported.
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
import presignal_weekly_aggregate as wagg

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
DAILY_DIR = os.path.join(WORKTREE, "research", "presignal_grinder_all")
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_b")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
W_HISTORY_TARGET = 120
TSH_CAP_W = 104
DATE_CUTOFF = "2020-01-02"
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def load_stage_a(setup):
    path = os.path.join(STAGE_A_DIR, f"{setup}_stage_a.json")
    with open(path) as f:
        return json.load(f)


def load_daily_clusters(setup):
    """Return set of (ticker, E_idx) for daily F1_and_Loc clusters."""
    path = os.path.join(DAILY_DIR, f"{setup}_F1_Loc_clusters.csv")
    if not os.path.exists(path):
        return None, None
    df = pd.read_csv(path)
    keys = set(zip(df["ticker"].astype(str), df["E_idx"].astype(int)))
    ex_matched = df[df["example_matched"]].copy() if "example_matched" in df.columns else None
    return keys, ex_matched


def rebuild_weekly_F1_hulls_and_bounds(setup, universe, stage_a):
    """Rebuild weekly F1 hulls and load weekly Location bounds for a setup,
    using the same NaN-lenient logic as Stage A.
    """
    examples = get_examples(setup)
    W_N = stage_a["W_N"]

    ex_meta = []
    ex_paths_N = []  # (n_ex, W_N), NaN-padded
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
        ex_meta.append({"ticker": ex["ticker"], "entry_date": ex["entry_date"], "E_idx": E})
        ex_paths_N.append(path)

    ex_paths_N = np.array(ex_paths_N)
    f1_filters = vsc.build_filters(ex_paths_N)

    # Sanity: every example passes its own filter under NaN-lenient check.
    f1_ex = vsc.check_F1_batch(ex_paths_N, f1_filters["hulls"])
    if not f1_ex.all():
        raise RuntimeError(f"{setup}: F1 sanity fail on rebuild ({int((~f1_ex).sum())} of {len(ex_paths_N)})")

    # Bounds, horizons from JSON
    bounds = {k: tuple(v) for k, v in stage_a["bounds"].items()}
    horizons = stage_a["horizons"]
    return {
        "ex_meta": ex_meta,
        "f1_hulls": f1_filters["hulls"],
        "hull_n_contrib": f1_filters["hull_n_contrib"],
        "bounds": bounds,
        "W_N": W_N,
        "W_M1": horizons["W_M1_pos"],
        "W_M2": horizons["W_M2_trend"],
        "W_M5": horizons["W_M5_vol_ratio"],
    }


def process_setup(setup, universe, tickers_sorted):
    t_setup = time.time()
    print(f"\n{'=' * 70}", flush=True)
    print(f"=== {setup.upper()}", flush=True)
    print(f"{'=' * 70}", flush=True)

    stage_a = load_stage_a(setup)
    built = rebuild_weekly_F1_hulls_and_bounds(setup, universe, stage_a)
    ex_meta = built["ex_meta"]
    W_N = built["W_N"]
    W_M1, W_M2, W_M5 = built["W_M1"], built["W_M2"], built["W_M5"]
    f1_hulls = built["f1_hulls"]
    loc_bounds = built["bounds"]
    n_ex = len(ex_meta)
    print(f"n_ex={n_ex}  W_N={W_N}  W_M1={W_M1}  W_M2={W_M2}  W_M5={W_M5}", flush=True)

    per_ticker_ex_set = {}
    for m in ex_meta:
        per_ticker_ex_set.setdefault(m["ticker"], set()).add(int(m["E_idx"]))

    # --- Full-universe weekly scan ---
    per_ticker_wf1 = {}    # ticker -> list of daily E passing weekly F1
    per_ticker_wloc = {}   # ticker -> list of daily E passing weekly Loc
    per_ticker_wboth = {}  # ticker -> list of daily E passing both
    total_cand = 0
    wf1_bars = wloc_bars = wboth_bars = 0
    t_scan = time.time()

    for i, ticker in enumerate(tickers_sorted):
        df = universe.get(ticker)
        if df is None:
            continue
        try:
            idx = wagg.build_ticker_weekly_indexing(df)
        except Exception as e:
            continue
        if idx["L"] < 2 or idx["n_weeks"] < 1:
            continue
        ds = vsc.dates_as_str(df)
        res = wagg.scan_ticker_weekly(
            idx, W_N, W_M1, W_M2, W_M5, TSH_CAP_W,
            f1_hulls, loc_bounds, DATE_CUTOFF, ds,
        )
        if res is None:
            continue
        total_cand += len(res["E"])
        E = res["E"]
        f1 = res["f1"]
        loc_all = res["p1"] & res["p2"] & res["p3"] & res["p4a"] & res["p4b"] & res["p5"]
        both = res["all_pass"]

        wf1_bars += int(f1.sum())
        wloc_bars += int(loc_all.sum())
        wboth_bars += int(both.sum())
        if f1.any():
            per_ticker_wf1[ticker] = [int(e) for e in E[f1]]
        if loc_all.any():
            per_ticker_wloc[ticker] = [int(e) for e in E[loc_all]]
        if both.any():
            per_ticker_wboth[ticker] = [int(e) for e in E[both]]

        if (i + 1) % 2000 == 0:
            print(f"  weekly scan {i+1}/{len(tickers_sorted)}  ({time.time()-t_scan:.0f}s)  "
                  f"cand={total_cand:,}  wF1={wf1_bars:,}  wLoc={wloc_bars:,}  wBoth={wboth_bars:,}",
                  flush=True)
    print(f"Weekly scan done: {time.time()-t_scan:.1f}s  total_cand={total_cand:,}", flush=True)

    # --- section5.2 dedup on each survivor set ---
    def build_clusters(per_ticker):
        out = []
        for t, E_list in per_ticker.items():
            ex_set = per_ticker_ex_set.get(t, set())
            reps = vsc.dedupe_clusters(E_list, ex_set)
            for r in reps:
                out.append((t, r, r in ex_set))
        return out

    wf1_clusters = build_clusters(per_ticker_wf1)
    wloc_clusters = build_clusters(per_ticker_wloc)
    wboth_clusters = build_clusters(per_ticker_wboth)

    def counts(clusters):
        ex_m = sum(1 for _, _, m in clusters if m)
        return len(clusters), ex_m, len(clusters) - ex_m

    wf1_n, wf1_ex, wf1_non = counts(wf1_clusters)
    wloc_n, wloc_ex, wloc_non = counts(wloc_clusters)
    wboth_n, wboth_ex, wboth_non = counts(wboth_clusters)
    print(f"Weekly clusters  F1={wf1_n:,} (ex {wf1_ex}/{n_ex}, non-ex {wf1_non:,})", flush=True)
    print(f"                 Loc={wloc_n:,} (ex {wloc_ex}/{n_ex}, non-ex {wloc_non:,})", flush=True)
    print(f"                 Both={wboth_n:,} (ex {wboth_ex}/{n_ex}, non-ex {wboth_non:,})", flush=True)

    # --- Daily AND weekly intersection ---
    daily_keys, _ = load_daily_clusters(setup)
    if daily_keys is None:
        print(f"  WARN: no daily F1_and_Loc CSV at {DAILY_DIR}/{setup}_F1_Loc_clusters.csv - skipping intersect", flush=True)
        dw_keys = None
    else:
        weekly_keys = set((t, e) for (t, e, _) in wboth_clusters)
        dw_keys = daily_keys & weekly_keys

    # --- 100% EX end-to-end sanity ---
    ex_keys_all = set((m["ticker"], int(m["E_idx"])) for m in ex_meta)
    ex_in_wboth = ex_keys_all & set((t, e) for (t, e, _) in wboth_clusters)
    missing_weekly = ex_keys_all - ex_in_wboth
    if missing_weekly:
        print(f"  WEEKLY EX FAIL: {len(missing_weekly)}/{n_ex} examples missing from weekly F1_and_Loc:", flush=True)
        for t, e in sorted(missing_weekly):
            print(f"    {t} E_idx={e}", flush=True)

    if dw_keys is not None:
        ex_in_daily = ex_keys_all & daily_keys
        missing_daily = ex_keys_all - ex_in_daily
        if missing_daily:
            print(f"  DAILY EX FAIL: {len(missing_daily)}/{n_ex} examples missing from daily F1_and_Loc:", flush=True)
            for t, e in sorted(missing_daily):
                print(f"    {t} E_idx={e}", flush=True)
        ex_in_dw = ex_keys_all & dw_keys
        missing_dw = ex_keys_all - ex_in_dw
        if missing_dw:
            print(f"  DW EX FAIL: {len(missing_dw)}/{n_ex} examples missing from daily-and-weekly:", flush=True)
    else:
        ex_in_daily = set()
        ex_in_dw = set()
        missing_daily = set()
        missing_dw = set()

    # --- Persist ---
    def _save_cluster_csv(path, clusters):
        if not clusters:
            pd.DataFrame(columns=["ticker", "E_idx", "date", "example_matched"]).to_csv(path, index=False)
            return
        rows = []
        date_cache = {}
        for t, E, is_ex in sorted(clusters):
            df = universe.get(t)
            if df is None:
                continue
            ds = date_cache.get(t)
            if ds is None:
                ds = vsc.dates_as_str(df)
                date_cache[t] = ds
            rows.append({"ticker": t, "E_idx": E, "date": ds[E] if E < len(ds) else "",
                         "example_matched": is_ex})
        pd.DataFrame(rows).to_csv(path, index=False)

    _save_cluster_csv(os.path.join(OUT_DIR, f"{setup}_W_F1_clusters.csv"), wf1_clusters)
    _save_cluster_csv(os.path.join(OUT_DIR, f"{setup}_W_Loc_clusters.csv"), wloc_clusters)
    _save_cluster_csv(os.path.join(OUT_DIR, f"{setup}_W_F1_Loc_clusters.csv"), wboth_clusters)
    if dw_keys is not None:
        dw_clusters = sorted([(t, e, (t, e) in ex_keys_all) for (t, e) in dw_keys])
        _save_cluster_csv(os.path.join(OUT_DIR, f"{setup}_DW_F1_Loc_clusters.csv"), dw_clusters)
        dw_n = len(dw_keys)
        dw_ex = len(ex_in_dw)
    else:
        dw_n = dw_ex = None

    summary = {
        "setup": setup,
        "n_ex": n_ex,
        "W_N": W_N, "W_M1": W_M1, "W_M2": W_M2, "W_M5": W_M5,
        "total_candidates": int(total_cand),
        "weekly_F1_bars": int(wf1_bars),
        "weekly_Loc_bars": int(wloc_bars),
        "weekly_Both_bars": int(wboth_bars),
        "weekly_F1_clusters": wf1_n,
        "weekly_F1_ex_matched": wf1_ex,
        "weekly_Loc_clusters": wloc_n,
        "weekly_Loc_ex_matched": wloc_ex,
        "weekly_F1_Loc_clusters": wboth_n,
        "weekly_F1_Loc_ex_matched": wboth_ex,
        "daily_F1_Loc_clusters": len(daily_keys) if daily_keys is not None else None,
        "daily_F1_Loc_ex_matched": len(ex_in_daily) if daily_keys is not None else None,
        "DW_clusters": dw_n,
        "DW_ex_matched": dw_ex,
        "missing_weekly": sorted([(t, e) for (t, e) in missing_weekly]),
        "missing_daily": sorted([(t, e) for (t, e) in (missing_daily if dw_keys is not None else set())]),
        "missing_DW": sorted([(t, e) for (t, e) in (missing_dw if dw_keys is not None else set())]),
        "elapsed_s": round(time.time() - t_setup, 1),
    }
    with open(os.path.join(OUT_DIR, f"{setup}_stage_b.json"), 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"Setup elapsed: {time.time()-t_setup:.1f}s", flush=True)
    return summary


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    t0 = time.time()
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)} tickers  ({time.time()-t0:.1f}s)", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print(f"FAIL: ticker count below {MIN_TICKER_COUNT}")
        sys.exit(1)
    tickers_sorted = sorted(universe.keys())

    results = []
    for setup in SETUPS:
        try:
            r = process_setup(setup, universe, tickers_sorted)
            results.append(r)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  SETUP {setup} FAILED: {e}", flush=True)

    with open(os.path.join(OUT_DIR, "all_setups_stage_b.json"), 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\n{'=' * 110}", flush=True)
    print(f"=== MASTER - weekly Stage B summary", flush=True)
    print(f"{'=' * 110}", flush=True)
    hdr = (f"{'setup':<8}{'n_ex':<6}{'W_N':<5}"
           f"{'wF1_cl':>10}{'wLoc_cl':>11}{'wBoth_cl':>11}{'wEX':>10}"
           f"{'dF1L_cl':>10}{'dEX':>10}{'DW_cl':>10}{'DW_EX':>10}")
    print(hdr, flush=True)
    for r in results:
        print(f"{r['setup']:<8}{r['n_ex']:<6}{r['W_N']:<5}"
              f"{r['weekly_F1_clusters']:>10,}"
              f"{r['weekly_Loc_clusters']:>11,}"
              f"{r['weekly_F1_Loc_clusters']:>11,}"
              f"{r['weekly_F1_Loc_ex_matched']}/{r['n_ex']:<8}"
              f"{(r['daily_F1_Loc_clusters'] if r['daily_F1_Loc_clusters'] is not None else '-'):>10}"
              f"{(r['daily_F1_Loc_ex_matched'] if r['daily_F1_Loc_ex_matched'] is not None else '-')}/{r['n_ex']:<8}"
              f"{(r['DW_clusters'] if r['DW_clusters'] is not None else '-'):>10}"
              f"{(r['DW_ex_matched'] if r['DW_ex_matched'] is not None else '-')}/{r['n_ex']:<8}",
              flush=True)

    print(f"\nWrote outputs to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
