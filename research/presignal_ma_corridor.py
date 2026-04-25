"""Presignal MA-corridor filter.

Per setup × scale (daily, weekly):
  1. Extract MA feature matrix at every (MA type, lead-up offset) cell across
     labelled examples and a shared random post-2020 market sample.
     MA basis: SMA + EMA at periods {5,8,10,13,20,21,30,50,65,100,150,200}.
  2. Per-cell reject rate = fraction of market sample OUTSIDE the example
     bounding-box corridor at that cell. High reject = meat.
  3. Kneedle elbow on sorted-ascending reject-rate curve → threshold; cells
     with reject >= threshold are selected.
  4. LOO selection stability: hold out each example, re-run selection;
     survivors must be selected in every fold.
  5. Corridor + frontier-gap LOO slack on survivors.
  6. Universe scan: strict-AND across all surviving daily + weekly cells,
     NaN-lenient per cell. Fast per-ticker cache of daily + weekly MAs.
  7. §5.2 cluster dedup, 100% EX verification, per-setup volume report.

Weekly anchor rule: close[E-1] (partial-week anchor). For speed the anchor
week's MA is patched per-candidate from precomputed end-of-week-close MAs
(one scalar update per MA period).
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import presignal_weekly_aggregate as wagg

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_ma_corridor")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
MA_PERIODS = [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]
MA_TYPES = [f"SMA{p}" for p in MA_PERIODS] + [f"EMA{p}" for p in MA_PERIODS]
N_MA = len(MA_TYPES)  # 24

MAX_DAILY_OFFSET = 40
DATE_CUTOFF = "2020-01-02"
N_MARKET_SAMPLE = 5000
SEED = 42
MIN_TICKER_COUNT = 11000


# ───────────── MA compute ─────────────

def compute_mas(close, periods):
    """(2*len(periods), L). Rows: SMA_p1..SMA_pN, EMA_p1..EMA_pN."""
    L = len(close)
    c = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    out = np.full((2 * len(periods), L), np.nan, dtype=np.float64)
    cumsum_c = np.cumsum(np.where(np.isfinite(c), c, 0.0))
    cumsum_ok = np.cumsum(np.isfinite(c).astype(np.int64))
    for i, p in enumerate(periods):
        if L < p:
            continue
        num = cumsum_c[p - 1:] - np.concatenate([[0.0], cumsum_c[:L - p]])
        count = cumsum_ok[p - 1:] - np.concatenate([[0], cumsum_ok[:L - p]])
        sma = np.where(count == p, num / p, np.nan)
        out[i, p - 1:L] = sma
    for i, p in enumerate(periods):
        alpha = 2.0 / (p + 1.0)
        ema = np.full(L, np.nan)
        first = -1
        for t in range(L):
            if np.isfinite(c[t]):
                first = t
                break
        if first < 0:
            continue
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
        out[len(periods) + i, :] = ema
    return out


# ───────────── per-ticker cache ─────────────

def build_ticker_cache(df):
    """Cache of everything needed for fast per-candidate feature extraction:
    daily close + daily MAs + weekly close + weekly MAs + w_pos_all.
    Weekly MAs computed on end-of-week closes."""
    close = df["close"].values.astype(np.float64)
    L = len(close)
    daily_mas = compute_mas(close, MA_PERIODS)

    wagg_idx = wagg.build_ticker_weekly_indexing(df)
    weekly_close = wagg_idx["weekly_close"]
    weekly_mas = compute_mas(weekly_close, MA_PERIODS)
    return {
        "close": close,
        "daily_mas": daily_mas,
        "weekly_close": weekly_close,
        "weekly_mas": weekly_mas,
        "w_pos_all": wagg_idx["w_pos_all"],
        "L": L,
        "n_weeks": wagg_idx["n_weeks"],
    }


# ───────────── feature extraction ─────────────

def daily_feat_at(tc, E, N_off=MAX_DAILY_OFFSET):
    """(N_MA, N_off) log(MA(E-k)/close(E)). NaN-lenient."""
    close = tc["close"]
    L = tc["L"]
    c_E = close[E]
    if not np.isfinite(c_E) or c_E <= 0:
        return None
    log_c_E = np.log(c_E)
    mas = tc["daily_mas"]
    feat = np.full((N_MA, N_off), np.nan, dtype=np.float64)
    for k in range(N_off):
        idx = E - k
        if idx < 0:
            continue
        vals = mas[:, idx]
        ok = np.isfinite(vals) & (vals > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            feat[ok, k] = np.log(vals[ok]) - log_c_E
    return feat


def weekly_feat_at(tc, E, W_N):
    """(N_MA, W_N) log(MA(W-k)/anchor_close) where anchor_close = close[E-1].
    Anchor-week MA patched per-candidate (swap weekly_close[anchor_pos]
    scalar correction for SMA; re-step EMA from position anchor_pos-1).
    NaN-lenient."""
    close = tc["close"]
    if E < 1:
        return None
    anchor_close = close[E - 1]
    if not np.isfinite(anchor_close) or anchor_close <= 0:
        return None
    w_pos_all = tc["w_pos_all"]
    anchor_pos = int(w_pos_all[E - 1])
    weekly_close = tc["weekly_close"]
    weekly_mas = tc["weekly_mas"]
    n_weeks = tc["n_weeks"]
    log_anchor = np.log(anchor_close)
    feat = np.full((N_MA, W_N), np.nan, dtype=np.float64)

    # offsets k>=1: precomputed weekly MA at week (anchor_pos - k)
    for k in range(1, W_N):
        idx = anchor_pos - k
        if idx < 0:
            continue
        vals = weekly_mas[:, idx]
        ok = np.isfinite(vals) & (vals > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            feat[ok, k] = np.log(vals[ok]) - log_anchor

    # offset 0: anchor week MA with partial anchor close
    # SMA_p at anchor_pos: mean of weekly_close[anchor_pos - p + 1 .. anchor_pos - 1] + partial
    # EMA_p at anchor_pos: alpha * partial + (1 - alpha) * prev_EMA_p[anchor_pos - 1]
    ac_wc = weekly_close[anchor_pos] if 0 <= anchor_pos < n_weeks else np.nan
    prev_pos = anchor_pos - 1
    for i, p in enumerate(MA_PERIODS):
        # SMA
        sma_val = np.nan
        if anchor_pos >= p - 1 and anchor_pos < n_weeks:
            # need p-1 prior weekly closes before anchor_pos, all finite
            lo_idx = anchor_pos - p + 1
            prior = weekly_close[lo_idx:anchor_pos]  # p-1 values, weeks (anchor_pos - p + 1) .. (anchor_pos - 1)
            if len(prior) == p - 1 and np.all(np.isfinite(prior) & (prior > 0)):
                sma_val = (np.sum(prior) + anchor_close) / p
        if np.isfinite(sma_val) and sma_val > 0:
            with np.errstate(invalid='ignore', divide='ignore'):
                feat[i, 0] = np.log(sma_val) - log_anchor
        # EMA
        ema_val = np.nan
        alpha = 2.0 / (p + 1.0)
        if prev_pos >= 0 and prev_pos < n_weeks:
            prev_ema = weekly_mas[len(MA_PERIODS) + i, prev_pos]
            if np.isfinite(prev_ema) and prev_ema > 0:
                ema_val = alpha * anchor_close + (1 - alpha) * prev_ema
            elif prev_pos == 0 and np.isfinite(anchor_close):
                ema_val = anchor_close  # single-seed fallback
        if np.isfinite(ema_val) and ema_val > 0:
            with np.errstate(invalid='ignore', divide='ignore'):
                feat[len(MA_PERIODS) + i, 0] = np.log(ema_val) - log_anchor
    return feat


# ───────────── examples + market sample ─────────────

def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def collect_example_feats(setup, universe, tcache, W_N_max):
    """Returns (daily_feats, weekly_feats_W_N_max, kept_examples).
    Weekly feats at W_N_max size, slice per setup later."""
    examples = get_examples(setup)
    d_feats, w_feats, kept = [], [], []
    for ex in examples:
        t = ex["ticker"]
        df = universe.get(t)
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        if t not in tcache:
            tcache[t] = build_ticker_cache(df)
        tc = tcache[t]
        dff = daily_feat_at(tc, E)
        wff = weekly_feat_at(tc, E, W_N_max)
        if dff is None or wff is None:
            continue
        d_feats.append(dff)
        w_feats.append(wff)
        kept.append(ex)
    d_arr = np.stack(d_feats) if d_feats else None
    w_arr = np.stack(w_feats) if w_feats else None
    return d_arr, w_arr, kept


def sample_market_feats(universe, n_target, rng, W_N_max, tcache):
    tickers = list(universe.keys())
    d_feats, w_feats = [], []
    attempts = 0
    max_attempts = n_target * 10
    while len(d_feats) < n_target and attempts < max_attempts:
        t = tickers[rng.integers(0, len(tickers))]
        df = universe[t]
        L = len(df)
        if L < max(MA_PERIODS) + MAX_DAILY_OFFSET:
            attempts += 1
            continue
        ds = vsc.dates_as_str(df)
        eligible = np.where(
            (np.arange(L) >= max(MA_PERIODS) + MAX_DAILY_OFFSET) & (ds >= DATE_CUTOFF)
        )[0]
        if len(eligible) == 0:
            attempts += 1
            continue
        E = int(eligible[rng.integers(0, len(eligible))])
        if t not in tcache:
            tcache[t] = build_ticker_cache(df)
        tc = tcache[t]
        dff = daily_feat_at(tc, E)
        wff = weekly_feat_at(tc, E, W_N_max)
        if dff is None or wff is None:
            attempts += 1
            continue
        if not np.all(np.isfinite(dff)):
            attempts += 1
            continue
        d_feats.append(dff)
        w_feats.append(wff)
        attempts += 1
    return (np.stack(d_feats) if d_feats else None,
            np.stack(w_feats) if w_feats else None)


# ───────────── scoring + selection ─────────────

def reject_rate_matrix(ex_feats, mkt_feats):
    """(n_MA, n_off) reject rate = frac of market feats OUTSIDE example bounding box.
    NaN-lenient: cells where ex corridor is undefined → NaN score; market NaN → excluded."""
    with np.errstate(invalid='ignore'):
        lo = np.nanmin(ex_feats, axis=0)  # (n_MA, n_off)
        hi = np.nanmax(ex_feats, axis=0)
    n_MA, n_off = lo.shape
    reject = np.full((n_MA, n_off), np.nan, dtype=np.float64)
    for m in range(n_MA):
        for k in range(n_off):
            l = lo[m, k]; h = hi[m, k]
            if not (np.isfinite(l) and np.isfinite(h)):
                continue
            vals = mkt_feats[:, m, k]
            fin = np.isfinite(vals)
            if fin.sum() == 0:
                continue
            in_cor = (vals[fin] >= l) & (vals[fin] <= h)
            reject[m, k] = 1.0 - in_cor.mean()
    return reject


def kneedle_idx(y_sorted_asc):
    n = len(y_sorted_asc)
    if n < 3:
        return 0
    x = np.arange(n, dtype=np.float64)
    y = y_sorted_asc.astype(np.float64)
    dx = float(n - 1)
    dy = float(y[-1] - y[0])
    num = np.abs(dy * x - dx * y + dx * y[0])
    return int(np.argmax(num))


def select_meat_cells(reject):
    """High reject = meat. Sort ASCENDING, kneedle elbow → threshold;
    cells with reject >= threshold selected."""
    n_MA, n_off = reject.shape
    flat = reject.ravel()
    finite_idx = np.where(np.isfinite(flat))[0]
    if len(finite_idx) < 3:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    sorted_vals = np.sort(flat[finite_idx])
    k = kneedle_idx(sorted_vals)
    threshold = sorted_vals[k]
    selected_flat = finite_idx[flat[finite_idx] >= threshold]
    sel_ma = (selected_flat // n_off).astype(np.int64)
    sel_off = (selected_flat % n_off).astype(np.int64)
    return sel_ma, sel_off


def loo_stable_cells(ex_feats, mkt_feats):
    n_ex = ex_feats.shape[0]
    if n_ex < 3:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    base_score = reject_rate_matrix(ex_feats, mkt_feats)
    b_ma, b_off = select_meat_cells(base_score)
    stable = set(zip(b_ma.tolist(), b_off.tolist()))
    if not stable:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    for i in range(n_ex):
        mask = np.ones(n_ex, dtype=bool)
        mask[i] = False
        sub = ex_feats[mask]
        sc = reject_rate_matrix(sub, mkt_feats)
        ma_i, off_i = select_meat_cells(sc)
        stable &= set(zip(ma_i.tolist(), off_i.tolist()))
        if not stable:
            break
    if not stable:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    sel = sorted(stable)
    sel_ma = np.array([m for m, o in sel], dtype=np.int64)
    sel_off = np.array([o for m, o in sel], dtype=np.int64)
    return sel_ma, sel_off


def build_corridors(ex_feats, sel_ma, sel_off):
    n_sel = len(sel_ma)
    lo = np.zeros(n_sel, dtype=np.float64)
    hi = np.zeros(n_sel, dtype=np.float64)
    for i, (m, k) in enumerate(zip(sel_ma, sel_off)):
        vals = ex_feats[:, m, k]
        vals = vals[np.isfinite(vals)]
        if len(vals) < 2:
            lo[i] = -np.inf
            hi[i] = np.inf
            continue
        v = np.sort(vals)
        lo[i] = v[0] - (v[1] - v[0])
        hi[i] = v[-1] + (v[-1] - v[-2])
    return lo, hi


# ───────────── universe scan ─────────────

def scan_ticker(ticker, tc, df, W_N, sel_d_ma, sel_d_off, d_lo, d_hi,
                sel_w_ma, sel_w_off, w_lo, w_hi, date_cutoff_str, dates_str):
    close = tc["close"]
    L = tc["L"]
    if L < 2:
        return []
    eligible = np.where((np.arange(L) >= 1) & (dates_str >= date_cutoff_str))[0]
    if len(eligible) == 0:
        return []
    c_E = close[eligible]
    c_Em1 = close[eligible - 1]
    valid = np.isfinite(c_E) & (c_E > 0) & np.isfinite(c_Em1) & (c_Em1 > 0)
    eligible = eligible[valid]
    if len(eligible) == 0:
        return []

    # DAILY vectorized check
    daily_mas = tc["daily_mas"]
    log_close = np.log(np.where((close > 0) & np.isfinite(close), close, np.nan))
    if len(sel_d_ma) > 0:
        ok_d = np.ones(len(eligible), dtype=bool)
        for ci in range(len(sel_d_ma)):
            m = int(sel_d_ma[ci]); k = int(sel_d_off[ci])
            E_arr = eligible
            src = E_arr - k
            valid_src = src >= 0
            ma_vals = np.full(len(E_arr), np.nan)
            if valid_src.any():
                ma_vals[valid_src] = daily_mas[m, src[valid_src]]
            ma_ok = np.isfinite(ma_vals) & (ma_vals > 0)
            with np.errstate(invalid='ignore', divide='ignore'):
                log_ma = np.where(ma_ok, np.log(np.where(ma_ok, ma_vals, 1.0)), np.nan)
            f = log_ma - log_close[E_arr]
            in_cell = np.isnan(f) | ((f >= d_lo[ci]) & (f <= d_hi[ci]))
            ok_d &= in_cell
        eligible = eligible[ok_d]
        if len(eligible) == 0:
            return []

    # WEEKLY per-candidate (fast via tc's precomputed weekly MAs)
    passes = []
    for E in eligible:
        wf = weekly_feat_at(tc, int(E), W_N)
        if wf is None:
            continue
        ok_w = True
        for ci in range(len(sel_w_ma)):
            m = int(sel_w_ma[ci]); k = int(sel_w_off[ci])
            v = wf[m, k]
            if not np.isfinite(v):
                continue
            if v < w_lo[ci] or v > w_hi[ci]:
                ok_w = False
                break
        if ok_w:
            passes.append({"ticker": ticker, "date": dates_str[E], "E_idx": int(E)})
    return passes


def scan_ticker_fast(ticker, tc, df, W_N, sel_d_ma, sel_d_off, d_lo, d_hi,
                     sel_w_ma, sel_w_off, w_lo, w_hi, date_cutoff_str, dates_str):
    """Fully vectorized replacement for scan_ticker — daily + weekly via fancy
    indexing; weekly k==0 (anchor-week MA patch) handled per selected k0 cell
    but still vectorized across eligible bars."""
    close = tc["close"]
    L = tc["L"]
    if L < 2:
        return []
    eligible = np.where((np.arange(L) >= 1) & (dates_str >= date_cutoff_str))[0]
    if len(eligible) == 0:
        return []
    c_E = close[eligible]
    c_Em1 = close[eligible - 1]
    valid = np.isfinite(c_E) & (c_E > 0) & np.isfinite(c_Em1) & (c_Em1 > 0)
    eligible = eligible[valid]
    if len(eligible) == 0:
        return []

    log_close_all = np.log(np.where((close > 0) & np.isfinite(close), close, np.nan))

    # DAILY vectorized over cells
    n_d = len(sel_d_ma)
    if n_d > 0:
        daily_mas = tc["daily_mas"]
        log_close_E = log_close_all[eligible]
        src = eligible[:, None] - sel_d_off[None, :]  # (n_elig, n_d)
        valid_src = src >= 0
        src_safe = np.where(valid_src, src, 0)
        ma_matrix = daily_mas[sel_d_ma[None, :], src_safe]
        ma_matrix = np.where(valid_src, ma_matrix, np.nan)
        ma_ok = np.isfinite(ma_matrix) & (ma_matrix > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            log_ma = np.where(ma_ok, np.log(np.where(ma_ok, ma_matrix, 1.0)), np.nan)
        f = log_ma - log_close_E[:, None]
        in_cell = np.isnan(f) | ((f >= d_lo[None, :]) & (f <= d_hi[None, :]))
        ok_d = in_cell.all(axis=1)
        eligible = eligible[ok_d]
        if len(eligible) == 0:
            return []

    n_elig = len(eligible)

    # WEEKLY vectorized
    n_w = len(sel_w_ma)
    if n_w > 0:
        w_pos_all = tc["w_pos_all"]
        weekly_close = tc["weekly_close"]
        weekly_mas = tc["weekly_mas"]
        n_weeks = tc["n_weeks"]
        N_MA_PER = len(MA_PERIODS)

        anchor_close = close[eligible - 1]
        anchor_pos = w_pos_all[eligible - 1].astype(np.int64)
        log_anchor = np.log(anchor_close)

        is_k0 = (sel_w_off == 0)
        k_ge1_idx = np.where(~is_k0)[0]
        k_eq0_idx = np.where(is_k0)[0]

        w_ma_matrix = np.full((n_elig, n_w), np.nan)

        if len(k_ge1_idx) > 0:
            sub_ma = sel_w_ma[k_ge1_idx]
            sub_off = sel_w_off[k_ge1_idx]
            src = anchor_pos[:, None] - sub_off[None, :]
            valid_src = (src >= 0) & (src < n_weeks)
            src_safe = np.where(valid_src, src, 0)
            vals = weekly_mas[sub_ma[None, :], src_safe]
            vals = np.where(valid_src, vals, np.nan)
            w_ma_matrix[:, k_ge1_idx] = vals

        if len(k_eq0_idx) > 0:
            wc_finite = np.isfinite(weekly_close) & (weekly_close > 0)
            wc_safe = np.where(wc_finite, weekly_close, 0.0)
            wc_cumsum = np.concatenate([[0.0], np.cumsum(wc_safe)])
            wc_okcumsum = np.concatenate([[0], np.cumsum(wc_finite.astype(np.int64))])
            for ci in k_eq0_idx:
                m = int(sel_w_ma[ci])
                if m < N_MA_PER:
                    p = MA_PERIODS[m]
                    lo_idx = anchor_pos - p + 1
                    hi_idx = anchor_pos
                    valid_range = (lo_idx >= 0) & (anchor_pos < n_weeks) & (hi_idx <= n_weeks)
                    lo_safe = np.where(valid_range, lo_idx, 0)
                    hi_safe = np.where(valid_range, hi_idx, 0)
                    sum_prior = wc_cumsum[hi_safe] - wc_cumsum[lo_safe]
                    count_prior = wc_okcumsum[hi_safe] - wc_okcumsum[lo_safe]
                    all_ok = valid_range & (count_prior == (p - 1))
                    val = np.where(all_ok, (sum_prior + anchor_close) / p, np.nan)
                    w_ma_matrix[:, ci] = val
                else:
                    p = MA_PERIODS[m - N_MA_PER]
                    alpha = 2.0 / (p + 1.0)
                    prev_pos = anchor_pos - 1
                    valid_prev = (prev_pos >= 0) & (prev_pos < n_weeks)
                    prev_safe = np.where(valid_prev, prev_pos, 0)
                    prev_ema = weekly_mas[m, prev_safe]
                    prev_ok = valid_prev & np.isfinite(prev_ema) & (prev_ema > 0)
                    val = np.where(prev_ok, alpha * anchor_close + (1 - alpha) * prev_ema, np.nan)
                    w_ma_matrix[:, ci] = val

        w_ma_ok = np.isfinite(w_ma_matrix) & (w_ma_matrix > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            log_w_ma = np.where(w_ma_ok, np.log(np.where(w_ma_ok, w_ma_matrix, 1.0)), np.nan)
        f_w = log_w_ma - log_anchor[:, None]
        in_cell_w = np.isnan(f_w) | ((f_w >= w_lo[None, :]) & (f_w <= w_hi[None, :]))
        ok_w = in_cell_w.all(axis=1)
        eligible = eligible[ok_w]

    return [{"ticker": ticker, "date": dates_str[int(E)], "E_idx": int(E)} for E in eligible]


# ───────────── main ─────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(SEED)

    print(f"OHLCV cache: {CACHE_DIR}", flush=True)
    pkl = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    with open(pkl, "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print(f"FAIL: ticker count too low"); sys.exit(1)

    # Determine W_N per setup and shared W_N_max
    W_N_by_setup = {}
    for s in SETUPS:
        sa = json.load(open(os.path.join(STAGE_A_DIR, f"{s}_stage_a.json")))
        W_N_by_setup[s] = int(sa["W_N"])
    W_N_max = max(W_N_by_setup.values())
    print(f"W_N per setup: {W_N_by_setup}  W_N_max={W_N_max}", flush=True)

    tcache = {}  # per-ticker cache shared across setups

    # Shared market sample at W_N_max (slice per setup)
    t0 = time.time()
    print(f"\nSampling {N_MARKET_SAMPLE:,} market feature vectors (shared across setups)...", flush=True)
    d_mkt, w_mkt_full = sample_market_feats(universe, N_MARKET_SAMPLE, rng, W_N_max, tcache)
    print(f"  got daily {d_mkt.shape}  weekly {w_mkt_full.shape}  ({time.time()-t0:.1f}s)", flush=True)

    per_setup_state = {}
    for setup in SETUPS:
        W_N = W_N_by_setup[setup]
        print(f"\n=== {setup.upper()}  W_N={W_N}", flush=True)

        t0 = time.time()
        d_ex, w_ex_full, kept = collect_example_feats(setup, universe, tcache, W_N_max)
        if d_ex is None or w_ex_full is None:
            print("  no examples; skip"); continue
        w_ex = w_ex_full[:, :, :W_N]
        w_mkt = w_mkt_full[:, :, :W_N]
        print(f"  examples kept: {len(kept)}  daily {d_ex.shape}  weekly {w_ex.shape}  ({time.time()-t0:.1f}s)", flush=True)

        t0 = time.time()
        sel_d_ma, sel_d_off = loo_stable_cells(d_ex, d_mkt)
        sel_w_ma, sel_w_off = loo_stable_cells(w_ex, w_mkt)
        print(f"  daily stable cells: {len(sel_d_ma)} | weekly stable cells: {len(sel_w_ma)}  ({time.time()-t0:.1f}s)", flush=True)

        if len(sel_d_ma) == 0 and len(sel_w_ma) == 0:
            print("  zero selected cells — filter is null; skip")
            continue

        d_lo, d_hi = build_corridors(d_ex, sel_d_ma, sel_d_off)
        w_lo, w_hi = build_corridors(w_ex, sel_w_ma, sel_w_off)

        ex_pass_d = np.ones(len(kept), dtype=bool)
        for ci in range(len(sel_d_ma)):
            m = int(sel_d_ma[ci]); k = int(sel_d_off[ci])
            vals = d_ex[:, m, k]
            ok = np.isnan(vals) | ((vals >= d_lo[ci]) & (vals <= d_hi[ci]))
            ex_pass_d &= ok
        ex_pass_w = np.ones(len(kept), dtype=bool)
        for ci in range(len(sel_w_ma)):
            m = int(sel_w_ma[ci]); k = int(sel_w_off[ci])
            vals = w_ex[:, m, k]
            ok = np.isnan(vals) | ((vals >= w_lo[ci]) & (vals <= w_hi[ci]))
            ex_pass_w &= ok
        ex_pass = ex_pass_d & ex_pass_w
        print(f"  EX pass (self-check): daily {ex_pass_d.sum()}/{len(kept)}  weekly {ex_pass_w.sum()}/{len(kept)}  combined {ex_pass.sum()}/{len(kept)}", flush=True)

        d_labels = [f"{MA_TYPES[int(m)]}@E-{int(k)}" for m, k in zip(sel_d_ma, sel_d_off)]
        w_labels = [f"{MA_TYPES[int(m)]}@W-{int(k)}" for m, k in zip(sel_w_ma, sel_w_off)]
        print(f"  daily cells:  " + ", ".join(d_labels), flush=True)
        print(f"  weekly cells: " + ", ".join(w_labels), flush=True)

        per_setup_state[setup] = {
            "W_N": W_N,
            "kept_examples": kept,
            "sel_d_ma": sel_d_ma, "sel_d_off": sel_d_off,
            "sel_w_ma": sel_w_ma, "sel_w_off": sel_w_off,
            "d_lo": d_lo, "d_hi": d_hi,
            "w_lo": w_lo, "w_hi": w_hi,
            "ex_pass_d": int(ex_pass_d.sum()),
            "ex_pass_w": int(ex_pass_w.sum()),
            "ex_pass_combined": int(ex_pass.sum()),
        }
        with open(os.path.join(OUT_DIR, f"{setup}_selection.json"), "w") as f:
            json.dump({
                "setup": setup,
                "W_N": W_N,
                "n_ex_kept": len(kept),
                "daily_cells": d_labels,
                "weekly_cells": w_labels,
                "d_lo": d_lo.tolist(), "d_hi": d_hi.tolist(),
                "w_lo": w_lo.tolist(), "w_hi": w_hi.tolist(),
                "ex_pass_d_count": int(ex_pass_d.sum()),
                "ex_pass_w_count": int(ex_pass_w.sum()),
                "ex_pass_combined_count": int(ex_pass.sum()),
                "kept_examples": [{"ticker": e["ticker"], "entry_date": e["entry_date"]} for e in kept],
            }, f, indent=2)

    # === Universe scan phase ===
    scan_summary = {}
    for setup, st in per_setup_state.items():
        print(f"\n=== SCAN  {setup.upper()}", flush=True)
        sel_d_ma = st["sel_d_ma"]; sel_d_off = st["sel_d_off"]
        sel_w_ma = st["sel_w_ma"]; sel_w_off = st["sel_w_off"]
        d_lo = st["d_lo"]; d_hi = st["d_hi"]
        w_lo = st["w_lo"]; w_hi = st["w_hi"]
        W_N = st["W_N"]

        t0 = time.time()
        passes = []
        n_seen = 0
        for tk, df in universe.items():
            n_seen += 1
            if tk not in tcache:
                tcache[tk] = build_ticker_cache(df)
            tc = tcache[tk]
            ds = vsc.dates_as_str(df)
            res = scan_ticker(tk, tc, df, W_N,
                              sel_d_ma, sel_d_off, d_lo, d_hi,
                              sel_w_ma, sel_w_off, w_lo, w_hi,
                              DATE_CUTOFF, ds)
            passes.extend(res)
            if n_seen % 1000 == 0:
                dt = time.time() - t0
                print(f"  [{setup}] {n_seen}/{len(universe):,} tickers  {len(passes):,} passes  ({dt:.1f}s)", flush=True)
        dt = time.time() - t0
        print(f"  pre-dedup passes: {len(passes):,}  ({dt:.1f}s)", flush=True)

        # Dedup §5.2 per ticker
        example_keys = set()
        for e in st["kept_examples"]:
            if e["ticker"] in universe:
                E = vsc.lookup_idx(universe[e["ticker"]], e["entry_date"])
                if E >= 0:
                    example_keys.add((e["ticker"], E))

        by_ticker = {}
        for p in passes:
            by_ticker.setdefault(p["ticker"], []).append(p["E_idx"])
        dedup_total = 0
        ex_covered = 0
        for tk, E_list in by_ticker.items():
            ex_set = {e_idx for tkk, e_idx in example_keys if tkk == tk}
            E_list_sorted = sorted(E_list)
            deduped = vsc.dedupe_clusters(E_list_sorted, ex_set)
            dedup_total += len(deduped)
            ex_covered += sum(1 for e in ex_set if e in deduped)
        print(f"  post-dedup clusters: {dedup_total:,}", flush=True)
        print(f"  EX cluster coverage (end-to-end): {ex_covered}/{len(example_keys)}", flush=True)

        scan_summary[setup] = {
            "W_N": W_N,
            "n_ex_kept": len(st["kept_examples"]),
            "daily_cells": len(sel_d_ma),
            "weekly_cells": len(sel_w_ma),
            "pre_dedup_passes": len(passes),
            "post_dedup_clusters": dedup_total,
            "ex_pass_end_to_end": ex_covered,
            "ex_pass_selection_combined": st["ex_pass_combined"],
            "scan_seconds": dt,
        }

    with open(os.path.join(OUT_DIR, "scan_summary.json"), "w") as f:
        json.dump(scan_summary, f, indent=2)
    print(f"\n=== SUMMARY ===")
    print(json.dumps(scan_summary, indent=2))
    print(f"\nOutputs in {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
