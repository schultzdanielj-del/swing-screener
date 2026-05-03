"""
Nightly Update -- Single command to refresh all data before grinding.

Usage:
    python local_runner/nightly.py
    python local_runner/nightly.py --force

What it does (in order):
    1. EODHD freshness check (downloads 1 bar SPY, compares to cache)
       - If cache is already current → stops here (runs seed vault only)
    2. Appends local daily OHLCV cache (EODHD)
    3. Appends weekly OHLCV cache (EODHD)
    4. Appends monthly OHLCV cache (EODHD)
    5. Appends intermediate cache (196 indicators per ticker, ~1-2 min)
    6. Rebuilds D1 universe matrix
    7. Refreshes earnings dates
    8. Appends market context cache (256 instruments)
    9. Refreshes fundamentals cache
   10. Pushes seed vault backup to Railway (disaster recovery)

Intermediate cache architecture:
  - Stores 196 indicator intermediates per bar (not 16,000 expressions)
  - All expressions are derived on-the-fly from intermediates for scans
  - Full 16,000-expression matrix is materialized only before consensus runs
  - See intermediate_cache_builder.py, scan_engine.py, materialize_for_consensus.py

Run after market close (~4:30pm ET).
After completion, grind iterations are fast (~2-3 min each).

No Railway dependency for OHLCV. Railway is used for:
  - Earnings dates (step 7)
  - Seed vault backup (step 10)
"""

import os
import sys
import time
from datetime import datetime

# Add local_runner to path
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LOCAL_DIR)

# Add project root to path for scripts/
PROJECT_ROOT = os.path.dirname(LOCAL_DIR)
sys.path.insert(0, PROJECT_ROOT)

API_BASE = "https://web-production-e3025.up.railway.app"

TOTAL_STEPS = 10


def ts():
    """Timestamp for logging."""
    return datetime.now().strftime("%H:%M:%S")


def step_header(num, total, title):
    print(f"\n{'-'*60}")
    print(f"  Step {num}/{total}: {title}")
    print(f"  {ts()}")
    print(f"{'-'*60}")


def step_1_freshness_check():
    """Check if EODHD has new data since last cache update.

    Downloads 1 bar for SPY, compares to last cached date.
    Returns True if new data (continue pipeline), False if current (skip).
    """
    step_header(1, TOTAL_STEPS, "EODHD Freshness Check")

    from cache_builder import check_freshness
    return check_freshness()


def step_2_daily_cache():
    """Append new bars to local daily OHLCV cache via EODHD."""
    step_header(2, TOTAL_STEPS, "Local daily OHLCV Cache -- Append (EODHD)")

    from cache_builder import append_daily_cache
    t0 = time.time()
    data = append_daily_cache()
    elapsed = time.time() - t0
    print(f"  OK {len(data)} tickers in cache ({elapsed:.1f}s)")


def step_3_weekly_cache():
    """Append new bars to weekly OHLCV cache via EODHD."""
    step_header(3, TOTAL_STEPS, "Weekly OHLCV Cache -- Append (EODHD)")

    try:
        from cache_builder import append_weekly
        t0 = time.time()
        data = append_weekly()
        elapsed = time.time() - t0
        print(f"  OK Weekly cache updated ({elapsed:.1f}s)")
    except FileNotFoundError as e:
        print(f"  WARN {e}")
        print("  (Run 'python local_runner/cache_builder.py --htf' first)")
    except Exception as e:
        print(f"  FAIL Weekly cache append failed: {e}")
        print("  (Non-fatal -- expr cache will resample from daily as fallback)")


def step_4_monthly_cache():
    """Append new bars to monthly OHLCV cache via EODHD."""
    step_header(4, TOTAL_STEPS, "Monthly OHLCV Cache -- Append (EODHD)")

    try:
        from cache_builder import append_monthly
        t0 = time.time()
        data = append_monthly()
        elapsed = time.time() - t0
        print(f"  OK Monthly cache updated ({elapsed:.1f}s)")
    except FileNotFoundError as e:
        print(f"  WARN {e}")
        print("  (Run 'python local_runner/cache_builder.py --htf' first)")
    except Exception as e:
        print(f"  FAIL Monthly cache append failed: {e}")
        print("  (Non-fatal -- expr cache will resample from daily as fallback)")


