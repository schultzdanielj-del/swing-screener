"""Fixed BASE-only scan:
 - Bands built from scan-path lr computation (no more float precision mismatch).
 - Output keyed by SIGNAL BAR (E-1), not entry bar E.
 - No-sigma [min_i lr_i, max_i lr_i] bands.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import presignal_sma_band_extract as ext

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
TRAJ_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band")
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")

SETUP = "bf"
DATE_CUTOFF = "2020-01-02"
MIN_TICKER_COUNT = 11000


# ─────────── scan-path lr extraction (single bar) ───────────

def scan_lr_single(tc, E, N_daily, W_N, w_cells):
    """Return (d_lr (21, N_daily), w_lr (15, W_N)) using the same compute path
    that scan_lr_batch uses. For building bands consistent with the scan."""
    close = tc["close"]; L = tc["L"]
    if E < 1 or E >= L:
        return None, None
    if not (np.isfinite(close[E - 1]) and close[E - 1] > 0):
        return None, None

    # DAILY
    d_log_ma = tc["d_log_ma"]
    d_log_anchor = d_log_ma[:, E - 1]
    ks = np.arange(1, N_daily + 1, dtype=np.int64)
    src = E - ks
    valid_src = src >= 0
    src_safe = np.where(valid_src, src, 0)
    d_log_at_src = d_log_ma[:, src_safe]
    d_log_at_src = np.where(valid_src[None, :], d_log_at_src, np.nan)
    d_lr = d_log_at_src - d_log_anchor[:, None]

    # WEEKLY
    anchor_close = close[E - 1]
    w_pos_all = tc["w_pos_all"]
    anchor_pos = int(w_pos_all[E - 1])
    n_weeks = tc["n_weeks"]
    weekly_close = tc["weekly_close"]
    w_log_ma = tc["w_log_ma"]
    n_w_ma = len(w_cells)

    wc_finite = np.isfinite(weekly_close) & (weekly_close > 0)
    wc_safe = np.where(wc_finite, weekly_close, 0.0)
    wc_cumsum = np.concatenate([[0.0], np.cumsum(wc_safe)])
    wc_okcount = np.concatenate([[0], np.cumsum(wc_finite.astype(np.int64))])

    patched_log_anchor = np.full(n_w_ma, np.nan, dtype=np.float64)
    for m, (fam, p) in enumerate(w_cells):
        if fam == 'SMA':
            lo_idx = anchor_pos - p + 1
            valid_range = (lo_idx >= 0) and (anchor_pos < n_weeks)
            if valid_range:
                sum_prior = wc_cumsum[anchor_pos] - wc_cumsum[lo_idx]
                count_prior = wc_okcount[anchor_pos] - wc_okcount[lo_idx]
                if count_prior == (p - 1):
                    patched = (sum_prior + anchor_close) / p
                    if patched > 0:
                        patched_log_anchor[m] = np.log(patched)
        else:  # EMA
            prev_pos = anchor_pos - 1
            if 0 <= prev_pos < n_weeks:
                prev_log = w_log_ma[m, prev_pos]
                if np.isfinite(prev_log):
                    prev_ema = np.exp(prev_log)
                    alpha = 2.0 / (p + 1.0)
                    patched = alpha * anchor_close + (1 - alpha) * prev_ema
                    if patched > 0:
                        patched_log_anchor[m] = np.log(patched)

    w_lr = np.full((n_w_ma, W_N), np.nan, dtype=np.float64)
    mask_anchor = np.isfinite(patched_log_anchor)
    w_lr[mask_anchor, 0] = 0.0
    for k in range(1, W_N):
        wpos = anchor_pos - k
        if 0 <= wpos < n_weeks:
            w_lr[:, k] = w_log_ma[:, wpos] - patched_log_anchor
    return d_lr, w_lr


# ─────────── scan-path lr extraction (vectorized over eligible bars) ───────────

def scan_ticker_fast(tc, N_daily, W_N, d_up, d_lo, w_up, w_lo, dates_str, w_cells,
                    example_E_list=None):
    """Returns np.int64 array of passing E values.
    Uses SAME lr computation as scan_lr_single → band consistency.
    If example_E_list provided, forces those E values into eligibility (regardless of cutoff).
    """
    close = tc["close"]; L = tc["L"]
    if L < 2:
        return np.array([], dtype=np.int64)
    eligible_mask = (np.arange(L) >= 1) & (dates_str >= DATE_CUTOFF)
    if example_E_list is not None:
        for e in example_E_list:
            if 1 <= e < L:
                eligible_mask[e] = True
    eligible = np.where(eligible_mask)[0]
    if len(eligible) == 0:
        return np.array([], dtype=np.int64)
    c_Em1 = close[eligible - 1]
    valid = np.isfinite(c_Em1) & (c_Em1 > 0)
    eligible = eligible[valid]
    if len(eligible) == 0:
        return np.array([], dtype=np.int64)
    n_elig = len(eligible)

    # DAILY
    d_log_ma = tc["d_log_ma"]
    d_log_anchor = d_log_ma[:, eligible - 1]  # (21, n_elig)
    ks = np.arange(1, N_daily + 1, dtype=np.int64)
    src_mat = eligible[:, None] - ks[None, :]
    valid_src = src_mat >= 0
    src_safe = np.where(valid_src, src_mat, 0)
    d_log_at_src = d_log_ma[:, src_safe]
    d_log_at_src = np.where(valid_src[None, :, :], d_log_at_src, np.nan)
    d_lr = d_log_at_src - d_log_anchor[:, :, None]  # (21, n_elig, N_daily)
    nan_val = np.isnan(d_lr)
    nan_band = np.isnan(d_up[:, None, :]) | np.isnan(d_lo[:, None, :])
    with np.errstate(invalid='ignore'):
        in_band = (d_lr >= d_lo[:, None, :]) & (d_lr <= d_up[:, None, :])
    d_cell_pass = nan_val | nan_band | in_band
    d_pass = d_cell_pass.all(axis=(0, 2))
    eligible = eligible[d_pass]
    if len(eligible) == 0:
        return np.array([], dtype=np.int64)

    # WEEKLY
    w_pos_all = tc["w_pos_all"]
    w_log_ma = tc["w_log_ma"]
    n_weeks = tc["n_weeks"]
    weekly_close = tc["weekly_close"]
    anchor_pos = w_pos_all[eligible - 1].astype(np.int64)
    anchor_close = close[eligible - 1]
    n_elig = len(eligible)
    n_w_ma = len(w_cells)

    wc_finite = np.isfinite(weekly_close) & (weekly_close > 0)
    wc_safe = np.where(wc_finite, weekly_close, 0.0)
    wc_cumsum = np.concatenate([[0.0], np.cumsum(wc_safe)])
    wc_okcount = np.concatenate([[0], np.cumsum(wc_finite.astype(np.int64))])

    patched_log_anchor = np.full((n_w_ma, n_elig), np.nan, dtype=np.float64)
    for m, (fam, p) in enumerate(w_cells):
        if fam == 'SMA':
            lo_idx = anchor_pos - p + 1
            valid_range = (lo_idx >= 0) & (anchor_pos < n_weeks)
            lo_safe = np.where(valid_range, lo_idx, 0)
            hi_safe = np.where(valid_range, anchor_pos, 0)
            sum_prior = wc_cumsum[hi_safe] - wc_cumsum[lo_safe]
            count_prior = wc_okcount[hi_safe] - wc_okcount[lo_safe]
            all_ok = valid_range & (count_prior == (p - 1))
            patched = np.where(all_ok, (sum_prior + anchor_close) / p, np.nan)
        else:
            prev_pos = anchor_pos - 1
            valid_prev = (prev_pos >= 0) & (prev_pos < n_weeks)
            prev_safe = np.where(valid_prev, prev_pos, 0)
            prev_log = w_log_ma[m, prev_safe]
            prev_log = np.where(valid_prev, prev_log, np.nan)
            prev_ema = np.exp(prev_log)
            alpha = 2.0 / (p + 1.0)
            patched = alpha * anchor_close + (1 - alpha) * prev_ema
        pat_ok = np.isfinite(patched) & (patched > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            patched_log_anchor[m, pat_ok] = np.log(patched[pat_ok])

    # Weekly k=0 band is trivial [0,0]; candidate lr at k=0 is 0 or NaN → always passes. Skip.
    if W_N > 1:
        ks_w = np.arange(1, W_N, dtype=np.int64)
        src_mat_w = anchor_pos[:, None] - ks_w[None, :]
        valid_src_w = (src_mat_w >= 0) & (src_mat_w < n_weeks)
        src_safe_w = np.where(valid_src_w, src_mat_w, 0)
        w_log_at_src = w_log_ma[:, src_safe_w]
        w_log_at_src = np.where(valid_src_w[None, :, :], w_log_at_src, np.nan)
        w_lr = w_log_at_src - patched_log_anchor[:, :, None]

        w_up_slice = w_up[:, 1:W_N]; w_lo_slice = w_lo[:, 1:W_N]
        nan_val_w = np.isnan(w_lr)
        nan_band_w = np.isnan(w_up_slice[:, None, :]) | np.isnan(w_lo_slice[:, None, :])
        with np.errstate(invalid='ignore'):
            in_band_w = (w_lr >= w_lo_slice[:, None, :]) & (w_lr <= w_up_slice[:, None, :])
        w_cell_pass = nan_val_w | nan_band_w | in_band_w
        w_pass = w_cell_pass.all(axis=(0, 2))
        eligible = eligible[w_pass]
    return eligible.astype(np.int64)


# ─────────── main ───────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)

    # Load Phase 1 for metadata only (N_daily, W_N, w_cells, examples list)
    with open(os.path.join(TRAJ_DIR, f"{SETUP}_trajectories.pkl"), "rb") as f:
        traj = pickle.load(f)
    N_daily = traj["N_daily"]; W_N = traj["W_N"]
    w_cells = traj["weekly_cells"]
    ex_list = traj["examples"]  # {ticker, entry_date, E_idx}

    # ── Build bands from scan-path lr (consistency with scan) ──
    print(f"\nBuilding bands via scan-path lr for {len(ex_list)} examples...", flush=True)
    tcache = {}
    d_lrs, w_lrs = [], []
    kept_ex = []
    for ex in ex_list:
        tk = ex["ticker"]; E = ex["E_idx"]
        df = universe.get(tk)
        if df is None:
            print(f"  {tk} not in universe", flush=True)
            continue
        if tk not in tcache:
            tcache[tk] = ext.build_ticker_cache(df)
        tc = tcache[tk]
        d_lr, w_lr = scan_lr_single(tc, E, N_daily, W_N, w_cells)
        if d_lr is None:
            print(f"  {tk}/{ex['entry_date']} E={E} invalid", flush=True)
            continue
        d_lrs.append(d_lr); w_lrs.append(w_lr); kept_ex.append(ex)
    d_lr_arr = np.stack(d_lrs)  # (n_ex, 21, N_daily)
    w_lr_arr = np.stack(w_lrs)
    with np.errstate(invalid='ignore'):
        d_up = np.nanmax(d_lr_arr, axis=0); d_lo = np.nanmin(d_lr_arr, axis=0)
        w_up = np.nanmax(w_lr_arr, axis=0); w_lo = np.nanmin(w_lr_arr, axis=0)
    print(f"  kept {len(kept_ex)} examples; bands daily {d_up.shape}, weekly {w_up.shape}", flush=True)

    # ── Pre-flight EX check: every example must pass bands ──
    print("\nPre-flight EX pass check (against freshly-built bands)...", flush=True)
    def cell_pass(lr, up, lo):
        nv = np.isnan(lr); nb = np.isnan(up) | np.isnan(lo)
        with np.errstate(invalid='ignore'):
            ib = (lr >= lo) & (lr <= up)
        return nv | nb | ib
    n_pass = 0
    for i, ex in enumerate(kept_ex):
        dp = cell_pass(d_lr_arr[i], d_up, d_lo).all()
        wp = cell_pass(w_lr_arr[i], w_up, w_lo).all()
        if dp and wp:
            n_pass += 1
        else:
            print(f"  FAIL: {ex['ticker']}/{ex['entry_date']} daily_all={dp} weekly_all={wp}", flush=True)
    print(f"  {n_pass}/{len(kept_ex)} examples pass their own bands (should be 100%)", flush=True)
    if n_pass != len(kept_ex):
        print("  *** BAND CONSTRUCTION BUG — aborting ***", flush=True)
        return

    # ── Universe scan ──
    print(f"\nScan {SETUP.upper()}  N_daily={N_daily}  W_N={W_N}  {len(universe):,} tickers", flush=True)
    # Pre-compute per-ticker example E list
    ex_by_ticker = {}
    for ex in kept_ex:
        ex_by_ticker.setdefault(ex["ticker"], []).append(ex["E_idx"])

    t0 = time.time()
    by_ticker_passes = {}  # ticker -> list of entry E (before signal-bar rekey)
    for idx, tk in enumerate(universe, 1):
        df = universe[tk]
        if tk not in tcache:
            tcache[tk] = ext.build_ticker_cache(df)
        tc = tcache[tk]
        ds = vsc.dates_as_str(df)
        ex_Es = ex_by_ticker.get(tk)
        passes = scan_ticker_fast(tc, N_daily, W_N, d_up, d_lo, w_up, w_lo, ds, w_cells,
                                  example_E_list=ex_Es)
        if len(passes):
            by_ticker_passes[tk] = passes.tolist()
        if idx % 1000 == 0:
            n = sum(len(v) for v in by_ticker_passes.values())
            print(f"  {idx}/{len(universe):,}  passes={n:,}  ({time.time()-t0:.1f}s)", flush=True)
    pre_dedup = sum(len(v) for v in by_ticker_passes.values())
    print(f"  pre-dedup passes (entry-bar keyed): {pre_dedup:,}  ({time.time()-t0:.1f}s)", flush=True)

    # ── Rekey output to signal bar (E-1) ──
    by_ticker_signal = {tk: [E - 1 for E in E_list] for tk, E_list in by_ticker_passes.items()}
    example_signal_keys = {(ex["ticker"], ex["E_idx"] - 1) for ex in kept_ex}

    # ── Dedup (on signal bars) ──
    dedup_total = 0
    ex_covered = 0
    dedup_by_ticker_signal = {}
    for tk, S_list in by_ticker_signal.items():
        ex_sigs = {s for (t, s) in example_signal_keys if t == tk}
        deduped = vsc.dedupe_clusters(sorted(S_list), ex_sigs)
        dedup_total += len(deduped)
        ex_covered += sum(1 for s in ex_sigs if s in deduped)
        if deduped:
            dedup_by_ticker_signal[tk] = deduped
    print(f"  post-dedup clusters (signal-bar keyed): {dedup_total:,}", flush=True)
    print(f"  EX end-to-end: {ex_covered}/{len(kept_ex)}", flush=True)
    ex_pre = sum(1 for (tk, s) in example_signal_keys if tk in by_ticker_signal and s in by_ticker_signal[tk])
    print(f"  EX pre-dedup pass: {ex_pre}/{len(kept_ex)}", flush=True)

    summary = {
        "setup": SETUP,
        "N_daily": N_daily, "W_N": W_N,
        "n_ex": len(kept_ex),
        "pre_dedup_passes": pre_dedup,
        "post_dedup_clusters": dedup_total,
        "ex_pre_dedup_pass": ex_pre,
        "ex_end_to_end": ex_covered,
        "key_semantic": "signal_bar (E-1)",
        "scan_seconds": time.time() - t0,
    }
    out = os.path.join(OUT_DIR, f"{SETUP}_passes_fixed.pkl")
    with open(out, "wb") as f:
        pickle.dump({
            "setup": SETUP,
            "pre_dedup_by_ticker": by_ticker_signal,
            "dedup_by_ticker": dedup_by_ticker_signal,
            "example_signal_keys": list(example_signal_keys),
            "examples": kept_ex,
            "summary": summary,
        }, f)
    print(f"\nsaved {out}", flush=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
