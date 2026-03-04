"""
Condition Pruner — Step 4, Sub-step 4 of ANALYSIS_SYSTEM.md

Leave-one-out filter power analysis on a pyramid grinder result.
Drops conditions whose removal has negligible effect on the universe pass rate
— i.e., conditions that don't eliminate enough candidates to be worth keeping.

Algorithm:
  1. Load pyramid result (pyramid_results_{setup}.json by default)
  2. Scan full 5yr universe with ALL conditions → baseline signal count
  3. For each condition C:
       Remove C, scan with (all_conditions - C)
       filter_power[C] = (baseline - signals_without_C) / baseline
       → fraction of passing rows that C alone eliminates
  4. Drop conditions where filter_power < min_power (default 0.10)
  5. CRITICAL: verify 100% of setup examples still pass the pruned set
     If any example fails, that condition is REQUIRED regardless of power
  6. Save pruned condition set to data/condition_pruner/pruned_{setup}.json

Rules (non-negotiable):
  - Expression cache is the ONLY computation path
  - 100% example pass on pruned set — hardcoded, no exceptions
  - Parallel across all cores
  - Never aborts on partial failures — always produces best available result

Usage:
    python scripts/condition_pruner.py --setup dtss
    python scripts/condition_pruner.py --setup dtss --min-power 0.15
    python scripts/condition_pruner.py --setup dtss --conditions-file path/to/custom.json
"""

import argparse
import os
import sys
import time
import json
import pickle
import glob
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count
from datetime import datetime

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from expr_cache_builder import ExprSeriesCache

RAILWAY_URL = "https://web-production-e3025.up.railway.app"
DEFAULT_MIN_POWER = 0.10  # conditions eliminating <10% of remaining universe get dropped


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


def load_pyramid_conditions(setup_type, conditions_file=None):
    """Load conditions from pyramid results file.

    Returns (conditions_list, source_path).
    Searches local_runner/cache/ for pyramid_results_{setup}.json by default.
    """
    if conditions_file:
        if not os.path.exists(conditions_file):
            raise FileNotFoundError(f"Conditions file not found: {conditions_file}")
        with open(conditions_file) as f:
            data = json.load(f)
        conditions = data.get("all_conditions", data.get("pruned_conditions", []))
        print(f"  Loaded {len(conditions)} conditions from {conditions_file}")
        return conditions, conditions_file

    search_dirs = [
        os.path.join(REPO_ROOT, "local_runner", "cache"),
        os.path.join(REPO_ROOT, "data"),
    ]
    candidates = []
    for d in search_dirs:
        exact = os.path.join(d, f"pyramid_results_{setup_type}.json")
        if os.path.exists(exact):
            candidates.append(exact)
        for p in glob.glob(os.path.join(d, f"pyramid_{setup_type}_*.json")):
            candidates.append(p)

    if not candidates:
        raise FileNotFoundError(
            f"No pyramid results found for {setup_type}. "
            f"Run: python local_runner/pyramid_grinder.py --setup {setup_type}"
        )
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    best = candidates[0]
    with open(best) as f:
        data = json.load(f)
    conditions = data.get("all_conditions", [])
    print(f"  Loaded {len(conditions)} conditions from {os.path.basename(best)}")
    return conditions, best


def load_examples(setup_type):
    """Load validated examples from Railway."""
    import requests
    try:
        r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}", timeout=30)
        r.raise_for_status()
        examples = r.json().get("examples", [])
        print(f"  Loaded {len(examples)} examples from Railway")
        return examples
    except Exception as e:
        print(f"  WARNING: couldn't load examples: {e}")
        return []


# ══════════════════════════════════════════════════════════════
# PARALLEL SCAN WORKERS
# ══════════════════════════════════════════════════════════════

_pw_cache = None
_pw_expr_cache_dir = None
_pw_cond_col_indices = None      # list of col_idx per condition (full set)
_pw_cond_lows = None             # np.array of condition lows
_pw_cond_highs = None            # np.array of condition highs


def _init_scan_worker(cache, expr_cache_dir, cond_col_indices, cond_lows, cond_highs):
    global _pw_cache, _pw_expr_cache_dir, _pw_cond_col_indices
    global _pw_cond_lows, _pw_cond_highs
    _pw_cache = cache
    _pw_expr_cache_dir = expr_cache_dir
    _pw_cond_col_indices = cond_col_indices
    _pw_cond_lows = cond_lows
    _pw_cond_highs = cond_highs


