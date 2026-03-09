"""
Proximity Grinder — Trim losers by leveraging sacrificial signal bars.

Post-convergence step. Finds conditions that all win pile signals pass but that
eliminate lose pile signals. Sacrificial signals (leftward duplicates from dedup)
provide analytical leverage — they're structurally similar to losers (early/premature
fires) so conditions that kill sacrificial signals also kill losers.

THREE PILES:
  Win pile (100% must pass, untouchable):
    - Deduped winner signals (exit triggered + move >= ADR threshold)
    - These are the rightmost signal bar per consecutive cluster

  Sacrifice pile (OK to trim):
    - Pre-dedup leftward duplicates from winner AND loser clusters
    - These got deduped out (collapsed into the rightmost bar)
    - Gives the grinder more foothold to find discriminating conditions

  Lose pile (target to trim):
    - Deduped loser signals (no exit triggered, or move < ADR threshold)

PROCESS:
  1. Load refined conditions from setup_refiner output
  2. Re-scan full universe with those conditions → raw signals
  3. Dedup, keeping track of sacrificial (leftward) signals
  4. Apply exit filter to classify deduped signals as winner/loser
  5. Build win pile / sacrifice pile / lose pile
  6. Pull expression values from expr cache for all signal bars
  7. Compute value ranges from win pile (5% margin)
  8. Build matrix of (sacrifice + lose) rows × candidate expressions
  9. Beam search: find conditions within win pile ranges that trim the most rows
  10. Validate: 100% win pile still passes
  11. Output: proximity conditions + before/after metrics

Usage:
    python scripts/proximity_grinder.py --setup dtss
    python scripts/proximity_grinder.py --setup dtss --beam 5000 --depth 50
    python scripts/proximity_grinder.py --setup dtss --dry-run  # show piles only

Requires:
  - Refinement grind output in data/setup_refiner/refined_{setup}.json
  - 5yr OHLCV cache
  - Expression series cache
  - Examples from Railway API
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
from datetime import datetime
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

RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD = 120
EXCLUDED_TICKERS = {"BRK-B", "SMMT", "VUZI", "SERV", "SOUN"}

SETUP_CONFIGS = {
    "dtss": {"direction": "short"},
}


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    print(f"  Loading 5yr cache...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_refined_result(setup_type):
    """Load the latest refinement grind output."""
    path = os.path.join(REPO_ROOT, "data", "setup_refiner", f"refined_{setup_type}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No refinement grind output found: {path}\n"
            f"  Run: python scripts/setup_refiner.py --setup {setup_type}"
        )
    with open(path) as f:
        data = json.load(f)
    conditions = data.get("pruned_conditions", [])
    exit_cond = data.get("exit_condition")
    min_adr = data.get("min_adr_threshold", 0)
    print(f"  Loaded refined result: {len(conditions)} conditions, "
          f"{data.get('n_deduped', '?')} deduped signals, "
          f"{data.get('n_filtered', '?')} filtered winners")
    return conditions, exit_cond, min_adr, data


def load_exit_condition(setup_type, refined_data):
    """Extract exit condition from refined data, or load from profit grind."""
    ec = refined_data.get("exit_condition")
    if ec:
        print(f"  Exit condition from refined data: {ec['expression']} "
              f"{ec['direction']} {ec['threshold']}")
        return ec
    # Fallback: load from profit grind
    from setup_refiner import load_exit_condition as _load_exit
    return _load_exit(setup_type)


def load_examples(setup_type):
    try:
        r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}", timeout=30)
        r.raise_for_status()
        examples = r.json().get("examples", [])
        print(f"  Loaded {len(examples)} examples from Railway")
        return examples
    except Exception as e:
        raise RuntimeError(f"Could not load examples: {e}")


# ══════════════════════════════════════════════════════════════
# SCAN — re-scan universe with refined conditions
# ══════════════════════════════════════════════════════════════

_sw_cache = None
_sw_conditions = None
_sw_expr_cache_dir = None
_sw_cond_col_indices = None


def _init_scan_worker(cache, conditions, expr_cache_dir, cond_col_indices):
    global _sw_cache, _sw_conditions, _sw_expr_cache_dir, _sw_cond_col_indices
    _sw_cache = cache
    _sw_conditions = conditions
    _sw_expr_cache_dir = expr_cache_dir
    _sw_cond_col_indices = cond_col_indices


def _load_scan_npz(ticker):
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = os.path.join(_sw_expr_cache_dir, f"{safe}.npz")
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        return loaded["dates"], loaded["data"]
    except Exception:
        return None, None


def _scan_batch(tickers):
    signals = []
    for ticker in tickers:
        df = _sw_cache.get(ticker)
        if df is None or len(df) < 100:
            continue
        try:
            dates_cache, data_cache = _load_scan_npz(ticker)
            if dates_cache is None or len(dates_cache) != len(df):
                continue
            n_bars = len(df)
            mask = np.ones(n_bars, dtype=bool)
            mask[:50] = False
            for i, cond in enumerate(_sw_conditions):
                col_idx = _sw_cond_col_indices[i]
                if col_idx is None:
                    mask[:] = False
                    break
                series = data_cache[:, col_idx]
                in_range = (series >= cond["low"]) & (series <= cond["high"])
                in_range[np.isnan(series)] = False
                mask &= in_range
            for idx in np.where(mask)[0]:
                signals.append({
                    "ticker": ticker,
                    "date": str(df["date"].values[idx])[:10],
                    "bar_idx": int(idx),
                    "close": float(df["close"].values[idx]),
                })
        except Exception:
            pass
    return signals


def scan_signals(cache, conditions, expr_cache, workers):
    tickers = [t for t in cache.keys() if t not in EXCLUDED_TICKERS]
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    cond_col_indices = [expr_cache.expr_index(c["name"]) for c in conditions]
    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    print(f"\n  Scanning {len(tickers):,} tickers x {len(conditions)} conditions  "
          f"({workers} workers)...")
    t0 = time.time()
    all_signals = []

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_scan_worker,
        initargs=(cache, conditions, expr_cache_dir, cond_col_indices)
    ) as pool:
        futures = [pool.submit(_scan_batch, b) for b in batches]
        done = 0
        for f in as_completed(futures):
            all_signals.extend(f.result())
            done += 1
            if done % max(len(batches) // 5, 1) == 0 or done == len(batches):
                pct = done / len(batches) * 100
                print(f"    {pct:.0f}%  {len(all_signals):,} signals  "
                      f"[{time.time()-t0:.0f}s]")

    print(f"  Raw signals: {len(all_signals):,}  ({time.time()-t0:.0f}s)")
    return all_signals


# ══════════════════════════════════════════════════════════════
# DEDUP — keep rightmost, track sacrificial (leftward) signals
# ══════════════════════════════════════════════════════════════

def deduplicate_with_sacrificial(signals):
    """Dedup consecutive signal bars per ticker. Returns (deduped, sacrificial).

    deduped: rightmost bar per cluster (what you'd trade)
    sacrificial: all leftward bars that got collapsed (analytical fuel)
    """
    signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))

    deduped = []
    sacrificial = []
    i = 0
    while i < len(signals):
        j = i + 1
        ticker = signals[i]["ticker"]
        while j < len(signals):
            if signals[j]["ticker"] != ticker:
                break
            if signals[j]["bar_idx"] != signals[j-1]["bar_idx"] + 1:
                break
            j += 1
        # signals[i:j] is a consecutive cluster
        rightmost = signals[j-1]
        rightmost["cluster_size"] = j - i
        rightmost["cluster_start_date"] = signals[i]["date"]
        deduped.append(rightmost)

        # All bars before the rightmost are sacrificial
        for k in range(i, j - 1):
            sacrificial.append(signals[k])

        i = j

    print(f"  Deduped: {len(signals):,} raw → {len(deduped):,} deduped + "
          f"{len(sacrificial):,} sacrificial")
    return deduped, sacrificial


# ══════════════════════════════════════════════════════════════
# EXIT FILTER — classify deduped signals as winner/loser
# ══════════════════════════════════════════════════════════════

def apply_exit_filter(deduped_signals, cache, exit_cond, direction, expr_cache):
    """Apply exit condition to deduped signals. Returns (winners, losers)."""
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]
    exit_col_idx = expr_cache.expr_index(expr_name)
    adr_col_idx = expr_cache.expr_index("adr14")

    if exit_col_idx is None:
        raise RuntimeError(f"Exit expression '{expr_name}' not in expr cache")

    print(f"\n  Applying exit: {expr_name} {exit_dir} {exit_thresh}")

    winners = []
    losers = []
    _ticker_cache = {}

    for sig in deduped_signals:
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        df = cache.get(ticker)
        if df is None or bar_idx >= len(df) - 1:
            losers.append(sig)
            continue

        try:
            if ticker not in _ticker_cache:
                dates, data = expr_cache.get_ticker(ticker)
                _ticker_cache[ticker] = (dates, data)
            cached_dates, cached_data = _ticker_cache[ticker]
            if cached_dates is None or len(cached_dates) != len(df):
                losers.append(sig)
                continue

            signal_close = sig["close"]
            adr_at_signal = float(cached_data[bar_idx, adr_col_idx]) if adr_col_idx is not None else 1.0
            if np.isnan(adr_at_signal) or adr_at_signal <= 0:
                adr_at_signal = 1.0

            # Scan forward for exit trigger
            exit_triggered = False
            exit_bar = None
            max_bars = min(MAX_FORWARD, len(df) - bar_idx - 1)

            for offset in range(1, max_bars + 1):
                check_idx = bar_idx + offset
                exit_val = float(cached_data[check_idx, exit_col_idx])
                if np.isnan(exit_val):
                    continue
                if exit_dir == "<=" and exit_val <= exit_thresh:
                    exit_triggered = True
                    exit_bar = offset
                    break
                elif exit_dir == ">=" and exit_val >= exit_thresh:
                    exit_triggered = True
                    exit_bar = offset
                    break

            if not exit_triggered:
                losers.append(sig)
                continue

            # Measure move
            exit_idx = bar_idx + exit_bar
            exit_close = float(df["close"].values[exit_idx])

            if direction == "short":
                move_adr = (signal_close - exit_close) / adr_at_signal
            else:
                move_adr = (exit_close - signal_close) / adr_at_signal

            sig["exit_triggered"] = True
            sig["exit_bar"] = exit_bar
            sig["exit_close"] = exit_close
            sig["move_adr"] = round(move_adr, 2)
            sig["adr_at_signal"] = round(adr_at_signal, 4)

            if move_adr >= 1.0:  # ADR threshold for meaningful win
                winners.append(sig)
            else:
                losers.append(sig)

        except Exception:
            losers.append(sig)

    print(f"  Winners: {len(winners)}, Losers: {len(losers)}")
    return winners, losers


# ══════════════════════════════════════════════════════════════
# PILE BUILDER — construct win/sacrifice/lose piles
# ══════════════════════════════════════════════════════════════

def build_piles(winners, losers, sacrificial, examples, cache):
    """Build the three piles for the proximity grinder.

    Win pile: deduped winners (untouchable, 100% must pass)
    Sacrifice pile: leftward duplicates from ALL clusters (winner and loser)
    Lose pile: deduped losers (target to trim)
    """
    # Match examples to winner signals — for logging only
    # (examples are already in the win pile via the winner classification)
    example_tickers = {}
    for ex in examples:
        ticker = ex["ticker"]
        entry = ex.get("entryDate") or ex.get("entry_date")
        if ticker and entry:
            if ticker not in example_tickers:
                example_tickers[ticker] = []
            example_tickers[ticker].append(entry)

    n_example_matched = 0
    for w in winners:
        ticker = w["ticker"]
        if ticker in example_tickers:
            sig_date = pd.to_datetime(w["date"])
            for entry_date in example_tickers[ticker]:
                entry_dt = pd.to_datetime(entry_date)
                diff = abs((sig_date - entry_dt).days)
                if diff <= 7:
                    w["is_example_match"] = True
                    n_example_matched += 1
                    break

    print(f"\n  ── Pile Summary ──")
    print(f"  Win pile:       {len(winners):,} deduped winners (untouchable)")
    print(f"    (of which {n_example_matched} match examples within ±7 days)")
    print(f"  Sacrifice pile: {len(sacrificial):,} leftward duplicates")
    print(f"  Lose pile:      {len(losers):,} deduped losers (target)")
    print(f"  Total trimmable: {len(sacrificial) + len(losers):,} "
          f"(sacrifice + lose)")

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
    can be used as conditions.
    """
    n_win = win_matrix.shape[0]
    ranges = {}

    for j, expr in enumerate(expressions):
        vals = win_matrix[:, j]
        valid = vals[~np.isnan(vals)]
        if len(valid) < n_win:
            # At least one win signal has NaN — can't use this expression
            continue
        lo, hi = float(np.min(valid)), float(np.max(valid))
        margin = (hi - lo) * 0.05
        ranges[expr["name"]] = (lo - margin, hi + margin)

    print(f"  Win ranges computed: {len(ranges)} expressions with full coverage "
          f"(out of {len(expressions)})")
    return ranges


def run_beam_search(trim_matrix, trim_dates, win_ranges, expressions,
                    beam_width=5000, depth=50, target_remaining=0):
    """Beam search to minimize remaining trimmable signals.

    trim_matrix: (n_trim, n_expr) — sacrifice + lose pile values
    win_ranges: {expr_name: (low, high)} — conditions must stay within these
    target_remaining: stop when remaining <= this
    """
    n_rows, n_expr = trim_matrix.shape
    expr_names = [e["name"] for e in expressions]

    print(f"\n  ── Beam Search ──")
    print(f"  Trimmable rows: {n_rows:,}")
    print(f"  Candidate expressions: {len(win_ranges):,}")
    print(f"  Beam width: {beam_width:,}, Depth: {depth}")

    # Precompute: for each candidate expression, which trim rows pass?
    # "pass" means the value is within the win pile's range
    cand_indices = []
    cand_passes = []

    for j, name in enumerate(expr_names):
        if name not in win_ranges:
            continue
        lo, hi = win_ranges[name]
        vals = trim_matrix[:, j]
        passes = ((vals >= lo) & (vals <= hi)) | np.isnan(vals)
        n_pass = int(np.sum(passes))
        # Only useful if it filters out at least 1% of rows
        if n_pass < n_rows * 0.99:
            cand_indices.append(j)
            cand_passes.append(passes)

    n_useful = len(cand_indices)
    print(f"  Useful candidates (filter ≥1%): {n_useful}")

    if n_useful == 0:
        print("  No useful candidates found. Proximity grind cannot trim further.")
        return [], n_rows

    # Convert to numpy for fast masking
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
    print(f"  Level 1: best={best.remaining:,} remaining "
          f"({trimmed:,} trimmed, {trimmed/max(n_rows,1)*100:.1f}%)")

    if best.remaining <= target_remaining:
        return _extract_conditions(best, cand_indices, expressions, win_ranges), best.remaining

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
            print(f"  Level {lv}: ceiling — no further improvement possible")
            break

        next_level.sort(key=lambda n: n.remaining)
        current_level = next_level[:beam_width]

        if current_level[0].remaining < best.remaining:
            best = current_level[0]

        trimmed = n_rows - best.remaining
        print(f"  Level {lv}: best={best.remaining:,} remaining "
              f"({trimmed:,} trimmed, {trimmed/max(n_rows,1)*100:.1f}%)"
              f"  [{len(best.conditions)} conditions]")

        if best.remaining <= target_remaining:
            print(f"  Target reached.")
            break

        # Early stop if no improvement for 3 levels
        if lv >= 4 and best.remaining == current_level[0].remaining:
            # Check last 3 levels
            pass  # let it continue — beam might find new combos

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
        print("\n  ✗ VALIDATION FAILED — win pile signals do not all pass:")
        for f in failures:
            print(f)
        return False

    print(f"\n  ✓ Validation passed: all {n_win} win pile signals pass "
          f"all {len(proximity_conditions)} proximity conditions")
    return True


# ══════════════════════════════════════════════════════════════
# SAVE + UPLOAD
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

    # Timestamped file
    fname = f"proximity_{setup_type}_{n_conds}cond_{ts}.json"
    path = os.path.join(out_dir, fname)
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {path}")

    # Latest pointer
    latest = os.path.join(out_dir, f"proximity_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Latest: {latest}")

    return output


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run_proximity_grind(setup_type, beam_width=5000, depth=50, dry_run=False,
                        workers=None):
    if workers is None:
        workers = max(1, cpu_count() - 1)

    direction = SETUP_CONFIGS.get(setup_type, {}).get("direction", "short")

    print(f"\n{'='*70}")
    print(f"  PROXIMITY GRINDER — {setup_type.upper()}")
    print(f"  Direction: {direction}")
    print(f"{'='*70}\n")

    t0 = time.time()

    # ── 1. Load refined conditions ──
    print("Phase 1: Loading data...")
    conditions, exit_cond_raw, min_adr, refined_data = load_refined_result(setup_type)

    exit_cond = load_exit_condition(setup_type, refined_data)
    if exit_cond.get("type") == "multi":
        raise NotImplementedError("Multi-stage exit not yet supported in proximity grinder")

    examples = load_examples(setup_type)
    cache = load_5yr_cache()
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found or invalid. "
                           "Run: python local_runner/expr_cache_builder.py --build")

    # ── 2. Re-scan with refined conditions ──
    print("\nPhase 2: Scanning universe with refined conditions...")
    raw_signals = scan_signals(cache, conditions, expr_cache, workers)

    # ── 3. Dedup with sacrificial tracking ──
    print("\nPhase 3: Deduplicating...")
    deduped, sacrificial = deduplicate_with_sacrificial(raw_signals)

    # ── 4. Apply exit filter ──
    print("\nPhase 4: Applying exit filter...")
    winners, losers = apply_exit_filter(deduped, cache, exit_cond, direction, expr_cache)

    # ── 5. Build piles ──
    print("\nPhase 5: Building piles...")
    win_pile, sacrifice_pile, lose_pile = build_piles(
        winners, losers, sacrificial, examples, cache
    )

    if dry_run:
        print(f"\n  DRY RUN — stopping here.")
        print(f"\n  Time: {time.time()-t0:.0f}s")
        return

    if len(win_pile) == 0:
        print("\n  ERROR: Win pile is empty. Cannot run proximity grind.")
        return

    if len(lose_pile) == 0:
        print("\n  Nothing to trim — lose pile is empty.")
        return

    # ── 6. Extract expression values ──
    print("\nPhase 6: Extracting expression values...")
    expressions = generate_all()
    print(f"  Expression library: {len(expressions):,}")

    win_matrix = extract_signal_values(win_pile, expressions, expr_cache)
    trim_signals = sacrifice_pile + lose_pile
    trim_matrix = extract_signal_values(trim_signals, expressions, expr_cache)

    # Track which trim rows are losers vs sacrificial
    n_sacrifice = len(sacrifice_pile)
    n_lose = len(lose_pile)

    # ── 7. Compute win pile ranges ──
    print("\nPhase 7: Computing win pile ranges...")
    win_ranges = compute_win_ranges(win_matrix, expressions)

    # ── 8. Beam search ──
    print("\nPhase 8: Beam search for proximity conditions...")
    trim_dates = [s["date"] for s in trim_signals]
    proximity_conditions, remaining = run_beam_search(
        trim_matrix, trim_dates, win_ranges, expressions,
        beam_width=beam_width, depth=depth,
    )

    if not proximity_conditions:
        print("\n  No proximity conditions found. Signal set cannot be trimmed further.")
        return

    # ── 9. Validate ──
    print("\nPhase 9: Validating win pile...")
    valid = validate_win_pile(win_matrix, proximity_conditions, expressions)
    if not valid:
        print("\n  CRITICAL: Validation failed. Aborting.")
        return

    # ── 10. Compute before/after metrics ──
    total_trim = len(trim_signals)
    trimmed = total_trim - remaining

    # How many of the trimmed were from lose pile vs sacrifice?
    # Re-apply conditions to lose pile only
    lose_matrix = trim_matrix[n_sacrifice:, :]
    expr_name_to_idx = {e["name"]: i for i, e in enumerate(expressions)}
    lose_mask = np.ones(n_lose, dtype=bool)
    for cond in proximity_conditions:
        j = expr_name_to_idx[cond["name"]]
        vals = lose_matrix[:, j]
        in_range = ((vals >= cond["low"]) & (vals <= cond["high"])) | np.isnan(vals)
        lose_mask &= in_range
    losers_remaining = int(np.sum(lose_mask))
    losers_trimmed = n_lose - losers_remaining

    # Same for sacrifice pile
    sac_matrix = trim_matrix[:n_sacrifice, :]
    sac_mask = np.ones(n_sacrifice, dtype=bool)
    for cond in proximity_conditions:
        j = expr_name_to_idx[cond["name"]]
        vals = sac_matrix[:, j]
        in_range = ((vals >= cond["low"]) & (vals <= cond["high"])) | np.isnan(vals)
        sac_mask &= in_range
    sac_remaining = int(np.sum(sac_mask))
    sac_trimmed = n_sacrifice - sac_remaining

    # New totals
    new_total = len(win_pile) + losers_remaining
    new_win_rate = len(win_pile) / max(new_total, 1) * 100
    old_total = len(win_pile) + n_lose
    old_win_rate = len(win_pile) / max(old_total, 1) * 100

    metrics = {
        "win_pile": len(win_pile),
        "sacrifice_pile_before": n_sacrifice,
        "sacrifice_trimmed": sac_trimmed,
        "lose_pile_before": n_lose,
        "losers_trimmed": losers_trimmed,
        "losers_remaining": losers_remaining,
        "old_total_deduped": old_total,
        "new_total_deduped": new_total,
        "old_win_rate_pct": round(old_win_rate, 1),
        "new_win_rate_pct": round(new_win_rate, 1),
        "n_proximity_conditions": len(proximity_conditions),
    }

    print(f"\n{'='*70}")
    print(f"  PROXIMITY GRIND RESULTS — {setup_type.upper()}")
    print(f"{'='*70}")
    print(f"  Proximity conditions found: {len(proximity_conditions)}")
    for c in proximity_conditions:
        print(f"    {c['name']:40s}  [{c['low']:.4f}, {c['high']:.4f}]  ({c['category']})")
    print(f"\n  Win pile (untouched):    {len(win_pile):,}")
    print(f"  Sacrifice pile:         {n_sacrifice:,} → {sac_remaining:,} "
          f"(-{sac_trimmed:,})")
    print(f"  Lose pile:              {n_lose:,} → {losers_remaining:,} "
          f"(-{losers_trimmed:,})")
    print(f"\n  Deduped signals:        {old_total:,} → {new_total:,}")
    print(f"  Win rate:               {old_win_rate:.1f}% → {new_win_rate:.1f}%")
    print(f"\n  Time: {time.time()-t0:.0f}s")

    # ── 11. Save ──
    result = save_results(setup_type, proximity_conditions, metrics, conditions)

    return result


def main():
    parser = argparse.ArgumentParser(description="Proximity Grinder")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--beam", type=int, default=5000,
                        help="Beam width (default: 5000)")
    parser.add_argument("--depth", type=int, default=50,
                        help="Search depth (default: 50)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers (default: cpu_count - 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build piles and show stats, don't grind")
    args = parser.parse_args()

    run_proximity_grind(
        setup_type=args.setup,
        beam_width=args.beam,
        depth=args.depth,
        dry_run=args.dry_run,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
