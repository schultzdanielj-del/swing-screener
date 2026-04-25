"""Dump rate and crossings curves for the 5 calibration tickers at asof
2026-04-10, so we can see what shapes are driving the derivation gaps
(chop_upper mismatch, downside saturation) before designing fixes.

Shows for each ticker, each side (up/down):
  - L grid, crossings count, returned count, rate
  - Derivative of rate (rate[i+1] - rate[i])
  - Marker for current upside_1 derivation point
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reversal_profile_derive as rpd
import trendline_primitive_v6 as tp

TICKERS = ["AAPL", "MSFT", "TSLA", "RIVN", "SPY"]
ASOF = np.datetime64("2026-04-10")


def dump_side(ticker, side, L_grid, crossings, returned):
    with np.errstate(divide="ignore", invalid="ignore"):
        rate = np.where(crossings > 0, returned / np.maximum(crossings, 1), np.nan)
    valid = crossings > 0
    rates_valid = rate[valid]
    if rates_valid.size == 0:
        print(f"  no data")
        return
    med = float(np.median(rates_valid))
    print(f"  median rate = {med:.3f}")
    print(f"  {'L':>5} {'cross':>8} {'ret':>6} {'rate':>7} {'d_rate':>8} {'notes':<}")
    prev_rate = None
    for i, L in enumerate(L_grid):
        if not valid[i]:
            d_rate = ""
            prev_rate = None
            row = f"  {L:>5.1f} {crossings[i]:>8d} {returned[i]:>6d} {'nan':>7} {'':>8}"
            if L <= 12.0:  # keep print bounded
                pass
            continue
        r = rate[i]
        if prev_rate is None or np.isnan(prev_rate):
            d_rate_str = ""
        else:
            d_rate_str = f"{r - prev_rate:+.3f}"
        tag = ""
        if r > med and (prev_rate is None or prev_rate <= med):
            tag = " <-- first lift above median (upside_1 candidate)"
        print(f"  {L:>5.1f} {crossings[i]:>8d} {returned[i]:>6d} {r:>7.3f} {d_rate_str:>8}{tag}")
        prev_rate = r


def main():
    for ticker in TICKERS:
        dates, ext = tp.load_ticker_50ext(ticker)
        asof_bar = int(np.searchsorted(dates, ASOF, side="right") - 1)
        vals = ext[: asof_bar + 1].astype(float)
        vals_nn = vals[~np.isnan(vals)]
        max_abs = float(np.ceil(np.nanmax(np.abs(vals))))
        L_grid = np.arange(rpd.L_STEP, max_abs + 1.0 + rpd.L_STEP, rpd.L_STEP)

        up_c, up_r = rpd._build_curves(vals, L_grid, "up")
        dn_c, dn_r = rpd._build_curves(vals, L_grid, "down")

        print(f"\n{'=' * 72}")
        print(f"{ticker} (n_bars={len(vals)}, n_valid={(~np.isnan(vals)).sum()}, "
              f"max_abs_ext={max_abs:.1f}, L_max={L_grid[-1]:.1f})")
        print(f"{'=' * 72}")
        print(f"\nUPSIDE:")
        dump_side(ticker, "up", L_grid, up_c, up_r)
        print(f"\nDOWNSIDE:")
        dump_side(ticker, "down", L_grid, dn_c, dn_r)

        # Show current derivation output
        prof = rpd.derive_profile(ext, asof_bar)
        print(f"\nCurrent derivation: u1={prof['upside_1']:.2f}, u2={prof['upside_2']:.2f}, "
              f"chop_upper={prof['chop_upper']:.2f}, d1={prof['downside_1']:.2f}, "
              f"d2={prof['downside_2']:.2f}, chop_lower={prof['chop_lower']:.2f}")


if __name__ == "__main__":
    main()
