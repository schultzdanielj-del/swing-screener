"""HTF 3-family visual shape comparison — which filter best carves while
preserving 100% EX pass?

All three families operate on log(close[E-k]/close[E]) anchored paths over
N=39 bars (offsets k=1..N). Purely geometric, scale-invariant.

F1 — Joint consecutive-offset hull.
    Per adjacent offset pair (k, k+1), build 2D convex hull of example
    (p[k], p[k+1]) points. Candidate passes pair k iff its (p[k], p[k+1])
    is inside the hull. Candidate passes F1 iff it passes every pair.

F2 — Per-offset return band.
    Per offset pair (k, k+1), return = p[k] - p[k+1] (log-return going
    forward in time from E-(k+1) to E-k). Band = [min, max] of that return
    across examples. Candidate passes iff every return is in its band.

F3 — Path-distance to nearest example.
    Euclidean distance over the N-vector path. Threshold = max pairwise
    distance among example paths. Candidate passes iff min distance to any
    example is <= threshold.

Scans SAMPLE_SIZE random tickers (excluding example tickers). Reports
100% EX pass verification, per-family wild carve rates, and a random
sample of surviving candidates per family (saved to CSV for spot-check).
"""
from __future__ import annotations

import json
import os
import pickle
import random
import sqlite3
import sys
import time

import numpy as np
import pandas as pd
from matplotlib.path import Path as MplPath
from scipy.spatial import ConvexHull, QhullError

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_DIR = os.path.join(WORKTREE, "research", "visual_shape_compare")

SETUP = "htf"
N_BARS = 39  # from research/n_derivation_cache/htf_summary.json
DATE_CUTOFF = "2020-01-02"  # only count candidates with E-date on/after this

MIN_TICKER_COUNT = 11000  # preflight gate


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def extract_example_path(df, E_idx, N):
    """Return length-N array path[k] = log(close[E-(k+1)] / close[E])  for k=0..N-1.
    path[0] is log(close[E-1]/close_E), path[N-1] is log(close[E-N]/close_E).

    NaN-lenient: offsets beyond available history (E_idx - k - 1 < 0) are NaN.
    Individual bars with non-finite or non-positive close are also NaN.
    Returns None only if E_idx < 1 (no history) or close_E invalid.
    """
    if E_idx < 1:
        return None
    close = df["close"].values.astype(np.float64)
    close_E = close[E_idx]
    if not np.isfinite(close_E) or close_E <= 0:
        return None
    reach = min(N, E_idx)  # how many bars back are available
    path = np.full(N, np.nan, dtype=np.float64)
    prev = close[E_idx - reach:E_idx][::-1]  # [E-1, E-2, ..., E-reach]
    ok = np.isfinite(prev) & (prev > 0)
    path[:reach] = np.where(ok, np.log(np.where(ok, prev, 1.0)) - np.log(close_E), np.nan)
    return path


def build_hull_path(points):
    """Return an MplPath for the 2D convex hull of points (shape (n, 2)),
    or a fallback MplPath covering the bounding box if hull is degenerate.
    Handles near-collinear and duplicate-point cases."""
    pts = np.asarray(points, dtype=np.float64)
    uniq = np.unique(pts, axis=0)
    if len(uniq) >= 3:
        try:
            hull = ConvexHull(uniq)
            ring = uniq[hull.vertices]
            return MplPath(np.vstack([ring, ring[:1]]), closed=True)
        except QhullError:
            pass
    # degenerate — fall back to axis-aligned bounding box of all points
    xmin, ymin = uniq.min(axis=0)
    xmax, ymax = uniq.max(axis=0)
    # tiny epsilon expansion so exact-match points are inside
    eps = max(1e-12, 1e-9 * (abs(xmax) + abs(ymax) + 1.0))
    box = np.array([
        [xmin - eps, ymin - eps],
        [xmax + eps, ymin - eps],
        [xmax + eps, ymax + eps],
        [xmin - eps, ymax + eps],
        [xmin - eps, ymin - eps],
    ])
    return MplPath(box, closed=True)


