"""Overfit protection on F1 + Location filters, all 5 setups.

Two tests per setup:

  G1 LOO stability — drop each example i in turn, rebuild filter (re-derive
     location M* on N-1, rebuild bounds), check if example i still passes
     the shrunken filter. Reports fail-rate. Spec §5.7 G1 target is 0.

  G3 Permutation null — draw N random (ticker, E) bars from the universe,
     build F1 + Location filter as if those random bars were the examples,
     scan a 500-ticker sample, measure carve rate. Repeat NUM_PERMS trials.
     Compare to real filter's carve rate. If real ≈ random, filter isn't
     selecting for a real pattern.

Outputs: research/presignal_grinder_all/{setup}_overfit.json and a master
research/presignal_grinder_all/overfit_summary.json.
"""
from __future__ import annotations

import json
import os
import pickle
import random
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
NUM_PERMS = 5
PERM_SAMPLE_TICKERS = 500
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def load_example_rows(setup, N_BARS, universe):
    examples = get_examples(setup)
    rows = []
    paths = []
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
        close = df["close"].values.astype(np.float64)
        log_returns = np.diff(np.log(np.where(close > 0, close, np.nan)))
        rows.append({
            "ticker": ex["ticker"], "entry_date": ex["entry_date"],
            "E_idx": E, "close": close, "log_returns": log_returns,
            "path": p,
        })
        paths.append(p)
    return rows, np.array(paths)


def rebuild_location_bounds(ex_rows, N_BARS):
    """Derive M1, M2, M5 and bounds from a set of example rows."""
    fn_d1 = lambda r, M: loc.desc_1_pos(r["close"], r["E_idx"], M)
    fn_d2 = lambda r, M: loc.desc_2_trend(r["close"], r["E_idx"], N_BARS, M)
    fn_d5 = lambda r, M: loc.desc_5_vol_ratio(r["log_returns"], r["E_idx"], M)

    M1, _, _ = loc.derive_horizon(ex_rows, fn_d1, "_D1_loo")
    M2, _, _ = loc.derive_horizon(ex_rows, fn_d2, "_D2_loo")
    M5, _, _ = loc.derive_horizon(ex_rows, fn_d5, "_D5_loo")

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
    return bounds, (M1, M2, M5)


def location_single_pass(held, bounds, Ms, N_BARS):
    """Check if a single example (dict with close, E_idx, log_returns) passes
    the full 5-descriptor location bounds. Returns (passes, per-descriptor pass dict)."""
    M1, M2, M5 = Ms
    v1 = loc.desc_1_pos(held["close"], held["E_idx"], M1)
    v2 = loc.desc_2_trend(held["close"], held["E_idx"], N_BARS, M2)
    v3 = loc.desc_3_tsh_single(held["close"], held["E_idx"])
    v4a, v4b = loc.desc_4_ath_atl(held["close"], held["E_idx"])
    v5 = loc.desc_5_vol_ratio(held["log_returns"], held["E_idx"], M5)

    def ok(x, lo, hi):
        if x is None:
            return True
        try:
            if not np.isfinite(x):
                return True
        except TypeError:
            return True
        scale = max(abs(lo), abs(hi), 1.0)
        eps = 1e-10 * scale
        return (lo - eps) <= x <= (hi + eps)

    d = {
        "D1": ok(v1, *bounds["D1"]),
        "D2": ok(v2, *bounds["D2"]),
        "D3": ok(v3, *bounds["D3"]),
        "D4a": ok(v4a, *bounds["D4a"]),
        "D4b": ok(v4b, *bounds["D4b"]),
        "D5": ok(v5, *bounds["D5"]),
    }
    return all(d.values()), d


def run_loo(setup, N_BARS, universe):
    """Leave-one-out stability on F1 and Location filters."""
    ex_rows, ex_paths = load_example_rows(setup, N_BARS, universe)
    n_ex = len(ex_rows)
    if n_ex < 4:
        return None
    print(f"  LOO on {n_ex} examples...", flush=True)

    # Reduce verbose derive_horizon print spam by swallowing stdout during LOO
    import io, contextlib
    f1_fails = []
    loc_fails = []

    for i in range(n_ex):
        mask = np.ones(n_ex, dtype=bool)
        mask[i] = False
        other_paths = ex_paths[mask]
        other_rows = [ex_rows[j] for j in range(n_ex) if j != i]

        # F1 rebuild
        f1_filters = vsc.build_filters(other_paths)
        held_path = ex_paths[i:i + 1]
        pr = vsc.check_F1_batch(held_path, f1_filters["hulls"])
        if not bool(pr[0]):
            f1_fails.append(ex_rows[i]["ticker"] + "/" + str(ex_rows[i]["entry_date"]))

        # Location rebuild — suppress derive_horizon chatter
        with contextlib.redirect_stdout(io.StringIO()):
            bounds, Ms = rebuild_location_bounds(other_rows, N_BARS)
        loc_pass, _ = location_single_pass(ex_rows[i], bounds, Ms, N_BARS)
        if not loc_pass:
            loc_fails.append(ex_rows[i]["ticker"] + "/" + str(ex_rows[i]["entry_date"]))

    print(f"    F1 LOO fails:  {len(f1_fails)}/{n_ex}  {f1_fails[:5]}", flush=True)
    print(f"    Loc LOO fails: {len(loc_fails)}/{n_ex}  {loc_fails[:5]}", flush=True)
    return {
        "n_ex": n_ex,
        "f1_loo_fails": f1_fails,
        "f1_loo_fail_rate": len(f1_fails) / n_ex,
        "loc_loo_fails": loc_fails,
        "loc_loo_fail_rate": len(loc_fails) / n_ex,
    }


