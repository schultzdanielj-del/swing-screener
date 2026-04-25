"""Entry maturity grinder (v1).

Objective: for each expression in the 16,039-feature cache, score its ability to
discriminate MATURE (rightmost-of-presignal-run) from PREMATURE (earlier-in-run)
pyramid∩presignal bars, using only the bar's close-of-day data. No forward look.

Training population: breakouts pool (HTF + BF + BASE), no-dedupe version
(research/classifier_pool/{setup}_pool.json current).

For each cluster (ticker, sig_bar):
  - Positive (mature=1) iff sig_bar is rightmost of its contiguous presignal run
    (i.e., sig_bar+1 is NOT in presignal pre_dedup set for that ticker).
  - Negative (mature=0) otherwise.

Score per expression: AUC (rank-based). Higher AUC = better separation.

Output: research/entry_maturity_grinder/top_features.json (top 50 by AUC).
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MAIN = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN, "local_runner"))
from expr_cache_builder import _open_npz  # noqa: E402

WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE = os.path.join(MAIN, "local_runner", "cache")
EXPR_CACHE_DIR = os.path.join(CACHE, "expr_series")
POOL_DIR = os.path.join(WORKTREE, "research", "classifier_pool")
PRESIG_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")
OUT_DIR = os.path.join(WORKTREE, "research", "entry_maturity_grinder")

SETUPS = ["htf", "bf", "base"]


def load_expr_vocab():
    with open(os.path.join(EXPR_CACHE_DIR, "_manifest.json")) as f:
        m = json.load(f)
    return m["expr_names"]


def load_ticker_expr_at_bars(ticker, bar_idxs):
    """Return array of shape (len(bar_idxs), n_exprs) of expression values at those bars.
    Returns None if ticker not in cache or bar_idxs invalid."""
    safe = ticker.replace("/", "_").replace(".", "_")
    p = os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")
    if not os.path.exists(p):
        return None
    z = _open_npz(p)
    data = z["data"]
    dates = z["dates"].astype("<U10")
    n_cache_bars = data.shape[0]
    # OHLCV bar_idxs ≠ expr cache bar indices. Need to map via date.
    # Load ohlcv dates once per ticker via the pool? Simpler: use universe.
    # Actually we were given bar_idxs that index OHLCV. To map to expr cache rows,
    # we need the date per bar_idx and find that date in dates.
    return dates, data


def load_ohlcv_universe():
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        return pickle.load(f)


def compute_auc(values, labels):
    """AUC of values predicting labels (1=positive). NaN values excluded.
    Returns AUC if computable; None if insufficient data."""
    mask = np.isfinite(values)
    v = values[mask]; y = labels[mask]
    if len(v) < 10 or y.sum() < 2 or (1 - y).sum() < 2:
        return None
    # Rank-based AUC
    order = np.argsort(v, kind="mergesort")
    y_sorted = y[order]
    n_pos = int(y_sorted.sum())
    n_neg = len(y_sorted) - n_pos
    # Sum of ranks of positives (1-indexed)
    ranks = np.arange(1, len(y_sorted) + 1)
    sum_ranks_pos = int(ranks[y_sorted.astype(bool)].sum())
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(auc)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Loading expression vocab...", flush=True)
    expr_names = load_expr_vocab()
    n_exprs = len(expr_names)
    print(f"  {n_exprs:,} expressions", flush=True)

    print("Loading OHLCV universe...", flush=True)
    universe = load_ohlcv_universe()
    print(f"  {len(universe):,} tickers", flush=True)

    # Gather cluster rows across all 3 breakout pools + label as mature/premature
    rows = []  # each: {ticker, sig_bar_idx, sig_date, is_example, setup, mature}
    for setup in SETUPS:
        pool = json.load(open(os.path.join(POOL_DIR, f"{setup}_pool.json")))
        with open(os.path.join(PRESIG_DIR, f"{setup}_passes.pkl"), "rb") as f:
            presig = pickle.load(f)
        pre_sets = {t: set(v) for t, v in presig["pre_dedup_by_ticker"].items()}
        for c in pool["clusters"]:
            tk = c["ticker"]
            sig_idx = c["rightmost"]["bar_idx"]
            pre = pre_sets.get(tk, set())
            mature = (sig_idx in pre) and ((sig_idx + 1) not in pre)
            rows.append({
                "setup": setup,
                "ticker": tk,
                "sig_bar_idx": int(sig_idx),
                "sig_date": c["rightmost"]["date"],
                "is_example": int(c["is_example"]),
                "mature": int(mature),
            })
    df_rows = pd.DataFrame(rows)
    print(f"\nCluster rows (total): {len(df_rows)}", flush=True)
    print(f"  mature (rightmost-of-run): {df_rows['mature'].sum()}", flush=True)
    print(f"  premature (non-rightmost): {(1 - df_rows['mature']).sum()}", flush=True)
    print(f"  examples in pool: {df_rows['is_example'].sum()}", flush=True)
    ex_mature = df_rows[(df_rows.is_example == 1) & (df_rows.mature == 1)]
    ex_premature = df_rows[(df_rows.is_example == 1) & (df_rows.mature == 0)]
    print(f"  examples mature (in positive): {len(ex_mature)}  "
          f"premature (would be in negative if kept): {len(ex_premature)}", flush=True)
    # Training uses WILD ONLY. Examples are held out for validation so their
    # mature-or-not structural label doesn't contaminate the training signal.
    is_training = (df_rows["is_example"] == 0).values
    n_train = int(is_training.sum())
    print(f"\nTraining set (wild only): {n_train}  "
          f"mature={int(df_rows.loc[is_training, 'mature'].sum())}  "
          f"premature={n_train - int(df_rows.loc[is_training, 'mature'].sum())}", flush=True)
    print(f"Validation set (examples): {int((~is_training).sum())} — rule must fire on these", flush=True)

    # Build value matrix (n_clusters, n_exprs). Stream per ticker.
    print("\nExtracting expression values per cluster...", flush=True)
    values = np.full((len(df_rows), n_exprs), np.nan, dtype=np.float32)
    by_ticker = df_rows.groupby("ticker").groups
    n_tickers = len(by_ticker)
    n_resolved = 0; n_missing_cache = 0; n_missing_date = 0
    for i, (tk, row_idxs) in enumerate(by_ticker.items()):
        safe = tk.replace("/", "_").replace(".", "_")
        p = os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")
        if not os.path.exists(p):
            n_missing_cache += len(row_idxs); continue
        z = _open_npz(p)
        cache_dates = z["dates"].astype("<U10")
        cache_data = z["data"]
        for ridx in row_idxs:
            sig_date = df_rows.at[ridx, "sig_date"]
            pos = int(np.searchsorted(cache_dates, sig_date, side="left"))
            if pos >= len(cache_dates) or cache_dates[pos] != sig_date:
                n_missing_date += 1; continue
            values[ridx, :] = cache_data[pos, :].astype(np.float32)
            n_resolved += 1
        if (i + 1) % 100 == 0 or (i + 1) == n_tickers:
            print(f"  [{i+1}/{n_tickers}] tickers processed  resolved={n_resolved}  "
                  f"missing_cache={n_missing_cache}  missing_date={n_missing_date}", flush=True)

    labels = df_rows["mature"].values.astype(np.float32)
    values_train = values[is_training, :]
    labels_train = labels[is_training]
    values_eval = values[~is_training, :]  # examples — validation only

    print(f"\nComputing AUC for {n_exprs:,} expressions (training only)...", flush=True)
    aucs = np.full(n_exprs, np.nan)
    for e in range(n_exprs):
        a = compute_auc(values_train[:, e], labels_train)
        if a is not None:
            aucs[e] = a
        if (e + 1) % 2000 == 0:
            print(f"  [{e+1}/{n_exprs}] scored", flush=True)

    # Discrimination = distance from 0.5 (either direction). Flip sign for direction.
    dist = np.abs(aucs - 0.5)
    rank = np.argsort(-dist)  # descending by |AUC - 0.5|

    top = []
    for r_i in rank[:200]:
        if np.isnan(aucs[r_i]):
            continue
        v = values_train[:, r_i]
        mask = np.isfinite(v)
        if not mask.any():
            continue
        v_ok = v[mask]; y_ok = labels_train[mask]
        order = np.argsort(v_ok)
        v_sorted = v_ok[order]; y_sorted = y_ok[order]
        total_pos = int(y_sorted.sum())
        total_neg = len(y_sorted) - total_pos
        if total_pos == 0 or total_neg == 0:
            continue
        # Cumulative positive below threshold
        cum_pos = np.cumsum(y_sorted)
        cum_neg = np.arange(1, len(y_sorted) + 1) - cum_pos
        # At each split k (elements <= sorted[k] go to "below"), TPR_above = (total_pos - cum_pos[k]) / total_pos
        # FPR_above = (total_neg - cum_neg[k]) / total_neg
        tpr_above = (total_pos - cum_pos) / total_pos
        fpr_above = (total_neg - cum_neg) / total_neg
        j_above = tpr_above - fpr_above
        tpr_below = cum_pos / total_pos
        fpr_below = cum_neg / total_neg
        j_below = tpr_below - fpr_below
        best_idx_above = int(np.argmax(j_above))
        best_idx_below = int(np.argmax(j_below))
        if j_above[best_idx_above] >= j_below[best_idx_below]:
            thr = float(v_sorted[best_idx_above])
            direction = ">"
            j_best = float(j_above[best_idx_above])
            tpr = float(tpr_above[best_idx_above])
            fpr = float(fpr_above[best_idx_above])
        else:
            thr = float(v_sorted[best_idx_below])
            direction = "<="
            j_best = float(j_below[best_idx_below])
            tpr = float(tpr_below[best_idx_below])
            fpr = float(fpr_below[best_idx_below])
        # Validation: apply threshold to examples (held out). Count how many
        # examples' bar would be classified as MATURE (positive) by this rule.
        v_eval = values_eval[:, r_i]
        eval_mask = np.isfinite(v_eval)
        if direction == ">":
            eval_pass = (v_eval > thr) & eval_mask
        else:
            eval_pass = (v_eval <= thr) & eval_mask
        n_eval_mature = int(eval_pass.sum())
        n_eval_evaluable = int(eval_mask.sum())

        top.append({
            "expression": expr_names[r_i],
            "auc": float(aucs[r_i]),
            "abs_auc_minus_0.5": float(dist[r_i]),
            "best_threshold": thr,
            "direction": direction,
            "youden_j": j_best,
            "tpr": tpr,
            "fpr": fpr,
            "n_train_evaluable": int(mask.sum()),
            "n_train_positive": total_pos,
            "n_train_negative": total_neg,
            "n_examples_evaluable": n_eval_evaluable,
            "n_examples_classified_mature": n_eval_mature,
            "examples_mature_rate": float(n_eval_mature / max(n_eval_evaluable, 1)),
        })

    out_path = os.path.join(OUT_DIR, "top_features.json")
    assert out_path.startswith(WORKTREE), f"refusing write: {out_path}"
    with open(out_path, "w") as f:
        json.dump({
            "n_clusters": int(len(df_rows)),
            "n_positive": int(labels.sum()),
            "n_negative": int((1 - labels).sum()),
            "n_exprs_evaluated": int((~np.isnan(aucs)).sum()),
            "n_exprs_total": int(n_exprs),
            "top_by_abs_auc": top[:50],
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }, f, indent=2)
    print(f"\nwrote {out_path}")

    print("\nTop 15 discriminators (by |AUC - 0.5|):")
    for t in top[:15]:
        print(f"  AUC={t['auc']:.3f}  J={t['youden_j']:.3f}  tpr={t['tpr']:.2f} fpr={t['fpr']:.2f}  "
              f"ex_mature={t['n_examples_classified_mature']}/{t['n_examples_evaluable']} "
              f"({t['examples_mature_rate']:.0%})  "
              f"{t['expression']:<36} {t['direction']} {t['best_threshold']:.4f}")

    # Highlight rules that pass high ex_mature_rate — those are worth further work
    print("\nTop rules also classifying >= 90% of examples as mature:")
    n_printed = 0
    for t in top[:100]:
        if t['examples_mature_rate'] >= 0.90 and n_printed < 10:
            print(f"  AUC={t['auc']:.3f}  ex_mature={t['n_examples_classified_mature']}/{t['n_examples_evaluable']} "
                  f"({t['examples_mature_rate']:.0%})  "
                  f"{t['expression']:<36} {t['direction']} {t['best_threshold']:.4f}")
            n_printed += 1
    if n_printed == 0:
        print("  [none in top 100 — rule over-discriminates; examples look like non-rightmost bars]")


if __name__ == "__main__":
    main()
