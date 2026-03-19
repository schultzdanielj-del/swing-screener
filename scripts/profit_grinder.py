"""
Profit Grinder — Phase 4: Trade Exit Optimization

Brute-forces stop/target/trail/trim parameters against post-entry OHLCV price
action for all signals where the exit condition fired (winners with move_adr).

Three trim modes computed independently — all stored in one output file:
  1-stage: 100% exit at target (pure stop/target)
  2-stage: trim X% at target_1, remainder at target_2/trail/stop/time
  3-stage: trim X% at target_1, Y% at target_2, remainder at target_3/trail/stop/time

The UI (scan tuning) picks which trim mode to display and which metric to rank by.
All data is pre-computed — no re-run needed for any UI interaction.

Reads from:
  - EV grinder output (signals with move_adr = exit signal fired)
  - 5yr OHLCV cache (forward price bars for trade simulation)

Outputs:
  - Three result sets (1/2/3-stage), each containing:
    - Full combo grid with stats (params + ~30 metrics per combo)
    - Top N combos per mode with per-trade detail + equity curves
    - Best combo per metric
  - Per-trade R-multiples include quality_score + killed_at_depth so UI can
    re-slice by quality or refinement depth without re-running

Usage:
    python scripts/profit_grinder.py --setup dtss
    python scripts/profit_grinder.py --setup dtss --workers 12 --max-forward 120
"""

import argparse
import sys
import os
import time
import json
import numpy as np
import pickle
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DEFAULT_WORKERS = os.cpu_count() or 8
MAX_FORWARD_DEFAULT = 120

# Equity simulation
INITIAL_CAPITAL = 100_000
RISK_PER_TRADE = 0.01
TRADING_DAYS_PER_YEAR = 252

# Top N combos per mode to store full trade detail + equity curves
TOP_N_DETAIL = 100

# ============================================================
# Grid Definitions
# ============================================================
# All targets/stops in ADR units

STOPS = (0.5, 3.0, 0.25)              # 11 values
TRAIL_ACTIVATES = (1.0, 4.0, 0.5)     # 7 values
TRAIL_DISTANCES = (0.5, 2.0, 0.25)    # 7 values
TIME_STOP = 60                          # max bars to hold

# 1-stage: 100% exit at target
TARGETS_1 = (1.0, 12.0, 0.5)          # 23 values

# 2-stage: trim at target_1, remainder at target_2/trail/stop
TARGETS_2A = (1.0, 8.0, 1.0)          # 8 values
TARGETS_2B = (2.0, 12.0, 1.5)         # 7 values
TRIMS_2 = [0.25, 0.33, 0.50]          # 3 values

# 3-stage: trim at t1, trim at t2, remainder at t3/trail/stop
TARGETS_3A = (1.0, 6.0, 1.0)          # 6 values
TARGETS_3B = (2.0, 8.0, 1.5)          # 5 values
TARGETS_3C = (4.0, 12.0, 2.0)         # 5 values
TRIMS_3 = [0.25, 0.33]                # 2 values per stage


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
    print(f"Loading EV grinder data from {os.path.basename(path)}...")
    with open(path, "r") as f:
        data = json.load(f)
    print(f"  {len(data.get('signals', []))} total signals")
    return data, path


# ============================================================
# Signal Selection — exit-fired signals only
# ============================================================

def select_signals(ev_data):
    """
    Select signals where the exit condition fired (move_adr is not null).
    These are the only signals with meaningful forward trade data.
    """
    raw = ev_data.get("signals", [])
    passed = []
    skipped_no_move = 0
    skipped_no_entry = 0

    for sig in raw:
        if sig.get("move_adr") is None:
            skipped_no_move += 1
            continue
        if sig.get("entry_high") is None or sig.get("adr_at_signal") is None:
            skipped_no_entry += 1
            continue
        if sig["adr_at_signal"] <= 0:
            skipped_no_entry += 1
            continue
        passed.append(sig)

    print(f"\nSignal selection (exit condition fired):")
    print(f"  Total: {len(raw)}")
    print(f"  No exit signal (move_adr=null): {skipped_no_move}")
    print(f"  Missing entry/ADR data: {skipped_no_entry}")
    print(f"  Selected: {len(passed)}")
    return passed


# ============================================================
# Trade Data Preparation
# ============================================================

