"""
Gated Take-Profit Grinder
=========================

For each setup, use the seed-42 earnings-cap pick 1 (and pick 2 if present) as
a structural GATE. Once the gate condition first becomes true at bar B,
consider a standing limit order at signal_close +/- X*adr active from bar B+1
onward. Exit at first bar B' >= B+1 where:
  - longs:  high[B'] >= signal_close + X*adr  -> fill at that target
  - shorts: low[B']  <= signal_close - X*adr  -> fill at that target
If no fill before earnings cap, forced exit at close[cap_bar].

For comparison: also report rule-close execution (current grinder behavior) and
rule-fire-bar-peak execution (unrealistic upper bound) on the same picks.

Sweep X ∈ {3..30 ADR}.

No new expressions. No new fit parameters beyond X. Uses only the pick 1
selected by the earnings-cap grinder at the default (seed-42) split.

Outputs:
  data/diagnostics/gated_tp_{setup}.json
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
X_GRID = [3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40]
EARNINGS_BUFFER = 1

# seed-42 canonical picks from the earnings-cap grinder
SEED42_PICKS = {
    "htf":  [
        {"expression": "w_adx_slope_20_off1", "direction": "<=", "threshold": 1.708008},
        {"expression": "macd_hist_slope_6_19_9_off3", "direction": ">=", "threshold": 1.857031},
    ],
    "bf":   [
        {"expression": "w_nr_h_maxh35_atr14", "direction": ">=", "threshold": 0.307177},
    ],
    "base": [
        {"expression": "w_es_ext50_rsi_slope_7_off3", "direction": "<=", "threshold": -39.314967},
    ],
    "dtss": [
        {"expression": "w_ext_slope_xavgc50_off2", "direction": ">=", "threshold": 0.363281},
    ],
}


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


def condition_first_true_bar(expr_vals, direction, threshold, cap_bar):
    """Find first bar in [1, cap_bar] where the condition becomes true.

    expr_vals: 1D float array indexed from bar 0 (signal) forward.
    Returns 1-indexed bar or None.
    """
    for bar in range(1, min(len(expr_vals), cap_bar + 1)):
        v = expr_vals[bar]
        if np.isnan(v):
            continue
        if direction == "<=" and v <= threshold:
            return bar
        if direction == ">=" and v >= threshold:
            return bar
    return None


def any_condition_armed(expr_vals_list, dirs, thresholds, cap_bar):
    """First bar where ANY of the given conditions becomes true. Returns None if none."""
    earliest = None
    for evs, d, t in zip(expr_vals_list, dirs, thresholds):
        b = condition_first_true_bar(evs, d, t, cap_bar)
        if b is not None:
            if earliest is None or b < earliest:
                earliest = b
    return earliest


def run_setup(setup, ohlcv, earnings_map, expr_cache):
    direction = get_direction(setup)
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}_earnings.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]
    picks_info = SEED42_PICKS[setup]

    # Pre-resolve expression column indices
    col_idx_list = [expr_cache.expr_index(p["expression"]) for p in picks_info]
    if any(ci is None for ci in col_idx_list):
        missing = [p["expression"] for p, ci in zip(picks_info, col_idx_list) if ci is None]
        print(f"[{setup}] missing expressions in cache: {missing}")
        return None

    dirs = [p["direction"] for p in picks_info]
    thresholds = [p["threshold"] for p in picks_info]

    # Per-example data
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

        fwd_highs = h[scan_idx + 1 : scan_idx + cap + 1]
        fwd_lows = l[scan_idx + 1 : scan_idx + cap + 1]
        fwd_closes = c[scan_idx + 1 : scan_idx + cap + 1]

        # MFE earnings-bounded
        if direction == "short":
            mfe_adr = (signal_close - float(np.min(fwd_lows))) / adr
        else:
            mfe_adr = (float(np.max(fwd_highs)) - signal_close) / adr
        if mfe_adr <= 0:
            continue

        # Expression series aligned to expression-cache, then mapped to 0-indexed
        # from signal bar.
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

        expr_vals_list = []
        for ci in col_idx_list:
            series = expr_data[expr_scan_idx : expr_scan_idx + cap + 1, ci]
            expr_vals_list.append(series.astype(np.float32))

        # First bar where ANY of the picks fires (bar 0 = signal; skip it)
        armed_bar = any_condition_armed(expr_vals_list, dirs, thresholds, cap)

        per_ex.append({
            "ticker": ticker,
            "signal_date": signal_date,
            "signal_close": signal_close,
            "adr": adr,
            "cap": cap,
            "mfe_adr": mfe_adr,
            "armed_bar": armed_bar,
            "fwd_highs": fwd_highs,
            "fwd_lows": fwd_lows,
            "fwd_closes": fwd_closes,
        })

    n = len(per_ex)
    print(f"\n[{setup.upper()}] n={n}  direction={direction}  gate picks: "
          + " OR ".join([f"{p['expression']} {p['direction']} {p['threshold']}" for p in picks_info]))

    # Baseline: rule-close execution (current grinder behavior)
    close_caps = []
    for r in per_ex:
        if r["armed_bar"] is None:
            # Forced exit at cap
            if direction == "short":
                realized = (r["signal_close"] - float(r["fwd_closes"][-1])) / r["adr"]
            else:
                realized = (float(r["fwd_closes"][-1]) - r["signal_close"]) / r["adr"]
        else:
            # Rule fires at armed_bar; current exit = close at armed_bar
            bar_idx = r["armed_bar"] - 1  # fwd arrays are 0-indexed from bar 1
            if direction == "short":
                realized = (r["signal_close"] - float(r["fwd_closes"][bar_idx])) / r["adr"]
            else:
                realized = (float(r["fwd_closes"][bar_idx]) - r["signal_close"]) / r["adr"]
        close_caps.append(realized / r["mfe_adr"])
    print(f"  rule-close execution: mean={np.mean(close_caps):.3f}  median={np.median(close_caps):.3f}")

    # Gated TP: armed after gate fires, limit order at target, fills intrabar
    tp_results = []
    for X in X_GRID:
        caps = []
        gated_triggered = 0
        for r in per_ex:
            armed = r["armed_bar"]
            # If gate never fires, forced exit at cap
            if armed is None:
                if direction == "short":
                    realized = (r["signal_close"] - float(r["fwd_closes"][-1])) / r["adr"]
                else:
                    realized = (float(r["fwd_closes"][-1]) - r["signal_close"]) / r["adr"]
            else:
                # Search for TP fill from bar armed+1 to cap (inclusive)
                # fwd arrays are 0-indexed from bar 1; bar index = armed - 1
                if direction == "short":
                    target_price = r["signal_close"] - X * r["adr"]
                    search_lows = r["fwd_lows"][armed:]  # from bar armed+1
                    hit_idx = np.where(search_lows <= target_price)[0]
                    if len(hit_idx) > 0:
                        realized = X
                        gated_triggered += 1
                    else:
                        realized = (r["signal_close"] - float(r["fwd_closes"][-1])) / r["adr"]
                else:
                    target_price = r["signal_close"] + X * r["adr"]
                    search_highs = r["fwd_highs"][armed:]
                    hit_idx = np.where(search_highs >= target_price)[0]
                    if len(hit_idx) > 0:
                        realized = X
                        gated_triggered += 1
                    else:
                        realized = (float(r["fwd_closes"][-1]) - r["signal_close"]) / r["adr"]
            caps.append(realized / r["mfe_adr"])
        arr = np.array(caps)
        tp_results.append({
            "X": X,
            "tp_trigger_rate": round(gated_triggered / n, 3),
            "mean_capture": round(float(arr.mean()), 4),
            "median_capture": round(float(np.median(arr)), 4),
        })

    tp_results.sort(key=lambda r: -r["mean_capture"])
    best = tp_results[0]
    print(f"  gated-TP best X={best['X']} adr  trig={best['tp_trigger_rate']}  "
          f"mean={best['mean_capture']:.3f}  median={best['median_capture']:.3f}")
    print(f"  vs rule-close:  {np.mean(close_caps):.3f}  (delta: "
          f"{best['mean_capture'] - np.mean(close_caps):+.3f})")

    # Print top 3 TP variants
    for r in tp_results[:3]:
        print(f"    X={r['X']:>4}  trig={r['tp_trigger_rate']:.2f}  "
              f"mean={r['mean_capture']:.3f}  median={r['median_capture']:.3f}")

    out = {
        "setup": setup,
        "n": n,
        "gate_picks": picks_info,
        "rule_close_mean": round(float(np.mean(close_caps)), 4),
        "tp_results": tp_results,
    }
    with open(os.path.join(OUT_DIR, f"gated_tp_{setup}.json"), "w") as f:
        json.dump(out, f, indent=2)


def main():
    print("Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    earnings_map = load_earnings_map()
    print(f"Loaded earnings for {len(earnings_map)} tickers")
    print("Opening expression cache...")
    expr_cache = ExprSeriesCache()
    print(f"Cache has {expr_cache.n_expressions:,} expressions\n")

    for setup in SETUPS:
        run_setup(setup, ohlcv, earnings_map, expr_cache)


if __name__ == "__main__":
    main()
