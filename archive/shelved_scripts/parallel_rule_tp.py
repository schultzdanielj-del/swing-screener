"""
Parallel Rule-OR-TP Exit
========================

For each setup, exit whichever fires first:
  (A) Rule condition becomes true at a bar's close -> exit at that close
  (B) Intrabar high/low crosses signal_close +/- X*adr -> exit at target (limit fill)
  (C) Neither fires by earnings cap -> forced exit at close[cap_bar]

Because (B) is checked bar-by-bar in its intrabar window, and (A) is checked
at bar's close, a given bar can fire either (or both). If both, (B) wins since
intrabar precedes close. The rule is not "armed by" condition — they're racing.

Sweep X. Pick the X that maximizes mean capture. Report vs rule-close baseline.
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
X_GRID = [3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30]
EARNINGS_BUFFER = 1

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


def run_setup(setup, ohlcv, earnings_map, expr_cache):
    direction = get_direction(setup)
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}_earnings.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]
    picks_info = SEED42_PICKS[setup]
    col_idx_list = [expr_cache.expr_index(p["expression"]) for p in picks_info]
    dirs = [p["direction"] for p in picks_info]
    ths = [p["threshold"] for p in picks_info]

    # Resolve examples
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
        if direction == "short":
            mfe_adr = (signal_close - float(np.min(fwd_lows))) / adr
        else:
            mfe_adr = (float(np.max(fwd_highs)) - signal_close) / adr
        if mfe_adr <= 0:
            continue

        # Per-bar rule-fire mask
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

        rule_fire_bar = None  # 1-indexed from signal (first post-signal bar = 1)
        for bar in range(1, cap + 1):
            for ci, d_op, th in zip(col_idx_list, dirs, ths):
                v = float(expr_data[expr_scan_idx + bar, ci])
                if np.isnan(v):
                    continue
                if (d_op == "<=" and v <= th) or (d_op == ">=" and v >= th):
                    rule_fire_bar = bar
                    break
            if rule_fire_bar is not None:
                break

        per_ex.append({
            "ticker": ticker,
            "signal_close": signal_close,
            "adr": adr,
            "cap": cap,
            "mfe_adr": mfe_adr,
            "rule_fire_bar": rule_fire_bar,
            "fwd_highs": fwd_highs,
            "fwd_lows": fwd_lows,
            "fwd_closes": fwd_closes,
        })

    n = len(per_ex)

    # Baseline: rule-close only
    rc_caps = []
    for r in per_ex:
        if r["rule_fire_bar"] is None:
            exit_c = float(r["fwd_closes"][-1])
        else:
            exit_c = float(r["fwd_closes"][r["rule_fire_bar"] - 1])
        if direction == "short":
            realized = (r["signal_close"] - exit_c) / r["adr"]
        else:
            realized = (exit_c - r["signal_close"]) / r["adr"]
        rc_caps.append(realized / r["mfe_adr"])

    print(
        f"\n[{setup.upper()}] n={n}  rule-close mean={np.mean(rc_caps):.3f}  median={np.median(rc_caps):.3f}"
    )

    # Parallel rule-OR-TP
    results = []
    for X in X_GRID:
        caps = []
        tp_wins = 0
        rule_wins = 0
        forced = 0
        for r in per_ex:
            # Find earliest TP bar (intrabar)
            if direction == "short":
                target_price = r["signal_close"] - X * r["adr"]
                tp_hits = np.where(r["fwd_lows"] <= target_price)[0]
            else:
                target_price = r["signal_close"] + X * r["adr"]
                tp_hits = np.where(r["fwd_highs"] >= target_price)[0]
            tp_bar = int(tp_hits[0]) + 1 if len(tp_hits) else None

            # Race: first to fire
            rule_bar = r["rule_fire_bar"]
            if tp_bar is not None and (rule_bar is None or tp_bar <= rule_bar):
                # TP wins
                realized = X
                tp_wins += 1
            elif rule_bar is not None:
                # Rule wins, exit at close
                if direction == "short":
                    realized = (r["signal_close"] - float(r["fwd_closes"][rule_bar - 1])) / r["adr"]
                else:
                    realized = (float(r["fwd_closes"][rule_bar - 1]) - r["signal_close"]) / r["adr"]
                rule_wins += 1
            else:
                # Neither — forced
                if direction == "short":
                    realized = (r["signal_close"] - float(r["fwd_closes"][-1])) / r["adr"]
                else:
                    realized = (float(r["fwd_closes"][-1]) - r["signal_close"]) / r["adr"]
                forced += 1
            caps.append(realized / r["mfe_adr"])

        arr = np.array(caps)
        results.append({
            "X": X,
            "tp_wins": tp_wins,
            "rule_wins": rule_wins,
            "forced": forced,
            "mean": float(arr.mean()),
            "median": float(np.median(arr)),
        })

    results.sort(key=lambda r: -r["mean"])
    best = results[0]
    print(
        f"  parallel best X={best['X']} ADR  tp_wins={best['tp_wins']} rule={best['rule_wins']} "
        f"forced={best['forced']}  mean={best['mean']:.3f}  median={best['median']:.3f}  "
        f"(delta rule-close: {best['mean'] - np.mean(rc_caps):+.3f})"
    )
    for r in results[:3]:
        print(
            f"    X={r['X']:>3}  tp={r['tp_wins']:>3d}  rule={r['rule_wins']:>3d}  "
            f"forced={r['forced']:>3d}  mean={r['mean']:.3f}  median={r['median']:.3f}"
        )

    out_path = os.path.join(OUT_DIR, f"parallel_rule_tp_{setup}.json")
    with open(out_path, "w") as f:
        json.dump(
            {
                "setup": setup,
                "n": n,
                "rule_close_mean": float(np.mean(rc_caps)),
                "results": results,
            },
            f,
            indent=2,
        )


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
