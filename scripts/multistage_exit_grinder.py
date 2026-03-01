"""
Multi-Stage Exit Grinder — Task 3.7

Grinds for optimal multi-stage conditional exits with position sizing.
Separate from single-stage exit_grinder.py — both systems coexist.

Architecture:
    Stage 1 (Capital Protection): Fires early on duds (stocks that stall/reverse).
        - Has a max_window ceiling (e.g. 20 bars). If not triggered, stock is a runner.
        - Trims configurable % of position (grinder discovers optimal trim).
    
    Stage 2 (Partial Profit): Locks in gains once move is confirmed.
        - Gated by MFE threshold (e.g. must have moved ≥ 2 ADR before activating).
        - Condition triggers partial exit. Trims configurable % of remaining.
    
    Stage 3 (Trailing Exit): Rides the remainder until trend breaks.
        - Gated by time or MFE. Condition exits remaining position.
        - Hard backstop at max_forward bars.

Grind Strategy:
    Phase A: Independent stage discovery (parallel, fast)
        - Grind each stage's conditions independently with stage-appropriate scoring
    Phase B: Joint optimization (combinatorial)
        - Top-K from each stage → test all K³ combinations
        - Score by aggregate weighted capture efficiency
        - 100% example pass rule applies to COMBINED system

Scoring:
    effective_capture = Σ (trim_pct_i × captured_move_at_exit_i)
    capture_efficiency = effective_capture / MFE

Usage:
    python scripts/multistage_exit_grinder.py --setup dtss
    python scripts/multistage_exit_grinder.py --setup dtss --phase A   # independent only
    python scripts/multistage_exit_grinder.py --setup dtss --phase B   # joint only (needs Phase A results)
    python scripts/multistage_exit_grinder.py --setup dtss --workers 12
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
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product as itertools_product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.exit_expressions import (
    generate_exit_expressions, generate_exit_boolean_conditions,
    WINDOWS,
)

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
MAX_FORWARD_DEFAULT = 120
DEFAULT_WORKERS = os.cpu_count() or 8

LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

# Grindable parameter sets
TRIM_PCTS = [0.33, 0.50, 0.75, 1.0]
STAGE1_MAX_WINDOWS = [10, 15, 20, 25]
STAGE2_MFE_GATES_ADR = [1.0, 1.5, 2.0, 2.5, 3.0]
STAGE3_MFE_GATES_ADR = [2.0, 3.0, 4.0, 5.0]
STAGE3_BAR_GATES = [15, 20, 30, 40]

# Phase A: how many top candidates per stage to pass to Phase B
TOP_K_PER_STAGE = 15


# ============================================================
# Data Classes
# ============================================================

@dataclass
class ExampleData:
    """Loaded example with OHLCV and computed forward data."""
    id: int
    ticker: str
    entry_date: str
    df: pd.DataFrame
    entry_idx: int
    entry_high: float
    entry_low: float
    n_forward: int
    mfe_pct: float
    mfe_adr: float          # MFE in ADR units
    adr_at_entry: float
    direction: str
    # Pre-computed forward arrays (set after matrix build)
    fwd_close: np.ndarray = field(default=None, repr=False)
    fwd_low: np.ndarray = field(default=None, repr=False)
    fwd_high: np.ndarray = field(default=None, repr=False)


@dataclass
class StageCondition:
    """A single stage's exit condition."""
    expr_name: str
    direction: str           # "above" or "below"
    threshold: float


@dataclass
class StageConfig:
    """Full configuration for one exit stage."""
    stage_id: int            # 1, 2, or 3
    condition: StageCondition
    trim_pct: float          # fraction of position to trim (of remaining)
    gate_type: str           # "none", "mfe_adr", "bars_min"
    gate_value: float        # MFE ADR threshold or min bars
    max_window: Optional[int]  # max bars to look for trigger (Stage 1)


@dataclass
class StageExitEvent:
    """Records when/how a stage fired for one example."""
    stage_id: int
    bar: int                 # forward bar index where condition triggered
    price: float             # close price at exit
    pct_trimmed: float       # fraction of TOTAL position trimmed this event
    pct_move: float          # % move at this exit point
    remaining_after: float   # remaining position after this trim


@dataclass
class MultiStageResult:
    """Full result of a multi-stage exit configuration across all examples."""
    stages: List[StageConfig]
    per_example: List[dict]
    median_capture_eff: float
    avg_capture_eff: float
    floor_capture_eff: float
    median_effective_pct: float
    avg_bars_to_full_exit: float
    all_examples_complete: bool


# ============================================================
# Data Loading (reused from exit_grinder.py pattern)
# ============================================================

def load_5yr_cache():
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
    r = requests.get(f"{RAILWAY_URL}/api/examples/{setup_type}")
    r.raise_for_status()
    data = r.json()
    examples = data["examples"]
    print(f"Loaded {len(examples)} {setup_type.upper()} examples")
    return examples


