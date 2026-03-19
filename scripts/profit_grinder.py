"""
Profit Grinder — Phase 4: Trade Exit Optimization

Brute-forces stop/target/trail/trim parameters against post-entry OHLCV price
action for all scored signals from the EV grinder. Computes a comprehensive
stats panel per parameter combo (SQN, expectancy, CAGR, max drawdown, Sharpe,
Sortino, Calmar, profit factor, etc.). The output is a pure data dump — the UI
decides how to present and rank it.

Reads from:
  - EV grinder output (scored signals with quality_score, predicted WR/MFE, EV)
  - 5yr OHLCV cache (forward price bars for trade simulation)
  - scan_settings JSON (locked refinement depth + quality threshold) OR CLI overrides

Outputs:
  - Full parameter grid with all stats per combo
  - Per-combo trade list with per-trade detail
  - Equity curves per combo
  - Best combo per metric
  - Saved to local_runner/cache/profit_{setup}_{timestamp}.json + Railway mirror

Optimized for speed:
  - Vectorized numpy bar-walking (no Python loop over bars)
  - Parallel across parameter combos via ThreadPoolExecutor
  - OHLCV sliced once into contiguous arrays, shared across threads
  - RAM-conscious: equity curves stored only for top N combos

Usage:
    python scripts/profit_grinder.py --setup dtss
    python scripts/profit_grinder.py --setup dtss --min-depth 50 --min-quality 40
    python scripts/profit_grinder.py --setup dtss --max-forward 120 --workers 12
"""

import argparse
import sys
import os
import time
import json
import numpy as np
import pickle
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Config
# ============================================================
RAILWAY_URL = "https://web-production-e3025.up.railway.app"
LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DEFAULT_WORKERS = os.cpu_count() or 8
MAX_FORWARD_DEFAULT = 120  # bars after entry to simulate

# Parameter grid defaults
STOP_RANGE = (0.5, 3.0, 0.25)    # min, max, step — in ADR units
TARGET_RANGE = (1.0, 12.0, 0.5)  # min, max, step — in ADR units
TRAIL_ACTIVATE_RANGE = (1.0, 4.0, 0.5)   # activate trailing after N ADR profit
TRAIL_DISTANCE_RANGE = (0.5, 2.0, 0.25)  # trail stop distance in ADR
TRIM_PCTS = [0.0, 0.33, 0.50]    # trim 0%, 33%, 50% at target
TIME_STOP = 60  # max bars to hold (0 = disabled)

# Equity simulation
INITIAL_CAPITAL = 100_000
RISK_PER_TRADE = 0.01  # 1% fixed fractional
TRADING_DAYS_PER_YEAR = 252

# Top N combos to store full equity curves + trade lists for
TOP_N_FULL_DETAIL = 50


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
    print(f"  {len(data.get('signals', []))} pre-refinement signals")
    print(f"  {len(data.get('signals_post', []))} post-refinement signals")
    return data, path


