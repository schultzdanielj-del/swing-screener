"""ADVD eyeball probe: price + ADVD per ticker, single PNG."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

REPO = Path(__file__).resolve().parents[2]
CACHE_PATH = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"
OUT_PATH = Path(__file__).parent / "te_swks_uec_advd.png"

TICKERS = ["TE", "SWKS", "UEC"]
BARS = 250
ADVD_A_WIN = 20      # responsive
ADVD_B_WIN = 252     # 1 year, slow
ADVD_C_LOOKBACK = 100  # trailing-anchor window length
ADVD_C_GAP = 20        # exclude the most recent N bars from the denominator

with open(CACHE_PATH, "rb") as f:
    cache = pickle.load(f)

# Style
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

fig, axes = plt.subplots(2, 3, figsize=(15, 6), sharex="col",
                         gridspec_kw={"height_ratios": [2, 1], "hspace": 0.08, "wspace": 0.18})

for col, ticker in enumerate(TICKERS):
    full = cache[ticker].copy()
    body = full["close"].astype(float) - full["open"].astype(float)
    vol = full["volume"].astype(float)
    close = full["close"].astype(float)
    raw_flow_abs = (body * vol).abs()
    total_dv = close * vol  # total dollar volume per bar

    # Each variant: mean(|body x vol|) / mean(close x vol) x 100  (a percentage)
    # A: short rolling
    advd_a = raw_flow_abs.rolling(ADVD_A_WIN).mean() / total_dv.rolling(ADVD_A_WIN).mean() * 100
    # B: long rolling (1 year)
    advd_b = raw_flow_abs.rolling(ADVD_B_WIN).mean() / total_dv.rolling(ADVD_B_WIN).mean() * 100
    # C: trailing-anchor — mean over [t-LB-GAP, t-GAP-1], excludes the most recent GAP bars
    num_c = raw_flow_abs.shift(ADVD_C_GAP).rolling(ADVD_C_LOOKBACK).mean()
    den_c = total_dv.shift(ADVD_C_GAP).rolling(ADVD_C_LOOKBACK).mean()
    advd_c = num_c / den_c * 100

    df = full.tail(BARS)
    dates = pd.to_datetime(df["date"])
    close = df["close"].astype(float)
    advd_a_tail = advd_a.tail(BARS)
    advd_b_tail = advd_b.tail(BARS)
    advd_c_tail = advd_c.tail(BARS)

    ax_p = axes[0, col]
    ax_p.plot(dates, close, color="white", lw=1.0)
    ax_p.set_title(ticker, color="white", fontsize=12, pad=4)
    ax_p.grid(True, alpha=0.25)
    ax_p.tick_params(labelbottom=False)
    if col == 0:
        ax_p.set_ylabel("Close", color="#ddd")

    ax_a = axes[1, col]
    ax_a.plot(dates, advd_a_tail, color="#888888", lw=0.9, label=f"A: {ADVD_A_WIN}-bar rolling")
    ax_a.plot(dates, advd_b_tail, color="#5fc8ff", lw=1.0, label=f"B: {ADVD_B_WIN}-bar rolling")
    ax_a.plot(dates, advd_c_tail, color="#ffcc00", lw=1.0,
              label=f"C: {ADVD_C_LOOKBACK}-bar trailing, gap {ADVD_C_GAP}")
    ax_a.grid(True, alpha=0.25)
    ax_a.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax_a.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    plt.setp(ax_a.get_xticklabels(), rotation=0, ha="center")
    if col == 0:
        ax_a.set_ylabel("ADVD (%)", color="#ddd")
        ax_a.legend(loc="upper left", fontsize=7, facecolor="#0a0a0a",
                    edgecolor="#333", labelcolor="#ddd")

fig.suptitle("D1 price + ADVD% (mean |body x vol| / mean close x vol), last {} bars".format(BARS),
             color="#ddd", fontsize=10, y=0.98)
plt.savefig(OUT_PATH, facecolor="black", dpi=130, bbox_inches="tight")
plt.close()
print(f"Saved: {OUT_PATH}")
