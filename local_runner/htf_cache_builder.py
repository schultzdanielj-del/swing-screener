"""
HTF Cache Builder — Weekly and Monthly OHLCV caches from yfinance.

Stores:
  - local_runner/cache/universe_ohlcv_weekly.pkl
  - local_runner/cache/universe_ohlcv_monthly.pkl

Same format as daily 5yr cache: dict of {ticker: pd.DataFrame}
Each DataFrame has columns: date, open, high, low, close, volume

Usage:
    # Full build (first time, ~20-30 min with rate limiting):
    python local_runner/htf_cache_builder.py --build

    # Nightly append (update current partial bar + append closed bars):
    python local_runner/htf_cache_builder.py --append

    # Status:
    python local_runner/htf_cache_builder.py --status

Requires: pip install yfinance pandas
"""

import os
import sys
import time
import pickle
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
CACHE_5YR_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")

WEEKLY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_weekly.pkl")
WEEKLY_META = os.path.join(CACHE_DIR, "cache_weekly_meta.txt")

MONTHLY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_monthly.pkl")
MONTHLY_META = os.path.join(CACHE_DIR, "cache_monthly_meta.txt")

MAX_WORKERS = 20
BATCH_SIZE = 50  # tickers per batch for full build (rate limit safety)
BATCH_SLEEP = 1.0  # seconds between batches


# ══════════════════════════════════════════════════════════════
# YFINANCE HELPERS
# ══════════════════════════════════════════════════════════════

def _yf_download_htf(ticker, interval, period="5y"):
    """Download weekly or monthly OHLCV for one ticker from yfinance.

    Args:
        ticker: stock symbol
        interval: '1wk' or '1mo'
        period: yfinance period string

    Returns (ticker, DataFrame) or (ticker, None)
    DataFrame has columns: date, open, high, low, close, volume
    """
    try:
        raw = yf.download(ticker, period=period, interval=interval, progress=False)
        if raw.empty:
            return ticker, None

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw.reset_index()
        df.columns = [c.lower() for c in df.columns]

        if "adj close" in df.columns:
            df = df.drop(columns=["adj close"], errors="ignore")

        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                return ticker, None
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date"] = pd.to_datetime(df["date"])
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df = df.sort_values("date").reset_index(drop=True)
        df = df.dropna(subset=["close"]).reset_index(drop=True)

        if len(df) < 3:
            return ticker, None

        return ticker, df
    except Exception:
        return ticker, None


def _yf_download_htf_recent(ticker, interval, period="1mo"):
    """Download recent HTF bars for appending.

    Args:
        ticker: stock symbol
        interval: '1wk' or '1mo'
        period: short period to fetch recent bars

    Returns (ticker, DataFrame) or (ticker, None)
    """
    return _yf_download_htf(ticker, interval, period)


# ══════════════════════════════════════════════════════════════
# FULL BUILD
# ══════════════════════════════════════════════════════════════

def _get_ticker_list():
    """Get ticker list from existing 5yr daily cache."""
    if not os.path.exists(CACHE_5YR_FILE):
        raise FileNotFoundError(
            f"Daily 5yr cache not found: {CACHE_5YR_FILE}\n"
            "  Run cache_builder.py --5yr first."
        )
    with open(CACHE_5YR_FILE, "rb") as f:
        universe = pickle.load(f)
    tickers = list(universe.keys())
    del universe  # free memory
    return tickers


