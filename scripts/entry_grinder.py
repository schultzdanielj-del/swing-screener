"""
Entry Grinder — Brute-force stop placement for breakout/breakdown setups.

Finds the most aggressive stop placement and stop-raise strategy that all
examples survive, plus the breakeven window. Output feeds into the
classification system for non-example signals.

Three parts:
  1. Static stop test (8 candidates, 100% survival)
  2. Ratcheting stop test (10 candidates × (fw+1) positions, monotonic, 100% survival)
  3. Breakeven window (max bars until price permanently clears entry zone)

Scoring: lowest avg/median ADR risk during forward window.

Usage:
    python scripts/entry_grinder.py --setup brko
"""

import argparse
import os
import sys
import time
import json
import pickle
import sqlite3

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import numpy as np
import pandas as pd
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_daily_cache():
    """Load daily OHLCV cache."""
    path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    if not os.path.exists(path):
        path = os.path.join(CACHE_DIR, "universe_ohlcv.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")
    with open(path, "rb") as f:
        return pickle.load(f)


def load_setup_direction(setup_type):
    """Get trade direction from setups table."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction FROM setups WHERE setup_type=?", (setup_type,)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Setup '{setup_type}' not found in setups table")
    return row[0]


def load_exit_condition(setup_type):
    """Load exit condition from signal_exit_grind output."""
    exit_path = os.path.join(
        REPO_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup_type}.json"
    )
    if not os.path.exists(exit_path):
        raise FileNotFoundError(f"No exit condition file: {exit_path}")
    with open(exit_path) as f:
        data = json.load(f)
    if data.get("grinder_type") == "signal_exit" and data.get("top_conditions"):
        return data["top_conditions"][0]
    raise ValueError(f"Invalid exit condition file: {exit_path}")


def load_forward_window(setup_type):
    """Load forward_window from raw_signal_clusters file header."""
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(cluster_path):
        raise FileNotFoundError(
            f"No cluster file: {cluster_path}\n"
            f"Run: python local_runner/pyramid_grinder.py --setup {setup_type} --refine"
        )
    with open(cluster_path) as f:
        data = json.load(f)
    fw = data.get("forward_window")
    if fw is None:
        raise ValueError(f"No forward_window in cluster file: {cluster_path}")
    return fw


def load_examples_from_db(setup_type):
    """Load examples directly from the examples table in SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker, entry_date",
        (setup_type,)
    ).fetchall()
    conn.close()
    return [{"ticker": r["ticker"], "entry_date": r["entry_date"]} for r in rows]


def date_to_idx(df_dates_str, target_date_str):
    """Find the OHLCV row index for a given date string.

    Args:
        df_dates_str: list of date strings (pre-computed, YYYY-MM-DD)
        target_date_str: target date string

    Returns index or None if not found.
    """
    target = str(target_date_str)[:10]
    for i, d in enumerate(df_dates_str):
        if d == target:
            return i
    return None


def compute_adr14(highs, lows, bar_idx):
    """Compute 14-period ADR at a given bar index.

    Uses bars [bar_idx-13 .. bar_idx] inclusive (14 bars).
    """
    start = max(0, bar_idx - 13)
    if start >= bar_idx:
        return float(highs[bar_idx] - lows[bar_idx])
    return float(np.mean(highs[start:bar_idx + 1] - lows[start:bar_idx + 1]))


