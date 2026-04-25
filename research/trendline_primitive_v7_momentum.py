"""v7 primitive: Momentum-Channel Trendline Pair (feature 2).

Template stamped off v6 (Contracting-Range feature 1). Same skeleton,
per-candidate features, top-N collapse, and proximity ranking. Differences:

- Smaller pivot prominence (0.5 ADR) to pick up micro-dips/peaks on the
  side of an active mountain.
- Shorter relevance window (80 bars) and min-span (15 bars) — channel
  lines live within one rising leg, not across cycles.
- Both upper channel (peak-anchored) and lower channel (trough-anchored)
  are ASCENDING (slope > 0). Drops feature 1's anchor-type↔slope matching.
- Drops feature 1's origin-sign opposition rule — momentum channels
  originate from positive-ext territory with positive slope.
- Both anchors must be positive (mountain context; channel does not live
  in negative territory).
- Cross-cycle / staleness: channel is active only while ext stays above
  chop_upper (or >0 fallback). Any bar in [i0, asof] with ext ≤ 0
  retires the channel.
"""
import io
import os

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import zstandard as zstd
from scipy.signal import find_peaks

from reversal_profile_derive import derive_profile

CACHE_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\expr_series"
OUT_DIR = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research"
EXT50_D_COL = 358

WINDOW_BARS = 260           # match feature 1 — capture lines from past climbs
ANCHOR_SLIDE_BARS = 3
PROMINENCE = 0.5            # ADR — micro-pivot prominence
MIN_SPAN_BARS = 15          # shorter min anchor separation for channel phase
TOUCH_TOL = 0.25
TOP_N = 3


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
    lo = max(0, idx - half_window)
    hi = min(len(ext) - 1, idx + half_window)
    seg = ext[lo:hi + 1]
    if np.all(np.isnan(seg)):
        return idx, float(ext[idx])
    if direction == "peak":
        k = int(np.nanargmax(seg)) + lo
    else:
        k = int(np.nanargmin(seg)) + lo
    return k, float(ext[k])


def find_anchors(ext, asof_bar, window_start):
    """Anchors are ONLY the raw detected pivots at PROMINENCE. No slide —
    slide can pick non-pivot bars between detected pivots (observed on SPY:
    slide moved a detected trough to a neighboring bar on the descent to a
    deeper real trough that the ±3 slide window couldn't reach)."""
    nn_up = np.nan_to_num(ext, nan=-1e9)
    nn_dn = -np.nan_to_num(ext, nan=1e9)
    peaks, _ = find_peaks(nn_up, prominence=PROMINENCE)
    troughs, _ = find_peaks(nn_dn, prominence=PROMINENCE)

    def prep(idxs):
        idxs = idxs[(idxs <= asof_bar) & (idxs >= window_start)]
        return idxs.astype(int), ext[idxs].astype(float)

    peak_i, peak_v = prep(peaks)
    trough_i, trough_v = prep(troughs)
    return peak_i, peak_v, trough_i, trough_v


def has_line_break(ext, i0, v0, i1, v1, asof_bar):
    if asof_bar <= i0:
        return False
    idx = np.arange(i0, asof_bar + 1)
    if i1 == i0:
        proj = np.full(len(idx), v0, dtype=float)
    else:
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx].astype(float)
    mask = ~np.isnan(vals)
    if mask.sum() < 2:
        return False
    diff = vals - proj
    sign = np.where(np.isnan(diff), 0, np.sign(diff))
    prev_sign = 0
    for k in range(len(idx)):
        if not mask[k]:
            continue
        s = sign[k]
        if s == 0:
            continue
        if prev_sign == 0:
            prev_sign = s
            continue
        if s != prev_sign:
            return True
    return False


