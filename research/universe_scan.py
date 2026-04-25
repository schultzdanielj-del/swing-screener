"""Universe scan: apply the per-setup example envelope (full expression cache,
entry-anchored, N-bar lead-up [E-N, E-1]) as a strict-AND filter across every
ticker x every candidate bar. 100% example pass by construction.

Per-setup envelope is produced by research/n_derivation_cache.py:
  - feature_indices: relevant cache columns (Spearman rho > 0 on range curve)
  - ex_min, ex_max:  (N_bars, n_relevant) bounding box from labeled examples
  - NaN bands pass through (symmetric NaN-as-pass)

Scan: for each candidate bar c on each ticker, extract the N_bars preceding
bars. A candidate fails if ANY (offset, feature) value is non-NaN and outside
its [ex_min, ex_max] band. Passing candidates are emitted as (ticker, date).

Output:
  research/universe_scan/{setup}_candidates.csv  - (ticker, date, bar_idx)
  research/universe_scan/summary.txt             - per-setup hit counts, top tickers
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
OUT_DIR = os.path.join(WORKTREE, "research", "universe_scan")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]

# Iteration mode: scan a random subset of tickers so each run turns around quickly.
# Set SAMPLE_N_TICKERS=0 to disable and scan full universe.
SAMPLE_N_TICKERS = 500
SAMPLE_SEED = 42

warnings.filterwarnings("ignore", category=RuntimeWarning)


def dates_as_str(dates):
    if pd.api.types.is_datetime64_any_dtype(dates):
        return pd.to_datetime(dates).strftime('%Y-%m-%d').values.astype('<U10')
    return np.array([str(d)[:10] for d in dates], dtype='<U10')


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


def get_examples_bar_idx(setup, universe):
    """Return {(ticker, entry_date_str): True} for all DB examples of this setup
    whose entry_date resolves to a bar in the OHLCV universe. Used by the
    sanity check — every example must appear in the candidate CSV.
    """
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    out = set()
    for ticker, entry_date in rows:
        df = universe.get(ticker)
        if df is None:
            continue
        out.add((ticker, str(entry_date)[:10]))
    return out


def scan_ticker(ticker, ec, env, N):
    """Return list of (date_str, bar_idx) for candidate entries on this ticker
    that pass the full envelope. Candidates are bar positions c where
    c >= N (so the full N-bar lead-up is available).
    """
    dates, data = ec.get_ticker(ticker)
    if dates is None:
        return []
    n_bars = data.shape[0]
    if n_bars <= N:
        return []
    feat_idx = env["feature_indices"]
    ex_min = env["ex_min"]   # (N, n_rel)
    ex_max = env["ex_max"]
    # Subset feature columns once
    sub = data[:, feat_idx].astype(np.float32, copy=False)  # (n_bars, n_rel)

    # Candidates: c in [N, n_bars - 1]. Window for c covers bars [c-N, c-1].
    # n_candidates = n_bars - N.
    n_cand = n_bars - N
    passes = np.ones(n_cand, dtype=bool)

    # Iterate offset 0 (E-1, tightest) first so most failures carve early.
    for k in range(N):
        if not passes.any():
            break
        # Bar positions contributing to offset k across candidates:
        # candidate c uses bar at position c - (k+1) for offset k.
        # positions range: N-k-1 .. n_bars-k-2 inclusive.
        start = N - k - 1
        stop = start + n_cand  # = n_bars - k - 1
        vals = sub[start:stop, :]  # (n_cand, n_rel)
        emin = ex_min[k, :][None, :]  # (1, n_rel)
        emax = ex_max[k, :][None, :]
        # NaN comparisons are False, so non-finite vals or NaN bands = pass.
        fail_any = np.any((vals < emin) | (vals > emax), axis=1)
        passes &= ~fail_any

    hit_positions = np.where(passes)[0]
    if len(hit_positions) == 0:
        del data, dates, sub
        return []
    ds = dates_as_str(dates)
    hits = []
    for hp in hit_positions:
        c = int(hp) + N
        if c < len(ds):
            hits.append((ds[c], c))
    del data, dates, sub
    return hits


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

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

    # Iteration subset (restricted to tickers present in the expr cache)
    if SAMPLE_N_TICKERS > 0:
        rng = np.random.default_rng(SAMPLE_SEED)
        pool = sorted(ec_tickers)  # deterministic given seed
        pool_arr = np.array(pool)
        rng.shuffle(pool_arr)
        ticker_subset = list(pool_arr[:SAMPLE_N_TICKERS])
        print(f"ITERATION MODE: {len(ticker_subset)} random tickers (seed={SAMPLE_SEED})", flush=True)
    else:
        ticker_subset = sorted(ec_tickers)
        print(f"FULL MODE: all {len(ticker_subset)} cache tickers", flush=True)

    summary_lines = []
    summary_lines.append("=" * 80)
    summary_lines.append("UNIVERSE SCAN - full-cache envelope, strict AND, NaN-as-pass")
    summary_lines.append("=" * 80)
    mode_str = (f"ITERATION MODE: {SAMPLE_N_TICKERS} random tickers (seed={SAMPLE_SEED})"
                if SAMPLE_N_TICKERS > 0 else f"FULL MODE: all tickers")
    summary_lines.append(mode_str)
    summary_lines.append(f"Tickers scanned: {len(ticker_subset):,}")
    summary_lines.append("")

    for setup in SETUP_ORDER:
        print(f"\n=== {setup.upper()} ===", flush=True)
        env = load_envelope(setup)
        if env is None:
            print(f"  no envelope artifact for {setup} (run n_derivation_cache.py first)")
            continue
        N = env["ex_min"].shape[0]
        n_rel = env["ex_min"].shape[1]
        n_bands_under_cov = int(np.sum(np.isnan(env["ex_min"])))
        total_bands = N * n_rel
        print(f"  envelope: N_bars={N}, n_relevant={n_rel}, "
              f"NaN bands (under-coverage): {n_bands_under_cov:,}/{total_bands:,}",
              flush=True)

        # Sanity prep: example (ticker, date) set to check against candidate CSV
        ex_pairs = get_examples_bar_idx(setup, universe)
        # Always scan every example's ticker to validate; union with random subset.
        ex_tickers = {t for t, _ in ex_pairs if t in ec_tickers}
        scan_tickers = list(set(ticker_subset) | ex_tickers)
        print(f"  scan tickers: {len(ticker_subset)} sample + "
              f"{len(ex_tickers - set(ticker_subset))} example tickers "
              f"= {len(scan_tickers)} total", flush=True)

        t0 = time.time()
        all_hits = []  # list of (ticker, date, bar_idx)
        n_scanned = 0
        n_candidate_bars = 0
        for ti, ticker in enumerate(scan_tickers):
            hits = scan_ticker(ticker, ec, env, N)
            all_hits.extend([(ticker, d, b) for d, b in hits])
            # Count candidate bars even for zero-hit tickers
            n_bars_t = ec.get_ticker_bar_count(ticker)
            if n_bars_t > N:
                n_candidate_bars += n_bars_t - N
            n_scanned += 1
            if (ti + 1) % 50 == 0:
                print(f"    {ti+1}/{len(scan_tickers)} tickers, "
                      f"{len(all_hits):,} hits so far, "
                      f"{time.time() - t0:.1f}s", flush=True)
        elapsed = time.time() - t0
        n_hits = len(all_hits)
        carve_rate = n_hits / max(n_candidate_bars, 1)
        print(f"  scan: {elapsed:.1f}s, {n_hits:,} hits / "
              f"{n_candidate_bars:,} candidate bars ({carve_rate*100:.4f}%)", flush=True)

        # Write CSV
        csv_path = os.path.join(OUT_DIR, f"{setup}_candidates.csv")
        pd.DataFrame(all_hits, columns=["ticker", "date", "bar_idx"]).to_csv(
            csv_path, index=False)

        # Sanity check: every example must appear in hits
        hit_pairs = {(t, d) for t, d, _ in all_hits}
        missing_examples = sorted(ex_pairs - hit_pairs)
        sanity_ok = len(missing_examples) == 0
        if sanity_ok:
            print(f"  SANITY OK: all {len(ex_pairs)} examples appear in candidate CSV",
                  flush=True)
        else:
            print(f"  SANITY FAIL: {len(missing_examples)}/{len(ex_pairs)} examples missing",
                  flush=True)
            for tkr, dt in missing_examples[:10]:
                print(f"    missing: {tkr} {dt}", flush=True)
            if len(missing_examples) > 10:
                print(f"    ... {len(missing_examples)-10} more", flush=True)

        by_ticker = defaultdict(list)
        for t, d, _ in all_hits:
            by_ticker[t].append(d)
        n_tickers_hit = len(by_ticker)
        top = sorted(by_ticker.items(), key=lambda x: -len(x[1]))[:10]

        summary_lines.append("-" * 80)
        summary_lines.append(f"{setup.upper()}  N_bars={N}  n_relevant={n_rel}  "
                             f"examples_in_CSV={len(ex_pairs) - len(missing_examples)}/{len(ex_pairs)}")
        summary_lines.append(f"  candidate bars scanned: {n_candidate_bars:,}")
        summary_lines.append(f"  candidates passing envelope: {n_hits:,}  "
                             f"({carve_rate*100:.4f}%)")
        summary_lines.append(f"  unique tickers hit: {n_tickers_hit:,}")
        summary_lines.append(f"  scan time: {elapsed:.1f}s")
        summary_lines.append(f"  sanity: {'OK' if sanity_ok else 'FAIL'}")
        if not sanity_ok:
            summary_lines.append(f"  missing examples: {len(missing_examples)}")
        summary_lines.append(f"  top-10 tickers by hit count:")
        for t, dates in top:
            summary_lines.append(f"    {t:<8}  {len(dates):>5} hits  "
                                 f"(first: {min(dates)}, last: {max(dates)})")
        summary_lines.append("")

    with open(os.path.join(OUT_DIR, "summary.txt"), 'w', encoding='utf-8') as f:
        f.write("\n".join(summary_lines))
    print(f"\nWrote summary to {OUT_DIR}/summary.txt", flush=True)


if __name__ == "__main__":
    main()
