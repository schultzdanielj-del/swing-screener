import pickle
import json
import time
import heapq
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from numba import njit, prange


REPO = Path(r"C:/Users/Dan/Documents/ScanPerfect/swing-screener")
OHLCV_PATH = REPO / "local_runner/cache/universe_ohlcv_daily.pkl"
OUT_DIR = REPO / "research" / "avatar_viability_bank"
OUT_DIR.mkdir(exist_ok=True)


BANK = [
    {
        "setup": "DTSS",
        "klass": "fade",
        "n_bars": 86,
        "ticker": "CELH",
        "entry_date": "2024-05-22",
        "asof": "2024-05-21",
        "phase_boundary_dates": ["2024-03-14", "2024-04-22"],
        "phase_types": ["uptrend", "pullback", "retrace"],
    },
    {
        "setup": "BASE",
        "klass": "breakout",
        "n_bars": 261,
        "ticker": "ASTS",
        "entry_date": "2025-06-03",
        "asof": "2025-06-02",
        "phase_boundary_dates": ["2024-08-20"],
        "phase_types": ["uptrend", "range"],
    },
    {
        "setup": "PARS",
        "klass": "parabolic",
        "n_bars": 21,
        "ticker": "CAR",
        "entry_date": "2026-04-22",
        "asof": "2026-04-21",
        "phase_boundary_dates": [],
        "phase_types": ["uptrend"],
    },
]


TOP_N = 30
TRADABLE_MIN_PRICE = 1.0
TRADABLE_MIN_DVOL = 4_000_000.0
TRADABLE_MIN_ADRP = 1.8
TRADABLE_MIN_MCAP = 100_000_000.0
FADE_EXCLUDE_LAST = 10
ADR_LOOKBACK = 20

EXCLUDED_INDUSTRIES = {"Biotechnology"}
FUNDAMENTALS_PATH = REPO / "local_runner/cache/fundamentals_cache.json"


