"""
Forward-prop correctness tests — zero-tolerance, all tickers.

The existing .npz files ARE the truth (produced by _compute_ticker_full).
We don't recompute them. Instead:
  1. Load .npz → last row = truth, first N-1 rows = base
  2. Setup forward-prop on the N-1 row base
  3. Forward-prop bar N
  4. Compare against .npz row N — exact float16 match required

Cost: ~1.8s/ticker (setup 0.9s + forward-prop 0.87s)
All tickers with 14 workers: ~24 minutes.

Usage:
    python scripts/test_forward_prop.py                     # Gate 1: AAPL
    python scripts/test_forward_prop.py --ticker MSFT       # Gate 1: specific
    python scripts/test_forward_prop.py --gate2             # Gate 2: 100 random
    python scripts/test_forward_prop.py --all               # ALL tickers
    python scripts/test_forward_prop.py --all --workers 14  # ALL, parallel
"""

import os
import sys
import time
import json
import random
import shutil
import tempfile
import argparse
import zipfile
import io
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from local_runner.expr_cache_builder import (
    EXPR_CACHE_DIR, _load_expressions, _load_daily_cache, _load_htf_cache,
    _truncate_to_cache_window, _df_to_dict,
)

# ══════════════════════════════════════════════════════════════
# WORKER
# ══════════════════════════════════════════════════════════════

_w_expressions = None
_w_n_exprs = 0
_w_ext_name_to_idx = None


def _init_test_worker(expressions):
    """Init worker globals."""
    global _w_expressions, _w_n_exprs, _w_ext_name_to_idx
    _w_expressions = expressions
    _w_n_exprs = len(expressions)
    _w_ext_name_to_idx = {}
    for j, expr in enumerate(expressions):
        if expr["name"] in ["ext_avgc50_adr14", "ext_avgc200_adr14"]:
            _w_ext_name_to_idx[expr["name"]] = j


