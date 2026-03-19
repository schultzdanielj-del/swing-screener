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
INITIAL_CAPITAL = 100_000
RISK_PER_TRADE = 0.01
TRADING_DAYS_PER_YEAR = 252
TOP_N_DETAIL = 100
LOSS_ASSUMPTION_ADR = 1.0
N_THRESHOLDS = 50
BOOLEAN_AGG_PREFIXES = ("ct_", "st_", "tir_")

SETUP_CONFIGS = {
    "dtss": {"direction": "short"},
    "3-4db": {"direction": "short"},
    "htf": {"direction": "long"},
}


# ============================================================
# Data Loading (unchanged from Inc 1)
# ============================================================

def load_5yr_cache():
    for name in ("universe_ohlcv_5yr.pkl", "universe_ohlcv.pkl"):
        path = os.path.join(CACHE_DIR, name)
        if os.path.exists(path):
            print(f"  Loading 5yr OHLCV cache from {os.path.basename(path)}...")
            with open(path, "rb") as f:
                cache = pickle.load(f)
            print(f"  {len(cache)} tickers loaded")
            return cache
    raise FileNotFoundError("No OHLCV cache found.")


def find_latest_ev_file(setup_type):
    prefix = f"ev_{setup_type}_"
    candidates = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
                  if f.startswith(prefix) and f.endswith(".json")]
    if not candidates:
        raise FileNotFoundError(f"No EV grinder output for {setup_type}")
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


def load_ev_data(setup_type, ev_file=None):
    path = ev_file or find_latest_ev_file(setup_type)
    print(f"  Loading EV grinder data from {os.path.basename(path)}...")
    with open(path, "r") as f:
        data = json.load(f)
    print(f"  {len(data.get('signals', []))} total signals")
    return data, path


def load_entry_scores(setup_type):
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

    lookup = {}
    for s in data.get("scored_signals", []):
        ticker = s.get("ticker")
        sig_date = s.get("signal_date", s.get("date"))
        score = s.get("entry_candle_score")
        if ticker and sig_date and score is not None:
            lookup[(ticker, sig_date)] = score
    print(f"  {len(lookup)} signals with entry_candle_score")
    return lookup


def load_vetting_decisions(setup_type):
    example_keys = set()
    rejected_keys = set()
    if not os.path.exists(DB_PATH):
        print(f"  WARNING: Database not found at {DB_PATH}")
        return example_keys, rejected_keys
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT ticker, entry_date, chart_date FROM examples WHERE setup_type=?", (setup_type,)):
            example_keys.add((r["ticker"], r["entry_date"]))
            if r["chart_date"]:
                example_keys.add((r["ticker"], r["chart_date"]))
        try:
            for r in conn.execute("SELECT ticker, signal_date FROM rejected_signals WHERE setup_type=?", (setup_type,)):
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
    raw = ev_data.get("signals", [])
    signals = []
    counts = {"no_move": 0, "no_entry": 0, "rejected": 0, "examples": 0,
              "vetted_yes": 0, "unvetted": 0, "no_score": 0}

    for sig in raw:
        if sig.get("move_adr") is None:
            counts["no_move"] += 1; continue
        if sig.get("entry_high") is None or sig.get("adr_at_signal") is None:
            counts["no_entry"] += 1; continue
        if sig["adr_at_signal"] <= 0:
            counts["no_entry"] += 1; continue

        key = (sig["ticker"], sig["date"])
        if key in rejected_keys:
            counts["rejected"] += 1; continue

        if sig.get("is_example", False):
            weight, category = 1.0, "example"
            counts["examples"] += 1
        else:
            ec = entry_scores.get(key)
            if ec is not None:
                weight, category = max(float(ec), 0.0), "unvetted"
                counts["unvetted"] += 1
            else:
                weight, category = 0.0, "unvetted_no_score"
                counts["no_score"] += 1

        s = dict(sig)
        s["weight"] = weight
        s["weight_category"] = category
        s["entry_candle_score"] = entry_scores.get(key)
        signals.append(s)

    counts["total"] = len(signals)
    return signals, counts


# ============================================================
# Expression Filtering
# ============================================================