# ══════════════════════════════════════════════════════════════
# EXAMPLE EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_examples(db_examples, universe_cache, exit_cond, direction, forward_window):
    """Extract per-example data from SQLite examples + OHLCV.

    Signal bar = bar immediately before entry_date (the scan candle).
    Pre-signal bar = bar before signal bar.

    For each example:
      - Resolve entry bar, signal bar, pre-signal bar by DATE
      - Extract reference bar prices
      - Compute ADR at entry bar
      - Find exit bar by walking forward and evaluating exit condition

    Returns list of example dicts.
    """
    from expr_cache_builder import ExprSeriesCache

    print(f"  Examples in DB: {len(db_examples)}")

    # Load expr cache for exit condition evaluation
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        raise RuntimeError("Expression cache not found or invalid.")

    exit_expr = exit_cond["expression"]
    exit_thresh = exit_cond["threshold"]
    exit_dir = exit_cond["direction"]
    exit_col = expr_cache.expr_index(exit_expr)
    if exit_col is None:
        raise ValueError(f"Exit expression '{exit_expr}' not in expr cache")

    examples = []
    skipped = []

    for ex_raw in db_examples:
        ticker = ex_raw["ticker"]
        entry_date = str(ex_raw["entry_date"])[:10]

        # Load OHLCV
        df = universe_cache.get(ticker)
        if df is None:
            skipped.append(f"{ticker} {entry_date}: not in OHLCV cache")
            continue

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        highs = df["high"].values.astype(np.float64)
        lows = df["low"].values.astype(np.float64)
        closes = df["close"].values.astype(np.float64)
        dates_str = [str(d)[:10] for d in df["date"].values]

        # Resolve entry bar by date
        entry_idx = date_to_idx(dates_str, entry_date)
        if entry_idx is None:
            skipped.append(f"{ticker} {entry_date}: entry_date not in OHLCV")
            continue

        # Signal bar = bar immediately before entry bar
        signal_idx = entry_idx - 1
        if signal_idx < 0:
            skipped.append(f"{ticker} {entry_date}: no bar before entry")
            continue
        signal_date = dates_str[signal_idx]

        # Forward window bars (from signal bar)
        fw_end_idx = min(signal_idx + forward_window, len(df) - 1)
        if fw_end_idx <= signal_idx:
            skipped.append(f"{ticker} {entry_date}: no forward window bars")
            continue

        fw_highs = highs[signal_idx:fw_end_idx + 1]
        fw_lows = lows[signal_idx:fw_end_idx + 1]

        # Reference bars: 3 bars, each with high and low
        fw_highest_high_offset = int(np.argmax(fw_highs))
        fw_lowest_low_offset = int(np.argmin(fw_lows))
        fw_hh_idx = signal_idx + fw_highest_high_offset
        fw_ll_idx = signal_idx + fw_lowest_low_offset

        ref_bars = {
            "signal": {"idx": signal_idx, "high": float(highs[signal_idx]), "low": float(lows[signal_idx])},
            "fw_highest_high": {"idx": fw_hh_idx, "high": float(highs[fw_hh_idx]), "low": float(lows[fw_hh_idx])},
            "fw_lowest_low": {"idx": fw_ll_idx, "high": float(highs[fw_ll_idx]), "low": float(lows[fw_ll_idx])},
        }

        # ADR at entry bar
        adr_at_entry = compute_adr14(highs, lows, entry_idx)
        if adr_at_entry <= 0 or np.isnan(adr_at_entry):
            skipped.append(f"{ticker} {entry_date}: invalid ADR {adr_at_entry}")
            continue

        # Worst-case entry price (for scoring only)
        if direction == "long":
            worst_entry = float(lows[entry_idx]) + adr_at_entry
        else:
            worst_entry = float(highs[entry_idx]) - adr_at_entry

        # Find exit bar: walk forward from signal bar, evaluate exit expression
        cached_dates, cached_data = expr_cache.get_ticker(ticker)
        if cached_dates is None:
            skipped.append(f"{ticker} {entry_date}: not in expr cache")
            continue
        # Resolve signal date in expr cache dates (date-based, not index-based)
        cache_dates_str = [str(d)[:10] for d in cached_dates]
        if signal_date not in cache_dates_str:
            skipped.append(f"{ticker} {entry_date}: signal date {signal_date} not in expr cache")
            continue
        cache_signal_idx = cache_dates_str.index(signal_date)

        exit_series = cached_data[:, exit_col]
        exit_bar_idx = None
        max_search = len(cached_data) - 1

        for fi in range(1, max_search - cache_signal_idx + 1):
            check_idx = cache_signal_idx + fi
            if check_idx > max_search:
                break
            v = exit_series[check_idx]
            if np.isnan(v):
                continue
            if exit_dir in (">=", "above") and v >= exit_thresh:
                exit_bar_idx = check_idx
                break
            elif exit_dir in ("<=", "below") and v <= exit_thresh:
                exit_bar_idx = check_idx
                break

        if exit_bar_idx is None:
            exit_bar_idx = len(df) - 1
        else:
            # exit_bar_idx is cache-relative — convert to OHLCV index via date
            exit_date = cache_dates_str[exit_bar_idx]
            exit_bar_idx = date_to_idx(dates_str, exit_date)
            if exit_bar_idx is None:
                skipped.append(f"{ticker} {entry_date}: exit date {exit_date} not in OHLCV")
                continue

        exit_bars_from_signal = exit_bar_idx - signal_idx

        examples.append({
            "ticker": ticker,
            "entry_date": entry_date,
            "signal_date": signal_date,
            "direction": direction,
            "forward_window": forward_window,
            "signal_idx": signal_idx,
            "entry_idx": entry_idx,
            "fw_end_idx": fw_end_idx,
            "exit_bar_idx": exit_bar_idx,
            "exit_bars_from_signal": exit_bars_from_signal,
            "ref_bars": ref_bars,
            "dates_str": dates_str,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "adr_at_entry": adr_at_entry,
            "worst_entry": worst_entry,
            "entry_low": float(lows[entry_idx]),
            "entry_high": float(highs[entry_idx]),
        })

    print(f"  Examples extracted: {len(examples)}")
    if skipped:
        print(f"  Skipped {len(skipped)}:")
        for s in skipped:
            print(f"    {s}")

    return examples