def build_trade_arrays(signals, ohlcv_cache, direction, max_forward):
    """
    Build contiguous numpy arrays for vectorized trade simulation.
    Slices forward OHLCV from the bar after each signal bar.
    """
    import pandas as pd

    n = len(signals)
    fwd_highs = np.full((n, max_forward), np.nan, dtype=np.float64)
    fwd_lows = np.full((n, max_forward), np.nan, dtype=np.float64)
    fwd_closes = np.full((n, max_forward), np.nan, dtype=np.float64)
    entry_prices = np.zeros(n, dtype=np.float64)
    adr_values = np.zeros(n, dtype=np.float64)
    n_bars = np.zeros(n, dtype=np.int32)

    # Per-signal metadata for output
    signal_meta = []
    loaded = 0
    skipped = 0

    # Group by ticker for efficient OHLCV lookup
    ticker_groups = {}
    for i, sig in enumerate(signals):
        t = sig["ticker"]
        if t not in ticker_groups:
            ticker_groups[t] = []
        ticker_groups[t].append(i)

    for ticker, indices in ticker_groups.items():
        df = ohlcv_cache.get(ticker)
        if df is None:
            skipped += len(indices)
            continue

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

        date_strs = df["date"].dt.strftime("%Y-%m-%d").values
        date_to_idx = {d: idx for idx, d in enumerate(date_strs)}
        h_arr = df["high"].values.astype(np.float64)
        l_arr = df["low"].values.astype(np.float64)
        c_arr = df["close"].values.astype(np.float64)

        for i in indices:
            sig = signals[i]
            bar_idx = date_to_idx.get(sig["date"])
            if bar_idx is None:
                skipped += 1
                continue

            if direction == "short":
                entry_prices[i] = sig["entry_high"]
            else:
                entry_prices[i] = l_arr[bar_idx]

            adr_values[i] = sig["adr_at_signal"]

            start = bar_idx + 1
            end = min(start + max_forward, len(df))
            actual = end - start

            if actual <= 0:
                skipped += 1
                continue

            fwd_highs[i, :actual] = h_arr[start:end]
            fwd_lows[i, :actual] = l_arr[start:end]
            fwd_closes[i, :actual] = c_arr[start:end]
            n_bars[i] = actual

            fwd_dates = date_strs[start:end].tolist()

            signal_meta.append({
                "idx": i,
                "ticker": ticker,
                "signal_date": sig["date"],
                "entry_price": float(entry_prices[i]),
                "adr": float(adr_values[i]),
                "classification": sig["classification"],
                "is_example": sig.get("is_example", False),
                "quality_score": sig.get("quality_score", 0),
                "predicted_wr": sig.get("predicted_wr", 0),
                "predicted_mfe": sig.get("predicted_mfe", 0),
                "ev": sig.get("ev", 0),
                "move_adr": sig.get("move_adr"),
                "killed_at_depth": sig.get("killed_at_depth"),
                "fwd_dates": fwd_dates,
            })
            loaded += 1

    valid_mask = n_bars > 0

    print(f"\nTrade data built:")
    print(f"  Loaded: {loaded}, Skipped: {skipped}")
    if (n_bars > 0).any():
        print(f"  Forward bars: min={n_bars[valid_mask].min()}, "
              f"max={n_bars.max()}, median={int(np.median(n_bars[valid_mask]))}")

    return {
        "fwd_highs": fwd_highs,
        "fwd_lows": fwd_lows,
        "fwd_closes": fwd_closes,
        "entry_prices": entry_prices,
        "adr_values": adr_values,
        "n_bars": n_bars,
        "valid_mask": valid_mask,
        "signal_meta": signal_meta,
        "n_valid": int(valid_mask.sum()),
    }


# ============================================================
# Trade Simulation — Vectorized, Multi-Stage Trim
# ============================================================

REASON_NAMES = {0: "stop", 1: "target_1", 2: "target_2", 3: "target_3",
                4: "trail", 5: "time"}


