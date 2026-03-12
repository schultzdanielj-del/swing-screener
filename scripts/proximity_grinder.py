"""
Proximity Grinder — Trim losers by finding conditions that separate
win pile from lose pile.

Post-convergence step (Step 6). Finds conditions that ALL win pile signals
pass but that eliminate lose pile signals. Pure EV gain — every loser removed
raises win rate and profit factor.

DATA SOURCE:
  Reads from the refinement grinder's local output file:
    local_runner/cache/refinement_{setup}_*.json
  The refinement grinder outputs winner_signals and loser_signals
  (losers that survived refinement conditions).

COMPUTATION PATH:
  Uses expr cache as single computation path — same as pyramid_grinder.py
  and all other grinders. No live ExpressionEngine.compute().

  NaN handling matches pyramid_grinder exactly:
    - Win pile range computation: require ALL win signals non-NaN per expression
    - Lose pile filtering: NaN = FAIL (does not pass the condition)
    - Validation: NaN = FAIL

  Parallelized matrix extraction via ProcessPoolExecutor (full CPU usage).

RAILWAY UPLOAD:
  Appends proximity conditions to the current cycle's cycle_conditions.
  Does NOT re-upload signals — the existing classified signal set is unchanged.
  Downstream steps (regime model, health check) work from the same signal set.

Usage:
    python scripts/proximity_grinder.py --setup dtss
    python scripts/proximity_grinder.py --setup dtss --beam 10000 --depth 100
    python scripts/proximity_grinder.py --setup dtss --dry-run
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


# ══════════════════════════════════════════════════════════════
# DATA LOADING — reads from refinement grinder local output
# ══════════════════════════════════════════════════════════════

def load_piles_from_refinement(setup_type):
    """Load winner/loser piles from the refinement grinder's output file.

    Reads the latest refinement_*.json from local_runner/cache/.
    The refinement grinder outputs winner_signals and loser_signals
    (losers that survived refinement conditions).

    Returns (win_pile, lose_pile).
    """
    import glob

    cache_dir = os.path.join(LOCAL_DIR, "cache")
    pattern = os.path.join(cache_dir, f"refinement_{setup_type}_*.json")
    files = sorted(glob.glob(pattern))

    if not files:
        raise RuntimeError(
            f"No refinement grinder output found: {pattern}\n"
            f"Run step 4 first: python local_runner/pyramid_grinder.py --setup {setup_type} --blackout"
        )

    # Latest by filename (timestamp-sorted)
    latest = files[-1]
    print(f"  Loading: {os.path.basename(latest)}")

    with open(latest) as f:
        data = json.load(f)

    win_pile = data.get("winner_signals", [])
    lose_pile = data.get("loser_signals", [])

    if not win_pile:
        raise RuntimeError(
            f"No winner_signals in {latest}.\n"
            f"Re-run the refinement grinder to get updated output with signal lists."
        )

    print(f"  Win pile:  {len(win_pile):,} (winners — untouchable)")
    print(f"  Lose pile: {len(lose_pile):,} (surviving losers — target to trim)")

    pre_conditions = data.get("all_conditions", [])
    exit_cond = data.get("exit_condition")
    print(f"  Pre-proximity conditions: {len(pre_conditions)}")
    if exit_cond:
        print(f"  Exit: {exit_cond.get('expression')} {exit_cond.get('direction')} {exit_cond.get('threshold')}")

    return win_pile, lose_pile, pre_conditions, exit_cond


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
# BEAM SEARCH — minimize remaining trimmable signals
# ══════════════════════════════════════════════════════════════

def run_beam_search(trim_matrix, win_ranges, expressions,
                    beam_width=10000, depth=100):
    """Beam search to minimize remaining trimmable signals.

    trim_matrix: (n_lose, n_expr) — lose pile values
    win_ranges: {expr_name: (low, high)} — conditions must stay within these

    NaN handling: NaN = FAIL (does not pass). Matches pyramid_grinder behavior.
    A trim signal with NaN for a condition will be filtered OUT (trimmed),
    which is correct — if a signal can't be evaluated, it doesn't survive.

    Score = number of remaining rows (minimize).
    """
    n_rows, n_expr = trim_matrix.shape
    expr_names = [e["name"] for e in expressions]

    print(f"\n  Beam Search:")
    print(f"    Trimmable rows: {n_rows:,}")
    print(f"    Candidate expressions: {len(win_ranges):,}")
    print(f"    Beam: {beam_width:,}, Depth: {depth}")

    # Precompute: for each candidate, which trim rows pass (within win range)?
    # NaN = FAIL — matches pyramid_grinder line 312
    cand_indices = []
    cand_passes = []

    for j, name in enumerate(expr_names):
        if name not in win_ranges:
            continue
        lo, hi = win_ranges[name]
        vals = trim_matrix[:, j]
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
        print("    No useful candidates. Cannot trim further.")
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
            "tier": "proximity",
            "filter_power": None,
        })
    return conditions


# ══════════════════════════════════════════════════════════════
# VALIDATION — NaN = FAIL, matches pyramid_grinder
# ══════════════════════════════════════════════════════════════

def validate_win_pile(win_matrix, proximity_conditions, expressions):
    """Verify every win pile signal passes all proximity conditions.

    NaN = FAIL — matches pyramid_grinder's locked condition application
    (line 312: in_range[np.isnan(series)] = False).

    This MUST pass 100%. If it fails, the grinder has a bug.
    """
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    n_win = win_matrix.shape[0]
    failures = []

    for cond in proximity_conditions:
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
          f"{len(proximity_conditions)} proximity conditions = 100% pass")
    return True


# ══════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════

def compute_metrics(win_pile, lose_pile, trim_matrix,
                    proximity_conditions, expressions):
    """Compute trim metrics with NaN = FAIL."""
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    n_lose = len(lose_pile)

    # How many losers trimmed?
    lose_mask = np.ones(n_lose, dtype=bool)
    for cond in proximity_conditions:
        j = expr_name_to_idx[cond["name"]]
        vals = trim_matrix[:, j]
        in_range = (vals >= cond["low"]) & (vals <= cond["high"])
        in_range[np.isnan(vals)] = False
        lose_mask &= in_range
    losers_remaining = int(np.sum(lose_mask))
    losers_trimmed = n_lose - losers_remaining

    # Win rate before/after
    n_winners = len(win_pile)
    old_total = n_winners + n_lose
    new_total = n_winners + losers_remaining
    old_wr = n_winners / max(old_total, 1) * 100
    new_wr = n_winners / max(new_total, 1) * 100

    return {
        "win_pile": n_winners,
        "lose_pile_before": n_lose,
        "losers_trimmed": losers_trimmed,
        "losers_remaining": losers_remaining,
        "old_total": old_total,
        "new_total": new_total,
        "old_win_rate_pct": round(old_wr, 1),
        "new_win_rate_pct": round(new_wr, 1),
        "n_proximity_conditions": len(proximity_conditions),
    }


# ══════════════════════════════════════════════════════════════
# SAVE + RAILWAY UPLOAD
# ══════════════════════════════════════════════════════════════

def save_results(setup_type, proximity_conditions, metrics):
    """Save proximity grind results locally."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n_conds = len(proximity_conditions)

    output = {
        "setup_type": setup_type,
        "timestamp": ts,
        "step": "proximity_grind",
        "n_proximity_conditions": n_conds,
        "proximity_conditions": proximity_conditions,
        "metrics": metrics,
    }

    out_dir = os.path.join(REPO_ROOT, "data", "proximity_grind")
    os.makedirs(out_dir, exist_ok=True)

    fname = f"proximity_{setup_type}_{n_conds}cond_{ts}.json"
    path = os.path.join(out_dir, fname)
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {path}")

    latest = os.path.join(out_dir, f"proximity_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Latest: {latest}")

    from file_mirror import mirror_file
    mirror_file(path)
    mirror_file(latest)

    return output, path


