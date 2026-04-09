"""
Sandbox: Test highest contextual AVWAP detection on 10 tickers.

For each bar, sweep every prior bar as a potential AVWAP anchor.
Pick the one that produces the highest AVWAP value at the current bar.
Report the AVWAP price and its distance from close (ATR-normalized).

This is a standalone TA concept. It has nothing to do with algo lines,
trendlines, pivots, or any other detection system.

Run locally:
    python -m scripts.sandbox_avwap_convergence

Requires: local_runner/cache/universe_ohlcv_daily.pkl
"""

import os
import sys
import time
import pickle
import numpy as np
import pandas as pd

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.lsp_detector_v2 import precompute_avwap_arrays


def _compute_atr14(highs, lows, closes):
    """14-period SMA-based ATR."""
    prev_close = np.empty_like(closes)
    prev_close[0] = closes[0]
    prev_close[1:] = closes[:-1]
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev_close),
                               np.abs(lows - prev_close)))
    atr = np.full_like(tr, np.nan)
    period = 14
    if len(tr) >= period:
        cumsum = np.cumsum(tr)
        atr[period - 1] = cumsum[period - 1] / period
        atr[period:] = (cumsum[period:] - cumsum[:-period]) / period
    return atr


def find_highest_avwap(bar_idx, cum_tpv, cum_v):
    """Find the anchor that produces the highest AVWAP at bar_idx.

    Sweeps every prior bar as a potential anchor. Pure vectorized numpy.

    Returns (avwap_value, anchor_idx) or (np.nan, -1) if none valid.
    """
    if bar_idx < 1 or bar_idx >= len(cum_tpv):
        return (np.nan, -1)

    # All possible anchors: bar 0 through bar_idx-1
    anchors = np.arange(0, bar_idx)

    tpv_at_bar = cum_tpv[bar_idx]
    v_at_bar = cum_v[bar_idx]

    # Compute cumulative values BEFORE each anchor
    # For anchor=0: previous is 0
    # For anchor>0: previous is cum[anchor-1]
    prev_tpv = np.empty(len(anchors))
    prev_v = np.empty(len(anchors))
    prev_tpv[0] = 0.0
    prev_v[0] = 0.0
    if len(anchors) > 1:
        prev_tpv[1:] = cum_tpv[anchors[1:] - 1]
        prev_v[1:] = cum_v[anchors[1:] - 1]

    total_tpv = tpv_at_bar - prev_tpv
    total_v = v_at_bar - prev_v

    valid = total_v > 0
    if not valid.any():
        return (np.nan, -1)

    avwaps = np.full(len(anchors), -np.inf)
    avwaps[valid] = total_tpv[valid] / total_v[valid]

    best_local_idx = avwaps.argmax()
    best_avwap = avwaps[best_local_idx]

    if best_avwap == -np.inf:
        return (np.nan, -1)

    return (best_avwap, anchors[best_local_idx])


def test_ticker(ticker, df):
    """Test highest AVWAP detection on one ticker."""
    if not pd.api.types.is_datetime64_any_dtype(df['date']):
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])

    n_bars = len(df)
    highs = df['high'].values.astype(np.float64)
    lows = df['low'].values.astype(np.float64)
    closes = df['close'].values.astype(np.float64)

    cum_tpv, cum_v = precompute_avwap_arrays(df)
    atr = _compute_atr14(highs, lows, closes)

    # Sample every 50th bar from 100 onward, plus last bar
    sample_bars = list(range(100, n_bars, 50))
    if n_bars - 1 not in sample_bars:
        sample_bars.append(n_bars - 1)

    results = {
        'ticker': ticker,
        'n_bars': n_bars,
        'samples': [],
    }

    for bar_idx in sample_bars:
        close = closes[bar_idx]
        atr_val = atr[bar_idx]
        if close <= 0 or np.isnan(atr_val) or atr_val <= 0:
            continue

        avwap_price, anchor_idx = find_highest_avwap(bar_idx, cum_tpv, cum_v)

        distance = np.nan
        if not np.isnan(avwap_price):
            distance = (avwap_price - close) / atr_val

        results['samples'].append({
            'bar': bar_idx,
            'date': str(df['date'].iloc[bar_idx].date()),
            'close': round(close, 2),
            'avwap': round(avwap_price, 2) if not np.isnan(avwap_price) else 'NaN',
            'distance_atr': round(distance, 2) if not np.isnan(distance) else 'NaN',
            'anchor_bar': anchor_idx,
            'anchor_date': str(df['date'].iloc[anchor_idx].date()) if 0 <= anchor_idx < n_bars else '—',
        })

    return results


def main():
    cache_path = "local_runner/cache/universe_ohlcv_daily.pkl"
    if not os.path.exists(cache_path):
        cache_path = "local_runner/cache/universe_ohlcv_5yr.pkl"
    if not os.path.exists(cache_path):
        cache_path = "local_runner/cache/universe_ohlcv.pkl"
    if not os.path.exists(cache_path):
        print(f"ERROR: No OHLCV cache found at {cache_path}")
        sys.exit(1)

    print(f"Loading OHLCV cache from {cache_path}...")
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)

    test_tickers = ['AAPL', 'TSLA', 'NVDA', 'META', 'POWL',
                    'SMCI', 'CELH', 'MSTR', 'COIN', 'AMD']

    available = [t for t in test_tickers if t in cache]
    if len(available) < 10:
        for t in list(cache.keys()):
            if t not in available:
                available.append(t)
            if len(available) >= 10:
                break

    print(f"\nTesting {len(available)} tickers: {', '.join(available)}")
    print("=" * 95)

    for ticker in available:
        data = cache[ticker]
        df = pd.DataFrame(data) if isinstance(data, dict) else data

        if len(df) < 100:
            print(f"\n{ticker}: Only {len(df)} bars, skipping")
            continue

        t0 = time.time()
        results = test_ticker(ticker, df)
        elapsed = time.time() - t0

        print(f"\n{'─' * 95}")
        print(f"{ticker}: {results['n_bars']} bars, {elapsed:.2f}s")

        if not results['samples']:
            print("  No valid bars sampled")
            continue

        for s in results['samples'][-5:]:
            av = f"{s['avwap']:.2f}" if isinstance(s['avwap'], float) else str(s['avwap'])
            dist = f"{s['distance_atr']:.2f}" if isinstance(s['distance_atr'], float) else str(s['distance_atr'])
            print(f"  {s['date']} close=${s['close']:>8.2f} | "
                  f"AVWAP=${av:>8s}  dist={dist:>6s} ATR | "
                  f"anchor={s['anchor_date']}")

    print(f"\n{'=' * 95}")
    print("Done. Verify:")
    print("  1. AVWAP prices are the highest possible from any anchor")
    print("  2. Anchor dates land on candles that make sense visually")
    print("  3. Distance values are reasonable (positive = AVWAP above price)")


if __name__ == "__main__":
    main()