# ══════════════════════════════════════════════════════════════
# PART 1: STATIC STOP TEST
# ══════════════════════════════════════════════════════════════

# 8 static candidates: high and low of each of 4 reference bars
STATIC_LABELS = [
    "signal_low", "signal_high",
    "fw_highest_high_low", "fw_highest_high_high",
    "fw_lowest_low_low", "fw_lowest_low_high",
]

STATIC_REF_MAP = [
    ("signal", "low"), ("signal", "high"),
    ("fw_highest_high", "low"), ("fw_highest_high", "high"),
    ("fw_lowest_low", "low"), ("fw_lowest_low", "high"),
]


def run_static_stop_test(examples, direction, forward_window):
    """Part 1: Test 6 static stop candidates across all examples.

    For each candidate, check if the close ever breaches the stop from
    entry bar through exit bar. 100% survival required.

    Returns list of result dicts sorted by aggressiveness (tightest first).
    """
    print(f"\n  ── PART 1: STATIC STOP TEST ──")
    n_ex = len(examples)
    results = []

    for ci, (ref_bar_name, price_field) in enumerate(STATIC_REF_MAP):
        label = STATIC_LABELS[ci]
        n_survive = 0
        worst_margin_pct = float("inf")
        risk_adrs = []

        for ex in examples:
            # Stop level for this example
            stop_level = ex["ref_bars"][ref_bar_name][price_field]

            entry_idx = ex["entry_idx"]
            exit_bar_idx = ex["exit_bar_idx"]
            closes = ex["closes"]

            # Check survival: entry bar through exit bar (not in trade before entry)
            check_closes = closes[entry_idx:exit_bar_idx + 1]

            if direction == "long":
                # Breach = close below stop
                min_close = float(np.min(check_closes))
                if min_close >= stop_level:
                    n_survive += 1
                margin = (min_close - stop_level) / stop_level if stop_level != 0 else float("inf")
            else:
                # Breach = close above stop
                max_close = float(np.max(check_closes))
                if max_close <= stop_level:
                    n_survive += 1
                margin = (stop_level - max_close) / stop_level if stop_level != 0 else float("inf")

            worst_margin_pct = min(worst_margin_pct, margin)

            # Risk-ADR scoring: distance from worst-case entry to stop, in ADR
            # Computed across forward window only (signal through fw end)
            fw_end_idx = ex["fw_end_idx"]
            if direction == "long":
                risk = (ex["worst_entry"] - stop_level) / ex["adr_at_entry"]
            else:
                risk = (stop_level - ex["worst_entry"]) / ex["adr_at_entry"]
            risk_adrs.append(risk)

        survival_rate = n_survive / n_ex
        avg_risk = float(np.mean(risk_adrs)) if risk_adrs else 0
        median_risk = float(np.median(risk_adrs)) if risk_adrs else 0

        results.append({
            "label": label,
            "survival_rate": round(survival_rate, 4),
            "n_survive": n_survive,
            "n_total": n_ex,
            "worst_margin_pct": round(worst_margin_pct * 100, 2) if worst_margin_pct != float("inf") else None,
            "avg_risk_adr": round(avg_risk, 4),
            "median_risk_adr": round(median_risk, 4),
        })

        status = "✓" if n_survive == n_ex else "✗"
        print(f"    {status} {label:25s}  survive={n_survive}/{n_ex}  "
              f"worst_margin={worst_margin_pct*100:.2f}%  "
              f"avg_risk={avg_risk:.2f} ADR  median_risk={median_risk:.2f} ADR")

    # Sort: 100% survivors first, then by avg_risk_adr ascending (tightest)
    results.sort(key=lambda r: (0 if r["survival_rate"] == 1.0 else 1, r["avg_risk_adr"]))

    n_pass = sum(1 for r in results if r["survival_rate"] == 1.0)
    print(f"\n  Static: {n_pass}/{len(STATIC_LABELS)} candidates with 100% survival")

    return results


