"""Extension Chart Trendlines — D1 50-ext primitive.

Spec: EXPRESSION_ENGINE_V2.md §6 Extension Chart Trendlines.
Authoritative algorithm: research/trendline_primitive_v7_momentum.py.

Port-time corrections (vs research):
  1. Drop WINDOW_BARS — full ext cache history is the lookback (Dan, 2026-04-24).
  2. find_peaks runs on `ext[:asof_bar+1]` only — historical immutability.
  3. Drop dead `slide_to_local_extremum` and ANCHOR_SLIDE_BARS.
  4. Rename to drop `momentum` residue.

The primitive is path-pure: the snapshot at bar t is a function of
`ext[:t+1]` and Levels at bar t. Forward-prop calls the same function at
the new bar — no carried state.

D1 only. ext50 only. Total emitted: 16 numeric × 6 lines + 5 aggregates +
3 pass-throughs = 104 columns per bar.
"""
import numpy as np
from scipy.signal import find_peaks

# Spec constants (locked 2026-04-23). PROMINENCE 0.5 ADR is the micro-pivot
# threshold that picks up dips/peaks on a climb; MIN_SPAN_BARS 15 is geometric
# minimum line span; TOUCH_TOL 0.25 ADR is pivot-on-line tolerance for the
# touches feature; TOP_N 3 emits up to 3 lines per side.
PROMINENCE = 0.5
MIN_SPAN_BARS = 15
TOUCH_TOL = 0.25
TOP_N = 3

# Per-line emitted fields. anchors expand to 4 numeric columns; total 16.
PER_LINE_FIELDS = (
    "anchor_i0", "anchor_v0", "anchor_i1", "anchor_v1",
    "slope", "anchor_type", "proj_asof", "signed_dist",
    "zero_bar", "pos_bars", "neg_bars", "touches",
    "last_cross_bar", "last_cross_dir", "total_cross", "span",
)
N_PER_LINE = len(PER_LINE_FIELDS)  # 16

# anchor_type encoding (numeric for cache storage)
ANCHOR_TYPE_PEAK = 0.0       # peak_anchored = descending (upper cascade)
ANCHOR_TYPE_TROUGH = 1.0     # trough_anchored = ascending (lower cascade)

AGGREGATE_NAMES = (
    "total_candidates", "count_descending", "count_ascending",
    "nearest_descending_dist", "nearest_ascending_dist",
)
PASSTHROUGH_NAMES = ("upside_1", "upside_2", "chop_upper")
N_AGGREGATES = len(AGGREGATE_NAMES)
N_PASSTHROUGHS = len(PASSTHROUGH_NAMES)
N_SLOTS = 2 * TOP_N  # 6 (3 U + 3 L)
N_TOTAL = N_SLOTS * N_PER_LINE + N_AGGREGATES + N_PASSTHROUGHS  # 96 + 5 + 3 = 104

# Levels columns the primitive reads at bar t (used for gate 7 + pass-through)
LEVELS_INPUT_COLS = (
    "ext_avgc50_adr14_upside_1", "ext_avgc50_adr14_upside_2",
    "ext_avgc50_adr14_downside_1", "ext_avgc50_adr14_downside_2",
    "ext_avgc50_adr14_chop_upper",
)


def get_ext50_trendline_expression_names():
    """Return the 104 ordered column names. Same order across all consumers."""
    names = []
    for slot_kind in ("u", "l"):
        for rank in (1, 2, 3):
            for f in PER_LINE_FIELDS:
                names.append(f"ext50_tl_{slot_kind}{rank}_{f}")
    for a in AGGREGATE_NAMES:
        names.append(f"ext50_tl_{a}")
    for p in PASSTHROUGH_NAMES:
        names.append(f"ext50_tl_{p}")
    assert len(names) == N_TOTAL
    return names


# ─── Per-bar geometry helpers (mirror research; numpy where trivial) ────

def line_projection(i0, v0, i1, v1, i):
    if i1 == i0:
        return v0
    return v0 + (v1 - v0) * (i - i0) / (i1 - i0)


def _has_line_break(ext, i0, v0, i1, v1, asof_bar):
    """Sign change of (ext - projection) over [i0, asof_bar]."""
    if asof_bar <= i0:
        return False
    idx = np.arange(i0, asof_bar + 1)
    if i1 == i0:
        proj = np.full(len(idx), v0, dtype=np.float64)
    else:
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx].astype(np.float64)
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


def _has_origination_side_break(ext, i0, v0, i1, v1, asof_bar):
    """Sign change of (ext - projection) restricted to bars where the projection
    is on the same side of zero as v0. Stops at the first bar where projection
    leaves the origin side (after at least one origin-side bar). Used for
    descending lines — pokes in the post-zero hill segment are tolerated."""
    if v0 == 0 or asof_bar <= i0:
        return False
    idx = np.arange(i0, asof_bar + 1)
    if i1 == i0:
        proj = np.full(len(idx), v0, dtype=np.float64)
    else:
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx].astype(np.float64)
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
            if (not on_origin[k]) and (k > 0) and on_origin[k - 1]:
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


def _compute_cross_events(ext, i0, v0, i1, v1, asof_bar):
    """Return (total_cross, last_cross_bar, last_cross_dir) over [i0, asof_bar]."""
    if asof_bar <= i0:
        return 0, -1, 0
    idx = np.arange(i0, asof_bar + 1)
    if i1 == i0:
        proj = np.full(len(idx), v0, dtype=np.float64)
    else:
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    vals = ext[idx].astype(np.float64)
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


def _compute_zero_crossing(i0, v0, i1, v1, asof_bar):
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


def _compute_segment_bars(i0, v0, i1, v1, asof_bar):
    if asof_bar <= i0:
        return 0, 0
    idx = np.arange(i0, asof_bar + 1)
    if i1 == i0:
        proj = np.full(len(idx), v0, dtype=np.float64)
    else:
        proj = v0 + (v1 - v0) * (idx - i0) / (i1 - i0)
    pos_bars = int(np.sum(proj >= 0))
    neg_bars = int(np.sum(proj < 0))
    return pos_bars, neg_bars


def _compute_touches(i0, v0, i1, v1, asof_bar, pivot_idx, pivot_val, tol=TOUCH_TOL):
    """Touch count: 2 anchor touches + intermediate pivots within tol of projection."""
    if len(pivot_idx) == 0:
        return 2
    mask = (pivot_idx > i0) & (pivot_idx <= asof_bar) & (pivot_idx != i1)
    if not mask.any():
        return 2
    pis = pivot_idx[mask]
    pvs = pivot_val[mask]
    if i1 == i0:
        proj = np.full_like(pis, v0, dtype=np.float64)
    else:
        proj = v0 + (v1 - v0) * (pis - i0) / (i1 - i0)
    hits = int(np.sum(np.abs(pvs - proj) <= tol))
    return 2 + hits


def _find_anchors(ext, asof_bar):
    """Pivots at PROMINENCE over [0, asof_bar]. Port-time fix: history slice
    only — research file feeds the full ext array and lets future bars
    influence past peak detection, breaking historical immutability."""
    history = ext[:asof_bar + 1]
    nn_up = np.nan_to_num(history, nan=-1e9)
    nn_dn = -np.nan_to_num(history, nan=1e9)
    peaks, _ = find_peaks(nn_up, prominence=PROMINENCE)
    troughs, _ = find_peaks(nn_dn, prominence=PROMINENCE)
    peaks = peaks.astype(int)
    troughs = troughs.astype(int)
    return peaks, history[peaks].astype(np.float64), troughs, history[troughs].astype(np.float64)