def build_filters(example_paths):
    """Build F1 hulls, F2 return bands, F3 threshold from the example paths.

    example_paths: shape (n_ex, N). May contain NaN for offsets an example
    doesn't reach (recent IPOs etc.); those examples contribute to each
    offset pair only where they have finite values at both.

    Returns dict with:
      - hulls: list of length N-1. Each entry is either an MplPath (when
        >=3 examples have finite values at both offsets of the pair) or
        None (auto-pass at that pair for all candidates).
      - band_lo, band_hi: NaN-aware per-offset-pair return min/max.
      - threshold, example_paths: kept for the HTF smoke-test main(). Not
        used by downstream presignal_grinder_all.
    """
    n_ex, N = example_paths.shape

    # F1 — NaN-lenient 2D hull per offset pair
    hulls = []
    hull_n_contrib = np.zeros(N - 1, dtype=np.int32)
    for k in range(N - 1):
        pts = example_paths[:, [k, k + 1]]
        mask = np.isfinite(pts).all(axis=1)
        hull_n_contrib[k] = int(mask.sum())
        if mask.sum() >= 3:
            hulls.append(build_hull_path(pts[mask]))
        else:
            hulls.append(None)

    # F2 — NaN-aware min/max over finite returns per offset pair
    returns = example_paths[:, :-1] - example_paths[:, 1:]
    with np.errstate(all='ignore'):
        band_lo = np.nanmin(returns, axis=0)
        band_hi = np.nanmax(returns, axis=0)

    # F3 — max pairwise Euclidean distance among examples; NaN-skipped per-pair.
    # Only used by HTF smoke test main(); NaN may inflate or deflate it. Not
    # load-bearing for the grinder.
    diffs = example_paths[:, None, :] - example_paths[None, :, :]
    with np.errstate(all='ignore'):
        dists = np.sqrt(np.nansum(diffs * diffs, axis=2))
    threshold = float(np.nan_to_num(dists, nan=0.0).max())

    return {
        "hulls": hulls,
        "hull_n_contrib": hull_n_contrib,
        "band_lo": band_lo.astype(np.float64),
        "band_hi": band_hi.astype(np.float64),
        "threshold": threshold,
        "example_paths": example_paths,
    }


def check_F1_batch(paths, hulls):
    """paths: (n_cand, N). Return bool array (n_cand,) — NaN-lenient F1
    check. Candidate passes the pair (k, k+1) iff:
      (a) hull at that pair is None (too few examples contributed — auto-pass), OR
      (b) candidate has NaN at offset k or k+1 (no data at that reach — auto-pass), OR
      (c) candidate's (p[k], p[k+1]) point is inside hulls[k].

    Candidate passes F1 iff every pair passes. A tiny positive radius is
    used so hull-vertex points count as 'inside' (needed for 100% EX pass)."""
    n_cand, N = paths.shape
    passing = np.ones(n_cand, dtype=bool)
    for k in range(N - 1):
        hull = hulls[k]
        if hull is None:
            continue
        pair_pts = paths[:, [k, k + 1]]
        has_both = np.isfinite(pair_pts).all(axis=1)
        in_hull = np.ones(n_cand, dtype=bool)  # auto-pass default for NaN
        if has_both.any():
            in_hull[has_both] = hull.contains_points(pair_pts[has_both], radius=1e-9)
        passing &= in_hull
    return passing


def check_F2_batch(paths, band_lo, band_hi):
    """paths: (n_cand, N). Return bool array — True iff every return is in band."""
    returns = paths[:, :-1] - paths[:, 1:]  # (n_cand, N-1)
    below = returns < band_lo[None, :]
    above = returns > band_hi[None, :]
    fail = below | above  # (n_cand, N-1)
    return ~fail.any(axis=1)