def compute_tradable(df, shares_outstanding=None):
    closes = df["close"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    dvols = df["dvol_20d"].values.astype(np.float64) if "dvol_20d" in df.columns else None

    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.where(lows > 0, highs / lows, np.nan)
    rolling = pd.Series(ratios).rolling(ADR_LOOKBACK, min_periods=1).mean().values
    adrp = (rolling - 1.0) * 100.0

    tradable = (closes >= TRADABLE_MIN_PRICE) & (~np.isnan(adrp)) & (adrp >= TRADABLE_MIN_ADRP)
    if dvols is not None:
        tradable &= (dvols >= TRADABLE_MIN_DVOL)
    if shares_outstanding is not None and shares_outstanding > 0:
        mcap = closes * float(shares_outstanding)
        tradable &= (mcap >= TRADABLE_MIN_MCAP)
    return tradable


@njit(cache=True)
def breakout_filter_per_ticker(highs, lows, closes, volumes, end_idxs, n_bars):
    out = np.zeros(len(end_idxs), dtype=np.bool_)
    for k in range(len(end_idxs)):
        end = end_idxs[k]
        start = end - n_bars + 1
        max_high = -1e18
        anchor = start
        for i in range(start, end + 1):
            if highs[i] > max_high:
                max_high = highs[i]
                anchor = i
        num = 0.0
        den = 0.0
        for i in range(anchor, end + 1):
            typ = (highs[i] + lows[i] + closes[i]) / 3.0
            v = volumes[i]
            num += typ * v
            den += v
        if den <= 0.0:
            out[k] = False
        else:
            avwap = num / den
            out[k] = closes[end] <= avwap
    return out


@njit(cache=True)
def fade_filter_per_ticker(highs, closes, end_idxs, n_bars, exclude_last):
    out = np.zeros(len(end_idxs), dtype=np.bool_)
    for k in range(len(end_idxs)):
        end = end_idxs[k]
        start = end - n_bars + 1
        scope_end = end - exclude_last
        if scope_end < start:
            out[k] = False
            continue
        max_high = -1e18
        for i in range(start, scope_end + 1):
            if highs[i] > max_high:
                max_high = highs[i]
        out[k] = closes[end] <= max_high
    return out


def compute_dollar_adr_at(highs, lows, idx, lookback=ADR_LOOKBACK):
    """Mean (high - low) over the lookback bars ending at idx (inclusive)."""
    start = max(0, idx - lookback + 1)
    return float(np.mean(highs[start:idx + 1] - lows[start:idx + 1]))


def compute_bank_phase_bars(df, n_bars, asof_date, phase_boundary_dates):
    """Return: closes (in-window n_bars array), highs, lows, ADR_at_asof, boundary_bars (window-relative).
    Convention: boundary bar belongs to the next phase. Phase k spans [boundary_bars[k-1] .. boundary_bars[k]-1]
    with virtual boundary_bars[0] = 0 and boundary_bars[-1] = n_bars."""
    dates = df["date"].values
    asof_np = np.datetime64(asof_date)
    asof_idx = int(np.searchsorted(dates, asof_np))
    if asof_idx >= len(dates) or str(dates[asof_idx])[:10] != asof_date:
        raise RuntimeError(f"asof {asof_date} not found in OHLCV")
    if asof_idx - n_bars + 1 < 0:
        raise RuntimeError(f"insufficient history before asof {asof_date}")

    win_start_idx = asof_idx - n_bars + 1
    closes = df["close"].values[win_start_idx:asof_idx + 1].astype(np.float64)
    highs = df["high"].values[win_start_idx:asof_idx + 1].astype(np.float64)
    lows = df["low"].values[win_start_idx:asof_idx + 1].astype(np.float64)
    if (closes <= 0).any() or not np.isfinite(closes).all():
        raise RuntimeError("non-positive or non-finite closes in bank window")

    boundary_bars = []
    for d in phase_boundary_dates:
        d_np = np.datetime64(d)
        full_idx = int(np.searchsorted(dates, d_np))
        if full_idx >= len(dates) or str(dates[full_idx])[:10] != d:
            raise RuntimeError(f"phase boundary {d} not in OHLCV")
        rel = full_idx - win_start_idx
        if rel <= 0 or rel >= n_bars:
            raise RuntimeError(f"phase boundary {d} out of window: rel_bar={rel}")
        boundary_bars.append(rel)

    adr_full = (df["high"].values[:asof_idx + 1] - df["low"].values[:asof_idx + 1]).astype(np.float64)
    adr_at_asof = float(np.mean(adr_full[max(0, asof_idx - ADR_LOOKBACK + 1):asof_idx + 1]))

    return closes, highs, lows, adr_at_asof, boundary_bars


def signature_from_partition(closes, boundary_bars, adr_anchor=None, add_dtss_relations=None):
    """closes: length-n array; boundary_bars: list of K-1 internal boundaries.
    Phase k spans bars [bdry[k-1] .. bdry[k]-1] with bdry[0]=0, bdry[K]=n.

    Per phase, 6 generic dimensions (no setup-specific math):
      - length_frac      = phase bars / window bars
      - end_level        = log(close[phase_end] / close[window_start])
      - max_level        = log(close[phase_max] / close[window_start])
      - min_level        = log(close[phase_min] / close[window_start])
      - directness       = |end_log - start_log| / sum(|delta_log|) over phase
      - argmax_position  = (bar_index_of_phase_max - phase_start) / phase_length, in [0, 1)

    `adr_anchor` and `add_dtss_relations` retained for API compatibility but unused.

    Returns: flat array of 6*K floats."""
    n = len(closes)
    full_bdry = [0] + list(boundary_bars) + [n]
    log_closes = np.log(closes)
    log_anchor = log_closes[0]

    abs_deltas = np.abs(np.diff(log_closes))
    prefix_path = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        prefix_path[i] = prefix_path[i - 1] + abs_deltas[i - 1]

    sig = []
    for k in range(len(full_bdry) - 1):
        seg_start = full_bdry[k]
        seg_end_excl = full_bdry[k + 1]
        seg_end = seg_end_excl - 1
        seg_log = log_closes[seg_start:seg_end_excl]
        seg_len = seg_end_excl - seg_start
        length_frac = seg_len / n
        end_level = float(seg_log[-1] - log_anchor)
        max_level = float(seg_log.max() - log_anchor)
        min_level = float(seg_log.min() - log_anchor)
        net = abs(float(log_closes[seg_end] - log_closes[seg_start]))
        path = float(prefix_path[seg_end] - prefix_path[seg_start])
        directness = net / path if path > 0.0 else 1.0
        argmax_local = int(np.argmax(seg_log))
        argmax_position = argmax_local / seg_len if seg_len > 0 else 0.0
        sig.extend([length_frac, end_level, max_level, min_level, directness, argmax_position])

    return np.array(sig, dtype=np.float64)


@njit(cache=True)
def dp_fit_2phase(closes, target_sig, adr_anchor):
    """K=2: search 1 internal boundary in [1..n-1].
    Phase 0 = bars [0..b-1], Phase 1 = bars [b..n-1].
    Per phase 6-d: length_frac, end_level, max_level, min_level, directness, argmax_position.
    Returns (best_dist, best_b)."""
    n = len(closes)
    levels = np.empty(n, dtype=np.float64)
    log_anchor = np.log(closes[0])
    for i in range(n):
        levels[i] = np.log(closes[i]) - log_anchor

    pmax = np.empty(n, dtype=np.float64)
    pmin = np.empty(n, dtype=np.float64)
    pmax_idx = np.empty(n, dtype=np.int64)
    pmax[0] = levels[0]; pmin[0] = levels[0]; pmax_idx[0] = 0
    for i in range(1, n):
        if levels[i] > pmax[i - 1]:
            pmax[i] = levels[i]
            pmax_idx[i] = i
        else:
            pmax[i] = pmax[i - 1]
            pmax_idx[i] = pmax_idx[i - 1]
        pmin[i] = pmin[i - 1] if pmin[i - 1] < levels[i] else levels[i]

    smax = np.empty(n, dtype=np.float64)
    smin = np.empty(n, dtype=np.float64)
    smax_idx = np.empty(n, dtype=np.int64)
    smax[n - 1] = levels[n - 1]; smin[n - 1] = levels[n - 1]; smax_idx[n - 1] = n - 1
    for i in range(n - 2, -1, -1):
        if levels[i] > smax[i + 1]:
            smax[i] = levels[i]
            smax_idx[i] = i
        else:
            smax[i] = smax[i + 1]
            smax_idx[i] = smax_idx[i + 1]
        smin[i] = smin[i + 1] if smin[i + 1] < levels[i] else levels[i]

    prefix_path = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        d = levels[i] - levels[i - 1]
        prefix_path[i] = prefix_path[i - 1] + (d if d >= 0.0 else -d)

    best_dist = 1e18
    best_b = 1

    for b in range(1, n):
        # Phase 0 = [0..b-1]
        p0_len = b / n
        p0_end = levels[b - 1]
        p0_max = pmax[b - 1]
        p0_min = pmin[b - 1]
        net0 = levels[b - 1] - levels[0]
        if net0 < 0.0: net0 = -net0
        path0 = prefix_path[b - 1]
        p0_dir = net0 / path0 if path0 > 0.0 else 1.0
        p0_argmax_pos = pmax_idx[b - 1] / b if b > 0 else 0.0

        # Phase 1 = [b..n-1]
        p1_len_n = n - b
        p1_len = p1_len_n / n
        p1_end = levels[n - 1]
        p1_max = smax[b]
        p1_min = smin[b]
        net1 = levels[n - 1] - levels[b]
        if net1 < 0.0: net1 = -net1
        path1 = prefix_path[n - 1] - prefix_path[b]
        p1_dir = net1 / path1 if path1 > 0.0 else 1.0
        p1_argmax_pos = (smax_idx[b] - b) / p1_len_n if p1_len_n > 0 else 0.0

        d = (p0_len - target_sig[0])**2 + (p0_end - target_sig[1])**2 + \
            (p0_max - target_sig[2])**2 + (p0_min - target_sig[3])**2 + \
            (p0_dir - target_sig[4])**2 + (p0_argmax_pos - target_sig[5])**2 + \
            (p1_len - target_sig[6])**2 + (p1_end - target_sig[7])**2 + \
            (p1_max - target_sig[8])**2 + (p1_min - target_sig[9])**2 + \
            (p1_dir - target_sig[10])**2 + (p1_argmax_pos - target_sig[11])**2

        if d < best_dist:
            best_dist = d
            best_b = b

    return best_dist, best_b


@njit(cache=True)
def dp_fit_3phase(closes, target_sig, adr_anchor):
    """K=3: search 2 internal boundaries (b1, b2) with 1 <= b1 < b2 <= n-1.
    Phase 0 = [0..b1-1], Phase 1 = [b1..b2-1], Phase 2 = [b2..n-1].
    Per phase 6-d: length_frac, end_level, max_level, min_level, directness, argmax_position.
    target_sig is 18-dim (3 phases × 6).
    Hard constraint: skip splits where phase_0_max < close[asof] (right side broke
    above leftside peak — invalid). If no split satisfies, returns infinity.
    Returns (best_dist, best_b1, best_b2)."""
    n = len(closes)
    levels = np.empty(n, dtype=np.float64)
    log_anchor = np.log(closes[0])
    for i in range(n):
        levels[i] = np.log(closes[i]) - log_anchor
    asof_level = levels[n - 1]

    # range max/min/argmax over levels
    rmax = np.full((n, n), -1e18, dtype=np.float64)
    rmin = np.full((n, n), 1e18, dtype=np.float64)
    rargmax = np.zeros((n, n), dtype=np.int64)
    for i in range(n):
        m = -1e18; mn = 1e18; am = i
        for j in range(i, n):
            if levels[j] > m:
                m = levels[j]
                am = j
            if levels[j] < mn:
                mn = levels[j]
            rmax[i, j] = m
            rmin[i, j] = mn
            rargmax[i, j] = am

    # prefix path length in log space
    prefix_path = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        d = levels[i] - levels[i - 1]
        prefix_path[i] = prefix_path[i - 1] + (d if d >= 0.0 else -d)

    best_dist = 1e18
    best_b1 = 1
    best_b2 = 2

    for b1 in range(1, n - 1):
        # hard constraint: phase 0 max must be >= close[asof] (right side hasn't broken
        # above leftside peak). Skip b1 values where this fails — phase 0 here is [0..b1-1].
        if rmax[0, b1 - 1] < asof_level:
            continue

        for b2 in range(b1 + 1, n):
            # Phase 0 = [0..b1-1]
            p0_len = b1 / n
            p0_end = levels[b1 - 1]
            p0_max = rmax[0, b1 - 1]
            p0_min = rmin[0, b1 - 1]
            net0 = levels[b1 - 1] - levels[0]
            if net0 < 0.0: net0 = -net0
            path0 = prefix_path[b1 - 1]
            p0_dir = net0 / path0 if path0 > 0.0 else 1.0
            p0_argmax_pos = rargmax[0, b1 - 1] / b1 if b1 > 0 else 0.0

            # Phase 1 = [b1..b2-1]
            p1_len_n = b2 - b1
            p1_len = p1_len_n / n
            p1_end = levels[b2 - 1]
            p1_max = rmax[b1, b2 - 1]
            p1_min = rmin[b1, b2 - 1]
            net1 = levels[b2 - 1] - levels[b1]
            if net1 < 0.0: net1 = -net1
            path1 = prefix_path[b2 - 1] - prefix_path[b1]
            p1_dir = net1 / path1 if path1 > 0.0 else 1.0
            p1_argmax_pos = (rargmax[b1, b2 - 1] - b1) / p1_len_n if p1_len_n > 0 else 0.0

            # Phase 2 = [b2..n-1]
            p2_len_n = n - b2
            p2_len = p2_len_n / n
            p2_end = levels[n - 1]
            p2_max = rmax[b2, n - 1]
            p2_min = rmin[b2, n - 1]
            net2 = levels[n - 1] - levels[b2]
            if net2 < 0.0: net2 = -net2
            path2 = prefix_path[n - 1] - prefix_path[b2]
            p2_dir = net2 / path2 if path2 > 0.0 else 1.0
            p2_argmax_pos = (rargmax[b2, n - 1] - b2) / p2_len_n if p2_len_n > 0 else 0.0

            d = (p0_len - target_sig[0])**2 + (p0_end - target_sig[1])**2 + \
                (p0_max - target_sig[2])**2 + (p0_min - target_sig[3])**2 + \
                (p0_dir - target_sig[4])**2 + (p0_argmax_pos - target_sig[5])**2 + \
                (p1_len - target_sig[6])**2 + (p1_end - target_sig[7])**2 + \
                (p1_max - target_sig[8])**2 + (p1_min - target_sig[9])**2 + \
                (p1_dir - target_sig[10])**2 + (p1_argmax_pos - target_sig[11])**2 + \
                (p2_len - target_sig[12])**2 + (p2_end - target_sig[13])**2 + \
                (p2_max - target_sig[14])**2 + (p2_min - target_sig[15])**2 + \
                (p2_dir - target_sig[16])**2 + (p2_argmax_pos - target_sig[17])**2

            if d < best_dist:
                best_dist = d
                best_b1 = b1
                best_b2 = b2

    return best_dist, best_b1, best_b2


@njit(cache=True)
def scan_dp_2phase(closes_all, highs_all, lows_all, end_idxs, n_bars, target_sig):
    """Per-candidate-window DP fit; returns dist array and best_b array."""
    n_cands = len(end_idxs)
    dists = np.full(n_cands, np.inf, dtype=np.float64)
    bs = np.zeros(n_cands, dtype=np.int64)
    for k in range(n_cands):
        end = end_idxs[k]
        start = end - n_bars + 1
        # ADR at asof: mean(high-low) over last 20 bars
        adr_start = max(0, end - ADR_LOOKBACK + 1)
        s = 0.0
        cnt = 0
        for i in range(adr_start, end + 1):
            s += highs_all[i] - lows_all[i]
            cnt += 1
        adr = s / cnt
        if adr <= 0.0:
            continue
        win = closes_all[start:end + 1]
        d, b = dp_fit_2phase(win, target_sig, adr)
        dists[k] = d
        bs[k] = b
    return dists, bs


@njit(cache=True)
def scan_dp_3phase(closes_all, highs_all, lows_all, end_idxs, n_bars, target_sig):
    n_cands = len(end_idxs)
    dists = np.full(n_cands, np.inf, dtype=np.float64)
    b1s = np.zeros(n_cands, dtype=np.int64)
    b2s = np.zeros(n_cands, dtype=np.int64)
    for k in range(n_cands):
        end = end_idxs[k]
        start = end - n_bars + 1
        adr_start = max(0, end - ADR_LOOKBACK + 1)
        s = 0.0
        cnt = 0
        for i in range(adr_start, end + 1):
            s += highs_all[i] - lows_all[i]
            cnt += 1
        adr = s / cnt
        if adr <= 0.0:
            continue
        win = closes_all[start:end + 1]
        d, b1, b2 = dp_fit_3phase(win, target_sig, adr)
        dists[k] = d
        b1s[k] = b1
        b2s[k] = b2
    return dists, b1s, b2s


def greedy_nonoverlap(end_idxs, neg_dists, n_bars):
    """neg_dists: negative distance (so argsort -neg_dists picks lowest distance first)."""
    order = np.argsort(-neg_dists)
    used = np.zeros(len(end_idxs), dtype=bool)
    kept = []
    for i in order:
        if used[i]:
            continue
        kept.append(int(i))
        used |= np.abs(end_idxs - end_idxs[i]) < n_bars
    return kept


@njit(cache=True)
def dp_fit_1phase(closes, target_sig):
    """K=1: no internal boundaries. Whole window IS phase 0.
    target_sig is 6-dim (one phase × 6 dims).
    Returns best_dist."""
    n = len(closes)
    log_anchor = np.log(closes[0])
    levels = np.empty(n, dtype=np.float64)
    for i in range(n):
        levels[i] = np.log(closes[i]) - log_anchor

    p0_len = 1.0  # n / n
    p0_end = levels[n - 1]
    p0_max = -1e18
    p0_min = 1e18
    p0_argmax_idx = 0
    for i in range(n):
        if levels[i] > p0_max:
            p0_max = levels[i]
            p0_argmax_idx = i
        if levels[i] < p0_min:
            p0_min = levels[i]

    net = levels[n - 1] - levels[0]
    if net < 0.0: net = -net
    path = 0.0
    for i in range(1, n):
        d = levels[i] - levels[i - 1]
        path += d if d >= 0.0 else -d
    p0_dir = net / path if path > 0.0 else 1.0
    p0_argmax_pos = p0_argmax_idx / n if n > 0 else 0.0

    return (p0_len - target_sig[0])**2 + (p0_end - target_sig[1])**2 + \
           (p0_max - target_sig[2])**2 + (p0_min - target_sig[3])**2 + \
           (p0_dir - target_sig[4])**2 + (p0_argmax_pos - target_sig[5])**2


@njit(parallel=True, cache=True)
def parallel_dp_fit_1phase(closes_flat, eligible_end_idxs, n_bars, target_sig):
    n = len(eligible_end_idxs)
    dists = np.full(n, 1e18, dtype=np.float64)
    for k in prange(n):
        end = eligible_end_idxs[k]
        win = closes_flat[end - n_bars + 1:end + 1]
        d = dp_fit_1phase(win, target_sig)
        dists[k] = d
    return dists


@njit(parallel=True, cache=True)
def parallel_dp_fit_3phase(closes_flat, eligible_end_idxs, n_bars, target_sig):
    n = len(eligible_end_idxs)
    dists = np.full(n, 1e18, dtype=np.float64)
    b1s = np.zeros(n, dtype=np.int64)
    b2s = np.zeros(n, dtype=np.int64)
    for k in prange(n):
        end = eligible_end_idxs[k]
        win = closes_flat[end - n_bars + 1:end + 1]
        d, b1, b2 = dp_fit_3phase(win, target_sig, 1.0)
        dists[k] = d
        b1s[k] = b1
        b2s[k] = b2
    return dists, b1s, b2s


@njit(parallel=True, cache=True)
def parallel_dp_fit_2phase(closes_flat, eligible_end_idxs, n_bars, target_sig):
    n = len(eligible_end_idxs)
    dists = np.full(n, 1e18, dtype=np.float64)
    bs = np.zeros(n, dtype=np.int64)
    for k in prange(n):
        end = eligible_end_idxs[k]
        win = closes_flat[end - n_bars + 1:end + 1]
        d, b = dp_fit_2phase(win, target_sig, 1.0)
        dists[k] = d
        bs[k] = b
    return dists, bs


_fundamentals_cache = None


def load_fundamentals():
    global _fundamentals_cache
    if _fundamentals_cache is None:
        with open(FUNDAMENTALS_PATH) as f:
            _fundamentals_cache = json.load(f)["tickers"]
    return _fundamentals_cache


def flatten_and_filter(cache, n_bars, klass):
    """Sequential pre-pass: per ticker, apply tradable mask + class filter + finite check.
    Skip tickers in EXCLUDED_INDUSTRIES or missing fundamentals. Enforce close >= $1
    across the entire candidate window (not just asof bar). Returns flat closes array,
    eligible end_idxs (in flat-array space), per-eligible ticker index, plus per-ticker
    metadata (names, starts, dates) for post-processing."""
    fundamentals = load_fundamentals()
    tickers = []
    starts = []
    dates_per_ticker = []
    closes_chunks = []
    eligible_end_idxs_chunks = []
    eligible_t_chunks = []
    n_skipped_industry = 0

    offset = 0
    for ticker, df in cache.items():
        if df is None or len(df) < n_bars + ADR_LOOKBACK + 1:
            continue

        info = fundamentals.get(ticker) or {}
        industry = info.get("industry")
        if industry in EXCLUDED_INDUSTRIES:
            n_skipped_industry += 1
            continue
        # Allow tickers without fundamentals (ETFs/ETNs); skip mcap check when shares missing.
        shares = info.get("shares_outstanding")

        n = len(df)
        closes = df["close"].values.astype(np.float64)
        highs = df["high"].values.astype(np.float64)
        lows = df["low"].values.astype(np.float64)
        volumes = df["volume"].values.astype(np.float64)
        tradable = compute_tradable(df, shares_outstanding=shares)

        end_idxs = np.arange(n_bars - 1, n)
        ok = tradable[end_idxs]
        end_idxs = end_idxs[ok]
        if len(end_idxs) == 0:
            continue

        valid = np.zeros(len(end_idxs), dtype=bool)
        for j, e in enumerate(end_idxs):
            w = closes[e - n_bars + 1:e + 1]
            valid[j] = (np.isfinite(w).all()
                        and (w >= TRADABLE_MIN_PRICE).all())
        end_idxs = end_idxs[valid]
        if len(end_idxs) == 0:
            continue

        end_idxs64 = end_idxs.astype(np.int64)
        if klass == "breakout":
            filt_pass = breakout_filter_per_ticker(highs, lows, closes, volumes, end_idxs64, n_bars)
        elif klass == "fade":
            filt_pass = fade_filter_per_ticker(highs, closes, end_idxs64, n_bars, FADE_EXCLUDE_LAST)
        else:
            filt_pass = np.ones(len(end_idxs), dtype=np.bool_)
        end_idxs = end_idxs[filt_pass]
        if len(end_idxs) == 0:
            continue

        t_idx = len(tickers)
        tickers.append(ticker)
        starts.append(offset)
        dates_per_ticker.append(df["date"].values)
        closes_chunks.append(closes)
        eligible_end_idxs_chunks.append((end_idxs + offset).astype(np.int64))
        eligible_t_chunks.append(np.full(len(end_idxs), t_idx, dtype=np.int64))
        offset += n

    print(f"    skipped: {n_skipped_industry} biotech")
    if not eligible_end_idxs_chunks:
        return None
    return {
        "tickers": tickers,
        "starts": np.array(starts, dtype=np.int64),
        "dates_per_ticker": dates_per_ticker,
        "closes_flat": np.concatenate(closes_chunks),
        "eligible_end_idxs": np.concatenate(eligible_end_idxs_chunks),
        "eligible_t": np.concatenate(eligible_t_chunks),
    }


def scan_one_ticker(ticker, df, n_bars, klass, n_phases, target_sig):
    """Worker: scans one ticker, returns list of (dist, ticker, asof_str, extras, end_idx)
    after per-ticker dedup. Returns empty list if ticker has insufficient history or no candidates."""
    if df is None or len(df) < n_bars + ADR_LOOKBACK + 1:
        return []

    closes_raw = df["close"].values.astype(np.float64)
    highs_raw = df["high"].values.astype(np.float64)
    lows_raw = df["low"].values.astype(np.float64)
    volumes_raw = df["volume"].values.astype(np.float64)
    tradable = compute_tradable(df)

    n = len(closes_raw)
    end_idxs = np.arange(n_bars - 1, n)
    ok = tradable[end_idxs]
    if ok.sum() == 0:
        return []
    end_idxs = end_idxs[ok]

    valid = np.zeros(len(end_idxs), dtype=bool)
    for j, e in enumerate(end_idxs):
        w = closes_raw[e - n_bars + 1:e + 1]
        valid[j] = np.isfinite(w).all() and (w > 0).all()
    end_idxs = end_idxs[valid]
    if len(end_idxs) == 0:
        return []

    end_idxs64 = end_idxs.astype(np.int64)
    if klass == "breakout":
        filt_pass = breakout_filter_per_ticker(highs_raw, lows_raw, closes_raw, volumes_raw,
                                                end_idxs64, n_bars)
    elif klass == "fade":
        filt_pass = fade_filter_per_ticker(highs_raw, closes_raw, end_idxs64, n_bars, FADE_EXCLUDE_LAST)
    else:
        filt_pass = np.ones(len(end_idxs), dtype=np.bool_)

    if not filt_pass.any():
        return []
    end_idxs = end_idxs[filt_pass]
    end_idxs64 = end_idxs.astype(np.int64)

    if n_phases == 2:
        dists, bs = scan_dp_2phase(closes_raw, highs_raw, lows_raw, end_idxs64, n_bars, target_sig)
        extras = [(int(b),) for b in bs]
    elif n_phases == 3:
        dists, b1s, b2s = scan_dp_3phase(closes_raw, highs_raw, lows_raw, end_idxs64, n_bars, target_sig)
        extras = [(int(b1), int(b2)) for b1, b2 in zip(b1s, b2s)]
    else:
        raise NotImplementedError

    finite = np.isfinite(dists)
    if not finite.any():
        return []
    end_idxs_f = end_idxs[finite]
    dists_f = dists[finite]
    extras_f = [extras[i] for i in range(len(extras)) if finite[i]]

    kept_idx = greedy_nonoverlap(end_idxs_f, -dists_f, n_bars)

    dates = df["date"].values
    out = []
    for i in kept_idx:
        end_idx = int(end_idxs_f[i])
        d = float(dists_f[i])
        asof = dates[end_idx]
        asof_str = str(asof.astype("datetime64[D]")) if isinstance(asof, np.datetime64) else str(asof)[:10]
        out.append((d, ticker, asof_str, extras_f[i], end_idx))
    return out


def scan(cache, bank_entry, target_sig):
    """Two-stage scan: (1) sequential pre-pass per ticker applying tradable + class filter
    + finite check, building flat arrays of eligible candidate end_idxs; (2) parallel
    DP fit over all eligible candidates via numba prange; (3) sequential per-ticker
    dedup + global top-N aggregation."""
    klass = bank_entry["klass"]
    n_bars = bank_entry["n_bars"]
    n_phases = len(bank_entry["phase_types"])
    bank_pair = (bank_entry["ticker"], bank_entry["asof"])

    print(f"  pre-pass: filtering candidates per ticker...")
    t0 = time.time()
    flat = flatten_and_filter(cache, n_bars, klass)
    if flat is None:
        print(f"    no eligible candidates")
        return []
    n_cands = len(flat["eligible_end_idxs"])
    print(f"    {len(flat['tickers'])} tickers, {n_cands:,} eligible candidates, {time.time()-t0:.1f}s")

    print(f"  parallel DP fit ({n_phases}-phase) over {n_cands:,} candidates...")
    t1 = time.time()
    if n_phases == 1:
        dists = parallel_dp_fit_1phase(flat["closes_flat"], flat["eligible_end_idxs"], n_bars, target_sig)
        extras_arr = np.zeros((len(dists), 0), dtype=np.int64)
    elif n_phases == 2:
        dists, bs = parallel_dp_fit_2phase(flat["closes_flat"], flat["eligible_end_idxs"], n_bars, target_sig)
        extras_arr = bs.reshape(-1, 1)
    elif n_phases == 3:
        dists, b1s, b2s = parallel_dp_fit_3phase(flat["closes_flat"], flat["eligible_end_idxs"], n_bars, target_sig)
        extras_arr = np.column_stack([b1s, b2s])
    else:
        raise NotImplementedError
    print(f"    DP fit done, {time.time()-t1:.1f}s")

    print(f"  per-ticker dedup + global top-{TOP_N}...")
    t2 = time.time()
    finite_mask = np.isfinite(dists)
    elig = flat["eligible_end_idxs"][finite_mask]
    elig_t = flat["eligible_t"][finite_mask]
    dists_f = dists[finite_mask]
    extras_f = extras_arr[finite_mask]

    heap = []
    all_dedup_dists = []
    per_day_min = {}
    order = np.argsort(elig_t, kind="stable")
    elig_sorted = elig[order]
    elig_t_sorted = elig_t[order]
    dists_sorted = dists_f[order]
    extras_sorted = extras_f[order]

    n = len(elig_sorted)
    i = 0
    while i < n:
        t_idx = elig_t_sorted[i]
        j = i
        while j < n and elig_t_sorted[j] == t_idx:
            j += 1
        ticker = flat["tickers"][t_idx]
        local_start = flat["starts"][t_idx]
        local_end_idxs = (elig_sorted[i:j] - local_start).astype(np.int64)
        local_dists = dists_sorted[i:j]
        local_extras = extras_sorted[i:j]
        kept = greedy_nonoverlap(local_end_idxs, -local_dists, n_bars)
        dates = flat["dates_per_ticker"][t_idx]
        for kk in kept:
            local_end = int(local_end_idxs[kk])
            d = float(local_dists[kk])
            asof = dates[local_end]
            asof_str = str(asof.astype("datetime64[D]")) if isinstance(asof, np.datetime64) else str(asof)[:10]
            if (ticker, asof_str) == bank_pair:
                continue
            all_dedup_dists.append(d)
            cur = per_day_min.get(asof_str)
            if cur is None or d < cur:
                per_day_min[asof_str] = d
            extras_tup = tuple(int(x) for x in local_extras[kk])
            entry = (-d, ticker, asof_str, extras_tup, local_end)
            if len(heap) < TOP_N:
                heapq.heappush(heap, entry)
            elif -d > heap[0][0]:
                heapq.heapreplace(heap, entry)
        i = j

    top = sorted(heap, key=lambda x: -x[0])
    setup = bank_entry["setup"]
    np.save(OUT_DIR / f"{setup}_all_dedup_distances.npy", np.array(all_dedup_dists, dtype=np.float64))
    np.save(OUT_DIR / f"{setup}_per_day_min_distances.npy", np.array(sorted(per_day_min.values()), dtype=np.float64))
    print(f"    aggregation done, {time.time()-t2:.1f}s; saved distance distributions ({len(all_dedup_dists):,} candidates, {len(per_day_min):,} days); total {time.time()-t0:.1f}s")
    return top


def render_grid(top_list, cache, bank_entry, bank_closes, bank_boundary_bars, out_path):
    n_bars = bank_entry["n_bars"]
    n_phases = len(bank_entry["phase_types"])

    n = len(top_list)
    cols = 5
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 3.2, rows * 2.6))
    axes = axes.flatten() if rows > 1 else np.array([axes]).flatten()

    bank_log = np.log(bank_closes)
    bank_norm = bank_log - bank_log[-1]

    for i, (neg_d, ticker, asof, extras, end_idx) in enumerate(top_list):
        ax = axes[i]
        df = cache[ticker]
        cand_close = df["close"].values[end_idx - n_bars + 1:end_idx + 1].astype(np.float64)
        cand_log = np.log(cand_close)
        cand_norm = cand_log - cand_log[-1]
        x = np.arange(n_bars)

        ax.plot(x, bank_norm, color="black", alpha=0.35, linewidth=1.2,
                label=f"avatar: {bank_entry['ticker']} {bank_entry['asof']}")
        ax.plot(x, cand_norm, color="blue", linewidth=1.5, label="candidate")

        # bank phase boundaries (gray solid v-lines)
        for b in bank_boundary_bars:
            ax.axvline(b, color="gray", alpha=0.4, linewidth=0.8)
        # candidate DP-fit boundaries (blue dashed v-lines)
        for b in extras:
            ax.axvline(b, color="blue", alpha=0.5, linewidth=0.8, linestyle="--")

        d = -neg_d
        ax.set_title(f"#{i+1}  {ticker}  {asof}\ndist={d:.3f}", fontsize=8)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=7)
        if i == 0:
            ax.legend(fontsize=6, loc="best")

    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    setup = bank_entry["setup"]
    plt.suptitle(f"{setup} phase-DP top {TOP_N} — bank: {bank_entry['ticker']} {bank_entry['asof']}, "
                 f"N={n_bars}, phases={n_phases} ({'-'.join(bank_entry['phase_types'])})",
                 fontsize=10, y=0.998)
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close()
    print(f"  saved {out_path}")


