"""
Backtest Runner — Visual verification of pyramid grinder signals.

Loads pyramid grinder conditions, scans all tickers across full history,
generates candlestick charts for each signal for visual review.

This is Step 6 of the build plan. The grinder found conditions mathematically,
now we need to verify the signals *look* like real setups.

Usage:
    python scripts/backtest_runner.py [--setup dtss] [--max-charts 200] [--no-charts]
    python scripts/backtest_runner.py --setup dtss --charts-only  # skip scan, regenerate charts from CSV

Outputs:
    local_runner/cache/backtest_signals_{setup}.csv     — all signals (date, ticker, bar_idx)
    local_runner/cache/backtest_summary_{setup}.txt     — summary statistics
    local_runner/cache/backtest_charts_{setup}/         — chart images per signal

Requires:
    - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
    - Pyramid grinder results (local_runner/cache/pyramid_results_{setup}.json
      or historical_results_{setup}.json)
"""

import os
import sys
import time
import json
import pickle
import argparse
import numpy as np
import pandas as pd
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import multiprocessing as mp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series


# ══════════════════════════════════════════════════════════════
# SIGNAL SCANNER (parallel)
# ══════════════════════════════════════════════════════════════

_worker_cache = None
_worker_conditions = None


def _init_scan_worker(cache, conditions):
    global _worker_cache, _worker_conditions
    _worker_cache = cache
    _worker_conditions = conditions


def _scan_batch(tickers):
    """Scan a batch of tickers for all conditions. Returns (signals, skipped)."""
    signals = []
    skipped = 0
    for ticker in tickers:
        df = _worker_cache.get(ticker)
        if df is None or len(df) < 100:
            skipped += 1
            continue
        try:
            engine = ExpressionEngine(df)
            n_bars = len(df)
            pass_mask = np.ones(n_bars, dtype=bool)
            pass_mask[:50] = False  # skip warmup

            for cond in _worker_conditions:
                series = compute_series(engine, cond["compute"])
                low, high = cond["low"], cond["high"]
                in_range = (series >= low) & (series <= high)
                in_range[np.isnan(series)] = False
                pass_mask &= in_range

            signal_indices = np.where(pass_mask)[0]
            if len(signal_indices) > 0:
                dates = df["date"].values
                for idx in signal_indices:
                    d = str(dates[idx])[:10]
                    signals.append({
                        "date": d,
                        "ticker": ticker,
                        "bar_idx": int(idx),
                    })
        except Exception:
            skipped += 1
    return signals, skipped


