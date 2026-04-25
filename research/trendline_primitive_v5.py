"""v5 primitive: cascade generator over a bounded relevance window.

Replaces v4's single-active state machine with a candidate-enumerator that
emits up to N cascade lines per side. Addresses five calibration failures
from the asof-image review (2026-04-22):

  1. Lower line direction-agnostic: ascending (wedge) OR descending (channel).
  2. Cascade on BOTH sides — multiple stacked valid lines may coexist.
  3. Anchor preference = extremum within ±W bars, not first-qualifying pivot.
  4. Stale-anchor retirement via relevance window (WINDOW_BARS).
  5. Downside lines tolerate occasional piercing; upside strict.
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

WINDOW_BARS = 260          # relevance window = ~12 months
ANCHOR_SLIDE_BARS = 5      # ± window for anchor displacement to local extremum
TOUCH_TOL = 0.3            # ADR — intermediate pivot within this counts as touch
UPPER_PIERCE_TOL = 0.2     # ADR — upper tangent tolerance
UPPER_PIERCE_FRAC = 0.05   # allow up to 5% of bars to poke above
LOWER_PIERCE_TOL = 0.3     # ADR — loose tangency tolerance for lower
LOWER_PIERCE_FRAC = 0.10   # allow up to 10% of intervening bars to poke through
MIN_SPAN_BARS = 30
MIN_VAL_RANGE = 1.0        # ADR — minimum anchor value spread
MIN_TOUCHES = 3
STALE_BARS = 180           # i1 must be within this of asof_bar to stay active
PROMINENCE = 1.0           # ADR — scipy find_peaks prominence
MAX_CASCADE = 3            # emit up to this many per side


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


def slide_to_local_extremum(ext, idx, direction, half_window=ANCHOR_SLIDE_BARS):
    """Given an anchor at `idx`, check ±half_window bars for a more-extreme
    value and slide the anchor there if found. Handles NaN safely."""
    lo = max(0, idx - half_window)
    hi = min(len(ext) - 1, idx + half_window)
    seg = ext[lo:hi + 1]
    if np.all(np.isnan(seg)):
        return idx, float(ext[idx])
    if direction == "upper":
        k = int(np.nanargmax(seg)) + lo
    else:
        k = int(np.nanargmin(seg)) + lo
    return k, float(ext[k])


def strict_upper_tangent(ext, i0, v0, i1, v1, asof_bar, tol=UPPER_PIERCE_TOL, frac=UPPER_PIERCE_FRAC):
    """Count-based upper tangent: at most frac*span bars pierce by > tol."""
    if i1 <= i0 or asof_bar < i1:
        return False
    idx = np.arange(i0, asof_bar + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx]
    mask = ~np.isnan(vals)
    pierces = vals[mask] > (proj[mask] + tol)
    span = mask.sum()
    allowed = max(1, int(frac * span))
    return pierces.sum() <= allowed


def loose_lower_tangent(ext, i0, v0, i1, v1, asof_bar, tol=LOWER_PIERCE_TOL, frac=LOWER_PIERCE_FRAC):
    """Count-based survival: at most frac*span bars pierce the line by > tol."""
    if i1 <= i0 or asof_bar < i1:
        return False
    idx = np.arange(i0, asof_bar + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx]
    mask = ~np.isnan(vals)
    pierces = vals[mask] < (proj[mask] - tol)
    span = mask.sum()
    allowed = max(1, int(frac * span))
    return pierces.sum() <= allowed


def count_touches(i0, v0, i1, v1, pivot_idxs, pivot_vals, tol=TOUCH_TOL):
    touches = 2
    for pi, pv in zip(pivot_idxs, pivot_vals):
        if pi <= i0 or pi >= i1:
            continue
        proj = line_projection(i0, v0, i1, v1, pi)
        if abs(pv - proj) <= tol:
            touches += 1
    return touches


def line_passes_gates(i0, v0, i1, v1, touches, asof_bar):
    if touches < MIN_TOUCHES:
        return False
    # Effective usable span = first anchor to asof. Captures lines with
    # short anchor-to-anchor distance but long forward projection.
    if (asof_bar - i0) < MIN_SPAN_BARS:
        return False
    if abs(v0 - v1) < MIN_VAL_RANGE:
        return False
    return True


def enumerate_upper_cascade(ext, asof_bar, window_start):
    """Enumerate all valid upper cascade lines anchored within window."""
    peaks_i, _ = find_peaks(np.nan_to_num(ext, nan=-1e9), prominence=PROMINENCE)
    peaks_i = peaks_i[peaks_i <= asof_bar]
    peaks_i = peaks_i[peaks_i >= window_start]
    peaks_i = peaks_i[ext[peaks_i] >= 0]
    peaks_v = ext[peaks_i]

    # slide to local extrema
    slid = [slide_to_local_extremum(ext, int(i), "upper") for i in peaks_i]
    if not slid:
        return []
    slid_i = np.array([s[0] for s in slid], dtype=int)
    slid_v = np.array([s[1] for s in slid], dtype=float)

    lines = []
    for a in range(len(slid_i)):
        for b in range(a + 1, len(slid_i)):
            i0, v0 = int(slid_i[a]), float(slid_v[a])
            i1, v1 = int(slid_i[b]), float(slid_v[b])
            if v1 > v0:
                continue
            if (asof_bar - i1) > STALE_BARS:
                continue
            if not strict_upper_tangent(ext, i0, v0, i1, v1, asof_bar):
                continue
            t = count_touches(i0, v0, i1, v1, slid_i, slid_v)
            if not line_passes_gates(i0, v0, i1, v1, t, asof_bar):
                continue
            lines.append({"i0": i0, "v0": v0, "i1": i1, "v1": v1, "touches": t,
                          "slope": (v1 - v0) / (i1 - i0)})
    return lines


def enumerate_lower_cascade(ext, asof_bar, window_start):
    """Enumerate all valid lower cascade lines — direction-agnostic."""
    trough_idx, _ = find_peaks(-np.nan_to_num(ext, nan=1e9), prominence=PROMINENCE)
    trough_idx = trough_idx[trough_idx <= asof_bar]
    trough_idx = trough_idx[trough_idx >= window_start]
    trough_idx = trough_idx[ext[trough_idx] <= 0]
    trough_val = ext[trough_idx]

    slid = [slide_to_local_extremum(ext, int(i), "lower") for i in trough_idx]
    if not slid:
        return []
    slid_i = np.array([s[0] for s in slid], dtype=int)
    slid_v = np.array([s[1] for s in slid], dtype=float)

    lines = []
    for a in range(len(slid_i)):
        for b in range(a + 1, len(slid_i)):
            i0, v0 = int(slid_i[a]), float(slid_v[a])
            i1, v1 = int(slid_i[b]), float(slid_v[b])
            # direction-agnostic — both ascending and descending allowed
            if (asof_bar - i1) > STALE_BARS:
                continue
            if not loose_lower_tangent(ext, i0, v0, i1, v1, asof_bar):
                continue
            t = count_touches(i0, v0, i1, v1, slid_i, slid_v)
            if not line_passes_gates(i0, v0, i1, v1, t, asof_bar):
                continue
            lines.append({"i0": i0, "v0": v0, "i1": i1, "v1": v1, "touches": t,
                          "slope": (v1 - v0) / (i1 - i0)})
    return lines


def dedupe_and_rank(lines, asof_bar, max_keep=MAX_CASCADE, proj_tol=0.5):
    """Rank by touches desc then span desc. Two lines are duplicates if their
    projections at asof are within proj_tol AND slopes differ by < 30%."""
    if not lines:
        return []
    for L in lines:
        L["proj_asof"] = line_projection(L["i0"], L["v0"], L["i1"], L["v1"], asof_bar)
    lines = sorted(lines,
                   key=lambda L: (-L["touches"], -(L["i1"] - L["i0"])))
    kept = []
    for L in lines:
        is_dup = False
        for K in kept:
            proj_close = abs(L["proj_asof"] - K["proj_asof"]) < proj_tol
            slope_close = abs(L["slope"] - K["slope"]) < max(1e-6, 0.3 * abs(K["slope"] + 1e-6))
            if proj_close and slope_close:
                is_dup = True
                break
        if not is_dup:
            kept.append(L)
        if len(kept) >= max_keep:
            break
    return kept


def pick_majority_direction(lines):
    """Group lines by slope sign; return only the majority-direction group."""
    if not lines:
        return []
    asc = [L for L in lines if L["slope"] > 0]
    desc = [L for L in lines if L["slope"] <= 0]
    # Majority by aggregate touch count — more signal than raw count
    asc_score = sum(L["touches"] for L in asc)
    desc_score = sum(L["touches"] for L in desc)
    return asc if asc_score >= desc_score else desc


def cascade_at(ext, asof_bar):
    window_start = max(0, asof_bar - WINDOW_BARS)
    upper_raw = enumerate_upper_cascade(ext, asof_bar, window_start)
    lower_raw = enumerate_lower_cascade(ext, asof_bar, window_start)
    lower_raw = pick_majority_direction(lower_raw)
    return dedupe_and_rank(upper_raw, asof_bar), dedupe_and_rank(lower_raw, asof_bar)


def draw_asof(ticker, dates, ext, asof_date_str):
    asof_dt = np.datetime64(asof_date_str)
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    if asof_bar < 0 or asof_bar >= len(dates):
        print(f"{ticker} {asof_date_str}: out of range")
        return

    upper_lines, lower_lines = cascade_at(ext, asof_bar)

    start_bar = max(0, asof_bar - WINDOW_BARS)
    vis_ext = ext[start_bar: asof_bar + 1]
    vis_dates = dates[start_bar: asof_bar + 1]
    mask_nn = ~np.isnan(vis_ext)

    fig, ax = plt.subplots(figsize=(16, 5.5))
    colors = np.where(vis_ext[mask_nn] >= 0, "#4a9", "#c64")
    ax.bar(vis_dates[mask_nn], vis_ext[mask_nn], width=1.0, color=colors, edgecolor="none", alpha=0.55)
    ax.axhline(0, color="#333", lw=0.8)

    def draw(L, color, lw, label):
        i0, v0, i1, v1, t = L["i0"], L["v0"], L["i1"], L["v1"], L["touches"]
        ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, zorder=4, label=f"{label} ({t}t)")
        if asof_bar > i1:
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            ax.plot([dates[i1], dates[asof_bar]], [v1, proj], color=color, lw=lw, ls=":", alpha=0.7, zorder=3)
        ax.scatter([dates[i0], dates[i1]], [v0, v1], color=color, s=34, zorder=6, edgecolor="#000", lw=0.5)

    upper_shades = ["#a00", "#d33", "#f66"]
    lower_shades = ["#00a", "#33d", "#66f"]
    for n, L in enumerate(upper_lines):
        draw(L, upper_shades[n % len(upper_shades)], 2.0 - 0.3 * n, f"U{n + 1}")
    for n, L in enumerate(lower_lines):
        draw(L, lower_shades[n % len(lower_shades)], 2.0 - 0.3 * n, f"L{n + 1}")

    ax.axvline(dates[asof_bar], color="#555", lw=0.9, ls="-.", alpha=0.6)
    ax.set_xlim(vis_dates[0], vis_dates[-1] + np.timedelta64(5, "D"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)
    ax.set_title(f"{ticker} 50-ext as of {str(dates[asof_bar])[:10]} — v5 cascade "
                 f"(u={len(upper_lines)} l={len(lower_lines)})")
    ax.set_ylabel("ext (ADR)")
    ax.legend(loc="upper right", fontsize=7)
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"ext50_v5_{ticker}_{asof_date_str}.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)

    def fmt(L, kind):
        proj = line_projection(L["i0"], L["v0"], L["i1"], L["v1"], asof_bar)
        return (f"  {kind}: {L['v0']:+.2f}@{str(dates[L['i0']])[:7]} -> "
                f"{L['v1']:+.2f}@{str(dates[L['i1']])[:7]} "
                f"| slope={L['slope']:+.4f} | {L['touches']}t | now {proj:+.2f}")
    print(f"{ticker} asof {asof_date_str} -> {os.path.basename(out)}")
    for n, L in enumerate(upper_lines):
        print(fmt(L, f"U{n + 1}"))
    for n, L in enumerate(lower_lines):
        print(fmt(L, f"L{n + 1}"))


def diagnose(ticker, asof_date_str):
    """Verbose enumeration dump — which candidate pairs get produced, which
    gate they fail at, so we can see why a side emits nothing."""
    dates, ext = load_ticker_50ext(ticker)
    asof_dt = np.datetime64(asof_date_str)
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    window_start = max(0, asof_bar - WINDOW_BARS)
    print(f"\n=== {ticker} asof {asof_date_str} (bar {asof_bar}, window {window_start}..{asof_bar}) ===")

    for side, label in (("upper", "UPPER"), ("lower", "LOWER")):
        if side == "upper":
            pks, _ = find_peaks(np.nan_to_num(ext, nan=-1e9), prominence=PROMINENCE)
            pks = pks[(pks <= asof_bar) & (pks >= window_start)]
            pks = pks[ext[pks] >= 0]
        else:
            pks, _ = find_peaks(-np.nan_to_num(ext, nan=1e9), prominence=PROMINENCE)
            pks = pks[(pks <= asof_bar) & (pks >= window_start)]
            pks = pks[ext[pks] <= 0]

        print(f"  [{label}] raw pivots in window: {len(pks)}")
        for p in pks:
            print(f"    bar {p} = {str(dates[p])[:10]} ext={ext[p]:+.2f}")

        slid = [slide_to_local_extremum(ext, int(i), side) for i in pks]
        if not slid:
            continue
        slid_i = np.array([s[0] for s in slid], dtype=int)
        slid_v = np.array([s[1] for s in slid], dtype=float)

        fail_tangent = fail_gate = accepted = 0
        for a in range(len(slid_i)):
            for b in range(a + 1, len(slid_i)):
                i0, v0 = int(slid_i[a]), float(slid_v[a])
                i1, v1 = int(slid_i[b]), float(slid_v[b])
                if side == "upper" and v1 > v0:
                    continue
                if (asof_bar - i1) > STALE_BARS:
                    continue
                if side == "upper":
                    ok = strict_upper_tangent(ext, i0, v0, i1, v1, asof_bar)
                else:
                    ok = loose_lower_tangent(ext, i0, v0, i1, v1, asof_bar)
                # (diagnose uses same checks as enumerate_*)
                if not ok:
                    fail_tangent += 1
                    continue
                t = count_touches(i0, v0, i1, v1, slid_i, slid_v)
                if not line_passes_gates(i0, v0, i1, v1, t, asof_bar):
                    fail_gate += 1
                    continue
                accepted += 1
        print(f"  [{label}] fail_tangent={fail_tangent} fail_gate={fail_gate} accepted={accepted}")


def main():
    jobs = [
        ("AAPL", "2026-04-10"),
        ("CAR", "2026-04-10"),
        ("SPY", "2026-04-10"),
        ("MSFT", "2026-04-10"),
        ("TSLA", "2026-04-10"),
    ]
    for tkr, asof in jobs:
        dates, ext = load_ticker_50ext(tkr)
        draw_asof(tkr, dates, ext, asof)


if __name__ == "__main__":
    main()