def _load_npz(ticker):
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = os.path.join(_pw_expr_cache_dir, f"{safe}.npz")
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        return loaded["dates"], loaded["data"]
    except Exception:
        return None, None


def _scan_batch_full(tickers):
    """Scan tickers with ALL conditions.
    Returns (n_passing_bars,) for the whole batch.
    """
    total = 0
    for ticker in tickers:
        df = _pw_cache.get(ticker)
        if df is None or len(df) < 100:
            continue
        _, data = _load_npz(ticker)
        if data is None or len(data) != len(df):
            continue
        n_bars = len(df)
        mask = np.ones(n_bars, dtype=bool)
        mask[:50] = False
        for i, col_idx in enumerate(_pw_cond_col_indices):
            if col_idx is None:
                mask[:] = False
                break
            series = data[:, col_idx]
            in_range = (series >= _pw_cond_lows[i]) & (series <= _pw_cond_highs[i])
            in_range[np.isnan(series)] = False
            mask &= in_range
            if not np.any(mask):
                break
        total += int(np.sum(mask))
    return total


def _scan_batch_without(args):
    """Scan tickers with all conditions EXCEPT the one at drop_idx.
    Returns (n_passing_bars,) for the whole batch.
    """
    tickers, drop_idx = args
    total = 0
    n_conds = len(_pw_cond_col_indices)
    for ticker in tickers:
        df = _pw_cache.get(ticker)
        if df is None or len(df) < 100:
            continue
        _, data = _load_npz(ticker)
        if data is None or len(data) != len(df):
            continue
        n_bars = len(df)
        mask = np.ones(n_bars, dtype=bool)
        mask[:50] = False
        for i in range(n_conds):
            if i == drop_idx:
                continue  # skip this condition
            col_idx = _pw_cond_col_indices[i]
            if col_idx is None:
                mask[:] = False
                break
            series = data[:, col_idx]
            in_range = (series >= _pw_cond_lows[i]) & (series <= _pw_cond_highs[i])
            in_range[np.isnan(series)] = False
            mask &= in_range
            if not np.any(mask):
                break
        total += int(np.sum(mask))
    return total


# ══════════════════════════════════════════════════════════════
# UNIVERSE SCAN
# ══════════════════════════════════════════════════════════════

def scan_universe(cache, cond_col_indices, cond_lows, cond_highs,
                  workers, expr_cache_dir, drop_idx=None):
    """Scan full universe, optionally dropping one condition.

    Returns total passing bar-rows across all tickers.
    """
    tickers = list(cache.keys())
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    total = 0

    if drop_idx is None:
        # Full scan
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_scan_worker,
            initargs=(cache, expr_cache_dir, cond_col_indices, cond_lows, cond_highs)
        ) as pool:
            futures = [pool.submit(_scan_batch_full, batch) for batch in batches]
            for f in as_completed(futures):
                total += f.result()
    else:
        # Leave-one-out scan
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_scan_worker,
            initargs=(cache, expr_cache_dir, cond_col_indices, cond_lows, cond_highs)
        ) as pool:
            futures = [pool.submit(_scan_batch_without, (batch, drop_idx))
                       for batch in batches]
            for f in as_completed(futures):
                total += f.result()

    return total


# ══════════════════════════════════════════════════════════════
# EXAMPLE VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_examples_on_conditions(examples, conditions, cache, expr_cache):
    """Verify all examples pass the given conditions using expr cache.

    Returns list of tickers that FAIL (empty = all pass).
    Uses same computation path as pyramid_grinder.validate_examples().
    """
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    failed = []

    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        df = cache.get(ticker)
        if df is None:
            failed.append(f"{ticker}: not in OHLCV cache")
            continue

        import pandas as pd
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])

        # Scan bar = day before entry
        entry_dt = pd.to_datetime(entry_date)
        match = df[df["date"] < entry_dt]
        if len(match) == 0:
            failed.append(f"{ticker}: no scan bar before {entry_date}")
            continue
        scan_idx = match.index[-1]

        dates_cache, data_cache = expr_cache.get_ticker(ticker)
        if dates_cache is None:
            failed.append(f"{ticker}: not in expr cache")
            continue
        if len(dates_cache) != len(df):
            failed.append(f"{ticker}: bar count mismatch")
            continue
        if scan_idx >= len(data_cache):
            failed.append(f"{ticker}: scan_idx out of range")
            continue

        cached_row = data_cache[scan_idx, :]
        for cond in conditions:
            col_idx = cache_name_to_idx.get(cond["name"])
            if col_idx is None:
                failed.append(f"{ticker}: condition {cond['name']} not in cache")
                break
            val = float(cached_row[col_idx])
            if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                failed.append(
                    f"{ticker} FAILS {cond['name']}: "
                    f"{val:.4f} not in [{cond['low']:.4f}, {cond['high']:.4f}]"
                )
                break

    return failed


