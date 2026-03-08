"""
Setup Refiner — Step 4 Sub-steps 3+4 combined.

Takes the output of the blackout re-grind (pyramid_grinder.py --blackout),
prunes weak conditions via leave-one-out filter power analysis, then
immediately runs the signal filter and uploads to Railway for vetting.

ISOLATION GUARANTEE:
  All outputs use _refined suffix and write to data/setup_refiner/.
  Nothing in data/signal_filter/ or data/condition_pruner/ is touched.
  Base pipeline results (filtered_{setup}.json etc.) are NEVER overwritten.

Sequence:
  1. Load blackout pyramid result (auto-discovers latest _blackout_ file,
     or use --conditions-file to specify explicitly)
  2. Leave-one-out filter power analysis — drop conditions < min_power
  3. Required-condition check — never drop a condition examples depend on
  4. Validate pruned set: 100% examples must pass (hard requirement)
  5. Scan full universe with pruned conditions
  6. Dedup → apply exit → measure ADR → filter → rank
  7. Save to data/setup_refiner/ and upload to Railway

Usage:
    python scripts/setup_refiner.py --setup dtss
    python scripts/setup_refiner.py --setup dtss --min-power 0.15
    python scripts/setup_refiner.py --setup dtss --conditions-file path/to/result.json
    python scripts/setup_refiner.py --setup dtss --skip-prune  # filter only, no pruning
"""

import argparse
import os
import sys
import time
import json
import pickle
import glob
import numpy as np
import pandas as pd
import requests
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count
from datetime import datetime

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
MAX_FORWARD = 120
DEFAULT_MIN_POWER = 0.10

