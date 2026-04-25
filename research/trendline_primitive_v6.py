"""v6 primitive: data-exposing trendline enumerator for 50-ext series.

Replaces v5's rule-encoding approach per 2026-04-23 architectural correction
(see memory: feedback_primitive_exposes_data_not_rules.md, project_extension_
trendline_flip_mechanic.md, project_extension_trendline_proximity_rule.md).

Design: enumerate every valid candidate line in the relevance window, then
expose 13 raw structural features per candidate. No hardcoded tangency gates,
no `is_terminal`/`is_flipped` booleans, no direction-picker, no proximity-only
output. The grinder derives semantic rules from the raw features downstream.

Per candidate line:
  1. Anchors (i0, v0, i1, v1)
  2. Slope (ADR per bar)
  3. Anchor type: peak_anchored / trough_anchored
  4. Projection at asof
  5. Signed distance from current ext at asof
  6. Zero-crossing bar (or -1 if none)
  7. Bars in positive-territory segment up to asof
  8. Bars in negative-territory segment up to asof
  9. Touch count (pivots within tolerance, i0..asof)
 10. Bar of last cross event (or -1 if none)
 11. Direction of last cross (+1 ext crossed up through line, -1 down through, 0 none)
 12. Total cross count over line's life up to asof
 13. Span (asof - i0)

Column collapse for the cache: top-3 nearest-above + top-3 nearest-below by
|signed distance| + aggregates = ~83 expressions.
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

WINDOW_BARS = 260           # relevance window ~12 months
ANCHOR_SLIDE_BARS = 5       # geometric anchor displacement to local extremum
PROMINENCE = 1.0            # ADR — pivot prominence on ext series
MIN_SPAN_BARS = 30          # geometric min anchor separation
MIN_VAL_RANGE = 0.0         # ADR — drop horizontal near-flat noise; 0 = keep all
TOUCH_TOL = 0.3             # ADR — touch distance tolerance
TOP_N = 3                   # top-N per side exposed as flat columns


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
    """Return two arrays of (idx, val) for peak anchors and trough anchors,
    both slid to local extrema within ±ANCHOR_SLIDE_BARS."""
    nn_up = np.nan_to_num(ext, nan=-1e9)
    nn_dn = -np.nan_to_num(ext, nan=1e9)
    peaks, _ = find_peaks(nn_up, prominence=PROMINENCE)
    troughs, _ = find_peaks(nn_dn, prominence=PROMINENCE)

    def prep(idxs, direction):
        idxs = idxs[(idxs <= asof_bar) & (idxs >= window_start)]
        slid = [slide_to_local_extremum(ext, int(i), direction) for i in idxs]
        if not slid:
            return np.zeros(0, dtype=int), np.zeros(0, dtype=float)
        # dedupe after slide (multiple pivots may slide to same bar)
        seen = {}
        for i, v in slid:
            seen[i] = v
        out = sorted(seen.items())
        return (np.array([i for i, _ in out], dtype=int),
                np.array([v for _, v in out], dtype=float))

    peak_i, peak_v = prep(peaks, "peak")
    trough_i, trough_v = prep(troughs, "trough")
    return peak_i, peak_v, trough_i, trough_v


def compute_cross_events(ext, i0, v0, i1, v1, asof_bar):
    """Enumerate cross events from i0 to asof_bar. A cross occurs on bar b
    when sign(ext[b] - proj(b)) flips vs previous non-NaN bar. Returns
    (total_count, last_cross_bar, last_cross_direction). Direction:
    +1 = ext crossed up through line (was below, now above),
    -1 = ext crossed down through line (was above, now below),
     0 = no cross."""
    if asof_bar <= i0:
        return 0, -1, 0
    idx = np.arange(i0, asof_bar + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0) if i1 != i0 else np.full_like(idx, v0, dtype=float)
    vals = ext[idx].astype(float)
    mask = ~np.isnan(vals)
    if mask.sum() < 2:
        return 0, -1, 0
    diff = vals - proj
    # sign of diff, ignoring NaN positions
    sign = np.where(np.isnan(diff), 0, np.sign(diff))
    # zero-diff bars count as "touching" — treat as no state change
    # detect transitions only between genuine +/- states
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
            last_dir = int(s)  # sign of new side = direction ext went
            prev_sign = s
    return total, last_bar, last_dir


def has_origination_side_break(ext, i0, v0, i1, v1, asof_bar):
    """Returns True if the ext series crosses the line projection while the
    projection is still on the same side of zero as v0 (origination side).
    Anchor touches (ext == proj exactly) do not count. Bar-sign zero
    projections (at the zero-crossing itself) are treated as boundary, not
    origination side."""
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
    on_origin_side = (proj_sign == origin_sign)
    prev_sign = 0
    for k in range(len(idx)):
        if not mask[k] or not on_origin_side[k]:
            # past the zero crossing or NaN bar: no longer on origin side
            if not on_origin_side[k] and k > 0 and on_origin_side[k - 1]:
                break  # left origination segment
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


def compute_zero_crossing(i0, v0, i1, v1, asof_bar):
    """Bar where line projection crosses y=0 within [i0, asof_bar].
    Returns -1 if line never crosses zero in that range (same sign throughout)
    or if slope is zero."""
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
    """Count bars where projection is ≥ 0 vs < 0 over [i0, asof_bar].
    Skips NaN ext bars (purely geometric — based on projection sign, not ext)."""
    if asof_bar <= i0:
        return 0, 0
    idx = np.arange(i0, asof_bar + 1)
    proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0) if i1 != i0 else np.full_like(idx, v0, dtype=float)
    pos_bars = int(np.sum(proj >= 0))
    neg_bars = int(np.sum(proj < 0))
    return pos_bars, neg_bars


def compute_touches(i0, v0, i1, v1, asof_bar, pivot_idx, pivot_val, tol=TOUCH_TOL):
    """Count pivots (from anchor pools) within tol of line projection,
    over (i0, asof_bar]. Both anchors themselves count as 2 baseline."""
    if len(pivot_idx) == 0:
        return 2  # both anchors
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


def spans_cycle_reset(ext, i0, i1, asof_bar, anchor_type, v0, v1,
                      upside_1, downside_1, upside_2, downside_2):
    """Cross-cycle / stale / overextension detection.

    Four checks per candidate (drop if ANY fires):
    - Anchor check: v1 must be on the descended side of cycle-top level
      (upper: v1 <= u1; lower: v1 >= d1).
    - Between-anchors cross-cycle: ext in [i0+1, i1-1] reaches the opposite
      reversal (upper: d1; lower: u1) = valley/top formed mid-cascade =
      different cycles.
    - Post-i1 staleness: ext in [i1+1, asof] reaches a new cycle top (u1)
      = cycle ended = line is stale.
    - Projection-in-range: proj_at_asof must stay within [d2, u2]; a
      projection beyond the extreme reversal levels is overextended.

    NaN-fallback: sign-mirror the known side. For lower cascade between +
    post-i1 both use u1, combined into a single [i0+1, asof] scan."""
    u1 = upside_1 if not np.isnan(upside_1) else (-downside_1 if not np.isnan(downside_1) else float("nan"))
    d1 = downside_1 if not np.isnan(downside_1) else (-upside_1 if not np.isnan(upside_1) else float("nan"))
    u2 = upside_2 if not np.isnan(upside_2) else (-downside_2 if not np.isnan(downside_2) else float("nan"))
    d2 = downside_2 if not np.isnan(downside_2) else (-upside_2 if not np.isnan(upside_2) else float("nan"))

    if i1 > i0:
        slope = (v1 - v0) / (i1 - i0)
        proj = v0 + slope * (asof_bar - i0)
        if not np.isnan(u2) and proj > u2:
            return True
        if not np.isnan(d2) and proj < d2:
            return True

    if anchor_type == "peak_anchored":
        if not np.isnan(u1) and v1 > u1:
            return True
        if not np.isnan(d1) and i1 > i0 + 1:
            segment = ext[i0 + 1 : i1]
            with np.errstate(invalid="ignore"):
                if np.any(segment <= d1):
                    return True
        if not np.isnan(u1) and asof_bar > i1 + 1:
            segment = ext[i1 + 1 : asof_bar + 1]
            with np.errstate(invalid="ignore"):
                if np.any(segment >= u1):
                    return True
    else:
        if not np.isnan(d1) and v1 < d1:
            return True
        if not np.isnan(u1) and asof_bar > i0 + 1:
            segment = ext[i0 + 1 : asof_bar + 1]
            with np.errstate(invalid="ignore"):
                if np.any(segment >= u1):
                    return True
    return False


def enumerate_candidates(ext, asof_bar, window_start,
                         upside_1=float("nan"), downside_1=float("nan"),
                         upside_2=float("nan"), downside_2=float("nan")):
    """Enumerate all valid candidate lines. Returns list of dicts with the
    13 per-line features plus raw anchor info."""
    peak_i, peak_v, trough_i, trough_v = find_anchors(ext, asof_bar, window_start)

    # unified pivot pool for touch counting — touches can come from either
    # pool along a line's life regardless of anchor type
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
                if MIN_VAL_RANGE > 0 and abs(v1 - v0) < MIN_VAL_RANGE:
                    continue
                if (asof_bar - i0) < MIN_SPAN_BARS:
                    continue
                slope = (v1 - v0) / (i1 - i0)
                # origin-sign ↔ slope-sign opposition
                if slope < 0 and v0 < 0:
                    continue
                if slope > 0 and v0 > 0:
                    continue
                # anchors must be on same side of zero
                s0, s1 = np.sign(v0), np.sign(v1)
                if s0 != 0 and s1 != 0 and s0 != s1:
                    continue
                if s0 == 0 or s1 == 0:
                    continue  # exact-zero anchors rejected (extremely rare)
                # anchor type must match slope direction
                if anchor_type == "peak_anchored" and slope >= 0:
                    continue
                if anchor_type == "trough_anchored" and slope <= 0:
                    continue
                # cross-cycle / staleness / overextension gate
                if spans_cycle_reset(ext, i0, i1, asof_bar, anchor_type, v0, v1,
                                     upside_1, downside_1, upside_2, downside_2):
                    continue
                # no break on origination side of zero
                if has_origination_side_break(ext, i0, v0, i1, v1, asof_bar):
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
    """Structural-pair split: peak-anchored = upper cascade (Contracting-Range
    upper line), trough-anchored = lower cascade (direction-agnostic). Rank
    within each by |signed_dist| ascending, return top-N each."""
    upper = [c for c in candidates if c["anchor_type"] == "peak_anchored"]
    lower = [c for c in candidates if c["anchor_type"] == "trough_anchored"]
    upper.sort(key=lambda c: abs(c["signed_dist"]))
    lower.sort(key=lambda c: abs(c["signed_dist"]))
    return upper[:top_n], lower[:top_n]


def aggregates(candidates):
    total = len(candidates)
    pos_territory = sum(1 for c in candidates if c["proj_asof"] >= 0)
    neg_territory = sum(1 for c in candidates if c["proj_asof"] < 0)
    desc = [c for c in candidates if c["slope"] < 0]
    asc = [c for c in candidates if c["slope"] > 0]
    nearest_desc = min((abs(c["signed_dist"]) for c in desc), default=float("nan"))
    nearest_asc = min((abs(c["signed_dist"]) for c in asc), default=float("nan"))
    return {
        "total_candidates": total,
        "count_pos_territory": pos_territory,
        "count_neg_territory": neg_territory,
        "nearest_descending_dist": nearest_desc,
        "nearest_ascending_dist": nearest_asc,
    }


def audit_rules(ext, L, asof_bar):
    """Re-check the three structural rules on an emitted line. Returns list
    of violation strings (empty if clean)."""
    violations = []
    i0, v0, i1, v1 = L["i0"], L["v0"], L["i1"], L["v1"]
    slope = L["slope"]
    if slope > 0 and v0 > 0:
        violations.append(f"RULE1 upsloping but v0={v0:+.3f} > 0")
    if slope < 0 and v0 < 0:
        violations.append(f"RULE2 downsloping but v0={v0:+.3f} < 0")
    # rule 3 — walk bars on origin side and list every sign flip
    if v0 != 0:
        idx = np.arange(i0, asof_bar + 1)
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0) if i1 != i0 else np.full(len(idx), v0, dtype=float)
        vals = ext[idx].astype(float)
        mask = ~np.isnan(vals)
        diff = vals - proj
        sign_arr = np.where(np.isnan(diff), 0, np.sign(diff))
        origin_sign = np.sign(v0)
        proj_sign = np.sign(proj)
        on_origin = (proj_sign == origin_sign)
        flips = []
        prev_sign = 0
        for k in range(len(idx)):
            if not mask[k] or not on_origin[k]:
                if not on_origin[k] and k > 0 and on_origin[k - 1]:
                    break
                continue
            s = sign_arr[k]
            if s == 0:
                continue
            if prev_sign == 0:
                prev_sign = s
                continue
            if s != prev_sign:
                flips.append(int(idx[k]))
                prev_sign = s
        if flips:
            violations.append(f"RULE3 origin-side flips at bars {flips[:5]}"
                              + (f" (+{len(flips) - 5} more)" if len(flips) > 5 else ""))
    return violations


def cascade_at(ext, asof_bar):
    window_start = max(0, asof_bar - WINDOW_BARS)
    prof = derive_profile(ext, asof_bar)
    u1, d1 = prof["upside_1"], prof["downside_1"]
    u2, d2 = prof["upside_2"], prof["downside_2"]
    candidates = enumerate_candidates(ext, asof_bar, window_start, u1, d1, u2, d2)
    above, below = rank_by_proximity(candidates)
    agg = aggregates(candidates)
    agg.update({"upside_1": u1, "downside_1": d1, "upside_2": u2, "downside_2": d2})
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

    # line-chart rendering (no histogram) per 2026-04-23 calibration convention
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
        # full projection from i0 to asof
        ax.plot([dates[i0], dates[i1]], [v0, v1], color=color, lw=lw, zorder=4,
                label=f"{label} ({L['touches']}t, {L['total_cross']}x)")
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
    ax.set_title(f"{ticker} 50-ext as of {str(dates[asof_bar])[:10]} — v6 "
                 f"(total={agg['total_candidates']} upper={len(upper)} lower={len(lower)})")
    ax.set_ylabel("ext (ADR)")
    ax.legend(loc="upper right", fontsize=7)
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"ext50_v6_{ticker}_{asof_date_str}.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)

    def fmt(L, kind):
        return (f"  {kind}: {L['anchor_type']:<15s} "
                f"{L['v0']:+.2f}@{str(dates[L['i0']])[:7]} -> "
                f"{L['v1']:+.2f}@{str(dates[L['i1']])[:7]} "
                f"| slope={L['slope']:+.4f} | proj={L['proj_asof']:+.2f} "
                f"| dist={L['signed_dist']:+.2f} | {L['touches']}t | "
                f"cross={L['total_cross']} (last bar={L['last_cross_bar']}, dir={L['last_cross_dir']:+d}) "
                f"| zero_bar={L['zero_bar']} | pos/neg={L['pos_bars']}/{L['neg_bars']}")
    print(f"\n{ticker} asof {asof_date_str} -> {os.path.basename(out)}")
    print(f"  aggregates: {agg}")
    for n, L in enumerate(upper):
        print(fmt(L, f"U{n + 1}"))
        vio = audit_rules(ext, L, asof_bar)
        for v in vio:
            print(f"     !! {v}")
    for n, L in enumerate(lower):
        print(fmt(L, f"L{n + 1}"))
        vio = audit_rules(ext, L, asof_bar)
        for v in vio:
            print(f"     !! {v}")


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