def run_permutation_null(setup, N_BARS, universe, real_carve):
    """Draw random pseudo-examples, build F1 filter, scan sample, compare
    carve rate to real."""
    tickers = sorted(universe.keys())
    ex_rows, ex_paths = load_example_rows(setup, N_BARS, universe)
    n_ex = len(ex_rows)
    ex_tickers = {r["ticker"] for r in ex_rows}
    pool = [t for t in tickers if t not in ex_tickers]

    rng = np.random.RandomState(12345 + hash(setup) % 1000)
    results = []
    for perm_i in range(NUM_PERMS):
        t_p = time.time()
        # Draw n_ex random (ticker, E) with valid paths, post-2020
        chosen = []
        attempts = 0
        while len(chosen) < n_ex and attempts < 20_000:
            attempts += 1
            t_idx = rng.randint(0, len(pool))
            ticker = pool[t_idx]
            df = universe.get(ticker)
            if df is None:
                continue
            L = len(df)
            if L < N_BARS + 10:
                continue
            # Pick E with enough history AND post-cutoff date
            E_candidates_low = N_BARS
            E_candidates_high = L - 1
            if E_candidates_high <= E_candidates_low:
                continue
            E = int(rng.randint(E_candidates_low, E_candidates_high + 1))
            ds = vsc.dates_as_str(df)
            if ds[E] < DATE_CUTOFF:
                continue
            p = vsc.extract_example_path(df, E, N_BARS)
            if p is None:
                continue
            chosen.append((ticker, E, p))

        if len(chosen) < n_ex:
            continue

        rand_paths = np.array([c[2] for c in chosen])
        f1_filters = vsc.build_filters(rand_paths)

        # Scan sample of 500 tickers (exclude chosen ticker set)
        chosen_tickers = {c[0] for c in chosen}
        sample_pool = [t for t in pool if t not in chosen_tickers]
        rng.shuffle(sample_pool)
        sample = sample_pool[:PERM_SAMPLE_TICKERS]

        total = 0
        f1_pass = 0
        for ticker in sample:
            df = universe.get(ticker)
            if df is None:
                continue
            n_cand, f1_E, _, _ = vsc.scan_ticker(df, N_BARS, f1_filters, DATE_CUTOFF)
            total += n_cand
            f1_pass += len(f1_E)

        carve = f1_pass / total if total > 0 else 0.0
        results.append({
            "f1_pass": int(f1_pass),
            "total": int(total),
            "f1_bar_carve": float(carve),
            "sec": round(time.time() - t_p, 1),
        })
        print(f"    perm {perm_i + 1}/{NUM_PERMS}: carve={carve * 100:.4f}%  "
              f"({f1_pass}/{total})  {time.time() - t_p:.1f}s", flush=True)

    if not results:
        return None

    carves = np.array([r["f1_bar_carve"] for r in results])
    return {
        "n_ex": n_ex,
        "trials": len(results),
        "random_carve_mean": float(carves.mean()),
        "random_carve_std": float(carves.std()),
        "random_carve_min": float(carves.min()),
        "random_carve_max": float(carves.max()),
        "real_carve": float(real_carve),
        "ratio_real_over_random": float(real_carve / max(carves.mean(), 1e-12)),
        "trials_raw": results,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)} tickers", flush=True)

    master = {}
    for setup, N_BARS in SETUPS:
        print(f"\n=== {setup.upper()} ===", flush=True)
        t0 = time.time()

        # Read real carve from main scan summary
        summary_path = os.path.join(OUT_DIR, f"{setup}_summary.json")
        real_carve = None
        if os.path.exists(summary_path):
            with open(summary_path) as f:
                s = json.load(f)
            if s.get("total_candidates"):
                # Use F1 BAR carve for comparability with permutation (which also uses F1 bars)
                real_carve = s["F1_bars"] / s["total_candidates"]
        if real_carve is None:
            print(f"  WARNING: no main summary, real carve unknown", flush=True)
            real_carve = 0.0

        loo = run_loo(setup, N_BARS, universe)
        perm = run_permutation_null(setup, N_BARS, universe, real_carve)

        out = {"setup": setup, "N_bars": N_BARS, "loo": loo, "permutation_null": perm}
        with open(os.path.join(OUT_DIR, f"{setup}_overfit.json"), 'w') as f:
            json.dump(out, f, indent=2)
        master[setup] = out
        print(f"  {setup} overfit elapsed: {time.time() - t0:.1f}s", flush=True)

    with open(os.path.join(OUT_DIR, "overfit_summary.json"), 'w') as f:
        json.dump(master, f, indent=2)

    # Master table
    print(f"\n{'=' * 100}", flush=True)
    print(f"=== OVERFIT SUMMARY", flush=True)
    print(f"{'=' * 100}", flush=True)
    print(f"{'setup':<8}{'N':<5}{'n_ex':<6}"
          f"{'F1 LOO fail':>14}{'Loc LOO fail':>14}"
          f"{'real carve':>14}{'random mean':>14}{'ratio':>10}", flush=True)
    for setup, _ in SETUPS:
        d = master.get(setup, {})
        l = d.get("loo")
        p = d.get("permutation_null")
        if l is None:
            continue
        real_c = (p["real_carve"] if p else 0.0) * 100
        rand_m = (p["random_carve_mean"] if p else 0.0) * 100
        ratio = (p["ratio_real_over_random"] if p else 0.0)
        print(f"{setup:<8}{d['N_bars']:<5}{l['n_ex']:<6}"
              f"{l['f1_loo_fail_rate'] * 100:>10.1f}%  "
              f"{l['loc_loo_fail_rate'] * 100:>11.1f}%  "
              f"{real_c:>11.4f}%  {rand_m:>11.4f}%  {ratio:>9.4f}", flush=True)


if __name__ == "__main__":
    main()