SETUP_CONFIGS = {
    "dtss": {"direction": "short"},
}


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    print(f"  Loading 5yr cache...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_conditions(setup_type, conditions_file=None):
    """Load conditions from blackout pyramid result or explicit file.

    Auto-discovery order:
      1. Files matching *_blackout_* in local_runner/cache/ and data/
      2. Any pyramid result file (latest by mtime)

    Returns (conditions_list, source_path).
    """
    if conditions_file:
        if not os.path.exists(conditions_file):
            raise FileNotFoundError(f"Conditions file not found: {conditions_file}")
        with open(conditions_file) as f:
            data = json.load(f)
        conds = data.get("all_conditions", data.get("pruned_conditions", []))
        print(f"  Loaded {len(conds)} conditions from {os.path.basename(conditions_file)}")
        return conds, conditions_file

    search_dirs = [
        os.path.join(REPO_ROOT, "local_runner", "cache"),
        os.path.join(REPO_ROOT, "data"),
    ]

    # Resolution order (strict — never falls through to base pipeline results):
    # 1. pyramid_results_{setup}_blackout.json  — latest pointer from --blackout run
    # 2. pyramid_{setup}_*_blackout_*.json      — any timestamped blackout archive
    # Refuses to load base pipeline results to prevent mixing stages.
    blackout_latest = os.path.join(
        os.path.join(REPO_ROOT, "local_runner", "cache"),
        f"pyramid_results_{setup_type}_blackout.json"
    )
    blackout_archives = []
    for d in search_dirs:
        for p in glob.glob(os.path.join(d, f"pyramid_{setup_type}_*_blackout_*.json")):
            blackout_archives.append(p)

    if os.path.exists(blackout_latest):
        best = blackout_latest
        print(f"  Found blackout latest: {os.path.basename(best)}")
    elif blackout_archives:
        blackout_archives.sort(key=os.path.getmtime, reverse=True)
        best = blackout_archives[0]
        print(f"  Found blackout archive: {os.path.basename(best)}")
    else:
        raise FileNotFoundError(
            f"No blackout pyramid result found for {setup_type}.\n"
            f"  Run: python local_runner/pyramid_grinder.py --setup {setup_type} --blackout\n"
            f"  Use --conditions-file to point at a specific file if needed."
        )

    with open(best) as f:
        data = json.load(f)
    conds = data.get("all_conditions", [])
    print(f"  Loaded {len(conds)} conditions  "
          f"(grinder signals: {data.get('summary', {}).get('final_total', '?')})")
    return conds, best


def load_exit_condition(setup_type):
    """Load best exit condition based on the choice stored in Railway.

    Priority:
      1. Railway /api/exit-grind/{setup}/choice  — routes to single or multi
         - single: profit_grind/profit_{setup}.json  top condition
         - multi:  multistage_exit/ms_exit_{setup}.json  result stages
      2. Local profit_grind fallback (no choice set yet)
      3. signal_exit_grinder output — legacy fallback
    """
    # 1. Check Railway for explicit choice
    try:
        r = requests.get(f"{RAILWAY_URL}/api/exit-grind/{setup_type}/choice", timeout=10)
        if r.status_code == 200:
            choice = r.json().get("choice")
            print(f"  Exit choice from Railway: {choice}")

            if choice == "single":
                profit_path = os.path.join(REPO_ROOT, "data", "profit_grind", f"profit_{setup_type}.json")
                if os.path.exists(profit_path):
                    with open(profit_path) as f:
                        pdata = json.load(f)
                    # Support both old format (top_conditions) and new format (results)
                    top = pdata.get("top_conditions") or pdata.get("results", [])
                    if top:
                        best = top[0]
                        # New format uses expr_name, old uses expression
                        expr = best.get("expression") or best.get("expr_name")
                        ec = {
                            "expression": expr,
                            "threshold": best.get("threshold"),
                            "direction": best.get("direction", "<="),
                        }
                        print(f"  Exit (single-stage, profit grind): {ec['expression']} {ec['direction']} {ec['threshold']}")
                        return ec
                raise FileNotFoundError(
                    f"Choice is 'single' but no profit grind output found.\n"
                    f"  Run: python scripts/profit_grinder.py --setup {setup_type}"
                )

            elif choice == "multi":
                ms_path = os.path.join(REPO_ROOT, "data", "multistage_exit", f"ms_exit_{setup_type}.json")
                if os.path.exists(ms_path):
                    with open(ms_path) as f:
                        mdata = json.load(f)
                    result = mdata.get("result")
                    if result and result.get("stages"):
                        # Return multi-stage descriptor — caller must handle list of stages
                        ec = {
                            "type": "multi",
                            "stages": result["stages"],
                            "n_stages": result["n_stages"],
                            "floor_capture_eff": result.get("floor_capture_eff"),
                            "median_capture_eff": result.get("median_capture_eff"),
                        }
                        print(f"  Exit (multi-stage, {result['n_stages']} stages): "
                              + " → ".join(f"{s['expr_name']} {s['direction']} {s['threshold']:.4f} (trim {s['trim_pct']:.0%})"
                                           for s in result["stages"]))
                        return ec
                raise FileNotFoundError(
                    f"Choice is 'multi' but no multistage exit output found.\n"
                    f"  Run: python scripts/multistage_exit_grinder.py --setup {setup_type}"
                )
    except requests.RequestException as e:
        print(f"  WARNING: Could not fetch exit choice from Railway ({e}). Falling back to local files.")

    # 2. Local fallback: profit grind (no choice set)
    profit_path = os.path.join(REPO_ROOT, "data", "profit_grind", f"profit_{setup_type}.json")
    if os.path.exists(profit_path):
        with open(profit_path) as f:
            pdata = json.load(f)
        top = pdata.get("top_conditions") or pdata.get("results", [])
        if top:
            best = top[0]
            expr = best.get("expression") or best.get("expr_name")
            ec = {
                "expression": expr,
                "threshold": best.get("threshold"),
                "direction": best.get("direction", "<="),
            }
            print(f"  Exit (profit grind, no choice set): {ec['expression']} {ec['direction']} {ec['threshold']}")
            return ec

    # 3. Legacy fallback: signal_exit_grinder output
    search_dirs = [
        os.path.join(REPO_ROOT, "data", "signal_exit_grinder"),
        os.path.join(REPO_ROOT, "data"),
        os.path.join(REPO_ROOT, "local_runner", "cache"),
    ]
    for d in search_dirs:
        exact = os.path.join(d, f"exit_{setup_type}.json")
        if os.path.exists(exact):
            with open(exact) as f:
                edata = json.load(f)
            top = edata.get("top_conditions", [edata]) if "top_conditions" in edata else [edata]
            best = top[0]
            ec = {
                "expression": best.get("expression", best.get("name")),
                "threshold": best.get("threshold"),
                "direction": best.get("direction", "<="),
            }
            print(f"  Exit (signal exit grind): {ec['expression']} {ec['direction']} {ec['threshold']}")
            return ec
        for p in sorted(glob.glob(os.path.join(d, f"exit_{setup_type}_*.json")),
                        key=os.path.getmtime, reverse=True):
            with open(p) as f:
                edata = json.load(f)
            top = edata.get("top_conditions", [edata]) if "top_conditions" in edata else [edata]
            best = top[0]
            ec = {
                "expression": best.get("expression", best.get("name")),
                "threshold": best.get("threshold"),
                "direction": best.get("direction", "<="),
            }
            print(f"  Exit (signal exit grind): {ec['expression']} {ec['direction']} {ec['threshold']}  "
                  f"({os.path.basename(p)})")
            return ec
    raise FileNotFoundError(
        f"No exit condition found for {setup_type}.\n"
        f"  Run: python scripts/profit_grinder.py --setup {setup_type}"
    )
def load_examples(setup_type):
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
# PRUNER — leave-one-out filter power
# ══════════════════════════════════════════════════════════════

def _validate_examples(examples, conditions, cache, expr_cache):
    """Verify all examples pass the given conditions using expr cache.

    Returns list of failure descriptions (empty = all pass).
    Skips examples whose tickers aren't in the OHLCV or expr cache
    (known exclusions like BRK-B, SMMT, VUZI — data availability,
    not condition failures).
    """
    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)
    failed = []
    skipped = []

    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        df = cache.get(ticker)
        if df is None:
            skipped.append(f"{ticker}: not in OHLCV cache")
            continue

        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])

        # Scan bar = day before entry
        entry_dt = pd.to_datetime(entry_date)
        match = df[df["date"] < entry_dt]
        if len(match) == 0:
            skipped.append(f"{ticker}: no scan bar before {entry_date}")
            continue
        scan_idx = match.index[-1]

        dates_cache, data_cache = expr_cache.get_ticker(ticker)
        if dates_cache is None:
            skipped.append(f"{ticker}: not in expr cache")
            continue
        if len(dates_cache) != len(df):
            skipped.append(f"{ticker}: bar count mismatch")
            continue
        if scan_idx >= len(data_cache):
            skipped.append(f"{ticker}: scan_idx out of range")
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

    if skipped:
        print(f"  Skipped {len(skipped)} examples (not in cache): "
              f"{[s.split(':')[0] for s in skipped]}")

    return failed


