"""
Exit Grinder — Step 6 of ANALYSIS_SYSTEM.md

Brute forces post-signal expressions against validated examples' forward paths
to find TA-driven exit conditions that reliably capture the most move.

Benchmark: entry candle high → exit candle close (% move + ADR captured).
For shorts: positive captured = price went down from entry high.

Usage:
    python scripts/exit_grinder.py --setup dtss --max-forward 120

Process:
    1. Load examples from Railway API
    2. Fetch full OHLCV per ticker from Railway (universe_ohlcv)
    3. Build ExitExprEngine per example
    4. Compute all base exit expressions at every forward bar
    5. For each expression, test threshold conditions
    6. Score by: floor capture efficiency (worst example), with % move shown
    7. Rank and report

Output includes % move (entry high → exit close) for easy visualization.
"""

import argparse
import sys
import os
import time
import numpy as np
import pandas as pd
import requests
from dataclasses import dataclass, field
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.exit_expressions import generate_exit_expressions
from scripts.exit_compute import ExitExprEngine

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_LOOKBACK = 1500  # bars before entry for MA warmup
MAX_FORWARD_DEFAULT = 120  # bars after entry to analyze


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
    engine: object             # ExitExprEngine
    entry_high: float          # entry candle high (benchmark ref)
    n_forward: int             # bars available after entry
    mfe_pct: float             # max favorable excursion as % from entry high


@dataclass
class ExitCandidate:
    """A candidate exit condition with scores across all examples."""
    expr_name: str
    direction: str              # "above" or "below" threshold
    threshold: float
    # Per-example results
    exit_bars: list             # forward bar index where condition first triggers
    exit_closes: list           # close price at exit bar
    pct_moves: list             # % move: (entry_high - exit_close) / entry_high * 100
    adr_captured: list          # move captured in ADR units
    capture_effs: list          # captured / MFE per example
    # Aggregates
    examples_triggered: int     # how many examples this condition triggered on
    floor_pct_move: float       # worst % move across examples
    median_pct_move: float
    avg_pct_move: float
    floor_capture_eff: float    # worst capture efficiency
    median_capture_eff: float
    avg_bars_to_exit: float


# ============================================================
# Data Loading
# ============================================================

def load_examples(setup_type: str) -> list:
    """Load examples from Railway API."""
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    data = r.json()
    examples = data["examples"]
    print(f"Loaded {len(examples)} {setup_type.upper()} examples")
    return examples


def fetch_ticker_ohlcv(ticker: str, end_date: str = None, lookback: int = MAX_LOOKBACK) -> pd.DataFrame:
    """Fetch OHLCV from Railway universe_ohlcv."""
    params = {"lookback": lookback}
    if end_date:
        params["end_date"] = end_date
    r = requests.get(f"{RAILWAY_URL}/api/ohlcv/bulk/{ticker}", params=params)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise ValueError(f"API error for {ticker}: {data['error']}")
    rows = data["results"]
    if not rows:
        raise ValueError(f"No OHLCV data for {ticker}")
    df = pd.DataFrame(rows)
    df = df.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def build_example_data(example: dict, direction: str, max_forward: int, spy_df: pd.DataFrame = None) -> Optional[ExampleData]:
    """Load OHLCV and build ExitExprEngine for one example."""
    ticker = example["ticker"]
    entry_date = example["entryDate"]

    try:
        # Fetch enough data: lookback for MAs + forward bars
        df = fetch_ticker_ohlcv(ticker, lookback=MAX_LOOKBACK)

        # Find entry bar
        date_matches = df.index[df["date"] == entry_date].tolist()
        if not date_matches:
            print(f"  SKIP {ticker} — entry date {entry_date} not found in data")
            return None
        entry_idx = date_matches[0]

        # Check forward bars available
        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            print(f"  SKIP {ticker} — only {n_available} forward bars")
            return None

        actual_forward = min(max_forward, n_available)

        # Build engine
        engine = ExitExprEngine(df, entry_idx, direction=direction, spy_df=spy_df,
                                max_forward=actual_forward)

        entry_high = df["high"].iloc[entry_idx]

        # Compute MFE % (max favorable excursion from entry high)
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
            engine=engine,
            entry_high=entry_high,
            n_forward=actual_forward,
            mfe_pct=mfe_pct,
        )
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")
        return None


# ============================================================
# Expression Matrix Building
# ============================================================

