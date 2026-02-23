"""
Profiler Test Run — Dry run with 25 test expressions.

Usage:
    python scripts/run_profiler_test.py

Runs against:
  1. All DTSS examples (from cache)
  2. 50 random universe tickers (from API)

Reports timing and sample results.
"""

import json
import os
import sys
import time
import concurrent.futures
import requests
import numpy as np
import pandas as pd

# Add repo root to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.expression_engine import ExpressionEngine

API_BASE = "https://web-production-e3025.up.railway.app"
DATA_DIR = os.path.join(REPO_ROOT, "data")


def load_expressions(path):
    with open(path) as f:
        data = json.load(f)
    return data["expressions"]


def fetch_ohlcv_from_api(ticker, date=None):
    """Fetch OHLCV from Railway API via SQL query."""
    try:
        where = f"ticker = '{ticker}'"
        if date:
            where += f" AND date <= '{date}'"
        sql = f"SELECT date, open, high, low, close, volume FROM universe_ohlcv WHERE {where} ORDER BY date DESC LIMIT 300"
        r = requests.post(f"{API_BASE}/api/query", json={"sql": sql}, timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
        rows = data.get("results", [])
        if not rows:
            return None
        df = pd.DataFrame(rows)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def load_example_ohlcv(setup_type, example_id):
    """Load example OHLCV from API."""
    try:
        r = requests.get(f"{API_BASE}/api/ohlcv/local/{setup_type}/{example_id}", timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("candles"):
            return None
        df = pd.DataFrame(data["candles"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def get_examples(setup_type):
    """Get example list from API."""
    r = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=15)
    return r.json() if r.status_code == 200 else []


def get_random_tickers(n):
    """Get n random tickers from tradable universe."""
    try:
        r = requests.get(f"{API_BASE}/api/universe/random?n={n}", timeout=15)
        if r.status_code == 200:
            return r.json().get("tickers", [])
    except:
        pass
    # Fallback: query DB directly
    try:
        r = requests.post(f"{API_BASE}/api/query", json={
            "sql": f"SELECT ticker FROM tradable_universe ORDER BY RANDOM() LIMIT {n}"
        }, timeout=15)
        if r.status_code == 200:
            return [row["ticker"] for row in r.json().get("results", [])]
    except:
        pass
    return []


def profile_ticker(df, target_idx, expressions):
    """Compute all expressions for one ticker at target index."""
    engine = ExpressionEngine(df)
    engine.set_target(target_idx)
    results = {}
    for expr in expressions:
        val = engine.compute(expr)
        results[expr["name"]] = float(val) if not (val is None or np.isnan(val)) else None
    return results


def main():
    print("\n" + "=" * 60)
    print("  PROFILER TEST RUN — 25 expressions, dry run")
    print("=" * 60)

    # Load test expressions
    expr_path = os.path.join(DATA_DIR, "dtss_expressions_test.json")
    expressions = load_expressions(expr_path)
    print(f"\n  Loaded {len(expressions)} test expressions")

    # ── Phase 1: Examples ────────────────────────────────
    print(f"\n{'─' * 50}")
    print("  Phase 1: DTSS Examples")
    print(f"{'─' * 50}")

    examples_resp = get_examples("dtss")
    examples = examples_resp.get("examples", []) if isinstance(examples_resp, dict) else examples_resp
    print(f"  Found {len(examples)} examples")

    t0 = time.time()
    example_results = {}
    errors = []

    for ex in examples:
        ticker = ex.get("ticker", "?")
        ex_id = ex.get("id")
        entry_date = ex.get("entryDate") or ex.get("chartDate")

        df = load_example_ohlcv("dtss", ex_id)
        if df is None or len(df) < 50:
            errors.append(f"  ✗ {ticker}: no data or too short")
            continue

        # Find target index (entry date or last bar)
        if entry_date:
            date_match = df[df["date"].dt.strftime("%Y-%m-%d") == entry_date]
            if len(date_match) > 0:
                target_idx = date_match.index[0] - 1  # scan runs BEFORE entry candle closes
            else:
                target_idx = len(df) - 1
        else:
            target_idx = len(df) - 1

        results = profile_ticker(df, target_idx, expressions)
        example_results[ticker] = results
        non_null = sum(1 for v in results.values() if v is not None)
        print(f"  ✓ {ticker:8s} — {non_null}/{len(expressions)} values computed")

    t_examples = time.time() - t0
    print(f"\n  Examples done: {len(example_results)} tickers, {t_examples:.1f}s")

    if errors:
        print(f"  Errors: {len(errors)}")
        for e in errors[:5]:
            print(f"    {e}")

    # ── Phase 2: Universe sample ─────────────────────────
    print(f"\n{'─' * 50}")
    print("  Phase 2: Universe Sample (50 tickers)")
    print(f"{'─' * 50}")

    tickers = get_random_tickers(50)
    print(f"  Got {len(tickers)} random tickers")

    t0 = time.time()
    universe_results = {}
    uni_errors = 0

    for ticker in tickers:
        df = fetch_ohlcv_from_api(ticker)
        if df is None or len(df) < 50:
            uni_errors += 1
            continue

        target_idx = len(df) - 1
        results = profile_ticker(df, target_idx, expressions)
        universe_results[ticker] = results

    t_universe = time.time() - t0
    print(f"\n  Universe done: {len(universe_results)} tickers, {t_universe:.1f}s")
    if uni_errors:
        print(f"  Skipped: {uni_errors} (no data / too short)")

    # Per-ticker timing
    if len(universe_results) > 0:
        ms_per = (t_universe / len(universe_results)) * 1000
        est_full = ms_per * 4167 / 1000
        print(f"  Per ticker: {ms_per:.0f}ms")
        print(f"  Estimated full universe (4,167): {est_full:.0f}s ({est_full/60:.1f} min)")

    # ── Sample output ────────────────────────────────────
    print(f"\n{'─' * 50}")
    print("  Sample Results (first example)")
    print(f"{'─' * 50}")

    if example_results:
        first_ticker = list(example_results.keys())[0]
        first = example_results[first_ticker]
        for name, val in first.items():
            val_str = f"{val:.4f}" if val is not None else "NULL"
            print(f"    {name:40s} {val_str}")

    # ── Summary ──────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Expressions:     {len(expressions)}")
    print(f"  Examples:        {len(example_results)} / {len(examples)}")
    print(f"  Universe sample: {len(universe_results)} / {len(tickers)}")
    print(f"  Example time:    {t_examples:.1f}s")
    print(f"  Universe time:   {t_universe:.1f}s")
    if len(universe_results) > 0:
        print(f"  Est. full run:   {est_full:.0f}s ({est_full/60:.1f} min)")
    print(f"  Status:          {'✓ ALL GOOD' if len(example_results) > 0 else '✗ PROBLEMS'}")
    print()


if __name__ == "__main__":
    main()
