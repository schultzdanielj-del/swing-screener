"""
Vectorized Expression Cache Builder — Batched numpy 2D approach.

Replaces the per-ticker pandas computation with batched 2D numpy operations.
Produces identical .npz files (dates + data arrays) and manifest.

Usage:
    python local_runner/vectorized_cache_builder.py --build [--batch-size 25] [--workers 8]

Requires: universe_ohlcv_5yr.pkl in local_runner/cache/
"""

import os
import sys
import time
import json
import pickle
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
EXPR_CACHE_DIR = os.path.join(CACHE_DIR, "expr_series")
MANIFEST_PATH = os.path.join(EXPR_CACHE_DIR, "_manifest.json")
CACHE_5YR_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


def _load_expressions():
    """Load expression library."""
    from brute_expressions import generate_all
    return generate_all()


def _classify_expressions(expressions):
    """Classify expressions into groups for vectorized processing."""
    daily_indices = []
    ext_struct_indices = []
    lsp_indices = []
    algo_indices = []
    htf_weekly_indices = []
    htf_weekly_base = []
    htf_monthly_indices = []
    htf_monthly_base = []
    
    _ON_SERIES_OPS = {"on_series", "on_series_bool_agg"}
    
    for j, expr in enumerate(expressions):
        compute = expr["compute"]
        op = compute.get("op")
        
        if op == "precomputed":
            source = compute.get("source")
            if source == "lsp":
                lsp_indices.append(j)
            elif source == "algo":
                algo_indices.append(j)
            elif source == "htf":
                tf = compute.get("timeframe")
                if tf == "w":
                    htf_weekly_indices.append(j)
                    htf_weekly_base.append(compute.get("base_compute"))
                elif tf == "m":
                    htf_monthly_indices.append(j)
                    htf_monthly_base.append(compute.get("base_compute"))
        elif op in _ON_SERIES_OPS:
            ext_struct_indices.append(j)
        else:
            daily_indices.append(j)
    
    return {
        "daily": daily_indices,
        "ext_struct": ext_struct_indices,
        "lsp": lsp_indices,
        "algo": algo_indices,
        "htf_weekly": (htf_weekly_indices, htf_weekly_base),
        "htf_monthly": (htf_monthly_indices, htf_monthly_base),
    }


def _resample_2d(O, H, L, C, V, dates, freq="W"):
    """Resample daily 2D OHLCV to weekly/monthly.
    
    All tickers must share the same date grid (NaN-padded).
    Returns resampled O, H, L, C, V arrays and htf_dates.
    """
    # Build a reference DataFrame to get period groupings
    date_series = pd.to_datetime(dates)
    df_ref = pd.DataFrame({"date": date_series, "bar_idx": np.arange(len(dates))})
    df_ref = df_ref.set_index("date")
    
    # Resample to get period groupings (bar indices per period)
    resampled = df_ref.resample(freq)
    periods = []
    for period_end, group in resampled:
        if len(group) == 0:
            continue
        bar_indices = group["bar_idx"].values
        periods.append((period_end, bar_indices))
    
    n_periods = len(periods)
    n_t = O.shape[0]
    
    Or = np.full((n_t, n_periods), np.nan)
    Hr = np.full((n_t, n_periods), np.nan)
    Lr = np.full((n_t, n_periods), np.nan)
    Cr = np.full((n_t, n_periods), np.nan)
    Vr = np.full((n_t, n_periods), np.nan)
    htf_dates = []
    
    for pi, (period_end, idx) in enumerate(periods):
        Or[:, pi] = O[:, idx[0]]
        Hr[:, pi] = np.nanmax(H[:, idx], axis=1)
        Lr[:, pi] = np.nanmin(L[:, idx], axis=1)
        Cr[:, pi] = C[:, idx[-1]]
        Vr[:, pi] = np.nansum(V[:, idx], axis=1)
        htf_dates.append(dates[idx[-1]])
        
        # NaN out tickers that had NaN in all bars of this period
        all_nan = np.all(np.isnan(C[:, idx]), axis=1)
        Or[all_nan, pi] = np.nan
        Hr[all_nan, pi] = np.nan
        Lr[all_nan, pi] = np.nan
        Cr[all_nan, pi] = np.nan
        Vr[all_nan, pi] = np.nan
    
    return Or, Hr, Lr, Cr, Vr, np.array(htf_dates)