def simulate_trades(trade_data, direction, stop_adr, targets, trim_pcts,
                    trail_activate_adr, trail_distance_adr, time_stop, max_forward):
    """
    Simulate all trades for one parameter combo with multi-stage trim.

    targets: list of 1-3 target levels in ADR units (ascending).
    trim_pcts: list matching targets. Last entry is ignored (remainder exits there
               or via trail/stop/time). For 1-stage, targets=[T], trim_pcts=[1.0].
               For 2-stage, targets=[T1, T2], trim_pcts=[0.33, 1.0].
               For 3-stage, targets=[T1, T2, T3], trim_pcts=[0.25, 0.33, 1.0].

    trim_pcts[i] = fraction of ORIGINAL position to sell at targets[i].
    The last target sells whatever remains.
    """
    fwd_highs = trade_data["fwd_highs"]
    fwd_lows = trade_data["fwd_lows"]
    fwd_closes = trade_data["fwd_closes"]
    entry_prices = trade_data["entry_prices"]
    adr_values = trade_data["adr_values"]
    n_bars_arr = trade_data["n_bars"]
    valid_mask = trade_data["valid_mask"]

    n_signals = len(entry_prices)
    n_stages = len(targets)

    # Precompute price levels
    # Shorts: stop ABOVE entry (price up = loss), target BELOW (price down = win)
    # Longs:  stop BELOW entry (price down = loss), target ABOVE (price up = win)
    if direction == "short":
        stop_prices = entry_prices + stop_adr * adr_values
        target_prices = [entry_prices - t * adr_values for t in targets]
        trail_activate_price = entry_prices - trail_activate_adr * adr_values \
            if trail_activate_adr > 0 else None
    else:
        stop_prices = entry_prices - stop_adr * adr_values
        target_prices = [entry_prices + t * adr_values for t in targets]
        trail_activate_price = entry_prices + trail_activate_adr * adr_values \
            if trail_activate_adr > 0 else None

    # Output arrays
    exit_bar = np.full(n_signals, -1, dtype=np.int32)
    exit_reason = np.full(n_signals, -1, dtype=np.int8)
    total_r = np.full(n_signals, np.nan, dtype=np.float64)
    mfe_during = np.full(n_signals, np.nan, dtype=np.float64)
    mae_during = np.full(n_signals, np.nan, dtype=np.float64)
    bars_held = np.full(n_signals, 0, dtype=np.int32)

    # Per-signal state
    remaining_pct = np.ones(n_signals, dtype=np.float64)
    realized_r = np.zeros(n_signals, dtype=np.float64)
    current_stage = np.zeros(n_signals, dtype=np.int32)  # which target we're waiting for next
    trailing_active = np.zeros(n_signals, dtype=np.bool_)
    trail_stop_price = np.full(n_signals, np.nan, dtype=np.float64)
    best_price = entry_prices.copy()

    risk_per_share = stop_adr * adr_values
    eff_time_stop = time_stop if time_stop > 0 else max_forward

    for bar in range(max_forward):
        active = valid_mask & (exit_bar < 0) & (bar < n_bars_arr)
        if not active.any():
            break

        h = fwd_highs[:, bar]
        l = fwd_lows[:, bar]
        c = fwd_closes[:, bar]

        if direction == "short":
            best_price = np.where(active & (l < best_price), l, best_price)
            stopped = active & (h >= stop_prices)
            mfe_during = np.where(active & (np.isnan(mfe_during) | (l < mfe_during)), l, mfe_during)
            mae_during = np.where(active & (np.isnan(mae_during) | (h > mae_during)), h, mae_during)
        else:
            best_price = np.where(active & (h > best_price), h, best_price)
            stopped = active & (l <= stop_prices)
            mfe_during = np.where(active & (np.isnan(mfe_during) | (h > mfe_during)), h, mfe_during)
            mae_during = np.where(active & (np.isnan(mae_during) | (l < mae_during)), l, mae_during)

        # Stop loss: close entire remaining position
        stop_hit = stopped & (exit_bar < 0)
        if stop_hit.any():
            stop_r = -1.0 * remaining_pct[stop_hit]  # -1R per unit remaining
            realized_r[stop_hit] += stop_r
            remaining_pct[stop_hit] = 0.0
            exit_bar = np.where(stop_hit, bar, exit_bar)
            exit_reason = np.where(stop_hit, 0, exit_reason)
            bars_held = np.where(stop_hit, bar + 1, bars_held)
            total_r = np.where(stop_hit, realized_r, total_r)

        # Check targets (stage by stage)
        still_open = active & (exit_bar < 0)
        for stage_idx in range(n_stages):
            if not still_open.any():
                break

            at_this_stage = still_open & (current_stage == stage_idx)
            if not at_this_stage.any():
                continue

            tp = target_prices[stage_idx]
            if direction == "short":
                hit = at_this_stage & (l <= tp)
            else:
                hit = at_this_stage & (h >= tp)

            if not hit.any():
                continue

            is_final = (stage_idx == n_stages - 1)
            if is_final:
                # Sell all remaining at this target
                pct_to_sell = remaining_pct[hit]
            else:
                pct_to_sell = np.full(hit.sum(), trim_pcts[stage_idx])
                # Don't sell more than remaining
                pct_to_sell = np.minimum(pct_to_sell, remaining_pct[hit])

            # R-multiple for this trim: (target_adr / stop_adr) * pct_sold
            stage_r = (targets[stage_idx] / stop_adr) * pct_to_sell
            realized_r[hit] += stage_r
            remaining_pct[hit] -= pct_to_sell
            current_stage[hit] = stage_idx + 1

            if is_final:
                # Trade fully closed at final target
                exit_bar = np.where(hit, bar, exit_bar)
                exit_reason = np.where(hit, stage_idx + 1, exit_reason)  # 1=target_1, 2=target_2, 3=target_3
                bars_held = np.where(hit, bar + 1, bars_held)
                total_r = np.where(hit, realized_r, total_r)
                still_open = still_open & ~hit

        # Trail activation + trail stop (for trimmed remainder after last target)
        still_open = active & (exit_bar < 0)
        if trail_activate_adr > 0 and still_open.any():
            # Only activate trailing once past all targets (all trims done)
            past_targets = still_open & (current_stage >= n_stages)
            if direction == "short":
                newly_trailing = past_targets & ~trailing_active & (l <= trail_activate_price)
            else:
                newly_trailing = past_targets & ~trailing_active & (h >= trail_activate_price)
            trailing_active = trailing_active | newly_trailing

            trail_mask = still_open & trailing_active
            if trail_mask.any():
                if direction == "short":
                    new_trail = best_price + trail_distance_adr * adr_values
                    trail_stop_price = np.where(
                        trail_mask & (np.isnan(trail_stop_price) | (new_trail < trail_stop_price)),
                        new_trail, trail_stop_price)
                    trail_hit = trail_mask & (h >= trail_stop_price)
                else:
                    new_trail = best_price - trail_distance_adr * adr_values
                    trail_stop_price = np.where(
                        trail_mask & (np.isnan(trail_stop_price) | (new_trail > trail_stop_price)),
                        new_trail, trail_stop_price)
                    trail_hit = trail_mask & (l <= trail_stop_price)

                if trail_hit.any():
                    # Close remaining at trail stop
                    if direction == "short":
                        trail_r_per_unit = (entry_prices - trail_stop_price) / risk_per_share
                    else:
                        trail_r_per_unit = (trail_stop_price - entry_prices) / risk_per_share
                    realized_r[trail_hit] += trail_r_per_unit[trail_hit] * remaining_pct[trail_hit]
                    remaining_pct[trail_hit] = 0.0
                    exit_bar = np.where(trail_hit, bar, exit_bar)
                    exit_reason = np.where(trail_hit, 4, exit_reason)  # trail
                    bars_held = np.where(trail_hit, bar + 1, bars_held)
                    total_r = np.where(trail_hit, realized_r, total_r)

        # Time stop
        if bar + 1 >= eff_time_stop:
            time_hit = active & (exit_bar < 0)
            if time_hit.any():
                if direction == "short":
                    time_r_per_unit = (entry_prices - c) / risk_per_share
                else:
                    time_r_per_unit = (c - entry_prices) / risk_per_share
                realized_r[time_hit] += time_r_per_unit[time_hit] * remaining_pct[time_hit]
                remaining_pct[time_hit] = 0.0
                exit_bar = np.where(time_hit, bar, exit_bar)
                exit_reason = np.where(time_hit, 5, exit_reason)  # time
                bars_held = np.where(time_hit, bar + 1, bars_held)
                total_r = np.where(time_hit, realized_r, total_r)

    # Still open after max_forward — close at last close
    still_open = valid_mask & (exit_bar < 0)
    if still_open.any():
        last_idx = np.clip(n_bars_arr - 1, 0, max_forward - 1)
        last_c = np.array([fwd_closes[i, last_idx[i]] if last_idx[i] >= 0 else np.nan
                           for i in range(n_signals)])
        if direction == "short":
            end_r = (entry_prices - last_c) / risk_per_share
        else:
            end_r = (last_c - entry_prices) / risk_per_share
        realized_r[still_open] += end_r[still_open] * remaining_pct[still_open]
        remaining_pct[still_open] = 0.0
        exit_bar = np.where(still_open, last_idx, exit_bar)
        exit_reason = np.where(still_open, 5, exit_reason)
        bars_held = np.where(still_open, last_idx + 1, bars_held)
        total_r = np.where(still_open, realized_r, total_r)

    # MFE/MAE in ADR units
    if direction == "short":
        mfe_adr = (entry_prices - mfe_during) / adr_values
        mae_adr = (mae_during - entry_prices) / adr_values
    else:
        mfe_adr = (mfe_during - entry_prices) / adr_values
        mae_adr = (entry_prices - mae_during) / adr_values

    return {
        "exit_bar": exit_bar,
        "exit_reason": exit_reason,
        "r_multiple": total_r,
        "mfe_adr": mfe_adr,
        "mae_adr": mae_adr,
        "bars_held": bars_held,
        "valid_mask": valid_mask & (exit_bar >= 0),
    }


