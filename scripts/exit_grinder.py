"""
Exit Grinder — Step 6 of ANALYSIS_SYSTEM.md

Brute forces post-signal expressions against validated examples' forward paths
to find TA-driven exit conditions that reliably capture the most move.

Benchmark: entry candle high → exit candle close (% move + ADR captured).
For shorts: positive captured = price went down from entry high.

Optimizations:
    - ProcessPoolExecutor for parallel example matrix builds
    - Boolean aggregations computed via pure numpy (no engine modification)
    - Vectorized threshold testing with numpy broadcasting
    - All CPU cores used

Usage:
    python scripts/exit_grinder.py --setup dtss --max-forward 120
    python scripts/exit_grinder.py --setup dtss --max-forward 120 --workers 12
"""

import argparse
import sys
import os
import time
import json
import numpy as np
import pandas as pd
import pickle
import requests
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ProcessPoolExecutor, as_completed

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.exit_expressions import (
    generate_exit_expressions, generate_exit_boolean_conditions,
    generate_all_exit_expressions, WINDOWS,
)

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD_DEFAULT = 252  # bars after entry to analyze (~1 year)
DEFAULT_WORKERS = os.cpu_count() or 8

# Local cache paths (same as pyramid_grinder)
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")


# ============================================================
# Data Classes
# ============================================================

@dataclass
class ExampleData:
    """Loaded example with OHLCV and engine ready."""
    id: int
    ticker: str
    entry_date: str
    df: pd.DataFrame          # full OHLCV
    entry_idx: int             # index of entry bar in df
    entry_high: float          # entry candle high (benchmark ref)
    n_forward: int             # bars available after entry
    mfe_pct: float             # max favorable excursion as % from entry high
    direction: str


@dataclass
class ExitCandidate:
    """A candidate exit condition with scores across all examples."""
    expr_name: str
    direction: str              # "above" or "below" threshold
    threshold: float
    # Per-example results
    exit_bars: list             # forward bar index where condition first triggers
    pct_moves: list             # % move: (entry_high - exit_close) / entry_high * 100
    adr_captured: list          # move in ADR units
    capture_effs: list          # captured / MFE per example
    # Aggregates
    examples_triggered: int
    floor_pct_move: float
    median_pct_move: float
    avg_pct_move: float
    floor_capture_eff: float
    median_capture_eff: float
    avg_bars_to_exit: float


# ============================================================
# Data Loading
# ============================================================

