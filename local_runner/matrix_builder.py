"""
Matrix Builder — Precompute expression values for universe + examples.

Two matrices:
  1. Universe matrix: ALL tickers × ALL expressions at last bar (daily refresh)
     - Shared across setups
     - ~20-40 min to compute, cached to disk
     - Rebuilds automatically if >24h old

  2. Example matrix: setup examples × ALL expressions at entry bar (per-setup)
     - Fast (~5s per example)
     - Rebuilds when examples change

Usage:
    from local_runner.matrix_builder import get_universe_matrix, get_example_matrix
"""

import os
import sys
import time
import pickle
import json
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timezone, timedelta

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

API_BASE = "https://web-production-e3025.up.railway.app"


def _load_expressions(setup_type=None, force=False):
    """Load generic expressions (same for all setups).
    Regenerates JSON cache if force=True or if source .py is newer than cached .json.
    """
    path = os.path.join(CACHE_DIR, "brute_expressions.json")
    src = os.path.join(LOCAL_DIR, "brute_expressions.py")
    need_regen = force or not os.path.exists(path)
    if not need_regen and os.path.exists(src):
        need_regen = os.path.getmtime(src) > os.path.getmtime(path)
    if need_regen:
        from brute_expressions import generate_all
        os.makedirs(CACHE_DIR, exist_ok=True)
        exprs = generate_all()
        cats = {}
        for e in exprs:
            cat = e["category"]
            cats[cat] = cats.get(cat, 0) + 1
        with open(path, "w") as f:
            json.dump({"total": len(exprs), "by_category": cats, "expressions": exprs}, f)
        return exprs
    with open(path) as f:
        return json.load(f)["expressions"]


def _load_ohlcv_cache():
    """Load OHLCV cache, building if needed."""
    cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(cache_file):
        from cache_builder import build_cache
        build_cache(force=True)
    with open(cache_file, "rb") as f:
        return pickle.load(f)


_worker_expressions = None
_worker_cache = None

def _init_worker(expressions, cache_dict):
    """Initialize worker process with shared expression list and OHLCV cache.
    Cache is serialized ONCE per worker at startup, not per task."""
    global _worker_expressions, _worker_cache
    _worker_expressions = expressions
    _worker_cache = cache_dict


def _compute_ticker_worker_single(ticker):
    """Worker function for single ticker (used inside batch)."""
    global _worker_expressions, _worker_cache
    expressions = _worker_expressions
    df = _worker_cache.get(ticker)
    if df is None or len(df) < 50:
        return None
    try:
        from scripts.expression_engine import ExpressionEngine
        engine = ExpressionEngine(df)
        engine.set_target(len(df) - 1)
        values = np.full(len(expressions), np.nan)
        for j, expr in enumerate(expressions):
            try:
                val = engine.compute(expr)
                if val is not None and not np.isnan(val):
                    values[j] = val
            except:
                pass
        return values
    except:
        return np.full(len(expressions), np.nan)


def _compute_ticker_batch(ticker_list):
    """Process a batch of tickers. Returns list of (ticker, values) tuples."""
    results = []
    for ticker in ticker_list:
        vals = _compute_ticker_worker_single(ticker)
        results.append((ticker, vals))
    return results


def _compute_ticker_values(df, target_idx, expressions):
    """Compute all expression values for one ticker at one bar."""
    from scripts.expression_engine import ExpressionEngine
    engine = ExpressionEngine(df)
    engine.set_target(target_idx)
    values = np.full(len(expressions), np.nan)
    for j, expr in enumerate(expressions):
        try:
            val = engine.compute(expr)
            if val is not None and not np.isnan(val):
                values[j] = val
        except:
            pass
    return values