def _build_one_cache(interval, output_file, meta_file, label):
    """Full build for one HTF timeframe.

    Downloads 5yr of data for all tickers in batches to avoid rate limiting.
    """
    print(f"\n  {'=' * 50}")
    print(f"  {label} OHLCV CACHE — Full Build")
    print(f"  {'=' * 50}")

    tickers = _get_ticker_list()
    print(f"  {len(tickers)} tickers to fetch")

    t0 = time.time()
    universe = {}
    failed = []
    n_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(n_batches):
        batch_start = batch_idx * BATCH_SIZE
        batch_end = min(batch_start + BATCH_SIZE, len(tickers))
        batch = tickers[batch_start:batch_end]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_yf_download_htf, t, interval, "5y"): t
                for t in batch
            }
            for future in as_completed(futures):
                ticker, df = future.result()
                if df is not None:
                    universe[ticker] = df
                else:
                    failed.append(ticker)

        done = batch_end
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(tickers) - done) / rate if rate > 0 else 0
        print(f"    {done:,}/{len(tickers):,} "
              f"({len(universe):,} ok, {len(failed)} failed) "
              f"[{elapsed/60:.1f}m elapsed, ~{eta/60:.1f}m left]")

        # Sleep between batches to avoid rate limiting
        if batch_idx < n_batches - 1:
            time.sleep(BATCH_SLEEP)

    elapsed = time.time() - t0

    # Save
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(output_file, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(meta_file, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(output_file) / 1024 / 1024
    print(f"\n  {label} build complete:")
    print(f"    Tickers: {len(universe):,} ok, {len(failed)} failed")
    print(f"    File: {output_file} ({size_mb:.1f} MB)")
    print(f"    Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")

    if failed and len(failed) <= 20:
        print(f"    Failed: {', '.join(failed[:20])}")

    return universe


def build_htf_caches():
    """Full build of both weekly and monthly OHLCV caches."""
    print("\n" + "=" * 60)
    print("  HTF CACHE BUILDER — Weekly + Monthly OHLCV from yfinance")
    print("=" * 60)

    weekly = _build_one_cache("1wk", WEEKLY_FILE, WEEKLY_META, "WEEKLY")
    monthly = _build_one_cache("1mo", MONTHLY_FILE, MONTHLY_META, "MONTHLY")

    return weekly, monthly


# ══════════════════════════════════════════════════════════════
# NIGHTLY APPEND
# ══════════════════════════════════════════════════════════════

def _merge_htf_bars(existing_df, new_df):
    """Merge new HTF bars into existing cache.

    Rules:
    - If new bar's date matches last cached bar: overwrite (partial period updated)
    - If new bar's date is after last cached bar: append (new period started)
    - Bars before last cached bar: ignore (frozen history)

    Returns updated DataFrame.
    """
    if existing_df is None or len(existing_df) == 0:
        return new_df
    if new_df is None or len(new_df) == 0:
        return existing_df

    last_cached_date = existing_df["date"].iloc[-1]
    rows_to_append = []

    for _, row in new_df.iterrows():
        row_date = row["date"]

        if row_date == last_cached_date:
            # Overwrite last bar (partial period — close/high/low may have changed)
            for col in ["open", "high", "low", "close", "volume"]:
                existing_df.at[existing_df.index[-1], col] = row[col]
        elif row_date > last_cached_date:
            # New period — collect for append
            rows_to_append.append(row)
            last_cached_date = row_date
        # else: row_date < last_cached_date → frozen, skip

    if rows_to_append:
        append_df = pd.DataFrame(rows_to_append)
        existing_df = pd.concat([existing_df, append_df], ignore_index=True)
        existing_df = existing_df.sort_values("date").reset_index(drop=True)

    return existing_df


def _append_one_cache(interval, output_file, meta_file, label, recent_period):
    """Nightly append for one HTF timeframe.

    For each ticker in the existing cache:
    - Fetch recent bars from yfinance
    - Merge: overwrite partial period, append new closed periods

    New tickers (in 5yr daily but not in HTF cache) get full 5yr fetch.
    """
    print(f"\n  {label} OHLCV Cache — Append")

    if not os.path.exists(output_file):
        print(f"  No existing {label.lower()} cache. Running full build...")
        return _build_one_cache(interval, output_file, meta_file, label)

    # Load existing
    with open(output_file, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe)} tickers in cache")

    # Check for new tickers from 5yr daily cache
    try:
        all_tickers = _get_ticker_list()
        new_tickers = [t for t in all_tickers if t not in universe]
    except FileNotFoundError:
        all_tickers = list(universe.keys())
        new_tickers = []

    to_update = list(universe.keys())

    print(f"  Tickers to update: {len(to_update)}")
    if new_tickers:
        print(f"  New tickers (full fetch): {len(new_tickers)}")

    t0 = time.time()
    updated = 0
    no_change = 0
    failed = 0

    # Update existing tickers — fetch recent bars
    if to_update:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_yf_download_htf_recent, t, interval, recent_period): t
                for t in to_update
            }
            done = 0
            for future in as_completed(futures):
                ticker = futures[future]
                done += 1
                try:
                    _, new_df = future.result()
                    if new_df is not None and len(new_df) > 0:
                        old_len = len(universe[ticker])
                        old_last = str(universe[ticker]["date"].iloc[-1])[:10]
                        universe[ticker] = _merge_htf_bars(universe[ticker], new_df)
                        new_len = len(universe[ticker])
                        new_last = str(universe[ticker]["date"].iloc[-1])[:10]
                        if new_len != old_len or new_last != old_last:
                            updated += 1
                        else:
                            no_change += 1
                    else:
                        no_change += 1
                except Exception:
                    failed += 1

                if done % 500 == 0 or done == len(to_update):
                    elapsed = time.time() - t0
                    print(f"    {done:,}/{len(to_update):,} checked "
                          f"({updated} updated, {no_change} current, {failed} failed) "
                          f"[{elapsed:.0f}s]")

    # Full fetch for new tickers
    new_added = 0
    if new_tickers:
        print(f"\n  Fetching {len(new_tickers)} new tickers...")
        # Batch new tickers to avoid rate limits
        for batch_start in range(0, len(new_tickers), BATCH_SIZE):
            batch = new_tickers[batch_start:batch_start + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(_yf_download_htf, t, interval, "5y"): t
                    for t in batch
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        _, df = future.result()
                        if df is not None and len(df) >= 3:
                            universe[ticker] = df
                            new_added += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
            if batch_start + BATCH_SIZE < len(new_tickers):
                time.sleep(BATCH_SLEEP)

    elapsed = time.time() - t0

    # Save
    with open(output_file, "wb") as f:
        pickle.dump(universe, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(meta_file, "w") as f:
        f.write(datetime.now().isoformat())

    size_mb = os.path.getsize(output_file) / 1024 / 1024
    print(f"  {label} append: {updated} updated, {new_added} new, "
          f"{no_change} unchanged, {failed} failed ({elapsed:.0f}s, {size_mb:.1f} MB)")

    return universe


def append_weekly():
    """Nightly append for weekly cache."""
    return _append_one_cache(
        interval="1wk",
        output_file=WEEKLY_FILE,
        meta_file=WEEKLY_META,
        label="Weekly",
        recent_period="1mo"  # last month covers current + recently closed weeks
    )


def append_monthly():
    """Nightly append for monthly cache."""
    return _append_one_cache(
        interval="1mo",
        output_file=MONTHLY_FILE,
        meta_file=MONTHLY_META,
        label="Monthly",
        recent_period="3mo"  # last 3 months covers current + recently closed months
    )


def append_htf_caches():
    """Nightly append for both weekly and monthly caches."""
    append_weekly()
    append_monthly()


# ══════════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════════

def show_status():
    """Show HTF cache status."""
    print("\n  HTF OHLCV Cache Status")
    print("  " + "─" * 40)

    for label, pkl_file, meta_file in [
        ("Weekly", WEEKLY_FILE, WEEKLY_META),
        ("Monthly", MONTHLY_FILE, MONTHLY_META),
    ]:
        if not os.path.exists(pkl_file):
            print(f"\n  {label}: not built")
            continue

        with open(pkl_file, "rb") as f:
            data = pickle.load(f)

        size_mb = os.path.getsize(pkl_file) / 1024 / 1024
        bar_counts = [len(df) for df in data.values()]

        meta_ts = "unknown"
        if os.path.exists(meta_file):
            with open(meta_file) as f:
                meta_ts = f.read().strip()

        print(f"\n  {label}:")
        print(f"    Tickers: {len(data):,}")
        print(f"    File: {size_mb:.1f} MB")
        print(f"    Updated: {meta_ts}")
        if bar_counts:
            print(f"    Bars: min={min(bar_counts)}, "
                  f"avg={sum(bar_counts)/len(bar_counts):.0f}, "
                  f"max={max(bar_counts)}")

        # Sample last dates
        sample_tickers = ["SPY", "AAPL", "MSFT"]
        for t in sample_tickers:
            if t in data and len(data[t]) > 0:
                last = str(data[t]["date"].iloc[-1])[:10]
                print(f"    {t} last bar: {last} ({len(data[t])} bars)")

        del data


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="HTF OHLCV Cache Builder (Weekly + Monthly)")
    parser.add_argument("--build", action="store_true", help="Full build from yfinance")
    parser.add_argument("--append", action="store_true", help="Nightly append")
    parser.add_argument("--status", action="store_true", help="Show cache status")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.append:
        append_htf_caches()
    elif args.build:
        build_htf_caches()
    else:
        parser.print_help()
        print("\n  Hint: Run --build for first time, --append for nightly updates.")


if __name__ == "__main__":
    main()