def load_scan_settings(setup_type):
    """Load locked scan settings. Returns None if not found (CLI overrides used)."""
    path = os.path.join(CACHE_DIR, f"scan_settings_{setup_type}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


# ============================================================
# Signal Filtering + Trade Data Preparation
# ============================================================

def filter_signals(ev_data, min_depth, min_quality, direction):
    """
    Filter signals from EV grinder output based on refinement depth and quality score.
    Returns list of trade-ready signal dicts.
    
    A signal passes if:
    1. It's a winner (classification == AUTO_WIN) or survived to the chosen depth
       (killed_at_depth is None or killed_at_depth > min_depth)
    2. quality_score >= min_quality
    3. Has entry_high and adr_at_signal (needed for stop/target calculation)
    """
    # Use the pre-refinement signals (full 893 set) with killed_at_depth filtering
    raw_signals = ev_data.get("signals", [])
    
    passed = []
    skipped_depth = 0
    skipped_quality = 0
    skipped_data = 0
    
    for sig in raw_signals:
        # Depth filter: signal dies if killed_at_depth <= min_depth
        kad = sig.get("killed_at_depth")
        if kad is not None and kad <= min_depth:
            skipped_depth += 1
            continue
        
        # Quality filter
        qs = sig.get("quality_score", 0)
        if qs < min_quality:
            skipped_quality += 1
            continue
        
        # Data availability — need entry_high and adr for stop/target
        entry_high = sig.get("entry_high")
        adr = sig.get("adr_at_signal")
        if entry_high is None or adr is None or adr <= 0:
            skipped_data += 1
            continue
        
        passed.append(sig)
    
    print(f"\nSignal filtering (depth>={min_depth}, quality>={min_quality}):")
    print(f"  Input: {len(raw_signals)} signals")
    print(f"  Killed by depth: {skipped_depth}")
    print(f"  Below quality threshold: {skipped_quality}")
    print(f"  Missing entry/ADR data: {skipped_data}")
    print(f"  Passed: {len(passed)}")
    
    winners = sum(1 for s in passed if s["classification"] == "AUTO_WIN")
    losers = len(passed) - winners
    print(f"  Winners: {winners}, Losers: {losers}, WR: {winners/len(passed)*100:.1f}%" if passed else "  No signals passed")
    
    return passed


def build_trade_arrays(signals, ohlcv_cache, direction, max_forward):
    """
    Build contiguous numpy arrays for vectorized trade simulation.
    
    For each signal, slices forward OHLCV from entry bar.
    Returns parallel arrays that can be indexed by signal index.
    
    For shorts: entry = short at entry_high, want price to go DOWN.
    For longs: entry = buy at entry_low (approximation), want price to go UP.
    """
    import pandas as pd
    
    n = len(signals)
    # Pre-allocate. Max possible forward bars = max_forward.
    # Each signal gets a row in these 2D arrays.
    fwd_highs = np.full((n, max_forward), np.nan, dtype=np.float64)
    fwd_lows = np.full((n, max_forward), np.nan, dtype=np.float64)
    fwd_closes = np.full((n, max_forward), np.nan, dtype=np.float64)
    
    entry_prices = np.zeros(n, dtype=np.float64)
    adr_values = np.zeros(n, dtype=np.float64)
    n_bars = np.zeros(n, dtype=np.int32)  # actual forward bars available
    
    # For per-trade reporting
    trade_meta = []
    
    loaded = 0
    skipped = 0
    
    # Group signals by ticker for efficient OHLCV lookup
    ticker_groups = {}
    for i, sig in enumerate(signals):
        t = sig["ticker"]
        if t not in ticker_groups:
            ticker_groups[t] = []
        ticker_groups[t].append(i)
    
    for ticker, indices in ticker_groups.items():
        df = ohlcv_cache.get(ticker)
        if df is None:
            for i in indices:
                skipped += 1
            continue
        
        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["date"]):
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        
        # Build date lookup once per ticker
        date_strs = df["date"].dt.strftime("%Y-%m-%d").values
        date_to_idx = {d: idx for idx, d in enumerate(date_strs)}
        
        h_arr = df["high"].values.astype(np.float64)
        l_arr = df["low"].values.astype(np.float64)
        c_arr = df["close"].values.astype(np.float64)
        
        for i in indices:
            sig = signals[i]
            sig_date = sig["date"]
            
            bar_idx = date_to_idx.get(sig_date)
            if bar_idx is None:
                skipped += 1
                continue
            
            entry_high = sig["entry_high"]
            adr = sig["adr_at_signal"]
            
            # Entry price
            if direction == "short":
                entry_prices[i] = entry_high
            else:
                # For longs, approximate entry as low of entry bar
                entry_prices[i] = l_arr[bar_idx]
            
            adr_values[i] = adr
            
            # Forward window starts the bar AFTER the signal bar
            # (signal bar = the rightmost cluster bar; you enter next day)
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
            
            # Forward dates for trade reporting
            fwd_dates = date_strs[start:end].tolist()
            
            trade_meta.append({
                "idx": i,
                "ticker": ticker,
                "signal_date": sig_date,
                "entry_price": entry_prices[i],
                "adr": adr,
                "classification": sig["classification"],
                "is_example": sig.get("is_example", False),
                "quality_score": sig.get("quality_score", 0),
                "predicted_wr": sig.get("predicted_wr", 0),
                "predicted_mfe": sig.get("predicted_mfe", 0),
                "ev": sig.get("ev", 0),
                "fwd_dates": fwd_dates,
            })
            loaded += 1
    
    print(f"\nTrade data built:")
    print(f"  Loaded: {loaded}, Skipped: {skipped}")
    print(f"  Forward bars: min={n_bars[n_bars > 0].min() if (n_bars > 0).any() else 0}, "
          f"max={n_bars.max()}, median={int(np.median(n_bars[n_bars > 0])) if (n_bars > 0).any() else 0}")
    
    # Build index mapping: trade_meta[j]["idx"] -> position in arrays
    valid_mask = n_bars > 0
    
    return {
        "fwd_highs": fwd_highs,
        "fwd_lows": fwd_lows,
        "fwd_closes": fwd_closes,
        "entry_prices": entry_prices,
        "adr_values": adr_values,
        "n_bars": n_bars,
        "valid_mask": valid_mask,
        "trade_meta": trade_meta,
        "n_valid": int(valid_mask.sum()),
    }


# ============================================================
# Trade Simulation — Vectorized
# ============================================================

