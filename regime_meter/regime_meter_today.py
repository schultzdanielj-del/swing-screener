"""
Regime meter -- K-NN similarity lookup, signal-vs-noise horizon selector,
and today's output payload.

Pipeline:
  1. Load regime_vector_history + per-column normalization + per-sector
     forward-path tensors (each sector has its own anchor_dates).
  2. For a target date, build its regime vector, z-score against the
     persisted params, find the K nearest historical anchors using the
     shrinkage-weighted distance: observed squared diffs on the columns
     non-NaN in BOTH target and anchor, plus a prior-squared-diff
     contribution for each missing column, averaged across all 24 slots
     and sqrt'd. Thin-evidence anchors get pulled toward the random-match
     baseline so they can't outrank deep-evidence anchors by chance.
  3. For each sector, drop candidates that aren't in that sector's
     anchor_dates -- effective K varies per sector.
  4. For each candidate horizon in {5, 10, 20, 40}, pool conditional
     forward returns across the per-sector admissible candidates and
     compare against the per-sector unconditional pool via KS-2samp.
     Pick the horizon with the largest KS-stat.
  5. Emit JSON payload with target date, neighbor list (including each
     anchor's non-NaN column count), per-sector effective K, anchor-era
     distribution, picked horizon + divergence info, and per-sector path
     matrices ready for a cone-heatmap renderer downstream.

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
DECADE_LABELS      = ("1990s", "2000s", "2010s", "2020s")

# Expected squared difference between two independent z-scored values.
# Each column has unit variance after z-scoring, so for independent draws
# E[(z_a - z_b)^2] = Var(z_a) + Var(z_b) = 2. Used as the prior contribution
# for missing columns in the shrinkage distance below.
PRIOR_SQUARED_DIFF = 2.0
N_REGIME_COLUMNS   = 24


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
    """Returns dict[sector] -> (anchors, paths) where anchors and paths can
    have different lengths across sectors (per-sector admissibility)."""
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
    return out


# --- Distance --------------------------------------------------------------
def zscore_matrix(matrix, means, stds):
    """Z-score a (rows, cols) float matrix; NaN inputs stay NaN."""
    return (matrix - means) / stds


def shrunk_msd_distances(target_z, hist_z,
                          n_full=N_REGIME_COLUMNS,
                          prior=PRIOR_SQUARED_DIFF):
    """For each history row, distance = sqrt of evidence-weighted MSD.

    Counts only the columns non-NaN in BOTH target and the row (N_obs).
    Those columns contribute their observed squared diff. The remaining
    n_full - N_obs columns each contribute the prior squared diff (the
    expected value for two random independent z-scored values, = 2).
    Estimated MSD is the average across all n_full slots:

        estimated_MSD = (sum_of_observed_sq_diffs
                          + (n_full - N_obs) * prior) / n_full
        distance      = sqrt(estimated_MSD)

    This pulls thin-anchor distances toward the random-match baseline:
    matching well on a few cols can't beat matching decently on all of
    them. No arbitrary floor needed -- depth of evidence is built into
    the score directly.

    Anchors with N_obs == 0 still get a finite distance = sqrt(prior),
    equivalent to the random-match baseline; they just can't beat
    anchors with real evidence.

    target_z: shape (n_cols,)         may contain NaN
    hist_z:   shape (n_rows, n_cols)  may contain NaN
    """
    diffs = hist_z - target_z[None, :]
    sq = diffs ** 2
    n_obs = np.isfinite(sq).sum(axis=1)
    sum_sq = np.nansum(sq, axis=1)
    estimated = (sum_sq + (n_full - n_obs) * prior) / n_full
    return np.sqrt(estimated)


# --- K-NN ------------------------------------------------------------------
def find_neighbors(target_date, k=DEFAULT_K):
    """Return (neighbor_dates, distances, n_cols_per_neighbor) -- the K
    history dates closest to target under the shrinkage-weighted distance.

    No threshold filter. Depth of evidence is built into the distance via
    the prior contribution for missing columns (see shrunk_msd_distances).
    """
    target = pd.Timestamp(target_date).normalize()

    means, stds = load_normalization()
    history = load_history()
    history_dates = pd.DatetimeIndex(history["date"])

    target_vec = regime_vector(target).to_numpy(dtype="float64")
    target_z   = (target_vec - means) / stds

    hist_matrix = history[OUTPUT_COLUMNS].to_numpy(dtype="float64")
    hist_z      = zscore_matrix(hist_matrix, means, stds)

    dists = shrunk_msd_distances(target_z, hist_z)

    # Drop target's own row if present in history.
    if target in history_dates:
        self_pos = int(history_dates.get_loc(target))
        dists[self_pos] = np.inf

    # Per-row non-NaN column count comes from the mask columns in history.
    mask_cols = [f"mask_{c}" for c in OUTPUT_COLUMNS]
    if all(c in history.columns for c in mask_cols):
        n_cols_per_row = history[mask_cols].sum(axis=1).to_numpy(dtype="int64")
    else:
        n_cols_per_row = (~np.isnan(hist_matrix)).sum(axis=1).astype("int64")

    order = np.argsort(dists, kind="stable")[:k]
    neighbor_dates = history_dates[order]
    distances      = dists[order]
    n_cols         = n_cols_per_row[order]
    return neighbor_dates, distances, n_cols


# --- Per-sector admissibility ---------------------------------------------
def per_sector_positions(neighbor_dates, paths_by_sector):
    """For each sector, locate each neighbor date in that sector's
    anchor_dates. Returns dict[sector] -> array of positions for admissible
    neighbors (i.e. positions >= 0)."""
    out = {}
    for sector, (anchors, _) in paths_by_sector.items():
        pos = anchors.get_indexer(neighbor_dates)
        out[sector] = pos[pos >= 0]
    return out


def sector_effective_k(per_sector_pos):
    return {sector: int(len(pos)) for sector, pos in per_sector_pos.items()}


# --- Horizon picker --------------------------------------------------------
def _pool_returns_at_horizon(positions_per_sector, paths_by_sector, horizon):
    """Returns pooled return array across sectors at horizon-1 column.
    Each sector contributes len(positions) values."""
    col = horizon - 1  # 0-indexed
    chunks = []
    for sector, positions in positions_per_sector.items():
        arr = paths_by_sector[sector][1]
        if len(positions) > 0:
            chunks.append(arr[positions, col])
    if not chunks:
        return np.array([], dtype="float64")
    return np.concatenate(chunks)


def _unconditional_positions_per_sector(paths_by_sector):
    """For the unconditional pool, every anchor in each sector's anchor
    list contributes its forward return at the target horizon."""
    return {sector: np.arange(len(anchors))
            for sector, (anchors, _) in paths_by_sector.items()}


def pick_horizon(per_sector_neighbor_pos, paths_by_sector,
                 horizons=HORIZON_CANDIDATES):
    """For each horizon, KS-2samp between (conditional pool across
    per-sector admissible neighbors) and (unconditional pool across each
    sector's full anchor list). Return picked horizon + per-horizon stats.
    """
    uncond_pos = _unconditional_positions_per_sector(paths_by_sector)
    per_horizon = {}
    for h in horizons:
        cond_returns   = _pool_returns_at_horizon(per_sector_neighbor_pos,
                                                   paths_by_sector, h)
        uncond_returns = _pool_returns_at_horizon(uncond_pos,
                                                   paths_by_sector, h)
        if cond_returns.size == 0 or uncond_returns.size == 0:
            per_horizon[h] = {
                "ks_stat":         0.0,
                "ks_pvalue":       1.0,
                "n_conditional":   int(cond_returns.size),
                "n_unconditional": int(uncond_returns.size),
            }
            continue
        ks = ks_2samp(cond_returns, uncond_returns)
        per_horizon[h] = {
            "ks_stat":         float(ks.statistic),
            "ks_pvalue":       float(ks.pvalue),
            "n_conditional":   int(cond_returns.size),
            "n_unconditional": int(uncond_returns.size),
        }
    picked = max(horizons, key=lambda h: per_horizon[h]["ks_stat"])
    return picked, per_horizon


# --- Anchor-era distribution ----------------------------------------------
def _decade_label(d):
    year = pd.Timestamp(d).year
    if year < 2000:
        return "1990s"
    if year < 2010:
        return "2000s"
    if year < 2020:
        return "2010s"
    return "2020s"


def anchor_era_distribution(neighbor_dates):
    counts = {label: 0 for label in DECADE_LABELS}
    for d in neighbor_dates:
        counts[_decade_label(d)] += 1
    return counts


# --- Payload assembly ------------------------------------------------------
def build_payload(target_date, k=DEFAULT_K):
    target = pd.Timestamp(target_date).normalize()

    paths_by_sector = load_forward_paths()
    neighbor_dates, distances, n_cols = find_neighbors(target, k=k)

    per_sector_pos = per_sector_positions(neighbor_dates, paths_by_sector)
    eff_k          = sector_effective_k(per_sector_pos)

    picked, per_horizon = pick_horizon(per_sector_pos, paths_by_sector)

    # Per-sector forward-path slices at the picked horizon.
    sector_paths = {}
    for sector, positions in per_sector_pos.items():
        arr = paths_by_sector[sector][1]
        if len(positions) == 0:
            sector_paths[sector] = []
        else:
            sliced = arr[positions, :picked]
            sector_paths[sector] = sliced.tolist()

    payload = {
        "target_date":           target.strftime("%Y-%m-%d"),
        "k":                     k,
        "picked_horizon":        picked,
        "divergence_metric":     "ks_2samp",
        "divergence_by_horizon": per_horizon,
        "neighbors": [
            {"date": d.strftime("%Y-%m-%d"),
             "distance": float(dist),
             "n_cols_non_nan": int(n)}
            for d, dist, n in zip(neighbor_dates, distances, n_cols)
        ],
        "sector_effective_k":       eff_k,
        "anchor_era_distribution":  anchor_era_distribution(neighbor_dates),
        "sector_paths":             sector_paths,
        "bars_forward":             list(range(1, picked + 1)),
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
    """Latest date in the regime-vector history parquet. The MSD-over-
    intersection distance handles partial-NaN targets cleanly, so there
    is no need to filter to fully-available rows."""
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

    print(f"\n  Per-sector effective K:")
    for sector, eff in payload["sector_effective_k"].items():
        print(f"    {sector:<5s}  {eff:>3d}/{payload['k']}")

    print(f"\n  Anchor-era distribution:")
    for label, cnt in payload["anchor_era_distribution"].items():
        print(f"    {label}: {cnt}")

    print(f"\n  Top 10 neighbors:")
    for n in payload["neighbors"][:10]:
        print(f"    {n['date']}  d={n['distance']:.4f}  "
              f"cols={n['n_cols_non_nan']}/24")

    size_kb = write_payload(payload, TODAY_OUTPUT_JSON)
    print(f"\n  Wrote {size_kb:.1f} KB -> {TODAY_OUTPUT_JSON}")

    print("\n  DONE.")


if __name__ == "__main__":
    main()
