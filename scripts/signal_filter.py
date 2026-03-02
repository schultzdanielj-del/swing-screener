"""
Signal Filter — Deduplicate, apply exit, rank for vetting.

Scans all 5yr history for signal conditions, then:
  1. Deduplicates: consecutive signal bars for same ticker → keep rightmost
  2. Applies exit condition: run each signal forward, check if exit fires
  3. Measures exit distance: signal close → exit close (in ADR)
  4. Filters: keep only signals where exit distance ≥ example floor
  5. Ranks: sort by exit distance descending
  6. Outputs: ranked JSON for chart vetting + uploads to Railway

Also deduplicates examples the same way (one signal bar per example).

Usage:
    python scripts/signal_filter.py --setup dtss
    python scripts/signal_filter.py --setup dtss --min-adr 2.0
    python scripts/signal_filter.py --setup dtss --charts  # also generate charts
"""

import argparse
import os
import sys
import time
import json
import pickle
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.expression_engine import ExpressionEngine
from scripts.backtest_conditions import compute_series

# ============================================================
# Config
# ============================================================
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD = 120
DEFAULT_WORKERS = os.cpu_count() or 8

SETUP_CONFIGS = {
    "dtss": {"direction": "short", "examples_endpoint": "/api/examples/dtss"},
}