def simulate_trades_vectorized(trade_data, direction, stop_adr, target_adr,
                                trail_activate_adr, trail_distance_adr,
                                trim_pct, time_stop, max_forward):
    """
    Simulate all trades for one parameter combo. Vectorized where possible,
    with a tight numpy loop over bars (unavoidable for trailing stop state).
    
    Returns per-trade results as numpy arrays for fast stats computation.
    
    For shorts:
      - Entry = short at entry_price (entry_high)
      - Stop loss = entry_price + stop_adr * ADR (price goes UP = loss)
      - Target = entry_price - target_adr * ADR (price goes DOWN = win)
      - Trail: after price drops trail_activate_adr * ADR below entry,
               set trailing stop at current_low + trail_distance_adr * ADR
    
    For longs: reverse all directions.
    """
    fwd_highs = trade_data["fwd_highs"]
    fwd_lows = trade_data["fwd_lows"]
    fwd_closes = trade_data["fwd_closes"]
    entry_prices = trade_data["entry_prices"]
    adr_values = trade_data["adr_values"]
    n_bars_arr = trade_data["n_bars"]
    valid_mask = trade_data["valid_mask"]
    
    n_signals = len(entry_prices)
    
    # Output arrays
    exit_bar = np.full(n_signals, -1, dtype=np.int32)
    exit_price = np.full(n_signals, np.nan, dtype=np.float64)
    exit_reason = np.full(n_signals, -1, dtype=np.int8)  # 0=stop, 1=target, 2=trail, 3=time
    r_multiple = np.full(n_signals, np.nan, dtype=np.float64)
    mfe_during = np.full(n_signals, np.nan, dtype=np.float64)  # max favorable excursion during trade
    mae_during = np.full(n_signals, np.nan, dtype=np.float64)  # max adverse excursion during trade
    bars_held = np.full(n_signals, 0, dtype=np.int32)
    
    # Precompute stop/target price levels
    stop_prices = np.where(valid_mask, 
                           entry_prices + (stop_adr * adr_values if direction == "short" 
                                          else -stop_adr * adr_values),
                           np.nan)
    target_prices = np.where(valid_mask,
                             entry_prices - (target_adr * adr_values if direction == "short"
                                            else -target_adr * adr_values),
                             np.nan)
    trail_activate_prices = np.where(valid_mask,
                                     entry_prices - (trail_activate_adr * adr_values if direction == "short"
                                                    else -trail_activate_adr * adr_values),
                                     np.nan)
    
    # Trim tracking
    has_trim = trim_pct > 0
    trim_realized_r = np.zeros(n_signals, dtype=np.float64)
    trim_done = np.zeros(n_signals, dtype=np.bool_)
    remaining_pct = np.ones(n_signals, dtype=np.float64)  # 1.0 = full position
    
    # Effective time stop
    eff_time_stop = time_stop if time_stop > 0 else max_forward
    
    # Bar-by-bar simulation — tight loop over bars, vectorized across signals
    # Trail stop state per signal
    trailing_active = np.zeros(n_signals, dtype=np.bool_)
    trail_stop_price = np.full(n_signals, np.nan, dtype=np.float64)
    best_price = entry_prices.copy()  # best price seen (lowest for shorts, highest for longs)
    
    for bar in range(max_forward):
        # Which signals are still open and have data for this bar?
        active = valid_mask & (exit_bar < 0) & (bar < n_bars_arr)
        if not active.any():
            break
        
        h = fwd_highs[:, bar]
        l = fwd_lows[:, bar]
        c = fwd_closes[:, bar]
        
        if direction == "short":
            # Update best price (lowest low seen)
            new_best = np.where(active & (l < best_price), l, best_price)
            best_price = new_best
            
            # Check stop loss: high >= stop_price
            stopped = active & (h >= stop_prices)
            
            # Check target: low <= target_price
            targeted = active & ~stopped & (l <= target_prices)
            
            # Check trail activation (only if trailing is configured)
            if trail_activate_adr > 0:
                newly_trailing = active & ~stopped & ~targeted & ~trailing_active & \
                                (l <= trail_activate_prices)
            else:
                newly_trailing = np.zeros(n_signals, dtype=np.bool_)
            trailing_active = trailing_active | newly_trailing
            
            # Update trail stop for trailing signals
            trail_mask = active & trailing_active & ~stopped & ~targeted
            if trail_mask.any():
                new_trail = best_price + trail_distance_adr * adr_values
                # Trail stop can only move DOWN (tighten) for shorts
                trail_stop_price = np.where(
                    trail_mask & (np.isnan(trail_stop_price) | (new_trail < trail_stop_price)),
                    new_trail, trail_stop_price
                )
            
            # Check trail stop hit
            trail_stopped = active & trailing_active & ~stopped & ~targeted & \
                           (h >= trail_stop_price)
            
            # MFE/MAE: track running min low and max high
            mfe_during = np.where(active & (np.isnan(mfe_during) | (l < mfe_during)), l, mfe_during)
            mae_during = np.where(active & (np.isnan(mae_during) | (h > mae_during)), h, mae_during)
            
        else:  # long
            # Update best price (highest high seen)
            new_best = np.where(active & (h > best_price), h, best_price)
            best_price = new_best
            
            # Stop: low <= stop_price
            stopped = active & (l <= stop_prices)
            
            # Target: high >= target_price
            targeted = active & ~stopped & (h >= target_prices)
            
            # Trail activation (only if trailing is configured)
            if trail_activate_adr > 0:
                newly_trailing = active & ~stopped & ~targeted & ~trailing_active & \
                                (h >= trail_activate_prices)
            else:
                newly_trailing = np.zeros(n_signals, dtype=np.bool_)
            trailing_active = trailing_active | newly_trailing
            
            # Update trail stop
            trail_mask = active & trailing_active & ~stopped & ~targeted
            if trail_mask.any():
                new_trail = best_price - trail_distance_adr * adr_values
                trail_stop_price = np.where(
                    trail_mask & (np.isnan(trail_stop_price) | (new_trail > trail_stop_price)),
                    new_trail, trail_stop_price
                )
            
            trail_stopped = active & trailing_active & ~stopped & ~targeted & \
                           (l <= trail_stop_price)
            
            mfe_during = np.where(active & (np.isnan(mfe_during) | (h > mfe_during)), h, mfe_during)
            mae_during = np.where(active & (np.isnan(mae_during) | (l < mae_during)), l, mae_during)
        
        # Handle trim at target
        if has_trim:
            trim_now = targeted & ~trim_done
            if trim_now.any():
                if direction == "short":
                    trim_r = (entry_prices - target_prices) / (stop_adr * adr_values)
                else:
                    trim_r = (target_prices - entry_prices) / (stop_adr * adr_values)
                trim_realized_r = np.where(trim_now, trim_pct * trim_r, trim_realized_r)
                remaining_pct = np.where(trim_now, 1.0 - trim_pct, remaining_pct)
                trim_done = trim_done | trim_now
                # Target hit but trade continues (trim portion closed, rest trails)
                # Don't close the trade — let it continue to trail stop or time stop
                targeted = targeted & ~trim_now  # remove from "fully closed at target"
        
        # Record exits
        # Priority: stop > target > trail_stop > (continue)
        for mask, reason_code, price_fn in [
            (stopped, 0, lambda: stop_prices),
            (targeted, 1, lambda: target_prices),
            (trail_stopped, 2, lambda: trail_stop_price),
        ]:
            newly_exited = mask & (exit_bar < 0)
            if newly_exited.any():
                exit_bar = np.where(newly_exited, bar, exit_bar)
                exit_price = np.where(newly_exited, price_fn(), exit_price)
                exit_reason = np.where(newly_exited, reason_code, exit_reason)
                bars_held = np.where(newly_exited, bar + 1, bars_held)
        
        # Time stop check
        if bar + 1 >= eff_time_stop:
            time_stopped = active & (exit_bar < 0)
            if time_stopped.any():
                exit_bar = np.where(time_stopped, bar, exit_bar)
                exit_price = np.where(time_stopped, c, exit_price)
                exit_reason = np.where(time_stopped, 3, exit_reason)
                bars_held = np.where(time_stopped, bar + 1, bars_held)
    
    # Any still open after max_forward — close at last available close
    still_open = valid_mask & (exit_bar < 0)
    if still_open.any():
        last_bar_idx = np.clip(n_bars_arr - 1, 0, max_forward - 1)
        last_closes = np.array([fwd_closes[i, last_bar_idx[i]] if last_bar_idx[i] >= 0 else np.nan 
                                for i in range(n_signals)])
        exit_bar = np.where(still_open, last_bar_idx, exit_bar)
        exit_price = np.where(still_open, last_closes, exit_price)
        exit_reason = np.where(still_open, 3, exit_reason)  # time stop
        bars_held = np.where(still_open, last_bar_idx + 1, bars_held)
    
    # Compute R-multiples
    risk_per_share = stop_adr * adr_values
    risk_per_share = np.where(risk_per_share > 0, risk_per_share, np.nan)
    
    if direction == "short":
        raw_pnl = (entry_prices - exit_price) * remaining_pct
        r_multiple = raw_pnl / risk_per_share + trim_realized_r
        # Convert MFE/MAE to ADR units
        mfe_adr = (entry_prices - mfe_during) / adr_values  # positive = good for shorts
        mae_adr = (mae_during - entry_prices) / adr_values   # positive = adverse for shorts
    else:
        raw_pnl = (exit_price - entry_prices) * remaining_pct
        r_multiple = raw_pnl / risk_per_share + trim_realized_r
        mfe_adr = (mfe_during - entry_prices) / adr_values
        mae_adr = (entry_prices - mae_during) / adr_values
    
    return {
        "exit_bar": exit_bar,
        "exit_price": exit_price,
        "exit_reason": exit_reason,  # 0=stop, 1=target, 2=trail, 3=time
        "r_multiple": r_multiple,
        "mfe_adr": mfe_adr,
        "mae_adr": mae_adr,
        "bars_held": bars_held,
        "valid_mask": valid_mask & (exit_bar >= 0),
    }


