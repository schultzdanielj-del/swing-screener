"""v4 primitive: adds touch-count gating, minimum-span gating, and value-
range gating so that trivial 2-anchor 'lines' (TSLA-garbage type) are
rejected before being promoted to active.

Gates applied when we'd promote a candidate line to active:
  - touches >= 3 (origin + endpoint + >= 1 intermediate pivot within
    TOUCH_TOL ADR of the line projection at that pivot's bar)
  - bar_span >= MIN_SPAN_BARS between anchors
  - value_range >= MIN_VAL_RANGE ADR between anchor values

If a candidate line doesn't satisfy the gates, origin/endpoint are kept
but no active line is reported (the state machine keeps looking).
"""
import io
import os

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import zstandard as zstd
from scipy.signal import find_peaks

CACHE_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\expr_series"
OUT_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research"
EXT50_D_COL = 358

TOUCH_TOL = 0.3  # ADR units — intermediate pivot within this of the line counts as touch
MIN_SPAN_BARS = 30
MIN_VAL_RANGE = 1.0  # ADR units


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


def count_touches_upper(i0, v0, i1, v1, peak_idxs, peak_vals, tol=TOUCH_TOL):
    """Count pivots in [i0, i1] (inclusive) within tol of the line projection.
    Returns 2 for just the anchors, 3+ if any intermediate pivot is near."""
    touches = 2  # anchors always count
    for pi, pv in zip(peak_idxs, peak_vals):
        if pi <= i0 or pi >= i1:
            continue
        proj = line_projection(i0, v0, i1, v1, pi)
        if abs(pv - proj) <= tol:
            touches += 1
    return touches


def count_touches_lower(i0, v0, i1, v1, trough_idxs, trough_vals, tol=TOUCH_TOL):
    touches = 2
    for ti, tv in zip(trough_idxs, trough_vals):
        if ti <= i0 or ti >= i1:
            continue
        proj = line_projection(i0, v0, i1, v1, ti)
        if abs(tv - proj) <= tol:
            touches += 1
    return touches


def line_passes_gates(i0, v0, i1, v1, touches):
    if touches < 3:
        return False
    if (i1 - i0) < MIN_SPAN_BARS:
        return False
    if abs(v0 - v1) < MIN_VAL_RANGE:
        return False
    return True


def run_upper_state_machine(ext, peak_idxs, peak_vals):
    """Upper downtrendline state machine with touch-count + span + range gates."""
    active = None  # (i0,v0,i1,v1,touches)
    origin = None
    broken = []

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

    last_processed_bar = -1

    for pi, pv in zip(peak_idxs, peak_vals):
        if active is not None:
            bb = find_break(active[0], active[1], active[2], active[3],
                            scan_from=last_processed_bar + 1, scan_until=pi - 1)
            if bb is not None:
                broken.append((*active, bb, float(ext[bb])))
                active = None
                origin = None

        if active is None:
            if origin is None:
                origin = (int(pi), float(pv))
            else:
                if pv >= origin[1]:
                    origin = (int(pi), float(pv))
                else:
                    if valid_upper_tangent(ext, origin[0], origin[1], pi, pv):
                        touches = count_touches_upper(origin[0], origin[1], pi, pv, peak_idxs, peak_vals)
                        if line_passes_gates(origin[0], origin[1], pi, pv, touches):
                            active = (origin[0], origin[1], int(pi), float(pv), touches)
                        # else: keep origin, don't promote
        else:
            if pv >= active[1]:
                # regime reset
                broken.append((*active, int(pi), float(pv)))
                active = None
                origin = (int(pi), float(pv))
            elif pv >= active[3]:
                proj = line_projection(active[0], active[1], active[2], active[3], pi)
                if pv > proj + 1e-6:
                    broken.append((*active, int(pi), float(pv)))
                    active = None
                    origin = (int(pi), float(pv))
            else:
                # try to extend
                if valid_upper_tangent(ext, active[0], active[1], pi, pv):
                    touches = count_touches_upper(active[0], active[1], pi, pv, peak_idxs, peak_vals)
                    if line_passes_gates(active[0], active[1], pi, pv, touches):
                        active = (active[0], active[1], int(pi), float(pv), touches)
        last_processed_bar = int(pi)

    if active is not None:
        bb = find_break(active[0], active[1], active[2], active[3],
                        scan_from=last_processed_bar + 1, scan_until=len(ext) - 1)
        if bb is not None:
            broken.append((*active, bb, float(ext[bb])))
            active = None

    return active, broken


