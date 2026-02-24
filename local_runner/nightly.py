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
    step_header(1, 5, "Railway — Append Missing Days")

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
    step_header(2, 5, "Local Daily OHLCV Cache")

    from cache_builder import build_cache
    t0 = time.time()
    data = build_cache(force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {len(data)} tickers cached in {elapsed:.1f}s")


def step_3_5yr_cache():
    """Refresh local 5yr OHLCV cache."""
    step_header(3, 5, "Local 5yr OHLCV Cache")

    from cache_builder import build_5yr_cache
    t0 = time.time()
    data = build_5yr_cache(force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {len(data)} tickers cached in {elapsed:.1f}s")


def step_4_expr_cache():
    """Append new bars to expression series cache."""
    step_header(4, 5, "Expression Series Cache — Append")

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
    step_header(5, 5, "Universe Matrix Rebuild")

    from matrix_builder import get_universe_matrix

    def progress(phase, pct, detail):
        print(f"    [{pct:3d}%] {detail}")

    t0 = time.time()
    result = get_universe_matrix(progress_fn=progress, force=True)
    elapsed = time.time() - t0
    print(f"  ✓ {result['n_universe']} tickers × {result['n_exprs']} expressions in {elapsed:.1f}s")


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
