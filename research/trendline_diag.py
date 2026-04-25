"""Diagnostic: trace why a specific expected trendline anchor is not
emitted by v6. Usage:
    python trendline_diag.py TICKER TARGET_DATE SIDE
  SIDE = 'trough' (ascending, lower cascade) or 'peak' (descending, upper cascade)."""
import sys

import numpy as np
from scipy.signal import find_peaks

import trendline_primitive_v6 as tp


def main():
    TICKER = sys.argv[1]
    TARGET_DT = sys.argv[2]
    SIDE = sys.argv[3]  # "peak" or "trough"
    assert SIDE in ("peak", "trough")

    dates, ext = tp.load_ticker_50ext(TICKER)
    asof_dt = np.datetime64("2026-04-10")
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    window_start = max(0, asof_bar - tp.WINDOW_BARS)

    target_dt = np.datetime64(TARGET_DT)
    target_bar = int(np.searchsorted(dates, target_dt, side="right") - 1)
    print(f"=== {TICKER} SIDE={SIDE} target={TARGET_DT} (bar {target_bar}) ext_at_target={ext[target_bar]:+.3f} ===")
    print(f"asof_bar = {asof_bar} ({str(dates[asof_bar])[:10]})")
    print(f"window = bars {window_start}..{asof_bar}")

    # raw pivots at current prominence
    if SIDE == "peak":
        raw, _ = find_peaks(np.nan_to_num(ext, nan=-1e9), prominence=tp.PROMINENCE)
    else:
        raw, _ = find_peaks(-np.nan_to_num(ext, nan=1e9), prominence=tp.PROMINENCE)
    raw_in = raw[(raw <= asof_bar) & (raw >= window_start)]
    print(f"\nRAW {SIDE}s in window (prom={tp.PROMINENCE}): {len(raw_in)}")
    for t in raw_in:
        marker = " <-- near target" if abs(int(t) - target_bar) <= 10 else ""
        print(f"  bar {t} = {str(dates[int(t)])[:10]} ext={ext[t]:+.3f}{marker}")

    peak_i, peak_v, trough_i, trough_v = tp.find_anchors(ext, asof_bar, window_start)
    anch_i = peak_i if SIDE == "peak" else trough_i
    anch_v = peak_v if SIDE == "peak" else trough_v
    print(f"\nSLID {SIDE}s in window: {len(anch_i)}")
    for i, v in zip(anch_i, anch_v):
        marker = " <-- near target" if abs(int(i) - target_bar) <= 10 else ""
        print(f"  bar {i} = {str(dates[int(i)])[:10]} ext={v:+.3f}{marker}")

    for prom in [0.5, 0.25, 0.1]:
        if SIDE == "peak":
            r2, _ = find_peaks(np.nan_to_num(ext, nan=-1e9), prominence=prom)
        else:
            r2, _ = find_peaks(-np.nan_to_num(ext, nan=1e9), prominence=prom)
        r2_in = r2[(r2 <= asof_bar) & (r2 >= window_start)]
        has = any(abs(int(t) - target_bar) <= 10 for t in r2_in)
        near_targets = [int(t) for t in r2_in if abs(int(t) - target_bar) <= 10]
        print(f"\nprom={prom}: {len(r2_in)} {SIDE}s in window. near-target bars: {near_targets}")

    # enumerate pairs with i0 within ±10 of target
    anchor_type = "peak_anchored" if SIDE == "peak" else "trough_anchored"
    print(f"\n--- pair tracing (i0 within ±10 bars of target) ---")
    for ia_idx, (i0_raw, v0) in enumerate(zip(anch_i, anch_v)):
        i0 = int(i0_raw)
        if abs(i0 - target_bar) > 10:
            continue
        print(f"\n  candidate i0 = bar {i0} ({str(dates[i0])[:10]}) v0={v0:+.3f}")
        for ib_idx in range(ia_idx + 1, len(anch_i)):
            i1 = int(anch_i[ib_idx])
            v1 = float(anch_v[ib_idx])
            if i1 <= i0:
                continue
            span_ab = i1 - i0
            span_aoi = asof_bar - i0
            slope = (v1 - v0) / (i1 - i0)
            fails = []
            if span_ab < tp.MIN_SPAN_BARS:
                fails.append(f"MIN_SPAN(i0..i1={span_ab})")
            if span_aoi < tp.MIN_SPAN_BARS:
                fails.append(f"span_to_asof({span_aoi})")
            if slope < 0 and v0 < 0:
                fails.append("rule2")
            if slope > 0 and v0 > 0:
                fails.append("rule1")
            s0, s1 = np.sign(v0), np.sign(v1)
            if s0 != 0 and s1 != 0 and s0 != s1:
                fails.append(f"same-side(v0={v0:+.2f},v1={v1:+.2f})")
            if s0 == 0 or s1 == 0:
                fails.append("zero-anchor")
            if anchor_type == "peak_anchored" and slope >= 0:
                fails.append(f"peak+slope>=0({slope:+.4f})")
            if anchor_type == "trough_anchored" and slope <= 0:
                fails.append(f"trough+slope<=0({slope:+.4f})")
            brk = tp.has_origination_side_break(ext, i0, v0, i1, v1, asof_bar)
            if brk:
                fails.append("origin-side-break")
            status = "PASS" if not fails else f"FAIL: {','.join(fails)}"
            print(f"    i1 bar {i1} ({str(dates[i1])[:10]}) v1={v1:+.3f} slope={slope:+.4f}  [{status}]")


if __name__ == "__main__":
    main()