def _enumerate_candidates(ext, asof_bar, levels):
    """Run all gates; return list of candidate dicts (post-gate, pre-rank)."""
    peak_i, peak_v, trough_i, trough_v = _find_anchors(ext, asof_bar)

    if (len(peak_i) + len(trough_i)) > 0:
        all_piv_idx = np.concatenate([peak_i, trough_i])
        all_piv_val = np.concatenate([peak_v, trough_v])
        order = np.argsort(all_piv_idx)
        all_piv_idx = all_piv_idx[order]
        all_piv_val = all_piv_val[order]
    else:
        all_piv_idx = np.zeros(0, dtype=int)
        all_piv_val = np.zeros(0, dtype=np.float64)

    ext_asof = float(ext[asof_bar]) if not np.isnan(ext[asof_bar]) else 0.0
    upside_2 = levels.get("upside_2", float("nan"))
    downside_2 = levels.get("downside_2", float("nan"))

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
                # Origin-sign opposition
                if slope < 0 and v0 < 0:
                    continue
                if slope > 0 and v0 > 0:
                    continue
                # Same-side anchors
                s0 = np.sign(v0)
                s1 = np.sign(v1)
                if s0 == 0 or s1 == 0:
                    continue
                if s0 != s1:
                    continue
                # Anchor-type ↔ slope direction
                if anchor_type == "peak_anchored" and slope >= 0:
                    continue
                if anchor_type == "trough_anchored" and slope <= 0:
                    continue
                # Asymmetric break check
                if slope < 0:
                    if _has_origination_side_break(ext, i0, v0, i1, v1, asof_bar):
                        continue
                else:
                    if _has_line_break(ext, i0, v0, i1, v1, asof_bar):
                        continue
                # Projection-in-range (NaN pass-through)
                proj_asof = line_projection(i0, v0, i1, v1, asof_bar)
                if not np.isnan(upside_2) and proj_asof > upside_2:
                    continue
                if not np.isnan(downside_2) and proj_asof < downside_2:
                    continue

                signed_dist = proj_asof - ext_asof
                zero_bar = _compute_zero_crossing(i0, v0, i1, v1, asof_bar)
                pos_bars, neg_bars = _compute_segment_bars(i0, v0, i1, v1, asof_bar)
                touches = _compute_touches(i0, v0, i1, v1, asof_bar, all_piv_idx, all_piv_val)
                total_cross, last_cross_bar, last_cross_dir = _compute_cross_events(
                    ext, i0, v0, i1, v1, asof_bar)
                candidates.append({
                    "i0": i0, "v0": v0, "i1": i1, "v1": v1,
                    "slope": slope, "anchor_type": anchor_type,
                    "proj_asof": proj_asof, "signed_dist": signed_dist,
                    "zero_bar": zero_bar, "pos_bars": pos_bars,
                    "neg_bars": neg_bars, "touches": touches,
                    "last_cross_bar": last_cross_bar, "last_cross_dir": last_cross_dir,
                    "total_cross": total_cross, "span": asof_bar - i0,
                })

    add_pairs(peak_i, peak_v, "peak_anchored")
    add_pairs(trough_i, trough_v, "trough_anchored")
    return candidates


def _rank_and_aggregate(candidates):
    """Top-3 per side + 5 aggregates."""
    upper = sorted([c for c in candidates if c["anchor_type"] == "peak_anchored"],
                    key=lambda c: abs(c["signed_dist"]))[:TOP_N]
    lower = sorted([c for c in candidates if c["anchor_type"] == "trough_anchored"],
                    key=lambda c: abs(c["signed_dist"]))[:TOP_N]
    desc = [c for c in candidates if c["slope"] < 0]
    asc = [c for c in candidates if c["slope"] > 0]
    nearest_desc = min((abs(c["signed_dist"]) for c in desc), default=float("nan"))
    nearest_asc = min((abs(c["signed_dist"]) for c in asc), default=float("nan"))
    aggregates = {
        "total_candidates": len(candidates),
        "count_descending": len(desc),
        "count_ascending": len(asc),
        "nearest_descending_dist": float(nearest_desc) if not np.isnan(nearest_desc) else float("nan"),
        "nearest_ascending_dist": float(nearest_asc) if not np.isnan(nearest_asc) else float("nan"),
    }
    return upper, lower, aggregates


def cascade_at(ext, asof_bar, levels):
    """Single-bar snapshot. Public — used by PNG render and forward-prop step.

    Args:
        ext: 1D ndarray of ext50 values (full ticker history).
        asof_bar: bar index to compute snapshot for.
        levels: dict with keys upside_1, upside_2, downside_1, downside_2,
                chop_upper — each a scalar value AT bar asof_bar.

    Returns dict {upper, lower, aggregates, all_candidates} where upper/lower
    are sorted lists of candidate dicts (top 3 per side).
    """
    if asof_bar < 1 or asof_bar >= len(ext):
        return {"upper": [], "lower": [],
                "aggregates": {"total_candidates": 0, "count_descending": 0,
                               "count_ascending": 0,
                               "nearest_descending_dist": float("nan"),
                               "nearest_ascending_dist": float("nan")},
                "all_candidates": []}
    candidates = _enumerate_candidates(ext, asof_bar, levels)
    upper, lower, aggregates = _rank_and_aggregate(candidates)
    return {"upper": upper, "lower": lower, "aggregates": aggregates,
            "all_candidates": candidates}


def _slot_to_vector(slot, vec_out, base_idx):
    """Pack one candidate dict into 16 consecutive entries of vec_out starting at base_idx.
    Empty slot (None) → all 16 NaN."""
    if slot is None:
        for i in range(N_PER_LINE):
            vec_out[base_idx + i] = float("nan")
        return
    vec_out[base_idx + 0] = float(slot["i0"])
    vec_out[base_idx + 1] = float(slot["v0"])
    vec_out[base_idx + 2] = float(slot["i1"])
    vec_out[base_idx + 3] = float(slot["v1"])
    vec_out[base_idx + 4] = float(slot["slope"])
    vec_out[base_idx + 5] = ANCHOR_TYPE_PEAK if slot["anchor_type"] == "peak_anchored" else ANCHOR_TYPE_TROUGH
    vec_out[base_idx + 6] = float(slot["proj_asof"])
    vec_out[base_idx + 7] = float(slot["signed_dist"])
    vec_out[base_idx + 8] = float(slot["zero_bar"])
    vec_out[base_idx + 9] = float(slot["pos_bars"])
    vec_out[base_idx + 10] = float(slot["neg_bars"])
    vec_out[base_idx + 11] = float(slot["touches"])
    vec_out[base_idx + 12] = float(slot["last_cross_bar"])
    vec_out[base_idx + 13] = float(slot["last_cross_dir"])
    vec_out[base_idx + 14] = float(slot["total_cross"])
    vec_out[base_idx + 15] = float(slot["span"])


