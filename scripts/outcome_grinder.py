"""
Outcome Grinder — Step 7 of ANALYSIS_SYSTEM.md

Phase 1: Apply Step 6 exit conditions to all Step 3 signals.
         Signals where exit triggers = OUTCOME SIGNALS.
         Signals where exit never triggers = NON-OUTCOME SIGNALS.

Phase 2: (Future) Grind for additional post-signal behavior that
         separates examples from non-outcome signals.

Input:
    - Step 3 signals from Railway (backtest_signals table)
    - Step 6 exit conditions from data/exit_grind/exit_grind_{setup}.json
    - Examples from Railway
    - OHLCV data from Railway

Output:
    - data/outcome_grind/outcome_signals_{setup}.json

Usage:
    python scripts/outcome_grinder.py --setup dtss
    python scripts/outcome_grinder.py --setup dtss --max-forward 120 --workers 12
    python scripts/outcome_grinder.py --setup dtss --exit-rank 1  # use rank N exit condition
"""

import argparse
import sys
import os
import time
import json
import numpy as np
import pandas as pd
import requests
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_LOOKBACK = 1500
MAX_FORWARD_DEFAULT = 120
DEFAULT_WORKERS = os.cpu_count() or 8


# ============================================================
# Data Loading
# ============================================================

def load_signals(setup_type: str) -> list:
    """Load Step 3 signals from Railway."""
    r = requests.get(f"{RAILWAY_URL}/api/backtest/signals/{setup_type}")
    r.raise_for_status()
    d = r.json()
    signals = d["results"]
    print(f"Loaded {len(signals)} {setup_type.upper()} signals "
          f"({d['unique_tickers']} unique tickers)")
    return signals


def load_examples(setup_type: str) -> list:
    """Load validated examples from Railway."""
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    d = r.json()
    examples = d["examples"]
    print(f"Loaded {len(examples)} {setup_type.upper()} examples")
    return examples


def load_exit_conditions(setup_type: str) -> dict:
    """Load Step 6 exit grinder results."""
    path = f"data/exit_grind/exit_grind_{setup_type}.json"
    if not os.path.exists(path):
        raise FileNotFoundError(f"Exit grinder results not found: {path}")
    with open(path) as f:
        d = json.load(f)

    # Handle different exit grinder output formats
    n_conds = d.get("n_conditions_found") or len(d.get("top_conditions", d.get("results", [])))

    # Normalize: older format used "results" key, newer uses "top_conditions"
    if "top_conditions" not in d and "results" in d:
        d["top_conditions"] = d["results"]

    print(f"Loaded {n_conds} exit conditions from {path}")
    print(f"  Keys: {list(d.keys())}")
    return d


def fetch_ticker_ohlcv(ticker: str, lookback: int = MAX_LOOKBACK) -> pd.DataFrame:
    """Fetch OHLCV from Railway."""
    r = requests.get(f"{RAILWAY_URL}/api/ohlcv/bulk/{ticker}",
                     params={"lookback": lookback})
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


# ============================================================
# Exit Condition Evaluation (subprocess-safe)
# ============================================================

