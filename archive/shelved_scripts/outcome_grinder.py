"""
Outcome Grinder — Step 7 of ANALYSIS_SYSTEM.md

Phase 1: Apply Step 6 exit condition to all pyramid signals.
         Measures signal bar close → exit bar close.
         Two requirements for OUTCOME classification:
         1. Exit condition triggers within max_forward bars of signal
         2. Move at exit is >= min_adr ADRs in the setup direction

Input:
    - Pyramid grinder results JSON (contains all signals + example signal bars)
    - Exit grinder results JSON (contains ranked exit conditions)
    - Local 5yr OHLCV cache (universe_ohlcv_5yr.pkl)

Output:
    - data/outcome_grind/outcome_signals_{setup}.json

Usage:
    # Auto-finds signal/exit from grinds/ storage:
    python scripts/outcome_grinder.py --setup dtss

    # Override specific inputs:
    python scripts/outcome_grinder.py --setup dtss \
        --pyramid path/to/signal.json \
        --exit-grind path/to/exit.json

    # Adjust parameters:
    python scripts/outcome_grinder.py --setup dtss \
        --min-adr 1.5 --max-forward 120 --exit-rank 1
"""

import argparse
import sys
import os
import time
import json
import pickle
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.expression_engine import ExpressionEngine

# ============================================================
# Config
# ============================================================
MAX_FORWARD_DEFAULT = 120
MIN_ADR_DEFAULT = 1.0
DEFAULT_WORKERS = os.cpu_count() or 8


# ============================================================
# Data Loading — all local, no API calls
# ============================================================

def load_pyramid(path: str) -> dict:
    """Load pyramid grinder results JSON."""
    with open(path) as f:
        data = json.load(f)
    # Get final signals from last tier
    tier_order = ["5yr", "1yr", "6mo", "1mo", "1wk", "D1"]
    signals = None
    for tier in tier_order:
        if tier in data.get("tier_results", {}):
            tr = data["tier_results"][tier]
            if "final_signals" in tr:
                signals = tr["final_signals"]
                break
    if signals is None:
        raise ValueError("No final_signals found in pyramid results")

    example_sigs = data.get("example_signals", [])
    print(f"Loaded pyramid: {len(signals)} signals, {len(example_sigs)} example signal bars")
    print(f"  Conditions: {data['n_conditions']}, peak={data['summary']['final_peak']}, "
          f"avg={data['summary']['final_avg']}")
    return {
        "signals": signals,
        "example_signals": example_sigs,
        "conditions": data.get("all_conditions", []),
        "metadata": data,
    }


def load_exit_grind(path: str, rank: int = 1) -> dict:
    """Load exit grinder results and select exit condition by rank."""
    with open(path) as f:
        data = json.load(f)

    results = data.get("results", data.get("top_conditions", []))
    if not results:
        raise ValueError("No exit conditions found")

    if rank > len(results):
        raise ValueError(f"Requested rank {rank} but only {len(results)} conditions")

    ec = results[rank - 1]

    # Normalize field names
    expr_name = ec.get("expr_name", ec.get("expression", ""))
    direction = ec.get("direction", "below")
    threshold = ec.get("threshold", 0)

    print(f"Exit condition #{rank}: {expr_name} {direction} {threshold}")
    print(f"  Median % move: {ec.get('median_pct_move', 'N/A')}")
    print(f"  Median capture eff: {ec.get('median_capture_eff', 'N/A')}")
    print(f"  Trigger rate: {ec.get('examples_triggered', '?')}/{data.get('n_examples', '?')}")

    return {
        "expr_name": expr_name,
        "direction": direction,
        "threshold": threshold,
        "raw": ec,
    }


def load_ohlcv_cache(path: str) -> dict:
    """Load local 5yr OHLCV cache. Returns {ticker: DataFrame}."""
    print(f"Loading OHLCV cache from {path}...")
    t0 = time.time()
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache)} tickers in {time.time()-t0:.1f}s")
    return cache


def _parse_pyramid(data: dict) -> dict:
    """Parse pyramid data that's already loaded as a dict."""
    tier_order = ["5yr", "1yr", "6mo", "1mo", "1wk", "D1"]
    signals = None
    for tier in tier_order:
        if tier in data.get("tier_results", {}):
            tr = data["tier_results"][tier]
            if "final_signals" in tr:
                signals = tr["final_signals"]
                break
    if signals is None:
        raise ValueError("No final_signals found in pyramid results")

    example_sigs = data.get("example_signals", [])
    print(f"Loaded pyramid: {len(signals)} signals, {len(example_sigs)} example signal bars")
    print(f"  Conditions: {data['n_conditions']}, peak={data['summary']['final_peak']}, "
          f"avg={data['summary']['final_avg']}")
    return {
        "signals": signals,
        "example_signals": example_sigs,
        "conditions": data.get("all_conditions", []),
        "metadata": data,
    }