def build_example_data(example: dict, direction: str, max_forward: int,
                       universe_cache: dict) -> Optional[ExampleData]:
    """Build ExampleData with pre-computed forward arrays."""
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
        entry_low = df["low"].iloc[entry_idx]

        # Compute ADR at entry
        from scripts.expression_engine import ExpressionEngine
        eng = ExpressionEngine(df)
        adr_series = eng._adr(14).values
        adr_at_entry = adr_series[entry_idx]
        if np.isnan(adr_at_entry) or adr_at_entry <= 0:
            adr_at_entry = 1.0

        # Forward arrays
        fwd_close = df["close"].values[entry_idx:entry_idx + actual_forward + 1]
        fwd_low = df["low"].values[entry_idx:entry_idx + actual_forward + 1]
        fwd_high = df["high"].values[entry_idx:entry_idx + actual_forward + 1]

        # MFE
        if direction == "short":
            mfe_price = np.min(fwd_low)
            mfe_raw = entry_high - mfe_price
            mfe_pct = mfe_raw / entry_high * 100
        else:
            mfe_price = np.max(fwd_high)
            mfe_raw = mfe_price - entry_low
            mfe_pct = mfe_raw / entry_low * 100

        mfe_adr = mfe_raw / adr_at_entry

        return ExampleData(
            id=example["id"],
            ticker=ticker,
            entry_date=entry_date,
            df=df,
            entry_idx=entry_idx,
            entry_high=entry_high,
            entry_low=entry_low,
            n_forward=actual_forward,
            mfe_pct=mfe_pct,
            mfe_adr=mfe_adr,
            adr_at_entry=adr_at_entry,
            direction=direction,
            fwd_close=fwd_close,
            fwd_low=fwd_low,
            fwd_high=fwd_high,
        )
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")
        return None


# ============================================================
# Expression Matrix Building (same parallel pattern as exit_grinder)
# ============================================================

def _build_one_example_matrix(args):
    """Build expression matrix for one example. Runs in subprocess."""
    ex_dict, expressions, direction, spy_pickle = args

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
    for expr in expressions:
        try:
            series = engine.compute(expr["compute"])
            result[expr["name"]] = series
        except Exception:
            failed += 1

    return ex_dict["ticker"], result, failed, len(expressions)


def build_all_matrices_parallel(examples: list, expressions: list,
                                direction: str, spy_df: pd.DataFrame,
                                workers: int) -> list:
    """Build expression matrices for all examples in parallel."""
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
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_build_one_example_matrix, task): i
                   for i, task in enumerate(tasks)}
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                ticker, matrix, failed, total = future.result()
                matrices[idx] = matrix
                done += 1
                computed = total - failed
                print(f"  [{done}/{len(examples)}] {ticker:8s} — "
                      f"{computed}/{total} computed"
                      + (f" ({failed} failed)" if failed else ""))
            except Exception as e:
                done += 1
                print(f"  [{done}/{len(examples)}] ERROR: {e}")

    elapsed = time.time() - t0
    print(f"Matrix build: {elapsed:.1f}s ({workers} workers)")
    return matrices


def compute_boolean_aggregations(base_matrices, native_bools, threshold_bools,
                                 n_forwards, expr_name_to_idx=None):
    """Compute boolean aggregation expressions from base series.
    
    Delegates to exit_grinder's implementation for parity.
    """
    from scripts.exit_grinder import compute_boolean_aggregations as _orig_agg
    return _orig_agg(base_matrices, native_bools, threshold_bools, n_forwards)


# ============================================================
# Multi-Stage Simulator
# ============================================================

