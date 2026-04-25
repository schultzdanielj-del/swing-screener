"""Diagnostic: trace why a specific expected trendline anchor is not
emitted by v6. Edit TICKER / TARGET_DT / SIDE to trace. SIDE = 'trough'
for ascending (lower cascade), 'peak' for descending (upper cascade)."""
import sys

import numpy as np

import trendline_primitive_v6 as tp


def main():
    TICKER = sys.argv[1] if len(sys.argv) > 1 else "CAR"
    TARGET_DT = sys.argv[2] if len(sys.argv) > 2 else "2026-02-03"
    SIDE = sys.argv[3] if len(sys.argv) > 3 else "trough"

    dates, ext = tp.load_ticker_50ext(TICKER)
    asof_dt = np.datetime64("2026-04-10")
    asof_bar = int(np.searchsorted(dates, asof_dt, side="right") - 1)
    window_start = max(0, asof_bar - tp.WINDOW_BARS)

    target_dt = np.datetime64(TARGET_DT)
    target_bar = int(np.searchsorted(dates, target_dt, side="right") - 1)
    print(f"TICKER={TICKER}  SIDE={SIDE}")
    print(f"asof_bar = {asof_bar} ({str(dates[asof_bar])[:10]})")
    print(f"target i0 bar = {target_bar} ({str(dates[target_bar])[:10]}), ext = {ext[target_bar]:+.3f}")
    print(f"window = bars {window_start}..{asof_bar}")

    # raw trough pivots
    from scipy.signal import find_peaks
    troughs, props = find_peaks(-np.nan_to_num(ext, nan=1e9), prominence=tp.PROMINENCE)
    troughs_in_win = troughs[(troughs <= asof_bar) & (troughs >= window_start)]
    print(f"\nRAW troughs in window (prominence >= {tp.PROMINENCE}): {len(troughs_in_win)}")
    for t in troughs_in_win:
        print(f"  bar {t} = {str(dates[t])[:10]} ext={ext[t]:+.3f}")

    # slid troughs
    peak_i, peak_v, trough_i, trough_v = tp.find_anchors(ext, asof_bar, window_start)
    print(f"\nSLID troughs in window: {len(trough_i)}")
    for i, v in zip(trough_i, trough_v):
        marker = " <-- near target" if abs(int(i) - target_bar) <= 10 else ""
        print(f"  bar {i} = {str(dates[int(i)])[:10]} ext={v:+.3f}{marker}")

    # check with lower prominence
    for prom in [0.5, 0.25, 0.1]:
        troughs2, _ = find_peaks(-np.nan_to_num(ext, nan=1e9), prominence=prom)
        t2_in_win = troughs2[(troughs2 <= asof_bar) & (troughs2 >= window_start)]
        has_target = any(abs(int(t) - target_bar) <= 10 for t in t2_in_win)
        print(f"\nprominence={prom}: {len(t2_in_win)} troughs in window, target-near-by-10bars: {has_target}")

    # Enumerate ascending pairs with i0 within ±10 bars of target, print gate failures
    print(f"\n--- ascending pair tracing (i0 within ±10 bars of target_bar={target_bar}) ---")
    for ia_idx, (i0_raw, v0) in enumerate(zip(trough_i, trough_v)):
        i0 = int(i0_raw)
        if abs(i0 - target_bar) > 10:
            continue
        print(f"\n  candidate i0 = bar {i0} ({str(dates[i0])[:10]}) v0={v0:+.3f}")
        for ib_idx in range(ia_idx + 1, len(trough_i)):
            i1 = int(trough_i[ib_idx])
            v1 = float(trough_v[ib_idx])
            if i1 <= i0:
                continue
            span_ab = i1 - i0
            span_aoi = asof_bar - i0
            slope = (v1 - v0) / (i1 - i0)
            fails = []
            if span_ab < tp.MIN_SPAN_BARS:
                fails.append(f"MIN_SPAN_BARS(i0..i1={span_ab})")
            if span_aoi < tp.MIN_SPAN_BARS:
                fails.append(f"span_to_asof({span_aoi})")
            if slope < 0 and v0 < 0:
                fails.append("rule2")
            if slope > 0 and v0 > 0:
                fails.append("rule1")
            s0, s1 = np.sign(v0), np.sign(v1)
            if s0 != 0 and s1 != 0 and s0 != s1:
                fails.append(f"same-side(v0={v0:+.2f} v1={v1:+.2f})")
            if s0 == 0 or s1 == 0:
                fails.append("zero-anchor")
            if slope <= 0:
                fails.append(f"trough+slope<=0({slope:+.4f})")
            brk = tp.has_origination_side_break(ext, i0, v0, i1, v1, asof_bar)
            if brk:
                fails.append("origin-side-break")
            status = "PASS" if not fails else f"FAIL: {','.join(fails)}"
            print(f"    -> i1 bar {i1} ({str(dates[i1])[:10]}) v1={v1:+.3f} slope={slope:+.4f}  [{status}]")


if __name__ == "__main__":
    main()