def _parse_exit_grind(data: dict, rank: int = 1) -> dict:
    """Parse exit grind data that's already loaded as a dict."""
    results = data.get("results", data.get("top_conditions", []))
    if not results:
        raise ValueError("No exit conditions found")
    if rank > len(results):
        raise ValueError(f"Requested rank {rank} but only {len(results)} conditions")

    ec = results[rank - 1]
    expr_name = ec.get("expr_name", ec.get("expression", ""))
    direction = ec.get("direction", "below")
    threshold = ec.get("threshold", 0)

    print(f"Exit condition #{rank}: {expr_name} {direction} {threshold}")
    return {
        "expr_name": expr_name,
        "direction": direction,
        "threshold": threshold,
        "raw": ec,
    }


# ============================================================
# Exit Condition Evaluation
# ============================================================

def _compute_adx(high, low, close, period):
    """Compute ADX. Returns numpy array same length as input."""
    n = len(close)
    if n < period * 2:
        return np.full(n, np.nan)

    # True Range
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                     abs(high[i] - close[i-1]),
                     abs(low[i] - close[i-1]))

    # +DM, -DM
    pdm = np.zeros(n)
    ndm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i-1]
        down = low[i-1] - low[i]
        if up > down and up > 0:
            pdm[i] = up
        if down > up and down > 0:
            ndm[i] = down

    # Wilder smoothing
    alpha = 1.0 / period
    atr_arr = np.zeros(n)
    pdi_arr = np.zeros(n)
    ndi_arr = np.zeros(n)

    # Seed with SMA
    atr_arr[period] = np.mean(tr[1:period+1])
    pdi_arr[period] = np.mean(pdm[1:period+1])
    ndi_arr[period] = np.mean(ndm[1:period+1])

    for i in range(period + 1, n):
        atr_arr[i] = atr_arr[i-1] * (1 - alpha) + tr[i] * alpha
        pdi_arr[i] = pdi_arr[i-1] * (1 - alpha) + pdm[i] * alpha
        ndi_arr[i] = ndi_arr[i-1] * (1 - alpha) + ndm[i] * alpha

    # DI+ and DI-
    with np.errstate(divide='ignore', invalid='ignore'):
        di_p = np.where(atr_arr > 0, 100 * pdi_arr / atr_arr, 0)
        di_n = np.where(atr_arr > 0, 100 * ndi_arr / atr_arr, 0)

        # DX
        di_sum = di_p + di_n
        dx = np.where(di_sum > 0, 100 * np.abs(di_p - di_n) / di_sum, 0)

    # ADX = Wilder smooth of DX
    adx_arr = np.full(n, np.nan)
    start = period * 2
    if start < n:
        adx_arr[start] = np.mean(dx[period+1:start+1])
        for i in range(start + 1, n):
            adx_arr[i] = adx_arr[i-1] * (1 - alpha) + dx[i] * alpha

    return adx_arr


def _compute_exit_series(engine, expr_name):
    """Compute full-history exit expression series using ExpressionEngine.
    
    Parses expression names like 'adx_7_declining_count_true_10b' and computes
    using the same indicator functions as the exit grinder (ExpressionEngine).
    This ensures exact numerical parity across all grinders.
    """
    import re
    
    # Parse: {indicator}_{period}_{bool_condition}_{agg_type}_{window}b
    # Example: adx_7_declining_count_true_10b
    m = re.match(r'^(\w+?)_(\d+)_(\w+?)_(count_true|pct_true|since_true|true_in_row)_(\d+)b$', expr_name)
    if not m:
        raise ValueError(f"Cannot parse exit expression name: {expr_name}")
    
    indicator = m.group(1)    # adx
    period = int(m.group(2))  # 7
    bool_cond = m.group(3)    # declining
    agg_type = m.group(4)     # count_true
    agg_window = int(m.group(5))  # 10
    
    # Get indicator series from ExpressionEngine (same as exit grinder)
    if indicator == "adx":
        base_vals = engine._adx(period).values
    elif indicator == "rsi":
        base_vals = engine._rsi(period).values
    elif indicator == "cci":
        base_vals = engine._cci(period).values
    elif indicator == "stoch":
        base_vals = engine._stoch(period).values
    else:
        raise ValueError(f"Unsupported indicator in exit expression: {indicator}")
    
    # Compute boolean condition
    n = len(base_vals)
    bool_vals = np.zeros(n)
    if bool_cond == "declining":
        bool_vals[3:] = (base_vals[3:] < base_vals[:-3]).astype(float)
    elif bool_cond == "rising":
        bool_vals[3:] = (base_vals[3:] > base_vals[:-3]).astype(float)
    else:
        raise ValueError(f"Unsupported boolean condition: {bool_cond}")
    
    # Compute aggregation on full history
    result = np.full(n, np.nan)
    if agg_type == "count_true":
        cumsum = np.cumsum(bool_vals)
        result[agg_window-1:] = cumsum[agg_window-1:] - np.concatenate([[0], cumsum[:n-agg_window]])
    elif agg_type == "pct_true":
        cumsum = np.cumsum(bool_vals)
        result[agg_window-1:] = (cumsum[agg_window-1:] - np.concatenate([[0], cumsum[:n-agg_window]])) / agg_window
    elif agg_type == "since_true":
        bars = 0
        found = False
        for i in range(n):
            if bool_vals[i] > 0.5:
                found = True
                bars = 0
            elif found:
                bars += 1
            if found:
                result[i] = float(bars)
    elif agg_type == "true_in_row":
        streak = 0
        for i in range(n):
            if bool_vals[i] > 0.5:
                streak += 1
            else:
                streak = 0
            result[i] = float(streak)
    
    return result