# ── Single-pass LOO workers ──
# Instead of 87 separate universe scans, we do ONE pass per ticker:
#   1. Load NPZ, evaluate all N conditions into a boolean matrix (n_bars × N)
#   2. Compute baseline mask = AND of all columns
#   3. For each condition i, the without-i count = sum(baseline | ~col_i)
#      (removing a condition can only ADD passing bars, never remove them)
# This gives us baseline + all N leave-one-out counts in a single data pass.

_loo_cache = None
_loo_expr_cache_dir = None
_loo_cond_col_indices = None
_loo_cond_lows = None
_loo_cond_highs = None


def _init_loo_worker(cache, expr_cache_dir, cond_col_indices, cond_lows, cond_highs):
    global _loo_cache, _loo_expr_cache_dir, _loo_cond_col_indices
    global _loo_cond_lows, _loo_cond_highs
    _loo_cache = cache
    _loo_expr_cache_dir = expr_cache_dir
    _loo_cond_col_indices = cond_col_indices
    _loo_cond_lows = cond_lows
    _loo_cond_highs = cond_highs


def _load_loo_cols(ticker):
    """Load only the needed expression columns for a ticker.

    Returns (n_bars, n_conds) array with columns in condition order,
    or None if ticker not cached or has missing conditions.
    """
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = os.path.join(_loo_expr_cache_dir, f"{safe}.npz")
    if not os.path.exists(path):
        return None
    try:
        loaded = np.load(path, allow_pickle=True)
        full_data = loaded["data"]
        # Slice to only the columns we need — (n_bars, n_conds) instead of (n_bars, 12175)
        return full_data[:, _loo_cond_col_indices]
    except Exception:
        return None


