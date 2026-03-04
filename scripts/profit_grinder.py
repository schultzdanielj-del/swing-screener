"""
Profit Grinder — Step 4 of ANALYSIS_SYSTEM.md

Brute forces post-entry expressions against validated examples' forward paths
to find TA-driven exit conditions that maximize capture from the ENTRY BAR HIGH.

This is the trade management exit grinder — for real trades.

Key differences from signal_exit_grinder.py:
  - Benchmarks from ENTRY BAR HIGH (not signal bar close)
  - Uses ExprSeriesCache (same single computation path as all grinders)
  - Outputs exit conditions + per-example exit dates for the blackout filter
  - Writes to data/profit_grind/ (never touches signal_exit_grind output)

The signal_exit_grinder.py is a separate tool that runs from the signal bar
and feeds the signal filter / vetting pipeline. It stays unchanged.

Output feeds:
  1. UI — profit grinder results page (Step 4)
  2. Blackout filter — per-example exit dates used to mask post-entry bars
     in the next signal grinder re-run

Rules (non-negotiable):
  - 100% example pass rate — hardcoded, no exceptions
  - Expression cache is the ONLY computation path
  - Parallel across all cores

Usage:
    python scripts/profit_grinder.py --setup dtss
    python scripts/profit_grinder.py --setup dtss --max-forward 120 --workers 8
"""

import argparse
import os
import sys
import time
import json
import pickle
import numpy as np
import pandas as pd
import requests
from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from expr_cache_builder import ExprSeriesCache

# ============================================================
# Config
# ============================================================
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD_DEFAULT = 120
DEFAULT_WORKERS = os.cpu_count() or 8
N_THRESHOLDS = 20

SETUP_CONFIGS = {
    "dtss":  {"direction": "short"},
    "3-4db": {"direction": "short"},
    "htf":   {"direction": "long"},
}


# ============================================================
# Data Classes
# ============================================================
@dataclass
class ExampleEntry:
    """An example anchored to its entry bar."""
    id: int
    ticker: str
    entry_date: str
    entry_bar_idx: int    # index of entry bar in OHLCV df
    entry_high: float     # entry bar high — benchmark ref for all moves
    adr_at_entry: float   # ADR on entry bar (for normalizing moves)
    mfe_adr: float        # max favorable excursion from entry high, in ADR
    n_forward: int        # bars available after entry bar


@dataclass
class ProfitCandidate:
    """A candidate exit condition scored across all examples."""
    expression: str
    direction: str        # ">=" or "<="
    threshold: float
    # Per-example results
    exit_bars: list       # forward bar offset where condition first triggers
    exit_dates: list      # calendar date of exit bar per example
    move_adrs: list       # entry_high → exit_close in ADR
    capture_effs: list    # move / MFE per example
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
def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    print(f"  Loading 5yr cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_examples(setup_type):
    """Load validated examples from Railway."""
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}", timeout=30)
    r.raise_for_status()
    examples = r.json().get("examples", [])
    print(f"  Loaded {len(examples)} examples from Railway")
    return examples


