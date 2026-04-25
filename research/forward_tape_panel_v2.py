"""Forward-tape panel v2 — entry-detector input (breakouts only this pass).

Purpose: for every cluster in `classifier_pool/{setup}_pool.json`, produce a
forward-tape panel of signal-bar-anchored scalars and per-(cluster, k) event
rows. Output feeds the entry-detector entry_grinder and fidelity gate.

§2 c1 invariant: extractor is a pure function of `sig_idx`. No `entry_idx`,
no `is_example` passed in. `is_example` is tagged outside the extractor as
metadata from the pool's `is_example` column.

Scope: breakouts only (htf, bf, base). Fades are out of scope per plan L165.

Inputs per setup:
  - pool:       research/classifier_pool/{setup}_pool.json
  - N_bars:     research/n_derivation_cache/{setup}_summary.json.N_bars
  - W_N:        research/presignal_weekly_stage_a/{setup}_stage_a.json.W_N
  - bands:      research/presignal_sma_band/{setup}_trajectories.pkl
                  (d_up/d_lo/w_up/w_lo built by nanmax/nanmin over daily_logratio
                   and weekly_logratio matrices; same bands the presignal scan
                   uses)
  - pyramid:    latest local_runner/cache/pyramid_{setup}_mp_sig*_*.json,
                  tier_results[last_tier].conditions ∩ all_conditions lookup
  - exit rule:  data/signal_exit_grind/signal_exit_{setup}.json top_conditions[0]
  - ohlcv:      universe_ohlcv_daily.pkl (weekly cache not needed — ticker cache
                  builds weekly MAs from daily internally)
  - ern:        scanperfect.db.earnings_dates
  - exprs:      expr_series/{TICKER}.npz (per-ticker), manifest gives expr name→col

Outputs:
  research/forward_tape_panel_v2/{setup}_scalars.pkl
  research/forward_tape_panel_v2/{setup}_events.pkl
  research/forward_tape_panel_v2/summary.json
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import presignal_sma_band_extract as ext
import visual_shape_compare as vsc

MAIN = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN, "local_runner"))
from expr_cache_builder import _open_npz  # noqa: E402
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE = os.path.join(MAIN, "local_runner", "cache")
DB = os.path.join(MAIN, "data", "scanperfect.db")
EXPR_CACHE_DIR = os.path.join(CACHE, "expr_series")

POOL_DIR = os.path.join(WORKTREE, "research", "classifier_pool")
TRAJ_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band")
PRESIG_SCAN_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")
N_DERIV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
W_STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
SIGNAL_EXIT_DIR = os.path.join(MAIN, "data", "signal_exit_grind")
OUT_DIR = os.path.join(WORKTREE, "research", "forward_tape_panel_v2")

BREAKOUT_SETUPS = ["htf", "bf", "base"]
DIRECTION = {"htf": +1, "bf": +1, "base": +1}  # breakouts only
MAX_FORWARD = 120


# ─────────── loaders ───────────

def load_pool(setup):
    with open(os.path.join(POOL_DIR, f"{setup}_pool.json")) as f:
        return json.load(f)


def load_n_bars(setup):
    with open(os.path.join(N_DERIV_DIR, f"{setup}_summary.json")) as f:
        return int(json.load(f)["N_bars"])


def load_w_n(setup):
    with open(os.path.join(W_STAGE_A_DIR, f"{setup}_stage_a.json")) as f:
        return int(json.load(f)["W_N"])


def load_setup_bands(setup):
    """Build (d_up, d_lo, w_up, w_lo) from the presignal trajectories matrix.
    Same bands the presignal scan uses, via nanmax/nanmin over example log-ratios."""
    with open(os.path.join(TRAJ_DIR, f"{setup}_trajectories.pkl"), "rb") as f:
        t = pickle.load(f)
    d_lr = t["daily_logratio"]   # (n_ex, 21, N_daily)
    w_lr = t["weekly_logratio"]  # (n_ex, 15, W_N)
    with np.errstate(invalid="ignore"):
        d_up = np.nanmax(d_lr, axis=0)
        d_lo = np.nanmin(d_lr, axis=0)
        w_up = np.nanmax(w_lr, axis=0)
        w_lo = np.nanmin(w_lr, axis=0)
    return {
        "d_up": d_up, "d_lo": d_lo,
        "w_up": w_up, "w_lo": w_lo,
        "daily_cells": t["daily_cells"],
        "weekly_cells": t["weekly_cells"],
        "N_daily": int(t["N_daily"]),
        "W_N": int(t["W_N"]),
    }


def load_presignal_pre_dedup(setup):
    with open(os.path.join(PRESIG_SCAN_DIR, f"{setup}_passes.pkl"), "rb") as f:
        return pickle.load(f)["pre_dedup_by_ticker"]


def latest_pyramid_file(setup):
    pat = os.path.join(CACHE, f"pyramid_{setup}_mp_sig*_pk*_*.json")
    files = [f for f in glob.glob(pat) if "sig0_pk0" not in os.path.basename(f)]
    def ts(p):
        m = re.search(r"_(\d{8})_(\d{6})\.json$", p)
        return (m.group(1), m.group(2)) if m else ("", "")
    return max(files, key=ts) if files else None


def load_pyramid_conds(setup):
    """Return list of locked pyramid conditions for this setup, each as
    {name, expr, low, high}. Locked = tier_results[last_tier].conditions (by name)
    cross-referenced against all_conditions (for bounds)."""
    p = latest_pyramid_file(setup)
    if p is None:
        return []
    with open(p) as f:
        d = json.load(f)
    all_by_name = {c["name"]: c for c in d.get("all_conditions", [])}
    tier_keys = list(d.get("tier_results", {}).keys())
    if not tier_keys:
        return []
    locked_names = d["tier_results"][tier_keys[-1]].get("conditions", [])
    out = []
    for nm in locked_names:
        c = all_by_name.get(nm)
        if c is None:
            continue
        out.append({
            "name": c["name"],
            "expr": c.get("expr", c["name"]),
            "low": float(c["low"]),
            "high": float(c["high"]),
        })
    return out


def load_exit_rule(setup):
    p = os.path.join(SIGNAL_EXIT_DIR, f"signal_exit_{setup}.json")
    with open(p) as f:
        d = json.load(f)
    top = None
    for key in ("top_conditions", "candidates", "conditions"):
        if key in d and isinstance(d[key], list) and d[key]:
            top = d[key][0]; break
    if top is None:
        raise RuntimeError(f"could not locate top condition in {p}")
    return {
        "expr": top["expression"],
        "op": top["direction"],
        "thr": float(top["threshold"]),
    }


def load_earnings_map():
    with sqlite3.connect(DB) as c:
        ern = defaultdict(list)
        for tk, d in c.execute("SELECT ticker, earnings_date FROM earnings_dates"):
            ern[tk].append(str(d)[:10])
    return {t: np.array(sorted(set(v)), dtype="<U10") for t, v in ern.items()}


def load_expr_vocab():
    with open(os.path.join(EXPR_CACHE_DIR, "_manifest.json")) as f:
        m = json.load(f)
    return {n: i for i, n in enumerate(m["expr_names"])}


def load_universe():
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        return pickle.load(f)


def load_expr_cache_for_ticker(ticker):
    safe = ticker.replace("/", "_").replace(".", "_")
    p = os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")
    if not os.path.exists(p):
        return None
    z = _open_npz(p)
    return {
        "dates": z["dates"].astype("<U10"),
        "data": z["data"].astype(np.float64),
    }


# ─────────── helpers ───────────

def dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def compute_adr14(df, at_idx):
    lo = max(0, at_idx - 13)
    h = df["high"].values[lo:at_idx + 1].astype(float)
    l = df["low"].values[lo:at_idx + 1].astype(float)
    v = float(np.mean(h - l))
    return v


def earnings_cap_offset(ern_arr, dates_arr, sig_idx):
    df_end_off = len(dates_arr) - 1 - sig_idx
    cap_off = min(MAX_FORWARD, df_end_off)
    if ern_arr is None or len(ern_arr) == 0:
        return cap_off
    sig_date = dates_arr[sig_idx]
    pos = int(np.searchsorted(ern_arr, sig_date, side="right"))
    if pos >= len(ern_arr):
        return cap_off
    ern_date = ern_arr[pos]
    ern_bar = int(np.searchsorted(dates_arr, ern_date, side="left"))
    if ern_bar <= sig_idx:
        return cap_off
    earnings_cap = ern_bar - 1 - sig_idx
    return max(0, min(cap_off, earnings_cap))


def cluster_run_backward(pre_idxs_set, sig_idx):
    run = [sig_idx]
    k = sig_idx - 1
    while k in pre_idxs_set:
        run.append(k); k -= 1
    run.reverse()
    return run


# ─────────── reanchor — per-forward-anchor cell status ───────────

def eval_reanchor_forward(tc, anchors, N_daily, W_N, d_up, d_lo, w_up, w_lo, w_cells):
    """For each forward anchor A in `anchors`, compute per-cell pass/above/below
    counts for daily and weekly cells. Mirrors scan_ticker_fast's vectorized
    lr construction but aggregates per anchor.

    Returns dict of arrays shape (n_anchors,):
      d_total, d_pass, d_above, d_below  (daily counts — cells evaluable)
      w_total, w_pass, w_above, w_below  (weekly counts — cells evaluable)
      valid_anchor (bool — did we have a valid anchor close)
    NaN-lenient: cells with NaN lr or NaN band count as "pass" (auto-pass).
    Above/below counts only non-NaN cells strictly outside the band.
    """
    close = tc["close"]; L = tc["L"]
    d_log_ma = tc["d_log_ma"]
    w_log_ma = tc["w_log_ma"]
    w_pos_all = tc["w_pos_all"]
    weekly_close = tc["weekly_close"]
    n_weeks = tc["n_weeks"]
    n_w_ma = len(w_cells)

    anchors = np.asarray(anchors, dtype=np.int64)
    n_a = len(anchors)
    out = {
        "d_pass": np.zeros(n_a, dtype=np.int64),
        "d_above": np.zeros(n_a, dtype=np.int64),
        "d_below": np.zeros(n_a, dtype=np.int64),
        "d_total": np.zeros(n_a, dtype=np.int64),
        "w_pass": np.zeros(n_a, dtype=np.int64),
        "w_above": np.zeros(n_a, dtype=np.int64),
        "w_below": np.zeros(n_a, dtype=np.int64),
        "w_total": np.zeros(n_a, dtype=np.int64),
        "valid_anchor": np.zeros(n_a, dtype=bool),
    }

    valid = (anchors >= 1) & (anchors < L)
    if valid.any():
        c_at = np.full(n_a, np.nan)
        c_at[valid] = close[anchors[valid] - 1]
        valid &= np.isfinite(c_at) & (c_at > 0)
    out["valid_anchor"] = valid
    if not valid.any():
        return out

    A = anchors[valid]
    nA = len(A)

    # Daily
    d_log_anchor = d_log_ma[:, A - 1]                      # (21, nA)
    ks = np.arange(1, N_daily + 1, dtype=np.int64)
    src_mat = A[:, None] - ks[None, :]                     # (nA, N_daily)
    valid_src = src_mat >= 0
    src_safe = np.where(valid_src, src_mat, 0)
    d_log_at_src = d_log_ma[:, src_safe]                   # (21, nA, N_daily)
    d_log_at_src = np.where(valid_src[None, :, :], d_log_at_src, np.nan)
    d_lr = d_log_at_src - d_log_anchor[:, :, None]
    nan_val_d = np.isnan(d_lr)
    nan_band_d = np.isnan(d_up[:, None, :]) | np.isnan(d_lo[:, None, :])
    cell_evaluable_d = ~nan_val_d & ~nan_band_d            # (21, nA, N_daily)
    with np.errstate(invalid="ignore"):
        in_band_d = (d_lr >= d_lo[:, None, :]) & (d_lr <= d_up[:, None, :])
        above_d = d_lr > d_up[:, None, :]
        below_d = d_lr < d_lo[:, None, :]
    # Auto-pass rule: NaN-lenient cells count toward pass
    pass_d = (nan_val_d | nan_band_d | in_band_d)          # (21, nA, N_daily)
    above_mask_d = above_d & cell_evaluable_d
    below_mask_d = below_d & cell_evaluable_d

    # Aggregate per anchor
    out["d_pass"][valid] = pass_d.sum(axis=(0, 2))
    out["d_above"][valid] = above_mask_d.sum(axis=(0, 2))
    out["d_below"][valid] = below_mask_d.sum(axis=(0, 2))
    # Total cells = cells where a band is defined (finite lo & hi) — broadcast per anchor
    d_cells_with_band = (~nan_band_d).sum()  # scalar — total cells with defined band
    out["d_total"][valid] = d_cells_with_band  # broadcast to every valid anchor

    # Weekly — partial-week anchor patched at close[A-1], per scan_ticker_fast
    anchor_pos = w_pos_all[A - 1].astype(np.int64)         # (nA,)
    anchor_close = close[A - 1]
    wc_finite = np.isfinite(weekly_close) & (weekly_close > 0)
    wc_safe = np.where(wc_finite, weekly_close, 0.0)
    wc_cumsum = np.concatenate([[0.0], np.cumsum(wc_safe)])
    wc_okcount = np.concatenate([[0], np.cumsum(wc_finite.astype(np.int64))])

    patched_log_anchor = np.full((n_w_ma, nA), np.nan, dtype=np.float64)
    for m, (fam, p) in enumerate(w_cells):
        if fam == "SMA":
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
        with np.errstate(invalid="ignore", divide="ignore"):
            patched_log_anchor[m, pat_ok] = np.log(patched[pat_ok])

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
        cell_evaluable_w = ~nan_val_w & ~nan_band_w
        with np.errstate(invalid="ignore"):
            in_band_w = (w_lr >= w_lo_slice[:, None, :]) & (w_lr <= w_up_slice[:, None, :])
            above_w = w_lr > w_up_slice[:, None, :]
            below_w = w_lr < w_lo_slice[:, None, :]
        pass_w = (nan_val_w | nan_band_w | in_band_w)
        above_mask_w = above_w & cell_evaluable_w
        below_mask_w = below_w & cell_evaluable_w

        out["w_pass"][valid] = pass_w.sum(axis=(0, 2))
        out["w_above"][valid] = above_mask_w.sum(axis=(0, 2))
        out["w_below"][valid] = below_mask_w.sum(axis=(0, 2))
        w_cells_with_band = (~nan_band_w).sum()
        out["w_total"][valid] = w_cells_with_band

    return out


# ─────────── pyramid_retest + exit_fire — expr-cache walk ───────────

def eval_expr_forward(expr_cache, sig_idx_in_cache, max_off, expr_cols):
    """For each expression col in `expr_cols`, return its values at bars
    sig_idx_in_cache + 1 .. sig_idx_in_cache + max_off.
    Shape: (len(expr_cols), max_off). Bars out of range → NaN.
    """
    data = expr_cache["data"]
    L = data.shape[0]
    out = np.full((len(expr_cols), max_off), np.nan, dtype=np.float64)
    for j, col in enumerate(expr_cols):
        if col is None:
            continue
        end = min(sig_idx_in_cache + max_off, L - 1)
        start = sig_idx_in_cache + 1
        if start > end:
            continue
        n = end - start + 1
        out[j, :n] = data[start:end + 1, col]
    return out


def match_sig_in_expr_cache(expr_cache, sig_date):
    """Return the sig_idx inside the expression cache's date axis (may differ
    from the OHLCV sig_idx due to different history cutoffs — EXPR_CACHE_START
    = 2020-01-02)."""
    pos = np.searchsorted(expr_cache["dates"], sig_date, side="left")
    if pos >= len(expr_cache["dates"]):
        return None
    if expr_cache["dates"][pos] != sig_date:
        return None
    return int(pos)


# ─────────── THE extractor ───────────

def build_panel_rows(df_daily, df_weekly, sig_idx, sig_date,
                    setup_params, locked_cells, locked_pyramid_conds,
                    exit_rule, ern_arr, expr_cache_handle,
                    sma_arrays_daily, sma_arrays_weekly):
    """Pure function of sig_idx. No entry_idx, no is_example.
    Returns (scalar_dict, list_of_event_dicts).

    Inputs:
      df_daily          — ticker's full daily OHLCV DataFrame
      df_weekly         — unused at this build (weekly MAs live in sma_arrays_*)
      sig_idx           — signal bar index in df_daily
      sig_date          — ISO string of that bar's date
      setup_params      — dict: direction, N_bars, W_N, pre_dedup_set_for_ticker
      locked_cells      — dict: d_up/d_lo/w_up/w_lo/daily_cells/weekly_cells/N_daily/W_N
      locked_pyramid_conds — list of {name, expr, low, high}
      exit_rule         — {expr, op, thr}
      ern_arr           — np.array of earnings dates for this ticker
      expr_cache_handle — {dates, data} for this ticker's npz, or None
      sma_arrays_daily, sma_arrays_weekly — the ticker cache dict from build_ticker_cache
    """
    direction = setup_params["direction"]
    N_bars = setup_params["N_bars"]
    W_N_params = setup_params["W_N"]
    pre_set = setup_params["pre_dedup_set_for_ticker"]

    dates_arr = dates_str(df_daily)
    cap = earnings_cap_offset(ern_arr, dates_arr, sig_idx)

    sig_open = float(df_daily["open"].values[sig_idx])
    sig_high = float(df_daily["high"].values[sig_idx])
    sig_low = float(df_daily["low"].values[sig_idx])
    sig_close = float(df_daily["close"].values[sig_idx])
    adr = compute_adr14(df_daily, sig_idx)

    # V-levels — data-derived lookback, per forward_tape_panel.py convention
    lo_bar = max(0, sig_idx - N_bars + 1)
    v1 = float(np.max(df_daily["high"].values[lo_bar:sig_idx + 1]))
    if sig_idx in pre_set:
        run = cluster_run_backward(pre_set, sig_idx)
        v2 = float(np.max(df_daily["high"].values[run[0]:run[-1] + 1]))
    else:
        v2 = sig_high
    v3_window = W_N_params * 5
    v3_lo = max(0, sig_idx - v3_window + 1)
    v3 = float(np.max(df_daily["high"].values[v3_lo:sig_idx + 1]))
    v4 = float(np.max(df_daily["close"].values[lo_bar:sig_idx + 1]))
    if sig_idx - 1 >= lo_bar:
        v5 = float(np.max(df_daily["high"].values[lo_bar:sig_idx]))
    else:
        v5 = float("nan")

    scalar = {
        "signal_bar_idx": int(sig_idx),
        "signal_date": sig_date,
        "sig_open": sig_open, "sig_high": sig_high,
        "sig_low": sig_low, "sig_close": sig_close,
        "adr_at_signal": adr,
        "direction": int(direction),
        "earnings_cap_offset": int(cap),
        "v1": v1, "v2": v2, "v3": v3, "v4": v4, "v5": v5,
    }

    if cap < 1 or not np.isfinite(adr) or adr <= 0:
        return scalar, []

    # Forward bar windows
    ks = np.arange(1, cap + 1, dtype=np.int64)
    forward_bars = sig_idx + ks
    forward_dates = dates_arr[forward_bars]
    f_open = df_daily["open"].values[forward_bars].astype(float)
    f_high = df_daily["high"].values[forward_bars].astype(float)
    f_low = df_daily["low"].values[forward_bars].astype(float)
    f_close = df_daily["close"].values[forward_bars].astype(float)

    # V-level breach + confirm — per forward bar
    v_levels = np.array([v1, v2, v3, v4, v5], dtype=float)
    v_breach = (f_high[:, None] > v_levels[None, :])      # (cap, 5)
    v_confirm = v_breach & (f_close[:, None] > v_levels[None, :])

    sighigh_breach = f_high > sig_high
    sighigh_confirm = sighigh_breach & (f_close > sig_high)

    # Candle anatomy per bar (ADR units)
    body_adr = (f_close - f_open) / adr
    upper_wick_adr = (f_high - np.maximum(f_open, f_close)) / adr
    lower_wick_adr = (np.minimum(f_open, f_close) - f_low) / adr
    range_adr = (f_high - f_low) / adr

    # Reanchor — per-forward-anchor cell status
    reanchor = eval_reanchor_forward(
        sma_arrays_daily, forward_bars,
        locked_cells["N_daily"], locked_cells["W_N"],
        locked_cells["d_up"], locked_cells["d_lo"],
        locked_cells["w_up"], locked_cells["w_lo"],
        locked_cells["weekly_cells"],
    )
    d_total = reanchor["d_total"].astype(float)
    w_total = reanchor["w_total"].astype(float)
    total_cells = d_total + w_total
    # Safety: avoid div/0
    d_total_safe = np.where(d_total > 0, d_total, np.nan)
    w_total_safe = np.where(w_total > 0, w_total, np.nan)
    tot_safe = np.where(total_cells > 0, total_cells, np.nan)

    reanchor_pass_frac = (reanchor["d_pass"] + reanchor["w_pass"]) / tot_safe
    reanchor_daily_pass_frac = reanchor["d_pass"] / d_total_safe
    reanchor_weekly_pass_frac = reanchor["w_pass"] / w_total_safe
    reanchor_upper_exit_frac = (reanchor["d_above"] + reanchor["w_above"]) / tot_safe
    reanchor_lower_exit_frac = (reanchor["d_below"] + reanchor["w_below"]) / tot_safe

    reanchor_upper_exit = (reanchor["d_above"] + reanchor["w_above"]) > 0
    reanchor_lower_exit = (reanchor["d_below"] + reanchor["w_below"]) > 0

    # Pyramid retest + exit_fire — walk expression cache
    pyr_above = np.zeros(cap, dtype=np.int64)
    pyr_below = np.zeros(cap, dtype=np.int64)
    pyr_pass = np.zeros(cap, dtype=np.int64)
    pyr_total = np.zeros(cap, dtype=np.int64)
    exit_fire = np.zeros(cap, dtype=bool)

    if expr_cache_handle is not None:
        sig_in_cache = match_sig_in_expr_cache(expr_cache_handle, sig_date)
        if sig_in_cache is not None:
            # Build col lookup for pyramid conds + exit rule
            vocab = setup_params["_expr_vocab"]
            pyr_cols = [vocab.get(c["expr"]) for c in locked_pyramid_conds]
            pyr_vals = eval_expr_forward(expr_cache_handle, sig_in_cache, cap, pyr_cols)
            pyr_lows = np.array([c["low"] for c in locked_pyramid_conds], dtype=float)
            pyr_highs = np.array([c["high"] for c in locked_pyramid_conds], dtype=float)

            if len(locked_pyramid_conds):
                evaluable = ~np.isnan(pyr_vals)        # (n_conds, cap)
                with np.errstate(invalid="ignore"):
                    above = (pyr_vals > pyr_highs[:, None]) & evaluable
                    below = (pyr_vals < pyr_lows[:, None]) & evaluable
                    in_band = (pyr_vals >= pyr_lows[:, None]) & (pyr_vals <= pyr_highs[:, None]) & evaluable
                # "Missing col" (None) contributed NaN row; evaluable filters it
                pyr_above = above.sum(axis=0)
                pyr_below = below.sum(axis=0)
                pyr_pass = in_band.sum(axis=0)
                pyr_total = evaluable.sum(axis=0)

            exit_col = vocab.get(exit_rule["expr"])
            if exit_col is not None:
                ex_vals = eval_expr_forward(expr_cache_handle, sig_in_cache, cap, [exit_col])[0]
                op = exit_rule["op"]; thr = exit_rule["thr"]
                with np.errstate(invalid="ignore"):
                    if op in ("<=", "≤"):
                        exit_fire = np.where(np.isnan(ex_vals), False, ex_vals <= thr)
                    elif op in (">=", "≥"):
                        exit_fire = np.where(np.isnan(ex_vals), False, ex_vals >= thr)
                    elif op == "<":
                        exit_fire = np.where(np.isnan(ex_vals), False, ex_vals < thr)
                    elif op == ">":
                        exit_fire = np.where(np.isnan(ex_vals), False, ex_vals > thr)

    pyramid_retest_upper_exit = pyr_above > 0
    pyramid_retest_lower_exit = pyr_below > 0
    pyr_tot_safe = np.where(pyr_total > 0, pyr_total, np.nan)
    pyramid_retest_pass_frac = pyr_pass / pyr_tot_safe
    pyramid_retest_upper_exit_frac = pyr_above / pyr_tot_safe
    pyramid_retest_lower_exit_frac = pyr_below / pyr_tot_safe

    # Assemble events
    events = []
    for i in range(cap):
        events.append({
            "offset": int(i + 1),
            "date": forward_dates[i],
            "open": float(f_open[i]), "high": float(f_high[i]),
            "low": float(f_low[i]), "close": float(f_close[i]),
            # V breach + confirm
            "v1_breach": bool(v_breach[i, 0]), "v1_confirm": bool(v_confirm[i, 0]),
            "v2_breach": bool(v_breach[i, 1]), "v2_confirm": bool(v_confirm[i, 1]),
            "v3_breach": bool(v_breach[i, 2]), "v3_confirm": bool(v_confirm[i, 2]),
            "v4_breach": bool(v_breach[i, 3]), "v4_confirm": bool(v_confirm[i, 3]),
            "v5_breach": bool(v_breach[i, 4]), "v5_confirm": bool(v_confirm[i, 4]),
            # Sig-high breach/confirm
            "sighigh_breach": bool(sighigh_breach[i]),
            "sighigh_confirm": bool(sighigh_confirm[i]),
            # Reanchor events
            "reanchor_upper_exit": bool(reanchor_upper_exit[i]),
            "reanchor_lower_exit": bool(reanchor_lower_exit[i]),
            # Pyramid retest events
            "pyramid_retest_upper_exit": bool(pyramid_retest_upper_exit[i]),
            "pyramid_retest_lower_exit": bool(pyramid_retest_lower_exit[i]),
            # Exit fire
            "exit_fire": bool(exit_fire[i]),
            # Continuous scalars
            "reanchor_pass_frac": float(reanchor_pass_frac[i]) if not np.isnan(reanchor_pass_frac[i]) else float("nan"),
            "reanchor_daily_pass_frac": float(reanchor_daily_pass_frac[i]) if not np.isnan(reanchor_daily_pass_frac[i]) else float("nan"),
            "reanchor_weekly_pass_frac": float(reanchor_weekly_pass_frac[i]) if not np.isnan(reanchor_weekly_pass_frac[i]) else float("nan"),
            "reanchor_upper_exit_frac": float(reanchor_upper_exit_frac[i]) if not np.isnan(reanchor_upper_exit_frac[i]) else float("nan"),
            "reanchor_lower_exit_frac": float(reanchor_lower_exit_frac[i]) if not np.isnan(reanchor_lower_exit_frac[i]) else float("nan"),
            "pyramid_retest_pass_frac": float(pyramid_retest_pass_frac[i]) if not np.isnan(pyramid_retest_pass_frac[i]) else float("nan"),
            "pyramid_retest_upper_exit_frac": float(pyramid_retest_upper_exit_frac[i]) if not np.isnan(pyramid_retest_upper_exit_frac[i]) else float("nan"),
            "pyramid_retest_lower_exit_frac": float(pyramid_retest_lower_exit_frac[i]) if not np.isnan(pyramid_retest_lower_exit_frac[i]) else float("nan"),
            "body_adr": float(body_adr[i]),
            "upper_wick_adr": float(upper_wick_adr[i]),
            "lower_wick_adr": float(lower_wick_adr[i]),
            "range_adr": float(range_adr[i]),
        })

    return scalar, events


# §2 c1 structural invariant — extractor must NEVER see entry_idx.
assert "entry" not in build_panel_rows.__code__.co_varnames, (
    "build_panel_rows took an entry-related argument — §2 c1 violation"
)


# ─────────── driver ───────────

def run_setup(setup, universe, ern_map, expr_vocab):
    print(f"\n=== {setup.upper()} ===", flush=True)

    pool = load_pool(setup)
    N_bars = load_n_bars(setup)
    W_N = load_w_n(setup)
    bands = load_setup_bands(setup)
    pyr_conds = load_pyramid_conds(setup)
    exit_rule = load_exit_rule(setup)
    pre_dedup = load_presignal_pre_dedup(setup)

    direction = DIRECTION[setup]
    print(f"  clusters={len(pool['clusters'])}  N_bars={N_bars}  W_N={W_N}  "
          f"daily_cells_with_band={(~np.isnan(bands['d_up']) & ~np.isnan(bands['d_lo'])).sum()}  "
          f"weekly_cells_with_band={(~np.isnan(bands['w_up']) & ~np.isnan(bands['w_lo'])).sum()}",
          flush=True)
    print(f"  pyramid_conds_locked={len(pyr_conds)}  "
          f"exit_rule={exit_rule['expr']} {exit_rule['op']} {exit_rule['thr']}",
          flush=True)

    # Per-ticker caches — expr cache + ticker cache (SMA/EMA arrays)
    expr_cache_by_ticker = {}
    tcache_by_ticker = {}
    scalars = []
    events = []
    unresolved = []
    t0 = time.time()
    for idx, c in enumerate(pool["clusters"]):
        ticker = c["ticker"]; sig_idx = int(c["rightmost"]["bar_idx"])
        sig_date = c["rightmost"]["date"]

        df = universe.get(ticker)
        if df is None:
            unresolved.append((ticker, sig_idx, "no_df")); continue

        if ticker not in tcache_by_ticker:
            tcache_by_ticker[ticker] = ext.build_ticker_cache(df)
        tc = tcache_by_ticker[ticker]

        if ticker not in expr_cache_by_ticker:
            expr_cache_by_ticker[ticker] = load_expr_cache_for_ticker(ticker)
        expr_handle = expr_cache_by_ticker[ticker]

        ern_arr = ern_map.get(ticker)
        pre_set = set(pre_dedup.get(ticker, []))

        setup_params = {
            "direction": direction,
            "N_bars": N_bars,
            "W_N": W_N,
            "pre_dedup_set_for_ticker": pre_set,
            "_expr_vocab": expr_vocab,
        }

        try:
            scalar_data, event_rows = build_panel_rows(
                df, None, sig_idx, sig_date,
                setup_params, bands, pyr_conds, exit_rule,
                ern_arr, expr_handle, tc, tc,
            )
        except Exception as e:
            unresolved.append((ticker, sig_idx, f"extract_error: {e}"))
            continue

        scalar_row = {
            "setup": setup,
            "cluster_id": int(c["cluster_id"]),
            "ticker": ticker,
            "is_example": int(c["is_example"]),
            **scalar_data,
        }
        scalars.append(scalar_row)
        for ev in event_rows:
            ev_row = {"cluster_id": int(c["cluster_id"]), **ev}
            events.append(ev_row)

        if (idx + 1) % 25 == 0 or (idx + 1) == len(pool["clusters"]):
            print(f"  [{setup}] {idx+1}/{len(pool['clusters'])}  events_so_far={len(events):,}  "
                  f"({time.time()-t0:.1f}s)", flush=True)

    return scalars, events, unresolved


def main():
    assert OUT_DIR.startswith(WORKTREE), f"refusing write outside worktree: {OUT_DIR}"
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Loading OHLCV...", flush=True)
    universe = load_universe()
    print(f"  tickers: {len(universe):,}", flush=True)
    assert len(universe) > 11000, f"universe count {len(universe)} — STOPPING"

    print("Loading earnings map...", flush=True)
    ern_map = load_earnings_map()
    print(f"  tickers with earnings: {len(ern_map):,}", flush=True)

    print("Loading expr vocab...", flush=True)
    expr_vocab = load_expr_vocab()
    print(f"  vocab size: {len(expr_vocab):,}", flush=True)

    summary = {}
    for setup in BREAKOUT_SETUPS:
        scalars, events, unresolved = run_setup(setup, universe, ern_map, expr_vocab)
        s_df = pd.DataFrame(scalars)
        e_df = pd.DataFrame(events)
        s_path = os.path.join(OUT_DIR, f"{setup}_scalars.pkl")
        e_path = os.path.join(OUT_DIR, f"{setup}_events.pkl")
        assert s_path.startswith(WORKTREE), f"refusing write: {s_path}"
        assert e_path.startswith(WORKTREE), f"refusing write: {e_path}"
        s_df.to_pickle(s_path)
        e_df.to_pickle(e_path)
        summary[setup] = {
            "n_clusters_scalars": len(s_df),
            "n_event_rows": len(e_df),
            "n_unresolved": len(unresolved),
            "unresolved_sample": unresolved[:5],
            "scalars_path": os.path.basename(s_path),
            "events_path": os.path.basename(e_path),
        }
        print(f"  wrote {s_path} ({len(s_df)} rows)")
        print(f"  wrote {e_path} ({len(e_df)} rows)")

    sum_path = os.path.join(OUT_DIR, "summary.json")
    assert sum_path.startswith(WORKTREE), f"refusing write: {sum_path}"
    with open(sum_path, "w") as f:
        json.dump({
            "setups": summary,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }, f, indent=2)
    print(f"\nwrote {sum_path}")


if __name__ == "__main__":
    main()
