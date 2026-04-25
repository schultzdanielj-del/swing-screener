"""Scan-only driver for the MA-corridor filter.

Loads per-setup selections from research/presignal_ma_corridor/{setup}_selection.json
(written by presignal_ma_corridor.py's selection phase) and runs the universe scan
+ §5.2 dedup + 100% EX end-to-end verification.

Uses the same ticker cache / daily MA / weekly MA machinery as the main script.
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
import presignal_ma_corridor as pmc  # reuse compute_mas, build_ticker_cache, weekly_feat_at, scan_ticker

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_ma_corridor")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
DATE_CUTOFF = "2020-01-02"


def decode_cell_label(label, kind):
    """Decode 'SMA5@E-12' or 'EMA20@W-3' → (ma_idx, offset)."""
    if kind == "daily":
        head, k = label.split("@E-")
    else:
        head, k = label.split("@W-")
    m_idx = pmc.MA_TYPES.index(head)
    return m_idx, int(k)


def load_selection(setup):
    path = os.path.join(OUT_DIR, f"{setup}_selection.json")
    sel = json.load(open(path))
    W_N = int(sel["W_N"])
    d_lbls = sel["daily_cells"]
    w_lbls = sel["weekly_cells"]
    sel_d_ma = np.array([decode_cell_label(l, "daily")[0] for l in d_lbls], dtype=np.int64)
    sel_d_off = np.array([decode_cell_label(l, "daily")[1] for l in d_lbls], dtype=np.int64)
    sel_w_ma = np.array([decode_cell_label(l, "weekly")[0] for l in w_lbls], dtype=np.int64)
    sel_w_off = np.array([decode_cell_label(l, "weekly")[1] for l in w_lbls], dtype=np.int64)
    d_lo = np.array(sel["d_lo"], dtype=np.float64)
    d_hi = np.array(sel["d_hi"], dtype=np.float64)
    w_lo = np.array(sel["w_lo"], dtype=np.float64)
    w_hi = np.array(sel["w_hi"], dtype=np.float64)
    return {
        "W_N": W_N,
        "sel_d_ma": sel_d_ma, "sel_d_off": sel_d_off,
        "sel_w_ma": sel_w_ma, "sel_w_off": sel_w_off,
        "d_lo": d_lo, "d_hi": d_hi,
        "w_lo": w_lo, "w_hi": w_hi,
        "kept_examples": sel["kept_examples"],
        "n_ex_kept": sel["n_ex_kept"],
        "ex_pass_d_count": sel["ex_pass_d_count"],
        "ex_pass_w_count": sel["ex_pass_w_count"],
        "ex_pass_combined_count": sel["ex_pass_combined_count"],
    }


def main():
    print(f"OHLCV cache: {CACHE_DIR}", flush=True)
    pkl = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    with open(pkl, "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)

    tcache = {}
    scan_summary = {}
    for setup in SETUPS:
        st = load_selection(setup)
        print(f"\n=== SCAN  {setup.upper()}  daily_cells={len(st['sel_d_ma'])}  weekly_cells={len(st['sel_w_ma'])}  W_N={st['W_N']}", flush=True)

        t0 = time.time()
        passes = []
        n_seen = 0
        for tk, df in universe.items():
            n_seen += 1
            if tk not in tcache:
                tcache[tk] = pmc.build_ticker_cache(df)
            tc = tcache[tk]
            ds = vsc.dates_as_str(df)
            res = pmc.scan_ticker_fast(tk, tc, df, st["W_N"],
                                  st["sel_d_ma"], st["sel_d_off"], st["d_lo"], st["d_hi"],
                                  st["sel_w_ma"], st["sel_w_off"], st["w_lo"], st["w_hi"],
                                  DATE_CUTOFF, ds)
            passes.extend(res)
            if n_seen % 1000 == 0:
                dt = time.time() - t0
                rate = n_seen / dt if dt > 0 else 0
                eta = (len(universe) - n_seen) / rate if rate > 0 else 0
                print(f"  [{setup}] {n_seen}/{len(universe):,} tickers  {len(passes):,} passes  ({dt:.1f}s, eta {eta:.0f}s)", flush=True)
        dt = time.time() - t0
        print(f"  pre-dedup passes: {len(passes):,}  ({dt:.1f}s)", flush=True)

        example_keys = set()
        for e in st["kept_examples"]:
            if e["ticker"] in universe:
                E = vsc.lookup_idx(universe[e["ticker"]], e["entry_date"])
                if E >= 0:
                    example_keys.add((e["ticker"], E))

        by_ticker = {}
        for p in passes:
            by_ticker.setdefault(p["ticker"], []).append(p["E_idx"])
        dedup_total = 0
        ex_covered = 0
        for tk, E_list in by_ticker.items():
            ex_set = {e_idx for tkk, e_idx in example_keys if tkk == tk}
            deduped = vsc.dedupe_clusters(sorted(E_list), ex_set)
            dedup_total += len(deduped)
            ex_covered += sum(1 for e in ex_set if e in deduped)
        print(f"  post-dedup clusters: {dedup_total:,}", flush=True)
        print(f"  EX cluster coverage (end-to-end): {ex_covered}/{len(example_keys)}", flush=True)

        scan_summary[setup] = {
            "W_N": st["W_N"],
            "n_ex_kept": st["n_ex_kept"],
            "daily_cells": int(len(st["sel_d_ma"])),
            "weekly_cells": int(len(st["sel_w_ma"])),
            "pre_dedup_passes": len(passes),
            "post_dedup_clusters": dedup_total,
            "ex_pass_end_to_end": ex_covered,
            "ex_pass_selection_combined": st["ex_pass_combined_count"],
            "scan_seconds": dt,
        }

        # Save passes per setup (lean format)
        with open(os.path.join(OUT_DIR, f"{setup}_passes.json"), "w") as f:
            json.dump({
                "setup": setup,
                "n_pre_dedup": len(passes),
                "n_post_dedup": dedup_total,
                "ex_coverage": ex_covered,
                "n_ex_examples": len(example_keys),
            }, f, indent=2)

    with open(os.path.join(OUT_DIR, "scan_summary.json"), "w") as f:
        json.dump(scan_summary, f, indent=2)
    print(f"\n=== SUMMARY ===", flush=True)
    print(json.dumps(scan_summary, indent=2), flush=True)
    print(f"\nSpread (post-dedup): max/min = "
          f"{max(v['post_dedup_clusters'] for v in scan_summary.values()):,} / "
          f"{min(v['post_dedup_clusters'] for v in scan_summary.values()):,}", flush=True)


if __name__ == "__main__":
    main()