def _get_et_date():
    """Get current date in US/Eastern time (handles EST/EDT automatically)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except ImportError:
        utc_now = datetime.now(timezone.utc)
        offset = timedelta(hours=-4 if 3 <= utc_now.month <= 10 else -5)
        return (utc_now + offset).date()


def _universe_matrix_fresh():
    """Check if universe matrix exists and was built today (ET date)."""
    path = os.path.join(CACHE_DIR, "universe_matrix.pkl")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        built = data.get("computed_at", "")
        if not built:
            return False
        built_dt = datetime.fromisoformat(built)
        if built_dt.tzinfo is None:
            built_dt = built_dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            built_date = built_dt.astimezone(ZoneInfo("America/New_York")).date()
        except ImportError:
            offset = timedelta(hours=-4 if 3 <= built_dt.month <= 10 else -5)
            built_date = (built_dt + offset).date()
        return built_date == _get_et_date()
    except:
        return False


def get_universe_matrix(progress_fn=None, force=False):
    """
    Get or build the universe expression matrix.
    Returns dict with universe_matrix, universe_tickers, expr_names, expr_categories.
    """
    path = os.path.join(CACHE_DIR, "universe_matrix.pkl")

    def log(msg):
        print(f"  [matrix] {msg}")
        if progress_fn:
            progress_fn("matrix", 5, msg)

    # Check cache
    if not force and _universe_matrix_fresh():
        log("Matrix is fresh — loading from cache...")
        with open(path, "rb") as f:
            data = pickle.load(f)
        # Verify expression count matches
        expressions = _load_expressions(force=False)
        cached_n = data.get("n_exprs")
        current_n = len(expressions)
        if cached_n == current_n:
            log(f"Cache OK: {data['n_universe']} tickers × {current_n} expressions (built {data.get('computed_at', 'unknown')})")
            if progress_fn:
                progress_fn("matrix", 10, f"Universe matrix cached: {data['n_universe']} tickers")
            return data
        log(f"Expression count changed ({cached_n} → {current_n}) — rebuilding...")
    elif force:
        log("Force rebuild requested.")
    else:
        # Not fresh — log why
        if not os.path.exists(path):
            log("No matrix cache found — building from scratch...")
        else:
            try:
                with open(path, "rb") as f:
                    data = pickle.load(f)
                built = data.get("computed_at", "unknown")
                log(f"Matrix stale (built {built}, ET date mismatch) — rebuilding...")
            except:
                log("Matrix cache unreadable — rebuilding...")

    # Build from scratch
    expressions = _load_expressions()
    if progress_fn:
        progress_fn("matrix", 5, f"Building universe matrix ({len(expressions)} expressions)...")

    # Load OHLCV
    universe_cache = _load_ohlcv_cache()
    uni_tickers = list(universe_cache.keys())
    universe_matrix = np.full((len(uni_tickers), len(expressions)), np.nan)

    if progress_fn:
        progress_fn("matrix", 10, f"Computing {len(uni_tickers)} tickers × {len(expressions)} expressions...")

    t0 = time.time()

    # Parallel build — use all cores minus 1 for OS headroom
    # Set MATRIX_WORKERS=1 to disable parallelism for debugging
    from multiprocessing import cpu_count as _cpu_count
    n_workers = int(os.environ.get("MATRIX_WORKERS", max(_cpu_count() - 1, 1)))

    # Build ticker index for mapping results back
    ticker_to_idx = {t: i for i, t in enumerate(uni_tickers)}
    valid_tickers = [t for t in uni_tickers 
                     if universe_cache.get(t) is not None and len(universe_cache[t]) >= 50]

    print(f"    {'Parallel' if n_workers > 1 else 'Sequential'} build: {len(valid_tickers)} tickers × {len(expressions)} expressions"
          + (f", {n_workers} workers" if n_workers > 1 else ""))

    completed = 0

    if n_workers <= 1:
        # Sequential fallback
        global _worker_expressions, _worker_cache
        _worker_expressions = expressions
        _worker_cache = universe_cache
        for ticker in valid_tickers:
            vals = _compute_ticker_worker_single(ticker)
            if vals is not None:
                universe_matrix[ticker_to_idx[ticker]] = vals
            completed += 1
            if completed % 100 == 0 or completed == len(valid_tickers):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(valid_tickers) - completed) / rate if rate > 0 else 0
                pct = 10 + int(85 * completed / len(valid_tickers))
                msg = f"Universe: {completed}/{len(valid_tickers)} ({completed/len(valid_tickers)*100:.0f}%) [{elapsed:.0f}s, ~{eta:.0f}s left]"
                print(f"    {msg}")
                if progress_fn:
                    progress_fn("matrix", pct, msg)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        # Batch tickers — cache serialized once per worker via initializer
        batch_size = max(len(valid_tickers) // (n_workers * 4), 50)
        batches = [valid_tickers[i:i+batch_size] for i in range(0, len(valid_tickers), batch_size)]

        print(f"    {len(batches)} batches of ~{batch_size} tickers")

        with ProcessPoolExecutor(max_workers=n_workers,
                                 initializer=_init_worker, 
                                 initargs=(expressions, universe_cache)) as executor:
            futures = {executor.submit(_compute_ticker_batch, batch): batch 
                       for batch in batches}

            for future in as_completed(futures):
                try:
                    batch_results = future.result()
                    for ticker, vals in batch_results:
                        if vals is not None:
                            universe_matrix[ticker_to_idx[ticker]] = vals
                except Exception as e:
                    pass  # leave as NaN

                completed += 1
                done_tickers = completed * batch_size
                if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                    elapsed = time.time() - t0
                    rate = done_tickers / elapsed if elapsed > 0 else 0
                    eta = (len(valid_tickers) - done_tickers) / rate if rate > 0 else 0
                    pct = 10 + int(85 * min(done_tickers, len(valid_tickers)) / len(valid_tickers))
                    msg = f"Universe: ~{min(done_tickers, len(valid_tickers))}/{len(valid_tickers)} ({min(done_tickers, len(valid_tickers))/len(valid_tickers)*100:.0f}%) [{elapsed:.0f}s, ~{max(eta,0):.0f}s left]"
                    print(f"    {msg}")
                    if progress_fn:
                        progress_fn("matrix", pct, msg)

    data = {
        "universe_matrix": universe_matrix,
        "universe_tickers": uni_tickers,
        "expr_names": [e["name"] for e in expressions],
        "expr_categories": [e.get("category", "unknown") for e in expressions],
        "n_exprs": len(expressions),
        "n_universe": len(uni_tickers),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(path) / 1024 / 1024
    elapsed = time.time() - t0
    print(f"    Universe matrix saved: {size_mb:.1f} MB ({elapsed:.0f}s)")
    if progress_fn:
        progress_fn("matrix", 95, f"Universe matrix built: {size_mb:.1f} MB in {elapsed/60:.1f} min")

    return data


def get_example_matrix(setup_type, progress_fn=None):
    """
    Get or build the example expression matrix for a setup.
    Fast — only examples, not universe.
    Uses same generic expression set as universe matrix.
    Returns dict with example_matrix, example_tickers.
    """
    path = os.path.join(CACHE_DIR, f"example_matrix_{setup_type}.pkl")

    # Get current example IDs from API to check freshness
    try:
        r = requests.get(f"{API_BASE}/api/examples/{setup_type}", timeout=15)
        raw_examples = r.json().get("examples", []) if r.status_code == 200 else []
    except:
        raw_examples = []

    current_ids = sorted([str(e.get("id", "")) for e in raw_examples])

    # Check cache
    if os.path.exists(path):
        with open(path, "rb") as f:
            cached = pickle.load(f)
        cached_ids = sorted(cached.get("example_ids", []))
        expressions = _load_expressions()
        if cached_ids == current_ids and cached.get("n_exprs") == len(expressions):
            if progress_fn:
                progress_fn("examples", 100, f"Example matrix cached: {len(current_ids)} examples")
            return cached

    # Build
    expressions = _load_expressions()
    if progress_fn:
        progress_fn("examples", 10, f"Computing {len(raw_examples)} examples ({len(expressions)} expressions)...")

    example_matrix = np.full((len(raw_examples), len(expressions)), np.nan)
    example_tickers = []
    example_ids = []

    def _process_example(i_ex_tuple):
        """Fetch OHLCV + compute expressions for one example."""
        i, ex = i_ex_tuple
        ticker = ex.get("ticker", "?")
        entry_date = ex.get("entryDate") or ex.get("chartDate")
        ex_id = str(ex.get("id", ""))
        try:
            r2 = requests.get(f"{API_BASE}/api/ohlcv/local/{setup_type}/{ex.get('id')}", timeout=15)
            if r2.status_code != 200:
                return (i, ticker, ex_id, entry_date, None, "HTTP " + str(r2.status_code))
            candles = r2.json().get("candles", [])
            if not candles:
                return (i, ticker, ex_id, entry_date, None, "no candles")
            df = pd.DataFrame(candles)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            target_idx = len(df) - 1
            if entry_date:
                matches = df[df["date"].dt.strftime("%Y-%m-%d") == entry_date]
                if len(matches) > 0:
                    target_idx = matches.index[0] - 1

            values = _compute_ticker_values(df, target_idx, expressions)
            n_valid = int(np.sum(~np.isnan(values)))
            return (i, ticker, ex_id, entry_date, values, f"{n_valid}/{len(expressions)}")
        except Exception as e:
            return (i, ticker, ex_id, entry_date, None, str(e))

    # Run all examples concurrently (I/O-bound: API fetch)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    n_threads = min(10, len(raw_examples))
    completed = 0

    with ThreadPoolExecutor(max_workers=n_threads) as executor:
        futures = {executor.submit(_process_example, (i, ex)): i
                   for i, ex in enumerate(raw_examples)}

        for future in as_completed(futures):
            i, ticker, ex_id, entry_date, values, msg = future.result()
            if values is not None:
                example_matrix[i] = values
                symbol = "✓"
            else:
                symbol = "✗"
            print(f"    {symbol} {ticker:8s} ({entry_date}) — {msg}")

            completed += 1
            if progress_fn:
                pct = 10 + int(90 * completed / len(raw_examples))
                progress_fn("examples", pct, f"Examples: {completed}/{len(raw_examples)} ({ticker})")

    # Build ticker/id lists in original order
    for i, ex in enumerate(raw_examples):
        example_tickers.append(f"{ex.get('ticker', '?')}_{ex.get('id', '')}")
        example_ids.append(str(ex.get("id", "")))

    data = {
        "example_matrix": example_matrix,
        "example_tickers": example_tickers,
        "example_ids": example_ids,
        "expr_names": [e["name"] for e in expressions],
        "expr_categories": [e.get("category", "unknown") for e in expressions],
        "n_exprs": len(expressions),
        "n_examples": len(raw_examples),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    if progress_fn:
        progress_fn("examples", 100, f"Example matrix built: {len(raw_examples)} examples")

    return data



