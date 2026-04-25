"""Derive the entry-inference mechanic for breakouts and fades from examples.

Hypothesis (breakouts): entry candle = first forward bar where high > signal-bar high.
Hypothesis (fades):     entry candle = first forward bar where low  < signal-bar low.

For each example, find the first forward bar matching the hypothesis and compare to
the recorded entry_idx = signal_idx + 1. Also check weaker variants if strict
fails — e.g. >= vs >, close vs high/low, within-window caps.
"""

from __future__ import annotations

import os, sys, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")

BREAKOUT = {"htf", "bf", "base"}
FADE = {"dtss", "3-4db"}


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def first_matching_bar(df, signal_idx, rule, max_forward=120):
    """Return offset from signal_idx (>=1) of the first forward bar where `rule(df, idx, signal_idx)` is True,
    or None if no such bar within max_forward bars or end of data."""
    end = min(signal_idx + max_forward, len(df) - 1)
    for i in range(signal_idx + 1, end + 1):
        if rule(df, i, signal_idx):
            return i - signal_idx
    return None


def rule_high_gt_sig_high(df, i, sidx):
    return df["high"].values[i] > df["high"].values[sidx]


def rule_high_gte_sig_high(df, i, sidx):
    return df["high"].values[i] >= df["high"].values[sidx]


def rule_close_gt_sig_high(df, i, sidx):
    return df["close"].values[i] > df["high"].values[sidx]


def rule_close_gt_sig_close(df, i, sidx):
    return df["close"].values[i] > df["close"].values[sidx]


def rule_low_lt_sig_low(df, i, sidx):
    return df["low"].values[i] < df["low"].values[sidx]


def rule_low_lte_sig_low(df, i, sidx):
    return df["low"].values[i] <= df["low"].values[sidx]


def rule_close_lt_sig_low(df, i, sidx):
    return df["close"].values[i] < df["low"].values[sidx]


def rule_close_lt_sig_close(df, i, sidx):
    return df["close"].values[i] < df["close"].values[sidx]


BREAKOUT_RULES = [
    ("high > sig_high",      rule_high_gt_sig_high),
    ("high >= sig_high",     rule_high_gte_sig_high),
    ("close > sig_high",     rule_close_gt_sig_high),
    ("close > sig_close",    rule_close_gt_sig_close),
]
def rule_close_lt_sig_high(df, i, sidx):
    return df["close"].values[i] < df["high"].values[sidx]


def rule_high_lt_sig_high(df, i, sidx):
    return df["high"].values[i] < df["high"].values[sidx]


def rule_high_lte_sig_high(df, i, sidx):
    return df["high"].values[i] <= df["high"].values[sidx]


def rule_close_lt_sig_open(df, i, sidx):
    return df["close"].values[i] < df["open"].values[sidx]


def rule_low_lt_sig_close(df, i, sidx):
    return df["low"].values[i] < df["close"].values[sidx]


def rule_high_not_exceeding_sig_high_and_close_down(df, i, sidx):
    return (df["high"].values[i] <= df["high"].values[sidx]
            and df["close"].values[i] < df["close"].values[sidx])


def rule_close_lt_sig_low_or_low_lt_sig_low(df, i, sidx):
    return (df["close"].values[i] < df["low"].values[sidx]
            or df["low"].values[i] < df["low"].values[sidx])


def rule_bear_bar(df, i, sidx):
    return df["close"].values[i] < df["open"].values[i]


def rule_lower_high_and_lower_close(df, i, sidx):
    return (df["high"].values[i] < df["high"].values[sidx]
            and df["close"].values[i] < df["close"].values[sidx])


def rule_close_lte_sig_close(df, i, sidx):
    return df["close"].values[i] <= df["close"].values[sidx]


def rule_trivial_next_bar(df, i, sidx):
    return True  # always fires, so always picks signal+1 — is DB convention i.e. entry = signal+1?