# ============================================================
# Example Entry Bar Resolution
# ============================================================
def resolve_example_entries(examples, ohlcv_cache, expr_cache, direction, max_forward):
    """
    For each example, anchor to the entry bar.
    Compute entry_high, ADR, MFE from entry bar high.

    Returns list of ExampleEntry.
    """
    adr_col_idx = expr_cache.expr_index("adr14")

    results = []
    skipped = []

    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))

        df = ohlcv_cache.get(ticker)
        if df is None:
            skipped.append(f"{ticker}: not in OHLCV cache")
            continue

        dates_str = [str(d)[:10] for d in df["date"].values]
        if entry_date not in dates_str:
            skipped.append(f"{ticker}: entry date {entry_date} not found")
            continue

        entry_idx = dates_str.index(entry_date)

        if entry_idx < 50:
            skipped.append(f"{ticker}: entry bar too early (idx {entry_idx})")
            continue

        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            skipped.append(f"{ticker}: only {n_available} forward bars after entry")
            continue

        # Check expression cache
        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None:
            skipped.append(f"{ticker}: not in expression cache")
            continue
        if len(cached_dates) != len(df):
            skipped.append(f"{ticker}: bar count mismatch (ohlcv={len(df)}, cache={len(cached_dates)})")
            continue

        # ADR at entry bar
        if adr_col_idx is not None:
            adr_val = float(cached_data[entry_idx, adr_col_idx])
        else:
            h = df["high"].values
            l = df["low"].values
            start = max(0, entry_idx - 13)
            adr_val = float(np.mean(h[start:entry_idx + 1] - l[start:entry_idx + 1]))

        if adr_val <= 0 or np.isnan(adr_val):
            skipped.append(f"{ticker}: invalid ADR at entry bar")
            continue

        entry_high = float(df["high"].values[entry_idx])
        actual_forward = min(max_forward, n_available)

        # MFE from entry bar high (in ADR)
        if direction == "short":
            fwd_lows = df["low"].values[entry_idx + 1: entry_idx + actual_forward + 1]
            mfe_price = float(np.min(fwd_lows))
            mfe_adr = (entry_high - mfe_price) / adr_val
        else:
            fwd_highs = df["high"].values[entry_idx + 1: entry_idx + actual_forward + 1]
            entry_low = float(df["low"].values[entry_idx])
            mfe_price = float(np.max(fwd_highs))
            mfe_adr = (mfe_price - entry_low) / adr_val

        results.append(ExampleEntry(
            id=ex.get("id"),
            ticker=ticker,
            entry_date=entry_date,
            entry_bar_idx=entry_idx,
            entry_high=entry_high,
            adr_at_entry=round(adr_val, 4),
            mfe_adr=round(mfe_adr, 2),
            n_forward=actual_forward,
        ))
        print(f"    {ticker:8s} entry {entry_date}  high={entry_high:.2f}  "
              f"ADR={adr_val:.2f}  MFE={mfe_adr:.1f} ADR  fwd={actual_forward}b")

    if skipped:
        print(f"\n  Skipped {len(skipped)}:")
        for s in skipped:
            print(f"    {s}")

    print(f"\n  Resolved {len(results)}/{len(examples)} examples")
    return results


# ============================================================
# Forward Matrix Build (from expression cache)
# ============================================================
def build_forward_matrices(example_entries, expr_cache, n_expressions):
    """
    For each example, extract expression values for all forward bars
    from the expression cache (entry_bar+1 through entry_bar+n_forward).

    Returns: list of (n_forward, n_expressions) arrays, one per example.
    Single computation path — expression cache only.
    """
    matrices = []
    for ex in example_entries:
        cached_dates, cached_data = expr_cache.get_ticker(ex.ticker)
        if cached_dates is None:
            matrices.append(None)
            continue

        # Forward window: entry_bar+1 through entry_bar+n_forward (inclusive)
        start = ex.entry_bar_idx + 1
        end = start + ex.n_forward
        if end > len(cached_data):
            end = len(cached_data)

        fwd_data = cached_data[start:end].copy()  # (n_forward, n_expressions)
        matrices.append(fwd_data)

    valid = sum(1 for m in matrices if m is not None)
    print(f"  Built forward matrices: {valid}/{len(example_entries)} valid")
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


