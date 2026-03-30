"""
Vectorized Expression Cache Builder — Batched numpy 2D approach.

Replaces the per-ticker pandas computation with batched 2D numpy operations.
Produces identical .npz files (dates + data arrays) and manifest.

Usage:
    python local_runner/vectorized_cache_builder.py --build [--batch-size 25] [--workers 8]

Requires: universe_ohlcv_daily.pkl in local_runner/cache/
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
CACHE_DAILY_FILE = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
CACHE_LEGACY_5YR = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")

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
    """Main entry point: build full expression cache using vectorized 2D approach.
    
    Strategy: compute intermediates for ALL tickers at once (~20GB), then
    for each output batch, compute expressions using pre-computed intermediates
    (zero-cost slicing). This eliminates per-batch intermediate recomputation.
    """
    from vectorized_dispatch import (build_intermediates, compute_expr_2d,
        _safe_div, _shift, _eval_bool_condition_2d, _compute_on_series_2d,
        _get_ext_series_2d, _rolling_rank_2d)
    from vectorized_indicators import count_true_2d, since_true_2d, true_in_row_2d
    
    print("\n" + "=" * 70)
    print("  VECTORIZED EXPRESSION CACHE BUILDER")
    print("=" * 70)
    
    # Load expressions
    expressions = _load_expressions()
    groups = _classify_expressions(expressions)
    n_exprs = len(expressions)
    n_daily = len(groups['daily'])
    n_ext = len(groups['ext_struct'])
    n_bool = sum(1 for e in expressions if e['compute'].get('op') in ('count_true','since_true','true_in_row'))
    w_indices, w_base_computes = groups["htf_weekly"]
    m_indices, m_base_computes = groups["htf_monthly"]
    print(f"\n  {n_exprs} expressions")
    print(f"    Daily: {n_daily}, ExtStruct: {n_ext}, Boolean: {n_bool}")
    print(f"    HTF: {len(w_indices)} weekly + {len(m_indices)} monthly")
    print(f"    Per-ticker: {len(groups['lsp'])} LSP + {len(groups['algo'])} algo")
    
    # Load OHLCV
    print(f"\n  Loading OHLCV cache...")
    _cache_file = CACHE_DAILY_FILE if os.path.exists(CACHE_DAILY_FILE) else CACHE_LEGACY_5YR
    with open(_cache_file, "rb") as f:
        cache = pickle.load(f)
    
    valid = {t: df for t, df in cache.items() if df is not None and len(df) >= 50}
    del cache
    tickers = sorted(valid.keys())
    bar_counts = {t: len(df) for t, df in valid.items()}
    max_bars = max(bar_counts.values())
    print(f"  {len(tickers)} tickers, max bars: {max_bars}")
    
    # Build global 2D arrays for ALL tickers
    print(f"\n  Building global OHLCV matrices ({len(tickers)} × {max_bars})...")
    t0 = time.time()
    n_all = len(tickers)
    O = np.full((n_all, max_bars), np.nan)
    H = np.full((n_all, max_bars), np.nan)
    L = np.full((n_all, max_bars), np.nan)
    C = np.full((n_all, max_bars), np.nan)
    V = np.full((n_all, max_bars), np.nan)
    offsets = []
    ticker_dates = []  # per-ticker date arrays for .npz output
    
    for i, ticker in enumerate(tickers):
        df = valid[ticker]
        n = len(df)
        offset = max_bars - n
        offsets.append(offset)
        O[i, offset:] = df["open"].values
        H[i, offset:] = df["high"].values
        L[i, offset:] = df["low"].values
        C[i, offset:] = df["close"].values
        V[i, offset:] = df["volume"].values
        ticker_dates.append(pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values)
    
    del valid  # free ~2GB
    print(f"  OHLCV matrices built in {time.time()-t0:.0f}s")
    
    # Build intermediates for ALL tickers at once
    print(f"  Computing intermediates for all {n_all} tickers...")
    t_im = time.time()
    im = build_intermediates(O, H, L, C, V)
    im["_close"] = C
    print(f"  Intermediates computed in {time.time()-t_im:.0f}s ({len(im)} arrays)")
    
    # Reference dates for HTF resampling
    ref_ticker_idx = offsets.index(0) if 0 in offsets else np.argmin(offsets)
    ref_dates = pd.to_datetime(
        pd.date_range('2020-01-01', periods=max_bars, freq='B')  # placeholder
    ).values
    # Use actual dates from a ticker with max bars
    for i, ticker in enumerate(tickers):
        if bar_counts[ticker] == max_bars:
            df_ref = pd.DataFrame({"date": ticker_dates[i]})
            ref_dates = pd.to_datetime(df_ref["date"]).values
            break
    
    # Resample to weekly/monthly and build HTF intermediates
    print(f"  Computing HTF intermediates...")
    t_htf = time.time()
    
    Ow, Hw, Lw, Cw, Vw, w_dates = _resample_2d(O, H, L, C, V, ref_dates, "W")
    im_w = build_intermediates(Ow, Hw, Lw, Cw, Vw)
    im_w["_close"] = Cw
    htf_w_map = _build_htf_map(ref_dates, w_dates)
    
    Om, Hm, Lm, Cm, Vm, m_dates = _resample_2d(O, H, L, C, V, ref_dates, "ME")
    im_m = build_intermediates(Om, Hm, Lm, Cm, Vm)
    im_m["_close"] = Cm
    htf_m_map = _build_htf_map(ref_dates, m_dates)
    
    print(f"  HTF intermediates computed in {time.time()-t_htf:.0f}s "
          f"(weekly: {Cw.shape[1]} bars, monthly: {Cm.shape[1]} bars)")
    
    # Create output directory
    os.makedirs(EXPR_CACHE_DIR, exist_ok=True)
    
    # ══════════════════════════════════════════════════════════
    # EXPRESSION COMPUTATION — process in output batches
    # ══════════════════════════════════════════════════════════
    
    n_batches = (n_all + batch_size - 1) // batch_size
    print(f"\n  Computing expressions, writing in {n_batches} batches of {batch_size}...")
    t_expr = time.time()
    ticker_info = {}
    total_completed = 0
    
    for batch_idx in range(n_batches):
        bs = batch_idx * batch_size
        be = min(bs + batch_size, n_all)
        bt = slice(bs, be)
        n_t = be - bs
        
        # Allocate output for this batch
        batch_output = np.full((n_t, max_bars, n_exprs), np.nan, dtype=np.float32)
        
        # Slice intermediates for this batch (numpy views — zero copy)
        im_b = {k: v[bt] for k, v in im.items()}
        Ob, Hb, Lb, Cb, Vb = O[bt], H[bt], L[bt], C[bt], V[bt]
        
        # ── Daily expressions ──
        for j in groups["daily"]:
            try:
                r = compute_expr_2d(expressions[j]["compute"], im_b, Ob, Hb, Lb, Cb, Vb)
                batch_output[:, :, j] = r.astype(np.float32)
            except Exception:
                pass
        
        # ── Extension structure ──
        for j in groups["ext_struct"]:
            try:
                r = compute_expr_2d(expressions[j]["compute"], im_b, Ob, Hb, Lb, Cb, Vb)
                batch_output[:, :, j] = r.astype(np.float32)
            except Exception:
                pass
        
        # ── Boolean aggregates (cached conditions) ──
        cond_cache = {}
        running_cache = {}
        
        bool_exprs = [(j, expressions[j]["compute"]) for j in range(n_exprs)
                      if expressions[j]["compute"].get("op") in ("count_true", "since_true", "true_in_row")]
        
        # Pre-compute all conditions and running counters for this batch
        for j, comp in bool_exprs:
            cn = comp["condition"]
            if cn not in cond_cache:
                ba = _eval_bool_condition_2d(cn, im_b, Ob, Hb, Lb, Cb, Vb)
                cond_cache[cn] = ba
                # Running counter for since_true
                run = np.full((n_t, max_bars), np.nan)
                run[:, 0] = np.where(ba[:, 0], 0.0, np.nan)
                for jj in range(1, max_bars):
                    is_true = ba[:, jj]
                    run[:, jj] = np.where(is_true, 0.0, run[:, jj-1] + 1)
                    still_nan = np.isnan(run[:, jj-1]) & ~is_true
                    run[still_nan, jj] = np.nan
                running_cache[cn] = run
        
        for j, comp in bool_exprs:
            cn = comp["condition"]
            p = comp["period"]
            op = comp["op"]
            if op == "count_true":
                batch_output[:, :, j] = count_true_2d(cond_cache[cn], p).astype(np.float32)
            elif op == "since_true":
                run = running_cache[cn]
                result = np.full((n_t, max_bars), np.nan)
                result[:, p-1:] = np.where(
                    np.isnan(run[:, p-1:]) | (run[:, p-1:] >= p), -1.0, run[:, p-1:])
                batch_output[:, :, j] = result.astype(np.float32)
            elif op == "true_in_row":
                batch_output[:, :, j] = true_in_row_2d(cond_cache[cn], p).astype(np.float32)
        
        del cond_cache, running_cache
        
        # ── HTF weekly ──
        im_wb = {k: v[bt] for k, v in im_w.items()}
        Owb, Hwb, Lwb, Cwb, Vwb = Ow[bt], Hw[bt], Lw[bt], Cw[bt], Vw[bt]
        for k, j in enumerate(w_indices):
            try:
                wr = compute_expr_2d(w_base_computes[k], im_wb, Owb, Hwb, Lwb, Cwb, Vwb)
                batch_output[:, :, j] = wr[:, htf_w_map].astype(np.float32)
            except Exception:
                pass
        
        # ── HTF monthly ──
        im_mb = {k: v[bt] for k, v in im_m.items()}
        Omb, Hmb, Lmb, Cmb, Vmb = Om[bt], Hm[bt], Lm[bt], Cm[bt], Vm[bt]
        for k, j in enumerate(m_indices):
            try:
                mr = compute_expr_2d(m_base_computes[k], im_mb, Omb, Hmb, Lmb, Cmb, Vmb)
                batch_output[:, :, j] = mr[:, htf_m_map].astype(np.float32)
            except Exception:
                pass
        
        # ── Write .npz files ──
        for i in range(n_t):
            ti = bs + i
            ticker = tickers[ti]
            n = bar_counts[ticker]
            offset = offsets[ti]
            data = batch_output[i, offset:offset + n, :]
            np.savez_compressed(
                _ticker_cache_path(ticker),
                data=data,
                dates=ticker_dates[ti],
            )
            ticker_info[ticker] = {
                "n_bars": int(n),
                "last_date": str(ticker_dates[ti][-1]),
            }
        
        total_completed += n_t
        del batch_output
        
        elapsed = time.time() - t_expr
        rate = total_completed / elapsed if elapsed > 0 else 0
        eta = (n_all - total_completed) / rate if rate > 0 else 0
        pct = total_completed / n_all * 100
        filled = int(30 * total_completed / n_all)
        bar = "█" * filled + "░" * (30 - filled)
        print(f"\r    [{bar}] {pct:5.1f}%  {total_completed:,}/{n_all:,}  "
              f"{elapsed:.0f}s elapsed  ETA {eta:.0f}s", end="", flush=True)
        if (batch_idx + 1) % 10 == 0 or batch_idx == n_batches - 1:
            print()
    
    expr_time = time.time() - t_expr
    print(f"\n  Expression phase: {expr_time:.0f}s ({expr_time/60:.1f} min)")
    
    # Free large arrays
    del O, H, L, C, V, im, Ow, Hw, Lw, Cw, Vw, im_w, Om, Hm, Lm, Cm, Vm, im_m
    import gc; gc.collect()
    
    # ══════════════════════════════════════════════════════════
    # PER-TICKER PASS: LSP + ALGO LINES
    # ══════════════════════════════════════════════════════════
    
    lsp_indices = groups["lsp"]
    algo_indices = groups["algo"]
    
    if lsp_indices or algo_indices:
        print(f"\n  Per-ticker pass: {len(lsp_indices)} LSP + {len(algo_indices)} algo")
        print(f"  Workers: {n_lsp_workers}")
        
        # Reload OHLCV for per-ticker pass (needed for LSP/algo)
        _cache_file = CACHE_DAILY_FILE if os.path.exists(CACHE_DAILY_FILE) else CACHE_LEGACY_5YR
        with open(_cache_file, "rb") as f:
            cache = pickle.load(f)
        
        work_items = []
        for ticker in tickers:
            df = cache.get(ticker)
            if df is None or len(df) < 50:
                continue
            df_dict = {
                "date": df["date"].values,
                "open": df["open"].values,
                "high": df["high"].values,
                "low": df["low"].values,
                "close": df["close"].values,
                "volume": df["volume"].values,
            }
            work_items.append((ticker, df_dict, lsp_indices, algo_indices, expressions))
        
        del cache
        
        t_lsp = time.time()
        lsp_completed = 0
        
        with ProcessPoolExecutor(max_workers=n_lsp_workers) as pool:
            futures = {pool.submit(_compute_lsp_algo_ticker, item): item[0] for item in work_items}
            for future in as_completed(futures):
                ticker_name, results = future.result()
                if results:
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
                    pct = lsp_completed / len(work_items) * 100
                    print(f"    LSP/Algo: {lsp_completed:,}/{len(work_items):,} ({pct:.0f}%) [{elapsed:.0f}s]")
        
        lsp_time = time.time() - t_lsp
        print(f"  Per-ticker pass: {lsp_time:.0f}s ({lsp_time/60:.1f} min)")
    
    # ══════════════════════════════════════════════════════════
    # MANIFEST
    # ══════════════════════════════════════════════════════════
    
    total_time = time.time() - t0
    
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
