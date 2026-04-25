"""V1 accuracy test: run V1 on each setup's examples, count false-negatives.

For each setup (DTSS, HTF, BF, BASE):
  Load examples from the latest pyramid_{setup}_mp_*.json (the grinder already
  resolved ticker + signal_date + entry_date for every example).
  For each example:
    signal_idx  = bar index of signal_date in expression cache
    entry_idx   = signal_idx + 1
    stop_level  = low[entry_idx]  (long) / high[entry_idx]  (short)
    race_start  = entry_idx + 1
    race_end    = min(signal_idx + 120, end_of_data)
    Load exit-condition series for this ticker (from expr cache column).
    Race: first bar in race range where
      - low < stop (long) OR high > stop (short) -> LOSS 'stop_hit'
      - exit-expr close-value crosses threshold -> WIN 'exit_fired'
      Same bar tie: stop wins (intraday fires before close-eval).
      Neither: WIN 'timeout'.
  Count examples that V1 classifies as WIN naturally (no override).

Output: per-setup pass rate + per-example detail for any misclassifications.
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3
from collections import Counter

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
sys.path.insert(0, os.path.join(MAIN_ROOT, "scripts"))
os.chdir(MAIN_ROOT)
from expr_cache_builder import ExprSeriesCache  # noqa: E402

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
EXIT_DIR = os.path.join(MAIN_ROOT, "data", "signal_exit_grind")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)


def latest_pyramid(setup):
    fs = sorted(glob.glob(os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*.json")))
    fs = [f for f in fs if "sig0_pk0" not in os.path.basename(f)]
    return fs[-1] if fs else None


def load_exit(setup):
    p = os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    tops = d.get("top_conditions")
    return tops[0] if tops else None


def load_direction(setup):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT direction FROM setups WHERE setup_type=?", (setup,)).fetchone()
    return row[0] if row else None


def align_ohlcv_to_expr(df, cd):
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        ohlcv_dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    else:
        ohlcv_dates = np.array([str(d)[:10] for d in df["date"].values])
    expr_first = str(cd[0])[:10]
    hits = np.where(ohlcv_dates == expr_first)[0]
    if len(hits) == 0:
        return None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    return df_t if len(df_t) == len(cd) else None


def classify_v1(df_t, exit_series, signal_idx, direction, exit_dir, exit_thresh, race_cap=120):
    n = len(df_t)
    if signal_idx + 1 >= n:
        return ("WIN", "no_entry_bar", None, None)
    entry = signal_idx + 1
    lows = df_t["low"].values
    highs = df_t["high"].values
    stop_level = float(lows[entry]) if direction == "long" else float(highs[entry])
    race_start = entry + 1
    race_end = min(signal_idx + race_cap, n - 1)
    if race_start > race_end:
        return ("WIN", "no_race_window", None, None)
    lo = lows[race_start:race_end + 1]
    hi = highs[race_start:race_end + 1]
    if direction == "long":
        stop_hits = np.where(lo < stop_level)[0]
    else:
        stop_hits = np.where(hi > stop_level)[0]
    first_stop = int(stop_hits[0]) if len(stop_hits) else None

    ex_slice = exit_series[race_start:race_end + 1]
    finite = ~np.isnan(ex_slice)
    if exit_dir in (">=", "above"):
        ex_hits = np.where(finite & (ex_slice >= exit_thresh))[0]
    else:
        ex_hits = np.where(finite & (ex_slice <= exit_thresh))[0]
    first_exit = int(ex_hits[0]) if len(ex_hits) else None

    stop_bar_rel = first_stop + (race_start - signal_idx) if first_stop is not None else None
    exit_bar_rel = first_exit + (race_start - signal_idx) if first_exit is not None else None

    if first_stop is not None and (first_exit is None or first_stop <= first_exit):
        return ("LOSS", "stop_hit", exit_bar_rel, stop_bar_rel)
    if first_exit is not None:
        return ("WIN", "exit_fired", exit_bar_rel, stop_bar_rel)
    return ("WIN", "timeout", None, None)


def adr14_at(df_t, i):
    s = max(0, i - 13)
    h = df_t["high"].values[s:i + 1]
    l = df_t["low"].values[s:i + 1]
    return float(np.mean(h - l)) if len(h) else float("nan")


def run_setup(setup, universe_cache, expr_cache):
    print(f"\n=== {setup.upper()} ===", flush=True)
    pyr_path = latest_pyramid(setup)
    if not pyr_path:
        print("  no pyramid file"); return None
    pyr = json.load(open(pyr_path))
    examples = pyr.get("example_signals", [])
    if not examples:
        print("  no example_signals"); return None
    exit_cond = load_exit(setup)
    if not exit_cond:
        print("  no exit"); return None
    direction = load_direction(setup)
    if not direction:
        print("  no direction"); return None

    exit_col = expr_cache.expr_index(exit_cond["expression"])
    if exit_col is None:
        print(f"  exit expr '{exit_cond['expression']}' not in expr cache"); return None

    print(f"  pyramid file: {os.path.basename(pyr_path)}")
    print(f"  n_examples={len(examples)}, direction={direction}")
    print(f"  exit: {exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}")

    results = []
    for ex in examples:
        tk = ex["ticker"]
        signal_date = ex["date"]
        entry_date = ex["entry_date"]
        df = universe_cache.get(tk)
        if df is None:
            results.append({**ex, "cls": "ERROR", "reason": "no_ohlcv"})
            continue
        cd, cdata = expr_cache.get_ticker(tk)
        if cd is None:
            results.append({**ex, "cls": "ERROR", "reason": "no_expr_cache"})
            continue
        df_t = align_ohlcv_to_expr(df, cd)
        if df_t is None:
            results.append({**ex, "cls": "ERROR", "reason": "alignment_fail"})
            continue
        # Resolve signal_date to cache index
        expr_dates = [str(d)[:10] for d in cd]
        try:
            signal_idx = expr_dates.index(signal_date)
        except ValueError:
            results.append({**ex, "cls": "ERROR", "reason": "signal_date_not_in_cache"})
            continue
        exit_series = cdata[:, exit_col]
        cls, reason, ex_bar, stop_bar = classify_v1(
            df_t, exit_series, signal_idx, direction,
            exit_cond["direction"], float(exit_cond["threshold"]),
        )
        # Compute stop distance in ADR for diagnostic
        entry_idx = signal_idx + 1
        adr = adr14_at(df_t, signal_idx)
        if entry_idx < len(df_t) and np.isfinite(adr) and adr > 0:
            sc = float(df_t["close"].values[signal_idx])
            if direction == "long":
                stop = float(df_t["low"].values[entry_idx])
                stop_dist_adr = round((sc - stop) / adr, 2)
            else:
                stop = float(df_t["high"].values[entry_idx])
                stop_dist_adr = round((stop - sc) / adr, 2)
        else:
            stop_dist_adr = None
        results.append({
            "ticker": tk,
            "signal_date": signal_date,
            "entry_date": entry_date,
            "signal_idx": signal_idx,
            "cls": cls,
            "reason": reason,
            "exit_bar_rel": ex_bar,
            "stop_bar_rel": stop_bar,
            "stop_dist_adr": stop_dist_adr,
        })

    wins = [r for r in results if r.get("cls") == "WIN"]
    losses = [r for r in results if r.get("cls") == "LOSS"]
    errors = [r for r in results if r.get("cls") == "ERROR"]

    print(f"  V1 classification on examples:")
    print(f"    WIN (correct): {len(wins)}/{len(examples)}")
    print(f"    LOSS (false-negative -- would need override): {len(losses)}")
    print(f"    ERROR (data problem): {len(errors)}")
    print(f"    Reason breakdown: {dict(Counter(r['reason'] for r in results))}")
    if losses:
        print(f"\n  False-negative examples:")
        for r in losses:
            print(f"    {r['ticker']:<7s} signal={r['signal_date']} entry={r['entry_date']} "
                  f"stop_bar_rel={r['stop_bar_rel']} stop_dist_adr={r['stop_dist_adr']} "
                  f"exit_bar_rel={r['exit_bar_rel']}")

    return {
        "setup": setup,
        "direction": direction,
        "n_examples": len(examples),
        "n_v1_win": len(wins),
        "n_v1_loss_false_negative": len(losses),
        "n_error": len(errors),
        "reasons": dict(Counter(r["reason"] for r in results)),
        "false_negatives": losses,
    }


def main():
    print("Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        u = pickle.load(f)
    print(f"  {len(u):,} tickers", flush=True)
    print("Loading expr cache index...", flush=True)
    c = ExprSeriesCache()
    print(f"  {c.n_expressions} exprs", flush=True)

    all_rows = []
    for s in ["dtss", "htf", "bf", "base"]:
        r = run_setup(s, u, c)
        if r:
            all_rows.append(r)

    print("\n===== V1 example-accuracy summary =====")
    print(f"{'setup':6s} {'dir':5s} {'n_ex':>5s} {'v1_win':>7s} {'v1_loss':>8s} {'pass_pct':>9s}")
    for r in all_rows:
        pct = round(100 * r["n_v1_win"] / max(r["n_examples"], 1), 1)
        print(f"{r['setup']:6s} {r['direction']:5s} "
              f"{r['n_examples']:5d} {r['n_v1_win']:7d} "
              f"{r['n_v1_loss_false_negative']:8d} {pct:>8.1f}%")

    with open(os.path.join(OUT_DIR, "v1_example_accuracy.json"), "w") as f:
        json.dump(all_rows, f, indent=2, default=str)
    print(f"\nWrote {os.path.join(OUT_DIR, 'v1_example_accuracy.json')}")


if __name__ == "__main__":
    main()
