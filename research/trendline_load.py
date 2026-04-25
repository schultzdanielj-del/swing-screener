"""Load 50-ext daily series for one or more tickers and save a baseline plot.

Purpose: see the raw ext data we're designing the trendline features against,
before overlaying any derivation primitive. Apples-to-apples with Dan's
hand-drawn AAPL / CAR trendline images in the research folder.
"""
import io
import json
import os
import sys

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import zstandard as zstd

CACHE_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\expr_series"
OUT_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research"
EXT50_D_COL = 358  # from manifest: ext_avgc50_adr14


def load_ticker_50ext(ticker):
    path = os.path.join(CACHE_DIR, f"{ticker}.npz")
    with open(path, "rb") as f:
        head = f.read(2)
    with open(path, "rb") as f:
        raw = f.read()
    if head == b"PK":
        npz = np.load(io.BytesIO(raw), allow_pickle=True)
    else:
        npz = np.load(io.BytesIO(zstd.ZstdDecompressor().decompress(raw)), allow_pickle=True)
    data = npz["data"].astype(np.float32)
    dates = npz["dates"]
    ext = data[:, EXT50_D_COL]
    dates_np = np.array([np.datetime64(str(d)[:10]) for d in dates])
    return dates_np, ext


def plot_ext(ticker, dates, ext, out_path):
    fig, ax = plt.subplots(figsize=(14, 4))
    mask = ~np.isnan(ext)
    ax.axhline(0, color="#888", lw=0.7)
    ax.axhline(3, color="#2a7", lw=0.4, ls="--")
    ax.axhline(-3, color="#a72", lw=0.4, ls="--")
    # histogram-style bars
    colors = np.where(ext[mask] >= 0, "#4a9", "#c64")
    ax.bar(dates[mask], ext[mask], width=1.0, color=colors, edgecolor="none")
    ax.set_title(f"{ticker}  50-SMA extension (daily, ADR14-normalized)")
    ax.set_ylabel("ext (ADR units)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_minor_locator(mdates.MonthLocator())
    ax.grid(True, axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def main():
    tickers = sys.argv[1:] or ["AAPL", "SPY", "CAR", "TSLA", "RIVN", "MSFT"]
    for tkr in tickers:
        try:
            dates, ext = load_ticker_50ext(tkr)
        except FileNotFoundError:
            print(f"{tkr}: not in cache — skipping")
            continue
        out = os.path.join(OUT_DIR, f"ext50_raw_{tkr}.png")
        plot_ext(tkr, dates, ext, out)
        nonnan = np.where(~np.isnan(ext))[0]
        print(f"{tkr}: {len(ext)} bars, non-nan {nonnan[0]}..{nonnan[-1]}, "
              f"ext range [{np.nanmin(ext):.2f}, {np.nanmax(ext):.2f}] -> {out}")


if __name__ == "__main__":
    main()
