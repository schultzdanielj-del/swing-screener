"""Two-stage universe scan per PRESIGNAL_GRINDER.md §13.4.

Stage 1: ADR-adjusted OHLC (atr_close/high/low) x N offsets bounding box
  with LOO-deviation budget calibrated on the labeled examples.
  ADR formulas from research/lead_up_investigation.py:
    atr_close[k] = (close[E-k] - close[E]) / ATR_E
    atr_high[k]  = (high[E-k]  - close[E]) / ATR_E
    atr_low[k]   = (low[E-k]   - close[E]) / ATR_E
    ATR_E = mean of TR over last ATR_PERIOD bars ending at E.
  Budget: for each example i, held out against the box of remaining examples,
  count bars at offsets 1..N that fall OUTSIDE the LOO box at any of the 3
  axes. budget_setup = max_i of those counts. Candidates pass Stage 1 iff
  their outside-count vs the all-examples OHLC box is <= budget.

Stage 2: narrower-basis cache filter, option (b) tighter-than-market.
  Uses relevant-feature envelope from research/n_derivation_cache/{setup}_envelope.npz
  (ex_min, ex_max over labeled examples) and market-wide per-feature range
  from research/rand_range/rand_range.npz.
  Band (offset k, feature c) survives iff ex_range[k,c] < rand_range[fidx[c]].
  Non-surviving bands treated as pass-through (NaN-as-pass).
  For Stage 1 survivors, strict AND: any surviving-band value out of
  [ex_min, ex_max] rejects the candidate.

Output:
  research/universe_scan/two_stage_{setup}_candidates.csv  - (ticker, date, bar_idx)
  research/universe_scan/two_stage_summary.txt             - per-setup summary

Window semantics per §13: candidate bar E, window [E-N, E-1] inclusive,
entry bar E itself excluded. Offsets k=1..N map to bar E-k. Envelope
row 0 = E-1 (k=1), row N-1 = E-N (k=N).
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys
import time
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
ENV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
RAND_RANGE_FILE = os.path.join(WORKTREE, "research", "rand_range", "rand_range.npz")
OUT_DIR = os.path.join(WORKTREE, "research", "universe_scan")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
ATR_PERIOD = 14

# Full universe. Set to >0 to subset (iteration).
SAMPLE_N_TICKERS = 0
SAMPLE_SEED = 42

# Parallel workers. Each loads a ~1.5GB OHLCV dict copy at init. OMP=1 per
# worker to avoid BLAS thread contention.
N_WORKERS = 6

# Diagnostic: skip Stage 2 to isolate Stage 1 carve power.
STAGE2_ENABLED = False

warnings.filterwarnings("ignore", category=RuntimeWarning)

# Worker-process globals, populated by _worker_init on spawn.
_W_UNIVERSE = None
_W_EC = None
_W_PAYLOADS = None


def dates_as_str_arr(dates):
    if pd.api.types.is_datetime64_any_dtype(dates):
        return pd.to_datetime(dates).strftime('%Y-%m-%d').values.astype('<U10')
    return np.array([str(d)[:10] for d in dates], dtype='<U10')


def dates_as_str_df(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values.astype('<U10')
    return np.array([str(d)[:10] for d in df["date"].values], dtype='<U10')


def compute_atr_series(high, low, close, period=ATR_PERIOD):
    """Simple mean true-range series. atr[i] = mean TR over last `period`
    bars ending at i inclusive. atr[i] = NaN for i < period.
    """
    n = len(close)
    tr = np.full(n, np.nan, dtype=np.float64)
    # TR[i] = max(high[i]-low[i], |high[i]-close[i-1]|, |low[i]-close[i-1]|)
    # tr[0] undefined (no prior close); set NaN.
    tr[1:] = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
    )
    atr = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return atr
    # Mean of TR over window [i-period+1, i]. Need tr to be valid across that
    # window (tr[0] is NaN so first usable atr index is `period`).
    # Use simple rolling sum for speed.
    valid_tr = np.where(np.isfinite(tr), tr, 0.0)
    valid_mask = np.isfinite(tr).astype(np.int32)
    csum = np.concatenate(([0.0], np.cumsum(valid_tr)))
    cmask = np.concatenate(([0], np.cumsum(valid_mask)))
    for i in range(period, n):
        s = csum[i + 1] - csum[i + 1 - period]
        m = cmask[i + 1] - cmask[i + 1 - period]
        if m == period:
            atr[i] = s / period
    return atr


def load_examples_ohlc_window(setup, universe, N):
    """For each labeled example, extract (N, 3) atr_close/high/low array
    covering offsets k=1..N (bar E-k for k=1..N). Returns (n_ex, N, 3)
    array of valid examples and a list of (ticker, entry_date_str) pairs.

    Row 0 = offset k=1 (bar E-1), row N-1 = offset k=N (bar E-N). Matches
    envelope row ordering.
    """
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()

    out_windows = []
    out_pairs = []
    for ticker, entry_date in rows:
        entry_date_str = str(entry_date)[:10]
        df = universe.get(ticker)
        if df is None:
            continue
        ds = dates_as_str_df(df)
        m = np.where(ds == entry_date_str)[0]
        if len(m) == 0:
            continue
        E_idx = int(m[0])
        # Need N bars before E (positions E-N..E-1) and ATR_PERIOD of history
        # before E to compute ATR_E.
        if E_idx < N or E_idx < ATR_PERIOD:
            continue
        close = df["close"].values.astype(np.float64)
        high = df["high"].values.astype(np.float64)
        low = df["low"].values.astype(np.float64)
        close_E = close[E_idx]
        if not np.isfinite(close_E) or close_E <= 0:
            continue
        atr_series = compute_atr_series(high, low, close, ATR_PERIOD)
        atr_E = atr_series[E_idx]
        if not np.isfinite(atr_E) or atr_E <= 0:
            continue
        # offsets k=1..N -> bar E-k
        ks = np.arange(1, N + 1)
        idx_k = E_idx - ks  # positions in close array
        c_k = close[idx_k]
        h_k = high[idx_k]
        l_k = low[idx_k]
        if not (np.all(np.isfinite(c_k)) and np.all(np.isfinite(h_k)) and
                np.all(np.isfinite(l_k))):
            continue
        w = np.empty((N, 3), dtype=np.float32)
        w[:, 0] = (c_k - close_E) / atr_E
        w[:, 1] = (h_k - close_E) / atr_E
        w[:, 2] = (l_k - close_E) / atr_E
        out_windows.append(w)
        out_pairs.append((ticker, entry_date_str))

    if not out_windows:
        return np.zeros((0, N, 3), dtype=np.float32), []
    return np.stack(out_windows, axis=0), out_pairs


def compute_stage1_budget(ex_windows):
    """LOO-deviation budget.

    For each example i, build bounding box over examples[j != i], then count
    bars at offsets 1..N of example i that fall outside that LOO box at any
    of the 3 axes. budget = max over i of those counts.

    ex_windows shape: (n_ex, N, 3)
    """
    n_ex, N, axes = ex_windows.shape
    if n_ex < 2:
        return 0
    counts = np.zeros(n_ex, dtype=np.int32)
    for i in range(n_ex):
        mask = np.ones(n_ex, dtype=bool)
        mask[i] = False
        others = ex_windows[mask]  # (n_ex-1, N, 3)
        loo_min = np.nanmin(others, axis=0)  # (N, 3)
        loo_max = np.nanmax(others, axis=0)
        held = ex_windows[i]  # (N, 3)
        outside_per_cell = (held < loo_min) | (held > loo_max)  # (N, 3)
        outside_per_bar = outside_per_cell.any(axis=1)  # (N,)
        counts[i] = int(outside_per_bar.sum())
    return int(counts.max())


def build_stage2_mask(env, rand_range_vec):
    """Surviving bands per option (b): ex_range[k,c] < rand_range[fidx[c]].

    Returns (N, n_rel) bool: True for surviving cells.
    Bands with under-coverage (ex_min or ex_max NaN) are treated as
    non-surviving (pass-through).
    """
    ex_min = env["ex_min"]  # (N, n_rel)
    ex_max = env["ex_max"]
    fidx = env["feature_indices"]
    ex_range = ex_max - ex_min  # (N, n_rel), NaN where either is NaN
    market_range = rand_range_vec[fidx]  # (n_rel,)
    # Where market_range is <=0 or NaN, we can't compute: treat as non-surviving.
    mr = market_range[None, :]  # (1, n_rel)
    survives = np.isfinite(ex_range) & np.isfinite(mr) & (mr > 0) & (ex_range < mr)
    return survives


def build_stage2_surv_cols(survives):
    """Precompute per-offset integer arrays of surviving column indices.

    Used for one-shot fancy indexing in Stage 2 scan, avoiding the double-
    allocation of `sub[rows][:, bool_mask]`.
    """
    N = survives.shape[0]
    return [np.where(survives[k, :])[0].astype(np.int64) for k in range(N)]


def scan_ticker(ticker, ec, universe, setup_payloads):
    """For one ticker, evaluate every candidate bar E >= N_max across all
    setups. Returns dict {setup: list of (date_str, bar_idx)}.
    """
    dates, cache_data = ec.get_ticker(ticker)
    if dates is None:
        return {s: [] for s in SETUP_ORDER}
    df = universe.get(ticker)
    if df is None:
        return {s: [] for s in SETUP_ORDER}
    ds_cache = dates_as_str_arr(dates)
    ds_ohlcv = dates_as_str_df(df)
    # Align OHLCV to expr cache dates. expr cache dates should be a subset or
    # aligned with OHLCV. Build index mapping from cache row -> OHLCV row.
    # Simplest: intersect by date. We operate on cache rows and look up OHLCV
    # values by date-string equality.
    ohlcv_pos = {d: i for i, d in enumerate(ds_ohlcv)}
    # For every cache row, find OHLCV row (or -1 if missing).
    ohlcv_idx = np.array([ohlcv_pos.get(d, -1) for d in ds_cache], dtype=np.int32)
    valid_ohlcv = ohlcv_idx >= 0
    if not valid_ohlcv.any():
        return {s: [] for s in SETUP_ORDER}
    # Reindex OHLC arrays to cache-row alignment. -1 positions get NaN.
    close_full = df["close"].values.astype(np.float64)
    high_full = df["high"].values.astype(np.float64)
    low_full = df["low"].values.astype(np.float64)
    n_cache = len(ds_cache)
    close_al = np.full(n_cache, np.nan, dtype=np.float64)
    high_al = np.full(n_cache, np.nan, dtype=np.float64)
    low_al = np.full(n_cache, np.nan, dtype=np.float64)
    valid_idx = np.where(valid_ohlcv)[0]
    close_al[valid_idx] = close_full[ohlcv_idx[valid_idx]]
    high_al[valid_idx] = high_full[ohlcv_idx[valid_idx]]
    low_al[valid_idx] = low_full[ohlcv_idx[valid_idx]]
    # ATR must be computed on consecutive OHLCV trading days (TR uses prior
    # close). Compute over the full OHLCV series, then reindex to cache rows.
    atr_full = compute_atr_series(high_full, low_full, close_full, ATR_PERIOD)
    atr_al = np.full(n_cache, np.nan, dtype=np.float64)
    atr_al[valid_idx] = atr_full[ohlcv_idx[valid_idx]]

    results = {}
    n_bars = cache_data.shape[0]

    for setup, payload in setup_payloads.items():
        N = payload["N"]
        env = payload["env"]
        survives = payload["survives"]   # (N, n_rel) bool
        ex_min = env["ex_min"]           # (N, n_rel)
        ex_max = env["ex_max"]
        fidx = env["feature_indices"]
        ohlc_min = payload["ohlc_min"]   # (N, 3)
        ohlc_max = payload["ohlc_max"]
        budget = payload["budget"]

        if n_bars <= N:
            results[setup] = []
            continue

        # Candidates: E indices in [N, n_bars-1]. Need atr_al[E] and close_al[E]
        # finite (to normalize), and close_al[E-k]/high/low finite for k=1..N.
        # We'll build (n_cand, N) outside-count for Stage 1 vectorized per offset.
        n_cand = n_bars - N
        E_idx = np.arange(N, n_bars, dtype=np.int32)  # (n_cand,)

        close_E = close_al[E_idx]
        atr_E = atr_al[E_idx]
        anchor_valid = np.isfinite(close_E) & np.isfinite(atr_E) & (atr_E > 0)

        stage1_outside = np.zeros(n_cand, dtype=np.int32)
        stage1_invalid = ~anchor_valid.copy()  # mark invalid candidates
        # Early reject: once outside count exceeds budget, candidate is dead.
        alive = anchor_valid.copy()

        for k in range(1, N + 1):
            if not alive.any():
                break
            idx_k = E_idx - k  # positions
            c_k = close_al[idx_k]
            h_k = high_al[idx_k]
            l_k = low_al[idx_k]
            # Normalized axes
            atr_c = (c_k - close_E) / atr_E
            atr_h = (h_k - close_E) / atr_E
            atr_l = (l_k - close_E) / atr_E
            # Envelope row for this k (k=1 -> row 0)
            row = k - 1
            c_min, c_max = ohlc_min[row, 0], ohlc_max[row, 0]
            h_min, h_max = ohlc_min[row, 1], ohlc_max[row, 1]
            l_min, l_max = ohlc_min[row, 2], ohlc_max[row, 2]
            outside_c = (atr_c < c_min) | (atr_c > c_max)
            outside_h = (atr_h < h_min) | (atr_h > h_max)
            outside_l = (atr_l < l_min) | (atr_l > l_max)
            outside = outside_c | outside_h | outside_l
            stage1_outside += outside.astype(np.int32)
            # Flag invalid if any of the bar's OHLC values missing.
            bar_valid = np.isfinite(c_k) & np.isfinite(h_k) & np.isfinite(l_k)
            stage1_invalid |= ~bar_valid
            # Kill candidates that already exceed budget.
            alive &= (stage1_outside <= budget) & bar_valid

        stage1_pass = alive & (stage1_outside <= budget) & ~stage1_invalid

        if not stage1_pass.any():
            results[setup] = []
            continue

        # Stage 2: strict AND over surviving bands.
        surv_cand_idx = np.where(stage1_pass)[0]  # indices into 0..n_cand-1
        if not STAGE2_ENABLED:
            # Stage 1 only: emit survivors directly.
            if len(surv_cand_idx) == 0:
                results[setup] = []
                continue
            hit_E_idx = surv_cand_idx + N
            hit_dates = ds_cache[hit_E_idx]
            results[setup] = list(zip(hit_dates.tolist(), hit_E_idx.tolist()))
            continue
        sub = cache_data[:, fidx].astype(np.float32, copy=False)  # (n_bars, n_rel)
        passes = np.ones(len(surv_cand_idx), dtype=bool)
        surv_cols = payload["surv_cols"]  # list of int arrays, one per offset

        for k in range(1, N + 1):
            if not passes.any():
                break
            row = k - 1
            cols_k = surv_cols[row]
            if cols_k.size == 0:
                continue
            pos_k = (N + surv_cand_idx) - k  # = E_idx[surv_cand_idx] - k
            # One-shot fancy-indexing: single allocation of (n_surv_cand, n_cols_k).
            vals = sub[pos_k[:, None], cols_k[None, :]]
            emin = ex_min[row, cols_k][None, :]
            emax = ex_max[row, cols_k][None, :]
            fail_any = np.any((vals < emin) | (vals > emax), axis=1)
            passes &= ~fail_any

        hit_local_idx = surv_cand_idx[passes]
        if len(hit_local_idx) == 0:
            results[setup] = []
            continue
        hit_E_idx = hit_local_idx + N
        hit_dates = ds_cache[hit_E_idx]
        results[setup] = list(zip(hit_dates.tolist(), hit_E_idx.tolist()))

    del cache_data, dates
    return results


def _worker_init(ohlcv_path, payloads):
    """Each worker: load OHLCV dict, open expr cache, stash payloads.
    Runs ONCE per worker on process spawn.
    """
    import os as _os
    import pickle as _pickle
    import sys as _sys
    # Pin numpy/BLAS to single thread per worker to avoid contention.
    _os.environ["OMP_NUM_THREADS"] = "1"
    _os.environ["OPENBLAS_NUM_THREADS"] = "1"
    _os.environ["MKL_NUM_THREADS"] = "1"
    _os.environ["NUMEXPR_NUM_THREADS"] = "1"
    _sys.path.insert(0, r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner")
    from expr_cache_builder import ExprSeriesCache as _EC  # noqa
    global _W_UNIVERSE, _W_EC, _W_PAYLOADS
    with open(ohlcv_path, 'rb') as f:
        _W_UNIVERSE = _pickle.load(f)
    _W_EC = _EC()
    _W_PAYLOADS = payloads


def _worker_scan(ticker):
    """Worker entry: scan one ticker. Returns (ticker, results_dict, bar_count)."""
    results = scan_ticker(ticker, _W_EC, _W_UNIVERSE, _W_PAYLOADS)
    n_bars = _W_EC.get_ticker_bar_count(ticker)
    return ticker, results, n_bars


def load_envelope(setup):
    path = os.path.join(ENV_DIR, f"{setup}_envelope.npz")
    if not os.path.exists(path):
        return None
    d = np.load(path)
    return {
        "feature_indices": d["feature_indices"],
        "ex_min": d["ex_min"].astype(np.float32),
        "ex_max": d["ex_max"].astype(np.float32),
        "ex_count": d["ex_count"],
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    # Preflight
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"OHLCV tickers: {len(universe)}", flush=True)
    if len(universe) < 11000:
        print("FAIL: OHLCV ticker count too low")
        sys.exit(1)

    print("Opening expression cache...", flush=True)
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expr cache invalid")
        sys.exit(1)
    ec_tickers = ec.get_available_tickers()
    print(f"Expr cache: {ec.n_expressions} features x {len(ec_tickers)} tickers", flush=True)
    if len(ec_tickers) < 11000:
        print("FAIL: expr cache ticker count too low")
        sys.exit(1)

    # Load rand_range
    if not os.path.exists(RAND_RANGE_FILE):
        print(f"FAIL: rand_range missing at {RAND_RANGE_FILE}. "
              f"Run research/compute_rand_range.py first.")
        sys.exit(1)
    rr = np.load(RAND_RANGE_FILE)
    rand_range_vec = rr["feature_range"].astype(np.float32)
    print(f"rand_range loaded: n_feat={rand_range_vec.shape[0]}, "
          f"finite_count={int(np.sum(np.isfinite(rand_range_vec)))}", flush=True)

    # Build per-setup payloads (N, envelope, OHLC box, LOO budget, survives mask).
    setup_payloads = {}
    for setup in SETUP_ORDER:
        print(f"\n--- Prep {setup.upper()} ---", flush=True)
        env = load_envelope(setup)
        if env is None:
            print(f"  no envelope for {setup}; skipping")
            continue
        N = int(env["ex_min"].shape[0])
        n_rel = int(env["ex_min"].shape[1])
        print(f"  N_bars={N}  n_relevant={n_rel}", flush=True)

        ex_windows, ex_pairs = load_examples_ohlc_window(setup, universe, N)
        print(f"  examples with valid OHLC window: {len(ex_pairs)}", flush=True)
        if len(ex_pairs) < 2:
            print(f"  skip (not enough examples)")
            continue

        ohlc_min = np.nanmin(ex_windows, axis=0).astype(np.float32)  # (N, 3)
        ohlc_max = np.nanmax(ex_windows, axis=0).astype(np.float32)
        budget = compute_stage1_budget(ex_windows)
        print(f"  stage1 OHLC bounds built; LOO budget = {budget} bars out of "
              f"{N} (per-candidate max allowed outside-bars)", flush=True)

        survives = build_stage2_mask(env, rand_range_vec)
        n_surv = int(survives.sum())
        total = survives.size
        print(f"  stage2 surviving bands: {n_surv:,}/{total:,} "
              f"({n_surv/total*100:.1f}%)", flush=True)

        # Example self-pass verification against the all-examples box (should be 100%).
        # Stage 1 self: held-in, outside count = 0 for every example vs the full box.
        # Stage 2 self: every example bar at (k,c) is within [ex_min, ex_max] by
        # construction, so surviving-bands check passes trivially.
        surv_cols = build_stage2_surv_cols(survives)
        setup_payloads[setup] = {
            "N": N,
            "env": env,
            "ohlc_min": ohlc_min,
            "ohlc_max": ohlc_max,
            "budget": budget,
            "survives": survives,
            "surv_cols": surv_cols,
            "ex_pairs": set(ex_pairs),
        }

    # Ticker iteration
    if SAMPLE_N_TICKERS > 0:
        rng = np.random.default_rng(SAMPLE_SEED)
        pool_arr = np.array(sorted(ec_tickers))
        rng.shuffle(pool_arr)
        ticker_subset = list(pool_arr[:SAMPLE_N_TICKERS])
        print(f"\nITERATION MODE: {len(ticker_subset)} random tickers "
              f"(seed={SAMPLE_SEED})", flush=True)
    else:
        ticker_subset = sorted(ec_tickers)
        print(f"\nFULL MODE: all {len(ticker_subset)} cache tickers", flush=True)

    # Ensure every example's ticker is in the scan (for sanity self-pass).
    ex_all_tickers = set()
    for setup, p in setup_payloads.items():
        for t, _ in p["ex_pairs"]:
            if t in ec_tickers:
                ex_all_tickers.add(t)
    scan_tickers = list(set(ticker_subset) | ex_all_tickers)
    print(f"Scan tickers total: {len(scan_tickers)} "
          f"(+{len(ex_all_tickers - set(ticker_subset))} example tickers)",
          flush=True)

    # Free parent's OHLCV dict before spawning workers (each worker reloads
    # its own copy from disk). Keeps parent memory low during the scan.
    del universe

    all_hits = {s: [] for s in setup_payloads}
    n_cands_scanned = {s: 0 for s in setup_payloads}

    from concurrent.futures import ProcessPoolExecutor, as_completed
    import multiprocessing as _mp

    print(f"Spawning {N_WORKERS} workers (each loads OHLCV + opens expr cache "
          f"once at init)...", flush=True)
    t0 = time.time()
    ctx = _mp.get_context("spawn")
    completed = 0
    with ProcessPoolExecutor(max_workers=N_WORKERS, mp_context=ctx,
                             initializer=_worker_init,
                             initargs=(ohlcv_path, setup_payloads)) as pool:
        futures = {pool.submit(_worker_scan, t): t for t in scan_tickers}
        total = len(futures)
        for fut in as_completed(futures):
            ticker, per_setup, n_bars_t = fut.result()
            for s, hits in per_setup.items():
                if hits:
                    all_hits[s].extend([(ticker, d, b) for d, b in hits])
                N = setup_payloads[s]["N"]
                if n_bars_t > N:
                    n_cands_scanned[s] += n_bars_t - N
            completed += 1
            if completed % 500 == 0 or completed == total:
                elapsed = time.time() - t0
                rate = completed / max(elapsed, 1e-6)
                eta = (total - completed) / max(rate, 1e-6)
                tot_hits = sum(len(v) for v in all_hits.values())
                print(f"  {completed}/{total} tickers, {tot_hits:,} total hits, "
                      f"{elapsed:.1f}s elapsed, ETA {eta:.1f}s", flush=True)

    elapsed_total = time.time() - t0
    print(f"\nScan done in {elapsed_total:.1f}s", flush=True)

    # Write outputs and summary
    summary_lines = []
    summary_lines.append("=" * 80)
    summary_lines.append("TWO-STAGE UNIVERSE SCAN (PRESIGNAL_GRINDER §13.4)")
    summary_lines.append("=" * 80)
    mode = (f"ITERATION {SAMPLE_N_TICKERS} tickers seed={SAMPLE_SEED}"
            if SAMPLE_N_TICKERS > 0 else "FULL")
    summary_lines.append(f"Mode: {mode}  |  Tickers scanned: {len(scan_tickers):,}")
    summary_lines.append(f"Total scan time: {elapsed_total:.1f}s")
    summary_lines.append("")

    for setup, p in setup_payloads.items():
        hits = all_hits[setup]
        N = p["N"]
        n_rel = p["env"]["ex_min"].shape[1]
        budget = p["budget"]
        n_surv = int(p["survives"].sum())
        total_bands = p["survives"].size

        csv_path = os.path.join(OUT_DIR, f"two_stage_{setup}_candidates.csv")
        pd.DataFrame(hits, columns=["ticker", "date", "bar_idx"]).to_csv(
            csv_path, index=False)

        hit_pairs = {(t, d) for t, d, _ in hits}
        ex_pairs = p["ex_pairs"]
        # Only count example tickers that are in ec_tickers (we scanned them)
        ex_pairs_scanned = {(t, d) for (t, d) in ex_pairs if t in set(scan_tickers)}
        missing = sorted(ex_pairs_scanned - hit_pairs)
        self_pass_ok = len(missing) == 0

        n_hits = len(hits)
        n_cand = n_cands_scanned[setup]
        rate_pct = (n_hits / max(n_cand, 1)) * 100.0
        by_ticker = defaultdict(list)
        for t, d, _ in hits:
            by_ticker[t].append(d)
        top = sorted(by_ticker.items(), key=lambda x: -len(x[1]))[:10]

        summary_lines.append("-" * 80)
        summary_lines.append(f"{setup.upper()}  N={N}  n_rel={n_rel}  "
                             f"stage2_surviving_bands={n_surv:,}/{total_bands:,}  "
                             f"stage1_budget={budget}")
        summary_lines.append(f"  candidate bars: {n_cand:,}")
        summary_lines.append(f"  hits (passed both stages): {n_hits:,} "
                             f"({rate_pct:.4f}%)")
        summary_lines.append(f"  unique tickers hit: {len(by_ticker):,}")
        summary_lines.append(f"  example self-pass: "
                             f"{len(ex_pairs_scanned) - len(missing)}/"
                             f"{len(ex_pairs_scanned)}  "
                             f"{'OK' if self_pass_ok else 'FAIL'}")
        if not self_pass_ok:
            for t, d in missing[:10]:
                summary_lines.append(f"    missing: {t} {d}")
            if len(missing) > 10:
                summary_lines.append(f"    ... {len(missing) - 10} more")
        summary_lines.append(f"  top-10 tickers by hit count:")
        for t, dates in top:
            summary_lines.append(f"    {t:<8}  {len(dates):>6} hits  "
                                 f"(first: {min(dates)}, last: {max(dates)})")
        summary_lines.append("")

    out_path = os.path.join(OUT_DIR, "two_stage_summary.txt")
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(summary_lines))
    print(f"Wrote summary to {out_path}", flush=True)


if __name__ == "__main__":
    main()
