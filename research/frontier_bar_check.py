"""Per-example per-bar frontier check.

For each example, for each of its N lead-up bars, ask:
  is there AT LEAST ONE feature at this bar where this example's value
  equals the envelope min or max (across all examples)?

A bar with ``frontier = True`` means the example touches the bounding box
on at least one axis at that bar. The question of interest: does any
example have ZERO frontier bars in its entire N window? If yes, that
example sits fully interior (box shape unchanged by removing it).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
ENV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
EPS = 1e-8


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


def extract_window(ec, ticker, entry_date, N, feat_idx):
    dates, data = ec.get_ticker(ticker)
    if dates is None:
        return None
    ds = dates_as_str(dates)
    m = np.where(ds == str(entry_date)[:10])[0]
    if len(m) == 0:
        return None
    E_idx = int(m[0])
    if E_idx < N:
        return None
    win = data[E_idx - N:E_idx, feat_idx].astype(np.float32)
    return win[::-1, :]


def main():
    print(f"OUT: {ENV_DIR}", flush=True)
    print("Opening expression cache...", flush=True)
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expr cache invalid")
        sys.exit(1)

    rollup = []
    rollup.append("=" * 80)
    rollup.append("FRONTIER BAR CHECK  per example, how many of its N bars touch")
    rollup.append("the envelope min or max on at least one feature?")
    rollup.append("=" * 80)

    for setup in SETUP_ORDER:
        print(f"\n=== {setup.upper()} ===", flush=True)
        env_path = os.path.join(ENV_DIR, f"{setup}_envelope.npz")
        if not os.path.exists(env_path):
            print(f"  no envelope for {setup}", flush=True)
            continue
        env = np.load(env_path)
        ex_min = env["ex_min"].astype(np.float32)
        ex_max = env["ex_max"].astype(np.float32)
        feat_idx = env["feature_indices"]
        N, n_rel = ex_min.shape

        examples = get_examples(setup)
        per_ex = []
        t0 = time.time()
        for ex in examples:
            win = extract_window(ec, ex["ticker"], ex["entry_date"], N, feat_idx)
            if win is None:
                per_ex.append({
                    "ticker": ex["ticker"],
                    "date": str(ex["entry_date"])[:10],
                    "status": "DROPPED",
                })
                continue
            measured = ~np.isnan(win) & ~np.isnan(ex_min) & ~np.isnan(ex_max)
            at_min = measured & (np.abs(win - ex_min) <= EPS)
            at_max = measured & (np.abs(win - ex_max) <= EPS)
            frontier_cells = at_min | at_max  # (N, n_rel)
            # Per bar: does it have ANY frontier feature?
            frontier_per_bar = np.any(frontier_cells, axis=1)  # (N,)
            n_frontier_bars = int(frontier_per_bar.sum())
            per_ex.append({
                "ticker": ex["ticker"],
                "date": str(ex["entry_date"])[:10],
                "status": "ok",
                "n_frontier_bars": n_frontier_bars,
                "first_frontier_offset": (int(np.where(frontier_per_bar)[0][0])
                                          if n_frontier_bars > 0 else None),
                "last_frontier_offset": (int(np.where(frontier_per_bar)[0][-1])
                                         if n_frontier_bars > 0 else None),
            })
        print(f"  {setup}: analyzed in {time.time() - t0:.1f}s", flush=True)

        ok = [r for r in per_ex if r["status"] == "ok"]
        drp = [r for r in per_ex if r["status"] != "ok"]

        lines = []
        lines.append("=" * 80)
        lines.append(f"{setup.upper()}  N={N}  n_rel={n_rel}")
        lines.append("=" * 80)
        lines.append(f"{'ticker':<8} {'date':<12} {'n_front_bars':>14} "
                     f"{'first_off':>10} {'last_off':>10}")
        for r in ok:
            lines.append(f"{r['ticker']:<8} {r['date']:<12} "
                         f"{r['n_frontier_bars']:>14} "
                         f"{str(r['first_frontier_offset']):>10} "
                         f"{str(r['last_frontier_offset']):>10}")
        for r in drp:
            lines.append(f"{r['ticker']:<8} {r['date']:<12} DROPPED")
        lines.append("")
        # Summary
        if ok:
            counts = [r["n_frontier_bars"] for r in ok]
            zero_ex = [r for r in ok if r["n_frontier_bars"] == 0]
            lines.append(f"Examples analyzed: {len(ok)}")
            lines.append(f"n_frontier_bars  min={min(counts)}  median={int(np.median(counts))}  "
                         f"max={max(counts)}  of N={N}")
            lines.append(f"Examples with ZERO frontier bars: {len(zero_ex)} / {len(ok)}")
            if zero_ex:
                lines.append("  zero-frontier examples:")
                for r in zero_ex:
                    lines.append(f"    {r['ticker']:<8} {r['date']}")
        lines.append("")
        path = os.path.join(ENV_DIR, f"frontier_bars_{setup}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        print(f"  wrote {path}", flush=True)

        if ok:
            counts = [r["n_frontier_bars"] for r in ok]
            zero_n = sum(1 for c in counts if c == 0)
            rollup.append(f"{setup:<8}  N={N:<4}  n_ex_ok={len(ok):<4}  "
                          f"frontier_bars: min={min(counts):<4} "
                          f"median={int(np.median(counts)):<4} max={max(counts):<4}  "
                          f"zero-frontier-examples={zero_n}")

    rollup_path = os.path.join(ENV_DIR, "frontier_bars_summary.txt")
    with open(rollup_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(rollup))
    print("\n" + "\n".join(rollup), flush=True)
    print(f"\nWrote rollup to {rollup_path}", flush=True)


if __name__ == "__main__":
    main()