def run_lower_state_machine(ext, trough_idxs, trough_vals):
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
            bb = find_break(active[0], active[1], active[2], active[3],
                            scan_from=last_processed_bar + 1, scan_until=ti - 1)
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
                        touches = count_touches_lower(origin[0], origin[1], ti, tv, trough_idxs, trough_vals)
                        if line_passes_gates(origin[0], origin[1], ti, tv, touches):
                            active = (origin[0], origin[1], int(ti), float(tv), touches)
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
                    touches = count_touches_lower(active[0], active[1], ti, tv, trough_idxs, trough_vals)
                    if line_passes_gates(active[0], active[1], ti, tv, touches):
                        active = (active[0], active[1], int(ti), float(tv), touches)
        last_processed_bar = int(ti)

    if active is not None:
        bb = find_break(active[0], active[1], active[2], active[3],
                        scan_from=last_processed_bar + 1, scan_until=len(ext) - 1)
        if bb is not None:
            broken.append((*active, bb, float(ext[bb])))
            active = None

    return active, broken


def state_as_of(ext, asof_bar, prominence=1.0):
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


def draw_asof(ticker, dates, ext, asof_date_str, prom=1.0, window_bars=260):
    asof_dt = np.datetime64(asof_date_str)
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    up_active, up_flipped, lo_active, lo_flipped = state_as_of(ext, asof_bar, prom)

    start_bar = max(0, asof_bar - window_bars)
    vis_ext = ext[start_bar: asof_bar + 1]
    vis_dates = dates[start_bar: asof_bar + 1]
    mask_nn = ~np.isnan(vis_ext)

    fig, ax = plt.subplots(figsize=(16, 5.5))
    colors = np.where(vis_ext[mask_nn] >= 0, "#4a9", "#c64")
    ax.bar(vis_dates[mask_nn], vis_ext[mask_nn], width=1.0, color=colors, edgecolor="none", alpha=0.55)
    ax.axhline(0, color="#333", lw=0.8)

    def draw_line(seg, color, ls, lw, label):
        if seg is None:
            return
        if len(seg) == 5:
            i0, v0, i1, v1, touches = seg
        elif len(seg) == 7:
            i0, v0, i1, v1, touches, bb, bv = seg
        else:
            i0, v0, i1, v1 = seg[:4]
            touches = None
        if i0 < start_bar and i1 < start_bar:
            return
        label_with_touches = f"{label} ({touches}t)" if touches else label
        ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, ls=ls, zorder=4, label=label_with_touches)
        if asof_bar > i1:
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            ax.plot([dates[i1], dates[asof_bar]], [v1, proj], color=color, lw=lw, ls=":", alpha=0.7, zorder=3)
        ax.scatter([dates[i0], dates[i1]], [v0, v1], color=color, s=36, zorder=6, edgecolor="#000", lw=0.5)

    draw_line(up_active, "#a00", "-", 2.0, "active upper")
    draw_line(up_flipped, "#a00", "--", 1.4, "flipped upper")
    draw_line(lo_active, "#00a", "-", 2.0, "active lower")
    draw_line(lo_flipped, "#00a", "--", 1.4, "flipped lower")

    ax.axvline(dates[asof_bar], color="#555", lw=0.9, ls="-.", alpha=0.6)
    ax.set_xlim(vis_dates[0], vis_dates[-1] + np.timedelta64(5, "D"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)
    ax.set_title(f"{ticker} 50-ext as of {str(dates[asof_bar])[:10]} — v4 (touch≥3, span≥{MIN_SPAN_BARS}, range≥{MIN_VAL_RANGE})")
    ax.set_ylabel("ext (ADR)")
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"ext50_v4_{ticker}_{asof_date_str}.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"{ticker} asof {asof_date_str} -> {os.path.basename(out)}"
          f"  up_active={'yes' if up_active else 'no'} up_flipped={'yes' if up_flipped else 'no'}"
          f"  lo_active={'yes' if lo_active else 'no'} lo_flipped={'yes' if lo_flipped else 'no'}")


def main():
    jobs = [
        ("AAPL", "2026-04-10"),
        ("CAR", "2026-04-10"),
        ("SPY", "2026-04-10"),
        ("MSFT", "2026-04-10"),
        ("TSLA", "2026-04-10"),
        ("RIVN", "2026-04-10"),
    ]
    for tkr, asof in jobs:
        dates, ext = load_ticker_50ext(tkr)
        draw_asof(tkr, dates, ext, asof)


if __name__ == "__main__":
    main()
