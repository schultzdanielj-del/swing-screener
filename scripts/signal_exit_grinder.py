"""
Signal Exit Grinder — Cache-compatible exit discovery for signal filtering.

PURPOSE: Find the best exit condition using ONLY expressions in the expression cache.
Runs forward from the DEDUPLICATED SIGNAL BAR (scan candle = entry - 1), not the entry bar.
Output feeds directly into signal_filter.py for vetting pipeline.

This is separate from exit_grinder.py (trade management exit), which:
  - Uses 6,410 expressions from exit_expressions.py (many entry-relative)
  - Runs forward from the ENTRY bar
  - Uses ExitExprEngine (separate computation path)
  - Is shelved until example library is stronger

DUAL-EXIT OR-SET (schema v2):
  - Pick 1: the best 100%-coverage candidate (current behavior, ranked by median capture eff).
  - Pick 2: a second candidate that fires earlier than pick 1 on a subset of examples.
    At scan time, whichever of pick 1 / pick 2 fires first wins.
  - Pick 2 is allowed sub-100% coverage (default >=25%), must pass redundancy + min-gain
    defenses, and is chosen on a 70/30 holdout with train/holdout aggregates reported.

RULES:
  - Pick 1 still requires 100% example pass rate (non-negotiable baseline).
  - Expression cache is the ONLY computation path — no live compute_series.
  - Same data path as signal grinder + signal filter.
  - Backward compat: top_conditions[0] always equals or_exit_set[0] (pick 1).

Usage:
    python scripts/signal_exit_grinder.py --setup dtss
    python scripts/signal_exit_grinder.py --setup htf --no-upload
    python scripts/signal_exit_grinder.py --setup htf --pick2-min-trigger 0.3 --min-gain 0.03
"""

import argparse
import os
import sys
import time
import json
import pickle

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import numpy as np
import pandas as pd
from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Worktree detection: resolve to main repo for data/cache access
_claude_marker = os.sep + ".claude" + os.sep
if _claude_marker in REPO_ROOT:
    REPO_ROOT = os.environ.get(
        "SCANPERFECT_REPO_ROOT",
        REPO_ROOT[:REPO_ROOT.index(_claude_marker)],
    )

# READ_ROOT: where we read inputs from (db, pyramid conditions). When running the
# grinder from a sibling worktree, set SCANPERFECT_READ_ROOT to the main repo path
# so input lookups find the live db and pyramid JSONs. Writes stay at REPO_ROOT.
READ_ROOT = os.environ.get("SCANPERFECT_READ_ROOT", REPO_ROOT)

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from expr_cache_builder import ExprSeriesCache

# ============================================================
# Config
# ============================================================
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.environ.get("SCANPERFECT_CACHE_DIR", os.path.join(LOCAL_DIR, "cache"))
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD_DEFAULT = 120
DEFAULT_WORKERS = os.cpu_count() or 8
N_THRESHOLDS = 20

def _get_setup_direction(setup_type):
    """Look up trade direction from the local setups table."""
    import sqlite3
    db_path = os.path.join(READ_ROOT, "data", "scanperfect.db")
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT direction FROM setups WHERE setup_type=?", (setup_type,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Setup '{setup_type}' not found in setups table")
    return row[0]


# ============================================================
# Data Classes
# ============================================================
@dataclass
class ExampleSignal:
    """An example with its deduplicated signal bar."""
    id: int
    ticker: str
    entry_date: str
    signal_date: str
    signal_bar_idx: int  # OHLCV-relative scan candle = entry - 1
    cache_signal_idx: int  # expression-cache-relative index for signal date
    signal_close: float
    adr_at_signal: float
    mfe_adr: float  # max favorable excursion in ADR from signal close
    n_forward: int  # bars available after signal


@dataclass
class ExitCandidate:
    """A candidate exit condition scored across all examples.

    Per-example arrays (exit_bars, move_adrs, capture_effs) are length n_examples
    and aligned to the example_signals list. Entries are None for examples where
    the condition did not trigger. Aggregate fields are computed over triggered
    examples only.
    """
    expression: str
    direction: str       # ">=" or "<="
    threshold: float
    exit_bars: list      # length n_examples, None where not triggered
    move_adrs: list      # length n_examples, None where not triggered
    capture_effs: list   # length n_examples, None where not triggered
    n_triggered: int
    floor_adr: float
    median_adr: float
    mean_adr: float
    floor_capture_eff: float
    median_capture_eff: float
    mean_capture_eff: float
    avg_bars_to_exit: float