def build_expr_col_map(expr_names):
    expr_col_map = []
    filtered_names = []
    n_excluded = 0
    for col_idx, name in enumerate(expr_names):
        if name.startswith(BOOLEAN_AGG_PREFIXES):
            n_excluded += 1; continue
        expr_col_map.append((len(expr_col_map), col_idx))
        filtered_names.append(name)
    return expr_col_map, filtered_names, n_excluded


# ============================================================
# Forward Data Construction
# ============================================================

def build_forward_data(signals, ohlcv_cache, expr_cache, expr_col_map,
                       direction, max_forward):
    """Build per-signal forward expression arrays and forward close arrays.

    RAM-safe: stores data as a list of small per-signal arrays, not one big
    contiguous block. Total ~2 GB spread across 364 separate allocations.
    """
    import pandas as pd

    n_signals = len(signals)
    cache_cols = np.array([col_idx for _, col_idx in expr_col_map], dtype=np.int32)

    fwd_expr = [None] * n_signals
    fwd_closes = [None] * n_signals
    entry_prices = np.zeros(n_signals, dtype=np.float64)
    adr_values = np.zeros(n_signals, dtype=np.float64)
    signal_meta = []

    loaded = skipped_ohlcv = skipped_expr = skipped_date = skipped_fwd = 0

    ticker_groups = {}
    for i, sig in enumerate(signals):
        ticker_groups.setdefault(sig["ticker"], []).append(i)

    for ticker, indices in ticker_groups.items():
        df = ohlcv_cache.get(ticker)
        if df is None:
            skipped_ohlcv += len(indices)
            for i in indices:
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_ohlcv"})
            continue

        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df = df.copy(); df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        date_strs = df["date"].dt.strftime("%Y-%m-%d").values
        date_to_idx = {d: idx for idx, d in enumerate(date_strs)}
        c_arr = df["close"].values.astype(np.float64)

        expr_dates, expr_data = expr_cache.get_ticker(ticker)
        if expr_dates is None:
            skipped_expr += len(indices)
            for i in indices:
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_expr"})
            continue

        expr_date_map = {str(d)[:10]: idx for idx, d in enumerate(expr_dates)}

        for i in indices:
            sig = signals[i]
            sd = sig["date"]
            oi = date_to_idx.get(sd)
            ei = expr_date_map.get(sd)
            if oi is None or ei is None:
                skipped_date += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_date", "date": sd})
                continue

            entry_prices[i] = sig["entry_high"] if direction == "short" else df["low"].values[oi]
            adr_values[i] = sig["adr_at_signal"]

            n_fwd = min(min(len(df) - oi - 1, len(expr_data) - ei - 1), max_forward)
            if n_fwd < 1:
                skipped_fwd += 1
                signal_meta.append({"idx": i, "ticker": ticker, "status": "no_fwd", "date": sd})
                continue

            fwd_closes[i] = c_arr[oi+1:oi+1+n_fwd].copy()
            fwd_expr[i] = expr_data[ei+1:ei+1+n_fwd][:, cache_cols].copy()

            fwd_dates = date_strs[oi+1:oi+1+n_fwd].tolist()
            signal_meta.append({
                "idx": i, "ticker": ticker, "signal_date": sd,
                "entry_price": float(entry_prices[i]), "adr": float(adr_values[i]),
                "classification": sig.get("classification"),
                "is_example": sig.get("is_example", False),
                "quality_score": sig.get("quality_score", 0),
                "move_adr": sig.get("move_adr"),
                "killed_at_depth": sig.get("killed_at_depth"),
                "weight": sig["weight"], "weight_category": sig["weight_category"],
                "entry_candle_score": sig.get("entry_candle_score"),
                "n_forward_bars": n_fwd, "fwd_dates": fwd_dates, "status": "ok",
            })
            loaded += 1

    valid_mask = np.array([fwd_expr[i] is not None for i in range(n_signals)])
    stats = {"loaded": loaded, "skipped_ohlcv": skipped_ohlcv, "skipped_expr": skipped_expr,
             "skipped_date": skipped_date, "skipped_fwd": skipped_fwd,
             "n_valid": int(valid_mask.sum())}
    return fwd_expr, fwd_closes, entry_prices, adr_values, signal_meta, valid_mask, stats


