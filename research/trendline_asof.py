"""Viz: at any chosen 'as-of' bar, render only the trendlines that would be
in the cache at that bar. Max 4 lines: active upper, flipped upper, active
lower, flipped lower. No historical clutter.

Uses v3 state-machine math. Multiple as-of cuts per ticker lets Dan check
whether the lines drawn at each moment match what he'd draw.
"""
import io
import os

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import zstandard as zstd
from scipy.signal import find_peaks

from trendline_primitive_v3 import (
    run_upper_state_machine,
    run_lower_state_machine,
    line_projection,
)

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


def state_as_of(ext, asof_bar, prominence=1.0):
    """Return the active+flipped lines as of bar `asof_bar`.
    Feed the state machine only bars [0..asof_bar], filter peaks/troughs to
    that range, run the machine, report the end state."""
    sub = ext[: asof_bar + 1]
    peak_idx, _ = find_peaks(sub, prominence=prominence)
    pos_peaks = [(i, sub[i]) for i in peak_idx if sub[i] > 0]
    trough_idx, _ = find_peaks(-sub, prominence=prominence)
    neg_troughs = [(i, sub[i]) for i in trough_idx if sub[i] < 0]

    pi = np.array([p[0] for p in pos_peaks], dtype=int) if pos_peaks else np.array([], dtype=int)
    pv = np.array([p[1] for p in pos_peaks]) if pos_peaks else np.array([])
    ti = np.array([t[0] for t in neg_troughs], dtype=int) if neg_troughs else np.array([], dtype=int)
    tv = np.array([t[1] for t in neg_troughs]) if neg_troughs else np.array([])

    up_active, up_broken = run_upper_state_machine(sub, pi, pv)
    lo_active, lo_broken = run_lower_state_machine(sub, ti, tv)
    up_flipped = up_broken[-1] if up_broken else None
    lo_flipped = lo_broken[-1] if lo_broken else None
    return up_active, up_flipped, lo_active, lo_flipped


def draw_asof_panel(ax, dates, ext, asof_bar, prom, window_bars=260):
    start_bar = max(0, asof_bar - window_bars)
    up_active, up_flipped, lo_active, lo_flipped = state_as_of(ext, asof_bar, prom)

    # histogram slice
    vis_ext = ext[start_bar : asof_bar + 1]
    vis_dates = dates[start_bar : asof_bar + 1]
    mask_nn = ~np.isnan(vis_ext)
    colors = np.where(vis_ext[mask_nn] >= 0, "#4a9", "#c64")
    ax.bar(vis_dates[mask_nn], vis_ext[mask_nn], width=1.0, color=colors, edgecolor="none", alpha=0.55)
    ax.axhline(0, color="#333", lw=0.8)

    def draw_line(seg, color, ls, lw, label):
        if seg is None:
            return
        if len(seg) == 4:
            i0, v0, i1, v1 = seg
        else:
            i0, v0, i1, v1 = seg[:4]
        if i0 < start_bar and i1 < start_bar:
            return
        # anchor segment
        ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, ls=ls, zorder=4, label=label)
        # extend projection forward to asof_bar
        if asof_bar > i1:
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            ax.plot([dates[i1], dates[asof_bar]], [v1, proj], color=color, lw=lw, ls=":",
                    alpha=0.7, zorder=3)
        # anchor markers
        ax.scatter([dates[i0], dates[i1]], [v0, v1], color=color, s=36, zorder=6, edgecolor="#000", lw=0.5)

    draw_line(up_active, "#a00", "-", 2.0, "active upper (resistance)")
    draw_line(up_flipped, "#a00", "--", 1.4, "flipped upper (now support)")
    draw_line(lo_active, "#00a", "-", 2.0, "active lower (support)")
    draw_line(lo_flipped, "#00a", "--", 1.4, "flipped lower (now resistance)")

    # vertical line at asof_bar
    ax.axvline(dates[asof_bar], color="#555", lw=0.9, ls="-.", alpha=0.6)
    ax.set_xlim(vis_dates[0], vis_dates[-1] + np.timedelta64(5, "D"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)

    # state summary in title
    def fmt_seg(seg, kind):
        if seg is None:
            return f"{kind}: none"
        if len(seg) == 4:
            i0, v0, i1, v1 = seg
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            return f"{kind}: {v0:.2f}@{str(dates[i0])[:7]} → {v1:.2f}@{str(dates[i1])[:7]} | now {proj:.2f}"
        else:
            i0, v0, i1, v1, bb, bv = seg
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            return f"{kind}(flipped): {v0:.2f}@{str(dates[i0])[:7]} → {v1:.2f}@{str(dates[i1])[:7]} broke {bv:.2f}@{str(dates[bb])[:7]} | now {proj:.2f}"
    info = "\n".join([
        fmt_seg(up_active, "UPPER live"),
        fmt_seg(up_flipped, "UPPER"),
        fmt_seg(lo_active, "LOWER live"),
        fmt_seg(lo_flipped, "LOWER"),
    ])
    return info


def plot_ticker_asof(ticker, asof_date_str, prom=1.0, window_bars=260):
    dates, ext = load_ticker_50ext(ticker)
    asof_dt = np.datetime64(asof_date_str)
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    if asof_bar < 0 or asof_bar >= len(dates):
        print(f"{ticker} asof {asof_date_str}: out of range")
        return

    fig, ax = plt.subplots(figsize=(16, 5.5))
    info = draw_asof_panel(ax, dates, ext, asof_bar, prom, window_bars)
    ax.set_title(f"{ticker} 50-ext — state as of {str(dates[asof_bar])[:10]} (prom ≥ {prom})")
    ax.set_ylabel("ext (ADR)")
    ax.legend(loc="upper right", fontsize=8)
    fig.text(0.01, -0.02, info, family="monospace", fontsize=8, va="top")
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"ext50_asof_{ticker}_{asof_date_str}.png")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"{ticker} asof {asof_date_str} -> {os.path.basename(out)}")


def main():
    jobs = [
        ("AAPL", "2026-04-10"),
        ("CAR", "2026-04-10"),
        ("SPY", "2026-04-10"),
        ("MSFT", "2026-04-10"),
        ("TSLA", "2026-04-10"),
    ]
    for tkr, asof in jobs:
        plot_ticker_asof(tkr, asof)


if __name__ == "__main__":
    main()