# ============================================================
# Stats Computation
# ============================================================

def compute_stats(sim_result):
    """Compute full stats panel from simulation results."""
    mask = sim_result["valid_mask"]
    r = sim_result["r_multiple"][mask]
    bars = sim_result["bars_held"][mask]
    reasons = sim_result["exit_reason"][mask]
    mfe = sim_result["mfe_adr"][mask]
    mae = sim_result["mae_adr"][mask]

    n = len(r)
    if n < 2:
        return None

    winners = r > 0
    losers = r <= 0
    n_win = int(winners.sum())
    n_loss = int(losers.sum())
    wr = n_win / n

    avg_win_r = float(np.mean(r[winners])) if n_win > 0 else 0.0
    avg_loss_r = float(np.mean(r[losers])) if n_loss > 0 else 0.0
    median_win_r = float(np.median(r[winners])) if n_win > 0 else 0.0
    median_loss_r = float(np.median(r[losers])) if n_loss > 0 else 0.0
    best_r = float(np.max(r)) if n > 0 else 0.0
    worst_r = float(np.min(r)) if n > 0 else 0.0

    expectancy = float(np.mean(r))
    std_r = float(np.std(r, ddof=1)) if n > 1 else 0.0
    sqn = float(np.sqrt(n) * expectancy / std_r) if std_r > 0 else 0.0

    gross_wins = float(np.sum(r[winners])) if n_win > 0 else 0.0
    gross_losses = float(np.abs(np.sum(r[losers]))) if n_loss > 0 else 0.001
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
    payoff_ratio = abs(avg_win_r / avg_loss_r) if avg_loss_r != 0 else float('inf')

    max_cw = _max_consecutive(winners)
    max_cl = _max_consecutive(losers)

    equity = _build_equity_curve(r, INITIAL_CAPITAL, RISK_PER_TRADE)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, 1)
    max_dd = float(np.max(dd))
    avg_dd = float(np.mean(dd[dd > 0])) if (dd > 0).any() else 0.0
    max_dd_dur = _max_dd_duration(equity)

    total_ret = equity[-1] / equity[0] if equity[0] > 0 else 1.0
    total_bars_held = int(np.sum(bars))
    years = total_bars_held / TRADING_DAYS_PER_YEAR if total_bars_held > 0 else 1.0
    cagr = (total_ret ** (1 / years) - 1) if years > 0 and total_ret > 0 else 0.0

    avg_bars = float(np.mean(bars)) if n > 0 else 1.0
    trades_per_year = TRADING_DAYS_PER_YEAR / avg_bars if avg_bars > 0 else 1.0
    annual_r = expectancy * trades_per_year
    annual_std = std_r * np.sqrt(trades_per_year)
    sharpe = annual_r / annual_std if annual_std > 0 else 0.0

    downside = r[r < 0]
    ds_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 1.0
    annual_ds = ds_std * np.sqrt(trades_per_year)
    sortino = annual_r / annual_ds if annual_ds > 0 else 0.0
    calmar = cagr / max_dd if max_dd > 0 else float('inf')

    reason_counts = {}
    for code, name in REASON_NAMES.items():
        reason_counts[name] = int((reasons == code).sum())

    mfe_v = mfe[np.isfinite(mfe)]
    mae_v = mae[np.isfinite(mae)]

    avg_bars_win = float(np.mean(bars[winners])) if n_win > 0 else 0.0
    avg_bars_loss = float(np.mean(bars[losers])) if n_loss > 0 else 0.0

    return {
        "n_trades": n, "n_winners": n_win, "n_losers": n_loss,
        "win_rate": round(wr, 4),
        "expectancy": round(expectancy, 4),
        "sqn": round(sqn, 4),
        "profit_factor": round(profit_factor, 4),
        "payoff_ratio": round(payoff_ratio, 4),
        "avg_win_r": round(avg_win_r, 4),
        "avg_loss_r": round(avg_loss_r, 4),
        "median_win_r": round(median_win_r, 4),
        "median_loss_r": round(median_loss_r, 4),
        "best_r": round(best_r, 4),
        "worst_r": round(worst_r, 4),
        "total_r": round(float(np.sum(r)), 4),
        "std_r": round(std_r, 4),
        "max_consec_winners": max_cw,
        "max_consec_losers": max_cl,
        "max_drawdown": round(max_dd, 4),
        "avg_drawdown": round(avg_dd, 4),
        "max_dd_duration_trades": max_dd_dur,
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "exit_reasons": reason_counts,
        "avg_bars_winners": round(avg_bars_win, 1),
        "avg_bars_losers": round(avg_bars_loss, 1),
        "avg_bars_all": round(float(np.mean(bars)), 1),
        "mfe_median_adr": round(float(np.median(mfe_v)), 2) if len(mfe_v) > 0 else None,
        "mfe_avg_adr": round(float(np.mean(mfe_v)), 2) if len(mfe_v) > 0 else None,
        "mae_median_adr": round(float(np.median(mae_v)), 2) if len(mae_v) > 0 else None,
        "mae_avg_adr": round(float(np.mean(mae_v)), 2) if len(mae_v) > 0 else None,
        "final_equity": round(float(equity[-1]), 2),
        "total_return_pct": round(float((equity[-1] / equity[0] - 1) * 100), 2),
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


def _build_equity_curve(r_multiples, initial_capital, risk_pct):
    eq = np.zeros(len(r_multiples) + 1)
    eq[0] = initial_capital
    for i, r in enumerate(r_multiples):
        eq[i + 1] = eq[i] + eq[i] * risk_pct * r
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
# Parameter Grid Builders
# ============================================================

def _trail_combos():
    """Generate trail parameter combos: no-trail + all trail combos."""
    combos = [(0.0, 0.0)]  # no trail
    for ta in np.arange(*TRAIL_ACTIVATES[:2], TRAIL_ACTIVATES[2]):
        for td in np.arange(*TRAIL_DISTANCES[:2], TRAIL_DISTANCES[2]):
            combos.append((round(float(ta), 4), round(float(td), 4)))
    return combos


def build_grid_1stage():
    """1-stage: 100% exit at target."""
    stops = np.arange(STOPS[0], STOPS[1] + 0.001, STOPS[2])
    targets = np.arange(TARGETS_1[0], TARGETS_1[1] + 0.001, TARGETS_1[2])
    trails = _trail_combos()
    combos = []
    for s in stops:
        for t in targets:
            if t <= s:
                continue
            for ta, td in trails:
                combos.append({
                    "stop_adr": round(float(s), 4),
                    "targets": [round(float(t), 4)],
                    "trim_pcts": [1.0],
                    "trail_activate_adr": ta,
                    "trail_distance_adr": td,
                })
    return combos


def build_grid_2stage():
    """2-stage: trim at target_1, remainder at target_2."""
    stops = np.arange(STOPS[0], STOPS[1] + 0.001, STOPS[2])
    t1s = np.arange(TARGETS_2A[0], TARGETS_2A[1] + 0.001, TARGETS_2A[2])
    t2s = np.arange(TARGETS_2B[0], TARGETS_2B[1] + 0.001, TARGETS_2B[2])
    trails = _trail_combos()
    combos = []
    for s in stops:
        for t1 in t1s:
            if t1 <= s:
                continue
            for t2 in t2s:
                if t2 <= t1:
                    continue
                for trim1 in TRIMS_2:
                    for ta, td in trails:
                        combos.append({
                            "stop_adr": round(float(s), 4),
                            "targets": [round(float(t1), 4), round(float(t2), 4)],
                            "trim_pcts": [trim1, 1.0],
                            "trail_activate_adr": ta,
                            "trail_distance_adr": td,
                        })
    return combos


def build_grid_3stage():
    """3-stage: trim at t1, trim at t2, remainder at t3."""
    stops = np.arange(STOPS[0], STOPS[1] + 0.001, STOPS[2])
    t1s = np.arange(TARGETS_3A[0], TARGETS_3A[1] + 0.001, TARGETS_3A[2])
    t2s = np.arange(TARGETS_3B[0], TARGETS_3B[1] + 0.001, TARGETS_3B[2])
    t3s = np.arange(TARGETS_3C[0], TARGETS_3C[1] + 0.001, TARGETS_3C[2])
    trails = _trail_combos()
    combos = []
    for s in stops:
        for t1 in t1s:
            if t1 <= s:
                continue
            for t2 in t2s:
                if t2 <= t1:
                    continue
                for t3 in t3s:
                    if t3 <= t2:
                        continue
                    for trim1 in TRIMS_3:
                        for trim2 in TRIMS_3:
                            if trim1 + trim2 >= 1.0:
                                continue
                            for ta, td in trails:
                                combos.append({
                                    "stop_adr": round(float(s), 4),
                                    "targets": [round(float(t1), 4), round(float(t2), 4), round(float(t3), 4)],
                                    "trim_pcts": [trim1, trim2, 1.0],
                                    "trail_activate_adr": ta,
                                    "trail_distance_adr": td,
                                })
    return combos


# ============================================================
# Parallel Grinding
# ============================================================

def _grind_one(args_tuple):
    combo, trade_data, direction, time_stop, max_forward = args_tuple
    sim = simulate_trades(
        trade_data, direction,
        stop_adr=combo["stop_adr"],
        targets=combo["targets"],
        trim_pcts=combo["trim_pcts"],
        trail_activate_adr=combo["trail_activate_adr"],
        trail_distance_adr=combo["trail_distance_adr"],
        time_stop=time_stop,
        max_forward=max_forward,
    )
    stats = compute_stats(sim)
    if stats is None:
        return None
    stats["params"] = combo
    # Store R-multiples for UI re-slicing
    valid = sim["valid_mask"]
    stats["_r_multiples"] = sim["r_multiple"][valid].tolist()
    stats["_signal_indices"] = np.where(valid)[0].tolist()
    return stats


def grind_mode(mode_name, combos, trade_data, direction, time_stop, max_forward, workers):
    """Grind all combos for one trim mode."""
    n = len(combos)
    print(f"\n  {mode_name}: {n:,} combos...", flush=True)
    t0 = time.time()

    tasks = [(c, trade_data, direction, time_stop, max_forward) for c in combos]
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_grind_one, t): i for i, t in enumerate(tasks)}
        done = 0
        for future in as_completed(futures):
            r = future.result()
            if r is not None:
                results.append(r)
            done += 1
            if done % 2000 == 0 or done == n:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(f"    [{done:,}/{n:,}] {len(results):,} valid, "
                      f"{elapsed:.0f}s ({rate:.0f}/s)", flush=True)

    elapsed = time.time() - t0
    print(f"    Done: {len(results):,} valid in {elapsed:.1f}s")
    return results


