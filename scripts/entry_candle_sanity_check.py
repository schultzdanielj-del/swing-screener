"""
Entry Candle Scorer — Sanity Check

Verifies all data paths needed for the entry candle scorer:
1. Examples from Railway API (ticker + entry_date)
2. Refinement output (winner_signals + bar_idx)
3. Raw signal clusters (forward_window)
4. Expr cache lookups at entry candle bars and forward window bars

No computation, no scoring — just confirms data is accessible and shapes are correct.
"""

import os
import sys
import json
import numpy as np
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import ExprSeriesCache

API_BASE = "https://web-production-e3025.up.railway.app"
SETUP = "dtss"


def main():
    print("=" * 60)
    print("  ENTRY CANDLE SCORER — SANITY CHECK")
    print("=" * 60)

    # ── 1. Load examples from Railway API ──
    print("\n  1. Loading examples from Railway API...")
    resp = requests.get(f"{API_BASE}/api/examples/{SETUP}", timeout=30)
    resp.raise_for_status()
    examples = resp.json().get("examples", [])
    print(f"     {len(examples)} examples loaded")
    if examples:
        ex = examples[0]
        print(f"     Sample: {ex.get('ticker')} entry_date={ex.get('entryDate', ex.get('entry_date'))}")

    # ── 2. Load refinement output ──
    print("\n  2. Loading refinement output...")
    import glob
    ref_files = glob.glob(os.path.join(CACHE_DIR, f"refinement_{SETUP}_*.json"))
    if not ref_files:
        print("     ERROR: No refinement output found")
        return
    ref_files.sort(key=os.path.getmtime, reverse=True)
    ref_path = ref_files[0]
    print(f"     File: {os.path.basename(ref_path)}")
    with open(ref_path) as f:
        ref_data = json.load(f)
    winners = ref_data.get("winner_signals", [])
    print(f"     Winner signals: {len(winners)}")
    if winners:
        w = winners[0]
        print(f"     Sample: {w.get('ticker')} bar_idx={w.get('bar_idx')} move_adr={w.get('move_adr')}")

    # ── 3. Load raw_signal_clusters for forward_window ──
    print("\n  3. Loading raw_signal_clusters for forward_window...")
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{SETUP}.json")
    if not os.path.exists(cluster_path):
        print("     ERROR: No raw_signal_clusters file found")
        return
    with open(cluster_path) as f:
        cluster_data = json.load(f)
    forward_window = cluster_data.get("forward_window")
    print(f"     forward_window: {forward_window} bars")

    # ── 4. Load expr cache ──
    print("\n  4. Loading expr cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("     ERROR: Expr cache not valid")
        return
    print(f"     {expr_cache.n_expressions} expressions")

    # ── 5. Check 3 example entry candles ──
    print("\n  5. Checking example entry candle lookups...")
    checked = 0
    for ex in examples:
        if checked >= 3:
            break
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        if not ticker or not entry_date:
            continue

        dates, data = expr_cache.get_ticker(ticker)
        if dates is None:
            print(f"     {ticker}: not in expr cache, skipping")
            continue

        dates_str = [str(d)[:10] for d in dates]
        if entry_date not in dates_str:
            print(f"     {ticker}: entry_date {entry_date} not in cache dates, skipping")
            continue

        entry_idx = dates_str.index(entry_date)
        vec = data[entry_idx, :]
        n_valid = int(np.sum(~np.isnan(vec)))
        print(f"     {ticker} entry_date={entry_date} bar_idx={entry_idx} "
              f"vector shape={vec.shape} valid_values={n_valid}/{len(vec)} "
              f"sample=[{vec[0]:.4f}, {vec[1]:.4f}, {vec[2]:.4f}]")
        checked += 1

    if checked == 0:
        print("     ERROR: Could not look up any example entry candles")
        return

    # ── 6. Check 3 winner signal forward windows ──
    print("\n  6. Checking winner signal forward window lookups...")
    checked = 0
    for w in winners:
        if checked >= 3:
            break
        ticker = w.get("ticker")
        bar_idx = w.get("bar_idx")
        if ticker is None or bar_idx is None:
            continue

        dates, data = expr_cache.get_ticker(ticker)
        if dates is None:
            print(f"     {ticker}: not in expr cache, skipping")
            continue

        # Forward window: bar_idx+1 through bar_idx+forward_window
        fw_start = bar_idx + 1
        fw_end = min(bar_idx + forward_window, len(data) - 1)
        n_fw_bars = fw_end - fw_start + 1

        if n_fw_bars <= 0:
            print(f"     {ticker} bar_idx={bar_idx}: no forward window bars (end of data)")
            continue

        fw_vectors = data[fw_start:fw_end + 1, :]
        n_valid_per_bar = [int(np.sum(~np.isnan(fw_vectors[i, :]))) for i in range(min(3, n_fw_bars))]
        print(f"     {ticker} bar_idx={bar_idx} fw_bars={n_fw_bars} "
              f"fw_matrix_shape={fw_vectors.shape} "
              f"valid_per_bar(first 3)={n_valid_per_bar}")
        checked += 1

    if checked == 0:
        print("     ERROR: Could not look up any winner forward windows")
        return

    print("\n  ✓ All data paths confirmed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