# ============================================================
# Stats Computation
# ============================================================

REASON_NAMES = {0: "stop", 1: "target", 2: "trail", 3: "time"}

def compute_combo_stats(sim_result, trade_data):
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
    n_win = winners.sum()
    n_loss = losers.sum()
    
    wr = n_win / n if n > 0 else 0
    
    avg_win_r = float(np.mean(r[winners])) if n_win > 0 else 0.0
    avg_loss_r = float(np.mean(r[losers])) if n_loss > 0 else 0.0
    median_win_r = float(np.median(r[winners])) if n_win > 0 else 0.0
    median_loss_r = float(np.median(r[losers])) if n_loss > 0 else 0.0
    best_win_r = float(np.max(r[winners])) if n_win > 0 else 0.0
    worst_loss_r = float(np.min(r[losers])) if n_loss > 0 else 0.0
    
    # Expectancy
    expectancy = float(np.mean(r))
    
    # SQN = sqrt(N) * mean(R) / std(R)
    std_r = float(np.std(r, ddof=1)) if n > 1 else 1.0
    sqn = (np.sqrt(n) * expectancy / std_r) if std_r > 0 else 0.0
    
    # Profit factor
    gross_wins = float(np.sum(r[winners])) if n_win > 0 else 0.0
    gross_losses = float(np.abs(np.sum(r[losers]))) if n_loss > 0 else 0.001
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
    
    # Payoff ratio
    payoff_ratio = abs(avg_win_r / avg_loss_r) if avg_loss_r != 0 else float('inf')
    
    # Consecutive wins/losses
    max_consec_win = _max_consecutive(winners)
    max_consec_loss = _max_consecutive(losers)
    
    # Equity curve (fixed fractional)
    equity = _build_equity_curve(r, INITIAL_CAPITAL, RISK_PER_TRADE)
    
    # Drawdown
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / peak
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0
    avg_dd = float(np.mean(dd[dd > 0])) if (dd > 0).any() else 0.0
    
    # Max drawdown duration (trades, not bars)
    max_dd_duration = _max_dd_duration(equity)
    
    # CAGR
    total_return = equity[-1] / equity[0] if equity[0] > 0 else 1.0
    # Estimate years from total bars held
    total_bars = int(np.sum(bars))
    years = total_bars / TRADING_DAYS_PER_YEAR if total_bars > 0 else 1.0
    cagr = (total_return ** (1 / years) - 1) if years > 0 and total_return > 0 else 0.0
    
    # Sharpe-like (annualized return / annualized std of R)
    annual_r = expectancy * (TRADING_DAYS_PER_YEAR / (np.mean(bars) if np.mean(bars) > 0 else 1))
    annual_std = std_r * np.sqrt(TRADING_DAYS_PER_YEAR / (np.mean(bars) if np.mean(bars) > 0 else 1))
    sharpe = annual_r / annual_std if annual_std > 0 else 0.0
    
    # Sortino (downside deviation)
    downside = r[r < 0]
    downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 1.0
    annual_downside = downside_std * np.sqrt(TRADING_DAYS_PER_YEAR / (np.mean(bars) if np.mean(bars) > 0 else 1))
    sortino = annual_r / annual_downside if annual_downside > 0 else 0.0
    
    # Calmar
    calmar = cagr / max_dd if max_dd > 0 else float('inf')
    
    # Exit reason breakdown
    reason_counts = {}
    for code, name in REASON_NAMES.items():
        reason_counts[name] = int((reasons == code).sum())
    
    # MFE/MAE stats
    mfe_valid = mfe[np.isfinite(mfe)]
    mae_valid = mae[np.isfinite(mae)]
    
    # Bars held
    avg_bars_win = float(np.mean(bars[winners])) if n_win > 0 else 0
    avg_bars_loss = float(np.mean(bars[losers])) if n_loss > 0 else 0
    
    return {
        "n_trades": int(n),
        "n_winners": int(n_win),
        "n_losers": int(n_loss),
        "win_rate": round(float(wr), 4),
        "expectancy": round(float(expectancy), 4),
        "sqn": round(float(sqn), 4),
        "profit_factor": round(float(profit_factor), 4),
        "payoff_ratio": round(float(payoff_ratio), 4),
        "avg_win_r": round(float(avg_win_r), 4),
        "avg_loss_r": round(float(avg_loss_r), 4),
        "median_win_r": round(float(median_win_r), 4),
        "median_loss_r": round(float(median_loss_r), 4),
        "best_win_r": round(float(best_win_r), 4),
        "worst_loss_r": round(float(worst_loss_r), 4),
        "total_r": round(float(np.sum(r)), 4),
        "std_r": round(float(std_r), 4),
        "max_consec_winners": int(max_consec_win),
        "max_consec_losers": int(max_consec_loss),
        "max_drawdown": round(float(max_dd), 4),
        "avg_drawdown": round(float(avg_dd), 4),
        "max_dd_duration_trades": int(max_dd_duration),
        "cagr": round(float(cagr), 4),
        "sharpe": round(float(sharpe), 4),
        "sortino": round(float(sortino), 4),
        "calmar": round(float(calmar), 4),
        "exit_reasons": reason_counts,
        "avg_bars_held_winners": round(float(avg_bars_win), 1),
        "avg_bars_held_losers": round(float(avg_bars_loss), 1),
        "avg_bars_held_all": round(float(np.mean(bars)), 1),
        "mfe_median_adr": round(float(np.median(mfe_valid)), 2) if len(mfe_valid) > 0 else None,
        "mfe_avg_adr": round(float(np.mean(mfe_valid)), 2) if len(mfe_valid) > 0 else None,
        "mae_median_adr": round(float(np.median(mae_valid)), 2) if len(mae_valid) > 0 else None,
        "mae_avg_adr": round(float(np.mean(mae_valid)), 2) if len(mae_valid) > 0 else None,
        "final_equity": round(float(equity[-1]), 2),
        "total_return_pct": round(float((equity[-1] / equity[0] - 1) * 100), 2),
    }