class MultiStageSimulator:
    """Simulates a multi-stage exit on one example's expression matrix.
    
    Given stage configs and an example's expression series, walks forward
    bar-by-bar and applies stages in order with position tracking.
    """

    @staticmethod
    def simulate_example(stages: List[StageConfig], matrix: dict,
                         example: ExampleData) -> Tuple[List[StageExitEvent], float, float]:
        """Simulate multi-stage exit for one example.
        
        Returns:
            (events, effective_capture_pct, capture_efficiency)
            
        Rules:
            - Stages checked in order (1 → 2 → 3)
            - Each stage can only fire ONCE
            - Stage 1 has max_window — stops checking after that bar
            - Stage 2/3 have gates (MFE threshold or bar minimum)
            - Remaining position exits at max_forward if Stage 3 doesn't fire
            - effective_capture = Σ (trim_pct * pct_move_at_exit)
        """
        n_fwd = example.n_forward
        direction = example.direction
        entry_high = example.entry_high
        entry_low = example.entry_low
        adr = example.adr_at_entry
        fwd_close = example.fwd_close
        fwd_low = example.fwd_low
        fwd_high = example.fwd_high

        # Running MFE in ADR units
        if direction == "short":
            running_mfe_price = np.minimum.accumulate(fwd_low)
            running_mfe_adr = (entry_high - running_mfe_price) / adr
        else:
            running_mfe_price = np.maximum.accumulate(fwd_high)
            running_mfe_adr = (running_mfe_price - entry_low) / adr

        events = []
        remaining_position = 1.0
        stages_fired = set()

        for bar in range(1, n_fwd + 1):
            if bar >= len(fwd_close):
                break
            if remaining_position <= 0.001:
                break

            mfe_adr_now = running_mfe_adr[bar] if bar < len(running_mfe_adr) else 0.0

            for stage in stages:
                if stage.stage_id in stages_fired:
                    continue
                if remaining_position <= 0.001:
                    break

                # Check max_window (Stage 1)
                if stage.max_window is not None and bar > stage.max_window:
                    continue

                # Check gate
                if stage.gate_type == "mfe_adr" and mfe_adr_now < stage.gate_value:
                    continue
                if stage.gate_type == "bars_min" and bar < stage.gate_value:
                    continue

                # Check condition
                cond = stage.condition
                series = matrix.get(cond.expr_name)
                if series is None or bar >= len(series):
                    continue
                val = series[bar]
                if np.isnan(val):
                    continue

                triggered = False
                if cond.direction == "above" and val > cond.threshold:
                    triggered = True
                elif cond.direction == "below" and val < cond.threshold:
                    triggered = True

                if triggered:
                    # Compute move at this bar
                    exit_price = fwd_close[bar]
                    if direction == "short":
                        pct_move = (entry_high - exit_price) / entry_high * 100
                    else:
                        pct_move = (exit_price - entry_low) / entry_low * 100

                    # Trim
                    actual_trim = min(stage.trim_pct, 1.0)
                    pct_of_total = remaining_position * actual_trim
                    remaining_after = remaining_position - pct_of_total

                    events.append(StageExitEvent(
                        stage_id=stage.stage_id,
                        bar=bar,
                        price=exit_price,
                        pct_trimmed=pct_of_total,
                        pct_move=pct_move,
                        remaining_after=remaining_after,
                    ))

                    remaining_position = remaining_after
                    stages_fired.add(stage.stage_id)
                    break  # only one stage fires per bar

        # Backstop: if position remains, exit at last bar
        if remaining_position > 0.001:
            last_bar = min(n_fwd, len(fwd_close) - 1)
            exit_price = fwd_close[last_bar]
            if direction == "short":
                pct_move = (entry_high - exit_price) / entry_high * 100
            else:
                pct_move = (exit_price - entry_low) / entry_low * 100

            events.append(StageExitEvent(
                stage_id=99,  # backstop
                bar=last_bar,
                price=exit_price,
                pct_trimmed=remaining_position,
                pct_move=pct_move,
                remaining_after=0.0,
            ))

        # Compute effective capture
        effective_pct = sum(e.pct_trimmed * e.pct_move for e in events)
        capture_eff = effective_pct / example.mfe_pct if example.mfe_pct > 0 else 0.0

        return events, effective_pct, capture_eff

    @staticmethod
    def simulate_all(stages: List[StageConfig], matrices: list,
                     examples: list) -> Optional[MultiStageResult]:
        """Simulate multi-stage exit across all examples.
        
        Returns None if any example fails to have all non-backstop stages fire.
        100% pass rule: every example must complete through the stage system.
        """
        per_example = []
        capture_effs = []
        effective_pcts = []
        bars_to_full = []
        all_complete = True

        for i, ex in enumerate(examples):
            matrix = matrices[i]
            if matrix is None:
                all_complete = False
                continue

            events, eff_pct, cap_eff = MultiStageSimulator.simulate_example(
                stages, matrix, ex
            )

            last_bar = max(e.bar for e in events) if events else 0

            per_example.append({
                "ticker": ex.ticker,
                "entry_date": ex.entry_date,
                "mfe_pct": ex.mfe_pct,
                "mfe_adr": ex.mfe_adr,
                "effective_pct": eff_pct,
                "capture_eff": cap_eff,
                "events": [
                    {
                        "stage_id": e.stage_id,
                        "bar": e.bar,
                        "price": e.price,
                        "pct_trimmed": e.pct_trimmed,
                        "pct_move": e.pct_move,
                    }
                    for e in events
                ],
                "bars_to_full_exit": last_bar,
            })

            capture_effs.append(cap_eff)
            effective_pcts.append(eff_pct)
            bars_to_full.append(last_bar)

        if not capture_effs:
            return None

        return MultiStageResult(
            stages=stages,
            per_example=per_example,
            median_capture_eff=float(np.median(capture_effs)),
            avg_capture_eff=float(np.mean(capture_effs)),
            floor_capture_eff=float(np.min(capture_effs)),
            median_effective_pct=float(np.median(effective_pcts)),
            avg_bars_to_full_exit=float(np.mean(bars_to_full)),
            all_examples_complete=all_complete,
        )


# ============================================================
# Phase A: Independent Stage Discovery
# ============================================================

def classify_examples_by_mfe(examples: list, dud_threshold_adr: float = 1.5) -> Tuple[list, list]:
    """Split examples into duds and runners by MFE in ADR.
    
    Duds: MFE < dud_threshold_adr (the move never really got going)
    Runners: MFE >= dud_threshold_adr
    """
    duds = [i for i, ex in enumerate(examples) if ex.mfe_adr < dud_threshold_adr]
    runners = [i for i, ex in enumerate(examples) if ex.mfe_adr >= dud_threshold_adr]
    return duds, runners