def check_F3_batch(paths, example_paths, threshold):
    """paths: (n_cand, N). Return bool array — True iff min dist to any example <= threshold."""
    # pairwise distances: paths (n_cand, N), example_paths (n_ex, N)
    # result: (n_cand, n_ex)
    n_cand = paths.shape[0]
    n_ex = example_paths.shape[0]
    # compute in a loop over examples to avoid a huge intermediate
    min_d2 = np.full(n_cand, np.inf)
    for i in range(n_ex):
        diffs = paths - example_paths[i][None, :]
        d2 = np.sum(diffs * diffs, axis=1)
        np.minimum(min_d2, d2, out=min_d2)
    return min_d2 <= (threshold * threshold)


def scan_ticker(df, N, filters, date_cutoff):
    """Scan one ticker: for every candidate E with E >= 1 and E-date >= cutoff,
    build an N-length log-ratio path (NaN for offsets beyond E's reach) and
    apply the filters. Returns (n_candidates, f1_pass_E, f2_pass_E, f3_pass_E).

    Candidates with reach < N contribute to whatever offsets they have and
    auto-pass offsets they can't reach. This mirrors live-trading semantics:
    a recent-IPO's few-bar history is checked as-is; absence of older bars
    isn't a disqualifier. See presignal spec §4.5 NaN-lenient-symmetric.
    """
    close = df["close"].values.astype(np.float64)
    L = len(close)
    empty = np.array([], dtype=np.int64)
    if L < 2:
        return 0, empty, empty, empty

    log_close = np.log(np.where(close > 0, close, np.nan))

    # Eligible candidates: E in 1..L-1 with date_str >= cutoff and close[E] finite.
    ds = dates_as_str(df)
    E_range = np.arange(1, L, dtype=np.int64)
    E_range = E_range[(ds[E_range] >= date_cutoff)]
    if len(E_range) == 0:
        return 0, empty, empty, empty
    log_close_E = log_close[E_range]
    valid_E_mask = np.isfinite(log_close_E)
    E_range = E_range[valid_E_mask]
    log_close_E = log_close_E[valid_E_mask]
    if len(E_range) == 0:
        return 0, empty, empty, empty

    # NaN-padded sliding window: pad N NaNs at the front so every candidate's
    # N-bar lookback lands in-range. For candidate E, window = padded[E:E+N],
    # where padded[E+i] = log_close[E+i-N] if E+i-N >= 0 else NaN. Reversed so
    # path[0] corresponds to E-1, path[N-1] to E-N (NaN if E-N < 0).
    padded = np.concatenate([np.full(N, np.nan), log_close])
    swv = np.lib.stride_tricks.sliding_window_view(padded, N)
    paths_pre = swv[E_range][:, ::-1]
    paths = paths_pre - log_close_E[:, None]

    n_cand = len(E_range)
    f1 = check_F1_batch(paths, filters["hulls"])
    f2 = check_F2_batch(paths, filters["band_lo"], filters["band_hi"])
    f3 = check_F3_batch(paths, filters["example_paths"], filters["threshold"])

    return (
        n_cand,
        E_range[f1],
        E_range[f2],
        E_range[f3],
    )