def _eval_signal(args):
    """Evaluate exit condition on one signal's post-signal bars.

    Runs in subprocess — must reimport everything.
    Returns dict with signal info + outcome classification.
    """
    signal, exit_cond, direction, max_forward, lookback = args

    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    import numpy as np
    import pandas as pd
    import requests

    ticker = signal["ticker"]
    signal_date = signal["date"]

    try:
        # Fetch OHLCV
        r = requests.get(f"https://web-production-e3025.up.railway.app/api/ohlcv/bulk/{ticker}",
                         params={"lookback": lookback})
        r.raise_for_status()
        data = r.json()
        if "error" in data or not data.get("results"):
            return {"ticker": ticker, "date": signal_date, "status": "no_data"}

        df = pd.DataFrame(data["results"])
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Find signal date — this is the SCAN candle date.
        # Entry is the NEXT bar's open.
        date_matches = df.index[df["date"] == signal_date].tolist()
        if not date_matches:
            return {"ticker": ticker, "date": signal_date, "status": "date_not_found"}

        scan_idx = date_matches[0]
        entry_idx = scan_idx + 1  # entry bar = day after scan

        if entry_idx >= len(df):
            return {"ticker": ticker, "date": signal_date, "status": "no_entry_bar"}

        n_available = len(df) - entry_idx - 1
        if n_available < 5:
            return {"ticker": ticker, "date": signal_date, "status": "insufficient_bars",
                    "bars_available": n_available}

        actual_forward = min(max_forward, n_available)

        # Compute the exit expression using ExitExprEngine
        from scripts.exit_compute import ExitExprEngine

        engine = ExitExprEngine(df, entry_idx, direction=direction,
                                max_forward=actual_forward)

        compute_spec = exit_cond["compute_spec"]
        series = engine.compute(compute_spec)

        # Check if exit condition triggers
        threshold = exit_cond["threshold"]
        exit_dir = exit_cond["direction"]

        triggered = False
        trigger_bar = -1
        for bar_i in range(1, len(series)):  # skip bar 0 (entry bar itself)
            val = series[bar_i]
            if np.isnan(val):
                continue
            if exit_dir == ">=" and val >= threshold:
                triggered = True
                trigger_bar = bar_i
                break
            elif exit_dir == "<=" and val <= threshold:
                triggered = True
                trigger_bar = bar_i
                break
            elif exit_dir == ">" and val > threshold:
                triggered = True
                trigger_bar = bar_i
                break
            elif exit_dir == "<" and val < threshold:
                triggered = True
                trigger_bar = bar_i
                break

        # Compute move metrics at trigger bar (or end)
        entry_high = df["high"].iloc[entry_idx]
        entry_close = df["close"].iloc[entry_idx]

        if triggered:
            exit_idx = entry_idx + trigger_bar
            exit_close = df["close"].iloc[exit_idx]
        else:
            exit_idx = entry_idx + actual_forward
            exit_close = df["close"].iloc[exit_idx]

        # Compute move (short direction: positive = price went down)
        if direction == "short":
            pct_move = (entry_high - exit_close) / entry_high * 100
        else:
            entry_low = df["low"].iloc[entry_idx]
            pct_move = (exit_close - entry_low) / entry_low * 100

        # Compute MFE
        fwd_slice = slice(entry_idx, entry_idx + actual_forward + 1)
        if direction == "short":
            mfe_price = df["low"].iloc[fwd_slice].min()
            mfe_pct = (entry_high - mfe_price) / entry_high * 100
        else:
            mfe_price = df["high"].iloc[fwd_slice].max()
            entry_low = df["low"].iloc[entry_idx]
            mfe_pct = (mfe_price - entry_low) / entry_low * 100

        capture_eff = pct_move / mfe_pct if mfe_pct > 0 else 0.0

        # ADR at entry for normalization
        adr_vals = []
        lookback_start = max(0, entry_idx - 14)
        for j in range(lookback_start, entry_idx):
            adr_vals.append(df["high"].iloc[j] - df["low"].iloc[j])
        adr_at_entry = np.mean(adr_vals) if adr_vals else 1.0

        if direction == "short":
            adr_captured = (entry_high - exit_close) / adr_at_entry
            mfe_adr = (entry_high - mfe_price) / adr_at_entry
        else:
            entry_low = df["low"].iloc[entry_idx]
            adr_captured = (exit_close - entry_low) / adr_at_entry
            mfe_adr = (mfe_price - entry_low) / adr_at_entry

        return {
            "ticker": ticker,
            "date": signal_date,
            "status": "outcome" if triggered else "non_outcome",
            "exit_triggered": triggered,
            "trigger_bar": trigger_bar if triggered else -1,
            "bars_available": actual_forward,
            "pct_move": round(pct_move, 4),
            "mfe_pct": round(mfe_pct, 4),
            "capture_eff": round(capture_eff, 4),
            "adr_captured": round(adr_captured, 4),
            "mfe_adr": round(mfe_adr, 4),
            "entry_high": round(entry_high, 2),
            "exit_close": round(exit_close, 2),
        }

    except Exception as e:
        return {"ticker": ticker, "date": signal_date, "status": "error",
                "error": str(e)}


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Outcome Grinder — Step 7")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--direction", default="short")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--exit-rank", type=int, default=1,
                        help="Which exit condition to use (1=best, 2=second best, etc.)")
    parser.add_argument("--all-exits", action="store_true",
                        help="Test all exit conditions and show overlap matrix")
    args = parser.parse_args()

    print(f"Outcome Grinder — Step 7")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Workers: {args.workers}")
    print()

    # ── 1. Load data ──
    signals = load_signals(args.setup)
    examples = load_examples(args.setup)
    exit_data = load_exit_conditions(args.setup)

    # Build example set for cross-reference
    example_set = set()
    for ex in examples:
        # Example entryDate is the entry date; signal date is the scan candle = 1 day before
        # But signals are stored by scan date, so we need to match carefully
        example_set.add((ex["ticker"], ex["entryDate"]))

    # ── 2. Select exit condition(s) ──
    exit_conds = exit_data.get("top_conditions", exit_data.get("results", []))
    if not exit_conds:
        print("ERROR: No exit conditions found in Step 6 results.")
        return

    # Normalize field names across different exit grinder output formats
    for ec in exit_conds:
        # Old format used "expr_name", new uses "expression"
        if "expr_name" in ec and "expression" not in ec:
            ec["expression"] = ec["expr_name"]
        # Old format used "above"/"below", new uses ">="/"<="
        if ec.get("direction") == "above":
            ec["direction"] = ">="
        elif ec.get("direction") == "below":
            ec["direction"] = "<="
        # Old format: "floor_capture_eff", new: "floor_efficiency"
        if "floor_capture_eff" in ec and "floor_efficiency" not in ec:
            ec["floor_efficiency"] = ec["floor_capture_eff"]
        # Ensure category exists
        if "category" not in ec:
            ec["category"] = "unknown"

    if args.all_exits:
        selected_exits = exit_conds
        print(f"\nTesting ALL {len(selected_exits)} exit conditions")
    else:
        rank = args.exit_rank - 1
        if rank >= len(exit_conds):
            print(f"ERROR: Only {len(exit_conds)} exit conditions, requested rank {args.exit_rank}")
            return
        selected_exits = [exit_conds[rank]]
        ec = selected_exits[0]
        print(f"\nUsing exit condition #{args.exit_rank}:")
        print(f"  Expression: {ec['expression']}")
        print(f"  Direction: {ec['direction']}")
        print(f"  Threshold: {ec['threshold']}")
        print(f"  Floor capture eff: {ec.get('floor_efficiency', 'N/A')}")

    # ── 3. Build compute spec for exit condition ──
    # Map expression name back to compute spec
    # The exit_grind results store expression names; we need to reconstruct the compute spec
    for ec in selected_exits:
        ec["compute_spec"] = _build_compute_spec(ec["expression"], ec["category"])
        if ec["compute_spec"] is None:
            print(f"  WARNING: Cannot build compute spec for {ec['expression']} — skipping")

    selected_exits = [ec for ec in selected_exits if ec.get("compute_spec") is not None]
    if not selected_exits:
        print("ERROR: No valid exit conditions after compute spec resolution.")
        return

    # ── 4. Process each exit condition ──
    for exit_idx, ec in enumerate(selected_exits):
        if len(selected_exits) > 1:
            print(f"\n{'='*80}")
            print(f"Exit condition {exit_idx+1}/{len(selected_exits)}: "
                  f"{ec['expression']} {ec['direction']} {ec['threshold']}")
            print(f"{'='*80}")

        exit_cond = {
            "expression": ec["expression"],
            "direction": ec["direction"],
            "threshold": ec["threshold"],
            "compute_spec": ec["compute_spec"],
        }

        # ── 5. Run evaluation in parallel ──
        tasks = [
            (sig, exit_cond, args.direction, args.max_forward, MAX_LOOKBACK)
            for sig in signals
        ]

        print(f"\nEvaluating {len(signals)} signals ({args.workers} workers)...")
        t0 = time.time()

        results = []
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_eval_signal, task): task[0] for task in tasks}
            done = 0
            for future in as_completed(futures):
                done += 1
                try:
                    result = future.result()
                    results.append(result)
                    if done % 50 == 0 or done == len(signals):
                        outcomes = sum(1 for r in results if r.get("status") == "outcome")
                        non_outcomes = sum(1 for r in results if r.get("status") == "non_outcome")
                        errors = sum(1 for r in results if r.get("status") in ("error", "no_data", "date_not_found"))
                        print(f"  [{done}/{len(signals)}] "
                              f"outcomes={outcomes}, non-outcomes={non_outcomes}, "
                              f"errors={errors}")
                except Exception as e:
                    done_sig = futures[future]
                    results.append({
                        "ticker": done_sig["ticker"],
                        "date": done_sig["date"],
                        "status": "error",
                        "error": str(e),
                    })

        elapsed = time.time() - t0
        print(f"\nDone in {elapsed:.1f}s ({len(signals)/elapsed:.1f} signals/s)")

        # ── 6. Classify and report ──
        outcome_signals = [r for r in results if r.get("status") == "outcome"]
        non_outcome_signals = [r for r in results if r.get("status") == "non_outcome"]
        error_signals = [r for r in results
                         if r.get("status") in ("error", "no_data", "date_not_found",
                                                 "no_entry_bar", "insufficient_bars")]

        print(f"\n{'='*80}")
        print(f"OUTCOME CLASSIFICATION — {ec['expression']} {ec['direction']} {ec['threshold']}")
        print(f"{'='*80}")
        print(f"  Total signals:     {len(signals)}")
        print(f"  OUTCOME signals:   {len(outcome_signals)} ({len(outcome_signals)/len(signals)*100:.1f}%)")
        print(f"  Non-outcome:       {len(non_outcome_signals)} ({len(non_outcome_signals)/len(signals)*100:.1f}%)")
        print(f"  Errors/skipped:    {len(error_signals)}")

        # Check which examples are in the outcome set
        example_outcomes = []
        example_missing = []
        for ex in examples:
            # Match by ticker + entry date being 1 day after scan date
            found = False
            for r in outcome_signals:
                if r["ticker"] == ex["ticker"]:
                    # Signal date is scan date, example entryDate is entry date
                    # They should be consecutive trading days
                    found = True
                    example_outcomes.append(r)
                    break
            if not found:
                example_missing.append(ex)

        print(f"\n  Examples in outcomes: {len(example_outcomes)}/{len(examples)}")
        if example_missing:
            print(f"  Examples MISSING from outcomes:")
            for ex in example_missing:
                print(f"    {ex['ticker']} {ex['entryDate']}")

        # Stats on outcome signals
        if outcome_signals:
            pct_moves = [r["pct_move"] for r in outcome_signals if "pct_move" in r]
            adr_caps = [r["adr_captured"] for r in outcome_signals if "adr_captured" in r]
            trigger_bars = [r["trigger_bar"] for r in outcome_signals if r.get("trigger_bar", -1) > 0]
            cap_effs = [r["capture_eff"] for r in outcome_signals if "capture_eff" in r]

            print(f"\n  Outcome signal stats:")
            print(f"    % Move:      floor={min(pct_moves):+.2f}%  median={np.median(pct_moves):+.2f}%  "
                  f"avg={np.mean(pct_moves):+.2f}%")
            print(f"    ADR captured: floor={min(adr_caps):+.2f}  median={np.median(adr_caps):+.2f}  "
                  f"avg={np.mean(adr_caps):+.2f}")
            print(f"    Trigger bar: avg={np.mean(trigger_bars):.1f}  "
                  f"median={np.median(trigger_bars):.0f}  max={max(trigger_bars)}")
            print(f"    Capture eff: floor={min(cap_effs):.3f}  median={np.median(cap_effs):.3f}  "
                  f"avg={np.mean(cap_effs):.3f}")

        # Stats on non-outcome signals
        if non_outcome_signals:
            pct_moves_no = [r["pct_move"] for r in non_outcome_signals if "pct_move" in r]
            adr_caps_no = [r["adr_captured"] for r in non_outcome_signals if "adr_captured" in r]
            if pct_moves_no:
                print(f"\n  Non-outcome signal stats (at end of window):")
                print(f"    % Move:      floor={min(pct_moves_no):+.2f}%  median={np.median(pct_moves_no):+.2f}%  "
                      f"avg={np.mean(pct_moves_no):+.2f}%")
                print(f"    ADR captured: floor={min(adr_caps_no):+.2f}  median={np.median(adr_caps_no):+.2f}  "
                      f"avg={np.mean(adr_caps_no):+.2f}")

        # Win rate preview
        total_classified = len(outcome_signals) + len(non_outcome_signals)
        if total_classified > 0:
            win_rate = len(outcome_signals) / total_classified
            print(f"\n  Preliminary win rate: {win_rate:.1%} "
                  f"({len(outcome_signals)}/{total_classified} classified signals)")

        # Daily distribution of outcome signals
        if outcome_signals:
            from collections import Counter
            daily_counts = Counter()
            for r in outcome_signals:
                daily_counts[r["date"]] += 1
            peak_day = max(daily_counts.values())
            avg_day = np.mean(list(daily_counts.values()))
            print(f"\n  Outcome signal distribution:")
            print(f"    Total days with outcomes: {len(daily_counts)}")
            print(f"    Peak outcomes/day: {peak_day}")
            print(f"    Avg outcomes/day: {avg_day:.1f}")

        # ── 7. Save results ──
        os.makedirs("data/outcome_grind", exist_ok=True)
        outpath = f"data/outcome_grind/outcome_signals_{args.setup}.json"

        output = {
            "setup_type": args.setup,
            "direction": args.direction,
            "max_forward": args.max_forward,
            "exit_condition": {
                "expression": ec["expression"],
                "direction": ec["direction"],
                "threshold": ec["threshold"],
                "category": ec["category"],
                "floor_efficiency": ec["floor_efficiency"],
            },
            "summary": {
                "total_signals": len(signals),
                "outcome_signals": len(outcome_signals),
                "non_outcome_signals": len(non_outcome_signals),
                "errors": len(error_signals),
                "win_rate": len(outcome_signals) / total_classified if total_classified > 0 else 0,
                "examples_in_outcomes": len(example_outcomes),
                "examples_total": len(examples),
            },
            "outcome_signals": sorted(
                [r for r in outcome_signals],
                key=lambda r: r["date"]
            ),
            "non_outcome_signals": sorted(
                [r for r in non_outcome_signals],
                key=lambda r: r["date"]
            ),
            "error_signals": error_signals,
        }

        # Convert numpy types for JSON serialization
        def convert(obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            elif isinstance(obj, (np.floating,)):
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            return obj

        with open(outpath, "w") as f:
            json.dump(output, f, indent=2, default=convert)
        print(f"\nResults saved to {outpath}")

        # Print top 10 outcome signals by ADR captured
        if outcome_signals:
            top_outcomes = sorted(outcome_signals, key=lambda r: r.get("adr_captured", 0), reverse=True)
            print(f"\n{'─'*80}")
            print(f"TOP 10 OUTCOME SIGNALS by ADR captured:")
            print(f"{'─'*80}")
            print(f"{'Ticker':8s} {'Date':12s} {'%Move':>8s} {'ADR':>8s} {'MFE%':>8s} "
                  f"{'CapEff':>8s} {'Bar#':>5s}")
            for r in top_outcomes[:10]:
                print(f"{r['ticker']:8s} {r['date']:12s} {r['pct_move']:+7.2f}% "
                      f"{r['adr_captured']:+7.2f} {r['mfe_pct']:+7.2f}% "
                      f"{r['capture_eff']:7.3f} {r['trigger_bar']:5d}")

        # Print top 10 NON-outcome signals (the ones that fizzled most)
        if non_outcome_signals:
            worst = sorted(non_outcome_signals, key=lambda r: r.get("pct_move", 0))
            print(f"\n{'─'*80}")
            print(f"TOP 10 NON-OUTCOME SIGNALS (worst fizzles):")
            print(f"{'─'*80}")
            print(f"{'Ticker':8s} {'Date':12s} {'%Move':>8s} {'ADR':>8s} {'MFE%':>8s} "
                  f"{'Bars':>5s}")
            for r in worst[:10]:
                bars = r.get("bars_available", "?")
                print(f"{r['ticker']:8s} {r['date']:12s} {r.get('pct_move',0):+7.2f}% "
                      f"{r.get('adr_captured',0):+7.2f} {r.get('mfe_pct',0):+7.2f}% "
                      f"{bars:>5}")


def _build_compute_spec(expression_name: str, category: str) -> dict:
    """Reconstruct compute spec from expression name.

    The exit_grind results store expression names, but the ExitExprEngine
    needs a compute spec dict. This reverses the naming convention from
    exit_expressions.py.
    """
    # ── MA reclaim expressions ──
    if expression_name.startswith("bars_since_reclaim_"):
        ma = expression_name.replace("bars_since_reclaim_", "")
        return {"op": "bars_since_reclaim", "ma": ma}

    if expression_name.startswith("close_above_"):
        ma = expression_name.replace("close_above_", "")
        return {"op": "close_above_ma", "ma": ma}

    if expression_name.startswith("bars_since_touch_"):
        ma = expression_name.replace("bars_since_touch_", "")
        return {"op": "bars_since_touch_ma", "ma": ma}

    if expression_name.startswith("reclaim_then_lost_"):
        ma = expression_name.replace("reclaim_then_lost_", "")
        return {"op": "reclaim_then_lost", "ma": ma}

    # ── Time ──
    if expression_name == "bars_since_signal":
        return {"op": "bars_since_signal"}

    # ── Move captured ──
    if expression_name.startswith("move_captured_close_"):
        norm = expression_name.replace("move_captured_close_", "")
        return {"op": "move_captured", "price": "close", "normalizer": norm}

    if expression_name.startswith("move_captured_low_"):
        norm = expression_name.replace("move_captured_low_", "")
        return {"op": "move_captured", "price": "low", "normalizer": norm}

    # ── MFE ──
    if expression_name.startswith("mfe_close_"):
        norm = expression_name.replace("mfe_close_", "")
        return {"op": "mfe", "price": "close", "normalizer": norm}

    if expression_name.startswith("mfe_low_"):
        norm = expression_name.replace("mfe_low_", "")
        return {"op": "mfe", "price": "low", "normalizer": norm}

    # ── Extension from MA ──
    # Pattern: ext_{ma}_{norm}  e.g. ext_avgc50_adr14
    if expression_name.startswith("ext_") and not expression_name.startswith("ext_ceil"):
        parts = expression_name.split("_")
        # ext_avgc50_adr14 → ma=avgc50, normalizer=adr14
        # ext_xavgc21_adr14 → ma=xavgc21, normalizer=adr14
        if len(parts) >= 3:
            ma = parts[1]
            norm = parts[2]
            return {"op": "extension_from_ma", "ma": ma, "normalizer": norm}

    # ── Distance from MA ──
    if expression_name.startswith("distance_from_"):
        rest = expression_name.replace("distance_from_", "")
        parts = rest.split("_")
        if len(parts) >= 2:
            ma = parts[0]
            norm = "_".join(parts[1:])
            return {"op": "distance_from_ma", "ma": ma, "normalizer": norm}

    # ── Extension ceiling ratio ──
    if expression_name.startswith("ext_ceil_"):
        rest = expression_name.replace("ext_ceil_", "")
        # ext_ceil_avgc50_adr14_lb252
        parts = rest.split("_")
        if len(parts) >= 3 and parts[-1].startswith("lb"):
            lookback = int(parts[-1][2:])
            norm = parts[-2]
            ma = "_".join(parts[:-2])
            return {"op": "ext_ceiling_ratio", "ma": ma, "normalizer": norm,
                    "lookback": lookback}

    # ── Momentum ──
    if expression_name.startswith("rsi_"):
        parts = expression_name.split("_")
        if "slope" in parts:
            # rsi_14_slope_3
            period = int(parts[1])
            offset = int(parts[3])
            return {"op": "rsi_slope", "period": period, "offset": offset}
        else:
            period = int(parts[1])
            return {"op": "rsi", "period": period}

    if expression_name.startswith("roc_"):
        period = int(expression_name.replace("roc_", ""))
        return {"op": "roc", "period": period}

    if expression_name.startswith("stoch_"):
        period = int(expression_name.replace("stoch_", ""))
        return {"op": "stochastic", "period": period}

    if expression_name.startswith("adx_") and "slope" not in expression_name:
        period = int(expression_name.replace("adx_", ""))
        return {"op": "adx", "period": period}

    if expression_name.startswith("di_spread_"):
        period = int(expression_name.replace("di_spread_", ""))
        return {"op": "di_spread", "period": period}

    if expression_name.startswith("macd_hist_slope_"):
        parts = expression_name.split("_")
        # macd_hist_slope_12_26_9_3b
        return {"op": "macd_histogram_slope",
                "fast": int(parts[3]), "slow": int(parts[4]),
                "signal": int(parts[5]), "offset": int(parts[6].rstrip("b"))}

    if expression_name.startswith("macd_hist_"):
        parts = expression_name.split("_")
        # macd_hist_12_26_9
        return {"op": "macd_histogram",
                "fast": int(parts[2]), "slow": int(parts[3]),
                "signal": int(parts[4])}

    # ── Volume ──
    if expression_name.startswith("rvol_"):
        period = int(expression_name.replace("rvol_", ""))
        return {"op": "rvol", "avg_period": period}

    # ── Bollinger ──
    if expression_name.startswith("bb_pctb_"):
        period = int(expression_name.replace("bb_pctb_", ""))
        return {"op": "bollinger_pctb", "period": period}

    # ── Relative strength ──
    if expression_name.startswith("rs_vs_spy_"):
        period = int(expression_name.replace("rs_vs_spy_", ""))
        return {"op": "rs_vs_spy", "period": period}

    # ── Bar range ──
    if expression_name.startswith("bar_range_"):
        norm = expression_name.replace("bar_range_", "")
        return {"op": "bar_range", "normalizer": norm}

    # ── Capture efficiency ──
    if expression_name == "capture_efficiency":
        return {"op": "capture_efficiency"}

    # ── Retrace from MFE ──
    if expression_name == "retrace_from_mfe_pct_raw":
        return {"op": "retrace_from_mfe_pct"}

    print(f"  WARNING: Unknown expression name pattern: {expression_name}")
    return None


if __name__ == "__main__":
    main()
