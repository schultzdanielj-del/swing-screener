"""Universe scan with LOO-derived per-bar deviation budget.

Same full-cache envelope as universe_scan.py, but a candidate passes if the
number of its N bars with ANY feature outside the envelope is <= the budget.

Budget derivation: for each example i, rebuild the box without i; count the
number of bars in example_i's window where at least one feature falls
strictly outside the remaining-examples box. The largest such count across
all examples = max_allowed_bars. Candidates get at most that many
deviation bars.

Strict-AND is the special case budget=0.
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
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
ENV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
OUT_DIR = os.path.join(WORKTREE, "research", "universe_scan")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]

SAMPLE_N_TICKERS = 500
SAMPLE_SEED = 42

warnings.filterwarnings("ignore", category=RuntimeWarning)


def dates_as_str(dates):
    if pd.api.types.is_datetime64_any_dtype(dates):
        return pd.to_datetime(dates).strftime('%Y-%m-%d').values.astype('<U10')
    return np.array([str(d)[:10] for d in dates], dtype='<U10')


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


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


def extract_example_windows(examples, ec, N, feat_idx):
    """Return (windows, ok_rows, ex_labels).
    windows: (n_ex_ok, N, n_rel) float32, row 0 = E-1, row N-1 = E-N.
    ex_labels: list of (ticker, entry_date_str) for each ok row.
    Dropped examples (insufficient history) are omitted.
    """
    n_rel = len(feat_idx)
    wins = []
    labels = []
    by_ticker = defaultdict(list)
    for i, ex in enumerate(examples):
        by_ticker[ex["ticker"]].append((i, ex["entry_date"]))
    for ticker, entries in by_ticker.items():
        dates, data = ec.get_ticker(ticker)
        if dates is None:
            continue
        ds = dates_as_str(dates)
        for ex_i, date in entries:
            m = np.where(ds == str(date)[:10])[0]
            if len(m) == 0:
                continue
            E_idx = int(m[0])
            if E_idx < N:
                continue
            win = data[E_idx - N:E_idx, feat_idx].astype(np.float32, copy=False)
            wins.append(win[::-1, :].copy())
            labels.append((ticker, str(date)[:10]))
        del data, dates, ds
    if not wins:
        return np.zeros((0, N, n_rel), dtype=np.float32), []
    return np.stack(wins, axis=0), labels


def compute_loo_budget(windows, ex_min, ex_max):
    """Return (n_expanding_bars_per_ex, max_allowed_bars).
    A bar of example_i is 'expanding' iff at that bar, example_i holds a
    sole extreme (min or max) at at least one feature across all examples.
    Equivalently: example_i's value at (k, c) is strictly outside the
    min/max of the OTHER examples at (k, c).
    """
    n_ex, N, n_rel = windows.shape
    # Equality with ex_min / ex_max. NaN == NaN is False, so NaN cells never register.
    at_full_min = (windows == ex_min[None, :, :])
    at_full_max = (windows == ex_max[None, :, :])
    # Count how many examples hold the min/max at each (k, c).
    count_at_min = at_full_min.sum(axis=0)  # (N, n_rel)
    count_at_max = at_full_max.sum(axis=0)
    # Sole frontier = this example is at the extreme AND it's the only one.
    sole_min = at_full_min & (count_at_min[None, :, :] == 1)
    sole_max = at_full_max & (count_at_max[None, :, :] == 1)
    creates_frontier = sole_min | sole_max  # (n_ex, N, n_rel)
    bar_expanding = creates_frontier.any(axis=2)  # (n_ex, N)
    n_expanding_per_ex = bar_expanding.sum(axis=1).astype(np.int32)  # (n_ex,)
    max_allowed_bars = int(n_expanding_per_ex.max()) if n_ex > 0 else 0
    return n_expanding_per_ex, max_allowed_bars


def scan_ticker_with_budget(ticker, ec, env, N, budget_bars):
    """Count outside bars per candidate; pass if count <= budget_bars."""
    dates, data = ec.get_ticker(ticker)
    if dates is None:
        return []
    n_bars = data.shape[0]
    if n_bars <= N:
        return []
    feat_idx = env["feature_indices"]
    ex_min = env["ex_min"]
    ex_max = env["ex_max"]
    sub = data[:, feat_idx].astype(np.float32, copy=False)
    n_cand = n_bars - N
    n_outside_bars = np.zeros(n_cand, dtype=np.int32)
    # Early-termination only possible if budget_bars == 0 (strict AND).
    for k in range(N):
        start = N - k - 1
        stop = start + n_cand
        vals = sub[start:stop, :]
        emin = ex_min[k, :][None, :]
        emax = ex_max[k, :][None, :]
        fail_any = np.any((vals < emin) | (vals > emax), axis=1)
        n_outside_bars += fail_any.astype(np.int32)
        if budget_bars == 0 and not np.any(n_outside_bars == 0):
            del data, dates, sub
            return []
    passes = n_outside_bars <= budget_bars
    hit_positions = np.where(passes)[0]
    if len(hit_positions) == 0:
        del data, dates, sub
        return []
    ds = dates_as_str(dates)
    hits = []
    for hp in hit_positions:
        c = int(hp) + N
        if c < len(ds):
            hits.append((ds[c], c, int(n_outside_bars[hp])))
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

    if SAMPLE_N_TICKERS > 0:
        rng = np.random.default_rng(SAMPLE_SEED)
        pool = sorted(ec_tickers)
        pool_arr = np.array(pool)
        rng.shuffle(pool_arr)
        ticker_subset = list(pool_arr[:SAMPLE_N_TICKERS])
        print(f"ITERATION MODE: {len(ticker_subset)} random tickers (seed={SAMPLE_SEED})",
              flush=True)
    else:
        ticker_subset = sorted(ec_tickers)
        print(f"FULL MODE: all {len(ticker_subset)} cache tickers", flush=True)

    summary_lines = []
    summary_lines.append("=" * 80)
    summary_lines.append("UNIVERSE SCAN - full-cache envelope + LOO deviation budget")
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
            print(f"  no envelope for {setup}; run n_derivation_cache.py first")
            continue
        N = env["ex_min"].shape[0]
        n_rel = env["ex_min"].shape[1]
        print(f"  envelope: N_bars={N}, n_relevant={n_rel}", flush=True)

        # LOO budget
        print("  re-extracting example windows...", flush=True)
        t0 = time.time()
        examples = get_examples(setup)
        wins, ex_labels = extract_example_windows(
            examples, ec, N, env["feature_indices"])
        n_ok = wins.shape[0]
        print(f"  {n_ok}/{len(examples)} examples loaded  ({time.time()-t0:.1f}s)",
              flush=True)
        if n_ok == 0:
            continue
        t0 = time.time()
        n_expanding, max_allowed = compute_loo_budget(
            wins, env["ex_min"], env["ex_max"])
        print(f"  LOO: n_expanding_bars per example "
              f"min={int(n_expanding.min())} median={int(np.median(n_expanding))} "
              f"max={int(n_expanding.max())} of N={N}  "
              f"({time.time()-t0:.1f}s)", flush=True)
        max_allowed_pct = max_allowed / N * 100 if N > 0 else 0
        print(f"  BUDGET: {max_allowed} bars / {N} ({max_allowed_pct:.1f}%)", flush=True)

        # Identify which example set the budget (sanity / interpretability)
        budget_setters = [ex_labels[i] for i in range(n_ok)
                          if int(n_expanding[i]) == max_allowed]
        print(f"  budget-setting example(s): "
              f"{', '.join(t + ' ' + d for t, d in budget_setters[:3])}"
              f"{'...' if len(budget_setters) > 3 else ''}", flush=True)

        # Free LOO tensors before scanning
        del wins

        # Scan
        ex_tickers = {t for t, _ in ex_labels if t in ec_tickers}
        scan_tickers = list(set(ticker_subset) | ex_tickers)
        print(f"  scan tickers: {len(ticker_subset)} sample + "
              f"{len(ex_tickers - set(ticker_subset))} example tickers "
              f"= {len(scan_tickers)}", flush=True)

        t0 = time.time()
        all_hits = []
        n_candidate_bars = 0
        for ti, ticker in enumerate(scan_tickers):
            hits = scan_ticker_with_budget(ticker, ec, env, N, max_allowed)
            all_hits.extend([(ticker, d, b, n_out) for d, b, n_out in hits])
            n_bars_t = ec.get_ticker_bar_count(ticker)
            if n_bars_t > N:
                n_candidate_bars += n_bars_t - N
            if (ti + 1) % 50 == 0:
                print(f"    {ti+1}/{len(scan_tickers)} tickers, "
                      f"{len(all_hits):,} hits, {time.time()-t0:.1f}s", flush=True)
        elapsed = time.time() - t0
        n_hits = len(all_hits)
        carve_rate = n_hits / max(n_candidate_bars, 1)
        print(f"  scan: {elapsed:.1f}s, {n_hits:,} hits / "
              f"{n_candidate_bars:,} cand bars ({carve_rate*100:.4f}%)", flush=True)

        # Write CSV
        csv_path = os.path.join(OUT_DIR, f"deviation_{setup}_candidates.csv")
        pd.DataFrame(all_hits, columns=["ticker", "date", "bar_idx", "n_outside_bars"]
                     ).to_csv(csv_path, index=False)

        # Sanity: all valid examples appear in hits
        ex_pairs = set(ex_labels)
        hit_pairs = {(t, d) for t, d, _, _ in all_hits}
        missing = sorted(ex_pairs - hit_pairs)
        sanity_ok = len(missing) == 0
        if sanity_ok:
            print(f"  SANITY OK: all {len(ex_pairs)} valid examples appear", flush=True)
        else:
            print(f"  SANITY FAIL: {len(missing)}/{len(ex_pairs)} missing", flush=True)
            for tkr, dt in missing[:10]:
                print(f"    missing: {tkr} {dt}", flush=True)

        # Count wild vs example hits
        n_wild = sum(1 for t, d, _, _ in all_hits if (t, d) not in ex_pairs)
        n_example_in_csv = n_hits - n_wild

        # n_outside_bars distribution on hits
        outside_counts = [n_out for _, _, _, n_out in all_hits]
        if outside_counts:
            q = np.quantile(outside_counts, [0.25, 0.5, 0.75])
            print(f"  hit n_outside_bars quantiles: p25={int(q[0])}, p50={int(q[1])}, "
                  f"p75={int(q[2])}, max={max(outside_counts)}", flush=True)

        # Top ticker lineup
        by_ticker = defaultdict(list)
        for t, d, _, _ in all_hits:
            by_ticker[t].append(d)
        n_tickers_hit = len(by_ticker)
        top = sorted(by_ticker.items(), key=lambda x: -len(x[1]))[:10]

        summary_lines.append("-" * 80)
        summary_lines.append(f"{setup.upper()}  N_bars={N}  n_relevant={n_rel}  "
                             f"budget={max_allowed}/{N} ({max_allowed_pct:.1f}%)")
        summary_lines.append(f"  candidate bars scanned: {n_candidate_bars:,}")
        summary_lines.append(f"  total hits: {n_hits:,}  "
                             f"(examples: {n_example_in_csv}, wild: {n_wild})  "
                             f"carve {carve_rate*100:.4f}%")
        summary_lines.append(f"  unique tickers hit: {n_tickers_hit}")
        summary_lines.append(f"  scan time: {elapsed:.1f}s")
        summary_lines.append(f"  sanity: {'OK' if sanity_ok else 'FAIL'} "
                             f"({len(ex_pairs) - len(missing)}/{len(ex_pairs)})")
        if outside_counts:
            summary_lines.append(f"  hit n_outside_bars: p25={int(q[0])}, p50={int(q[1])}, "
                                 f"p75={int(q[2])}, max={max(outside_counts)}")
        summary_lines.append("  top-10 tickers by hit count:")
        for t, dates in top:
            summary_lines.append(f"    {t:<8}  {len(dates):>5} hits  "
                                 f"(first: {min(dates)}, last: {max(dates)})")
        summary_lines.append("")

    with open(os.path.join(OUT_DIR, "deviation_summary.txt"), 'w', encoding='utf-8') as f:
        f.write("\n".join(summary_lines))
    print(f"\nWrote summary to {OUT_DIR}/deviation_summary.txt", flush=True)


if __name__ == "__main__":
    main()
