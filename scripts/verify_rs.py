"""Quick verification: compute RS_D1 for MSFT on 2025-10-27 and print it.

Expected from manual calculation: approximately -13.13

Usage: python scripts/verify_rs.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from scripts.setup_grinder import (
        load_daily_ohlcv, build_rs_lookup, _resample_to_weekly,
        find_latest_refinement, load_signals_from_refinement,
        compute_adr_14, compute_dollar_volume_20d, _find_bar_idx
    )

    print("Loading OHLCV cache...")
    ohlcv = load_daily_ohlcv()
    print(f"  {len(ohlcv)} tickers")

    # Compute RS for just MSFT and SPY (no parallelism needed)
    msft_df = ohlcv["MSFT"]
    spy_df = ohlcv["SPY"]

    print("\nComputing MSFT RS...")
    msft_weekly = _resample_to_weekly(msft_df)
    msft_d1, msft_w1 = build_rs_lookup(msft_df, msft_weekly)

    print("Computing SPY RS...")
    spy_weekly = _resample_to_weekly(spy_df)
    spy_d1, spy_w1 = build_rs_lookup(spy_df, spy_weekly)

    date = "2025-10-27"
    print(f"\n=== MSFT {date} ===")
    print(f"  MSFT D1 raw: {msft_d1.get(date)}")
    print(f"  SPY  D1 raw: {spy_d1.get(date)}")

    if msft_d1.get(date) is not None and spy_d1.get(date) is not None:
        rs_d1 = msft_d1[date] - spy_d1[date]
        print(f"  RS_D1 (MSFT - SPY): {rs_d1:.4f}")
        print(f"\n  Expected: ~-13.13")
        diff = abs(rs_d1 - (-13.1341))
        print(f"  Difference: {diff:.4f}")
        if diff < 0.5:
            print("  PASS")
        else:
            print(f"  FAIL - off by {diff:.2f}")
    else:
        print("  Missing data for this date")

    if msft_w1.get(date) is not None and spy_w1.get(date) is not None:
        rs_w1 = msft_w1[date] - spy_w1[date]
        print(f"\n  RS_W1 (MSFT - SPY): {rs_w1:.4f}")

    # Also verify other features for this signal
    dates = [str(d)[:10] for d in msft_df["date"].values]
    bar_idx = _find_bar_idx(dates, date)
    if bar_idx >= 0:
        adr = compute_adr_14(msft_df, bar_idx)
        dv = compute_dollar_volume_20d(msft_df, bar_idx)
        price = float(msft_df.iloc[bar_idx]["close"])
        print(f"\n  Price: {price:.2f}")
        print(f"  ADR14: {adr:.4f}")
        print(f"  DolVol20d: {dv:,.0f}")
        print(f"  Days since IPO: {bar_idx}")


if __name__ == "__main__":
    main()