def grind_stage1(examples: list, matrices: list, expr_names: list,
                 direction: str, n_thresholds: int = 20,
                 top_k: int = TOP_K_PER_STAGE) -> list:
    """Grind Stage 1: Capital Protection.
    
    Goal: find conditions that fire EARLY on duds while NOT firing on runners
    during their initial move.
    
    Score: For each condition, simulate it as a standalone Stage 1 with
    various trim_pcts and max_windows. Score by:
        - Must trigger on ALL examples (100% pass)
        - Median capture efficiency of the combined system
        - Prefer conditions that fire earlier on low-MFE examples
    """
    n_examples = len(examples)
    print(f"\n{'='*80}")
    print(f"PHASE A — STAGE 1 GRIND (Capital Protection)")
    print(f"{'='*80}")
    print(f"  {n_examples} examples, {len(expr_names)} expressions")

    candidates = []
    t0 = time.time()
    tested = 0

    for expr_i, expr_name in enumerate(expr_names):
        if (expr_i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{expr_i+1}/{len(expr_names)}] {rate:.0f} expr/s, "
                  f"{len(candidates)} candidates")

        # Gather series across examples
        example_series = []
        all_values = []
        for i, matrix in enumerate(matrices):
            if matrix is not None and expr_name in matrix:
                series = matrix[expr_name]
                example_series.append((i, series))
                vals = series[~np.isnan(series)]
                if len(vals) > 0:
                    all_values.append(vals)

        if len(example_series) < n_examples:
            continue
        if not all_values:
            continue

        combined = np.concatenate(all_values)
        pcts = np.linspace(5, 95, n_thresholds)
        thresholds = list(set(round(float(t), 6) for t in np.percentile(combined, pcts)))
        if not thresholds:
            continue

        for thresh in thresholds:
            for dir_test in ["above", "below"]:
                tested += 1

                # Find first trigger bar per example
                trigger_bars = {}
                for ex_idx, series in example_series:
                    for bar in range(1, len(series)):
                        val = series[bar]
                        if np.isnan(val):
                            continue
                        hit = (val > thresh) if dir_test == "above" else (val < thresh)
                        if hit:
                            trigger_bars[ex_idx] = bar
                            break

                # Must trigger on ALL examples at some point
                if len(trigger_bars) < n_examples:
                    continue

                # Test with different max_windows and trim_pcts
                cond = StageCondition(expr_name, dir_test, thresh)

                for max_win in STAGE1_MAX_WINDOWS:
                    for trim_pct in TRIM_PCTS:
                        stage = StageConfig(
                            stage_id=1,
                            condition=cond,
                            trim_pct=trim_pct,
                            gate_type="none",
                            gate_value=0.0,
                            max_window=max_win,
                        )

                        # Quick simulate: just Stage 1 + backstop
                        result = MultiStageSimulator.simulate_all(
                            [stage], matrices, examples
                        )

                        if result is None:
                            continue

                        candidates.append({
                            "stage_config": stage,
                            "result": result,
                            "trigger_bars": dict(trigger_bars),
                        })

    elapsed = time.time() - t0
    print(f"\n  Stage 1: tested {tested:,} conditions in {elapsed:.1f}s")
    print(f"  {len(candidates)} valid candidates")

    # Sort by median capture eff (higher = better)
    candidates.sort(key=lambda c: c["result"].median_capture_eff, reverse=True)
    top = candidates[:top_k]

    if top:
        print(f"\n  Top {len(top)} Stage 1 candidates:")
        for i, c in enumerate(top):
            sc = c["stage_config"]
            r = c["result"]
            print(f"    #{i+1}: {sc.condition.expr_name} {sc.condition.direction} "
                  f"{sc.condition.threshold:.4f} | trim={sc.trim_pct:.0%} "
                  f"maxwin={sc.max_window} | median_eff={r.median_capture_eff:.3f} "
                  f"floor={r.floor_capture_eff:.3f}")

    return top


def grind_stage2(examples: list, matrices: list, expr_names: list,
                 direction: str, n_thresholds: int = 20,
                 top_k: int = TOP_K_PER_STAGE) -> list:
    """Grind Stage 2: Partial Profit.
    
    Goal: find conditions that trigger near peak profit on confirmed movers.
    Gated by MFE threshold — only activates once move is confirmed.
    
    Score by median capture efficiency when used as standalone Stage 2
    (no Stage 1, just gate + condition + backstop).
    """
    n_examples = len(examples)
    print(f"\n{'='*80}")
    print(f"PHASE A — STAGE 2 GRIND (Partial Profit)")
    print(f"{'='*80}")
    print(f"  {n_examples} examples, {len(expr_names)} expressions")

    candidates = []
    t0 = time.time()
    tested = 0

    for expr_i, expr_name in enumerate(expr_names):
        if (expr_i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{expr_i+1}/{len(expr_names)}] {rate:.0f} expr/s, "
                  f"{len(candidates)} candidates")

        example_series = []
        all_values = []
        for i, matrix in enumerate(matrices):
            if matrix is not None and expr_name in matrix:
                series = matrix[expr_name]
                example_series.append((i, series))
                vals = series[~np.isnan(series)]
                if len(vals) > 0:
                    all_values.append(vals)

        if len(example_series) < n_examples:
            continue
        if not all_values:
            continue

        combined = np.concatenate(all_values)
        pcts = np.linspace(5, 95, n_thresholds)
        thresholds = list(set(round(float(t), 6) for t in np.percentile(combined, pcts)))
        if not thresholds:
            continue

        for thresh in thresholds:
            for dir_test in ["above", "below"]:
                tested += 1

                cond = StageCondition(expr_name, dir_test, thresh)

                for mfe_gate in STAGE2_MFE_GATES_ADR:
                    for trim_pct in TRIM_PCTS:
                        stage = StageConfig(
                            stage_id=2,
                            condition=cond,
                            trim_pct=trim_pct,
                            gate_type="mfe_adr",
                            gate_value=mfe_gate,
                            max_window=None,
                        )

                        result = MultiStageSimulator.simulate_all(
                            [stage], matrices, examples
                        )

                        if result is None:
                            continue

                        candidates.append({
                            "stage_config": stage,
                            "result": result,
                        })

    elapsed = time.time() - t0
    print(f"\n  Stage 2: tested {tested:,} conditions in {elapsed:.1f}s")
    print(f"  {len(candidates)} valid candidates")

    candidates.sort(key=lambda c: c["result"].median_capture_eff, reverse=True)
    top = candidates[:top_k]

    if top:
        print(f"\n  Top {len(top)} Stage 2 candidates:")
        for i, c in enumerate(top):
            sc = c["stage_config"]
            r = c["result"]
            print(f"    #{i+1}: {sc.condition.expr_name} {sc.condition.direction} "
                  f"{sc.condition.threshold:.4f} | trim={sc.trim_pct:.0%} "
                  f"mfe_gate={sc.gate_value:.1f}ADR | median_eff={r.median_capture_eff:.3f} "
                  f"floor={r.floor_capture_eff:.3f}")

    return top


