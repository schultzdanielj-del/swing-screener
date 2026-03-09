"""
Proximity Grinder — Trim losers by leveraging sacrificial signal bars.

Post-convergence step. Finds conditions that all win pile signals pass but that
eliminate lose pile signals. Sacrificial signals (leftward duplicates from dedup)
provide analytical leverage — they're structurally similar to losers (early/premature
fires) so conditions that kill sacrificial signals also kill losers.

THREE PILES:
  Win pile (100% must pass, untouchable):
    - Deduped winner signals from refinement grind (exit triggered + move >= ADR)
    - These are the rightmost signal bar per consecutive cluster

  Sacrifice pile (OK to trim):
    - Pre-dedup leftward duplicates from ALL clusters (winner and loser)
    - These got deduped out (collapsed into the rightmost bar)
    - Gives the grinder more foothold to find discriminating conditions

  Lose pile (target to trim):
    - Deduped loser signals from refinement grind (no exit / move < ADR)

DATA CONGRUENCE:
  All signal data comes from setup_refiner output (refined_{setup}.json).
  No re-scanning, no re-classifying. The proximity grinder reads the exact
  same signals and classifications the refiner produced. This ensures all
  pipeline steps calculate data the same way.

  The refined output must contain:
    - signals: filtered winners (exit triggered + move >= ADR)
    - all_deduped_classified: full deduped set (winners + losers)
    - sacrificial_signals: leftward duplicate bars from dedup

  If these are empty, re-run: python scripts/setup_refiner.py --setup {setup}

Usage:
    python scripts/proximity_grinder.py --setup dtss
    python scripts/proximity_grinder.py --setup dtss --beam 5000 --depth 50
    python scripts/proximity_grinder.py --setup dtss --dry-run  # show piles only
"""

import argparse
import os
import sys
import time
import json
import numpy as np
from datetime import datetime

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


# ══════════════════════════════════════════════════════════════
# DATA LOADING — read from setup_refiner output, no re-computation
# ══════════════════════════════════════════════════════════════

def load_refined_result(setup_type):
    """Load the refinement grind output with full signal data.

    Requires all_deduped_classified and sacrificial_signals to be populated.
    If empty, the setup_refiner needs to be re-run.
    """
    path = os.path.join(REPO_ROOT, "data", "setup_refiner", f"refined_{setup_type}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No refinement grind output found: {path}\n"
            f"  Run: python scripts/setup_refiner.py --setup {setup_type}"
        )
    with open(path) as f:
        data = json.load(f)

    winners = data.get("signals", [])
    all_deduped = data.get("all_deduped_classified", [])
    sacrificial = data.get("sacrificial_signals", [])
    conditions = data.get("pruned_conditions", [])

    if not all_deduped or not sacrificial:
        raise RuntimeError(
            f"Refined output missing full signal data.\n"
            f"  all_deduped_classified: {len(all_deduped)}\n"
            f"  sacrificial_signals: {len(sacrificial)}\n"
            f"  Re-run: python scripts/setup_refiner.py --setup {setup_type}"
        )

    print(f"  Loaded refined result: {len(conditions)} conditions")
    print(f"    Winners (filtered):      {len(winners)}")
    print(f"    All deduped (classified): {len(all_deduped)}")
    print(f"    Sacrificial (pre-dedup):  {len(sacrificial)}")

    return winners, all_deduped, sacrificial, conditions, data


# ══════════════════════════════════════════════════════════════
# PILE BUILDER — construct win/sacrifice/lose from refiner data
# ══════════════════════════════════════════════════════════════