def snapshot_to_vector(snap, levels):
    """Pack a cascade_at result + Levels at this bar into a 104-element vector.

    Order matches get_ext50_trendline_expression_names().
    """
    vec = np.full(N_TOTAL, np.nan, dtype=np.float64)
    upper = (snap["upper"] + [None, None, None])[:TOP_N]
    lower = (snap["lower"] + [None, None, None])[:TOP_N]
    base = 0
    for slot in upper:
        _slot_to_vector(slot, vec, base)
        base += N_PER_LINE
    for slot in lower:
        _slot_to_vector(slot, vec, base)
        base += N_PER_LINE
    agg = snap["aggregates"]
    for i, name in enumerate(AGGREGATE_NAMES):
        vec[base + i] = float(agg[name])
    base += N_AGGREGATES
    vec[base + 0] = float(levels.get("upside_1", float("nan")))
    vec[base + 1] = float(levels.get("upside_2", float("nan")))
    vec[base + 2] = float(levels.get("chop_upper", float("nan")))
    return vec


def bootstrap_ext50_trendline_state(ext_series):
    """One-time setup: capture the full ext50 history at the .npz end-bar.

    Forward-prop is path-pure (no per-pair state to roll), but it does need
    the full ext history to run find_peaks at the new bar. Storing the
    ext50 series in .state avoids loading the full .npz on every nightly
    append. Variable-length list of float64 values (NaN-safe).
    """
    vals = np.asarray(ext_series, dtype=np.float64)
    return {"ext50_history": [float(v) for v in vals]}


def step_ext50_trendline_one_bar(prior_state, new_ext_value, levels_at_new_bar):
    """Forward-prop one bar.

    Args:
        prior_state: dict with key 'ext50_history' (list of float values up
                     through the bar before today). Empty {} treated as no history.
        new_ext_value: today's ext50 value (scalar).
        levels_at_new_bar: dict with the 5 Levels constants AT today's bar
                           (read from the in-memory expr_row after Phase 5d
                           reversal_profile fills them).

    Returns:
        (vec, new_state) where vec is the 104-element snapshot vector
        and new_state has the appended ext history.
    """
    history = list(prior_state.get("ext50_history", []))
    history.append(float(new_ext_value))
    ext = np.asarray(history, dtype=np.float64)
    asof_bar = len(ext) - 1
    snap = cascade_at(ext, asof_bar, levels_at_new_bar)
    vec = snapshot_to_vector(snap, levels_at_new_bar)
    return vec, {"ext50_history": history}


def _precompute_pair(ext, i0, v0, i1, v1, slope, anchor_type,
                       all_piv_idx, all_piv_val, n_bars,
                       t_appear_combined=None):
    """Precompute per-pair cumulative arrays once.

    Length of arrays = n_bars - i0 (covering bars [i0, n_bars-1]).
    Per-bar emission later indexes via local_t = t - i0.

    Returns dict with: i0, v0, i1, v1, slope, anchor_type, proj,
    first_break_bar (local), zero_bar_global (absolute or -1),
    last_cross_bar_arr (absolute), last_cross_dir_arr, total_cross_arr,
    pos_bars_arr, neg_bars_arr, touches_arr.
    """
    bar_range = np.arange(i0, n_bars)
    # Match cascade_at's float-arithmetic order exactly: helpers in cascade
    # compute proj as v0 + (v1-v0)*(idx-i0)/(i1-i0) per call. Using a
    # precomputed `slope` gives a different epsilon at the i1 anchor bar
    # (where mathematical diff is zero) and flips the sign there, causing
    # spurious sign-change detections.
    proj = v0 + (v1 - v0) * (bar_range - i0) / (i1 - i0)
    vals = ext[i0:n_bars].astype(np.float64)
    diff = vals - proj
    sign_int = np.sign(diff).astype(int)
    nan_mask = np.isnan(diff)
    sign_int[nan_mask] = 0

    # Cross events (cumulative)
    nz_mask = sign_int != 0
    valid_local_idx = np.flatnonzero(nz_mask)  # local indices (0..len-1) of nonzero signs
    n_local = len(bar_range)
    total_cross_arr = np.zeros(n_local, dtype=np.int64)
    last_cross_bar_arr = np.full(n_local, -1, dtype=np.int64)
    last_cross_dir_arr = np.zeros(n_local, dtype=np.int64)
    if len(valid_local_idx) >= 2:
        valid_signs = sign_int[valid_local_idx]
        change_mask = valid_signs[1:] != valid_signs[:-1]
        change_positions_in_valid = np.flatnonzero(change_mask) + 1
        if len(change_positions_in_valid) > 0:
            change_local_bars = valid_local_idx[change_positions_in_valid]
            change_dirs = valid_signs[change_positions_in_valid]
            change_abs_bars = change_local_bars + i0
            # For each local bar k, count of changes with local_bar <= k
            # change_local_bars is sorted ascending (valid_local_idx is sorted)
            local_bars_arr = np.arange(n_local)
            counts = np.searchsorted(change_local_bars, local_bars_arr, side="right")
            total_cross_arr = counts.astype(np.int64)
            has = counts > 0
            last_cross_bar_arr[has] = change_abs_bars[counts[has] - 1]
            last_cross_dir_arr[has] = change_dirs[counts[has] - 1]

    # First break bar (local index, -1 if none)
    first_break_bar = -1
    if slope < 0 and v0 != 0:
        # Origination-side scan only; stops at first off_origin transition after
        # at least one on_origin bar.
        origin_sign = np.sign(v0)
        proj_sign_arr = np.sign(proj)
        on_origin = (proj_sign_arr == origin_sign)
        if on_origin[0]:
            transitions_off = np.flatnonzero((~on_origin[1:]) & on_origin[:-1])
            stop_idx = (int(transitions_off[0]) + 1) if len(transitions_off) > 0 else n_local
            scan_mask = on_origin[:stop_idx] & (sign_int[:stop_idx] != 0)
            scan_idx = np.flatnonzero(scan_mask)
            if len(scan_idx) >= 2:
                scan_signs = sign_int[scan_idx]
                cm = scan_signs[1:] != scan_signs[:-1]
                if cm.any():
                    pos_in_scan = int(np.argmax(cm)) + 1
                    first_break_bar = int(scan_idx[pos_in_scan])
        # else: never enters origin region → first_break_bar stays -1
    elif slope > 0:
        # Whole-life scan
        if len(valid_local_idx) >= 2:
            v_signs = sign_int[valid_local_idx]
            cm = v_signs[1:] != v_signs[:-1]
            if cm.any():
                pos_in_valid = int(np.argmax(cm)) + 1
                first_break_bar = int(valid_local_idx[pos_in_valid])
    # zero-slope pairs were filtered out earlier (anchor-type↔slope gate)

    # pos_bars / neg_bars cumulative (proj sign at each bar)
    pos_inc = (proj >= 0).astype(np.int64)
    neg_inc = (proj < 0).astype(np.int64)
    pos_bars_arr = np.cumsum(pos_inc)
    neg_bars_arr = np.cumsum(neg_inc)

    # touches (cumulative count + 2 anchor touches). The cumulative count
    # at bar t includes hit pivots K' such that t_appear[K'] ≤ t — matches
    # cascade_at, which builds all_piv_idx from find_peaks(ext[:asof+1]).
    piv_mask = (all_piv_idx > i0) & (all_piv_idx != i1) & (all_piv_idx < n_bars)
    if piv_mask.any():
        piv_in = all_piv_idx[piv_mask]
        piv_in_v = all_piv_val[piv_mask]
        piv_proj = v0 + (v1 - v0) * (piv_in - i0) / (i1 - i0)
        hit_mask = np.abs(piv_in_v - piv_proj) <= TOUCH_TOL
        if hit_mask.any():
            hit_pivots = piv_in[hit_mask]
            if t_appear_combined is not None:
                hit_t_appear = np.array(
                    [t_appear_combined.get(int(p), n_bars) for p in hit_pivots],
                    dtype=np.int64,
                )
            else:
                # Fallback: assume immediately visible (legacy behavior; not bit-equivalent)
                hit_t_appear = (hit_pivots + 1).astype(np.int64)
            sort_order = np.argsort(hit_t_appear)
            sorted_t_appears = hit_t_appear[sort_order]
            local_bars_arr = np.arange(n_local)
            abs_bars = local_bars_arr + i0
            cumcount = np.searchsorted(sorted_t_appears, abs_bars, side="right")
            touches_arr = cumcount.astype(np.int64) + 2
        else:
            touches_arr = np.full(n_local, 2, dtype=np.int64)
    else:
        touches_arr = np.full(n_local, 2, dtype=np.int64)

    # zero_bar (absolute bar index where projection crosses zero, or -1 if outside [i0, n_bars))
    if v1 == v0:
        zero_bar_global = -1
    else:
        bar_zero = i0 - v0 / slope
        bar_zero_int = int(np.round(bar_zero))
        if bar_zero_int < i0 or bar_zero_int >= n_bars:
            zero_bar_global = -1
        else:
            zero_bar_global = bar_zero_int

    return {
        "i0": i0, "v0": v0, "i1": i1, "v1": v1, "slope": slope,
        "anchor_type": anchor_type,
        "proj": proj,
        "first_break_bar": first_break_bar,
        "zero_bar_global": zero_bar_global,
        "last_cross_bar_arr": last_cross_bar_arr,
        "last_cross_dir_arr": last_cross_dir_arr,
        "total_cross_arr": total_cross_arr,
        "pos_bars_arr": pos_bars_arr,
        "neg_bars_arr": neg_bars_arr,
        "touches_arr": touches_arr,
    }


