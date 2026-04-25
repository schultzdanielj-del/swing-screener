#!/usr/bin/env python3
"""Soft-cluster the 58 BRKO examples into 3 subtypes via PCA + GaussianMixture
on entry-1 expression vectors. Writes cluster_brko_assignments.csv and prints
per-cluster stats cross-referenced against signal_exit_brko.json.

Deterministic (random_state=42). Safe to rerun.
"""
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "scanperfect.db"
EXIT_JSON_PATH = REPO_ROOT / "data" / "signal_exit_grind" / "signal_exit_brko.json"
CSV_OUT = REPO_ROOT / "local_runner" / "cache" / "cluster_brko_assignments.csv"

sys.path.insert(0, str(REPO_ROOT / "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

from sklearn.decomposition import PCA  # noqa: E402
from sklearn.mixture import GaussianMixture  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402


def load_examples(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type='brko' "
        "ORDER BY ticker, entry_date"
    ).fetchall()
    conn.close()
    return rows


def build_feature_matrix(examples, cache):
    n = len(examples)
    n_expr = len(cache.expr_names)
    matrix = np.empty((n, n_expr), dtype=np.float32)
    for i, (ticker, entry_date) in enumerate(examples):
        dates, data = cache.get_ticker(ticker)
        if dates is None:
            raise RuntimeError(f"Ticker {ticker} not in expression cache")
        idx = int(np.searchsorted(dates, entry_date))
        if idx >= len(dates) or str(dates[idx]) != entry_date:
            raise RuntimeError(
                f"Entry date {entry_date} not found in cache for {ticker}"
            )
        signal_idx = idx - 1
        if signal_idx < 0:
            raise RuntimeError(
                f"Signal bar (entry-1) before cache start for {ticker} {entry_date}"
            )
        matrix[i] = data[signal_idx, :]
    return matrix


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pca", type=int, default=8,
                        help="PCA components (default 8)")
    args = parser.parse_args()

    examples = load_examples(DB_PATH)
    print(f"Loaded {len(examples)} BRKO examples from DB")

    cache = ExprSeriesCache()
    if not cache.is_valid():
        print("ERROR: expression cache not valid")
        sys.exit(1)

    X = build_feature_matrix(examples, cache)
    print(f"Feature matrix: {X.shape}")

    col_has_nan = np.any(np.isnan(X), axis=0)
    keep = ~col_has_nan
    X_clean = X[:, keep]
    print(f"Dropped {int(col_has_nan.sum())} cols with NaN; kept {X_clean.shape[1]}")

    scaler = StandardScaler()
    X_std = scaler.fit_transform(X_clean)

    n_pca = min(args.pca, X_std.shape[0], X_std.shape[1])
    pca = PCA(n_components=n_pca, random_state=42)
    X_pca = pca.fit_transform(X_std)
    evr = pca.explained_variance_ratio_.sum()
    print(f"PCA to {n_pca} components; cumulative explained variance: {evr:.3f}")

    # covariance_type='diag' (not sklearn default 'full') — full covariance overfits
    # at 58 samples / 8 dims and produces near-deterministic assignments with no
    # boundary cases, defeating the plan's soft-cluster goal.
    gmm = GaussianMixture(n_components=3, covariance_type="diag", random_state=42)
    gmm.fit(X_pca)
    proba = gmm.predict_proba(X_pca)

    primary = np.argmax(proba, axis=1)
    secondary = [""] * len(examples)
    for i in range(len(examples)):
        p = int(primary[i])
        cands = [(j, float(proba[i, j])) for j in range(3)
                 if j != p and proba[i, j] > 0.25]
        if cands:
            cands.sort(key=lambda t: -t[1])
            secondary[i] = str(cands[0][0])

    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "entry_date", "p_0", "p_1", "p_2",
                    "primary", "secondary"])
        for i, (ticker, entry_date) in enumerate(examples):
            w.writerow([
                ticker, entry_date,
                f"{proba[i, 0]:.4f}", f"{proba[i, 1]:.4f}", f"{proba[i, 2]:.4f}",
                int(primary[i]), secondary[i],
            ])
    print(f"Wrote {CSV_OUT}")

    with open(EXIT_JSON_PATH) as f:
        exit_data = json.load(f)
    top_cond = exit_data["top_conditions"][0]
    ex_list = exit_data["examples"]
    exit_bars = top_cond["per_example_exit_bars"]
    eff = top_cond["per_example_efficiency"]
    lookup = {(e["ticker"], e["entry_date"]): (exit_bars[i], eff[i])
              for i, e in enumerate(ex_list)}

    print()
    print(f"Reference exit: {top_cond['expression']} {top_cond['direction']} "
          f"{top_cond['threshold']} (signal_exit_brko.json top condition)")
    print()
    print("Cluster | N primary | N incl secondary | Median exit_bars | Median efficiency | In-lookup")
    print("--------+-----------+------------------+------------------+-------------------+----------")
    for c in range(3):
        p_idxs = [i for i in range(len(examples)) if int(primary[i]) == c]
        member_idxs = set(p_idxs)
        for i in range(len(examples)):
            if secondary[i] != "" and int(secondary[i]) == c:
                member_idxs.add(i)
        member_idxs = sorted(member_idxs)
        bars_vals, eff_vals = [], []
        for i in member_idxs:
            key = examples[i]
            if key in lookup:
                b, e = lookup[key]
                bars_vals.append(b)
                eff_vals.append(e)
        med_bars = float(np.median(bars_vals)) if bars_vals else float("nan")
        med_eff = float(np.median(eff_vals)) if eff_vals else float("nan")
        print(f"{c:7d} | {len(p_idxs):9d} | {len(member_idxs):16d} | "
              f"{med_bars:16.1f} | {med_eff:17.3f} | {len(bars_vals):9d}")

    n_multi = sum(1 for s in secondary if s != "")
    print()
    print(f"Total: {len(examples)} examples, {n_multi} multi-assigned "
          f"(secondary p > 0.25)")


if __name__ == "__main__":
    main()