def _max_consecutive(bool_arr):
    """Max consecutive True values in a boolean array."""
    if len(bool_arr) == 0:
        return 0
    max_run = 0
    current = 0
    for v in bool_arr:
        if v:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return max_run


def _build_equity_curve(r_multiples, initial_capital, risk_pct):
    """Build equity curve from R-multiples using fixed fractional sizing."""
    equity = np.zeros(len(r_multiples) + 1)
    equity[0] = initial_capital
    for i, r in enumerate(r_multiples):
        risk_amount = equity[i] * risk_pct
        pnl = risk_amount * r
        equity[i + 1] = equity[i] + pnl
        if equity[i + 1] <= 0:
            equity[i + 1:] = 0
            break
    return equity


def _max_dd_duration(equity):
    """Max drawdown duration in number of trades."""
    peak = equity[0]
    max_dur = 0
    current_dur = 0
    for val in equity[1:]:
        if val >= peak:
            peak = val
            current_dur = 0
        else:
            current_dur += 1
            max_dur = max(max_dur, current_dur)
    return max_dur


# ============================================================
# Parameter Grid + Parallel Grinding
# ============================================================

def build_param_grid(args):
    """Build the full parameter grid from CLI args or defaults."""
    stops = np.arange(args.stop_min, args.stop_max + 0.001, args.stop_step)
    targets = np.arange(args.target_min, args.target_max + 0.001, args.target_step)
    trail_acts = np.arange(args.trail_act_min, args.trail_act_max + 0.001, args.trail_act_step)
    trail_dists = np.arange(args.trail_dist_min, args.trail_dist_max + 0.001, args.trail_dist_step)
    trim_pcts = [float(x) for x in args.trim_pcts.split(",")]
    
    combos = []
    
    for stop in stops:
        for target in targets:
            if target <= stop:
                continue  # target must exceed stop to make sense
            
            # No-trail combos (pure stop/target)
            for trim in trim_pcts:
                combos.append({
                    "stop_adr": round(float(stop), 4),
                    "target_adr": round(float(target), 4),
                    "trail_activate_adr": 0.0,
                    "trail_distance_adr": 0.0,
                    "trim_pct": round(float(trim), 4),
                })
            
            # Trail combos
            for trail_act in trail_acts:
                for trail_dist in trail_dists:
                    for trim in trim_pcts:
                        combos.append({
                            "stop_adr": round(float(stop), 4),
                            "target_adr": round(float(target), 4),
                            "trail_activate_adr": round(float(trail_act), 4),
                            "trail_distance_adr": round(float(trail_dist), 4),
                            "trim_pct": round(float(trim), 4),
                        })
    
    return combos


