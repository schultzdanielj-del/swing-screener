"""
Backtest Conditions — Evaluate grinder-discovered conditions across historical days.

Computes full indicator series once per ticker, then checks thresholds across
all bars with numpy vectorization. ~17s for 4,100 tickers × 200 days.

Usage:
    python scripts/backtest_conditions.py --days 200

Input: grinder result conditions (hardcoded below — update after each grind)
Output: list of (date, ticker) signals where ALL conditions passed
"""

import sys
import os
import time
import pickle
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.expression_engine import ExpressionEngine
from scripts.profiling_engine import count_true, since_true, true_in_row


# ══════════════════════════════════════════════════════════
# GRINDER CONDITIONS — Update these after each grind run
# ══════════════════════════════════════════════════════════
CONDITIONS = [
    {
        "name": "slope_avgc100_off1_adr14",
        "compute": {"op": "ma_slope", "ma": "avgc100", "offset": 1, "normalizer": "adr14"},
        "low": 0.024, "high": 0.132,
    },
    {
        "name": "spread_xavgc13_xavgc50_adr14",
        "compute": {"op": "ma_spread", "ma_fast": "xavgc13", "ma_slow": "xavgc50", "normalizer": "adr14"},
        "low": 0.101, "high": 2.824,
    },
    {
        "name": "adx_7",
        "compute": {"op": "adx", "period": 7},
        "low": 29.731, "high": 85.461,
    },
    {
        "name": "ct_c_gt_avgc200_50",
        "compute": {"op": "count_true", "condition": "c_gt_avgc200", "period": 50},
        "low": 0.000, "high": 0.000,
    },
    {
        "name": "ct_c_gt_c1_15",
        "compute": {"op": "count_true", "condition": "c_gt_c1", "period": 15},
        "low": 6.700, "high": 13.300,
    },
]