def load_5yr_cache():
    """Load 5-year OHLCV cache from local disk."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")
    print(f"Loading 5yr OHLCV cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  {len(cache)} tickers loaded")
    return cache


def load_examples(setup_type: str) -> list:
    """Load examples from Railway API (metadata only — ticker + entry date)."""
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    data = r.json()
    examples = data["examples"]
    print(f"Loaded {len(examples)} {setup_type.upper()} examples")
    return examples


def build_example_data(example: dict, direction: str, max_forward: int,
                       universe_cache: dict, spy_df: pd.DataFrame = None) -> Optional[ExampleData]:
    """Build ExampleData using local 5yr OHLCV cache."""
    ticker = example["ticker"]
    entry_date = example["entryDate"]

    try:
        df = universe_cache.get(ticker)
        if df is None:
            print(f"  SKIP {ticker} — not in 5yr cache")
            return None

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        entry_dt = pd.to_datetime(entry_date)
        date_matches = df.index[df["date"] == entry_dt].tolist()
        if not date_matches:
            # Try matching date string
            date_matches = df.index[df["date"].dt.strftime("%Y-%m-%d") == entry_date].tolist()
        if not date_matches:
            print(f"  SKIP {ticker} — entry date {entry_date} not found")
            return None
        entry_idx = date_matches[0]

        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            print(f"  SKIP {ticker} — only {n_available} forward bars")
            return None

        actual_forward = min(max_forward, n_available)
        entry_high = df["high"].iloc[entry_idx]

        # Compute MFE %
        if direction == "short":
            fwd_lows = df["low"].iloc[entry_idx:entry_idx + actual_forward + 1].values
            mfe_price = np.min(fwd_lows)
            mfe_pct = (entry_high - mfe_price) / entry_high * 100
        else:
            fwd_highs = df["high"].iloc[entry_idx:entry_idx + actual_forward + 1].values
            entry_low = df["low"].iloc[entry_idx]
            mfe_price = np.max(fwd_highs)
            mfe_pct = (mfe_price - entry_low) / entry_low * 100

        return ExampleData(
            id=example["id"],
            ticker=ticker,
            entry_date=entry_date,
            df=df,
            entry_idx=entry_idx,
            entry_high=entry_high,
            n_forward=actual_forward,
            mfe_pct=mfe_pct,
            direction=direction,
        )
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")
        return None


# ============================================================
# Expression Matrix Building — Per Example (parallelizable)
# ============================================================

def _build_one_example_matrix(args):
    """Build expression matrix for one example. Runs in subprocess."""
    ex_dict, expressions, direction, spy_pickle = args
    
    # Reimport inside subprocess
    import pandas as pd
    import numpy as np
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.exit_compute import ExitExprEngine

    df = pd.DataFrame(ex_dict["df_records"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    entry_idx = ex_dict["entry_idx"]
    n_forward = ex_dict["n_forward"]

    spy_df = None
    if spy_pickle is not None:
        spy_df = pd.DataFrame(spy_pickle)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in spy_df.columns:
                spy_df[col] = pd.to_numeric(spy_df[col], errors="coerce")

    engine = ExitExprEngine(df, entry_idx, direction=direction, spy_df=spy_df,
                            max_forward=n_forward)

    result = {}
    failed = 0
    failed_names = []
    for expr in expressions:
        try:
            series = engine.compute(expr["compute"])
            result[expr["name"]] = series
        except Exception as e:
            failed += 1
            failed_names.append((expr["name"], str(e)))

    return ex_dict["ticker"], result, failed, len(expressions), failed_names


def build_all_matrices_parallel(examples: list, expressions: list,
                                 direction: str, spy_df: pd.DataFrame,
                                 workers: int) -> list:
    """Build expression matrices for all examples in parallel."""
    # Serialize example data for subprocesses
    spy_records = spy_df.to_dict("records") if spy_df is not None else None
    
    tasks = []
    for ex in examples:
        ex_dict = {
            "ticker": ex.ticker,
            "entry_idx": ex.entry_idx,
            "n_forward": ex.n_forward,
            "df_records": ex.df.to_dict("records"),
        }
        tasks.append((ex_dict, expressions, direction, spy_records))

    print(f"\nComputing {len(expressions)} expressions × {len(examples)} examples "
          f"({workers} workers)...")
    t0 = time.time()

    matrices = [None] * len(examples)
    ticker_to_idx = {ex.ticker + ex.entry_date: i for i, ex in enumerate(examples)}

    all_failed_names = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_build_one_example_matrix, task): task[0]["ticker"]
                   for task in tasks}
        done = 0
        for future in as_completed(futures):
            ticker_name = futures[future]
            try:
                ticker, matrix, failed, total, failed_names = future.result()
                # Find matching example
                for i, ex in enumerate(examples):
                    if ex.ticker == ticker and matrices[i] is None:
                        matrices[i] = matrix
                        break
                done += 1
                computed = total - failed
                print(f"  [{done}/{len(examples)}] {ticker:8s} — "
                      f"{computed}/{total} computed" +
                      (f" ({failed} failed)" if failed else ""))
                # Track failed names (first example's failures are representative)
                if failed_names and not all_failed_names:
                    all_failed_names = {name: err for name, err in failed_names}
            except Exception as e:
                done += 1
                print(f"  [{done}/{len(examples)}] {ticker_name:8s} — ERROR: {e}")

    elapsed = time.time() - t0
    print(f"Matrix build: {elapsed:.1f}s ({workers} workers)")

    if all_failed_names:
        print(f"\n  ⚠ {len(all_failed_names)} expressions failed:")
        for name, err in sorted(all_failed_names.items()):
            print(f"    ✗ {name}: {err}")

    return matrices


# ============================================================
# Boolean Aggregation — Pure Numpy, No Engine Needed
# ============================================================

def compute_boolean_aggregations(base_matrices: list, native_bools: list,
                                  threshold_bools: list, n_forwards: list) -> list:
    """Compute boolean aggregation expressions from base series.
    
    For each example's matrix, derive bool series from native ops and
    threshold conditions, then compute rolling aggregations.
    
    Returns list of dicts (one per example) with aggregation name → series.
    """
    t0 = time.time()
    n_examples = len(base_matrices)
    
    # Build bool condition evaluators
    # Native bools: just grab the base series (already 0/1)
    native_names = [e["name"] for e in native_bools]
    
    # Threshold bools: evaluate base_series > threshold or < threshold
    thresh_specs = []
    for tb in threshold_bools:
        cond = tb["condition"]
        # Build the compute spec to match against base expression names
        base_op = cond["base_op"]
        # Reconstruct the base expression name from condition params
        thresh_specs.append({
            "name": tb["name"],
            "cond": cond,
        })
    
    agg_types = ["count_true", "pct_true", "since_true", "true_in_row"]
    
    result_matrices = []
    total_aggs = 0
    
    for ex_i in range(n_examples):
        matrix = base_matrices[ex_i]
        if matrix is None:
            result_matrices.append({})
            continue
            
        n_fwd = n_forwards[ex_i]
        agg_dict = {}
        
        # Step 1: Build all boolean series for this example
        bool_series = {}
        
        # Native booleans (already in base matrix as 0/1 series)
        for name in native_names:
            if name in matrix:
                bool_series[name] = matrix[name].astype(np.float64)
        
        # Threshold booleans (derive from base continuous series)
        for spec in thresh_specs:
            cond = spec["cond"]
            # Find the matching base expression
            base_name = _find_base_expr_name(cond, matrix)
            if base_name is None or base_name not in matrix:
                continue
            base_vals = matrix[base_name]
            thresh = cond["threshold"]
            if cond["direction"] == "above":
                bool_series[spec["name"]] = (base_vals > thresh).astype(np.float64)
            else:
                bool_series[spec["name"]] = (base_vals < thresh).astype(np.float64)
        
        # Step 2: Compute rolling aggregations over windows
        for bool_name, bseries in bool_series.items():
            n = len(bseries)
            for w in WINDOWS:
                if n < w:
                    continue
                
                # count_true: sum of True in last w bars
                cumsum = np.cumsum(bseries)
                ct = np.full(n, np.nan)
                ct[w-1:] = cumsum[w-1:] - np.concatenate([[0], cumsum[:n-w]])
                agg_dict[f"{bool_name}_count_true_{w}b"] = ct
                
                # pct_true: count_true / w
                pt = ct / w
                agg_dict[f"{bool_name}_pct_true_{w}b"] = pt
                
                # since_true: bars since last True (0 = currently True)
                st = np.full(n, np.nan)
                last_true = -999
                for j in range(n):
                    if bseries[j] > 0.5:
                        last_true = j
                    if last_true >= 0:
                        st[j] = j - last_true
                agg_dict[f"{bool_name}_since_true_{w}b"] = st
                
                # true_in_row: current consecutive True count
                tir = np.full(n, np.nan)
                streak = 0
                for j in range(n):
                    if bseries[j] > 0.5:
                        streak += 1
                    else:
                        streak = 0
                    tir[j] = streak
                agg_dict[f"{bool_name}_true_in_row_{w}b"] = tir
                
                total_aggs += 4
        
        result_matrices.append(agg_dict)
    
    elapsed = time.time() - t0
    print(f"Boolean aggregations: {total_aggs} series in {elapsed:.1f}s")
    return result_matrices


def _find_base_expr_name(cond: dict, matrix: dict) -> Optional[str]:
    """Find the base expression name in the matrix that matches a threshold condition."""
    op = cond["base_op"]
    
    # Direct op-to-name mappings based on expression naming patterns
    if op == "rsi":
        return f"rsi_{cond['period']}"
    elif op == "roc":
        return f"roc_{cond['period']}"
    elif op == "stochastic":
        return f"stoch_{cond['period']}"
    elif op == "adx":
        return f"adx_{cond['period']}"
    elif op == "adx_slope":
        return f"adx_{cond['period']}_slope_{cond.get('offset', 3)}"
    elif op == "di_spread":
        return f"di_spread_{cond['period']}"
    elif op == "macd_histogram":
        return f"macd_hist_{cond['fast']}_{cond['slow']}_{cond['signal']}"
    elif op == "macd_histogram_slope":
        return f"macd_hist_slope_{cond['fast']}_{cond['slow']}_{cond['signal']}_{cond.get('offset',3)}b"
    elif op == "capture_efficiency":
        return "capture_efficiency"
    elif op == "ext_slope":
        return f"ext_slope_{cond['ma']}_{cond['normalizer']}_{cond.get('offset',3)}b"
    elif op == "ext_ceiling_ratio":
        return f"ext_ceil_{cond['ma']}_{cond['normalizer']}_lb{cond.get('lookback',252)}"
    elif op == "retrace_from_mfe_pct":
        return "retrace_from_mfe_pct_raw"
    elif op == "rvol":
        return f"rvol_{cond.get('avg_period', 20)}"
    elif op == "bar_range":
        return f"bar_range_{cond.get('normalizer', 'adr14')}"
    elif op == "bollinger_pctb":
        return f"bb_pctb_{cond['period']}"
    elif op == "rs_vs_spy":
        return f"rs_vs_spy_{cond.get('period', 10)}"
    
    return None


# ============================================================
# Vectorized Threshold Testing
# ============================================================

def compute_move_at_bar(example: ExampleData, bar_idx: int) -> dict:
    """Compute move metrics at a specific forward bar."""
    abs_idx = example.entry_idx + bar_idx
    if abs_idx >= len(example.df):
        return None
    exit_close = example.df["close"].iloc[abs_idx]
    entry_high = example.entry_high

    if example.direction == "short":
        raw_move = entry_high - exit_close
        pct_move = raw_move / entry_high * 100
    else:
        entry_low = example.df["low"].iloc[example.entry_idx]
        raw_move = exit_close - entry_low
        pct_move = raw_move / entry_low * 100

    # ADR at entry for normalization
    from scripts.expression_engine import ExpressionEngine
    eng = ExpressionEngine(example.df)
    adr_series = eng._adr(14).values
    adr_val = adr_series[example.entry_idx]
    adr_captured = raw_move / adr_val if adr_val > 0 else 0.0

    capture_eff = pct_move / example.mfe_pct if example.mfe_pct > 0 else 0.0

    return {
        "exit_close": exit_close,
        "pct_move": pct_move,
        "adr_captured": adr_captured,
        "capture_eff": capture_eff,
    }


def generate_thresholds(values: np.ndarray, n_thresholds: int = 20) -> list:
    """Generate threshold values from data distribution using percentiles."""
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


def grind_exits(examples: list, all_matrices: list, expr_names: list,
                direction: str = "short", n_thresholds: int = 20,
                min_bar: int = 1, min_trigger_pct: float = 1.0,
                top_n: int = 50) -> list:
    """Grind all expressions × thresholds × directions.
    
    Vectorized: for each expression, stack all examples' series into a 2D array,
    then test thresholds with numpy broadcasting.
    """
    n_examples = len(examples)
    min_triggered = max(1, int(n_examples * min_trigger_pct))
    
    # Pre-compute move data for every example at every bar
    # This avoids recomputing per-candidate
    max_bars = max(ex.n_forward for ex in examples) + 1
    move_cache = {}  # (example_idx, bar) → move dict
    
    print(f"\nPre-computing move data for {n_examples} examples × {max_bars} bars...")
    t0 = time.time()
    for i, ex in enumerate(examples):
        for bar in range(min_bar, ex.n_forward + 1):
            move = compute_move_at_bar(ex, bar)
            if move:
                move_cache[(i, bar)] = move
    print(f"Move cache: {len(move_cache)} entries in {time.time()-t0:.1f}s")

    all_candidates = []
    n_exprs = len(expr_names)

    print(f"\nGrinding {n_exprs} expressions × ~{n_thresholds} thresholds × 2 directions...")
    print(f"Min trigger: {min_triggered}/{n_examples} ({min_trigger_pct*100:.0f}%)")

    t0 = time.time()
    tested = 0
    passed = 0

    for expr_i, expr_name in enumerate(expr_names):
        if (expr_i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{expr_i+1}/{n_exprs}] {rate:.0f} expr/s, "
                  f"{passed} candidates, {tested} tested")

        # Gather this expression's values across all examples
        all_values = []
        example_series = []
        for i, matrix in enumerate(all_matrices):
            if matrix is not None and expr_name in matrix:
                series = matrix[expr_name]
                example_series.append((i, series))
                vals = series[~np.isnan(series)]
                if len(vals) > 0:
                    all_values.append(vals)

        if len(example_series) < min_triggered:
            continue

        if not all_values:
            continue

        combined = np.concatenate(all_values)
        thresholds = generate_thresholds(combined, n_thresholds)
        if not thresholds:
            continue

        # Test both directions
        for thresh in thresholds:
            for dir_test in ["above", "below"]:
                tested += 1
                
                exit_bars = [-1] * n_examples
                pct_moves = [np.nan] * n_examples
                adr_captured = [np.nan] * n_examples
                capture_effs = [np.nan] * n_examples
                triggered = 0

                for ex_idx, series in example_series:
                    # Find first bar >= min_bar where condition triggers
                    for bar in range(min_bar, len(series)):
                        val = series[bar]
                        if np.isnan(val):
                            continue
                        hit = (val > thresh) if dir_test == "above" else (val < thresh)
                        if hit:
                            key = (ex_idx, bar)
                            if key in move_cache:
                                move = move_cache[key]
                                exit_bars[ex_idx] = bar
                                pct_moves[ex_idx] = move["pct_move"]
                                adr_captured[ex_idx] = move["adr_captured"]
                                capture_effs[ex_idx] = move["capture_eff"]
                                triggered += 1
                            break

                if triggered < min_triggered:
                    continue

                valid_pcts = [p for p in pct_moves if not np.isnan(p)]
                valid_effs = [e for e in capture_effs if not np.isnan(e)]
                valid_bars = [b for b in exit_bars if b >= 0]

                passed += 1
                all_candidates.append(ExitCandidate(
                    expr_name=expr_name,
                    direction=dir_test,
                    threshold=thresh,
                    exit_bars=exit_bars,
                    pct_moves=pct_moves,
                    adr_captured=adr_captured,
                    capture_effs=capture_effs,
                    examples_triggered=triggered,
                    floor_pct_move=min(valid_pcts),
                    median_pct_move=float(np.median(valid_pcts)),
                    avg_pct_move=float(np.mean(valid_pcts)),
                    floor_capture_eff=min(valid_effs),
                    median_capture_eff=float(np.median(valid_effs)),
                    avg_bars_to_exit=float(np.mean(valid_bars)),
                ))

    elapsed = time.time() - t0
    print(f"\nDone: tested {tested:,} conditions in {elapsed:.1f}s")
    print(f"Passed filter: {passed:,} candidates (>= {min_triggered} examples)")

    all_candidates.sort(key=lambda c: (c.median_pct_move, c.avg_pct_move), reverse=True)
    return all_candidates[:top_n]


# ============================================================
# Reporting
# ============================================================

def print_results(candidates: list, examples: list, top_n: int = 30):
    """Print ranked exit conditions with per-example % moves."""
    if not candidates:
        print("\nNo exit conditions found matching criteria.")
        return

    n_show = min(top_n, len(candidates))

    print(f"\n{'='*120}")
    print(f"TOP {n_show} EXIT CONDITIONS — ranked by floor capture efficiency")
    print(f"{'='*120}")

    for rank, cand in enumerate(candidates[:n_show], 1):
        print(f"\n{'─'*120}")
        print(f"#{rank:3d}  {cand.expr_name} {cand.direction} {cand.threshold:.4f}")
        print(f"      Triggered: {cand.examples_triggered}/{len(examples)}  |  "
              f"Avg bars: {cand.avg_bars_to_exit:.1f}  |  "
              f"Floor capture eff: {cand.floor_capture_eff:.2f}  |  "
              f"Median capture eff: {cand.median_capture_eff:.2f}")
        print(f"      % Move  →  floor: {cand.floor_pct_move:+.2f}%  |  "
              f"median: {cand.median_pct_move:+.2f}%  |  "
              f"avg: {cand.avg_pct_move:+.2f}%")

        print(f"      {'Ticker':8s} {'Entry Date':12s} {'Bar#':>5s} "
              f"{'% Move':>8s} {'ADR Capt':>9s} {'Capt Eff':>9s}")
        for i, ex in enumerate(examples):
            bar = cand.exit_bars[i]
            if bar < 0:
                print(f"      {ex.ticker:8s} {ex.entry_date:12s}   ---   (not triggered)")
                continue
            pct = cand.pct_moves[i]
            adr = cand.adr_captured[i]
            eff = cand.capture_effs[i]
            print(f"      {ex.ticker:8s} {ex.entry_date:12s} {bar:5d} "
                  f"{pct:+7.2f}% {adr:+8.2f} ADR {eff:8.2f}")


def print_mfe_summary(examples: list):
    """Print MFE summary for context."""
    print(f"\n{'='*80}")
    print(f"MFE SUMMARY — Maximum Favorable Excursion per example")
    print(f"{'='*80}")
    print(f"{'Ticker':8s} {'Entry Date':12s} {'Entry High':>11s} {'MFE %':>8s} {'Fwd Bars':>9s}")
    for ex in examples:
        print(f"{ex.ticker:8s} {ex.entry_date:12s} ${ex.entry_high:10.2f} "
              f"{ex.mfe_pct:+7.2f}% {ex.n_forward:9d}")
    mfes = [ex.mfe_pct for ex in examples]
    print(f"\n  Floor MFE:  {min(mfes):+.2f}%")
    print(f"  Median MFE: {np.median(mfes):+.2f}%")
    print(f"  Avg MFE:    {np.mean(mfes):+.2f}%")


def save_results(candidates: list, examples: list, setup_type: str, args):
    """Save results to JSON."""
    os.makedirs("data/exit_grind", exist_ok=True)
    outpath = f"data/exit_grind/exit_grind_{setup_type}.json"

    data = {
        "setup_type": setup_type,
        "direction": args.direction,
        "max_forward": args.max_forward,
        "n_thresholds": args.n_thresholds,
        "min_trigger_pct": args.min_trigger_pct,
        "n_examples": len(examples),
        "examples": [
            {"ticker": ex.ticker, "entry_date": ex.entry_date,
             "entry_high": ex.entry_high, "mfe_pct": ex.mfe_pct}
            for ex in examples
        ],
        "results": [
            {
                "rank": i + 1,
                "expr_name": c.expr_name,
                "direction": c.direction,
                "threshold": c.threshold,
                "examples_triggered": c.examples_triggered,
                "floor_pct_move": c.floor_pct_move,
                "median_pct_move": c.median_pct_move,
                "avg_pct_move": c.avg_pct_move,
                "floor_capture_eff": c.floor_capture_eff,
                "median_capture_eff": c.median_capture_eff,
                "avg_bars_to_exit": c.avg_bars_to_exit,
                "exit_bars": c.exit_bars,
                "pct_moves": c.pct_moves,
                "adr_captured": c.adr_captured,
                "capture_effs": c.capture_effs,
            }
            for i, c in enumerate(candidates)
        ],
    }

    with open(outpath, "w") as f:
        json.dump(data, f, indent=2, default=lambda x: None if isinstance(x, float) and np.isnan(x) else x)
    print(f"\nResults saved to {outpath}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Exit Grinder — Step 6")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--n-thresholds", type=int, default=20)
    parser.add_argument("--min-bar", type=int, default=1)
    parser.add_argument("--min-trigger-pct", type=float, default=1.0)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--direction", default="short")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--base-only", action="store_true",
                        help="Only test base expressions (skip boolean aggregations)")
    args = parser.parse_args()

    print(f"Exit Grinder — Step 6")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Thresholds: {args.n_thresholds}")
    print(f"Min trigger: {args.min_trigger_pct*100:.0f}%, Min bar: {args.min_bar}")
    print(f"Workers: {args.workers}")

    # 1. Load examples
    raw_examples = load_examples(args.setup)

    # 2. Load 5yr OHLCV cache
    universe_cache = load_5yr_cache()

    # 3. Get SPY from cache
    spy_df = universe_cache.get("SPY")
    if spy_df is not None:
        spy_df = spy_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(spy_df["date"]):
            spy_df["date"] = pd.to_datetime(spy_df["date"])
        spy_df = spy_df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            spy_df[col] = pd.to_numeric(spy_df[col], errors="coerce")
        print(f"SPY: {len(spy_df)} bars from local cache")
    else:
        print("WARNING: SPY not in cache")

    # 4. Build ExampleData
    print(f"\nBuilding example data...")
    examples = []
    for raw in raw_examples:
        print(f"  {raw['ticker']:8s} {raw['entryDate']}...", end="", flush=True)
        ex = build_example_data(raw, args.direction, args.max_forward, universe_cache, spy_df)
        if ex:
            examples.append(ex)
            print(f" OK — {ex.n_forward} fwd bars, MFE={ex.mfe_pct:+.2f}%")
        else:
            print(" SKIP")

    if len(examples) < 3:
        print(f"\nOnly {len(examples)} examples — need at least 3. Aborting.")
        return

    print(f"\n{len(examples)} examples ready")
    print_mfe_summary(examples)

    # 4. Generate expression library
    base_exprs = generate_exit_expressions()
    native_bools, threshold_bools = generate_exit_boolean_conditions(base_exprs)
    print(f"\nExpression library: {len(base_exprs)} base expressions")
    print(f"Boolean conditions: {len(native_bools)} native + {len(threshold_bools)} threshold")

    # 5. Build base expression matrices (parallel)
    base_matrices = build_all_matrices_parallel(
        examples, base_exprs, args.direction, spy_df, args.workers
    )

    # 6. Compute boolean aggregations (if not --base-only)
    if not args.base_only:
        n_forwards = [ex.n_forward for ex in examples]
        agg_matrices = compute_boolean_aggregations(
            base_matrices, native_bools, threshold_bools, n_forwards
        )
        
        # Merge base + aggregation matrices
        all_matrices = []
        for i in range(len(examples)):
            merged = {}
            if base_matrices[i]:
                merged.update(base_matrices[i])
            if agg_matrices[i]:
                merged.update(agg_matrices[i])
            all_matrices.append(merged)
        
        # Collect all expression names
        all_expr_names = [e["name"] for e in base_exprs]
        # Add aggregation names (from first non-empty matrix)
        for m in agg_matrices:
            if m:
                all_expr_names.extend(sorted(m.keys()))
                break
        
        print(f"\nTotal expressions to grind: {len(all_expr_names)}")
    else:
        all_matrices = base_matrices
        all_expr_names = [e["name"] for e in base_exprs]
        print(f"\n--base-only: grinding {len(all_expr_names)} base expressions")

    # 7. Grind
    candidates = grind_exits(
        examples, all_matrices, all_expr_names,
        direction=args.direction,
        n_thresholds=args.n_thresholds,
        min_bar=args.min_bar,
        min_trigger_pct=args.min_trigger_pct,
        top_n=args.top_n,
    )

    # 8. Report
    print_results(candidates, examples, top_n=args.top_n)

    # 9. Save
    save_results(candidates, examples, args.setup, args)

    # 10. Summary
    if candidates:
        best = candidates[0]
        print(f"\n{'='*80}")
        print(f"BEST EXIT: {best.expr_name} {best.direction} {best.threshold:.4f}")
        print(f"  Triggers on {best.examples_triggered}/{len(examples)} examples")
        print(f"  Floor % move: {best.floor_pct_move:+.2f}%")
        print(f"  Median % move: {best.median_pct_move:+.2f}%")
        print(f"  Floor capture eff: {best.floor_capture_eff:.2f}")
        print(f"  Avg bars to exit: {best.avg_bars_to_exit:.1f}")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