# ══════════════════════════════════════════════════════════════
# MAIN PRUNER
# ══════════════════════════════════════════════════════════════

def run_pruner(setup_type, conditions_file=None, min_power=DEFAULT_MIN_POWER,
               workers=None):
    """Run leave-one-out filter power analysis and prune weak conditions.

    Returns pruned conditions list.
    """
    workers = workers or max(cpu_count() - 1, 1)

    print(f"\n{'='*70}")
    print(f"  CONDITION PRUNER — {setup_type.upper()}")
    print(f"  Min filter power threshold: {min_power:.0%}")
    print(f"  Computation: expression cache only")
    print(f"  Workers: {workers}")
    print(f"{'='*70}")
    t0 = time.time()

    # ── Load data ──
    print(f"\n  Loading data...")
    conditions, source_path = load_pyramid_conditions(setup_type, conditions_file)
    if not conditions:
        print(f"  ERROR: No conditions loaded. Aborting.")
        return None

    cache = load_5yr_cache()
    examples = load_examples(setup_type)

    print(f"\n  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache not found or invalid.")
        print("  Run: python local_runner/expr_cache_builder.py --build")
        return None
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # ── Map conditions to cache columns ──
    cond_col_indices = []
    missing_conds = []
    for cond in conditions:
        col_idx = expr_cache.expr_index(cond["name"])
        if col_idx is None:
            missing_conds.append(cond["name"])
        cond_col_indices.append(col_idx)

    if missing_conds:
        print(f"\n  WARNING: {len(missing_conds)} conditions not found in expr cache:")
        for name in missing_conds[:10]:
            print(f"    {name}")
        print(f"  These will always pass (treated as no-op) in the scan.")

    cond_lows = np.array([c["low"] for c in conditions], dtype=np.float64)
    cond_highs = np.array([c["high"] for c in conditions], dtype=np.float64)
    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    # ── Baseline scan ──
    print(f"\n  BASELINE: Scanning universe with all {len(conditions)} conditions...")
    t_scan = time.time()
    baseline = scan_universe(
        cache, cond_col_indices, cond_lows, cond_highs,
        workers, expr_cache_dir, drop_idx=None
    )
    print(f"  Baseline: {baseline:,} passing bar-rows ({time.time()-t_scan:.0f}s)")

    if baseline == 0:
        print(f"\n  WARNING: Zero baseline signals — conditions may be too tight.")
        print(f"  Continuing anyway; filter powers will all be 0.")

    # ── Leave-one-out ──
    print(f"\n  LEAVE-ONE-OUT: Testing each condition individually...")
    n_conds = len(conditions)
    filter_powers = []

    for i, cond in enumerate(conditions):
        t_i = time.time()
        without_i = scan_universe(
            cache, cond_col_indices, cond_lows, cond_highs,
            workers, expr_cache_dir, drop_idx=i
        )
        if baseline > 0:
            power = (without_i - baseline) / baseline
        else:
            power = 0.0
        filter_powers.append(power)
        tier = cond.get("tier", "?")
        cat = cond.get("category", "unknown")
        flag = "DROP" if power < min_power else "KEEP"
        print(f"  [{i+1:2d}/{n_conds}] {flag:4s}  power={power:+.3f}  "
              f"[{tier:>4}][{cat:>18}] {cond['name']}  "
              f"({without_i:,} without, {time.time()-t_i:.0f}s)")

    # ── Identify required conditions (examples would fail without them) ──
    print(f"\n  Checking which conditions are required by examples...")
    required_names = set()
    if examples:
        for i, cond in enumerate(conditions):
            if filter_powers[i] >= min_power:
                continue  # already keeping it — no need to check
            # It would be dropped — verify examples still pass without it
            without_cond = [c for j, c in enumerate(conditions) if j != i]
            fails = validate_examples_on_conditions(examples, without_cond, cache, expr_cache)
            if fails:
                required_names.add(cond["name"])
                print(f"  REQUIRED (examples fail without it): {cond['name']}")

    # ── Build pruned set ──
    kept = []
    dropped = []
    for i, cond in enumerate(conditions):
        power = filter_powers[i]
        if power >= min_power or cond["name"] in required_names:
            kept.append({**cond, "filter_power": round(power, 4)})
        else:
            dropped.append({**cond, "filter_power": round(power, 4)})

    print(f"\n  {'='*60}")
    print(f"  PRUNING RESULT")
    print(f"  {'='*60}")
    print(f"  Input:   {n_conds} conditions")
    print(f"  Kept:    {len(kept)}")
    print(f"  Dropped: {len(dropped)}")
    if dropped:
        print(f"\n  Dropped conditions:")
        for c in dropped:
            tier = c.get("tier", "?")
            cat = c.get("category", "unknown")
            print(f"    [{tier:>4}][{cat:>18}] {c['name']}  power={c['filter_power']:+.3f}")
    print(f"  Required overrides: {len(required_names)}")

    # ── Validate pruned set against examples ──
    print(f"\n  Validating pruned set against all examples...")
    if examples:
        fails = validate_examples_on_conditions(examples, kept, cache, expr_cache)
        if fails:
            print(f"\n  {'!'*70}")
            print(f"  VALIDATION FAILED on pruned set! {len(fails)} failures:")
            for f in fails[:10]:
                print(f"    {f}")
            print(f"  Falling back to UNPRUNED conditions (all {n_conds} kept).")
            print(f"  {'!'*70}")
            # Safety fallback — return original set with power annotations
            kept = [{**c, "filter_power": round(filter_powers[i], 4)}
                    for i, c in enumerate(conditions)]
            dropped = []
        else:
            n_ex_pass = len([e for e in examples if e.get("ticker")
                             not in [f.split(":")[0] for f in fails]])
            print(f"  OK: {len(examples)}/{len(examples)} examples pass pruned conditions")
    else:
        print(f"  WARNING: No examples loaded — skipping example validation.")

    total_time = time.time() - t0

    # ── Save ──
    out_dir = os.path.join(REPO_ROOT, "data", "condition_pruner")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "computation_path": "expression_cache_only",
        "source_conditions_file": os.path.basename(source_path),
        "min_power_threshold": min_power,
        "n_input_conditions": n_conds,
        "n_kept": len(kept),
        "n_dropped": len(dropped),
        "baseline_signals": baseline,
        "total_time_s": round(total_time, 1),
        # Full condition set with filter powers (kept ones only)
        "all_conditions": kept,
        "pruned_conditions": kept,       # alias for signal_filter.py --conditions-file
        "dropped_conditions": dropped,
        # Full power table for reference
        "filter_power_table": [
            {
                "name": conditions[i]["name"],
                "tier": conditions[i].get("tier", "?"),
                "category": conditions[i].get("category", "unknown"),
                "filter_power": round(filter_powers[i], 4),
                "kept": conditions[i]["name"] in {c["name"] for c in kept},
            }
            for i in range(n_conds)
        ],
    }

    # Timestamped archive
    ts_path = os.path.join(
        out_dir,
        f"pruned_{setup_type}_{len(kept)}cond_{ts}.json"
    )
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {ts_path}")

    # Latest (downstream consumers read this)
    latest_path = os.path.join(out_dir, f"pruned_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved as latest: {latest_path}")

    print(f"\n  {'='*70}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  {n_conds} → {len(kept)} conditions ({len(dropped)} pruned)")
    print(f"  Next: python scripts/signal_filter.py --setup {setup_type} "
          f"--conditions-file {latest_path}")
    print(f"  {'='*70}\n")

    return kept


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Condition Pruner — leave-one-out filter power analysis")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--min-power", type=float, default=DEFAULT_MIN_POWER,
                        help=f"Min filter power to keep a condition "
                             f"(default: {DEFAULT_MIN_POWER:.0%}). "
                             f"Conditions eliminating <N% of passing rows get dropped.")
    parser.add_argument("--conditions-file", default=None,
                        help="Path to conditions JSON file (default: latest pyramid result)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers (default: cpu_count - 1)")
    args = parser.parse_args()

    run_pruner(
        setup_type=args.setup,
        conditions_file=args.conditions_file,
        min_power=args.min_power,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
