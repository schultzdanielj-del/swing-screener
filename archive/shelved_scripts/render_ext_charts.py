"""
Render Extension Charts
=======================

For each example in each setup, produce a multi-panel PNG showing:
  Top panel:    price + MA stack (8/21/50/200 EMAs) with signal/exit/MFE bars marked.
  Middle panel: extension lines (ext_avgc8_adr14, ext_avgc21_adr14, ext_avgc50_adr14, ext_avgc200_adr14).
  Bottom panel: histogram of ext_avgc50_adr14 over pre-signal context window.

Windows:
  - Pre-signal context: 504 bars (~2 years daily)
  - Post-signal: 120 bars (matches grinder MAX_FORWARD)

Reads:
  - OHLCV from main (SCANPERFECT_READ_ROOT)
  - Expression cache from worktree (sandboxed)
  - Latest signal_exit_{setup}.json for exit bar per example

Writes:
  - data/diagnostics/charts/{setup}/{ticker}_{signal_date}.png

No cache writes. No network. Pure diagnostic renderer.
"""

import json
import os
import pickle
import sqlite3
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Use the project's ExprSeriesCache for proper .npz loading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner"))
from expr_cache_builder import ExprSeriesCache

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READ_ROOT = os.environ.get("SCANPERFECT_READ_ROOT", REPO_ROOT)
CACHE_DIR = os.environ.get(
    "SCANPERFECT_CACHE_DIR", os.path.join(REPO_ROOT, "local_runner", "cache")
)