def grind_stage3(examples: list, matrices: list, expr_names: list,
                 direction: str, n_thresholds: int = 20,
                 top_k: int = TOP_K_PER_STAGE) -> list:
    """Grind Stage 3: Trailing Exit.
    
    Goal: find conditions that ride the trend until it breaks.
    Gated by MFE or time — only for extended moves.
    This stage always exits 100% of remaining position.
    """
    n_examples = len(examples)
    print(f"\n{'='*80}")
    print(f"PHASE A — STAGE 3 GRIND (Trailing Exit)")
    print(f"{'='*80}")
    print(f"  {n_examples} examples, {len(expr_names)} expressions")

    candidates = []
    t0 = time.time()
    tested = 0

    for expr_i, expr_name in enumerate(expr_names):
        if (expr_i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{expr_i+1}/{len(expr_names)}] {rate:.0f} expr/s, "
                  f"{len(candidates)} candidates")

        example_series = []
        all_values = []
        for i, matrix in enumerate(matrices):
            if matrix is not None and expr_name in matrix:
                series = matrix[expr_name]
                example_series.append((i, series))
                vals = series[~np.isnan(series)]
                if len(vals) > 0:
                    all_values.append(vals)

        if len(example_series) < n_examples:
            continue
        if not all_values:
            continue

        combined = np.concatenate(all_values)
        pcts = np.linspace(5, 95, n_thresholds)
        thresholds = list(set(round(float(t), 6) for t in np.percentile(combined, pcts)))
        if not thresholds:
            continue

        for thresh in thresholds:
            for dir_test in ["above", "below"]:
                tested += 1

                cond = StageCondition(expr_name, dir_test, thresh)

                # Stage 3 always exits 100% remaining
                # Test with both MFE gates and bar gates
                gate_configs = []
                for mfe_g in STAGE3_MFE_GATES_ADR:
                    gate_configs.append(("mfe_adr", mfe_g))
                for bar_g in STAGE3_BAR_GATES:
                    gate_configs.append(("bars_min", float(bar_g)))

                for gate_type, gate_val in gate_configs:
                    stage = StageConfig(
                        stage_id=3,
                        condition=cond,
                        trim_pct=1.0,  # always full exit
                        gate_type=gate_type,
                        gate_value=gate_val,
                        max_window=None,
                    )

                    result = MultiStageSimulator.simulate_all(
                        [stage], matrices, examples
                    )

                    if result is None:
                        continue

                    candidates.append({
                        "stage_config": stage,
                        "result": result,
                    })

    elapsed = time.time() - t0
    print(f"\n  Stage 3: tested {tested:,} conditions in {elapsed:.1f}s")
    print(f"  {len(candidates)} valid candidates")

    candidates.sort(key=lambda c: c["result"].median_capture_eff, reverse=True)
    top = candidates[:top_k]

    if top:
        print(f"\n  Top {len(top)} Stage 3 candidates:")
        for i, c in enumerate(top):
            sc = c["stage_config"]
            r = c["result"]
            gate_str = f"mfe>={sc.gate_value:.1f}ADR" if sc.gate_type == "mfe_adr" \
                else f"bars>={int(sc.gate_value)}"
            print(f"    #{i+1}: {sc.condition.expr_name} {sc.condition.direction} "
                  f"{sc.condition.threshold:.4f} | {gate_str} | "
                  f"median_eff={r.median_capture_eff:.3f} floor={r.floor_capture_eff:.3f}")

    return top


# ============================================================
# Phase B: Joint Optimization
# ============================================================