def step_5_intermediate_cache():
    """Append new bars to intermediate cache.

    Computes 196 intermediates for today's bar for all tickers (~1-2 min).
    """
    step_header(5, TOTAL_STEPS, "Intermediate Cache - Append")

    im_cache_dir = os.path.join(LOCAL_DIR, "cache", "intermediate_series")

    if not os.path.exists(im_cache_dir) or len(os.listdir(im_cache_dir)) < 100:
        print("  No intermediate cache found. Running full rebuild...")
        from intermediate_cache_builder import load_daily_cache, build_full
        daily_cache = load_daily_cache()
        t0 = time.time()
        build_full(daily_cache, workers=14)
        elapsed = time.time() - t0
        print(f"  OK Full intermediate rebuild done in {elapsed:.1f}s")
    else:
        from intermediate_cache_builder import load_daily_cache, build_full
        daily_cache = load_daily_cache()
        t0 = time.time()
        build_full(daily_cache, workers=14)
        elapsed = time.time() - t0
        print(f"  OK Intermediate cache rebuild done in {elapsed:.1f}s")


def step_6_matrix():
    """Rebuild D1 universe matrix.

    With intermediate cache architecture, the expression cache (.npz files)
    may be stale. The matrix builder handles this gracefully:
    - If expr cache exists (even stale): reads from it (~30s)
    - If expr cache missing: falls back to live compute (~30 min, NaN for LSP/HTF)

    The grinder reads materialized .npz files during consensus, NOT this matrix.
    The matrix is primarily for the dashboard and analysis scripts.
    """
    step_header(6, TOTAL_STEPS, "Universe Matrix Rebuild")

    from matrix_builder import get_universe_matrix

    # Check if expression cache exists
    expr_cache_dir = os.path.join(LOCAL_DIR, "cache", "expr_series")
    has_expr_cache = os.path.exists(expr_cache_dir) and any(
        f.endswith(".npz") for f in os.listdir(expr_cache_dir)
    ) if os.path.exists(expr_cache_dir) else False

    if not has_expr_cache:
        print("  Expression cache not found. Skipping matrix rebuild.")
        print("  (Matrix will be built during consensus materialization)")
        print("  (Daily scans run from intermediate cache -- not affected)")
        return

    def progress(phase, pct, detail):
        print(f"    [{pct:3d}%] {detail}")

    t0 = time.time()
    result = get_universe_matrix(progress_fn=progress, force=True)
    elapsed = time.time() - t0
    print(f"  OK {result['n_universe']} tickers x {result['n_exprs']} expressions in {elapsed:.1f}s")


def step_7_earnings():
    """Report earnings date status. Earnings are refreshed quarterly, not nightly.

    Run manually: python scripts/fetch_earnings.py
    """
    step_header(7, TOTAL_STEPS, "Earnings Dates -- Status")

    try:
        import sqlite3
        db_path = os.path.join(PROJECT_ROOT, "data", "scanperfect.db")
        if not os.path.exists(db_path):
            print("  No local DB -- earnings dates not available")
            return
        db = sqlite3.connect(db_path)
        n_tickers = db.execute("SELECT COUNT(DISTINCT ticker) FROM earnings_dates").fetchone()[0]
        n_rows = db.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0]
        latest = db.execute("SELECT MAX(updated_at) FROM earnings_dates").fetchone()[0]
        db.close()
        print(f"  {n_tickers} tickers, {n_rows} dates (last updated: {latest})")
        print("  (Quarterly refresh -- run 'python scripts/fetch_earnings.py' manually)")
    except Exception as e:
        print(f"  FAIL {e}")