def _grind_one_combo(args_tuple):
    """Worker: simulate one parameter combo. Returns (combo_dict, stats_dict)."""
    (combo, trade_data_shared, direction, time_stop, max_forward) = args_tuple

    sim = simulate_trades_vectorized(
        trade_data_shared, direction,
        stop_adr=combo["stop_adr"],
        target_adr=combo["target_adr"],
        trail_activate_adr=combo["trail_activate_adr"],
        trail_distance_adr=combo["trail_distance_adr"],
        trim_pct=combo["trim_pct"],
        time_stop=time_stop,
        max_forward=max_forward,
    )

    stats = compute_combo_stats(sim, trade_data_shared)
    if stats is None:
        return None

    # Attach combo params to stats
    stats["params"] = combo

    return stats


def grind_all_combos(combos, trade_data, direction, time_stop, max_forward, workers):
    """Run all parameter combos. Parallel via ThreadPoolExecutor."""
    n = len(combos)
    print(f"\nGrinding {n:,} parameter combos ({workers} workers)...")
    t0 = time.time()

    results = []

    from concurrent.futures import ThreadPoolExecutor

    tasks = [(combo, trade_data, direction, time_stop, max_forward) for combo in combos]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_grind_one_combo, task): i for i, task in enumerate(tasks)}
        done = 0
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                results.append(result)
            done += 1
            if done % 500 == 0 or done == n:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(f"  [{done:,}/{n:,}] {len(results):,} valid combos, "
                      f"{elapsed:.1f}s ({rate:.0f}/s)")

    elapsed = time.time() - t0
    print(f"\nDone: {len(results):,} valid combos from {n:,} tested in {elapsed:.1f}s")

    return results


# ============================================================
# Build Per-Trade Detail for Top Combos
# ============================================================

