"""
Refinement Grinder — Find conditions that separate winning signals from losing signals.

Step 4 of the pipeline. Grinds (examples + exit-triggered winners) vs (no-exit losers).
Finds conditions visible on the signal bar that predict whether the setup will actually
move — eliminates losers without losing any winners.

KEY DIFFERENCES FROM STEP 1 (signal grind):
  Step 1 grinds examples vs the full tradable universe (~4,167 tickers × 5yr).
  Step 4 grinds classified winners vs classified losers from the cycle's signal set.
  The universe is ONLY the losing signals — much smaller, much more targeted.

DATA SOURCE:
  Railway is the authoritative store. Piles come from cycle_signals:
    - Win pile: AUTO_WIN + AI_WIN + MANUAL_WIN (examples + exit-triggered winners)
    - Lose pile: AUTO_LOSS + MANUAL_LOSS (exit never triggered or move < ADR)

  No blackout masking needed: we operate on discrete signal bars, not a full universe
  scan. Expression values at each signal bar reflect only data available at that point.

COMPUTATION PATH:
  Expression cache only. No live ExpressionEngine.compute(). Same float32 data path
  as pyramid_grinder and proximity_grinder. NaN handling matches exactly.

OUTPUT:
  Refinement conditions APPEND to step 1 conditions (tier="refinement").
  Combined conditions produce a SUBSET of step 1's signal set — fewer signals, higher WR.

SACRIFICIAL SIGNALS:
  Also extracts and uploads sacrificial signals (leftward duplicate signal bars from
  examples). These are needed by the proximity grind (step 6) later.

Usage:
    python scripts/refinement_grinder.py --setup dtss
    python scripts/refinement_grinder.py --setup dtss --beam 10000 --depth 100
    python scripts/refinement_grinder.py --setup dtss --dry-run
"""

import argparse
import os
import sys
import time
import json
import numpy as np
import requests
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache

API_BASE = "https://web-production-e3025.up.railway.app"

WIN_CLASSES = {"AUTO_WIN", "AI_WIN", "MANUAL_WIN"}
LOSE_CLASSES = {"AUTO_LOSS", "MANUAL_LOSS"}


# ══════════════════════════════════════════════════════════════
# DATA LOADING — Railway is the authoritative store
# ══════════════════════════════════════════════════════════════

def get_current_cycle(setup_type):
    """Get current cycle_id for this setup type from Railway."""
    r = requests.get(f"{API_BASE}/api/v2/cycles/{setup_type}", timeout=30)
    r.raise_for_status()
    cycles = r.json().get("cycles", [])
    current = [c for c in cycles if c.get("is_current") == 1]
    if not current:
        raise RuntimeError(f"No current cycle for {setup_type}")
    return current[0]["cycle_id"]


def load_piles_from_railway(cycle_id):
    """Load win and lose piles from Railway cycle signals.

    Win pile: AUTO_WIN + AI_WIN + MANUAL_WIN (examples + exit-triggered winners)
    Lose pile: AUTO_LOSS + MANUAL_LOSS (exit never triggered or move < ADR)

    Returns (win_pile, lose_pile, all_signals).
    """
    r = requests.get(f"{API_BASE}/api/v2/cycles/{cycle_id}/signals", timeout=60)
    r.raise_for_status()
    signals = r.json().get("signals", [])

    if not signals:
        raise RuntimeError(f"No signals found for cycle {cycle_id}")

    win_pile = []
    lose_pile = []
    for sig in signals:
        cls = sig.get("classification", "")
        if cls in WIN_CLASSES:
            win_pile.append(sig)
        elif cls in LOSE_CLASSES:
            lose_pile.append(sig)

    print(f"  Loaded from Railway (cycle {cycle_id}):")
    print(f"    Total signals: {len(signals):,}")
    print(f"    Win pile:      {len(win_pile):,}")
    print(f"    Lose pile:     {len(lose_pile):,}")

    # Breakdown
    n_examples = sum(1 for s in win_pile if s.get("is_example"))
    n_exit_win = sum(1 for s in win_pile if not s.get("is_example"))
    print(f"    Win breakdown:  {n_examples} examples + {n_exit_win} exit-triggered winners")

    return win_pile, lose_pile, signals