def _compute_t_appear(ext, n_bars, full_peaks, full_troughs, prominence):
    """Smallest t such that each pivot is in find_peaks(ext[:t+1]).

    Empirically (5 sandbox tickers) pivot detection is monotonic in t —
    once detected, never disappears. Single forward pass detects each
    pivot's first appearance. Loop bails out as soon as all pivots have
    been seen (typically well before n_bars).
    """
    nn_up = np.nan_to_num(ext, nan=-1e9)
    nn_dn = -np.nan_to_num(ext, nan=1e9)
    remaining_peaks = set(int(p) for p in full_peaks)
    remaining_troughs = set(int(p) for p in full_troughs)
    t_appear_peak = {}
    t_appear_trough = {}
    for t in range(1, n_bars):
        if not remaining_peaks and not remaining_troughs:
            break
        if remaining_peaks:
            peaks_t, _ = find_peaks(nn_up[:t + 1], prominence=prominence)
            for p in peaks_t:
                p_int = int(p)
                if p_int in remaining_peaks:
                    t_appear_peak[p_int] = t
                    remaining_peaks.discard(p_int)
        if remaining_troughs:
            troughs_t, _ = find_peaks(nn_dn[:t + 1], prominence=prominence)
            for p in troughs_t:
                p_int = int(p)
                if p_int in remaining_troughs:
                    t_appear_trough[p_int] = t
                    remaining_troughs.discard(p_int)
    return t_appear_peak, t_appear_trough