# ============================================================
# Trade Detail for Top Combos
# ============================================================

def build_trade_detail(combo, trade_data, direction, time_stop, max_forward):
    """Re-run simulation and extract per-trade detail."""
    sim = simulate_trades(
        trade_data, direction,
        stop_adr=combo["params"]["stop_adr"],
        targets=combo["params"]["targets"],
        trim_pcts=combo["params"]["trim_pcts"],
        trail_activate_adr=combo["params"]["trail_activate_adr"],
        trail_distance_adr=combo["params"]["trail_distance_adr"],
        time_stop=time_stop,
        max_forward=max_forward,
    )

    meta_lookup = {m["idx"]: m for m in trade_data["signal_meta"]}
    trades = []
    for i in range(len(sim["valid_mask"])):
        if not sim["valid_mask"][i]:
            continue
        meta = meta_lookup.get(i, {})
        eb = int(sim["exit_bar"][i])
        fwd_dates = meta.get("fwd_dates", [])
        trades.append({
            "ticker": meta.get("ticker", ""),
            "signal_date": meta.get("signal_date", ""),
            "entry_price": round(float(trade_data["entry_prices"][i]), 4),
            "exit_date": fwd_dates[eb] if eb < len(fwd_dates) else None,
            "exit_reason": REASON_NAMES.get(int(sim["exit_reason"][i]), "unknown"),
            "r_multiple": round(float(sim["r_multiple"][i]), 4),
            "bars_held": int(sim["bars_held"][i]),
            "mfe_adr": round(float(sim["mfe_adr"][i]), 2) if np.isfinite(sim["mfe_adr"][i]) else None,
            "mae_adr": round(float(sim["mae_adr"][i]), 2) if np.isfinite(sim["mae_adr"][i]) else None,
            "quality_score": meta.get("quality_score", 0),
            "killed_at_depth": meta.get("killed_at_depth"),
        })

    trades.sort(key=lambda t: t["signal_date"])
    r_arr = np.array([t["r_multiple"] for t in trades])
    equity = _build_equity_curve(r_arr, INITIAL_CAPITAL, RISK_PER_TRADE).tolist()
    return trades, equity