def compute_series(engine, comp):
    """Compute full indicator series for one expression.
    
    Returns numpy array of values across all bars. Each series is computed
    once per ticker — not per-bar.
    
    Covers all ops that brute_expressions.py can generate.
    """
    op = comp["op"]
    
    if op == "ma_slope":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((ma - ma.shift(comp["offset"])) / norm).values
    
    elif op == "ma_spread":
        fast = engine._ma(comp["ma_fast"])
        slow = engine._ma(comp["ma_slow"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((fast - slow) / norm).values
    
    elif op == "extension":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((engine.c - ma) / norm).values
    
    elif op == "adx":
        return engine._adx(comp["period"]).values
    
    elif op == "rsi":
        return engine._rsi(comp["period"]).values
    
    elif op == "roc":
        p = comp["period"]
        return (100 * (engine.c / engine.c.shift(p) - 1)).values
    
    elif op == "stochastic":
        return engine._stoch(comp["period"]).values
    
    elif op == "cci":
        return engine._cci(comp["period"]).values
    
    elif op == "volume_ratio":
        avg = engine._ma(f"avgv{comp['avg_period']}")
        return (engine.v / avg).values
    
    elif op == "count_true":
        b = engine._bool_series(comp["condition"])
        return count_true(b, comp["period"]).values
    
    elif op == "since_true":
        b = engine._bool_series(comp["condition"])
        return since_true(b, comp["period"]).values
    
    elif op == "true_in_row":
        b = engine._bool_series(comp["condition"])
        return true_in_row(b, comp["period"]).values
    
    elif op == "range_position":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.c - minl) / rng.replace(0, np.nan)).values
    
    elif op == "pullback":
        maxh = engine._maxh(comp["period"])
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((maxh - engine.c) / norm).values
    
    elif op == "candle_range_ratio":
        rng = engine.h - engine.l
        atr_val = engine._atr(14)
        return (rng / atr_val).values
    
    elif op == "body_range_ratio":
        rng = engine.h - engine.l
        body = (engine.c - engine.o).abs()
        return (body / rng.replace(0, np.nan)).values
    
    elif op == "di_spread":
        return (engine._diplus(comp["period"]) - engine._diminus(comp["period"])).values
    
    elif op == "adx_slope":
        a = engine._adx(comp["period"])
        return (a - a.shift(comp["offset"])).values
    
    elif op == "rsi_slope":
        r = engine._rsi(comp["period"])
        return (r - r.shift(comp["offset"])).values
    
    elif op == "extension_slope":
        ma = engine._ma(comp["ma"])
        norm = _get_normalizer(engine, comp["normalizer"])
        ext = engine.c - ma
        return ((ext - ext.shift(comp["offset"])) / norm).values
    
    elif op == "extension_peak_ratio":
        ma = engine._ma(comp["ma"])
        lb = comp["lookback"]
        ext = engine.c - ma
        max_ext = ext.rolling(lb, min_periods=1).max()
        return (ext / max_ext.replace(0, np.nan)).values
    
    elif op == "extension_ceiling_ratio":
        ma = engine._ma(comp["ma"])
        lb = comp["lookback"]
        norm = _get_normalizer(engine, comp["normalizer"])
        ext_norm = (engine.c - ma) / norm
        max_ext = ext_norm.rolling(lb, min_periods=1).max()
        return (ext_norm / max_ext.replace(0, np.nan)).values
    
    elif op == "ext_adr_multiples":
        ma_val = engine._ma(comp["ma"])
        adr_val = engine._adr(14)
        return ((engine.c - ma_val) / adr_val.replace(0, np.nan)).values
    
    elif op == "range_width":
        p = comp["period"]
        rng = engine._maxh(p) - engine._minl(p)
        norm = _get_normalizer(engine, comp["normalizer"])
        return (rng / norm).values
    
    elif op == "bop":
        return engine._bop(comp["period"]).values
    
    elif op == "roc_delta":
        p = comp["period"]
        co = comp["compare_offset"]
        roc_now = engine.c / engine.c.shift(p) - 1
        roc_prev = engine.c.shift(co) / engine.c.shift(co + p) - 1
        return (100 * (roc_now - roc_prev)).values
    
    elif op == "channel_slope":
        p = comp["period"]
        maxh = engine._maxh(p)
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((maxh - maxh.shift(p)) / norm).values
    
    elif op == "distance_to_maxh":
        price_ref = comp["price_ref"]
        price = engine.c if price_ref == "C" else engine.h
        maxh = engine._maxh(comp["maxh_period"]).shift(1)
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((maxh - price) / norm).values
    
    elif op == "ratio_c_maxh":
        maxh = engine._maxh(comp["maxh_period"]).shift(1)
        return (engine.c / maxh.replace(0, np.nan)).values
    
    elif op == "upper_wick_ratio":
        rng = engine.h - engine.l
        upper = engine.h - pd.concat([engine.c, engine.o], axis=1).max(axis=1)
        return (upper / rng.replace(0, np.nan)).values
    
    elif op == "ma_cross_count":
        fast = engine._ma(comp["ma_fast"])
        slow = engine._ma(comp["ma_slow"])
        above = (fast > slow).astype(int)
        cross = above.diff().abs()
        return cross.rolling(comp["period"], min_periods=1).sum().values

    # ── NEW OPS (Step 3 expansion) ──

    elif op == "distance_to_minl":
        price_ref = comp["price_ref"]
        price = engine.c if price_ref == "C" else engine.l
        minl = engine._minl(comp["minl_period"]).shift(1)
        norm = _get_normalizer(engine, comp["normalizer"])
        return ((price - minl) / norm).values

    elif op == "ratio_c_minl":
        minl = engine._minl(comp["minl_period"]).shift(1)
        return (engine.c / minl.replace(0, np.nan)).values

    elif op == "percentile_rank":
        source = comp["source"]
        period = comp["period"]
        if source == "close":
            s = engine.c
        elif source == "volume":
            s = engine.v
        elif source == "range":
            s = engine.h - engine.l
        elif source == "atr14":
            s = engine._atr(14)
        elif source == "rsi14":
            s = engine._rsi(14)
        else:
            s = engine.c
        # Vectorized percentile rank via rolling
        def pct_rank(window):
            if len(window) < 2:
                return np.nan
            return (window <= window.iloc[-1]).sum() / len(window) * 100
        return s.rolling(period, min_periods=2).apply(pct_rank, raw=False).values

    elif op == "spread_slope":
        fast = engine._ma(comp["ma_fast"])
        slow = engine._ma(comp["ma_slow"])
        norm = _get_normalizer(engine, comp["normalizer"])
        spread = (fast - slow) / norm
        offset = comp["offset"]
        return (spread - spread.shift(offset)).values

    elif op == "rvol_continuous":
        period = comp["period"]
        avg_period = comp["avg_period"]
        avg_vol = engine.v.rolling(avg_period, min_periods=avg_period // 2).mean()
        rvol = engine.v / avg_vol.replace(0, np.nan)
        return rvol.rolling(period, min_periods=1).mean().values

    elif op == "cumulative_rvol":
        period = comp["period"]
        avg_period = comp["avg_period"]
        avg_vol = engine.v.rolling(avg_period, min_periods=avg_period // 2).mean()
        rvol = engine.v / avg_vol.replace(0, np.nan)
        return rvol.rolling(period, min_periods=1).sum().values

    elif op == "slope_ratio":
        fast_ma = engine._ma(comp["fast_ma"])
        slow_ma = engine._ma(comp["slow_ma"])
        offset = comp["offset"]
        fast_slope = fast_ma - fast_ma.shift(offset)
        slow_slope = slow_ma - slow_ma.shift(offset)
        return (fast_slope / slow_slope.replace(0, np.nan)).values

    elif op == "retrace_high":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.h - minl) / rng.replace(0, np.nan)).values

    elif op == "retrace_low":
        p = comp["period"]
        maxh = engine._maxh(p)
        minl = engine._minl(p)
        rng = maxh - minl
        return ((engine.l - minl) / rng.replace(0, np.nan)).values

    elif op == "vwap_slope":
        p = comp["period"]
        offset = comp["offset"]
        norm = _get_normalizer(engine, comp["normalizer"])
        tp = (engine.h + engine.l + engine.c) / 3
        vwap = (tp * engine.v).rolling(p, min_periods=1).sum() / \
               engine.v.rolling(p, min_periods=1).sum().replace(0, np.nan)
        return ((vwap - vwap.shift(offset)) / norm).values

    elif op == "bars_since_ma_cross":
        ma = engine._ma(comp["ma"])
        max_lb = comp.get("max_lookback", 120)
        above = (engine.c > ma).values
        n = len(above)
        result = np.full(n, float(max_lb))
        for i in range(1, n):
            above_now = above[i]
            for back in range(1, min(max_lb, i + 1)):
                if above[i - back] != above_now:
                    result[i] = float(back)
                    break
        return result

    elif op == "gap_count":
        p = comp["period"]
        threshold = comp.get("threshold", 0.5)
        atr_s = engine._atr(14).values
        gaps = np.abs(engine.o.values[1:] - engine.c.values[:-1])
        # Pad first element
        gaps = np.concatenate([[0.0], gaps])
        # Gap in ATR units
        with np.errstate(divide='ignore', invalid='ignore'):
            gap_atr = np.where(atr_s > 0, gaps / atr_s, 0.0)
        is_gap = (gap_atr > threshold).astype(float)
        # Rolling count over period
        result = np.full(len(is_gap), np.nan)
        for i in range(p - 1, len(is_gap)):
            result[i] = np.sum(is_gap[i - p + 1:i + 1])
        return result

    else:
        raise ValueError(f"Unsupported op for backtest series: {op}")


