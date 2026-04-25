"""Verify scipy find_peaks alone (no slide) always returns bars that match
the peak-definition (lower bars on both sides, handling plateaus) on the
50-ext series for the 5 calibration tickers at 2026-04-10.

Separately quantifies v6's slide step: how many pivots it would shift, and
how many of those shifts land on bars that are NOT peaks by definition
(the SPY-failure-mode signature).

Prints a tabular summary + pass/fail verdict. No persistent outputs."""
import os
import sys

import numpy as np
from scipy.signal import find_peaks

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trendline_primitive_v6 as tp  # loader only

TICKERS = ["AAPL", "MSFT", "TSLA", "CAR", "SPY"]
ASOF = np.datetime64("2026-04-10")
WINDOW = 260
SLIDE = 5  # v6 ANCHOR_SLIDE_BARS
PROMS = [("feat1", 1.0), ("feat2", 0.5)]


def is_local_extremum(ext, i, kind):
    """Return (ok, reason). Plateau-aware: walks outward past equal values
    until a strict change or a boundary/NaN."""
    n = len(ext)
    if i <= 0 or i >= n - 1:
        return False, "boundary"
    v = ext[i]
    if np.isnan(v):
        return False, "center_nan"
    L = i - 1
    while L >= 0 and not np.isnan(ext[L]) and ext[L] == v:
        L -= 1
    R = i + 1
    while R < n and not np.isnan(ext[R]) and ext[R] == v:
        R += 1
    if L < 0 or R >= n:
        return False, "plateau_boundary"
    if np.isnan(ext[L]) or np.isnan(ext[R]):
        return False, "adjacent_nan"
    if kind == "peak":
        return (ext[L] < v and ext[R] < v), "strict_check"
    else:
        return (ext[L] > v and ext[R] > v), "strict_check"


def slide(ext, i, kind):
    lo, hi = max(0, i - SLIDE), min(len(ext) - 1, i + SLIDE)
    seg = ext[lo:hi + 1]
    if np.all(np.isnan(seg)):
        return i
    if kind == "peak":
        return int(np.nanargmax(seg)) + lo
    return int(np.nanargmin(seg)) + lo


def main():
    print(f"{'ticker':<6} {'feat':<6} {'kind':<7} "
          f"{'fp_ct':>6} {'fp_bad':>7} {'shift_ct':>9} {'shift_bad':>10}")
    print("-" * 64)

    any_fp_bad = 0
    any_shift_bad = 0
    fp_bad_detail = []
    shift_bad_detail = []

    for ticker in TICKERS:
        dates, ext = tp.load_ticker_50ext(ticker)
        asof_bar = int(np.searchsorted(dates, ASOF, side="right") - 1)
        w_start = max(0, asof_bar - WINDOW)

        nn_up = np.nan_to_num(ext, nan=-1e9)
        nn_dn = -np.nan_to_num(ext, nan=1e9)

        for fname, prom in PROMS:
            peaks_raw = find_peaks(nn_up, prominence=prom)[0]
            troughs_raw = find_peaks(nn_dn, prominence=prom)[0]

            for kind, raw in (("peak", peaks_raw), ("trough", troughs_raw)):
                pivots = raw[(raw >= w_start) & (raw <= asof_bar)]
                fp_ct = len(pivots)
                fp_bad = 0
                shift_ct = 0
                shift_bad = 0
                for p in pivots:
                    p = int(p)
                    ok, reason = is_local_extremum(ext, p, kind)
                    if not ok:
                        fp_bad += 1
                        fp_bad_detail.append(
                            (ticker, fname, kind, p, reason, float(ext[p])))
                    s = slide(ext, p, kind)
                    if s != p:
                        shift_ct += 1
                        ok_s, reason_s = is_local_extremum(ext, s, kind)
                        if not ok_s:
                            shift_bad += 1
                            shift_bad_detail.append(
                                (ticker, fname, kind, p, s, reason_s,
                                 float(ext[p]), float(ext[s])))
                any_fp_bad += fp_bad
                any_shift_bad += shift_bad
                print(f"{ticker:<6} {fname:<6} {kind:<7} "
                      f"{fp_ct:>6d} {fp_bad:>7d} {shift_ct:>9d} {shift_bad:>10d}")

    print("-" * 64)
    print(f"\nTotal find_peaks pivots failing peak-definition: {any_fp_bad}")
    print(f"Total slide shifts landing on non-peak bars:     {any_shift_bad}")

    if fp_bad_detail:
        print("\n--- find_peaks failures (investigate) ---")
        for t, fn, kn, p, r, v in fp_bad_detail[:20]:
            print(f"  {t} {fn} {kn} bar={p} val={v:.3f} reason={r}")
    if shift_bad_detail:
        print("\n--- slide shifts to non-pivots (expected > 0) ---")
        for t, fn, kn, p_orig, p_new, r, v_orig, v_new in shift_bad_detail[:20]:
            print(f"  {t} {fn} {kn} orig_bar={p_orig} val={v_orig:.3f} -> "
                  f"slid_bar={p_new} val={v_new:.3f} reason={r}")

    print()
    if any_fp_bad == 0:
        print("[PASS] find_peaks alone reliably finds real peaks on this data.")
    else:
        print("[FAIL] find_peaks returned bars that are not local extrema.")


if __name__ == "__main__":
    main()