def compute_all_ext50_trendline_series(ext, levels_at_bar):
    """Fully vectorized full-walk: 2D matrices [pair × bar].

    Output bit-equivalent to cascade_at at every bar (modulo float64 noise
    from operation reordering — kept under 1e-9 by mirroring cascade's
    proj formula `v0 + (v1-v0)*(t-i0)/(i1-i0)` exactly).

    Per-ticker target: ≤0.5s. The slow per-pair-loop version is preserved
    as `compute_all_ext50_trendline_series_per_pair_loop` for parity tests.
    """
    n_bars = len(ext)
    out = np.full((n_bars, N_TOTAL), np.nan, dtype=np.float64)
    names = get_ext50_trendline_expression_names()
    if n_bars < 2:
        return {names[j]: out[:, j] for j in range(N_TOTAL)}

    # --- Step 1: pivots over full history + t_appear -----------------
    nn_up = np.nan_to_num(ext, nan=-1e9)
    nn_dn = -np.nan_to_num(ext, nan=1e9)
    full_peaks, _ = find_peaks(nn_up, prominence=PROMINENCE)
    full_troughs, _ = find_peaks(nn_dn, prominence=PROMINENCE)
    full_peaks = full_peaks.astype(int)
    full_troughs = full_troughs.astype(int)
    t_appear_peak, t_appear_trough = _compute_t_appear(
        ext, n_bars, full_peaks, full_troughs, PROMINENCE,
    )
    t_appear_combined = dict(t_appear_peak)
    t_appear_combined.update(t_appear_trough)

    # Combined pivot list (for touches calc)
    if (len(full_peaks) + len(full_troughs)) > 0:
        all_piv_idx_init = np.concatenate([full_peaks, full_troughs])
        all_piv_val_init = np.concatenate([ext[full_peaks], ext[full_troughs]])
        order = np.argsort(all_piv_idx_init)
        all_piv_idx = all_piv_idx_init[order]
        all_piv_val = all_piv_val_init[order]
        all_piv_t_appear = np.array(
            [t_appear_combined.get(int(p), n_bars) for p in all_piv_idx],
            dtype=np.int64,
        )
    else:
        all_piv_idx = np.zeros(0, dtype=int)
        all_piv_val = np.zeros(0, dtype=np.float64)
        all_piv_t_appear = np.zeros(0, dtype=np.int64)

    # --- Step 2: enumerate survivors as flat arrays ------------------
    pair_i0 = []; pair_i1 = []; pair_v0 = []; pair_v1 = []
    pair_slope = []; pair_atype = []; pair_visible_from = []
    for anchors, atype_int, t_appear_map in (
        (full_peaks, ANCHOR_TYPE_PEAK, t_appear_peak),
        (full_troughs, ANCHOR_TYPE_TROUGH, t_appear_trough),
    ):
        if len(anchors) < 2:
            continue
        for a in range(len(anchors)):
            i0 = int(anchors[a])
            v0 = float(ext[i0])
            for b in range(a + 1, len(anchors)):
                i1 = int(anchors[b])
                if i1 - i0 < MIN_SPAN_BARS:
                    continue
                v1 = float(ext[i1])
                slope = (v1 - v0) / (i1 - i0)
                if slope < 0 and v0 < 0:
                    continue
                if slope > 0 and v0 > 0:
                    continue
                if v0 == 0 or v1 == 0 or (v0 > 0) != (v1 > 0):
                    continue
                if atype_int == ANCHOR_TYPE_PEAK and slope >= 0:
                    continue
                if atype_int == ANCHOR_TYPE_TROUGH and slope <= 0:
                    continue
                vf = max(t_appear_map.get(i0, n_bars), t_appear_map.get(i1, n_bars))
                pair_i0.append(i0); pair_i1.append(i1)
                pair_v0.append(v0); pair_v1.append(v1)
                pair_slope.append(slope); pair_atype.append(atype_int)
                pair_visible_from.append(vf)

    P = len(pair_i0)
    if P == 0:
        # Pass-through Levels still emitted at every bar
        upside_1_arr = np.asarray(levels_at_bar.get("upside_1", np.full(n_bars, np.nan)), dtype=np.float64)
        upside_2_arr = np.asarray(levels_at_bar.get("upside_2", np.full(n_bars, np.nan)), dtype=np.float64)
        chop_upper_arr = np.asarray(levels_at_bar.get("chop_upper", np.full(n_bars, np.nan)), dtype=np.float64)
        comp_base = N_SLOTS * N_PER_LINE
        out[1:, comp_base + 0] = 0  # total_candidates
        out[1:, comp_base + 1] = 0  # count_descending
        out[1:, comp_base + 2] = 0  # count_ascending
        # nearest_*_dist stay NaN
        out[1:, comp_base + N_AGGREGATES + 0] = upside_1_arr[1:]
        out[1:, comp_base + N_AGGREGATES + 1] = upside_2_arr[1:]
        out[1:, comp_base + N_AGGREGATES + 2] = chop_upper_arr[1:]
        return {names[j]: out[:, j] for j in range(N_TOTAL)}

    pair_i0 = np.array(pair_i0, dtype=np.int64)
    pair_i1 = np.array(pair_i1, dtype=np.int64)
    pair_v0 = np.array(pair_v0, dtype=np.float64)
    pair_v1 = np.array(pair_v1, dtype=np.float64)
    pair_slope = np.array(pair_slope, dtype=np.float64)
    pair_atype = np.array(pair_atype, dtype=np.float64)
    pair_visible_from = np.array(pair_visible_from, dtype=np.int64)

    # --- Step 3: 2D proj matrix [P, n_bars] (NaN before i0) ---------
    bars_2d = np.arange(n_bars, dtype=np.float64)[None, :]  # (1, N)
    i0_col = pair_i0[:, None].astype(np.float64)             # (P, 1)
    i1_minus_i0 = (pair_i1 - pair_i0)[:, None].astype(np.float64)  # (P, 1)
    v0_col = pair_v0[:, None]; v1_col = pair_v1[:, None]
    proj = v0_col + (v1_col - v0_col) * (bars_2d - i0_col) / i1_minus_i0
    # Mask bars before i0 to NaN
    valid_bar_mask = bars_2d >= i0_col  # (P, N) bool
    proj = np.where(valid_bar_mask, proj, np.nan)

    # --- Step 4: diff and sign ----------------------------------------
    ext_2d = ext[None, :]
    diff = ext_2d - proj  # (P, N)
    sign_int = np.zeros_like(diff, dtype=np.int8)
    pos_mask = diff > 0
    neg_mask = diff < 0
    sign_int[pos_mask] = 1
    sign_int[neg_mask] = -1
    # Bars before i0 get sign 0 (already; diff is NaN → > 0 is False, < 0 is False)

    # --- Step 5: cumulative cross arrays ([P, N]) ---------------------
    # For each pair, scan signs in order. A "cross event" at bar k is when
    # sign_int[k] != 0 AND the most recent prior nonzero sign exists AND differs.
    # Implement via per-pair forward fill of last nonzero sign, then compare.
    # Vectorize across pairs: use cumulative argmax-of-(nonzero) trick.
    #
    # Simpler and fast: use np.maximum.accumulate on a "running last nonzero index"
    # per pair (axis=1).
    nz_mask = sign_int != 0  # (P, N)
    # For each (p, k), running last index where nz_mask was True at or before k
    # (or -1 if none). Use cumulative max of the index where nonzero.
    idx_when_nz = np.where(nz_mask, np.arange(n_bars, dtype=np.int64)[None, :], -1)
    last_nz_idx = np.maximum.accumulate(idx_when_nz, axis=1)  # (P, N)
    # Sign at last nonzero index per (p, k)
    has_prev = last_nz_idx >= 0
    last_nz_sign = np.zeros_like(sign_int)
    rows = np.arange(P)[:, None]
    cols = np.where(has_prev, last_nz_idx, 0)
    last_nz_sign = np.where(has_prev, sign_int[rows, cols], 0)
    # PRIOR last nonzero (excluding current bar): shift by 1 along axis 1
    prior_last_nz_sign = np.concatenate(
        [np.zeros((P, 1), dtype=np.int8), last_nz_sign[:, :-1]], axis=1
    )
    # A cross event occurs at bar k when current sign != 0, prior_last != 0, and they differ
    cross_event = (sign_int != 0) & (prior_last_nz_sign != 0) & (sign_int != prior_last_nz_sign)
    # total_cross_at_bar [P, N]: cumulative cross events up to (and including) bar k
    total_cross_2d = np.cumsum(cross_event, axis=1, dtype=np.int64)
    # last_cross_bar: index k where most recent cross_event happened, -1 if none
    cross_idx_at_bar = np.where(cross_event, np.arange(n_bars)[None, :], -1)
    last_cross_bar_2d = np.maximum.accumulate(cross_idx_at_bar, axis=1)
    # last_cross_dir: sign at last_cross_bar (if any), else 0
    has_cross = last_cross_bar_2d >= 0
    cols2 = np.where(has_cross, last_cross_bar_2d, 0)
    last_cross_dir_2d = np.where(has_cross, sign_int[rows, cols2], 0)

    # --- Step 6: pos/neg bars cumulative ------------------------------
    pos_inc = (proj >= 0).astype(np.int64)
    pos_inc[~valid_bar_mask] = 0
    neg_inc = (proj < 0).astype(np.int64)
    neg_inc[~valid_bar_mask] = 0
    pos_bars_2d = np.cumsum(pos_inc, axis=1)
    neg_bars_2d = np.cumsum(neg_inc, axis=1)

    # --- Step 7: first_break_bar per pair (LOCAL index from i0), vectorized.
    # For ascending lines (whole-life scan): use raw sign_int.
    # For descending lines (origin-side scan, stops at first off_origin
    # transition): for linear projections this equals "filter sign to 0 at
    # off_origin bars, then find first sign change". A linear descending
    # projection crosses zero exactly once and stays on the opposite side,
    # so once on_origin → False, it never returns to True. Filtering off_origin
    # bars to 0 cleanly restricts the cross-detection scan.
    origin_sign_per_pair = np.sign(pair_v0)
    proj_sign_2d = np.sign(proj)
    on_origin_2d = proj_sign_2d == origin_sign_per_pair[:, None]
    is_descending = pair_atype == ANCHOR_TYPE_PEAK  # (P,)

    scan_sign_2d = sign_int.copy()
    desc_idx_arr = np.flatnonzero(is_descending)
    if len(desc_idx_arr) > 0:
        scan_sign_2d[desc_idx_arr] = np.where(
            on_origin_2d[desc_idx_arr], sign_int[desc_idx_arr], 0
        )

    # Find first cross event per pair using the same maximum.accumulate trick
    nz_scan = scan_sign_2d != 0
    idx_when_nz_scan = np.where(nz_scan, np.arange(n_bars, dtype=np.int64)[None, :], -1)
    last_nz_idx_scan = np.maximum.accumulate(idx_when_nz_scan, axis=1)
    has_prev_scan = last_nz_idx_scan >= 0
    rows_p = np.arange(P)[:, None]
    cols_safe = np.where(has_prev_scan, last_nz_idx_scan, 0)
    last_nz_sign_scan = np.where(has_prev_scan, scan_sign_2d[rows_p, cols_safe], 0)
    prior_last_nz_sign_scan = np.concatenate(
        [np.zeros((P, 1), dtype=scan_sign_2d.dtype), last_nz_sign_scan[:, :-1]], axis=1
    )
    cross_event_scan = (
        (scan_sign_2d != 0)
        & (prior_last_nz_sign_scan != 0)
        & (scan_sign_2d != prior_last_nz_sign_scan)
    )
    # Mask out pre-i0 bars
    bars_arr_for_mask = np.arange(n_bars, dtype=np.int64)
    cross_event_scan[bars_arr_for_mask[None, :] < pair_i0[:, None]] = False

    any_cross = cross_event_scan.any(axis=1)
    first_cross_abs = np.where(any_cross, cross_event_scan.argmax(axis=1), n_bars)
    first_break_bar_local = np.where(any_cross, first_cross_abs - pair_i0, -1).astype(np.int64)

    # --- Step 7b: drop pairs that are never activatable ---------------
    # A pair can only be active at some bar t if there exists t with
    #   t >= visible_from AND local_t in [MIN_SPAN_BARS, first_break_bar)
    # ⇒ first_break_bar == -1 OR first_break_bar > MIN_SPAN_BARS
    # AND visible_from + MIN_SPAN_BARS <= n_bars (some t can satisfy both).
    activatable = (
        ((first_break_bar_local == -1) | (first_break_bar_local > MIN_SPAN_BARS))
        & (pair_i0 + MIN_SPAN_BARS < n_bars)
        & (pair_visible_from < n_bars)
    )
    if not activatable.all():
        keep = activatable
        pair_i0 = pair_i0[keep]; pair_i1 = pair_i1[keep]
        pair_v0 = pair_v0[keep]; pair_v1 = pair_v1[keep]
        pair_slope = pair_slope[keep]; pair_atype = pair_atype[keep]
        pair_visible_from = pair_visible_from[keep]
        first_break_bar_local = first_break_bar_local[keep]
        proj = proj[keep]
        sign_int = sign_int[keep]
        valid_bar_mask = valid_bar_mask[keep]
        on_origin_2d = on_origin_2d[keep]
        total_cross_2d = total_cross_2d[keep]
        last_cross_bar_2d = last_cross_bar_2d[keep]
        last_cross_dir_2d = last_cross_dir_2d[keep]
        pos_bars_2d = pos_bars_2d[keep]
        neg_bars_2d = neg_bars_2d[keep]
        is_descending = is_descending[keep]
        P = int(keep.sum())
    if P == 0:
        upside_1_arr = np.asarray(levels_at_bar.get("upside_1", np.full(n_bars, np.nan)), dtype=np.float64)
        upside_2_arr = np.asarray(levels_at_bar.get("upside_2", np.full(n_bars, np.nan)), dtype=np.float64)
        chop_upper_arr = np.asarray(levels_at_bar.get("chop_upper", np.full(n_bars, np.nan)), dtype=np.float64)
        comp_base = N_SLOTS * N_PER_LINE
        out[1:, comp_base + 0] = 0
        out[1:, comp_base + 1] = 0
        out[1:, comp_base + 2] = 0
        out[1:, comp_base + N_AGGREGATES + 0] = upside_1_arr[1:]
        out[1:, comp_base + N_AGGREGATES + 1] = upside_2_arr[1:]
        out[1:, comp_base + N_AGGREGATES + 2] = chop_upper_arr[1:]
        return {names[j]: out[:, j] for j in range(N_TOTAL)}

    # --- Step 8: zero_bar_global per pair (absolute bar, or -1) -------
    # bar_zero = i0 - v0/slope; round; valid if in [i0, n_bars).
    with np.errstate(divide="ignore", invalid="ignore"):
        bar_zero_real = pair_i0.astype(np.float64) - pair_v0 / pair_slope
    bar_zero_int = np.round(bar_zero_real).astype(np.int64)
    zero_bar_global = np.where(
        (bar_zero_int >= pair_i0) & (bar_zero_int < n_bars),
        bar_zero_int, -1
    )

    # --- Step 9: touches_2d cumulative -------------------------------
    # Vectorize hit detection across all (pair, pivot) pairs in one numpy
    # batch, then loop only over pairs that actually have ≥ 1 hit (typically
    # a small fraction). For non-hit pairs, touches stays at the 2-anchor baseline.
    touches_2d = np.full((P, n_bars), 2, dtype=np.int64)
    if len(all_piv_idx) > 0:
        # Filter: pivot index > i0 AND != i1 AND < n_bars (per pair).
        # Build a 2D mask [P, n_pivots] of "candidate pivots for this pair".
        n_piv = len(all_piv_idx)
        piv_idx_b = all_piv_idx[None, :]  # (1, K)
        cand_piv_mask = (piv_idx_b > pair_i0[:, None]) & \
                        (piv_idx_b != pair_i1[:, None]) & \
                        (piv_idx_b < n_bars)  # (P, K) bool
        # Vectorized projection: piv_proj[p, k] = v0[p] + (v1[p]-v0[p])*(piv_idx[k]-i0[p])/(i1[p]-i0[p])
        piv_proj_2d = pair_v0[:, None] + \
            (pair_v1[:, None] - pair_v0[:, None]) * \
            (piv_idx_b.astype(np.float64) - pair_i0[:, None].astype(np.float64)) / \
            (pair_i1[:, None] - pair_i0[:, None]).astype(np.float64)
        diff_2d = all_piv_val[None, :] - piv_proj_2d  # (P, K)
        hit_2d = cand_piv_mask & (np.abs(diff_2d) <= TOUCH_TOL)
        n_hits_per_pair = hit_2d.sum(axis=1)
        # Loop only over pairs that have hits — typically a small fraction
        bars_arr = np.arange(n_bars, dtype=np.int64)
        for p in np.flatnonzero(n_hits_per_pair > 0):
            hit_t_appear = all_piv_t_appear[hit_2d[p]]
            sorted_ta = np.sort(hit_t_appear)
            cumcount = np.searchsorted(sorted_ta, bars_arr, side="right")
            touches_2d[p] = cumcount + 2

    # --- Step 10: per-bar emission (fully vectorized across bars) ----
    upside_1_arr = np.asarray(levels_at_bar.get("upside_1", np.full(n_bars, np.nan)), dtype=np.float64)
    upside_2_arr = np.asarray(levels_at_bar.get("upside_2", np.full(n_bars, np.nan)), dtype=np.float64)
    downside_2_arr = np.asarray(levels_at_bar.get("downside_2", np.full(n_bars, np.nan)), dtype=np.float64)
    chop_upper_arr = np.asarray(levels_at_bar.get("chop_upper", np.full(n_bars, np.nan)), dtype=np.float64)

    bars_int = np.arange(n_bars, dtype=np.int64)
    # Build 2D candidate mask [P, N]
    local_t_2d = bars_int[None, :] - pair_i0[:, None]  # (P, N)
    in_span_2d = (local_t_2d >= MIN_SPAN_BARS) & (local_t_2d < (n_bars - pair_i0[:, None]))
    visible_2d = bars_int[None, :] >= pair_visible_from[:, None]
    not_broken_2d = (first_break_bar_local[:, None] == -1) | (local_t_2d < first_break_bar_local[:, None])
    # proj_in_range: gate uses Levels at each bar (broadcast)
    proj_in_range_2d = np.ones((P, n_bars), dtype=bool)
    u2_finite = ~np.isnan(upside_2_arr)
    d2_finite = ~np.isnan(downside_2_arr)
    # proj > upside_2 invalidates only where upside_2 is finite
    proj_in_range_2d &= ~(u2_finite[None, :] & (proj > upside_2_arr[None, :]))
    proj_in_range_2d &= ~(d2_finite[None, :] & (proj < downside_2_arr[None, :]))
    candidate_mask_2d = in_span_2d & visible_2d & not_broken_2d & proj_in_range_2d  # (P, N)

    # signed_dist_2d (only valid where candidate_mask_2d is True; else inf for ranking)
    ext_for_diff = np.where(np.isnan(ext), 0.0, ext)
    signed_dist_2d = proj - ext_for_diff[None, :]
    abs_dist_2d = np.where(candidate_mask_2d, np.abs(signed_dist_2d), np.inf)

    is_descending_per_pair = is_descending  # (P,) bool

    # Aggregates by side
    desc_mask_2d = candidate_mask_2d & is_descending_per_pair[:, None]
    asc_mask_2d = candidate_mask_2d & (~is_descending_per_pair[:, None])
    n_desc_per_bar = desc_mask_2d.sum(axis=0).astype(np.int64)
    n_asc_per_bar = asc_mask_2d.sum(axis=0).astype(np.int64)
    n_total_per_bar = n_desc_per_bar + n_asc_per_bar
    abs_desc = np.where(desc_mask_2d, np.abs(signed_dist_2d), np.inf)
    abs_asc = np.where(asc_mask_2d, np.abs(signed_dist_2d), np.inf)
    nearest_desc_per_bar = abs_desc.min(axis=0)
    nearest_asc_per_bar = abs_asc.min(axis=0)
    nearest_desc_per_bar = np.where(np.isinf(nearest_desc_per_bar), np.nan, nearest_desc_per_bar)
    nearest_asc_per_bar = np.where(np.isinf(nearest_asc_per_bar), np.nan, nearest_asc_per_bar)

    # Top-N per side per bar via argpartition (then sort within top N)
    def _top_n_per_side(side_mask_2d):
        """Return (top_pair_idx_2d shape (TOP_N, N) int, valid_2d bool)
        with pair indices ranked by ascending abs_dist; -1 (and valid=False)
        for empty slots."""
        side_abs = np.where(side_mask_2d, np.abs(signed_dist_2d), np.inf)
        if P == 0:
            return (np.full((TOP_N, n_bars), -1, dtype=np.int64),
                    np.zeros((TOP_N, n_bars), dtype=bool))
        kth = min(TOP_N, P) - 1
        partitioned = np.argpartition(side_abs, kth=kth, axis=0)[:TOP_N]  # (TOP_N, N)
        partitioned_dists = np.take_along_axis(side_abs, partitioned, axis=0)
        order = np.argsort(partitioned_dists, axis=0, kind="stable")
        sorted_idx = np.take_along_axis(partitioned, order, axis=0)
        sorted_dists = np.take_along_axis(partitioned_dists, order, axis=0)
        valid = ~np.isinf(sorted_dists)
        sorted_idx = np.where(valid, sorted_idx, -1)
        return sorted_idx, valid

    upper_idx_2d, upper_valid_2d = _top_n_per_side(desc_mask_2d)
    lower_idx_2d, lower_valid_2d = _top_n_per_side(asc_mask_2d)

    # Pack output. For each rank r in [0, TOP_N), for each bar t: gather fields
    # from per-pair arrays + 2D cumulative arrays at (pair_idx, t).
    def _pack_slot(slot_offset, idx_2d, valid_2d):
        """slot_offset: 0 for upper start, TOP_N*N_PER_LINE for lower start."""
        for r in range(TOP_N):
            base = slot_offset + r * N_PER_LINE
            sel = idx_2d[r]  # (N,) pair indices, -1 where invalid
            valid = valid_2d[r]  # (N,) bool
            sel_safe = np.where(valid, sel, 0)  # use 0 for invalid (will overwrite with NaN)
            # Per-pair scalar fields (1D gather)
            i0_v = pair_i0[sel_safe].astype(np.float64)
            v0_v = pair_v0[sel_safe]
            i1_v = pair_i1[sel_safe].astype(np.float64)
            v1_v = pair_v1[sel_safe]
            slope_v = pair_slope[sel_safe]
            atype_v = pair_atype[sel_safe]
            # Per-(pair, bar) 2D gathers
            cols = bars_int  # (N,)
            proj_v = proj[sel_safe, cols]
            ext_v = ext_for_diff
            signed_v = proj_v - ext_v
            pos_v = pos_bars_2d[sel_safe, cols].astype(np.float64)
            neg_v = neg_bars_2d[sel_safe, cols].astype(np.float64)
            tch_v = touches_2d[sel_safe, cols].astype(np.float64)
            lcb_v = last_cross_bar_2d[sel_safe, cols].astype(np.float64)
            lcd_v = last_cross_dir_2d[sel_safe, cols].astype(np.float64)
            tcr_v = total_cross_2d[sel_safe, cols].astype(np.float64)
            span_v = (cols.astype(np.float64) - i0_v)
            zb = zero_bar_global[sel_safe].astype(np.int64)
            zb_valid = (zb != -1) & (zb >= pair_i0[sel_safe]) & (zb <= cols)
            zero_bar_v = np.where(zb_valid, zb, -1).astype(np.float64)

            # Mask: invalid → NaN (per-row)
            nan_mask = ~valid
            for col_off, arr in enumerate([i0_v, v0_v, i1_v, v1_v, slope_v, atype_v,
                                           proj_v, signed_v, zero_bar_v, pos_v, neg_v,
                                           tch_v, lcb_v, lcd_v, tcr_v, span_v]):
                col = arr.copy()
                col[nan_mask] = np.nan
                out[:, base + col_off] = col

    _pack_slot(0, upper_idx_2d, upper_valid_2d)
    _pack_slot(TOP_N * N_PER_LINE, lower_idx_2d, lower_valid_2d)

    # Aggregates + pass-throughs (vectorized across bars)
    comp_base = N_SLOTS * N_PER_LINE
    out[:, comp_base + 0] = n_total_per_bar.astype(np.float64)
    out[:, comp_base + 1] = n_desc_per_bar.astype(np.float64)
    out[:, comp_base + 2] = n_asc_per_bar.astype(np.float64)
    out[:, comp_base + 3] = nearest_desc_per_bar
    out[:, comp_base + 4] = nearest_asc_per_bar
    out[:, comp_base + N_AGGREGATES + 0] = upside_1_arr
    out[:, comp_base + N_AGGREGATES + 1] = upside_2_arr
    out[:, comp_base + N_AGGREGATES + 2] = chop_upper_arr
    # Bar 0 stays NaN per cascade contract (cascade returns empty at bar < 1).
    out[0, :] = np.nan

    return {names[j]: out[:, j] for j in range(N_TOTAL)}


