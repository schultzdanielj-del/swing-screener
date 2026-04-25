"""
Per-Ticker Ext-Rank Exit (prototype)
====================================

Hypothesis: since max-forward ext reaches the ticker's own 90–100th percentile
on 90–100% of long-setup examples, a per-ticker-calibrated exit — "exit when
ext_avgc50_adr14 crosses this ticker's Nth percentile (from pre-signal history)"
— should capture reversals mechanistically with minimal overfit risk.

Method:
  - Reference distribution: ticker's last 504 ext_avgc50_adr14 values BEFORE signal bar.
  - For each forward bar: compute what percentile of the pre-signal distribution
    the current ext value lies at.
  - Exit when rank crosses threshold T (for longs) or falls below T (for shorts).
  - Earnings cap fallback if no trigger.

T is the only parameter. Sweep T ∈ {0.70, 0.75, 0.80, 0.85, 0.90, 0.95}.

Score vs existing earnings-cap picks on same resolved example sets.
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
CACHE_DIR = os.environ.get(
    "SCANPERFECT_CACHE_DIR", os.path.join(REPO_ROOT, "local_runner", "cache")
)
OHLCV_PATH = os.path.join(READ_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
DB_PATH = os.path.join(READ_ROOT, "data", "scanperfect.db")
GRIND_DIR = os.path.join(REPO_ROOT, "data", "signal_exit_grind")
OUT_DIR = os.path.join(REPO_ROOT, "data", "diagnostics")
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache

SETUPS = ["htf", "bf", "base", "dtss"]
EXT_COL = "ext_avgc50_adr14"
LOOKBACK = 504
EARNINGS_BUFFER = 1
T_GRID = [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


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


def run_setup(setup, ohlcv, earnings_map, expr_cache):
    direction = get_direction(setup)
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}_earnings.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]
    ci = expr_cache.expr_index(EXT_COL)
    if ci is None:
        print(f"[{setup}] missing {EXT_COL}")
        return

    # Resolve per example
    per_ex = []
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

        fwd_closes = c[scan_idx + 1 : scan_idx + cap + 1]

        if direction == "short":
            mfe_adr = (signal_close - float(np.min(l[scan_idx + 1 : scan_idx + cap + 1]))) / adr
        else:
            mfe_adr = (float(np.max(h[scan_idx + 1 : scan_idx + cap + 1])) - signal_close) / adr
        if mfe_adr <= 0:
            continue

        try:
            expr_dates, expr_data = expr_cache.get_ticker(ticker)
        except Exception:
            continue
        if expr_dates is None:
            continue
        expr_dates_str = [str(d)[:10] for d in expr_dates]
        if signal_date not in expr_dates_str:
            continue
        expr_scan_idx = expr_dates_str.index(signal_date)

        # Pre-signal reference distribution
        start_ref = max(0, expr_scan_idx - LOOKBACK)
        ref_vals = expr_data[start_ref : expr_scan_idx, ci].astype(np.float32)
        ref_vals = ref_vals[~np.isnan(ref_vals)]
        if len(ref_vals) < 100:
            continue
        ref_sorted = np.sort(ref_vals)

        # Forward ext values
        fwd_ext = expr_data[expr_scan_idx + 1 : expr_scan_idx + cap + 1, ci].astype(np.float32)

        per_ex.append({
            "ticker": ticker,
            "signal_close": signal_close,
            "adr": adr,
            "cap": cap,
            "mfe_adr": mfe_adr,
            "ref_sorted": ref_sorted,
            "fwd_ext": fwd_ext,
            "fwd_closes": fwd_closes,
        })

    n = len(per_ex)
    print(f"\n[{setup.upper()}] n={n}  direction={direction}")

    results = []
    for T in T_GRID:
        caps = []
        triggered = 0
        for r in per_ex:
            ref = r["ref_sorted"]
            fwd = r["fwd_ext"]
            # Find first bar where pctrank crosses T (for longs) or falls below T (for shorts)
            # pctrank = fraction of ref <= fwd value
            fire_bar = None
            for b in range(len(fwd)):
                v = fwd[b]
                if np.isnan(v):
                    continue
                rank = float(np.searchsorted(ref, v, side="right")) / len(ref)
                if direction == "short":
                    # short exits when ext falls to low percentile (retracement)
                    if rank <= T:
                        fire_bar = b + 1
                        break
                else:
                    if rank >= T:
                        fire_bar = b + 1
                        break
            if fire_bar is None:
                # forced
                if direction == "short":
                    realized = (r["signal_close"] - float(r["fwd_closes"][-1])) / r["adr"]
                else:
                    realized = (float(r["fwd_closes"][-1]) - r["signal_close"]) / r["adr"]
            else:
                if direction == "short":
                    realized = (r["signal_close"] - float(r["fwd_closes"][fire_bar - 1])) / r["adr"]
                else:
                    realized = (float(r["fwd_closes"][fire_bar - 1]) - r["signal_close"]) / r["adr"]
                triggered += 1
            caps.append(realized / r["mfe_adr"])
        arr = np.array(caps)
        results.append({
            "T": T,
            "trigger_rate": round(triggered / n, 3),
            "mean": float(arr.mean()),
            "median": float(np.median(arr)),
        })

    results.sort(key=lambda r: -r["mean"])
    print("  Top 3 by mean capture:")
    for r in results[:3]:
        print(
            f"    T={r['T']:.2f}  trig={r['trigger_rate']:.2f}  "
            f"mean={r['mean']:.3f}  median={r['median']:.3f}"
        )

    out = {"setup": setup, "n": n, "direction": direction, "results": results}
    with open(os.path.join(OUT_DIR, f"ext_rank_exit_{setup}.json"), "w") as f:
        json.dump(out, f, indent=2)


def main():
    print("Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    earnings_map = load_earnings_map()
    expr_cache = ExprSeriesCache()
    for s in SETUPS:
        run_setup(s, ohlcv, earnings_map, expr_cache)


if __name__ == "__main__":
    main()