def step_8_market_cache():
    """Append new bars to market context cache (266 instruments) and recompute."""
    step_header(8, TOTAL_STEPS, "Market Context Cache -- Append")

    try:
        from market_cache_builder import append_new_bars
        append_new_bars(n_threads=16)
        print("  OK Market cache updated")
    except ImportError:
        print("  FAIL market_cache_builder.py not found -- skipping")
        print("  (Market regime data will be stale until cache is built)")
    except Exception as e:
        print(f"  FAIL Market cache append failed: {e}")
        print("  (Non-fatal -- regime model will use stale data)")


def step_9_fundamentals():
    """Refresh fundamentals cache -- fetch new tickers, periodic full re-fetch."""
    step_header(9, TOTAL_STEPS, "Fundamentals Cache -- Incremental")

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
            print("  FAIL fetch_fundamentals.py not found -- skipping")
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
            # Re-fetch everything -- shares outstanding and float drift over time
            to_fetch = all_tickers
            print(f"  Monday -- full re-fetch of {len(to_fetch)} tickers")
        elif new_tickers:
            to_fetch = new_tickers
            print(f"  {len(new_tickers)} new tickers to fetch")
        else:
            print(f"  OK Fundamentals cache current ({len(existing)} tickers, no new)")
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
                print(f"  WARN Rate limited at {ticker}. Sleeping 30s...")
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
        print(f"  OK Fundamentals: {n_ok} fetched, {n_err} errors, {len(results)} total")

        try:
            from file_mirror import mirror_file
            from scripts.fetch_fundamentals import OUTPUT_FILE
            mirror_file(OUTPUT_FILE)
        except Exception:
            pass

    except Exception as e:
        print(f"  FAIL Fundamentals refresh failed: {e}")
        print("  (Non-fatal -- EV grinder will use existing cache)")


def step_10_seed_vault():
    """Push seed vault backup to Railway."""
    step_header(10, TOTAL_STEPS, "Seed Vault -- Backup to Railway")

    try:
        from scripts.seed_vault import backup
        backup()
    except ImportError:
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
            from seed_vault import backup
            backup()
        except ImportError:
            print("  FAIL seed_vault.py not found -- skipping")
            return
    except Exception as e:
        print(f"  FAIL Seed vault backup failed: {e}")
        print("  (Non-fatal -- data is safe locally)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nightly data refresh")
    parser.add_argument("--force", action="store_true",
                        help="Skip freshness check, run all steps regardless")
    parser.add_argument("--skip", type=int, nargs="+", default=[],
                        help="Step numbers to skip (e.g. --skip 5 to skip expr cache)")
    args = parser.parse_args()
    skip_steps = set(args.skip)

    print(f"\n{'='*60}")
    print(f"  Nightly Update")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"{'='*60}")

    total_start = time.time()

    if args.force:
        print("\n  --force: skipping freshness check, running all steps")
    if skip_steps:
        print(f"\n  --skip: will skip step(s) {', '.join(str(s) for s in sorted(skip_steps))}")

    if not args.force:
        # Step 1: EODHD freshness check (gate)
        has_new_data = step_1_freshness_check()

        if not has_new_data:
            # No new market data, but still run seed vault backup
            step_10_seed_vault()
            print(f"\n{'='*60}")
            print(f"  Done -- no data updates needed, seed vault backed up")
            print(f"  {ts()}")
            print(f"{'='*60}\n")
            return

    # Steps 2-9: refresh all local data
    steps = [
        (2, step_2_daily_cache),
        (3, step_3_weekly_cache),
        (4, step_4_monthly_cache),
        (5, step_5_intermediate_cache),
        (6, step_6_matrix),
        (7, step_7_earnings),
        (8, step_8_market_cache),
        (9, step_9_fundamentals),
    ]
    for num, fn in steps:
        if num in skip_steps:
            print(f"\n  SKIP Skipping step {num} (--skip)")
            continue
        fn()

    # Step 10: seed vault always runs
    step_10_seed_vault()

    total_elapsed = time.time() - total_start
    minutes = total_elapsed / 60

    print(f"\n{'='*60}")
    print(f"  Nightly Update Complete")
    print(f"  Total time: {minutes:.1f} min")
    print(f"  {ts()}")
    print(f"  Ready to grind!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