def extract_sacrificial_signals(all_signals, setup_type):
    """Find sacrificial signals — leftward duplicate signal bars near examples.

    For each example signal, find all deduped signals for the same ticker within
    ±7 calendar days. The rightmost (closest to entry) is the real signal (win pile).
    All signals to the left are sacrificial — they fired early.

    Returns list of sacrificial signal dicts ready for Railway upload.
    """
    import pandas as pd

    # Get example signals
    example_signals = [s for s in all_signals if s.get("is_example")]
    if not example_signals:
        print("  No example signals found — skipping sacrificial extraction.")
        return []

    # Build lookup: ticker -> list of (signal_date, bar_idx, signal_dict)
    by_ticker = {}
    for sig in all_signals:
        ticker = sig["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = []
        by_ticker[ticker].append(sig)

    sacrificial = []
    for ex in example_signals:
        ticker = ex["ticker"]
        ex_date = pd.to_datetime(ex["signal_date"])
        ex_bar_idx = ex["bar_idx"]

        if ticker not in by_ticker:
            continue

        # Find all signals within ±7 calendar days for this ticker
        nearby = []
        for sig in by_ticker[ticker]:
            sig_date = pd.to_datetime(sig["signal_date"])
            delta = abs((sig_date - ex_date).days)
            if delta <= 7:
                nearby.append(sig)

        if len(nearby) <= 1:
            continue

        # Sort by bar_idx ascending — rightmost is the real signal
        nearby.sort(key=lambda s: s["bar_idx"])
        rightmost_idx = nearby[-1]["bar_idx"]

        # All to the LEFT of rightmost are sacrificial
        for sig in nearby:
            if sig["bar_idx"] < rightmost_idx:
                sacrificial.append({
                    "ticker": sig["ticker"],
                    "signal_date": sig["signal_date"],
                    "bar_idx": sig["bar_idx"],
                    "close": sig.get("close"),
                    "adr": sig.get("adr"),
                    "example_ticker": ex["ticker"],
                    "example_signal_date": ex["signal_date"],
                    "example_bar_idx": ex_bar_idx,
                })

    # Deduplicate by (ticker, bar_idx) in case multiple examples share nearby signals
    seen = set()
    deduped = []
    for s in sacrificial:
        key = (s["ticker"], s["bar_idx"])
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    print(f"  Sacrificial signals: {len(deduped)} (from {len(example_signals)} examples)")
    return deduped


def upload_sacrificial_signals(cycle_id, sacrificial):
    """Upload sacrificial signals to Railway for the proximity grind."""
    if not sacrificial:
        print("  No sacrificial signals to upload.")
        return

    try:
        payload = {"signals": sacrificial, "replace": True}
        r = requests.post(
            f"{API_BASE}/api/v2/cycles/{cycle_id}/sacrificial_signals",
            json=payload, timeout=30,
        )
        r.raise_for_status()
        print(f"  Uploaded {len(sacrificial)} sacrificial signals to Railway")
    except Exception as e:
        print(f"  WARNING: Sacrificial signal upload failed: {e}")
        print(f"  Proximity grind will run without sacrificial signals.")


# ══════════════════════════════════════════════════════════════
# PARALLEL MATRIX EXTRACTION — matches pyramid_grinder pattern
# ══════════════════════════════════════════════════════════════

# Worker globals (set by initializer, shared across calls within a worker)
_w_signals = None
_w_expr_to_cache_col = None
_w_n_expr = None


def _init_extract_worker(signals, expr_to_cache_col, n_expr):
    """Initializer: serialize shared data once per worker process."""
    global _w_signals, _w_expr_to_cache_col, _w_n_expr
    _w_signals = signals
    _w_expr_to_cache_col = expr_to_cache_col
    _w_n_expr = n_expr


def _extract_batch(sig_indices):
    """Worker: extract expression values for a batch of signal indices.

    Reads from expr cache .npz files directly (same path as pyramid_grinder).
    Returns list of (sig_index, values_array) tuples.
    """
    from expr_cache_builder import load_ticker_cache

    results = []
    ticker_cache = {}

    for si in sig_indices:
        sig = _w_signals[si]
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]

        if ticker not in ticker_cache:
            dates, data = load_ticker_cache(ticker)
            ticker_cache[ticker] = (dates, data)
        cached_dates, cached_data = ticker_cache[ticker]

        if cached_dates is None or cached_data is None:
            continue
        if bar_idx >= len(cached_data):
            continue

        row = cached_data[bar_idx, :]
        values = np.full(_w_n_expr, np.nan, dtype=np.float32)
        for j, cache_col in enumerate(_w_expr_to_cache_col):
            if cache_col is not None and cache_col < len(row):
                values[j] = row[cache_col]
        results.append((si, values))

    return results


