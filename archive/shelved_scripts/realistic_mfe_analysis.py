"""
Realistic MFE Analysis
======================

Re-scores the existing pick-0 exits against an MFE that's bounded by a realistic
hold-period ceiling: (a) bars-to-next-earnings-date, and/or (b) a fixed N-bar
cap. A move you can't realistically hold to shouldn't count against the
denominator.

For each example, for each cap regime, we compute:
  realistic_forward_bars = min(fixed_cap, bars_to_earnings, n_available)
  realistic_mfe_adr     = max favorable excursion in that window
  realistic_capture     = captured_move (at pick-0 exit bar) / realistic_mfe_adr

If the exit bar lies beyond the realistic cap, the capture is computed against
the price at the cap bar (treat the cap as a forced exit).

Cap regimes tested:
  - raw120      : no cap (current grinder baseline)
  - cap_40
  - cap_60
  - cap_80
  - earnings    : bars-to-next-earnings only
  - earnings_60 : min(earnings, 60)
  - earnings_40 : min(earnings, 40)

Outputs:
  data/diagnostics/realistic_mfe_summary.txt  — per-setup means per regime
  data/diagnostics/realistic_mfe_per_example.csv
"""

import csv
import json
import os
import pickle
import sqlite3
import sys
from datetime import datetime

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
EXCLUDE_IF_NO_EARNINGS = True

CAP_REGIMES = {
    "raw120": lambda e_bars, n_avail: min(120, n_avail),
    "earnings": lambda e_bars, n_avail: (
        None if e_bars is None else min(e_bars, n_avail)
    ),
    "earnings_m1": lambda e_bars, n_avail: (
        None if e_bars is None else min(max(e_bars - 1, 1), n_avail)
    ),
    "earnings_m3": lambda e_bars, n_avail: (
        None if e_bars is None else min(max(e_bars - 3, 1), n_avail)
    ),
}


def get_direction(setup_type):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    return row[0]


