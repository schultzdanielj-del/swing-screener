"""v2 primitive: monotone-decreasing stack of peaks (and ascending stack of
troughs), drawn over zoomed windows matching Dan's hand-drawn reference
images.

Stack semantics:
  - For every new peak in time order, pop all stack members with value <=
    the new peak (they are superseded). Push the new peak.
  - At any bar t, stack holds the current global-declining cascade seen so
    far. Adjacent pairs in the stack are the active downtrendline segments.
  - Popped peaks define LINES that once existed and have been broken (the
    new peak rose above the downtrendline from the previous peak through the
    popped peak). Those broken lines flip polarity per TA 101 — rendered as
    dashed blue (for a popped-down line now acting as support).
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


def monotone_stack(peak_idx, peak_val, direction="down"):
    """Walk peaks left-to-right.

    - 'down': stack is monotone strictly-decreasing. When a new peak >= top,
      pop until new peak < top, then push. The popped peaks define
      'broken' segments (the downtrendline from the survivor through the
      popped peak was violated by the new higher peak).
    - 'up': mirror for troughs — stack strictly-ascending.

    Returns:
      segments_live: list of dicts {i0,i1,v0,v1} for currently-active chain
        segments (connections between adjacent stack members as of end of
        data).
      segments_broken: list of dicts {i0,i1,v0,v1,break_bar,break_val} for
        lines that were connections at some point but got superseded.
    """
    stack = []  # list of (idx, val)
    broken = []
    for i, v in zip(peak_idx, peak_val):
        if direction == "down":
            while stack and stack[-1][1] <= v:
                # stack[-1] gets popped. The line that was most recently
                # drawn TO stack[-1] from stack[-2] is now broken at bar i.
                popped_idx, popped_val = stack.pop()
                if stack:
                    survivor_idx, survivor_val = stack[-1]
                    broken.append({
                        "i0": survivor_idx, "i1": popped_idx,
                        "v0": float(survivor_val), "v1": float(popped_val),
                        "break_bar": int(i), "break_val": float(v),
                    })
            stack.append((int(i), float(v)))
        else:  # up (troughs)
            while stack and stack[-1][1] >= v:
                popped_idx, popped_val = stack.pop()
                if stack:
                    survivor_idx, survivor_val = stack[-1]
                    broken.append({
                        "i0": survivor_idx, "i1": popped_idx,
                        "v0": float(survivor_val), "v1": float(popped_val),
                        "break_bar": int(i), "break_val": float(v),
                    })
            stack.append((int(i), float(v)))

    # live segments = adjacent pairs in final stack
    live = []
    for k in range(len(stack) - 1):
        live.append({
            "i0": stack[k][0], "i1": stack[k + 1][0],
            "v0": stack[k][1], "v1": stack[k + 1][1],
        })
    return live, broken, stack


def plot_window(ticker, dates, ext, prominence, date_start, date_end, out_path, title_suffix=""):
    # build peaks and troughs over ALL data (so cascade context is correct)
    peaks_i, _ = find_peaks(ext, prominence=prominence)
    troughs_i, _ = find_peaks(-ext, prominence=prominence)
    peaks_v = ext[peaks_i]
    troughs_v = ext[troughs_i]

    down_live, down_broken, _ = monotone_stack(peaks_i, peaks_v, "down")
    up_live, up_broken, _ = monotone_stack(troughs_i, troughs_v, "up")

    # window mask
    start = np.datetime64(date_start) if date_start else dates[0]
    end = np.datetime64(date_end) if date_end else dates[-1]
    win_mask = (dates >= start) & (dates <= end)

    fig, ax = plt.subplots(figsize=(16, 5.5))
    visible_ext = ext[win_mask]
    visible_dates = dates[win_mask]
    mask_nn = ~np.isnan(visible_ext)
    colors = np.where(visible_ext[mask_nn] >= 0, "#4a9", "#c64")
    ax.bar(visible_dates[mask_nn], visible_ext[mask_nn], width=1.0, color=colors, edgecolor="none", alpha=0.55)
    ax.axhline(0, color="#333", lw=0.8)

    def in_win(i):
        return start <= dates[i] <= end

    # peak / trough markers (only those in window)
    pk_in = np.array([i for i in peaks_i if in_win(i)])
    tr_in = np.array([i for i in troughs_i if in_win(i)])
    if len(pk_in):
        ax.scatter(dates[pk_in], ext[pk_in], marker="v", color="#063", s=28, zorder=5)
    if len(tr_in):
        ax.scatter(dates[tr_in], ext[tr_in], marker="^", color="#630", s=28, zorder=5)

    # LIVE downtrend segments (red solid)
    for seg in down_live:
        if dates[seg["i1"]] < start or dates[seg["i0"]] > end:
            continue
        ax.plot([dates[seg["i0"]], dates[seg["i1"]]], [seg["v0"], seg["v1"]],
                color="#a00", lw=1.6, zorder=4)

    # BROKEN downtrend segments (red dashed, shown as their line extended to break bar)
    for seg in down_broken:
        if dates[seg["i1"]] < start or dates[seg["i0"]] > end:
            continue
        # solid between survivor and popped peak...
        ax.plot([dates[seg["i0"]], dates[seg["i1"]]], [seg["v0"], seg["v1"]],
                color="#a00", lw=1.0, ls="--", alpha=0.55, zorder=3)
        # ...and projection forward to the break bar
        bb = seg["break_bar"]
        if start <= dates[bb] <= end:
            slope = (seg["v1"] - seg["v0"]) / (seg["i1"] - seg["i0"])
            proj = seg["v0"] + slope * (bb - seg["i0"])
            ax.plot([dates[seg["i1"]], dates[bb]], [seg["v1"], proj],
                    color="#a00", lw=0.8, ls=":", alpha=0.4, zorder=2)
            ax.scatter(dates[bb], seg["break_val"], marker="x", color="#c00", s=45, zorder=6)

    # LIVE uptrend segments (blue solid)
    for seg in up_live:
        if dates[seg["i1"]] < start or dates[seg["i0"]] > end:
            continue
        ax.plot([dates[seg["i0"]], dates[seg["i1"]]], [seg["v0"], seg["v1"]],
                color="#00a", lw=1.6, zorder=4)

    # BROKEN uptrend segments (blue dashed)
    for seg in up_broken:
        if dates[seg["i1"]] < start or dates[seg["i0"]] > end:
            continue
        ax.plot([dates[seg["i0"]], dates[seg["i1"]]], [seg["v0"], seg["v1"]],
                color="#00a", lw=1.0, ls="--", alpha=0.55, zorder=3)
        bb = seg["break_bar"]
        if start <= dates[bb] <= end:
            slope = (seg["v1"] - seg["v0"]) / (seg["i1"] - seg["i0"])
            proj = seg["v0"] + slope * (bb - seg["i0"])
            ax.plot([dates[seg["i1"]], dates[bb]], [seg["v1"], proj],
                    color="#00a", lw=0.8, ls=":", alpha=0.4, zorder=2)
            ax.scatter(dates[bb], seg["break_val"], marker="x", color="#00c", s=45, zorder=6)

    ax.set_xlim(start, end)
    title = f"{ticker} 50-ext — v2 stack primitive (prom ≥ {prominence} ADR)"
    if title_suffix:
        title += f" — {title_suffix}"
    ax.set_title(title)
    ax.set_ylabel("ext (ADR)")
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main():
    # (ticker, date_start, date_end, tag) — windows chosen to match Dan's images
    jobs = [
        # AAPL — Dan's image is roughly Sep 2025 through Jul 2026
        ("AAPL", "2025-08-01", "2026-05-01", "dan_image_window"),
        # AAPL full history for cascade context
        ("AAPL", None, None, "full_history"),
        # CAR — feature 2 momo image is Feb-May 2026
        ("CAR", "2026-01-15", "2026-05-15", "dan_image_window"),
        ("CAR", None, None, "full_history"),
        # SPY for the stage-cycle feature 1 behavior
        ("SPY", "2025-08-01", "2026-05-01", "recent_chop"),
        ("SPY", None, None, "full_history"),
    ]
    for tkr, ds, de, tag in jobs:
        try:
            dates, ext = load_ticker_50ext(tkr)
        except FileNotFoundError:
            print(f"{tkr}: not in cache")
            continue
        for prom in (1.0, 1.5):
            out = os.path.join(
                OUT_DIR,
                f"ext50_v2_{tkr}_{tag}_prom{prom:.1f}.png",
            )
            plot_window(tkr, dates, ext, prom, ds, de, out, tag)
            print(f"{tkr} {tag} prom={prom} -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
