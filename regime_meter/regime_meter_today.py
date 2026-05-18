"""
Regime meter — K-NN similarity lookup, signal-vs-noise horizon selector,
and today's output payload.

Pipeline (step 1):
  1. Load regime_vector_history + normalization params + per-sector
     forward-path tensors (all worktree-local).
  2. For a target date, build its regime vector, z-score against the
     persisted params, find the K nearest historical anchors by Euclidean
     distance over the z-scored vector.
  3. For each candidate horizon in {5, 10, 20, 40}, pool the K similar-day
     forward returns across all 11 sectors and compare against the
     unconditional pool at the same horizon via Kolmogorov-Smirnov.
     Pick the horizon with the largest KS-statistic.
  4. Emit a JSON payload containing target date, neighbor list, picked
     horizon + divergence info, and per-sector path matrices ready for a
     cone-heatmap renderer downstream.

Reads + writes only inside the worktree.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

_THIS = os.path.dirname(os.path.abspath(__file__))
_WORKTREE = os.path.dirname(_THIS)
if _WORKTREE not in sys.path:
    sys.path.insert(0, _WORKTREE)

from regime_meter.regime_vector import (
    OUTPUT_COLUMNS,
    SECTOR_KEYS,
    _load_spy_calendar,
    regime_vector,
)


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = _THIS
WORKTREE_ROOT = _WORKTREE
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
HISTORY_PARQUET      = os.path.join(CACHE_DIR, "regime_vector_history.parquet")
NORM_JSON            = os.path.join(CACHE_DIR, "regime_vector_normalization.json")
FORWARD_PATHS_DIR    = os.path.join(CACHE_DIR, "sector_forward_paths")
TODAY_OUTPUT_JSON    = os.path.join(CACHE_DIR, "regime_meter_today.json")

# --- Defaults --------------------------------------------------------------
DEFAULT_K          = 30
HORIZON_CANDIDATES = (5, 10, 20, 40)


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


# --- Loaders ---------------------------------------------------------------
def load_history():
    if not os.path.exists(HISTORY_PARQUET):
        raise ValueError(
            f"history parquet missing at {HISTORY_PARQUET} "
            "(run regime_meter/regime_vector_history.py)"
        )
    df = pd.read_parquet(HISTORY_PARQUET)
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_normalization():
    if not os.path.exists(NORM_JSON):
        raise ValueError(
            f"normalization JSON missing at {NORM_JSON} "
            "(run regime_meter/normalization_build.py)"
        )
    with open(NORM_JSON, "r", encoding="utf-8") as f:
        payload = json.load(f)
    means = np.array([payload["columns"][c]["mean"] for c in OUTPUT_COLUMNS], dtype="float64")
    stds  = np.array([payload["columns"][c]["std"]  for c in OUTPUT_COLUMNS], dtype="float64")
    return means, stds


def load_forward_paths():
    """Returns dict[sector] -> (anchor_dates, paths) where paths is (n,40)."""
    out = {}
    for sector in SECTOR_KEYS:
        path = os.path.join(FORWARD_PATHS_DIR, f"{sector}.npz")
        if not os.path.exists(path):
            raise ValueError(
                f"forward-paths file missing at {path} "
                "(run regime_meter/forward_paths_build.py)"
            )
        z = np.load(path, allow_pickle=True)
        anchors = pd.DatetimeIndex(pd.to_datetime(z["anchor_dates"]))
        out[sector] = (anchors, z["paths"])
    # Cross-check all sectors share the same anchor index.
    ref_anchors = out[SECTOR_KEYS[0]][0]
    for sector in SECTOR_KEYS[1:]:
        if not out[sector][0].equals(ref_anchors):
            raise ValueError(f"forward-path anchor mismatch: {sector} differs from {SECTOR_KEYS[0]}")
    return ref_anchors, {s: arr for s, (_, arr) in out.items()}


# --- Z-score + K-NN --------------------------------------------------------
def zscore(matrix, means, stds):
    return (matrix - means) / stds


def find_neighbors(target_date, k=DEFAULT_K, eligible_dates=None):
    """Return (neighbor_dates, distances) - K closest history dates to target.

    Excludes target itself from the neighbor pool (no self-match).
    If eligible_dates is provided, neighbors are drawn only from that set
    (used to restrict to dates that have forward paths available).
    """
    target = pd.Timestamp(target_date).normalize()

    means, stds = load_normalization()
    history = load_history()
    history_dates = pd.DatetimeIndex(history["date"])

    target_vec = regime_vector(target).to_numpy(dtype="float64")
    target_z   = (target_vec - means) / stds

    hist_matrix = history[OUTPUT_COLUMNS].to_numpy(dtype="float64")
    hist_z      = zscore(hist_matrix, means, stds)

    diffs    = hist_z - target_z
    dists_sq = np.einsum("ij,ij->i", diffs, diffs)

    # Drop target's own row if present in history.
    if target in history_dates:
        self_pos = int(history_dates.get_loc(target))
        dists_sq[self_pos] = np.inf

    # Drop history rows not in the eligible set.
    if eligible_dates is not None:
        eligible_set = pd.DatetimeIndex(eligible_dates)
        in_eligible = history_dates.isin(eligible_set)
        dists_sq = np.where(in_eligible, dists_sq, np.inf)
        n_eligible = int(np.isfinite(dists_sq).sum())
        if n_eligible < k:
            raise ValueError(
                f"only {n_eligible} eligible history rows after filtering, need k={k}"
            )

    order = np.argsort(dists_sq, kind="stable")[:k]
    neighbor_dates = history_dates[order]
    distances = np.sqrt(dists_sq[order])
    return neighbor_dates, distances


# --- Horizon picker --------------------------------------------------------
def _pool_returns_at_horizon(anchor_mask, paths_by_sector, horizon):
    """Returns (n_anchors_in_mask * 11_sectors,) array of returns at horizon-1 index."""
    col = horizon - 1  # 0-indexed
    pooled = [arr[anchor_mask, col] for arr in paths_by_sector.values()]
    return np.concatenate(pooled)


def pick_horizon(neighbor_dates, anchors, paths_by_sector,
                 horizons=HORIZON_CANDIDATES):
    """For each horizon, KS-stat between similar-day pooled returns vs
    unconditional pooled returns. Return picked horizon + per-horizon stats.
    """
    # Map neighbor dates to row indices in the anchor array. Neighbors that
    # are too recent to have forward paths get dropped (their forward window
    # extends past today).
    neighbor_positions = anchors.get_indexer(neighbor_dates)
    valid_mask = neighbor_positions >= 0
    neighbor_positions = neighbor_positions[valid_mask]

    if len(neighbor_positions) == 0:
        raise ValueError("no neighbors have forward paths available")

    neighbor_mask = np.zeros(len(anchors), dtype=bool)
    neighbor_mask[neighbor_positions] = True
    uncond_mask = np.ones(len(anchors), dtype=bool)

    per_horizon = {}
    for h in horizons:
        cond_returns   = _pool_returns_at_horizon(neighbor_mask, paths_by_sector, h)
        uncond_returns = _pool_returns_at_horizon(uncond_mask,   paths_by_sector, h)
        ks = ks_2samp(cond_returns, uncond_returns)
        per_horizon[h] = {
            "ks_stat":  float(ks.statistic),
            "ks_pvalue": float(ks.pvalue),
            "n_conditional":   int(cond_returns.size),
            "n_unconditional": int(uncond_returns.size),
        }

    picked = max(horizons, key=lambda h: per_horizon[h]["ks_stat"])
    n_neighbors_with_paths = int(valid_mask.sum())
    n_neighbors_dropped    = int((~valid_mask).sum())
    return picked, per_horizon, n_neighbors_with_paths, n_neighbors_dropped


# --- Payload assembly ------------------------------------------------------
def build_payload(target_date, k=DEFAULT_K):
    target = pd.Timestamp(target_date).normalize()

    anchors, paths_by_sector = load_forward_paths()
    # Restrict the neighbor pool to anchors that have forward paths so K is
    # honored even when target is near the end of history.
    neighbor_dates, distances = find_neighbors(target, k=k, eligible_dates=anchors)
    picked, per_horizon, _, _ = pick_horizon(
        neighbor_dates, anchors, paths_by_sector
    )

    # All neighbors are anchors by construction, so positions are valid.
    neighbor_positions = anchors.get_indexer(neighbor_dates)
    assert (neighbor_positions >= 0).all(), "neighbor not in anchor index"

    sector_paths = {}
    for sector, arr in paths_by_sector.items():
        sliced = arr[neighbor_positions, :picked]
        sector_paths[sector] = sliced.tolist()

    payload = {
        "target_date": target.strftime("%Y-%m-%d"),
        "k": k,
        "picked_horizon": picked,
        "divergence_metric": "ks_2samp",
        "divergence_by_horizon": per_horizon,
        "neighbors": [
            {"date": d.strftime("%Y-%m-%d"), "distance": float(dist)}
            for d, dist in zip(neighbor_dates, distances)
        ],
        "sector_paths": sector_paths,
        "bars_forward": list(range(1, picked + 1)),
    }
    return payload


def write_payload(payload, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return os.path.getsize(out_path) / 1024


# --- CLI -------------------------------------------------------------------
def _latest_target_date():
    history = load_history()
    return pd.Timestamp(history["date"].iloc[-1]).normalize()


def main():
    parser = argparse.ArgumentParser(
        description="Regime meter -- K-NN similarity, horizon picker, today's payload"
    )
    parser.add_argument("--date", default=None,
                        help="Target date YYYY-MM-DD. Default: latest history date.")
    parser.add_argument("--k", type=int, default=DEFAULT_K,
                        help=f"Number of nearest neighbors. Default {DEFAULT_K}.")
    args = parser.parse_args()

    print("=" * 70)
    print("  REGIME METER -- TODAY OUTPUT (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(TODAY_OUTPUT_JSON)
    print(f"  Worktree root: {WORKTREE_ROOT}")
    print(f"  Output:        {TODAY_OUTPUT_JSON}")

    if args.date is None:
        target = _latest_target_date()
    else:
        target = pd.Timestamp(args.date).normalize()
    print(f"  Target date:   {target.date()}")
    print(f"  K:             {args.k}")

    print()
    print("  Building payload ...")
    payload = build_payload(target, k=args.k)

    print(f"\n  Picked horizon: {payload['picked_horizon']} bars")
    print(f"  Neighbors: {len(payload['neighbors'])}")
    print(f"\n  Per-horizon KS divergence vs unconditional:")
    for h, stats in payload["divergence_by_horizon"].items():
        flag = "  <-- picked" if h == payload["picked_horizon"] else ""
        print(f"    h={h:>2}  ks={stats['ks_stat']:.4f}  p={stats['ks_pvalue']:.3g}"
              f"  n_cond={stats['n_conditional']}  n_uncond={stats['n_unconditional']}{flag}")

    print(f"\n  Top 5 neighbors (of {len(payload['neighbors'])}):")
    for n in payload["neighbors"][:5]:
        print(f"    {n['date']}  d={n['distance']:.4f}")

    size_kb = write_payload(payload, TODAY_OUTPUT_JSON)
    print(f"\n  Wrote {size_kb:.1f} KB -> {TODAY_OUTPUT_JSON}")

    print("\n  DONE.")


if __name__ == "__main__":
    main()
