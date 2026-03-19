"""
Profit Grinder — Phase 4: TA-Expression-Based Exit Optimization

Brute-forces the expression cache (~12K expressions after boolean exclusion)
testing every expression × threshold × direction against forward expression
values of all winner signals to find optimal exit conditions.

Weighting:
  - Examples + vetted YES: weight 1.0, hard trigger requirement
  - Vetted NO: excluded entirely
  - Unvetted winners: weighted by entry_candle_score (cosine similarity to
    example centroid). Non-triggers scored as 1-ADR loss at their weight.

Three trim modes computed independently:
  1-stage: 100% exit when expression condition fires
  2-stage: trim X% at condition A, exit remainder at condition B
  3-stage: trim X% at A, trim Y% at B, exit remainder at C

See PROFIT_GRINDER.md for full spec.

Usage:
    python scripts/profit_grinder.py --setup dtss
    python scripts/profit_grinder.py --setup dtss --workers 12 --max-forward 120
"""

import argparse
import sys
import os
import time
import json
import glob
import sqlite3
import numpy as np
import pickle
from datetime import datetime, timezone

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

# ============================================================
# Config
# ============================================================
DEFAULT_WORKERS = os.cpu_count() or 8
MAX_FORWARD_DEFAULT = 120

# Equity simulation
INITIAL_CAPITAL = 100_000
RISK_PER_TRADE = 0.01
TRADING_DAYS_PER_YEAR = 252

# Top N combos per mode to store full trade detail + equity curves
TOP_N_DETAIL = 100

# Loss assumption (ADR) for non-trigger penalty and stats
LOSS_ASSUMPTION_ADR = 1.0

# Thresholds per expression
N_THRESHOLDS = 50

# Boolean aggregation prefixes to exclude (monotonically increasing,
# structurally wrong for exit detection)
BOOLEAN_AGG_PREFIXES = ("ct_", "st_", "tir_")

SETUP_CONFIGS = {
    "dtss": {"direction": "short"},
    "3-4db": {"direction": "short"},
    "htf": {"direction": "long"},
}


# ============================================================
# Data Loading
# ============================================================