def has_origination_side_break(ext, i0, v0, i1, v1, asof_bar):
    if v0 == 0 or asof_bar <= i0:
        return False
    idx = np.arange(i0, asof_bar + 1)
    if i1 == i0:
        proj = np.full(len(idx), v0, dtype=float)
    else:
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx].astype(float)
    mask = ~np.isnan(vals)
    if mask.sum() < 2:
        return False
    diff = vals - proj
    sign = np.where(np.isnan(diff), 0, np.sign(diff))
    origin_sign = np.sign(v0)
    proj_sign = np.sign(proj)
    on_origin = (proj_sign == origin_sign)
    prev_sign = 0
    for k in range(len(idx)):
        if not mask[k] or not on_origin[k]:
            if not on_origin[k] and k > 0 and on_origin[k - 1]:
                break
            continue
        s = sign[k]
        if s == 0:
            continue
        if prev_sign == 0:
            prev_sign = s
            continue
        if s != prev_sign:
            return True
    return False


def compute_cross_events(ext, i0, v0, i1, v1, asof_bar):
    if asof_bar <= i0:
        return 0, -1, 0
    idx = np.arange(i0, asof_bar + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0) if i1 != i0 else np.full_like(idx, v0, dtype=float)
    vals = ext[idx].astype(float)
    mask = ~np.isnan(vals)
    if mask.sum() < 2:
        return 0, -1, 0
    diff = vals - proj
    sign = np.where(np.isnan(diff), 0, np.sign(diff))
    total = 0
    last_bar = -1
    last_dir = 0
    prev_sign = 0
    for k in range(len(idx)):
        if not mask[k]:
            continue
        s = sign[k]
        if s == 0:
            continue
        if prev_sign == 0:
            prev_sign = s
            continue
        if s != prev_sign:
            total += 1
            last_bar = int(idx[k])
            last_dir = int(s)
            prev_sign = s
    return total, last_bar, last_dir


def compute_zero_crossing(i0, v0, i1, v1, asof_bar):
    if v1 == v0:
        return -1
    slope = (v1 - v0) / (i1 - i0) if i1 != i0 else 0.0
    if slope == 0:
        return -1
    bar_zero = i0 - v0 / slope
    bar_zero_int = int(np.round(bar_zero))
    if bar_zero_int < i0 or bar_zero_int > asof_bar:
        return -1
    return bar_zero_int


def compute_segment_bars(ext, i0, v0, i1, v1, asof_bar):
    if asof_bar <= i0:
        return 0, 0
    idx = np.arange(i0, asof_bar + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0) if i1 != i0 else np.full_like(idx, v0, dtype=float)
    pos_bars = int(np.sum(proj >= 0))
    neg_bars = int(np.sum(proj < 0))
    return pos_bars, neg_bars


def compute_touches(i0, v0, i1, v1, asof_bar, pivot_idx, pivot_val, tol=TOUCH_TOL):
    if len(pivot_idx) == 0:
        return 2
    mask = (pivot_idx > i0) & (pivot_idx <= asof_bar) & (pivot_idx != i1)
    if not mask.any():
        return 2
    pis = pivot_idx[mask]
    pvs = pivot_val[mask]
    if i1 == i0:
        proj = np.full_like(pis, v0, dtype=float)
    else:
        proj = v0 + (v1 - v0) * (pis - i0) / (i1 - i0)
    hits = int(np.sum(np.abs(pvs - proj) <= tol))
    return 2 + hits