def dedupe_clusters(E_list, example_E_set):
    """Apply CLASSIFIER_SPEC §5.2 cluster dedup:
      - consecutive-bar dedupe per ticker (rightmost wins)
      - split clusters at example signal bars so each example is its own
        cluster's rightmost.

    E_list: sorted list of F1-pass E-indices (ints) for one ticker.
    example_E_set: set of example E-indices for this ticker (may be empty).

    Returns list of cluster representatives (E-indices), in order.
    """
    if not E_list:
        return []
    E_list = sorted(E_list)
    reps = []
    run_start = E_list[0]
    run_end = E_list[0]

    def emit(rs, re):
        exs_in_run = sorted(e for e in example_E_set if rs <= e <= re)
        if not exs_in_run:
            reps.append(re)
            return
        cursor = rs
        for ex_e in exs_in_run:
            reps.append(ex_e)
            cursor = ex_e + 1
        if cursor <= re:
            reps.append(re)

    for E in E_list[1:]:
        if E == run_end + 1:
            run_end = E
        else:
            emit(run_start, run_end)
            run_start = E
            run_end = E
    emit(run_start, run_end)
    return reps


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    # Preflight OHLCV cache
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    t0 = time.time()
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe tickers: {len(universe)}  (loaded in {time.time() - t0:.1f}s)", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print(f"FAIL: OHLCV ticker count below {MIN_TICKER_COUNT}", flush=True)
        sys.exit(1)

    # Load examples
    examples = get_examples(SETUP)
    print(f"{SETUP.upper()} examples in DB: {len(examples)}", flush=True)

    example_paths = []
    example_meta = []
    for ex in examples:
        df = universe.get(ex["ticker"])
        if df is None:
            continue
        E_idx = lookup_idx(df, ex["entry_date"])
        if E_idx < 0:
            continue
        p = extract_example_path(df, E_idx, N_BARS)
        if p is None:
            continue
        example_paths.append(p)
        example_meta.append({"ticker": ex["ticker"], "entry_date": ex["entry_date"], "E_idx": E_idx})

    n_ex = len(example_paths)
    example_paths = np.array(example_paths)  # (n_ex, N)
    print(f"Valid example paths: {n_ex} / {len(examples)}  "
          f"(shape {example_paths.shape})", flush=True)

    # Build filters
    print("Building filters...", flush=True)
    filters = build_filters(example_paths)
    print(f"  F1: {len(filters['hulls'])} hulls built", flush=True)
    print(f"  F2: band_lo range [{filters['band_lo'].min():.4f}, {filters['band_lo'].max():.4f}]; "
          f"band_hi range [{filters['band_hi'].min():.4f}, {filters['band_hi'].max():.4f}]", flush=True)
    print(f"  F3: threshold (max pairwise Euclidean) = {filters['threshold']:.4f}", flush=True)

    # Sanity — every example passes its own filter (100% EX pass)
    print("Sanity: 100% EX pass check...", flush=True)
    f1_ex = check_F1_batch(example_paths, filters["hulls"])
    f2_ex = check_F2_batch(example_paths, filters["band_lo"], filters["band_hi"])
    f3_ex = check_F3_batch(example_paths, filters["example_paths"], filters["threshold"])
    print(f"  F1 example pass: {f1_ex.sum()}/{n_ex}", flush=True)
    print(f"  F2 example pass: {f2_ex.sum()}/{n_ex}", flush=True)
    print(f"  F3 example pass: {f3_ex.sum()}/{n_ex}", flush=True)
    if not (f1_ex.all() and f2_ex.all() and f3_ex.all()):
        print("FAIL: not all examples pass their own filter. Aborting.", flush=True)
        sys.exit(1)

    # Full universe scan (examples included — their entry bars pass F1 by
    # construction and will appear in the cluster-dedupe output where §5.2
    # guarantees each example is its own cluster's rightmost).
    tickers = sorted(universe.keys())
    print(f"Full-universe scan: {len(tickers)} tickers, date cutoff >= {DATE_CUTOFF}", flush=True)

    # Build per-ticker example E-idx map for cluster splits.
    # Also snapshot each example's E-idx under its ticker (already have example_meta above).
    per_ticker_ex = {}
    for m in example_meta:
        per_ticker_ex.setdefault(m["ticker"], set()).add(int(m["E_idx"]))

    # Per-ticker F1 survivors; also F2/F3 bar counts for continued comparison.
    print("Scanning...", flush=True)
    total_candidates = 0
    f2_bar_count = 0
    f3_bar_count = 0
    f1_per_ticker = {}  # ticker -> list of F1-pass E-idx
    t_scan = time.time()

    for i, ticker in enumerate(tickers):
        df = universe.get(ticker)
        if df is None:
            continue
        n_cand, f1_E, f2_E, f3_E = scan_ticker(df, N_BARS, filters, DATE_CUTOFF)
        total_candidates += n_cand
        if len(f1_E) > 0:
            f1_per_ticker[ticker] = [int(e) for e in f1_E]
        f2_bar_count += len(f2_E)
        f3_bar_count += len(f3_E)
        if (i + 1) % 1000 == 0:
            print(f"  {i + 1}/{len(tickers)} tickers ({time.time() - t_scan:.1f}s)  "
                  f"candidates: {total_candidates:,}  "
                  f"F1 bars: {sum(len(v) for v in f1_per_ticker.values()):,}  "
                  f"F2 bars: {f2_bar_count:,}  F3 bars: {f3_bar_count:,}",
                  flush=True)

    print(f"\nScan complete in {time.time() - t_scan:.1f}s", flush=True)
    print(f"Total candidates scanned (post-{DATE_CUTOFF}): {total_candidates:,}", flush=True)

    # §5.2 cluster dedup on F1 survivors
    f1_clusters = []  # list of (ticker, rep_E_idx, is_example_matched)
    for ticker, E_list in f1_per_ticker.items():
        ex_set = per_ticker_ex.get(ticker, set())
        reps = dedupe_clusters(E_list, ex_set)
        for r in reps:
            f1_clusters.append((ticker, r, r in ex_set))

    f1_bar_count = sum(len(v) for v in f1_per_ticker.values())
    ex_matched = sum(1 for _, _, m in f1_clusters if m)
    non_ex_matched = len(f1_clusters) - ex_matched

    def rate(n):
        return (n / total_candidates) if total_candidates > 0 else 0.0

    print(f"\n=== RESULTS — HTF, N={N_BARS}, {n_ex} examples, full universe, date >= {DATE_CUTOFF} ===", flush=True)
    print(f"F1 bar-count survivors:   {f1_bar_count:>9,}   ({rate(f1_bar_count) * 100:.4f}% bar-carve)", flush=True)
    print(f"F1 cluster-count (§5.2):  {len(f1_clusters):>9,}   (example-matched: {ex_matched}, non-example: {non_ex_matched})", flush=True)
    print(f"F2 bar-count survivors:   {f2_bar_count:>9,}   ({rate(f2_bar_count) * 100:.4f}% bar-carve)", flush=True)
    print(f"F3 bar-count survivors:   {f3_bar_count:>9,}   ({rate(f3_bar_count) * 100:.4f}% bar-carve)", flush=True)

    # Save summary and F1 cluster CSV (the only survivor set we care about going forward)
    summary = {
        "setup": SETUP,
        "N_bars": N_BARS,
        "date_cutoff": DATE_CUTOFF,
        "n_examples_valid": int(n_ex),
        "n_tickers_scanned": len(tickers),
        "total_candidates_scanned": int(total_candidates),
        "F1_bar_count": f1_bar_count,
        "F1_cluster_count": len(f1_clusters),
        "F1_cluster_example_matched": ex_matched,
        "F1_cluster_non_example": non_ex_matched,
        "F2_bar_count": f2_bar_count,
        "F3_bar_count": f3_bar_count,
        "F1_bar_carve_rate": rate(f1_bar_count),
        "F2_bar_carve_rate": rate(f2_bar_count),
        "F3_bar_carve_rate": rate(f3_bar_count),
        "F3_threshold": filters["threshold"],
        "ex_pass_F1": int(f1_ex.sum()),
        "ex_pass_F2": int(f2_ex.sum()),
        "ex_pass_F3": int(f3_ex.sum()),
    }
    with open(os.path.join(OUT_DIR, "summary.json"), 'w') as f:
        json.dump(summary, f, indent=2)

    # F1 cluster list with dates
    rows = []
    for ticker, E_idx, is_ex in sorted(f1_clusters):
        df = universe.get(ticker)
        if df is None:
            continue
        date = dates_as_str(df)[E_idx] if E_idx < len(df) else ""
        rows.append({"ticker": ticker, "E_idx": E_idx, "date": date, "example_matched": is_ex})
    if rows:
        pd.DataFrame(rows).to_csv(os.path.join(OUT_DIR, "F1_clusters.csv"), index=False)
        print(f"  wrote {len(rows):,} F1 clusters -> F1_clusters.csv", flush=True)

    print(f"\nWrote summary to {os.path.join(OUT_DIR, 'summary.json')}", flush=True)


if __name__ == "__main__":
    main()
