"""§4 SMA-band presignal grinder — Phase 1: multi-MA trajectory extraction per example.

For each setup, for each labelled example, extracts:
  - Anchored log-ratio: log(MA_p[E-k] / MA_p[E-1]) daily, log(MA_p[W-k] / MA_p[W-0]) weekly.
  - Per-example sigma_i(k): rolling SD of log(MA_p) over p bars at bar E-k (daily) / W-k (weekly).

Scales:
  - Daily MA types: SMA {5,8,10,13,20,30,50,100,150,200} + EMA {3,5,8,10,13,20,30,50,100,150,200}.
  - Weekly MA types: SMA {5,8,10,13,20,30,50} + EMA {3,5,8,10,13,20,30,50}.

Per-setup N values:
  - Daily N: research/n_derivation_cache/{setup}_summary.json  field N_bars.
  - Weekly W_N: research/presignal_weekly_stage_a/{setup}_stage_a.json  field W_N.

Output: research/presignal_sma_band/{setup}_trajectories.pkl.
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
import presignal_weekly_aggregate as wagg

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
N_DERIV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]

SMA_DAILY = [5, 8, 10, 13, 20, 30, 50, 100, 150, 200]
EMA_DAILY = [3, 5, 8, 10, 13, 20, 30, 50, 100, 150, 200]
SMA_WEEKLY = [5, 8, 10, 13, 20, 30, 50]
EMA_WEEKLY = [3, 5, 8, 10, 13, 20, 30, 50]

MIN_TICKER_COUNT = 11000


# ───────────── MA compute ─────────────

def rolling_sma(close, p):
    L = len(close)
    c = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(c), c, 0.0))])
    co = np.concatenate([[0], np.cumsum(np.isfinite(c).astype(np.int64))])
    out = np.full(L, np.nan)
    if L < p:
        return out
    t_arr = np.arange(p - 1, L)
    lo_arr = t_arr - p + 1
    ok = co[t_arr + 1] - co[lo_arr]
    s = cs[t_arr + 1] - cs[lo_arr]
    out[t_arr] = np.where(ok == p, s / p, np.nan)
    return out


def rolling_ema(close, p):
    L = len(close)
    c = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    alpha = 2.0 / (p + 1.0)
    ema = np.full(L, np.nan)
    first = -1
    for t in range(L):
        if np.isfinite(c[t]):
            first = t
            break
    if first < 0:
        return ema
    ema[first] = c[first]
    for t in range(first + 1, L):
        v = c[t]
        prev = ema[t - 1]
        if np.isfinite(v) and np.isfinite(prev):
            ema[t] = alpha * v + (1 - alpha) * prev
        elif np.isfinite(v):
            ema[t] = v
        else:
            ema[t] = prev
    return ema


def rolling_sd_vec(x, p):
    """Rolling SD (ddof=0) of x over window p. NaN if any NaN in window.
    Vectorized via cumulative sums."""
    L = len(x)
    out = np.full(L, np.nan)
    if L < p:
        return out
    x = np.asarray(x, dtype=np.float64)
    finite = np.isfinite(x)
    x_safe = np.where(finite, x, 0.0)
    x_sq_safe = np.where(finite, x * x, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(x_safe)])
    cs2 = np.concatenate([[0.0], np.cumsum(x_sq_safe)])
    cfin = np.concatenate([[0], np.cumsum(finite.astype(np.int64))])
    t_arr = np.arange(p - 1, L)
    lo_arr = t_arr - p + 1
    fin_count = cfin[t_arr + 1] - cfin[lo_arr]
    s = cs[t_arr + 1] - cs[lo_arr]
    s2 = cs2[t_arr + 1] - cs2[lo_arr]
    mean = s / p
    var = s2 / p - mean * mean
    var = np.where(var < 0, 0.0, var)
    sd = np.sqrt(var)
    out[t_arr] = np.where(fin_count == p, sd, np.nan)
    return out


def log_safe(x):
    x = np.asarray(x, dtype=np.float64)
    out = np.full_like(x, np.nan, dtype=np.float64)
    m = np.isfinite(x) & (x > 0)
    out[m] = np.log(x[m])
    return out


def build_scale_tensors(close_series, sma_periods, ema_periods):
    """For every (family, period) in sma_periods + ema_periods, compute:
       log_ma[i, :]  — log of MA series (NaN where MA undefined).
       sd_log_ma[i, :] — rolling SD of log_ma over window = that MA's period.
    Returns (log_ma, sd_log_ma, cells) with cells ordered SMA first then EMA.
    """
    L = len(close_series)
    cells = [('SMA', p) for p in sma_periods] + [('EMA', p) for p in ema_periods]
    n = len(cells)
    log_ma = np.full((n, L), np.nan, dtype=np.float64)
    sd_log_ma = np.full((n, L), np.nan, dtype=np.float64)
    for i, (fam, p) in enumerate(cells):
        if fam == 'SMA':
            ma = rolling_sma(close_series, p)
        else:
            ma = rolling_ema(close_series, p)
        lm = log_safe(ma)
        log_ma[i] = lm
        sd_log_ma[i] = rolling_sd_vec(lm, p)
    return log_ma, sd_log_ma, cells


def build_tradable_mask(df):
    """Per-bar tradable mask matching SIGNAL_GRINDER.md 3-criteria definition:
       close >= 1.0
       dvol_20d >= 4,000,000
       (rolling 20-bar mean(high/low) - 1) * 100 >= 1.8
    Returns (L,) bool. NaN in any input -> False (not tradable)."""
    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    dvol_20d = df["dvol_20d"].values.astype(np.float64) if "dvol_20d" in df.columns else None
    L = len(close)

    c_ok = np.isfinite(close) & (close >= 1.0)

    if dvol_20d is None:
        # Fallback: compute from close * volume if dvol_20d not precomputed
        volume = df["volume"].values.astype(np.float64)
        dv = np.where(np.isfinite(close) & np.isfinite(volume), close * volume, np.nan)
        finite_dv = np.isfinite(dv)
        dv_safe = np.where(finite_dv, dv, 0.0)
        cs = np.concatenate([[0.0], np.cumsum(dv_safe)])
        cf = np.concatenate([[0], np.cumsum(finite_dv.astype(np.int64))])
        dvol = np.full(L, np.nan)
        if L >= 20:
            t = np.arange(19, L)
            lo = t - 19
            cnt = cf[t + 1] - cf[lo]
            s = cs[t + 1] - cs[lo]
            dvol[t] = np.where(cnt == 20, s / 20.0, np.nan)
        dvol_20d = dvol
    dv_ok = np.isfinite(dvol_20d) & (dvol_20d >= 4_000_000.0)

    # 20-bar rolling mean(H/L)
    with np.errstate(invalid='ignore', divide='ignore'):
        hl = np.where((low > 0) & np.isfinite(low) & np.isfinite(high), high / low, np.nan)
    finite_hl = np.isfinite(hl)
    hl_safe = np.where(finite_hl, hl, 0.0)
    cs_hl = np.concatenate([[0.0], np.cumsum(hl_safe)])
    cf_hl = np.concatenate([[0], np.cumsum(finite_hl.astype(np.int64))])
    mean_hl = np.full(L, np.nan)
    if L >= 20:
        t = np.arange(19, L)
        lo = t - 19
        cnt = cf_hl[t + 1] - cf_hl[lo]
        s = cs_hl[t + 1] - cs_hl[lo]
        mean_hl[t] = np.where(cnt == 20, s / 20.0, np.nan)
    adrp = (mean_hl - 1.0) * 100.0
    adrp_ok = np.isfinite(adrp) & (adrp >= 1.8)

    return c_ok & dv_ok & adrp_ok


def build_ticker_cache(df):
    close = df["close"].values.astype(np.float64)
    L = len(close)
    d_log_ma, d_sd_log_ma, d_cells = build_scale_tensors(close, SMA_DAILY, EMA_DAILY)
    wagg_idx = wagg.build_ticker_weekly_indexing(df)
    weekly_close = wagg_idx["weekly_close"]
    w_log_ma, w_sd_log_ma, w_cells = build_scale_tensors(weekly_close, SMA_WEEKLY, EMA_WEEKLY)
    tradable = build_tradable_mask(df)
    return {
        "close": close, "L": L,
        "tradable": tradable,
        "d_log_ma": d_log_ma, "d_sd_log_ma": d_sd_log_ma, "d_cells": d_cells,
        "weekly_close": weekly_close,
        "w_log_ma": w_log_ma, "w_sd_log_ma": w_sd_log_ma, "w_cells": w_cells,
        "w_pos_all": wagg_idx["w_pos_all"],
        "n_weeks": wagg_idx["n_weeks"],
    }


# ───────────── per-example extraction ─────────────

def extract_daily(tc, E, N_daily):
    """(n_cells, N_daily) log-ratio and sigma.
    Output index i → bar offset k = i+1 (so i=0 is anchor E-1, i=N-1 is E-N).
    Anchor = log(MA[E-1]); log-ratio = log(MA[E-k]) - log(MA[E-1])."""
    d_log_ma = tc["d_log_ma"]; d_sd_log_ma = tc["d_sd_log_ma"]
    L = tc["L"]
    if E < 1 or E > L:
        return None, None
    anchor_lm = d_log_ma[:, E - 1]  # (n_cells,)
    n_cells = d_log_ma.shape[0]
    log_ratio = np.full((n_cells, N_daily), np.nan, dtype=np.float64)
    sigma = np.full((n_cells, N_daily), np.nan, dtype=np.float64)
    for i in range(N_daily):
        k = i + 1  # i=0 is anchor bar E-1
        src = E - k
        if 0 <= src < L:
            log_ratio[:, i] = d_log_ma[:, src] - anchor_lm
            sigma[:, i] = d_sd_log_ma[:, src]
    return log_ratio, sigma


def extract_weekly(tc, E, W_N):
    """(n_cells, W_N) log-ratio and sigma.
    Output index i → offset k = i (so i=0 is anchor W-0 = partial-week anchor,
    i=W_N-1 is W-(W_N-1)).
    Anchor = log(MA[W-0]) with partial-week patch at close[E-1]."""
    close = tc["close"]
    if E < 1:
        return None, None
    anchor_close = close[E - 1]
    if not np.isfinite(anchor_close) or anchor_close <= 0:
        return None, None
    weekly_close = tc["weekly_close"]
    w_log_ma = tc["w_log_ma"]; w_sd_log_ma = tc["w_sd_log_ma"]
    w_cells = tc["w_cells"]; w_pos_all = tc["w_pos_all"]; n_weeks = tc["n_weeks"]
    anchor_pos = int(w_pos_all[E - 1])
    n_cells = len(w_cells)

    # Patched anchor log-MA and patched anchor sigma, per cell.
    anchor_log_ma = np.full(n_cells, np.nan, dtype=np.float64)
    anchor_sigma = np.full(n_cells, np.nan, dtype=np.float64)
    for i, (fam, p) in enumerate(w_cells):
        # Patch anchor MA
        if fam == 'SMA':
            if anchor_pos >= p - 1 and anchor_pos < n_weeks:
                lo = anchor_pos - p + 1
                prior = weekly_close[lo:anchor_pos]  # p-1 historical full-week closes
                if len(prior) == p - 1 and np.all(np.isfinite(prior) & (prior > 0)):
                    sma_val = (np.sum(prior) + anchor_close) / p
                    if sma_val > 0:
                        anchor_log_ma[i] = np.log(sma_val)
        else:  # EMA
            prev_pos = anchor_pos - 1
            if 0 <= prev_pos < n_weeks:
                prev_log = w_log_ma[i, prev_pos]
                if np.isfinite(prev_log):
                    prev_ema = np.exp(prev_log)
                    alpha = 2.0 / (p + 1.0)
                    ema_val = alpha * anchor_close + (1 - alpha) * prev_ema
                    if ema_val > 0:
                        anchor_log_ma[i] = np.log(ema_val)
        # Patched sigma: rolling SD over last p log-MA values, last one replaced
        if np.isfinite(anchor_log_ma[i]) and anchor_pos >= p - 1 and anchor_pos < n_weeks:
            lo = anchor_pos - p + 1
            prior_log = w_log_ma[i, lo:anchor_pos]  # p-1 historical log-MA values
            if len(prior_log) == p - 1 and np.all(np.isfinite(prior_log)):
                window = np.concatenate([prior_log, [anchor_log_ma[i]]])  # p values
                anchor_sigma[i] = window.std(ddof=0)

    log_ratio = np.full((n_cells, W_N), np.nan, dtype=np.float64)
    sigma = np.full((n_cells, W_N), np.nan, dtype=np.float64)
    # k=0: anchor (log-ratio = 0 by construction where anchor finite)
    mask_anchor = np.isfinite(anchor_log_ma)
    log_ratio[mask_anchor, 0] = 0.0
    sigma[:, 0] = anchor_sigma
    # k>=1: historical full weekly MA at anchor_pos - k
    for k in range(1, W_N):
        wpos = anchor_pos - k
        if 0 <= wpos < n_weeks:
            log_ratio[:, k] = w_log_ma[:, wpos] - anchor_log_ma
            sigma[:, k] = w_sd_log_ma[:, wpos]
    return log_ratio, sigma


# ───────────── main ─────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"OHLCV cache: {CACHE_DIR}", flush=True)
    pkl = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    with open(pkl, "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print(f"FAIL: ticker count too low ({len(universe)} < {MIN_TICKER_COUNT})")
        sys.exit(1)

    N_daily_by_setup = {}
    W_N_by_setup = {}
    for s in SETUPS:
        with open(os.path.join(N_DERIV_DIR, f"{s}_summary.json")) as f:
            N_daily_by_setup[s] = int(json.load(f)["N_bars"])
        with open(os.path.join(STAGE_A_DIR, f"{s}_stage_a.json")) as f:
            W_N_by_setup[s] = int(json.load(f)["W_N"])
    print(f"N_daily per setup: {N_daily_by_setup}", flush=True)
    print(f"W_N per setup:    {W_N_by_setup}", flush=True)

    conn = sqlite3.connect(DB)
    tcache = {}

    per_setup_summary = {}
    for setup in SETUPS:
        N_daily = N_daily_by_setup[setup]
        W_N = W_N_by_setup[setup]
        print(f"\n=== {setup.upper()}  N_daily={N_daily}  W_N={W_N}", flush=True)
        rows = conn.execute(
            "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
        ).fetchall()
        print(f"  {len(rows)} examples in DB", flush=True)

        ex_data = []
        t0 = time.time()
        for ticker, entry_date in rows:
            df = universe.get(ticker)
            if df is None:
                continue
            E = vsc.lookup_idx(df, entry_date)
            if E < 0:
                continue
            if ticker not in tcache:
                tcache[ticker] = build_ticker_cache(df)
            tc = tcache[ticker]
            d_lr, d_sig = extract_daily(tc, E, N_daily)
            w_lr, w_sig = extract_weekly(tc, E, W_N)
            if d_lr is None or w_lr is None:
                continue
            ex_data.append({
                "ticker": ticker, "entry_date": entry_date, "E_idx": int(E),
                "d_logratio": d_lr, "d_sigma": d_sig,
                "w_logratio": w_lr, "w_sigma": w_sig,
            })
        print(f"  kept {len(ex_data)} examples  ({time.time()-t0:.1f}s)", flush=True)
        if not ex_data:
            print("  skip"); continue

        d_lr_arr = np.stack([e["d_logratio"] for e in ex_data])
        d_sig_arr = np.stack([e["d_sigma"] for e in ex_data])
        w_lr_arr = np.stack([e["w_logratio"] for e in ex_data])
        w_sig_arr = np.stack([e["w_sigma"] for e in ex_data])

        any_tc = next(iter(tcache.values()))
        d_cells = any_tc["d_cells"]
        w_cells = any_tc["w_cells"]

        d_nan_lr = float(np.isnan(d_lr_arr).mean())
        d_nan_sig = float(np.isnan(d_sig_arr).mean())
        w_nan_lr = float(np.isnan(w_lr_arr).mean())
        w_nan_sig = float(np.isnan(w_sig_arr).mean())
        print(f"  daily tensor  logratio {d_lr_arr.shape}  sigma {d_sig_arr.shape}", flush=True)
        print(f"  weekly tensor logratio {w_lr_arr.shape}  sigma {w_sig_arr.shape}", flush=True)
        print(f"  NaN rate  daily lr {d_nan_lr:.1%}  sig {d_nan_sig:.1%}  |  weekly lr {w_nan_lr:.1%}  sig {w_nan_sig:.1%}", flush=True)

        out_path = os.path.join(OUT_DIR, f"{setup}_trajectories.pkl")
        with open(out_path, "wb") as f:
            pickle.dump({
                "setup": setup,
                "N_daily": N_daily, "W_N": W_N,
                "daily_cells": d_cells, "weekly_cells": w_cells,
                "daily_logratio": d_lr_arr, "daily_sigma": d_sig_arr,
                "weekly_logratio": w_lr_arr, "weekly_sigma": w_sig_arr,
                "examples": [{"ticker": e["ticker"], "entry_date": e["entry_date"], "E_idx": e["E_idx"]} for e in ex_data],
            }, f)
        print(f"  saved {out_path}", flush=True)

        per_setup_summary[setup] = {
            "n_ex": len(ex_data),
            "N_daily": N_daily, "W_N": W_N,
            "n_daily_cells": len(d_cells), "n_weekly_cells": len(w_cells),
            "daily_nan_lr": d_nan_lr, "daily_nan_sig": d_nan_sig,
            "weekly_nan_lr": w_nan_lr, "weekly_nan_sig": w_nan_sig,
        }
    conn.close()

    # Cross-check: HTF × daily × SMA10 tight-window elbow vs viz (expect ~10).
    print("\n=== HTF × daily × SMA10 tightness cross-check ===", flush=True)
    htf_path = os.path.join(OUT_DIR, "htf_trajectories.pkl")
    if os.path.exists(htf_path):
        with open(htf_path, "rb") as f:
            htf = pickle.load(f)
        sma10_idx = None
        for i, (fam, p) in enumerate(htf["daily_cells"]):
            if fam == "SMA" and p == 10:
                sma10_idx = i; break
        if sma10_idx is not None:
            log_r = htf["daily_logratio"][:, sma10_idx, :]  # (n_ex, N_daily)
            per_bar_sd = np.nanstd(log_r, axis=0, ddof=0)
            print(f"  per-bar cross-ex SD (k=1..{htf['N_daily']}, k=1 is anchor):", flush=True)
            for i in range(min(len(per_bar_sd), 40)):
                print(f"    k={i + 1:3d}  SD={per_bar_sd[i]:.5f}", flush=True)
            y = per_bar_sd.astype(np.float64)
            x = np.arange(len(y), dtype=np.float64)
            dx = float(x[-1] - x[0]); dy = float(y[-1] - y[0])
            if dy > 0:
                num = np.abs(dy * x - dx * y + dx * y[0])
                elbow = int(np.argmax(num))
            else:
                elbow = 0
            print(f"  kneedle elbow at index={elbow}  (k={elbow + 1})  -> tight window = last {elbow + 1} bars from anchor", flush=True)
            print(f"  (viz expectation: last ~10 bars)", flush=True)

    # Distributions of tightness + sigma per setup.
    print("\n=== Per-setup tightness + sigma distributions ===", flush=True)

    def q(x, qs=(0.05, 0.25, 0.5, 0.75, 0.95)):
        xf = x[np.isfinite(x)]
        if len(xf) == 0:
            return [np.nan] * len(qs)
        return np.quantile(xf, qs)

    for setup in per_setup_summary:
        with open(os.path.join(OUT_DIR, f"{setup}_trajectories.pkl"), "rb") as f:
            st = pickle.load(f)
        d_ts = np.nanstd(st["daily_logratio"], axis=0, ddof=0).ravel()
        w_ts = np.nanstd(st["weekly_logratio"], axis=0, ddof=0).ravel()
        d_sig = st["daily_sigma"].ravel()
        w_sig = st["weekly_sigma"].ravel()
        dtq = q(d_ts); wtq = q(w_ts)
        dsq = q(d_sig); wsq = q(w_sig)
        print(f"  {setup}:", flush=True)
        print(f"    daily  tightness  q5/25/50/75/95 = {dtq[0]:.4f} {dtq[1]:.4f} {dtq[2]:.4f} {dtq[3]:.4f} {dtq[4]:.4f}", flush=True)
        print(f"    weekly tightness  q5/25/50/75/95 = {wtq[0]:.4f} {wtq[1]:.4f} {wtq[2]:.4f} {wtq[3]:.4f} {wtq[4]:.4f}", flush=True)
        print(f"    daily  sigma_i    q5/25/50/75/95 = {dsq[0]:.4f} {dsq[1]:.4f} {dsq[2]:.4f} {dsq[3]:.4f} {dsq[4]:.4f}", flush=True)
        print(f"    weekly sigma_i    q5/25/50/75/95 = {wsq[0]:.4f} {wsq[1]:.4f} {wsq[2]:.4f} {wsq[3]:.4f} {wsq[4]:.4f}", flush=True)

    print("\nPhase 1 complete.", flush=True)


if __name__ == "__main__":
    main()