def joint_optimize(stage1_candidates: list, stage2_candidates: list,
                   stage3_candidates: list, matrices: list,
                   examples: list, top_n: int = 50) -> list:
    """Test all combinations of top Stage 1/2/3 candidates.
    
    Also tests 2-stage combos (skip Stage 1, skip Stage 2, etc.)
    to find the best overall configuration.
    """
    n1 = len(stage1_candidates)
    n2 = len(stage2_candidates)
    n3 = len(stage3_candidates)

    # Build all combos: 3-stage, 2-stage (1+3, 1+2, 2+3), and 1-stage
    combos = []

    # Full 3-stage
    for i1 in range(n1):
        for i2 in range(n2):
            for i3 in range(n3):
                combos.append((
                    stage1_candidates[i1]["stage_config"],
                    stage2_candidates[i2]["stage_config"],
                    stage3_candidates[i3]["stage_config"],
                ))

    # 2-stage: Stage 1 + Stage 3 (skip partial profit)
    for i1 in range(n1):
        for i3 in range(n3):
            combos.append((
                stage1_candidates[i1]["stage_config"],
                stage3_candidates[i3]["stage_config"],
            ))

    # 2-stage: Stage 2 + Stage 3 (no dud protection, just profit + trail)
    for i2 in range(n2):
        for i3 in range(n3):
            combos.append((
                stage2_candidates[i2]["stage_config"],
                stage3_candidates[i3]["stage_config"],
            ))

    # 2-stage: Stage 1 + Stage 2 (protection + profit, backstop for trail)
    for i1 in range(n1):
        for i2 in range(n2):
            combos.append((
                stage1_candidates[i1]["stage_config"],
                stage2_candidates[i2]["stage_config"],
            ))

    total = len(combos)
    print(f"\n{'='*80}")
    print(f"PHASE B — JOINT OPTIMIZATION")
    print(f"{'='*80}")
    print(f"  {n1} × {n2} × {n3} = {n1*n2*n3} full 3-stage combos")
    print(f"  + {n1*n3 + n2*n3 + n1*n2} 2-stage combos")
    print(f"  Total: {total} combinations to test")

    results = []
    t0 = time.time()

    for combo_i, stage_tuple in enumerate(combos):
        if (combo_i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (combo_i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{combo_i+1}/{total}] {rate:.0f} combos/s, "
                  f"{len(results)} valid")

        stages = list(stage_tuple)
        result = MultiStageSimulator.simulate_all(stages, matrices, examples)

        if result is not None:
            results.append(result)

    elapsed = time.time() - t0
    print(f"\n  Joint: {total:,} combos in {elapsed:.1f}s")
    print(f"  {len(results)} valid results")

    # Sort by median capture efficiency
    results.sort(key=lambda r: (r.median_capture_eff, r.floor_capture_eff), reverse=True)
    return results[:top_n]


# ============================================================
# Reporting
# ============================================================

def print_results(results: list, examples: list, top_n: int = 20):
    """Print ranked multi-stage exit configurations."""
    if not results:
        print("\nNo valid multi-stage configurations found.")
        return

    n_show = min(top_n, len(results))
    print(f"\n{'='*120}")
    print(f"TOP {n_show} MULTI-STAGE EXIT CONFIGURATIONS")
    print(f"{'='*120}")

    for rank, result in enumerate(results[:n_show], 1):
        n_stages = len(result.stages)
        stage_ids = [s.stage_id for s in result.stages]

        print(f"\n{'─'*120}")
        print(f"#{rank:3d}  [{n_stages}-stage: {stage_ids}]  "
              f"median_eff={result.median_capture_eff:.3f}  "
              f"avg_eff={result.avg_capture_eff:.3f}  "
              f"floor_eff={result.floor_capture_eff:.3f}  "
              f"median_pct={result.median_effective_pct:+.2f}%  "
              f"avg_bars={result.avg_bars_to_full_exit:.1f}")

        for stage in result.stages:
            gate_str = ""
            if stage.gate_type == "mfe_adr":
                gate_str = f"  gate: MFE≥{stage.gate_value:.1f}ADR"
            elif stage.gate_type == "bars_min":
                gate_str = f"  gate: bars≥{int(stage.gate_value)}"
            win_str = f"  maxwin={stage.max_window}" if stage.max_window else ""
            print(f"      Stage {stage.stage_id}: {stage.condition.expr_name} "
                  f"{stage.condition.direction} {stage.condition.threshold:.4f}  "
                  f"trim={stage.trim_pct:.0%}{gate_str}{win_str}")

        # Per-example detail
        print(f"\n      {'Ticker':8s} {'Entry Date':12s} {'MFE%':>7s} {'Eff%':>7s} "
              f"{'CapEff':>7s} {'Events':>40s}")
        for pe in result.per_example:
            events_str = " → ".join(
                f"S{e['stage_id']}@b{e['bar']}({e['pct_trimmed']:.0%},{e['pct_move']:+.1f}%)"
                for e in pe["events"]
            )
            print(f"      {pe['ticker']:8s} {pe['entry_date']:12s} "
                  f"{pe['mfe_pct']:+6.2f}% {pe['effective_pct']:+6.2f}% "
                  f"{pe['capture_eff']:6.3f} {events_str}")


