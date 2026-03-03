"""
Matrix Builder — Load precomputed expression values for universe + examples.

ALL data comes from the expression series cache (expr_series/*.npz files).
No live computation — expressions are precomputed by expr_cache_builder.py.

This ensures:
  1. All grinders use the exact same computation path
  2. LSP, HTF, and daily expressions all work identically
  3. Universe matrix build is ~30s (file reads) instead of ~30 min (live compute)

Two matrices:
  1. Universe matrix: ALL tickers × ALL expressions at last bar
     - Loaded from expr cache (last row per ticker)
     - Rebuilds if cache fingerprint changes

  2. Example matrix: setup examples × ALL expressions at entry bar
     - Loaded from expr cache (scan_idx row per example)
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
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

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


# ══════════════════════════════════════════════════════════════
# EXPR CACHE LOADING (parallel)
# ══════════════════════════════════════════════════════════════

def _load_ticker_last_bar(ticker):
    """Load the last bar's expression values for a ticker from the expr cache.
    Returns (ticker, values_array) or (ticker, None) if not cached.
    """
    from expr_cache_builder import load_ticker_cache
    dates, data = load_ticker_cache(ticker)
    if dates is None or data is None or len(data) == 0:
        return (ticker, None)
    # Last bar = last row
    return (ticker, data[-1, :].astype(np.float64))


def _load_ticker_at_bar(args):
    """Load expression values at a specific bar index for a ticker.
    args: (ticker, scan_idx, entry_date_str)
    Returns (ticker, entry_date_str, values_array) or (..., None).
    """
    ticker, scan_idx, entry_date_str = args
    from expr_cache_builder import load_ticker_cache
    dates, data = load_ticker_cache(ticker)
    if dates is None or data is None:
        return (ticker, entry_date_str, None)
    if scan_idx >= len(data):
        return (ticker, entry_date_str, None)
    return (ticker, entry_date_str, data[scan_idx, :].astype(np.float64))


# ══════════════════════════════════════════════════════════════
# FRESHNESS CHECK
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
# UNIVERSE MATRIX — loads from expr cache
# ══════════════════════════════════════════════════════════════

def get_universe_matrix(progress_fn=None, force=False):
    """
    Get or build the universe expression matrix by loading from the expression
    series cache. Each ticker's last bar is extracted from its cached .npz file.

    Falls back to live computation ONLY if no expression cache exists.

    Returns dict with universe_matrix, universe_tickers, expr_names, expr_categories.
    """
    path = os.path.join(CACHE_DIR, "universe_matrix.pkl")

    def log(msg):
        print(f"  [matrix] {msg}")
        if progress_fn:
            progress_fn("matrix", 5, msg)

    # Load expression library
    expressions = _load_expressions(force=False)

    # Check cache freshness
    if not force and _universe_matrix_fresh():
        log("Matrix is fresh — loading from cache...")
        with open(path, "rb") as f:
            data = pickle.load(f)
        cached_n = data.get("n_exprs")
        current_n = len(expressions)
        if cached_n == current_n:
            log(f"Cache OK: {data['n_universe']} tickers × {current_n} expressions "
                f"(built {data.get('computed_at', 'unknown')})")
            if progress_fn:
                progress_fn("matrix", 10, f"Universe matrix cached: {data['n_universe']} tickers")
            return data
        log(f"Expression count changed ({cached_n} → {current_n}) — rebuilding...")
    elif force:
        log("Force rebuild requested.")
    else:
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

    # ── Try loading from expression series cache (fast path) ──
    from expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()

    if expr_cache.is_valid():
        # Cache exists — check if our expressions are a subset of what's cached
        cache_names = set(expr_cache.expr_names)
        our_names = [e["name"] for e in expressions]
        missing = [n for n in our_names if n not in cache_names]
        if missing:
            log(f"⚠ {len(missing)} expressions not in cache. Falling back to live computation.")
            return _build_universe_live(expressions, path, progress_fn)
        return _build_universe_from_cache(expr_cache, expressions, path, progress_fn)
    else:
        log("⚠ Expression series cache not available.")
        log("  Run: python local_runner/expr_cache_builder.py --build")
        log("  Falling back to live computation (slow)...")
        return _build_universe_live(expressions, path, progress_fn)


def _build_universe_from_cache(expr_cache, expressions, save_path, progress_fn=None):
    """Build universe matrix by reading last bar from each ticker's cache file.
    Uses ProcessPoolExecutor for parallel file I/O.
    """
    def log(msg):
        print(f"  [matrix] {msg}")
        if progress_fn:
            progress_fn("matrix", 5, msg)

    log(f"Building from expression cache ({expr_cache.n_expressions} expressions)...")

    # Get list of cached tickers
    cached_tickers = sorted(expr_cache.get_available_tickers())
    n_tickers = len(cached_tickers)
    n_exprs = len(expressions)

    if progress_fn:
        progress_fn("matrix", 10, f"Loading {n_tickers} tickers from cache...")

    t0 = time.time()

    # Verify our expressions exist in cache (cache may have more, that's OK)
    cache_names = expr_cache.expr_names
    current_names = [e["name"] for e in expressions]
    cache_name_set = set(cache_names)
    missing = [n for n in current_names if n not in cache_name_set]
    if missing:
        log(f"⚠ {len(missing)} expressions not in cache. Cannot build from cache.")
        raise RuntimeError(f"Missing expressions in cache: {missing[:5]}")

    # Build column index mapping: our expression index -> cache column index
    cache_name_to_idx = {n: i for i, n in enumerate(cache_names)}
    col_map = [cache_name_to_idx[n] for n in current_names]
    exact_match = (cache_names == current_names)

    universe_matrix = np.full((n_tickers, n_exprs), np.nan)
    ticker_to_idx = {t: i for i, t in enumerate(cached_tickers)}

    # Parallel file loading
    n_workers = max(cpu_count() - 1, 1)

    log(f"Parallel load: {n_tickers} tickers, {n_workers} workers")

    completed_tickers = 0

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_load_ticker_last_bar, ticker): ticker
                   for ticker in cached_tickers}

        for future in as_completed(futures):
            ticker, values = future.result()
            if values is not None:
                idx = ticker_to_idx[ticker]
                if exact_match:
                    universe_matrix[idx] = values
                else:
                    universe_matrix[idx] = values[col_map]
            completed_tickers += 1
            if completed_tickers % 500 == 0 or completed_tickers == n_tickers:
                elapsed = time.time() - t0
                rate = completed_tickers / elapsed if elapsed > 0 else 0
                eta = (n_tickers - completed_tickers) / rate if rate > 0 else 0
                pct = 10 + int(85 * completed_tickers / n_tickers)
                msg = (f"Universe: {completed_tickers}/{n_tickers} "
                       f"({completed_tickers/n_tickers*100:.0f}%) "
                       f"[{elapsed:.0f}s, ~{eta:.0f}s left]")
                print(f"    {msg}")
                if progress_fn:
                    progress_fn("matrix", pct, msg)

    data = {
        "universe_matrix": universe_matrix,
        "universe_tickers": cached_tickers,
        "expr_names": current_names,
        "expr_categories": [e.get("category", "unknown") for e in expressions],
        "n_exprs": n_exprs,
        "n_universe": n_tickers,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "expr_cache",
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(save_path, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(save_path) / 1024 / 1024
    elapsed = time.time() - t0
    log(f"Universe matrix saved: {size_mb:.1f} MB ({elapsed:.0f}s) — "
        f"{n_tickers} tickers × {n_exprs} expressions [from expr cache]")
    if progress_fn:
        progress_fn("matrix", 95, f"Universe matrix built: {size_mb:.1f} MB in {elapsed:.0f}s")

    return data


def _build_universe_live(expressions, save_path, progress_fn=None):
    """FALLBACK: Build universe matrix by computing expressions live.
    Only used when no expression cache exists.
    WARNING: This path cannot compute LSP/HTF/precomputed expressions — they will be NaN.
    """
    def log(msg):
        print(f"  [matrix] {msg}")
        if progress_fn:
            progress_fn("matrix", 5, msg)

    log("Building universe matrix via live computation (NO expr cache)...")
    log("⚠ LSP, weekly, and monthly expressions will be NaN (precomputed-only).")

    # Load OHLCV
    cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(cache_file):
        # Try 5yr cache
        cache_file = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(cache_file):
        from cache_builder import build_cache
        cache_file = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
        build_cache(force=True)
    with open(cache_file, "rb") as f:
        universe_cache = pickle.load(f)

    uni_tickers = list(universe_cache.keys())
    n_exprs = len(expressions)
    universe_matrix = np.full((len(uni_tickers), n_exprs), np.nan)

    if progress_fn:
        progress_fn("matrix", 10, f"Computing {len(uni_tickers)} tickers × {n_exprs} expressions...")

    t0 = time.time()

    from scripts.expression_engine import ExpressionEngine

    for i, ticker in enumerate(uni_tickers):
        df = universe_cache.get(ticker)
        if df is None or len(df) < 50:
            continue
        try:
            engine = ExpressionEngine(df)
            engine.set_target(len(df) - 1)
            for j, expr in enumerate(expressions):
                try:
                    val = engine.compute(expr)
                    if val is not None and not np.isnan(val):
                        universe_matrix[i, j] = val
                except:
                    pass
        except:
            pass

        if (i + 1) % 100 == 0 or (i + 1) == len(uni_tickers):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(uni_tickers) - i - 1) / rate if rate > 0 else 0
            pct = 10 + int(85 * (i + 1) / len(uni_tickers))
            msg = (f"Universe: {i+1}/{len(uni_tickers)} "
                   f"({(i+1)/len(uni_tickers)*100:.0f}%) "
                   f"[{elapsed:.0f}s, ~{eta:.0f}s left]")
            print(f"    {msg}")
            if progress_fn:
                progress_fn("matrix", pct, msg)

    data = {
        "universe_matrix": universe_matrix,
        "universe_tickers": uni_tickers,
        "expr_names": [e["name"] for e in expressions],
        "expr_categories": [e.get("category", "unknown") for e in expressions],
        "n_exprs": n_exprs,
        "n_universe": len(uni_tickers),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "live_compute",
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(save_path, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(save_path) / 1024 / 1024
    elapsed = time.time() - t0
    log(f"Universe matrix saved: {size_mb:.1f} MB ({elapsed:.0f}s) [LIVE — some expressions NaN]")
    if progress_fn:
        progress_fn("matrix", 95, f"Universe matrix built: {size_mb:.1f} MB in {elapsed/60:.1f} min")

    return data


# ══════════════════════════════════════════════════════════════
# EXAMPLE MATRIX — kept for backward compat with get_example_matrix()
# (pyramid_grinder uses its own compute_example_ranges instead)
# ══════════════════════════════════════════════════════════════

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
            n_valid = int(np.sum(~np.isnan(values)))
            return (i, ticker, ex_id, entry_date, values, f"{n_valid}/{len(expressions)}")
        except Exception as e:
            return (i, ticker, ex_id, entry_date, None, str(e))

    # Run all examples concurrently (I/O-bound: API fetch)
    from concurrent.futures import ThreadPoolExecutor
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
