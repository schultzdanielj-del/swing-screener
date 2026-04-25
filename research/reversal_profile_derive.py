"""Reversal-profile derivation for 50-ext series, per EXPRESSION_ENGINE_V2.md
spec lines 99-131. Returns the 6 constants: upside_1, upside_2, downside_1,
downside_2, chop_upper, chop_lower.

Computed at a given asof_bar using bars [0, asof_bar] (expanding window).
"""
import numpy as np

FWD_WINDOW = 14
L_STEP = 0.5


def _build_curves(vals, L_grid, side):
    """Return per-L (crossings_count, returned_count) for one side.
    side = 'up' uses positive L; side = 'down' uses negative L (so L_grid
    magnitudes are mirrored)."""
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
    """Knee = point of max perpendicular distance from the chord connecting
    first and last points of the decline tail. Tail starts at argmax of
    crossings. Spec lines 118-121."""
    if len(counts_sub) < 3:
        return None
    # argmax of sub-curve = tail start (spec: tail starts at argmax of crossings)
    argmax_rel = int(np.argmax(counts_sub))
    L_tail = L_sub[argmax_rel:]
    c_tail = counts_sub[argmax_rel:]
    if len(L_tail) < 3:
        return None
    # chord from (L_tail[0], c_tail[0]) to (L_tail[-1], c_tail[-1])
    x0, y0 = L_tail[0], c_tail[0]
    x1, y1 = L_tail[-1], c_tail[-1]
    dx, dy = x1 - x0, y1 - y0
    seg_len = np.hypot(dx, dy)
    if seg_len < 1e-9:
        return None
    # perp distance for each point on tail
    dists_pos = np.zeros(len(L_tail))
    dists_neg = np.zeros(len(L_tail))
    for i, (x, y) in enumerate(zip(L_tail, c_tail)):
        # signed perpendicular distance
        d = ((x - x0) * dy - (y - y0) * dx) / seg_len
        if d > 0:
            dists_pos[i] = d
        else:
            dists_neg[i] = -d
    max_pos = dists_pos.max()
    max_neg = dists_neg.max()
    # concavity check (spec line 130): if max_pos <= max_neg, fall back to
    # argmax of curve (first L where rate hits max)
    if max_pos <= max_neg:
        return None  # signal concave fallback
    idx = int(np.argmax(dists_pos)) + argmax_rel
    return L_sub[idx]


def derive_profile(ext, asof_bar):
    """Return dict of the 6 constants at asof_bar. NaN if insufficient sample."""
    vals = ext[: asof_bar + 1].astype(float)
    vals_nonnan = vals[~np.isnan(vals)]
    if len(vals_nonnan) < 3:
        return {k: float("nan") for k in
                ("upside_1", "upside_2", "downside_1", "downside_2",
                 "chop_upper", "chop_lower")}

    max_abs = float(np.ceil(np.nanmax(np.abs(vals))))
    L_max = max_abs + 1.0
    L_grid = np.arange(L_STEP, L_max + L_STEP, L_STEP)

    up_c, up_r = _build_curves(vals, L_grid, "up")
    dn_c, dn_r = _build_curves(vals, L_grid, "down")

    def derive_side(L_grid, crossings, returned):
        # rate = returned / crossings, ignoring L values with 0 crossings
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(crossings > 0, returned / np.maximum(crossings, 1), np.nan)
        has_data = crossings > 0
        if has_data.sum() < 3:
            return float("nan"), float("nan"), float("nan")

        rates_valid = rate[has_data]
        median_rate = float(np.median(rates_valid))

        # upside_1: first L ascending where rate > median
        u1 = float("nan")
        for i in range(len(L_grid)):
            if has_data[i] and rate[i] > median_rate:
                u1 = float(L_grid[i])
                break
        if np.isnan(u1):
            return float("nan"), float("nan"), float("nan")

        # upside_2: knee of crossings-decline tail, L > u1
        mask_u2 = (L_grid > u1) & has_data
        u2 = float("nan")
        if mask_u2.sum() >= 3:
            L_sub = L_grid[mask_u2]
            c_sub = crossings[mask_u2]
            knee = _knee_of_decline(L_sub, c_sub)
            if knee is None:
                # concave fallback: argmax of rate curve in sub-range
                rate_sub = rate[mask_u2]
                if np.any(~np.isnan(rate_sub)):
                    max_rate = np.nanmax(rate_sub)
                    for j, r in enumerate(rate_sub):
                        if r == max_rate:
                            u2 = float(L_sub[j])
                            break
            else:
                u2 = float(knee)

        # chop_upper: L with MAX rate in region L <= u1 - 0.5
        chop_upper = float("nan")
        mask_chop = (L_grid <= u1 - 0.5) & has_data
        if mask_chop.any():
            L_chop = L_grid[mask_chop]
            r_chop = rate[mask_chop]
            if np.any(~np.isnan(r_chop)):
                max_r = np.nanmax(r_chop)
                for j, r in enumerate(r_chop):
                    if r == max_r:
                        chop_upper = float(L_chop[j])
                        break
        return u1, u2, chop_upper

    u1, u2, c_up = derive_side(L_grid, up_c, up_r)
    d1, d2, c_dn = derive_side(L_grid, dn_c, dn_r)

    return {
        "upside_1": u1,
        "upside_2": u2,
        "chop_upper": c_up,
        "downside_1": -d1 if not np.isnan(d1) else float("nan"),
        "downside_2": -d2 if not np.isnan(d2) else float("nan"),
        "chop_lower": -c_dn if not np.isnan(c_dn) else float("nan"),
    }


