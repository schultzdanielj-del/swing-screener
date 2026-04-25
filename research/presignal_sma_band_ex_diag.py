"""Diagnose the EX coverage bug. For each missing example, determine which cell fails
and compare scan-computed lr to Phase 1's stored lr for that example.
"""
from __future__ import annotations

import os
import pickle
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import presignal_sma_band_extract as ext

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
TRAJ_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band")
SCAN_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")

SETUP = "htf"


def build_bands_no_sigma(traj):
    d_lr = traj["daily_logratio"]
    w_lr = traj["weekly_logratio"]
    with np.errstate(invalid='ignore'):
        d_up = np.nanmax(d_lr, axis=0); d_lo = np.nanmin(d_lr, axis=0)
        w_up = np.nanmax(w_lr, axis=0); w_lo = np.nanmin(w_lr, axis=0)
    return d_up, d_lo, w_up, w_lo


def scan_one(tc, E, N_daily, W_N, w_cells):
    """Return (daily_lr (21, N_daily), weekly_lr (15, W_N)) computed the way scan_ticker_fast does.
    Returns None if E invalid."""
    close = tc["close"]; L = tc["L"]
    if E < 1 or E >= L:
        return None, None
    if not (np.isfinite(close[E - 1]) and close[E - 1] > 0):
        return None, None

    # DAILY — same logic as scan_ticker_fast
    d_log_ma = tc["d_log_ma"]  # (21, L)
    d_log_anchor = d_log_ma[:, E - 1]  # (21,)
    ks = np.arange(1, N_daily + 1, dtype=np.int64)
    src = E - ks  # (N_daily,)
    valid_src = src >= 0
    src_safe = np.where(valid_src, src, 0)
    d_log_at_src = d_log_ma[:, src_safe]  # (21, N_daily)
    d_log_at_src = np.where(valid_src[None, :], d_log_at_src, np.nan)
    d_lr_out = d_log_at_src - d_log_anchor[:, None]  # (21, N_daily)

    # WEEKLY
    anchor_close = close[E - 1]
    w_pos_all = tc["w_pos_all"]
    anchor_pos = int(w_pos_all[E - 1])
    n_weeks = tc["n_weeks"]
    weekly_close = tc["weekly_close"]
    w_log_ma = tc["w_log_ma"]  # (15, n_weeks)
    n_w_ma = len(w_cells)

    wc_finite = np.isfinite(weekly_close) & (weekly_close > 0)
    wc_safe = np.where(wc_finite, weekly_close, 0.0)
    wc_cumsum = np.concatenate([[0.0], np.cumsum(wc_safe)])
    wc_okcount = np.concatenate([[0], np.cumsum(wc_finite.astype(np.int64))])

    patched_log_anchor = np.full(n_w_ma, np.nan, dtype=np.float64)
    for m, (fam, p) in enumerate(w_cells):
        if fam == 'SMA':
            lo_idx = anchor_pos - p + 1
            valid_range = (lo_idx >= 0) & (anchor_pos < n_weeks)
            if valid_range:
                sum_prior = wc_cumsum[anchor_pos] - wc_cumsum[lo_idx]
                count_prior = wc_okcount[anchor_pos] - wc_okcount[lo_idx]
                if count_prior == (p - 1):
                    patched = (sum_prior + anchor_close) / p
                    if patched > 0:
                        patched_log_anchor[m] = np.log(patched)
        else:
            prev_pos = anchor_pos - 1
            if 0 <= prev_pos < n_weeks:
                prev_log = w_log_ma[m, prev_pos]
                if np.isfinite(prev_log):
                    prev_ema = np.exp(prev_log)
                    alpha = 2.0 / (p + 1.0)
                    patched = alpha * anchor_close + (1 - alpha) * prev_ema
                    if patched > 0:
                        patched_log_anchor[m] = np.log(patched)

    # Weekly lr: k=0 trivially 0 (when patched finite); k>=1 from w_log_ma
    w_lr_out = np.full((n_w_ma, W_N), np.nan, dtype=np.float64)
    mask_anchor = np.isfinite(patched_log_anchor)
    w_lr_out[mask_anchor, 0] = 0.0
    for k in range(1, W_N):
        wpos = anchor_pos - k
        if 0 <= wpos < n_weeks:
            w_lr_out[:, k] = w_log_ma[:, wpos] - patched_log_anchor
    return d_lr_out, w_lr_out


