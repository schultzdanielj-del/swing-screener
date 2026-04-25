"""Does the candidate entry-trigger rule hold on non-example signals too?

For each breakout rule and each fade rule, compute:
  - Hit rate at signal+1 on labeled examples (already tested).
  - Hit rate at signal+1 on all non-example signals in the pool.

If example hit rate ~= non-example hit rate, the rule is grinder construction — it holds
everywhere, no trigger information. If example >> non-example, winners were selected for
the property and the rule is non-trivial.
"""

from __future__ import annotations

import os, json, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")

LATEST_PYRAMID = {
    "htf":   "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":    "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":  "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":  "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db": "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}
BREAKOUT = {"htf", "bf", "base"}
FADE = {"dtss", "3-4db"}


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


BR_RULES = [
    ("close > sig_close",  lambda df, i, s: df["close"].values[i] > df["close"].values[s]),
    ("high  > sig_high",   lambda df, i, s: df["high"].values[i]  > df["high"].values[s]),
    ("close > sig_high",   lambda df, i, s: df["close"].values[i] > df["high"].values[s]),
]
FD_RULES = [
    ("close < sig_close",  lambda df, i, s: df["close"].values[i] < df["close"].values[s]),
    ("close < sig_high",   lambda df, i, s: df["close"].values[i] < df["high"].values[s]),
    ("low   < sig_close",  lambda df, i, s: df["low"].values[i]   < df["close"].values[s]),
    ("low   < sig_low",    lambda df, i, s: df["low"].values[i]   < df["low"].values[s]),
    ("bear_bar close<open",lambda df, i, s: df["close"].values[i] < df["open"].values[i]),
]


def main():
    with sqlite3.connect(DB) as c:
        examples = [
            {"setup": s, "ticker": t, "entry_date": d}
            for s, t, d in c.execute(
                "SELECT setup_type, ticker, entry_date FROM examples "
                "WHERE setup_type IN ('htf','bf','base','dtss','3-4db')"
            ).fetchall()
        ]
    ex_set = {(e["setup"], e["ticker"], e["entry_date"]) for e in examples}

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    # For each setup, get signals: list of (ticker, date). Subtract example signal bars
    # (ticker, date = entry_date - 1 trading bar). We subtract by matching: signal bar
    # for an example is the bar where OHLCV date = entry_date's predecessor. Easier: just
    # check for each signal whether it corresponds to an example's signal bar by date math.
    results = {}
    for setup, fn in LATEST_PYRAMID.items():
        with open(os.path.join(CACHE_DIR, fn)) as f:
            pyr = json.load(f)
        tiers = pyr["tier_results"]
        last = tiers[list(tiers.keys())[-1]]
        signals = last["final_signals"]
        rules = BR_RULES if setup in BREAKOUT else FD_RULES

        # Build example signal-bar set per ticker: for an example (setup, ticker, entry_date),
        # signal date = bar just before entry_date in OHLCV
        ex_signal_dates = defaultdict(set)  # {ticker: {signal_date_str}}
        for e in examples:
            if e["setup"] != setup: continue
            df = universe.get(e["ticker"])
            if df is None: continue
            dates = ohlcv_dates_str(df)
            if e["entry_date"] not in set(dates): continue
            eidx = int(np.where(dates == e["entry_date"])[0][0])
            if eidx - 1 < 0: continue
            ex_signal_dates[e["ticker"]].add(dates[eidx - 1])

        # Counts per rule
        per_rule_counts = {name: {"ex_hit": 0, "ex_total": 0,
                                  "non_hit": 0, "non_total": 0} for name, _ in rules}
        unresolvable = 0
        for s in signals:
            ticker, date = s["ticker"], s["date"]
            df = universe.get(ticker)
            if df is None: unresolvable += 1; continue
            dates = ohlcv_dates_str(df)
            if date not in set(dates): unresolvable += 1; continue
            sidx = int(np.where(dates == date)[0][0])
            if sidx + 1 >= len(df): continue
            is_example = (date in ex_signal_dates.get(ticker, set()))
            for name, rule_fn in rules:
                hit = bool(rule_fn(df, sidx + 1, sidx))
                bucket = "ex" if is_example else "non"
                per_rule_counts[name][f"{bucket}_total"] += 1
                if hit:
                    per_rule_counts[name][f"{bucket}_hit"] += 1

        results[setup] = per_rule_counts

    # Report
    print("Rule hit-rate at signal+1 — examples vs non-examples (the wild)")
    print("=" * 84)
    for setup, rc in results.items():
        klass = "breakout" if setup in BREAKOUT else "fade"
        print(f"\n  {setup.upper():<6} ({klass})")
        print(f"  {'rule':<24}  {'ex hits':>10}  {'ex %':>6}  {'non hits':>10}  {'non %':>6}")
        for name, d in rc.items():
            ex_pct = 100 * d["ex_hit"] / d["ex_total"] if d["ex_total"] else 0
            non_pct = 100 * d["non_hit"] / d["non_total"] if d["non_total"] else 0
            print(f"  {name:<24}  {d['ex_hit']:>4}/{d['ex_total']:<5}  {ex_pct:>5.1f}%  "
                  f"{d['non_hit']:>5}/{d['non_total']:<5}  {non_pct:>5.1f}%")


if __name__ == "__main__":
    main()