def compute_all_ext50_trendline_series_per_pair_loop(ext, levels_at_bar):
    """Per-pair-loop version (kept for parity testing / reference).
    Output bit-identical to cascade_at at every bar.

    Pivot visibility: find_peaks runs on ext[:t+1] in cascade_at. Boundary
    peaks become detectable only after enough right-side context arrives.
    We compute t_appear[K] (first bar where K is detected by find_peaks
    on the truncated history) once per ticker, then per-bar gate pairs by
    visible_from = max(t_appear[i0], t_appear[i1]) ≤ t.

    Args:
        ext: 1D ndarray of ext50 values, length n_bars.
        levels_at_bar: dict[level_name -> ndarray(n_bars,)] with keys
            upside_1, upside_2, downside_1, downside_2, chop_upper.

    Returns:
        dict[col_name -> ndarray(n_bars,) float64]
    """
    n_bars = len(ext)
    out = np.full((n_bars, N_TOTAL), np.nan, dtype=np.float64)
    names = get_ext50_trendline_expression_names()

    if n_bars < 2:
        return {names[j]: out[:, j] for j in range(N_TOTAL)}

    # Step 1: pivots over full history (single find_peaks call)
    nn_up = np.nan_to_num(ext, nan=-1e9)
    nn_dn = -np.nan_to_num(ext, nan=1e9)
    peaks, _ = find_peaks(nn_up, prominence=PROMINENCE)
    troughs, _ = find_peaks(nn_dn, prominence=PROMINENCE)
    peaks = peaks.astype(int)
    troughs = troughs.astype(int)
    if (len(peaks) + len(troughs)) > 0:
        all_piv_idx_init = np.concatenate([peaks, troughs])
        all_piv_val_init = np.concatenate([ext[peaks], ext[troughs]])
        order = np.argsort(all_piv_idx_init)
        all_piv_idx = all_piv_idx_init[order]
        all_piv_val = all_piv_val_init[order]
    else:
        all_piv_idx = np.zeros(0, dtype=int)
        all_piv_val = np.zeros(0, dtype=np.float64)

    # Step 1.5: t_appear per pivot (so per-bar emission only sees pivots
    # that find_peaks(ext[:t+1]) would have detected at bar t).
    t_appear_peak, t_appear_trough = _compute_t_appear(
        ext, n_bars, peaks, troughs, PROMINENCE,
    )
    # Combined map for the touches calc (both peaks and troughs participate).
    t_appear_combined = dict(t_appear_peak)
    t_appear_combined.update(t_appear_trough)

    # Step 2: enumerate pairs, apply bar-independent gates, precompute survivors.
    # Each survivor carries `visible_from` = max(t_appear[i0], t_appear[i1]).
    survivors = []
    for anchors, anchor_type_str, t_appear_map in (
        (peaks, "peak_anchored", t_appear_peak),
        (troughs, "trough_anchored", t_appear_trough),
    ):
        if len(anchors) < 2:
            continue
        for a in range(len(anchors)):
            for b in range(a + 1, len(anchors)):
                i0 = int(anchors[a])
                i1 = int(anchors[b])
                if i1 - i0 < MIN_SPAN_BARS:
                    continue
                v0 = float(ext[i0])
                v1 = float(ext[i1])
                slope = (v1 - v0) / (i1 - i0)
                if slope < 0 and v0 < 0:
                    continue
                if slope > 0 and v0 > 0:
                    continue
                s0 = np.sign(v0); s1 = np.sign(v1)
                if s0 == 0 or s1 == 0:
                    continue
                if s0 != s1:
                    continue
                if anchor_type_str == "peak_anchored" and slope >= 0:
                    continue
                if anchor_type_str == "trough_anchored" and slope <= 0:
                    continue
                t_app_i0 = t_appear_map.get(i0, n_bars)
                t_app_i1 = t_appear_map.get(i1, n_bars)
                visible_from = max(t_app_i0, t_app_i1)
                pair = _precompute_pair(
                    ext, i0, v0, i1, v1, slope, anchor_type_str,
                    all_piv_idx, all_piv_val, n_bars,
                    t_appear_combined=t_appear_combined,
                )
                pair["visible_from"] = visible_from
                survivors.append(pair)

    # Step 3: per-bar emission
    upside_1_arr = np.asarray(levels_at_bar.get("upside_1", np.full(n_bars, np.nan)), dtype=np.float64)
    upside_2_arr = np.asarray(levels_at_bar.get("upside_2", np.full(n_bars, np.nan)), dtype=np.float64)
    downside_2_arr = np.asarray(levels_at_bar.get("downside_2", np.full(n_bars, np.nan)), dtype=np.float64)
    chop_upper_arr = np.asarray(levels_at_bar.get("chop_upper", np.full(n_bars, np.nan)), dtype=np.float64)

    for t in range(1, n_bars):
        ext_t = float(ext[t]) if not np.isnan(ext[t]) else 0.0
        u2_t = float(upside_2_arr[t])
        d2_t = float(downside_2_arr[t])

        candidates = []
        for surv in survivors:
            # Pivot-visibility gate: both anchors must be detectable at bar t.
            if t < surv["visible_from"]:
                continue
            i0 = surv["i0"]
            local_t = t - i0
            if local_t < MIN_SPAN_BARS:
                continue
            if local_t >= len(surv["proj"]):
                continue
            # Asymmetric break gate
            fb = surv["first_break_bar"]
            if fb != -1 and local_t >= fb:
                continue
            # Projection-in-range gate
            proj_t = surv["proj"][local_t]
            if not np.isnan(u2_t) and proj_t > u2_t:
                continue
            if not np.isnan(d2_t) and proj_t < d2_t:
                continue

            zb = surv["zero_bar_global"]
            zero_bar = zb if (zb >= surv["i0"] and zb <= t and zb != -1) else -1
            candidates.append({
                "i0": surv["i0"], "v0": surv["v0"], "i1": surv["i1"], "v1": surv["v1"],
                "slope": surv["slope"], "anchor_type": surv["anchor_type"],
                "proj_asof": float(proj_t),
                "signed_dist": float(proj_t - ext_t),
                "zero_bar": int(zero_bar),
                "pos_bars": int(surv["pos_bars_arr"][local_t]),
                "neg_bars": int(surv["neg_bars_arr"][local_t]),
                "touches": int(surv["touches_arr"][local_t]),
                "last_cross_bar": int(surv["last_cross_bar_arr"][local_t]),
                "last_cross_dir": int(surv["last_cross_dir_arr"][local_t]),
                "total_cross": int(surv["total_cross_arr"][local_t]),
                "span": int(t - surv["i0"]),
            })

        upper, lower, agg = _rank_and_aggregate(candidates)
        levels_t = {
            "upside_1": float(upside_1_arr[t]),
            "upside_2": u2_t,
            "chop_upper": float(chop_upper_arr[t]),
        }
        snap = {"upper": upper, "lower": lower, "aggregates": agg, "all_candidates": candidates}
        out[t] = snapshot_to_vector(snap, levels_t)

    return {names[j]: out[:, j] for j in range(N_TOTAL)}