def build_trade_detail(combo_stats, trade_data, direction, time_stop, max_forward):
    """Re-run simulation for one combo and extract per-trade detail."""
    params = combo_stats["params"]
    sim = simulate_trades_vectorized(
        trade_data, direction,
        stop_adr=params["stop_adr"],
        target_adr=params["target_adr"],
        trail_activate_adr=params["trail_activate_adr"],
        trail_distance_adr=params["trail_distance_adr"],
        trim_pct=params["trim_pct"],
        time_stop=time_stop,
        max_forward=max_forward,
    )

    trades = []
    meta_lookup = {m["idx"]: m for m in trade_data["trade_meta"]}

    for i in range(len(sim["valid_mask"])):
        if not sim["valid_mask"][i]:
            continue

        meta = meta_lookup.get(i, {})
        exit_b = int(sim["exit_bar"][i])
        fwd_dates = meta.get("fwd_dates", [])
        exit_date = fwd_dates[exit_b] if exit_b < len(fwd_dates) else None

        trades.append({
            "ticker": meta.get("ticker", ""),
            "signal_date": meta.get("signal_date", ""),
            "entry_price": round(float(trade_data["entry_prices"][i]), 4),
            "exit_date": exit_date,
            "exit_price": round(float(sim["exit_price"][i]), 4),
            "exit_reason": REASON_NAMES.get(int(sim["exit_reason"][i]), "unknown"),
            "r_multiple": round(float(sim["r_multiple"][i]), 4),
            "bars_held": int(sim["bars_held"][i]),
            "mfe_adr": round(float(sim["mfe_adr"][i]), 2) if np.isfinite(sim["mfe_adr"][i]) else None,
            "mae_adr": round(float(sim["mae_adr"][i]), 2) if np.isfinite(sim["mae_adr"][i]) else None,
            "quality_score": meta.get("quality_score", 0),
            "classification": meta.get("classification", ""),
        })

    # Sort by date
    trades.sort(key=lambda t: t["signal_date"])

    # Build equity curve
    r_arr = np.array([t["r_multiple"] for t in trades])
    equity = _build_equity_curve(r_arr, INITIAL_CAPITAL, RISK_PER_TRADE)

    return trades, equity.tolist()


# ============================================================
# Output
# ============================================================

