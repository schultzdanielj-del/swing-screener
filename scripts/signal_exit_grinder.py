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

RULES:
  - 100% example pass rate — hardcoded, no exceptions
  - Expression cache is the ONLY computation path — no live compute_series
  - Same data path as signal grinder + signal filter
  - Parallel across all cores

Usage:
    python scripts/signal_exit_grinder.py --setup dtss
    python scripts/signal_exit_grinder.py --setup dtss --max-forward 120 --workers 8
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
    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
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
    signal_bar_idx: int  # scan candle = entry - 1
    signal_close: float
    adr_at_signal: float
    mfe_adr: float  # max favorable excursion in ADR from signal close
    n_forward: int  # bars available after signal


@dataclass
class ExitCandidate:
    """A candidate exit condition scored across all examples."""
    expression: str
    direction: str       # ">=" or "<="
    threshold: float
    # Per-example results
    exit_bars: list      # forward bar where condition triggers
    move_adrs: list      # signal close → exit close in ADR
    capture_effs: list   # move / MFE per example
    # Aggregates
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
    path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    print(f"  Loading daily cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_pyramid_conditions(setup_type):
    """Load signal conditions from pyramid results (same logic as signal_filter)."""
    import glob
    search_dirs = [
        os.path.join(REPO_ROOT, "local_runner", "cache"),
        os.path.join(REPO_ROOT, "data"),
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
    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
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
def resolve_example_signals(examples, cache, conditions, expr_cache, direction,
                            max_forward):
    """
    For each example, find the deduplicated signal bar (scan candle = entry - 1),
    verify all conditions pass via expression cache, compute MFE from signal bar.
    
    Returns list of ExampleSignal.
    """
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
        if scan_idx < 50:
            print(f"    {ticker}: signal bar too early (idx {scan_idx})")
            continue

        # Load expression cache
        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None:
            print(f"    {ticker}: not in expression cache, skipping")
            continue
        if len(cached_dates) != len(df):
            print(f"    {ticker}: bar count mismatch (ohlcv={len(df)}, cache={len(cached_dates)})")
            continue

        # Verify ALL conditions pass at signal bar
        n_fail = 0
        for i, cond in enumerate(conditions):
            col_idx = cond_col_indices[i]
            if col_idx is None:
                n_fail += 1
                continue
            val = cached_data[scan_idx, col_idx]
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                n_fail += 1

        if n_fail > 0:
            print(f"    {ticker}: {n_fail}/{len(conditions)} conditions failed — skipping")
            continue

        # ADR at signal bar
        if adr_col_idx is not None:
            adr_val = float(cached_data[scan_idx, adr_col_idx])
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

        # Extract forward window: signal_bar+1 through signal_bar+n_forward
        start = ex.signal_bar_idx + 1
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
    
    For each candidate, find first forward bar where condition triggers,
    measure signal_close → exit_close in ADR.
    
    RULE: 100% example pass — hardcoded, no exceptions.
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
    print(f"  Requirement: ALL {n_examples} examples must trigger (100%)")

    t0 = time.time()
    tested = 0
    passed = 0
    all_candidates = []

    for expr_i in range(n_exprs):
        if (expr_i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"    [{expr_i+1}/{n_exprs}] {rate:.0f} expr/s, "
                  f"{passed} candidates passed, {tested:,} tested")

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
            continue  # can't hit 100% if some examples have no data

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
                exit_bars = []
                move_adrs = []
                capture_effs = []
                triggered = 0

                for ex_idx, series in example_series:
                    ex = example_signals[ex_idx]
                    closes = fwd_closes[ex_idx]

                    # Find first bar >= min_bar where condition triggers
                    found = False
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

                                exit_bars.append(bar)
                                move_adrs.append(move)
                                capture_effs.append(cap_eff)
                                triggered += 1
                            found = True
                            break

                    if not found:
                        break  # early exit — can't reach 100%

                if triggered < n_examples:
                    continue

                passed += 1
                valid_bars = [b for b in exit_bars]
                all_candidates.append(ExitCandidate(
                    expression=expr_name,
                    direction=dir_label,
                    threshold=round(thresh, 6),
                    exit_bars=exit_bars,
                    move_adrs=[round(m, 4) for m in move_adrs],
                    capture_effs=[round(e, 4) for e in capture_effs],
                    n_triggered=triggered,
                    floor_adr=round(min(move_adrs), 4),
                    median_adr=round(float(np.median(move_adrs)), 4),
                    mean_adr=round(float(np.mean(move_adrs)), 4),
                    floor_capture_eff=round(min(capture_effs), 4),
                    median_capture_eff=round(float(np.median(capture_effs)), 4),
                    mean_capture_eff=round(float(np.mean(capture_effs)), 4),
                    avg_bars_to_exit=round(float(np.mean(valid_bars)), 1),
                ))

    elapsed = time.time() - t0
    print(f"\n  Done: tested {tested:,} conditions in {elapsed:.1f}s")
    print(f"  Passed 100% filter: {passed:,} candidates")

    # Sort by median capture efficiency (primary), then median ADR (secondary)
    all_candidates.sort(
        key=lambda c: (c.median_capture_eff, c.median_adr),
        reverse=True
    )
    return all_candidates


# ============================================================
# Display
# ============================================================
def print_results(candidates, example_signals):
    """Print the best exit condition."""
    if not candidates:
        print("\n  No candidates found.")
        return
    c = candidates[0]
    print(f"\n  BEST: {c.expression} {c.direction} {c.threshold}")
    print(f"  Capture eff: floor={c.floor_capture_eff:.2f}  median={c.median_capture_eff:.2f}")
    print(f"  ADR move:    floor={c.floor_adr:.1f}  median={c.median_adr:.1f}")
    print(f"  Avg bars to exit: {c.avg_bars_to_exit:.0f}")


# ============================================================
# Save
# ============================================================
def save_results(candidates, example_signals, setup_type, meta):
    """Save results in format compatible with signal_filter.py."""
    out_dir = os.path.join(REPO_ROOT, "data", "signal_exit_grind")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    best = candidates[0] if candidates else None

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "grinder_type": "signal_exit",
        "computation_path": "expression_cache_only",
        "n_examples": len(example_signals),
        "n_expressions_tested": meta.get("n_expressions", 0),
        "n_candidates_found": len(candidates),
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
        # Top conditions in same format signal_filter expects
        "top_conditions": [
            {
                "expression": c.expression,
                "direction": c.direction,
                "threshold": c.threshold,
                "floor_efficiency": c.floor_capture_eff,
                "median_efficiency": c.median_capture_eff,
                "mean_efficiency": c.mean_capture_eff,
                "floor_adr": c.floor_adr,
                "median_adr": c.median_adr,
                "mean_adr": c.mean_adr,
                "avg_bars_to_exit": c.avg_bars_to_exit,
                "n_examples_triggered": c.n_triggered,
                "per_example_efficiency": c.capture_effs,
                "per_example_adr": c.move_adrs,
                "per_example_exit_bars": c.exit_bars,
            }
            for c in candidates[:50]
        ],
    }

    # Timestamped
    n_ex = len(example_signals)
    floor_tag = f"{best.floor_adr:+.1f}" if best else "na"
    ts_path = os.path.join(out_dir,
        f"signal_exit_{setup_type}_{n_ex}ex_{floor_tag}adr_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Saved: {ts_path}")

    # Latest (signal_filter.py reads this)
    latest_path = os.path.join(out_dir, f"signal_exit_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {latest_path}")

    from file_mirror import mirror_file
    mirror_file(ts_path)
    mirror_file(latest_path)

    # Upload to Railway
    if best:
        _upload_exit_to_railway(setup_type, best, args_max_forward=meta.get("max_forward", MAX_FORWARD_DEFAULT))

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
        description="Signal Exit Grinder — cache-compatible exit for signal filtering")
    parser.add_argument("--setup", required=True, help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT,
                        help="Max forward bars from signal (default: 120)")
    parser.add_argument("--n-thresholds", type=int, default=N_THRESHOLDS,
                        help="Thresholds per expression (default: 20)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--top-n", type=int, default=50,
                        help="Top N results to save")
    parser.add_argument("--conditions-file", type=str, default=None,
                        help="Path to JSON with pre-supplied signal conditions "
                             "(bypasses internal load_pyramid_conditions)")
    args = parser.parse_args()

    setup = args.setup
    direction = _get_setup_direction(setup)

    print(f"\n{'='*60}")
    print(f"  SIGNAL EXIT GRINDER — {setup.upper()}")
    print(f"  Expression cache only — same computation path as signal grinder")
    print(f"{'='*60}")
    t0 = time.time()

    # Load expression cache
    print(f"\n  Loading expression cache...")
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
    example_signals = resolve_example_signals(
        examples, ohlcv_cache, conditions, expr_cache, direction, args.max_forward)

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

    # Display
    if candidates:
        print_results(candidates, example_signals)

    # Save
    save_results(candidates, example_signals, setup, {
        "n_expressions": n_expressions,
        "max_forward": args.max_forward,
    })

    # Summary
    total_time = time.time() - t0
    best = candidates[0] if candidates else None
    print(f"\n{'='*60}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  Examples: {len(example_signals)} resolved")
    print(f"  Expressions tested: {n_expressions:,}")
    print(f"  Candidates found: {len(candidates)}")
    if best:
        print(f"  Best: {best.expression} {best.direction} {best.threshold}")
        print(f"    Median capture eff: {best.median_capture_eff:.2f}")
        print(f"    Floor ADR: {best.floor_adr:.1f}")
        print(f"    Median ADR: {best.median_adr:.1f}")
        print(f"    Avg bars to exit: {best.avg_bars_to_exit:.0f}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