# ============================================================
# Output
# ============================================================

def package_mode_results(mode_name, results, trade_data, direction, time_stop, max_forward):
    """Package results for one trim mode."""
    if not results:
        return {"mode": mode_name, "n_combos": 0, "best_per_metric": {},
                "top_combos": [], "grid": []}

    metrics = ["sqn", "expectancy", "cagr", "sharpe", "sortino",
               "calmar", "profit_factor", "win_rate", "total_r"]

    best_per = {}
    for m in metrics:
        by_m = sorted(results, key=lambda s: s.get(m, float('-inf')), reverse=True)
        if by_m:
            best_per[m] = {"params": by_m[0]["params"], "value": by_m[0].get(m, 0)}

    # Sort by SQN for top combos
    results.sort(key=lambda s: s.get("sqn", float('-inf')), reverse=True)

    # Build detail for top N
    top = []
    for combo in results[:TOP_N_DETAIL]:
        trades, equity = build_trade_detail(combo, trade_data, direction, time_stop, max_forward)
        entry = {k: v for k, v in combo.items() if not k.startswith("_")}
        entry["trades"] = trades
        entry["equity_curve"] = equity
        top.append(entry)

    # Grid: stats only (no trades/equity/r_multiples)
    grid = []
    for combo in results:
        entry = {k: v for k, v in combo.items() if not k.startswith("_")}
        grid.append(entry)

    return {
        "mode": mode_name,
        "n_combos": len(results),
        "best_per_metric": best_per,
        "top_combos": top,
        "grid": grid,
    }