# ============================================================
# Parallel worker for grind_profits
# ============================================================
def _grind_expr_chunk(args):
    """Worker: grind a chunk of expressions. Returns list of ProfitCandidate dicts."""
    (expr_indices, expr_names, forward_matrices,
     fwd_closes, fwd_dates, example_entries,
     direction, n_thresholds, min_bar) = args

    n_examples = len(example_entries)
    candidates = []

    for expr_i in expr_indices:
        expr_name = expr_names[expr_i]

        # Gather forward values for this expression across all examples
        example_series = []
        all_values = []
        for i, matrix in enumerate(forward_matrices):
            if matrix is None or expr_i >= matrix.shape[1]:
                continue
            series = matrix[:, expr_i]
            example_series.append((i, series))
            vals = series[~np.isnan(series)]
            if len(vals) > 0:
                all_values.append(vals)

        if len(example_series) < n_examples:
            continue
        if not all_values:
            continue

        combined = np.concatenate(all_values)
        clean = combined[~np.isnan(combined)]
        if len(clean) < 5:
            continue
        pcts = np.linspace(5, 95, n_thresholds)
        thresholds_raw = np.percentile(clean, pcts)
        seen = set()
        thresholds = []
        for t in thresholds_raw:
            t_r = round(float(t), 6)
            if t_r not in seen:
                seen.add(t_r)
                thresholds.append(t_r)
        if not thresholds:
            continue

        for thresh in thresholds:
            for dir_label, dir_op in [(">=", "ge"), ("<=", "le")]:
                exit_bars = []
                exit_dates_out = []
                move_adrs = []
                capture_effs = []
                triggered = 0

                for ex_idx, series in example_series:
                    ex = example_entries[ex_idx]
                    closes = fwd_closes[ex_idx]
                    dates = fwd_dates[ex_idx]

                    found = False
                    for bar in range(min_bar, len(series)):
                        val = series[bar]
                        if np.isnan(val):
                            continue
                        hit = (val >= thresh) if dir_op == "ge" else (val <= thresh)
                        if hit:
                            if bar < len(closes):
                                exit_close = float(closes[bar])
                                exit_date = dates[bar] if bar < len(dates) else ""
                                if direction == "short":
                                    move = (ex.entry_high - exit_close) / ex.adr_at_entry
                                else:
                                    move = (exit_close - ex.entry_high) / ex.adr_at_entry
                                cap_eff = move / ex.mfe_adr if ex.mfe_adr > 0 else 0.0
                                exit_bars.append(bar)
                                exit_dates_out.append(exit_date)
                                move_adrs.append(move)
                                capture_effs.append(cap_eff)
                                triggered += 1
                            found = True
                            break

                    if not found:
                        break  # early exit — can't reach 100%

                if triggered < n_examples:
                    continue

                candidates.append(ProfitCandidate(
                    expression=expr_name,
                    direction=dir_label,
                    threshold=round(thresh, 6),
                    exit_bars=exit_bars,
                    exit_dates=exit_dates_out,
                    move_adrs=[round(m, 4) for m in move_adrs],
                    capture_effs=[round(e, 4) for e in capture_effs],
                    n_triggered=triggered,
                    floor_adr=round(min(move_adrs), 4),
                    median_adr=round(float(np.median(move_adrs)), 4),
                    mean_adr=round(float(np.mean(move_adrs)), 4),
                    floor_capture_eff=round(min(capture_effs), 4),
                    median_capture_eff=round(float(np.median(capture_effs)), 4),
                    mean_capture_eff=round(float(np.mean(capture_effs)), 4),
                    avg_bars_to_exit=round(float(np.mean(exit_bars)), 1),
                ))

    return candidates