def load_earnings_map():
    """{ticker: sorted list of earnings date strings 'YYYY-MM-DD'}"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    conn.close()
    m = {}
    for ticker, date in rows:
        m.setdefault(ticker, []).append(date)
    for t in m:
        m[t].sort()
    return m


def bars_to_next_earnings(df, scan_idx, earnings_list):
    """Number of trading bars from scan_idx+1 to first earnings date > signal_date.
    Returns None if no future earnings found.
    """
    if not earnings_list:
        return None
    dates_after_signal = [str(d)[:10] for d in df["date"].values[scan_idx + 1 :]]
    signal_date = str(df["date"].values[scan_idx])[:10]
    # First earnings date strictly after signal
    for ed in earnings_list:
        if ed > signal_date:
            # Find first trading bar >= ed. If exact match, use it; if earnings
            # falls between trading days, use next trading day.
            for i, d in enumerate(dates_after_signal):
                if d >= ed:
                    return i + 1  # 1-indexed: bar 1 = first post-signal bar
            return None  # Earnings is beyond cached data
    return None


def run_setup(setup, ohlcv, earnings_map):
    direction = get_direction(setup)
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]
    pick0 = grind["or_exit_set"][0]
    exit_bars = pick0["per_example_exit_bars"]

    rows = []
    for i, ex in enumerate(examples):
        ticker = ex["ticker"]
        signal_date = ex["signal_date"]
        df = ohlcv.get(ticker)
        if df is None:
            continue
        dates_all = [str(d)[:10] for d in df["date"].values]
        if signal_date not in dates_all:
            continue
        scan_idx = dates_all.index(signal_date)
        n_available = len(df) - scan_idx - 1
        if n_available < 5:
            continue

        signal_close = float(df["close"].values[scan_idx])
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        adr_start = max(0, scan_idx - 13)
        adr = float(np.mean(h[adr_start : scan_idx + 1] - l[adr_start : scan_idx + 1]))
        if adr <= 0:
            continue

        e_bars = bars_to_next_earnings(df, scan_idx, earnings_map.get(ticker, []))

        exit_bar = exit_bars[i]

        row = {
            "setup": setup,
            "ticker": ticker,
            "signal_date": signal_date,
            "n_available": n_available,
            "bars_to_earnings": e_bars,
            "exit_bar": exit_bar,
        }

        # Compute captured move at pick-0 exit (close at exit bar)
        if exit_bar is not None and 1 <= exit_bar <= n_available:
            exit_close = float(c[scan_idx + exit_bar])
        else:
            exit_close = None
        if exit_close is not None:
            if direction == "short":
                captured = (signal_close - exit_close) / adr
            else:
                captured = (exit_close - signal_close) / adr
        else:
            captured = None
        row["pick0_captured_adr"] = round(captured, 3) if captured is not None else None

        for regime_name, cap_fn in CAP_REGIMES.items():
            cap = cap_fn(e_bars, n_available)
            if cap is None or cap < 2:
                row[f"mfe_{regime_name}"] = None
                row[f"cap_exit_bar_{regime_name}"] = None
                row[f"captured_{regime_name}"] = None
                row[f"capture_eff_{regime_name}"] = None
                continue

            # MFE over bars [1, cap]
            if direction == "short":
                fwd = l[scan_idx + 1 : scan_idx + cap + 1]
                favorable = signal_close - fwd
            else:
                fwd = h[scan_idx + 1 : scan_idx + cap + 1]
                favorable = fwd - signal_close
            mfe_adr = float(np.max(favorable)) / adr if len(favorable) else 0.0

            # Effective exit bar: if pick-0 exit is within the cap, use it.
            # Else, treat cap as a forced exit (you would have been stopped out at the cap).
            if exit_bar is not None and 1 <= exit_bar <= cap:
                eff_exit_bar = exit_bar
                eff_exit_close = float(c[scan_idx + exit_bar])
            else:
                eff_exit_bar = cap
                eff_exit_close = float(c[scan_idx + cap])
            if direction == "short":
                eff_captured = (signal_close - eff_exit_close) / adr
            else:
                eff_captured = (eff_exit_close - signal_close) / adr
            cap_eff = eff_captured / mfe_adr if mfe_adr > 1e-6 else 0.0

            row[f"mfe_{regime_name}"] = round(mfe_adr, 3)
            row[f"cap_exit_bar_{regime_name}"] = eff_exit_bar
            row[f"captured_{regime_name}"] = round(eff_captured, 3)
            row[f"capture_eff_{regime_name}"] = round(cap_eff, 4)

        rows.append(row)

    return rows


def summarize(all_rows):
    lines = []
    lines.append("=" * 80)
    lines.append("REALISTIC MFE ANALYSIS — capture efficiency by cap regime, per setup")
    lines.append("=" * 80)
    lines.append(
        f"\n{'SETUP':<6} {'N':>3}  "
        + "  ".join([f"{r:>12}" for r in CAP_REGIMES.keys()])
    )
    for setup in SETUPS:
        rows = [r for r in all_rows if r["setup"] == setup]
        if not rows:
            continue
        parts = [f"{setup.upper():<6} {len(rows):>3}"]
        for regime in CAP_REGIMES.keys():
            vals = [
                r[f"capture_eff_{regime}"]
                for r in rows
                if r[f"capture_eff_{regime}"] is not None
            ]
            if vals:
                parts.append(f"{np.mean(vals):>12.3f}")
            else:
                parts.append(f"{'-':>12}")
        lines.append("  ".join(parts))

    # Also: median capture_eff per regime
    lines.append(
        f"\n(median) {'SETUP':<6} {'N':>3}  "
        + "  ".join([f"{r:>12}" for r in CAP_REGIMES.keys()])
    )
    for setup in SETUPS:
        rows = [r for r in all_rows if r["setup"] == setup]
        if not rows:
            continue
        parts = [f"{setup.upper():<6} {len(rows):>3}"]
        for regime in CAP_REGIMES.keys():
            vals = [
                r[f"capture_eff_{regime}"]
                for r in rows
                if r[f"capture_eff_{regime}"] is not None
            ]
            if vals:
                parts.append(f"{np.median(vals):>12.3f}")
            else:
                parts.append(f"{'-':>12}")
        lines.append("  ".join(parts))

    # Earnings-bounded-forward diagnostic: how many examples are bound by earnings?
    lines.append("\n--- Earnings-distance distribution per setup ---")
    for setup in SETUPS:
        rows = [r for r in all_rows if r["setup"] == setup]
        e_bars = [r["bars_to_earnings"] for r in rows if r["bars_to_earnings"] is not None]
        if not e_bars:
            lines.append(f"  {setup.upper()}: no earnings data")
            continue
        arr = np.array(e_bars, dtype=float)
        lines.append(
            f"  {setup.upper()} n={len(arr):3d}  mean={arr.mean():6.1f}  "
            f"median={np.median(arr):6.1f}  p25={np.percentile(arr,25):6.1f}  "
            f"p75={np.percentile(arr,75):6.1f}  min={arr.min():.0f}  max={arr.max():.0f}"
        )
        # How many have earnings before bar 40 / 60 / 80?
        before_40 = int(np.sum(arr < 40))
        before_60 = int(np.sum(arr < 60))
        before_80 = int(np.sum(arr < 80))
        lines.append(
            f"         earnings within 40 bars: {before_40} ({100*before_40/len(arr):.0f}%)  "
            f"within 60: {before_60} ({100*before_60/len(arr):.0f}%)  "
            f"within 80: {before_80} ({100*before_80/len(arr):.0f}%)"
        )

    return "\n".join(lines)


def main():
    print(f"Loading OHLCV from {OHLCV_PATH}")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    earnings_map = load_earnings_map()
    print(f"Loaded earnings for {len(earnings_map)} tickers")

    all_rows = []
    for setup in SETUPS:
        rows = run_setup(setup, ohlcv, earnings_map)
        all_rows.extend(rows)
        print(f"  {setup}: {len(rows)} rows")

    # Write summary
    summary = summarize(all_rows)
    print("\n" + summary)
    with open(os.path.join(OUT_DIR, "realistic_mfe_summary.txt"), "w") as f:
        f.write(summary)

    # Write CSV
    csv_path = os.path.join(OUT_DIR, "realistic_mfe_per_example.csv")
    if all_rows:
        keys = list(all_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(all_rows)
    print(f"\nWrote {csv_path}")


if __name__ == "__main__":
    main()