def main():
    print(f"OHLCV cache path: {OHLCV_PATH}")
    print("Loading OHLCV pickle ...")
    t0 = time.time()
    with open(OHLCV_PATH, "rb") as f:
        cache = pickle.load(f)
    print(f"  {len(cache)} tickers, {time.time()-t0:.1f}s")
    if len(cache) < 11000:
        raise RuntimeError(f"unexpected cache size {len(cache)}; expected ~11200+")

    for bank_entry in BANK:
        setup = bank_entry["setup"]
        ticker = bank_entry["ticker"]
        n_bars = bank_entry["n_bars"]

        print(f"\n=== {setup} phase-DP probe (1 bank example: {ticker}) ===")
        if ticker not in cache:
            print(f"  ERROR: {ticker} not in cache; skipping")
            continue

        df = cache[ticker]
        bank_closes, bank_highs, bank_lows, adr_anchor, boundary_bars = compute_bank_phase_bars(
            df, n_bars, bank_entry["asof"], bank_entry["phase_boundary_dates"]
        )
        n_phases = len(bank_entry["phase_types"])
        target_sig = signature_from_partition(bank_closes, boundary_bars)
        print(f"  bank window: n_bars={n_bars}, asof={bank_entry['asof']}")
        print(f"  bank phase boundary bars (window-relative): {boundary_bars}")
        full_b = [0] + list(boundary_bars) + [n_bars]
        for k, t in enumerate(bank_entry["phase_types"]):
            seg_start = full_b[k]
            seg_end = full_b[k + 1] - 1
            sig = target_sig[k * 6:(k + 1) * 6]
            print(f"    phase {k} ({t}): bars [{seg_start}..{seg_end}], "
                  f"len={sig[0]:.3f}, end={sig[1]:.3f}, max={sig[2]:.3f}, "
                  f"min={sig[3]:.3f}, directness={sig[4]:.3f}, argmax_pos={sig[5]:.3f}")

        top = scan(cache, bank_entry, target_sig)

        results = {
            "setup": setup,
            "klass": bank_entry["klass"],
            "n_bars": n_bars,
            "bank_ticker": ticker,
            "bank_asof": bank_entry["asof"],
            "bank_phase_types": bank_entry["phase_types"],
            "bank_phase_boundary_bars": list(boundary_bars),
            "bank_signature": target_sig.tolist(),
            "adr_anchor": adr_anchor,
            "top": [
                {"rank": i + 1, "ticker": t, "asof": a, "dist": float(-neg_d),
                 "fit_boundaries": list(extras)}
                for i, (neg_d, t, a, extras, _) in enumerate(top)
            ],
        }
        json_path = OUT_DIR / f"{setup}_phase_probe_results.json"
        with open(json_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"  saved {json_path}")

        render_grid(
            top, cache, bank_entry, bank_closes, boundary_bars,
            OUT_DIR / f"{setup}_phase_probe_top{TOP_N}.png",
        )


if __name__ == "__main__":
    main()