def build_expression_matrix(example: ExampleData, expressions: list) -> dict:
    """Compute all base expressions for one example, returning dict of arrays.

    Returns:
        {expr_name: numpy array of length n_forward+1 (bar 0 = entry bar)}
    """
    result = {}
    failed = []
    for expr in expressions:
        try:
            series = example.engine.compute(expr["compute"])
            result[expr["name"]] = series
        except Exception as e:
            failed.append((expr["name"], str(e)))

    if failed:
        print(f"  {example.ticker}: {len(failed)} expressions failed (of {len(expressions)})")

    return result


# ============================================================
# Move Computation
# ============================================================

def compute_move_at_bar(example: ExampleData, bar_idx: int, direction: str) -> dict:
    """Compute the move metrics at a specific forward bar.

    Returns dict with:
        pct_move: % from entry high to exit close (positive = favorable)
        adr_captured: move in ADR units
        capture_eff: captured / MFE
    """
    abs_idx = example.entry_idx + bar_idx
    exit_close = example.df["close"].iloc[abs_idx]
    entry_high = example.entry_high

    if direction == "short":
        raw_move = entry_high - exit_close
        pct_move = raw_move / entry_high * 100
    else:
        entry_low = example.df["low"].iloc[example.entry_idx]
        raw_move = exit_close - entry_low
        pct_move = raw_move / entry_low * 100

    # ADR at exit bar
    adr_series = example.engine._adr14
    adr_val = adr_series[bar_idx] if bar_idx < len(adr_series) else adr_series[-1]
    adr_captured = raw_move / adr_val if adr_val > 0 else 0.0

    # Capture efficiency
    mfe = example.mfe_pct
    capture_eff = pct_move / mfe if mfe > 0 else 0.0

    return {
        "exit_close": exit_close,
        "pct_move": pct_move,
        "adr_captured": adr_captured,
        "capture_eff": capture_eff,
    }


# ============================================================
# Threshold Grinding
# ============================================================

def generate_thresholds(values: np.ndarray, n_thresholds: int = 20) -> list:
    """Generate threshold values from the data distribution.

    Uses percentiles of non-NaN values to ensure thresholds are data-driven.
    """
    clean = values[~np.isnan(values)]
    if len(clean) < 5:
        return []

    # Use percentiles for evenly-spaced coverage
    pcts = np.linspace(5, 95, n_thresholds)
    thresholds = np.percentile(clean, pcts)

    # Deduplicate (round to avoid float noise)
    seen = set()
    result = []
    for t in thresholds:
        t_round = round(float(t), 6)
        if t_round not in seen:
            seen.add(t_round)
            result.append(t_round)

    return result


def test_threshold_condition(expr_matrices: list, expr_name: str, direction: str,
                             threshold: float, examples: list,
                             setup_direction: str, min_bar: int = 1) -> Optional[ExitCandidate]:
    """Test one threshold condition across all examples.

    For direction='above': exit when expression value > threshold
    For direction='below': exit when expression value < threshold

    min_bar: earliest bar to allow exit (skip bar 0 = entry bar).

    Returns ExitCandidate if the condition triggers on at least 1 example.
    """
    exit_bars = []
    exit_closes = []
    pct_moves = []
    adr_captured_list = []
    capture_effs = []
    triggered_count = 0

    for i, (matrix, example) in enumerate(zip(expr_matrices, examples)):
        if expr_name not in matrix:
            # Expression failed for this example — skip
            exit_bars.append(-1)
            exit_closes.append(np.nan)
            pct_moves.append(np.nan)
            adr_captured_list.append(np.nan)
            capture_effs.append(np.nan)
            continue

        series = matrix[expr_name]

        # Find first bar >= min_bar where condition is met
        found = False
        for bar in range(min_bar, len(series)):
            val = series[bar]
            if np.isnan(val):
                continue
            triggered = (val > threshold) if direction == "above" else (val < threshold)
            if triggered:
                move = compute_move_at_bar(example, bar, setup_direction)
                exit_bars.append(bar)
                exit_closes.append(move["exit_close"])
                pct_moves.append(move["pct_move"])
                adr_captured_list.append(move["adr_captured"])
                capture_effs.append(move["capture_eff"])
                triggered_count += 1
                found = True
                break

        if not found:
            exit_bars.append(-1)
            exit_closes.append(np.nan)
            pct_moves.append(np.nan)
            adr_captured_list.append(np.nan)
            capture_effs.append(np.nan)

    if triggered_count == 0:
        return None

    # Compute aggregates (only over triggered examples)
    valid_pcts = [p for p in pct_moves if not np.isnan(p)]
    valid_effs = [e for e in capture_effs if not np.isnan(e)]
    valid_bars = [b for b in exit_bars if b >= 0]

    return ExitCandidate(
        expr_name=expr_name,
        direction=direction,
        threshold=threshold,
        exit_bars=exit_bars,
        exit_closes=exit_closes,
        pct_moves=pct_moves,
        adr_captured=adr_captured_list,
        capture_effs=capture_effs,
        examples_triggered=triggered_count,
        floor_pct_move=min(valid_pcts) if valid_pcts else -999,
        median_pct_move=float(np.median(valid_pcts)) if valid_pcts else 0,
        avg_pct_move=float(np.mean(valid_pcts)) if valid_pcts else 0,
        floor_capture_eff=min(valid_effs) if valid_effs else -999,
        median_capture_eff=float(np.median(valid_effs)) if valid_effs else 0,
        avg_bars_to_exit=float(np.mean(valid_bars)) if valid_bars else 999,
    )


