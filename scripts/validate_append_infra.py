"""
Validate incremental append infrastructure — tests on ~50 tickers.

Tests:
  1. _append_one_ticker produces .append + .append_dates files
  2. load_ticker_cache reads .npz + .append correctly (shape, dtype, bar count)
  3. Appended rows match _compute_ticker_full output exactly (correctness gate)
  4. signal_filter._load_ticker_npz also reads .append files correctly
  5. Cleanup: removes test .append files after validation

Run from repo root:
    python scripts/validate_append_infra.py [--n 50]
"""

import os
import sys
import time
import random
import argparse
import warnings
import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


def main():
    parser = argparse.ArgumentParser(description="Validate incremental append infrastructure")
    parser.add_argument("--n", type=int, default=50, help="Number of tickers to test")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for ticker selection")
    args = parser.parse_args()

    from expr_cache_builder import (
        _load_expressions, _load_daily_cache, _load_htf_cache,
        _df_to_dict, _truncate_to_cache_window, _init_worker,
        _compute_ticker_full, _append_one_ticker, _ticker_append_path,
        _ticker_append_dates_path, _ticker_cache_path,
        load_ticker_cache, load_manifest, save_ticker_cache,
        EXPR_CACHE_DIR
    )

    print("=" * 70)
    print("  INCREMENTAL APPEND INFRASTRUCTURE — VALIDATION")
    print("=" * 70)

    # ── Load manifest and find tickers with new bars ──
    manifest = load_manifest()
    if manifest is None:
        print("\n  FAIL: No manifest found. Run --build first.")
        return False

    cached_tickers = manifest.get("tickers", {})
    print(f"\n  Cached tickers in manifest: {len(cached_tickers)}")

    # Load OHLCV
    print("  Loading daily OHLCV cache...")
    universe_cache = _load_daily_cache()
    print(f"  {len(universe_cache)} tickers in OHLCV cache")

    # Load HTF
    weekly_cache = _load_htf_cache("weekly")
    monthly_cache = _load_htf_cache("monthly")
    print(f"  Weekly HTF: {len(weekly_cache) if weekly_cache else 0} tickers")
    print(f"  Monthly HTF: {len(monthly_cache) if monthly_cache else 0} tickers")

    # Find tickers that have new bars (OHLCV has more bars than manifest says)
    candidates = []
    for ticker, df in universe_cache.items():
        if ticker not in cached_tickers:
            continue
        df = _truncate_to_cache_window(df)
        if df is None or len(df) < 50:
            continue
        existing_n = cached_tickers[ticker]["n_bars"]
        if len(df) > existing_n:
            candidates.append((ticker, df, existing_n))

    print(f"  Tickers with new bars: {len(candidates)}")

    if not candidates:
        print("\n  No tickers have new bars. Nothing to test.")
        print("  (Run OHLCV append first to add new bars to the daily cache.)")
        return True

    # Select test sample
    random.seed(args.seed)
    n_test = min(args.n, len(candidates))
    test_tickers = random.sample(candidates, n_test)
    print(f"  Testing on {n_test} randomly sampled tickers")

    # ── Init worker (same as ProcessPoolExecutor initializer) ──
    print("\n  Loading expressions and initializing worker...")
    expressions = _load_expressions()
    _init_worker(expressions)
    n_exprs = len(expressions)
    print(f"  {n_exprs} expressions")

    # ── Run tests ──
    passed = 0
    failed = 0
    errors = []
    created_files = []  # Track for cleanup

    t0 = time.time()

    for i, (ticker, df, existing_n) in enumerate(test_tickers):
        df_dict = {
            "date": df["date"].values,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values,
        }
        weekly_df_dict = _df_to_dict(weekly_cache.get(ticker)) if weekly_cache else None
        monthly_df_dict = _df_to_dict(monthly_cache.get(ticker)) if monthly_cache else None

        total_bars = len(df)
        n_new = total_bars - existing_n
        ticker_errors = []

        # ── Test 1: Verify .npz exists and has expected shape before append ──
        base_dates, base_data = load_ticker_cache(ticker)
        if base_dates is None:
            ticker_errors.append("No existing .npz file")
            errors.append((ticker, ticker_errors))
            failed += 1
            continue

        # Clean any stale .append files from prior aborted runs
        append_path = _ticker_append_path(ticker)
        append_dates_path = _ticker_append_dates_path(ticker)
        for p in [append_path, append_dates_path]:
            if os.path.exists(p):
                os.remove(p)

        # Verify base shape matches manifest
        if len(base_dates) != existing_n:
            # Could have stale .append from the aborted run — check raw .npz
            npz_path = _ticker_cache_path(ticker)
            loaded = np.load(npz_path, allow_pickle=True)
            raw_n = len(loaded["dates"])
            if raw_n != existing_n:
                ticker_errors.append(
                    f"Base .npz bar count {raw_n} != manifest {existing_n}")
                errors.append((ticker, ticker_errors))
                failed += 1
                continue
            # The base was correct, we cleaned .append files, reload
            base_dates, base_data = load_ticker_cache(ticker)

        # ── Test 2: Run _append_one_ticker ──
        append_args = (ticker, df_dict, weekly_df_dict, monthly_df_dict, existing_n)
        result = _append_one_ticker(append_args)
        ticker_out, result_n_bars, result_last_date = result

        if ticker_out is None:
            ticker_errors.append("_append_one_ticker returned None")
            errors.append((ticker, ticker_errors))
            failed += 1
            continue

        # Track created files for cleanup
        if os.path.exists(append_path):
            created_files.append(append_path)
        if os.path.exists(append_dates_path):
            created_files.append(append_dates_path)

        # Verify return values
        if result_n_bars != total_bars:
            ticker_errors.append(
                f"Return n_bars {result_n_bars} != expected {total_bars}")

        # ── Test 3: Verify .append file exists and has correct size ──
        if not os.path.exists(append_path):
            ticker_errors.append(".append file not created")
        else:
            file_size = os.path.getsize(append_path)
            expected_size = n_new * n_exprs * 2  # float16 = 2 bytes
            if file_size != expected_size:
                ticker_errors.append(
                    f".append size {file_size} != expected {expected_size} "
                    f"({n_new} rows x {n_exprs} exprs x 2 bytes)")

        if not os.path.exists(append_dates_path):
            ticker_errors.append(".append_dates file not created")
        else:
            with open(append_dates_path) as f:
                date_lines = [line.strip() for line in f if line.strip()]
            if len(date_lines) != n_new:
                ticker_errors.append(
                    f".append_dates has {len(date_lines)} lines, expected {n_new}")

        # ── Test 4: Verify load_ticker_cache reads combined data ──
        combined_dates, combined_data = load_ticker_cache(ticker)
        if combined_dates is None:
            ticker_errors.append("load_ticker_cache returned None after append")
        else:
            if len(combined_dates) != total_bars:
                ticker_errors.append(
                    f"Combined bar count {len(combined_dates)} != expected {total_bars}")
            if combined_data.shape != (total_bars, n_exprs):
                ticker_errors.append(
                    f"Combined shape {combined_data.shape} != expected ({total_bars}, {n_exprs})")
            if combined_data.dtype != np.float32:
                ticker_errors.append(
                    f"Combined dtype {combined_data.dtype} != expected float32")

        # ── Test 5: Correctness gate — compare appended rows vs full compute ──
        compute_args = (ticker, df_dict, weekly_df_dict, monthly_df_dict)
        _, full_dates, full_data = _compute_ticker_full(compute_args)

        if full_dates is None:
            ticker_errors.append("_compute_ticker_full returned None")
        elif combined_dates is not None and combined_data is not None:
            # Compare the NEW rows only (the appended ones)
            for row_offset in range(n_new):
                row_idx = existing_n + row_offset
                if row_idx >= len(full_dates) or row_idx >= len(combined_dates):
                    ticker_errors.append(
                        f"Row index {row_idx} out of bounds")
                    break

                # Date must match
                if str(combined_dates[row_idx]) != str(full_dates[row_idx]):
                    ticker_errors.append(
                        f"Date mismatch at row {row_idx}: "
                        f"append={combined_dates[row_idx]} vs full={full_dates[row_idx]}")

                # Values must match after float16 round-trip.
                # _append_one_ticker writes float16, load reads float16→float32.
                # _compute_ticker_full returns float32 directly.
                # So we compare: float32→float16→float32 vs float32
                appended_row = combined_data[row_idx]
                full_row = full_data[row_idx]
                full_row_f16 = full_row.astype(np.float16).astype(np.float32)

                # Both should be identical after float16 round-trip
                mismatches = np.where(
                    ~(np.isnan(appended_row) & np.isnan(full_row_f16)) &
                    (appended_row != full_row_f16)
                )[0]

                if len(mismatches) > 0:
                    # Show first few mismatches
                    first_few = mismatches[:5]
                    details = []
                    for m in first_few:
                        details.append(
                            f"col {m} ({expressions[m]['name']}): "
                            f"append={appended_row[m]:.6f} vs full_f16={full_row_f16[m]:.6f}")
                    ticker_errors.append(
                        f"Value mismatches at row {row_idx}: {len(mismatches)} cols. "
                        f"First: {'; '.join(details)}")

        # ── Test 6: Verify signal_filter._load_ticker_npz reads same data ──
        try:
            import scripts.signal_filter as sf
            # Set the worker global directly (normally set by _init_scan_worker)
            sf._worker_expr_cache = EXPR_CACHE_DIR
            sf_dates, sf_data = sf._load_ticker_npz(ticker)
            if sf_dates is None:
                ticker_errors.append("signal_filter._load_ticker_npz returned None")
            elif combined_dates is not None:
                if len(sf_dates) != len(combined_dates):
                    ticker_errors.append(
                        f"signal_filter bar count {len(sf_dates)} != "
                        f"load_ticker_cache bar count {len(combined_dates)}")
                elif not np.array_equal(sf_data, combined_data):
                    # Check allowing NaN equality
                    nan_mask = np.isnan(sf_data) & np.isnan(combined_data)
                    val_equal = (sf_data == combined_data) | nan_mask
                    if not val_equal.all():
                        n_diff = (~val_equal).sum()
                        ticker_errors.append(
                            f"signal_filter data differs from load_ticker_cache: "
                            f"{n_diff} values differ")
        except Exception as e:
            ticker_errors.append(f"signal_filter test error: {e}")

        # ── Result ──
        if ticker_errors:
            errors.append((ticker, ticker_errors))
            failed += 1
        else:
            passed += 1

        if (i + 1) % 10 == 0 or (i + 1) == n_test:
            elapsed = time.time() - t0
            print(f"    {i+1}/{n_test} tested ({passed} passed, {failed} failed) "
                  f"[{elapsed:.1f}s]")

    # ── Cleanup .append files created during test ──
    print(f"\n  Cleaning up {len(created_files)} test files...")
    for f in created_files:
        if os.path.exists(f):
            os.remove(f)

    # Also clean any .append_dates that might have been missed
    for ticker, df, existing_n in test_tickers:
        for path_fn in [_ticker_append_path, _ticker_append_dates_path]:
            p = path_fn(ticker)
            if os.path.exists(p):
                os.remove(p)

    total_time = time.time() - t0

    # ── Summary ──
    print(f"\n  {'=' * 50}")
    if failed == 0:
        print(f"  ALL {passed} TICKERS PASSED")
    else:
        print(f"  {passed} PASSED, {failed} FAILED")
        print(f"\n  Failures:")
        for ticker, errs in errors:
            print(f"    {ticker}:")
            for e in errs:
                print(f"      - {e}")
    print(f"  {'=' * 50}")
    print(f"  Time: {total_time:.1f}s ({total_time/n_test:.2f}s per ticker)")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
