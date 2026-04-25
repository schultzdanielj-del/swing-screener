"""Location axis — HTF descriptor derivation, filter build, universe scan,
and intersection with F1 clusters.

Five scale-invariant location descriptors at candidate E:

  D1  pos(M)       = (close[E] - min(close[E-M..E-1])) / (max - min)
  D2  trend(M)     = log(close[E-N-1] / close[E-N-M-1])        (pre-lead-in log-return)
  D3  tsh          = min{k>=1 : close[E-k] > close[E]}, capped at TSH_CAP
  D4a log_ath      = log(close[E] / max(close[0..E]))           (<= 0)
  D4b log_atl      = log(close[E] / min(close[0..E]))           (>= 0)
  D5  vol_ratio(M) = std(log_returns over last M bars) / std(log_returns over all prior)

Horizons M for D1, D2, D5 are derived per-descriptor by kneedle elbow on
spread(max-min over examples)-vs-M. Bounding regions are [min_ex, max_ex]
at the derived M*. NaN-lenient: an example's NaN doesn't constrain bounds.
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

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_DIR = os.path.join(WORKTREE, "research", "location_axis")
COMPARE_DIR = os.path.join(WORKTREE, "research", "visual_shape_compare")

SETUP = "htf"
N_BARS = 39
DATE_CUTOFF = "2020-01-02"
M_SWEEP = np.arange(10, 253)  # 10..252 inclusive — horizon candidates
TSH_CAP = 504
MIN_TICKER_COUNT = 11000


# ---------- helpers

def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def finite_nonzero_close(close):
    return np.isfinite(close) & (close > 0)


# ---------- per-ticker precomputation for full scan

def precompute_ticker(df):
    """Return per-ticker arrays needed for descriptors at any candidate E.

    Returns None if ticker has too few bars or no finite closes.
    """
    close = df["close"].values.astype(np.float64)
    L = len(close)
    if L < 3:
        return None
    finite = finite_nonzero_close(close)
    if finite.sum() < 2:
        return None

    log_close = np.where(finite, np.log(np.where(close > 0, close, 1.0)), np.nan)
    # returns[i] = log_close[i+1] - log_close[i]; length L-1; NaN-propagating
    log_returns = np.diff(log_close)

    # cumulative max and min of close up to each index (inclusive)
    # NaN-safe: use np.fmax / np.fmin with careful handling
    c_for_cum = np.where(finite, close, np.nan)
    cum_max = np.maximum.accumulate(np.where(finite, close, -np.inf))
    cum_min = np.minimum.accumulate(np.where(finite, close, np.inf))
    cum_max = np.where(np.isfinite(cum_max), cum_max, np.nan)
    cum_min = np.where(cum_min < np.inf, cum_min, np.nan)

    # Previous greater element (PGE) index: largest j < i such that close[j] > close[i].
    # Monotonic stack; complexity O(L).
    pge = np.full(L, -1, dtype=np.int64)
    stack = []
    for i in range(L):
        if not finite[i]:
            # drop candidate i; leave pge[i] = -1 sentinel
            while stack and close[stack[-1]] <= close[i] and finite[stack[-1]]:
                # close[i] is NaN here so comparison NaN — skip handling by just continuing
                stack.pop()
            # intentionally don't push NaN
            continue
        while stack and close[stack[-1]] <= close[i]:
            stack.pop()
        if stack:
            pge[i] = stack[-1]
        stack.append(i)

    # tsh[i] = i - pge[i], capped; if pge[i] == -1, tsh = min(i, TSH_CAP) (saturated going back to start)
    tsh = np.where(pge >= 0, np.minimum(np.arange(L) - pge, TSH_CAP),
                   np.minimum(np.arange(L), TSH_CAP)).astype(np.float64)
    tsh = np.where(finite, tsh, np.nan)

    return {
        "close": close,
        "log_close": log_close,
        "log_returns": log_returns,
        "cum_max": cum_max,
        "cum_min": cum_min,
        "tsh": tsh,
        "finite": finite,
        "L": L,
    }


# ---------- descriptor computations at a single E (used during horizon derivation)

def desc_1_pos(close, E, M):
    if E < M or E >= len(close):
        return np.nan
    window = close[E - M:E]
    if not np.all(np.isfinite(window)) or np.any(window <= 0):
        return np.nan
    c_E = close[E]
    if not np.isfinite(c_E):
        return np.nan
    lo, hi = window.min(), window.max()
    if hi <= lo:
        return np.nan
    return (c_E - lo) / (hi - lo)


def desc_2_trend(close, E, N, M):
    # log(close[E-N-1] / close[E-N-M-1])
    j1 = E - N - 1
    j2 = E - N - M - 1
    if j2 < 0 or j1 < 0 or j1 >= len(close):
        return np.nan
    c1, c2 = close[j1], close[j2]
    if not (np.isfinite(c1) and np.isfinite(c2)) or c1 <= 0 or c2 <= 0:
        return np.nan
    return float(np.log(c1 / c2))


def desc_3_tsh_single(close, E, cap=TSH_CAP):
    c_E = close[E]
    if not np.isfinite(c_E) or c_E <= 0:
        return np.nan
    max_k = min(E, cap)
    for k in range(1, max_k + 1):
        cj = close[E - k]
        if np.isfinite(cj) and cj > c_E:
            return float(k)
    return float(max_k)


def desc_4_ath_atl(close, E):
    c_E = close[E]
    if not np.isfinite(c_E) or c_E <= 0:
        return np.nan, np.nan
    prior = close[:E + 1]
    mask = np.isfinite(prior) & (prior > 0)
    if not mask.any():
        return np.nan, np.nan
    prior = prior[mask]
    ath = prior.max()
    atl = prior.min()
    if ath <= 0 or atl <= 0:
        return np.nan, np.nan
    return float(np.log(c_E / ath)), float(np.log(c_E / atl))


def desc_5_vol_ratio(log_returns, E, M):
    # last M returns before E: indices [E-M, E-1] -- but log_returns has length L-1 with
    # log_returns[i] = log(close[i+1]/close[i]). Return into bar E is log_returns[E-1].
    # So "last M returns" up to move-into-E = log_returns[E-M:E].
    if E < M + 1:
        return np.nan
    recent = log_returns[E - M:E]
    allprior = log_returns[:E]
    recent = recent[np.isfinite(recent)]
    allprior = allprior[np.isfinite(allprior)]
    if len(recent) < 2 or len(allprior) < 2:
        return np.nan
    v_r = float(np.std(recent, ddof=1))
    v_a = float(np.std(allprior, ddof=1))
    if v_a <= 0:
        return np.nan
    return v_r / v_a


# ---------- horizon derivation

def kneedle_elbow_idx(y):
    y = np.array(y, dtype=np.float64)
    n = len(y)
    if n < 3:
        return 0
    finite = np.isfinite(y)
    if not finite.any():
        return n // 2
    idxs = np.arange(n)
    if not finite.all():
        y = y.copy()
        y[~finite] = np.interp(idxs[~finite], idxs[finite], y[finite])
    x0, y0 = 0.0, float(y[0])
    x1, y1 = float(n - 1), float(y[-1])
    a = y1 - y0
    b = -(x1 - x0)
    c = x1 * y0 - x0 * y1
    denom = float(np.sqrt(a * a + b * b))
    if denom == 0:
        return n // 2
    xs = np.arange(n, dtype=np.float64)
    dist = np.abs(a * xs + b * y + c) / denom
    return int(np.argmax(dist))


def derive_horizon(ex_rows, compute_fn, name):
    """For each M in M_SWEEP, compute descriptor on all examples. Measure
    spread (max-min over finite values). Kneedle elbow on spread-vs-M.
    Returns (M*, spread_array, per_M_values)."""
    n_ex = len(ex_rows)
    values = np.full((len(M_SWEEP), n_ex), np.nan, dtype=np.float64)
    for mi, M in enumerate(M_SWEEP):
        for ei, r in enumerate(ex_rows):
            values[mi, ei] = compute_fn(r, int(M))
    # spread at each M, NaN-skipped
    spread = np.full(len(M_SWEEP), np.nan, dtype=np.float64)
    n_valid = np.zeros(len(M_SWEEP), dtype=np.int32)
    for mi in range(len(M_SWEEP)):
        v = values[mi][np.isfinite(values[mi])]
        n_valid[mi] = len(v)
        if len(v) >= 2:
            spread[mi] = float(v.max() - v.min())
    # kneedle on spread
    idx = kneedle_elbow_idx(spread)
    M_star = int(M_SWEEP[idx])
    print(f"  [{name}] M*={M_star}  spread@M*={spread[idx]:.4f}  "
          f"n_valid@M*={n_valid[idx]}/{n_ex}  "
          f"spread range [{np.nanmin(spread):.4f}, {np.nanmax(spread):.4f}]", flush=True)
    return M_star, spread, values


# ---------- bounding region

def bounds_at_M(ex_rows, compute_fn, M):
    vals = [compute_fn(r, int(M)) for r in ex_rows]
    vals = np.array(vals, dtype=np.float64)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return np.nan, np.nan, 0
    return float(finite.min()), float(finite.max()), len(finite)


def bounds_no_M(ex_rows, compute_fn_no_M):
    vals = [compute_fn_no_M(r) for r in ex_rows]
    vals = np.array(vals, dtype=np.float64)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return np.nan, np.nan, 0
    return float(finite.min()), float(finite.max()), len(finite)


# ---------- full-universe scan: per-candidate location-axis check

def scan_ticker(pre, N, M1, M2, M5, bounds, date_cutoff, dates_str):
    """For each valid candidate E in this ticker, check all 5 descriptors.

    E_min = N (matches F1's history requirement). Descriptors whose own
    history requirement isn't met for a given candidate return NaN, which
    passes the NaN-lenient bounds check.
    """
    close = pre["close"]
    log_returns = pre["log_returns"]
    cum_max = pre["cum_max"]
    cum_min = pre["cum_min"]
    tsh_arr = pre["tsh"]
    L = pre["L"]

    # Candidates: any E with close[E] valid, date >= cutoff. Descriptors
    # NaN-lenient per §4.5 for offsets beyond the candidate's reach.
    E_min = 1
    E_range = np.arange(E_min, L, dtype=np.int64)
    if len(E_range) == 0:
        return None

    cand_dates = dates_str[E_range]
    E_range = E_range[cand_dates >= date_cutoff]
    if len(E_range) == 0:
        return None

    n = len(E_range)
    c_E = close[E_range]

    # ---------- D1 pos(M1) — needs E >= M1
    D1 = np.full(n, np.nan, dtype=np.float64)
    can_D1 = E_range >= M1
    if can_D1.any() and L >= M1:
        c_clean = np.where(np.isfinite(close) & (close > 0), close, np.nan)
        swv_M1 = np.lib.stride_tricks.sliding_window_view(c_clean, int(M1))
        eligible_E = E_range[can_D1]
        sw_idx = eligible_E - M1
        in_range = (sw_idx >= 0) & (sw_idx < swv_M1.shape[0])
        window = swv_M1[np.clip(sw_idx, 0, swv_M1.shape[0] - 1)]
        w_min = np.nanmin(window, axis=1)
        w_max = np.nanmax(window, axis=1)
        enough = np.sum(np.isfinite(window), axis=1) >= 2
        rng = w_max - w_min
        c_el = close[eligible_E]
        with np.errstate(invalid='ignore', divide='ignore'):
            vals = np.where(
                in_range & enough & (rng > 0) & np.isfinite(c_el) & (c_el > 0),
                (c_el - w_min) / rng, np.nan
            )
        D1[can_D1] = vals

    # ---------- D2 trend(M2) — needs E >= N + M2 + 1
    D2 = np.full(n, np.nan, dtype=np.float64)
    can_D2 = E_range >= (N + M2 + 1)
    if can_D2.any():
        eligible_E = E_range[can_D2]
        j1 = eligible_E - N - 1
        j2 = eligible_E - N - M2 - 1
        c1 = close[np.clip(j1, 0, L - 1)]
        c2 = close[np.clip(j2, 0, L - 1)]
        with np.errstate(invalid='ignore', divide='ignore'):
            vals = np.where(
                (j2 >= 0) & np.isfinite(c1) & np.isfinite(c2) & (c1 > 0) & (c2 > 0),
                np.log(c1 / c2), np.nan
            )
        D2[can_D2] = vals

    # ---------- D3 tsh — precomputed, always available
    D3 = tsh_arr[E_range]

    # ---------- D4a / D4b — cum_max / cum_min up to E
    cmax = cum_max[E_range]
    cmin = cum_min[E_range]
    with np.errstate(invalid='ignore', divide='ignore'):
        D4a = np.where(np.isfinite(cmax) & np.isfinite(c_E) & (cmax > 0) & (c_E > 0),
                       np.log(c_E / cmax), np.nan)
        D4b = np.where(np.isfinite(cmin) & np.isfinite(c_E) & (cmin > 0) & (c_E > 0),
                       np.log(c_E / cmin), np.nan)

    # ---------- D5 vol_ratio(M5) — needs E >= M5 + 1
    D5 = np.full(n, np.nan, dtype=np.float64)
    can_D5 = E_range >= (M5 + 1)
    if can_D5.any() and L - 1 >= M5:
        eligible_E = E_range[can_D5]
        swv_ret = np.lib.stride_tricks.sliding_window_view(log_returns, int(M5))
        sw_idx_r = eligible_E - M5
        in_range_r = (sw_idx_r >= 0) & (sw_idx_r < swv_ret.shape[0])
        recent_win = swv_ret[np.clip(sw_idx_r, 0, swv_ret.shape[0] - 1)]
        with np.errstate(invalid='ignore'):
            recent_std = np.nanstd(recent_win, axis=1, ddof=1)

        lr = log_returns
        lr_finite = np.isfinite(lr)
        lr_clean = np.where(lr_finite, lr, 0.0)
        lr_sq = np.where(lr_finite, lr * lr, 0.0)
        cnt = np.cumsum(lr_finite.astype(np.int64))
        sx = np.cumsum(lr_clean)
        sx2 = np.cumsum(lr_sq)
        idx_lr = np.clip(eligible_E - 1, 0, len(lr) - 1)
        ncount = cnt[idx_lr]
        sxv = sx[idx_lr]
        sx2v = sx2[idx_lr]
        with np.errstate(invalid='ignore', divide='ignore'):
            mean_all = sxv / np.where(ncount > 0, ncount, 1)
            var_all = (sx2v - ncount * (mean_all ** 2)) / np.where(ncount > 1, ncount - 1, 1)
            long_std = np.where(ncount >= 2, np.sqrt(np.where(var_all >= 0, var_all, 0.0)), np.nan)
        with np.errstate(invalid='ignore', divide='ignore'):
            vals = np.where(
                in_range_r & np.isfinite(recent_std) & np.isfinite(long_std) & (long_std > 0),
                recent_std / long_std, np.nan
            )
        D5[can_D5] = vals

    # Apply bounds — NaN-lenient, with a small relative epsilon to absorb
    # floating-point drift between how bounds are computed (direct std etc.)
    # and how the scan computes descriptors (cumulative sums).
    def in_bounds(x, lo, hi):
        scale = max(abs(lo), abs(hi), 1.0)
        eps = 1e-10 * scale
        with np.errstate(invalid='ignore'):
            return np.isnan(x) | ((x >= lo - eps) & (x <= hi + eps))

    p1 = in_bounds(D1, *bounds["D1"])
    p2 = in_bounds(D2, *bounds["D2"])
    p3 = in_bounds(D3, *bounds["D3"])
    p4a = in_bounds(D4a, *bounds["D4a"])
    p4b = in_bounds(D4b, *bounds["D4b"])
    p5 = in_bounds(D5, *bounds["D5"])
    all_pass = p1 & p2 & p3 & p4a & p4b & p5

    return {
        "E": E_range,
        "D1": D1, "D2": D2, "D3": D3, "D4a": D4a, "D4b": D4b, "D5": D5,
        "p1": p1, "p2": p2, "p3": p3, "p4a": p4a, "p4b": p4b, "p5": p5,
        "all_pass": all_pass,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    # Load OHLCV
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe tickers: {len(universe)}", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print("FAIL: ticker count too low")
        sys.exit(1)

    # Load examples
    examples = get_examples(SETUP)
    ex_rows = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E_idx = lookup_idx(df, ex["entry_date"])
        if E_idx < 0:
            continue
        close = df["close"].values.astype(np.float64)
        log_close = np.log(np.where(close > 0, close, np.nan))
        log_returns = np.diff(log_close)
        ex_rows.append({
            "ticker": ex["ticker"],
            "entry_date": ex["entry_date"],
            "E_idx": E_idx,
            "close": close,
            "log_close": log_close,
            "log_returns": log_returns,
        })
    n_ex = len(ex_rows)
    print(f"Valid examples: {n_ex}", flush=True)

    # Derive horizons for D1, D2, D5
    print("\nDeriving horizons...", flush=True)
    fn_d1 = lambda r, M: desc_1_pos(r["close"], r["E_idx"], M)
    fn_d2 = lambda r, M: desc_2_trend(r["close"], r["E_idx"], N_BARS, M)
    fn_d5 = lambda r, M: desc_5_vol_ratio(r["log_returns"], r["E_idx"], M)
    M1_star, s1, _ = derive_horizon(ex_rows, fn_d1, "D1 pos")
    M2_star, s2, _ = derive_horizon(ex_rows, fn_d2, "D2 trend")
    M5_star, s5, _ = derive_horizon(ex_rows, fn_d5, "D5 vol_ratio")

    # Build bounds
    print("\nBuilding bounds at derived M*...", flush=True)
    D1_lo, D1_hi, n1 = bounds_at_M(ex_rows, fn_d1, M1_star)
    D2_lo, D2_hi, n2 = bounds_at_M(ex_rows, fn_d2, M2_star)
    D5_lo, D5_hi, n5 = bounds_at_M(ex_rows, fn_d5, M5_star)
    # D3 / D4 no horizon
    D3_lo, D3_hi, n3 = bounds_no_M(ex_rows, lambda r: desc_3_tsh_single(r["close"], r["E_idx"]))
    D4_vals = [desc_4_ath_atl(r["close"], r["E_idx"]) for r in ex_rows]
    D4a_vals = np.array([v[0] for v in D4_vals])
    D4b_vals = np.array([v[1] for v in D4_vals])
    D4a_lo = float(np.nanmin(D4a_vals)); D4a_hi = float(np.nanmax(D4a_vals))
    D4b_lo = float(np.nanmin(D4b_vals)); D4b_hi = float(np.nanmax(D4b_vals))

    bounds = {
        "D1":  (D1_lo, D1_hi),
        "D2":  (D2_lo, D2_hi),
        "D3":  (D3_lo, D3_hi),
        "D4a": (D4a_lo, D4a_hi),
        "D4b": (D4b_lo, D4b_hi),
        "D5":  (D5_lo, D5_hi),
    }
    print(f"  D1  pos(M={M1_star})       bounds [{D1_lo:.4f}, {D1_hi:.4f}]   n_ex={n1}/{n_ex}", flush=True)
    print(f"  D2  trend(M={M2_star})     bounds [{D2_lo:.4f}, {D2_hi:.4f}]   n_ex={n2}/{n_ex}", flush=True)
    print(f"  D3  tsh                    bounds [{D3_lo:.0f}, {D3_hi:.0f}]       n_ex={n3}/{n_ex}", flush=True)
    print(f"  D4a log_ath                bounds [{D4a_lo:.4f}, {D4a_hi:.4f}]", flush=True)
    print(f"  D4b log_atl                bounds [{D4b_lo:.4f}, {D4b_hi:.4f}]", flush=True)
    print(f"  D5  vol_ratio(M={M5_star}) bounds [{D5_lo:.4f}, {D5_hi:.4f}]   n_ex={n5}/{n_ex}", flush=True)

    # Sanity — every example passes the location filter (100% EX pass)
    print("\nSanity: 100% EX pass check on location axis...", flush=True)
    ex_pass_count = 0
    for r in ex_rows:
        v1 = desc_1_pos(r["close"], r["E_idx"], M1_star)
        v2 = desc_2_trend(r["close"], r["E_idx"], N_BARS, M2_star)
        v3 = desc_3_tsh_single(r["close"], r["E_idx"])
        v4a, v4b = desc_4_ath_atl(r["close"], r["E_idx"])
        v5 = desc_5_vol_ratio(r["log_returns"], r["E_idx"], M5_star)
        def ok(x, lo, hi):
            return (not np.isfinite(x)) or (lo <= x <= hi)
        pass1 = ok(v1, *bounds["D1"])
        pass2 = ok(v2, *bounds["D2"])
        pass3 = ok(v3, *bounds["D3"])
        pass4a = ok(v4a, *bounds["D4a"])
        pass4b = ok(v4b, *bounds["D4b"])
        pass5 = ok(v5, *bounds["D5"])
        if pass1 and pass2 and pass3 and pass4a and pass4b and pass5:
            ex_pass_count += 1
        else:
            print(f"    EX FAIL: {r['ticker']} {r['entry_date']}  "
                  f"D1={v1} D2={v2} D3={v3} D4a={v4a} D4b={v4b} D5={v5}", flush=True)
    print(f"  EX pass: {ex_pass_count}/{n_ex}", flush=True)
    if ex_pass_count != n_ex:
        print("FAIL: location axis rejects some examples — bounds logic bug. Aborting.", flush=True)
        sys.exit(1)

    # Full-universe scan
    print(f"\nFull-universe location scan, date >= {DATE_CUTOFF}...", flush=True)
    tickers = sorted(universe.keys())
    per_ticker_ex = {}
    for r in ex_rows:
        per_ticker_ex.setdefault(r["ticker"], set()).add(int(r["E_idx"]))

    # per-descriptor drop counters
    total = 0
    kept_d1 = kept_d2 = kept_d3 = kept_d4a = kept_d4b = kept_d5 = 0
    passed = 0
    loc_per_ticker = {}

    t0 = time.time()
    for i, ticker in enumerate(tickers):
        df = universe.get(ticker)
        if df is None:
            continue
        pre = precompute_ticker(df)
        if pre is None:
            continue
        ds = dates_as_str(df)
        res = scan_ticker(pre, N_BARS, M1_star, M2_star, M5_star, bounds, DATE_CUTOFF, ds)
        if res is None:
            continue
        total += len(res["E"])
        kept_d1 += int(res["p1"].sum())
        kept_d2 += int(res["p2"].sum())
        kept_d3 += int(res["p3"].sum())
        kept_d4a += int(res["p4a"].sum())
        kept_d4b += int(res["p4b"].sum())
        kept_d5 += int(res["p5"].sum())
        ap = res["all_pass"]
        passed += int(ap.sum())
        if ap.any():
            loc_per_ticker[ticker] = [int(res["E"][k]) for k in np.where(ap)[0]]
        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{len(tickers)} tickers ({time.time() - t0:.1f}s)  "
                  f"candidates: {total:,}  loc-pass: {passed:,}",
                  flush=True)
    print(f"\nScan complete in {time.time() - t0:.1f}s", flush=True)

    # Dedup per §5.2
    from itertools import chain
    # import here to avoid clutter
    def dedupe_clusters(E_list, example_E_set):
        if not E_list:
            return []
        E_list = sorted(E_list)
        reps = []
        run_start = E_list[0]
        run_end = E_list[0]

        def emit(rs, re):
            exs_in_run = sorted(e for e in example_E_set if rs <= e <= re)
            if not exs_in_run:
                reps.append(re)
                return
            cursor = rs
            for ex_e in exs_in_run:
                reps.append(ex_e)
                cursor = ex_e + 1
            if cursor <= re:
                reps.append(re)

        for E in E_list[1:]:
            if E == run_end + 1:
                run_end = E
            else:
                emit(run_start, run_end)
                run_start = E
                run_end = E
        emit(run_start, run_end)
        return reps

    loc_clusters = []
    for ticker, E_list in loc_per_ticker.items():
        ex_set = per_ticker_ex.get(ticker, set())
        reps = dedupe_clusters(E_list, ex_set)
        for r in reps:
            loc_clusters.append((ticker, r, r in ex_set))

    ex_match_loc = sum(1 for _, _, m in loc_clusters if m)
    non_ex_loc = len(loc_clusters) - ex_match_loc

    def pct(n, d):
        return (n / d * 100.0) if d > 0 else 0.0

    print(f"\n=== LOCATION AXIS — HTF, full universe, date >= {DATE_CUTOFF} ===", flush=True)
    print(f"Total candidates:  {total:,}", flush=True)
    print(f"Per-descriptor KEEP counts (higher = more permissive = that descriptor doing less carving):", flush=True)
    print(f"  D1  pos                kept {kept_d1:>12,}  ({pct(kept_d1, total):.2f}%)", flush=True)
    print(f"  D2  trend              kept {kept_d2:>12,}  ({pct(kept_d2, total):.2f}%)", flush=True)
    print(f"  D3  tsh                kept {kept_d3:>12,}  ({pct(kept_d3, total):.2f}%)", flush=True)
    print(f"  D4a log_ath            kept {kept_d4a:>12,}  ({pct(kept_d4a, total):.2f}%)", flush=True)
    print(f"  D4b log_atl            kept {kept_d4b:>12,}  ({pct(kept_d4b, total):.2f}%)", flush=True)
    print(f"  D5  vol_ratio          kept {kept_d5:>12,}  ({pct(kept_d5, total):.2f}%)", flush=True)
    print(f"Joint (all 5 pass):     {passed:>12,}  ({pct(passed, total):.4f}% bar carve-through)", flush=True)
    print(f"Location clusters (§5.2):  {len(loc_clusters):,}  (example-matched: {ex_match_loc}, non-example: {non_ex_loc})", flush=True)

    # Intersection with F1 clusters
    f1_path = os.path.join(COMPARE_DIR, "F1_clusters.csv")
    if os.path.exists(f1_path):
        f1 = pd.read_csv(f1_path)
        # Build set of F1 (ticker, E_idx) pairs
        f1_keys = set(zip(f1["ticker"].astype(str), f1["E_idx"].astype(int)))
        loc_keys = set((t, e) for (t, e, _) in loc_clusters)
        both = f1_keys & loc_keys
        print(f"\n=== F1 INTERSECT LOCATION (cluster-level intersection) ===", flush=True)
        print(f"  F1 clusters:        {len(f1_keys):,}", flush=True)
        print(f"  Location clusters:  {len(loc_keys):,}", flush=True)
        print(f"  F1 INTERSECT Location:      {len(both):,}", flush=True)
        # Example-matched in intersection
        ex_keys = {(r["ticker"], int(r["E_idx"])) for r in ex_rows}
        ex_in_both = both & ex_keys
        print(f"  ex-matched in INTERSECT:    {len(ex_in_both)} / {n_ex}", flush=True)

    # Save summary
    summary = {
        "setup": SETUP,
        "date_cutoff": DATE_CUTOFF,
        "n_examples_valid": n_ex,
        "horizons": {
            "M1_pos": M1_star,
            "M2_trend": M2_star,
            "M5_vol_ratio": M5_star,
            "TSH_CAP": TSH_CAP,
        },
        "bounds": {k: list(v) for k, v in bounds.items()},
        "ex_pass_location": ex_pass_count,
        "total_candidates": int(total),
        "per_descriptor_keep": {
            "D1": int(kept_d1), "D2": int(kept_d2), "D3": int(kept_d3),
            "D4a": int(kept_d4a), "D4b": int(kept_d4b), "D5": int(kept_d5),
        },
        "joint_pass_bars": int(passed),
        "location_clusters": len(loc_clusters),
        "location_clusters_example_matched": ex_match_loc,
        "location_clusters_non_example": non_ex_loc,
    }
    with open(os.path.join(OUT_DIR, "summary.json"), 'w') as f:
        json.dump(summary, f, indent=2)

    # Save location cluster csv — cache dates per ticker to avoid 600k redundant parses.
    rows = []
    date_cache = {}
    for ticker, E, is_ex in sorted(loc_clusters):
        df = universe.get(ticker)
        if df is None:
            continue
        ds = date_cache.get(ticker)
        if ds is None:
            ds = dates_as_str(df)
            date_cache[ticker] = ds
        date = ds[E] if E < len(ds) else ""
        rows.append({"ticker": ticker, "E_idx": E, "date": date, "example_matched": is_ex})
    if rows:
        pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, "location_clusters.csv"), index=False)

    print(f"\nWrote summary + clusters to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
