"""
Universe breadth-aggregate precompute (worktree-local).

Reads the main repo's universe_ohlcv_daily.pkl (~11,500 tickers, ~920 MB)
read-only, applies the standard per-bar tradable filter (close >= $1,
dvol_20d >= $4M, 20-bar ADRP >= 1.8%), and writes four daily breadth
aggregates to regime_meter/cache/breadth_daily.parquet:

  - pct_above_50ma                  % of eligible tradable tickers > 50-bar SMA
  - stockbee_4pct_ratio_10d         10-day-sum-up / 10-day-sum-down of +/-4% bars
  - stockbee_25pct_up_count_63d     count of tradable tickers up   >= 25% over 63 bars
  - stockbee_25pct_down_count_63d   count of tradable tickers down >= 25% over 63 bars

Default: full rebuild. Idempotent overwrite.

Reads main-repo caches read-only. All writes stay inside the worktree.
"""
import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd


# --- Paths -----------------------------------------------------------------
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
OUT_PARQUET   = os.path.join(CACHE_DIR, "breadth_daily.parquet")

MAIN_REPO_CACHE = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache"
SPY_PICKLE_PATH = os.path.join(MAIN_REPO_CACHE, "market_ohlcv.pkl")
UNIVERSE_PICKLE = os.path.join(MAIN_REPO_CACHE, "universe_ohlcv_daily.pkl")

# --- Window ----------------------------------------------------------------
HISTORY_START = "2016-01-01"

# --- Tradable filter thresholds (per OHLCV_CACHE.md) ----------------------
MIN_CLOSE = 1.00
MIN_DVOL  = 4_000_000
MIN_ADRP  = 1.8

# --- Lookbacks -------------------------------------------------------------
ADRP_WINDOW     = 20
SMA50_WINDOW    = 50
STOCKBEE_WINDOW = 10
QUARTER_BARS    = 63
PCT_4           = 0.04
PCT_25          = 0.25

AGG_COLUMNS = [
    "pct_above_50ma",
    "stockbee_4pct_ratio_10d",
    "stockbee_25pct_up_count_63d",
    "stockbee_25pct_down_count_63d",
]


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def load_spy_calendar():
    if not os.path.exists(SPY_PICKLE_PATH):
        sys.exit(f"ABORT: SPY pickle missing at {SPY_PICKLE_PATH}")
    with open(SPY_PICKLE_PATH, "rb") as f:
        cache = pickle.load(f)
    if "SPY" not in cache:
        sys.exit("ABORT: SPY not in market_ohlcv.pkl")
    dates = pd.to_datetime(cache["SPY"]["date"]).sort_values().reset_index(drop=True)
    dates = dates[dates >= pd.Timestamp(HISTORY_START)].reset_index(drop=True)
    print(
        f"  SPY calendar: {len(dates)} trading days "
        f"({dates.iloc[0].date()} -> {dates.iloc[-1].date()})"
    )
    return pd.DatetimeIndex(dates)


def load_universe():
    if not os.path.exists(UNIVERSE_PICKLE):
        sys.exit(f"ABORT: universe pickle missing at {UNIVERSE_PICKLE}")
    print(f"  Loading universe pickle ({UNIVERSE_PICKLE}) ...")
    with open(UNIVERSE_PICKLE, "rb") as f:
        cache = pickle.load(f)
    print(f"  Universe: {len(cache)} tickers")
    return cache


def align_universe(cache, master_dates):
    """Reindex every ticker's close/high/low/dvol_20d to the SPY calendar.

    Returns four (n_dates, n_tickers) DataFrames. Tickers without enough
    bars to ever pass the filter still get included; their rows are NaN
    where they have no data and the tradable mask drops them.
    """
    tickers = sorted(cache.keys())
    print(f"  Aligning {len(tickers)} tickers to {len(master_dates)} SPY dates ...")

    closes, highs, lows, dvols = {}, {}, {}, {}
    for i, t in enumerate(tickers):
        df = cache[t]
        if "dvol_20d" not in df.columns:
            sys.exit(f"ABORT: ticker {t!r} missing 'dvol_20d' column")
        s = (
            df.set_index(pd.to_datetime(df["date"]))[
                ["close", "high", "low", "dvol_20d"]
            ]
            .reindex(master_dates)
        )
        closes[t] = s["close"]
        highs[t]  = s["high"]
        lows[t]   = s["low"]
        dvols[t]  = s["dvol_20d"]
        if (i + 1) % 2000 == 0:
            print(f"    {i + 1}/{len(tickers)} aligned")

    close_mat = pd.DataFrame(closes, index=master_dates)
    high_mat  = pd.DataFrame(highs,  index=master_dates)
    low_mat   = pd.DataFrame(lows,   index=master_dates)
    dvol_mat  = pd.DataFrame(dvols,  index=master_dates)
    print(f"  Aligned matrices: {close_mat.shape} (dates, tickers)")
    return close_mat, high_mat, low_mat, dvol_mat


def compute_tradable_mask(close_mat, high_mat, low_mat, dvol_mat):
    print(f"  Computing tradable mask ...")
    hl_ratio = high_mat / low_mat
    adrp = (
        hl_ratio.rolling(ADRP_WINDOW, min_periods=ADRP_WINDOW).mean() - 1
    ) * 100

    tradable = (
        (close_mat >= MIN_CLOSE)
        & (dvol_mat >= MIN_DVOL)
        & (adrp >= MIN_ADRP)
    ).fillna(False)

    n_bars_total    = int(tradable.size)
    n_bars_tradable = int(tradable.values.sum())
    pct = (n_bars_tradable / n_bars_total) * 100
    print(
        f"  Tradable bars: {n_bars_tradable:,} / {n_bars_total:,} "
        f"({pct:.2f}% of aligned cells)"
    )
    return tradable