def save_output(modes, trade_data, ev_path, setup_type, direction, time_stop, max_forward):
    """Save everything to JSON."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Signal metadata (for UI re-slicing by quality/depth)
    sig_summary = []
    for m in trade_data["signal_meta"]:
        sig_summary.append({
            "idx": m["idx"], "ticker": m["ticker"], "signal_date": m["signal_date"],
            "quality_score": m["quality_score"], "killed_at_depth": m["killed_at_depth"],
            "move_adr": m["move_adr"], "is_example": m["is_example"],
        })

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "ev_source": os.path.basename(ev_path),
        "direction": direction,
        "n_signals": trade_data["n_valid"],
        "time_stop": time_stop,
        "max_forward": max_forward,
        "initial_capital": INITIAL_CAPITAL,
        "risk_per_trade": RISK_PER_TRADE,
        "signals": sig_summary,
        "stage_1": modes.get("1-stage"),
        "stage_2": modes.get("2-stage"),
        "stage_3": modes.get("3-stage"),
    }

    def np_fix(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj) if np.isfinite(obj) else None
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, (np.bool_,)): return bool(obj)
        return obj

    fname = f"profit_{setup_type}_{ts}.json"
    path = os.path.join(CACHE_DIR, fname)
    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=np_fix)
    print(f"\nSaved: {path}")

    latest = os.path.join(CACHE_DIR, f"profit_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2, default=np_fix)
    print(f"Saved latest: {latest}")

    try:
        from file_mirror import mirror_file
        mirror_file(path)
        mirror_file(latest)
        print("Mirrored to Railway")
    except Exception as e:
        print(f"WARNING: Railway mirror failed: {e}")

    return path


# ============================================================
# Reporting
# ============================================================

def print_mode_summary(mode_data):
    """Print top results for one mode."""
    if not mode_data or mode_data["n_combos"] == 0:
        print(f"  {mode_data.get('mode', '?')}: No valid combos")
        return

    print(f"\n  {mode_data['mode']} \u2014 {mode_data['n_combos']:,} combos")
    for metric in ["sqn", "expectancy", "cagr", "sharpe", "profit_factor"]:
        best = mode_data["best_per_metric"].get(metric)
        if best:
            p = best["params"]
            tgts = "\u2192".join(f"{t}ADR" for t in p["targets"])
            trims = "/".join(f"{int(t*100)}%" for t in p["trim_pcts"])
            trail = f", trail@{p['trail_activate_adr']}\u2192{p['trail_distance_adr']}" if p["trail_activate_adr"] > 0 else ""
            print(f"    Best {metric}: {best['value']:.4f}  "
                  f"(stop={p['stop_adr']}, targets={tgts}, trims={trims}{trail})")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Profit Grinder \u2014 Phase 4 Exit Optimization")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--direction", default="short")
    parser.add_argument("--ev-file", default=None)
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--time-stop", type=int, default=TIME_STOP)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    print(f"Profit Grinder \u2014 Phase 4 Exit Optimization")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Time stop: {args.time_stop}")
    print(f"Workers: {args.workers}")

    # 1. Load EV grinder output
    ev_data, ev_path = load_ev_data(args.setup, args.ev_file)

    # 2. Select signals with exit data
    signals = select_signals(ev_data)
    if len(signals) < 5:
        print(f"\nOnly {len(signals)} signals \u2014 need at least 5. Aborting.")
        sys.exit(1)

    # 3. Load OHLCV + build trade arrays
    ohlcv_cache = load_5yr_cache()
    trade_data = build_trade_arrays(signals, ohlcv_cache, args.direction, args.max_forward)
    del ohlcv_cache  # free ~2GB

    if trade_data["n_valid"] < 5:
        print(f"\nOnly {trade_data['n_valid']} valid trades. Aborting.")
        sys.exit(1)

    # 4. Build grids
    grid_1 = build_grid_1stage()
    grid_2 = build_grid_2stage()
    grid_3 = build_grid_3stage()
    total = len(grid_1) + len(grid_2) + len(grid_3)
    print(f"\nParameter grids: 1-stage={len(grid_1):,}, 2-stage={len(grid_2):,}, "
          f"3-stage={len(grid_3):,}, total={total:,}")

    # 5. Grind all three modes
    t0 = time.time()
    r1 = grind_mode("1-stage", grid_1, trade_data, args.direction,
                     args.time_stop, args.max_forward, args.workers)
    r2 = grind_mode("2-stage", grid_2, trade_data, args.direction,
                     args.time_stop, args.max_forward, args.workers)
    r3 = grind_mode("3-stage", grid_3, trade_data, args.direction,
                     args.time_stop, args.max_forward, args.workers)
    total_time = time.time() - t0
    print(f"\nTotal grind time: {total_time:.1f}s ({total_time/60:.1f} min)")

    # 6. Package results
    print("\nPackaging results...")
    modes = {
        "1-stage": package_mode_results("1-stage", r1, trade_data, args.direction,
                                         args.time_stop, args.max_forward),
        "2-stage": package_mode_results("2-stage", r2, trade_data, args.direction,
                                         args.time_stop, args.max_forward),
        "3-stage": package_mode_results("3-stage", r3, trade_data, args.direction,
                                         args.time_stop, args.max_forward),
    }

    # 7. Report
    print(f"\n{'='*80}")
    print("TOP RESULTS")
    print(f"{'='*80}")
    for m in modes.values():
        print_mode_summary(m)

    # 8. Save
    save_path = save_output(modes, trade_data, ev_path, args.setup,
                            args.direction, args.time_stop, args.max_forward)

    print(f"\n{'='*80}")
    print(f"PROFIT GRINDER COMPLETE")
    print(f"  Signals: {trade_data['n_valid']}")
    print(f"  Total combos: {total:,}")
    print(f"  Time: {total_time:.1f}s")
    print(f"  Output: {save_path}")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
