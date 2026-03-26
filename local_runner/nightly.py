"""
Nightly Update — Single command to refresh all data before grinding.

Usage:
    python local_runner/nightly.py

What it does (in order):
    1. Checks yfinance (SPY) for new trading data since last cache update
       - If cache is already current → stops here, prints "up to date"
    2. Appends new bars to local 5yr OHLCV cache (fetches from yfinance)
    3. Rebuilds tradable_universe table from fresh 5yr cache data
    4. Appends expression series cache (new bars + new tickers)
    5. Rebuilds D1 universe matrix
    6. Refreshes earnings dates
    7. Appends market context cache (256 instruments OHLCV + recomputes expressions)
    8. Refreshes fundamentals cache (new tickers daily, full re-fetch Mondays)
    9. Pushes seed vault backup to Railway (disaster recovery)

Run after market close (~4:30pm ET). Total time: ~15-20 min.
After completion, grind iterations are fast (~2-3 min each).
"""

import os
import sys
import time
import requests
from datetime import datetime

# Add local_runner to path
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LOCAL_DIR)

# Add project root to path for scripts/
PROJECT_ROOT = os.path.dirname(LOCAL_DIR)
sys.path.insert(0, PROJECT_ROOT)

API_BASE = "https://web-production-e3025.up.railway.app"


def ts():
    """Timestamp for logging."""
    return datetime.now().strftime("%H:%M:%S")


def step_header(num, total, title):
    print(f"\n{'─'*60}")
    print(f"  Step {num}/{total}: {title}")
    print(f"  {ts()}")
    print(f"{'─'*60}")


TOTAL_STEPS = 9


def step_1_freshness_check():
    """Check if yfinance has new trading data since last 5yr cache update."""
    step_header(1, TOTAL_STEPS, "Freshness Check — yfinance vs local cache")

    import pickle
    import pandas as pd

    cache_file = os.path.join(LOCAL_DIR, "cache", "universe_ohlcv_5yr.pkl")
    cache_meta = os.path.join(LOCAL_DIR, "cache", "cache_5yr_meta.txt")

    # Find the last date in the 5yr cache by checking a few tickers
    cache_last_date = None
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                universe = pickle.load(f)
            # Sample a few liquid tickers to find latest cached date
            for probe in ["SPY", "AAPL", "MSFT", "QQQ"]:
                if probe in universe and len(universe[probe]) > 0:
                    d = str(universe[probe]["date"].iloc[-1])[:10]
                    if cache_last_date is None or d > cache_last_date:
                        cache_last_date = d
            del universe  # free memory
            print(f"  Cache last date: {cache_last_date}")
        except Exception as e:
            print(f"  ⚠ Could not read cache: {e}")
    else:
        print("  No 5yr cache found — will do full build")
        return True

    if cache_last_date is None:
        print("  ⚠ Could not determine cache last date — continuing")
        return True

    # Check what yfinance has available (use SPY as reference)
    try:
        import yfinance as yf
        spy = yf.download("SPY", period="5d", progress=False, timeout=15)
        if spy is None or spy.empty:
            print("  ⚠ Could not fetch SPY from yfinance — continuing anyway")
            return True
        if isinstance(spy.columns, pd.MultiIndex):
            spy.columns = spy.columns.droplevel(1)
        yf_last_date = str(spy.index[-1])[:10]
        print(f"  yfinance last date: {yf_last_date}")
    except Exception as e:
        print(f"  ⚠ yfinance check failed: {e}")
        print("  Continuing with cache refresh anyway...")
        return True

    if cache_last_date >= yf_last_date:
        print(f"\n  ✓ Cache is current (cache: {cache_last_date}, yfinance: {yf_last_date})")
        print("  Nothing to do — all data is current.")
        return False

    diff_days = (pd.Timestamp(yf_last_date) - pd.Timestamp(cache_last_date)).days
    print(f"\n  New data available: {diff_days} calendar day(s) behind")
    print(f"  Cache: {cache_last_date} → yfinance: {yf_last_date}")
    return True


def step_2_5yr_cache():
    """Append new bars to local 5yr OHLCV cache. Never rebuilds, never touches old bars."""
    step_header(2, TOTAL_STEPS, "5yr OHLCV Cache — Append from yfinance")

    from cache_builder import append_5yr_cache
    t0 = time.time()
    data = append_5yr_cache()
    elapsed = time.time() - t0
    print(f"  ✓ {len(data)} tickers in cache ({elapsed:.1f}s)")