def _loo_single_pass_batch(tickers):
    """Single-pass LOO for a batch of tickers.

    Returns: (baseline_count, np.array of without_counts[n_conds])
    All N leave-one-out counts computed in one data pass per ticker.
    """
    n_conds = len(_loo_cond_col_indices)
    batch_baseline = 0
    batch_without = np.zeros(n_conds, dtype=np.int64)

    for ticker in tickers:
        df = _loo_cache.get(ticker)
        if df is None or len(df) < 100:
            continue
        data = _load_loo_cols(ticker)
        if data is None or len(data) != len(df):
            continue

        n_bars = len(df)

        # data is already (n_bars, n_conds) — columns in condition order
        # Build boolean matrix directly
        bool_matrix = np.ones((n_bars, n_conds), dtype=bool)
        for i in range(n_conds):
            series = data[:, i]
            in_range = (series >= _loo_cond_lows[i]) & (series <= _loo_cond_highs[i])
            in_range[np.isnan(series)] = False
            bool_matrix[:, i] = in_range

        # Skip first 50 bars
        bool_matrix[:50, :] = False

        # Baseline = AND of all columns
        baseline_mask = np.all(bool_matrix, axis=1)  # (n_bars,)
        ticker_baseline = int(np.sum(baseline_mask))
        batch_baseline += ticker_baseline

        if ticker_baseline == 0:
            # If no bars pass all conditions, removing any single condition
            # might still yield 0 or very few. Need to compute properly.
            # without_i = bars where all conditions EXCEPT i pass
            for i in range(n_conds):
                # All cols except i
                cols = list(range(n_conds))
                cols.pop(i)
                without_mask = np.all(bool_matrix[:, cols], axis=1)
                batch_without[i] += int(np.sum(without_mask))
        else:
            # Fast path: for each condition i, without_i mask = baseline OR (NOT cond_i)
            # Equivalent: bars that pass all other conditions (may or may not pass i)
            # = baseline_mask | (~bool_matrix[:, i] & all_others_pass)
            # Simpler: without_i = sum where all columns except i are True
            # Since baseline = all True, any bar in baseline is always in without_i.
            # Additional bars: those where only condition i fails.
            # without_i_mask = baseline_mask | (all_except_i & ~cond_i)
            # But easiest: for each i, mask_without_i = AND of all cols except i
            # We can compute this efficiently:
            #   all_pass_except_i = baseline_mask | ~bool_matrix[:, i]
            #   Wait, that's not right either.
            #
            # Correct: without_i = AND of all columns j where j != i
            # = baseline_mask is AND of all including i
            # If bar passes baseline, it passes without_i (superset).
            # If bar fails baseline, it fails at least one condition.
            #   If it fails ONLY condition i, it passes without_i.
            #   If it fails any other condition too, it fails without_i.
            #
            # Efficient: n_fails[bar] = count of False in bool_matrix[bar, :]
            # without_i passes if: the ONLY failure (if any) is condition i
            # = (n_fails == 0) OR (n_fails == 1 AND bool_matrix[:, i] == False)

            n_fails = np.sum(~bool_matrix, axis=1)  # (n_bars,)
            for i in range(n_conds):
                without_i_count = int(np.sum(
                    (n_fails == 0) | ((n_fails == 1) & (~bool_matrix[:, i]))
                ))
                batch_without[i] += without_i_count

    return batch_baseline, batch_without