def _test_one_ticker(args):
    """Test one ticker. Uses temp dir for all file I/O.

    Args: (ticker, df_dict, weekly_dict, monthly_dict)
    Returns: (ticker, passed, n_mismatches, details)
    """
    ticker, df_dict, weekly_dict, monthly_dict = args
    n_exprs = _w_n_exprs
    safe = ticker.replace("/", "_").replace(".", "_")

    from scripts.setup_forward_prop import (
        N_INTERMEDIATES, _init_worker as _init_setup_worker, _setup_one_ticker,
    )
    from local_runner.forward_prop_engine import (
        _init_fp_worker, _forward_prop_one_ticker,
    )

    try:
        # ── Load existing .npz (the truth) ──
        npz_path = os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")
        if not os.path.exists(npz_path):
            return (ticker, True, 0, "skipped (no .npz)")

        loaded = np.load(npz_path, allow_pickle=True)
        full_data = loaded["data"]  # (N, 15805) float16
        full_dates = loaded["dates"]
        n_bars = full_data.shape[0]

        if n_bars < 100:
            return (ticker, True, 0, "skipped (<100 bars)")

        # Truth = last row of .npz (already float16 — this IS the ground truth)
        truth_f16 = full_data[-1]  # float16

        # Base = first N-1 rows
        base_data = full_data[:-1]
        base_dates = full_dates[:-1]

        # ── Find bar N's OHLCV from daily cache ──
        last_date = str(full_dates[-1])[:10]
        df_dates = df_dict["date"]

        # Find the bar in OHLCV that matches the last .npz date
        ohlcv_dates_str = [str(d)[:10] for d in df_dates]
        try:
            bar_pos = ohlcv_dates_str.index(last_date)
        except ValueError:
            return (ticker, True, 0, f"skipped (date {last_date} not in OHLCV)")

        today_ohlcv = {
            "open": float(df_dict["open"][bar_pos]),
            "high": float(df_dict["high"][bar_pos]),
            "low": float(df_dict["low"][bar_pos]),
            "close": float(df_dict["close"][bar_pos]),
            "volume": float(df_dict["volume"][bar_pos]),
            "date": df_dict["date"][bar_pos],
        }

        # df_dict for LSP/algo needs all bars up to and including bar N
        # (they scan full OHLCV history)
        df_dict_to_bar = {
            "date": df_dict["date"][:bar_pos + 1],
            "open": df_dict["open"][:bar_pos + 1],
            "high": df_dict["high"][:bar_pos + 1],
            "low": df_dict["low"][:bar_pos + 1],
            "close": df_dict["close"][:bar_pos + 1],
            "volume": df_dict["volume"][:bar_pos + 1],
        }

        # df_dict for setup needs bars up to N-1
        df_dict_base = {
            "date": df_dict["date"][:bar_pos],
            "open": df_dict["open"][:bar_pos],
            "high": df_dict["high"][:bar_pos],
            "low": df_dict["low"][:bar_pos],
            "close": df_dict["close"][:bar_pos],
            "volume": df_dict["volume"][:bar_pos],
        }

        if len(df_dict_base["date"]) < 50:
            return (ticker, True, 0, "skipped (base <50 bars)")

        # ── Use temp dir ──
        tmp_dir = tempfile.mkdtemp(prefix=f"fpt_{safe}_")
        try:
            import local_runner.expr_cache_builder as ecb
            import local_runner.forward_prop_engine as fpe
            import scripts.setup_forward_prop as sfp
            orig_ecb = ecb.EXPR_CACHE_DIR
            orig_fpe = fpe.EXPR_CACHE_DIR
            orig_sfp = sfp.EXPR_CACHE_DIR
            ecb.EXPR_CACHE_DIR = tmp_dir
            fpe.EXPR_CACHE_DIR = tmp_dir
            sfp.EXPR_CACHE_DIR = tmp_dir

            # Write base .npz (N-1 rows) to temp dir
            tmp_npz = os.path.join(tmp_dir, f"{safe}.npz")
            base_f16 = base_data.astype(np.float16) if base_data.dtype != np.float16 else base_data
            with zipfile.ZipFile(tmp_npz, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                for name, arr in [("data", base_f16), ("dates", base_dates)]:
                    buf = io.BytesIO()
                    np.save(buf, arr)
                    zf.writestr(name + ".npy", buf.getvalue())

            # Run setup
            _init_setup_worker(_w_expressions, _w_ext_name_to_idx)
            setup_result = _setup_one_ticker((ticker, df_dict_base, weekly_dict, monthly_dict))
            if setup_result is None or setup_result[0] is None:
                return (ticker, True, 0, "skipped (setup failed)")

            # Run forward-prop
            _init_fp_worker(_w_expressions)
            fp_result = _forward_prop_one_ticker(
                (ticker, today_ohlcv, df_dict_to_bar, weekly_dict, monthly_dict)
            )
            if fp_result[0] is None:
                return (ticker, False, n_exprs, "forward-prop returned None")

            # Load appended row
            append_path = os.path.join(tmp_dir, f"{safe}.append")
            if not os.path.exists(append_path):
                return (ticker, False, n_exprs, "no .append created")

            total_cols = n_exprs + N_INTERMEDIATES
            raw = np.fromfile(append_path, dtype=np.float16)
            if raw.size == 0 or raw.size % total_cols != 0:
                return (ticker, False, n_exprs, f"bad .append size {raw.size}")
            fp_f16 = raw.reshape(-1, total_cols)[-1, :n_exprs]  # float16 already

            # ── Compare: exact float16 match, zero tolerance ──
            both_nan = np.isnan(truth_f16) & np.isnan(fp_f16)
            match = (truth_f16 == fp_f16) | both_nan
            mismatches = ~match
            n_mm = int(np.sum(mismatches))

            if n_mm == 0:
                return (ticker, True, 0, "PASS")

            # Mismatch details
            mm_idx = np.where(mismatches)[0]
            op_counts = {}
            for idx in mm_idx:
                if idx < n_exprs:
                    comp = _w_expressions[idx]["compute"]
                    op = comp.get("op", "?")
                    src = comp.get("source", "")
                    key = f"precomputed_{src}" if op == "precomputed" and src else op
                else:
                    key = "?"
                op_counts[key] = op_counts.get(key, 0) + 1

            truth_val_fp_nan = int(np.sum(
                ~np.isnan(truth_f16.astype(np.float32)) &
                np.isnan(fp_f16.astype(np.float32)) & mismatches))
            truth_nan_fp_val = int(np.sum(
                np.isnan(truth_f16.astype(np.float32)) &
                ~np.isnan(fp_f16.astype(np.float32)) & mismatches))
            both_real = n_mm - truth_val_fp_nan - truth_nan_fp_val

            top_ops = sorted(op_counts.items(), key=lambda x: -x[1])[:5]
            details = (f"{n_mm} mm | val/NaN:{truth_val_fp_nan} NaN/val:{truth_nan_fp_val} "
                       f"real:{both_real} | " +
                       ", ".join(f"{k}={v}" for k, v in top_ops))

            # Print first 30 non-HTF mismatches for debugging
            non_htf = [i for i in mm_idx if _w_expressions[i]["compute"].get("op") != "precomputed"]
            if non_htf:
                print(f"\n    First 30 non-HTF mismatches for {ticker}:")
                for idx in non_htf[:30]:
                    name = _w_expressions[idx]["name"]
                    op = _w_expressions[idx]["compute"].get("op", "?")
                    tv = float(truth_f16[idx]) if not np.isnan(truth_f16[idx]) else "NaN"
                    fv = float(fp_f16[idx]) if not np.isnan(fp_f16[idx]) else "NaN"
                    print(f"      [{idx:5d}] {name:45s} {op:20s} truth={str(tv):>12s} fp={str(fv):>12s}")

            return (ticker, False, n_mm, details)

        finally:
            ecb.EXPR_CACHE_DIR = orig_ecb
            fpe.EXPR_CACHE_DIR = orig_fpe
            sfp.EXPR_CACHE_DIR = orig_sfp
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as e:
        return (ticker, False, -1, f"exception: {e}")


# ══════════════════════════════════════════════════════════════
# TEST RUNNER
# ══════════════════════════════════════════════════════════════

def _load_all_data():
    """Load expressions + OHLCV + HTF. Build work items."""
    expressions = _load_expressions()
    print(f"  {len(expressions)} expressions")

    print("  Loading OHLCV...")
    universe = _load_daily_cache()
    weekly = _load_htf_cache("weekly")
    monthly = _load_htf_cache("monthly")

    work = []
    for ticker, df in universe.items():
        df = _truncate_to_cache_window(df)
        if df is None or len(df) < 100:
            continue
        df_dict = {
            "date": df["date"].values,
            "open": df["open"].values,
            "high": df["high"].values,
            "low": df["low"].values,
            "close": df["close"].values,
            "volume": df["volume"].values,
        }
        w = _df_to_dict(weekly.get(ticker)) if weekly else None
        m = _df_to_dict(monthly.get(ticker)) if monthly else None
        work.append((ticker, df_dict, w, m))

    print(f"  {len(work)} tickers eligible")
    return expressions, work


def run_test(tickers, expressions, all_work, n_workers=1):
    """Run test on a set of tickers. Returns (passed, failed, skipped, failures)."""
    if tickers is not None:
        ticker_set = set(tickers)
        work = [w for w in all_work if w[0] in ticker_set]
    else:
        work = all_work

    if not work:
        print("  No tickers to test.")
        return 0, 0, 0, []

    passed = 0
    failed = 0
    skipped = 0
    failures = []
    t0 = time.time()

    def _process(result):
        nonlocal passed, failed, skipped
        t_out, ok, n_mm, details = result
        if "skipped" in details:
            skipped += 1
            return "SKIP"
        elif ok:
            passed += 1
            return "PASS"
        else:
            failed += 1
            failures.append(result)
            return "FAIL"

    def _progress(total, ticker, status, details=""):
        elapsed = time.time() - t0
        rate = total / elapsed if elapsed > 0 else 0
        eta = (len(work) - total) / rate if rate > 0 else 0
        line = (f"    [{total:5d}/{len(work)}] {ticker:8s} {status} "
                f"({passed}P/{failed}F/{skipped}S) "
                f"[{elapsed/60:.1f}m, ~{eta/60:.1f}m left]")
        if status == "FAIL":
            line += f"  {details}"
        print(line)

    if n_workers <= 1:
        _init_test_worker(expressions)
        for i, item in enumerate(work):
            result = _test_one_ticker(item)
            status = _process(result)
            total = passed + failed + skipped
            if total % 10 == 0 or status == "FAIL" or total == len(work):
                _progress(total, result[0], status, result[3])
    else:
        max_in_flight = n_workers * 3
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_init_test_worker,
            initargs=(expressions,),
        ) as pool:
            pending = {}
            idx = 0
            for _ in range(min(max_in_flight, len(work))):
                if idx < len(work):
                    f = pool.submit(_test_one_ticker, work[idx])
                    pending[f] = idx
                    idx += 1

            while pending:
                done = next(iter(as_completed(pending)))
                pending.pop(done)
                try:
                    result = done.result()
                except Exception as e:
                    result = ("?", False, -1, f"worker crash: {e}")
                status = _process(result)

                if idx < len(work):
                    f = pool.submit(_test_one_ticker, work[idx])
                    pending[f] = idx
                    idx += 1

                total = passed + failed + skipped
                if total % 50 == 0 or status == "FAIL" or total == len(work):
                    _progress(total, result[0], status, result[3])

    return passed, failed, skipped, failures


