"""
Compute per-column z-score normalization parameters (mean + std) from
regime_vector_history.parquet and persist as regime_vector_normalization.json.

Used by the K-NN similarity engine: every regime vector (history and live)
is z-scored against these parameters before Euclidean distance is computed.
Standard deviation uses sample-std (ddof=1) so the params survive cleanly
if the history later grows.

NaN-tolerant: per-column mean/std use only the rows where that column is
non-NaN (each column normalizes against its own population, not against
a row-intersection). Each column's first non-NaN date and non-NaN count
are recorded alongside the params.

Reads + writes only inside the worktree.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

_THIS = os.path.dirname(os.path.abspath(__file__))
_WORKTREE = os.path.dirname(_THIS)
if _WORKTREE not in sys.path:
    sys.path.insert(0, _WORKTREE)

from regime_meter.regime_vector import OUTPUT_COLUMNS


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = _THIS
WORKTREE_ROOT = _WORKTREE
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
HISTORY_PARQUET = os.path.join(CACHE_DIR, "regime_vector_history.parquet")
NORM_JSON       = os.path.join(CACHE_DIR, "regime_vector_normalization.json")

# Minimum std below which we abort — a vanishingly small std signals a
# degenerate column and would blow up z-score arithmetic at runtime.
MIN_STD = 1e-9


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def load_history():
    if not os.path.exists(HISTORY_PARQUET):
        sys.exit(
            f"ABORT: history parquet missing at {HISTORY_PARQUET} "
            "(run regime_meter/regime_vector_history.py)"
        )
    df = pd.read_parquet(HISTORY_PARQUET)
    df["date"] = pd.to_datetime(df["date"])

    missing = [c for c in OUTPUT_COLUMNS if c not in df.columns]
    if missing:
        sys.exit(f"ABORT: history parquet missing columns: {missing}")

    print(f"  Loaded history: {len(df)} rows, "
          f"{df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}")
    return df


def compute_params(df):
    """Per-column mean + sample std (ddof=1) computed over only the rows
    where that column is non-NaN. Each column is normalized against its
    own population, not against a row-intersection.
    """
    print(f"\n  Computing per-column mean + std (ddof=1) over non-NaN values ...")
    params = {}
    for c in OUTPUT_COLUMNS:
        s = df[c].astype("float64")
        nn = s.notna()
        n_nn = int(nn.sum())
        if n_nn == 0:
            sys.exit(f"ABORT: column {c!r} has zero non-NaN values")
        sub = s[nn]
        mean = float(sub.mean())
        std  = float(sub.std(ddof=1))
        if not np.isfinite(mean) or not np.isfinite(std):
            sys.exit(
                f"ABORT: non-finite mean/std for column {c!r}: "
                f"mean={mean}, std={std}"
            )
        if std < MIN_STD:
            sys.exit(f"ABORT: column {c!r} std={std} below MIN_STD={MIN_STD}")
        first_nn_date = df.loc[nn, "date"].iloc[0].strftime("%Y-%m-%d")
        params[c] = {
            "mean":              mean,
            "std":               std,
            "n_non_nan_rows":    n_nn,
            "first_non_nan_date": first_nn_date,
        }
    return params


def build_output(df, params):
    return {
        "source":     "regime_vector_history.parquet",
        "rows":       int(len(df)),
        "first_date": df["date"].iloc[0].strftime("%Y-%m-%d"),
        "last_date":  df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "ddof":       1,
        "columns":    params,
    }


def print_summary(params):
    print(f"\n  Per-column normalization parameters:")
    print(f"    {'column':<35s} {'mean':>12s}  {'std':>12s} {'n':>6s} {'first-non-NaN':<14s}")
    print(f"    {'-'*35} {'-'*12}  {'-'*12} {'-'*6} {'-'*14}")
    for c in OUTPUT_COLUMNS:
        m = params[c]["mean"]
        s = params[c]["std"]
        n = params[c]["n_non_nan_rows"]
        d = params[c]["first_non_nan_date"]
        print(f"    {c:<35s} {m:>+12.4f}  {s:>12.4f} {n:>6d} {d:<14s}")


def write_output(payload, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"\n  Writing -> {out_path}")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  {size_kb:.1f} KB written")


def main():
    parser = argparse.ArgumentParser(
        description="Build regime_vector_normalization.json (worktree-local)"
    )
    parser.parse_args()

    print("=" * 70)
    print("  REGIME VECTOR NORMALIZATION BUILD (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(NORM_JSON)
    print(f"  Worktree root: {WORKTREE_ROOT}")
    print(f"  Input:         {HISTORY_PARQUET}")
    print(f"  Output:        {NORM_JSON}")

    print()
    df = load_history()
    params = compute_params(df)
    payload = build_output(df, params)
    print_summary(params)
    write_output(payload, NORM_JSON)

    print("\n  DONE.")


if __name__ == "__main__":
    main()