def main():
    print(f"Loading {SETUP} trajectories + scan passes...", flush=True)
    with open(os.path.join(TRAJ_DIR, f"{SETUP}_trajectories.pkl"), "rb") as f:
        traj = pickle.load(f)
    with open(os.path.join(SCAN_DIR, f"{SETUP}_passes.pkl"), "rb") as f:
        scan = pickle.load(f)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    d_up, d_lo, w_up, w_lo = build_bands_no_sigma(traj)
    N_daily = traj["N_daily"]; W_N = traj["W_N"]
    d_cells = traj["daily_cells"]; w_cells = traj["weekly_cells"]
    examples = traj["examples"]  # list of {ticker, entry_date, E_idx}
    pre_dedup = scan["pre_dedup_by_ticker"]

    # Find missing examples
    missing = []
    passing = []
    for ex in examples:
        tk = ex["ticker"]; E = ex["E_idx"]
        if tk in pre_dedup and E in set(pre_dedup[tk]):
            passing.append(ex)
        else:
            missing.append(ex)
    print(f"{SETUP}: {len(passing)} passing, {len(missing)} missing\n", flush=True)

    # For each missing example, re-scan and identify failing cells
    for ex_i, ex in enumerate(missing):
        tk = ex["ticker"]; E = ex["E_idx"]; ed = ex["entry_date"]
        df = universe.get(tk)
        if df is None:
            print(f"{tk}/{ed} E={E}: ticker NOT in universe")
            continue
        tc = ext.build_ticker_cache(df)
        d_lr, w_lr = scan_one(tc, E, N_daily, W_N, w_cells)
        if d_lr is None:
            print(f"{tk}/{ed} E={E}: scan_one returned None (invalid E or anchor)")
            continue

        # Compare to Phase 1 stored
        p1_idx = next((i for i, e in enumerate(examples) if e["ticker"] == tk and e["E_idx"] == E), None)
        p1_d = traj["daily_logratio"][p1_idx] if p1_idx is not None else None
        p1_w = traj["weekly_logratio"][p1_idx] if p1_idx is not None else None

        # Which cells fail scan vs band (no-sigma, NaN-lenient)
        def cell_fail(lr, up, lo):
            nan_val = np.isnan(lr)
            nan_band = np.isnan(up) | np.isnan(lo)
            with np.errstate(invalid='ignore'):
                in_band = (lr >= lo) & (lr <= up)
            return ~(nan_val | nan_band | in_band)

        d_fail = cell_fail(d_lr, d_up, d_lo)
        w_fail = cell_fail(w_lr, w_up, w_lo)

        n_d_fail = int(d_fail.sum()); n_w_fail = int(w_fail.sum())
        print(f"=== {tk}/{ed} E={E}  daily_fail={n_d_fail}  weekly_fail={n_w_fail}")

        # For up to 3 fails in each, print details
        if n_d_fail > 0:
            mi, ki = np.where(d_fail)
            for j in range(min(3, n_d_fail)):
                m = int(mi[j]); k = int(ki[j])
                fam, p = d_cells[m]
                scan_v = d_lr[m, k]
                p1_v = p1_d[m, k] if p1_d is not None else float('nan')
                diff = scan_v - p1_v if np.isfinite(scan_v) and np.isfinite(p1_v) else float('nan')
                print(f"  daily FAIL  {fam}{p}@k={k+1}  scan_lr={scan_v:.6f}  phase1_lr={p1_v:.6f}  diff={diff:.6f}")
                print(f"             band=[{d_lo[m,k]:.6f}, {d_up[m,k]:.6f}]  width={d_up[m,k]-d_lo[m,k]:.6f}")
        if n_w_fail > 0:
            mi, ki = np.where(w_fail)
            for j in range(min(3, n_w_fail)):
                m = int(mi[j]); k = int(ki[j])
                fam, p = w_cells[m]
                scan_v = w_lr[m, k]
                p1_v = p1_w[m, k] if p1_w is not None else float('nan')
                diff = scan_v - p1_v if np.isfinite(scan_v) and np.isfinite(p1_v) else float('nan')
                print(f"  weekly FAIL {fam}{p}@k={k}  scan_lr={scan_v!r}  phase1_lr={p1_v!r}")
                print(f"             band.up={w_up[m,k]!r}  band.lo={w_lo[m,k]!r}")
                print(f"             scan_lr - band_up = {scan_v - w_up[m,k]!r}    scan_lr - phase1_lr = {scan_v - p1_v!r}")
        print()


if __name__ == "__main__":
    main()