# ══════════════════════════════════════════════════════════════
# PART 2: INDICATOR STOP SWEEP
# ══════════════════════════════════════════════════════════════

def _compute_sma(arr, period):
    """Simple moving average. Returns array same length, NaN-padded at start."""
    out = np.full(len(arr), np.nan)
    if period > len(arr):
        return out
    cs = np.cumsum(arr)
    out[period - 1:] = (cs[period - 1:] - np.concatenate(([0], cs[:-period]))) / period
    return out


def _compute_ema(arr, period):
    """Exponential moving average."""
    out = np.full(len(arr), np.nan)
    if period > len(arr):
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _compute_atr(highs, lows, closes, period):
    """Average True Range."""
    n = len(highs)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    return _compute_ema(tr, period)


def _generate_indicators(highs, lows, closes, direction):
    """Generate all indicator stop levels from OHLCV arrays.

    Returns list of (label, values_array) where values_array is same length
    as the input arrays.
    """
    n = len(closes)
    indicators = []

    # SMA periods
    sma_periods = list(range(2, 201))
    for p in sma_periods:
        sma = _compute_sma(closes, p)
        indicators.append((f"sma_{p}", sma))

    # EMA periods
    ema_periods = list(range(2, 201))
    for p in ema_periods:
        ema = _compute_ema(closes, p)
        indicators.append((f"ema_{p}", ema))

    # ATR for offset calculations (common periods)
    atr_periods = [7, 10, 14, 20]
    atr_cache = {}
    for ap in atr_periods:
        atr = _compute_atr(highs, lows, closes, ap)
        atr_cache[ap] = atr

    # SMA minus N × ATR
    sma_offset_periods = [5, 8, 10, 13, 20, 21, 50, 100, 200]
    atr_mults = [x / 10.0 for x in range(1, 51)]  # 0.1 to 5.0
    for sp in sma_offset_periods:
        sma = _compute_sma(closes, sp)
        for ap in atr_periods:
            atr = atr_cache[ap]
            for mult in atr_mults:
                if direction == "long":
                    vals = sma - mult * atr
                else:
                    vals = sma + mult * atr
                indicators.append((f"sma{sp}_atr{ap}x{mult:.1f}", vals))

    # EMA minus N × ATR
    ema_offset_periods = [5, 8, 10, 13, 20, 21, 50, 100, 200]
    for ep in ema_offset_periods:
        ema = _compute_ema(closes, ep)
        for ap in atr_periods:
            atr = atr_cache[ap]
            for mult in atr_mults:
                if direction == "long":
                    vals = ema - mult * atr
                else:
                    vals = ema + mult * atr
                indicators.append((f"ema{ep}_atr{ap}x{mult:.1f}", vals))

    # Bollinger lower/upper band: SMA ± mult × stddev
    bb_periods = list(range(5, 101))
    bb_mults = [x / 4.0 for x in range(2, 13)]  # 0.5 to 3.0 in 0.25 steps
    for bp in bb_periods:
        sma = _compute_sma(closes, bp)
        std = np.full(n, np.nan)
        for i in range(bp - 1, n):
            std[i] = np.std(closes[i - bp + 1:i + 1], ddof=0)
        for bm in bb_mults:
            if direction == "long":
                vals = sma - bm * std
            else:
                vals = sma + bm * std
            indicators.append((f"bb{bp}_x{bm:.2f}", vals))

    # Keltner lower/upper: EMA ± mult × ATR
    kelt_ema_periods = [5, 8, 10, 13, 20, 21, 50]
    kelt_mults = [x / 4.0 for x in range(2, 13)]  # 0.5 to 3.0
    for kp in kelt_ema_periods:
        ema = _compute_ema(closes, kp)
        for ap in atr_periods:
            atr = atr_cache[ap]
            for km in kelt_mults:
                if direction == "long":
                    vals = ema - km * atr
                else:
                    vals = ema + km * atr
                indicators.append((f"kelt{kp}_atr{ap}x{km:.2f}", vals))

    # Close/High minus N × ATR (no MA, just raw price offset)
    for ap in atr_periods:
        atr = atr_cache[ap]
        for mult in atr_mults:
            if direction == "long":
                indicators.append((f"close_atr{ap}x{mult:.1f}", closes - mult * atr))
                indicators.append((f"high_atr{ap}x{mult:.1f}", highs - mult * atr))
            else:
                indicators.append((f"close_atr{ap}x{mult:.1f}", closes + mult * atr))
                indicators.append((f"low_atr{ap}x{mult:.1f}", lows + mult * atr))

    return indicators


