"""
Per-sector forward log-return path tensors (worktree-local).

For each of the 11 SPDR sector ETFs, for every anchor day where a regime
vector exists AND 40 future trading days are available, compute the
forward log-return path:

    path[i, k] = log(close[anchor_i + k + 1] / close[anchor_i])

for k in 0..39. Column k is the (k+1)-bar-forward log return.

Output: regime_meter/cache/sector_forward_paths/{sector}.npz per sector,
each containing:
  - anchor_dates: shape (n,)        object array of pd.Timestamp
  - paths:        shape (n, 40)     float64
  - bars_forward: shape (40,)       int64, values 1..40

XLC uses the chain-linked close series from regime_vector (XLK scaled by
the XLC/XLK ratio at 2018-06-19 inception, for all dates before that),
so returns are continuous across the splice.

Reads main-repo caches read-only via regime_vector helpers. Writes only
inside the worktree.
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

_THIS = os.path.dirname(os.path.abspath(__file__))
_WORKTREE = os.path.dirname(_THIS)
if _WORKTREE not in sys.path:
    sys.path.insert(0, _WORKTREE)

from regime_meter.regime_vector import (
    SECTOR_KEYS,
    _load_close_matrix,
    _load_spy_calendar,
)


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = _THIS
WORKTREE_ROOT = _WORKTREE
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
HISTORY_PARQUET = os.path.join(CACHE_DIR, "regime_vector_history.parquet")
OUT_DIR         = os.path.join(CACHE_DIR, "sector_forward_paths")

MAX_HORIZON = 40


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def load_anchors(spy_dates):
    if not os.path.exists(HISTORY_PARQUET):
        sys.exit(
            f"ABORT: history parquet missing at {HISTORY_PARQUET} "
            "(run regime_meter/regime_vector_history.py)"
        )
    df = pd.read_parquet(HISTORY_PARQUET)
    history_dates = pd.DatetimeIndex(pd.to_datetime(df["date"]))

    spy_pos = spy_dates.get_indexer(history_dates)
    if (spy_pos < 0).any():
        sys.exit("ABORT: some history dates not in SPY calendar")
    last_anchor_pos = len(spy_dates) - 1 - MAX_HORIZON
    eligible_mask = spy_pos <= last_anchor_pos
    anchors = history_dates[eligible_mask]
    anchor_pos = spy_pos[eligible_mask]

    n_dropped = int((~eligible_mask).sum())
    print(f"  History rows: {len(history_dates)}")
    print(f"  Dropped last {n_dropped} anchors lacking {MAX_HORIZON} forward bars")
    print(f"  Eligible anchors: {len(anchors)} "
          f"({anchors[0].date()} -> {anchors[-1].date()})")
    return anchors, anchor_pos


def build_sector_paths(close_mat, sector, anchors, anchor_pos):
    closes = close_mat[sector].to_numpy(dtype="float64")
    if (closes <= 0).any():
        sys.exit(f"ABORT: non-positive close in {sector}")

    n = len(anchors)
    paths = np.empty((n, MAX_HORIZON), dtype="float64")
    log_closes = np.log(closes)

    for i, ap in enumerate(anchor_pos):
        anchor_log = log_closes[ap]
        # k+1 bar forward, k=0..MAX_HORIZON-1
        paths[i, :] = log_closes[ap + 1 : ap + 1 + MAX_HORIZON] - anchor_log

    if not np.isfinite(paths).all():
        sys.exit(f"ABORT: non-finite values in {sector} forward paths")
    return paths


def write_sector(sector, anchors, paths, out_dir):
    _assert_inside_worktree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{sector}.npz")
    np.savez_compressed(
        out_path,
        anchor_dates=anchors.to_numpy(dtype="datetime64[ns]"),
        paths=paths,
        bars_forward=np.arange(1, MAX_HORIZON + 1, dtype="int64"),
    )
    size_kb = os.path.getsize(out_path) / 1024
    return out_path, size_kb


def print_sector_summary(sector, paths):
    h5  = paths[:, 4]   # 5 bars forward
    h20 = paths[:, 19]
    h40 = paths[:, 39]
    print(f"    {sector:<5s}  "
          f"h5  mean={h5.mean():+.4f} std={h5.std(ddof=1):.4f}  |  "
          f"h20 mean={h20.mean():+.4f} std={h20.std(ddof=1):.4f}  |  "
          f"h40 mean={h40.mean():+.4f} std={h40.std(ddof=1):.4f}")


def main():
    parser = argparse.ArgumentParser(
        description="Build per-sector forward-path tensors (worktree-local)"
    )
    parser.parse_args()

    print("=" * 70)
    print("  SECTOR FORWARD-PATH BUILD (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(OUT_DIR)
    print(f"  Worktree root: {WORKTREE_ROOT}")
    print(f"  Output dir:    {OUT_DIR}")
    print(f"  Sectors:       {SECTOR_KEYS}  (XLC chain-linked to XLK pre-2018-06-19)")
    print(f"  Horizon:       1..{MAX_HORIZON} bars")

    print()
    spy_dates = _load_spy_calendar()
    print(f"  SPY calendar:  {len(spy_dates)} dates "
          f"({spy_dates[0].date()} -> {spy_dates[-1].date()})")

    print()
    anchors, anchor_pos = load_anchors(spy_dates)

    print()
    print(f"  Loading close matrix ...")
    close_mat = _load_close_matrix()

    print()
    print(f"  Building forward-path tensors:")
    t0 = time.time()
    summaries = []
    for sector in SECTOR_KEYS:
        paths = build_sector_paths(close_mat, sector, anchors, anchor_pos)
        out_path, size_kb = write_sector(sector, anchors, paths, OUT_DIR)
        summaries.append((sector, paths, size_kb, out_path))
    elapsed = time.time() - t0
    print(f"  Built {len(SECTOR_KEYS)} sectors in {elapsed:.2f}s")

    print(f"\n  Per-sector summary (h5/h20/h40 log-return stats):")
    for sector, paths, _, _ in summaries:
        print_sector_summary(sector, paths)

    total_kb = sum(s[2] for s in summaries)
    print(f"\n  Wrote {len(SECTOR_KEYS)} files, total {total_kb:.1f} KB")

    print("\n  DONE.")


if __name__ == "__main__":
    main()