def save_results(results: list, examples: list, setup_type: str, args):
    """Save results to JSON — timestamped archive + latest."""
    from datetime import datetime

    os.makedirs("data/multistage_exit", exist_ok=True)

    best = results[0] if results else None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    n_ex = len(examples)
    n_stages = len(best.stages) if best else 0
    med_eff = f"{best.median_capture_eff:.3f}" if best else "na"

    data = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "direction": args.direction,
        "max_forward": args.max_forward,
        "n_examples": n_ex,
        "examples_summary": [
            {"ticker": ex.ticker, "entry_date": ex.entry_date,
             "mfe_pct": ex.mfe_pct, "mfe_adr": ex.mfe_adr}
            for ex in examples
        ],
        "results": [],
    }

    for rank, result in enumerate(results[:50]):
        entry = {
            "rank": rank + 1,
            "n_stages": len(result.stages),
            "median_capture_eff": result.median_capture_eff,
            "avg_capture_eff": result.avg_capture_eff,
            "floor_capture_eff": result.floor_capture_eff,
            "median_effective_pct": result.median_effective_pct,
            "avg_bars_to_full_exit": result.avg_bars_to_full_exit,
            "stages": [],
            "per_example": result.per_example,
        }
        for stage in result.stages:
            entry["stages"].append({
                "stage_id": stage.stage_id,
                "expr_name": stage.condition.expr_name,
                "direction": stage.condition.direction,
                "threshold": stage.condition.threshold,
                "trim_pct": stage.trim_pct,
                "gate_type": stage.gate_type,
                "gate_value": stage.gate_value,
                "max_window": stage.max_window,
            })
        data["results"].append(entry)

    def nan_handler(x):
        if isinstance(x, float) and np.isnan(x):
            return None
        return x

    desc = f"ms_exit_{setup_type}_{n_stages}stg_{med_eff}eff_{ts}"
    ts_path = os.path.join("data/multistage_exit", f"{desc}.json")
    with open(ts_path, "w") as f:
        json.dump(data, f, indent=2, default=nan_handler)
    print(f"\n  Saved: {ts_path}")

    latest_path = os.path.join("data/multistage_exit", f"ms_exit_{setup_type}.json")
    with open(latest_path, "w") as f:
        json.dump(data, f, indent=2, default=nan_handler)
    print(f"  Saved as latest: {latest_path}")