def dump_curves(ticker, ext, asof_bar):
    vals = ext[: asof_bar + 1].astype(float)
    max_abs = float(np.ceil(np.nanmax(np.abs(vals))))
    L_grid = np.arange(L_STEP, max_abs + 1.0 + L_STEP, L_STEP)
    up_c, up_r = _build_curves(vals, L_grid, "up")
    dn_c, dn_r = _build_curves(vals, L_grid, "down")
    print(f"\n--- {ticker} rate curves (n_bars_used={(~np.isnan(vals)).sum()}) ---")
    print(f"{'L':>6} {'up_cross':>10} {'up_ret':>8} {'up_rate':>10} {'dn_cross':>10} {'dn_ret':>8} {'dn_rate':>10}")
    for i, L in enumerate(L_grid):
        ur = up_r[i] / up_c[i] if up_c[i] > 0 else float("nan")
        dr = dn_r[i] / dn_c[i] if dn_c[i] > 0 else float("nan")
        print(f"{L:>6.1f} {up_c[i]:>10d} {up_r[i]:>8d} {ur:>10.3f} "
              f"{dn_c[i]:>10d} {dn_r[i]:>8d} {dr:>10.3f}")


def main():
    import trendline_primitive_v6 as tp
    asof_str = "2026-04-10"
    asof_dt = np.datetime64(asof_str)
    print(f"Reversal-profile constants computed from ext series up to {asof_str}:")
    print(f"{'ticker':<6} {'upside_1':>10} {'upside_2':>10} {'chop_upper':>12} {'downside_1':>12} {'downside_2':>12} {'chop_lower':>12}")
    for ticker in ["AAPL", "MSFT", "TSLA", "CAR", "SPY"]:
        dates, ext = tp.load_ticker_50ext(ticker)
        asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
        prof = derive_profile(ext, asof_bar)
        print(f"{ticker:<6} {prof['upside_1']:>10.2f} {prof['upside_2']:>10.2f} "
              f"{prof['chop_upper']:>12.2f} {prof['downside_1']:>12.2f} "
              f"{prof['downside_2']:>12.2f} {prof['chop_lower']:>12.2f}")
    print("\nCalibration from EXPRESSION_ENGINE_V2.md line 155-158:")
    print("  AAPL: upside_1 ~+4.0 or ~+5.5  upside_2 ~+7.0  chop ~-3 to +3")
    print("  MSFT: upside_1 +4.0           upside_2 +7.5")
    print("  TSLA: upside_1 +5.0 (or 3.75)  upside_2 +8.5  chop ~-1.5 to +1.75")
    print("\nAAPL rate curves for inspection:")
    dates, ext = tp.load_ticker_50ext("AAPL")
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    dump_curves("AAPL", ext, asof_bar)


if __name__ == "__main__":
    main()
