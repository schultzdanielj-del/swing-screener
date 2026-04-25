"""Reversal-profile derivation — Extension Chart Levels primitive.

Spec: EXPRESSION_ENGINE_V2.md §6 Extension Chart Levels.
Authoritative algorithm: research/reversal_profile_derive_v2.py.

Computes 6 per-bar constants from a 50- or 200-extension series:
  upside_1, upside_2, downside_1, downside_2, chop_upper, chop_lower

Per-bar Option C: maintain running per-L upcrossing + return tallies as
bars are scanned chronologically; recompute the 6 constants at every bar
from the current cumulative curve.

Historical immutability: the curve at bar N uses only bars 0..N, so the
6 constants at bar N never change between rebuilds.

Daily timeframe only — D1 50/200 SMA carry the structural significance;
weekly/monthly extensions of the same MAs do not. No HTF prefix.
"""
import numpy as np
from scipy.signal import find_peaks

CONSTANT_NAMES = (
    "upside_1", "upside_2", "downside_1", "downside_2", "chop_upper", "chop_lower",
)
N_CONSTANTS = len(CONSTANT_NAMES)

# Derivation constants (from research/reversal_profile_derive_v2.py)
FWD_WINDOW = 14
L_STEP = 0.5

# Valid input ext source columns. Used by registration + dispatch wiring.
EXT_SOURCE_COLUMNS = ("ext_avgc50_adr14", "ext_avgc200_adr14")


def get_reversal_profile_expression_names():
    """Return the 12 ordered column names in the same order brute_expressions
    must register them and the same order forward-prop emits."""
    names = []
    for src in EXT_SOURCE_COLUMNS:
        for cname in CONSTANT_NAMES:
            names.append(f"{src}_{cname}")
    return names


# ─── Constant derivation (mirrors research _derive_side) ─────────

def _knee_of_decline(L_sub, counts_sub):
    if len(counts_sub) < 3:
        return None
    argmax_rel = int(np.argmax(counts_sub))
    L_tail = L_sub[argmax_rel:]
    c_tail = counts_sub[argmax_rel:]
    if len(L_tail) < 3:
        return None
    x0, y0 = L_tail[0], c_tail[0]
    x1, y1 = L_tail[-1], c_tail[-1]
    dx, dy = x1 - x0, y1 - y0
    seg_len = np.hypot(dx, dy)
    if seg_len < 1e-9:
        return None
    dists_pos = np.zeros(len(L_tail))
    dists_neg = np.zeros(len(L_tail))
    for i, (x, y) in enumerate(zip(L_tail, c_tail)):
        d = ((x - x0) * dy - (y - y0) * dx) / seg_len
        if d > 0:
            dists_pos[i] = d
        else:
            dists_neg[i] = -d
    if dists_pos.max() < dists_neg.max():
        return None
    idx = int(np.argmax(dists_pos)) + argmax_rel
    return L_sub[idx]


def _find_chop_peak(L_grid, rate, valid, u1):
    """First genuine local peak in rate curve at L <= u1 - L_STEP.
    Prominence threshold = 2 * std of pre-peak baseline. NaN if none."""
    mask_sub = (L_grid <= u1 - L_STEP) & valid
    if mask_sub.sum() < 3:
        return float("nan")
    L_sub = L_grid[mask_sub]
    rate_sub = rate[mask_sub]

    pad = rate_sub.min() - 1.0
    padded = np.concatenate([[pad], rate_sub, [pad]])
    peaks_p, _ = find_peaks(padded)
    peaks = peaks_p - 1
    peaks = peaks[(peaks >= 0) & (peaks < len(rate_sub))]

    if len(peaks) == 0:
        return float("nan")

    for peak_i in peaks:
        if peak_i < 2:
            continue
        baseline = rate_sub[:peak_i]
        baseline_min = baseline.min()
        baseline_std = baseline.std(ddof=0) if len(baseline) >= 2 else 0.0
        prominence = rate_sub[peak_i] - baseline_min
        threshold = 2.0 * baseline_std
        if prominence > threshold and prominence > 0:
            return float(L_sub[peak_i])
    return float("nan")