def scan_all_signals(universe_cache, conditions):
    """Scan all tickers in parallel, return list of signal dicts."""
    n_workers = max(mp.cpu_count() - 1, 1)
    tickers = list(universe_cache.keys())
    batch_size = max(1, len(tickers) // (n_workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    print(f"\n  Scanning {len(tickers):,} tickers × {len(conditions)} conditions...")
    print(f"  {n_workers} workers, {len(batches)} batches of ~{batch_size}")
    t0 = time.time()

    all_signals = []
    total_skipped = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_scan_worker,
        initargs=(universe_cache, conditions)
    ) as pool:
        futures = [pool.submit(_scan_batch, batch) for batch in batches]
        done = 0
        for future in futures:
            batch_signals, batch_skipped = future.result()
            all_signals.extend(batch_signals)
            total_skipped += batch_skipped
            done += 1
            if done % max(len(batches) // 5, 1) == 0 or done == len(batches):
                elapsed = time.time() - t0
                pct = done / len(batches) * 100
                print(f"    {pct:.0f}% [{elapsed:.0f}s] {len(all_signals):,} signals")

    elapsed = time.time() - t0
    print(f"\n  Done: {len(all_signals):,} signals in {elapsed:.0f}s "
          f"(skipped {total_skipped})")
    return all_signals


# ══════════════════════════════════════════════════════════════
# CHART GENERATION
# ══════════════════════════════════════════════════════════════

def _build_dark_style():
    """Custom dark style matching config.yaml preferences."""
    import mplfinance as mpf
    return mpf.make_mpf_style(
        base_mpf_style='nightclouds',
        marketcolors=mpf.make_marketcolors(
            up='#26A69A',
            down='#EF5350',
            edge={'up': '#26A69A', 'down': '#EF5350'},
            wick={'up': '#26A69A', 'down': '#EF5350'},
            volume={'up': '#26A69A', 'down': '#EF5350'},
        ),
    )


def generate_signal_chart(ticker, df, signal_bar_idx, output_path,
                          lookback=120, forward=15):
    """Generate a candlestick chart for a single signal with entry candle marked.

    Args:
        ticker: Stock symbol
        df: Full OHLCV DataFrame for this ticker
        signal_bar_idx: Index of the signal bar in the DataFrame
        output_path: Where to save the chart image
        lookback: Bars before signal to show
        forward: Bars after signal to show

    Returns:
        True if chart generated successfully, False otherwise
    """
    import mplfinance as mpf
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend

    try:
        n_bars = len(df)
        start = max(0, signal_bar_idx - lookback)
        end = min(n_bars, signal_bar_idx + forward + 1)

        chart_df = df.iloc[start:end].copy()
        if len(chart_df) < 10:
            return False

        # Prepare DataFrame for mplfinance (needs DatetimeIndex)
        chart_df = chart_df.copy()
        chart_df["date"] = pd.to_datetime(chart_df["date"])
        chart_df = chart_df.set_index("date")

        # Rename columns to mplfinance standard
        col_map = {}
        for col in chart_df.columns:
            cl = col.lower()
            if cl == "open":
                col_map[col] = "Open"
            elif cl == "high":
                col_map[col] = "High"
            elif cl == "low":
                col_map[col] = "Low"
            elif cl == "close":
                col_map[col] = "Close"
            elif cl == "volume":
                col_map[col] = "Volume"
        chart_df = chart_df.rename(columns=col_map)

        # Ensure numeric
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in chart_df.columns:
                chart_df[col] = pd.to_numeric(chart_df[col], errors="coerce")

        chart_df = chart_df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(chart_df) < 10:
            return False

        # Build moving average overlays
        addplots = []
        ma_configs = [
            (8, "ema", "#ADD8E6", 1.0),    # Light blue
            (21, "ema", "#D2B48C", 1.0),   # Tan
            (50, "sma", "#FFD700", 1.2),   # Yellow
            (200, "sma", "#FF0000", 1.5),  # Red
        ]
        for period, ma_type, color, width in ma_configs:
            if len(chart_df) >= period:
                if ma_type == "ema":
                    ma_series = chart_df["Close"].ewm(span=period, adjust=False).mean()
                else:
                    ma_series = chart_df["Close"].rolling(window=period).mean()
                addplots.append(
                    mpf.make_addplot(ma_series, color=color, width=width)
                )

        # Mark the signal/entry candle with a scatter marker
        # Create a series with NaN everywhere except the signal bar
        signal_date_in_chart = signal_bar_idx - start
        if 0 <= signal_date_in_chart < len(chart_df):
            marker_series = pd.Series(np.nan, index=chart_df.index)
            marker_series.iloc[signal_date_in_chart] = (
                chart_df["Low"].iloc[signal_date_in_chart] * 0.98
            )
            addplots.append(
                mpf.make_addplot(
                    marker_series,
                    type='scatter',
                    marker='^',
                    markersize=100,
                    color='#FF00FF',  # Magenta triangle below entry candle
                )
            )

        # Get signal date for title
        signal_date_str = str(df["date"].iloc[signal_bar_idx])[:10]
        title = f"{ticker} — {signal_date_str}"

        style = _build_dark_style()

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        mpf.plot(
            chart_df,
            type='candle',
            volume=True,
            title=title,
            style=style,
            figsize=(14, 8),
            addplot=addplots if addplots else None,
            savefig={'fname': output_path, 'dpi': 100, 'bbox_inches': 'tight'},
            warn_too_much_data=500,
        )

        # Close figure to free memory
        import matplotlib.pyplot as plt
        plt.close('all')

        return True

    except Exception as e:
        print(f"    ✗ Chart failed for {ticker} bar {signal_bar_idx}: {e}")
        return False


def generate_all_charts(signals_df, universe_cache, setup_type, max_charts=200):
    """Generate charts for all signals (or up to max_charts).

    Charts are organized by date in the output folder.
    """
    charts_dir = os.path.join(CACHE_DIR, f"backtest_charts_{setup_type}")
    os.makedirs(charts_dir, exist_ok=True)

    n_signals = len(signals_df)
    if n_signals > max_charts:
        print(f"\n  {n_signals} signals > max_charts={max_charts}. "
              f"Generating charts for all {n_signals} (override with --max-charts).")

    print(f"\n  Generating {n_signals} charts → {charts_dir}")
    t0 = time.time()
    generated = 0
    failed = 0

    for i, row in signals_df.iterrows():
        ticker = row["ticker"]
        bar_idx = int(row["bar_idx"])
        date_str = row["date"]

        df = universe_cache.get(ticker)
        if df is None:
            failed += 1
            continue

        # Organize by date folder
        date_dir = os.path.join(charts_dir, date_str)
        filename = f"{ticker}_{date_str}.png"
        output_path = os.path.join(date_dir, filename)

        if os.path.exists(output_path):
            generated += 1
            continue

        ok = generate_signal_chart(ticker, df, bar_idx, output_path)
        if ok:
            generated += 1
        else:
            failed += 1

        if (generated + failed) % 20 == 0:
            elapsed = time.time() - t0
            total = generated + failed
            rate = total / elapsed if elapsed > 0 else 0
            eta = (n_signals - total) / rate if rate > 0 else 0
            print(f"    {total}/{n_signals} [{elapsed:.0f}s, ~{eta:.0f}s remaining] "
                  f"{generated} ok, {failed} failed")

    elapsed = time.time() - t0
    print(f"\n  Charts done: {generated} generated, {failed} failed in {elapsed:.0f}s")
    print(f"  Output: {charts_dir}")
    return charts_dir


# ══════════════════════════════════════════════════════════════
# SUMMARY STATISTICS
# ══════════════════════════════════════════════════════════════

def generate_summary(signals_df, setup_type, conditions):
    """Generate and print summary statistics, save to file."""
    lines = []

    def p(msg=""):
        print(msg)
        lines.append(msg)

    daily_counts = signals_df.groupby("date").size()
    ticker_counts = signals_df.groupby("ticker").size().sort_values(ascending=False)

    p(f"\n{'=' * 70}")
    p(f"  BACKTEST RUNNER — {setup_type.upper()} SIGNAL SUMMARY")
    p(f"{'=' * 70}")
    p(f"  Conditions: {len(conditions)}")
    p(f"  Total signals: {len(signals_df):,}")
    p(f"  Days with signals: {len(daily_counts)}")
    p(f"  Unique tickers: {signals_df['ticker'].nunique()}")

    if len(daily_counts) > 0:
        counts = daily_counts.values
        p(f"\n  Daily signal distribution:")
        p(f"    Average/day:   {np.mean(counts):.1f}")
        p(f"    Median/day:    {np.median(counts):.1f}")
        p(f"    Peak (max):    {np.max(counts)}")
        p(f"    Min:           {np.min(counts)}")
        p(f"    Std dev:       {np.std(counts):.1f}")
        p(f"    P90:           {np.percentile(counts, 90):.0f}")
        p(f"    P95:           {np.percentile(counts, 95):.0f}")
        p(f"    P99:           {np.percentile(counts, 99):.0f}")

        p(f"\n  Bracket distribution:")
        brackets = [(1, 1), (2, 3), (4, 5), (6, 10), (11, 20), (21, 50), (51, 9999)]
        for lo, hi in brackets:
            n = np.sum((counts >= lo) & (counts <= hi))
            if n > 0:
                label = f"{lo}-{hi}" if hi < 9999 else f"{lo}+"
                p(f"    {label:>8} signals/day: {n:>4} days "
                  f"({n / len(counts) * 100:.1f}%)")

        p(f"\n  Top 20 busiest days:")
        top_dates = daily_counts.sort_values(ascending=False).head(20)
        for i, (date, count) in enumerate(top_dates.items(), 1):
            day_tickers = signals_df[signals_df["date"] == date]["ticker"].tolist()
            ticker_str = ", ".join(sorted(day_tickers)[:8])
            if len(day_tickers) > 8:
                ticker_str += f" +{len(day_tickers) - 8}"
            p(f"    {i:3d}. {date}  {count:>3} signals  [{ticker_str}]")

    p(f"\n  Top 20 most frequent tickers:")
    for i, (ticker, count) in enumerate(ticker_counts.head(20).items(), 1):
        p(f"    {i:3d}. {ticker:>6}: {count:>3} signals")

    p(f"\n  Yearly distribution:")
    signals_df["year"] = signals_df["date"].str[:4]
    yearly = signals_df.groupby("year").size()
    for year, count in yearly.items():
        p(f"    {year}: {count:>4} signals")

    # Conditions used
    p(f"\n  Conditions ({len(conditions)}):")
    for i, c in enumerate(conditions, 1):
        tier = c.get("tier", "?")
        cat = c.get("category", "?")
        p(f"    {i:2d}. [{tier:>4}] [{cat:>18}] {c['name']:35s} "
          f"[{c['low']:.4f} — {c['high']:.4f}]")

    p(f"\n{'=' * 70}")

    # Save summary
    summary_path = os.path.join(CACHE_DIR, f"backtest_summary_{setup_type}.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n  Summary saved: {summary_path}")

    return summary_path


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def load_conditions(setup_type):
    """Load conditions from pyramid results (or historical_results compat format)."""
    # Try pyramid results first
    pyramid_path = os.path.join(CACHE_DIR, f"pyramid_results_{setup_type}.json")
    compat_path = os.path.join(CACHE_DIR, f"historical_results_{setup_type}.json")

    path = None
    if os.path.exists(pyramid_path):
        path = pyramid_path
    elif os.path.exists(compat_path):
        path = compat_path
    else:
        raise FileNotFoundError(
            f"No results found at {pyramid_path} or {compat_path}. "
            f"Run pyramid_grinder.py first."
        )

    with open(path) as f:
        results = json.load(f)

    conditions = results.get("all_conditions", [])
    if not conditions:
        raise ValueError(f"No conditions found in {path}")

    print(f"  Loaded {len(conditions)} conditions from {os.path.basename(path)}")
    return conditions, results


def main():
    parser = argparse.ArgumentParser(description="Backtest Runner — Visual Signal Verification")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    parser.add_argument("--max-charts", type=int, default=500,
                        help="Max charts to generate (default: 500)")
    parser.add_argument("--no-charts", action="store_true",
                        help="Skip chart generation, just scan and summarize")
    parser.add_argument("--charts-only", action="store_true",
                        help="Skip scanning, regenerate charts from existing CSV")
    parser.add_argument("--lookback", type=int, default=120,
                        help="Chart lookback bars before signal (default: 120)")
    parser.add_argument("--forward", type=int, default=15,
                        help="Chart forward bars after signal (default: 15)")
    args = parser.parse_args()

    print(f"\n{'=' * 70}")
    print(f"  BACKTEST RUNNER — Step 6")
    print(f"{'=' * 70}")
    print(f"  Setup: {args.setup.upper()}")

    # Load conditions
    conditions, results = load_conditions(args.setup)

    # Load 5yr cache
    cache_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_path):
        cache_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(cache_path):
        print(f"  ERROR: No OHLCV cache found at {cache_path}")
        print(f"  Run: python local_runner/cache_builder.py --5yr")
        return

    print(f"  Loading OHLCV cache...")
    t0 = time.time()
    with open(cache_path, "rb") as f:
        universe_cache = pickle.load(f)
    print(f"  {len(universe_cache):,} tickers loaded in {time.time() - t0:.1f}s")

    signals_csv = os.path.join(CACHE_DIR, f"backtest_signals_{args.setup}.csv")

    if args.charts_only:
        # Load existing signals CSV
        if not os.path.exists(signals_csv):
            print(f"  ERROR: No signals CSV at {signals_csv}")
            print(f"  Run without --charts-only first to generate signals.")
            return
        signals_df = pd.read_csv(signals_csv)
        print(f"  Loaded {len(signals_df):,} existing signals from CSV")
    else:
        # Scan for signals
        all_signals = scan_all_signals(universe_cache, conditions)

        if not all_signals:
            print("\n  No signals found. Check conditions.")
            return

        # Create DataFrame and save
        signals_df = pd.DataFrame(all_signals)
        signals_df = signals_df.sort_values(["date", "ticker"]).reset_index(drop=True)
        signals_df.to_csv(signals_csv, index=False)
        print(f"\n  Saved {len(signals_df):,} signals → {signals_csv}")

        # Generate summary
        generate_summary(signals_df, args.setup, conditions)

    # Generate charts
    if not args.no_charts:
        generate_all_charts(
            signals_df, universe_cache, args.setup,
            max_charts=args.max_charts,
        )
    else:
        print(f"\n  Skipping chart generation (--no-charts)")

    print(f"\n  ✓ Backtest runner complete.")
    print(f"  Signals CSV: {signals_csv}")
    if not args.no_charts:
        print(f"  Charts dir:  {os.path.join(CACHE_DIR, f'backtest_charts_{args.setup}')}")

    # Auto-upload signals to Railway for frontend Historical tab
    upload_signals_to_railway(signals_df, args.setup)


def upload_signals_to_railway(signals_df, setup_type):
    """Upload backtest signals to Railway so the frontend Historical tab updates."""
    import requests

    RAILWAY_URL = os.environ.get(
        "RAILWAY_API_URL",
        "https://web-production-e3025.up.railway.app"
    )
    endpoint = f"{RAILWAY_URL}/api/backtest/signals/upload"

    signals = [
        {"date": str(row["date"])[:10], "ticker": row["ticker"]}
        for _, row in signals_df.iterrows()
    ]

    payload = {
        "setup_type": setup_type,
        "signals": signals,
    }

    print(f"\n  Uploading {len(signals):,} signals to Railway...")
    try:
        resp = requests.post(endpoint, json=payload, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✓ Uploaded: {data.get('total', '?')} signals, "
                  f"{data.get('unique_tickers', '?')} tickers, "
                  f"max {data.get('max_signals_per_day', '?')}/day")
        else:
            print(f"  ✗ Upload failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        print(f"  ✗ Upload failed: {e}")
        print(f"  (Signals saved locally — upload manually or retry)")


if __name__ == "__main__":
    main()