def _print_results(label, passed, failed, skipped, failures, t0):
    elapsed = time.time() - t0
    print(f"\n  {'='*50}")
    print(f"  {label}")
    print(f"  {'='*50}")
    print(f"  Passed:  {passed}")
    print(f"  Failed:  {failed}")
    print(f"  Skipped: {skipped}")
    print(f"  Time:    {elapsed/60:.1f} min")
    if failures:
        print(f"\n  Failures:")
        for t, ok, n_mm, details in failures:
            print(f"    {t}: {details}")
    if failed == 0:
        print(f"\n  PASS")
    else:
        print(f"\n  FAIL")
    return failed == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Forward-prop correctness (zero tolerance)")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--gate2", action="store_true", help="100 random tickers")
    parser.add_argument("--all", action="store_true", help="ALL tickers (~24 min)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--workers", type=int, default=14)
    args = parser.parse_args()

    t0 = time.time()
    expressions, all_work = _load_all_data()

    if args.all:
        print(f"\n{'='*60}")
        print(f"  FULL VALIDATION — ALL {len(all_work)} tickers ({args.workers} workers)")
        print(f"{'='*60}")
        p, f, s, failures = run_test(None, expressions, all_work, n_workers=args.workers)
        success = _print_results("FULL VALIDATION", p, f, s, failures, t0)

    elif args.gate2:
        rng = random.Random(42)
        sample = rng.sample([w[0] for w in all_work], min(args.limit, len(all_work)))
        print(f"\n{'='*60}")
        print(f"  GATE 2 — {len(sample)} tickers ({args.workers} workers)")
        print(f"{'='*60}")
        p, f, s, failures = run_test(sample, expressions, all_work, n_workers=args.workers)
        success = _print_results("GATE 2", p, f, s, failures, t0)

    else:
        print(f"\n{'='*60}")
        print(f"  GATE 1 — {args.ticker}")
        print(f"{'='*60}")
        p, f, s, failures = run_test([args.ticker], expressions, all_work, n_workers=1)
        success = _print_results("GATE 1", p, f, s, failures, t0)

    sys.exit(0 if success else 1)