# ============================================================
# Extract one expression column into padded 2D (RAM-safe)
# ============================================================

def extract_column_padded(fwd_expr_list, valid_indices, expr_col, max_forward):
    """Extract one expression column from per-signal arrays into a padded 2D array.

    This is the RAM-safe alternative to building a full 3D padded array.
    Each call allocates ~175 KB (364 × 120 × 4 bytes) instead of 2.1 GB.

    Returns:
        col_2d: np.ndarray (n_valid, max_forward) float32, NaN-padded
    """
    n_valid = len(valid_indices)
    col_2d = np.full((n_valid, max_forward), np.nan, dtype=np.float32)
    for vi, si in enumerate(valid_indices):
        fe = fwd_expr_list[si]
        nb = fe.shape[0]
        col_2d[vi, :nb] = fe[:, expr_col]
    return col_2d


# ============================================================
# Weighted Stats
# ============================================================

def compute_weighted_stats(captured_adr, weights, triggered, move_adrs_actual,
                           n_bars_held):
    """Compute full weighted stats panel for one exit candidate."""
    n = len(captured_adr)
    if n < 2:
        return None

    w = weights.copy()
    w_sum = w.sum()
    if w_sum < 1e-10:
        return None

    is_win = captured_adr > 0
    is_loss = ~is_win

    wr = float(np.sum(w[is_win]) / w_sum)
    expectancy = float(np.sum(captured_adr * w) / w_sum)

    w_var = np.sum(w * (captured_adr - expectancy) ** 2) / w_sum
    w_std = float(np.sqrt(max(w_var, 0.0)))

    n_eff = (w_sum ** 2) / np.sum(w ** 2) if np.sum(w ** 2) > 0 else 1.0
    sqn = float(np.sqrt(n_eff) * expectancy / w_std) if w_std > 0 else 0.0

    gross_w = float(np.sum(captured_adr[is_win] * w[is_win])) if is_win.any() else 0.0
    gross_l = float(np.abs(np.sum(captured_adr[is_loss] * w[is_loss]))) if is_loss.any() else 0.001
    pf = gross_w / gross_l if gross_l > 0 else 999.0

    def _wm(arr, m, wt):
        if not m.any(): return 0.0
        s = wt[m].sum()
        return float(np.sum(arr[m] * wt[m]) / s) if s > 0 else 0.0

    avg_w = _wm(captured_adr, is_win, w)
    avg_l = _wm(captured_adr, is_loss, w)
    pr = abs(avg_w / avg_l) if avg_l != 0 else 999.0

    wa = captured_adr[is_win]
    la = captured_adr[is_loss]

    eq = _build_weighted_equity_curve(captured_adr, w, INITIAL_CAPITAL, RISK_PER_TRADE)
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
    max_dd = float(np.max(dd))
    avg_dd = float(np.mean(dd[dd > 0])) if (dd > 0).any() else 0.0

    total_bars = int(np.sum(n_bars_held))
    avg_bars = float(np.mean(n_bars_held))
    years = total_bars / TRADING_DAYS_PER_YEAR if total_bars > 0 else 1.0
    total_ret = eq[-1] / eq[0] if eq[0] > 0 else 1.0
    cagr = float((total_ret ** (1 / years) - 1)) if years > 0 and total_ret > 0 else 0.0

    tpy = TRADING_DAYS_PER_YEAR / avg_bars if avg_bars > 0 else 1.0
    ann_r = expectancy * tpy
    ann_std = w_std * np.sqrt(tpy)
    sharpe = float(ann_r / ann_std) if ann_std > 0 else 0.0

    ds = captured_adr[captured_adr < 0]
    ds_std = float(np.std(ds, ddof=1)) if len(ds) > 1 else 1.0
    sortino = float(ann_r / (ds_std * np.sqrt(tpy))) if ds_std > 0 else 0.0
    calmar = float(cagr / max_dd) if max_dd > 0 else 999.0

    tm = triggered & (move_adrs_actual > 0)
    if tm.any():
        ce = captured_adr[tm] / move_adrs_actual[tm]
        med_cap, floor_cap, mean_cap = float(np.median(ce)), float(np.min(ce)), float(np.mean(ce))
    else:
        med_cap = floor_cap = mean_cap = 0.0

    return {
        "n_signals": n, "n_triggered": int(triggered.sum()),
        "trigger_rate": round(triggered.sum() / n, 4),
        "n_winners": int(is_win.sum()), "n_losers": int(is_loss.sum()),
        "win_rate": round(wr, 4), "expectancy": round(expectancy, 4),
        "sqn": round(sqn, 4),
        "profit_factor": round(min(pf, 999.0), 4),
        "payoff_ratio": round(min(pr, 999.0), 4),
        "avg_win_adr": round(avg_w, 4), "avg_loss_adr": round(avg_l, 4),
        "median_win_adr": round(float(np.median(wa)), 4) if len(wa) > 0 else 0.0,
        "median_loss_adr": round(float(np.median(la)), 4) if len(la) > 0 else 0.0,
        "best_win_adr": round(float(np.max(wa)), 4) if len(wa) > 0 else 0.0,
        "worst_loss_adr": round(float(np.min(la)), 4) if len(la) > 0 else 0.0,
        "std_adr": round(w_std, 4),
        "max_consec_winners": _max_consecutive(is_win),
        "max_consec_losers": _max_consecutive(is_loss),
        "avg_bars_winners": round(float(np.mean(n_bars_held[is_win])), 1) if is_win.any() else 0.0,
        "avg_bars_losers": round(float(np.mean(n_bars_held[is_loss])), 1) if is_loss.any() else 0.0,
        "avg_bars_all": round(avg_bars, 1),
        "max_drawdown": round(max_dd, 4), "avg_drawdown": round(avg_dd, 4),
        "max_dd_duration_trades": _max_dd_duration(eq),
        "cagr": round(cagr, 4), "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4), "calmar": round(min(calmar, 999.0), 4),
        "final_equity": round(float(eq[-1]), 2),
        "total_return_pct": round(float((eq[-1] / eq[0] - 1) * 100), 2),
        "median_capture_eff": round(med_cap, 4),
        "floor_capture_eff": round(floor_cap, 4),
        "mean_capture_eff": round(mean_cap, 4),
    }


