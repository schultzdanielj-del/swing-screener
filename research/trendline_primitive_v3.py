"""v3 primitive: ONE active downtrendline (and one uptrendline mirror) at a
time, enforced as a VALID UPPER TANGENT (no bar between anchors is allowed
to pierce the line). Extend-or-retain on new lower peaks; retire on break.

State machine per side (upper downtrendline example):
  - Look for `origin` peak (the highest peak of the current regime). A new
    peak >= origin's value resets origin and retires any active line.
  - Once origin exists, look for an `endpoint` peak LOWER than origin such
    that the line origin->endpoint is a valid upper tangent (ext <= line
    projection on every bar in between, inclusive).
  - With an active line (origin, endpoint):
      - On a new peak lower than endpoint: try redraw as origin -> new peak.
        If valid tangent, extend (line shallows). Else keep prior line.
      - On any bar crossing above the line's forward projection: line
        breaks; retire to broken list; start a new hunt for origin/endpoint
        from the break bar forward.
      - On a new peak >= origin.val: regime reset; retire active line;
        origin = new peak.

Feature-1 sign constraints:
  - Upper downtrendline: only POSITIVE peaks are eligible anchors.
  - Lower uptrendline: only NEGATIVE troughs; mirror semantics.

Output per ticker/window: one solid active line + any broken lines as
dashed, on both sides.
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


def line_projection(i0, v0, i1, v1, i):
    if i1 == i0:
        return v0
    return v0 + (v1 - v0) * (i - i0) / (i1 - i0)


def valid_upper_tangent(ext, i0, v0, i1, v1):
    """No bar in [i0, i1] exceeds the line from (i0,v0) to (i1,v1)."""
    if i1 <= i0:
        return False
    idx = np.arange(i0, i1 + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    return np.all(ext[idx] <= proj + 1e-6)


def valid_lower_tangent(ext, i0, v0, i1, v1):
    if i1 <= i0:
        return False
    idx = np.arange(i0, i1 + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    return np.all(ext[idx] >= proj - 1e-6)


def run_upper_state_machine(ext, peak_idxs, peak_vals):
    """Process peaks in time order. Returns:
       active_line: (i0, v0, i1, v1) or None
       broken_lines: list of (i0, v0, i1, v1, break_bar, break_val)
    """
    active = None  # (origin_i, origin_v, endpoint_i, endpoint_v)
    origin = None  # (i, v) — set when we have a regime high but no endpoint yet
    broken = []

    # helper: scan forward from `from_bar` to `until_bar` and check if ext
    # pierces the active line's projection. Returns break_bar or None.
    def find_break(i0, v0, i1, v1, scan_from, scan_until):
        if scan_until < scan_from:
            return None
        idx = np.arange(scan_from, scan_until + 1)
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
        above = ext[idx] > proj + 1e-6
        if not np.any(above):
            return None
        first = idx[np.argmax(above)]
        return int(first)

    last_processed_bar = -1  # scan breaks forward from this +1

    for pi, pv in zip(peak_idxs, peak_vals):
        # First: check if any bar BEFORE this peak broke the active line.
        if active is not None:
            bb = find_break(*active, scan_from=last_processed_bar + 1, scan_until=pi - 1)
            if bb is not None:
                broken.append((*active, bb, float(ext[bb])))
                active = None
                origin = None  # reset; will re-establish from this peak or later

        if active is None:
            if origin is None:
                origin = (int(pi), float(pv))
            else:
                if pv >= origin[1]:
                    # new peak >= origin: regime reset
                    origin = (int(pi), float(pv))
                else:
                    # candidate endpoint — must form valid upper tangent
                    if valid_upper_tangent(ext, origin[0], origin[1], pi, pv):
                        active = (origin[0], origin[1], int(pi), float(pv))
                    # else: not valid, keep looking (origin unchanged)
        else:
            # active line exists
            if pv >= active[1]:
                # new peak >= origin.val: regime reset
                broken.append((*active, int(pi), float(pv)))
                active = None
                origin = (int(pi), float(pv))
            elif pv >= active[3]:
                # new peak between endpoint and origin in value — this
                # doesn't extend or break the line by itself; check if it's
                # below the line's projection
                proj = line_projection(active[0], active[1], active[2], active[3], pi)
                if pv > proj + 1e-6:
                    # peak pierced the line: break
                    broken.append((*active, int(pi), float(pv)))
                    active = None
                    origin = (int(pi), float(pv))
                # else: peak below projection, line still active, ignore
            else:
                # new peak lower than current endpoint — try to extend
                if valid_upper_tangent(ext, active[0], active[1], pi, pv):
                    active = (active[0], active[1], int(pi), float(pv))
                # else: can't extend cleanly; keep existing line
        last_processed_bar = int(pi)

    # After all peaks: scan for break up to end of series
    if active is not None:
        bb = find_break(*active, scan_from=last_processed_bar + 1, scan_until=len(ext) - 1)
        if bb is not None:
            broken.append((*active, bb, float(ext[bb])))
            active = None

    return active, broken


def run_lower_state_machine(ext, trough_idxs, trough_vals):
    """Mirror for negative troughs (lower uptrendline of feature 1)."""
    active = None
    origin = None
    broken = []

    def find_break(i0, v0, i1, v1, scan_from, scan_until):
        if scan_until < scan_from:
            return None
        idx = np.arange(scan_from, scan_until + 1)
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
        below = ext[idx] < proj - 1e-6
        if not np.any(below):
            return None
        first = idx[np.argmax(below)]
        return int(first)

    last_processed_bar = -1

    for ti, tv in zip(trough_idxs, trough_vals):
        if active is not None:
            bb = find_break(*active, scan_from=last_processed_bar + 1, scan_until=ti - 1)
            if bb is not None:
                broken.append((*active, bb, float(ext[bb])))
                active = None
                origin = None

        if active is None:
            if origin is None:
                origin = (int(ti), float(tv))
            else:
                if tv <= origin[1]:
                    origin = (int(ti), float(tv))
                else:
                    if valid_lower_tangent(ext, origin[0], origin[1], ti, tv):
                        active = (origin[0], origin[1], int(ti), float(tv))
        else:
            if tv <= active[1]:
                broken.append((*active, int(ti), float(tv)))
                active = None
                origin = (int(ti), float(tv))
            elif tv <= active[3]:
                proj = line_projection(active[0], active[1], active[2], active[3], ti)
                if tv < proj - 1e-6:
                    broken.append((*active, int(ti), float(tv)))
                    active = None
                    origin = (int(ti), float(tv))
            else:
                if valid_lower_tangent(ext, active[0], active[1], ti, tv):
                    active = (active[0], active[1], int(ti), float(tv))
        last_processed_bar = int(ti)

    if active is not None:
        bb = find_break(*active, scan_from=last_processed_bar + 1, scan_until=len(ext) - 1)
        if bb is not None:
            broken.append((*active, bb, float(ext[bb])))
            active = None

    return active, broken


def plot_window(ticker, dates, ext, prominence, date_start, date_end, out_path, tag):
    # Sign-constrained peaks/troughs for feature 1
    peak_idx, _ = find_peaks(ext, prominence=prominence)
    pos_peaks = [(i, ext[i]) for i in peak_idx if ext[i] > 0]
    if pos_peaks:
        pi_arr = np.array([p[0] for p in pos_peaks])
        pv_arr = np.array([p[1] for p in pos_peaks])
    else:
        pi_arr = np.array([], dtype=int)
        pv_arr = np.array([])

    trough_idx, _ = find_peaks(-ext, prominence=prominence)
    neg_troughs = [(i, ext[i]) for i in trough_idx if ext[i] < 0]
    if neg_troughs:
        ti_arr = np.array([t[0] for t in neg_troughs])
        tv_arr = np.array([t[1] for t in neg_troughs])
    else:
        ti_arr = np.array([], dtype=int)
        tv_arr = np.array([])

    up_active, up_broken = run_upper_state_machine(ext, pi_arr, pv_arr)
    lo_active, lo_broken = run_lower_state_machine(ext, ti_arr, tv_arr)

    start = np.datetime64(date_start) if date_start else dates[0]
    end = np.datetime64(date_end) if date_end else dates[-1]
    win_mask = (dates >= start) & (dates <= end)

    fig, ax = plt.subplots(figsize=(16, 5.5))
    vis_ext = ext[win_mask]
    vis_dates = dates[win_mask]
    mask_nn = ~np.isnan(vis_ext)
    colors = np.where(vis_ext[mask_nn] >= 0, "#4a9", "#c64")
    ax.bar(vis_dates[mask_nn], vis_ext[mask_nn], width=1.0, color=colors, edgecolor="none", alpha=0.55)
    ax.axhline(0, color="#333", lw=0.8)

    # anchor markers (only those inside window)
    def in_win(i):
        return start <= dates[i] <= end

    pos_in = [p for p in pos_peaks if in_win(p[0])]
    if pos_in:
        ax.scatter([dates[p[0]] for p in pos_in], [p[1] for p in pos_in],
                   marker="v", color="#063", s=30, zorder=5, label="pos peaks")
    neg_in = [t for t in neg_troughs if in_win(t[0])]
    if neg_in:
        ax.scatter([dates[t[0]] for t in neg_in], [t[1] for t in neg_in],
                   marker="^", color="#630", s=30, zorder=5, label="neg troughs")

    def draw_line(i0, v0, i1, v1, extend_to_bar, color, ls, lw, alpha):
        # draw anchor segment
        ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, ls=ls, alpha=alpha, zorder=4)
        # draw projection forward to extend_to_bar
        if extend_to_bar is not None and extend_to_bar > i1:
            proj = line_projection(i0, v0, i1, v1, extend_to_bar)
            ax.plot([dates[i1], dates[extend_to_bar]], [v1, proj], color=color, lw=lw * 0.8,
                    ls=":", alpha=alpha * 0.8, zorder=3)

    # Active upper line — extend projection to end of window
    if up_active is not None:
        i0, v0, i1, v1 = up_active
        end_bar = np.searchsorted(dates, end)
        draw_line(i0, v0, i1, v1, min(end_bar, len(ext) - 1), "#a00", "-", 1.8, 1.0)

    for seg in up_broken:
        i0, v0, i1, v1, bb, bv = seg
        draw_line(i0, v0, i1, v1, bb, "#a00", "--", 1.1, 0.55)
        ax.scatter(dates[bb], bv, marker="x", color="#c00", s=45, zorder=6)

    # Active lower line
    if lo_active is not None:
        i0, v0, i1, v1 = lo_active
        end_bar = np.searchsorted(dates, end)
        draw_line(i0, v0, i1, v1, min(end_bar, len(ext) - 1), "#00a", "-", 1.8, 1.0)

    for seg in lo_broken:
        i0, v0, i1, v1, bb, bv = seg
        draw_line(i0, v0, i1, v1, bb, "#00a", "--", 1.1, 0.55)
        ax.scatter(dates[bb], bv, marker="x", color="#00c", s=45, zorder=6)

    ax.set_xlim(start, end)
    ax.set_title(f"{ticker} 50-ext — v3 (one active line, valid upper tangent, prom ≥ {prominence}) — {tag}")
    ax.set_ylabel("ext (ADR)")
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main():
    jobs = [
        ("AAPL", "2025-08-01", "2026-05-01", "dan_image_window"),
        ("AAPL", None, None, "full_history"),
        ("CAR", "2026-01-15", "2026-05-15", "momo_image_window"),
        ("CAR", None, None, "full_history"),
        ("SPY", "2025-08-01", "2026-05-01", "recent_chop"),
    ]
    for tkr, ds, de, tag in jobs:
        try:
            dates, ext = load_ticker_50ext(tkr)
        except FileNotFoundError:
            print(f"{tkr}: not in cache")
            continue
        for prom in (1.0, 1.5):
            out = os.path.join(OUT_DIR, f"ext50_v3_{tkr}_{tag}_prom{prom:.1f}.png")
            plot_window(tkr, dates, ext, prom, ds, de, out, tag)
            print(f"{tkr} {tag} prom={prom} -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