def grind_profits(example_entries, forward_matrices, expr_names,
                  ohlcv_cache, direction, n_thresholds=N_THRESHOLDS,
                  min_bar=1, workers=None):
    """
    Brute-force all cache expressions x thresholds x directions.
    Parallel across all cores — splits expression range into chunks.

    RULE: 100% example pass — hardcoded, no exceptions.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    workers = workers or max((os.cpu_count() or 1) - 1, 1)
    n_examples = len(example_entries)
    n_exprs = len(expr_names)

    # Pre-build forward close and date arrays (small, passed to workers)
    fwd_closes = []
    fwd_dates = []
    for ex in example_entries:
        df = ohlcv_cache.get(ex.ticker)
        start = ex.entry_bar_idx + 1
        end = start + ex.n_forward
        fwd_closes.append(df["close"].values[start:end].astype(np.float64))
        all_dates = [str(d)[:10] for d in df["date"].values]
        fwd_dates.append(all_dates[start:end])

    print(f"\n  Grinding {n_exprs:,} expressions x ~{n_thresholds} thresholds x 2 directions")
    print(f"  Benchmark: entry bar high -> exit bar close (ADR)")
    print(f"  Requirement: ALL {n_examples} examples must trigger (100%, no exceptions)")
    print(f"  Workers: {workers}")

    # Split expressions into chunks — one chunk per worker
    chunk_size = max(1, n_exprs // workers)
    chunks = []
    for i in range(0, n_exprs, chunk_size):
        chunks.append(list(range(i, min(i + chunk_size, n_exprs))))

    t0 = time.time()
    all_candidates = []

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_grind_expr_chunk, (
                chunk, expr_names, forward_matrices,
                fwd_closes, fwd_dates, example_entries,
                direction, n_thresholds, min_bar
            )): chunk_i
            for chunk_i, chunk in enumerate(chunks)
        }
        done = 0
        for future in as_completed(futures):
            chunk_candidates = future.result()
            all_candidates.extend(chunk_candidates)
            done += 1
            if done % max(len(chunks) // 5, 1) == 0 or done == len(chunks):
                elapsed = time.time() - t0
                pct = done / len(chunks) * 100
                print(f"    {pct:.0f}%  [{elapsed:.0f}s]  {len(all_candidates)} candidates so far")

    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.1f}s  —  {len(all_candidates):,} candidates passed 100% filter")

    # Sort by median capture efficiency (primary), then median ADR (secondary)
    all_candidates.sort(
        key=lambda c: (c.median_capture_eff, c.median_adr),
        reverse=True
    )
    return all_candidates

# ============================================================
# Display
# ============================================================
def print_results(candidates, example_entries, top_n=10):
    """Print the top exit conditions."""
    if not candidates:
        print("\n  No candidates found.")
        return

    print(f"\n{'=' * 80}")
    print(f"  TOP {min(top_n, len(candidates))} PROFIT GRINDER RESULTS")
    print(f"  Benchmark: entry bar high -> exit bar close (ADR)")
    print(f"{'=' * 80}")

    for rank, c in enumerate(candidates[:top_n], 1):
        print(f"\n  #{rank}  {c.expression} {c.direction} {c.threshold}")
        print(f"      Capture eff:  floor={c.floor_capture_eff:.2f}  "
              f"median={c.median_capture_eff:.2f}  mean={c.mean_capture_eff:.2f}")
        print(f"      ADR move:     floor={c.floor_adr:.2f}  "
              f"median={c.median_adr:.2f}  mean={c.mean_adr:.2f}")
        print(f"      Avg bars to exit: {c.avg_bars_to_exit:.0f}")
        print(f"      {'Ticker':8s}  {'Entry Date':12s}  {'Exit Date':12s}  "
              f"{'Bar#':>5s}  {'ADR Move':>9s}  {'Capt Eff':>9s}")
        for i, ex in enumerate(example_entries):
            if i >= len(c.exit_bars):
                continue
            bar = c.exit_bars[i]
            edate = c.exit_dates[i] if i < len(c.exit_dates) else ""
            adr = c.move_adrs[i]
            eff = c.capture_effs[i]
            print(f"      {ex.ticker:8s}  {ex.entry_date:12s}  {edate:12s}  "
                  f"{bar:5d}  {adr:+8.2f} ADR  {eff:8.2f}")


def print_mfe_summary(example_entries):
    """Print MFE summary."""
    print(f"\n{'=' * 70}")
    print(f"  MFE SUMMARY — from entry bar high")
    print(f"{'=' * 70}")
    print(f"  {'Ticker':8s}  {'Entry Date':12s}  {'Entry High':>11s}  "
          f"{'ADR':>7s}  {'MFE (ADR)':>10s}  {'Fwd Bars':>9s}")
    for ex in example_entries:
        print(f"  {ex.ticker:8s}  {ex.entry_date:12s}  ${ex.entry_high:10.2f}  "
              f"{ex.adr_at_entry:6.2f}  {ex.mfe_adr:+9.1f}     {ex.n_forward:9d}")
    mfes = [ex.mfe_adr for ex in example_entries]
    print(f"\n  Floor MFE:  {min(mfes):+.2f} ADR")
    print(f"  Median MFE: {float(np.median(mfes)):+.2f} ADR")
    print(f"  Avg MFE:    {float(np.mean(mfes)):+.2f} ADR")


# ============================================================
# Save
# ============================================================
def save_results(candidates, example_entries, setup_type, meta):
    """
    Save results to data/profit_grind/.
    Two files:
      - Timestamped archive (never overwritten)
      - Latest (overwritten each run — downstream consumers read this)

    Output includes per-example exit dates for use by the blackout filter
    in the next signal grinder re-run.
    """
    out_dir = os.path.join(REPO_ROOT, "data", "profit_grind")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    best = candidates[0] if candidates else None
    n_ex = len(example_entries)
    floor_tag = f"{best.floor_adr:+.1f}" if best else "na"

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "grinder_type": "profit",
        "computation_path": "expression_cache_only",
        "benchmark": "entry_bar_high_to_exit_close_adr",
        "direction": meta.get("direction", "short"),
        "n_examples": n_ex,
        "n_expressions_tested": meta.get("n_expressions", 0),
        "n_candidates_found": len(candidates),
        # MFE summary
        "mfe_summary": {
            "floor_adr": round(float(min(ex.mfe_adr for ex in example_entries)), 2),
            "median_adr": round(float(np.median([ex.mfe_adr for ex in example_entries])), 2),
            "mean_adr": round(float(np.mean([ex.mfe_adr for ex in example_entries])), 2),
        },
        # Per-example data (includes entry dates for blackout filter)
        "examples": [
            {
                "id": ex.id,
                "ticker": ex.ticker,
                "entry_date": ex.entry_date,
                "entry_high": ex.entry_high,
                "adr_at_entry": ex.adr_at_entry,
                "mfe_adr": ex.mfe_adr,
                # exit_date from best condition — populated below
                "exit_date": None,
                "exit_bar": None,
            }
            for ex in example_entries
        ],
        # Top conditions
        "top_conditions": [
            {
                "rank": i + 1,
                "expression": c.expression,
                "direction": c.direction,
                "threshold": c.threshold,
                "floor_capture_eff": c.floor_capture_eff,
                "median_capture_eff": c.median_capture_eff,
                "mean_capture_eff": c.mean_capture_eff,
                "floor_adr": c.floor_adr,
                "median_adr": c.median_adr,
                "mean_adr": c.mean_adr,
                "avg_bars_to_exit": c.avg_bars_to_exit,
                "n_examples_triggered": c.n_triggered,
                "per_example_exit_bars": c.exit_bars,
                "per_example_exit_dates": c.exit_dates,
                "per_example_move_adrs": c.move_adrs,
                "per_example_capture_effs": c.capture_effs,
            }
            for i, c in enumerate(candidates[:50])
        ],
    }

    # Populate exit_date on examples from best condition
    if best:
        for i, ex_data in enumerate(output["examples"]):
            if i < len(best.exit_bars):
                ex_data["exit_date"] = best.exit_dates[i]
                ex_data["exit_bar"] = best.exit_bars[i]

    # Timestamped archive
    ts_path = os.path.join(
        out_dir,
        f"profit_{setup_type}_{n_ex}ex_{floor_tag}adr_{ts}.json"
    )
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Saved: {ts_path}")

    # Latest (downstream consumers read this)
    latest_path = os.path.join(out_dir, f"profit_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved as latest: {latest_path}")

    # Upload to Railway
    upload_to_railway(output, setup_type)

    return latest_path


def upload_to_railway(data, setup_type):
    """Upload profit grinder results to Railway DB."""
    try:
        r = requests.post(
            f"{RAILWAY_URL}/api/profit-grind/{setup_type}/upload",
            json=data,
            timeout=30
        )
        if r.status_code == 200:
            print(f"  Uploaded to Railway OK")
        else:
            print(f"  Railway upload: {r.status_code} — {r.text[:200]}")
    except Exception as e:
        print(f"  Railway upload failed: {e}")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Profit Grinder — exit from entry bar high, Step 4")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT,
                        help="Max forward bars from entry (default: 120)")
    parser.add_argument("--n-thresholds", type=int, default=N_THRESHOLDS,
                        help="Thresholds per expression (default: 20)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--top-n", type=int, default=10,
                        help="Top N results to display (default: 10)")
    args = parser.parse_args()

    setup = args.setup.lower()
    config = SETUP_CONFIGS.get(setup, {"direction": "short"})
    direction = config["direction"]

    print(f"\n{'=' * 70}")
    print(f"  PROFIT GRINDER — {setup.upper()}")
    print(f"  Benchmark: entry bar HIGH -> exit bar close (ADR)")
    print(f"  Computation: expression cache only")
    print(f"  Direction: {direction}")
    print(f"{'=' * 70}")
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

    # Load OHLCV cache
    ohlcv_cache = load_5yr_cache()

    # Load examples
    print(f"\n  Loading examples...")
    raw_examples = load_examples(setup)

    # Phase 1: Resolve entry bars
    print(f"\n  PHASE 1: Resolve entry bars")
    example_entries = resolve_example_entries(
        raw_examples, ohlcv_cache, expr_cache, direction, args.max_forward
    )

    if len(example_entries) < 3:
        print(f"  ERROR: Only {len(example_entries)} valid examples — need at least 3")
        sys.exit(1)

    print_mfe_summary(example_entries)

    # Phase 2: Build forward matrices from expression cache
    print(f"\n  PHASE 2: Build forward matrices from expression cache")
    forward_matrices = build_forward_matrices(example_entries, expr_cache, n_expressions)

    # Phase 3: Grind
    print(f"\n  PHASE 3: Grind")
    candidates = grind_profits(
        example_entries, forward_matrices, expr_names,
        ohlcv_cache, direction,
        n_thresholds=args.n_thresholds,
        workers=args.workers,
    )

    # Validate — 100% pass is hardcoded in grinder but double-check
    if candidates:
        best = candidates[0]
        failed = [example_entries[i].ticker for i in range(len(example_entries))
                  if i >= len(best.exit_bars) or best.exit_bars[i] < 0]
        if failed:
            print(f"\n{'!' * 70}")
            print(f"  INTERNAL ERROR — best result does NOT pass all examples!")
            print(f"  Failed: {failed}")
            print(f"{'!' * 70}")
        else:
            print(f"\n  Validation: {len(example_entries)}/{len(example_entries)} examples pass")

    # Display
    print_results(candidates, example_entries, top_n=args.top_n)

    # Save + upload
    save_results(candidates, example_entries, setup, {
        "direction": direction,
        "n_expressions": n_expressions,
    })

    # Summary
    total_time = time.time() - t0
    best = candidates[0] if candidates else None
    print(f"\n{'=' * 70}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  Examples: {len(example_entries)} resolved")
    print(f"  Expressions tested: {n_expressions:,}")
    print(f"  Candidates found: {len(candidates)}")
    if best:
        print(f"  Best: {best.expression} {best.direction} {best.threshold}")
        print(f"    Median capture eff: {best.median_capture_eff:.2f}")
        print(f"    Floor ADR:          {best.floor_adr:.2f}")
        print(f"    Median ADR:         {best.median_adr:.2f}")
        print(f"    Avg bars to exit:   {best.avg_bars_to_exit:.0f}")
        print(f"\n  Exit dates per example (for blackout filter):")
        for i, ex in enumerate(example_entries):
            edate = best.exit_dates[i] if i < len(best.exit_dates) else "?"
            print(f"    {ex.ticker:8s}  {ex.entry_date} -> {edate}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