def _max_consecutive(bool_arr):
    mx = cur = 0
    for v in bool_arr:
        if v: cur += 1; mx = max(mx, cur)
        else: cur = 0
    return mx


def _build_weighted_equity_curve(captured_adr, weights, cap, risk):
    n = len(captured_adr)
    eq = np.zeros(n + 1)
    eq[0] = cap
    for i in range(n):
        eq[i+1] = eq[i] + eq[i] * risk * captured_adr[i] * weights[i]
        if eq[i+1] <= 0:
            eq[i+1:] = 0; break
    return eq


def _max_dd_duration(eq):
    peak = eq[0]; mx = cur = 0
    for v in eq[1:]:
        if v >= peak: peak = v; cur = 0
        else: cur += 1; mx = max(mx, cur)
    return mx


# ============================================================
# 1-Stage Expression Grind (RAM-safe: one column at a time)
# ============================================================

def grind_1stage(fwd_expr_list, fwd_close_list, valid_indices,
                 entry_prices_v, adr_values_v, weights_v, is_hard_gate_v,
                 move_adrs_v, n_bars_per_signal, filtered_names,
                 direction, max_forward):
    """Brute-force all expressions × thresholds × directions for 1-stage exits.

    RAM-safe: extracts one expression column at a time into a small 2D array
    (~175 KB per column) instead of building a 2.1 GB 3D padded array.
    """
    n_valid = len(valid_indices)
    n_exprs = len(filtered_names)
    print(f"\n  ── 1-STAGE EXPRESSION GRIND ──")
    print(f"  {n_valid} signals × {n_exprs} expressions × ~{N_THRESHOLDS} thresholds × 2 directions")
    print(f"  Hard gate signals: {int(is_hard_gate_v.sum())}")
    print(f"  RAM-safe mode: one column at a time (~175 KB per extraction)")

    t0 = time.time()
    candidates = []
    tested = 0
    hard_gate_fails = 0

    # Pre-build padded close array (tiny: 364 × 120 × 8 bytes = 349 KB)
    close_2d = np.full((n_valid, max_forward), np.nan, dtype=np.float64)
    for vi, si in enumerate(valid_indices):
        fc = fwd_close_list[si]
        close_2d[vi, :len(fc)] = fc

    # Pre-build bar validity mask
    bar_valid = np.zeros((n_valid, max_forward), dtype=bool)
    for vi in range(n_valid):
        bar_valid[vi, :n_bars_per_signal[vi]] = True

    # Bar index array for vectorized first-bar finding
    bar_indices = np.arange(max_forward)[np.newaxis, :]  # (1, max_forward)

    for expr_i in range(n_exprs):
        if (expr_i + 1) % 1000 == 0:
            elapsed = time.time() - t0
            rate = (expr_i + 1) / elapsed if elapsed > 0 else 0
            print(f"    [{expr_i+1}/{n_exprs}] {rate:.0f} expr/s, "
                  f"{len(candidates)} candidates, {tested:,} tested, "
                  f"{hard_gate_fails:,} gate fails")

        # Extract one column: (n_valid, max_forward) — ~175 KB
        col = extract_column_padded(fwd_expr_list, valid_indices, expr_i, max_forward)

        # Finite mask for this column
        finite_mask = np.isfinite(col) & bar_valid
        finite_vals = col[finite_mask]
        if len(finite_vals) < n_valid:
            continue

        # Thresholds
        pcts = np.linspace(5, 95, N_THRESHOLDS)
        thresholds = np.unique(np.percentile(finite_vals, pcts))
        if len(thresholds) < 2:
            continue

        expr_name = filtered_names[expr_i]

        for thresh in thresholds:
            for dir_label, above in [("above", True), ("below", False)]:
                tested += 1

                if above:
                    hit = (col >= thresh) & finite_mask
                else:
                    hit = (col <= thresh) & finite_mask

                # First triggering bar per signal
                hit_bars = np.where(hit, bar_indices, max_forward + 1)
                first_bar = np.min(hit_bars, axis=1)
                triggered = first_bar < max_forward + 1

                # Hard gate
                if not triggered[is_hard_gate_v].all():
                    hard_gate_fails += 1
                    continue

                # Captured move
                captured_adr = np.full(n_valid, -LOSS_ASSUMPTION_ADR, dtype=np.float64)
                bars_held = np.full(n_valid, max_forward, dtype=np.int32)

                trig_idx = np.where(triggered)[0]
                for vi in trig_idx:
                    fb = first_bar[vi]
                    ec = close_2d[vi, fb]
                    if np.isfinite(ec) and adr_values_v[vi] > 0:
                        if direction == "short":
                            captured_adr[vi] = (entry_prices_v[vi] - ec) / adr_values_v[vi]
                        else:
                            captured_adr[vi] = (ec - entry_prices_v[vi]) / adr_values_v[vi]
                    bars_held[vi] = fb + 1

                stats = compute_weighted_stats(
                    captured_adr, weights_v, triggered, move_adrs_v, bars_held)
                if stats is None:
                    continue

                stats["expr_name"] = expr_name
                stats["direction"] = dir_label
                stats["threshold"] = round(float(thresh), 6)
                candidates.append(stats)

    elapsed = time.time() - t0
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
    parser = argparse.ArgumentParser(description="Profit Grinder — Phase 4")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--direction", default=None)
    parser.add_argument("--ev-file", default=None)
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
    print(f"  Loss assumption: {LOSS_ASSUMPTION_ADR} ADR")
    t_total = time.time()

    # ── Load data ──
    print(f"\n  ── LOADING DATA ──")
    ev_data, ev_path = load_ev_data(setup, args.ev_file)
    entry_scores = load_entry_scores(setup)
    example_keys, rejected_keys = load_vetting_decisions(setup)

    # ── Build population ──
    print(f"\n  ── BUILDING POPULATION ──")
    signals, counts = build_signal_population(ev_data, entry_scores, example_keys, rejected_keys)

    print(f"\n  Population: {counts['total']} signals")
    print(f"    Examples: {counts['examples']}  Unvetted: {counts['unvetted']}  "
          f"Rejected: {counts['rejected']}  No score: {counts['no_score']}")

    if counts["no_score"] > 0:
        print(f"  ⚠ {counts['no_score']} signals missing entry_candle_score")

    weights = np.array([s["weight"] for s in signals])
    print(f"  Weights: min={weights.min():.4f} med={np.median(weights):.4f} "
          f"max={weights.max():.4f} sum={weights.sum():.1f}")

    hard_gate = sum(1 for s in signals if s["weight_category"] in ("example", "vetted_yes"))
    print(f"  Hard gate (must trigger): {hard_gate}")

    if counts["total"] < 5:
        print(f"  ERROR: Need at least 5 signals."); sys.exit(1)

    # ── Expression cache ──
    print(f"\n  ── EXPRESSION CACHE ──")
    from expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache invalid."); sys.exit(1)

    expr_col_map, filtered_names, n_excluded = build_expr_col_map(expr_cache.expr_names)
    n_filtered = len(expr_col_map)
    print(f"  {len(expr_cache.expr_names)} total, {n_excluded} boolean excluded, {n_filtered} for search")

    # ── Forward matrices ──
    print(f"\n  ── FORWARD MATRIX CONSTRUCTION ──")
    ohlcv_cache = load_5yr_cache()
    fwd_expr, fwd_closes, entry_prices, adr_values, signal_meta, valid_mask, bstats = \
        build_forward_data(signals, ohlcv_cache, expr_cache, expr_col_map,
                           direction, args.max_forward)
    del ohlcv_cache

    print(f"  Loaded: {bstats['loaded']}  Valid: {bstats['n_valid']}")
    if bstats['loaded'] < counts['total']:
        print(f"  Skipped: ohlcv={bstats['skipped_ohlcv']} expr={bstats['skipped_expr']} "
              f"date={bstats['skipped_date']} fwd={bstats['skipped_fwd']}")

    # Verify examples
    ex_loaded = sum(1 for m in signal_meta if m.get("status") == "ok" and m.get("is_example"))
    if ex_loaded < counts["examples"]:
        print(f"  ✗ HARD FAIL: {ex_loaded}/{counts['examples']} examples loaded"); sys.exit(1)
    print(f"  ✓ All {counts['examples']} examples loaded")

    # ── Prepare grind arrays ──
    valid_indices = np.where(valid_mask)[0]

    # Sort chronologically for equity curve
    sig_dates = [signals[si]["date"] for si in valid_indices]
    date_order = np.argsort(sig_dates)
    valid_indices = valid_indices[date_order]

    weights_v = np.array([signals[si]["weight"] for si in valid_indices])
    entry_prices_v = entry_prices[valid_indices]
    adr_values_v = adr_values[valid_indices]
    move_adrs_v = np.array([signals[si].get("move_adr", 0) or 0 for si in valid_indices])
    is_hard_gate_v = np.array([signals[si]["weight_category"] in ("example", "vetted_yes")
                               for si in valid_indices])
    n_bars_per = np.array([fwd_expr[si].shape[0] for si in valid_indices], dtype=np.int32)

    # Estimate RAM: per-signal arrays are already allocated, grind adds ~175 KB per column extraction
    total_fwd_bytes = sum(fwd_expr[si].nbytes for si in valid_indices)
    print(f"\n  Forward data RAM: {total_fwd_bytes / 1e9:.2f} GB (spread across {len(valid_indices)} arrays)")
    print(f"  Grind will extract one column at a time (~{364 * args.max_forward * 4 / 1024:.0f} KB per extraction)")

    # ── Grind ──
    candidates = grind_1stage(
        fwd_expr, fwd_closes, valid_indices,
        entry_prices_v, adr_values_v, weights_v, is_hard_gate_v,
        move_adrs_v, n_bars_per, filtered_names, direction, args.max_forward)

    # ── Summary ──
    elapsed = time.time() - t_total
    print(f"\n  {'='*50}")
    print(f"  PROFIT GRINDER COMPLETE ({elapsed:.1f}s / {elapsed/60:.1f} min)")
    print(f"  {'='*50}")
    print(f"  Signals: {counts['total']}  Expressions: {n_filtered}  Candidates: {len(candidates)}")
    print(f"  EV source: {os.path.basename(ev_path)}")
    print(f"  {'='*50}")


if __name__ == "__main__":
    main()