def _derive_side(L_grid, crossings, returned):
    """Returns (u1, u2, chop_upper), all magnitudes (positive numbers).
    Mirrors research/reversal_profile_derive_v2._derive_side exactly."""
    with np.errstate(divide="ignore", invalid="ignore"):
        rate = np.where(crossings > 0, returned / np.maximum(crossings, 1), np.nan)
    valid = crossings > 0
    if valid.sum() < 3:
        return float("nan"), float("nan"), float("nan")

    rates_valid = rate[valid]
    obs_max = float(np.max(rates_valid))
    obs_median = float(np.median(rates_valid))

    u1 = float("nan")
    if obs_median < obs_max:
        for i in range(len(L_grid)):
            if valid[i] and rate[i] > obs_median:
                u1 = float(L_grid[i])
                break
    else:
        for i in range(len(L_grid)):
            if valid[i] and rate[i] >= obs_max - 1e-9:
                u1 = float(L_grid[i])
                break
    if np.isnan(u1):
        return float("nan"), float("nan"), float("nan")

    mask_u2 = (L_grid > u1) & valid
    u2 = float("nan")
    if mask_u2.sum() >= 3:
        L_sub = L_grid[mask_u2]
        c_sub = crossings[mask_u2]
        knee = _knee_of_decline(L_sub, c_sub)
        if knee is None:
            rate_sub = rate[mask_u2]
            if np.any(~np.isnan(rate_sub)):
                max_rate = np.nanmax(rate_sub)
                for j, r in enumerate(rate_sub):
                    if r == max_rate:
                        u2 = float(L_sub[j])
                        break
        else:
            u2 = float(knee)

    chop_upper = _find_chop_peak(L_grid, rate, valid, u1)
    return u1, u2, chop_upper


def _derive_six_constants(L_grid, c_up, r_up, c_dn, r_dn):
    """Run _derive_side for both sides; return 6-tuple (u1,u2,d1,d2,cu,cl).
    downside trio returned as negative magnitudes."""
    u1, u2, c_up_val = _derive_side(L_grid, c_up, r_up)
    d1, d2, c_dn_val = _derive_side(L_grid, c_dn, r_dn)
    return (
        u1,
        u2,
        -d1 if not np.isnan(d1) else float("nan"),
        -d2 if not np.isnan(d2) else float("nan"),
        c_up_val,
        -c_dn_val if not np.isnan(c_dn_val) else float("nan"),
    )


# ─── L-grid sizing ─────────────────────────────────────────────────

def _build_L_grid_for_max(max_abs):
    """Mirrors research: L_grid = arange(L_STEP, max_abs + 1.0 + L_STEP, L_STEP).

    `max_abs` is ceil(max(|ext|)) over the history considered. The +L_STEP on
    the upper bound keeps the inclusive endpoint behavior of np.arange.
    """
    if not np.isfinite(max_abs) or max_abs <= 0:
        return np.zeros(0, dtype=np.float64)
    return np.arange(L_STEP, max_abs + 1.0 + L_STEP, L_STEP, dtype=np.float64)


def _build_L_grid(vals):
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return np.zeros(0, dtype=np.float64)
    return _build_L_grid_for_max(float(np.ceil(np.max(np.abs(finite)))))


# ─── Option C running-tally walk ──────────────────────────────────

def _new_state(L_grid):
    """Empty per-side tally state for a given L_grid."""
    n_L = len(L_grid)
    return {
        "L_grid": list(L_grid),
        "c_up": np.zeros(n_L, dtype=np.int64),
        "r_up": np.zeros(n_L, dtype=np.int64),
        "c_dn": np.zeros(n_L, dtype=np.int64),
        "r_dn": np.zeros(n_L, dtype=np.int64),
        # pending: list of (bar_idx, L_idx) tuples per side
        "pending_up": [],
        "pending_dn": [],
    }