def _get_normalizer(engine, norm_name):
    """Get normalizer series by name."""
    if norm_name == "adr14":
        return engine._adr(14)
    elif norm_name == "atr14":
        return engine._atr(14)
    elif norm_name == "pct":
        return engine.c / 100
    elif norm_name == "close":
        return engine.c
    else:
        return engine._atr(14)


def backtest(universe_cache, conditions, lookback_days=200):
    """Run backtest across all tickers and historical days.
    
    For each ticker:
      1. Create ExpressionEngine (computes indicator series once)
      2. For each condition, get the full series via compute_series()
      3. Vectorized threshold check across all bars at once
      4. Collect signals in the lookback window
    
    Returns list of signal dicts: {date, ticker, bar_idx}
    """
    signals = []
    n_tickers = len(universe_cache)
    t0 = time.time()
    skipped = 0
    
    for idx, (ticker, df) in enumerate(universe_cache.items()):
        if df is None or len(df) < 100:
            skipped += 1
            continue
        
        n_bars = len(df)
        start_bar = max(50, n_bars - lookback_days)
        
        try:
            engine = ExpressionEngine(df)
            
            # Compute all series and check thresholds in one pass
            pass_mask = np.ones(n_bars, dtype=bool)
            for cond in conditions:
                series = compute_series(engine, cond["compute"])
                low, high = cond["low"], cond["high"]
                in_range = (series >= low) & (series <= high)
                in_range[np.isnan(series)] = False
                pass_mask &= in_range
            
            # Only look at backtest window
            pass_mask[:start_bar] = False
            
            # Collect signals
            signal_bars = np.where(pass_mask)[0]
            dates = df["date"]
            for bar_idx in signal_bars:
                d = dates.iloc[bar_idx]
                signals.append({
                    "date": str(d.date()) if hasattr(d, 'date') else str(d)[:10],
                    "ticker": ticker,
                    "bar_idx": int(bar_idx),
                })
        except Exception as e:
            skipped += 1
            continue
        
        if (idx + 1) % 500 == 0 or (idx + 1) == n_tickers:
            elapsed = time.time() - t0
            rate = (idx + 1) / elapsed
            eta = (n_tickers - idx - 1) / rate
            print(f"  {idx+1:,}/{n_tickers:,} tickers | {len(signals)} signals | "
                  f"{elapsed:.1f}s elapsed | ~{eta:.0f}s remaining")
    
    elapsed = time.time() - t0
    print(f"\nBacktest complete: {len(signals)} signals from "
          f"{n_tickers - skipped:,} tickers in {elapsed:.1f}s "
          f"({skipped} skipped)")
    return signals


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backtest grinder conditions across historical days")
    parser.add_argument("--days", type=int, default=200, help="Trading days to look back")
    args = parser.parse_args()
    
    # Load OHLCV cache
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "local_runner", "cache", "universe_ohlcv.pkl")
    if not os.path.exists(cache_file):
        print(f"Cache not found at {cache_file}")
        print("Run: python local_runner/cache_builder.py first")
        sys.exit(1)
    
    print(f"Loading OHLCV cache...")
    t0 = time.time()
    with open(cache_file, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe):,} tickers loaded in {time.time()-t0:.1f}s")
    
    print(f"\nConditions ({len(CONDITIONS)}):")
    for c in CONDITIONS:
        print(f"  {c['name']}: [{c['low']:.3f} — {c['high']:.3f}]")
    
    print(f"\nRunning {args.days}-day backtest...")
    signals = backtest(universe, CONDITIONS, lookback_days=args.days)
    
    # Summary
    if signals:
        df = pd.DataFrame(signals)
        print(f"\n{'='*60}")
        print(f"RESULTS: {len(signals)} total signals over {args.days} days")
        print(f"{'='*60}")
        
        # Signals by date
        by_date = df.groupby("date").size().sort_index()
        print(f"\nSignals by date:")
        for date, count in by_date.items():
            tickers = sorted(df[df["date"] == date]["ticker"].tolist())
            ticker_str = ", ".join(tickers[:15])
            if len(tickers) > 15:
                ticker_str += f"... (+{len(tickers)-15} more)"
            print(f"  {date}: {count:>3} — {ticker_str}")
        
        print(f"\n--- Summary ---")
        print(f"Total signals: {len(signals)}")
        print(f"Unique dates with signals: {df['date'].nunique()}")
        print(f"Unique tickers: {df['ticker'].nunique()}")
        print(f"Avg signals/day (signal days only): {len(signals)/df['date'].nunique():.1f}")
        print(f"Avg signals/day (all {args.days} days): {len(signals)/args.days:.1f}")
    else:
        print("\nNo signals found.")


if __name__ == "__main__":
    main()
