"""v1 primitive: monotone-decreasing stack of peaks + mirror for troughs.

Goal: visually check whether simple local-max detection + consecutive-peak
lines reproduces Dan's hand-drawn declining-peaks downtrendline and
rising-troughs uptrendline on the 50-ext.

Output: one PNG per ticker, showing raw ext + peak markers + trendline
segments (active in solid, flipped in dashed) + trough mirror.
"""
import io
import os
import sys

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import zstandard as zstd
from scipy.signal import find_peaks

CACHE_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\expr_series"
OUT_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research"
EXT50_D_COL = 358


def load_ticker_50ext(ticker):
    path = os.path.join(CACHE_DIR, f"{ticker}.npz")
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"PK":
        npz = np.load(io.BytesIO(raw), allow_pickle=True)
    else:
        npz = np.load(io.BytesIO(zstd.ZstdDecompressor().decompress(raw)), allow_pickle=True)
    data = npz["data"].astype(np.float32)
    dates = np.array([np.datetime64(str(d)[:10]) for d in npz["dates"]])
    return dates, data[:, EXT50_D_COL]


def find_peaks_with_prom(ext, prominence):
    """Return (idx, val) for local maxes with given prominence."""
    idx, props = find_peaks(ext, prominence=prominence)
    return idx, ext[idx]


def find_troughs_with_prom(ext, prominence):
    idx, props = find_peaks(-ext, prominence=prominence)
    return idx, ext[idx]


def compute_cascade_segments(peak_idx, peak_val, ext, direction="down"):
    """Walk peaks in time order. Emit segments between consecutive peaks that
    continue the cascade (next peak lower than prev for 'down', higher for 'up').
    For each segment, compute the 'break bar' — the first bar after the second
    peak where ext crosses the segment's forward projection.

    Returns list of dicts: {i0, i1, v0, v1, slope, break_bar or None}.
    """
    segments = []
    for k in range(len(peak_idx) - 1):
        i0, i1 = peak_idx[k], peak_idx[k + 1]
        v0, v1 = peak_val[k], peak_val[k + 1]
        if direction == "down" and v1 >= v0:
            continue  # not a declining step
        if direction == "up" and v1 <= v0:
            continue
        slope = (v1 - v0) / (i1 - i0)
        # forward projection: find first bar j > i1 where ext crosses the line
        break_bar = None
        for j in range(i1 + 1, len(ext)):
            proj = v0 + slope * (j - i0)
            if direction == "down" and ext[j] > proj:
                break_bar = j
                break
            if direction == "up" and ext[j] < proj:
                break_bar = j
                break
        segments.append({
            "i0": i0, "i1": i1, "v0": float(v0), "v1": float(v1),
            "slope": float(slope), "break_bar": break_bar,
        })
    return segments


def plot_with_primitive(ticker, dates, ext, prominence, out_path):
    peak_idx, peak_val = find_peaks_with_prom(ext, prominence)
    tro_idx, tro_val = find_troughs_with_prom(ext, prominence)

    down_segs = compute_cascade_segments(peak_idx, peak_val, ext, "down")
    up_segs = compute_cascade_segments(tro_idx, tro_val, ext, "up")

    fig, ax = plt.subplots(figsize=(18, 5))
    mask = ~np.isnan(ext)
    colors = np.where(ext[mask] >= 0, "#4a9", "#c64")
    ax.bar(dates[mask], ext[mask], width=1.0, color=colors, edgecolor="none", alpha=0.55)
    ax.axhline(0, color="#333", lw=0.8)

    # peak / trough markers
    ax.scatter(dates[peak_idx], peak_val, marker="v", color="#063", s=24, zorder=5, label=f"peaks (prom≥{prominence})")
    ax.scatter(dates[tro_idx], tro_val, marker="^", color="#630", s=24, zorder=5, label=f"troughs (prom≥{prominence})")

    # segments — active portion solid, post-break projection dashed
    for seg in down_segs:
        x0, x1 = dates[seg["i0"]], dates[seg["i1"]]
        ax.plot([x0, x1], [seg["v0"], seg["v1"]], color="#800", lw=1.3, zorder=4)
        if seg["break_bar"] is not None:
            j = seg["break_bar"]
            proj_val = seg["v0"] + seg["slope"] * (j - seg["i0"])
            ax.plot([dates[seg["i1"]], dates[j]], [seg["v1"], proj_val],
                    color="#800", lw=1.0, ls=":", alpha=0.6, zorder=3)
            ax.scatter(dates[j], ext[j], marker="x", color="#c00", s=40, zorder=6)
    for seg in up_segs:
        x0, x1 = dates[seg["i0"]], dates[seg["i1"]]
        ax.plot([x0, x1], [seg["v0"], seg["v1"]], color="#008", lw=1.3, zorder=4)
        if seg["break_bar"] is not None:
            j = seg["break_bar"]
            proj_val = seg["v0"] + seg["slope"] * (j - seg["i0"])
            ax.plot([dates[seg["i1"]], dates[j]], [seg["v1"], proj_val],
                    color="#008", lw=1.0, ls=":", alpha=0.6, zorder=3)
            ax.scatter(dates[j], ext[j], marker="x", color="#00c", s=40, zorder=6)

    ax.set_title(f"{ticker}  50-ext + v1 primitive (local-max prominence ≥ {prominence} ADR)")
    ax.set_ylabel("ext (ADR units)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_minor_locator(mdates.MonthLocator())
    ax.grid(True, axis="y", alpha=0.2)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return len(peak_idx), len(tro_idx), len(down_segs), len(up_segs)


def main():
    tickers = sys.argv[1:] or ["AAPL", "CAR", "SPY", "TSLA"]
    for tkr in tickers:
        try:
            dates, ext = load_ticker_50ext(tkr)
        except FileNotFoundError:
            print(f"{tkr}: not in cache")
            continue
        for prom in (0.5, 1.0, 1.5):
            out = os.path.join(OUT_DIR, f"ext50_v1_{tkr}_prom{prom:.1f}.png")
            n_pk, n_tr, n_ds, n_us = plot_with_primitive(tkr, dates, ext, prom, out)
            print(f"{tkr} prom={prom}: peaks={n_pk} troughs={n_tr} down_segs={n_ds} up_segs={n_us} -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
