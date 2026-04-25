"""
Bimodality Check on Extension Histograms
========================================

For each example, compute the pre-signal ext_avgc50_adr14 distribution
(lookback 504 bars). Then measure:

  - Bimodality Coefficient (BC): BC > 0.555 suggests bimodal/multimodal
    (SAS/JMP definition)
  - Signal-bar ext value's percentile in that distribution
  - Max forward ext value's percentile (where reversals happen)

If reversals cluster at a high percentile of each ticker's OWN history,
per-ticker features would directly encode it. If max-ext percentile is
random (e.g., 50%), the per-ticker hypothesis weakens.

Pure diagnostic; no exit rule changes.
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
HIST_COL = "ext_avgc50_adr14"
LOOKBACK = 504
EARNINGS_BUFFER = 1


def bimodality_coefficient(x):
    """SAS/JMP Bimodality Coefficient:
       BC = (g^2 + 1) / (k + 3*((n-1)^2 / ((n-2)*(n-3))))
       where g = skewness, k = excess kurtosis. BC > 0.555 suggests bimodal."""
    x = np.asarray(x, dtype=np.float64)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 4:
        return np.nan
    mu = x.mean()
    s = x.std()
    if s <= 0:
        return np.nan
    z = (x - mu) / s
    g = float(np.mean(z ** 3))
    k = float(np.mean(z ** 4) - 3.0)
    denom = k + 3.0 * ((n - 1) ** 2) / ((n - 2) * (n - 3))
    if denom <= 0:
        return np.nan
    return (g ** 2 + 1.0) / denom


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
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]
    ci = expr_cache.expr_index(HIST_COL)
    if ci is None:
        print(f"[{setup}] missing column {HIST_COL}")
        return []

    rows = []
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
        start_ctx = max(0, expr_scan_idx - LOOKBACK)
        pre_vals = expr_data[start_ctx : expr_scan_idx + 1, ci].astype(np.float32)
        pre_vals = pre_vals[~np.isnan(pre_vals)]
        if len(pre_vals) < 100:
            continue

        bc = bimodality_coefficient(pre_vals)
        signal_val = float(expr_data[expr_scan_idx, ci])
        if np.isnan(signal_val):
            continue

        # Where is signal value in ticker's own history?
        rank = float((pre_vals <= signal_val).sum()) / len(pre_vals)

        # Forward-max ext value percentile (where does reversal peak land?)
        e_bars = bars_to_next_earnings(df, scan_idx, earnings_map.get(ticker, []))
        if e_bars is None:
            forward_end = min(expr_scan_idx + 120, len(expr_data))
        else:
            n_avail = len(expr_data) - expr_scan_idx - 1
            cap = min(max(e_bars - EARNINGS_BUFFER, 1), n_avail, 120)
            forward_end = expr_scan_idx + cap + 1
        fwd_vals = expr_data[expr_scan_idx + 1 : forward_end, ci].astype(np.float32)
        fwd_vals = fwd_vals[~np.isnan(fwd_vals)]
        if len(fwd_vals) == 0:
            continue
        max_fwd_ext = float(np.max(fwd_vals))
        max_fwd_rank = float((pre_vals <= max_fwd_ext).sum()) / len(pre_vals)

        rows.append({
            "ticker": ticker,
            "signal_date": signal_date,
            "bc": bc,
            "signal_ext_rank": rank,
            "max_fwd_ext_rank": max_fwd_rank,
            "n_pre_bars": len(pre_vals),
        })
    return rows


def main():
    print("Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    earnings_map = load_earnings_map()
    expr_cache = ExprSeriesCache()

    print(f"\n{'SETUP':<6}  {'N':>3}  "
          f"{'bc_med':>8}  {'bc_bimod%':>10}  "
          f"{'sig_rank_med':>14}  {'maxfwd_rank_med':>16}  "
          f"{'maxfwd>=90%':>12}  {'maxfwd>=80%':>12}")
    for setup in SETUPS:
        rows = run_setup(setup, ohlcv, earnings_map, expr_cache)
        if not rows:
            continue
        bcs = np.array([r["bc"] for r in rows if not np.isnan(r["bc"])])
        sig_ranks = np.array([r["signal_ext_rank"] for r in rows])
        fwd_ranks = np.array([r["max_fwd_ext_rank"] for r in rows])
        bimod_pct = 100 * (bcs > 0.555).mean()
        p90_plus = 100 * (fwd_ranks >= 0.90).mean()
        p80_plus = 100 * (fwd_ranks >= 0.80).mean()
        print(
            f"{setup.upper():<6}  {len(rows):>3d}  "
            f"{np.median(bcs):>8.3f}  {bimod_pct:>9.0f}%  "
            f"{np.median(sig_ranks):>14.3f}  {np.median(fwd_ranks):>16.3f}  "
            f"{p90_plus:>11.0f}%  {p80_plus:>11.0f}%"
        )

        # Save CSV
        import csv
        with open(os.path.join(OUT_DIR, f"bimodality_{setup}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