def _step_at(state, a, b, t):
    """Apply one bar of Option C using scalar prev/curr values.

    a = vals[t-1], b = vals[t], t = absolute bar index. Mutates state in place.
    """
    L_grid = np.asarray(state["L_grid"], dtype=np.float64)
    n_L = len(L_grid)
    if n_L == 0 or t < 1:
        return
    if not (np.isfinite(a) and np.isfinite(b)):
        return  # match research: no update on NaN bars

    c_up = state["c_up"]; r_up = state["r_up"]
    c_dn = state["c_dn"]; r_dn = state["r_dn"]
    pending_up = state["pending_up"]
    pending_dn = state["pending_dn"]

    # ─ Upside ─
    crossed_up_mask = (a < L_grid) & (b >= L_grid)
    for li in np.flatnonzero(crossed_up_mask):
        c_up[li] += 1
        pending_up.append((t, int(li)))
    new_pending_up = []
    for (t0, li) in pending_up:
        if t - t0 > FWD_WINDOW:
            continue
        if b < L_grid[li]:
            r_up[li] += 1
        else:
            new_pending_up.append((t0, li))
    state["pending_up"] = new_pending_up

    # ─ Downside (mirror) ─
    crossed_dn_mask = (a > -L_grid) & (b <= -L_grid)
    for li in np.flatnonzero(crossed_dn_mask):
        c_dn[li] += 1
        pending_dn.append((t, int(li)))
    new_pending_dn = []
    for (t0, li) in pending_dn:
        if t - t0 > FWD_WINDOW:
            continue
        if b > -L_grid[li]:
            r_dn[li] += 1
        else:
            new_pending_dn.append((t0, li))
    state["pending_dn"] = new_pending_dn


def _step(state, vals, t):
    """Full-walk wrapper: extract a/b from vals and delegate to _step_at."""
    if t < 1 or t >= len(vals):
        return
    _step_at(state, vals[t - 1], vals[t], t)


def _expand_grid_if_needed(state, val):
    """Grow L_grid if a new |val| exceeds current grid coverage.

    Backfilling is unnecessary — past bars never crossed L values that
    weren't in the grid, so cumulative counts for the new entries start at 0.
    Mirrors per-bar rebuild semantics where high L's are absent for early
    bars (the constants derivation excludes L's with crossings == 0 anyway).
    """
    if not np.isfinite(val):
        return
    needed = float(np.ceil(abs(val)))
    L_grid = state["L_grid"]
    cur_max = max(L_grid) if L_grid else 0.0
    if needed + 1.0 <= cur_max:
        return
    new_grid = _build_L_grid_for_max(needed)
    if len(new_grid) <= len(L_grid):
        return
    extra = len(new_grid) - len(L_grid)
    state["L_grid"] = list(new_grid)
    state["c_up"] = np.concatenate([state["c_up"], np.zeros(extra, dtype=np.int64)])
    state["r_up"] = np.concatenate([state["r_up"], np.zeros(extra, dtype=np.int64)])
    state["c_dn"] = np.concatenate([state["c_dn"], np.zeros(extra, dtype=np.int64)])
    state["r_dn"] = np.concatenate([state["r_dn"], np.zeros(extra, dtype=np.int64)])


def _snapshot(state):
    """Compute the 6 constants from current tallies. Returns float64 array length 6."""
    L_grid = np.asarray(state["L_grid"], dtype=np.float64)
    if len(L_grid) == 0:
        return np.full(N_CONSTANTS, np.nan, dtype=np.float64)
    six = _derive_six_constants(L_grid, state["c_up"], state["r_up"],
                                  state["c_dn"], state["r_dn"])
    return np.array(six, dtype=np.float64)


