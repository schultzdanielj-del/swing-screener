"""
Take-Profit Limit-Order Grinder
===============================

For each setup, for a grid of price-target multiples X (in ADR), compute
per-example capture under the rule:
  exit = first bar where high >= signal_close + X*adr  (for longs)
         first bar where low  <= signal_close - X*adr  (for shorts)
Limit fill at signal_close +/- X*adr.

If not triggered before earnings-cap, forced exit at close of cap bar.
Capture = realized_move / earnings_bounded_mfe.

Per-example MFE is earnings-bounded (same logic as the main grinder's
--earnings-cap mode). Examples without earnings data are excluded.

Outputs:
  data/diagnostics/take_profit_grinder_{setup}.json with per-X mean/median
  per_example capture tables + trigger rates.
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
OUT_DIR = os.path.join(REPO_ROOT, "data", "diagnostics")
os.makedirs(OUT_DIR, exist_ok=True)

SETUPS = ["htf", "bf", "base", "dtss"]
X_GRID = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50]
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


def run_setup(setup, ohlcv, earnings_map):
    direction = get_direction(setup)
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}_earnings.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]

    # Resolve per-example forward window (earnings-bounded) and compute MFE
    resolved = []
    for ex in examples:
        ticker = ex["ticker"]
        signal_date = ex["signal_date"]
        df = ohlcv.get(ticker)
        if df is None:
            continue
        dates = [str(d)[:10] for d in df["date"].values]
        if signal_date not in dates:
            continue
        scan_idx = dates.index(signal_date)
        e_bars = bars_to_next_earnings(df, scan_idx, earnings_map.get(ticker, []))
        if e_bars is None:
            continue
        n_available = len(df) - scan_idx - 1
        cap = min(max(e_bars - EARNINGS_BUFFER, 1), n_available)
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

        if direction == "short":
            fwd = l[scan_idx + 1 : scan_idx + cap + 1]
            mfe_adr = (signal_close - float(np.min(fwd))) / adr
            fwd_highs = h[scan_idx + 1 : scan_idx + cap + 1]
            fwd_lows = l[scan_idx + 1 : scan_idx + cap + 1]
        else:
            fwd = h[scan_idx + 1 : scan_idx + cap + 1]
            mfe_adr = (float(np.max(fwd)) - signal_close) / adr
            fwd_highs = h[scan_idx + 1 : scan_idx + cap + 1]
            fwd_lows = l[scan_idx + 1 : scan_idx + cap + 1]

        forced_close = float(c[scan_idx + cap])

        resolved.append({
            "ticker": ticker,
            "signal_date": signal_date,
            "signal_close": signal_close,
            "adr": adr,
            "cap_bar": cap,
            "mfe_adr": mfe_adr,
            "fwd_highs": fwd_highs,
            "fwd_lows": fwd_lows,
            "forced_close": forced_close,
        })

    n = len(resolved)
    print(f"[{setup.upper()}] resolved {n} examples  direction={direction}")

    results = []
    for X in X_GRID:
        captures = []
        trig_count = 0
        for r in resolved:
            target = X * r["adr"]
            if direction == "short":
                target_price = r["signal_close"] - target
                triggered_bars = np.where(r["fwd_lows"] <= target_price)[0]
            else:
                target_price = r["signal_close"] + target
                triggered_bars = np.where(r["fwd_highs"] >= target_price)[0]
            if len(triggered_bars) > 0:
                realized = X  # limit fill at target
                trig_count += 1
            else:
                if direction == "short":
                    realized = (r["signal_close"] - r["forced_close"]) / r["adr"]
                else:
                    realized = (r["forced_close"] - r["signal_close"]) / r["adr"]
            cap_eff = realized / r["mfe_adr"] if r["mfe_adr"] > 0 else 0.0
            captures.append(cap_eff)

        arr = np.array(captures)
        results.append({
            "X": X,
            "trigger_rate": round(trig_count / n, 3) if n else 0.0,
            "mean_capture": round(float(arr.mean()), 4),
            "median_capture": round(float(np.median(arr)), 4),
            "p25": round(float(np.percentile(arr, 25)), 4),
            "p75": round(float(np.percentile(arr, 75)), 4),
        })

    # Best X by mean
    results.sort(key=lambda r: -r["mean_capture"])
    print(f"\n  Top 5 by mean capture:")
    for r in results[:5]:
        print(
            f"    X={r['X']:>4.1f} ADR  trig_rate={r['trigger_rate']:.2f}  "
            f"mean={r['mean_capture']:.3f}  median={r['median_capture']:.3f}  "
            f"p25={r['p25']:.3f}  p75={r['p75']:.3f}"
        )

    out_path = os.path.join(OUT_DIR, f"take_profit_grinder_{setup}.json")
    with open(out_path, "w") as f:
        json.dump({"setup": setup, "n": n, "results": results}, f, indent=2)
    print(f"  wrote {out_path}")
    return results


def main():
    print(f"Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    earnings_map = load_earnings_map()
    print(f"Loaded earnings for {len(earnings_map)} tickers\n")

    summary = []
    for setup in SETUPS:
        res = run_setup(setup, ohlcv, earnings_map)
        top = res[0]
        summary.append((setup, top))

    print("\n\n=== SUMMARY (best X per setup) ===")
    print(f"{'SETUP':<6}  {'best X':>7}  {'trig_rate':>10}  {'mean_cap':>9}  {'median':>8}")
    for setup, top in summary:
        print(
            f"{setup.upper():<6}  {top['X']:>7.1f}  {top['trigger_rate']:>10.2f}  "
            f"{top['mean_capture']:>9.3f}  {top['median_capture']:>8.3f}"
        )


if __name__ == "__main__":
    main()