def save_results(all_combo_stats, trade_data, ev_path, setup_type, args,
                 direction, time_stop, max_forward):
    """Save comprehensive results to JSON."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Sort by each major metric and find the best
    metrics_to_rank = ["sqn", "expectancy", "cagr", "sharpe", "sortino",
                       "calmar", "profit_factor", "win_rate", "total_r"]

    best_per_metric = {}
    for metric in metrics_to_rank:
        sorted_by = sorted(all_combo_stats, key=lambda s: s.get(metric, 0), reverse=True)
        if sorted_by:
            best_per_metric[metric] = {
                "params": sorted_by[0]["params"],
                "value": sorted_by[0].get(metric, 0),
            }

    # Default sort: SQN
    all_combo_stats.sort(key=lambda s: s.get("sqn", 0), reverse=True)

    # Build trade detail + equity curves for top N
    print(f"\nBuilding trade detail for top {TOP_N_FULL_DETAIL} combos...")
    top_with_detail = []
    for i, combo in enumerate(all_combo_stats[:TOP_N_FULL_DETAIL]):
        trades, equity = build_trade_detail(combo, trade_data, direction, time_stop, max_forward)
        entry = dict(combo)
        entry["trades"] = trades
        entry["equity_curve"] = equity
        top_with_detail.append(entry)
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{min(TOP_N_FULL_DETAIL, len(all_combo_stats))}]")

    # Strip equity/trades from the full grid (too large)
    grid_stats = []
    for combo in all_combo_stats:
        entry = {k: v for k, v in combo.items() if k not in ("trades", "equity_curve")}
        grid_stats.append(entry)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now().isoformat(),
        "ev_source": os.path.basename(ev_path),
        "direction": direction,
        "n_signals_input": trade_data["n_valid"],
        "n_combos_tested": len(all_combo_stats),
        "params_config": {
            "stop_range": [args.stop_min, args.stop_max, args.stop_step],
            "target_range": [args.target_min, args.target_max, args.target_step],
            "trail_activate_range": [args.trail_act_min, args.trail_act_max, args.trail_act_step],
            "trail_distance_range": [args.trail_dist_min, args.trail_dist_max, args.trail_dist_step],
            "trim_pcts": args.trim_pcts,
            "time_stop": time_stop,
            "max_forward": max_forward,
            "initial_capital": INITIAL_CAPITAL,
            "risk_per_trade": RISK_PER_TRADE,
        },
        "filter_settings": {
            "min_refinement_depth": args.min_depth,
            "min_quality_score": args.min_quality,
        },
        "best_per_metric": best_per_metric,
        "top_combos_with_detail": top_with_detail,
        "full_grid": grid_stats,
    }

    # Custom serializer for numpy types
    def np_fix(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj) if np.isfinite(obj) else None
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        return obj

    fname = f"profit_{setup_type}_{ts}.json"
    path = os.path.join(CACHE_DIR, fname)
    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=np_fix)
    print(f"\nSaved: {path}")

    # Latest pointer
    latest = os.path.join(CACHE_DIR, f"profit_{setup_type}.json")
    with open(latest, "w") as f:
        json.dump(output, f, indent=2, default=np_fix)
    print(f"Saved latest: {latest}")

    # Mirror to Railway
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

def print_top_results(all_combo_stats, top_n=10):
    """Print top combos by various metrics."""
    if not all_combo_stats:
        print("\nNo valid parameter combos found.")
        return

    metrics = [
        ("SQN", "sqn"),
        ("Expectancy", "expectancy"),
        ("CAGR", "cagr"),
        ("Sharpe", "sharpe"),
        ("Profit Factor", "profit_factor"),
    ]

    for label, key in metrics:
        sorted_list = sorted(all_combo_stats, key=lambda s: s.get(key, 0), reverse=True)
        best = sorted_list[0]
        p = best["params"]
        trail_str = (f", trail@{p['trail_activate_adr']}\u2192{p['trail_distance_adr']}ADR"
                     if p["trail_activate_adr"] > 0 else "")
        trim_str = f", trim {p['trim_pct']*100:.0f}%" if p["trim_pct"] > 0 else ""

        print(f"\n  Best by {label}: {best[key]:.4f}")
        print(f"    Stop={p['stop_adr']}ADR, Target={p['target_adr']}ADR{trail_str}{trim_str}")
        print(f"    WR={best['win_rate']:.1%}, Exp={best['expectancy']:.3f}R, "
              f"MaxDD={best['max_drawdown']:.1%}, Trades={best['n_trades']}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Profit Grinder \u2014 Phase 4 Exit Optimization")
    parser.add_argument("--setup", default="dtss", help="Setup type")
    parser.add_argument("--direction", default="short", help="Trade direction")
    parser.add_argument("--ev-file", default=None, help="Specific EV grinder output file")
    parser.add_argument("--max-forward", type=int, default=MAX_FORWARD_DEFAULT)
    parser.add_argument("--time-stop", type=int, default=TIME_STOP, help="Max bars to hold (0=disable)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)

    # Filter settings (override scan_settings if provided)
    parser.add_argument("--min-depth", type=int, default=None,
                       help="Min refinement depth (default: from scan_settings or 100)")
    parser.add_argument("--min-quality", type=float, default=None,
                       help="Min quality_score (default: from scan_settings or 0)")

    # Parameter grid
    parser.add_argument("--stop-min", type=float, default=STOP_RANGE[0])
    parser.add_argument("--stop-max", type=float, default=STOP_RANGE[1])
    parser.add_argument("--stop-step", type=float, default=STOP_RANGE[2])
    parser.add_argument("--target-min", type=float, default=TARGET_RANGE[0])
    parser.add_argument("--target-max", type=float, default=TARGET_RANGE[1])
    parser.add_argument("--target-step", type=float, default=TARGET_RANGE[2])
    parser.add_argument("--trail-act-min", type=float, default=TRAIL_ACTIVATE_RANGE[0])
    parser.add_argument("--trail-act-max", type=float, default=TRAIL_ACTIVATE_RANGE[1])
    parser.add_argument("--trail-act-step", type=float, default=TRAIL_ACTIVATE_RANGE[2])
    parser.add_argument("--trail-dist-min", type=float, default=TRAIL_DISTANCE_RANGE[0])
    parser.add_argument("--trail-dist-max", type=float, default=TRAIL_DISTANCE_RANGE[1])
    parser.add_argument("--trail-dist-step", type=float, default=TRAIL_DISTANCE_RANGE[2])
    parser.add_argument("--trim-pcts", type=str, default="0.0,0.33,0.50",
                       help="Comma-separated trim percentages")

    args = parser.parse_args()

    print(f"Profit Grinder \u2014 Phase 4 Exit Optimization")
    print(f"Setup: {args.setup.upper()}, Direction: {args.direction}")
    print(f"Max forward: {args.max_forward} bars, Time stop: {args.time_stop}")
    print(f"Workers: {args.workers}")

    # 1. Load scan settings (if no CLI overrides)
    settings = load_scan_settings(args.setup)
    min_depth = args.min_depth
    min_quality = args.min_quality

    if min_depth is None:
        min_depth = settings.get("refinement_depth", 100) if settings else 100
    if min_quality is None:
        min_quality = settings.get("min_quality_score", 0) if settings else 0

    print(f"Filter: min_depth={min_depth}, min_quality={min_quality}")

    # 2. Load EV grinder output
    ev_data, ev_path = load_ev_data(args.setup, args.ev_file)

    # 3. Filter signals
    signals = filter_signals(ev_data, min_depth, min_quality, args.direction)
    if len(signals) < 5:
        print(f"\nOnly {len(signals)} signals \u2014 need at least 5. Aborting.")
        sys.exit(1)

    # 4. Load OHLCV cache
    ohlcv_cache = load_5yr_cache()

    # 5. Build trade arrays
    trade_data = build_trade_arrays(signals, ohlcv_cache, args.direction, args.max_forward)

    if trade_data["n_valid"] < 5:
        print(f"\nOnly {trade_data['n_valid']} valid trades \u2014 need at least 5. Aborting.")
        sys.exit(1)

    # Free OHLCV cache — no longer needed, trade arrays are built
    del ohlcv_cache

    # 6. Build parameter grid
    combos = build_param_grid(args)
    print(f"\nParameter grid: {len(combos):,} combos")

    # 7. Grind all combos
    all_stats = grind_all_combos(
        combos, trade_data, args.direction, args.time_stop, args.max_forward, args.workers
    )

    # 8. Report
    print(f"\n{'='*80}")
    print(f"TOP RESULTS")
    print(f"{'='*80}")
    print_top_results(all_stats)

    # 9. Save
    save_path = save_results(
        all_stats, trade_data, ev_path, args.setup, args,
        args.direction, args.time_stop, args.max_forward
    )

    # 10. Summary
    print(f"\n{'='*80}")
    print(f"PROFIT GRINDER COMPLETE")
    print(f"  Signals tested: {trade_data['n_valid']}")
    print(f"  Parameter combos: {len(combos):,}")
    print(f"  Valid combos: {len(all_stats):,}")
    print(f"  Output: {save_path}")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