def load_5yr_cache():
    """Load 5-year OHLCV cache from local disk."""
    for name in ("universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            print(f"  Loading 5yr OHLCV cache from {os.path.basename(path)}...")
            with open(path, "rb") as f:
                cache = pickle.load(f)
            print(f"  {len(cache)} tickers loaded")
            return cache
    raise FileNotFoundError("No OHLCV cache found. Run cache_builder.py first.")


def find_latest_ev_file(setup_type):
    """Find the most recent EV grinder output file for this setup."""
    prefix = f"ev_{setup_type}_"
    candidates = []
    for fname in os.listdir(CACHE_DIR):
        if fname.startswith(prefix) and fname.endswith(".json"):
            candidates.append(os.path.join(CACHE_DIR, fname))
    if not candidates:
        raise FileNotFoundError(f"No EV grinder output found for {setup_type} in {CACHE_DIR}")
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def load_ev_data(setup_type, ev_file=None):
    """Load EV grinder output."""
    path = ev_file or find_latest_ev_file(setup_type)
    print(f"  Loading EV grinder data from {os.path.basename(path)}...")
    with open(path, "r") as f:
        data = json.load(f)
    print(f"  {len(data.get('signals', []))} total signals")
    return data, path


def load_entry_scores(setup_type):
    """Load entry candle scorer output. Returns dict of (ticker, date) -> entry_candle_score."""
    # Try latest pointer first, then timestamped files
    latest = os.path.join(CACHE_DIR, f"entry_scores_{setup_type}.json")
    if os.path.exists(latest):
        path = latest
    else:
        pattern = os.path.join(CACHE_DIR, f"entry_scores_{setup_type}_*.json")
        candidates = glob.glob(pattern)
        if not candidates:
            print(f"  WARNING: No entry scores found for {setup_type}")
            return {}
        candidates.sort(key=os.path.getmtime, reverse=True)
        path = candidates[0]

    print(f"  Loading entry scores from {os.path.basename(path)}...")
    with open(path, "r") as f:
        data = json.load(f)

    scored = data.get("scored_signals", [])
    lookup = {}
    for s in scored:
        # Entry scorer uses signal_date (= rightmost bar date = same as EV signal date)
        # and also has ticker
        ticker = s.get("ticker")
        sig_date = s.get("signal_date", s.get("date"))
        score = s.get("entry_candle_score")
        if ticker and sig_date and score is not None:
            lookup[(ticker, sig_date)] = score

    print(f"  {len(lookup)} signals with entry_candle_score")
    return lookup


def load_vetting_decisions(setup_type):
    """Load vetting decisions from local SQLite.

    Returns:
        example_keys: set of (ticker, signal_date) for examples
        rejected_keys: set of (ticker, signal_date) for vetted NO
    """
    example_keys = set()
    rejected_keys = set()

    if not os.path.exists(DB_PATH):
        print(f"  WARNING: Database not found at {DB_PATH}")
        return example_keys, rejected_keys

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Examples: entry_date is the entry bar (signal_date + 1 trading day)
        # But EV grinder signals already have is_example flag, so we use that
        # primarily. We load examples here to cross-reference.
        rows = conn.execute(
            "SELECT ticker, entry_date, chart_date FROM examples WHERE setup_type=?",
            (setup_type,)
        ).fetchall()
        for r in rows:
            # Store both entry_date and chart_date for matching flexibility
            example_keys.add((r["ticker"], r["entry_date"]))
            if r["chart_date"]:
                example_keys.add((r["ticker"], r["chart_date"]))

        # Rejected signals
        try:
            rows = conn.execute(
                "SELECT ticker, signal_date FROM rejected_signals WHERE setup_type=?",
                (setup_type,)
            ).fetchall()
            for r in rows:
                rejected_keys.add((r["ticker"], r["signal_date"]))
        except sqlite3.OperationalError:
            # Table might not exist yet
            pass

        conn.close()
    except Exception as e:
        print(f"  WARNING: Error loading vetting decisions: {e}")

    print(f"  Examples: {len(example_keys)} keys, Rejected: {len(rejected_keys)} keys")
    return example_keys, rejected_keys


# ============================================================
# Signal Population + Weighting
# ============================================================

def build_signal_population(ev_data, entry_scores, example_keys, rejected_keys):
    """Build the weighted signal population from EV grinder output.

    Returns:
        signals: list of signal dicts with 'weight' and 'weight_category' added
        stats: dict with population counts
    """
    raw = ev_data.get("signals", [])
    signals = []
    n_no_move = 0
    n_no_entry = 0
    n_rejected = 0
    n_examples = 0
    n_vetted_yes = 0  # future: when vetting produces YES flags
    n_unvetted = 0
    n_unvetted_no_score = 0

    for sig in raw:
        # Filter: must have move_adr (exit condition fired = winner)
        if sig.get("move_adr") is None:
            n_no_move += 1
            continue
        if sig.get("entry_high") is None or sig.get("adr_at_signal") is None:
            n_no_entry += 1
            continue
        if sig["adr_at_signal"] <= 0:
            n_no_entry += 1
            continue

        ticker = sig["ticker"]
        sig_date = sig["date"]
        key = (ticker, sig_date)

        # Check rejected (vetted NO) — exclude entirely
        if key in rejected_keys:
            n_rejected += 1
            continue

        # Determine weight and category
        if sig.get("is_example", False):
            weight = 1.0
            category = "example"
            n_examples += 1
        else:
            # Check entry candle score for unvetted winners
            ec_score = entry_scores.get(key)
            if ec_score is not None:
                weight = float(ec_score)
                category = "unvetted"
                n_unvetted += 1
            else:
                # Signal not in entry scores — log warning, weight 0.0
                weight = 0.0
                category = "unvetted_no_score"
                n_unvetted_no_score += 1

        sig_out = dict(sig)
        sig_out["weight"] = weight
        sig_out["weight_category"] = category
        sig_out["entry_candle_score"] = entry_scores.get(key)
        signals.append(sig_out)

    stats = {
        "total_raw": len(raw),
        "no_move_adr": n_no_move,
        "no_entry_data": n_no_entry,
        "rejected_excluded": n_rejected,
        "examples": n_examples,
        "vetted_yes": n_vetted_yes,
        "unvetted": n_unvetted,
        "unvetted_no_score": n_unvetted_no_score,
        "total_population": len(signals),
    }
    return signals, stats


# ============================================================
# Expression Filtering
# ============================================================

def build_expr_col_map(expr_names):
    """Build mapping from filtered expression index to cache column index.

    Excludes boolean aggregation expressions (ct_, st_, tir_ prefixes)
    which are monotonically increasing and structurally wrong for exit detection.

    Returns:
        expr_col_map: list of (filtered_idx, cache_col_idx) tuples
        filtered_names: list of expression names after filtering
        n_excluded: count of excluded expressions
    """
    expr_col_map = []
    filtered_names = []
    n_excluded = 0

    for col_idx, name in enumerate(expr_names):
        if name.startswith(BOOLEAN_AGG_PREFIXES):
            n_excluded += 1
            continue
        filtered_idx = len(expr_col_map)
        expr_col_map.append((filtered_idx, col_idx))
        filtered_names.append(name)

    return expr_col_map, filtered_names, n_excluded


# ============================================================
# Forward Matrix Construction
# ============================================================

def build_forward_data(signals, ohlcv_cache, expr_cache, expr_col_map,
                       direction, max_forward):
    """Build forward expression matrices and forward close arrays.

    For each signal:
      - Load ticker .npz from expression cache
      - Find entry bar by date
      - Slice forward expression values (entry+1 through entry+max_forward)
        for filtered (non-boolean) expressions only
      - Slice forward close prices from OHLCV for move measurement

    Returns:
        fwd_expr: list of numpy arrays, one per signal. Shape (n_fwd_bars, n_filtered_exprs).
                  None if signal couldn't be loaded.
        fwd_closes: list of numpy arrays, one per signal. Shape (n_fwd_bars,).
                    None if signal couldn't be loaded.
        entry_prices: numpy array (n_signals,) — entry price per signal
        adr_values: numpy array (n_signals,) — ADR at signal per signal
        signal_meta: list of per-signal metadata dicts
        build_stats: dict with counts
    """
    import pandas as pd

    n_signals = len(signals)
    n_filtered = len(expr_col_map)

    # Extract the cache column indices we need (filtered set)
    cache_cols = np.array([col_idx for _, col_idx in expr_col_map], dtype=np.int32)

    fwd_expr = [None] * n_signals
    fwd_closes = [None] * n_signals
    entry_prices = np.zeros(n_signals, dtype=np.float64)
    adr_values = np.zeros(n_signals, dtype=np.float64)
    signal_meta = []

    loaded = 0
    skipped_no_ohlcv = 0
    skipped_no_expr = 0
    skipped_no_date = 0
    skipped_no_forward = 0

    # Group by ticker for efficient cache loading
    ticker_groups = {}
    for i, sig in enumerate(signals):
        t = sig["ticker"]
        if t not in ticker_groups:
            ticker_groups[t] = []
        ticker_groups[t].append(i)

    for ticker, indices in ticker_groups.items():
        # Load OHLCV
        df = ohlcv_cache.get(ticker)
        if df is None:
            skipped_no_ohlcv += len(indices)
            for i in indices:
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_ohlcv"})
            continue

        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])

        df = df.sort_values("date").reset_index(drop=True)
        date_strs = df["date"].dt.strftime("%Y-%m-%d").values
        date_to_idx = {d: idx for idx, d in enumerate(date_strs)}
        c_arr = df["close"].values.astype(np.float64)

        # Load expression cache
        expr_dates, expr_data = expr_cache.get_ticker(ticker)
        if expr_dates is None:
            skipped_no_expr += len(indices)
            for i in indices:
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_expr_cache"})
            continue

        # Build date lookup for expression cache
        expr_date_strs = [str(d)[:10] for d in expr_dates]
        expr_date_to_idx = {d: idx for idx, d in enumerate(expr_date_strs)}

        for i in indices:
            sig = signals[i]
            sig_date = sig["date"]

            # Find bar in OHLCV
            ohlcv_idx = date_to_idx.get(sig_date)
            if ohlcv_idx is None:
                skipped_no_date += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_date_ohlcv",
                                    "date": sig_date})
                continue

            # Find bar in expression cache
            expr_idx = expr_date_to_idx.get(sig_date)
            if expr_idx is None:
                skipped_no_date += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_date_expr",
                                    "date": sig_date})
                continue

            # Entry price
            if direction == "short":
                entry_prices[i] = sig["entry_high"]
            else:
                entry_prices[i] = df["low"].values[ohlcv_idx]  # conservative for longs

            adr_values[i] = sig["adr_at_signal"]

            # Forward slicing — start from bar AFTER signal bar
            fwd_start_ohlcv = ohlcv_idx + 1
            fwd_end_ohlcv = min(fwd_start_ohlcv + max_forward, len(df))
            n_fwd_ohlcv = fwd_end_ohlcv - fwd_start_ohlcv

            fwd_start_expr = expr_idx + 1
            fwd_end_expr = min(fwd_start_expr + max_forward, len(expr_data))
            n_fwd_expr = fwd_end_expr - fwd_start_expr

            # Use the shorter of the two
            n_fwd = min(n_fwd_ohlcv, n_fwd_expr)

            if n_fwd < 1:
                skipped_no_forward += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_forward_bars",
                                    "date": sig_date})
                continue

            # Extract forward closes
            fwd_closes[i] = c_arr[fwd_start_ohlcv:fwd_start_ohlcv + n_fwd].copy()

            # Extract forward expression values (filtered columns only)
            fwd_expr[i] = expr_data[fwd_start_expr:fwd_start_expr + n_fwd][:, cache_cols].copy()

            # Forward dates for output
            fwd_dates = date_strs[fwd_start_ohlcv:fwd_start_ohlcv + n_fwd].tolist()

            signal_meta.append({
                "idx": i,
                "ticker": ticker,
                "signal_date": sig_date,
                "entry_price": float(entry_prices[i]),
                "adr": float(adr_values[i]),
                "classification": sig.get("classification"),
                "is_example": sig.get("is_example", False),
                "quality_score": sig.get("quality_score", 0),
                "move_adr": sig.get("move_adr"),
                "killed_at_depth": sig.get("killed_at_depth"),
                "weight": sig["weight"],
                "weight_category": sig["weight_category"],
                "entry_candle_score": sig.get("entry_candle_score"),
                "n_forward_bars": n_fwd,
                "fwd_dates": fwd_dates,
                "status": "ok",
            })
            loaded += 1

    # Build valid mask
    valid_mask = np.array([fwd_expr[i] is not None for i in range(n_signals)])

    build_stats = {
        "loaded": loaded,
        "skipped_no_ohlcv": skipped_no_ohlcv,
        "skipped_no_expr": skipped_no_expr,
        "skipped_no_date": skipped_no_date,
        "skipped_no_forward": skipped_no_forward,
        "total_skipped": n_signals - loaded,
        "n_valid": int(valid_mask.sum()),
    }

    return fwd_expr, fwd_closes, entry_prices, adr_values, signal_meta, valid_mask, build_stats


