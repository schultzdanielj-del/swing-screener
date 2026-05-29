"""Disproportionate buying flow vs broad market — per-ticker leading-indicator probe."""
import pickle
import time
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"
OUT_PATH = Path(__file__).parent / "te_swks_uec_dispflow.png"

TICKERS = ["TE", "SWKS", "UEC"]
BARS = 250                # display window
BASELINE_LB = 100         # trailing-anchor lookback (variant C)
BASELINE_GAP = 20         # excludes most recent N bars
SMOOTH_WINDOWS = [5, 10, 20, 65]   # rolling-sum windows for the indicator
CLIP = 5.0                # cap per-ticker normalized contribution to mean

print("Loading cache...")
with open(CACHE_PATH, "rb") as f:
    cache = pickle.load(f)
print(f"  {len(cache)} tickers")

print("Computing per-ticker normalized signed flow...")
t0 = time.time()
norm_series = {}
min_bars = BASELINE_LB + BASELINE_GAP + 1
for ticker, df in cache.items():
    if len(df) < min_bars:
        continue
    open_ = df["open"].astype(float).values
    close = df["close"].astype(float).values
    vol = df["volume"].astype(float).values
    raw = (close - open_) * vol
    raw_abs = np.abs(raw)
    # trailing-anchor baseline: mean of |raw| over [t-LB-GAP, t-GAP-1]
    s_abs = pd.Series(raw_abs)
    baseline = s_abs.shift(BASELINE_GAP).rolling(BASELINE_LB).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        normalized = np.where(baseline > 0, raw / baseline, np.nan)
    normalized = np.clip(normalized, -CLIP, CLIP)
    dates = pd.to_datetime(df["date"])
    norm_series[ticker] = pd.Series(normalized, index=dates, name=ticker)

print(f"  {len(norm_series)} tickers usable, {time.time()-t0:.1f}s")

print("Aligning + computing universe mean per bar...")
t0 = time.time()
big = pd.concat(norm_series.values(), axis=1)
universe_mean = big.mean(axis=1, skipna=True)
print(f"  universe_mean dates: {universe_mean.index.min()} .. {universe_mean.index.max()}, "
      f"{time.time()-t0:.1f}s")

# Plot
plt.rcParams.update({
    "figure.facecolor": "black",
    "axes.facecolor": "black",
    "axes.edgecolor": "#666",
    "axes.labelcolor": "#ddd",
    "xtick.color": "#bbb",
    "ytick.color": "#bbb",
    "text.color": "#ddd",
    "grid.color": "#1a1a1c",
    "font.size": 9,
})

n_rows = 1 + len(SMOOTH_WINDOWS)
height_ratios = [2.2] + [1.0] * len(SMOOTH_WINDOWS)
fig, axes = plt.subplots(n_rows, 3, figsize=(15, 11), sharex="col",
                         gridspec_kw={"height_ratios": height_ratios,
                                      "hspace": 0.10, "wspace": 0.20})

for col, ticker in enumerate(TICKERS):
    df = cache[ticker]
    ticker_norm = norm_series[ticker]
    aligned_uni = universe_mean.reindex(ticker_norm.index)
    disp = ticker_norm - aligned_uni

    tail_idx = ticker_norm.index[-BARS:]
    dates_plot = pd.to_datetime(df["date"]).iloc[-BARS:].values
    close_plot = df["close"].astype(float).iloc[-BARS:].values

    ax_p = axes[0, col]
    ax_p.plot(dates_plot, close_plot, color="white", lw=1.0)
    ax_p.set_title(ticker, color="white", fontsize=12, pad=4)
    ax_p.grid(True, alpha=0.25)
    ax_p.tick_params(labelbottom=False)
    if col == 0:
        ax_p.set_ylabel("Close", color="#ddd")

    for row, w in enumerate(SMOOTH_WINDOWS, start=1):
        disp_smooth = disp.rolling(w).sum()
        disp_vals = disp_smooth.loc[tail_idx].values

        ax_d = axes[row, col]
        ax_d.plot(dates_plot, disp_vals, color="#5fc8ff", lw=1.0)
        ax_d.axhline(0, color="#888", lw=0.7, alpha=0.7)
        valid = ~np.isnan(disp_vals)
        pos_mask = valid & (disp_vals > 0)
        neg_mask = valid & (disp_vals < 0)
        ax_d.fill_between(dates_plot, 0, disp_vals, where=pos_mask,
                          color="#1eff1e", alpha=0.30, interpolate=True)
        ax_d.fill_between(dates_plot, 0, disp_vals, where=neg_mask,
                          color="#ff3030", alpha=0.30, interpolate=True)
        ax_d.grid(True, alpha=0.25)
        if row < n_rows - 1:
            ax_d.tick_params(labelbottom=False)
        else:
            ax_d.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
            ax_d.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
            plt.setp(ax_d.get_xticklabels(), rotation=0, ha="center")
        if col == 0:
            ax_d.set_ylabel(f"{w}d sum", color="#ddd")

fig.suptitle(f"Disproportionate buying flow vs broad market — "
             f"baseline {BASELINE_LB}-bar trailing (gap {BASELINE_GAP}), "
             f"windows {SMOOTH_WINDOWS}, last {BARS} bars",
             color="#ddd", fontsize=10, y=0.99)
plt.savefig(OUT_PATH, facecolor="black", dpi=130, bbox_inches="tight")
plt.close()
print(f"Saved: {OUT_PATH}")