def extract_signal_values_parallel(signals, expressions, expr_cache):
    """Pull expression values from expr cache for each signal bar.

    Parallelized across CPU cores — matches pyramid_grinder's worker pattern.
    Returns np.array (n_signals, n_expressions) float32.
    """
    n_sig = len(signals)
    n_expr = len(expressions)
    matrix = np.full((n_sig, n_expr), np.nan, dtype=np.float32)

    if n_sig == 0:
        return matrix

    # Build expression -> cache column mapping
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    expr_to_cache_col = []
    for e in expressions:
        expr_to_cache_col.append(cache_name_to_idx.get(e["name"]))

    # Batch signals across workers
    n_workers = max(cpu_count() - 1, 1)
    batch_size = max(n_sig // (n_workers * 4), 10)
    all_indices = list(range(n_sig))
    batches = [all_indices[i:i + batch_size]
               for i in range(0, len(all_indices), batch_size)]

    t0 = time.time()
    n_loaded = 0

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_extract_worker,
        initargs=(signals, expr_to_cache_col, n_expr)
    ) as pool:
        futures = {pool.submit(_extract_batch, batch): batch for batch in batches}
        completed = 0
        for future in as_completed(futures):
            for si, values in future.result():
                matrix[si, :] = values
                n_loaded += 1
            completed += 1
            if completed % max(len(batches) // 5, 1) == 0 or completed == len(batches):
                elapsed = time.time() - t0
                print(f"    {completed}/{len(batches)} batches "
                      f"[{elapsed:.0f}s, {n_loaded:,} loaded]")

    pct = n_loaded / max(n_sig, 1) * 100
    elapsed = time.time() - t0
    print(f"  Extracted {n_loaded}/{n_sig} signals ({pct:.0f}%) "
          f"x {n_expr:,} expressions in {elapsed:.1f}s "
          f"({n_workers} workers)")
    return matrix


# ══════════════════════════════════════════════════════════════
# WIN PILE RANGES — matches pyramid_grinder.compute_example_ranges
# ══════════════════════════════════════════════════════════════

def compute_win_ranges(win_matrix, expressions):
    """Compute [min, max] range with 5% margin for each expression across win pile.

    CRITICAL: Matches pyramid_grinder exactly —
      - Require ALL win pile signals to have valid (non-NaN) values.
      - If any win signal has NaN for an expression, that expression cannot be
        used as a condition (it would fail validation for that signal).
      - 5% margin on each side, same as pyramid_grinder.
    """
    n_win = win_matrix.shape[0]
    ranges = {}

    for j, expr in enumerate(expressions):
        vals = win_matrix[:, j]
        valid = vals[~np.isnan(vals)]
        if len(valid) < n_win:
            continue
        lo, hi = float(np.min(valid)), float(np.max(valid))
        margin = (hi - lo) * 0.05
        ranges[expr["name"]] = (lo - margin, hi + margin)

    print(f"  Win ranges: {len(ranges):,} expressions with full coverage "
          f"(out of {len(expressions):,})")
    return ranges


# ══════════════════════════════════════════════════════════════
# BEAM SEARCH — minimize remaining losers
# ══════════════════════════════════════════════════════════════

def run_beam_search(lose_matrix, win_ranges, expressions,
                    beam_width=10000, depth=100):
    """Beam search to minimize remaining losers while keeping 100% of winners.

    lose_matrix: (n_lose, n_expr) — lose pile expression values
    win_ranges: {expr_name: (low, high)} — conditions must stay within these
                so every winner passes.

    NaN handling: NaN = FAIL (does not pass). Matches pyramid_grinder behavior.
    A loser with NaN for a condition will be filtered OUT (trimmed),
    which is correct — if a signal can't be evaluated, it doesn't survive.

    Score = number of remaining losers (minimize).
    """
    n_rows, n_expr = lose_matrix.shape
    expr_names = [e["name"] for e in expressions]

    print(f"\n  Beam Search:")
    print(f"    Losers to trim: {n_rows:,}")
    print(f"    Candidate expressions: {len(win_ranges):,}")
    print(f"    Beam: {beam_width:,}, Depth: {depth}")

    # Precompute: for each candidate, which lose rows pass (within win range)?
    # NaN = FAIL — matches pyramid_grinder
    cand_indices = []
    cand_passes = []

    for j, name in enumerate(expr_names):
        if name not in win_ranges:
            continue
        lo, hi = win_ranges[name]
        vals = lose_matrix[:, j]
        # NaN = FAIL (does not pass the condition)
        passes = (vals >= lo) & (vals <= hi)
        passes[np.isnan(vals)] = False
        n_pass = int(np.sum(passes))
        # Useful if it filters out at least 1% of rows
        if n_pass < n_rows * 0.99:
            cand_indices.append(j)
            cand_passes.append(passes)

    n_useful = len(cand_indices)
    print(f"    Useful candidates (filter >= 1%): {n_useful}")

    if n_useful == 0:
        print("    No useful candidates. Cannot refine further.")
        return [], n_rows

    cand_passes_arr = np.array(cand_passes, dtype=bool)  # (n_cands, n_rows)

    # Score each candidate individually
    base_mask = np.ones(n_rows, dtype=bool)
    scored = []
    for ci in range(n_useful):
        mask = base_mask & cand_passes_arr[ci]
        remaining = int(np.sum(mask))
        scored.append((ci, remaining, mask))

    scored.sort(key=lambda x: x[1])

    # Build initial beam
    class Node:
        __slots__ = ['conditions', 'row_mask', 'remaining']
        def __init__(self, conditions, row_mask, remaining):
            self.conditions = conditions
            self.row_mask = row_mask
            self.remaining = remaining

    n_seeds = min(beam_width * 2, len(scored))
    current_level = []
    for ci, remaining, mask in scored[:n_seeds]:
        current_level.append(Node(conditions=(ci,), row_mask=mask, remaining=remaining))

    current_level.sort(key=lambda n: n.remaining)
    current_level = current_level[:beam_width]

    best = current_level[0]
    trimmed = n_rows - best.remaining
    print(f"    Level  1: {best.remaining:,} remaining "
          f"(-{trimmed:,}, {trimmed / max(n_rows, 1) * 100:.1f}%)")

    # Deepen
    stall_count = 0
    for lv in range(2, depth + 1):
        next_level = []
        seen = set()

        for node in current_level:
            used = set(node.conditions)
            if not np.any(node.row_mask):
                continue

            for ci in range(n_useful):
                if ci in used:
                    continue
                combo = tuple(sorted(node.conditions + (ci,)))
                if combo in seen:
                    continue
                seen.add(combo)

                mask = node.row_mask & cand_passes_arr[ci]
                remaining = int(np.sum(mask))
                next_level.append(Node(conditions=combo, row_mask=mask,
                                       remaining=remaining))

                if len(next_level) >= beam_width * 8:
                    break
            if len(next_level) >= beam_width * 8:
                break

        if not next_level:
            print(f"    Level {lv:2d}: ceiling (no new combos)")
            break

        next_level.sort(key=lambda n: n.remaining)
        current_level = next_level[:beam_width]

        if current_level[0].remaining < best.remaining:
            best = current_level[0]
            stall_count = 0
        else:
            stall_count += 1

        trimmed = n_rows - best.remaining
        print(f"    Level {lv:2d}: {best.remaining:,} remaining "
              f"(-{trimmed:,}, {trimmed / max(n_rows, 1) * 100:.1f}%) "
              f"[{len(best.conditions)} conds]")

        if best.remaining == 0:
            break

        # Ceiling: if no improvement in 2 consecutive levels, stop
        if stall_count >= 2:
            print(f"    Ceiling at level {lv} (no improvement in 2 levels)")
            break

    return _extract_conditions(best, cand_indices, expressions, win_ranges), best.remaining


def _extract_conditions(node, cand_indices, expressions, win_ranges):
    """Convert beam search result to condition dicts."""
    conditions = []
    for ci in node.conditions:
        expr_idx = cand_indices[ci]
        expr = expressions[expr_idx]
        lo, hi = win_ranges[expr["name"]]
        conditions.append({
            "name": expr["name"],
            "expression_name": expr["name"],
            "expr": expr.get("expr", expr["name"]),
            "category": expr.get("category", "unknown"),
            "compute": expr.get("compute", {}),
            "low": lo,
            "high": hi,
            "tier": "refinement",
            "filter_power": None,
        })
    return conditions


# ══════════════════════════════════════════════════════════════
# VALIDATION — NaN = FAIL, matches pyramid_grinder
# ══════════════════════════════════════════════════════════════

def validate_win_pile(win_matrix, refinement_conditions, expressions):
    """Verify every win pile signal passes all refinement conditions.

    NaN = FAIL — matches pyramid_grinder's locked condition application.
    This MUST pass 100%. If it fails, the grinder has a bug.
    """
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    n_win = win_matrix.shape[0]
    failures = []

    for cond in refinement_conditions:
        j = expr_name_to_idx.get(cond["name"])
        if j is None:
            failures.append(f"  {cond['name']}: not in expression list")
            continue
        vals = win_matrix[:, j]
        # NaN = FAIL
        in_range = (vals >= cond["low"]) & (vals <= cond["high"])
        in_range[np.isnan(vals)] = False
        n_fail = int(np.sum(~in_range))
        if n_fail > 0:
            failures.append(f"  {cond['name']}: {n_fail}/{n_win} win signals FAIL")

    if failures:
        print("\n  VALIDATION FAILED — ABORTING:")
        for f in failures:
            print(f)
        return False

    print(f"\n  Validation passed: {n_win} win pile signals x "
          f"{len(refinement_conditions)} refinement conditions = 100% pass")
    return True


# ══════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════

def compute_metrics(win_pile, lose_pile, lose_matrix,
                    refinement_conditions, expressions):
    """Compute refinement metrics with NaN = FAIL."""
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    n_lose = len(lose_pile)
    n_win = len(win_pile)

    # How many losers trimmed?
    lose_mask = np.ones(n_lose, dtype=bool)
    for cond in refinement_conditions:
        j = expr_name_to_idx[cond["name"]]
        vals = lose_matrix[:, j]
        in_range = (vals >= cond["low"]) & (vals <= cond["high"])
        in_range[np.isnan(vals)] = False
        lose_mask &= in_range
    losers_remaining = int(np.sum(lose_mask))
    losers_trimmed = n_lose - losers_remaining

    # Win rate before/after
    old_total = n_win + n_lose
    new_total = n_win + losers_remaining
    old_wr = n_win / max(old_total, 1) * 100
    new_wr = n_win / max(new_total, 1) * 100

    # Median winner/loser ADR
    win_adrs = [s.get("move_adr") for s in win_pile if s.get("move_adr") is not None]
    lose_adrs = [s.get("move_adr") for s in lose_pile if s.get("move_adr") is not None]
    median_win_adr = sorted(win_adrs)[len(win_adrs) // 2] if win_adrs else 0
    median_lose_adr = sorted(lose_adrs)[len(lose_adrs) // 2] if lose_adrs else 0

    return {
        "win_pile": n_win,
        "lose_pile_before": n_lose,
        "losers_trimmed": losers_trimmed,
        "losers_remaining": losers_remaining,
        "old_total_deduped": old_total,
        "new_total_deduped": new_total,
        "old_win_rate_pct": round(old_wr, 1),
        "new_win_rate_pct": round(new_wr, 1),
        "n_refinement_conditions": len(refinement_conditions),
        "median_winner_adr": round(median_win_adr, 2),
        "median_loser_adr": round(median_lose_adr, 2),
    }


# ══════════════════════════════════════════════════════════════
# SAVE + RAILWAY UPLOAD
# ══════════════════════════════════════════════════════════════

def save_results(setup_type, refinement_conditions, metrics):
    """Save refinement grind results locally."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n_conds = len(refinement_conditions)

    output = {
        "setup_type": setup_type,
        "timestamp": ts,
        "step": "refinement_grind",
        "grinder_type": "refinement",
        "n_conditions": n_conds,
        "all_conditions": refinement_conditions,
        "metrics": metrics,
    }

    out_dir = os.path.join(REPO_ROOT, "data", "refinement_grind")
    os.makedirs(out_dir, exist_ok=True)

    fname = f"refinement_{setup_type}_{n_conds}cond_{ts}.json"
    path = os.path.join(out_dir, fname)
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {path}")

    latest = os.path.join(out_dir, f"refinement_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Latest: {latest}")

    from file_mirror import mirror_file
    mirror_file(path)
    mirror_file(latest)

    return output, path


def upload_to_railway(setup_type, refinement_conditions, metrics):
    """Upload refinement conditions to the current cycle's cycle_conditions.

    Replaces any existing refinement conditions (idempotent on re-run).
    Keeps all non-refinement conditions intact.
    """
    print(f"\n  Railway Upload:")

    # Find current cycle
    try:
        r = requests.get(f"{API_BASE}/api/v2/cycles/{setup_type}", timeout=30)
        r.raise_for_status()
        cycles = r.json().get("cycles", [])
        current = [c for c in cycles if c.get("is_current") == 1]
        if not current:
            print(f"  No current cycle for {setup_type} — skipping upload")
            return
        cycle_id = current[0]["cycle_id"]
    except Exception as e:
        print(f"  Failed to find current cycle: {e}")
        return

    print(f"  Cycle: {cycle_id}")

    # Read existing conditions
    try:
        r = requests.get(f"{API_BASE}/api/v2/cycles/{cycle_id}/conditions", timeout=30)
        r.raise_for_status()
        existing = r.json().get("conditions", [])
    except Exception as e:
        print(f"  Warning: could not read existing conditions: {e}")
        existing = []

    # Strip old refinement conditions, keep everything else
    kept = [c for c in existing if c.get("tier") != "refinement"]
    max_sort = max((c.get("sort_order", 0) for c in kept), default=0)

    # Append new refinement conditions
    new_conds = [
        {
            "tier": "refinement",
            "expression_name": c.get("expression_name", c.get("name", "")),
            "low": c["low"],
            "high": c["high"],
            "filter_power": c.get("filter_power"),
            "sort_order": max_sort + 1 + i,
        }
        for i, c in enumerate(refinement_conditions)
    ]

    all_conds = kept + new_conds

    try:
        payload = {"conditions": all_conds}
        r = requests.post(
            f"{API_BASE}/api/v2/cycles/{cycle_id}/conditions",
            json=payload, timeout=30,
        )
        r.raise_for_status()
        print(f"  Uploaded {len(kept)} existing + {len(new_conds)} refinement "
              f"= {len(all_conds)} total conditions")
    except Exception as e:
        print(f"  FAILED: {e}")
        print(f"  Conditions saved locally. Upload manually.")
        return

    # Verify
    try:
        r = requests.get(f"{API_BASE}/api/v2/cycles/{cycle_id}/conditions", timeout=30)
        r.raise_for_status()
        stored = r.json().get("conditions", [])
        n_ref = sum(1 for c in stored if c.get("tier") == "refinement")
        print(f"  Verified: {len(stored)} total conditions ({n_ref} refinement)")
    except Exception as e:
        print(f"  Verification failed: {e}")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run_refinement_grind(setup_type, beam_width=10000, depth=100, dry_run=False):
    print(f"\n{'=' * 70}")
    print(f"  REFINEMENT GRINDER — {setup_type.upper()}")
    print(f"{'=' * 70}\n")

    t0 = time.time()

    # ── 1. Get current cycle from Railway ──
    print("Phase 1: Loading data from Railway...")
    cycle_id = get_current_cycle(setup_type)
    print(f"  Current cycle: {cycle_id}")

    # ── 2. Load piles from Railway ──
    win_pile, lose_pile, all_signals = load_piles_from_railway(cycle_id)

    # ── 3. Extract sacrificial signals ──
    print(f"\nPhase 2: Extracting sacrificial signals...")
    sacrificial = extract_sacrificial_signals(all_signals, setup_type)

    if dry_run:
        print(f"\n  DRY RUN — stopping here. ({time.time() - t0:.0f}s)")
        return

    if len(win_pile) == 0:
        print("\n  ERROR: Win pile is empty. Cannot grind.")
        return
    if len(lose_pile) == 0:
        print("\n  Nothing to refine — lose pile is empty.")
        return

    # ── 4. Load expr cache + expressions ──
    print("\nPhase 3: Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found. "
                           "Run: python local_runner/expr_cache_builder.py --build")

    expressions = generate_all()
    print(f"  Expressions: {len(expressions):,}")
    print(f"  Expr cache: {expr_cache.n_expressions:,} expressions")

    # ── 5. Extract values (parallelized) ──
    print("\nPhase 4a: Extracting win pile expression values...")
    win_matrix = extract_signal_values_parallel(win_pile, expressions, expr_cache)

    print("\nPhase 4b: Extracting lose pile expression values...")
    lose_matrix = extract_signal_values_parallel(lose_pile, expressions, expr_cache)

    # ── 6. Compute win ranges (pyramid_grinder-compatible) ──
    print("\nPhase 5: Computing win pile ranges...")
    win_ranges = compute_win_ranges(win_matrix, expressions)

    if not win_ranges:
        print("  ERROR: No expressions with full win pile coverage.")
        return

    # ── 7. Beam search ──
    print("\nPhase 6: Beam search (minimize remaining losers)...")
    refinement_conditions, remaining = run_beam_search(
        lose_matrix, win_ranges, expressions,
        beam_width=beam_width, depth=depth,
    )

    if not refinement_conditions:
        print("\n  No conditions found. Cannot refine further.")
        return

    # ── 8. Validate (NaN = FAIL, hard abort on failure) ──
    print("\nPhase 7: Validating win pile (NaN = FAIL)...")
    if not validate_win_pile(win_matrix, refinement_conditions, expressions):
        print("\n  CRITICAL: Validation failed. Aborting — grinder has a bug.")
        return

    # ── 9. Compute metrics ──
    metrics = compute_metrics(
        win_pile, lose_pile, lose_matrix,
        refinement_conditions, expressions,
    )

    # ── 10. Print results ──
    print(f"\n{'=' * 70}")
    print(f"  REFINEMENT GRIND RESULTS — {setup_type.upper()}")
    print(f"{'=' * 70}")
    print(f"  Refinement conditions: {len(refinement_conditions)}")
    for c in refinement_conditions:
        print(f"    {c['name']:40s}  [{c['low']:.4f}, {c['high']:.4f}]  ({c['category']})")
    print(f"\n  Win pile (untouched):    {metrics['win_pile']:,}")
    print(f"  Lose pile:              {metrics['lose_pile_before']:,} -> "
          f"{metrics['losers_remaining']:,} (-{metrics['losers_trimmed']:,})")
    print(f"\n  Deduped signals:        {metrics['old_total_deduped']:,} -> "
          f"{metrics['new_total_deduped']:,}")
    print(f"  Win rate:               {metrics['old_win_rate_pct']:.1f}% -> "
          f"{metrics['new_win_rate_pct']:.1f}%")
    print(f"\n  Median winner ADR:      {metrics['median_winner_adr']}")
    print(f"  Median loser ADR:       {metrics['median_loser_adr']}")
    print(f"\n  Time: {time.time() - t0:.0f}s")

    # ── 11. Save locally ──
    save_results(setup_type, refinement_conditions, metrics)

    # ── 12. Upload conditions to Railway (append to existing) ──
    upload_to_railway(setup_type, refinement_conditions, metrics)

    # ── 13. Upload sacrificial signals to Railway ──
    print(f"\n  Uploading sacrificial signals...")
    upload_sacrificial_signals(cycle_id, sacrificial)

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Refinement Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--beam", type=int, default=10000, help="Beam width")
    parser.add_argument("--depth", type=int, default=100, help="Search depth")
    parser.add_argument("--peak-target", type=int, default=None,
                        help="Ignored (accepted for agent compatibility)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pile stats only, don't grind")
    args = parser.parse_args()

    run_refinement_grind(
        setup_type=args.setup,
        beam_width=args.beam,
        depth=args.depth,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
