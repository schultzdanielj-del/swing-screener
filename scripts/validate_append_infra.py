"""
Validate incremental append infrastructure — tests on ~50 tickers.

Strategy: For each test ticker, pretend the last row of the existing .npz
doesn't exist (set existing_n_bars = actual - 1). Run _append_one_ticker
with the full OHLCV — it computes everything and writes row N as an .append
file. Then verify the appended row matches what's already in the .npz.

No new OHLCV data needed — works with whatever's on disk right now.

Tests:
  1. _append_one_ticker produces .append + .append_dates files with correct size
  2. load_ticker_cache reads .npz + .append correctly (shape, dtype, bar count)
  3. Appended row matches the original last row from the .npz (correctness gate)
  4. signal_filter._load_ticker_npz reads .append files identically
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
        load_ticker_cache, load_manifest,
        EXPR_CACHE_DIR
    )

    print("=" * 70)
    print("  INCREMENTAL APPEND INFRASTRUCTURE — VALIDATION")
    print("=" * 70)

    # ── Load manifest ──
    manifest = load_manifest()
    if manifest is None:
        print("\n  FAIL: No manifest found. Run --build first.")
        return False

    cached_tickers = manifest.get("tickers", {})
    print(f"\n  Cached tickers in manifest: {len(cached_tickers)}")

    # ── Load OHLCV ──
    print("  Loading daily OHLCV cache...")
    universe_cache = _load_daily_cache()
    print(f"  {len(universe_cache)} tickers in OHLCV cache")

    weekly_cache = _load_htf_cache("weekly")
    monthly_cache = _load_htf_cache("monthly")
    print(f"  Weekly HTF: {len(weekly_cache) if weekly_cache else 0} tickers")
    print(f"  Monthly HTF: {len(monthly_cache) if monthly_cache else 0} tickers")

    # ── Find tickers that exist in both OHLCV and expr cache ──
    candidates = []
    for ticker in cached_tickers:
        if ticker not in universe_cache:
            continue
        df = _truncate_to_cache_window(universe_cache[ticker])
        if df is None or len(df) < 50:
            continue
        n_bars = cached_tickers[ticker]["n_bars"]
        if n_bars < 2:
            continue
        candidates.append((ticker, df, n_bars))

    print(f"  Eligible tickers: {len(candidates)}")

    if not candidates:
        print("\n  No eligible tickers found.")
        return False

    # Select test sample
    random.seed(args.seed)
    n_test = min(args.n, len(candidates))
    test_sample = random.sample(candidates, n_test)
    print(f"  Testing {n_test} randomly sampled tickers")

    # ── Init worker ──
    print("\n  Loading expressions and initializing worker...")
    expressions = _load_expressions()
    _init_worker(expressions)
    n_exprs = len(expressions)
    print(f"  {n_exprs} expressions\n")

    # ── Run tests ──
    passed = 0
    failed = 0
    errors = []

    t0 = time.time()

    for i, (ticker, df, actual_n_bars) in enumerate(test_sample):
        ticker_errors = []

        append_path = _ticker_append_path(ticker)
        append_dates_path = _ticker_append_dates_path(ticker)

        # Clean any stale .append files
        for p in [append_path, append_dates_path]:
            if os.path.exists(p):
                os.remove(p)

        # Load base .npz — ground truth
        npz_path = _ticker_cache_path(ticker)
        try:
            loaded = np.load(npz_path, allow_pickle=True)
            original_data = loaded["data"]
            original_dates = loaded["dates"]
            if original_data.dtype != np.float32:
                original_data = original_data.astype(np.float32)
        except Exception as e:
            ticker_errors.append(f"Can't load .npz: {e}")
            errors.append((ticker, ticker_errors))
            failed += 1
            continue

        npz_n_bars = len(original_dates)

        # ── Fake: pretend last row doesn't exist ──
        fake_existing_n = npz_n_bars - 1
        expected_last_row = original_data[-1, :]
        expected_last_date = str(original_dates[-1])

        # Build work item
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

        # ── Test 1: Run _append_one_ticker ──
        append_args = (ticker, df_dict, weekly_df_dict, monthly_df_dict, fake_existing_n)
        result = _append_one_ticker(append_args)
        ticker_out, result_n_bars, result_last_date = result

        if ticker_out is None:
            ticker_errors.append("_append_one_ticker returned None")
            errors.append((ticker, ticker_errors))
            failed += 1
            for p in [append_path, append_dates_path]:
                if os.path.exists(p):
                    os.remove(p)
            continue

        # ── Test 2: .append file size ──
        if not os.path.exists(append_path):
            ticker_errors.append(".append file not created")
        else:
            file_size = os.path.getsize(append_path)
            expected_size = 1 * n_exprs * 2  # 1 row, float16
            if file_size != expected_size:
                ticker_errors.append(
                    f".append size {file_size} != expected {expected_size}")

        if not os.path.exists(append_dates_path):
            ticker_errors.append(".append_dates file not created")
        else:
            with open(append_dates_path) as f:
                date_lines = [line.strip() for line in f if line.strip()]
            if len(date_lines) != 1:
                ticker_errors.append(
                    f".append_dates has {len(date_lines)} lines, expected 1")

        # ── Test 3: load_ticker_cache returns combined data ──
        # .npz has npz_n_bars rows. .append has 1 row. Combined = npz_n_bars + 1.
        combined_dates, combined_data = load_ticker_cache(ticker)
        if combined_dates is None:
            ticker_errors.append("load_ticker_cache returned None after append")
        else:
            expected_combined = npz_n_bars + 1
            if len(combined_dates) != expected_combined:
                ticker_errors.append(
                    f"Combined bar count {len(combined_dates)} != expected {expected_combined}")
            if combined_data is not None and combined_data.shape[1] != n_exprs:
                ticker_errors.append(
                    f"Combined columns {combined_data.shape[1]} != expected {n_exprs}")
            if combined_data is not None and combined_data.dtype != np.float32:
                ticker_errors.append(
                    f"Combined dtype {combined_data.dtype} != expected float32")

        # ── Test 4: Correctness gate ──
        # The appended row was computed fresh by _compute_ticker_full.
        # The expected row is from the .npz (built in a prior session).
        # Both go through float16. Compare via float16 round-trip.
        if combined_data is not None and len(combined_data) >= 1:
            appended_row = combined_data[-1, :]

            appended_f16 = appended_row.astype(np.float16).astype(np.float32)
            expected_f16 = expected_last_row.astype(np.float16).astype(np.float32)

            mismatches = np.where(
                ~(np.isnan(appended_f16) & np.isnan(expected_f16)) &
                (appended_f16 != expected_f16)
            )[0]

            if len(mismatches) > 0:
                first_few = mismatches[:5]
                details = []
                for m in first_few:
                    details.append(
                        f"col {m} ({expressions[m]['name']}): "
                        f"appended={appended_f16[m]:.6f} vs expected={expected_f16[m]:.6f}")
                ticker_errors.append(
                    f"Value mismatches: {len(mismatches)}/{n_exprs} cols. "
                    f"First: {'; '.join(details)}")

        # ── Test 5: signal_filter._load_ticker_npz ──
        try:
            import scripts.signal_filter as sf
            sf._worker_expr_cache = EXPR_CACHE_DIR
            sf_dates, sf_data = sf._load_ticker_npz(ticker)
            if sf_dates is None:
                ticker_errors.append("signal_filter._load_ticker_npz returned None")
            elif combined_dates is not None:
                if len(sf_dates) != len(combined_dates):
                    ticker_errors.append(
                        f"signal_filter bar count {len(sf_dates)} != "
                        f"load_ticker_cache count {len(combined_dates)}")
                elif sf_data is not None and combined_data is not None:
                    nan_mask = np.isnan(sf_data) & np.isnan(combined_data)
                    val_equal = (sf_data == combined_data) | nan_mask
                    if not val_equal.all():
                        n_diff = (~val_equal).sum()
                        ticker_errors.append(
                            f"signal_filter differs: {n_diff} values")
        except Exception as e:
            ticker_errors.append(f"signal_filter test error: {e}")

        # ── Cleanup ──
        for p in [append_path, append_dates_path]:
            if os.path.exists(p):
                os.remove(p)

        # ── Result ──
        if ticker_errors:
            errors.append((ticker, ticker_errors))
            failed += 1
        else:
            passed += 1

        if (i + 1) % 10 == 0 or (i + 1) == n_test:
            elapsed = time.time() - t0
            per = elapsed / (i + 1)
            eta = per * (n_test - i - 1)
            print(f"    {i+1}/{n_test} ({passed} pass, {failed} fail) "
                  f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s left, {per:.1f}s/ticker]")

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
    print(f"  Time: {total_time:.1f}s ({total_time/n_test:.1f}s per ticker)")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