def _eval_signal_batch(args):
    """Evaluate exit condition + ADR filter on a batch of signals for one ticker.

    Processes all signals for the same ticker in one shot to avoid
    redundant OHLCV lookups. Runs in subprocess.

    Returns list of result dicts.
    """
    ticker, sig_dates, is_example_flags, exit_cond, setup_direction, \
        max_forward, min_adr, min_mfe, ohlcv_data = args

    import numpy as np
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    results = []

    # Reconstruct DataFrame from passed arrays
    df = pd.DataFrame(ohlcv_data)
    df = df.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    dates_array = df["date"].values
    if len(dates_array) > 0 and hasattr(dates_array[0], 'strftime'):
        dates_str = np.array([str(d)[:10] for d in dates_array])
    elif len(dates_array) > 0 and isinstance(dates_array[0], str):
        dates_str = np.array([d[:10] for d in dates_array])
    else:
        dates_str = np.array([str(d)[:10] for d in dates_array])
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    close = df["close"].values.astype(np.float64)

    # Compute exit expression using ExpressionEngine (same as exit grinder)
    from scripts.expression_engine import ExpressionEngine
    engine = ExpressionEngine(df)
    exit_series = _compute_exit_series(engine, exit_cond["expr_name"])
    n = len(close)

    # ADR14 using ExpressionEngine (same as exit grinder)
    adr14 = engine._adr(14).values
    daily_range = high - low

    # Process each signal date
    for sig_date, is_example in zip(sig_dates, is_example_flags):
        # Find signal bar by date
        date_mask = dates_str == sig_date
        idx_matches = np.where(date_mask)[0]
        if len(idx_matches) == 0:
            results.append({
                "ticker": ticker, "date": sig_date,
                "is_example": is_example, "status": "date_not_found",
            })
            continue

        sig_idx = int(idx_matches[0])

        # All measurements from signal bar close
        sig_close = close[sig_idx]
        adr_at_signal = adr14[sig_idx]
        if np.isnan(adr_at_signal) or adr_at_signal <= 0:
            adr_at_signal = np.nanmean(daily_range[max(0, sig_idx-14):sig_idx])
            if np.isnan(adr_at_signal) or adr_at_signal <= 0:
                results.append({
                    "ticker": ticker, "date": sig_date,
                    "is_example": is_example, "status": "no_adr",
                })
                continue

        bars_available = n - sig_idx - 1
        if bars_available < 5:
            results.append({
                "ticker": ticker, "date": sig_date,
                "is_example": is_example, "status": "insufficient_bars",
                "bars_available": bars_available,
            })
            continue

        actual_forward = min(max_forward, bars_available)

        # === EXIT CONDITION: scan forward from signal bar + 1 ===
        exit_dir = exit_cond["direction"]
        threshold = exit_cond["threshold"]

        triggered = False
        trigger_bar_offset = -1
        trigger_abs_idx = -1

        for offset in range(1, actual_forward + 1):
            abs_idx = sig_idx + offset
            val = exit_series[abs_idx]
            if np.isnan(val):
                continue
            if exit_dir == "below" and val < threshold:
                triggered = True
                trigger_bar_offset = offset
                trigger_abs_idx = abs_idx
                break
            elif exit_dir == "above" and val > threshold:
                triggered = True
                trigger_bar_offset = offset
                trigger_abs_idx = abs_idx
                break
            elif exit_dir == "<=" and val <= threshold:
                triggered = True
                trigger_bar_offset = offset
                trigger_abs_idx = abs_idx
                break
            elif exit_dir == ">=" and val >= threshold:
                triggered = True
                trigger_bar_offset = offset
                trigger_abs_idx = abs_idx
                break

        if not triggered:
            end_idx = sig_idx + actual_forward
            end_close = close[end_idx]
            if setup_direction == "short":
                pct_move = (sig_close - end_close) / sig_close * 100
                adr_move = (sig_close - end_close) / adr_at_signal
            else:
                pct_move = (end_close - sig_close) / sig_close * 100
                adr_move = (end_close - sig_close) / adr_at_signal

            results.append({
                "ticker": ticker, "date": sig_date,
                "is_example": is_example, "status": "no_exit_trigger",
                "pct_move_at_end": round(float(pct_move), 2),
                "adr_move_at_end": round(float(adr_move), 2),
                "bars_available": actual_forward,
            })
            continue

        # Exit triggered — measure signal bar close to exit bar close
        exit_close = close[trigger_abs_idx]
        if setup_direction == "short":
            pct_move = (sig_close - exit_close) / sig_close * 100
            adr_move = (sig_close - exit_close) / adr_at_signal
        else:
            pct_move = (exit_close - sig_close) / sig_close * 100
            adr_move = (exit_close - sig_close) / adr_at_signal

        # Outcome = exit triggered + move >= adr_floor + MFE >= mfe_floor
        is_outcome = adr_move >= min_adr and mfe_adr >= min_mfe

        # MFE: signal bar to exit trigger bar
        fwd_slice = slice(sig_idx, trigger_abs_idx + 1)
        if setup_direction == "short":
            mfe_price = float(np.min(low[fwd_slice]))
            mfe_pct = (sig_close - mfe_price) / sig_close * 100
            mfe_adr = (sig_close - mfe_price) / adr_at_signal
        else:
            mfe_price = float(np.max(high[fwd_slice]))
            mfe_pct = (mfe_price - sig_close) / sig_close * 100
            mfe_adr = (mfe_price - sig_close) / adr_at_signal

        capture_eff = pct_move / mfe_pct if mfe_pct > 0 else 0.0

        status = "outcome" if is_outcome else "sub_adr"

        results.append({
            "ticker": ticker, "date": sig_date,
            "is_example": is_example,
            "status": status,
            "exit_triggered": True,
            "trigger_bar": trigger_bar_offset,
            "pct_move": round(float(pct_move), 2),
            "adr_move": round(float(adr_move), 2),
            "mfe_pct": round(float(mfe_pct), 2),
            "mfe_adr": round(float(mfe_adr), 2),
            "capture_eff": round(float(capture_eff), 4),
            "sig_close": round(float(sig_close), 2),
            "exit_close": round(float(exit_close), 2),
        })

    return results


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Outcome Grinder — Step 7 Phase 1")
    parser.add_argument("--pyramid", default=None, help="Path to pyramid/signal grinder JSON (default: grinds/{setup}/signal/latest.json)")
    parser.add_argument("--exit-grind", default=None, help="Path to exit grinder JSON (default: grinds/{setup}/exit/latest.json)")
    parser.add_argument("--cache", default=None, help="Path to 5yr OHLCV cache pkl (default: local_runner/cache/universe_ohlcv_5yr.pkl)")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--direction", default="short", help="Trade direction")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--min-adr", type=float, default=MIN_ADR_DEFAULT,
                        help="Minimum ADR move at exit to qualify as outcome")
    parser.add_argument("--exit-rank", type=int, default=1,
                        help="Which exit condition to use (1=best)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    print(f"Outcome Grinder — Step 7 Phase 1")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Min ADR: auto (from examples)")
    print(f"Workers: {args.workers}")
    print()

    # ── 1. Load all data locally ──
    # Resolve paths — load from local cache files directly
    # (GrindStorage was removed in V2; outcome_grinder is a V1 legacy script)

    # Signal grind (pyramid)
    if args.pyramid:
        pyramid = load_pyramid(args.pyramid)
    else:
        print("ERROR: --pyramid path required. GrindStorage was removed in V2.")
        print("  Use: --pyramid local_runner/cache/pyramid_dtss_*.json")
        sys.exit(1)

    # Exit grind
    if args.exit_grind:
        exit_cond = load_exit_grind(args.exit_grind, rank=args.exit_rank)
    else:
        print("ERROR: --exit-grind path required. GrindStorage was removed in V2.")
        print("  Use: --exit-grind data/profit_grind/profit_dtss.json")
        sys.exit(1)

    # OHLCV cache
    if args.cache:
        cache_path = args.cache
    else:
        cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "local_runner", "cache", "universe_ohlcv_5yr.pkl")
    ohlcv_cache = load_ohlcv_cache(cache_path)

    signals = pyramid["signals"]
    example_sigs = pyramid["example_signals"]

    # Build example lookup: (ticker, signal_date) -> True
    example_set = {(es["ticker"], es["date"]) for es in example_sigs}
    print(f"\nExample signal bars: {len(example_set)}")

    # ── 1b. Find earliest signal bar per example & compute ADR/MFE floors ──
    # For each example, look up to 10 trading days before entry for signal bars
    LOOKBACK_DAYS = 10
    
    # Build signal lookup: ticker -> list of signal dates
    from collections import defaultdict
    sig_by_ticker = defaultdict(set)
    for sig in signals:
        sig_by_ticker[sig["ticker"]].add(sig["date"])
    
    example_floors = []  # (ticker, entry_date, earliest_sig_date, adr_move, mfe_adr)
    
    for es in example_sigs:
        ticker = es["ticker"]
        entry_date = es.get("entry_date", "")
        sig_date = es["date"]  # the signal bar the pyramid found
        
        if ticker not in ohlcv_cache:
            continue
        
        df = ohlcv_cache[ticker]
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        
        dates = df["date"].values
        if len(dates) > 0 and hasattr(dates[0], 'strftime'):
            dates_str = np.array([str(d)[:10] for d in dates])
        elif len(dates) > 0 and isinstance(dates[0], str):
            dates_str = np.array([d[:10] for d in dates])
        else:
            dates_str = np.array([str(d)[:10] for d in dates])
        
        # Find entry bar index
        entry_matches = np.where(dates_str == entry_date[:10])[0] if entry_date else []
        sig_matches = np.where(dates_str == sig_date[:10])[0]
        
        if len(sig_matches) == 0:
            continue
        
        sig_idx = int(sig_matches[0])
        
        # If we have an entry date, find all signal bars within LOOKBACK_DAYS before it
        if len(entry_matches) > 0:
            entry_idx = int(entry_matches[0])
            lookback_start = max(0, entry_idx - LOOKBACK_DAYS)
            
            # Find earliest signal bar for this ticker in the lookback window
            earliest_sig_idx = None
            for i in range(lookback_start, entry_idx):
                if dates_str[i] in sig_by_ticker[ticker]:
                    if earliest_sig_idx is None:
                        earliest_sig_idx = i
            
            if earliest_sig_idx is None:
                earliest_sig_idx = sig_idx  # fallback to the known signal bar
        else:
            earliest_sig_idx = sig_idx
        
        earliest_sig_close = float(df["close"].iloc[earliest_sig_idx])
        earliest_sig_date = dates_str[earliest_sig_idx]
        
        # Compute exit trigger bar from earliest signal bar
        engine = ExpressionEngine(df)
        exit_series_vals = _compute_exit_series(engine, exit_cond["expr_name"])
        adr14_vals = engine._adr(14).values
        adr_at_sig = float(adr14_vals[earliest_sig_idx])
        
        if np.isnan(adr_at_sig) or adr_at_sig <= 0:
            continue
        
        exit_dir = exit_cond["direction"]
        threshold = exit_cond["threshold"]
        n = len(df)
        
        trigger_idx = None
        for offset in range(1, min(args.max_forward, n - earliest_sig_idx - 1) + 1):
            abs_idx = earliest_sig_idx + offset
            val = exit_series_vals[abs_idx]
            if np.isnan(val):
                continue
            hit = False
            if exit_dir == "below" and val < threshold:
                hit = True
            elif exit_dir == "above" and val > threshold:
                hit = True
            elif exit_dir == "<=" and val <= threshold:
                hit = True
            elif exit_dir == ">=" and val >= threshold:
                hit = True
            if hit:
                trigger_idx = abs_idx
                break
        
        if trigger_idx is None:
            print(f"  WARNING: exit never triggers for example {ticker} {earliest_sig_date} (earliest sig bar)")
            continue
        
        exit_close = float(df["close"].iloc[trigger_idx])
        
        # ADR move: signal close to exit close
        if args.direction == "short":
            adr_move = (earliest_sig_close - exit_close) / adr_at_sig
            # MFE: lowest low between signal and exit
            mfe_price = float(df["low"].iloc[earliest_sig_idx:trigger_idx + 1].min())
            mfe_adr = (earliest_sig_close - mfe_price) / adr_at_sig
        else:
            adr_move = (exit_close - earliest_sig_close) / adr_at_sig
            mfe_price = float(df["high"].iloc[earliest_sig_idx:trigger_idx + 1].max())
            mfe_adr = (mfe_price - earliest_sig_close) / adr_at_sig
        
        example_floors.append({
            "ticker": ticker,
            "entry_date": entry_date,
            "earliest_sig_date": earliest_sig_date,
            "sig_close": earliest_sig_close,
            "exit_close": exit_close,
            "adr_move": round(adr_move, 2),
            "mfe_adr": round(mfe_adr, 2),
            "trigger_bar": trigger_idx - earliest_sig_idx,
        })
    
    if example_floors:
        adr_moves = [ef["adr_move"] for ef in example_floors]
        mfe_adrs = [ef["mfe_adr"] for ef in example_floors]
        
        # Floor = worst example with wiggle room (90% of worst)
        adr_floor = round(min(adr_moves) * 0.9, 2)
        mfe_floor = round(min(mfe_adrs) * 0.9, 2)
        
        print(f"\n  Example floor analysis (from earliest signal bar, {LOOKBACK_DAYS}-day lookback):")
        print(f"  {'Ticker':8s} {'Earliest Sig':12s} {'Entry':12s} {'ADR Move':>9s} {'MFE ADR':>8s} {'Bar#':>5s}")
        for ef in sorted(example_floors, key=lambda x: x["adr_move"]):
            print(f"  {ef['ticker']:8s} {ef['earliest_sig_date']:12s} {ef['entry_date'][:10]:12s} "
                  f"{ef['adr_move']:+8.2f} {ef['mfe_adr']:+7.2f} {ef['trigger_bar']:5d}")
        print(f"\n  Worst ADR move: {min(adr_moves):.2f}  →  ADR floor (90%): {adr_floor}")
        print(f"  Worst MFE ADR:  {min(mfe_adrs):.2f}  →  MFE floor (90%): {mfe_floor}")
    else:
        print("\n  WARNING: Could not compute example floors!")
        adr_floor = args.min_adr
        mfe_floor = 0.0

    # ── 2. Group signals by ticker for batch processing ──
    from collections import defaultdict
    ticker_groups = defaultdict(list)
    for sig in signals:
        ticker = sig["ticker"]
        date = sig["date"]
        is_example = (ticker, date) in example_set
        ticker_groups[ticker].append((date, is_example))

    # Check cache coverage
    missing_tickers = [t for t in ticker_groups if t not in ohlcv_cache]
    if missing_tickers:
        print(f"\nWARNING: {len(missing_tickers)} tickers not in OHLCV cache: {missing_tickers[:10]}")

    available_tickers = [t for t in ticker_groups if t in ohlcv_cache]
    n_signals_available = sum(len(ticker_groups[t]) for t in available_tickers)
    print(f"\nProcessing {n_signals_available} signals across {len(available_tickers)} tickers")

    # ── 3. Build tasks — one per ticker ──
    tasks = []
    for ticker in available_tickers:
        sigs = ticker_groups[ticker]
        sig_dates = [s[0] for s in sigs]
        is_example_flags = [s[1] for s in sigs]

        # Pass OHLCV as dict-of-lists for pickling
        df = ohlcv_cache[ticker]
        ohlcv_data = {
            "date": df["date"].tolist() if hasattr(df["date"], "tolist") else list(df["date"]),
            "open": df["open"].tolist() if hasattr(df["open"], "tolist") else list(df["open"]),
            "high": df["high"].tolist() if hasattr(df["high"], "tolist") else list(df["high"]),
            "low": df["low"].tolist() if hasattr(df["low"], "tolist") else list(df["low"]),
            "close": df["close"].tolist() if hasattr(df["close"], "tolist") else list(df["close"]),
            "volume": df["volume"].tolist() if hasattr(df["volume"], "tolist") else list(df["volume"]),
        }

        tasks.append((
            ticker, sig_dates, is_example_flags,
            {"expr_name": exit_cond["expr_name"], "direction": exit_cond["direction"], "threshold": exit_cond["threshold"]},
            args.direction, args.max_forward, adr_floor, mfe_floor, ohlcv_data,
        ))

    # ── 4. Run in parallel ──
    print(f"\nEvaluating {len(tasks)} ticker batches ({args.workers} workers)...")
    t0 = time.time()

    all_results = []
    done_tickers = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_eval_signal_batch, task): task[0] for task in tasks}
        for future in as_completed(futures):
            done_tickers += 1
            try:
                batch_results = future.result()
                all_results.extend(batch_results)
            except Exception as e:
                ticker = futures[future]
                print(f"  ERROR on {ticker}: {e}")

            if done_tickers % 50 == 0 or done_tickers == len(tasks):
                n_out = sum(1 for r in all_results if r["status"] == "outcome")
                n_sub = sum(1 for r in all_results if r["status"] == "sub_adr")
                n_no = sum(1 for r in all_results if r["status"] == "no_exit_trigger")
                n_err = sum(1 for r in all_results if r["status"] in
                           ("date_not_found", "no_entry_bar", "insufficient_bars", "no_adr"))
                print(f"  [{done_tickers}/{len(tasks)}] "
                      f"outcome={n_out}, sub_adr={n_sub}, no_trigger={n_no}, error={n_err}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")

    # ── 5. Classify and report ──
    outcomes = [r for r in all_results if r["status"] == "outcome"]
    sub_adr = [r for r in all_results if r["status"] == "sub_adr"]
    no_trigger = [r for r in all_results if r["status"] == "no_exit_trigger"]
    errors = [r for r in all_results if r["status"] in
              ("date_not_found", "no_entry_bar", "insufficient_bars", "no_adr")]

    total_classified = len(outcomes) + len(sub_adr) + len(no_trigger)

    print(f"\n{'='*80}")
    print(f"OUTCOME CLASSIFICATION")
    print(f"Exit: {exit_cond['expr_name']} {exit_cond['direction']} {exit_cond['threshold']}")
    print(f"Min ADR: {adr_floor}")
    print(f"Min MFE: {mfe_floor}")
    print(f"{'='*80}")
    print(f"  Total signals:      {len(signals)}")
    print(f"  OUTCOME:            {len(outcomes):4d} ({len(outcomes)/len(signals)*100:.1f}%) — exit triggered + >= {adr_floor} ADR move")
    print(f"  Sub-ADR:            {len(sub_adr):4d} ({len(sub_adr)/len(signals)*100:.1f}%) — exit triggered but < {adr_floor} ADR move")
    print(f"  No exit trigger:    {len(no_trigger):4d} ({len(no_trigger)/len(signals)*100:.1f}%) — exit never fired in {args.max_forward} bars")
    print(f"  Errors/skipped:     {len(errors):4d}")

    # Example safety check
    example_outcomes = [r for r in outcomes if r["is_example"]]
    example_sub_adr = [r for r in sub_adr if r["is_example"]]
    example_no_trigger = [r for r in no_trigger if r["is_example"]]
    example_errors = [r for r in errors if r.get("is_example")]

    print(f"\n  EXAMPLE SAFETY CHECK:")
    print(f"    Examples as outcomes:    {len(example_outcomes)}/{len(example_set)}")
    if example_sub_adr:
        print(f"    Examples sub-ADR:       {len(example_sub_adr)} ⚠️")
        for r in example_sub_adr:
            print(f"      {r['ticker']} {r['date']} — {r['adr_move']:.2f} ADR (need {adr_floor})")
    if example_no_trigger:
        print(f"    Examples no trigger:    {len(example_no_trigger)} ⚠️")
        for r in example_no_trigger:
            print(f"      {r['ticker']} {r['date']}")
    if example_errors:
        print(f"    Examples error:         {len(example_errors)} ⚠️")
        for r in example_errors:
            print(f"      {r['ticker']} {r['date']} — {r['status']}")
    if len(example_outcomes) == len(example_set):
        print(f"    ✅ All examples classified as outcomes")

    # Stats on outcomes
    if outcomes:
        pct_moves = [r["pct_move"] for r in outcomes]
        adr_moves = [r["adr_move"] for r in outcomes]
        trigger_bars = [r["trigger_bar"] for r in outcomes]
        cap_effs = [r["capture_eff"] for r in outcomes]

        print(f"\n  Outcome stats:")
        print(f"    % Move:      floor={min(pct_moves):+.1f}%  median={np.median(pct_moves):+.1f}%  avg={np.mean(pct_moves):+.1f}%")
        print(f"    ADR move:    floor={min(adr_moves):.1f}  median={np.median(adr_moves):.1f}  avg={np.mean(adr_moves):.1f}")
        print(f"    Trigger bar: avg={np.mean(trigger_bars):.1f}  median={np.median(trigger_bars):.0f}  max={max(trigger_bars)}")
        print(f"    Capture eff: floor={min(cap_effs):.3f}  median={np.median(cap_effs):.3f}  avg={np.mean(cap_effs):.3f}")

    # Stats on sub-ADR (exit triggered but move too small)
    if sub_adr:
        pct_sub = [r["pct_move"] for r in sub_adr]
        adr_sub = [r["adr_move"] for r in sub_adr]
        print(f"\n  Sub-ADR stats (exit triggered, move < {adr_floor} ADR):")
        print(f"    % Move:      floor={min(pct_sub):+.1f}%  median={np.median(pct_sub):+.1f}%  avg={np.mean(pct_sub):+.1f}%")
        print(f"    ADR move:    floor={min(adr_sub):.2f}  median={np.median(adr_sub):.2f}  avg={np.mean(adr_sub):.2f}")

    # Stats on no-trigger
    if no_trigger:
        pct_no = [r.get("pct_move_at_end", 0) for r in no_trigger]
        adr_no = [r.get("adr_move_at_end", 0) for r in no_trigger]
        print(f"\n  No-trigger stats (at end of {args.max_forward}-bar window):")
        print(f"    % Move:      floor={min(pct_no):+.1f}%  median={np.median(pct_no):+.1f}%  avg={np.mean(pct_no):+.1f}%")
        print(f"    ADR move:    floor={min(adr_no):.2f}  median={np.median(adr_no):.2f}  avg={np.mean(adr_no):.2f}")

    # Win rate preview
    if total_classified > 0:
        win_rate = len(outcomes) / total_classified
        print(f"\n  Win rate (outcome / classified): {win_rate:.1%} ({len(outcomes)}/{total_classified})")

    # Daily distribution of outcomes
    if outcomes:
        from collections import Counter
        daily_counts = Counter(r["date"] for r in outcomes)
        peak_day = max(daily_counts.values())
        avg_day = np.mean(list(daily_counts.values()))
        print(f"\n  Outcome distribution:")
        print(f"    Days with outcomes: {len(daily_counts)}")
        print(f"    Peak outcomes/day: {peak_day}")
        print(f"    Avg outcomes/day: {avg_day:.1f}")

    # ── 6. MANDATORY VALIDATION: all examples must be outcomes ──
    failed = []
    if len(example_outcomes) < len(example_set):
        # Some examples didn't make it to outcome status
        outcome_keys = set((r["ticker"], r["date"]) for r in example_outcomes)
        for key in example_set:
            if key not in outcome_keys:
                failed.append(f"{key[0]} {key[1]} — not classified as outcome")
    if example_sub_adr:
        for r in example_sub_adr:
            failed.append(f"{r['ticker']} {r['date']} — sub-ADR ({r['adr_move']:.2f})")
    if example_no_trigger:
        for r in example_no_trigger:
            failed.append(f"{r['ticker']} {r['date']} — exit never triggered")
    if example_errors:
        for r in example_errors:
            failed.append(f"{r['ticker']} {r['date']} — {r['status']}")
    
    if failed:
        print(f"\n{'!'*80}")
        print(f"VALIDATION FAILED — not all examples classified as outcomes!")
        print(f"  Failed ({len(failed)}/{len(example_set)}):")
        for f in failed:
            print(f"    {f}")
        print(f"{'!'*80}")
        print(f"\nResults NOT saved. All examples must pass. No exceptions.")
        return

    # ── 7. Save results ──
    import hashlib
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(os.path.dirname(__file__), "..", "local_runner", "cache")
    os.makedirs(out_dir, exist_ok=True)

    output = {
        "setup_type": args.setup,
        "direction": args.direction,
        "max_forward": args.max_forward,
        "min_adr": adr_floor,
        "min_mfe": mfe_floor,
        "exit_condition": {
            "expr_name": exit_cond["expr_name"],
            "direction": exit_cond["direction"],
            "threshold": exit_cond["threshold"],
        },
        "pyramid_source": os.path.basename(args.pyramid) if args.pyramid else "grinds/latest",
        "summary": {
            "total_signals": len(signals),
            "outcomes": len(outcomes),
            "sub_adr": len(sub_adr),
            "no_trigger": len(no_trigger),
            "errors": len(errors),
            "win_rate": len(outcomes) / total_classified if total_classified > 0 else 0,
            "examples_as_outcomes": len(example_outcomes),
            "examples_total": len(example_set),
        },
        "outcomes": sorted(outcomes, key=lambda r: r["date"]),
        "sub_adr": sorted(sub_adr, key=lambda r: r["date"]),
        "no_trigger": sorted(no_trigger, key=lambda r: r["date"]),
        "errors": errors,
    }

    outpath = os.path.join(out_dir, f"outcome_{args.setup}_{ts}.json")
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {outpath}")

    from file_mirror import mirror_file
    mirror_file(outpath)

    # Top outcomes by ADR
    if outcomes:
        top = sorted(outcomes, key=lambda r: r["adr_move"], reverse=True)
        print(f"\n{'─'*80}")
        print(f"TOP 15 OUTCOMES by ADR move:")
        print(f"{'─'*80}")
        print(f"{'Ticker':8s} {'Date':12s} {'%Move':>8s} {'ADR':>6s} {'MFE%':>8s} "
              f"{'CapEff':>7s} {'Bar#':>5s} {'Ex?':>4s}")
        for r in top[:15]:
            ex = "✓" if r["is_example"] else ""
            print(f"{r['ticker']:8s} {r['date']:12s} {r['pct_move']:+7.1f}% "
                  f"{r['adr_move']:5.1f} {r['mfe_pct']:+7.1f}% "
                  f"{r['capture_eff']:6.3f} {r['trigger_bar']:5d} {ex:>4s}")


if __name__ == "__main__":
    main()