def step_3_tradable_universe():
    """Rebuild tradable_universe table from fresh 5yr cache data."""
    step_header(3, TOTAL_STEPS, "Tradable Universe Rebuild")

    try:
        from scripts.build_tradable import build_tradable_universe
    except ImportError:
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
            from build_tradable import build_tradable_universe
        except ImportError:
            print("  ✗ build_tradable.py not found — skipping")
            return

    t0 = time.time()
    count = build_tradable_universe()
    elapsed = time.time() - t0
    print(f"  ✓ {count} tradable tickers ({elapsed:.1f}s)")


def step_4_expr_cache():
    """Append new bars to expression series cache."""
    step_header(4, TOTAL_STEPS, "Expression Series Cache — Append")

    cache_dir = os.path.join(LOCAL_DIR, "cache", "expr_series")
    if not os.path.exists(cache_dir):
        print("  ⚠ No expression series cache found. Skipping.")
        print("  (Run 'python local_runner/expr_cache_builder.py --build' first)")
        return

    from expr_cache_builder import append_new_bars
    t0 = time.time()
    append_new_bars()
    elapsed = time.time() - t0
    print(f"  ✓ Expression cache append done in {elapsed:.1f}s")


def step_5_matrix():
    """Rebuild D1 universe matrix."""
    step_header(5, TOTAL_STEPS, "Universe Matrix Rebuild")

    from matrix_builder import get_universe_matrix

    def progress(phase, pct, detail):
        print(f"    [{pct:3d}%] {detail}")

    t0 = time.time()
    result = get_universe_matrix(progress_fn=progress, force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {result['n_universe']} tickers × {result['n_exprs']} expressions in {elapsed:.1f}s")


def step_6_earnings():
    """Refresh earnings dates for all tradable tickers."""
    step_header(6, TOTAL_STEPS, "Earnings Dates Refresh")

    print("  Calling POST /api/universe/refresh-earnings ...")
    print("  (Scrapes Yahoo Finance for all tradable tickers)")
    print()

    try:
        # Trigger the background task
        r = requests.post(f"{API_BASE}/api/universe/refresh-earnings", timeout=30)
        r.raise_for_status()
        print(f"  Started: {r.json().get('message', 'ok')}")

        # Poll until complete (check every 30s, max 60 min)
        import time as _time
        last_count = 0
        for _ in range(120):
            _time.sleep(30)
            try:
                sr = requests.get(f"{API_BASE}/api/universe/earnings-status", timeout=10)
                data = sr.json()
                count = data.get("tickers_with_earnings", 0)
                total = data.get("total_dates", 0)
                if count > last_count:
                    print(f"    {count} tickers, {total} dates...")
                    last_count = count
                elif count == last_count and count > 0:
                    # No change for 30s — probably done
                    print(f"  \u2713 Earnings refresh complete: {count} tickers, {total} dates")
                    return
            except:
                pass

        print("  \u2713 Earnings refresh sent (may still be running in background)")

    except Exception as e:
        print(f"  \u2717 Failed: {e}")
        print("  (Non-fatal — vetting will work without earnings dates)")


def step_7_market_cache():
    """Append new bars to market context cache (266 instruments) and recompute."""
    step_header(7, TOTAL_STEPS, "Market Context Cache — Append")

    try:
        from market_cache_builder import append_new_bars
        append_new_bars(n_threads=16)
        print("  ✓ Market cache updated")
    except ImportError:
        print("  ✗ market_cache_builder.py not found — skipping")
        print("  (Market regime data will be stale until cache is built)")
    except Exception as e:
        print(f"  ✗ Market cache append failed: {e}")
        print("  (Non-fatal — regime model will use stale data)")


def step_8_fundamentals():
    """Refresh fundamentals cache — fetch new tickers, periodic full re-fetch."""
    step_header(8, TOTAL_STEPS, "Fundamentals Cache — Incremental")

    try:
        from scripts.fetch_fundamentals import (
            load_universe_tickers, load_existing_cache,
            create_yahoo_session, fetch_ticker_data, save_cache,
            DEFAULT_DELAY
        )
    except ImportError:
        # Try alternative import path
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
            from fetch_fundamentals import (
                load_universe_tickers, load_existing_cache,
                create_yahoo_session, fetch_ticker_data, save_cache,
                DEFAULT_DELAY
            )
        except ImportError:
            print("  ✗ fetch_fundamentals.py not found — skipping")
            return

    try:
        all_tickers = load_universe_tickers()
        existing = load_existing_cache()

        # Find new tickers not in cache
        new_tickers = [t for t in all_tickers if t not in existing]

        # Weekly full re-fetch on Mondays (shares outstanding / float change)
        from datetime import datetime as _dt
        is_monday = _dt.now().weekday() == 0
        if is_monday:
            # Re-fetch everything — shares outstanding and float drift over time
            to_fetch = all_tickers
            print(f"  Monday — full re-fetch of {len(to_fetch)} tickers")
        elif new_tickers:
            to_fetch = new_tickers
            print(f"  {len(new_tickers)} new tickers to fetch")
        else:
            print(f"  ✓ Fundamentals cache current ({len(existing)} tickers, no new)")
            return

        opener, crumb = create_yahoo_session()
        results = dict(existing)
        n_ok = 0
        n_err = 0

        for i, ticker in enumerate(to_fetch):
            data = fetch_ticker_data(opener, crumb, ticker)
            if data and "error" not in data:
                results[ticker] = data
                n_ok += 1
            elif data and data.get("error") == "rate_limited":
                print(f"  ⚠ Rate limited at {ticker}. Sleeping 30s...")
                time.sleep(30)
                try:
                    opener, crumb = create_yahoo_session()
                except Exception:
                    pass
                data = fetch_ticker_data(opener, crumb, ticker)
                if data and "error" not in data:
                    results[ticker] = data
                    n_ok += 1
                else:
                    results[ticker] = data or {"error": "rate_limit_retry_failed"}
                    n_err += 1
            else:
                results[ticker] = data or {"error": "unknown"}
                n_err += 1

            if (i + 1) % 100 == 0:
                print(f"    {i + 1}/{len(to_fetch)} ok={n_ok} err={n_err}")

            time.sleep(DEFAULT_DELAY)

        save_cache(results)
        print(f"  ✓ Fundamentals: {n_ok} fetched, {n_err} errors, {len(results)} total")

        try:
            from file_mirror import mirror_file
            from scripts.fetch_fundamentals import OUTPUT_FILE
            mirror_file(OUTPUT_FILE)
        except Exception:
            pass

    except Exception as e:
        print(f"  ✗ Fundamentals refresh failed: {e}")
        print("  (Non-fatal — EV grinder will use existing cache)")


def step_9_seed_vault():
    """Push seed vault backup to Railway."""
    step_header(9, TOTAL_STEPS, "Seed Vault — Backup to Railway")

    try:
        from scripts.seed_vault import backup
        backup()
    except ImportError:
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
            from seed_vault import backup
            backup()
        except ImportError:
            print("  ✗ seed_vault.py not found — skipping")
            return
    except Exception as e:
        print(f"  ✗ Seed vault backup failed: {e}")
        print("  (Non-fatal — data is safe locally)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nightly data refresh")
    parser.add_argument("--force", action="store_true",
                        help="Skip freshness check, run all steps regardless")
    args = parser.parse_args()

    print(f"\n{'═'*60}")
    print(f"  Nightly Update")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"{'═'*60}")

    total_start = time.time()

    if args.force:
        print("\n  --force: skipping freshness check, running all steps")
    else:
        # Step 1: freshness check (gate — stops if already current)
        has_new_data = step_1_freshness_check()

        if not has_new_data:
            print(f"\n{'═'*60}")
            print(f"  Done — no updates needed")
            print(f"  {ts()}")
            print(f"{'═'*60}\n")
            return

    # Steps 2-9: refresh all local data + backup
    step_2_5yr_cache()
    step_3_tradable_universe()
    step_4_expr_cache()
    step_5_matrix()
    step_6_earnings()
    step_7_market_cache()
    step_8_fundamentals()
    step_9_seed_vault()

    total_elapsed = time.time() - total_start
    minutes = total_elapsed / 60

    print(f"\n{'═'*60}")
    print(f"  Nightly Update Complete")
    print(f"  Total time: {minutes:.1f} min")
    print(f"  {ts()}")
    print(f"  Ready to grind!")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
