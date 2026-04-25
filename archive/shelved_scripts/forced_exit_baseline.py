"""
Forced-Exit Baseline
====================

If you exit at earnings-minus-buffer regardless of any rule, what's the mean
capture efficiency per setup? This is the floor any rule-based exit must beat.
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
EARNINGS_BUFFER = 1


def get_direction(setup_type):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    return row[0]


def load_earnings_map():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    conn.close()
    m = {}
    for t, d in rows:
        m.setdefault(t, []).append(d)
    for t in m:
        m[t].sort()
    return m


def bars_to_next_earnings(df, scan_idx, earnings_list):
    if not earnings_list:
        return None
    signal_date = str(df["date"].values[scan_idx])[:10]
    dates_after = [str(d)[:10] for d in df["date"].values[scan_idx + 1 :]]
    for ed in earnings_list:
        if ed > signal_date:
            for i, d in enumerate(dates_after):
                if d >= ed:
                    return i + 1
            return None
    return None


def main():
    print(f"Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    earnings_map = load_earnings_map()

    print(
        f"\n{'SETUP':<6}  {'N':>3}  {'forced_mean':>12}  {'forced_median':>14}  "
        f"{'mfe_mean':>10}  {'exit_bar_median':>16}"
    )
    for setup in SETUPS:
        direction = get_direction(setup)
        with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}.json")) as f:
            grind = json.load(f)
        examples = grind["examples"]

        captures = []
        mfes = []
        exit_bars = []
        for ex in examples:
            ticker = ex["ticker"]
            signal_date = ex["signal_date"]
            df = ohlcv.get(ticker)
            if df is None:
                continue
            dates_all = [str(d)[:10] for d in df["date"].values]
            if signal_date not in dates_all:
                continue
            scan_idx = dates_all.index(signal_date)
            e_bars = bars_to_next_earnings(df, scan_idx, earnings_map.get(ticker, []))
            if e_bars is None:
                continue
            n_available = len(df) - scan_idx - 1
            cap = min(max(e_bars - EARNINGS_BUFFER, 1), n_available, 120)
            if cap < 2:
                continue

            signal_close = float(df["close"].values[scan_idx])
            h = df["high"].values
            l = df["low"].values
            c = df["close"].values
            adr_start = max(0, scan_idx - 13)
            adr = float(np.mean(h[adr_start : scan_idx + 1] - l[adr_start : scan_idx + 1]))
            if adr <= 0:
                continue

            # Forced exit at cap bar
            forced_close = float(c[scan_idx + cap])
            if direction == "short":
                forced_captured = (signal_close - forced_close) / adr
                fwd = l[scan_idx + 1 : scan_idx + cap + 1]
                mfe = (signal_close - float(np.min(fwd))) / adr
            else:
                forced_captured = (forced_close - signal_close) / adr
                fwd = h[scan_idx + 1 : scan_idx + cap + 1]
                mfe = (float(np.max(fwd)) - signal_close) / adr
            cap_eff = forced_captured / mfe if mfe > 1e-6 else 0.0
            captures.append(cap_eff)
            mfes.append(mfe)
            exit_bars.append(cap)

        arr = np.array(captures)
        mfe_arr = np.array(mfes)
        eb_arr = np.array(exit_bars)
        print(
            f"{setup.upper():<6}  {len(arr):>3d}  {arr.mean():>12.3f}  "
            f"{np.median(arr):>14.3f}  {mfe_arr.mean():>10.2f}  {np.median(eb_arr):>16.1f}"
        )


if __name__ == "__main__":
    main()