def run_indicator_stop_test(examples, direction):
    """Part 2: Test thousands of TA indicators as stop levels.

    Computes indicators directly from OHLCV. Tests each as a stop with
    both close-below and low-below breach types. Entry through exit.

    Returns (results_list, n_100pct, n_tested).
    """
    print(f"\n  ── PART 2: INDICATOR STOP SWEEP ──")

    n_ex = len(examples)
    t0 = time.time()

    # Generate indicators from first example to get labels and count
    sample = _generate_indicators(
        examples[0]["highs"], examples[0]["lows"], examples[0]["closes"], direction)
    labels = [s[0] for s in sample]
    n_ind = len(labels)
    print(f"  Generated {n_ind} indicators")

    # For each example, compute all indicators and test breach
    close_survive = np.zeros(n_ind, dtype=np.int32)
    low_survive = np.zeros(n_ind, dtype=np.int32)

    for ei, ex in enumerate(examples):
        entry_idx = ex["entry_idx"]
        exit_idx = ex["exit_bar_idx"]
        trade_closes = ex["closes"][entry_idx:exit_idx + 1]
        trade_lows = ex["lows"][entry_idx:exit_idx + 1]
        trade_highs = ex["highs"][entry_idx:exit_idx + 1]

        indicators = _generate_indicators(ex["highs"], ex["lows"], ex["closes"], direction)

        for ii, (label, vals) in enumerate(indicators):
            trade_vals = vals[entry_idx:exit_idx + 1]

            # Skip if indicator has NaN in trade window
            if np.any(np.isnan(trade_vals)):
                # NaN = indicator not yet computed (warmup period)
                # Treat as survived (stop not defined yet = no stop)
                close_survive[ii] += 1
                low_survive[ii] += 1
                continue

            if direction == "long":
                if not np.any(trade_closes < trade_vals):
                    close_survive[ii] += 1
                if not np.any(trade_lows < trade_vals):
                    low_survive[ii] += 1
            else:
                if not np.any(trade_closes > trade_vals):
                    close_survive[ii] += 1
                if not np.any(trade_highs > trade_vals):
                    low_survive[ii] += 1

    elapsed = time.time() - t0
    n_close_100 = int(np.sum(close_survive == n_ex))
    n_low_100 = int(np.sum(low_survive == n_ex))

    print(f"  Completed in {elapsed:.1f}s")
    print(f"  Close-below 100%: {n_close_100}/{n_ind}")
    print(f"  Low-below 100%:   {n_low_100}/{n_ind}")

    # Build results for >= 90% survival
    threshold = int(n_ex * 0.90)
    results = []
    for ii in range(n_ind):
        cs = int(close_survive[ii])
        ls = int(low_survive[ii])
        if cs >= threshold or ls >= threshold:
            results.append({
                "indicator": labels[ii],
                "close_survive": cs,
                "low_survive": ls,
                "n_total": n_ex,
                "close_rate": round(cs / n_ex, 4),
                "low_rate": round(ls / n_ex, 4),
            })

    # Sort: both 100% first, then close 100%, then by close survival desc
    results.sort(key=lambda r: (
        0 if r["close_survive"] == n_ex and r["low_survive"] == n_ex else
        1 if r["close_survive"] == n_ex else
        2 if r["low_survive"] == n_ex else 3,
        -r["close_survive"],
        -r["low_survive"],
    ))

    # Print
    print(f"\n  Indicators >= 90% survival ({len(results)} found):")
    print(f"  {'Indicator':35s}  {'Close':>10s}  {'Low':>10s}")
    for r in results[:80]:
        cs_str = f"{r['close_survive']}/{r['n_total']}"
        ls_str = f"{r['low_survive']}/{r['n_total']}"
        c_mark = "✓" if r["close_survive"] == n_ex else " "
        l_mark = "✓" if r["low_survive"] == n_ex else " "
        print(f"  {r['indicator']:35s}  {c_mark}{cs_str:>9s}  {l_mark}{ls_str:>9s}")
    if len(results) > 80:
        print(f"  ... and {len(results) - 80} more")

    return results, n_close_100, n_ind * 2


