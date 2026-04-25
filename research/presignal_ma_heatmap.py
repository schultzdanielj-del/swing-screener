"""Heatmap diagnostic: per-setup discriminating power of each (MA type, bar
offset) cell.

At each cell (m, k), build a bounding-box corridor [min(ex), max(ex)] of
log(MA_m(E - k) / close(E)) across the labeled examples. Corridor preserves
100% EX by construction. Compute the fraction of a random post-2020 market
sample that falls OUTSIDE this corridor at that cell — this is the cell's
reject rate, a direct measure of how much carving the single-cell filter
produces.

Dark cells (high reject) = meat. Light cells (low reject) = fat.

Writes PNG heatmaps + reject-rate matrix JSON per setup.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
import time

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_ma_heatmap")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
MA_PERIODS = [5, 10, 20, 50, 100, 200]
MA_TYPES = []  # filled in order: SMA5..200, EMA5..200
for p in MA_PERIODS:
    MA_TYPES.append(f"SMA{p}")
for p in MA_PERIODS:
    MA_TYPES.append(f"EMA{p}")

MAX_OFFSET = 40       # bars back from entry (offset 0 = entry bar)
DATE_CUTOFF = "2020-01-02"
N_MARKET_SAMPLE = 10000
SEED = 42
MIN_TICKER_COUNT = 11000


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def compute_mas(close):
    """Return (n_MA_types, L) array of MA values. NaN where insufficient history."""
    L = len(close)
    c = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    out = np.full((2 * len(MA_PERIODS), L), np.nan, dtype=np.float64)
    # SMA via rolling sum
    cumsum_c = np.cumsum(np.where(np.isfinite(c), c, 0.0))
    cumsum_ok = np.cumsum(np.isfinite(c).astype(np.int64))
    for i, p in enumerate(MA_PERIODS):
        if L < p:
            continue
        # Sum over last p values ending at index t (inclusive): cumsum[t] - cumsum[t-p]
        num = cumsum_c[p - 1:] - np.concatenate([[0.0], cumsum_c[:L - p]])
        count = cumsum_ok[p - 1:] - np.concatenate([[0], cumsum_ok[:L - p]])
        sma = np.where(count == p, num / p, np.nan)
        out[i, p - 1:L] = sma
    # EMA via recursion
    for i, p in enumerate(MA_PERIODS):
        alpha = 2.0 / (p + 1.0)
        ema = np.full(L, np.nan)
        # Seed with first finite value
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
        out[len(MA_PERIODS) + i, :] = ema
    return out


def feature_vec_at(mas, close, E):
    """Return (n_MA_types, MAX_OFFSET) matrix of log(MA(E - k) / close(E)) for
    k = 0..MAX_OFFSET-1. NaN where MA is missing or close(E) invalid."""
    L = mas.shape[1]
    c_E = close[E]
    if E < MAX_OFFSET - 1 or E >= L or not np.isfinite(c_E) or c_E <= 0:
        return None
    log_c_E = np.log(c_E)
    feat = np.full((mas.shape[0], MAX_OFFSET), np.nan, dtype=np.float64)
    for k in range(MAX_OFFSET):
        idx = E - k
        if idx < 0:
            continue
        vals = mas[:, idx]
        ok = np.isfinite(vals) & (vals > 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            feat[ok, k] = np.log(vals[ok]) - log_c_E
    return feat


def collect_examples(setup, universe, ma_cache):
    """Return (n_ex, n_MA, MAX_OFFSET) array of example MA-feature vectors."""
    examples = get_examples(setup)
    feats = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E = vsc.lookup_idx(df, ex["entry_date"])
        if E < 0:
            continue
        close = df["close"].values.astype(np.float64)
        if ex["ticker"] not in ma_cache:
            ma_cache[ex["ticker"]] = compute_mas(close)
        mas = ma_cache[ex["ticker"]]
        f = feature_vec_at(mas, close, E)
        if f is None:
            continue
        feats.append(f)
    return np.array(feats) if feats else None


def sample_market(universe, n_target, rng, ma_cache):
    """Return (n_sampled, n_MA, MAX_OFFSET) market feature array."""
    tickers = list(universe.keys())
    feats = []
    attempts = 0
    max_attempts = n_target * 5
    while len(feats) < n_target and attempts < max_attempts:
        t = tickers[rng.integers(0, len(tickers))]
        df = universe[t]
        close = df["close"].values.astype(np.float64)
        L = len(close)
        if L < max(MA_PERIODS) + MAX_OFFSET:
            attempts += 1
            continue
        ds = vsc.dates_as_str(df)
        # eligible E: has enough history and date cutoff
        eligible_idx = np.where(
            (np.arange(L) >= max(MA_PERIODS) + MAX_OFFSET) & (ds >= DATE_CUTOFF)
        )[0]
        if len(eligible_idx) == 0:
            attempts += 1
            continue
        E = int(eligible_idx[rng.integers(0, len(eligible_idx))])
        if t not in ma_cache:
            ma_cache[t] = compute_mas(close)
        mas = ma_cache[t]
        f = feature_vec_at(mas, close, E)
        if f is None or not np.all(np.isfinite(f)):
            attempts += 1
            continue
        feats.append(f)
        attempts += 1
    return np.array(feats)


def compute_heatmap(ex_feats, mkt_feats):
    """Per-cell reject rate (fraction of market bars outside bounding-box
    corridor [min_ex, max_ex]).
    ex_feats: (n_ex, n_MA, MAX_OFFSET). mkt_feats: (n_mkt, n_MA, MAX_OFFSET).
    Returns (n_MA, MAX_OFFSET) matrix of reject rates in [0, 1].
    """
    with np.errstate(invalid='ignore'):
        lo = np.nanmin(ex_feats, axis=0)  # (n_MA, MAX_OFFSET)
        hi = np.nanmax(ex_feats, axis=0)
    # Market in corridor: mkt >= lo and mkt <= hi
    in_cor = (mkt_feats >= lo[None, :, :]) & (mkt_feats <= hi[None, :, :])
    # NaN market values: treat as not-in-corridor (conservative — rejects them)
    in_cor = in_cor & np.isfinite(mkt_feats)
    reject = 1.0 - in_cor.mean(axis=0)
    return reject


def plot_heatmap(reject, setup, out_path):
    fig, ax = plt.subplots(figsize=(10, 6))
    im = ax.imshow(reject, aspect="auto", cmap="viridis", vmin=0.0, vmax=1.0)
    ax.set_yticks(range(len(MA_TYPES)))
    ax.set_yticklabels(MA_TYPES, fontsize=8)
    ax.set_xticks(range(0, MAX_OFFSET, 5))
    ax.set_xticklabels([f"E-{k}" for k in range(0, MAX_OFFSET, 5)], fontsize=8)
    ax.set_title(f"{setup.upper()} — per-cell single-corridor reject rate (dark=meat, light=fat)", fontsize=11)
    ax.set_xlabel("bar offset from entry")
    ax.set_ylabel("MA type")
    cbar = plt.colorbar(im, ax=ax)
    cbar.set_label("fraction of random post-2020 market bars outside example corridor")
    # Annotate top 5 meat cells
    flat_idx = np.argsort(reject.ravel())[::-1][:5]
    for fi in flat_idx:
        r, c = np.unravel_index(fi, reject.shape)
        ax.plot(c, r, "r*", markersize=14, markeredgecolor="white")
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close(fig)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = np.random.default_rng(SEED)

    print(f"Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print("FAIL: ticker count too low"); sys.exit(1)

    ma_cache = {}
    print(f"Sampling {N_MARKET_SAMPLE:,} market bars...", flush=True)
    t0 = time.time()
    mkt_feats = sample_market(universe, N_MARKET_SAMPLE, rng, ma_cache)
    print(f"  got {len(mkt_feats):,} market vectors ({time.time()-t0:.1f}s)", flush=True)

    results = {}
    for setup in SETUPS:
        print(f"\n=== {setup.upper()}", flush=True)
        ex_feats = collect_examples(setup, universe, ma_cache)
        if ex_feats is None:
            print(f"  no examples with feature vectors; skip", flush=True)
            continue
        print(f"  {len(ex_feats)} example feature matrices  (shape {ex_feats.shape})", flush=True)
        reject = compute_heatmap(ex_feats, mkt_feats)
        print(f"  reject rate per-cell:  min={np.nanmin(reject):.3f}  "
              f"median={np.nanmedian(reject):.3f}  max={np.nanmax(reject):.3f}", flush=True)
        top5 = np.argsort(reject.ravel())[::-1][:5]
        print(f"  top-5 meat cells:", flush=True)
        for fi in top5:
            r, c = np.unravel_index(fi, reject.shape)
            print(f"    {MA_TYPES[r]:>7}  E-{c:<2}  reject={reject[r, c]:.3f}", flush=True)
        # save heatmap png and data
        png_path = os.path.join(OUT_DIR, f"{setup}_heatmap.png")
        plot_heatmap(reject, setup, png_path)
        results[setup] = {
            "n_ex": int(len(ex_feats)),
            "n_mkt": int(len(mkt_feats)),
            "reject_rate_grid": reject.tolist(),
            "MA_types": MA_TYPES,
            "max_offset": MAX_OFFSET,
            "top_meat_cells": [
                {"MA": MA_TYPES[r], "offset": int(c), "reject": float(reject[r, c])}
                for fi in top5 for r, c in [np.unravel_index(fi, reject.shape)]
            ],
        }
        print(f"  saved {png_path}", flush=True)

    with open(os.path.join(OUT_DIR, "heatmap_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