def save_phase_a_results(stage1_cands, stage2_cands, stage3_cands, setup_type):
    """Save Phase A intermediate results for Phase B reuse."""
    os.makedirs("data/multistage_exit", exist_ok=True)

    def serialize_cand(c):
        sc = c["stage_config"]
        return {
            "stage_id": sc.stage_id,
            "expr_name": sc.condition.expr_name,
            "direction": sc.condition.direction,
            "threshold": sc.condition.threshold,
            "trim_pct": sc.trim_pct,
            "gate_type": sc.gate_type,
            "gate_value": sc.gate_value,
            "max_window": sc.max_window,
            "median_capture_eff": c["result"].median_capture_eff,
            "floor_capture_eff": c["result"].floor_capture_eff,
        }

    data = {
        "stage1": [serialize_cand(c) for c in stage1_cands],
        "stage2": [serialize_cand(c) for c in stage2_cands],
        "stage3": [serialize_cand(c) for c in stage3_cands],
    }

    path = os.path.join("data/multistage_exit", f"phase_a_{setup_type}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  Phase A results saved: {path}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Multi-Stage Exit Grinder — Task 3.7")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--n-thresholds", type=int, default=20)
    parser.add_argument("--direction", default="short")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--phase", default="AB", choices=["A", "B", "AB"],
                        help="Which phase to run (A=independent, B=joint, AB=both)")
    parser.add_argument("--top-k", type=int, default=TOP_K_PER_STAGE,
                        help="Top candidates per stage for Phase B")
    parser.add_argument("--base-only", action="store_true",
                        help="Only test base expressions (skip boolean aggregations)")
    args = parser.parse_args()

    print(f"Multi-Stage Exit Grinder — Task 3.7")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars")
    print(f"Phase: {args.phase}, Workers: {args.workers}")
    print(f"Top-K per stage: {args.top_k}")

    # 1. Load examples
    raw_examples = load_examples(args.setup)

    # 2. Load 5yr OHLCV cache
    universe_cache = load_5yr_cache()

    # 3. Get SPY
    spy_df = universe_cache.get("SPY")
    if spy_df is not None:
        spy_df = spy_df.copy()
        if not pd.api.types.is_datetime64_any_dtype(spy_df["date"]):
            spy_df["date"] = pd.to_datetime(spy_df["date"])
        spy_df = spy_df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            spy_df[col] = pd.to_numeric(spy_df[col], errors="coerce")

    # 4. Build ExampleData
    print(f"\nBuilding example data...")
    examples = []
    for raw in raw_examples:
        print(f"  {raw['ticker']:8s} {raw['entryDate']}...", end="", flush=True)
        ex = build_example_data(raw, args.direction, args.max_forward, universe_cache)
        if ex:
            examples.append(ex)
            print(f" OK — {ex.n_forward} fwd bars, MFE={ex.mfe_pct:+.2f}% "
                  f"({ex.mfe_adr:.1f} ADR)")
        else:
            print(" SKIP")

    if len(examples) < 3:
        print(f"\nOnly {len(examples)} examples — need at least 3. Aborting.")
        return

    print(f"\n{len(examples)} examples ready")

    # MFE summary
    mfes = [ex.mfe_pct for ex in examples]
    mfe_adrs = [ex.mfe_adr for ex in examples]
    print(f"\n  MFE:  floor={min(mfes):+.2f}%  median={np.median(mfes):+.2f}%  "
          f"avg={np.mean(mfes):+.2f}%")
    print(f"  ADR:  floor={min(mfe_adrs):.1f}  median={np.median(mfe_adrs):.1f}  "
          f"avg={np.mean(mfe_adrs):.1f}")

    # 5. Generate expression library
    base_exprs = generate_exit_expressions()
    native_bools, threshold_bools = generate_exit_boolean_conditions(base_exprs)
    print(f"\nExpression library: {len(base_exprs)} base expressions")
    print(f"Boolean conditions: {len(native_bools)} native + {len(threshold_bools)} threshold")

    # 6. Build expression matrices
    base_matrices = build_all_matrices_parallel(
        examples, base_exprs, args.direction, spy_df, args.workers
    )

    # 7. Boolean aggregations
    if not args.base_only:
        n_forwards = [ex.n_forward for ex in examples]
        agg_matrices = compute_boolean_aggregations(
            base_matrices, native_bools, threshold_bools, n_forwards
        )
        all_matrices = []
        for i in range(len(examples)):
            merged = {}
            if base_matrices[i]:
                merged.update(base_matrices[i])
            if agg_matrices[i]:
                merged.update(agg_matrices[i])
            all_matrices.append(merged)

        all_expr_names = [e["name"] for e in base_exprs]
        for m in agg_matrices:
            if m:
                all_expr_names.extend(sorted(m.keys()))
                break
        print(f"\nTotal expressions: {len(all_expr_names)}")
    else:
        all_matrices = base_matrices
        all_expr_names = [e["name"] for e in base_exprs]
        print(f"\n--base-only: {len(all_expr_names)} expressions")

    # 8. Phase A: Independent stage discovery
    if "A" in args.phase:
        stage1_cands = grind_stage1(
            examples, all_matrices, all_expr_names,
            args.direction, args.n_thresholds, args.top_k
        )
        stage2_cands = grind_stage2(
            examples, all_matrices, all_expr_names,
            args.direction, args.n_thresholds, args.top_k
        )
        stage3_cands = grind_stage3(
            examples, all_matrices, all_expr_names,
            args.direction, args.n_thresholds, args.top_k
        )
        save_phase_a_results(stage1_cands, stage2_cands, stage3_cands, args.setup)
    else:
        # Load Phase A results
        phase_a_path = os.path.join("data/multistage_exit", f"phase_a_{args.setup}.json")
        if not os.path.exists(phase_a_path):
            print(f"\nPhase A results not found at {phase_a_path}. Run with --phase A first.")
            return
        print(f"\nLoading Phase A results from {phase_a_path}")
        with open(phase_a_path) as f:
            phase_a = json.load(f)

        # Rebuild StageConfig objects from saved data
        def rebuild_cand(d):
            sc = StageConfig(
                stage_id=d["stage_id"],
                condition=StageCondition(d["expr_name"], d["direction"], d["threshold"]),
                trim_pct=d["trim_pct"],
                gate_type=d["gate_type"],
                gate_value=d["gate_value"],
                max_window=d.get("max_window"),
            )
            # Re-simulate to get full result
            result = MultiStageSimulator.simulate_all([sc], all_matrices, examples)
            return {"stage_config": sc, "result": result}

        stage1_cands = [rebuild_cand(d) for d in phase_a["stage1"]]
        stage2_cands = [rebuild_cand(d) for d in phase_a["stage2"]]
        stage3_cands = [rebuild_cand(d) for d in phase_a["stage3"]]
        stage1_cands = [c for c in stage1_cands if c["result"] is not None]
        stage2_cands = [c for c in stage2_cands if c["result"] is not None]
        stage3_cands = [c for c in stage3_cands if c["result"] is not None]
        print(f"  Loaded: {len(stage1_cands)} S1, {len(stage2_cands)} S2, "
              f"{len(stage3_cands)} S3 candidates")

    # 9. Phase B: Joint optimization
    if "B" in args.phase:
        if not stage1_cands and not stage2_cands and not stage3_cands:
            print("\nNo candidates from Phase A. Cannot run Phase B.")
            return

        # Need at least 2 stages for joint optimization
        joint_results = joint_optimize(
            stage1_cands, stage2_cands, stage3_cands,
            all_matrices, examples,
        )

        print_results(joint_results, examples)

        if joint_results:
            save_results(joint_results, examples, args.setup, args)

            # Summary
            best = joint_results[0]
            print(f"\n{'='*80}")
            print(f"BEST MULTI-STAGE EXIT:")
            for stage in best.stages:
                gate_str = ""
                if stage.gate_type == "mfe_adr":
                    gate_str = f"  gate: MFE≥{stage.gate_value:.1f}ADR"
                elif stage.gate_type == "bars_min":
                    gate_str = f"  gate: bars≥{int(stage.gate_value)}"
                win_str = f"  maxwin={stage.max_window}" if stage.max_window else ""
                print(f"  Stage {stage.stage_id}: {stage.condition.expr_name} "
                      f"{stage.condition.direction} {stage.condition.threshold:.4f}  "
                      f"trim={stage.trim_pct:.0%}{gate_str}{win_str}")
            print(f"\n  Median capture eff: {best.median_capture_eff:.3f} "
                  f"({best.median_capture_eff*100:.1f}% of MFE)")
            print(f"  Avg capture eff: {best.avg_capture_eff:.3f}")
            print(f"  Floor capture eff: {best.floor_capture_eff:.3f}")
            print(f"  Median effective move: {best.median_effective_pct:+.2f}%")
            print(f"  Avg bars to full exit: {best.avg_bars_to_full_exit:.1f}")
            print(f"{'='*80}")


if __name__ == "__main__":
    main()