def upload_to_railway(setup_type, proximity_conditions, metrics):
    """Upload proximity conditions to the current cycle's cycle_conditions.

    Replaces any existing proximity conditions (idempotent on re-run).
    Keeps all non-proximity conditions intact.
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

    # Strip old proximity conditions, keep everything else
    kept = [c for c in existing if c.get("tier") != "proximity"]
    max_sort = max((c.get("sort_order", 0) for c in kept), default=0)

    # Append new proximity conditions
    new_conds = [
        {
            "tier": "proximity",
            "expression_name": c.get("expression_name", c.get("name", "")),
            "low": c["low"],
            "high": c["high"],
            "filter_power": c.get("filter_power"),
            "sort_order": max_sort + 1 + i,
        }
        for i, c in enumerate(proximity_conditions)
    ]

    all_conds = kept + new_conds

    try:
        payload = {"conditions": all_conds}
        r = requests.post(
            f"{API_BASE}/api/v2/cycles/{cycle_id}/conditions",
            json=payload, timeout=30
        )
        r.raise_for_status()
        print(f"  Uploaded {len(kept)} existing + {len(new_conds)} proximity "
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
        n_prox = sum(1 for c in stored if c.get("tier") == "proximity")
        print(f"  Verified: {len(stored)} total conditions ({n_prox} proximity)")
    except Exception as e:
        print(f"  Verification failed: {e}")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run_proximity_grind(setup_type, beam_width=10000, depth=100, dry_run=False):
    print(f"\n{'=' * 70}")
    print(f"  PROXIMITY GRINDER — {setup_type.upper()}")
    print(f"{'=' * 70}\n")

    t0 = time.time()

    # ── 1. Load piles from refinement grinder output ──
    print("Phase 1: Loading data from refinement grinder output...")
    win_pile, lose_pile, pre_conditions, exit_cond = load_piles_from_refinement(setup_type)

    if dry_run:
        print(f"\n  DRY RUN — stopping here. ({time.time() - t0:.0f}s)")
        return

    if len(win_pile) == 0:
        print("\n  ERROR: Win pile is empty. Cannot grind.")
        return
    if len(lose_pile) == 0:
        print("\n  Nothing to trim — lose pile is empty.")
        return

    # ── 3. Load expr cache + expressions ──
    print("\nPhase 2: Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found. "
                           "Run: python local_runner/expr_cache_builder.py --build")

    expressions = generate_all()
    print(f"  Expressions: {len(expressions):,}")
    print(f"  Expr cache: {expr_cache.n_expressions:,} expressions")

    # ── 4. Extract values (parallelized) ──
    print("\nPhase 3a: Extracting win pile expression values...")
    win_matrix = extract_signal_values_parallel(win_pile, expressions, expr_cache)

    print("\nPhase 3b: Extracting lose pile expression values...")
    trim_matrix = extract_signal_values_parallel(lose_pile, expressions, expr_cache)

    # ── 5. Compute win ranges (pyramid_grinder-compatible) ──
    print("\nPhase 4: Computing win pile ranges...")
    win_ranges = compute_win_ranges(win_matrix, expressions)

    if not win_ranges:
        print("  ERROR: No expressions with full win pile coverage.")
        return

    # ── 6. Beam search ──
    print("\nPhase 5: Beam search...")
    proximity_conditions, remaining = run_beam_search(
        trim_matrix, win_ranges, expressions,
        beam_width=beam_width, depth=depth,
    )

    if not proximity_conditions:
        print("\n  No conditions found. Cannot trim further.")
        return

    # ── 7. Validate (NaN = FAIL, hard abort on failure) ──
    print("\nPhase 6: Validating win pile (NaN = FAIL)...")
    if not validate_win_pile(win_matrix, proximity_conditions, expressions):
        print("\n  CRITICAL: Validation failed. Aborting — grinder has a bug.")
        return

    # ── 8. Compute metrics ──
    metrics = compute_metrics(
        win_pile, lose_pile, trim_matrix,
        proximity_conditions, expressions
    )

    # ── 9. Print beam search results ──
    print(f"\n{'=' * 70}")
    print(f"  PROXIMITY GRIND RESULTS — {setup_type.upper()}")
    print(f"{'=' * 70}")
    print(f"  Proximity conditions: {len(proximity_conditions)}")
    for c in proximity_conditions:
        print(f"    {c['name']:40s}  [{c['low']:.4f}, {c['high']:.4f}]  ({c['category']})")
    print(f"\n  Win pile (untouched):    {metrics['win_pile']:,}")
    print(f"  Lose pile:              {metrics['lose_pile_before']:,} -> "
          f"{metrics['losers_remaining']:,} (-{metrics['losers_trimmed']:,})")
    print(f"\n  Total signals:          {metrics['old_total']:,} -> "
          f"{metrics['new_total']:,}")
    print(f"  Win rate:               {metrics['old_win_rate_pct']:.1f}% -> "
          f"{metrics['new_win_rate_pct']:.1f}%")

    # ── 10. Combine conditions + re-scan ──
    print(f"\n  ── COMBINE + RE-SCAN ──")

    combined_conditions = None
    rescan_winners = None
    rescan_losers = None
    rescan_sacrificial = None

    if pre_conditions and exit_cond:
        # Combine pre-proximity conditions + proximity conditions
        pre_names = {c["name"] for c in pre_conditions}
        prox_names = {c["name"] for c in proximity_conditions}
        overlap = pre_names & prox_names

        combined_conditions = list(pre_conditions)
        for pc in proximity_conditions:
            if pc["name"] in overlap:
                combined_conditions = [c for c in combined_conditions if c["name"] != pc["name"]]
            combined_conditions.append(pc)

        print(f"  Combined: {len(pre_conditions)} pre + {len(proximity_conditions)} proximity "
              f"({len(overlap)} overlap) = {len(combined_conditions)} total")

        # Re-scan using signal_filter's scan function
        # Free beam search data first to reduce memory pressure
        del win_matrix, trim_matrix
        import gc; gc.collect()

        import pickle
        _5yr_path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
        if not os.path.exists(_5yr_path):
            _5yr_path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
        print(f"  Loading 5yr cache...")
        with open(_5yr_path, "rb") as _f:
            _universe = pickle.load(_f)
        print(f"  Loaded {len(_universe):,} tickers")

        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        from signal_filter import scan_all_signals as _scan_all, _build_slim_cache
        from multiprocessing import cpu_count as _cpu_count

        # Build slim cache, free full cache, then scan
        _slim = _build_slim_cache(_universe)
        del _universe; gc.collect()

        _workers = max(_cpu_count() - 1, 1)
        _raw = _scan_all(_slim, combined_conditions, _workers, expr_cache)
        del _slim; gc.collect()

        # Reload full cache for exit/classify
        with open(_5yr_path, "rb") as _f:
            _universe = pickle.load(_f)

        # Dedup with sacrificial
        _raw.sort(key=lambda s: (s["ticker"], s["bar_idx"]))
        _deduped = []
        rescan_sacrificial = []
        _i = 0
        while _i < len(_raw):
            _j = _i + 1
            _tk = _raw[_i]["ticker"]
            while _j < len(_raw):
                if _raw[_j]["ticker"] != _tk:
                    break
                if _raw[_j]["bar_idx"] != _raw[_j-1]["bar_idx"] + 1:
                    break
                _j += 1
            _rm = _raw[_j-1]
            _rm["cluster_size"] = _j - _i
            _rm["cluster_start_date"] = _raw[_i]["date"]
            _deduped.append(_rm)
            for _k in range(_i, _j - 1):
                rescan_sacrificial.append(_raw[_k])
            _i = _j

        print(f"  Re-scan: {len(_raw):,} raw → {len(_deduped):,} deduped + "
              f"{len(rescan_sacrificial):,} sacrificial")

        # Classify: example bars from win pile
        _example_bars = {}
        for _sig in win_pile:
            if _sig.get("is_example"):
                _t = _sig["ticker"]
                _b = _sig.get("bar_idx")
                if _b is not None:
                    if _t not in _example_bars:
                        _example_bars[_t] = set()
                    _example_bars[_t].add(_b)

        # Apply exit + classify
        _exit_expr = exit_cond["expression"]
        _exit_thresh = exit_cond["threshold"]
        _exit_dir = exit_cond["direction"]
        _exit_col = expr_cache.expr_index(_exit_expr)
        _adr_col = expr_cache.expr_index("adr14")
        _MAX_FWD = 120

        _with_exit = []
        _tcache = {}
        _prev_tk = None
        for _sig in _deduped:
            _ticker = _sig["ticker"]
            _bar_idx = _sig["bar_idx"]
            _df = _universe.get(_ticker)
            if _df is None or _bar_idx >= len(_df) - 1:
                continue
            try:
                if _ticker not in _tcache:
                    if _prev_tk and _prev_tk != _ticker and _prev_tk in _tcache:
                        del _tcache[_prev_tk]
                    _tcache[_ticker] = expr_cache.get_ticker(_ticker)
                _prev_tk = _ticker
                _cd, _cdata = _tcache[_ticker]
                if _cd is None or len(_cd) != len(_df):
                    continue
                _adr = float(_cdata[_bar_idx, _adr_col]) if _adr_col is not None else 0
                if _adr <= 0 or np.isnan(_adr):
                    continue
                _sc = float(_df["close"].values[_bar_idx])
                _fwd = min(_MAX_FWD, len(_df) - _bar_idx - 1)
                if _fwd < 5:
                    continue
                _es = _cdata[:, _exit_col]
                _eb = None
                for _f in range(1, _fwd + 1):
                    _v = _es[_bar_idx + _f]
                    if np.isnan(_v):
                        continue
                    if _exit_dir in (">=", "above") and _v >= _exit_thresh:
                        _eb = _f; break
                    elif _exit_dir in ("<=", "below") and _v <= _exit_thresh:
                        _eb = _f; break
                if _eb is None:
                    continue
                _ec = float(_df["close"].values[_bar_idx + _eb])
                _move = (_sc - _ec) / _adr  # short direction
                _with_exit.append({**_sig, "move_adr": round(_move, 2),
                                   "exit_triggered": True})
            except Exception:
                continue

        _exit_lk = {(_s["ticker"], _s["bar_idx"]): _s for _s in _with_exit}
        _exit_adrs = [_s["move_adr"] for _s in _with_exit if _s.get("move_adr") is not None]
        _med_adr = sorted(_exit_adrs)[len(_exit_adrs) // 2] if _exit_adrs else 5.0

        rescan_winners = []
        rescan_losers = []
        for _sig in _deduped:
            _t = _sig["ticker"]
            _b = _sig["bar_idx"]
            _is_ex = 1 if (_t in _example_bars and _b in _example_bars[_t]) else 0
            _ed = _exit_lk.get((_t, _b))
            if _is_ex:
                _cls = "AUTO_WIN"
            elif _ed and _ed.get("move_adr", 0) >= _med_adr:
                _cls = "AUTO_WIN"
            else:
                _cls = "AUTO_LOSS"
            _row = {
                "ticker": _t, "signal_date": _sig["date"], "bar_idx": _b,
                "close": _sig.get("close"), "is_example": _is_ex,
                "classification": _cls,
                "exit_triggered": 1 if _ed else 0,
                "move_adr": _ed.get("move_adr") if _ed else None,
            }
            if _cls == "AUTO_WIN":
                rescan_winners.append(_row)
            else:
                rescan_losers.append(_row)

        _nw = len(rescan_winners)
        _nl = len(rescan_losers)
        print(f"  Re-classified: {_nw} winners / {_nl} losers "
              f"({_nw/max(_nw+_nl,1)*100:.1f}% WR)")

        del _universe  # free memory
    else:
        print(f"  WARNING: No pre-conditions or exit condition — skipping re-scan.")

    total_time = time.time() - t0

    # ── 11. Save locally ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    n_prox = len(proximity_conditions)

    output = {
        "setup_type": setup_type,
        "timestamp": ts,
        "total_time_s": round(total_time, 1),
        "step": "proximity_grind",
        "n_conditions": len(combined_conditions) if combined_conditions else len(pre_conditions),
        "n_pre_conditions": len(pre_conditions) if pre_conditions else 0,
        "n_proximity_conditions": n_prox,
        "all_conditions": combined_conditions if combined_conditions else pre_conditions,
        "proximity_conditions_only": proximity_conditions,
        "exit_condition": exit_cond,
        "params": {"beam_width": beam_width, "depth": depth},
        "summary": metrics,
        "winner_signals": rescan_winners if rescan_winners is not None else [],
        "loser_signals": rescan_losers if rescan_losers is not None else [],
        "sacrificial_signals": rescan_sacrificial if rescan_sacrificial is not None else [],
    }

    out_dir = os.path.join(REPO_ROOT, "data", "proximity_grind")
    os.makedirs(out_dir, exist_ok=True)

    fname = f"proximity_{setup_type}_{n_prox}cond_{ts}.json"
    path = os.path.join(out_dir, fname)
    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Saved: {path}")

    latest = os.path.join(out_dir, f"proximity_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Latest: {latest}")

    from file_mirror import mirror_file
    mirror_file(path)
    mirror_file(latest)

    # Upload to Railway cycle
    try:
        from grind_uploader import upload as railway_upload
        railway_upload(
            result=output,
            result_path=path,
            step_type="proximity_grind",
            setup_type=setup_type,
            activate=True,
        )
    except Exception as e:
        print(f"\n  WARNING: Railway upload failed: {e}")
        print(f"  Local file saved.")

    print(f"\n  {'='*70}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  {'='*70}\n")

    return output


def main():
    parser = argparse.ArgumentParser(description="Proximity Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--beam", type=int, default=10000, help="Beam width")
    parser.add_argument("--depth", type=int, default=100, help="Search depth")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pile stats only, don't grind")
    args = parser.parse_args()

    run_proximity_grind(
        setup_type=args.setup,
        beam_width=args.beam,
        depth=args.depth,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