# ============================================================
# Data Loading
# ============================================================
def load_daily_cache():
    # Try worktree CACHE_DIR first, then fall back to READ_ROOT/local_runner/cache
    # (common when expression cache is sandboxed in worktree but OHLCV stays in main).
    candidates = []
    for base in (CACHE_DIR, os.path.join(READ_ROOT, "local_runner", "cache")):
        for name in ("universe_ohlcv_daily.pkl", "universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
            candidates.append(os.path.join(base, name))
    path = None
    for p in candidates:
        if os.path.exists(p):
            path = p
            break
    if path is None:
        raise FileNotFoundError(
            f"OHLCV pickle not found. Tried: {candidates}"
        )
    print(f"  Loading daily cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_pyramid_conditions(setup_type):
    """Load signal conditions from pyramid results (same logic as signal_filter)."""
    import glob
    search_dirs = [
        os.path.join(READ_ROOT, "local_runner", "cache"),
        os.path.join(READ_ROOT, "data"),
    ]
    candidates = []
    for d in search_dirs:
        exact = os.path.join(d, f"pyramid_results_{setup_type}.json")
        if os.path.exists(exact):
            candidates.append(exact)
        pattern = os.path.join(d, f"pyramid_{setup_type}_*.json")
        candidates.extend(glob.glob(pattern))

    if not candidates:
        raise FileNotFoundError(f"No pyramid results found for {setup_type}")

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    best = candidates[0]
    with open(best) as f:
        data = json.load(f)
    conditions = data.get("all_conditions", [])
    total = data.get("summary", {}).get("final_total", "?")
    print(f"  Loaded {len(conditions)} conditions from {os.path.basename(best)}")
    print(f"  Grinder result: {total} total signals")
    return conditions


def load_examples(setup_type):
    """Load validated examples from local SQLite."""
    import sqlite3
    db_path = os.path.join(READ_ROOT, "data", "scanperfect.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker",
        (setup_type,)
    ).fetchall()
    conn.close()
    examples = [{"ticker": r["ticker"], "entry_date": r["entry_date"]} for r in rows]
    print(f"  Loaded {len(examples)} examples from local DB")
    return examples


# ============================================================
# Example Signal Bar Resolution
# ============================================================
def _load_earnings_map():
    """{ticker: sorted list of earnings-date strings 'YYYY-MM-DD'}"""
    import sqlite3
    db_path = os.path.join(READ_ROOT, "data", "scanperfect.db")
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    except Exception:
        conn.close()
        return {}
    conn.close()
    m = {}
    for ticker, date in rows:
        m.setdefault(ticker, []).append(date)
    for t in m:
        m[t].sort()
    return m


def _bars_to_next_earnings(df, scan_idx, earnings_list):
    """Trading bars from scan_idx+1 to first earnings date > signal_date.
    Returns None if no future earnings found in the cached data window.
    """
    if not earnings_list:
        return None
    signal_date = str(df["date"].values[scan_idx])[:10]
    dates_after = [str(d)[:10] for d in df["date"].values[scan_idx + 1:]]
    for ed in earnings_list:
        if ed > signal_date:
            for i, d in enumerate(dates_after):
                if d >= ed:
                    return i + 1
            return None
    return None


def resolve_example_signals(examples, cache, conditions, expr_cache, direction,
                            max_forward, earnings_cap=False, earnings_buffer=1,
                            earnings_map=None):
    """
    For each example, find the deduplicated signal bar (scan candle = entry - 1),
    verify all conditions pass via expression cache, compute MFE from signal bar.

    earnings_cap: if True, truncate n_forward to (bars-to-next-earnings - earnings_buffer).
                  Examples without earnings data are dropped.
    earnings_buffer: exit N bars BEFORE earnings. 1 = exit on the bar before earnings bar.

    Returns list of ExampleSignal.
    """
    if earnings_cap and earnings_map is None:
        earnings_map = _load_earnings_map()
    # Map condition names to cache column indices
    cond_col_indices = []
    for cond in conditions:
        col_idx = expr_cache.expr_index(cond["name"])
        cond_col_indices.append(col_idx)

    adr_col_idx = expr_cache.expr_index("adr14")

    results = []
    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entry_date")
        df = cache.get(ticker)
        if df is None:
            print(f"    {ticker}: not in OHLCV cache, skipping")
            continue

        # Find entry bar
        dates_str = [str(d)[:10] for d in df["date"].values]
        if entry_date not in dates_str:
            print(f"    {ticker}: entry date {entry_date} not found")
            continue

        entry_idx = dates_str.index(entry_date)
        scan_idx = entry_idx - 1  # signal bar = day before entry
        if scan_idx < 1:
            print(f"    {ticker}: signal bar too early (idx {scan_idx})")
            continue

        # Load expression cache — find signal bar by date, not OHLCV index
        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None:
            print(f"    {ticker}: not in expression cache, skipping")
            continue
        signal_date = dates_str[scan_idx]
        cache_dates_str = [str(d)[:10] for d in cached_dates]
        if signal_date not in cache_dates_str:
            print(f"    {ticker}: signal date {signal_date} not in expr cache")
            continue
        cache_scan_idx = cache_dates_str.index(signal_date)

        # Check how many conditions pass at signal bar (informational for examples)
        n_fail = 0
        for i, cond in enumerate(conditions):
            col_idx = cond_col_indices[i]
            if col_idx is None:
                n_fail += 1
                continue
            val = cached_data[cache_scan_idx, col_idx]
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                n_fail += 1

        if n_fail > 0:
            # Examples have confirmed entry dates — signal bar is ground truth.
            # Condition mismatches are expected when conditions are from a different
            # pyramid grind or the expression cache was rebuilt since.
            print(f"    {ticker}: {n_fail}/{len(conditions)} conditions off (proceeding — example entry date is ground truth)")

        # ADR at signal bar
        if adr_col_idx is not None:
            adr_val = float(cached_data[cache_scan_idx, adr_col_idx])
        else:
            h = df["high"].values
            l = df["low"].values
            start = max(0, scan_idx - 13)
            adr_val = float(np.mean(h[start:scan_idx+1] - l[start:scan_idx+1]))

        if adr_val <= 0 or np.isnan(adr_val):
            print(f"    {ticker}: invalid ADR at signal bar")
            continue

        signal_close = float(df["close"].values[scan_idx])
        n_available = len(df) - scan_idx - 1
        actual_forward = min(max_forward, n_available)

        if earnings_cap:
            e_bars = _bars_to_next_earnings(
                df, scan_idx, (earnings_map or {}).get(ticker, [])
            )
            if e_bars is None:
                print(f"    {ticker}: no earnings data — excluded under --earnings-cap")
                continue
            # exit earnings_buffer bars BEFORE the earnings bar
            capped = max(e_bars - earnings_buffer, 1)
            actual_forward = min(actual_forward, capped)

        if actual_forward < 5:
            print(f"    {ticker}: only {actual_forward} forward bars from signal")
            continue

        # Compute MFE from signal close (in ADR)
        if direction == "short":
            fwd_lows = df["low"].values[scan_idx + 1: scan_idx + actual_forward + 1]
            mfe_price = float(np.min(fwd_lows))
            mfe_adr = (signal_close - mfe_price) / adr_val
        else:
            fwd_highs = df["high"].values[scan_idx + 1: scan_idx + actual_forward + 1]
            mfe_price = float(np.max(fwd_highs))
            mfe_adr = (mfe_price - signal_close) / adr_val

        results.append(ExampleSignal(
            id=ex.get("id"),
            ticker=ticker,
            entry_date=entry_date,
            signal_date=dates_str[scan_idx],
            signal_bar_idx=scan_idx,
            cache_signal_idx=cache_scan_idx,
            signal_close=round(signal_close, 2),
            adr_at_signal=round(adr_val, 4),
            mfe_adr=round(mfe_adr, 2),
            n_forward=actual_forward,
        ))
        print(f"    {ticker}: signal {dates_str[scan_idx]} → MFE {mfe_adr:.1f} ADR")

    print(f"  Resolved {len(results)}/{len(examples)} examples with valid signal bars")
    return results


# ============================================================
# Forward Matrix Build (from expression cache)
# ============================================================
def build_forward_matrices(example_signals, expr_cache, n_expressions):
    """
    For each example, extract expression values for all forward bars from cache.
    
    Returns: list of (n_forward, n_expressions) arrays, one per example.
    All values come from expression cache — same data path as signal grinder.
    """
    matrices = []
    for ex in example_signals:
        cached_dates, cached_data = expr_cache.get_ticker(ex.ticker)
        if cached_dates is None:
            matrices.append(None)
            continue

        # Extract forward window in cache-relative coordinates
        start = ex.cache_signal_idx + 1
        end = start + ex.n_forward
        if end > len(cached_data):
            end = len(cached_data)

        fwd_data = cached_data[start:end].copy()  # (n_forward, n_expressions)
        matrices.append(fwd_data)

    valid = sum(1 for m in matrices if m is not None)
    print(f"  Built forward matrices: {valid}/{len(example_signals)} valid")
    return matrices


# ============================================================
# Core Grinder
# ============================================================
def generate_thresholds(values, n_thresholds=N_THRESHOLDS):
    """Generate threshold values from data distribution."""
    clean = values[~np.isnan(values)]
    if len(clean) < 5:
        return []
    pcts = np.linspace(5, 95, n_thresholds)
    thresholds = np.percentile(clean, pcts)
    seen = set()
    result = []
    for t in thresholds:
        t_round = round(float(t), 6)
        if t_round not in seen:
            seen.add(t_round)
            result.append(t_round)
    return result


def grind_signal_exits(example_signals, forward_matrices, expr_names,
                       ohlcv_cache, direction, n_thresholds=N_THRESHOLDS,
                       min_bar=1):
    """
    Brute-force all cache expressions × thresholds × directions.

    For each candidate, walk every example forward from its signal bar to find the
    first trigger. Per-example arrays are aligned to example_signals (length
    n_examples) with None entries where the condition did not trigger. Aggregates
    are computed over triggered examples only.

    Returns every candidate with at least one trigger. Downstream selection
    (select_pick_1 / select_pick_2) applies the 100% filter for pick 1 and the
    relaxed-coverage filter for pick 2.
    """
    n_examples = len(example_signals)
    n_exprs = len(expr_names)

    # Pre-build forward close arrays for move computation
    fwd_closes = []
    for ex in example_signals:
        df = ohlcv_cache.get(ex.ticker)
        start = ex.signal_bar_idx + 1
        end = start + ex.n_forward
        fwd_closes.append(df["close"].values[start:end].astype(np.float64))

    print(f"\n  Grinding {n_exprs:,} expressions × ~{n_thresholds} thresholds × 2 directions")
    print(f"  Walking all {n_examples} examples per candidate (no early-break).")

    t0 = time.time()
    tested = 0
    passed = 0
    all_candidates = []

    for expr_i in range(n_exprs):
        if (expr_i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"    [{expr_i+1}/{n_exprs}] {rate:.0f} expr/s, "
                  f"{passed} candidates kept, {tested:,} tested")

        # Gather forward values for this expression across all examples
        example_series = []
        all_values = []
        for i, matrix in enumerate(forward_matrices):
            if matrix is None:
                continue
            series = matrix[:, expr_i]
            example_series.append((i, series))
            vals = series[~np.isnan(series)]
            if len(vals) > 0:
                all_values.append(vals)

        if len(example_series) < n_examples:
            continue  # can't align per-example arrays if some examples have no data

        if not all_values:
            continue

        combined = np.concatenate(all_values)
        thresholds = generate_thresholds(combined, n_thresholds)
        if not thresholds:
            continue

        expr_name = expr_names[expr_i]

        for thresh in thresholds:
            for dir_label, dir_op in [(">=", "ge"), ("<=", "le")]:
                tested += 1
                # Per-example arrays, length n_examples, None for non-triggers
                per_bars = [None] * n_examples
                per_moves = [None] * n_examples
                per_effs = [None] * n_examples
                triggered = 0

                for ex_idx, series in example_series:
                    ex = example_signals[ex_idx]
                    closes = fwd_closes[ex_idx]

                    # Opposite-side-at-entry: if the indicator is already past the
                    # threshold at the entry bar (series[0] = first post-signal bar),
                    # this example is "pre-extended" and a forward trigger is just
                    # continuation noise, not a meaningful exit signal. Skip as non-trigger.
                    if len(series) > 0:
                        val0 = series[0]
                        if not np.isnan(val0):
                            pre_extended = (val0 >= thresh) if dir_op == "ge" else (val0 <= thresh)
                            if pre_extended:
                                continue

                    for bar in range(min_bar, len(series)):
                        val = series[bar]
                        if np.isnan(val):
                            continue
                        hit = (val >= thresh) if dir_op == "ge" else (val <= thresh)
                        if hit:
                            if bar < len(closes):
                                exit_close = float(closes[bar])
                                if direction == "short":
                                    move = (ex.signal_close - exit_close) / ex.adr_at_signal
                                else:
                                    move = (exit_close - ex.signal_close) / ex.adr_at_signal
                                cap_eff = move / ex.mfe_adr if ex.mfe_adr > 0 else 0.0
                                per_bars[ex_idx] = bar
                                per_moves[ex_idx] = move
                                per_effs[ex_idx] = cap_eff
                                triggered += 1
                            break

                if triggered == 0:
                    continue

                passed += 1
                trig_moves = [m for m in per_moves if m is not None]
                trig_effs = [e for e in per_effs if e is not None]
                trig_bars = [b for b in per_bars if b is not None]
                all_candidates.append(ExitCandidate(
                    expression=expr_name,
                    direction=dir_label,
                    threshold=round(thresh, 6),
                    exit_bars=per_bars,
                    move_adrs=[round(m, 4) if m is not None else None for m in per_moves],
                    capture_effs=[round(e, 4) if e is not None else None for e in per_effs],
                    n_triggered=triggered,
                    floor_adr=round(min(trig_moves), 4),
                    median_adr=round(float(np.median(trig_moves)), 4),
                    mean_adr=round(float(np.mean(trig_moves)), 4),
                    floor_capture_eff=round(min(trig_effs), 4),
                    median_capture_eff=round(float(np.median(trig_effs)), 4),
                    mean_capture_eff=round(float(np.mean(trig_effs)), 4),
                    avg_bars_to_exit=round(float(np.mean(trig_bars)), 1),
                ))

    elapsed = time.time() - t0
    print(f"\n  Done: tested {tested:,} conditions in {elapsed:.1f}s")
    print(f"  Candidates with at least one trigger: {passed:,}")

    # Primary sort: median capture efficiency (desc), median ADR (desc). Downstream
    # picks filter further (pick 1 = 100% coverage only).
    all_candidates.sort(
        key=lambda c: (c.median_capture_eff, c.median_adr),
        reverse=True
    )
    return all_candidates


# ============================================================
# Dual-Exit OR-Set Selection
# ============================================================
def or_combined_capture_effs(picks, n_examples):
    """For each example, return the capture_eff from whichever pick fires earliest.

    picks: list of ExitCandidate (ordered). picks[0] is expected to have full
    coverage (pick 1). Later picks may have None bars where they didn't trigger.
    """
    result = []
    for i in range(n_examples):
        best_bar = None
        best_eff = None
        for p in picks:
            b = p.exit_bars[i]
            if b is None:
                continue
            if best_bar is None or b < best_bar:
                best_bar = b
                best_eff = p.capture_effs[i]
        result.append(best_eff)
    return result


def aggregate_on_indices(eff_list, indices, metric):
    """Mean or median of eff_list at given indices, skipping None entries."""
    vals = [eff_list[i] for i in indices if eff_list[i] is not None]
    if not vals:
        return 0.0
    return float(np.mean(vals)) if metric == "mean" else float(np.median(vals))


def build_forced_exit_candidate(example_signals, ohlcv_cache, direction):
    """Synthetic pick-0 under --earnings-cap: fires at the last available bar
    (earnings_cap - buffer) for every example. Serves as the OR-set baseline;
    additional picks must beat it via the existing defenses.
    """
    n = len(example_signals)
    per_bars = [None] * n
    per_moves = [None] * n
    per_effs = [None] * n
    for i, ex in enumerate(example_signals):
        df = ohlcv_cache.get(ex.ticker)
        if df is None or ex.n_forward < 1:
            continue
        last_bar = ex.n_forward  # 1-indexed: last post-signal bar
        exit_close = float(df["close"].values[ex.signal_bar_idx + last_bar])
        if direction == "short":
            move = (ex.signal_close - exit_close) / ex.adr_at_signal
        else:
            move = (exit_close - ex.signal_close) / ex.adr_at_signal
        eff = move / ex.mfe_adr if ex.mfe_adr > 0 else 0.0
        per_bars[i] = last_bar
        per_moves[i] = round(move, 4)
        per_effs[i] = round(eff, 4)

    trig_moves = [m for m in per_moves if m is not None]
    trig_effs = [e for e in per_effs if e is not None]
    trig_bars = [b for b in per_bars if b is not None]
    return ExitCandidate(
        expression="earnings_forced_exit",
        direction="force",
        threshold=0.0,
        exit_bars=per_bars,
        move_adrs=per_moves,
        capture_effs=per_effs,
        n_triggered=len(trig_bars),
        floor_adr=round(min(trig_moves), 4) if trig_moves else 0.0,
        median_adr=round(float(np.median(trig_moves)), 4) if trig_moves else 0.0,
        mean_adr=round(float(np.mean(trig_moves)), 4) if trig_moves else 0.0,
        floor_capture_eff=round(min(trig_effs), 4) if trig_effs else 0.0,
        median_capture_eff=round(float(np.median(trig_effs)), 4) if trig_effs else 0.0,
        mean_capture_eff=round(float(np.mean(trig_effs)), 4) if trig_effs else 0.0,
        avg_bars_to_exit=round(float(np.mean(trig_bars)), 1) if trig_bars else 0.0,
    )


def select_pick_1(candidates, n_examples):
    """The best 100%-coverage candidate by median capture_eff (tiebreaker: median ADR)."""
    full_coverage = [c for c in candidates if c.n_triggered == n_examples]
    if not full_coverage:
        return None
    full_coverage.sort(key=lambda c: (c.median_capture_eff, c.median_adr), reverse=True)
    return full_coverage[0]


def select_next_pick(current_picks, candidates, n_examples, args, train_idx, holdout_idx, metric):
    """Greedy pick the next exit to add to the OR-set.

    Defenses applied to each candidate before scoring:
      - Not already in current_picks
      - Trigger rate >= args.pick2_min_trigger
      - Not redundant with ANY current pick (<=80% overlap within 2 bars across mutual triggers)

    Scoring: compute the OR-combined train/holdout aggregate when the candidate is
    added to current_picks. Gate: train_gain >= args.min_gain AND
    holdout_gain >= args.min_holdout_gain. Pick highest train_gain among passers.

    Returns (pick_or_None, report_dict, rejected_list).
    """
    baseline_effs = or_combined_capture_effs(current_picks, n_examples)
    baseline_train = aggregate_on_indices(baseline_effs, train_idx, metric)
    baseline_holdout = aggregate_on_indices(baseline_effs, holdout_idx, metric)

    picked_keys = {(p.expression, p.direction, p.threshold) for p in current_picks}
    eligible = []
    rejected = []

    for c in candidates:
        if (c.expression, c.direction, c.threshold) in picked_keys:
            continue

        trigger_rate = c.n_triggered / n_examples
        if trigger_rate < args.pick2_min_trigger:
            rejected.append((c, "trigger_rate_too_low", round(trigger_rate, 3)))
            continue

        redundant = False
        worst_overlap = 0.0
        for p in current_picks:
            overlap = 0
            both_triggered = 0
            for i in range(n_examples):
                bp = p.exit_bars[i]
                bc = c.exit_bars[i]
                if bp is not None and bc is not None:
                    both_triggered += 1
                    if abs(bc - bp) <= 2:
                        overlap += 1
            if both_triggered >= 3 and overlap / both_triggered > 0.8:
                redundant = True
                worst_overlap = max(worst_overlap, overlap / both_triggered)
                break
        if redundant:
            rejected.append((c, "redundant_with_existing_pick", round(worst_overlap, 3)))
            continue

        eligible.append(c)

    scored = []
    for c in eligible:
        effs_or = or_combined_capture_effs(current_picks + [c], n_examples)
        train_agg = aggregate_on_indices(effs_or, train_idx, metric)
        holdout_agg = aggregate_on_indices(effs_or, holdout_idx, metric)
        train_gain = train_agg - baseline_train
        holdout_gain = holdout_agg - baseline_holdout
        scored.append((c, train_agg, holdout_agg, train_gain, holdout_gain))

    passing = [s for s in scored if s[3] >= args.min_gain and s[4] >= args.min_holdout_gain]
    failed_gain = [s for s in scored if s[3] < args.min_gain]
    failed_holdout = [s for s in scored if s[3] >= args.min_gain and s[4] < args.min_holdout_gain]

    failed_holdout.sort(key=lambda s: s[3], reverse=True)
    for c, t_agg, h_agg, t_gain, h_gain in failed_holdout[:5]:
        rejected.append((c, "negative_holdout_gain", round(h_gain, 4)))
    failed_gain.sort(key=lambda s: s[3], reverse=True)
    for c, t_agg, h_agg, t_gain, h_gain in failed_gain[:5]:
        rejected.append((c, "insufficient_gain", round(t_gain, 4)))

    common_report = {
        "baseline_train": round(baseline_train, 4),
        "baseline_holdout": round(baseline_holdout, 4),
        "n_eligible_after_defenses": len(eligible),
        "n_passing_gates": len(passing),
        "n_failed_gain": len(failed_gain),
        "n_failed_holdout": len(failed_holdout),
    }

    if not passing:
        if not eligible:
            reason = "no_eligible_candidates_after_defenses"
        elif len(failed_gain) == len(scored):
            reason = "no_candidate_met_min_gain"
        else:
            reason = "all_gain_passers_failed_holdout"
        return None, {**common_report, "selected": False, "reason": reason}, rejected[:10]

    passing.sort(key=lambda s: (s[3], s[4]), reverse=True)
    best_c, best_train, best_holdout, best_gain, best_holdout_gain = passing[0]

    alt_seeds = [s for s in [7, 13, 31, 101] if s != args.holdout_seed]
    robustness = []
    new_picks = current_picks + [best_c]
    effs_or_best = or_combined_capture_effs(new_picks, n_examples)
    n_train = len(train_idx)
    for s in alt_seeds:
        rng2 = np.random.default_rng(s)
        alt_idx = list(range(n_examples))
        rng2.shuffle(alt_idx)
        alt_train = sorted(alt_idx[:n_train])
        alt_holdout = sorted(alt_idx[n_train:])
        alt_base = or_combined_capture_effs(current_picks, n_examples)
        alt_base_train = aggregate_on_indices(alt_base, alt_train, metric)
        alt_base_holdout = aggregate_on_indices(alt_base, alt_holdout, metric)
        alt_pick_train = aggregate_on_indices(effs_or_best, alt_train, metric)
        alt_pick_holdout = aggregate_on_indices(effs_or_best, alt_holdout, metric)
        robustness.append({
            "seed": s,
            "train_gain": round(alt_pick_train - alt_base_train, 4),
            "holdout_gain": round(alt_pick_holdout - alt_base_holdout, 4),
        })
    n_pos_holdout = sum(1 for r in robustness if r["holdout_gain"] > 0)

    report = {
        **common_report,
        "selected": True,
        "pick_train": round(best_train, 4),
        "pick_holdout": round(best_holdout, 4),
        "train_gain": round(best_gain, 4),
        "holdout_gain": round(best_holdout_gain, 4),
        "alt_seed_robustness": robustness,
        "n_alt_seeds_with_positive_holdout": n_pos_holdout,
        "n_alt_seeds_total": len(alt_seeds),
    }
    return best_c, report, rejected[:10]


def select_or_exit_set(pick_1, candidates, n_examples, args):
    """Greedy-build the OR-exit set: pick 1 is the 100%-coverage baseline, then
    repeatedly add picks that improve OR-combined capture (both train and holdout)
    until either max_picks is reached or no candidate qualifies.

    Returns (picks_list, reports_list, rejected_by_pick_list).
    """
    if pick_1 is None:
        return [], [], []

    # Fixed train/holdout split, same seed across all iterations
    rng = np.random.default_rng(args.holdout_seed)
    idx = list(range(n_examples))
    rng.shuffle(idx)
    n_train = int(round(n_examples * 0.7))
    train_idx = sorted(idx[:n_train])
    holdout_idx = sorted(idx[n_train:])
    metric = args.aggregate_metric

    picks = [pick_1]
    reports = []
    rejected_by_pick = []

    while len(picks) < args.max_picks:
        next_pick, report, rej = select_next_pick(
            picks, candidates, n_examples, args, train_idx, holdout_idx, metric)
        # Attach split metadata on the first call for reporting
        report = {
            "pick_index": len(picks),
            "split_seed": args.holdout_seed,
            "n_train": n_train,
            "n_holdout": n_examples - n_train,
            "aggregate_metric": metric,
            "pick2_min_trigger": args.pick2_min_trigger,
            "min_gain": args.min_gain,
            "min_holdout_gain": args.min_holdout_gain,
            **report,
        }
        reports.append(report)
        rejected_by_pick.append(rej)
        if next_pick is None:
            break
        picks.append(next_pick)

    return picks, reports, rejected_by_pick


def format_pick(candidate, pick_index, n_examples):
    """Serialize an ExitCandidate to a dict for JSON output."""
    return {
        "pick_index": pick_index,
        "expression": candidate.expression,
        "direction": candidate.direction,
        "threshold": candidate.threshold,
        "trigger_rate": round(candidate.n_triggered / n_examples, 4),
        "n_examples_triggered": candidate.n_triggered,
        "floor_efficiency": candidate.floor_capture_eff,
        "median_efficiency": candidate.median_capture_eff,
        "mean_efficiency": candidate.mean_capture_eff,
        "floor_adr": candidate.floor_adr,
        "median_adr": candidate.median_adr,
        "mean_adr": candidate.mean_adr,
        "avg_bars_to_exit": candidate.avg_bars_to_exit,
        "per_example_efficiency": candidate.capture_effs,
        "per_example_adr": candidate.move_adrs,
        "per_example_exit_bars": candidate.exit_bars,
    }


# ============================================================
# Display
# ============================================================
def print_results(picks, reports, example_signals):
    """Print each pick in the OR-exit set with its defense/score metadata."""
    n = len(example_signals)
    if not picks:
        print("\n  NO PICKS — no candidate with 100% example coverage.")
        return
    for i, p in enumerate(picks):
        tr_pct = 100.0 * p.n_triggered / n
        label = "100% coverage" if i == 0 else f"{tr_pct:.0f}% coverage"
        print(f"\n  PICK {i} ({label}): {p.expression} {p.direction} {p.threshold}")
        print(f"    Median capture eff (on triggered): {p.median_capture_eff:.3f}")
        print(f"    Median ADR move (on triggered):    {p.median_adr:.2f}")
        print(f"    Avg bars to exit (on triggered):   {p.avg_bars_to_exit:.0f}")
        if i >= 1 and i - 1 < len(reports):
            r = reports[i - 1]
            if r.get("selected"):
                print(f"    OR-combined train  aggregate: {r['pick_train']:.3f}  (baseline {r['baseline_train']:.3f}, gain +{r['train_gain']:.3f})")
                print(f"    OR-combined holdout aggregate: {r['pick_holdout']:.3f}  (baseline {r['baseline_holdout']:.3f}, gain {r['holdout_gain']:+.3f})")
                alt = r.get("alt_seed_robustness", [])
                if alt:
                    pos = r.get("n_alt_seeds_with_positive_holdout", 0)
                    total = r.get("n_alt_seeds_total", len(alt))
                    print(f"    Alt-seed holdout: {pos}/{total} positive  " +
                          ", ".join(f"seed {a['seed']}: {a['holdout_gain']:+.3f}" for a in alt))
    # Trailing report for the stop condition if we stopped short of max_picks
    if len(reports) > len(picks) - 1:
        stop = reports[-1]
        if not stop.get("selected"):
            print(f"\n  STOPPED after pick {len(picks) - 1}: {stop.get('reason', 'unknown')}  "
                  f"(eligible={stop.get('n_eligible_after_defenses', 0)}, "
                  f"passed_gain={stop.get('n_passing_gates', 0) + stop.get('n_failed_holdout', 0)}, "
                  f"passed_holdout={stop.get('n_passing_gates', 0)})")


# ============================================================
# Save
# ============================================================
def save_results(candidates, picks, reports, rejected_by_pick,
                 example_signals, setup_type, meta, no_upload=False, tag_suffix=""):
    """Save schema v2 output. or_exit_set contains every pick; reports contains
    one entry per attempted pick (including the final one that stopped the loop).
    top_conditions stays as a length-up-to-50 ranked 100%-coverage list for back-compat.
    """
    out_dir = os.path.join(REPO_ROOT, "data", "signal_exit_grind")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_examples = len(example_signals)

    or_exit_set = [format_pick(p, i, n_examples) for i, p in enumerate(picks)]
    pick_1 = picks[0] if picks else None

    full_coverage = [c for c in candidates if c.n_triggered == n_examples]
    full_coverage.sort(key=lambda c: (c.median_capture_eff, c.median_adr), reverse=True)
    top_conditions = [format_pick(c, i, n_examples) for i, c in enumerate(full_coverage[:50])]

    # Flatten rejected_by_pick into a single list with pick_index tag
    rejected_serialized = []
    for pick_idx, rej in enumerate(rejected_by_pick):
        for c, reason, value in (rej or [])[:10]:
            rejected_serialized.append({
                "for_pick_index": pick_idx + 1,  # rejected candidates were competing for the (pick_idx+1)th slot
                "expression": c.expression,
                "direction": c.direction,
                "threshold": c.threshold,
                "trigger_rate": round(c.n_triggered / n_examples, 4),
                "median_efficiency": c.median_capture_eff,
                "avg_bars_to_exit": c.avg_bars_to_exit,
                "rejection_reason": reason,
                "rejection_value": value,
            })

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "grinder_type": "signal_exit",
        "schema_version": 2,
        "computation_path": "expression_cache_only",
        "n_examples": n_examples,
        "n_expressions_tested": meta.get("n_expressions", 0),
        "n_candidates_found": len(candidates),
        "n_candidates_full_coverage": len(full_coverage),
        "n_picks": len(picks),
        "examples": [
            {
                "ticker": ex.ticker,
                "signal_date": ex.signal_date,
                "entry_date": ex.entry_date,
                "signal_close": ex.signal_close,
                "adr_at_signal": ex.adr_at_signal,
                "mfe_adr": ex.mfe_adr,
            }
            for ex in example_signals
        ],
        "or_exit_set": or_exit_set,
        "pick_reports": reports,
        "rejected_candidates": rejected_serialized,
        "top_conditions": top_conditions,
        "grinder_settings": {
            "min_gain": meta.get("min_gain"),
            "min_holdout_gain": meta.get("min_holdout_gain"),
            "pick2_min_trigger": meta.get("pick2_min_trigger"),
            "aggregate_metric": meta.get("aggregate_metric"),
            "holdout_seed": meta.get("holdout_seed"),
            "max_picks": meta.get("max_picks"),
            "max_forward": meta.get("max_forward"),
        },
    }

    floor_tag = f"{pick_1.floor_adr:+.1f}" if pick_1 else "na"
    ts_path = os.path.join(out_dir,
        f"signal_exit_{setup_type}{tag_suffix}_{n_examples}ex_{floor_tag}adr_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Saved: {ts_path}")

    latest_path = os.path.join(out_dir, f"signal_exit_{setup_type}{tag_suffix}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {latest_path}")

    if no_upload:
        print(f"  [--no-upload] skipping file_mirror and Railway upload (worktree mode)")
    else:
        from file_mirror import mirror_file
        mirror_file(ts_path)
        mirror_file(latest_path)
        if pick_1 is not None:
            _upload_exit_to_railway(setup_type, pick_1,
                                    args_max_forward=meta.get("max_forward", MAX_FORWARD_DEFAULT))

    return latest_path


def _upload_exit_to_railway(setup_type, best_candidate, args_max_forward=MAX_FORWARD_DEFAULT):
    """Upload best exit condition to Railway exit_conditions table."""
    import requests

    # Map direction format: ">=" -> "above", "<=" -> "below"
    dir_map = {">=": "above", "<=": "below"}
    railway_dir = dir_map.get(best_candidate.direction, best_candidate.direction)

    payload = {
        "setup_type": setup_type,
        "expression_name": best_candidate.expression,
        "direction": railway_dir,
        "threshold": best_candidate.threshold,
        "max_forward_bars": args_max_forward,
        "adr_threshold_multiplier": 1.0,
    }

    print(f"\n  ── EXIT UPLOAD TO RAILWAY ──")
    print(f"  {payload['expression_name']} {railway_dir} {payload['threshold']}")
    print(f"  max_forward_bars: {args_max_forward}")

    try:
        r = requests.post(f"{RAILWAY_URL}/api/v2/exit_conditions", json=payload, timeout=30)
        r.raise_for_status()
        print(f"  ✓ Railway exit condition updated")

        # Verify
        r2 = requests.get(f"{RAILWAY_URL}/api/v2/exit_conditions/{setup_type}", timeout=30)
        r2.raise_for_status()
        stored = r2.json().get("exit_condition", {})
        if stored.get("expression_name") == payload["expression_name"] and \
           stored.get("direction") == railway_dir and \
           abs(stored.get("threshold", 0) - payload["threshold"]) < 1e-4:
            print(f"  ✓ Verified: Railway matches local")
        else:
            print(f"  ⚠ MISMATCH — Railway: {stored}")
            print(f"           Local:   {payload}")
    except Exception as e:
        print(f"  ⚠ Railway upload failed: {e}")
        print(f"  Local file saved — manual upload needed")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Signal Exit Grinder — dual-exit OR-set (schema v2)")
    parser.add_argument("--setup", required=True, help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT,
                        help="Max forward bars from signal (default: 120)")
    parser.add_argument("--n-thresholds", type=int, default=N_THRESHOLDS,
                        help="Thresholds per expression (default: 20)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="(Stage 1b) Reserved for ProcessPoolExecutor wiring")
    parser.add_argument("--top-n", type=int, default=50,
                        help="Top N full-coverage candidates saved in top_conditions (backward compat)")
    parser.add_argument("--conditions-file", type=str, default=None,
                        help="JSON with pre-supplied signal conditions (bypasses load_pyramid_conditions)")
    # Dual-exit flags
    parser.add_argument("--min-gain", type=float, default=0.02,
                        help="Minimum OR-combined train aggregate gain to include pick 2 (default: 0.02)")
    parser.add_argument("--min-holdout-gain", type=float, default=0.0,
                        help="Minimum OR-combined holdout aggregate gain required to include pick 2 (default: 0.0)")
    parser.add_argument("--pick2-min-trigger", type=float, default=0.25,
                        help="Pick 2 must trigger on at least this fraction of examples (default: 0.25)")
    parser.add_argument("--aggregate-metric", choices=["mean", "median"], default="mean",
                        help="Aggregate metric for OR-combined capture (default: mean)")
    parser.add_argument("--holdout-seed", type=int, default=42,
                        help="Random seed for 70/30 holdout split (default: 42)")
    parser.add_argument("--max-picks", type=int, default=5,
                        help="Maximum size of the OR-exit set including pick 1 (default: 5)")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip Railway upload and file_mirror (use for worktree test runs)")
    parser.add_argument("--earnings-cap", action="store_true",
                        help="Cap forward window per-example at (next-earnings-date - buffer). "
                             "Excludes examples without earnings data. Use for realistic-hold scoring.")
    parser.add_argument("--earnings-buffer", type=int, default=1,
                        help="Exit this many bars BEFORE the earnings bar (default: 1)")
    parser.add_argument("--exclude-example", type=str, default=None,
                        help="Skip one example identified as 'TICKER:YYYY-MM-DD' (entry date). "
                             "For leave-one-out stability testing.")
    args = parser.parse_args()

    setup = args.setup
    direction = _get_setup_direction(setup)

    print(f"\n{'='*60}")
    print(f"  SIGNAL EXIT GRINDER — {setup.upper()}  [schema v2 dual-exit]")
    print(f"  Expression cache only — same computation path as signal grinder")
    print(f"{'='*60}")
    t0 = time.time()

    # Load expression cache
    print(f"\n  Loading expression cache...")
    print(f"  REPO_ROOT (writes) = {REPO_ROOT}")
    print(f"  READ_ROOT (reads)  = {READ_ROOT}")
    print(f"  CACHE_DIR          = {CACHE_DIR}")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache not found or invalid.")
        print("  Run: python local_runner/expr_cache_builder.py --build")
        sys.exit(1)
    n_expressions = expr_cache.n_expressions
    expr_names = expr_cache.expr_names
    print(f"  Expression cache: {n_expressions:,} expressions")

    # Load data
    ohlcv_cache = load_daily_cache()
    if args.conditions_file:
        print(f"  Loading conditions from: {args.conditions_file}")
        with open(args.conditions_file) as _cf:
            _cond_data = json.load(_cf)
        conditions = _cond_data.get("all_conditions", [])
        print(f"  Loaded {len(conditions)} conditions from --conditions-file")
    else:
        conditions = load_pyramid_conditions(setup)
    examples = load_examples(setup)

    # Phase 1: Resolve example signal bars
    print(f"\n  PHASE 1: Resolve example signal bars")
    if args.earnings_cap:
        print(f"  [--earnings-cap] Forward window capped at earnings-date minus {args.earnings_buffer} bar(s)")
    example_signals = resolve_example_signals(
        examples, ohlcv_cache, conditions, expr_cache, direction, args.max_forward,
        earnings_cap=args.earnings_cap, earnings_buffer=args.earnings_buffer)

    if len(example_signals) < 3:
        print(f"  ERROR: Only {len(example_signals)} valid examples — need at least 3")
        sys.exit(1)

    # Phase 2: Build forward matrices from expression cache
    print(f"\n  PHASE 2: Build forward matrices from expression cache")
    forward_matrices = build_forward_matrices(example_signals, expr_cache, n_expressions)

    # Phase 3: Grind
    print(f"\n  PHASE 3: Grind exits")
    candidates = grind_signal_exits(
        example_signals, forward_matrices, expr_names,
        ohlcv_cache, direction,
        n_thresholds=args.n_thresholds,
    )

    # Phase 4: Build OR-exit set (pick 1 + greedy pick 2, pick 3, ...)
    print(f"\n  PHASE 4: Select OR-exit set  seed={args.holdout_seed}  max_picks={args.max_picks}")
    n_examples = len(example_signals)
    if args.earnings_cap:
        pick_1 = build_forced_exit_candidate(example_signals, ohlcv_cache, direction)
        print(f"  [--earnings-cap] PICK 0 = synthetic forced-earnings-exit baseline")
        print(f"    mean_capture_eff={pick_1.mean_capture_eff:.3f}  "
              f"median={pick_1.median_capture_eff:.3f}  "
              f"median_adr={pick_1.median_adr:.2f}  "
              f"avg_bars_to_exit={pick_1.avg_bars_to_exit:.1f}")
    else:
        pick_1 = select_pick_1(candidates, n_examples)
    picks, reports, rejected_by_pick = select_or_exit_set(pick_1, candidates, n_examples, args)

    print_results(picks, reports, example_signals)

    save_results(
        candidates, picks, reports, rejected_by_pick,
        example_signals, setup,
        {
            "n_expressions": n_expressions,
            "max_forward": args.max_forward,
            "min_gain": args.min_gain,
            "min_holdout_gain": args.min_holdout_gain,
            "pick2_min_trigger": args.pick2_min_trigger,
            "aggregate_metric": args.aggregate_metric,
            "holdout_seed": args.holdout_seed,
            "max_picks": args.max_picks,
            "earnings_cap": args.earnings_cap,
            "earnings_buffer": args.earnings_buffer,
        },
        no_upload=args.no_upload,
        tag_suffix=("_earnings" if args.earnings_cap else ""),
    )

    # Summary
    total_time = time.time() - t0
    full_cov = sum(1 for c in candidates if c.n_triggered == n_examples)
    print(f"\n{'='*60}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  Examples resolved: {n_examples}")
    print(f"  Expressions tested: {n_expressions:,}")
    print(f"  Candidates with >=1 trigger: {len(candidates)}")
    print(f"  Candidates with 100% coverage: {full_cov}")
    print(f"  OR-exit set size: {len(picks)}  (max {args.max_picks})")
    if args.no_upload:
        print(f"  [--no-upload] Railway/mirror skipped (worktree mode)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
