"""Quick empirical test: rebuild full-N bands with sigma dropped, measure admission on
the same 5000 market sample from the ranker run. Direct A/B numeric comparison.

Band construction:
  With sigma (current spec):  upper = max_i(lr_i + sigma_i),  lower = min_i(lr_i - sigma_i)
  No sigma (option 1):         upper = max_i(lr_i),            lower = min_i(lr_i)
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TRAJ_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/research/presignal_sma_band"
RANKER_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/research/presignal_sma_band_ranker"

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]


def flatten(tensor_d, tensor_w):
    n = tensor_d.shape[0]
    N_d = tensor_d.shape[2]; W_N = tensor_w.shape[2]
    C_d = tensor_d.shape[1] * N_d; C_w = tensor_w.shape[1] * W_N
    out = np.full((n, C_d + C_w), np.nan, dtype=np.float64)
    out[:, :C_d] = tensor_d.reshape(n, C_d)
    out[:, C_d:] = tensor_w.reshape(n, C_w)
    return out


def admission_for_bands(mkt_lr_flat, upper, lower):
    """Strict-AND, NaN-lenient. Returns admission rate."""
    nan_val = np.isnan(mkt_lr_flat)
    nan_band = np.isnan(upper) | np.isnan(lower)
    with np.errstate(invalid='ignore'):
        in_band = (mkt_lr_flat >= lower[None, :]) & (mkt_lr_flat <= upper[None, :])
    passes_per_cell = nan_val | nan_band[None, :] | in_band
    return passes_per_cell.all(axis=1).mean()


def ex_coverage(ex_lr_flat, upper, lower):
    """Does every example pass?  Should be 1.0 for both variants by construction."""
    nan_val = np.isnan(ex_lr_flat)
    nan_band = np.isnan(upper) | np.isnan(lower)
    with np.errstate(invalid='ignore'):
        in_band = (ex_lr_flat >= lower[None, :]) & (ex_lr_flat <= upper[None, :])
    passes_per_cell = nan_val | nan_band[None, :] | in_band
    return passes_per_cell.all(axis=1).mean()


def main():
    # Load market sample used in the ranker run.
    # The ranker saved per-setup ranker.pkl but not the raw market matrices; I need to
    # re-read mkt_d, mkt_w from somewhere. Load the first ranker.pkl to pull market_meta,
    # then re-extract market from the same tickers+E to reuse the same seed=42 sample.
    # Simpler: use the ranker's admission at K=C (saved in ranker.pkl) for the with-sigma
    # baseline, and re-do the no-sigma case by re-running extraction on the same 5000 bars.
    # Market_meta is saved in ranker.pkl.
    import pickle as _pickle
    import visual_shape_compare as vsc
    import presignal_sma_band_extract as ext
    MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
    CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = _pickle.load(f)

    # Get market meta from htf ranker
    with open(os.path.join(RANKER_DIR, "htf_ranker.pkl"), "rb") as f:
        htf_rk = _pickle.load(f)
    mkt_meta = htf_rk["market_meta"]
    print(f"Market sample from ranker: {len(mkt_meta)} bars", flush=True)

    # Re-extract at max dims
    N_daily_max = 86
    W_N_max = 48
    tcache = {}
    d_list, w_list = [], []
    for m in mkt_meta:
        tk = m["ticker"]; E = int(m["E_idx"])
        df = universe[tk]
        if tk not in tcache:
            tcache[tk] = ext.build_ticker_cache(df)
        tc = tcache[tk]
        d_lr, _ = ext.extract_daily(tc, E, N_daily_max)
        w_lr, _ = ext.extract_weekly(tc, E, W_N_max)
        d_list.append(d_lr)
        w_list.append(w_lr)
    mkt_d_max = np.stack(d_list)
    mkt_w_max = np.stack(w_list)
    print(f"  daily {mkt_d_max.shape}  weekly {mkt_w_max.shape}", flush=True)

    # Per setup: compare
    print()
    print(f"{'setup':<8} {'N_daily':>7} {'W_N':>4} {'C':>6}  {'adm_with':>10}  {'adm_no':>10}  {'ex_with':>8} {'ex_no':>8}")
    print("-" * 80)
    for setup in SETUPS:
        with open(os.path.join(TRAJ_DIR, f"{setup}_trajectories.pkl"), "rb") as f:
            traj = _pickle.load(f)
        d_lr = traj["daily_logratio"]; d_sig = traj["daily_sigma"]
        w_lr = traj["weekly_logratio"]; w_sig = traj["weekly_sigma"]
        N_daily = traj["N_daily"]; W_N = traj["W_N"]

        # With-sigma bands
        with np.errstate(invalid='ignore'):
            d_up_with = np.nanmax(d_lr + d_sig, axis=0)
            d_lo_with = np.nanmin(d_lr - d_sig, axis=0)
            w_up_with = np.nanmax(w_lr + w_sig, axis=0)
            w_lo_with = np.nanmin(w_lr - w_sig, axis=0)
            d_up_no = np.nanmax(d_lr, axis=0)
            d_lo_no = np.nanmin(d_lr, axis=0)
            w_up_no = np.nanmax(w_lr, axis=0)
            w_lo_no = np.nanmin(w_lr, axis=0)

        # Flatten bands (daily first 21*N, then weekly 15*W_N)
        upper_with = np.concatenate([d_up_with.ravel(), w_up_with.ravel()])
        lower_with = np.concatenate([d_lo_with.ravel(), w_lo_with.ravel()])
        upper_no = np.concatenate([d_up_no.ravel(), w_up_no.ravel()])
        lower_no = np.concatenate([d_lo_no.ravel(), w_lo_no.ravel()])

        # Slice market to this setup's dims and flatten
        mkt_d = mkt_d_max[:, :, :N_daily]
        mkt_w = mkt_w_max[:, :, :W_N]
        mkt_flat = flatten(mkt_d, mkt_w)

        # Example flat
        ex_flat = flatten(d_lr, w_lr)

        adm_with = admission_for_bands(mkt_flat, upper_with, lower_with)
        adm_no = admission_for_bands(mkt_flat, upper_no, lower_no)
        ex_with = ex_coverage(ex_flat, upper_with, lower_with)
        ex_no = ex_coverage(ex_flat, upper_no, lower_no)

        C = len(upper_with)
        print(f"{setup:<8} {N_daily:>7} {W_N:>4} {C:>6}  {adm_with:>10.4%}  {adm_no:>10.4%}  {ex_with:>8.0%} {ex_no:>8.0%}")

    # Calibration spread comparison
    print()
    print("Spreads:")
    # Collect values
    results_with = {}
    results_no = {}
    for setup in SETUPS:
        with open(os.path.join(TRAJ_DIR, f"{setup}_trajectories.pkl"), "rb") as f:
            traj = _pickle.load(f)
        d_lr = traj["daily_logratio"]; d_sig = traj["daily_sigma"]
        w_lr = traj["weekly_logratio"]; w_sig = traj["weekly_sigma"]
        N_daily = traj["N_daily"]; W_N = traj["W_N"]
        with np.errstate(invalid='ignore'):
            d_up_with = np.nanmax(d_lr + d_sig, axis=0); d_lo_with = np.nanmin(d_lr - d_sig, axis=0)
            w_up_with = np.nanmax(w_lr + w_sig, axis=0); w_lo_with = np.nanmin(w_lr - w_sig, axis=0)
            d_up_no = np.nanmax(d_lr, axis=0); d_lo_no = np.nanmin(d_lr, axis=0)
            w_up_no = np.nanmax(w_lr, axis=0); w_lo_no = np.nanmin(w_lr, axis=0)
        upper_with = np.concatenate([d_up_with.ravel(), w_up_with.ravel()])
        lower_with = np.concatenate([d_lo_with.ravel(), w_lo_with.ravel()])
        upper_no = np.concatenate([d_up_no.ravel(), w_up_no.ravel()])
        lower_no = np.concatenate([d_lo_no.ravel(), w_lo_no.ravel()])
        mkt_d = mkt_d_max[:, :, :N_daily]; mkt_w = mkt_w_max[:, :, :W_N]
        mkt_flat = flatten(mkt_d, mkt_w)
        results_with[setup] = admission_for_bands(mkt_flat, upper_with, lower_with)
        results_no[setup] = admission_for_bands(mkt_flat, upper_no, lower_no)
    spread_with = max(results_with.values()) / max(min(results_with.values()), 1e-6)
    spread_no = max(results_no.values()) / max(min(results_no.values()), 1e-6)
    print(f"  with sigma: max/min = {spread_with:.1f}x")
    print(f"  no sigma:   max/min = {spread_no:.1f}x")


if __name__ == "__main__":
    main()