def prune_conditions(conditions, cache, examples, expr_cache, workers, min_power):
    """Prune weak conditions using leave-one-out filter power computed from expr cache.

    Single-pass approach: one data load per ticker computes baseline + all N
    leave-one-out counts simultaneously via boolean matrix algebra.

    filter_power = (without_i - baseline) / baseline
    → fraction of extra bars that leak through when condition i is removed.
    Drop conditions below min_power. Never drop what examples need.
    """
    n_conds = len(conditions)
    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    # Build arrays for workers
    cond_col_indices = [expr_cache.expr_index(c["name"]) for c in conditions]
    cond_lows = np.array([c["low"] for c in conditions], dtype=np.float64)
    cond_highs = np.array([c["high"] for c in conditions], dtype=np.float64)

    # Check all expressions resolve — can't slice NPZ with None indices
    missing = [conditions[i]["name"] for i in range(n_conds) if cond_col_indices[i] is None]
    if missing:
        print(f"  ERROR: {len(missing)} conditions not in expr cache: {missing[:5]}")
        print(f"  Cannot run LOO scan — rebuild expr cache first.")
        kept = [{**c, "filter_power": None} for c in conditions]
        return kept, [], []

    # Convert to numpy array for column slicing in workers
    cond_col_indices = np.array(cond_col_indices, dtype=np.int64)

    # Single-pass LOO scan
    print(f"\n  Single-pass LOO scan ({n_conds} conditions, {len(cache):,} tickers)...")
    t0 = time.time()

    tickers = list(cache.keys())
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    total_baseline = 0
    total_without = np.zeros(n_conds, dtype=np.int64)

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_loo_worker,
        initargs=(cache, expr_cache_dir, cond_col_indices, cond_lows, cond_highs)
    ) as pool:
        futures = [pool.submit(_loo_single_pass_batch, batch) for batch in batches]
        done = 0
        for f in as_completed(futures):
            b, w = f.result()
            total_baseline += b
            total_without += w
            done += 1
            if done % max(len(batches) // 5, 1) == 0 or done == len(batches):
                pct = done / len(batches) * 100
                print(f"    {pct:.0f}%  baseline={total_baseline:,}  [{time.time()-t0:.0f}s]")

    scan_time = time.time() - t0
    print(f"  Baseline: {total_baseline:,} passing bar-rows")
    print(f"  LOO scan complete: {scan_time:.0f}s (single pass)")

    if total_baseline == 0:
        print(f"  WARNING: 0 bars pass all conditions — cannot compute filter power.")
        kept = [{**c, "filter_power": None} for c in conditions]
        return kept, [], []

    # Compute filter powers
    filter_powers = []
    for i in range(n_conds):
        fp = (int(total_without[i]) - total_baseline) / total_baseline
        filter_powers.append(fp)

    # Print results sorted by power (highest first for readability)
    print(f"\n  Filter power results:")
    for i, cond in enumerate(conditions):
        fp = filter_powers[i]
        flag = "KEEP" if fp >= min_power else "DROP"
        tier = cond.get("tier", "?")
        cat = cond.get("category", "?")[:16]
        print(f"  {flag}  power={fp:>8.1%}  without={int(total_without[i]):>8,}  "
              f"[{tier:>4}][{cat:>16}] {cond['name']}")

    # Classify keep/drop
    kept, dropped, power_table = [], [], []
    for i, cond in enumerate(conditions):
        fp = filter_powers[i]
        entry = {**cond, "filter_power": round(fp, 6)}
        if fp >= min_power:
            kept.append(entry)
        else:
            dropped.append(entry)
        power_table.append({
            "name": cond["name"],
            "tier": tier,
            "category": cond.get("category", "?"),
            "filter_power": round(fp, 6),
            "kept": fp >= min_power,
            "required_override": False,
        })

    # Required-condition check — never drop what examples need
    if examples and dropped:
        reinstated = []
        for dc in dropped:
            without_conds = [c for c in conditions if c["name"] != dc["name"]]
            fails = _validate_examples(examples, without_conds, cache, expr_cache)
            if fails:
                print(f"  REQUIRED (example fails without): {dc['name']}")
                kept.append(dc)
                reinstated.append(dc["name"])
                for pt in power_table:
                    if pt["name"] == dc["name"]:
                        pt["required_override"] = True
                        pt["kept"] = True
        dropped = [c for c in dropped if c["name"] not in reinstated]
        if reinstated:
            print(f"  Reinstated {len(reinstated)} required conditions (examples depend on them)")

    n_by_power = len([c for c in kept if c.get("filter_power") is not None and c["filter_power"] >= min_power])
    n_required = len(kept) - n_by_power
    print(f"\n  ┌─────────────────────────────────────────────┐")
    print(f"  │  PRUNING SUMMARY                            │")
    print(f"  │  {n_conds:>3} input → {len(kept):>3} kept, {len(dropped):>3} dropped       │")
    print(f"  │  {n_by_power:>3} kept by power, {n_required:>3} required by examples │")
    print(f"  └─────────────────────────────────────────────┘")
    if dropped:
        print(f"  Dropped: {[c['name'] for c in dropped]}")

    # Final validation — pruned set must pass 100% of examples
    if examples:
        fails = _validate_examples(examples, kept, cache, expr_cache)
        if fails:
            print(f"\n  VALIDATION FAILED on pruned set ({len(fails)} failures):")
            for f in fails[:10]:
                print(f"    {f}")
            print(f"  Falling back to full condition set.")
            kept = [{**c, "filter_power": round(filter_powers[i], 6)}
                    for i, c in enumerate(conditions)]
            dropped = []
        else:
            print(f"  OK: all examples pass pruned conditions ({len(kept)} conditions)")

    return kept, dropped, power_table
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
    tickers = list(cache.keys())
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


def deduplicate_signals(signals):
    """Consecutive signal bars per ticker → keep rightmost."""
    signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))
    deduped = []
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
        rightmost = signals[j-1]
        rightmost["cluster_size"] = j - i
        rightmost["cluster_start_date"] = signals[i]["date"]
        deduped.append(rightmost)
        i = j
    print(f"  Deduped: {len(signals):,} → {len(deduped):,}")
    return deduped