# ══════════════════════════════════════════════════════════════
# PART 3: BREAKEVEN WINDOW
# ══════════════════════════════════════════════════════════════

def run_breakeven_window(examples, direction):
    """Part 3: Find the breakeven window.

    For each example:
      1. Breakeven level = entry bar low + 1 ADR (long) or entry bar high - 1 ADR (short)
         Using ADR at entry bar.
      2. Walk forward from entry bar. Find the last bar where close revisits
         the breakeven level.
      3. Breakeven window = max across all examples (bars from entry).

    Returns (breakeven_window_bars, per_example_details).
    """
    print(f"\n  ── PART 3: BREAKEVEN WINDOW ──")

    per_example = []
    max_window = 0

    for ex in examples:
        ticker = ex["ticker"]
        entry_idx = ex["entry_idx"]
        exit_bar_idx = ex["exit_bar_idx"]
        closes = ex["closes"]
        adr = ex["adr_at_entry"]

        if direction == "long":
            breakeven = ex["entry_low"] + adr
        else:
            breakeven = ex["entry_high"] - adr

        # Walk forward from entry bar to exit bar
        # Find the LAST bar where close revisits the breakeven level
        last_revisit = entry_idx  # default: entry bar itself
        for bi in range(entry_idx, exit_bar_idx + 1):
            if direction == "long":
                if closes[bi] < breakeven:
                    last_revisit = bi
            else:
                if closes[bi] > breakeven:
                    last_revisit = bi

        # Breakeven window = bars from entry to last revisit
        be_window = last_revisit - entry_idx

        per_example.append({
            "ticker": ticker,
            "entry_date": ex["entry_date"],
            "breakeven_level": round(breakeven, 4),
            "last_revisit_bar": be_window,
        })

        if be_window > max_window:
            max_window = be_window

    print(f"  Breakeven window: {max_window} bars (max across {len(examples)} examples)")
    print(f"  Per-example breakdown:")
    for pe in sorted(per_example, key=lambda x: -x["last_revisit_bar"])[:10]:
        print(f"    {pe['ticker']:6s} {pe['entry_date']}  "
              f"be_level={pe['breakeven_level']:.2f}  "
              f"window={pe['last_revisit_bar']} bars")
    if len(per_example) > 10:
        print(f"    ... and {len(per_example) - 10} more")

    return max_window, per_example


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Entry Grinder — brute-force stop placement")
    parser.add_argument("--setup", required=True, help="Setup type (e.g. brko)")
    parser.add_argument("--fw", type=int, default=None, help="Forward window override (skips cluster file)")
    args = parser.parse_args()
    setup_type = args.setup

    print(f"\n{'='*60}")
    print(f"  Entry Grinder — {setup_type}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    t_start = time.time()

    # ── Load inputs ──
    print(f"\n  Loading inputs...")
    direction = load_setup_direction(setup_type)
    print(f"  Direction: {direction}")

    exit_cond = load_exit_condition(setup_type)
    print(f"  Exit condition: {exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}")

    if args.fw is not None:
        forward_window = args.fw
        print(f"  Forward window: {forward_window} (override)")
    else:
        forward_window = load_forward_window(setup_type)
        print(f"  Forward window: {forward_window}")

    db_examples = load_examples_from_db(setup_type)
    print(f"  Examples in DB: {len(db_examples)}")

    print(f"\n  Loading OHLCV cache...")
    universe_cache = load_daily_cache()
    print(f"  OHLCV cache: {len(universe_cache)} tickers")

    # ── Extract examples ──
    print(f"\n  Extracting examples...")
    examples = extract_examples(db_examples, universe_cache, exit_cond, direction, forward_window)

    if not examples:
        print(f"  ERROR: No valid examples extracted. Cannot proceed.")
        return

    # Free OHLCV cache — examples hold their own arrays
    del universe_cache

    # ── Print example summary ──
    print(f"\n  ── EXAMPLE SUMMARY ({len(examples)} examples) ──")
    print(f"  {'Ticker':6s} {'Entry':12s} {'Signal':12s} {'ADR':>8s} "
          f"{'EntryLow':>10s} {'WrstEntry':>10s} {'ExitBars':>8s}")
    for ex in examples:
        print(f"  {ex['ticker']:6s} {ex['entry_date']:12s} {ex['signal_date']:12s} "
              f"{ex['adr_at_entry']:8.2f} "
              f"{ex['entry_low']:10.2f} {ex['worst_entry']:10.2f} "
              f"{ex['exit_bars_from_signal']:8d}")

    # ── Run Parts ──
    static_results = run_static_stop_test(examples, direction, forward_window)
    ind_results, ind_100, ind_tested = run_indicator_stop_test(
        examples, direction)
    breakeven_window, be_details = run_breakeven_window(examples, direction)

    # ── Build output ──
    elapsed = time.time() - t_start
    output = {
        "setup_type": setup_type,
        "direction": direction,
        "forward_window": forward_window,
        "n_examples": len(examples),
        "elapsed_s": round(elapsed, 1),
        "breakeven_window": breakeven_window,
        "exit_condition": {
            "expression": exit_cond["expression"],
            "threshold": exit_cond["threshold"],
            "direction": exit_cond["direction"],
        },
        "static_stops": static_results,
        "indicator_stops": ind_results,
        "indicator_stops_100pct": ind_100,
        "indicator_stops_tested": ind_tested,
    }

    # ── Save ──
    os.makedirs(CACHE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Timestamped
    ts_path = os.path.join(CACHE_DIR, f"entry_grinder_{setup_type}_{ts}.json")
    with open(ts_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {ts_path}")

    # Latest
    latest_path = os.path.join(CACHE_DIR, f"entry_grinder_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved: {latest_path}")

    # Mirror to Railway
    try:
        from file_mirror import mirror_file
        mirror_file(ts_path)
        mirror_file(latest_path)
        print(f"  Mirrored to Railway")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  Entry grinder completed in {elapsed:.1f}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