# ============================================================
# Data Loading
# ============================================================
def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    print(f"  Loading 5yr cache from {path}...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


def load_pyramid_conditions(setup_type):
    """Load signal conditions from pyramid results."""
    # Try multiple paths
    paths = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", f"pyramid_results_{setup_type}.json"),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                data = json.load(f)
            conditions = data.get("all_conditions", [])
            print(f"  Loaded {len(conditions)} conditions from {os.path.basename(p)}")
            return conditions
    raise FileNotFoundError(f"No pyramid results found for {setup_type}")


def load_exit_condition(setup_type):
    """Load best exit condition from exit grind results."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "exit_grind", f"exit_grind_{setup_type}.json")
    with open(path) as f:
        data = json.load(f)
    best = data["top_conditions"][0]
    print(f"  Exit condition: {best['expression']} {best['direction']} {best['threshold']}")
    return best


def load_examples(setup_type):
    """Load validated examples from Railway."""
    import requests
    try:
        r = requests.get(f"{RAILWAY_URL}/api/setups/{setup_type}/examples", timeout=30)
        r.raise_for_status()
        examples = r.json().get("examples", [])
        print(f"  Loaded {len(examples)} examples from Railway")
        return examples
    except Exception as e:
        print(f"  Warning: couldn't load examples from Railway: {e}")
        return []


# ============================================================
# Phase 1: Scan all signals (parallel)
# ============================================================
_worker_cache = None
_worker_conditions = None


def _init_scan_worker(cache, conditions):
    global _worker_cache, _worker_conditions
    _worker_cache = cache
    _worker_conditions = conditions


def _scan_batch(tickers):
    """Scan a batch of tickers. Returns list of {ticker, date, bar_idx, close}."""
    signals = []
    skipped = 0
    for ticker in tickers:
        df = _worker_cache.get(ticker)
        if df is None or len(df) < 100:
            skipped += 1
            continue
        try:
            engine = ExpressionEngine(df)
            n_bars = len(df)
            pass_mask = np.ones(n_bars, dtype=bool)
            pass_mask[:50] = False  # warmup

            for cond in _worker_conditions:
                series = compute_series(engine, cond["compute"])
                low, high = cond["low"], cond["high"]
                in_range = (series >= low) & (series <= high)
                in_range[np.isnan(series)] = False
                pass_mask &= in_range

            signal_indices = np.where(pass_mask)[0]
            if len(signal_indices) > 0:
                dates = df["date"].values
                closes = df["close"].values
                for idx in signal_indices:
                    signals.append({
                        "ticker": ticker,
                        "date": str(dates[idx])[:10],
                        "bar_idx": int(idx),
                        "close": float(closes[idx]),
                    })
        except Exception:
            skipped += 1
    return signals, skipped


def scan_all_signals(cache, conditions, workers):
    """Scan full universe for signal conditions."""
    tickers = list(cache.keys())
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    print(f"\n  Scanning {len(tickers):,} tickers × {len(conditions)} conditions...")
    print(f"  {workers} workers, {len(batches)} batches")
    t0 = time.time()

    all_signals = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_scan_worker,
        initargs=(cache, conditions)
    ) as pool:
        futures = [pool.submit(_scan_batch, batch) for batch in batches]
        done = 0
        for future in futures:
            batch_signals, _ = future.result()
            all_signals.extend(batch_signals)
            done += 1
            if done % max(len(batches) // 5, 1) == 0 or done == len(batches):
                elapsed = time.time() - t0
                pct = done / len(batches) * 100
                print(f"    {pct:.0f}% [{elapsed:.0f}s] {len(all_signals):,} signals")

    elapsed = time.time() - t0
    print(f"\n  ✓ {len(all_signals):,} raw signals in {elapsed:.0f}s")
    return all_signals


# ============================================================
# Phase 2: Deduplicate — consecutive bars → keep rightmost
# ============================================================
def deduplicate_signals(signals):
    """
    Group consecutive signal bars for the same ticker, keep only the rightmost
    (latest date) in each consecutive run.

    Rule: if there is ANY gap (even 1 bar where conditions didn't fire),
    it's a separate signal. Only truly back-to-back bars get collapsed.
    """
    # Sort by ticker then bar_idx
    signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))

    deduped = []
    i = 0
    while i < len(signals):
        current = signals[i]
        ticker = current["ticker"]

        # Walk forward through consecutive bars for same ticker
        j = i + 1
        while j < len(signals):
            nxt = signals[j]
            if nxt["ticker"] != ticker:
                break
            if nxt["bar_idx"] != signals[j - 1]["bar_idx"] + 1:
                break
            j += 1

        # signals[i:j] is a consecutive run — keep the rightmost (j-1)
        rightmost = signals[j - 1]
        cluster_size = j - i
        rightmost["cluster_size"] = cluster_size
        rightmost["cluster_start_date"] = signals[i]["date"]
        deduped.append(rightmost)

        i = j

    print(f"  ✓ Deduplicated: {len(signals):,} → {len(deduped):,} signals "
          f"({len(signals) - len(deduped):,} collapsed)")
    return deduped


# ============================================================
# Phase 3: Apply exit condition, measure distance
# ============================================================
def apply_exit_and_measure(signals, cache, exit_cond, direction, max_forward=MAX_FORWARD):
    """
    For each signal, run forward and check if exit condition fires.
    Measure signal close → exit close in ADR units.
    """
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]  # ">=" or "<="

    print(f"\n  Applying exit: {expr_name} {exit_dir} {exit_thresh}")
    print(f"  Direction: {direction}, max forward: {max_forward} bars")

    results = []
    no_exit = 0
    errors = 0

    for i, sig in enumerate(signals):
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        df = cache.get(ticker)

        if df is None or bar_idx >= len(df) - 1:
            errors += 1
            continue

        try:
            engine = ExpressionEngine(df)

            # Compute ADR at signal bar
            adr_series = engine._adr(14)
            adr_at_signal = float(adr_series.values[bar_idx])
            if adr_at_signal <= 0 or np.isnan(adr_at_signal):
                errors += 1
                continue

            signal_close = float(df["close"].values[bar_idx])
            n_available = len(df) - bar_idx - 1
            actual_forward = min(max_forward, n_available)

            if actual_forward < 5:
                errors += 1
                continue

            # Compute exit expression series
            exit_series = compute_series(engine, {"op": expr_name})

            # Find first bar after signal where exit fires
            exit_bar = None
            exit_close = None
            for fwd in range(1, actual_forward + 1):
                check_idx = bar_idx + fwd
                val = exit_series[check_idx]
                if np.isnan(val):
                    continue
                if exit_dir == ">=" and val >= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break
                elif exit_dir == "<=" and val <= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break

            if exit_bar is None:
                no_exit += 1
                continue

            # Measure distance: signal close → exit close in ADR
            if direction == "short":
                move_pct = (signal_close - exit_close) / signal_close * 100
                move_adr = (signal_close - exit_close) / adr_at_signal
            else:
                move_pct = (exit_close - signal_close) / signal_close * 100
                move_adr = (exit_close - signal_close) / adr_at_signal

            # Also compute MFE for reference
            fwd_slice = slice(bar_idx + 1, bar_idx + exit_bar + 1)
            if direction == "short":
                mfe_price = float(df["low"].values[fwd_slice].min())
                mfe_adr = (signal_close - mfe_price) / adr_at_signal
            else:
                mfe_price = float(df["high"].values[fwd_slice].max())
                mfe_adr = (mfe_price - signal_close) / adr_at_signal

            exit_date = str(df["date"].values[bar_idx + exit_bar])[:10]

            results.append({
                **sig,
                "signal_close": round(signal_close, 2),
                "adr_at_signal": round(adr_at_signal, 2),
                "exit_bar": exit_bar,
                "exit_date": exit_date,
                "exit_close": round(exit_close, 2),
                "move_pct": round(move_pct, 2),
                "move_adr": round(move_adr, 2),
                "mfe_adr": round(mfe_adr, 2),
                "capture_eff": round(move_adr / mfe_adr, 3) if mfe_adr > 0 else 0,
            })

        except Exception as e:
            errors += 1
            continue

        if (i + 1) % 50 == 0:
            print(f"    {i + 1}/{len(signals)} processed, {len(results)} with exit")

    print(f"\n  ✓ Exit applied: {len(results)} triggered, {no_exit} no exit, {errors} errors")
    return results


# ============================================================
# Phase 4: Filter by example floor + rank
# ============================================================
def exclude_existing_examples(signals, example_signals):
    """
    Remove signals that match existing examples.
    Match by ticker + signal bar within 5 bars of an example's signal bar.
    This ensures the vetting pile only shows NEW potential examples.
    """
    # Build lookup: ticker → set of signal bar indices from examples
    example_bars = {}
    for ex in example_signals:
        ticker = ex["ticker"]
        bar_idx = ex["signal_bar_idx"]
        if ticker not in example_bars:
            example_bars[ticker] = set()
        # Mark a window around the example signal bar
        for offset in range(-5, 6):
            example_bars[ticker].add(bar_idx + offset)

    before = len(signals)
    filtered = []
    for sig in signals:
        ticker = sig["ticker"]
        bar_idx = sig["bar_idx"]
        if ticker in example_bars and bar_idx in example_bars[ticker]:
            continue  # skip — this is an existing example
        filtered.append(sig)

    removed = before - len(filtered)
    if removed > 0:
        print(f"  ✓ Excluded {removed} signals matching existing examples")
    return filtered


def filter_and_rank(results, min_adr, direction):
    """Filter to signals above min_adr threshold, sort by move descending."""
    filtered = [r for r in results if r["move_adr"] >= min_adr]
    filtered.sort(key=lambda r: r["move_adr"], reverse=True)

    print(f"  ✓ Filtered: {len(results)} → {len(filtered)} signals (≥ {min_adr:.1f} ADR)")

    if filtered:
        moves = [r["move_adr"] for r in filtered]
        print(f"    Floor: {min(moves):.1f} ADR")
        print(f"    Median: {sorted(moves)[len(moves)//2]:.1f} ADR")
        print(f"    Max: {max(moves):.1f} ADR")
        print(f"    Avg exit bar: {sum(r['exit_bar'] for r in filtered)/len(filtered):.0f}")

    return filtered


# ============================================================
# Phase 5: Deduplicate examples (same logic)
# ============================================================
def deduplicate_examples(examples, cache, conditions):
    """
    For examples that have multiple signal bars, find and deduplicate them
    using the same logic as backtest signals.
    """
    results = []
    for ex in examples:
        ticker = ex.get("ticker")
        entry_date = ex.get("entryDate", ex.get("entry_date"))
        df = cache.get(ticker)
        if df is None:
            print(f"    {ticker}: not in cache, skipping")
            continue

        # Find entry bar
        dates_str = [str(d)[:10] for d in df["date"].values]
        if entry_date not in dates_str:
            print(f"    {ticker}: entry date {entry_date} not found")
            continue

        entry_idx = dates_str.index(entry_date)
        scan_idx = entry_idx - 1  # scan candle is day before entry

        # Scan backwards from scan candle to find the signal cluster
        engine = ExpressionEngine(df)
        signal_bars = []

        for check_idx in range(scan_idx, max(49, scan_idx - 30), -1):
            passes_all = True
            for cond in conditions:
                series = compute_series(engine, cond["compute"])
                val = series[check_idx]
                if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                    passes_all = False
                    break
            if passes_all:
                signal_bars.append(check_idx)
            else:
                break  # First non-passing bar = end of consecutive cluster

        if not signal_bars:
            print(f"    {ticker}: no signal bars found near entry")
            continue

        # Rightmost bar in the cluster (closest to entry)
        rightmost_idx = min(signal_bars)  # min because we scanned backwards
        # Actually, signal_bars are in reverse order, rightmost = first found
        rightmost_idx = signal_bars[0]  # scan_idx or close to it

        signal_date = dates_str[rightmost_idx]
        signal_close = float(df["close"].values[rightmost_idx])

        results.append({
            "id": ex.get("id"),
            "ticker": ticker,
            "entry_date": entry_date,
            "signal_date": signal_date,
            "signal_bar_idx": rightmost_idx,
            "signal_close": round(signal_close, 2),
            "cluster_size": len(signal_bars),
            "is_example": True,
        })
        print(f"    {ticker}: signal {signal_date} ({len(signal_bars)} bar cluster) → entry {entry_date}")

    print(f"  ✓ {len(results)}/{len(examples)} examples matched with signal bars")
    return results


# ============================================================
# Output
# ============================================================
def save_results(filtered, example_signals, setup_type, args):
    """Save ranked results for chart vetting."""
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "signal_filter")
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "exit_condition": args.get("exit_expr", ""),
        "min_adr_threshold": args.get("min_adr", 0),
        "n_raw_signals": args.get("n_raw", 0),
        "n_deduped": args.get("n_deduped", 0),
        "n_with_exit": args.get("n_with_exit", 0),
        "n_filtered": len(filtered),
        "n_examples_matched": len(example_signals),
        "example_signals": example_signals,
        "signals": filtered,
    }

    # Timestamped
    ts_path = os.path.join(out_dir, f"filtered_{setup_type}_{len(filtered)}sig_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {ts_path}")

    # Latest
    latest_path = os.path.join(out_dir, f"filtered_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Saved: {latest_path}")

    return latest_path


def measure_example_exit_distances(example_signals, cache, exit_cond, direction, max_forward=MAX_FORWARD):
    """
    For each deduplicated example signal bar, measure signal close → exit close in ADR.
    Same measurement as backtest signals so the floor is comparable.
    """
    expr_name = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]

    print(f"  Measuring example exit distances from deduplicated signal bars...")

    for ex in example_signals:
        ticker = ex["ticker"]
        bar_idx = ex["signal_bar_idx"]
        df = cache.get(ticker)

        if df is None or bar_idx >= len(df) - 1:
            ex["move_adr"] = None
            ex["error"] = "no data"
            continue

        try:
            engine = ExpressionEngine(df)
            adr_series = engine._adr(14)
            adr_at_signal = float(adr_series.values[bar_idx])
            signal_close = float(df["close"].values[bar_idx])

            if adr_at_signal <= 0 or np.isnan(adr_at_signal):
                ex["move_adr"] = None
                ex["error"] = "bad ADR"
                continue

            n_available = len(df) - bar_idx - 1
            actual_forward = min(max_forward, n_available)

            # Compute exit series
            exit_series = compute_series(engine, {"op": expr_name})

            # Find first exit bar after signal
            exit_bar = None
            exit_close = None
            for fwd in range(1, actual_forward + 1):
                check_idx = bar_idx + fwd
                val = exit_series[check_idx]
                if np.isnan(val):
                    continue
                if exit_dir == ">=" and val >= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break
                elif exit_dir == "<=" and val <= exit_thresh:
                    exit_bar = fwd
                    exit_close = float(df["close"].values[check_idx])
                    break

            if exit_bar is None:
                ex["move_adr"] = None
                ex["exit_bar"] = None
                ex["error"] = "no exit triggered"
                print(f"    {ticker}: NO EXIT within {actual_forward} bars")
                continue

            if direction == "short":
                move_adr = (signal_close - exit_close) / adr_at_signal
            else:
                move_adr = (exit_close - signal_close) / adr_at_signal

            exit_date = str(df["date"].values[bar_idx + exit_bar])[:10]

            ex["adr_at_signal"] = round(adr_at_signal, 2)
            ex["exit_bar"] = exit_bar
            ex["exit_date"] = exit_date
            ex["exit_close"] = round(exit_close, 2)
            ex["move_adr"] = round(move_adr, 2)

            print(f"    {ticker}: signal {ex['signal_date']} → exit {exit_date} "
                  f"= {move_adr:.1f} ADR ({exit_bar} bars)")

        except Exception as e:
            ex["move_adr"] = None
            ex["error"] = str(e)

    # Compute floor from examples that had valid exits
    valid_adrs = [ex["move_adr"] for ex in example_signals if ex.get("move_adr") is not None]
    if valid_adrs:
        floor = min(valid_adrs)
        median = sorted(valid_adrs)[len(valid_adrs) // 2]
        print(f"\n  ✓ Example exit distances (from deduped signal bars):")
        print(f"    {len(valid_adrs)}/{len(example_signals)} examples had valid exits")
        print(f"    Floor: {floor:.1f} ADR")
        print(f"    Median: {median:.1f} ADR")
        print(f"    Mean: {sum(valid_adrs)/len(valid_adrs):.1f} ADR")
        return floor
    else:
        print(f"  ⚠ No examples had valid exit distances!")
        return 0.0


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Signal Filter — Dedup + Exit + Rank")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--min-adr", type=float, default=None,
                        help="Min exit distance in ADR (default: derived from examples)")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    setup = args.setup
    config = SETUP_CONFIGS.get(setup, {"direction": "short"})
    direction = config["direction"]

    print(f"\n{'='*60}")
    print(f"  SIGNAL FILTER — {setup.upper()}")
    print(f"{'='*60}")
    t0 = time.time()

    # Load data
    cache = load_5yr_cache()
    conditions = load_pyramid_conditions(setup)
    exit_cond = load_exit_condition(setup)
    examples = load_examples(setup)

    # Phase 1: Deduplicate examples FIRST — need their exit distances for the floor
    print(f"\n  PHASE 1: Deduplicate example signal bars")
    example_signals = deduplicate_examples(examples, cache, conditions)

    # Phase 2: Measure example exit distances from deduplicated signal bars
    print(f"\n  PHASE 2: Measure example exit distances")
    example_floor = measure_example_exit_distances(
        example_signals, cache, exit_cond, direction, args.max_forward)

    min_adr = args.min_adr if args.min_adr is not None else example_floor
    print(f"\n  ADR floor from examples: {example_floor:.1f}")
    print(f"  Using filter threshold: {min_adr:.1f} ADR")

    # Phase 3: Scan all backtest signals
    print(f"\n  PHASE 3: Scan all signals")
    raw_signals = scan_all_signals(cache, conditions, args.workers)

    # Phase 4: Deduplicate backtest signals
    print(f"\n  PHASE 4: Deduplicate (consecutive → rightmost)")
    deduped = deduplicate_signals(raw_signals)

    # Phase 5: Apply exit + measure
    print(f"\n  PHASE 5: Apply exit condition + measure distance")
    with_exit = apply_exit_and_measure(deduped, cache, exit_cond, direction, args.max_forward)

    # Phase 6: Exclude existing examples
    print(f"\n  PHASE 6: Exclude existing examples")
    new_signals = exclude_existing_examples(with_exit, example_signals)

    # Phase 7: Filter + rank
    print(f"\n  PHASE 7: Filter + rank (≥ {min_adr:.1f} ADR)")
    filtered = filter_and_rank(new_signals, min_adr, direction)

    # Save
    print(f"\n  SAVING RESULTS")
    save_results(filtered, example_signals, setup, {
        "exit_expr": f"{exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}",
        "min_adr": min_adr,
        "example_floor_adr": example_floor,
        "n_raw": len(raw_signals),
        "n_deduped": len(deduped),
        "n_with_exit": len(with_exit),
    })

    total_time = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  DONE in {total_time:.0f}s")
    print(f"  Examples: {len(example_signals)} deduped, floor {example_floor:.1f} ADR")
    print(f"  Signals: {len(raw_signals):,} raw → {len(deduped):,} deduped → "
          f"{len(with_exit):,} with exit → {len(filtered):,} filtered")
    print(f"  Ready for chart vetting in data/signal_filter/")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