def build_piles(winners, all_deduped, sacrificial):
    """Build three piles from setup_refiner output.

    Win pile: deduped winners (from filtered signals — exit triggered + move >= ADR)
    Sacrifice pile: leftward duplicates from dedup (from sacrificial_signals)
    Lose pile: deduped signals NOT in winner set (from all_deduped_classified minus winners)
    """
    # Build a set of winner keys for fast lookup
    winner_keys = set()
    for w in winners:
        key = f"{w['ticker']}|{w['bar_idx']}"
        winner_keys.add(key)

    # Losers = all deduped signals not in winner set
    losers = []
    for sig in all_deduped:
        key = f"{sig['ticker']}|{sig['bar_idx']}"
        if key not in winner_keys:
            losers.append(sig)

    print(f"\n  ── Pile Summary ──")
    print(f"  Win pile:       {len(winners):,} deduped winners (untouchable)")
    print(f"  Sacrifice pile: {len(sacrificial):,} leftward duplicates")
    print(f"  Lose pile:      {len(losers):,} deduped losers (target)")
    print(f"  Total trimmable: {len(sacrificial) + len(losers):,}")

    return winners, sacrificial, losers


# ══════════════════════════════════════════════════════════════
# EXPRESSION VALUE EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_signal_values(signals, expressions, expr_cache):
    """Pull expression values from expr cache for each signal bar.

    Returns np.array (n_signals, n_expressions) float32.
    """
    n_sig = len(signals)
    n_expr = len(expressions)
    matrix = np.full((n_sig, n_expr), np.nan, dtype=np.float32)

    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    expr_to_cache_col = []
    for e in expressions:
        expr_to_cache_col.append(cache_name_to_idx.get(e["name"]))

    _ticker_cache = {}
    n_loaded = 0

    for i, sig in enumerate(signals):
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]

        if ticker not in _ticker_cache:
            dates, data = expr_cache.get_ticker(ticker)
            _ticker_cache[ticker] = (dates, data)
        cached_dates, cached_data = _ticker_cache[ticker]

        if cached_dates is None or bar_idx >= len(cached_data):
            continue

        row = cached_data[bar_idx, :]
        for j, cache_col in enumerate(expr_to_cache_col):
            if cache_col is not None and cache_col < len(row):
                matrix[i, j] = row[cache_col]
        n_loaded += 1

    pct = n_loaded / max(n_sig, 1) * 100
    print(f"  Extracted values for {n_loaded}/{n_sig} signals ({pct:.0f}%)")
    return matrix


# ══════════════════════════════════════════════════════════════
# BEAM SEARCH — find conditions that trim sacrifice + lose pile
# ══════════════════════════════════════════════════════════════