def enumerate_candidates(ext, asof_bar, window_start, upside_1=float("nan"),
                         downside_1=float("nan"), upside_2=float("nan"),
                         downside_2=float("nan"), chop_upper=float("nan")):
    peak_i, peak_v, trough_i, trough_v = find_anchors(ext, asof_bar, window_start)

    all_piv_idx = np.concatenate([peak_i, trough_i]) if len(peak_i) + len(trough_i) > 0 else np.zeros(0, dtype=int)
    all_piv_val = np.concatenate([peak_v, trough_v]) if len(peak_v) + len(trough_v) > 0 else np.zeros(0, dtype=float)
    order = np.argsort(all_piv_idx)
    all_piv_idx = all_piv_idx[order]
    all_piv_val = all_piv_val[order]

    ext_asof = float(ext[asof_bar]) if not np.isnan(ext[asof_bar]) else 0.0

    candidates = []

    def add_pairs(anchor_i, anchor_v, anchor_type):
        for a in range(len(anchor_i)):
            for b in range(a + 1, len(anchor_i)):
                i0, v0 = int(anchor_i[a]), float(anchor_v[a])
                i1, v1 = int(anchor_i[b]), float(anchor_v[b])
                if i1 <= i0:
                    continue
                if (i1 - i0) < MIN_SPAN_BARS:
                    continue
                if (asof_bar - i0) < MIN_SPAN_BARS:
                    continue
                slope = (v1 - v0) / (i1 - i0)
                # Universal origin-sign rule
                if slope < 0 and v0 < 0:
                    continue
                if slope > 0 and v0 > 0:
                    continue
                # Same-side anchors
                s0, s1 = np.sign(v0), np.sign(v1)
                if s0 != 0 and s1 != 0 and s0 != s1:
                    continue
                if s0 == 0 or s1 == 0:
                    continue
                # Anchor type must match slope direction
                if anchor_type == "peak_anchored" and slope >= 0:
                    continue
                if anchor_type == "trough_anchored" and slope <= 0:
                    continue
                # Asymmetric break check:
                # - Descending lines: origin-side only (pokes OK in flipped
                #   negative "hill" role).
                # - Ascending lines: whole-life strict (pokes rare in
                #   flipped positive "resistance" role).
                if slope < 0:
                    if has_origination_side_break(ext, i0, v0, i1, v1, asof_bar):
                        continue
                else:
                    if has_line_break(ext, i0, v0, i1, v1, asof_bar):
                        continue
                # Projection-in-range: drop wildly extrapolated lines
                proj_check = line_projection(i0, v0, i1, v1, asof_bar)
                if not np.isnan(upside_2) and proj_check > upside_2:
                    continue
                if not np.isnan(downside_2) and proj_check < downside_2:
                    continue
                proj_asof = line_projection(i0, v0, i1, v1, asof_bar)
                signed_dist = proj_asof - ext_asof
                zero_bar = compute_zero_crossing(i0, v0, i1, v1, asof_bar)
                pos_bars, neg_bars = compute_segment_bars(ext, i0, v0, i1, v1, asof_bar)
                touches = compute_touches(i0, v0, i1, v1, asof_bar, all_piv_idx, all_piv_val)
                total_cross, last_cross_bar, last_cross_dir = compute_cross_events(
                    ext, i0, v0, i1, v1, asof_bar)
                candidates.append({
                    "i0": i0, "v0": v0, "i1": i1, "v1": v1,
                    "slope": slope,
                    "anchor_type": anchor_type,
                    "proj_asof": proj_asof,
                    "signed_dist": signed_dist,
                    "zero_bar": zero_bar,
                    "pos_bars": pos_bars,
                    "neg_bars": neg_bars,
                    "touches": touches,
                    "last_cross_bar": last_cross_bar,
                    "last_cross_dir": last_cross_dir,
                    "total_cross": total_cross,
                    "span": asof_bar - i0,
                })

    add_pairs(peak_i, peak_v, "peak_anchored")
    add_pairs(trough_i, trough_v, "trough_anchored")
    return candidates


def rank_by_proximity(candidates, top_n=TOP_N):
    """Upper channel = peak-anchored ascending. Lower channel = trough-
    anchored ascending. Rank within each by |signed_dist| ascending."""
    upper = [c for c in candidates if c["anchor_type"] == "peak_anchored"]
    lower = [c for c in candidates if c["anchor_type"] == "trough_anchored"]
    upper.sort(key=lambda c: abs(c["signed_dist"]))
    lower.sort(key=lambda c: abs(c["signed_dist"]))
    return upper[:top_n], lower[:top_n]


def aggregates(candidates):
    total = len(candidates)
    desc = [c for c in candidates if c["slope"] < 0]
    asc = [c for c in candidates if c["slope"] > 0]
    nearest_desc = min((abs(c["signed_dist"]) for c in desc), default=float("nan"))
    nearest_asc = min((abs(c["signed_dist"]) for c in asc), default=float("nan"))
    return {
        "total_candidates": total,
        "count_ascending": len(asc),
        "count_descending": len(desc),
        "nearest_descending_dist": nearest_desc,
        "nearest_ascending_dist": nearest_asc,
    }


