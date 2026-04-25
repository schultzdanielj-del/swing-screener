"""Alignment: presignal grinder passes vs pyramid grinder signals.

For each setup:
  1. Load pyramid final_signals from latest pyramid_{setup}_mp_sig*.json.
     Pyramid 'date' = signal bar (= entry - 1 trading day, per grinder definition).
  2. Load presignal scan output. Prefer {setup}_passes_fixed.pkl (signal-bar keyed)
     over {setup}_passes.pkl (entry-bar keyed).
  3. For each pyramid (ticker, signal_date):
     - signal_idx = daily df lookup of signal_date
     - aligned if signal_idx is in presignal's pass set (after accounting for keying).
  4. Report pyramid count, presignal count, intersection, coverage, precision, lift.
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
PYRAMID_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
SCAN_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]


def latest_pyramid_file(setup):
    pat = os.path.join(PYRAMID_DIR, f"pyramid_{setup}_mp_sig*_pk*_*.json")
    files = glob.glob(pat)
    good = [f for f in files if "sig0_pk0" not in os.path.basename(f)] or files
    def ts_key(p):
        m = re.search(r"_(\d{8})_(\d{6})\.json$", p)
        return (m.group(1), m.group(2)) if m else ("", "")
    return max(good, key=ts_key) if good else None


def load_pyramid_signals(setup):
    """Load signals from the pyramid's FINAL tier — the one whose final_signals length
    matches summary.final_total. Fallback: last tier with final_signals in dict order."""
    p = latest_pyramid_file(setup)
    if p is None:
        return None, None
    with open(p) as f:
        d = json.load(f)
    tr = d.get("tier_results", {})
    target = d.get("summary", {}).get("final_total")
    best = None
    # Prefer tier matching summary.final_total
    if target is not None:
        for k, v in tr.items():
            if isinstance(v, dict) and "final_signals" in v:
                fs = v["final_signals"]
                if isinstance(fs, list) and len(fs) == target:
                    best = fs
                    break
    # Fallback: last tier with final_signals
    if best is None:
        for k, v in tr.items():
            if isinstance(v, dict) and "final_signals" in v:
                fs = v["final_signals"]
                if isinstance(fs, list):
                    best = fs  # keep overwriting, end with last
    if best is None:
        return None, None
    pairs = [(s["ticker"], s["date"]) for s in best if "ticker" in s and "date" in s]
    return pairs, os.path.basename(p)


def load_presignal(setup):
    """Returns (pre_dedup_by_ticker, dedup_by_ticker, keying, file)
    where keying is 'signal' or 'entry'."""
    fixed = os.path.join(SCAN_DIR, f"{setup}_passes_fixed.pkl")
    buggy = os.path.join(SCAN_DIR, f"{setup}_passes.pkl")
    for path in [fixed, buggy]:
        if os.path.exists(path):
            with open(path, "rb") as f:
                d = pickle.load(f)
            # Signal-bar keyed outputs have "example_signal_keys" field
            if "example_signal_keys" in d or d.get("summary", {}).get("key_semantic", "").startswith("signal"):
                keying = "signal"
            else:
                keying = "entry"
            return d["pre_dedup_by_ticker"], d["dedup_by_ticker"], keying, os.path.basename(path)
    return None, None, None, None


def main():
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    print(f"{'setup':<7} {'pyr_sigs':>9} {'presig_pre':>11} {'presig_ded':>11} "
          f"{'alg_pre':>8} {'alg_ded':>8} {'cov_pre':>8} {'prec_pre':>9} {'prec_ded':>9} {'file':<22}")
    print("-" * 110)

    results = {}
    for setup in SETUPS:
        pyr, pyr_file = load_pyramid_signals(setup)
        if pyr is None:
            print(f"{setup:<7}  pyramid missing"); continue
        pre_dedup, dedup, keying, presig_file = load_presignal(setup)
        if pre_dedup is None:
            print(f"{setup:<7}  presignal missing"); continue

        # For each pyramid signal, map to bar_idx in presignal keying
        aligned_pre = 0
        aligned_ded = 0
        no_ticker = 0
        bad_lookup = 0
        pre_set_by_ticker = {tk: set(E_list) for tk, E_list in pre_dedup.items()}
        ded_set_by_ticker = {tk: set(E_list) for tk, E_list in dedup.items()}
        for tk, sig_date in pyr:
            df = universe.get(tk)
            if df is None:
                no_ticker += 1; continue
            sig_idx = vsc.lookup_idx(df, sig_date)
            if sig_idx < 0:
                bad_lookup += 1; continue
            if keying == "signal":
                target = sig_idx            # presignal outputs signal bar = sig_idx
            else:
                target = sig_idx + 1        # presignal outputs entry bar = sig_idx + 1
            if tk in pre_set_by_ticker and target in pre_set_by_ticker[tk]:
                aligned_pre += 1
            if tk in ded_set_by_ticker and target in ded_set_by_ticker[tk]:
                aligned_ded += 1

        n_pyr_effective = len(pyr) - no_ticker - bad_lookup
        n_pre = sum(len(v) for v in pre_dedup.values())
        n_ded = sum(len(v) for v in dedup.values())
        cov_pre = aligned_pre / n_pyr_effective if n_pyr_effective else 0.0
        prec_pre = aligned_pre / n_pre if n_pre else 0.0
        prec_ded = aligned_ded / n_ded if n_ded else 0.0

        print(f"{setup:<7} {n_pyr_effective:>9,} {n_pre:>11,} {n_ded:>11,} "
              f"{aligned_pre:>8,} {aligned_ded:>8,} {cov_pre:>7.1%} {prec_pre:>8.2%} {prec_ded:>8.2%} {presig_file:<22}",
              flush=True)

        results[setup] = {
            "pyramid_file": pyr_file,
            "presignal_file": presig_file,
            "presignal_keying": keying,
            "pyramid_signals_total": len(pyr),
            "pyramid_signals_in_universe": n_pyr_effective,
            "presignal_pre_dedup": n_pre,
            "presignal_dedup": n_ded,
            "aligned_pre_dedup": aligned_pre,
            "aligned_dedup": aligned_ded,
            "coverage_pre_dedup": cov_pre,
            "precision_pre_dedup": prec_pre,
            "precision_dedup": prec_ded,
            "no_ticker_in_universe": no_ticker,
            "bad_signal_lookup": bad_lookup,
        }

    out = os.path.join(SCAN_DIR, "pyramid_alignment.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nsaved {out}", flush=True)


if __name__ == "__main__":
    main()