OHLCV_PATH = os.path.join(READ_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
DB_PATH = os.path.join(READ_ROOT, "data", "scanperfect.db")
EXPR_DIR = os.path.join(CACHE_DIR, "expr_series")
GRIND_DIR = os.path.join(REPO_ROOT, "data", "signal_exit_grind")
OUT_DIR = os.path.join(REPO_ROOT, "data", "diagnostics", "charts")
os.makedirs(OUT_DIR, exist_ok=True)

SETUPS = ["htf", "bf", "base", "dtss"]

# Extension columns to plot (must exist in expression cache)
EXT_COLS = [
    ("ext_xavgc8_adr14", "#d62728"),
    ("ext_xavgc21_adr14", "#ff7f0e"),
    ("ext_avgc50_adr14", "#1f77b4"),
    ("ext_avgc200_adr14", "#2ca02c"),
]
HIST_COL = "ext_avgc50_adr14"

PRE_BARS = 504
POST_BARS = 120


def load_expr_manifest():
    path = os.path.join(EXPR_DIR, "_manifest.json")
    with open(path) as f:
        return json.load(f)


_EXPR_CACHE = None


def _get_expr_cache():
    global _EXPR_CACHE
    if _EXPR_CACHE is None:
        _EXPR_CACHE = ExprSeriesCache()
    return _EXPR_CACHE


def load_expr_cache_for_ticker(ticker):
    cache = _get_expr_cache()
    try:
        dates, data = cache.get_ticker(ticker)
    except Exception:
        return None, None
    if dates is None:
        return None, None
    dates_str = [str(d)[:10] for d in dates]
    return dates_str, data


def get_direction(setup_type):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    return row[0]


def render_one(setup, direction, ex, exit_bar, capture_eff, ohlcv, expr_meta, out_path):
    ticker = ex["ticker"]
    signal_date = ex["signal_date"]

    df = ohlcv.get(ticker)
    if df is None:
        return False
    dates_all = [str(d)[:10] for d in df["date"].values]
    if signal_date not in dates_all:
        return False
    scan_idx = dates_all.index(signal_date)

    # OHLCV windows
    start_ctx = max(0, scan_idx - PRE_BARS)
    end_post = min(len(df), scan_idx + POST_BARS + 1)
    hist_win = df.iloc[start_ctx : scan_idx + 1]
    post_win = df.iloc[scan_idx : end_post]

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values

    # MFE bar (post-signal favorable extreme)
    if direction == "short":
        fwd = df["low"].values[scan_idx + 1 : end_post]
        favorable = close[scan_idx] - fwd
    else:
        fwd = df["high"].values[scan_idx + 1 : end_post]
        favorable = fwd - close[scan_idx]
    mfe_bar = int(np.argmax(favorable)) + 1 if len(favorable) else None

    # Extension values from expression cache
    expr_dates, expr_data = load_expr_cache_for_ticker(ticker)
    if expr_dates is None or signal_date not in expr_dates:
        return False
    expr_scan_idx = expr_dates.index(signal_date)
    expr_start = max(0, expr_scan_idx - PRE_BARS)
    expr_end = min(len(expr_dates), expr_scan_idx + POST_BARS + 1)

    expressions = expr_meta.get("expr_names") or expr_meta.get("expressions") or []
    col_idx = {name: i for i, name in enumerate(expressions)}

    # Align OHLCV x-axis to expression cache dates (use expression cache as canonical)
    x_dates = expr_dates[expr_start:expr_end]
    n_x = len(x_dates)
    # Map signal bar inside window
    sig_rel = expr_scan_idx - expr_start

    # Price series pulled from OHLCV aligned to x_dates
    x_closes = np.full(n_x, np.nan)
    x_highs = np.full(n_x, np.nan)
    x_lows = np.full(n_x, np.nan)
    ohlcv_date_to_idx = {d: i for i, d in enumerate(dates_all)}
    for i, d in enumerate(x_dates):
        j = ohlcv_date_to_idx.get(d)
        if j is not None:
            x_closes[i] = close[j]
            x_highs[i] = high[j]
            x_lows[i] = low[j]

    # MAs (compute from aligned closes — simple, cheap)
    def ema(arr, period):
        alpha = 2 / (period + 1)
        out = np.empty_like(arr)
        out[:] = np.nan
        first = np.nan
        for i in range(len(arr)):
            v = arr[i]
            if np.isnan(v):
                continue
            if np.isnan(first):
                first = v
                out[i] = v
            else:
                out[i] = alpha * v + (1 - alpha) * out[i - 1] if not np.isnan(out[i - 1]) else v
        return out

    def sma(arr, period):
        s = pd.Series(arr)
        return s.rolling(period, min_periods=1).mean().values

    ema8 = ema(x_closes, 8)
    ema21 = ema(x_closes, 21)
    sma50 = sma(x_closes, 50)
    sma200 = sma(x_closes, 200)

    # Extension series aligned to x_dates
    ext_series = {}
    for col_name, _color in EXT_COLS:
        ci = col_idx.get(col_name)
        if ci is None:
            continue
        s = expr_data[expr_start:expr_end, ci]
        ext_series[col_name] = s

    # Histogram data: pre-signal values of HIST_COL
    hist_col_idx = col_idx.get(HIST_COL)
    hist_values = None
    if hist_col_idx is not None:
        pre_end = sig_rel + 1
        pre_ext = expr_data[expr_start : expr_start + pre_end, hist_col_idx]
        hist_values = pre_ext[~np.isnan(pre_ext)]

    # -------- PLOT --------
    fig = plt.figure(figsize=(16, 10))
    gs = fig.add_gridspec(3, 4, height_ratios=[1.2, 1.0, 0.8])
    ax_price = fig.add_subplot(gs[0, :])
    ax_ext = fig.add_subplot(gs[1, :], sharex=ax_price)
    ax_hist = fig.add_subplot(gs[2, 0])
    ax_ext_post = fig.add_subplot(gs[2, 1:])

    x = np.arange(n_x)

    # Price panel
    ax_price.plot(x, x_closes, color="black", lw=0.8, label="close")
    ax_price.plot(x, ema8, color="#d62728", lw=0.7, label="8EMA")
    ax_price.plot(x, ema21, color="#ff7f0e", lw=0.7, label="21EMA")
    ax_price.plot(x, sma50, color="#1f77b4", lw=0.7, label="50SMA")
    ax_price.plot(x, sma200, color="#2ca02c", lw=0.7, label="200SMA")
    ax_price.axvline(sig_rel, color="black", ls="--", lw=1.0, label="signal")
    if exit_bar is not None:
        exit_abs = sig_rel + int(exit_bar)
        if 0 <= exit_abs < n_x:
            ax_price.axvline(exit_abs, color="red", ls=":", lw=1.0, label=f"exit@{exit_bar}")
    if mfe_bar is not None:
        mfe_abs = sig_rel + mfe_bar
        if 0 <= mfe_abs < n_x:
            ax_price.axvline(mfe_abs, color="green", ls=":", lw=1.0, label=f"MFE@{mfe_bar}")
    title = (
        f"{setup.upper()} {ticker} signal={signal_date}  dir={direction}  "
        f"mfe_adr={ex['mfe_adr']}  capture_eff={capture_eff:.2f}"
        if capture_eff is not None
        else f"{setup.upper()} {ticker} signal={signal_date}"
    )
    ax_price.set_title(title, fontsize=10)
    ax_price.legend(loc="upper left", fontsize=7, ncol=6)
    ax_price.grid(alpha=0.2)

    # Extension panel
    for col_name, color in EXT_COLS:
        if col_name in ext_series:
            ax_ext.plot(x, ext_series[col_name], color=color, lw=0.7, label=col_name.replace("ext_", ""))
    ax_ext.axhline(0, color="black", lw=0.4)
    ax_ext.axvline(sig_rel, color="black", ls="--", lw=1.0)
    if exit_bar is not None:
        exit_abs = sig_rel + int(exit_bar)
        if 0 <= exit_abs < n_x:
            ax_ext.axvline(exit_abs, color="red", ls=":", lw=1.0)
    if mfe_bar is not None:
        mfe_abs = sig_rel + mfe_bar
        if 0 <= mfe_abs < n_x:
            ax_ext.axvline(mfe_abs, color="green", ls=":", lw=1.0)
    ax_ext.set_ylabel("ext (ADR)")
    ax_ext.legend(loc="upper left", fontsize=7, ncol=4)
    ax_ext.grid(alpha=0.2)

    # Histogram panel (pre-signal ext_avgc50_adr14)
    if hist_values is not None and len(hist_values):
        ax_hist.hist(hist_values, bins=50, color="#1f77b4", alpha=0.7)
        ax_hist.axvline(
            ext_series[HIST_COL][sig_rel] if HIST_COL in ext_series else 0,
            color="black",
            ls="--",
            lw=1.0,
            label="at signal",
        )
        p90 = float(np.percentile(hist_values, 90))
        p95 = float(np.percentile(hist_values, 95))
        ax_hist.axvline(p90, color="orange", ls=":", lw=0.8, label=f"p90={p90:.1f}")
        ax_hist.axvline(p95, color="red", ls=":", lw=0.8, label=f"p95={p95:.1f}")
        ax_hist.set_title(f"pre-signal {HIST_COL} distribution", fontsize=8)
        ax_hist.legend(loc="upper right", fontsize=6)
        ax_hist.grid(alpha=0.2)

    # Post-signal ext zoom
    post_start = sig_rel
    post_end = min(n_x, sig_rel + POST_BARS + 1)
    for col_name, color in EXT_COLS:
        if col_name in ext_series:
            ax_ext_post.plot(
                np.arange(post_start, post_end) - sig_rel,
                ext_series[col_name][post_start:post_end],
                color=color,
                lw=0.9,
                label=col_name.replace("ext_", ""),
            )
    ax_ext_post.axhline(0, color="black", lw=0.4)
    ax_ext_post.axvline(0, color="black", ls="--", lw=1.0, label="signal")
    if exit_bar is not None:
        ax_ext_post.axvline(int(exit_bar), color="red", ls=":", lw=1.0, label=f"exit@{exit_bar}")
    if mfe_bar is not None:
        ax_ext_post.axvline(mfe_bar, color="green", ls=":", lw=1.0, label=f"MFE@{mfe_bar}")
    ax_ext_post.set_title("post-signal ext (bars from signal)", fontsize=8)
    ax_ext_post.legend(loc="upper left", fontsize=6, ncol=3)
    ax_ext_post.grid(alpha=0.2)

    plt.tight_layout()
    fig.savefig(out_path, dpi=85)
    plt.close(fig)
    return True


def run_setup(setup, ohlcv, expr_meta):
    print(f"\n=== {setup.upper()} ===")
    direction = get_direction(setup)
    with open(os.path.join(GRIND_DIR, f"signal_exit_{setup}.json")) as f:
        grind = json.load(f)
    examples = grind["examples"]
    pick0 = grind["or_exit_set"][0]
    exit_bars = pick0["per_example_exit_bars"]
    effs = pick0["per_example_efficiency"]

    setup_dir = os.path.join(OUT_DIR, setup)
    os.makedirs(setup_dir, exist_ok=True)

    n_ok = 0
    for i, ex in enumerate(examples):
        ticker = ex["ticker"]
        signal_date = ex["signal_date"]
        fname = f"{ticker}_{signal_date}.png"
        out_path = os.path.join(setup_dir, fname)
        ok = render_one(
            setup, direction, ex, exit_bars[i], effs[i], ohlcv, expr_meta, out_path
        )
        if ok:
            n_ok += 1
    print(f"  rendered {n_ok}/{len(examples)} charts into {setup_dir}")


def main():
    print(f"Loading OHLCV from {OHLCV_PATH}")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    expr_meta = load_expr_manifest()
    for setup in SETUPS:
        run_setup(setup, ohlcv, expr_meta)
    print("\nDone.")


if __name__ == "__main__":
    main()
