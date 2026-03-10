"""
Profit Grinder — Step 4a of ANALYSIS_SYSTEM.md

Brute forces post-entry expressions against validated examples' forward paths
to find TA-driven exit conditions that maximize capture from the ENTRY BAR HIGH.

Uses the SAME expression cache as the pyramid grinder (12,131 expressions).
No separate exit expression library — one library, one computation path.

For each example: load ticker .npz from expr cache, find entry bar by date,
slice forward window, test thresholds. Expressions that are all-NaN in the
forward window are auto-skipped.

Benchmark: entry candle high → exit candle close (% move + ADR captured).
For shorts: positive captured = price went down from entry high.

Usage:
    python scripts/profit_grinder.py --setup dtss
    python scripts/profit_grinder.py --setup dtss --max-forward 120 --workers 12
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
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ProcessPoolExecutor, as_completed

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD_DEFAULT = 120
DEFAULT_WORKERS = os.cpu_count() or 8

LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")


# ============================================================
# Data Classes
# ============================================================

@dataclass
class ExampleData:
    """Loaded example with forward expression matrix from cache."""
    id: int
    ticker: str
    entry_date: str
    entry_idx: int
    entry_high: float
    n_forward: int              # bars available after entry
    mfe_pct: float              # max favorable excursion as % from entry high
    direction: str
    # Forward expression matrix: (n_forward+1, n_expressions) — from expr cache
    fwd_matrix: np.ndarray
    # OHLCV arrays for scoring (entry bar onward)
    fwd_closes: np.ndarray
    fwd_highs: np.ndarray
    fwd_lows: np.ndarray
    adr_at_entry: float
    # Forward dates for exit date resolution
    fwd_dates: list


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
    """Load examples from Railway API."""
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    data = r.json()
    examples = data["examples"]
    print(f"Loaded {len(examples)} {setup_type.upper()} examples")
    return examples


def _load_one_example(args):
    """Load one example from expr cache + OHLCV cache. Runs in subprocess."""
    (ticker, entry_date, example_id, direction, max_forward,
     ohlcv_records, cache_dir) = args

    import numpy as np
    import pandas as pd
    import os

    try:
        # Build OHLCV DataFrame
        df = pd.DataFrame(ohlcv_records)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        # Find entry bar
        entry_dt = pd.to_datetime(entry_date)
        date_matches = df.index[df["date"] == entry_dt].tolist()
        if not date_matches:
            date_matches = df.index[df["date"].dt.strftime("%Y-%m-%d") == entry_date].tolist()
        if not date_matches:
            return None, f"SKIP {ticker} — entry date {entry_date} not found"

        entry_idx = date_matches[0]
        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            return None, f"SKIP {ticker} — only {n_available} forward bars"

        actual_forward = min(max_forward, n_available)
        entry_high = float(df["high"].iloc[entry_idx])

        # MFE
        if direction == "short":
            fwd_lows = df["low"].iloc[entry_idx:entry_idx + actual_forward + 1].values
            mfe_price = float(np.min(fwd_lows))
            mfe_pct = (entry_high - mfe_price) / entry_high * 100
        else:
            fwd_highs = df["high"].iloc[entry_idx:entry_idx + actual_forward + 1].values
            entry_low = float(df["low"].iloc[entry_idx])
            mfe_price = float(np.max(fwd_highs))
            mfe_pct = (mfe_price - entry_low) / entry_low * 100

        # ADR14 at entry
        highs = df["high"].values
        lows = df["low"].values
        hl = highs - lows
        start14 = max(0, entry_idx - 13)
        adr_val = float(np.mean(hl[start14:entry_idx + 1]))
        if adr_val <= 0:
            adr_val = 1.0

        # Forward OHLCV slices
        fwd_closes = df["close"].values[entry_idx:entry_idx + actual_forward + 1].astype(np.float64)
        fwd_highs_arr = df["high"].values[entry_idx:entry_idx + actual_forward + 1].astype(np.float64)
        fwd_lows_arr = df["low"].values[entry_idx:entry_idx + actual_forward + 1].astype(np.float64)

        # Load expression cache for this ticker
        safe_ticker = ticker.replace("/", "_").replace("\\", "_")
        npz_path = os.path.join(cache_dir, f"{safe_ticker}.npz")
        if not os.path.exists(npz_path):
            return None, f"SKIP {ticker} — not in expression cache"

        loaded = np.load(npz_path, allow_pickle=True)
        cache_dates = loaded["dates"]
        cache_data = loaded["data"]  # (n_bars, n_expressions)

        # Find entry bar in cache dates
        entry_str = entry_date if isinstance(entry_date, str) else str(entry_date)[:10]
        cache_date_strs = [str(d)[:10] for d in cache_dates]
        try:
            cache_entry_idx = cache_date_strs.index(entry_str)
        except ValueError:
            return None, f"SKIP {ticker} — entry date {entry_date} not in expr cache dates"

        # Slice forward window from cache
        cache_end = min(cache_entry_idx + actual_forward + 1, len(cache_data))
        fwd_matrix = cache_data[cache_entry_idx:cache_end].astype(np.float32)

        # Trim OHLCV arrays to match cache forward length
        actual_len = len(fwd_matrix)
        fwd_closes = fwd_closes[:actual_len]
        fwd_highs_arr = fwd_highs_arr[:actual_len]
        fwd_lows_arr = fwd_lows_arr[:actual_len]
        actual_forward = actual_len - 1  # -1 because includes entry bar

        # Forward dates for exit date resolution
        fwd_dates = [str(d)[:10] for d in df["date"].values[entry_idx:entry_idx + actual_len]]

        if actual_forward < 5:
            return None, f"SKIP {ticker} — only {actual_forward} forward bars in cache"

        return {
            "id": example_id,
            "ticker": ticker,
            "entry_date": entry_date,
            "entry_idx": entry_idx,
            "entry_high": entry_high,
            "n_forward": actual_forward,
            "mfe_pct": mfe_pct,
            "direction": direction,
            "fwd_matrix": fwd_matrix,
            "fwd_closes": fwd_closes,
            "fwd_highs": fwd_highs_arr,
            "fwd_lows": fwd_lows_arr,
            "adr_at_entry": adr_val,
            "fwd_dates": fwd_dates,
        }, None

    except Exception as e:
        return None, f"ERROR {ticker}: {e}"


def load_all_examples(raw_examples, direction, max_forward, universe_cache, workers):
    """Load all examples in parallel — expr cache + OHLCV."""
    expr_cache_dir = os.path.join(CACHE_DIR, "expr_series")

    tasks = []
    for raw in raw_examples:
        ticker = raw["ticker"]
        ohlcv_df = universe_cache.get(ticker)
        if ohlcv_df is None:
            print(f"  SKIP {ticker} — not in 5yr OHLCV cache")
            continue
        ohlcv_df = ohlcv_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(ohlcv_df["date"]):
            ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])
        records = ohlcv_df.to_dict("records")
        tasks.append((ticker, raw["entryDate"], raw["id"], direction, max_forward,
                       records, expr_cache_dir))

    print(f"\nLoading {len(tasks)} examples from expr cache ({workers} workers)...")
    t0 = time.time()

    examples = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_load_one_example, task): task[0] for task in tasks}
        done = 0
        for future in as_completed(futures):
            ticker = futures[future]
            done += 1
            result, err = future.result()
            if err:
                print(f"  [{done}/{len(tasks)}] {err}")
            elif result:
                ex = ExampleData(**result)
                examples.append(ex)
                print(f"  [{done}/{len(tasks)}] {ticker:8s} OK — "
                      f"{ex.n_forward} fwd bars, MFE={ex.mfe_pct:+.2f}%, "
                      f"matrix {ex.fwd_matrix.shape}")

    elapsed = time.time() - t0
    print(f"\n{len(examples)} examples loaded in {elapsed:.1f}s")
    return examples


# ============================================================
# Move Arrays — precompute pct/adr/eff for scoring
# ============================================================

def build_move_arrays(examples, direction, min_bar):
    """Precompute pct_move, adr_captured, capture_eff arrays for all examples x bars."""
    n_examples = len(examples)
    max_bars = max(ex.n_forward for ex in examples) + 1

    pct_arr = np.full((n_examples, max_bars), np.nan)
    adr_arr = np.full((n_examples, max_bars), np.nan)
    eff_arr = np.full((n_examples, max_bars), np.nan)

    for i, ex in enumerate(examples):
        entry_high = ex.entry_high
        adr_val = ex.adr_at_entry

        for bar in range(min_bar, len(ex.fwd_closes)):
            exit_close = ex.fwd_closes[bar]
            if direction == "short":
                raw_move = entry_high - exit_close
                pct_move = raw_move / entry_high * 100
            else:
                entry_low = ex.fwd_lows[0]
                raw_move = exit_close - entry_low
                pct_move = raw_move / entry_low * 100

            pct_arr[i, bar] = pct_move
            adr_arr[i, bar] = raw_move / adr_val
            eff_arr[i, bar] = pct_move / ex.mfe_pct if ex.mfe_pct > 0 else 0.0

    return pct_arr, adr_arr, eff_arr


# ============================================================
# Grinder — parallel threshold testing
# ============================================================

def _grind_chunk(args):
    """Worker: grind a chunk of expression columns. Returns list of result dicts."""
    (col_indices, col_names, fwd_matrices, n_forwards, n_examples,
     n_thresholds, min_bar, pct_arr, adr_arr, eff_arr) = args

    import numpy as np

    results = []

    for ci, col_idx in enumerate(col_indices):
        expr_name = col_names[ci]

        # Gather this expression's forward series across all examples
        example_series = []
        all_values = []
        for ex_i in range(n_examples):
            mat = fwd_matrices[ex_i]
            if col_idx >= mat.shape[1]:
                continue
            series = mat[:, col_idx].astype(np.float64)
            # Check if this expression has any valid values in forward window
            valid = ~np.isnan(series[min_bar:])
            if valid.any():
                example_series.append((ex_i, series))
                vals = series[min_bar:][valid]
                all_values.append(vals)

        # Must have data for ALL examples
        if len(example_series) < n_examples or not all_values:
            continue

        combined = np.concatenate(all_values)
        if len(combined) < 5:
            continue
        thresholds = np.unique(np.percentile(combined, np.linspace(5, 95, n_thresholds)))

        for thresh in thresholds:
            for dir_test in ["above", "below"]:
                exit_bars = np.full(n_examples, -1, dtype=np.int32)
                triggered = 0

                for ex_idx, series in example_series:
                    for bar in range(min_bar, len(series)):
                        val = series[bar]
                        if np.isnan(val):
                            continue
                        hit = (val > thresh) if dir_test == "above" else (val < thresh)
                        if hit:
                            exit_bars[ex_idx] = bar
                            triggered += 1
                            break

                if triggered < n_examples:
                    continue

                valid_mask = exit_bars >= 0
                bars_valid = exit_bars[valid_mask]
                pct_valid = pct_arr[valid_mask, bars_valid]
                adr_valid = adr_arr[valid_mask, bars_valid]
                eff_valid = eff_arr[valid_mask, bars_valid]

                ok = ~(np.isnan(pct_valid) | np.isnan(eff_valid))
                if ok.sum() < n_examples:
                    continue

                results.append({
                    "expr_name": expr_name,
                    "direction": dir_test,
                    "threshold": round(float(thresh), 6),
                    "exit_bars": exit_bars.tolist(),
                    "adr_captured": adr_valid[ok].tolist(),
                    "capture_effs": eff_valid[ok].tolist(),
                    "examples_triggered": int(triggered),
                    "floor_pct_move": float(np.min(pct_valid[ok])),
                    "median_pct_move": float(np.median(pct_valid[ok])),
                    "avg_pct_move": float(np.mean(pct_valid[ok])),
                    "floor_capture_eff": float(np.min(eff_valid[ok])),
                    "median_capture_eff": float(np.median(eff_valid[ok])),
                    "avg_bars_to_exit": float(np.mean(bars_valid[ok])),
                    "_pct_full": [float(pct_arr[i, exit_bars[i]]) if exit_bars[i] >= 0 else float('nan')
                                  for i in range(n_examples)],
                    "_adr_full": [float(adr_arr[i, exit_bars[i]]) if exit_bars[i] >= 0 else float('nan')
                                  for i in range(n_examples)],
                    "_eff_full": [float(eff_arr[i, exit_bars[i]]) if exit_bars[i] >= 0 else float('nan')
                                  for i in range(n_examples)],
                })

    return results


def grind_exits(examples, expr_names, expr_col_map=None, direction="short", n_thresholds=20,
                min_bar=1, top_n=50, workers=None):
    """Grind all expressions x thresholds x directions. Parallel across CPU cores."""
    import math
    workers = workers or (os.cpu_count() or 8)
    n_examples = len(examples)
    n_exprs = len(expr_names)

    # If no col_map, assume 1:1 mapping (expr_names[i] = column i)
    if expr_col_map is None:
        expr_col_map = list(range(n_exprs))

    print(f"\nPre-computing move arrays ({n_examples} examples)...")
    t0 = time.time()
    pct_arr, adr_arr, eff_arr = build_move_arrays(examples, direction, min_bar)
    print(f"  Done in {time.time()-t0:.1f}s")

    # Collect forward matrices and n_forwards for workers
    fwd_matrices = [ex.fwd_matrix for ex in examples]
    n_forwards = [ex.n_forward for ex in examples]

    print(f"\nGrinding {n_exprs:,} expressions x ~{n_thresholds} thresholds x 2 directions ({workers} workers)...")
    print(f"Requirement: ALL {n_examples} examples must trigger (100%, no exceptions)")

    # Split into chunks using actual cache column indices
    chunk_size = max(1, math.ceil(n_exprs / workers))
    chunks = []
    for i in range(0, n_exprs, chunk_size):
        # Use actual cache column indices, not sequential indices
        cache_col_chunk = expr_col_map[i:i+chunk_size]
        name_chunk = expr_names[i:i+chunk_size]
        chunks.append((cache_col_chunk, name_chunk))

    tasks = [(idx_chunk, name_chunk, fwd_matrices, n_forwards, n_examples,
              n_thresholds, min_bar, pct_arr, adr_arr, eff_arr)
             for idx_chunk, name_chunk in chunks]

    t0 = time.time()
    all_raw = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_grind_chunk, task): i for i, task in enumerate(tasks)}
        done = 0
        for future in as_completed(futures):
            chunk_results = future.result()
            all_raw.extend(chunk_results)
            done += 1
            if done % 4 == 0 or done == len(chunks):
                elapsed = time.time() - t0
                print(f"  [{done}/{len(chunks)} chunks done, {len(all_raw)} candidates, {elapsed:.0f}s]")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s — {len(all_raw):,} candidates passed 100% filter")

    # Filter: minimum floor capture efficiency (drop conditions where worst example is terrible)
    MIN_FLOOR = 0.15
    before = len(all_raw)
    all_raw = [r for r in all_raw if r["floor_capture_eff"] >= MIN_FLOOR]
    if before > len(all_raw):
        print(f"  Filtered {before - len(all_raw)} candidates below {MIN_FLOOR:.0%} floor")

    # Sort: MEDIAN capture primary, floor secondary
    all_raw.sort(key=lambda r: (r["median_capture_eff"], r["floor_capture_eff"]), reverse=True)
    return all_raw[:top_n]


# ============================================================
# Reporting
# ============================================================

def print_results(candidates, examples, top_n=30):
    if not candidates:
        print("\nNo exit conditions found matching criteria.")
        return

    n_show = min(top_n, len(candidates))
    print(f"\n{'='*120}")
    print(f"TOP {n_show} EXIT CONDITIONS — ranked by floor capture efficiency")
    print(f"{'='*120}")

    for rank, c in enumerate(candidates[:n_show], 1):
        print(f"\n{'─'*120}")
        print(f"#{rank:3d}  {c['expr_name']} {c['direction']} {c['threshold']:.4f}")
        print(f"      Triggered: {c['examples_triggered']}/{len(examples)}  |  "
              f"Avg bars: {c['avg_bars_to_exit']:.1f}  |  "
              f"Floor capture eff: {c['floor_capture_eff']:.2f}  |  "
              f"Median capture eff: {c['median_capture_eff']:.2f}")
        print(f"      % Move  ->  floor: {c['floor_pct_move']:+.2f}%  |  "
              f"median: {c['median_pct_move']:+.2f}%  |  "
              f"avg: {c['avg_pct_move']:+.2f}%")

        print(f"      {'Ticker':8s} {'Entry Date':12s} {'Bar#':>5s} "
              f"{'% Move':>8s} {'ADR Capt':>9s} {'Capt Eff':>9s}")
        for i, ex in enumerate(examples):
            bar = c["exit_bars"][i]
            if bar < 0:
                print(f"      {ex.ticker:8s} {ex.entry_date:12s}   ---   (not triggered)")
                continue
            pct = c["_pct_full"][i]
            adr = c["_adr_full"][i]
            eff = c["_eff_full"][i]
            print(f"      {ex.ticker:8s} {ex.entry_date:12s} {bar:5d} "
                  f"{pct:+7.2f}% {adr:+8.2f} ADR {eff:8.2f}")


def print_mfe_summary(examples):
    print(f"\n{'='*80}")
    print(f"MFE SUMMARY")
    print(f"{'='*80}")
    print(f"{'Ticker':8s} {'Entry Date':12s} {'Entry High':>11s} {'MFE %':>8s} {'Fwd Bars':>9s}")
    for ex in examples:
        print(f"{ex.ticker:8s} {ex.entry_date:12s} ${ex.entry_high:10.2f} "
              f"{ex.mfe_pct:+7.2f}% {ex.n_forward:9d}")
    mfes = [ex.mfe_pct for ex in examples]
    print(f"\n  Floor MFE:  {min(mfes):+.2f}%")
    print(f"  Median MFE: {np.median(mfes):+.2f}%")
    print(f"  Avg MFE:    {np.mean(mfes):+.2f}%")


def save_results(candidates, examples, setup_type, args, expr_names):
    """Save results to JSON — timestamped archive + latest + Railway upload."""
    from datetime import datetime

    os.makedirs("data/profit_grind", exist_ok=True)

    best = candidates[0] if candidates else None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_ex = len(examples)
    floor_adr = f"{min(best['adr_captured']):.1f}adr" if best else "na"

    # Build per-example exit dates from best candidate's exit bars
    exit_dates_per_example = {}
    if best:
        for i, ex in enumerate(examples):
            bar = best["exit_bars"][i]
            if bar >= 0 and bar < len(ex.fwd_dates):
                exit_dates_per_example[f"{ex.ticker}|{ex.entry_date}"] = ex.fwd_dates[bar]

    nan_fix = lambda x: None if isinstance(x, float) and np.isnan(x) else x

    data = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "direction": args.direction,
        "max_forward": args.max_forward,
        "n_thresholds": args.n_thresholds,
        "n_examples": n_ex,
        "n_expressions_tested": len(expr_names),
        "computation_source": "expression_cache",
        "examples": [
            {"ticker": ex.ticker, "entry_date": ex.entry_date,
             "entry_high": ex.entry_high, "mfe_pct": ex.mfe_pct}
            for ex in examples
        ],
        "exit_dates": exit_dates_per_example,
        "results": [
            {
                "rank": i + 1,
                "expr_name": c["expr_name"],
                "direction": c["direction"],
                "threshold": c["threshold"],
                "examples_triggered": c["examples_triggered"],
                "floor_pct_move": c["floor_pct_move"],
                "median_pct_move": c["median_pct_move"],
                "avg_pct_move": c["avg_pct_move"],
                "floor_capture_eff": c["floor_capture_eff"],
                "median_capture_eff": c["median_capture_eff"],
                "avg_bars_to_exit": c["avg_bars_to_exit"],
                "exit_bars": c["exit_bars"],
                "pct_moves": c["_pct_full"],
                "adr_captured": c["_adr_full"],
                "capture_effs": c["_eff_full"],
            }
            for i, c in enumerate(candidates)
        ],
    }

    # Timestamped archive
    ts_path = f"data/profit_grind/profit_{setup_type}_{n_ex}ex_{floor_adr}_{ts}.json"
    with open(ts_path, "w") as f:
        json.dump(data, f, indent=2, default=nan_fix)
    print(f"\n  Saved: {ts_path}")

    # Latest (overwritten each run)
    latest_path = f"data/profit_grind/profit_{setup_type}.json"
    with open(latest_path, "w") as f:
        json.dump(data, f, indent=2, default=nan_fix)
    print(f"  Saved as latest: {latest_path}")

    from file_mirror import mirror_file
    mirror_file(ts_path)
    mirror_file(latest_path)

    # Upload to Railway
    try:
        r = requests.post(
            f"{RAILWAY_URL}/api/profit-grind/{setup_type}/upload",
            json=data, timeout=60
        )
        if r.status_code == 200:
            print(f"  Uploaded to Railway OK")
        else:
            print(f"  WARNING: Railway upload failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  WARNING: Railway upload error: {e}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Profit Grinder — Step 4a (expr cache)")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--n-thresholds", type=int, default=50)
    parser.add_argument("--min-bar", type=int, default=1)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--direction", default="short")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    print(f"Profit Grinder — Step 4a (expression cache)")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Thresholds: {args.n_thresholds}")
    print(f"Min bar: {args.min_bar}, 100% example pass required")
    print(f"Workers: {args.workers}")

    # 1. Load expression cache manifest for expression names
    from local_runner.expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("ERROR: Expression cache not valid. Run expr_cache_builder.py --build first.")
        sys.exit(1)

    all_expr_names = expr_cache.expr_names

    # Filter out boolean aggregations — monotonically increasing during trends,
    # structurally wrong for exit detection (they fire early, not at move exhaustion)
    BOOL_PREFIXES = ("ct_", "st_", "tir_")
    # Build mapping: filtered index -> actual cache column index
    expr_names = []
    expr_col_map = []  # expr_col_map[i] = actual column index in .npz for expr_names[i]
    for col_i, name in enumerate(all_expr_names):
        if not name.startswith(BOOL_PREFIXES):
            expr_names.append(name)
            expr_col_map.append(col_i)

    n_excluded = len(all_expr_names) - len(expr_names)
    print(f"\nExpression cache: {len(all_expr_names)} total, "
          f"{n_excluded} boolean aggregations excluded, "
          f"{len(expr_names)} expressions for exit grind")
    print(f"  {len(expr_cache.get_available_tickers())} tickers")

    # 2. Load examples from Railway
    raw_examples = load_examples(args.setup)

    # 3. Load 5yr OHLCV cache
    universe_cache = load_5yr_cache()

    # 4. Load all examples (parallel — expr cache + OHLCV)
    examples = load_all_examples(
        raw_examples, args.direction, args.max_forward, universe_cache, args.workers
    )

    if len(examples) < 3:
        print(f"\nOnly {len(examples)} examples — need at least 3. Aborting.")
        sys.exit(1)

    print_mfe_summary(examples)

    # 5. Verify expression count matches cache (compare against total cache cols, not filtered)
    expected_cols = len(all_expr_names)
    for ex in examples:
        if ex.fwd_matrix.shape[1] != expected_cols:
            print(f"WARNING: {ex.ticker} has {ex.fwd_matrix.shape[1]} cols, expected {expected_cols}")

    # 6. Grind
    candidates = grind_exits(
        examples, expr_names,
        expr_col_map=expr_col_map,
        direction=args.direction,
        n_thresholds=args.n_thresholds,
        min_bar=args.min_bar,
        top_n=args.top_n,
        workers=args.workers,
    )

    # 7. Report
    print_results(candidates, examples, top_n=3)

    # 8. Safety check
    if candidates:
        best = candidates[0]
        failed = [ex.ticker for i, ex in enumerate(examples) if best["exit_bars"][i] < 0]
        if failed:
            print(f"\n{'!'*80}")
            print(f"INTERNAL ERROR — best exit does NOT trigger on all examples!")
            print(f"  Failed: {failed}")
            print(f"{'!'*80}")
        else:
            print(f"\n  Validation passed: {len(examples)}/{len(examples)} examples trigger")

    # 9. Save
    save_results(candidates, examples, args.setup, args, expr_names)

    # 10. Summary
    if candidates:
        best = candidates[0]
        print(f"\n{'='*80}")
        print(f"BEST EXIT: {best['expr_name']} {best['direction']} {best['threshold']:.4f}")
        print(f"  Floor capture eff: {best['floor_capture_eff']:.2f}")
        print(f"  Median capture eff: {best['median_capture_eff']:.2f}")
        print(f"  Floor % move: {best['floor_pct_move']:+.2f}%")
        print(f"  Median % move: {best['median_pct_move']:+.2f}%")
        print(f"  Avg bars to exit: {best['avg_bars_to_exit']:.1f}")
        print(f"  Expressions tested: {len(expr_names):,}")
        print(f"{'='*80}")


if __name__ == "__main__":
    main()