def cascade_at(ext, asof_bar):
    window_start = max(0, asof_bar - WINDOW_BARS)
    prof = derive_profile(ext, asof_bar)
    u1, d1 = prof["upside_1"], prof["downside_1"]
    u2, d2 = prof["upside_2"], prof["downside_2"]
    cu = prof["chop_upper"]
    candidates = enumerate_candidates(ext, asof_bar, window_start, u1, d1, u2, d2, cu)
    above, below = rank_by_proximity(candidates)
    agg = aggregates(candidates)
    agg.update({"upside_1": u1, "upside_2": u2, "chop_upper": cu})
    return above, below, agg, candidates


def draw_asof(ticker, dates, ext, asof_date_str):
    asof_dt = np.datetime64(asof_date_str)
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    if asof_bar < 0 or asof_bar >= len(dates):
        print(f"{ticker} {asof_date_str}: out of range")
        return

    upper, lower, agg, all_cands = cascade_at(ext, asof_bar)

    start_bar = max(0, asof_bar - WINDOW_BARS)
    vis_ext = ext[start_bar: asof_bar + 1]
    vis_dates = dates[start_bar: asof_bar + 1]
    mask_nn = ~np.isnan(vis_ext)

    fig, ax = plt.subplots(figsize=(16, 5.5))
    ax.plot(vis_dates[mask_nn], vis_ext[mask_nn], color="#333", lw=1.1, zorder=2)
    ax.fill_between(vis_dates[mask_nn], vis_ext[mask_nn], 0,
                    where=vis_ext[mask_nn] >= 0, color="#4a9", alpha=0.15, zorder=1)
    ax.fill_between(vis_dates[mask_nn], vis_ext[mask_nn], 0,
                    where=vis_ext[mask_nn] < 0, color="#c64", alpha=0.15, zorder=1)
    ax.axhline(0, color="#333", lw=0.8)

    def draw(L, color, lw, label):
        i0, i1 = L["i0"], L["i1"]
        v0, v1 = L["v0"], L["v1"]
        ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, zorder=4,
                label=f"{label} ({L['touches']}t)")
        if asof_bar > i1:
            proj = line_projection(i0, v0, i1, v1, asof_bar)
            ax.plot([dates[i1], dates[asof_bar]], [v1, proj],
                    color=color, lw=lw, ls=":", alpha=0.7, zorder=3)
        ax.scatter([dates[i0], dates[i1]], [v0, v1], color=color, s=34,
                   zorder=6, edgecolor="#000", lw=0.5)

    upper_shades = ["#a00", "#d33", "#f66"]
    lower_shades = ["#00a", "#33d", "#66f"]
    for n, L in enumerate(upper):
        draw(L, upper_shades[n % len(upper_shades)], 2.0 - 0.3 * n, f"U{n + 1}")
    for n, L in enumerate(lower):
        draw(L, lower_shades[n % len(lower_shades)], 2.0 - 0.3 * n, f"L{n + 1}")

    ax.axvline(dates[asof_bar], color="#555", lw=0.9, ls="-.", alpha=0.6)
    ax.set_xlim(vis_dates[0], vis_dates[-1] + np.timedelta64(5, "D"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", alpha=0.2)
    ax.set_title(f"{ticker} 50-ext as of {str(dates[asof_bar])[:10]} — v7 momentum-channel "
                 f"(total={agg['total_candidates']} upper={len(upper)} lower={len(lower)})")
    ax.set_ylabel("ext (ADR)")
    if upper or lower:
        ax.legend(loc="upper right", fontsize=7)
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"ext50_v7_momentum_{ticker}_{asof_date_str}.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)

    def fmt(L, kind):
        return (f"  {kind}: {L['anchor_type']:<15s} "
                f"{L['v0']:+.2f}@{str(dates[L['i0']])[:7]} -> "
                f"{L['v1']:+.2f}@{str(dates[L['i1']])[:7]} "
                f"| slope={L['slope']:+.4f} | proj={L['proj_asof']:+.2f} "
                f"| dist={L['signed_dist']:+.2f} | {L['touches']}t")
    print(f"\n{ticker} asof {asof_date_str} -> {os.path.basename(out)}")
    print(f"  aggregates: {agg}")
    for n, L in enumerate(upper):
        print(fmt(L, f"U{n + 1}"))
    for n, L in enumerate(lower):
        print(fmt(L, f"L{n + 1}"))


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
