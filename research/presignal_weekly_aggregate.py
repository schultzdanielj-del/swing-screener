"""Weekly aggregation for the presignal grinder weekly pass.

Strict-pre-signal anchor rule:
  - Anchor week = the ISO week containing date[E-1], closed at close[E-1].
  - Weekly-bar-k-back = the ISO week `anchor_iso_week - k`, closed at the
    last trading-day close in that week.

So `weekly_back[0] = close[E-1]` (possibly a partial Mon-through-(E-1) bar
if E-1 is mid-week), and `weekly_back[k]` for k>=1 is the last trading-day
close in ISO week `anchor_iso_week - k`. If E falls on a Monday, anchor_week
is the prior full week and `weekly_back[0] = close[E-1] =` prior Friday close.

All helpers here are used both for building example filters and for scanning
candidates in the full universe later.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _dates_as_datetimeindex(df):
    dates_arr = df["date"].values
    if pd.api.types.is_datetime64_any_dtype(dates_arr):
        return pd.DatetimeIndex(dates_arr)
    return pd.to_datetime(dates_arr)


def compute_weekly_back(df, E_idx, W_max_target=None):
    """From a daily OHLCV df and daily entry index E, build the pre-signal
    weekly-close series anchored at close[E-1].

    Returns a numpy array:
        [anchor_close, close_1_week_back, close_2_weeks_back, ...]
    With W_max_target=None (default), returns the FULL available pre-signal
    weekly history — needed so D4a/b (ATH/ATL over all history) and D5
    (vol ratio vs. all prior returns) match between Stage A and Stage B.
    With W_max_target set, returns at most W_max_target+1 entries.

    Returns None only on data-quality failure: E_idx < 1, invalid anchor close.
    Individual pre-anchor closes that are non-finite / non-positive propagate
    as NaN in the output (NaN-lenient per §4.5).
    """
    if E_idx < 1:
        return None
    close = df["close"].values.astype(np.float64)
    if E_idx >= len(close):
        return None
    anchor_close = close[E_idx - 1]
    if not np.isfinite(anchor_close) or anchor_close <= 0:
        return None

    dates = _dates_as_datetimeindex(df)
    # ISO week start (Monday). pd.Period('W') ends Sunday by default, start_time=Monday.
    week_start = dates.to_period('W').start_time
    ws_ns = week_start.values.astype('datetime64[ns]').astype(np.int64)

    # Restrict to indices 0..E-1 (strict pre-signal)
    prior_ws = ws_ns[:E_idx]

    # Since daily data is chronological, prior_ws is monotone non-decreasing.
    # unique() gives sorted unique week starts; first_idxs is the first daily
    # index in each week; last daily index in each week = next_first_idx - 1.
    unique_ws, first_idxs = np.unique(prior_ws, return_index=True)
    last_idxs = np.concatenate([first_idxs[1:], [E_idx]]) - 1

    anchor_ws = ws_ns[E_idx - 1]
    # anchor_ws is guaranteed to be in unique_ws (it's from prior_ws[E-1]).
    anchor_pos = int(np.searchsorted(unique_ws, anchor_ws))

    if W_max_target is None:
        actual_W = anchor_pos
    else:
        actual_W = min(W_max_target, anchor_pos)
    if actual_W < 1:
        # No history before anchor — still valid: return a length-1 array with
        # just the anchor. Descriptors/F1 will NaN-lenient auto-pass.
        return np.array([anchor_close], dtype=np.float64)

    weekly_back = np.empty(actual_W + 1, dtype=np.float64)
    weekly_back[0] = anchor_close
    for k in range(1, actual_W + 1):
        daily_idx = int(last_idxs[anchor_pos - k])
        c = close[daily_idx]
        if np.isfinite(c) and c > 0:
            weekly_back[k] = c
        else:
            weekly_back[k] = np.nan  # propagate NaN, don't drop
    return weekly_back


def weekly_path(weekly_back, W_N):
    """F1-compatible weekly path of length W_N, matching daily convention:
    path[k] = log(weekly_back[k+1] / weekly_back[0]) for k=0..W_N-1.
    path[0] is one-week-back vs anchor; path[W_N-1] is W_N weeks back.
    """
    anchor = weekly_back[0]
    return np.log(weekly_back[1:W_N + 1]) - np.log(anchor)


def weekly_ex_row(ticker, entry_date, weekly_back):
    """location_axis-compatible row dict, built on the weekly series.

    Converts weekly_back (anchor-first, then going back in time) into a
    chronologically ordered close array with the anchor at the last index,
    so that location_axis descriptor functions treat E_idx = len-1 as the
    anchor bar.
    """
    # Reverse: anchor-last chronological order
    weekly_chrono = weekly_back[::-1].copy()
    n = len(weekly_chrono)
    log_close = np.log(np.where(weekly_chrono > 0, weekly_chrono, np.nan))
    log_returns = np.diff(log_close)
    return {
        "ticker": ticker,
        "entry_date": entry_date,
        "E_idx": n - 1,  # anchor is the last entry (chronological)
        "close": weekly_chrono,
        "log_close": log_close,
        "log_returns": log_returns,
    }


# ---------------- scanner primitives (Stage B) ----------------

def build_ticker_weekly_indexing(df):
    """Pre-compute per-ticker ISO-week indexing used by weekly full-universe
    scanning. Keyed by daily-index domain; mapping into the ticker's weekly
    sequence is via `w_pos_all`.

    Returns a dict with:
      close          (L,)       daily closes
      ws_ns          (L,)       ISO-week-start nanoseconds per daily index
      unique_ws      (n_weeks,) sorted unique ISO-week starts for this ticker
      w_pos_all      (L,)       position of each daily index's week in unique_ws
      week_end_idx   (n_weeks,) largest daily index with week == unique_ws[p]
      weekly_close   (n_weeks,) daily close at each week_end_idx (= full-week close)
      L, n_weeks
    """
    close = df["close"].values.astype(np.float64)
    L = len(close)
    dates = _dates_as_datetimeindex(df)
    week_start = dates.to_period('W').start_time
    ws_ns = week_start.values.astype('datetime64[ns]').astype(np.int64)

    # ws_ns is monotone non-decreasing (daily data is chronological).
    unique_ws, first_idxs = np.unique(ws_ns, return_index=True)
    # week_end_idx across full series (not restricted to any E)
    week_end_idx = np.concatenate([first_idxs[1:], [L]]).astype(np.int64) - 1

    # w_pos_all[d] = index of ws_ns[d] in unique_ws. Since unique_ws is sorted
    # and ws_ns[d] is present in it by construction, searchsorted works.
    w_pos_all = np.searchsorted(unique_ws, ws_ns)

    weekly_close = close[week_end_idx]

    return {
        "close": close,
        "ws_ns": ws_ns,
        "unique_ws": unique_ws,
        "w_pos_all": w_pos_all,
        "week_end_idx": week_end_idx,
        "weekly_close": weekly_close,
        "L": L,
        "n_weeks": len(unique_ws),
    }


def _previous_greater_element_idx(series):
    """For each position i, return the largest j < i such that series[j] > series[i].
    If none exists, -1. NaN-safe: NaN entries get pge = -1 (and don't appear on stack).
    """
    n = len(series)
    pge = np.full(n, -1, dtype=np.int64)
    stack = []
    for i in range(n):
        v = series[i]
        if not np.isfinite(v):
            continue
        while stack and series[stack[-1]] <= v:
            stack.pop()
        if stack:
            pge[i] = stack[-1]
        stack.append(i)
    return pge


def scan_ticker_weekly(idx, W_N, W_M1, W_M2, W_M5, tsh_cap_w,
                       f1_hulls, loc_bounds, date_cutoff_str, dates_str):
    """Full-universe-ready per-ticker weekly scan.

    Candidates: every daily index d with d >= 1, dates_str[d] >= date_cutoff_str,
    and the candidate's anchor_pos_w (= w_pos_all[d - 1]) >= W_N so that W_N
    weekly bars of history are defined.

    Returns None if no eligible candidates. Otherwise dict with:
      E        (n,)   eligible daily indices (after all gates)
      f1       (n,)   bool — weekly F1 pass
      p1..p5   (n,)   bool — weekly Location per-descriptor pass (NaN-lenient)
      all_pass (n,)   bool — f1 & p1 & p2 & p3 & p4a & p4b & p5
    """
    close = idx["close"]
    ws_ns = idx["ws_ns"]
    w_pos_all = idx["w_pos_all"]
    week_end_idx = idx["week_end_idx"]
    weekly_close = idx["weekly_close"]
    L = idx["L"]
    n_weeks = idx["n_weeks"]

    empty = np.array([], dtype=np.int64)
    if L < 2 or n_weeks < 1:
        return None

    # Candidates: any d in 1..L-1 with date >= cutoff and anchor close finite.
    # NO minimum weekly-reach gate — short-history candidates are checked
    # NaN-lenient on the offsets they can reach and auto-pass offsets they
    # can't. Mirrors live-trading semantics (§4.5).
    d_all = np.arange(1, L, dtype=np.int64)
    if len(d_all) == 0:
        return None
    anchor_pos = w_pos_all[d_all - 1]
    elig = (dates_str[d_all] >= date_cutoff_str)
    if not elig.any():
        return None
    d = d_all[elig]
    anchor_pos = anchor_pos[elig]
    anchor_close = close[d - 1]
    valid = np.isfinite(anchor_close) & (anchor_close > 0)
    if not valid.any():
        return None
    d = d[valid]
    anchor_pos = anchor_pos[valid]
    anchor_close = anchor_close[valid]
    n = len(d)

    # Weekly F1 paths: (n, W_N), NaN where candidate doesn't reach offset.
    # back_pos can be negative — those entries are NaN by construction.
    back_pos = anchor_pos[:, None] - np.arange(1, W_N + 1)[None, :]  # (n, W_N)
    reachable = back_pos >= 0
    safe_idx = np.clip(back_pos, 0, n_weeks - 1)
    wc_lookup = weekly_close[safe_idx]
    wc_valid = reachable & np.isfinite(wc_lookup) & (wc_lookup > 0)
    wc_back = np.where(wc_valid, wc_lookup, np.nan)  # NaN for unreached or bad closes

    paths = np.log(wc_back) - np.log(anchor_close)[:, None]  # (n, W_N), NaN-lenient
    # check_F1_batch is NaN-lenient: NaN pairs auto-pass, None hulls auto-pass.
    import visual_shape_compare as _vsc
    f1 = _vsc.check_F1_batch(paths, f1_hulls)

    # ---- Location descriptors (NaN-lenient) ----
    # D1 pos(M1): last W_M1 weekly closes at positions anchor_pos - W_M1 .. anchor_pos - 1
    D1 = np.full(n, np.nan, dtype=np.float64)
    can_D1 = anchor_pos >= W_M1
    if can_D1.any() and n_weeks >= W_M1:
        swv = np.lib.stride_tricks.sliding_window_view(weekly_close, int(W_M1))
        sw_start = anchor_pos[can_D1] - W_M1
        in_range = (sw_start >= 0) & (sw_start < swv.shape[0])
        window = swv[np.clip(sw_start, 0, swv.shape[0] - 1)]
        w_min = np.nanmin(window, axis=1)
        w_max = np.nanmax(window, axis=1)
        rng = w_max - w_min
        c_el = anchor_close[can_D1]
        with np.errstate(invalid='ignore', divide='ignore'):
            vals = np.where(
                in_range & (rng > 0) & np.isfinite(c_el) & (c_el > 0),
                (c_el - w_min) / rng, np.nan
            )
        D1[can_D1] = vals

    # D2 trend(N, M2) on weekly: log(close[E - N - 1] / close[E - N - M2 - 1]) where E = len-1 of the chronological weekly series.
    # In scan terms: we want weekly_close at positions (anchor_pos - W_N - 1) and (anchor_pos - W_N - W_M2 - 1).
    D2 = np.full(n, np.nan, dtype=np.float64)
    can_D2 = anchor_pos >= (W_N + W_M2 + 1)
    if can_D2.any():
        p1 = anchor_pos[can_D2] - W_N - 1
        p2 = anchor_pos[can_D2] - W_N - W_M2 - 1
        c1 = weekly_close[np.clip(p1, 0, n_weeks - 1)]
        c2 = weekly_close[np.clip(p2, 0, n_weeks - 1)]
        with np.errstate(invalid='ignore', divide='ignore'):
            vals = np.where(
                (p2 >= 0) & np.isfinite(c1) & np.isfinite(c2) & (c1 > 0) & (c2 > 0),
                np.log(c1 / c2), np.nan
            )
        D2[can_D2] = vals

    # D3 tsh(cap W): weeks since weekly_close was last > anchor_close, capped at tsh_cap_w.
    # Vectorized per-k: advance k = 1..tsh_cap_w, record first hit per candidate.
    # O(n * cap) but with numpy vector ops per k — fast vs. Python nested loop.
    D3 = np.full(n, np.nan, dtype=np.float64)
    first_k = np.full(n, -1, dtype=np.int64)
    cap_arr = np.minimum(anchor_pos, int(tsh_cap_w))
    for k in range(1, int(tsh_cap_w) + 1):
        active = (first_k < 0) & (k <= cap_arr)
        if not active.any():
            break
        pos = anchor_pos[active] - k  # >= 0 by cap_arr gate
        wc = weekly_close[pos]
        hit_local = np.isfinite(wc) & (wc > anchor_close[active])
        active_indices = np.where(active)[0]
        hit_indices = active_indices[hit_local]
        if len(hit_indices) > 0:
            first_k[hit_indices] = k
    hit_mask = first_k >= 0
    D3[hit_mask] = first_k[hit_mask].astype(np.float64)
    D3[~hit_mask] = cap_arr[~hit_mask].astype(np.float64)

    # D4a/b log_ath/log_atl: on weekly close series up to and including anchor week (position anchor_pos).
    # Since weekly_close[anchor_pos] = full-week close of anchor week (may be > anchor_close if anchor_week
    # extends past d-1), we instead compute on weekly_close[0..anchor_pos-1] AND include anchor_close.
    # Simpler: cum_max / cum_min of weekly_close at positions 0..anchor_pos-1, then compare to anchor_close.
    D4a = np.full(n, np.nan, dtype=np.float64)
    D4b = np.full(n, np.nan, dtype=np.float64)
    wcs_for_cum = np.where(np.isfinite(weekly_close) & (weekly_close > 0), weekly_close, np.nan)
    cum_max_weekly = np.fmax.accumulate(np.where(np.isnan(wcs_for_cum), -np.inf, wcs_for_cum))
    cum_min_weekly = np.fmin.accumulate(np.where(np.isnan(wcs_for_cum), np.inf, wcs_for_cum))
    # At anchor_pos - 1 (weeks strictly before the anchor week)
    can_D4 = anchor_pos >= 1
    if can_D4.any():
        idx_prior = anchor_pos[can_D4] - 1
        cmax = cum_max_weekly[idx_prior]
        cmin = cum_min_weekly[idx_prior]
        # Combine with anchor_close to get full "ATH up to and including anchor"
        ath = np.maximum(cmax, anchor_close[can_D4])
        atl = np.minimum(cmin, anchor_close[can_D4])
        c_el = anchor_close[can_D4]
        with np.errstate(invalid='ignore', divide='ignore'):
            D4a[can_D4] = np.where(np.isfinite(ath) & (ath > 0) & np.isfinite(c_el) & (c_el > 0),
                                   np.log(c_el / ath), np.nan)
            D4b[can_D4] = np.where(np.isfinite(atl) & (atl > 0) & np.isfinite(c_el) & (c_el > 0),
                                   np.log(c_el / atl), np.nan)

    # D5 vol_ratio(M5): std of last M5 weekly log-returns ending at anchor /
    # std of all prior log-returns. The "return INTO the anchor week" uses the
    # partial anchor close (= close[d-1]) not the full-week anchor close, so we
    # substitute weekly_log_returns[anchor_pos-1] with partial_anchor_return per
    # candidate. This matches Stage A's ex_row compute (np.diff on weekly_chrono
    # with anchor_close placed at the last position).
    D5 = np.full(n, np.nan, dtype=np.float64)
    log_weekly = np.log(wcs_for_cum)
    weekly_log_returns = np.diff(log_weekly)  # length n_weeks - 1
    can_D5 = anchor_pos >= W_M5
    if can_D5.any() and len(weekly_log_returns) >= W_M5:
        ap_el = anchor_pos[can_D5]
        ac_el = anchor_close[can_D5]
        prior_wc = weekly_close[ap_el - 1]  # weekly_close at position anchor_pos - 1
        with np.errstate(invalid='ignore', divide='ignore'):
            partial_ret = np.where(
                np.isfinite(prior_wc) & (prior_wc > 0) & np.isfinite(ac_el) & (ac_el > 0),
                np.log(ac_el / prior_wc), np.nan
            )
        full_anchor_ret = weekly_log_returns[ap_el - 1]  # full-week anchor return (to substitute out)

        swv_r = np.lib.stride_tricks.sliding_window_view(weekly_log_returns, int(W_M5))
        sw_r = ap_el - W_M5
        in_range_r = (sw_r >= 0) & (sw_r < swv_r.shape[0])
        recent = swv_r[np.clip(sw_r, 0, swv_r.shape[0] - 1)].copy()  # (n_D5, W_M5)
        # Replace last column (full-week anchor return) with partial anchor return
        recent[:, -1] = partial_ret
        with np.errstate(invalid='ignore'):
            recent_std = np.nanstd(recent, axis=1, ddof=1)

        # "All prior" cum sums include weekly_log_returns[0..anchor_pos-1] — substitute
        # the anchor-week entry (index ap_el-1) full_anchor_ret -> partial_ret.
        lr = weekly_log_returns
        lr_finite = np.isfinite(lr)
        lr_clean = np.where(lr_finite, lr, 0.0)
        lr_sq = np.where(lr_finite, lr * lr, 0.0)
        cnt = np.cumsum(lr_finite.astype(np.int64))
        sx = np.cumsum(lr_clean)
        sx2 = np.cumsum(lr_sq)
        idx_lr = np.clip(ap_el - 1, 0, len(lr) - 1)
        ncount = cnt[idx_lr]
        sxv = sx[idx_lr]
        sx2v = sx2[idx_lr]

        old_finite = np.isfinite(full_anchor_ret)
        new_finite = np.isfinite(partial_ret)
        with np.errstate(invalid='ignore', divide='ignore'):
            sxv = sxv - np.where(old_finite, full_anchor_ret, 0.0) + np.where(new_finite, partial_ret, 0.0)
            sx2v = sx2v - np.where(old_finite, full_anchor_ret ** 2, 0.0) + np.where(new_finite, partial_ret ** 2, 0.0)
            ncount = ncount - old_finite.astype(np.int64) + new_finite.astype(np.int64)
            mean_all = sxv / np.where(ncount > 0, ncount, 1)
            var_all = (sx2v - ncount * (mean_all ** 2)) / np.where(ncount > 1, ncount - 1, 1)
            long_std = np.where(ncount >= 2, np.sqrt(np.where(var_all >= 0, var_all, 0.0)), np.nan)
            vals = np.where(
                in_range_r & np.isfinite(recent_std) & np.isfinite(long_std) & (long_std > 0),
                recent_std / long_std, np.nan
            )
        D5[can_D5] = vals

    # Apply bounds NaN-lenient
    def in_bounds(x, lo, hi):
        if not np.isfinite(lo) or not np.isfinite(hi):
            return np.ones_like(x, dtype=bool)  # fully NaN-lenient if bounds undefined
        scale = max(abs(lo), abs(hi), 1.0)
        eps = 1e-10 * scale
        with np.errstate(invalid='ignore'):
            return np.isnan(x) | ((x >= lo - eps) & (x <= hi + eps))

    p1 = in_bounds(D1, *loc_bounds["D1"])
    p2 = in_bounds(D2, *loc_bounds["D2"])
    p3 = in_bounds(D3, *loc_bounds["D3"])
    p4a = in_bounds(D4a, *loc_bounds["D4a"])
    p4b = in_bounds(D4b, *loc_bounds["D4b"])
    p5 = in_bounds(D5, *loc_bounds["D5"])
    all_pass = f1 & p1 & p2 & p3 & p4a & p4b & p5

    return {
        "E": d,
        "anchor_pos": anchor_pos,
        "f1": f1,
        "p1": p1, "p2": p2, "p3": p3, "p4a": p4a, "p4b": p4b, "p5": p5,
        "all_pass": all_pass,
    }
