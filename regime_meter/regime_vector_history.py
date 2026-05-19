"""
Build regime_vector_history.parquet -- one row per SPY trading day from
the earliest extended-pickle SPY date through today.

NaN-tolerant: each row carries the 24 regime-vector values plus 24
matching boolean mask columns (`mask_<colname>` = True iff that column
is non-NaN for that date). Downstream code uses the mask to decide
which anchors to use and at what column overlap.

A row is persisted only if at least one of its 24 columns is non-NaN.
There is no minimum-non-NaN-count gate at storage time -- the query
layer (Phase D distance + admissibility) is responsible for deciding
what's usable.

Reads worktree-local caches only via regime_vector(). Writes only
inside the worktree.
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

# Make `regime_meter.*` importable when run directly via `python regime_meter/...`
_THIS = os.path.dirname(os.path.abspath(__file__))
_WORKTREE = os.path.dirname(_THIS)
if _WORKTREE not in sys.path:
    sys.path.insert(0, _WORKTREE)

from regime_meter.regime_vector import (
    OUTPUT_COLUMNS,
    _load_spy_calendar,
    regime_vector,
)


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
OUT_PARQUET   = os.path.join(CACHE_DIR, "regime_vector_history.parquet")

PROGRESS_EVERY = 250

MASK_COLUMNS = [f"mask_{c}" for c in OUTPUT_COLUMNS]


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def build_history(spy_dates):
    n = len(spy_dates)
    print(f"  Building history over {n} trading days "
          f"({spy_dates[0].date()} -> {spy_dates[-1].date()}) ...")

    rows = []
    skipped_empty = 0
    t0 = time.time()
    for i, d in enumerate(spy_dates):
        try:
            v = regime_vector(d)
        except ValueError as e:
            sys.exit(
                f"ABORT: regime_vector({d.date()}) raised unexpectedly: {e}\n"
                "Phase C should only raise when target is not in SPY calendar, "
                "but the loop iterates the SPY calendar itself."
            )
        mask = v.notna().values
        if not mask.any():
            skipped_empty += 1
            continue
        row = [d] + list(v.values) + list(mask.tolist())
        rows.append(row)

        if (i + 1) % PROGRESS_EVERY == 0 or (i + 1) == n:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"    {i + 1}/{n} ({d.date()})  {rate:.0f} rows/s  "
                  f"kept {len(rows)}, skipped {skipped_empty}")

    df = pd.DataFrame(rows, columns=["date"] + OUTPUT_COLUMNS + MASK_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    for c in MASK_COLUMNS:
        df[c] = df[c].astype(bool)
    return df, skipped_empty


def run_sanity_checks(df, skipped_empty):
    print("\n  Sanity checks:")
    print(f"    Output rows:    {len(df)}")
    print(f"    Skipped (all-NaN): {skipped_empty}")
    print(f"    First date:     {df['date'].iloc[0].date()}")
    print(f"    Last date:      {df['date'].iloc[-1].date()}")
    expected_cols = 1 + len(OUTPUT_COLUMNS) + len(MASK_COLUMNS)
    print(f"    Column count:   {len(df.columns)}  (expected {expected_cols} "
          f"= date + {len(OUTPUT_COLUMNS)} values + {len(MASK_COLUMNS)} masks)")

    if len(df.columns) != expected_cols:
        sys.exit(f"ABORT: expected {expected_cols} columns, got {len(df.columns)}")

    # Sanity: mask must match value notna()
    for c in OUTPUT_COLUMNS:
        m = df[f"mask_{c}"]
        v = df[c]
        if (m != v.notna()).any():
            n_mismatch = int((m != v.notna()).sum())
            sys.exit(
                f"ABORT: mask_{c} disagrees with notna({c}) on {n_mismatch} rows"
            )

    # Per-column non-NaN coverage
    print(f"\n  Per-column non-NaN coverage:")
    print(f"    {'column':<35s} {'first non-NaN':<14s} {'count':>8s} {'pct':>8s}")
    print(f"    {'-'*35} {'-'*14} {'-'*8} {'-'*8}")
    for c in OUTPUT_COLUMNS:
        nn = df[c].notna()
        cnt = int(nn.sum())
        pct = (cnt / len(df)) * 100 if len(df) else 0.0
        if cnt > 0:
            first_d = df.loc[nn, "date"].iloc[0].date()
        else:
            first_d = "(none)"
        print(f"    {c:<35s} {str(first_d):<14s} {cnt:>8d} {pct:>7.1f}%")

    # Per-row mask distribution
    counts = df[MASK_COLUMNS].sum(axis=1)
    print(f"\n  Per-row non-NaN count distribution (across {len(df)} rows):")
    for thr in [1, 7, 12, 18, 24]:
        n_ge = int((counts >= thr).sum())
        pct_ge = (n_ge / len(df)) * 100 if len(df) else 0.0
        print(f"    rows with >= {thr:>2d} cols non-NaN: {n_ge:>5d}  ({pct_ge:>5.1f}%)")


def write_output(df, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"\n  Writing -> {out_path}")
    df.to_parquet(out_path, index=False)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  {size_kb:.1f} KB written")


def main():
    parser = argparse.ArgumentParser(
        description="Build regime_vector_history.parquet (worktree-local)"
    )
    parser.parse_args()

    print("=" * 70)
    print("  REGIME VECTOR HISTORY BUILD (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(OUT_PARQUET)
    print(f"  Worktree root: {WORKTREE_ROOT}")
    print(f"  Output:        {OUT_PARQUET}")

    print()
    spy_dates = _load_spy_calendar()
    print(f"  SPY calendar:  {len(spy_dates)} dates "
          f"({spy_dates[0].date()} -> {spy_dates[-1].date()})")

    print()
    df, skipped = build_history(spy_dates)
    run_sanity_checks(df, skipped)
    write_output(df, OUT_PARQUET)

    print("\n  DONE.")


if __name__ == "__main__":
    main()
