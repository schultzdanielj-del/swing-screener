"""Build per-setup classifier pool = (pyramid ∩ presignal pre-dedup) ∪ DB examples at E-1.

Revised 2026-04-21 evening: no rightmost-wins dedup. Every pyramid signal bar that
passes presignal (pre_dedup_by_ticker) is its own pool cluster. Previously the pool
used dedup_by_ticker (rightmost of each contiguous presignal run), which is a
forward-looking label and caused backtest-vs-live slippage. Under pre_dedup, live
scanner and backtest fire on the same bars.

For each of the 5 setups:
  1. Load presignal pre_dedup_by_ticker (all presignal-positive bars, signal-bar-keyed)
     from research/presignal_sma_band_scan/{setup}_passes.pkl.
  2. Load pyramid final_signals (ticker, signal_date pairs) from the most recent
     non-sig0 pyramid JSON, choosing the tier whose final_signals length matches
     summary.final_total.
  3. Map each pyramid signal_date to a signal_bar_idx via OHLCV dates (vsc.lookup_idx).
  4. Base hits = every (ticker, signal_bar_idx) in pyramid whose signal_bar_idx
     also lies in the presignal pre_dedup set for that ticker.
  5. DB union: every DB example's (ticker, sig_idx=entry_idx-1) is unioned into
     hits regardless of intersection membership. §2 c1 permits entry-bar reference
     for examples (ground-truth labeling); non-examples still come from intersection
     only. Ensures all DB examples land is_example=1 even when pyramid warmup mask
     or stale presignal seeds drop them.
  6. Attach is_example / example_entry_date / example_entry_idx via DB join.

Checks (hard fail if violated):
  - Intersection count per setup equals pyramid_alignment.json's `aligned_pre_dedup`.
  - Post-union: every DB example appears as a cluster (trivially true by construction).

Output per setup: research/classifier_pool/{setup}_pool.json

Writes only to the worktree. Reads OHLCV / DB / pyramid JSONs from main repo cache
read-only. Setup-agnostic: one algorithm, iterates all 5 setups.
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import re
import sqlite3
import sys
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
OHLCV_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")

SCAN_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")
OUT_DIR = os.path.join(WORKTREE, "research", "classifier_pool")
ALIGNMENT_JSON = os.path.join(SCAN_DIR, "pyramid_alignment.json")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]


def latest_pyramid_file(setup):
    pat = os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*_pk*_*.json")
    files = glob.glob(pat)
    good = [f for f in files if "sig0_pk0" not in os.path.basename(f)] or files
    def ts_key(p):
        m = re.search(r"_(\d{8})_(\d{6})\.json$", p)
        return (m.group(1), m.group(2)) if m else ("", "")
    return max(good, key=ts_key) if good else None


def load_pyramid_signals(setup):
    """Return (list of (ticker, signal_date) pairs, filename). Selects tier whose
    final_signals count matches summary.final_total; falls back to last tier."""
    p = latest_pyramid_file(setup)
    if p is None:
        return None, None
    with open(p) as f:
        d = json.load(f)
    tr = d.get("tier_results", {})
    target = d.get("summary", {}).get("final_total")
    best = None
    if target is not None:
        for k, v in tr.items():
            if isinstance(v, dict) and "final_signals" in v:
                fs = v["final_signals"]
                if isinstance(fs, list) and len(fs) == target:
                    best = fs
                    break
    if best is None:
        for k, v in tr.items():
            if isinstance(v, dict) and "final_signals" in v:
                fs = v["final_signals"]
                if isinstance(fs, list):
                    best = fs
    if best is None:
        return None, None
    pairs = [(s["ticker"], s["date"]) for s in best if "ticker" in s and "date" in s]
    return pairs, os.path.basename(p)


def load_presignal_dedup(setup):
    """Return (pre_dedup_by_ticker dict {ticker: [signal_bar_idx,...]}, filename).

    Uses pre_dedup (all presignal-positive bars) rather than dedup (rightmost-of-run)
    per 2026-04-21 revision — no forward-looking rightmost collapse.
    """
    path = os.path.join(SCAN_DIR, f"{setup}_passes.pkl")
    if not os.path.exists(path):
        return None, None
    with open(path, "rb") as f:
        d = pickle.load(f)
    # Signal-bar keying check — presignal grinder §4.4 keys output by signal bar E-1.
    is_signal_keyed = (
        "example_signal_keys" in d
        or d.get("summary", {}).get("key_semantic", "").startswith("signal")
    )
    if not is_signal_keyed:
        raise RuntimeError(
            f"{path} is not signal-bar-keyed — cannot intersect with pyramid signal_date."
        )
    return d["pre_dedup_by_ticker"], os.path.basename(path)


def load_db_examples(setup):
    """Return list of {ticker, entry_date} for this setup."""
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ticker, entry_date FROM examples WHERE setup_type=? "
            "ORDER BY ticker, entry_date",
            (setup,),
        ).fetchall()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def build_pool_for_setup(setup, universe, alignment_ref):
    dedup_by_ticker, presig_file = load_presignal_dedup(setup)
    if dedup_by_ticker is None:
        raise RuntimeError(f"presignal passes missing for {setup}")
    pyr_pairs, pyr_file = load_pyramid_signals(setup)
    if pyr_pairs is None:
        raise RuntimeError(f"pyramid JSON missing for {setup}")

    dedup_set_by_ticker = {tk: set(idxs) for tk, idxs in dedup_by_ticker.items()}

    # Build DB example lookup: (ticker, entry_date_str) -> entry_bar_idx.
    db_examples = load_db_examples(setup)
    example_lookup = {}   # (ticker, signal_bar_idx) -> {"entry_date": str, "entry_idx": int}
    missing_example_tickers = []
    for ex in db_examples:
        tk = ex["ticker"]
        entry_date = str(ex["entry_date"])[:10]
        df = universe.get(tk)
        if df is None:
            missing_example_tickers.append((tk, entry_date, "no_df"))
            continue
        entry_idx = vsc.lookup_idx(df, entry_date)
        if entry_idx < 1:
            missing_example_tickers.append((tk, entry_date, f"bad_lookup={entry_idx}"))
            continue
        sig_idx = entry_idx - 1
        example_lookup[(tk, sig_idx)] = {
            "entry_date": entry_date,
            "entry_idx": entry_idx,
        }
    if missing_example_tickers:
        raise RuntimeError(
            f"{setup}: {len(missing_example_tickers)} DB examples have missing/unresolvable "
            f"OHLCV rows: {missing_example_tickers[:5]}..."
        )

    # Walk pyramid signals; keep unique (ticker, signal_bar_idx) that land on a
    # presignal cluster rightmost. Track pyramid-only set for diagnostics.
    hits = set()
    pyr_sig_idx_set = set()
    raw_match_count = 0  # matches the align script's aligned_dedup counter (non-unique).
    no_ticker = 0
    bad_lookup = 0
    for tk, sig_date in pyr_pairs:
        df = universe.get(tk)
        if df is None:
            no_ticker += 1
            continue
        sig_idx = vsc.lookup_idx(df, sig_date)
        if sig_idx < 0:
            bad_lookup += 1
            continue
        pyr_sig_idx_set.add((tk, sig_idx))
        if tk in dedup_set_by_ticker and sig_idx in dedup_set_by_ticker[tk]:
            raw_match_count += 1
            hits.add((tk, sig_idx))

    # Verify against pyramid_alignment.json reference count.
    # Under no-dedupe revision, intersection is pyramid ∩ pre_dedup (not ∩ dedup).
    expected = alignment_ref[setup]["aligned_pre_dedup"]
    if raw_match_count != expected:
        raise AssertionError(
            f"{setup}: raw intersection count {raw_match_count} does not match "
            f"pyramid_alignment.json aligned_pre_dedup={expected}. "
            f"Re-run presignal_sma_band_pyramid_align.py first."
        )

    # DB-union: examples must land in the pool regardless of intersection membership.
    # §2 c1 permits entry-bar ref for examples; recovers IPO-window setups (CRCL)
    # dropped by pyramid warmup mask and swaps (AAPU/MSFU) with stale presignal seeds.
    intersection_native_examples = set(example_lookup.keys()) & hits
    db_examples_unioned_in = set(example_lookup.keys()) - hits
    hits |= set(example_lookup.keys())

    # Sort clusters deterministically: by ticker, then signal_bar_idx ascending.
    sorted_hits = sorted(hits, key=lambda p: (p[0], p[1]))

    clusters = []
    for idx, (tk, sig_idx) in enumerate(sorted_hits):
        df = universe[tk]
        sig_date = str(df["date"].values[sig_idx])[:10]
        sig_close = float(df["close"].values[sig_idx])
        ex_info = example_lookup.get((tk, sig_idx))
        is_example = ex_info is not None
        clusters.append({
            "cluster_id": idx,
            "ticker": tk,
            "rightmost": {
                "bar_idx": int(sig_idx),
                "date": sig_date,
                "close": sig_close,
            },
            "is_example": 1 if is_example else 0,
            "example_entry_date": ex_info["entry_date"] if is_example else None,
            "example_entry_idx": int(ex_info["entry_idx"]) if is_example else None,
        })

    # Post-union invariant: every DB example appears as a cluster by construction.
    example_hits = {(c["ticker"], c["rightmost"]["bar_idx"]) for c in clusters if c["is_example"]}
    db_example_keys = set(example_lookup.keys())
    assert db_example_keys <= example_hits, (
        f"{setup}: post-union DB example invariant broken — this should be impossible"
    )
    unioned_in_detail = []
    for key in sorted(db_examples_unioned_in):
        tk, sig_idx = key
        info = example_lookup[key]
        unioned_in_detail.append({
            "ticker": tk,
            "signal_bar_idx": int(sig_idx),
            "entry_date": info["entry_date"],
            "entry_idx": int(info["entry_idx"]),
            "in_presignal_dedup": tk in dedup_set_by_ticker and sig_idx in dedup_set_by_ticker[tk],
            "in_pyramid_final_signals": (tk, sig_idx) in pyr_sig_idx_set,
        })

    n_total = len(clusters)
    n_ex = sum(c["is_example"] for c in clusters)
    n_nonex = n_total - n_ex

    out = {
        "setup": setup,
        "pool_source": "presignal pre_dedup ∩ pyramid at signal bar E-1 (no rightmost dedupe)",
        "sources": {
            "presignal_file": presig_file,
            "pyramid_file": pyr_file,
            "ohlcv_file": os.path.basename(OHLCV_FILE),
            "db_file": os.path.basename(DB),
        },
        "counts": {
            "total": n_total,
            "examples": n_ex,
            "non_examples": n_nonex,
        },
        "sanity": {
            "pyramid_signals_read": len(pyr_pairs),
            "pyramid_no_ticker_in_universe": no_ticker,
            "pyramid_bad_signal_lookup": bad_lookup,
            "raw_match_count_vs_alignment_ref": {
                "raw": raw_match_count,
                "alignment_ref_aligned_dedup": expected,
            },
            "db_examples_total": len(example_lookup),
            "db_examples_in_native_intersection": len(intersection_native_examples),
            "db_examples_unioned_in_via_db": len(db_examples_unioned_in),
            "db_examples_unioned_in_detail": unioned_in_detail,
        },
        "clusters": clusters,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    return out


def main():
    # Safety: verify resolved output dir is inside worktree before writing.
    assert OUT_DIR.startswith(WORKTREE), (
        f"Refusing to write outside worktree: {OUT_DIR}"
    )
    os.makedirs(OUT_DIR, exist_ok=True)

    if not os.path.exists(ALIGNMENT_JSON):
        raise RuntimeError(f"missing {ALIGNMENT_JSON} — run pyramid_align first")
    with open(ALIGNMENT_JSON) as f:
        alignment_ref = json.load(f)

    print(f"Loading OHLCV: {OHLCV_FILE}", flush=True)
    with open(OHLCV_FILE, "rb") as f:
        universe = pickle.load(f)
    print(f"  tickers in universe: {len(universe):,}", flush=True)
    assert len(universe) > 11000, (
        f"OHLCV ticker count {len(universe)} looks wrong (expected ~11,200+). STOPPING."
    )
    print()

    print(f"{'setup':<7} {'pyr_sigs':>9} {'pool_total':>11} {'examples':>9} {'non_ex':>8}  "
          f"{'db_ex':>6} {'ex_native':>10} {'ex_unioned':>11}", flush=True)
    print("-" * 85, flush=True)

    summary_rows = []
    for setup in SETUPS:
        out = build_pool_for_setup(setup, universe, alignment_ref)

        pool_path = os.path.join(OUT_DIR, f"{setup}_pool.json")
        assert pool_path.startswith(WORKTREE), f"refusing write outside worktree: {pool_path}"
        with open(pool_path, "w") as f:
            json.dump(out, f, indent=2)

        c = out["counts"]
        s = out["sanity"]
        print(f"{setup:<7} {s['pyramid_signals_read']:>9,} "
              f"{c['total']:>11,} {c['examples']:>9,} {c['non_examples']:>8,}  "
              f"{s['db_examples_total']:>6} {s['db_examples_in_native_intersection']:>10} "
              f"{s['db_examples_unioned_in_via_db']:>11}",
              flush=True)
        summary_rows.append({"setup": setup, **c, "pool_path": os.path.basename(pool_path)})

    summary_path = os.path.join(OUT_DIR, "pool_summary.json")
    assert summary_path.startswith(WORKTREE), f"refusing write outside worktree: {summary_path}"
    with open(summary_path, "w") as f:
        json.dump({
            "pool_source": "presignal pre_dedup ∩ pyramid at signal bar E-1 (no rightmost dedupe)",
            "setups": summary_rows,
            "alignment_ref": ALIGNMENT_JSON,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }, f, indent=2)
    print(f"\nwrote {summary_path}", flush=True)


if __name__ == "__main__":
    main()