def compute_win_ranges(win_matrix, expressions):
    """Compute [min, max] range with 5% margin for each expression across win pile.

    Only expressions where ALL win pile signals have valid (non-NaN) values
    can be used as conditions — same rule as pyramid grinder.
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

    print(f"  Win ranges: {len(ranges)} expressions with full coverage "
          f"(out of {len(expressions)})")
    return ranges


def run_beam_search(trim_matrix, win_ranges, expressions,
                    beam_width=5000, depth=50):
    """Beam search to minimize remaining trimmable signals.

    trim_matrix: (n_trim, n_expr) — sacrifice + lose pile values
    win_ranges: {expr_name: (low, high)} — conditions must stay within these

    Score = number of remaining rows (minimize).
    """
    n_rows, n_expr = trim_matrix.shape
    expr_names = [e["name"] for e in expressions]

    print(f"\n  ── Beam Search ──")
    print(f"  Trimmable rows: {n_rows:,}")
    print(f"  Candidate expressions: {len(win_ranges):,}")
    print(f"  Beam: {beam_width:,}, Depth: {depth}")

    # Precompute: for each candidate, which trim rows pass (within win range)?
    cand_indices = []
    cand_passes = []

    for j, name in enumerate(expr_names):
        if name not in win_ranges:
            continue
        lo, hi = win_ranges[name]
        vals = trim_matrix[:, j]
        passes = ((vals >= lo) & (vals <= hi)) | np.isnan(vals)
        n_pass = int(np.sum(passes))
        # Useful if it filters out at least 1% of rows
        if n_pass < n_rows * 0.99:
            cand_indices.append(j)
            cand_passes.append(passes)

    n_useful = len(cand_indices)
    print(f"  Useful candidates (filter >= 1%): {n_useful}")

    if n_useful == 0:
        print("  No useful candidates. Cannot trim further.")
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
    print(f"  Level 1: {best.remaining:,} remaining "
          f"(-{trimmed:,}, {trimmed/max(n_rows,1)*100:.1f}%)")

    # Deepen
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
                next_level.append(Node(conditions=combo, row_mask=mask, remaining=remaining))

                if len(next_level) >= beam_width * 8:
                    break
            if len(next_level) >= beam_width * 8:
                break

        if not next_level:
            print(f"  Level {lv}: ceiling")
            break

        next_level.sort(key=lambda n: n.remaining)
        current_level = next_level[:beam_width]

        if current_level[0].remaining < best.remaining:
            best = current_level[0]

        trimmed = n_rows - best.remaining
        print(f"  Level {lv}: {best.remaining:,} remaining "
              f"(-{trimmed:,}, {trimmed/max(n_rows,1)*100:.1f}%) "
              f"[{len(best.conditions)} conds]")

        if best.remaining == 0:
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
# VALIDATION — confirm 100% win pile passes all conditions
# ══════════════════════════════════════════════════════════════

def validate_win_pile(win_matrix, proximity_conditions, expressions):
    """Verify every win pile signal passes all proximity conditions."""
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    n_win = win_matrix.shape[0]
    failures = []

    for cond in proximity_conditions:
        j = expr_name_to_idx.get(cond["name"])
        if j is None:
            failures.append(f"  {cond['name']}: not in expression list")
            continue
        vals = win_matrix[:, j]
        in_range = ((vals >= cond["low"]) & (vals <= cond["high"])) | np.isnan(vals)
        n_fail = int(np.sum(~in_range))
        if n_fail > 0:
            failures.append(f"  {cond['name']}: {n_fail}/{n_win} win signals FAIL")

    if failures:
        print("\n  VALIDATION FAILED:")
        for f in failures:
            print(f)
        return False

    print(f"\n  Validation passed: {n_win} win pile signals x "
          f"{len(proximity_conditions)} proximity conditions = 100% pass")
    return True


# ══════════════════════════════════════════════════════════════
# SAVE
# ══════════════════════════════════════════════════════════════

def save_results(setup_type, proximity_conditions, metrics, refined_conditions):
    """Save proximity grind results."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_conds = len(proximity_conditions)

    output = {
        "setup_type": setup_type,
        "timestamp": ts,
        "step": "proximity_grind",
        "n_proximity_conditions": n_conds,
        "n_refined_conditions": len(refined_conditions),
        "n_total_conditions": len(refined_conditions) + n_conds,
        "proximity_conditions": proximity_conditions,
        "all_conditions": refined_conditions + proximity_conditions,
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

    return output


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run_proximity_grind(setup_type, beam_width=5000, depth=50, dry_run=False):
    print(f"\n{'='*70}")
    print(f"  PROXIMITY GRINDER — {setup_type.upper()}")
    print(f"{'='*70}\n")

    t0 = time.time()

    # ── 1. Load refined data (no re-computation) ──
    print("Phase 1: Loading refined data...")
    winners, all_deduped, sacrificial, conditions, refined_data = \
        load_refined_result(setup_type)

    # ── 2. Build piles ──
    print("\nPhase 2: Building piles...")
    win_pile, sacrifice_pile, lose_pile = build_piles(
        winners, all_deduped, sacrificial
    )

    if dry_run:
        print(f"\n  DRY RUN — stopping here. ({time.time()-t0:.0f}s)")
        return

    if len(win_pile) == 0:
        print("\n  ERROR: Win pile is empty.")
        return
    if len(lose_pile) == 0:
        print("\n  Lose pile is empty — nothing to trim.")
        return

    # ── 3. Load expr cache + expressions ──
    print("\nPhase 3: Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found. "
                           "Run: python local_runner/expr_cache_builder.py --build")

    expressions = generate_all()
    print(f"  Expressions: {len(expressions):,}")

    # ── 4. Extract values ──
    print("\nPhase 4: Extracting expression values...")
    win_matrix = extract_signal_values(win_pile, expressions, expr_cache)

    trim_signals = sacrifice_pile + lose_pile
    trim_matrix = extract_signal_values(trim_signals, expressions, expr_cache)
    n_sacrifice = len(sacrifice_pile)
    n_lose = len(lose_pile)

    # ── 5. Compute win ranges ──
    print("\nPhase 5: Computing win pile ranges...")
    win_ranges = compute_win_ranges(win_matrix, expressions)

    # ── 6. Beam search ──
    print("\nPhase 6: Beam search...")
    proximity_conditions, remaining = run_beam_search(
        trim_matrix, win_ranges, expressions,
        beam_width=beam_width, depth=depth,
    )

    if not proximity_conditions:
        print("\n  No conditions found. Cannot trim further.")
        return

    # ── 7. Validate ──
    print("\nPhase 7: Validating win pile...")
    if not validate_win_pile(win_matrix, proximity_conditions, expressions):
        print("\n  CRITICAL: Validation failed. Aborting.")
        return

    # ── 8. Compute metrics ──
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}

    # How many losers trimmed?
    lose_matrix = trim_matrix[n_sacrifice:, :]
    lose_mask = np.ones(n_lose, dtype=bool)
    for cond in proximity_conditions:
        j = expr_name_to_idx[cond["name"]]
        vals = lose_matrix[:, j]
        in_range = ((vals >= cond["low"]) & (vals <= cond["high"])) | np.isnan(vals)
        lose_mask &= in_range
    losers_remaining = int(np.sum(lose_mask))
    losers_trimmed = n_lose - losers_remaining

    # How many sacrificial trimmed?
    sac_matrix = trim_matrix[:n_sacrifice, :]
    sac_mask = np.ones(n_sacrifice, dtype=bool)
    for cond in proximity_conditions:
        j = expr_name_to_idx[cond["name"]]
        vals = sac_matrix[:, j]
        in_range = ((vals >= cond["low"]) & (vals <= cond["high"])) | np.isnan(vals)
        sac_mask &= in_range
    sac_remaining = int(np.sum(sac_mask))
    sac_trimmed = n_sacrifice - sac_remaining

    # Win rate before/after
    n_winners = len(win_pile)
    old_total = n_winners + n_lose
    new_total = n_winners + losers_remaining
    old_wr = n_winners / max(old_total, 1) * 100
    new_wr = n_winners / max(new_total, 1) * 100

    metrics = {
        "win_pile": n_winners,
        "sacrifice_before": n_sacrifice,
        "sacrifice_trimmed": sac_trimmed,
        "lose_pile_before": n_lose,
        "losers_trimmed": losers_trimmed,
        "losers_remaining": losers_remaining,
        "old_total_deduped": old_total,
        "new_total_deduped": new_total,
        "old_win_rate_pct": round(old_wr, 1),
        "new_win_rate_pct": round(new_wr, 1),
        "n_proximity_conditions": len(proximity_conditions),
    }

    # ── 9. Print results ──
    print(f"\n{'='*70}")
    print(f"  PROXIMITY GRIND RESULTS — {setup_type.upper()}")
    print(f"{'='*70}")
    print(f"  Proximity conditions: {len(proximity_conditions)}")
    for c in proximity_conditions:
        print(f"    {c['name']:40s}  [{c['low']:.4f}, {c['high']:.4f}]  ({c['category']})")
    print(f"\n  Win pile (untouched):    {n_winners:,}")
    print(f"  Sacrifice pile:         {n_sacrifice:,} -> {sac_remaining:,} (-{sac_trimmed:,})")
    print(f"  Lose pile:              {n_lose:,} -> {losers_remaining:,} (-{losers_trimmed:,})")
    print(f"\n  Deduped signals:        {old_total:,} -> {new_total:,}")
    print(f"  Win rate:               {old_wr:.1f}% -> {new_wr:.1f}%")
    print(f"\n  Time: {time.time()-t0:.0f}s")

    # ── 10. Save ──
    save_results(setup_type, proximity_conditions, metrics, conditions)

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Proximity Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--beam", type=int, default=5000, help="Beam width")
    parser.add_argument("--depth", type=int, default=50, help="Search depth")
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