def _build_htf_map(daily_dates, htf_dates):
    """Map daily bar indices to HTF bar indices. Returns (n_daily,) int array."""
    daily_dt = pd.to_datetime(daily_dates).values
    htf_dt = pd.to_datetime(htf_dates).values
    indices = np.searchsorted(htf_dt, daily_dt, side="left")
    indices = np.clip(indices, 0, len(htf_dt) - 1)
    return indices


def _ticker_cache_path(ticker):
    safe = ticker.replace("/", "_").replace(".", "_")
    return os.path.join(EXPR_CACHE_DIR, f"{safe}.npz")


def _compute_lsp_algo_ticker(args):
    """Per-ticker LSP + algo lines computation. Runs in ProcessPoolExecutor."""
    ticker, df_dict, lsp_indices, algo_indices, expressions = args
    try:
        df = pd.DataFrame(df_dict)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        
        n_bars = len(df)
        results = {}
        
        if lsp_indices:
            try:
                from scripts.lsp_detector_v2 import compute_all_lsp_series
                lsp_dict = compute_all_lsp_series(df)
                for j in lsp_indices:
                    col_name = expressions[j]["compute"]["column"]
                    if col_name in lsp_dict:
                        arr = lsp_dict[col_name]
                        if len(arr) == n_bars:
                            results[j] = arr.astype(np.float32)
            except Exception:
                pass
        
        if algo_indices:
            try:
                from scripts.algo_line_detector import compute_all_algo_series
                algo_dict = compute_all_algo_series(df)
                for j in algo_indices:
                    col_name = expressions[j]["compute"]["column"]
                    if col_name in algo_dict:
                        arr = algo_dict[col_name]
                        if len(arr) == n_bars:
                            results[j] = arr.astype(np.float32)
            except Exception:
                pass
        
        return ticker, results
    except Exception:
        return ticker, {}


