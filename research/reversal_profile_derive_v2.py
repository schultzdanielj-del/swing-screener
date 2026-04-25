"""Reversal-profile derivation v2 — fixes chop_upper (true local peak
detection) and downside saturation (handle median = max case).

Key changes from v1:
  1. chop_upper: uses scipy.signal.find_peaks on the rate curve below
     upside_1, with prominence threshold derived from std(rate) of the
     baseline region. Returns NaN if no qualifying peak exists.
  2. upside_1 under saturation: if median rate = observed max (saturated
     at 1.0), fall back to 'first L where rate reaches max' instead of
     returning NaN.
  3. Same logic applied symmetrically to downside.

Everything else (upside_2 knee-of-decline) unchanged from v1.
"""
import numpy as np
from scipy.signal import find_peaks

FWD_WINDOW = 14
L_STEP = 0.5


def _build_curves(vals, L_grid, side):
    crossings = np.zeros(len(L_grid), dtype=int)
    returned = np.zeros(len(L_grid), dtype=int)
    n = len(vals)
    for li, L_mag in enumerate(L_grid):
        L = L_mag if side == "up" else -L_mag
        pending = []
        for t in range(1, n):
            a, b = vals[t - 1], vals[t]
            if np.isnan(a) or np.isnan(b):
                continue
            if side == "up":
                crossed = (a < L) and (b >= L)
                return_cond = lambda v: (not np.isnan(v)) and (v < L)
            else:
                crossed = (a > L) and (b <= L)
                return_cond = lambda v: (not np.isnan(v)) and (v > L)
            if crossed:
                crossings[li] += 1
                pending.append(t)
            new_pending = []
            for t0 in pending:
                if t - t0 > FWD_WINDOW:
                    continue
                if return_cond(b):
                    returned[li] += 1
                else:
                    new_pending.append(t0)
            pending = new_pending
    return crossings, returned


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
    Prominence threshold = 2 * std(rate) on the pre-peak sub-range.
    Returns NaN if no qualifying peak."""
    mask_sub = (L_grid <= u1 - L_STEP) & valid
    if mask_sub.sum() < 3:
        return float("nan")
    L_sub = L_grid[mask_sub]
    rate_sub = rate[mask_sub]

    # find all local maxima in the sub-range (via scipy find_peaks)
    # Pad with low values at boundaries so edge peaks get considered.
    pad = rate_sub.min() - 1.0
    padded = np.concatenate([[pad], rate_sub, [pad]])
    peaks_p, props = find_peaks(padded)
    peaks = peaks_p - 1  # shift back to rate_sub indexing
    peaks = peaks[(peaks >= 0) & (peaks < len(rate_sub))]

    if len(peaks) == 0:
        return float("nan")

    # For each peak, compute prominence vs its own left-side baseline
    # (min rate in L values less than peak's L, among valid rates).
    # Accept first peak whose prominence > 2 * std(baseline).
    for peak_i in peaks:
        if peak_i < 2:
            continue  # not enough baseline samples
        baseline = rate_sub[:peak_i]
        baseline_min = baseline.min()
        baseline_std = baseline.std(ddof=0) if len(baseline) >= 2 else 0.0
        prominence = rate_sub[peak_i] - baseline_min
        threshold = 2.0 * baseline_std
        if prominence > threshold and prominence > 0:
            return float(L_sub[peak_i])
    return float("nan")


def _derive_side(L_grid, crossings, returned):
    """Returns (u1, u2, chop_upper), all magnitudes (positive numbers)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        rate = np.where(crossings > 0, returned / np.maximum(crossings, 1), np.nan)
    valid = crossings > 0
    if valid.sum() < 3:
        return float("nan"), float("nan"), float("nan")

    rates_valid = rate[valid]
    obs_max = float(np.max(rates_valid))
    obs_median = float(np.median(rates_valid))

    # upside_1: normally first L with rate > median; under saturation
    # (median == max), use first L where rate reaches observed max.
    u1 = float("nan")
    if obs_median < obs_max:
        for i in range(len(L_grid)):
            if valid[i] and rate[i] > obs_median:
                u1 = float(L_grid[i])
                break
    else:
        # saturated
        for i in range(len(L_grid)):
            if valid[i] and rate[i] >= obs_max - 1e-9:
                u1 = float(L_grid[i])
                break
    if np.isnan(u1):
        return float("nan"), float("nan"), float("nan")

    # upside_2: knee-of-decline in crossings tail, L > u1 (unchanged)
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

    # chop_upper: first local peak in rate below u1, with data-derived prominence
    chop_upper = _find_chop_peak(L_grid, rate, valid, u1)

    return u1, u2, chop_upper


def derive_profile(ext, asof_bar):
    vals = ext[: asof_bar + 1].astype(float)
    vals_nn = vals[~np.isnan(vals)]
    if len(vals_nn) < 3:
        return {k: float("nan") for k in
                ("upside_1", "upside_2", "downside_1", "downside_2",
                 "chop_upper", "chop_lower")}

    max_abs = float(np.ceil(np.nanmax(np.abs(vals))))
    L_grid = np.arange(L_STEP, max_abs + 1.0 + L_STEP, L_STEP)

    up_c, up_r = _build_curves(vals, L_grid, "up")
    dn_c, dn_r = _build_curves(vals, L_grid, "down")

    u1, u2, c_up = _derive_side(L_grid, up_c, up_r)
    d1, d2, c_dn = _derive_side(L_grid, dn_c, dn_r)

    return {
        "upside_1": u1,
        "upside_2": u2,
        "chop_upper": c_up,
        "downside_1": -d1 if not np.isnan(d1) else float("nan"),
        "downside_2": -d2 if not np.isnan(d2) else float("nan"),
        "chop_lower": -c_dn if not np.isnan(c_dn) else float("nan"),
    }


def main():
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import trendline_primitive_v6 as tp

    asof_str = "2026-04-10"
    asof_dt = np.datetime64(asof_str)

    calib = {
        "AAPL":  {"u1": "~4.0 or ~5.5", "u2": "~7.0", "chop_u": "~+3"},
        "MSFT":  {"u1": "+4.0", "u2": "+7.5", "chop_u": "—"},
        "TSLA":  {"u1": "+5.0 (or 3.75)", "u2": "+8.5", "chop_u": "+1.75"},
        "RIVN":  {"u1": "+4.5", "u2": "—", "chop_u": "—"},
        "SPY":   {"u1": "—", "u2": "—", "chop_u": "—"},
    }

    print(f"Reversal-profile v2 at {asof_str}:")
    print(f"{'ticker':<6} {'u1':>6} {'u2':>6} {'chop_u':>8} {'d1':>8} "
          f"{'d2':>8} {'chop_l':>8}   {'calib u1/u2/chop_u':<}")
    for ticker in ["AAPL", "MSFT", "TSLA", "RIVN", "SPY"]:
        dates, ext = tp.load_ticker_50ext(ticker)
        asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
        prof = derive_profile(ext, asof_bar)
        c = calib[ticker]
        print(f"{ticker:<6} {prof['upside_1']:>6.2f} {prof['upside_2']:>6.2f} "
              f"{prof['chop_upper']:>8.2f} {prof['downside_1']:>8.2f} "
              f"{prof['downside_2']:>8.2f} {prof['chop_lower']:>8.2f}   "
              f"{c['u1']} / {c['u2']} / {c['chop_u']}")


if __name__ == "__main__":
    main()