# ============================================================
# Main Grinder Loop
# ============================================================

def grind_exits(examples: list, expr_matrices: list, expressions: list,
                direction: str = "short", n_thresholds: int = 20,
                min_bar: int = 1, min_trigger_pct: float = 0.8,
                top_n: int = 50) -> list:
    """Grind all expressions × thresholds × directions.

    Args:
        examples: list of ExampleData
        expr_matrices: list of dicts (one per example) with expr_name → series
        expressions: base expression list from generate_exit_expressions()
        direction: "short" or "long"
        n_thresholds: thresholds to test per expression
        min_bar: earliest exit bar (1 = day after entry)
        min_trigger_pct: minimum % of examples that must trigger (0.8 = 80%)
        top_n: return top N results

    Returns:
        list of ExitCandidate sorted by floor_capture_eff descending
    """
    n_examples = len(examples)
    min_triggered = max(1, int(n_examples * min_trigger_pct))

    all_candidates = []
    n_exprs = len(expressions)

    print(f"\nGrinding {n_exprs} expressions × ~{n_thresholds} thresholds × 2 directions...")
    print(f"Min trigger: {min_triggered}/{n_examples} examples ({min_trigger_pct*100:.0f}%)")
    print(f"Min exit bar: {min_bar} (skip entry bar)")

    t0 = time.time()
    tested = 0
    passed = 0

    for expr_i, expr in enumerate(expressions):
        name = expr["name"]

        if (expr_i + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{expr_i+1}/{n_exprs}] {rate:.1f} expr/s, {passed} candidates so far...")

        # Gather this expression's values across all examples to determine thresholds
        all_values = []
        for matrix in expr_matrices:
            if name in matrix:
                vals = matrix[name]
                all_values.append(vals[~np.isnan(vals)])

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
                candidate = test_threshold_condition(
                    expr_matrices, name, dir_test, thresh,
                    examples, direction, min_bar=min_bar
                )

                if candidate and candidate.examples_triggered >= min_triggered:
                    passed += 1
                    all_candidates.append(candidate)

    elapsed = time.time() - t0
    print(f"\nDone: tested {tested} conditions in {elapsed:.1f}s")
    print(f"Passed filter: {passed} candidates (triggered on >= {min_triggered} examples)")

    # Sort by floor capture efficiency (primary), then median pct move (secondary)
    all_candidates.sort(key=lambda c: (c.floor_capture_eff, c.median_pct_move), reverse=True)

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
    tickers = [ex.ticker for ex in examples]

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

        # Per-example breakdown
        print(f"      {'Ticker':8s} {'Entry Date':12s} {'Bar#':>5s} {'Entry High':>11s} "
              f"{'Exit Close':>11s} {'% Move':>8s} {'ADR Capt':>9s} {'Capt Eff':>9s}")
        for i, ex in enumerate(examples):
            bar = cand.exit_bars[i]
            if bar < 0:
                print(f"      {ex.ticker:8s} {ex.entry_date:12s}   ---   (not triggered)")
                continue
            pct = cand.pct_moves[i]
            adr = cand.adr_captured[i]
            eff = cand.capture_effs[i]
            ec = cand.exit_closes[i]
            print(f"      {ex.ticker:8s} {ex.entry_date:12s} {bar:5d} "
                  f"${ex.entry_high:10.2f} ${ec:10.2f} {pct:+7.2f}% {adr:+8.2f} ADR {eff:8.2f}")