def compute_aggregates(close_mat, tradable):
    print(f"  Computing 4 breadth aggregates ...")

    # 1. % above 50-MA among eligible tradable tickers
    sma50         = close_mat.rolling(SMA50_WINDOW, min_periods=SMA50_WINDOW).mean()
    eligible_50   = tradable & sma50.notna()
    above50       = (close_mat > sma50) & eligible_50
    n_eligible    = eligible_50.sum(axis=1).replace(0, np.nan)
    pct_above_50  = (above50.sum(axis=1) / n_eligible) * 100

    # 2. Stockbee 10-day 4% ratio
    ret_1d        = close_mat.pct_change(1, fill_method=None)
    up4_count     = ((ret_1d >=  PCT_4) & tradable).sum(axis=1)
    down4_count   = ((ret_1d <= -PCT_4) & tradable).sum(axis=1)
    sum_up_10d    = up4_count.rolling(STOCKBEE_WINDOW, min_periods=STOCKBEE_WINDOW).sum()
    sum_down_10d  = down4_count.rolling(STOCKBEE_WINDOW, min_periods=STOCKBEE_WINDOW).sum()
    stockbee_4pct = sum_up_10d / sum_down_10d.replace(0, np.nan)

    # 3 & 4. 25% breadth over 63 bars
    ret_63        = close_mat.pct_change(QUARTER_BARS, fill_method=None)
    up25_count    = ((ret_63 >=  PCT_25) & tradable).sum(axis=1).astype("float64")
    down25_count  = ((ret_63 <= -PCT_25) & tradable).sum(axis=1).astype("float64")
    up25_count.iloc[:QUARTER_BARS]   = np.nan
    down25_count.iloc[:QUARTER_BARS] = np.nan

    return pd.DataFrame({
        "date":                          close_mat.index.values,
        "pct_above_50ma":                pct_above_50.values,
        "stockbee_4pct_ratio_10d":       stockbee_4pct.values,
        "stockbee_25pct_up_count_63d":   up25_count.values,
        "stockbee_25pct_down_count_63d": down25_count.values,
    })


def trim_to_first_valid(df):
    valid_mask = df[AGG_COLUMNS].notna().all(axis=1)
    if not valid_mask.any():
        sys.exit("ABORT: no date has all 4 aggregates non-NaN")
    first_idx = int(valid_mask.idxmax())
    print(
        f"  First all-non-NaN date: {pd.Timestamp(df['date'].iloc[first_idx]).date()} "
        f"(row {first_idx})"
    )
    return df.iloc[first_idx:].reset_index(drop=True)


def run_sanity_checks(df):
    print("\n  Sanity checks:")
    print(f"    Output rows:   {len(df)}")
    print(f"    First date:    {pd.Timestamp(df['date'].iloc[0]).date()}")
    print(f"    Last date:     {pd.Timestamp(df['date'].iloc[-1]).date()}")

    for col in AGG_COLUMNS:
        n_nan = int(df[col].isna().sum())
        if n_nan != 0:
            print(f"    WARN: {col!r} has {n_nan} NaN row(s) after trim")

    last = df.iloc[-1]
    last_date = pd.Timestamp(last["date"]).date()
    print(f"\n    Most-recent date ({last_date}) values:")
    print(f"      pct_above_50ma:                  {last['pct_above_50ma']:.2f}")
    print(f"      stockbee_4pct_ratio_10d:         {last['stockbee_4pct_ratio_10d']:.3f}")
    print(f"      stockbee_25pct_up_count_63d:     {int(last['stockbee_25pct_up_count_63d'])}")
    print(f"      stockbee_25pct_down_count_63d:   {int(last['stockbee_25pct_down_count_63d'])}")


def write_output(df, out_path):
    _assert_inside_worktree(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"\n  Writing -> {out_path}")
    df.to_parquet(out_path, index=False)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"  {size_kb:.1f} KB written")


def main():
    parser = argparse.ArgumentParser(
        description="Universe breadth-aggregate precompute (worktree-local)"
    )
    parser.parse_args()

    print("=" * 70)
    print("  UNIVERSE BREADTH AGGREGATE PRECOMPUTE (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(OUT_PARQUET)
    print(f"  Worktree root:   {WORKTREE_ROOT}")
    print(f"  Output:          {OUT_PARQUET}")
    print(f"  Main repo cache: {MAIN_REPO_CACHE}")

    print()
    spy_dates = load_spy_calendar()

    print()
    universe = load_universe()

    print()
    close_mat, high_mat, low_mat, dvol_mat = align_universe(universe, spy_dates)
    del universe

    print()
    tradable = compute_tradable_mask(close_mat, high_mat, low_mat, dvol_mat)
    del high_mat, low_mat, dvol_mat

    print()
    df = compute_aggregates(close_mat, tradable)

    df = trim_to_first_valid(df)
    run_sanity_checks(df)
    write_output(df, OUT_PARQUET)

    print("\n  DONE.")


if __name__ == "__main__":
    main()
