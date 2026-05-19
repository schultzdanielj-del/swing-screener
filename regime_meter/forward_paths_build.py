"""
Per-sector forward log-return path tensors (worktree-local).

For each of the 11 SPDR sector ETFs, for every history anchor where that
sector has native (non-chain-linked) price data at the anchor AND for the
next 40 trading days, compute the forward log-return path:

    path[i, k] = log(close[anchor_i + k + 1] / close[anchor_i])

for k in 0..39. Column k is the (k+1)-bar-forward log return.

Per-sector anchor counts vary: XLE/XLB/XLI/XLY/XLP/XLV/XLF/XLK/XLU all
have data back to 1998-12-22, XLRE only from 2015-10-08, XLC only from
2018-06-19. The K-NN admissibility filter in regime_meter_today.py
exploits these per-sector pool depths.

NOTE: chain-linked closes are used for the regime vector's sector-
dispersion column (so that scalar is continuous across XLC/XLRE
inception splices), but here we use NATIVE closes only. Comparing
today's XLC forward returns against a 1999 anchor's XLC forward
returns shouldn't fall back to XLK -- that would be a misleading
match. Sector-admissibility correctly drops pre-2018 anchors for XLC.

Output: regime_meter/cache/sector_forward_paths/{sector}.npz per sector.
  - anchor_dates: shape (n_sector,)     datetime64[ns]
  - paths:        shape (n_sector, 40)  float64
  - bars_forward: shape (40,)           int64, values 1..40

n_sector varies across sectors.

Reads market_ohlcv_extended.pkl and regime_vector_history.parquet.
Writes only inside the worktree.
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
    _load_market_ohlcv,
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


def load_eligible_history(spy_dates):
    """History dates that have at least MAX_HORIZON forward SPY trading
    days available. Returns (history_dates, history_pos_in_spy)."""
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
    eligible = history_dates[eligible_mask]
    eligible_pos = spy_pos[eligible_mask]

    n_dropped = int((~eligible_mask).sum())
    print(f"  History rows: {len(history_dates)}")
    print(f"  Dropped last {n_dropped} anchors lacking {MAX_HORIZON} forward bars")
    print(f"  Candidate anchors: {len(eligible)} "
          f"({eligible[0].date()} -> {eligible[-1].date()})")
    return eligible, eligible_pos


def native_sector_log_closes(ohlcv, sector, spy_dates):
    """Return log(native close) reindexed to SPY calendar.

    Pre-inception SPY dates yield NaN (no value to forward-fill from).
    Post-inception SPY dates get the sector's native close, forward-filled
    across any sector-specific holidays.
    """
    df = ohlcv[sector]
    s = (
        df.set_index(pd.to_datetime(df["date"]))["close"]
        .sort_index()
        .reindex(spy_dates, method="ffill")
    )
    arr = s.to_numpy(dtype="float64")
    if (arr[~np.isnan(arr)] <= 0).any():
        sys.exit(f"ABORT: non-positive close in {sector} after reindex")
    return np.log(arr, where=arr > 0, out=np.full_like(arr, np.nan))


def build_sector_paths(log_closes, eligible, eligible_pos):
    """For each candidate anchor in `eligible` (positions in `eligible_pos`),
    keep only those where the sector has a finite anchor close AND finite
    closes for all MAX_HORIZON forward bars. Build the log-return path for
    each surviving anchor."""
    sector_anchors = []
    sector_paths   = []
    for d, ap in zip(eligible, eligible_pos):
        anchor_log = log_closes[ap]
        if not np.isfinite(anchor_log):
            continue
        forward = log_closes[ap + 1 : ap + 1 + MAX_HORIZON]
        if forward.size != MAX_HORIZON or not np.isfinite(forward).all():
            continue
        sector_anchors.append(d)
        sector_paths.append(forward - anchor_log)
    if not sector_anchors:
        return pd.DatetimeIndex([]), np.empty((0, MAX_HORIZON), dtype="float64")
    anchors_idx = pd.DatetimeIndex(sector_anchors)
    paths_arr   = np.stack(sector_paths, axis=0)
    return anchors_idx, paths_arr


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


def print_sector_summary(sector, anchors, paths):
    if len(anchors) == 0:
        print(f"    {sector:<5s}  EMPTY")
        return
    h5  = paths[:, 4]
    h20 = paths[:, 19]
    h40 = paths[:, 39]
    print(f"    {sector:<5s}  "
          f"n={len(anchors):>5d}  "
          f"first={anchors[0].date()}  "
          f"h5 m={h5.mean():+.4f}/s={h5.std(ddof=1):.4f}  "
          f"h20 m={h20.mean():+.4f}/s={h20.std(ddof=1):.4f}  "
          f"h40 m={h40.mean():+.4f}/s={h40.std(ddof=1):.4f}")


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
    print(f"  Sectors:       {SECTOR_KEYS}")
    print(f"  Horizon:       1..{MAX_HORIZON} bars")
    print(f"  Note:          Native close only (no chain-link). Per-sector "
          f"anchor counts vary.")

    print()
    spy_dates = _load_spy_calendar()
    print(f"  SPY calendar:  {len(spy_dates)} dates "
          f"({spy_dates[0].date()} -> {spy_dates[-1].date()})")

    print()
    eligible, eligible_pos = load_eligible_history(spy_dates)

    print()
    print(f"  Loading extended OHLCV ...")
    ohlcv = _load_market_ohlcv()

    print()
    print(f"  Building forward-path tensors:")
    t0 = time.time()
    summaries = []
    for sector in SECTOR_KEYS:
        log_closes = native_sector_log_closes(ohlcv, sector, spy_dates)
        anchors, paths = build_sector_paths(log_closes, eligible, eligible_pos)
        out_path, size_kb = write_sector(sector, anchors, paths, OUT_DIR)
        summaries.append((sector, anchors, paths, size_kb))
    elapsed = time.time() - t0
    print(f"  Built {len(SECTOR_KEYS)} sectors in {elapsed:.2f}s")

    print(f"\n  Per-sector summary:")
    for sector, anchors, paths, _ in summaries:
        print_sector_summary(sector, anchors, paths)

    total_kb = sum(s[3] for s in summaries)
    print(f"\n  Wrote {len(SECTOR_KEYS)} files, total {total_kb:.1f} KB")

    print("\n  DONE.")


if __name__ == "__main__":
    main()
