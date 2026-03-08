"""
Nightly Update — Single command to refresh all data before grinding.

Usage:
    python local_runner/nightly.py

What it does (in order):
    1. Triggers Railway /api/universe/append-daily (fetches missing trading days)
       - If DB is already up to date → stops here, prints "up to date"
    2. Refreshes local daily OHLCV cache (pulls from Railway)
    3. Refreshes local 5yr OHLCV cache (pulls from Railway)
    4. Appends expression series cache (new bars only)
    5. Rebuilds D1 universe matrix
    6. Refreshes earnings dates
    7. Appends market context cache (266 instruments for regime model)

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


def step_1_railway_append():
    """Trigger Railway to append missing trading days."""
    step_header(1, 7, "Railway — Append Missing Days")

    print("  Calling POST /api/universe/append-daily ...")
    print("  (This fetches new bars from yfinance for all tradable tickers)")
    print()

    try:
        r = requests.post(f"{API_BASE}/api/universe/append-daily", timeout=1800)  # 30 min max
        r.raise_for_status()
        result = r.json()
    except requests.exceptions.Timeout:
        print("  ✗ Request timed out after 30 minutes.")
        return False
    except Exception as e:
        print(f"  ✗ Failed to reach Railway: {e}")
        return False

    status = result.get("status")

    if status == "up_to_date":
        db_date = result.get("db_last_date", "?")
        yf_date = result.get("yf_latest", "?")
        print(f"  ✓ Already up to date (DB: {db_date}, yfinance: {yf_date})")
        print()
        print("  Nothing to do — all data is current.")
        return False  # Signal to stop: no new data

    elif status == "complete":
        print(f"  ✓ Append complete!")
        print(f"    DB was at:       {result.get('db_last_date_was', '?')}")
        print(f"    Now current to:  {result.get('yf_latest', '?')}")
        print(f"    Tickers updated: {result.get('tickers_processed', '?')}")
        print(f"    New rows:        {result.get('new_rows', '?')}")
        if result.get("failed", 0) > 0:
            print(f"    Failed:          {result.get('failed')}")
        if result.get("tradable_rebuilt"):
            print(f"    Tradable count:  {result.get('tradable_count', '?')}")
        return True  # New data — continue with cache refreshes

    else:
        print(f"  ✗ Unexpected response: {result}")
        return False


def step_2_daily_cache():
    """Refresh local daily OHLCV cache (300 bars)."""
    step_header(2, 7, "Local Daily OHLCV Cache")

    from cache_builder import build_cache
    t0 = time.time()
    data = build_cache(force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {len(data)} tickers cached in {elapsed:.1f}s")


def step_3_5yr_cache():
    """Refresh local 5yr OHLCV cache."""
    step_header(3, 7, "Local 5yr OHLCV Cache")

    from cache_builder import build_5yr_cache
    t0 = time.time()
    data = build_5yr_cache(force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {len(data)} tickers cached in {elapsed:.1f}s")


def step_4_expr_cache():
    """Append new bars to expression series cache."""
    step_header(4, 7, "Expression Series Cache — Append")

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
    step_header(5, 7, "Universe Matrix Rebuild")

    from matrix_builder import get_universe_matrix

    def progress(phase, pct, detail):
        print(f"    [{pct:3d}%] {detail}")

    t0 = time.time()
    result = get_universe_matrix(progress_fn=progress, force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {result['n_universe']} tickers × {result['n_exprs']} expressions in {elapsed:.1f}s")


def step_6_earnings():
    """Refresh earnings dates for all tradable tickers."""
    step_header(6, 7, "Earnings Dates Refresh")

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
    step_header(7, 7, "Market Context Cache — Append")

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


def main():
    print(f"\n{'═'*60}")
    print(f"  🌙 Nightly Update")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"{'═'*60}")

    total_start = time.time()

    # Step 1: Railway append (gate — stops if already current)
    has_new_data = step_1_railway_append()

    if not has_new_data:
        print(f"\n{'═'*60}")
        print(f"  🌙 Done — no updates needed")
        print(f"  {ts()}")
        print(f"{'═'*60}\n")
        return

    # Steps 2-5: refresh all local data
    step_2_daily_cache()
    step_3_5yr_cache()
    step_4_expr_cache()
    step_5_matrix()
    step_6_earnings()
    step_7_market_cache()

    total_elapsed = time.time() - total_start
    minutes = total_elapsed / 60

    print(f"\n{'═'*60}")
    print(f"  🌙 Nightly Update Complete")
    print(f"  Total time: {minutes:.1f} min")
    print(f"  {ts()}")
    print(f"  Ready to grind!")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
