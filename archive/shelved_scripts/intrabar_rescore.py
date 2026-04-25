"""
Intrabar Execution Rescore
==========================

Rescore existing earnings-cap pick sets under three execution assumptions,
for each setup, using the same fire-bar selections:

  close    : current grinder exits at close[fire_bar]  (baseline)
  mid      : exits at (close + high)/2 for longs, (close+low)/2 for shorts
  peak     : exits at high[fire_bar] for longs, low[fire_bar] for shorts  (upper bound)

Also includes the OR-combined pick set. Report per-setup mean capture under
each regime. Does NOT touch the grinder — pure post-processing.
"""

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

SETUPS = ["htf", "bf", "base", "dtss"]

# Seed-42 timestamped files (lock in canonical picks since the "latest" pointer
# gets overwritten by alt-seed runs).
SEED42_FILES = {
    "htf":  "signal_exit_htf_earnings_29ex_-4.4adr_20260416_210212.json",
    "bf":   "signal_exit_bf_earnings_40ex_-0.7adr_20260416_210529.json",
    "base": "signal_exit_base_earnings_38ex_+1.8adr_20260416_210753.json",
    "dtss": "signal_exit_dtss_earnings_64ex_-6.0adr_20260416_211138.json",
}


def get_direction(setup_type):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    return row[0]


def or_combined_bars(picks, n):
    """Return list of (winning_pick_idx, exit_bar) per example; earliest fire wins."""
    out = []
    for i in range(n):
        best_bar = None
        best_p = None
        for pi, p in enumerate(picks):
            b = p["per_example_exit_bars"][i]
            if b is None:
                continue
            if best_bar is None or b < best_bar:
                best_bar = b
                best_p = pi
        out.append((best_p, best_bar))
    return out


def main():
    print(f"Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)

    print(
        f"\n{'SETUP':<6}  {'N':>3}  {'mfe_mean':>8}  "
        f"{'close_mean':>10}  {'mid_mean':>9}  {'peak_mean':>10}  {'peak-close':>10}"
    )
    for setup in SETUPS:
        path = os.path.join(GRIND_DIR, f"signal_exit_{setup}_earnings.json")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            grind = json.load(f)
        direction = get_direction(setup)
        examples = grind["examples"]
        picks = grind["or_exit_set"]
        n = grind["n_examples"]

        bar_picks = or_combined_bars(picks, n)

        close_effs = []
        mid_effs = []
        peak_effs = []
        mfes = []

        for i, ex in enumerate(examples):
            ticker = ex["ticker"]
            signal_date = ex["signal_date"]
            adr = ex["adr_at_signal"]
            signal_close = ex["signal_close"]
            mfe = ex["mfe_adr"]
            if mfe <= 0:
                continue
            df = ohlcv.get(ticker)
            if df is None:
                continue
            dates = [str(d)[:10] for d in df["date"].values]
            if signal_date not in dates:
                continue
            scan_idx = dates.index(signal_date)
            pi, bar = bar_picks[i]
            if bar is None:
                continue
            abs_bar = scan_idx + bar
            if abs_bar >= len(df):
                continue
            c = float(df["close"].values[abs_bar])
            h = float(df["high"].values[abs_bar])
            l = float(df["low"].values[abs_bar])

            if direction == "short":
                capt_close = (signal_close - c) / adr
                capt_mid = (signal_close - (c + l) / 2) / adr
                capt_peak = (signal_close - l) / adr
            else:
                capt_close = (c - signal_close) / adr
                capt_mid = ((c + h) / 2 - signal_close) / adr
                capt_peak = (h - signal_close) / adr

            close_effs.append(capt_close / mfe)
            mid_effs.append(capt_mid / mfe)
            peak_effs.append(capt_peak / mfe)
            mfes.append(mfe)

        if not close_effs:
            print(f"{setup.upper():<6}  0  (no examples)")
            continue

        def mean(a):
            return float(np.mean(a))

        delta = mean(peak_effs) - mean(close_effs)
        print(
            f"{setup.upper():<6}  {len(close_effs):>3d}  {mean(mfes):>8.2f}  "
            f"{mean(close_effs):>10.3f}  {mean(mid_effs):>9.3f}  "
            f"{mean(peak_effs):>10.3f}  {delta:>+10.3f}"
        )


if __name__ == "__main__":
    main()
