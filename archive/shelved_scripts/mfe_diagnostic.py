"""
MFE Gap Diagnostic
==================

Purpose: test the "indicator lag ceiling" hypothesis from MFE_CAPTURE_PROJECT.md.
For each example in each setup, measure:
  - MFE bar: offset from signal bar where max favorable excursion occurred
  - Exit bar: offset where pick 0 rule fired (from latest signal_exit_{setup}.json)
  - Gap: exit_bar - mfe_bar (positive = late/lag, negative = preemptive exit)

Outputs a per-setup summary + per-example CSV to data/diagnostics/.
No OHLCV writes. No expression cache touches. Pure read + report.

Usage:
    SCANPERFECT_READ_ROOT=<main-repo> python scripts/mfe_diagnostic.py
"""

import csv
import json
import os
import pickle
import sqlite3
import sys

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_ROOT = os.environ.get("SCANPERFECT_READ_ROOT", REPO_ROOT)

OHLCV_PATH = os.path.join(READ_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
DB_PATH = os.path.join(READ_ROOT, "data", "scanperfect.db")
GRIND_DIR = os.path.join(REPO_ROOT, "data", "signal_exit_grind")
OUT_DIR = os.path.join(REPO_ROOT, "data", "diagnostics")
os.makedirs(OUT_DIR, exist_ok=True)

SETUPS = ["htf", "bf", "base", "dtss"]
MAX_FORWARD = 120  # matches grinder default


def get_direction(setup_type):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Setup '{setup_type}' not found")
    return row[0]


def load_ohlcv():
    print(f"Loading OHLCV from {OHLCV_PATH}")
    with open(OHLCV_PATH, "rb") as f:
        return pickle.load(f)


def compute_mfe_bar(df, signal_date, direction, max_forward=MAX_FORWARD):
    """Return (mfe_bar_offset, mfe_adr, signal_close, adr_at_signal, n_forward).

    MFE bar is the forward bar offset (1-indexed from signal) where the
    favorable extreme occurred. Uses ADR from a 14-bar trailing window at
    the signal bar (mirrors the grinder's adr_at_signal column).
    """
    dates = [str(d)[:10] for d in df["date"].values]
    if signal_date not in dates:
        return None
    scan_idx = dates.index(signal_date)

    # 14-bar ADR ending at signal bar
    h = df["high"].values
    l = df["low"].values
    start = max(0, scan_idx - 13)
    adr = float(np.mean(h[start : scan_idx + 1] - l[start : scan_idx + 1]))
    if adr <= 0:
        return None

    signal_close = float(df["close"].values[scan_idx])
    n_available = len(df) - scan_idx - 1
    n_forward = min(max_forward, n_available)
    if n_forward < 5:
        return None

    if direction == "short":
        fwd = df["low"].values[scan_idx + 1 : scan_idx + n_forward + 1]
        favorable = signal_close - fwd  # bigger = better for shorts
    else:
        fwd = df["high"].values[scan_idx + 1 : scan_idx + n_forward + 1]
        favorable = fwd - signal_close  # bigger = better for longs

    mfe_idx = int(np.argmax(favorable))
    mfe_adr = float(favorable[mfe_idx] / adr)
    mfe_bar = mfe_idx + 1  # 1-indexed from signal (bar 1 = first post-signal bar)

    return {
        "mfe_bar": mfe_bar,
        "mfe_adr": round(mfe_adr, 2),
        "signal_close": round(signal_close, 2),
        "adr_at_signal": round(adr, 4),
        "n_forward": n_forward,
    }


def load_latest_grind(setup_type):
    path = os.path.join(GRIND_DIR, f"signal_exit_{setup_type}.json")
    with open(path) as f:
        return json.load(f)


def describe(name, values):
    arr = np.array([v for v in values if v is not None], dtype=float)
    if len(arr) == 0:
        return f"  {name}: (no data)"
    return (
        f"  {name}: n={len(arr):3d}  "
        f"mean={arr.mean():+7.2f}  median={np.median(arr):+7.2f}  "
        f"p25={np.percentile(arr,25):+7.2f}  p75={np.percentile(arr,75):+7.2f}  "
        f"min={arr.min():+7.2f}  max={arr.max():+7.2f}"
    )


def run_setup(setup, ohlcv):
    print(f"\n=== {setup.upper()} ===")
    direction = get_direction(setup)
    print(f"  direction: {direction}")
    grind = load_latest_grind(setup)
    examples = grind["examples"]
    pick0 = grind["or_exit_set"][0]
    exit_bars = pick0["per_example_exit_bars"]
    per_eff = pick0["per_example_efficiency"]
    per_adr = pick0["per_example_adr"]
    print(
        f"  n_examples={len(examples)}  pick0={pick0['expression']} {pick0['direction']} "
        f"{pick0['threshold']:.4f}"
    )
    print(
        f"  pick0 baseline: med_eff={pick0['median_efficiency']:.3f} "
        f"mean_eff={pick0['mean_efficiency']:.3f} "
        f"med_adr={pick0['median_adr']:.2f} "
        f"avg_bars_to_exit={pick0['avg_bars_to_exit']}"
    )

    rows = []
    for i, ex in enumerate(examples):
        ticker = ex["ticker"]
        signal_date = ex["signal_date"]
        df = ohlcv.get(ticker)
        if df is None:
            continue
        info = compute_mfe_bar(df, signal_date, direction)
        if info is None:
            continue
        exit_bar = exit_bars[i]
        eff = per_eff[i]
        adr = per_adr[i]
        gap = None if exit_bar is None else exit_bar - info["mfe_bar"]
        rows.append(
            {
                "setup": setup,
                "ticker": ticker,
                "signal_date": signal_date,
                "signal_close": info["signal_close"],
                "adr_at_signal": info["adr_at_signal"],
                "mfe_adr_computed": info["mfe_adr"],
                "mfe_adr_json": ex["mfe_adr"],
                "mfe_bar": info["mfe_bar"],
                "exit_bar": exit_bar,
                "gap_exit_minus_mfe": gap,
                "capture_eff": eff,
                "capture_adr": adr,
                "n_forward": info["n_forward"],
            }
        )

    # Summary
    print(f"\n  Summary for {setup.upper()} (n={len(rows)}):")
    print(describe("MFE bar (offset from signal)   ", [r["mfe_bar"] for r in rows]))
    print(describe("exit bar                        ", [r["exit_bar"] for r in rows]))
    print(
        describe(
            "gap (exit - MFE, positive = late)",
            [r["gap_exit_minus_mfe"] for r in rows],
        )
    )
    print(describe("capture efficiency              ", [r["capture_eff"] for r in rows]))

    # Late-exit breakdown
    gaps = [r["gap_exit_minus_mfe"] for r in rows if r["gap_exit_minus_mfe"] is not None]
    late_5 = sum(1 for g in gaps if g > 5)
    late_15 = sum(1 for g in gaps if g > 15)
    late_30 = sum(1 for g in gaps if g > 30)
    premature = sum(1 for g in gaps if g < 0)
    at_mfe = sum(1 for g in gaps if -2 <= g <= 2)
    print(f"\n  Late-exit breakdown (n={len(gaps)}):")
    print(
        f"    gap > 5 bars:   {late_5:3d} ({100*late_5/len(gaps):4.1f}%) "
        f"gap > 15 bars: {late_15:3d} ({100*late_15/len(gaps):4.1f}%) "
        f"gap > 30 bars: {late_30:3d} ({100*late_30/len(gaps):4.1f}%)"
    )
    print(
        f"    preemptive (exit before MFE): {premature:3d} ({100*premature/len(gaps):4.1f}%)"
    )
    print(f"    near MFE (|gap| <= 2):        {at_mfe:3d} ({100*at_mfe/len(gaps):4.1f}%)")

    # Correlation between gap size and capture loss
    pairs = [
        (r["gap_exit_minus_mfe"], r["capture_eff"])
        for r in rows
        if r["gap_exit_minus_mfe"] is not None and r["capture_eff"] is not None
    ]
    if pairs:
        gaps_arr = np.array([p[0] for p in pairs], dtype=float)
        effs_arr = np.array([p[1] for p in pairs], dtype=float)
        if gaps_arr.std() > 0 and effs_arr.std() > 0:
            rho = float(np.corrcoef(gaps_arr, effs_arr)[0, 1])
            print(f"    corr(gap, capture_eff): {rho:+.3f}")
        # capture eff split by late vs near-MFE
        near_effs = effs_arr[np.abs(gaps_arr) <= 5]
        late_effs = effs_arr[gaps_arr > 15]
        if len(near_effs) and len(late_effs):
            print(
                f"    mean capture_eff near-MFE (|gap|<=5): {near_effs.mean():.3f}  "
                f"late (gap>15): {late_effs.mean():.3f}"
            )

    return rows


def main():
    ohlcv = load_ohlcv()
    all_rows = []
    for setup in SETUPS:
        rows = run_setup(setup, ohlcv)
        all_rows.extend(rows)

    # CSV dump
    csv_path = os.path.join(OUT_DIR, "mfe_gap_diagnostic.csv")
    with open(csv_path, "w", newline="") as f:
        if all_rows:
            writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} rows to {csv_path}")

    # Per-setup mean capture at different exit strategies (informational)
    print("\n=== Cross-setup summary ===")
    for setup in SETUPS:
        rows = [r for r in all_rows if r["setup"] == setup]
        if not rows:
            continue
        effs = np.array([r["capture_eff"] for r in rows if r["capture_eff"] is not None])
        print(
            f"  {setup.upper():5s}  n={len(effs):3d}  "
            f"pick0_mean_capture_eff={effs.mean():.3f}  target=0.700"
        )


if __name__ == "__main__":
    main()