def compute_all_reversal_profile_series(ext_series):
    """Walk full ext history; return 6 per-bar arrays in CONSTANT_NAMES order.

    Args:
        ext_series: 1D ndarray of ext values (e.g., ext_avgc50_adr14 column)

    Returns:
        dict[constant_name -> ndarray(n_bars,) float64]
    """
    vals = np.asarray(ext_series, dtype=np.float64)
    n = len(vals)
    out = {nm: np.full(n, np.nan, dtype=np.float64) for nm in CONSTANT_NAMES}
    if n == 0:
        return out

    # Pre-size L_grid using full-history max so all bars share one grid.
    # Per-bar derivation only counts L's with crossings>0 → high-L early bars
    # are correctly excluded even though present in the grid.
    L_grid = _build_L_grid(vals)
    state = _new_state(L_grid)

    for t in range(n):
        _step(state, vals, t)
        snap = _snapshot(state)
        for i, nm in enumerate(CONSTANT_NAMES):
            out[nm][t] = snap[i]
    return out


# ─── Forward-prop API ─────────────────────────────────────────────

def _state_to_json(state):
    """Convert state dict to JSON-serializable form (numpy → lists)."""
    return {
        "L_grid": list(map(float, state["L_grid"])),
        "c_up": list(map(int, state["c_up"])),
        "r_up": list(map(int, state["r_up"])),
        "c_dn": list(map(int, state["c_dn"])),
        "r_dn": list(map(int, state["r_dn"])),
        "pending_up": [[int(t0), int(li)] for (t0, li) in state["pending_up"]],
        "pending_dn": [[int(t0), int(li)] for (t0, li) in state["pending_dn"]],
    }


def _state_from_json(d):
    """Inverse of _state_to_json. Converts JSON dicts back to working state."""
    return {
        "L_grid": list(map(float, d.get("L_grid", []))),
        "c_up": np.asarray(d.get("c_up", []), dtype=np.int64),
        "r_up": np.asarray(d.get("r_up", []), dtype=np.int64),
        "c_dn": np.asarray(d.get("c_dn", []), dtype=np.int64),
        "r_dn": np.asarray(d.get("r_dn", []), dtype=np.int64),
        "pending_up": [(int(p[0]), int(p[1])) for p in d.get("pending_up", [])],
        "pending_dn": [(int(p[0]), int(p[1])) for p in d.get("pending_dn", [])],
    }


def bootstrap_reversal_state(ext_series):
    """One-time setup: walk full history, return JSON-serializable end-state.

    Returns a dict suitable to drop into the `.state` JSON file.
    """
    vals = np.asarray(ext_series, dtype=np.float64)
    L_grid = _build_L_grid(vals)
    state = _new_state(L_grid)
    n = len(vals)
    for t in range(n):
        _step(state, vals, t)
    return _state_to_json(state)


def step_reversal_one_bar(prior_state_json, ext_series, t=None):
    """Forward-prop one bar using a slice of ext history.

    Convenience wrapper used by sandbox/parity tests when the full ext array
    is available. Forward-prop runtime should call `step_reversal_at_bar`
    instead with just (prev_ext, new_ext, t) since it only stores prev_ext.
    """
    vals = np.asarray(ext_series, dtype=np.float64)
    if t is None:
        t = len(vals) - 1
    if t < 1 or t >= len(vals):
        # Nothing to do; emit current snapshot
        state = _state_from_json(prior_state_json) if prior_state_json else _new_state(_build_L_grid_for_max(0.0))
        return _snapshot(state), _state_to_json(state)
    return step_reversal_at_bar(prior_state_json, vals[t - 1], vals[t], t)


def step_reversal_at_bar(prior_state_json, prev_ext, new_ext, t):
    """Forward-prop one bar using only scalar prev_ext + new_ext + abs index.

    Args:
        prior_state_json: JSON dict from bootstrap_reversal_state (or empty {}).
        prev_ext: ext value at bar t-1.
        new_ext: ext value at bar t (just-computed).
        t: absolute bar index of new_ext (used for pending-queue expiry).

    Returns:
        (vec, new_state_json) where vec is float64 array length 6 (in
        CONSTANT_NAMES order).
    """
    state = _state_from_json(prior_state_json) if prior_state_json else _new_state(_build_L_grid_for_max(0.0))
    _expand_grid_if_needed(state, new_ext)
    _step_at(state, float(prev_ext), float(new_ext), int(t))
    vec = _snapshot(state)
    return vec, _state_to_json(state)