# ============================================================
# Main (Increment 1: Data Loading + Population + Matrices)
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Profit Grinder \u2014 Phase 4 Exit Optimization")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--direction", default=None,
                        help="Override direction (default: from setup config)")
    parser.add_argument("--ev-file", default=None,
                        help="Specific EV grinder output file")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    setup = args.setup
    config = SETUP_CONFIGS.get(setup, {"direction": "short"})
    direction = args.direction or config["direction"]

    print(f"\n{'='*70}")
    print(f"  PROFIT GRINDER \u2014 Phase 4 Exit Optimization")
    print(f"{'='*70}")
    print(f"  Setup: {setup.upper()}, Direction: {direction}")
    print(f"  Max forward: {args.max_forward} bars")
    print(f"  Workers: {args.workers}")
    print(f"  Loss assumption: {LOSS_ASSUMPTION_ADR} ADR")
    t_total = time.time()

    # ── 1. Load EV grinder output ──
    print(f"\n  \u2500\u2500 LOADING DATA \u2500\u2500")
    ev_data, ev_path = load_ev_data(setup, args.ev_file)

    # ── 2. Load entry candle scores ──
    entry_scores = load_entry_scores(setup)

    # ── 3. Load vetting decisions ──
    example_keys, rejected_keys = load_vetting_decisions(setup)

    # ── 4. Build weighted signal population ──
    print(f"\n  \u2500\u2500 BUILDING POPULATION \u2500\u2500")
    signals, pop_stats = build_signal_population(
        ev_data, entry_scores, example_keys, rejected_keys)

    print(f"\n  Population summary:")
    print(f"    Total raw signals:    {pop_stats['total_raw']}")
    print(f"    No move_adr (losers): {pop_stats['no_move_adr']}")
    print(f"    No entry/ADR data:    {pop_stats['no_entry_data']}")
    print(f"    Rejected (vetted NO): {pop_stats['rejected_excluded']}")
    print(f"    \u2500\u2500\u2500")
    print(f"    Examples:             {pop_stats['examples']} (weight 1.0)")
    print(f"    Vetted YES:           {pop_stats['vetted_yes']} (weight 1.0)")
    print(f"    Unvetted (scored):    {pop_stats['unvetted']} (weight = entry_candle_score)")
    print(f"    Unvetted (no score):  {pop_stats['unvetted_no_score']} (weight 0.0 \u2014 WARNING if > 0)")
    print(f"    Total population:     {pop_stats['total_population']}")

    if pop_stats["unvetted_no_score"] > 0:
        no_score_sigs = [s for s in signals if s["weight_category"] == "unvetted_no_score"]
        print(f"\n  \u26a0 WARNING: {len(no_score_sigs)} winner signals have no entry_candle_score:")
        for s in no_score_sigs[:10]:
            print(f"    {s['ticker']} {s['date']}")
        if len(no_score_sigs) > 10:
            print(f"    ... and {len(no_score_sigs) - 10} more")

    # Weight distribution
    weights = np.array([s["weight"] for s in signals])
    if len(weights) > 0:
        print(f"\n  Weight distribution:")
        print(f"    Min:    {weights.min():.4f}")
        print(f"    25th:   {np.percentile(weights, 25):.4f}")
        print(f"    Median: {np.percentile(weights, 50):.4f}")
        print(f"    75th:   {np.percentile(weights, 75):.4f}")
        print(f"    Max:    {weights.max():.4f}")
        print(f"    Mean:   {weights.mean():.4f}")
        print(f"    Sum:    {weights.sum():.2f}")

    hard_gate_count = sum(1 for s in signals
                          if s["weight_category"] in ("example", "vetted_yes"))
    print(f"\n  Hard gate signals (must trigger): {hard_gate_count}")

    if pop_stats["total_population"] < 5:
        print(f"\n  ERROR: Only {pop_stats['total_population']} signals. Need at least 5.")
        sys.exit(1)

    # ── 5. Load expression cache + build filter map ──
    print(f"\n  \u2500\u2500 EXPRESSION CACHE \u2500\u2500")
    from expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache not found or invalid.")
        sys.exit(1)

    expr_names = expr_cache.expr_names
    n_total_expr = len(expr_names)
    print(f"  Total expressions in cache: {n_total_expr}")

    expr_col_map, filtered_names, n_excluded = build_expr_col_map(expr_names)
    n_filtered = len(expr_col_map)
    print(f"  Boolean aggregations excluded: {n_excluded}")
    print(f"  Expressions for exit search:   {n_filtered}")

    # ── 6. Load OHLCV + build forward matrices ──
    print(f"\n  \u2500\u2500 FORWARD MATRIX CONSTRUCTION \u2500\u2500")
    ohlcv_cache = load_5yr_cache()

    fwd_expr, fwd_closes, entry_prices, adr_values, signal_meta, valid_mask, build_stats = \
        build_forward_data(signals, ohlcv_cache, expr_cache, expr_col_map,
                           direction, args.max_forward)

    # Free large caches
    del ohlcv_cache

    print(f"\n  Forward data built:")
    print(f"    Loaded:              {build_stats['loaded']}")
    print(f"    Skipped (no OHLCV):  {build_stats['skipped_no_ohlcv']}")
    print(f"    Skipped (no expr):   {build_stats['skipped_no_expr']}")
    print(f"    Skipped (no date):   {build_stats['skipped_no_date']}")
    print(f"    Skipped (no fwd):    {build_stats['skipped_no_forward']}")
    print(f"    Valid signals:       {build_stats['n_valid']}")

    # Forward bar stats
    fwd_bar_counts = [fwd_expr[i].shape[0] for i in range(len(signals)) if fwd_expr[i] is not None]
    if fwd_bar_counts:
        print(f"\n  Forward bars per signal:")
        print(f"    Min:    {min(fwd_bar_counts)}")
        print(f"    Median: {int(np.median(fwd_bar_counts))}")
        print(f"    Max:    {max(fwd_bar_counts)}")

    # Verify examples are all loaded
    example_loaded = sum(1 for m in signal_meta
                          if m.get("status") == "ok" and m.get("is_example"))
    example_total = pop_stats["examples"]
    if example_loaded < example_total:
        print(f"\n  \u2717 HARD FAIL: Only {example_loaded}/{example_total} examples loaded")
        failed = [m for m in signal_meta if m.get("is_example") and m.get("status") != "ok"]
        for m in failed:
            print(f"    {m.get('ticker')} {m.get('date', m.get('signal_date', '?'))}: {m.get('status')}")
        sys.exit(1)
    else:
        print(f"\n  \u2713 All {example_total} examples loaded successfully")

    # ── Summary ──
    elapsed = time.time() - t_total
    print(f"\n  {'='*50}")
    print(f"  INCREMENT 1 COMPLETE ({elapsed:.1f}s)")
    print(f"  {'='*50}")
    print(f"  Population: {pop_stats['total_population']} signals")
    print(f"    Examples: {pop_stats['examples']}, Unvetted: {pop_stats['unvetted']}")
    print(f"  Expressions: {n_filtered} (from {n_total_expr}, {n_excluded} boolean excluded)")
    print(f"  Valid forward matrices: {build_stats['n_valid']}")
    print(f"  EV source: {os.path.basename(ev_path)}")
    print(f"  {'='*50}")

    # TODO: Increment 2 will add 1-stage expression grinding here
    # TODO: Increment 3 will add multi-stage cascading search
    # TODO: Increment 4 will add output packaging + save


if __name__ == "__main__":
    main()