def print_mfe_summary(examples: list, direction: str):
    """Print MFE summary for context — shows maximum possible capture per example."""
    print(f"\n{'='*80}")
    print(f"MFE SUMMARY — Maximum Favorable Excursion per example")
    print(f"{'='*80}")
    print(f"{'Ticker':8s} {'Entry Date':12s} {'Entry High':>11s} {'MFE %':>8s} {'Fwd Bars':>9s}")
    for ex in examples:
        print(f"{ex.ticker:8s} {ex.entry_date:12s} ${ex.entry_high:10.2f} {ex.mfe_pct:+7.2f}% {ex.n_forward:9d}")
    mfes = [ex.mfe_pct for ex in examples]
    print(f"\n  Floor MFE:  {min(mfes):+.2f}%")
    print(f"  Median MFE: {np.median(mfes):+.2f}%")
    print(f"  Avg MFE:    {np.mean(mfes):+.2f}%")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Exit Grinder — Step 6")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT,
                        help="Max forward bars to analyze per example")
    parser.add_argument("--n-thresholds", type=int, default=20,
                        help="Thresholds to test per expression")
    parser.add_argument("--min-bar", type=int, default=1,
                        help="Earliest exit bar (1 = day after entry)")
    parser.add_argument("--min-trigger-pct", type=float, default=1.0,
                        help="Min fraction of examples that must trigger (1.0 = 100%%)")
    parser.add_argument("--top-n", type=int, default=50,
                        help="Top N results to show")
    parser.add_argument("--direction", default="short",
                        help="Trade direction: short or long")
    args = parser.parse_args()

    print(f"Exit Grinder — Step 6")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Thresholds: {args.n_thresholds}")
    print(f"Min trigger: {args.min_trigger_pct*100:.0f}%%, Min bar: {args.min_bar}")

    # 1. Load examples
    raw_examples = load_examples(args.setup)

    # 2. Fetch SPY data for relative strength
    print("\nFetching SPY data...")
    try:
        spy_df = fetch_ticker_ohlcv("SPY", lookback=MAX_LOOKBACK)
        print(f"  SPY: {len(spy_df)} bars")
    except Exception as e:
        print(f"  SPY fetch failed: {e} — relative strength will be NaN")
        spy_df = None

    # 3. Build ExampleData for each
    print(f"\nBuilding example data (fetching OHLCV + building engines)...")
    examples = []
    for raw in raw_examples:
        print(f"  {raw['ticker']:8s} {raw['entryDate']}...", end="", flush=True)
        ex = build_example_data(raw, args.direction, args.max_forward, spy_df)
        if ex:
            examples.append(ex)
            print(f" OK — {ex.n_forward} fwd bars, MFE={ex.mfe_pct:+.2f}%")
        else:
            print(" SKIP")

    if len(examples) < 3:
        print(f"\nOnly {len(examples)} examples loaded — need at least 3. Aborting.")
        return

    print(f"\n{len(examples)} examples ready")

    # 4. MFE summary
    print_mfe_summary(examples, args.direction)

    # 5. Generate expression library
    expressions = generate_exit_expressions()
    print(f"\nExpression library: {len(expressions)} base expressions")

    # 6. Build expression matrices
    print(f"\nComputing expression matrices ({len(expressions)} exprs × {len(examples)} examples)...")
    t0 = time.time()
    expr_matrices = []
    for ex in examples:
        print(f"  {ex.ticker:8s}...", end="", flush=True)
        matrix = build_expression_matrix(ex, expressions)
        expr_matrices.append(matrix)
        print(f" {len(matrix)}/{len(expressions)} computed")
    elapsed = time.time() - t0
    print(f"Matrix build: {elapsed:.1f}s")

    # 7. Grind
    candidates = grind_exits(
        examples, expr_matrices, expressions,
        direction=args.direction,
        n_thresholds=args.n_thresholds,
        min_bar=args.min_bar,
        min_trigger_pct=args.min_trigger_pct,
        top_n=args.top_n,
    )

    # 8. Report
    print_results(candidates, examples, top_n=args.top_n)

    # 9. Summary
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
