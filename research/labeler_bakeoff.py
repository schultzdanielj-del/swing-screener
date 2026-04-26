"""Labeler bakeoff — exhaustive comparison of labeler-design candidates on HTF data.

Built 2026-04-25. Runs entirely from the HALTED HTF pool grinder output + OHLCV.
Pure measurement, no labels written to grinder output paths.

Tests every reasonable labeler primitive against the same gates:
  C1 examples lock 28/28
  C2 round-trip → WIN (LOGI cid=149: mfe=11.97, stop=5)
  empirical: TAN cid=231 (mfe=68 no stop) → WIN, CRSR cid=67 (mfe=1.04, stop=2) → LOSS
  cross-tab: rule-consensus separation, stop_hit_bar separation, mfe separation

Output:
  research/labeler_bakeoff/results.json — full per-labeler results
  research/labeler_bakeoff/summary.txt — ranked summary
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import time
from typing import Dict, List, Tuple

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAIN_REPO = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
HALTED = os.path.join(MAIN_REPO, "data", "signal_exit_grind",
                      "signal_exit_pool_htf_HALTED_20260425_202509.json")
OHLCV = os.path.join(MAIN_REPO, "local_runner", "cache", "universe_ohlcv_daily.pkl")
OUT_DIR = os.path.join(MAIN_REPO, "research", "labeler_bakeoff")
os.makedirs(OUT_DIR, exist_ok=True)

SMA_PERIODS = [5, 8, 10, 13, 20, 30, 50, 100, 150, 200]
EMA_PERIODS = [3, 5, 8, 10, 13, 20, 30, 50, 100, 150, 200]


# ───────────────────────── MA compute ─────────────────────────
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
        v = c[t]; prev = ema[t - 1]
        if np.isfinite(v) and np.isfinite(prev):
            ema[t] = alpha * v + (1 - alpha) * prev
        elif np.isfinite(v):
            ema[t] = v
        else:
            ema[t] = prev
    return ema


# ───────────────────────── Feature builder ─────────────────────────
def build_all_features(meta_list: List[dict], ohlcv: dict, ma_specs: List[Tuple[str, int]]):
    """Returns dict of arrays:
      lr_close_ma: (n, max_h, n_ma) anchored log-MA-ratio at offset k+1 (k=0 is entry+1)
      mfe_by_bar: (n, max_h) cumulative max(high) - eff in ADR through bar k+1
      mae_by_bar: (n, max_h) (eff - cumulative min(low)) in ADR through bar k+1 (positive=drawdown)
      close_by_bar: (n, max_h) (close - eff) / ADR
      mfe_locked: (n, max_h) mfe locked at value at stop_hit_bar after stop
      mae_locked: (n, max_h) mae locked at value at stop_hit_bar after stop
      Plus scalars per cluster.
    """
    n = len(meta_list)
    n_ma = len(ma_specs)
    horizons = np.array([m["horizon"] for m in meta_list], dtype=np.int64)
    max_h = int(horizons.max())
    print(f"  Building features: n={n}, max_h={max_h}, n_ma={n_ma}")

    lr_close_ma = np.full((n, max_h, n_ma), np.nan, dtype=np.float64)
    mfe_by_bar = np.full((n, max_h), np.nan, dtype=np.float64)
    mae_by_bar = np.full((n, max_h), np.nan, dtype=np.float64)
    close_by_bar = np.full((n, max_h), np.nan, dtype=np.float64)
    mfe_locked = np.full((n, max_h), np.nan, dtype=np.float64)
    mae_locked = np.full((n, max_h), np.nan, dtype=np.float64)
    n_bars_close_above_eff = np.zeros(n, dtype=np.int64)
    time_to_mfe = np.zeros(n, dtype=np.int64)
    time_to_mae = np.zeros(n, dtype=np.int64)
    mfe_during_life = np.zeros(n, dtype=np.float64)
    mae_during_life = np.zeros(n, dtype=np.float64)
    eff_horizons = np.zeros(n, dtype=np.int64)

    for i, m in enumerate(meta_list):
        ticker = m["ticker"]
        df = ohlcv.get(ticker)
        if df is None:
            continue
        close = df["close"].values.astype(np.float64)
        high = df["high"].values.astype(np.float64)
        low = df["low"].values.astype(np.float64)
        e = m["entry_bar"]
        cap = m["cap_bar"]
        h = int(m["horizon"])
        adr = float(m["adr14_at_entry"])
        eff = float(m["effective_entry"])
        stop_hit_bar = m["stop_hit_bar"] if m["stop_hit_bar"] is not None else h
        if stop_hit_bar is None:
            stop_hit_bar = h
        eff_h = min(int(stop_hit_bar) + 1, h)
        eff_horizons[i] = eff_h

        # MA log-ratios
        for j, (kind, p) in enumerate(ma_specs):
            ma = rolling_sma(close, p) if kind == "sma" else rolling_ema(close, p)
            anchor = ma[e]
            if not np.isfinite(anchor) or anchor <= 0:
                continue
            fwd = ma[e + 1: cap + 1]
            with np.errstate(invalid="ignore", divide="ignore"):
                lr_close_ma[i, :h, j] = np.log(fwd / anchor)

        # cumulative MFE/MAE/close
        fwd_high = high[e + 1: cap + 1]
        fwd_low = low[e + 1: cap + 1]
        fwd_close = close[e + 1: cap + 1]
        cum_max_high = np.maximum.accumulate(fwd_high)
        cum_min_low = np.minimum.accumulate(fwd_low)
        mfe_by_bar[i, :h] = (cum_max_high - eff) / adr
        mae_by_bar[i, :h] = (eff - cum_min_low) / adr
        close_by_bar[i, :h] = (fwd_close - eff) / adr

        # locked variants
        stop_idx = min(int(stop_hit_bar), h - 1) if stop_hit_bar < h else h - 1
        # Lock cumulative MFE/MAE at stop_idx values
        mfe_locked[i, :h] = mfe_by_bar[i, :h]
        mae_locked[i, :h] = mae_by_bar[i, :h]
        if stop_hit_bar < h:
            mfe_locked[i, stop_idx + 1: h] = mfe_by_bar[i, stop_idx]
            mae_locked[i, stop_idx + 1: h] = mae_by_bar[i, stop_idx]

        # n_bars closed above eff (within trade life)
        in_life = fwd_close[: eff_h] > eff
        n_bars_close_above_eff[i] = int(in_life.sum())

        # scalars
        if eff_h > 0:
            tt_mfe = int(np.argmax(fwd_high[: eff_h])) if eff_h > 0 else 0
            tt_mae = int(np.argmin(fwd_low[: eff_h])) if eff_h > 0 else 0
            time_to_mfe[i] = tt_mfe
            time_to_mae[i] = tt_mae
            mfe_during_life[i] = float((fwd_high[: eff_h].max() - eff) / adr)
            mae_during_life[i] = float((eff - fwd_low[: eff_h].min()) / adr)

    return {
        "lr_close_ma": lr_close_ma,
        "mfe_by_bar": mfe_by_bar,
        "mae_by_bar": mae_by_bar,
        "close_by_bar": close_by_bar,
        "mfe_locked": mfe_locked,
        "mae_locked": mae_locked,
        "n_bars_close_above_eff": n_bars_close_above_eff,
        "time_to_mfe": time_to_mfe,
        "time_to_mae": time_to_mae,
        "mfe_during_life": mfe_during_life,
        "mae_during_life": mae_during_life,
        "eff_horizons": eff_horizons,
        "horizons": horizons,
        "max_h": max_h,
        "n_ma": n_ma,
        "ma_specs": ma_specs,
    }


# ───────────────────────── Labelers ─────────────────────────
def envelope_lower_per_offset(feat_2d_ex: np.ndarray) -> np.ndarray:
    """For 2D feature (n_ex, max_h), return Lower(k) = nanmin over examples."""
    with np.errstate(invalid="ignore"):
        return np.nanmin(feat_2d_ex, axis=0)


def envelope_upper_per_offset(feat_2d_ex: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.nanmax(feat_2d_ex, axis=0)


def envelope_lower_per_cell(feat_3d_ex: np.ndarray) -> np.ndarray:
    """For 3D feature (n_ex, max_h, n_ma), return Lower(k, ma) = nanmin over examples."""
    with np.errstate(invalid="ignore"):
        return np.nanmin(feat_3d_ex, axis=0)


def envelope_upper_per_cell(feat_3d_ex: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.nanmax(feat_3d_ex, axis=0)


def verdict_envelope_2d(feat_target, lower, eff_h, upper=None):
    """Per-target boolean: True (WIN) iff feat[k] passes Lower at every active k in window.
       Optional bilateral (also check Upper if provided). NaN-lenient."""
    n, max_h = feat_target.shape
    out = np.zeros(n, dtype=bool)
    for i in range(n):
        h = min(int(eff_h[i]), max_h)
        if h == 0:
            out[i] = True
            continue
        x = feat_target[i, :h]
        l = lower[:h]
        active = np.isfinite(x) & np.isfinite(l)
        if not active.any():
            out[i] = True
            continue
        passed = (x >= l)
        if upper is not None:
            u = upper[:h]
            active = active & np.isfinite(u)
            passed = passed & (x <= u)
        passed = passed | ~active
        out[i] = bool(passed.all())
    return out


def verdict_envelope_3d(feat_target, lower, eff_h, upper=None):
    """Same as 2d but on (n, max_h, n_ma) feature. AND across all cells."""
    n, max_h, n_ma = feat_target.shape
    out = np.zeros(n, dtype=bool)
    for i in range(n):
        h = min(int(eff_h[i]), max_h)
        if h == 0:
            out[i] = True
            continue
        x = feat_target[i, :h]   # (h, n_ma)
        l = lower[:h]
        active = np.isfinite(x) & np.isfinite(l)
        if not active.any():
            out[i] = True
            continue
        passed = (x >= l)
        if upper is not None:
            u = upper[:h]
            active = active & np.isfinite(u)
            passed = passed & (x <= u)
        passed = passed | ~active
        out[i] = bool(passed.all())
    return out


# Labelers as dict: name -> function returning verdict (bool[n_clusters])
def labeler_5_bilateral_close_ma_full(feat_ex, feat_all, ex_eff_h, all_eff_h):
    lower = envelope_lower_per_cell(feat_ex["lr_close_ma"])
    upper = envelope_upper_per_cell(feat_ex["lr_close_ma"])
    full_h_ex = feat_ex["horizons"]
    full_h_all = feat_all["horizons"]
    return verdict_envelope_3d(feat_all["lr_close_ma"], lower, full_h_all, upper=upper)


def labeler_5_lower_close_ma_full(feat_ex, feat_all, ex_eff_h, all_eff_h):
    lower = envelope_lower_per_cell(feat_ex["lr_close_ma"])
    full_h_all = feat_all["horizons"]
    return verdict_envelope_3d(feat_all["lr_close_ma"], lower, full_h_all)


def labeler_5_lower_close_ma_stopwin(feat_ex, feat_all, ex_eff_h, all_eff_h):
    lower = envelope_lower_per_cell(feat_ex["lr_close_ma"])
    return verdict_envelope_3d(feat_all["lr_close_ma"], lower, all_eff_h)


def labeler_5_mfe_lower_full(feat_ex, feat_all, ex_eff_h, all_eff_h):
    lower = envelope_lower_per_offset(feat_ex["mfe_locked"])
    full_h_all = feat_all["horizons"]
    return verdict_envelope_2d(feat_all["mfe_locked"], lower, full_h_all)


def labeler_5_mfe_lower_stopwin(feat_ex, feat_all, ex_eff_h, all_eff_h):
    """MFE-by-bar lower envelope, tested over trade life only (no lock semantics needed because
       we only test up to stop_hit_bar)."""
    lower = envelope_lower_per_offset(feat_ex["mfe_by_bar"])
    return verdict_envelope_2d(feat_all["mfe_by_bar"], lower, all_eff_h)


def labeler_5_close_by_bar_lower_full(feat_ex, feat_all, ex_eff_h, all_eff_h):
    lower = envelope_lower_per_offset(feat_ex["close_by_bar"])
    full_h_all = feat_all["horizons"]
    return verdict_envelope_2d(feat_all["close_by_bar"], lower, full_h_all)


def labeler_5_combined_mfe_and_closema(feat_ex, feat_all, ex_eff_h, all_eff_h):
    """MFE-by-bar lower (full window, locked) AND close-MA lower envelope (stop-window)."""
    lower_mfe = envelope_lower_per_offset(feat_ex["mfe_locked"])
    full_h_all = feat_all["horizons"]
    v1 = verdict_envelope_2d(feat_all["mfe_locked"], lower_mfe, full_h_all)
    lower_cma = envelope_lower_per_cell(feat_ex["lr_close_ma"])
    v2 = verdict_envelope_3d(feat_all["lr_close_ma"], lower_cma, all_eff_h)
    return v1 & v2


def labeler_5_combined_mfe_and_closema_full(feat_ex, feat_all, ex_eff_h, all_eff_h):
    """MFE-by-bar lower AND close-MA bilateral, both on full window."""
    lower_mfe = envelope_lower_per_offset(feat_ex["mfe_locked"])
    full_h_all = feat_all["horizons"]
    v1 = verdict_envelope_2d(feat_all["mfe_locked"], lower_mfe, full_h_all)
    lower_cma = envelope_lower_per_cell(feat_ex["lr_close_ma"])
    upper_cma = envelope_upper_per_cell(feat_ex["lr_close_ma"])
    v2 = verdict_envelope_3d(feat_all["lr_close_ma"], lower_cma, full_h_all, upper=upper_cma)
    return v1 & v2


def obvious_loser_mask(feat_all):
    """Structural definition: forward tape NEVER closed above eff_entry on any bar in trade life."""
    return feat_all["n_bars_close_above_eff"] == 0


def labeler_4_refined_max_margin(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h,
                                  feat_all_full):
    """Max-margin separator between examples (WIN class) and structural obvious-losers (LOSS class).
       Feature space: mfe_during_life, mae_during_life, time_to_mfe, time_to_mae (4D, ADR units / bar count).
       Labeler outputs WIN for verdict on the example side of the hyperplane."""
    try:
        from sklearn.svm import LinearSVC
    except ImportError:
        return None  # sklearn not available

    # Build training set: examples (label 1) + structural obvious losers (label 0)
    n_ex = feat_ex_subset["lr_close_ma"].shape[0]

    # Obvious losers among ALL clusters (examples can never be obvious losers; they're winners by GT)
    ol_mask = obvious_loser_mask(feat_all_full)

    # Feature vectors
    def fvec(feat_dict):
        return np.column_stack([
            feat_dict["mfe_during_life"],
            feat_dict["mae_during_life"],
            feat_dict["time_to_mfe"].astype(float),
            feat_dict["time_to_mae"].astype(float),
        ])

    X_ex = fvec(feat_ex_subset)  # (n_ex, 4)
    X_all = fvec(feat_all_full)   # (n_all, 4)
    X_ol = X_all[ol_mask]         # (n_ol, 4)

    if len(X_ol) < 5:
        return None  # too few losers to train

    X_train = np.vstack([X_ex, X_ol])
    y_train = np.concatenate([np.ones(n_ex), np.zeros(len(X_ol))])

    # Standardize
    mu = X_train.mean(axis=0)
    sigma = X_train.std(axis=0)
    sigma = np.where(sigma > 0, sigma, 1.0)
    X_train_z = (X_train - mu) / sigma
    X_all_z = (X_all - mu) / sigma

    clf = LinearSVC(C=1.0, max_iter=10000, dual=True)
    try:
        clf.fit(X_train_z, y_train)
    except Exception:
        return None

    pred_all = clf.decision_function(X_all_z)
    return pred_all > 0  # WIN class


def labeler_4_refined_kernel_svm(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h,
                                  feat_all_full):
    try:
        from sklearn.svm import SVC
    except ImportError:
        return None
    n_ex = feat_ex_subset["lr_close_ma"].shape[0]
    ol_mask = obvious_loser_mask(feat_all_full)
    def fvec(feat_dict):
        return np.column_stack([
            feat_dict["mfe_during_life"],
            feat_dict["mae_during_life"],
            feat_dict["time_to_mfe"].astype(float),
            feat_dict["time_to_mae"].astype(float),
        ])
    X_ex = fvec(feat_ex_subset)
    X_all = fvec(feat_all_full)
    X_ol = X_all[ol_mask]
    if len(X_ol) < 5:
        return None
    X_train = np.vstack([X_ex, X_ol])
    y_train = np.concatenate([np.ones(n_ex), np.zeros(len(X_ol))])
    mu = X_train.mean(axis=0); sigma = X_train.std(axis=0)
    sigma = np.where(sigma > 0, sigma, 1.0)
    X_train_z = (X_train - mu) / sigma
    X_all_z = (X_all - mu) / sigma
    clf = SVC(kernel="rbf", C=1.0, gamma="scale")
    try:
        clf.fit(X_train_z, y_train)
    except Exception:
        return None
    return clf.predict(X_all_z) == 1


def labeler_one_class_svm(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h, feat_all_full):
    try:
        from sklearn.svm import OneClassSVM
    except ImportError:
        return None
    def fvec(feat_dict):
        return np.column_stack([
            feat_dict["mfe_during_life"],
            feat_dict["mae_during_life"],
            feat_dict["time_to_mfe"].astype(float),
            feat_dict["time_to_mae"].astype(float),
        ])
    X_ex = fvec(feat_ex_subset)
    X_all = fvec(feat_all_full)
    mu = X_ex.mean(axis=0); sigma = X_ex.std(axis=0)
    sigma = np.where(sigma > 0, sigma, 1.0)
    X_ex_z = (X_ex - mu) / sigma
    X_all_z = (X_all - mu) / sigma
    clf = OneClassSVM(kernel="rbf", gamma="scale", nu=0.05)
    try:
        clf.fit(X_ex_z)
    except Exception:
        return None
    return clf.predict(X_all_z) == 1


def labeler_knn_examples_vs_losers(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h, feat_all_full,
                                    K=5):
    try:
        from sklearn.neighbors import KNeighborsClassifier
    except ImportError:
        return None
    n_ex = feat_ex_subset["lr_close_ma"].shape[0]
    ol_mask = obvious_loser_mask(feat_all_full)
    def fvec(feat_dict):
        return np.column_stack([
            feat_dict["mfe_during_life"],
            feat_dict["mae_during_life"],
            feat_dict["time_to_mfe"].astype(float),
            feat_dict["time_to_mae"].astype(float),
        ])
    X_ex = fvec(feat_ex_subset)
    X_all = fvec(feat_all_full)
    X_ol = X_all[ol_mask]
    if len(X_ol) < K:
        return None
    X_train = np.vstack([X_ex, X_ol])
    y_train = np.concatenate([np.ones(n_ex), np.zeros(len(X_ol))])
    mu = X_train.mean(axis=0); sigma = X_train.std(axis=0)
    sigma = np.where(sigma > 0, sigma, 1.0)
    X_train_z = (X_train - mu) / sigma
    X_all_z = (X_all - mu) / sigma
    clf = KNeighborsClassifier(n_neighbors=K)
    clf.fit(X_train_z, y_train)
    return clf.predict(X_all_z) == 1


def labeler_scalar_closed_above_at_least_once(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h,
                                              feat_all_full):
    """Pure structural: WIN iff at least one bar in trade life closed above eff_entry."""
    return feat_all_full["n_bars_close_above_eff"] > 0


def labeler_scalar_mfe_above_min_example(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h,
                                         feat_all_full):
    """Wild WIN iff mfe_during_life >= min(example mfe_during_life). Examples WIN by construction."""
    min_ex_mfe = feat_ex_subset["mfe_during_life"].min()
    return feat_all_full["mfe_during_life"] >= min_ex_mfe


def labeler_composite_mfe_and_closed_above(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h,
                                           feat_all_full):
    """L14 AND L13: WIN iff mfe_during_life >= min_ex_mfe AND at least one bar closed above eff."""
    min_ex_mfe = feat_ex_subset["mfe_during_life"].min()
    cond1 = feat_all_full["mfe_during_life"] >= min_ex_mfe
    cond2 = feat_all_full["n_bars_close_above_eff"] > 0
    return cond1 & cond2


def labeler_composite_mfe_and_envelope(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h,
                                       feat_all_full):
    """L14 AND L04: scalar gate AND per-bar MFE envelope."""
    min_ex_mfe = feat_ex_subset["mfe_during_life"].min()
    cond1 = feat_all_full["mfe_during_life"] >= min_ex_mfe
    lower = envelope_lower_per_offset(feat_ex_subset["mfe_locked"])
    full_h = feat_all_full["horizons"]
    cond2 = verdict_envelope_2d(feat_all_full["mfe_locked"], lower, full_h)
    return cond1 & cond2


def labeler_scalar_kneedle_mfe(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h, feat_all_full):
    """Kneedle elbow on sorted MFE distribution of examples + obvious-losers combined.
       Threshold derived from kneedle. Wild >= T → WIN."""
    n_ex = feat_ex_subset["lr_close_ma"].shape[0]
    ol_mask = obvious_loser_mask(feat_all_full)
    pop = np.concatenate([
        feat_ex_subset["mfe_during_life"],
        feat_all_full["mfe_during_life"][ol_mask],
    ])
    pop_sorted = np.sort(pop)
    # kneedle: max perpendicular distance from line connecting first to last
    n = len(pop_sorted)
    if n < 3:
        return feat_all_full["mfe_during_life"] >= pop_sorted[0]
    x = np.arange(n) / (n - 1)
    y = (pop_sorted - pop_sorted[0]) / (pop_sorted[-1] - pop_sorted[0] + 1e-12)
    # diagonal y=x; perpendicular distance ~ |y - x|
    d = y - x
    knee_idx = int(np.argmax(d)) if d.max() > 0 else 0
    T = pop_sorted[knee_idx]
    # Force lock: T must be <= min_ex_mfe so examples pass
    T = min(T, feat_ex_subset["mfe_during_life"].min())
    return feat_all_full["mfe_during_life"] >= T


def labeler_mahalanobis(feat_ex_subset, feat_all_subset, ex_eff_h, all_eff_h, feat_all_full):
    """Mahalanobis distance from example centroid; threshold = max(example distance).
       Wild within radius → WIN."""
    def fvec(feat_dict):
        return np.column_stack([
            feat_dict["mfe_during_life"],
            feat_dict["mae_during_life"],
            feat_dict["time_to_mfe"].astype(float),
            feat_dict["time_to_mae"].astype(float),
        ])
    X_ex = fvec(feat_ex_subset)
    X_all = fvec(feat_all_full)
    mu = X_ex.mean(axis=0)
    cov = np.cov(X_ex.T) + 1e-6 * np.eye(X_ex.shape[1])
    try:
        cov_inv = np.linalg.inv(cov)
    except np.linalg.LinAlgError:
        return None
    def dist(X):
        d = X - mu
        return np.sqrt(np.einsum("ij,jk,ik->i", d, cov_inv, d))
    d_ex = dist(X_ex)
    d_all = dist(X_all)
    R = d_ex.max()
    return d_all <= R


# ───────────────────────── Eval harness ─────────────────────────
def evaluate_labeler(name, verdict_all, n_examples, n_wild, halted, meta_all,
                     feat_all, case_targets):
    """Return dict of evaluation metrics."""
    if verdict_all is None:
        return {"name": name, "status": "skipped", "reason": "labeler returned None"}

    ex_v = verdict_all[:n_examples]
    wild_v = verdict_all[n_examples:]
    examples_lock = int(ex_v.sum())
    n_wild_win = int(wild_v.sum())
    wild_win_rate = float(n_wild_win / n_wild) if n_wild > 0 else 0.0

    # Cross-tab
    mfe = feat_all["mfe_during_life"][n_examples:]
    eff_h = feat_all["eff_horizons"][n_examples:]
    horiz = feat_all["horizons"][n_examples:]
    n_above = feat_all["n_bars_close_above_eff"][n_examples:]
    early_stop = eff_h < horiz

    win_mask = wild_v
    loss_mask = ~wild_v

    def safe_med(a):
        return float(np.median(a)) if len(a) else None

    crosstab = {
        "wild_win": {
            "n": int(win_mask.sum()),
            "mfe_median": safe_med(mfe[win_mask]),
            "stop_hit_median": safe_med(eff_h[win_mask].astype(float)),
            "early_stop_rate": float(early_stop[win_mask].mean()) if win_mask.any() else None,
            "n_above_median": safe_med(n_above[win_mask].astype(float)),
        },
        "wild_loss": {
            "n": int(loss_mask.sum()),
            "mfe_median": safe_med(mfe[loss_mask]),
            "stop_hit_median": safe_med(eff_h[loss_mask].astype(float)),
            "early_stop_rate": float(early_stop[loss_mask].mean()) if loss_mask.any() else None,
            "n_above_median": safe_med(n_above[loss_mask].astype(float)),
        },
    }

    # Rule consensus
    top = halted["top_conditions"]
    cluster_id_to_pool = {m["cluster_id"]: i for i, m in enumerate(halted["cluster_meta"])}
    rule_consensus = np.zeros(n_examples + n_wild, dtype=np.int64)
    for i, m in enumerate(meta_all):
        pool_i = cluster_id_to_pool[m["cluster_id"]]
        rule_consensus[i] = sum(1 for r in top if r["per_cluster_final_label"][pool_i] == "WIN")
    rc_wild = rule_consensus[n_examples:]
    crosstab["wild_win"]["rule_consensus_median"] = safe_med(rc_wild[win_mask].astype(float))
    crosstab["wild_loss"]["rule_consensus_median"] = safe_med(rc_wild[loss_mask].astype(float))

    # Case checks: TAN cid=231, CRSR cid=67, LOGI cid=149, GOLD cid=105, VNDA cid=251, GBTC cid=98
    cases = {}
    cluster_id_to_meta_i = {m["cluster_id"]: i for i, m in enumerate(meta_all)}
    for tk, cid, want in case_targets:
        if cid not in cluster_id_to_meta_i:
            cases[f"{tk}_cid{cid}"] = "missing"
            continue
        i = cluster_id_to_meta_i[cid]
        v = "WIN" if verdict_all[i] else "LOSS"
        cases[f"{tk}_cid{cid}"] = {"want": want, "got": v, "match": v == want}

    # Composite score: fraction of cases correct
    case_score = sum(1 for c in cases.values() if isinstance(c, dict) and c["match"]) / max(
        sum(1 for c in cases.values() if isinstance(c, dict)), 1
    )

    # Cross-tab quality: mfe gap and rule-consensus gap
    if win_mask.any() and loss_mask.any():
        mfe_gap = crosstab["wild_win"]["mfe_median"] - crosstab["wild_loss"]["mfe_median"]
        rc_gap = (crosstab["wild_win"]["rule_consensus_median"] or 0) - (
                  crosstab["wild_loss"]["rule_consensus_median"] or 0)
    else:
        mfe_gap = None; rc_gap = None

    return {
        "name": name,
        "status": "ok" if examples_lock == n_examples else f"LOCK_FAIL ({examples_lock}/{n_examples})",
        "examples_lock": examples_lock,
        "n_wild_win": n_wild_win,
        "wild_win_rate": wild_win_rate,
        "crosstab": crosstab,
        "cases": cases,
        "case_score": case_score,
        "mfe_gap": mfe_gap,
        "rc_gap": rc_gap,
    }


def main():
    print("Loading HALTED HTF JSON...")
    halted = json.load(open(HALTED))
    meta = halted["cluster_meta"]
    entered = [m for m in meta if m["status"] == "ENTERED"]
    examples = [m for m in entered if m["is_example"] == 1]
    wild = [m for m in entered if m["is_example"] == 0]
    print(f"  entered: {len(entered)}  examples: {len(examples)}  wild: {len(wild)}")

    print("Loading OHLCV...")
    with open(OHLCV, "rb") as f:
        ohlcv = pickle.load(f)
    print(f"  OHLCV tickers: {len(ohlcv):,}")

    ma_specs = [("sma", p) for p in SMA_PERIODS] + [("ema", p) for p in EMA_PERIODS]

    print("Building features for examples + wild combined...")
    t0 = time.time()
    meta_all = examples + wild
    feat_all = build_all_features(meta_all, ohlcv, ma_specs)
    print(f"  done in {time.time()-t0:.1f}s")

    n_ex = len(examples); n_wild = len(wild)

    feat_ex = {k: v[:n_ex] if isinstance(v, np.ndarray) else v for k, v in feat_all.items()}
    feat_wild = {k: v[n_ex:] if isinstance(v, np.ndarray) else v for k, v in feat_all.items()}

    ex_eff_h = feat_all["eff_horizons"][:n_ex]
    wild_eff_h = feat_all["eff_horizons"][n_ex:]
    all_eff_h = feat_all["eff_horizons"]

    # CORRECTED case targets (post-investigation 2026-04-25):
    # The cluster_meta mfe_adr field is over the full forward window (post-stop dead-tape included),
    # which misled the original case targets. The correct C2 interpretation is "trade had large MFE
    # *during its lifetime* (pre-stop)." Verified mfe_during_life values below per OHLC.
    case_targets = [
        ("TAN",  231, "WIN"),    # mfe_life=68.37 (no stop) — clear WIN
        ("TAN",  232, "LOSS"),   # mfe_life=1.18 (stop=3, post-stop reached 64) — clear LOSS
        ("HYMC", 125, "LOSS"),   # mfe_life=0.59 (stop=1, post-stop reached 53) — clear LOSS
        ("LOGI", 149, "LOSS"),   # mfe_life=2.27 (stop=5, post-stop reached 12) — borderline LOSS
        ("CRSR",  67, "LOSS"),   # mfe_life=1.04 (stop=2) — clear LOSS, low MFE during life
        ("GOSS", 108, "LOSS"),   # mfe_life=0.60 (stop=1) — clear LOSS
        ("VNDA", 251, "LOSS"),   # mfe_life=0.08 (stop=0) — clear LOSS
        ("AGI",    6, "WIN"),    # mfe_life=2.40 (full-life) — borderline WIN, just above OSCR's 2.38
        ("BZH",   41, "WIN"),    # mfe_life=9.09 (full-life) — clear WIN, real run
        ("APLS",  13, "LOSS"),   # mfe_life=0.42 (stop=2) — clear LOSS
    ]
    # Verify these CIDs exist
    cid_set = {m["cluster_id"] for m in meta_all}
    case_targets = [(t, c, w) for t, c, w in case_targets if c in cid_set]
    print(f"  Case targets: {len(case_targets)}")

    labelers = [
        # (name, function, feature space description)
        ("L01_5_bilateral_close_ma_full", labeler_5_bilateral_close_ma_full,
         "(5) bilateral envelope on close-MA log-ratios, full window"),
        ("L02_5_lower_close_ma_full", labeler_5_lower_close_ma_full,
         "(5) one-sided lower envelope on close-MA, full window"),
        ("L03_5_lower_close_ma_stopwin", labeler_5_lower_close_ma_stopwin,
         "(5) one-sided lower close-MA, stop-window (Fix B)"),
        ("L04_5_mfe_lower_full", labeler_5_mfe_lower_full,
         "(5) one-sided lower envelope on MFE-by-bar (locked at stop), full window"),
        ("L05_5_mfe_lower_stopwin", labeler_5_mfe_lower_stopwin,
         "(5) one-sided lower envelope on MFE-by-bar (no lock), stop-window"),
        ("L06_5_close_by_bar_lower_full", labeler_5_close_by_bar_lower_full,
         "(5) one-sided lower envelope on raw close-by-bar (no MA smoothing), full window"),
        ("L07_5_combined_mfe_AND_closema_stopwin", labeler_5_combined_mfe_and_closema,
         "(5) MFE-lower-full AND close-MA-lower-stopwin"),
        ("L08_5_combined_mfe_AND_closema_full", labeler_5_combined_mfe_and_closema_full,
         "(5) MFE-lower-full AND close-MA-bilateral-full"),
    ]

    # ML labelers — call with full feat_all
    ml_labelers = [
        ("L09_4refined_max_margin_linear", labeler_4_refined_max_margin,
         "(4-refined) linear max-margin separator, examples vs structural obvious-losers"),
        ("L10_4refined_kernel_svm_rbf", labeler_4_refined_kernel_svm,
         "(4-refined) RBF-kernel SVM on examples vs obvious-losers"),
        ("L11_one_class_svm_examples", labeler_one_class_svm,
         "One-class SVM trained on examples only, RBF kernel"),
        ("L12_knn_examples_vs_losers_K5", labeler_knn_examples_vs_losers,
         "K-NN K=5 against examples + obvious-losers"),
        ("L13_scalar_closed_above_eff", labeler_scalar_closed_above_at_least_once,
         "Pure scalar: WIN iff trade life had any bar close above effective_entry"),
        ("L14_scalar_mfe_above_min_ex", labeler_scalar_mfe_above_min_example,
         "Pure scalar: WIN iff mfe_during_life >= min(example mfe_during_life)"),
        ("L15_scalar_kneedle_mfe", labeler_scalar_kneedle_mfe,
         "Kneedle elbow on combined examples + obvious-losers MFE distribution"),
        ("L16_mahalanobis", labeler_mahalanobis,
         "Mahalanobis distance from example centroid; radius = max example distance"),
        ("L17_composite_L14_AND_L13", labeler_composite_mfe_and_closed_above,
         "L14 AND L13: mfe_life >= min_ex_mfe AND at least one bar closed above eff"),
        ("L18_composite_L14_AND_L04", labeler_composite_mfe_and_envelope,
         "L14 AND L04: scalar mfe gate AND per-bar MFE envelope"),
    ]

    results = []

    # Run envelope labelers (signature: feat_ex, feat_all_input, ex_eff_h, all_eff_h)
    for name, fn, desc in labelers:
        print(f"\n→ {name}: {desc}")
        try:
            v = fn(feat_ex, feat_all, ex_eff_h, all_eff_h)
            r = evaluate_labeler(name, v, n_ex, n_wild, halted, meta_all, feat_all, case_targets)
            r["description"] = desc
        except Exception as e:
            r = {"name": name, "status": f"error: {type(e).__name__}: {e}", "description": desc}
        print(f"   {r.get('status','?')} | wild_win_rate={r.get('wild_win_rate', 'n/a')}")
        results.append(r)

    # ML labelers signature different
    for name, fn, desc in ml_labelers:
        print(f"\n→ {name}: {desc}")
        try:
            v = fn(feat_ex, feat_wild, ex_eff_h, wild_eff_h, feat_all)
            r = evaluate_labeler(name, v, n_ex, n_wild, halted, meta_all, feat_all, case_targets)
            r["description"] = desc
        except Exception as e:
            import traceback
            r = {"name": name, "status": f"error: {type(e).__name__}: {e}",
                 "description": desc, "traceback": traceback.format_exc()[-500:]}
        print(f"   {r.get('status','?')} | wild_win_rate={r.get('wild_win_rate', 'n/a')}")
        results.append(r)

    # ──── Ranking ────
    print("\n\n══════════ RANKING ══════════")
    valid = [r for r in results if r.get("examples_lock") is not None
             and r.get("examples_lock") == n_ex]
    print(f"Labelers with lock=28/28: {len(valid)} / {len(results)}")

    # Score each labeler:
    #   case_score (fraction of 10 cases correct) [0..1]
    #   mfe_gap (positive = better)
    #   rc_gap (positive = better)
    #   non-degenerate admission (penalty for <5% or >95%)
    for r in valid:
        admit = r["wild_win_rate"]
        admit_penalty = 0.0
        if admit < 0.05 or admit > 0.95:
            admit_penalty = -0.5
        elif admit < 0.10 or admit > 0.85:
            admit_penalty = -0.2
        score = (r["case_score"] * 1.0
                 + (r["mfe_gap"] / 10 if r["mfe_gap"] else 0) * 0.5
                 + (r["rc_gap"] / 50 if r["rc_gap"] else 0) * 1.0
                 + admit_penalty)
        r["composite_score"] = float(score)

    valid.sort(key=lambda r: -r.get("composite_score", -1e9))

    print(f"\n{'rank':>4s} {'labeler':<45s} {'lock':>5s} {'admit':>6s} {'case':>5s} {'mfe_gap':>8s} {'rc_gap':>7s} {'score':>7s}")
    for rank, r in enumerate(valid, 1):
        print(f"{rank:>4d} {r['name']:<45s} {r['examples_lock']:>3d}/{n_ex:<2d} "
              f"{100*r['wild_win_rate']:>5.1f}%  "
              f"{r.get('case_score',0):>4.2f} "
              f"{(r.get('mfe_gap') or 0):>7.2f} "
              f"{(r.get('rc_gap') or 0):>7.1f} "
              f"{r.get('composite_score',0):>7.3f}")

    print(f"\n  Lock-failed labelers:")
    for r in results:
        if r not in valid:
            print(f"    {r['name']}: {r.get('status', '?')}")

    # Save
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "setup": "htf",
        "n_examples": n_ex,
        "n_wild": n_wild,
        "case_targets": [{"ticker": t, "cluster_id": c, "want": w} for t, c, w in case_targets],
        "results_ranked": valid,
        "results_unranked": [r for r in results if r not in valid],
    }
    out_path = os.path.join(OUT_DIR, "results.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")

    # Summary text
    summary_path = os.path.join(OUT_DIR, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("Labeler bakeoff — HTF\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Examples: {n_ex}, Wild ENTERED: {n_wild}\n")
        f.write(f"Generated: {out['generated_at']}\n\n")
        f.write("Ranked (lock-passing only):\n")
        for rank, r in enumerate(valid, 1):
            f.write(f"\n{rank}. {r['name']}  score={r.get('composite_score',0):.3f}\n")
            f.write(f"   {r.get('description', '')}\n")
            f.write(f"   Lock: {r['examples_lock']}/{n_ex}  "
                    f"Wild WIN: {r['n_wild_win']}/{n_wild} ({100*r['wild_win_rate']:.1f}%)\n")
            f.write(f"   MFE-gap (WIN-LOSS): {r.get('mfe_gap', 'n/a')}  "
                    f"RC-gap: {r.get('rc_gap', 'n/a')}\n")
            f.write(f"   Cases: {r['case_score']*10:.0f}/10 correct\n")
            for cn, cv in r.get("cases", {}).items():
                if isinstance(cv, dict):
                    mark = "OK " if cv["match"] else "BAD"
                    f.write(f"      [{mark}] {cn:>20s}  want={cv['want']}  got={cv['got']}\n")
            ct = r.get("crosstab", {})
            f.write(f"   WIN  cluster crosstab: mfe={ct.get('wild_win',{}).get('mfe_median')}  "
                    f"early_stop={ct.get('wild_win',{}).get('early_stop_rate')}  "
                    f"rc_med={ct.get('wild_win',{}).get('rule_consensus_median')}\n")
            f.write(f"   LOSS cluster crosstab: mfe={ct.get('wild_loss',{}).get('mfe_median')}  "
                    f"early_stop={ct.get('wild_loss',{}).get('early_stop_rate')}  "
                    f"rc_med={ct.get('wild_loss',{}).get('rule_consensus_median')}\n")
    print(f"  Summary: {summary_path}")


if __name__ == "__main__":
    main()