FADE_RULES = [
    ("low < sig_low",                            rule_low_lt_sig_low),
    ("low <= sig_low",                           rule_low_lte_sig_low),
    ("close < sig_low",                          rule_close_lt_sig_low),
    ("close < sig_close",                        rule_close_lt_sig_close),
    ("close <= sig_close",                       rule_close_lte_sig_close),
    ("close < sig_high",                         rule_close_lt_sig_high),
    ("high < sig_high",                          rule_high_lt_sig_high),
    ("high <= sig_high",                         rule_high_lte_sig_high),
    ("close < sig_open",                         rule_close_lt_sig_open),
    ("low < sig_close",                          rule_low_lt_sig_close),
    ("high<=sig_high AND close<sig_close",       rule_high_not_exceeding_sig_high_and_close_down),
    ("high<sig_high AND close<sig_close",        rule_lower_high_and_lower_close),
    ("close<sig_low OR low<sig_low",             rule_close_lt_sig_low_or_low_lt_sig_low),
    ("bear_bar (close < open)",                  rule_bear_bar),
    ("ALWAYS (i.e. signal+1 trivially)",         rule_trivial_next_bar),
]


def main():
    with sqlite3.connect(DB) as c:
        examples = [
            {"setup": s, "ticker": t, "entry_date": d}
            for s, t, d in c.execute(
                "SELECT setup_type, ticker, entry_date FROM examples "
                "WHERE setup_type IN ('htf','bf','base','dtss','3-4db') ORDER BY setup_type, ticker, entry_date"
            ).fetchall()
        ]
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    # Aggregate by class
    classes = {"breakout": BREAKOUT_RULES, "fade": FADE_RULES}
    examples_by_class = defaultdict(list)
    for e in examples:
        klass = "breakout" if e["setup"] in BREAKOUT else "fade"
        examples_by_class[klass].append(e)

    for klass, rules in classes.items():
        exs = examples_by_class[klass]
        print("=" * 74)
        print(f"{klass.upper()}  ({len(exs)} examples)")
        print("=" * 74)
        for rule_name, rule_fn in rules:
            matches_signal_plus_1 = 0
            first_is_later = 0
            no_match_at_all = 0
            offsets = []
            earliest_than_signalplus1 = 0  # shouldn't happen by construction, but check
            unresolvable = 0
            details_first_later = []
            for e in exs:
                df = universe.get(e["ticker"])
                if df is None:
                    unresolvable += 1; continue
                dates_str = ohlcv_dates_str(df)
                if e["entry_date"] not in set(dates_str):
                    unresolvable += 1; continue
                entry_idx = int(np.where(dates_str == e["entry_date"])[0][0])
                signal_idx = entry_idx - 1
                if signal_idx < 0:
                    unresolvable += 1; continue
                off = first_matching_bar(df, signal_idx, rule_fn)
                if off is None:
                    no_match_at_all += 1
                    continue
                offsets.append(off)
                if off == 1:
                    matches_signal_plus_1 += 1
                elif off > 1:
                    first_is_later += 1
                    details_first_later.append({
                        "setup": e["setup"], "ticker": e["ticker"], "entry_date": e["entry_date"],
                        "first_match_offset": off,
                    })
            resolved = matches_signal_plus_1 + first_is_later + no_match_at_all
            pct = 100 * matches_signal_plus_1 / max(resolved, 1)
            med = int(np.median(offsets)) if offsets else -1
            print(f"  rule: {rule_name:<22}  signal+1 match: {matches_signal_plus_1}/{resolved} ({pct:.1f}%)   "
                  f"first_later: {first_is_later}  no_match: {no_match_at_all}  median_offset: {med}")
            if details_first_later:
                print(f"    first-match-later examples (top 6):")
                for d in details_first_later[:6]:
                    print(f"      {d['setup']:<6}  {d['ticker']:<6}  {d['entry_date']}  first_match_offset={d['first_match_offset']}")
        print()


if __name__ == "__main__":
    main()
