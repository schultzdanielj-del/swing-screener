"""Diagnostic: does an MA-vector basis separate example setups from the
random market bar population?

At each labeled example's entry bar E, compute a 12-value vector:
  log(MA(p, style)(E) / close(E))
for p in [5, 10, 20, 50, 100, 200] and style in {SMA, EMA}.
Also compute the same vector at ~10,000 random post-2020 market bars.

For each setup, measure:
  - Example cluster centroid and covariance in MA-space.
  - Market cluster centroid.
  - Mahalanobis separation between centroids (using pooled covariance).
  - Max example Mahalanobis distance from own centroid (example cluster radius).
  - Fraction of market bars within that max-example-distance ball (= how
    much of the market an SD-band filter around examples would admit).
  - Per-MA-dim bounding box filter: fraction of market admitted.

The last two are direct proxies for how much carve the MA-vector basis
would provide per setup.
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

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_ma_shape_test")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
MA_PERIODS = [5, 10, 20, 50, 100, 200]
DATE_CUTOFF_STR = "2020-01-02"
N_MARKET_SAMPLE = 20000  # random market bars per pool
SEED = 42
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def ma_vector_at(close, E_idx):
    """Return a 12-value vector: log(SMA(p)/close(E)) and log(EMA(p)/close(E))
    for p in MA_PERIODS. NaN if insufficient history (need at least MA_PERIODS[-1]
    bars). Feature order: SMA5, SMA10, ..., SMA200, EMA5, ..., EMA200.
    """
    if E_idx < max(MA_PERIODS):
        return None  # not enough history for MA200
    close_E = close[E_idx]
    if not np.isfinite(close_E) or close_E <= 0:
        return None
    values = np.full(2 * len(MA_PERIODS), np.nan, dtype=np.float64)
    log_close_E = np.log(close_E)
    for i, p in enumerate(MA_PERIODS):
        window = close[E_idx - p + 1:E_idx + 1]  # p bars ending at E (inclusive)
        if len(window) < p:
            continue
        finite_mask = np.isfinite(window) & (window > 0)
        if not finite_mask.all():
            continue
        # SMA
        sma = window.mean()
        if sma > 0:
            values[i] = np.log(sma) - log_close_E
        # EMA: use adjust=False recursive formula, seed = first element
        # EMA(t) = alpha * x(t) + (1-alpha) * EMA(t-1), alpha = 2 / (p + 1)
        alpha = 2.0 / (p + 1.0)
        ema = window[0]
        for v in window[1:]:
            ema = alpha * v + (1 - alpha) * ema
        if ema > 0:
            values[len(MA_PERIODS) + i] = np.log(ema) - log_close_E
    return values


def dates_as_str(df):
    return vsc.dates_as_str(df)


def sample_market_bars(universe, n_target, rng):
    """Sample (ticker, E_idx) bars with E_idx >= max(MA_PERIODS) and date >= cutoff."""
    tickers = list(universe.keys())
    sampled = []
    # For efficiency: keep trying random tickers until we have n_target valid bars.
    max_attempts = n_target * 5
    attempts = 0
    while len(sampled) < n_target and attempts < max_attempts:
        t = tickers[rng.integers(0, len(tickers))]
        df = universe[t]
        close = df["close"].values.astype(np.float64)
        L = len(close)
        if L < max(MA_PERIODS) + 1:
            attempts += 1
            continue
        ds = dates_as_str(df)
        # Eligible E indices with date >= cutoff
        eligible = np.where((np.arange(L) >= max(MA_PERIODS)) & (ds >= DATE_CUTOFF_STR))[0]
        if len(eligible) == 0:
            attempts += 1
            continue
        E = int(eligible[rng.integers(0, len(eligible))])
        v = ma_vector_at(close, E)
        if v is None or not np.all(np.isfinite(v)):
            attempts += 1
            continue
        sampled.append({"ticker": t, "E_idx": E, "date": ds[E], "v": v})
        attempts += 1
    return sampled


def compute_example_vectors(setup, universe):
    examples = get_examples(setup)
    vecs = []
    meta = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        close = df["close"].values.astype(np.float64)
        v = ma_vector_at(close, E)
        if v is None or not np.all(np.isfinite(v)):
            continue
        vecs.append(v)
        meta.append({"ticker": ex["ticker"], "entry_date": ex["entry_date"], "E_idx": E})
    return np.array(vecs), meta


def mahalanobis_sq(X, mu, cov_inv):
    """X: (n, d). Return array of squared Mahalanobis distances (n,)."""
    diff = X - mu[None, :]
    return np.einsum("ni,ij,nj->n", diff, cov_inv, diff)


def analyze_setup(setup, universe, market_vectors, rng):
    print(f"\n{'=' * 80}", flush=True)
    print(f"=== {setup.upper()}", flush=True)
    print(f"{'=' * 80}", flush=True)

    ex_vecs, ex_meta = compute_example_vectors(setup, universe)
    n_ex = len(ex_vecs)
    print(f"example MA-vectors: {n_ex} (had MA200 available at entry)", flush=True)
    if n_ex < len(MA_PERIODS) * 2 + 2:
        print(f"  WARN: n_ex < d+2 — covariance will be rank-deficient", flush=True)

    mkt_vecs = np.array([m["v"] for m in market_vectors])
    n_mkt = len(mkt_vecs)

    # Example cluster stats
    mu_ex = ex_vecs.mean(axis=0)
    # Use example covariance with small ridge for invertibility
    cov_ex = np.cov(ex_vecs.T, ddof=1)
    cov_ex_reg = cov_ex + 1e-6 * np.eye(cov_ex.shape[0])
    try:
        cov_ex_inv = np.linalg.inv(cov_ex_reg)
    except np.linalg.LinAlgError:
        print(f"  covariance singular; using pseudoinverse", flush=True)
        cov_ex_inv = np.linalg.pinv(cov_ex_reg)

    # Market cluster stats
    mu_mkt = mkt_vecs.mean(axis=0)

    # Pooled covariance for centroid-separation measurement (use market cov as reference)
    cov_mkt = np.cov(mkt_vecs.T, ddof=1) + 1e-6 * np.eye(mkt_vecs.shape[1])
    try:
        cov_mkt_inv = np.linalg.inv(cov_mkt)
    except np.linalg.LinAlgError:
        cov_mkt_inv = np.linalg.pinv(cov_mkt)

    # Centroid separation: Mahalanobis distance between mu_ex and mu_mkt under market covariance
    diff = mu_ex - mu_mkt
    d_centroid_mkt_units = float(np.sqrt(diff @ cov_mkt_inv @ diff))

    # Example cluster radius: max Mahalanobis distance (under example covariance) from mu_ex
    d2_ex_self = mahalanobis_sq(ex_vecs, mu_ex, cov_ex_inv)
    r_ex_max = float(np.sqrt(d2_ex_self.max()))
    r_ex_median = float(np.sqrt(np.median(d2_ex_self)))

    # Fraction of market bars inside the example Mahalanobis ball of radius r_ex_max
    d2_mkt_to_ex = mahalanobis_sq(mkt_vecs, mu_ex, cov_ex_inv)
    frac_mkt_in_ellipsoid = float(np.mean(d2_mkt_to_ex <= r_ex_max ** 2))

    # Per-MA bounding-box filter: fraction of market admitted if per-dim bound
    # is [min_ex, max_ex] and strict-AND across the 12 dims.
    lo = ex_vecs.min(axis=0)
    hi = ex_vecs.max(axis=0)
    in_bbox = np.all((mkt_vecs >= lo[None, :]) & (mkt_vecs <= hi[None, :]), axis=1)
    frac_mkt_in_bbox = float(np.mean(in_bbox))

    # Per-MA SD-band filter: band = mean_ex +/- k * sd_ex with k = max example z-score
    sd = ex_vecs.std(axis=0, ddof=1)
    sd = np.where(sd > 0, sd, 1e-12)
    z_ex = (ex_vecs - mu_ex[None, :]) / sd[None, :]
    k_per_dim = np.abs(z_ex).max(axis=0)  # max z-score per dim across examples
    lo_sd = mu_ex - k_per_dim * sd
    hi_sd = mu_ex + k_per_dim * sd
    in_sdband = np.all((mkt_vecs >= lo_sd[None, :]) & (mkt_vecs <= hi_sd[None, :]), axis=1)
    frac_mkt_in_sdband = float(np.mean(in_sdband))

    print(f"  centroid separation (in market-std units): {d_centroid_mkt_units:.3f}", flush=True)
    print(f"  example cluster radius (Mahalanobis, own cov): max={r_ex_max:.3f} median={r_ex_median:.3f}", flush=True)
    print(f"  FRAC MARKET ADMITTED:", flush=True)
    print(f"    Mahalanobis ball (radius=max example dist):   {frac_mkt_in_ellipsoid*100:7.3f}%", flush=True)
    print(f"    per-dim bounding box [min_ex, max_ex]:        {frac_mkt_in_bbox*100:7.3f}%", flush=True)
    print(f"    per-dim SD-band (k = max example z-score):    {frac_mkt_in_sdband*100:7.3f}%", flush=True)

    return {
        "setup": setup,
        "n_ex": n_ex,
        "n_mkt": n_mkt,
        "mu_ex": mu_ex.tolist(),
        "mu_mkt": mu_mkt.tolist(),
        "d_centroid_mkt_units": d_centroid_mkt_units,
        "r_ex_max": r_ex_max,
        "r_ex_median": r_ex_median,
        "frac_market_in_mahalanobis_ball": frac_mkt_in_ellipsoid,
        "frac_market_in_per_dim_bbox": frac_mkt_in_bbox,
        "frac_market_in_sd_band": frac_mkt_in_sdband,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(SEED)

    print(f"OUT: {OUT_DIR}", flush=True)
    print(f"Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print("FAIL: universe count too low"); sys.exit(1)

    print(f"Sampling {N_MARKET_SAMPLE:,} random market bars post-{DATE_CUTOFF_STR}...", flush=True)
    t0 = time.time()
    market = sample_market_bars(universe, N_MARKET_SAMPLE, rng)
    print(f"  got {len(market):,} valid market MA-vectors ({time.time()-t0:.1f}s)", flush=True)

    results = []
    for setup in SETUPS:
        r = analyze_setup(setup, universe, market, rng)
        results.append(r)

    # Master table
    print(f"\n{'=' * 100}", flush=True)
    print(f"=== MASTER — fraction of market admitted by MA-vector filter (entry bar only)", flush=True)
    print(f"{'=' * 100}", flush=True)
    print(f"{'setup':<8}{'n_ex':>6}{'centroid_sep':>16}{'r_ex_max':>12}"
          f"{'mkt_in_ell':>14}{'mkt_in_bbox':>14}{'mkt_in_sdband':>16}", flush=True)
    for r in results:
        print(f"{r['setup']:<8}{r['n_ex']:>6}{r['d_centroid_mkt_units']:>16.3f}"
              f"{r['r_ex_max']:>12.3f}{r['frac_market_in_mahalanobis_ball']*100:>13.3f}%"
              f"{r['frac_market_in_per_dim_bbox']*100:>13.3f}%"
              f"{r['frac_market_in_sd_band']*100:>15.3f}%", flush=True)

    with open(os.path.join(OUT_DIR, "ma_shape_test.json"), "w") as f:
        json.dump({"results": results, "ma_periods": MA_PERIODS, "n_market_sample": N_MARKET_SAMPLE}, f, indent=2)

    print(f"\nWrote {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
