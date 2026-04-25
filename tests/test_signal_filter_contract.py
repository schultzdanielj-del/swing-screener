"""
Contract tests for signal filter / example matching.

The signal filter has one job: for each example, identify the signal bar
as exactly entry_idx - 1, with all conditions firing on that bar. One
example, one match, no proximity.

Run:  python tests/test_signal_filter_contract.py

Reads:
- data/scanperfect.db (examples table)
- local_runner/cache/expr_series/ (expression cache)
- local_runner/cache/ (OHLCV cache, raw_signal_clusters_*.json)
- local_runner/cache/pyramid_<setup>_*.json (signal conditions)
"""

import os
import sys
import json
import sqlite3
import unittest

import numpy as np
import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from pyramid_grinder import (  # noqa: E402
    load_daily_cache,
    _load_signal_conditions,
)
from expr_cache_builder import EXPR_CACHE_START, ExprSeriesCache  # noqa: E402

SETUPS = ["brko", "dtss"]
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
CLUSTERS_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")


def _load_examples(setup_type):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker, entry_date",
        (setup_type,),
    ).fetchall()
    conn.close()
    return [{"ticker": r["ticker"], "entry_date": r["entry_date"]} for r in rows]


def _truncate_to_expr_cache(universe):
    cache_start = pd.Timestamp(EXPR_CACHE_START)
    for tk in list(universe.keys()):
        df = universe[tk]
        if df is None or len(df) < 50:
            continue
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            dates = pd.to_datetime(df["date"]).values
        else:
            dates = df["date"].values
        start = int(np.searchsorted(dates, np.datetime64(cache_start), side="left"))
        if start > 0:
            universe[tk] = df.iloc[start:].reset_index(drop=True)


def _ticker_date_index(df):
    return {str(d)[:10]: i for i, d in enumerate(df["date"].values)}


class SignalFilterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        print("\n" + "=" * 78)
        print("Loading caches (one-time setup)...")
        print("=" * 78)
        cls.expr_cache = ExprSeriesCache()
        if not cls.expr_cache.is_valid():
            raise RuntimeError("Expression cache not found or invalid")
        cls.universe = load_daily_cache()
        _truncate_to_expr_cache(cls.universe)
        print(f"Loaded {len(cls.universe)} tickers, expr cache OK\n")

    def test_1_conditions_fire_on_entry_minus_1(self):
        """For every example: all signal conditions fire on entry_idx - 1."""
        all_failures = {}
        all_pass_count = 0
        all_total = 0

        for setup in SETUPS:
            print(f"\n--- {setup.upper()}: conditions on entry-1 ---")
            conditions, src = _load_signal_conditions(setup)
            self.assertGreater(len(conditions), 0, f"No conditions loaded for {setup}")
            print(f"Loaded {len(conditions)} conditions from {src}")

            cond_cols = []
            for cond in conditions:
                col = self.expr_cache.expr_index(cond["name"])
                cond_cols.append(col)
            n_missing = sum(1 for c in cond_cols if c is None)
            if n_missing:
                print(f"WARNING: {n_missing}/{len(conditions)} conditions not in expr cache")

            examples = _load_examples(setup)
            print(f"Loaded {len(examples)} examples from DB")

            failures = []
            n_pass = 0
            for ex in examples:
                ticker = ex["ticker"]
                entry_date = ex["entry_date"]
                df = self.universe.get(ticker)
                if df is None:
                    failures.append(f"{ticker} {entry_date}: NO_OHLCV")
                    continue
                date_idx = _ticker_date_index(df)
                if entry_date not in date_idx:
                    failures.append(f"{ticker} {entry_date}: ENTRY_NOT_IN_OHLCV (date may not be a trading day)")
                    continue
                entry_idx = date_idx[entry_date]
                scan_idx = entry_idx - 1
                if scan_idx < 0:
                    failures.append(f"{ticker} {entry_date}: scan_idx<0")
                    continue

                cached_dates, cached_data = self.expr_cache.get_ticker(ticker)
                if cached_dates is None:
                    failures.append(f"{ticker} {entry_date}: NO_EXPR_CACHE")
                    continue
                if scan_idx >= len(cached_data):
                    failures.append(f"{ticker} {entry_date}: scan_idx out of expr cache range")
                    continue

                cond_failures = []
                for ci, cond in enumerate(conditions):
                    col = cond_cols[ci]
                    if col is None:
                        cond_failures.append(f"{cond['name']}=NO_COL")
                        continue
                    val = float(cached_data[scan_idx, col])
                    if np.isnan(val):
                        cond_failures.append(f"{cond['name']}=NaN")
                    elif val < cond["low"] or val > cond["high"]:
                        cond_failures.append(
                            f"{cond['name']}={val:.4f} not in [{cond['low']:.4f}, {cond['high']:.4f}]"
                        )

                if cond_failures:
                    failures.append(
                        f"{ticker} {entry_date}: {len(cond_failures)} cond fails: "
                        + "; ".join(cond_failures[:3])
                        + (f" (+{len(cond_failures)-3} more)" if len(cond_failures) > 3 else "")
                    )
                else:
                    n_pass += 1

            print(f"PASS: {n_pass}/{len(examples)}")
            if failures:
                print(f"FAIL: {len(failures)}")
                for f in failures:
                    print(f"  {f}")
            all_failures[setup] = failures
            all_pass_count += n_pass
            all_total += len(examples)

        print(f"\n--- TEST 1 SUMMARY ---")
        print(f"Total: {all_pass_count}/{all_total} examples have conditions firing on entry-1")
        for setup, fs in all_failures.items():
            print(f"  {setup.upper()}: {len(fs)} failures")

        total_failures = sum(len(fs) for fs in all_failures.values())
        self.assertEqual(
            total_failures, 0,
            f"{total_failures} examples failed condition check on entry-1. "
            "Either the entry_date in DB is wrong, or the conditions don't actually catch entry-1 (GRINDER BUG)."
        )

    def test_2_grinder_produces_strict_matching(self):
        """For every example: exactly 1 cluster matches with rightmost == entry_idx - 1."""
        all_failures = {}
        all_pass_count = 0
        all_total = 0

        for setup in SETUPS:
            print(f"\n--- {setup.upper()}: strict cluster matching ---")
            cluster_path = os.path.join(CLUSTERS_DIR, f"raw_signal_clusters_{setup}.json")
            if not os.path.exists(cluster_path):
                self.fail(f"Cluster cache missing: {cluster_path}. Run pyramid_grinder.py first.")
            with open(cluster_path) as f:
                cluster_data = json.load(f)

            ex_clusters_by_entry = {}
            for c in cluster_data["clusters"]:
                if c.get("is_example") != 1:
                    continue
                key = (c["ticker"], c.get("example_entry_date"))
                ex_clusters_by_entry.setdefault(key, []).append(c)

            examples = _load_examples(setup)
            failures = []
            n_pass = 0
            for ex in examples:
                ticker = ex["ticker"]
                entry_date = ex["entry_date"]
                df = self.universe.get(ticker)
                if df is None:
                    failures.append(f"{ticker} {entry_date}: NO_OHLCV")
                    continue
                date_idx = _ticker_date_index(df)
                if entry_date not in date_idx:
                    failures.append(f"{ticker} {entry_date}: ENTRY_NOT_IN_OHLCV")
                    continue
                expected_scan_idx = date_idx[entry_date] - 1

                matched = ex_clusters_by_entry.get((ticker, entry_date), [])
                if len(matched) == 0:
                    failures.append(f"{ticker} {entry_date}: NO_MATCH (no cluster tagged with this entry)")
                    continue
                if len(matched) > 1:
                    sig_dates = [c["rightmost"]["date"] for c in matched]
                    failures.append(
                        f"{ticker} {entry_date}: DUPLICATE ({len(matched)} clusters matched, "
                        f"sig_dates={sig_dates})"
                    )
                    continue
                cluster = matched[0]
                actual_rightmost = cluster["rightmost"]["bar_idx"]
                if actual_rightmost != expected_scan_idx:
                    gap = date_idx[entry_date] - actual_rightmost
                    failures.append(
                        f"{ticker} {entry_date}: WRONG_BAR (rightmost_idx={actual_rightmost}, "
                        f"expected={expected_scan_idx}, gap={gap})"
                    )
                    continue
                n_pass += 1

            print(f"PASS: {n_pass}/{len(examples)}")
            if failures:
                print(f"FAIL: {len(failures)}")
                for f in failures:
                    print(f"  {f}")
            all_failures[setup] = failures
            all_pass_count += n_pass
            all_total += len(examples)

        print(f"\n--- TEST 2 SUMMARY ---")
        print(f"Total: {all_pass_count}/{all_total} examples have strict gap=1 single-cluster match")
        for setup, fs in all_failures.items():
            print(f"  {setup.upper()}: {len(fs)} failures")

        total_failures = sum(len(fs) for fs in all_failures.values())
        self.assertEqual(
            total_failures, 0,
            f"{total_failures} examples failed strict-matching contract. "
            "Matching code is using proximity / wrong cluster bar / duplicate matches."
        )

    def test_3_threshold_and_aggregate_snapshot(self):
        """Print current threshold values + aggregate stats. No assertion.
        Re-run after matching fix and diff visually.
        """
        print(f"\n--- TEST 3: SNAPSHOT (no assertions) ---")
        for setup in SETUPS:
            cluster_path = os.path.join(CLUSTERS_DIR, f"raw_signal_clusters_{setup}.json")
            if not os.path.exists(cluster_path):
                print(f"  {setup}: SKIP (no cluster cache)")
                continue
            with open(cluster_path) as f:
                d = json.load(f)
            clusters = d["clusters"]
            n_total = len(clusters)
            n_ex = sum(1 for c in clusters if c.get("is_example") == 1)
            n_win = sum(1 for c in clusters if c.get("classification", "").startswith("AUTO_WIN"))
            n_loss = sum(1 for c in clusters if c.get("classification") == "AUTO_LOSS")
            wr = (n_win / max(n_total, 1)) * 100

            sizes = [c.get("size", 1) for c in clusters]
            size_dist = {}
            for s in sizes:
                key = s if s <= 4 else "5+"
                size_dist[key] = size_dist.get(key, 0) + 1

            print(f"\n  {setup.upper()}")
            print(f"    forward_window:   {d.get('forward_window')}")
            print(f"    direction:        {d.get('direction')}")
            print(f"    n_clusters:       {n_total}")
            print(f"    n_examples:       {n_ex}")
            print(f"    n_winning:        {n_win} ({wr:.1f}%)")
            print(f"    n_losing:         {n_loss}")
            print(f"    cluster sizes:    {dict(sorted(size_dist.items(), key=lambda x: str(x[0])))}")

            ex_clusters = [c for c in clusters if c.get("is_example") == 1]
            ex_with_stop = [c for c in ex_clusters if c.get("stop_level") is not None]
            ex_with_winner = [c for c in ex_clusters if c.get("winner_level") is not None]
            if ex_with_stop:
                stops = [c["stop_level"] for c in ex_with_stop]
                print(f"    example stop_level range:    [{min(stops):.2f}, {max(stops):.2f}]")
            if ex_with_winner:
                wins = [c["winner_level"] for c in ex_with_winner]
                print(f"    example winner_level range:  [{min(wins):.2f}, {max(wins):.2f}]")

            move_adrs = [c.get("move_adr") for c in clusters
                         if c.get("classification", "").startswith("AUTO_WIN")
                         and c.get("move_adr") is not None]
            if move_adrs:
                ms = sorted(move_adrs)
                print(f"    winner move_adr:  floor={ms[0]:.2f} median={ms[len(ms)//2]:.2f} "
                      f"ceiling={ms[-1]:.2f} (n={len(ms)})")

    def test_4_cluster_signal_integrity(self):
        """For every cluster, the rightmost bar must pass all signal conditions."""
        all_failures = {}

        for setup in SETUPS:
            print(f"\n--- {setup.upper()}: cluster signal integrity ---")
            conditions, _ = _load_signal_conditions(setup)
            cond_cols = np.array(
                [self.expr_cache.expr_index(c["name"]) for c in conditions], dtype=object
            )
            valid_mask = np.array([col is not None for col in cond_cols])
            valid_cols = np.array([col for col in cond_cols if col is not None], dtype=int)
            valid_lows = np.array(
                [c["low"] for c, ok in zip(conditions, valid_mask) if ok], dtype=np.float64
            )
            valid_highs = np.array(
                [c["high"] for c, ok in zip(conditions, valid_mask) if ok], dtype=np.float64
            )
            valid_names = [c["name"] for c, ok in zip(conditions, valid_mask) if ok]

            cluster_path = os.path.join(CLUSTERS_DIR, f"raw_signal_clusters_{setup}.json")
            with open(cluster_path) as f:
                cluster_data = json.load(f)

            ticker_groups = {}
            for c in cluster_data["clusters"]:
                ticker_groups.setdefault(c["ticker"], []).append(c)

            failures = []
            n_check = 0
            for ticker, ticker_clusters in ticker_groups.items():
                cached_dates, cached_data = self.expr_cache.get_ticker(ticker)
                if cached_dates is None:
                    continue
                n_bars = len(cached_data)
                for c in ticker_clusters:
                    rightmost_idx = c["rightmost"]["bar_idx"]
                    if rightmost_idx >= n_bars:
                        continue
                    n_check += 1
                    row = cached_data[rightmost_idx, valid_cols]
                    is_nan = np.isnan(row)
                    out_of_range = (row < valid_lows) | (row > valid_highs)
                    bad_mask = is_nan | out_of_range
                    if bad_mask.any():
                        bad_idxs = np.where(bad_mask)[0][:2]
                        bad = [valid_names[i] for i in bad_idxs]
                        failures.append(
                            f"{ticker} cluster_id={c['cluster_id']} "
                            f"sig_date={c['rightmost']['date']} csize={c.get('size',1)}: "
                            f"rightmost bar fails ({','.join(bad)})"
                        )

            print(f"PASS: {n_check - len(failures)}/{n_check}")
            if failures:
                print(f"FAIL: {len(failures)}")
                for f in failures[:10]:
                    print(f"  {f}")
                if len(failures) > 10:
                    print(f"  ... and {len(failures) - 10} more")
            all_failures[setup] = failures

        total_failures = sum(len(fs) for fs in all_failures.values())
        self.assertEqual(
            total_failures, 0,
            f"{total_failures} clusters have rightmost bar that doesn't pass all signal conditions. "
            "Either the scan is broken or the cluster bookkeeping is off."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