def apply_exit_and_measure(signals, cache, exit_cond, direction, expr_cache):
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]

    exit_col_idx = expr_cache.expr_index(expr_name)
    if exit_col_idx is None:
        raise RuntimeError(f"Exit expression '{expr_name}' not in expression cache")
    adr_col_idx = expr_cache.expr_index("adr14")

    print(f"\n  Applying exit: {expr_name} {exit_dir} {exit_thresh}  "
          f"(direction={direction}, max_forward={MAX_FORWARD})")

    results = []
    no_exit = 0
    _ticker_cache = {}

    for sig in signals:
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        df = cache.get(ticker)
        if df is None or bar_idx >= len(df) - 1:
            continue
        try:
            if ticker not in _ticker_cache:
                _ticker_cache[ticker] = expr_cache.get_ticker(ticker)
            cached_dates, cached_data = _ticker_cache[ticker]
            if cached_dates is None or len(cached_dates) != len(df):
                continue

            adr = (float(cached_data[bar_idx, adr_col_idx])
                   if adr_col_idx is not None else None)
            if adr is None or adr <= 0 or np.isnan(adr):
                h = df["high"].values
                l = df["low"].values
                s = max(0, bar_idx - 13)
                adr = float(np.mean(h[s:bar_idx+1] - l[s:bar_idx+1]))
            if adr <= 0:
                continue

            signal_close = float(df["close"].values[bar_idx])
            actual_forward = min(MAX_FORWARD, len(df) - bar_idx - 1)
            if actual_forward < 5:
                continue

            exit_series = cached_data[:, exit_col_idx]
            exit_bar = None
            exit_close = None
            for fwd in range(1, actual_forward + 1):
                idx = bar_idx + fwd
                val = exit_series[idx]
                if np.isnan(val):
                    continue
                # Handle both formats: "below"/"above" (new) and "<="/">=" (old)
                is_below = exit_dir in ("<=", "below")
                is_above = exit_dir in (">=", "above")
                if is_above and val >= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[idx])
                    break
                elif is_below and val <= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[idx])
                    break

            if exit_bar is None:
                no_exit += 1
                continue

            if direction == "short":
                move_adr = (signal_close - exit_close) / adr
                mfe_price = float(df["low"].values[bar_idx+1:bar_idx+exit_bar+1].min())
                mfe_adr = (signal_close - mfe_price) / adr
            else:
                move_adr = (exit_close - signal_close) / adr
                mfe_price = float(df["high"].values[bar_idx+1:bar_idx+exit_bar+1].max())
                mfe_adr = (mfe_price - signal_close) / adr

            results.append({
                **sig,
                "signal_close": round(signal_close, 2),
                "adr_at_signal": round(adr, 2),
                "exit_bar": exit_bar,
                "exit_date": str(df["date"].values[bar_idx + exit_bar])[:10],
                "exit_close": round(exit_close, 2),
                "move_adr": round(move_adr, 2),
                "mfe_adr": round(mfe_adr, 2),
                "capture_eff": round(move_adr / mfe_adr, 3) if mfe_adr > 0 else 0,
            })
        except Exception:
            continue

    print(f"  Exit applied: {len(results)} triggered, {no_exit} no exit")
    return results


def compute_example_floor(examples, cache, exit_cond, direction, expr_cache):
    """Compute ADR floor from validated examples — same logic as signal_filter.py."""
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]
    exit_col_idx = expr_cache.expr_index(expr_name)
    adr_col_idx = expr_cache.expr_index("adr14")

    example_adrs = []
    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        df = cache.get(ticker)
        if df is None:
            continue
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
        dates_str = [str(d)[:10] for d in df["date"].values]
        if entry_date not in dates_str:
            continue
        entry_idx = dates_str.index(entry_date)
        scan_idx = entry_idx - 1

        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None or len(cached_dates) != len(df):
            continue

        adr = (float(cached_data[scan_idx, adr_col_idx])
               if adr_col_idx is not None else None)
        if not adr or adr <= 0:
            continue

        signal_close = float(df["close"].values[scan_idx])
        actual_forward = min(MAX_FORWARD, len(df) - scan_idx - 1)

        exit_series = cached_data[:, exit_col_idx] if exit_col_idx is not None else None
        if exit_series is None:
            continue

        exit_bar = None
        exit_close = None
        is_above = exit_dir in (">=", "above")
        is_below = exit_dir in ("<=", "below")
        for fwd in range(1, actual_forward + 1):
            idx = scan_idx + fwd
            val = exit_series[idx]
            if np.isnan(val):
                continue
            if is_above and val >= exit_thresh:
                exit_bar = fwd
                exit_close = float(df["close"].values[idx])
                break
            elif is_below and val <= exit_thresh:
                exit_bar = fwd
                exit_close = float(df["close"].values[idx])
                break

        if exit_bar is None:
            continue

        if direction == "short":
            move_adr = (signal_close - exit_close) / adr
        else:
            move_adr = (exit_close - signal_close) / adr
        example_adrs.append(move_adr)

    if not example_adrs:
        return 0.0

    floor = np.percentile(example_adrs, 10) * 0.90  # 90% of 10th percentile
    print(f"  Example ADR floor: {floor:.2f}  "
          f"(n={len(example_adrs)}, median={np.median(example_adrs):.2f})")
    return max(floor, 0.0)


def filter_and_rank(results, min_adr):
    filtered = [r for r in results if r["move_adr"] >= min_adr]
    filtered.sort(key=lambda r: r["move_adr"], reverse=True)
    print(f"  Filtered: {len(results):,} → {len(filtered):,}  (>= {min_adr:.2f} ADR)")
    if filtered:
        moves = [r["move_adr"] for r in filtered]
        print(f"    Min: {min(moves):.2f}  Median: {sorted(moves)[len(moves)//2]:.2f}  "
              f"Max: {max(moves):.2f}")
    return filtered


