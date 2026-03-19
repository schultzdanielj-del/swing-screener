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
        ticker = s.get("ticker")
        sig_date = s.get("signal_date", s.get("date"))
        score = s.get("entry_candle_score")
        if ticker and sig_date and score is not None:
            lookup[(ticker, sig_date)] = score

    print(f"  {len(lookup)} signals with entry_candle_score")
    return lookup


def load_vetting_decisions(setup_type):
    """Load vetting decisions from local SQLite."""
    example_keys = set()
    rejected_keys = set()

    if not os.path.exists(DB_PATH):
        print(f"  WARNING: Database not found at {DB_PATH}")
        return example_keys, rejected_keys

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT ticker, entry_date, chart_date FROM examples WHERE setup_type=?",
            (setup_type,)
        ).fetchall()
        for r in rows:
            example_keys.add((r["ticker"], r["entry_date"]))
            if r["chart_date"]:
                example_keys.add((r["ticker"], r["chart_date"]))

        try:
            rows = conn.execute(
                "SELECT ticker, signal_date FROM rejected_signals WHERE setup_type=?",
                (setup_type,)
            ).fetchall()
            for r in rows:
                rejected_keys.add((r["ticker"], r["signal_date"]))
        except sqlite3.OperationalError:
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
    """Build the weighted signal population from EV grinder output."""
    raw = ev_data.get("signals", [])
    signals = []
    n_no_move = 0
    n_no_entry = 0
    n_rejected = 0
    n_examples = 0
    n_vetted_yes = 0
    n_unvetted = 0
    n_unvetted_no_score = 0

    for sig in raw:
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

        if key in rejected_keys:
            n_rejected += 1
            continue

        if sig.get("is_example", False):
            weight = 1.0
            category = "example"
            n_examples += 1
        else:
            ec_score = entry_scores.get(key)
            if ec_score is not None:
                weight = max(float(ec_score), 0.0)  # clamp negative to 0
                category = "unvetted"
                n_unvetted += 1
            else:
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
    """Build mapping from filtered expression index to cache column index."""
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
    """Build forward expression matrices and forward close arrays."""
    import pandas as pd

    n_signals = len(signals)
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

    ticker_groups = {}
    for i, sig in enumerate(signals):
        t = sig["ticker"]
        if t not in ticker_groups:
            ticker_groups[t] = []
        ticker_groups[t].append(i)

    for ticker, indices in ticker_groups.items():
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

        expr_dates, expr_data = expr_cache.get_ticker(ticker)
        if expr_dates is None:
            skipped_no_expr += len(indices)
            for i in indices:
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_expr_cache"})
            continue

        expr_date_strs = [str(d)[:10] for d in expr_dates]
        expr_date_to_idx = {d: idx for idx, d in enumerate(expr_date_strs)}

        for i in indices:
            sig = signals[i]
            sig_date = sig["date"]

            ohlcv_idx = date_to_idx.get(sig_date)
            if ohlcv_idx is None:
                skipped_no_date += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_date_ohlcv",
                                    "date": sig_date})
                continue

            expr_idx = expr_date_to_idx.get(sig_date)
            if expr_idx is None:
                skipped_no_date += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_date_expr",
                                    "date": sig_date})
                continue

            if direction == "short":
                entry_prices[i] = sig["entry_high"]
            else:
                entry_prices[i] = df["low"].values[ohlcv_idx]

            adr_values[i] = sig["adr_at_signal"]

            fwd_start_ohlcv = ohlcv_idx + 1
            fwd_end_ohlcv = min(fwd_start_ohlcv + max_forward, len(df))
            n_fwd_ohlcv = fwd_end_ohlcv - fwd_start_ohlcv

            fwd_start_expr = expr_idx + 1
            fwd_end_expr = min(fwd_start_expr + max_forward, len(expr_data))
            n_fwd_expr = fwd_end_expr - fwd_start_expr

            n_fwd = min(n_fwd_ohlcv, n_fwd_expr)

            if n_fwd < 1:
                skipped_no_forward += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_forward_bars",
                                    "date": sig_date})
                continue

            fwd_closes[i] = c_arr[fwd_start_ohlcv:fwd_start_ohlcv + n_fwd].copy()
            fwd_expr[i] = expr_data[fwd_start_expr:fwd_start_expr + n_fwd][:, cache_cols].copy()

            fwd_dates = date_strs[fwd_start_ohlcv:fwd_start_ohlcv + n_fwd].tolist()

            signal_meta.append({
                "idx": i, "ticker": ticker, "signal_date": sig_date,
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
# Padded Array Construction (for vectorized grinding)
# ============================================================

def build_padded_arrays(fwd_expr, fwd_closes, valid_mask, max_forward):
    """Stack per-signal forward data into padded 3D/2D arrays.

    Returns:
        fwd_val_3d: np.ndarray (n_valid, max_forward, n_exprs) float32, NaN-padded
        fwd_close_2d: np.ndarray (n_valid, max_forward) float64, NaN-padded
        n_bars_arr: np.ndarray (n_valid,) int32 — actual forward bar count per signal
        valid_indices: np.ndarray (n_valid,) int — original signal indices
    """
    valid_indices = np.where(valid_mask)[0]
    n_valid = len(valid_indices)
    n_exprs = fwd_expr[valid_indices[0]].shape[1] if n_valid > 0 else 0

    fwd_val_3d = np.full((n_valid, max_forward, n_exprs), np.nan, dtype=np.float32)
    fwd_close_2d = np.full((n_valid, max_forward), np.nan, dtype=np.float64)
    n_bars_arr = np.zeros(n_valid, dtype=np.int32)

    for vi, si in enumerate(valid_indices):
        fe = fwd_expr[si]
        fc = fwd_closes[si]
        nb = fe.shape[0]
        n_bars_arr[vi] = nb
        fwd_val_3d[vi, :nb, :] = fe
        fwd_close_2d[vi, :nb] = fc

    return fwd_val_3d, fwd_close_2d, n_bars_arr, valid_indices


# ============================================================
# Weighted Stats Computation
# ============================================================

def compute_weighted_stats(captured_adr, weights, triggered, move_adrs_actual,
                           entry_prices_v, adr_values_v, n_bars_held,
                           direction):
    """Compute full weighted stats panel for one exit candidate.

    Args:
        captured_adr: (n_valid,) float — captured move in ADR per signal.
                      Negative = loss. Non-triggers already set to -LOSS_ASSUMPTION_ADR.
        weights: (n_valid,) float — entry_candle_score weight per signal
        triggered: (n_valid,) bool — whether the exit fired on each signal
        move_adrs_actual: (n_valid,) float — actual move_adr from EV grinder (for capture eff)
        entry_prices_v: (n_valid,) float
        adr_values_v: (n_valid,) float
        n_bars_held: (n_valid,) int — bars from entry to exit (or max_forward for non-triggers)
        direction: str

    Returns:
        dict with all stats, or None if insufficient data
    """
    n = len(captured_adr)
    if n < 2:
        return None

    w = weights.copy()
    w_sum = w.sum()
    if w_sum < 1e-10:
        return None

    # Winners and losers (by captured ADR)
    is_win = captured_adr > 0
    is_loss = captured_adr <= 0

    # Weighted win rate
    wr = float(np.sum(w[is_win]) / w_sum)

    # Weighted expectancy (mean captured ADR)
    expectancy = float(np.sum(captured_adr * w) / w_sum)

    # Weighted stdev
    w_mean = expectancy
    w_var = np.sum(w * (captured_adr - w_mean) ** 2) / w_sum
    w_std = float(np.sqrt(w_var)) if w_var > 0 else 0.0

    # SQN: sqrt(N_weighted) * expectancy / stdev
    # N_weighted approximated as (sum(w))^2 / sum(w^2) — effective sample size
    n_eff = (w_sum ** 2) / np.sum(w ** 2) if np.sum(w ** 2) > 0 else 1.0
    sqn = float(np.sqrt(n_eff) * expectancy / w_std) if w_std > 0 else 0.0

    # Weighted profit factor
    gross_wins = float(np.sum(captured_adr[is_win] * w[is_win])) if is_win.any() else 0.0
    gross_losses = float(np.abs(np.sum(captured_adr[is_loss] * w[is_loss]))) if is_loss.any() else 0.001
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')

    # Weighted averages for winners/losers
    def _wmean(arr, mask, wt):
        if not mask.any():
            return 0.0
        ws = wt[mask].sum()
        return float(np.sum(arr[mask] * wt[mask]) / ws) if ws > 0 else 0.0

    avg_win = _wmean(captured_adr, is_win, w)
    avg_loss = _wmean(captured_adr, is_loss, w)
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    # Per-trade stats (unweighted for simplicity — these are descriptive)
    win_adrs = captured_adr[is_win]
    loss_adrs = captured_adr[is_loss]
    median_win = float(np.median(win_adrs)) if len(win_adrs) > 0 else 0.0
    median_loss = float(np.median(loss_adrs)) if len(loss_adrs) > 0 else 0.0
    best_win = float(np.max(win_adrs)) if len(win_adrs) > 0 else 0.0
    worst_loss = float(np.min(loss_adrs)) if len(loss_adrs) > 0 else 0.0

    # Bars held
    avg_bars_win = float(np.mean(n_bars_held[is_win])) if is_win.any() else 0.0
    avg_bars_loss = float(np.mean(n_bars_held[is_loss])) if is_loss.any() else 0.0
    avg_bars_all = float(np.mean(n_bars_held))

    # Max consecutive (unweighted)
    max_cw = _max_consecutive(is_win)
    max_cl = _max_consecutive(is_loss)

    # Weighted equity curve — trades ordered by signal date (already sorted by caller)
    # Each trade contributes: equity * risk_pct * captured_adr * weight_scale
    # weight_scale normalizes so fully-weighted trades have full impact
    equity = _build_weighted_equity_curve(captured_adr, w, INITIAL_CAPITAL, RISK_PER_TRADE)
    peak = np.maximum.accumulate(equity)
    dd = np.where(peak > 0, (peak - equity) / peak, 0.0)
    max_dd = float(np.max(dd))
    avg_dd = float(np.mean(dd[dd > 0])) if (dd > 0).any() else 0.0
    max_dd_dur = _max_dd_duration(equity)

    # Growth stats
    total_ret = equity[-1] / equity[0] if equity[0] > 0 else 1.0
    total_bars = int(np.sum(n_bars_held))
    years = total_bars / TRADING_DAYS_PER_YEAR if total_bars > 0 else 1.0
    cagr = float((total_ret ** (1 / years) - 1)) if years > 0 and total_ret > 0 else 0.0

    trades_per_year = TRADING_DAYS_PER_YEAR / avg_bars_all if avg_bars_all > 0 else 1.0
    annual_r = expectancy * trades_per_year
    annual_std = w_std * np.sqrt(trades_per_year)
    sharpe = float(annual_r / annual_std) if annual_std > 0 else 0.0

    downside_adr = captured_adr[captured_adr < 0]
    if len(downside_adr) > 1:
        ds_std = float(np.std(downside_adr, ddof=1))
    else:
        ds_std = 1.0
    annual_ds = ds_std * np.sqrt(trades_per_year)
    sortino = float(annual_r / annual_ds) if annual_ds > 0 else 0.0
    calmar = float(cagr / max_dd) if max_dd > 0 else float('inf')

    # Capture efficiency (triggered signals only)
    trig_mask = triggered & (move_adrs_actual > 0)
    if trig_mask.any():
        cap_eff = captured_adr[trig_mask] / move_adrs_actual[trig_mask]
        median_capture = float(np.median(cap_eff))
        floor_capture = float(np.min(cap_eff))
        mean_capture = float(np.mean(cap_eff))
    else:
        median_capture = floor_capture = mean_capture = 0.0

    # Trigger rate
    n_triggered = int(triggered.sum())
    trigger_rate = n_triggered / n if n > 0 else 0.0

    return {
        "n_signals": n,
        "n_triggered": n_triggered,
        "trigger_rate": round(trigger_rate, 4),
        "n_winners": int(is_win.sum()),
        "n_losers": int(is_loss.sum()),
        "win_rate": round(wr, 4),
        "expectancy": round(expectancy, 4),
        "sqn": round(sqn, 4),
        "profit_factor": round(min(profit_factor, 999.0), 4),
        "payoff_ratio": round(min(payoff_ratio, 999.0), 4),
        "avg_win_adr": round(avg_win, 4),
        "avg_loss_adr": round(avg_loss, 4),
        "median_win_adr": round(median_win, 4),
        "median_loss_adr": round(median_loss, 4),
        "best_win_adr": round(best_win, 4),
        "worst_loss_adr": round(worst_loss, 4),
        "std_adr": round(w_std, 4),
        "max_consec_winners": max_cw,
        "max_consec_losers": max_cl,
        "avg_bars_winners": round(avg_bars_win, 1),
        "avg_bars_losers": round(avg_bars_loss, 1),
        "avg_bars_all": round(avg_bars_all, 1),
        "max_drawdown": round(max_dd, 4),
        "avg_drawdown": round(avg_dd, 4),
        "max_dd_duration_trades": max_dd_dur,
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "calmar": round(min(calmar, 999.0), 4),
        "final_equity": round(float(equity[-1]), 2),
        "total_return_pct": round(float((equity[-1] / equity[0] - 1) * 100), 2),
        "median_capture_eff": round(median_capture, 4),
        "floor_capture_eff": round(floor_capture, 4),
        "mean_capture_eff": round(mean_capture, 4),
    }


def _max_consecutive(bool_arr):
    if len(bool_arr) == 0:
        return 0
    mx = cur = 0
    for v in bool_arr:
        if v:
            cur += 1
            if cur > mx:
                mx = cur
        else:
            cur = 0
    return mx


def _build_weighted_equity_curve(captured_adr, weights, initial_capital, risk_pct):
    """Build equity curve where each trade's impact is scaled by its weight."""
    n = len(captured_adr)
    eq = np.zeros(n + 1)
    eq[0] = initial_capital
    for i in range(n):
        # Weight scales the risk taken on this trade.
        # Weight 1.0 = full position. Weight 0.5 = half position.
        pnl = eq[i] * risk_pct * captured_adr[i] * weights[i]
        eq[i + 1] = eq[i] + pnl
        if eq[i + 1] <= 0:
            eq[i + 1:] = 0
            break
    return eq


def _max_dd_duration(equity):
    peak = equity[0]
    mx = cur = 0
    for v in equity[1:]:
        if v >= peak:
            peak = v
            cur = 0
        else:
            cur += 1
            if cur > mx:
                mx = cur
    return mx


# ============================================================
# 1-Stage Expression Grind (Increment 2)
# ============================================================

def grind_1stage(fwd_val_3d, fwd_close_2d, n_bars_arr, entry_prices_v,
                 adr_values_v, weights_v, is_hard_gate_v, move_adrs_v,
                 filtered_names, direction, max_forward):
    """Brute-force all expressions × thresholds × directions for 1-stage exits.

    Vectorized: for each expression, extract column → for each threshold × direction,
    find first triggering bar across all signals simultaneously using numpy.

    Returns:
        candidates: list of dicts, one per passing candidate, sorted by SQN desc
    """
    n_valid, _, n_exprs = fwd_val_3d.shape
    print(f"\n  ── 1-STAGE EXPRESSION GRIND ──")
    print(f"  {n_valid} signals × {n_exprs} expressions × ~{N_THRESHOLDS} thresholds × 2 directions")
    print(f"  Hard gate signals: {int(is_hard_gate_v.sum())}")

    t0 = time.time()
    candidates = []
    tested = 0
    hard_gate_fails = 0

    # Pre-compute bar validity mask: True where we have actual data (not padding)
    bar_valid = np.zeros((n_valid, max_forward), dtype=bool)
    for vi in range(n_valid):
        bar_valid[vi, :n_bars_arr[vi]] = True

    for expr_i in range(n_exprs):
        if (expr_i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"    [{expr_i+1}/{n_exprs}] {rate:.0f} expr/s, "
                  f"{len(candidates)} candidates, {tested:,} tested, "
                  f"{hard_gate_fails:,} hard gate fails")

        # Extract this expression's values: (n_valid, max_forward)
        col = fwd_val_3d[:, :, expr_i]

        # Pool all non-NaN values for threshold generation
        finite_mask = np.isfinite(col) & bar_valid
        finite_vals = col[finite_mask]
        if len(finite_vals) < n_valid:
            continue  # too many NaNs

        # Generate thresholds
        pcts = np.linspace(5, 95, N_THRESHOLDS)
        thresholds = np.unique(np.percentile(finite_vals, pcts))
        if len(thresholds) < 2:
            continue

        expr_name = filtered_names[expr_i]

        for thresh in thresholds:
            for dir_label, above in [("above", True), ("below", False)]:
                tested += 1

                # Vectorized trigger detection
                if above:
                    hit = (col >= thresh) & finite_mask
                else:
                    hit = (col <= thresh) & finite_mask

                # Find first triggering bar per signal
                # Set non-hit positions to max_forward+1, then argmin
                hit_bars = np.where(hit, np.arange(max_forward)[np.newaxis, :], max_forward + 1)
                first_bar = np.min(hit_bars, axis=1)  # (n_valid,)
                triggered = first_bar <= max_forward  # actual trigger vs no trigger

                # Hard gate: all examples + vetted YES must trigger
                if not triggered[is_hard_gate_v].all():
                    hard_gate_fails += 1
                    continue

                # Compute captured move per signal
                captured_adr = np.full(n_valid, -LOSS_ASSUMPTION_ADR, dtype=np.float64)
                bars_held = np.full(n_valid, max_forward, dtype=np.int32)

                for vi in range(n_valid):
                    if triggered[vi]:
                        fb = first_bar[vi]
                        exit_close = fwd_close_2d[vi, fb]
                        if np.isfinite(exit_close) and adr_values_v[vi] > 0:
                            if direction == "short":
                                captured_adr[vi] = (entry_prices_v[vi] - exit_close) / adr_values_v[vi]
                            else:
                                captured_adr[vi] = (exit_close - entry_prices_v[vi]) / adr_values_v[vi]
                        bars_held[vi] = fb + 1

                # Compute weighted stats
                stats = compute_weighted_stats(
                    captured_adr, weights_v, triggered, move_adrs_v,
                    entry_prices_v, adr_values_v, bars_held, direction)

                if stats is None:
                    continue

                stats["expr_name"] = expr_name
                stats["direction"] = dir_label
                stats["threshold"] = round(float(thresh), 6)
                candidates.append(stats)

    elapsed = time.time() - t0

    # Sort by SQN descending
    candidates.sort(key=lambda c: c.get("sqn", float('-inf')), reverse=True)

    print(f"\n  Grind complete:")
    print(f"    Time: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"    Tested: {tested:,}")
    print(f"    Hard gate fails: {hard_gate_fails:,}")
    print(f"    Passing candidates: {len(candidates):,}")

    if candidates:
        top = candidates[0]
        print(f"\n  Top candidate (by SQN):")
        print(f"    {top['expr_name']} {top['direction']} {top['threshold']}")
        print(f"    SQN={top['sqn']:.3f}  Exp={top['expectancy']:.3f}  "
              f"WR={top['win_rate']:.1%}  PF={top['profit_factor']:.2f}")
        print(f"    Triggered: {top['n_triggered']}/{top['n_signals']}  "
              f"Capture: med={top['median_capture_eff']:.2f} floor={top['floor_capture_eff']:.2f}")
        print(f"    Equity: ${top['final_equity']:,.0f}  CAGR={top['cagr']:.1%}  "
              f"MaxDD={top['max_drawdown']:.1%}")

    # Print top 10
    if len(candidates) >= 10:
        print(f"\n  Top 10 by SQN:")
        print(f"    {'Rank':<5} {'Expression':<45} {'Dir':<6} {'Thresh':>8} "
              f"{'SQN':>6} {'Expect':>7} {'WR':>6} {'Trig%':>6} {'CapMed':>6}")
        print(f"    {'-'*100}")
        for i, c in enumerate(candidates[:10]):
            print(f"    {i+1:<5} {c['expr_name']:<45} {c['direction']:<6} "
                  f"{c['threshold']:>8.4f} {c['sqn']:>6.2f} {c['expectancy']:>7.3f} "
                  f"{c['win_rate']:>6.1%} {c['trigger_rate']:>6.1%} "
                  f"{c['median_capture_eff']:>6.2f}")

    return candidates


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Profit Grinder — Phase 4 Exit Optimization")
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
    print(f"  PROFIT GRINDER — Phase 4 Exit Optimization")
    print(f"{'='*70}")
    print(f"  Setup: {setup.upper()}, Direction: {direction}")
    print(f"  Max forward: {args.max_forward} bars")
    print(f"  Workers: {args.workers}")
    print(f"  Loss assumption: {LOSS_ASSUMPTION_ADR} ADR")
    t_total = time.time()

    # ── 1. Load EV grinder output ──
    print(f"\n  ── LOADING DATA ──")
    ev_data, ev_path = load_ev_data(setup, args.ev_file)

    # ── 2. Load entry candle scores ──
    entry_scores = load_entry_scores(setup)

    # ── 3. Load vetting decisions ──
    example_keys, rejected_keys = load_vetting_decisions(setup)

    # ── 4. Build weighted signal population ──
    print(f"\n  ── BUILDING POPULATION ──")
    signals, pop_stats = build_signal_population(
        ev_data, entry_scores, example_keys, rejected_keys)

    print(f"\n  Population summary:")
    print(f"    Total raw signals:    {pop_stats['total_raw']}")
    print(f"    No move_adr (losers): {pop_stats['no_move_adr']}")
    print(f"    No entry/ADR data:    {pop_stats['no_entry_data']}")
    print(f"    Rejected (vetted NO): {pop_stats['rejected_excluded']}")
    print(f"    ───")
    print(f"    Examples:             {pop_stats['examples']} (weight 1.0)")
    print(f"    Vetted YES:           {pop_stats['vetted_yes']} (weight 1.0)")
    print(f"    Unvetted (scored):    {pop_stats['unvetted']} (weight = entry_candle_score)")
    print(f"    Unvetted (no score):  {pop_stats['unvetted_no_score']} (weight 0.0 — WARNING if > 0)")
    print(f"    Total population:     {pop_stats['total_population']}")

    if pop_stats["unvetted_no_score"] > 0:
        no_score_sigs = [s for s in signals if s["weight_category"] == "unvetted_no_score"]
        print(f"\n  ⚠ WARNING: {len(no_score_sigs)} winner signals have no entry_candle_score:")
        for s in no_score_sigs[:10]:
            print(f"    {s['ticker']} {s['date']}")

    weights = np.array([s["weight"] for s in signals])
    if len(weights) > 0:
        print(f"\n  Weight distribution:")
        print(f"    Min:    {weights.min():.4f}")
        print(f"    Median: {np.percentile(weights, 50):.4f}")
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
    print(f"\n  ── EXPRESSION CACHE ──")
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
    print(f"\n  ── FORWARD MATRIX CONSTRUCTION ──")
    ohlcv_cache = load_5yr_cache()

    fwd_expr, fwd_closes, entry_prices, adr_values, signal_meta, valid_mask, build_stats = \
        build_forward_data(signals, ohlcv_cache, expr_cache, expr_col_map,
                           direction, args.max_forward)

    del ohlcv_cache

    print(f"\n  Forward data built:")
    print(f"    Loaded:              {build_stats['loaded']}")
    print(f"    Skipped (no OHLCV):  {build_stats['skipped_no_ohlcv']}")
    print(f"    Skipped (no expr):   {build_stats['skipped_no_expr']}")
    print(f"    Skipped (no date):   {build_stats['skipped_no_date']}")
    print(f"    Skipped (no fwd):    {build_stats['skipped_no_forward']}")
    print(f"    Valid signals:       {build_stats['n_valid']}")

    fwd_bar_counts = [fwd_expr[i].shape[0] for i in range(len(signals)) if fwd_expr[i] is not None]
    if fwd_bar_counts:
        print(f"\n  Forward bars per signal:")
        print(f"    Min:    {min(fwd_bar_counts)}")
        print(f"    Median: {int(np.median(fwd_bar_counts))}")
        print(f"    Max:    {max(fwd_bar_counts)}")

    # Verify examples
    example_loaded = sum(1 for m in signal_meta
                          if m.get("status") == "ok" and m.get("is_example"))
    example_total = pop_stats["examples"]
    if example_loaded < example_total:
        print(f"\n  ✗ HARD FAIL: Only {example_loaded}/{example_total} examples loaded")
        sys.exit(1)
    else:
        print(f"\n  ✓ All {example_total} examples loaded successfully")

    # ── 7. Build padded arrays for vectorized grinding ──
    print(f"\n  ── BUILDING PADDED ARRAYS ──")
    fwd_val_3d, fwd_close_2d, n_bars_arr, valid_indices = \
        build_padded_arrays(fwd_expr, fwd_closes, valid_mask, args.max_forward)

    # Free per-signal lists
    del fwd_expr, fwd_closes

    print(f"  Padded array shape: {fwd_val_3d.shape} "
          f"({fwd_val_3d.nbytes / 1e9:.1f} GB)")

    # Build helper arrays for the valid subset
    weights_v = np.array([signals[si]["weight"] for si in valid_indices], dtype=np.float64)
    entry_prices_v = entry_prices[valid_indices]
    adr_values_v = adr_values[valid_indices]
    move_adrs_v = np.array([signals[si].get("move_adr", 0) or 0 for si in valid_indices],
                           dtype=np.float64)
    is_hard_gate_v = np.array([signals[si]["weight_category"] in ("example", "vetted_yes")
                               for si in valid_indices], dtype=bool)

    # Sort by signal date for chronological equity curve
    sig_dates = [signals[si]["date"] for si in valid_indices]
    date_order = np.argsort(sig_dates)

    # Reorder everything to chronological
    fwd_val_3d = fwd_val_3d[date_order]
    fwd_close_2d = fwd_close_2d[date_order]
    n_bars_arr = n_bars_arr[date_order]
    weights_v = weights_v[date_order]
    entry_prices_v = entry_prices_v[date_order]
    adr_values_v = adr_values_v[date_order]
    move_adrs_v = move_adrs_v[date_order]
    is_hard_gate_v = is_hard_gate_v[date_order]
    valid_indices = valid_indices[date_order]

    # ── 8. 1-Stage Grind ──
    candidates = grind_1stage(
        fwd_val_3d, fwd_close_2d, n_bars_arr, entry_prices_v,
        adr_values_v, weights_v, is_hard_gate_v, move_adrs_v,
        filtered_names, direction, args.max_forward)

    # ── Summary ──
    elapsed = time.time() - t_total
    print(f"\n  {'='*50}")
    print(f"  PROFIT GRINDER COMPLETE ({elapsed:.1f}s / {elapsed/60:.1f} min)")
    print(f"  {'='*50}")
    print(f"  Population: {pop_stats['total_population']} signals")
    print(f"  Expressions: {n_filtered}")
    print(f"  Candidates: {len(candidates)}")
    print(f"  EV source: {os.path.basename(ev_path)}")
    print(f"  {'='*50}")

    # TODO: Increment 3 will add multi-stage cascading search
    # TODO: Increment 4 will add output packaging + save


if __name__ == "__main__":
    main()
