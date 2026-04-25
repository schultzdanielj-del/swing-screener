"""Visual rendering of the lead-up envelope analysis.

Produces per-setup PNG files showing:
  Panel 1 (top):    all example lead-ups overlaid, envelope band shaded,
                    subset of random market bars shown for contrast.
  Panel 2 (middle): example envelope range vs offset, with the kneedle elbow
                    marking N_divergence.
  Panel 3 (bottom): cumulative-AND random pass rate vs offset (how fast random
                    gets rejected as offsets are added to the strict AND filter).

Reads OHLCV + examples fresh, mirrors the math in research/lead_up_investigation.py.
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import time
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "lead_up_investigation", "charts")

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
K_MAX = 120
ATR_PERIOD = 14
RANDOM_SEED = 42
AXES = ["log_close", "log_high", "log_low", "atr_close", "atr_high", "atr_low"]


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


def compute_atr(high, low, close, idx, period=ATR_PERIOD):
    if idx < period:
        return np.nan
    h = high[idx - period + 1:idx + 1]
    l = low[idx - period + 1:idx + 1]
    c_prev = close[idx - period:idx]
    tr = np.maximum(h - l, np.maximum(np.abs(h - c_prev), np.abs(l - c_prev)))
    if not np.all(np.isfinite(tr)):
        return np.nan
    return float(tr.mean())


def extract_window(df, E_idx, k_max=K_MAX):
    if E_idx < k_max + ATR_PERIOD:
        return None
    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    close_E = close[E_idx]
    if not np.isfinite(close_E) or close_E <= 0:
        return None
    atr_E = compute_atr(high, low, close, E_idx, ATR_PERIOD)
    if not np.isfinite(atr_E) or atr_E <= 0:
        return None
    offsets = np.arange(k_max + 1)
    idx = E_idx - offsets
    closes_k = close[idx]
    highs_k = high[idx]
    lows_k = low[idx]
    if not np.all(np.isfinite(closes_k)) or np.any(closes_k <= 0):
        return None
    if not np.all(np.isfinite(highs_k)) or not np.all(np.isfinite(lows_k)):
        return None
    with np.errstate(all='ignore'):
        return {
            "log_close": np.log(closes_k / close_E),
            "log_high":  np.log(highs_k / close_E),
            "log_low":   np.log(lows_k / close_E),
            "atr_close": (closes_k - close_E) / atr_E,
            "atr_high":  (highs_k - close_E) / atr_E,
            "atr_low":   (lows_k - close_E) / atr_E,
        }


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def kneedle_elbow(values):
    v = np.array(values, dtype=np.float64)
    n = len(v)
    if n < 3:
        return 0
    x = np.arange(n) / (n - 1)
    vmax = float(np.max(v)) if np.max(v) > 0 else 1.0
    vmin = float(np.min(v))
    if vmax == vmin:
        return 0
    y = (v - vmin) / (vmax - vmin)
    dist = y - x
    return int(np.argmax(dist))


def render_setup(setup, ex_windows):
    """Single-panel overlay: all examples drawn on one chart, entry anchored at right,
    120-bar lookback going left. Example envelope shaded. N cutoff marked."""
    if len(ex_windows) < 3:
        return None

    ex_close = np.vstack([w["log_close"] for w in ex_windows])      # (n_ex, K+1)
    ex_high = np.vstack([w["log_high"] for w in ex_windows])
    ex_low = np.vstack([w["log_low"] for w in ex_windows])
    ex_atr_close = np.vstack([w["atr_close"] for w in ex_windows])

    n_ex = ex_close.shape[0]
    offsets = np.arange(K_MAX + 1)

    close_min = ex_close.min(axis=0)
    close_max = ex_close.max(axis=0)
    high_min = ex_high.min(axis=0)
    high_max = ex_high.max(axis=0)
    low_min = ex_low.min(axis=0)
    low_max = ex_low.max(axis=0)

    # N via kneedle elbow on combined example range trajectory
    close_range = close_max - close_min
    high_range = high_max - high_min
    low_range = low_max - low_min
    atr_close_range = ex_atr_close.max(axis=0) - ex_atr_close.min(axis=0)

    def normalize(a):
        a = np.array(a, dtype=np.float64)
        m = float(a.max()) if a.max() > 0 else 1.0
        return a / m
    combined = (normalize(close_range) + normalize(high_range)
                + normalize(low_range) + normalize(atr_close_range)) / 4.0
    N_div = kneedle_elbow(combined.tolist())

    # X-axis: entry at right (x=0), lookback going left (x=-120)
    xs = -offsets

    fig, ax = plt.subplots(1, 1, figsize=(16, 8))
    fig.suptitle(
        f"{setup.upper()}  —  all {n_ex} example lead-ups overlaid, anchored at entry bar  "
        f"(120-bar lookback, N_cutoff = {N_div})",
        fontsize=14
    )

    # Every example's close trajectory
    for i in range(n_ex):
        ax.plot(xs, ex_close[i], color='steelblue', linewidth=0.9, alpha=0.55, zorder=2)

    # Envelope shaded band (min/max of close across examples at each offset)
    ax.fill_between(xs, close_min, close_max, color='steelblue', alpha=0.12, zorder=1,
                    label="example envelope [min, max]")
    ax.plot(xs, close_min, color='steelblue', linewidth=1.4, linestyle='--', alpha=0.7, zorder=3)
    ax.plot(xs, close_max, color='steelblue', linewidth=1.4, linestyle='--', alpha=0.7, zorder=3)

    # N cutoff
    ax.axvline(x=-N_div, color='crimson', linewidth=2.0, linestyle='-',
               label=f"N cutoff = {N_div}", zorder=5)

    # Entry bar reference (x=0)
    ax.axvline(x=0, color='black', linewidth=1.0, alpha=0.8, zorder=4)
    ax.axhline(y=0, color='black', linewidth=0.6, alpha=0.5, zorder=0)

    ax.set_xlabel("Bars relative to entry bar (0 = entry, -k = k bars before entry)", fontsize=11)
    ax.set_ylabel("log(close / close_E)   (each example normalized to its own entry close)",
                  fontsize=11)
    ax.set_xlim(-K_MAX - 2, 4)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='lower left', fontsize=11)

    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, f"{setup}_lead_up.png")
    plt.savefig(out_path, dpi=140, bbox_inches='tight')
    plt.close(fig)
    print(f"  wrote {out_path}  (N={N_div})", flush=True)
    return {"setup": setup, "n_ex": n_ex, "N_divergence": N_div}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe tickers: {len(universe)}", flush=True)
    if len(universe) < 11000:
        print("FAIL: OHLCV ticker count too low", flush=True)
        return

    for setup in SETUP_ORDER:
        print(f"\n=== {setup.upper()} ===", flush=True)
        examples = get_examples(setup)
        ex_windows = []
        for ex in examples:
            df = universe.get(ex["ticker"])
            if df is None:
                continue
            E_idx = lookup_idx(df, ex["entry_date"])
            if E_idx < 0:
                continue
            w = extract_window(df, E_idx, K_MAX)
            if w is not None:
                ex_windows.append(w)
        print(f"  valid example windows: {len(ex_windows)}", flush=True)
        render_setup(setup, ex_windows)

    print("\nDONE.")


if __name__ == "__main__":
    main()
