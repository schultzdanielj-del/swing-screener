"""
Build regime_vector_history.parquet — one row per SPY trading day from the
earliest fully-computable date through today.

The earliest date is auto-probed by walking forward from 2016-01-04 and
trying regime_vector(date) on each; the first one that doesn't raise is
the start. After that, any raised ValueError is treated as a real bug and
aborts the run. (Today's binding constraint is the 200-MA distance window
on DXY/Gold/BTC, putting the start at ~2016-10-19.)

Reads main-repo caches read-only via regime_vector(). Writes only inside
the worktree.
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

PROBE_FLOOR     = pd.Timestamp("2016-01-04")
PROGRESS_EVERY  = 250


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def probe_start(spy_dates):
    """Walk forward from PROBE_FLOOR; return the first date for which
    regime_vector(date) returns without raising."""
    candidates = spy_dates[spy_dates >= PROBE_FLOOR]
    print(f"  Probing earliest computable date from {candidates[0].date()} ...")
    last_err = None
    for d in candidates:
        try:
            regime_vector(d)
            print(f"  -> first computable date: {d.date()}")
            return d
        except ValueError as e:
            last_err = e
            continue
    sys.exit(f"ABORT: no date in SPY calendar produces a valid regime vector. Last error: {last_err}")


def build_history(spy_dates, start_date):
    targets = spy_dates[spy_dates >= start_date]
    n = len(targets)
    print(f"  Building history over {n} trading days "
          f"({targets[0].date()} -> {targets[-1].date()}) ...")

    rows = []
    t0 = time.time()
    for i, d in enumerate(targets):
        try:
            v = regime_vector(d)
        except ValueError as e:
            sys.exit(
                f"ABORT: regime_vector({d.date()}) raised after probe succeeded: {e}\n"
                "This is a real data gap, not a start-of-history issue."
            )
        rows.append((d, *v.values))

        if (i + 1) % PROGRESS_EVERY == 0 or (i + 1) == n:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"    {i + 1}/{n} ({d.date()})  {rate:.0f} rows/s")

    df = pd.DataFrame(rows, columns=["date"] + OUTPUT_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    return df


def run_sanity_checks(df):
    print("\n  Sanity checks:")
    print(f"    Output rows:    {len(df)}")
    print(f"    First date:     {df['date'].iloc[0].date()}")
    print(f"    Last date:      {df['date'].iloc[-1].date()}")
    print(f"    Column count:   {len(df.columns)}  (expected 25 = date + 24)")

    if len(df.columns) != 25:
        sys.exit(f"ABORT: expected 25 columns, got {len(df.columns)}")

    n_nan = int(df[OUTPUT_COLUMNS].isna().sum().sum())
    if n_nan != 0:
        bad = df[OUTPUT_COLUMNS].isna().sum()
        bad = bad[bad > 0]
        sys.exit(f"ABORT: {n_nan} NaN values present:\n{bad}")
    print(f"    NaN cells:      0")

    n_inf = int(np.isinf(df[OUTPUT_COLUMNS].to_numpy()).sum())
    if n_inf != 0:
        sys.exit(f"ABORT: {n_inf} infinite values present")
    print(f"    Inf cells:      0")

    # Per-column min/max sanity print so Dan can eyeball.
    print(f"\n  Per-column ranges:")
    for c in OUTPUT_COLUMNS:
        s = df[c]
        print(f"    {c:<35s} min={s.min():+10.4f}  max={s.max():+10.4f}  mean={s.mean():+10.4f}")


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
    start = probe_start(spy_dates)

    print()
    df = build_history(spy_dates, start)
    run_sanity_checks(df)
    write_output(df, OUT_PARQUET)

    print("\n  DONE.")


if __name__ == "__main__":
    main()