# ══════════════════════════════════════════════════════════════
# OUTPUT — isolated to data/setup_refiner/
# ══════════════════════════════════════════════════════════════

def save_results(setup_type, pruned_conditions, dropped_conditions, power_table,
                 filtered_signals, source_path, exit_cond, min_adr,
                 n_raw, n_deduped, n_with_exit, skip_prune):
    """Save all outputs to data/setup_refiner/ — never touches other pipeline dirs."""
    out_dir = os.path.join(REPO_ROOT, "data", "setup_refiner")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_sig = len(filtered_signals)
    n_conds = len(pruned_conditions)

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "stage": "setup_refiner",
        "computation_path": "expression_cache_only",
        "source_conditions_file": os.path.basename(source_path),
        "skip_prune": skip_prune,
        "n_conditions_input": len(pruned_conditions) + len(dropped_conditions),
        "n_conditions_kept": n_conds,
        "n_conditions_dropped": len(dropped_conditions),
        "exit_condition": exit_cond,
        "min_adr_threshold": min_adr,
        "n_raw_signals": n_raw,
        "n_deduped": n_deduped,
        "n_with_exit": n_with_exit,
        "n_filtered": n_sig,
        "pruned_conditions": pruned_conditions,
        "dropped_conditions": dropped_conditions,
        "filter_power_table": power_table,
        "signals": filtered_signals,
    }

    # Timestamped archive
    ts_path = os.path.join(out_dir, f"refined_{setup_type}_{n_conds}cond_{n_sig}sig_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {ts_path}")

    # Latest pointer — never collides with signal_filter.py output
    latest_path = os.path.join(out_dir, f"refined_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved as latest: {latest_path}")

    return latest_path


