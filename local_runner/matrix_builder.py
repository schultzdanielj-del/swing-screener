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


def _load_expressions():
    """Load brute expressions, generating if needed."""
    path = os.path.join(CACHE_DIR, "brute_expressions.json")
    if not os.path.exists(path):
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

    # Check cache
    if not force and _universe_matrix_fresh():
        if progress_fn:
            progress_fn("matrix", 5, "Loading cached universe matrix...")
        with open(path, "rb") as f:
            data = pickle.load(f)
        # Verify expression count matches
        expressions = _load_expressions()
        if data.get("n_exprs") == len(expressions):
            if progress_fn:
                progress_fn("matrix", 10, f"Universe matrix cached: {data['n_universe']} tickers")
            return data
        if progress_fn:
            progress_fn("matrix", 5, "Expression count changed, rebuilding...")

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
    for i, ticker in enumerate(uni_tickers):
        df = universe_cache[ticker]
        if df is None or len(df) < 50:
            continue
        universe_matrix[i] = _compute_ticker_values(df, len(df) - 1, expressions)

        if (i + 1) % 100 == 0 or (i + 1) == len(uni_tickers):
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(uni_tickers) - i - 1) / rate if rate > 0 else 0
            pct = 10 + int(85 * (i + 1) / len(uni_tickers))
            msg = f"Universe: {i+1}/{len(uni_tickers)} ({(i+1)/len(uni_tickers)*100:.0f}%) [{elapsed:.0f}s, ~{eta:.0f}s left]"
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
        progress_fn("examples", 10, f"Computing {len(raw_examples)} examples...")

    example_matrix = np.full((len(raw_examples), len(expressions)), np.nan)
    example_tickers = []
    example_ids = []

    for i, ex in enumerate(raw_examples):
        ticker = ex.get("ticker", "?")
        entry_date = ex.get("entryDate") or ex.get("chartDate")
        ex_id = str(ex.get("id", ""))
        try:
            r2 = requests.get(f"{API_BASE}/api/ohlcv/local/{setup_type}/{ex.get('id')}", timeout=15)
            if r2.status_code != 200:
                continue
            candles = r2.json().get("candles", [])
            if not candles:
                continue
            df = pd.DataFrame(candles)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            target_idx = len(df) - 1
            if entry_date:
                matches = df[df["date"].dt.strftime("%Y-%m-%d") == entry_date]
                if len(matches) > 0:
                    target_idx = matches.index[0]

            example_matrix[i] = _compute_ticker_values(df, target_idx, expressions)
            n_valid = np.sum(~np.isnan(example_matrix[i]))
            print(f"    ✓ {ticker:8s} ({entry_date}) — {n_valid}/{len(expressions)}")
        except Exception as e:
            print(f"    ✗ {ticker}: {e}")

        example_tickers.append(f"{ticker}_{ex_id}")
        example_ids.append(ex_id)

        if progress_fn:
            pct = 10 + int(90 * (i + 1) / len(raw_examples))
            progress_fn("examples", pct, f"Examples: {i+1}/{len(raw_examples)} ({ticker})")

    data = {
        "example_matrix": example_matrix,
        "example_tickers": example_tickers,
        "example_ids": example_ids,
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