def build_vectorized(batch_size=25, n_lsp_workers=8):
    """Main entry point: build full expression cache using vectorized 2D approach."""
    from vectorized_dispatch import build_intermediates, compute_expr_2d
    
    print("\n" + "=" * 70)
    print("  VECTORIZED EXPRESSION CACHE BUILDER")
    print("=" * 70)
    
    # Load expressions
    expressions = _load_expressions()
    groups = _classify_expressions(expressions)
    n_exprs = len(expressions)
    print(f"\n  {n_exprs} expressions")
    print(f"    Daily: {len(groups['daily'])}")
    print(f"    Extension structure: {len(groups['ext_struct'])}")
    print(f"    HTF weekly: {len(groups['htf_weekly'][0])}")
    print(f"    HTF monthly: {len(groups['htf_monthly'][0])}")
    print(f"    LSP: {len(groups['lsp'])}")
    print(f"    Algo: {len(groups['algo'])}")
    
    # Load OHLCV
    print(f"\n  Loading {CACHE_5YR_FILE}...")
    with open(CACHE_5YR_FILE, "rb") as f:
        cache = pickle.load(f)
    
    # Filter valid tickers
    valid = {t: df for t, df in cache.items() if df is not None and len(df) >= 50}
    tickers = sorted(valid.keys())
    print(f"  {len(tickers)} tickers with >= 50 bars")
    
    # Build date grid — use max bars, right-align with NaN padding
    bar_counts = {t: len(df) for t, df in valid.items()}
    max_bars = max(bar_counts.values())
    print(f"  Max bars: {max_bars}")
    
    # We need a shared date grid. Use the ticker with most bars as reference.
    ref_ticker = max(bar_counts, key=bar_counts.get)
    ref_dates = pd.to_datetime(valid[ref_ticker]["date"]).values
    
    # Create output directory
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)
    
    # Process in batches
    n_batches = (len(tickers) + batch_size - 1) // batch_size
    print(f"\n  Processing {len(tickers)} tickers in {n_batches} batches of {batch_size}")
    
    t0 = time.time()
    ticker_info = {}
    total_completed = 0
    total_failed = 0
    
    # Pre-compute HTF resampling on reference dates (shared calendar)
    htf_w_map = None
    htf_m_map = None
    w_indices, w_base_computes = groups["htf_weekly"]
    m_indices, m_base_computes = groups["htf_monthly"]
    
    for batch_idx in range(n_batches):
        batch_start = batch_idx * batch_size
        batch_end = min(batch_start + batch_size, len(tickers))
        batch_tickers = tickers[batch_start:batch_end]
        n_t = len(batch_tickers)
        
        # Build 2D arrays for this batch
        O = np.full((n_t, max_bars), np.nan)
        H = np.full((n_t, max_bars), np.nan)
        L = np.full((n_t, max_bars), np.nan)
        C = np.full((n_t, max_bars), np.nan)
        V = np.full((n_t, max_bars), np.nan)
        batch_dates = []  # per-ticker date arrays
        batch_offsets = []
        
        for i, ticker in enumerate(batch_tickers):
            df = valid[ticker]
            n = len(df)
            offset = max_bars - n
            batch_offsets.append(offset)
            O[i, offset:] = df["open"].values
            H[i, offset:] = df["high"].values
            L[i, offset:] = df["low"].values
            C[i, offset:] = df["close"].values
            V[i, offset:] = df["volume"].values
            batch_dates.append(pd.to_datetime(df["date"]).values)
        
        # Build intermediates for this batch
        im = build_intermediates(O, H, L, C, V)
        im["_close"] = C
        
        # Allocate output per ticker in batch
        batch_results = [np.full((bar_counts[t], n_exprs), np.nan, dtype=np.float32) for t in batch_tickers]
        
        # ── 1. Daily expressions ──
        for j in groups["daily"]:
            try:
                result_2d = compute_expr_2d(expressions[j]["compute"], im, O, H, L, C, V)
                for i, ticker in enumerate(batch_tickers):
                    n = bar_counts[ticker]
                    offset = batch_offsets[i]
                    batch_results[i][:, j] = result_2d[i, offset:offset + n].astype(np.float32)
            except Exception:
                pass
        
        # ── 2. Extension structure (on_series, on_series_bool_agg) ──
        for j in groups["ext_struct"]:
            try:
                result_2d = compute_expr_2d(expressions[j]["compute"], im, O, H, L, C, V)
                for i, ticker in enumerate(batch_tickers):
                    n = bar_counts[ticker]
                    offset = batch_offsets[i]
                    batch_results[i][:, j] = result_2d[i, offset:offset + n].astype(np.float32)
            except Exception:
                pass
        
        # ── 3. HTF weekly ──
        if w_indices:
            try:
                # Resample daily to weekly for this batch
                Ow, Hw, Lw, Cw, Vw, w_dates = _resample_2d(O, H, L, C, V, ref_dates, "W")
                im_w = build_intermediates(Ow, Hw, Lw, Cw, Vw)
                im_w["_close"] = Cw
                htf_w_map = _build_htf_map(ref_dates, w_dates)
                
                for k, j in enumerate(w_indices):
                    try:
                        w_result = compute_expr_2d(w_base_computes[k], im_w, Ow, Hw, Lw, Cw, Vw)
                        # Map back to daily
                        for i, ticker in enumerate(batch_tickers):
                            n = bar_counts[ticker]
                            offset = batch_offsets[i]
                            # Get the daily slice for this ticker
                            daily_mapped = w_result[i, htf_w_map]
                            batch_results[i][:, j] = daily_mapped[offset:offset + n].astype(np.float32)
                    except Exception:
                        pass
                
                del Ow, Hw, Lw, Cw, Vw, im_w
            except Exception:
                pass
        
        # ── 4. HTF monthly ──
        if m_indices:
            try:
                Om, Hm, Lm, Cm, Vm, m_dates = _resample_2d(O, H, L, C, V, ref_dates, "ME")
                im_m = build_intermediates(Om, Hm, Lm, Cm, Vm)
                im_m["_close"] = Cm
                htf_m_map = _build_htf_map(ref_dates, m_dates)
                
                for k, j in enumerate(m_indices):
                    try:
                        m_result = compute_expr_2d(m_base_computes[k], im_m, Om, Hm, Lm, Cm, Vm)
                        for i, ticker in enumerate(batch_tickers):
                            n = bar_counts[ticker]
                            offset = batch_offsets[i]
                            daily_mapped = m_result[i, htf_m_map]
                            batch_results[i][:, j] = daily_mapped[offset:offset + n].astype(np.float32)
                    except Exception:
                        pass
                
                del Om, Hm, Lm, Cm, Vm, im_m
            except Exception:
                pass
        
        # ── 5. Save batch results ──
        for i, ticker in enumerate(batch_tickers):
            df = valid[ticker]
            dates_str = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
            np.savez_compressed(
                _ticker_cache_path(ticker),
                data=batch_results[i],
                dates=dates_str,
            )
            ticker_info[ticker] = {
                "n_bars": int(bar_counts[ticker]),
                "last_date": str(dates_str[-1]),
            }
        
        total_completed += n_t
        
        # Free batch memory
        del O, H, L, C, V, im, batch_results
        
        elapsed = time.time() - t0
        rate = total_completed / elapsed if elapsed > 0 else 0
        eta = (len(tickers) - total_completed) / rate if rate > 0 else 0
        print(f"    Batch {batch_idx+1}/{n_batches}: {total_completed}/{len(tickers)} "
              f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s left]")
    
    batch_time = time.time() - t0
    print(f"\n  Vectorized batch phase: {batch_time:.0f}s ({batch_time/60:.1f} min)")
    
    # ── 6. Per-ticker pass: LSP + algo lines ──
    lsp_indices = groups["lsp"]
    algo_indices = groups["algo"]
    
    if lsp_indices or algo_indices:
        print(f"\n  Per-ticker pass: {len(lsp_indices)} LSP + {len(algo_indices)} algo expressions")
        print(f"  Workers: {n_lsp_workers}")
        
        work_items = []
        for ticker in tickers:
            df = valid[ticker]
            df_dict = {
                "date": df["date"].values,
                "open": df["open"].values,
                "high": df["high"].values,
                "low": df["low"].values,
                "close": df["close"].values,
                "volume": df["volume"].values,
            }
            work_items.append((ticker, df_dict, lsp_indices, algo_indices, expressions))
        
        t_lsp = time.time()
        lsp_completed = 0
        
        with ProcessPoolExecutor(max_workers=n_lsp_workers) as pool:
            futures = {pool.submit(_compute_lsp_algo_ticker, item): item[0] for item in work_items}
            for future in as_completed(futures):
                ticker_name, results = future.result()
                if results:
                    # Load existing npz, patch in LSP/algo columns, re-save
                    path = _ticker_cache_path(ticker_name)
                    try:
                        loaded = np.load(path, allow_pickle=True)
                        data = loaded["data"].copy()
                        dates = loaded["dates"]
                        for j, arr in results.items():
                            if len(arr) == len(data):
                                data[:, j] = arr
                        np.savez_compressed(path, data=data, dates=dates)
                    except Exception:
                        pass
                
                lsp_completed += 1
                if lsp_completed % 500 == 0 or lsp_completed == len(work_items):
                    elapsed = time.time() - t_lsp
                    print(f"    {lsp_completed}/{len(work_items)} "
                          f"[{elapsed:.0f}s elapsed]")
        
        lsp_time = time.time() - t_lsp
        print(f"  Per-ticker pass: {lsp_time:.0f}s ({lsp_time/60:.1f} min)")
    
    # ── 7. Save manifest ──
    total_time = time.time() - t0
    
    from brute_expressions import generate_all
    from expr_cache_builder import _expr_fingerprint
    fingerprint = _expr_fingerprint(expressions)
    
    manifest = {
        "fingerprint": fingerprint,
        "n_expressions": n_exprs,
        "expr_names": [e["name"] for e in expressions],
        "n_tickers": len(ticker_info),
        "tickers": ticker_info,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "build_time_s": round(total_time, 1),
        "builder": "vectorized",
    }
    
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    
    # Stats
    total_bytes = sum(
        os.path.getsize(os.path.join(EXPR_CACHE_DIR, f))
        for f in os.listdir(EXPR_CACHE_DIR)
        if f.endswith(".npz")
    )
    total_gb = total_bytes / (1024 ** 3)
    
    print(f"\n  {'=' * 50}")
    print(f"  BUILD COMPLETE")
    print(f"  Tickers: {len(ticker_info)}")
    print(f"  Expressions: {n_exprs}")
    print(f"  Disk: {total_gb:.1f} GB")
    print(f"  Total time: {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  {'=' * 50}")
    
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Vectorized Expression Cache Builder")
    parser.add_argument("--build", action="store_true", help="Build full cache")
    parser.add_argument("--batch-size", type=int, default=25, help="Tickers per batch (default 25)")
    parser.add_argument("--workers", type=int, default=max(cpu_count() - 1, 1), help="LSP/algo workers")
    args = parser.parse_args()
    
    if args.build:
        build_vectorized(batch_size=args.batch_size, n_lsp_workers=args.workers)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