def upload_to_railway(setup_type, filtered_signals, pruned_conditions,
                       exit_cond, min_adr, n_raw, n_deduped, n_with_exit):
    """Upload refined signals to Railway vetting UI."""
    payload = {
        "setup_type": setup_type,
        "stage": "refined",
        "timestamp": datetime.now().isoformat(),
        "exit_condition": exit_cond,
        "min_adr_threshold": min_adr,
        "n_raw_signals": n_raw,
        "n_deduped": n_deduped,
        "n_with_exit": n_with_exit,
        "n_filtered": len(filtered_signals),
        "n_conditions": len(pruned_conditions),
        "signals": filtered_signals,
    }
    url = f"{RAILWAY_URL}/api/setup-grinder/{setup_type}/upload-signals"
    try:
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        print(f"  Uploaded {len(filtered_signals)} signals to Railway")
    except Exception as e:
        print(f"  WARNING: Railway upload failed: {e}")
        print(f"  Signals saved locally — upload manually if needed.")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run_refiner(setup_type, conditions_file=None, min_power=DEFAULT_MIN_POWER,
                min_adr=None, skip_prune=False, workers=None):
    workers = workers or max(cpu_count() - 1, 1)
    direction = SETUP_CONFIGS.get(setup_type, {}).get("direction", "short")

    print(f"\n{'='*70}")
    print(f"  SETUP REFINER — {setup_type.upper()}")
    print(f"  Stage: condition prune + signal filter  (isolated outputs)")
    print(f"  Min filter power: {min_power:.0%}  |  Workers: {workers}")
    print(f"  Skip prune: {skip_prune}")
    print(f"{'='*70}")
    t0 = time.time()

    # ── Load ──
    print(f"\n  Loading data...")
    conditions, source_path = load_conditions(setup_type, conditions_file)
    cache = load_5yr_cache()
    exit_cond = load_exit_condition(setup_type)
    # Normalize multi-stage exit to single-stage for signal measurement.
    # setup_refiner uses the exit condition to find where each trade exits and
    # measure ADR capture. Multi-stage logic lives in multistage_exit_grinder.py.
    # We use the first stage (fires first) as the effective exit signal here.
    if exit_cond.get("type") == "multi":
        first_stage = exit_cond["stages"][0]
        print(f"  Multi-stage exit: using stage 1 of {exit_cond['n_stages']} for signal measurement")
        exit_cond = {
            "expression": first_stage["expr_name"],
            "threshold": first_stage["threshold"],
            "direction": first_stage["direction"],
        }
    examples = load_examples(setup_type)

    print(f"\n  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError(
            "Expression cache not found or invalid.\n"
            "Run: python local_runner/expr_cache_builder.py --build"
        )
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # ── Prune ──
    if skip_prune:
        print(f"\n  Skipping prune — using all {len(conditions)} conditions as-is")
        pruned = [{**c, "filter_power": None} for c in conditions]
        dropped = []
        power_table = []
    else:
        print(f"\n  {'─'*60}")
        print(f"  PHASE 1: CONDITION PRUNING")
        print(f"  {'─'*60}")
        pruned, dropped, power_table = prune_conditions(
            conditions, cache, examples, expr_cache, workers, min_power
        )

    # ── Scan signals ──
    # If conditions were pruned, we must re-scan the universe with the pruned set
    # because fewer conditions means MORE signals can pass (superset of original).
    # If no pruning, load from pyramid JSON directly.
    print(f"\n  {'─'*60}")
    print(f"  PHASE 2: SIGNAL SCAN")
    print(f"  {'─'*60}")

    if dropped:
        print(f"  {len(dropped)} conditions pruned → re-scanning with {len(pruned)} conditions")
        raw_signals = scan_signals(cache, pruned, expr_cache, workers)
        n_raw = len(raw_signals)
    else:
        # No pruning — use pyramid signals directly
        with open(source_path) as f:
            pyramid_data = json.load(f)
        tier_results = pyramid_data.get("tier_results", {})
        raw_signal_list = []
        for key in sorted(tier_results.keys(), reverse=True):
            tr = tier_results[key]
            fs = tr.get("final_signals", [])
            if fs:
                raw_signal_list = fs
                print(f"  Using {len(fs)} signals from tier '{key}'")
                break

        if not raw_signal_list:
            print(f"  WARNING: No final_signals in pyramid result. Falling back to scan.")
            raw_signals = scan_signals(cache, pruned, expr_cache, workers)
            n_raw = len(raw_signals)
        else:
            raw_signals = []
            for sig in raw_signal_list:
                ticker = sig["ticker"]
                date_str = sig["date"]
                df = cache.get(ticker)
                if df is None:
                    continue
                if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                    df["date"] = pd.to_datetime(df["date"])
                date_matches = df.index[df["date"].dt.strftime("%Y-%m-%d") == date_str].tolist()
                if not date_matches:
                    continue
                bar_idx = date_matches[0]
                raw_signals.append({
                    "ticker": ticker,
                    "date": date_str,
                    "bar_idx": int(bar_idx),
                    "close": float(df["close"].values[bar_idx]),
                })
            n_raw = len(raw_signals)
            print(f"  Resolved {n_raw} signals with bar indices")

    # ── Dedup ──
    deduped = deduplicate_signals(raw_signals)
    n_deduped = len(deduped)

    # ── Exit ──
    with_exit = apply_exit_and_measure(deduped, cache, exit_cond, direction, expr_cache)
    n_with_exit = len(with_exit)

    # ── ADR floor ──
    if min_adr is None:
        min_adr = compute_example_floor(examples, cache, exit_cond, direction, expr_cache)

    # ── Filter + rank ──
    filtered = filter_and_rank(with_exit, min_adr)

    total_time = time.time() - t0

    # ── Save ──
    print(f"\n  {'─'*60}")
    print(f"  SAVING RESULTS (data/setup_refiner/ — isolated)")
    print(f"  {'─'*60}")
    latest_path = save_results(
        setup_type, pruned, dropped, power_table,
        filtered, source_path, exit_cond, min_adr,
        n_raw, n_deduped, n_with_exit, skip_prune
    )

    # ── Upload ──
    upload_to_railway(
        setup_type, filtered, pruned, exit_cond, min_adr,
        n_raw, n_deduped, n_with_exit
    )

    print(f"\n  {'='*70}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  Conditions: {len(conditions)} input → {len(pruned)} kept "
          f"({len(dropped)} pruned)")
    print(f"  Signals: {n_raw:,} raw → {n_deduped:,} deduped → "
          f"{n_with_exit:,} with exit → {len(filtered):,} final")
    print(f"  Output: {latest_path}")
    print(f"  {'='*70}\n")

    return filtered


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Setup Refiner — condition prune + signal filter (isolated stage)")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--min-power", type=float, default=DEFAULT_MIN_POWER,
                        help="Min filter power threshold (default: 0.10). "
                             "Conditions eliminating less than N pct of passing rows get pruned.")
    parser.add_argument("--min-adr", type=float, default=None,
                        help="Min exit distance in ADR (default: derived from examples)")
    parser.add_argument("--conditions-file", default=None,
                        help="Path to conditions JSON — bypasses auto-discovery. "
                             "Accepts pyramid result or any file with all_conditions key.")
    parser.add_argument("--skip-prune", action="store_true",
                        help="Skip condition pruning — run signal filter only")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of parallel workers (default: cpu_count - 1)")
    args = parser.parse_args()

    run_refiner(
        setup_type=args.setup,
        conditions_file=args.conditions_file,
        min_power=args.min_power,
        min_adr=args.min_adr,
        skip_prune=args.skip_prune,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
