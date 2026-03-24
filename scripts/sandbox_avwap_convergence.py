"""
Sandbox: Test pivot-to-pivot contextual AVWAP detection on 10 tickers.

THIS IS PURELY AVWAP DETECTION. It has NOTHING to do with algo lines.
The contextual AVWAP is a standalone TA concept: find the AVWAP anchored
in the trend leg leading into the previous D1 pivot that produces the
highest (pivot high) or lowest (pivot low) value at the current bar.

Run locally:
    python -m scripts.sandbox_avwap_convergence

Requires: local_runner/cache/universe_ohlcv_5yr.pkl
"""

import os
import sys
import time
import pickle
import numpy as np
import pandas as pd

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.lsp_detector_v2 import (
    LSPDetectorV2, precompute_avwap_arrays, resample_ohlcv
)


# ══════════════════════════════════════════════════════════════
# CONTEXTUAL AVWAP DETECTION
# ══════════════════════════════════════════════════════════════
# This is a standalone TA concept. It does NOT depend on algo lines,
# trendlines, or any other detection system. It only needs:
#   - D1 pivots (from LSP detector)
#   - Cumulative TPV/V arrays (for O(1) AVWAP computation)
#   - 8 EMA (for pivot separation filtering)
# ══════════════════════════════════════════════════════════════

def _compute_ema8(closes: np.ndarray) -> np.ndarray:
    """8-period EMA. Pure numpy."""
    n = len(closes)
    ema = np.full(n, np.nan, dtype=np.float64)
    if n < 8:
        return ema
    ema[7] = closes[:8].mean()
    mult = 2.0 / (8 + 1)
    for i in range(8, n):
        ema[i] = closes[i] * mult + ema[i - 1] * (1 - mult)
    return ema


def _ema_crossed_between(ema: np.ndarray, closes: np.ndarray,
                         start_idx: int, end_idx: int) -> bool:
    """Check if close crossed the 8 EMA between start_idx and end_idx.

    A cross = close went from one side of EMA to the other at any point
    in the range. This confirms the two pivots are on opposite sides of
    a meaningful move, not just minor wiggles in the same consolidation.
    """
    if start_idx < 0 or end_idx <= start_idx:
        return False

    segment_close = closes[start_idx:end_idx + 1]
    segment_ema = ema[start_idx:end_idx + 1]

    valid = ~np.isnan(segment_ema)
    if not valid.any():
        return False

    diff = segment_close[valid] - segment_ema[valid]
    if len(diff) < 2:
        return False

    signs = np.sign(diff)
    signs = signs[signs != 0]
    if len(signs) < 2:
        return False

    return np.any(signs[1:] != signs[:-1])


def find_contextual_avwap(bar_idx: int, pivots: list, cum_tpv: np.ndarray,
                          cum_v: np.ndarray, n_bars: int,
                          ema8: np.ndarray = None,
                          closes: np.ndarray = None) -> tuple:
    """Find the contextual AVWAP at bar_idx using pivot-to-pivot range.

    Logic:
    1. Find the nearest D1 pivot BEFORE bar_idx
    2. Walk backward to find the previous opposite pivot where the 8 EMA
       was crossed between the two — confirms a real trend change
    3. Sweep all anchors in that range for max (pivot high) or min (pivot low)
    4. Return (avwap_value, pivot_idx, opposite_pivot_idx, anchor_idx, pivot_is_high)

    Returns (np.nan, -1, -1, -1, None) if no valid pivots found.
    """
    if bar_idx >= len(cum_tpv):
        return (np.nan, -1, -1, -1, None)

    prior_pivots = [(p.idx, p.is_high, p.price, p.max_window)
                    for p in pivots if p.idx < bar_idx]

    if not prior_pivots:
        return (np.nan, -1, -1, -1, None)

    prior_pivots.sort(key=lambda x: -x[0])

    pivot_idx = prior_pivots[0][0]
    pivot_is_high = prior_pivots[0][1]

    # Find previous opposite pivot with 8 EMA cross between them
    opposite_idx = -1
    have_ema = ema8 is not None and closes is not None
    for p_idx, p_is_high, p_price, p_window in prior_pivots[1:]:
        if p_is_high != pivot_is_high:
            if have_ema:
                if _ema_crossed_between(ema8, closes, p_idx, pivot_idx):
                    opposite_idx = p_idx
                    break
            else:
                opposite_idx = p_idx
                break

    search_start = opposite_idx if opposite_idx >= 0 else 0
    search_end = pivot_idx

    if search_start >= search_end:
        return (np.nan, pivot_idx, opposite_idx, -1, pivot_is_high)

    # Vectorized AVWAP computation across all anchors in range
    anchors = np.arange(search_start, search_end)

    tpv_at_bar = cum_tpv[bar_idx]
    v_at_bar = cum_v[bar_idx]

    if anchors[0] == 0:
        prev_tpv = np.empty(len(anchors))
        prev_v = np.empty(len(anchors))
        prev_tpv[0] = 0.0
        prev_v[0] = 0.0
        if len(anchors) > 1:
            prev_tpv[1:] = cum_tpv[anchors[1:] - 1]
            prev_v[1:] = cum_v[anchors[1:] - 1]
    else:
        prev_tpv = cum_tpv[anchors - 1]
        prev_v = cum_v[anchors - 1]

    total_tpv = tpv_at_bar - prev_tpv
    total_v = v_at_bar - prev_v

    valid = total_v > 0
    if not valid.any():
        return (np.nan, pivot_idx, opposite_idx, -1, pivot_is_high)

    # Pick max AVWAP for pivot highs, min AVWAP for pivot lows
    if pivot_is_high:
        avwaps = np.full(len(anchors), -np.inf)
        avwaps[valid] = total_tpv[valid] / total_v[valid]
        best_local_idx = avwaps.argmax()
        best_avwap = avwaps[best_local_idx]
        if best_avwap == -np.inf:
            return (np.nan, pivot_idx, opposite_idx, -1, pivot_is_high)
    else:
        avwaps = np.full(len(anchors), np.inf)
        avwaps[valid] = total_tpv[valid] / total_v[valid]
        best_local_idx = avwaps.argmin()
        best_avwap = avwaps[best_local_idx]
        if best_avwap == np.inf:
            return (np.nan, pivot_idx, opposite_idx, -1, pivot_is_high)

    best_anchor_idx = anchors[best_local_idx]
    return (best_avwap, pivot_idx, opposite_idx, best_anchor_idx, pivot_is_high)


# ══════════════════════════════════════════════════════════════
# TEST HARNESS
# ══════════════════════════════════════════════════════════════

def test_ticker(ticker: str, df: pd.DataFrame) -> dict:
    """Test contextual AVWAP detection on one ticker."""

    if not pd.api.types.is_datetime64_any_dtype(df['date']):
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])

    n_bars = len(df)
    closes = df['close'].values.astype(np.float64)

    # Detect pivots via LSP detector
    weekly_df = resample_ohlcv(df, 'W') if n_bars >= 20 else None
    monthly_df = resample_ohlcv(df, 'ME') if n_bars >= 60 else None
    detector = LSPDetectorV2(df, weekly_df, monthly_df)
    pivots = detector.pivots

    # Precompute AVWAP arrays
    cum_tpv, cum_v = precompute_avwap_arrays(df)

    # 8 EMA for pivot separation filter
    ema8 = _compute_ema8(closes)

    # Sample bars (every 50th bar from 100 onward, plus last bar)
    sample_bars = list(range(100, n_bars, 50))
    if n_bars - 1 not in sample_bars:
        sample_bars.append(n_bars - 1)

    results = {
        'ticker': ticker,
        'n_bars': n_bars,
        'n_pivots': len(pivots),
        'samples': [],
    }

    for bar_idx in sample_bars:
        close = closes[bar_idx]
        if close <= 0:
            continue

        avwap_price, pivot_used, opp_pivot, anchor_used, pivot_is_high = \
            find_contextual_avwap(bar_idx, pivots, cum_tpv, cum_v, n_bars,
                                 ema8=ema8, closes=closes)

        results['samples'].append({
            'bar': bar_idx,
            'date': str(df['date'].iloc[bar_idx].date()),
            'close': round(close, 2),
            'avwap': round(avwap_price, 2) if not np.isnan(avwap_price) else 'NaN',
            'pivot_type': 'HIGH' if pivot_is_high else 'LOW' if pivot_is_high is not None else '—',
            'pivot_bar': pivot_used,
            'opp_pivot_bar': opp_pivot,
            'anchor_bar': anchor_used,
            'width': (pivot_used - opp_pivot) if opp_pivot >= 0 else 'N/A',
        })

    return results


def main():
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
    print("=" * 100)

    for ticker in available:
        data = cache[ticker]
        df = pd.DataFrame(data) if isinstance(data, dict) else data

        if len(df) < 100:
            print(f"\n{ticker}: Only {len(df)} bars, skipping")
            continue

        t0 = time.time()
        results = test_ticker(ticker, df)
        elapsed = time.time() - t0

        print(f"\n{'─' * 100}")
        print(f"{ticker}: {results['n_bars']} bars, {results['n_pivots']} pivots, {elapsed:.2f}s")

        if not results['samples']:
            print("  No valid bars sampled")
            continue

        # Show last 5 samples
        recent = results['samples'][-5:]
        for s in recent:
            av = f"{s['avwap']:.2f}" if isinstance(s['avwap'], float) else str(s['avwap'])
            print(f"  bar {s['bar']:4d} ({s['date']}) close=${s['close']:>8.2f} | "
                  f"AVWAP=${av:>8s}  pivot={s['pivot_type']:4s} "
                  f"pivot@{s['pivot_bar']}  opp@{s['opp_pivot_bar']}  "
                  f"anchor@{s['anchor_bar']}  width={s['width']}")

    print(f"\n{'=' * 100}")
    print("Done. Verify:")
    print("  1. Search widths represent real trend legs (not 1-3 bar wiggles)")
    print("  2. AVWAP prices are plausible relative to close")
    print("  3. Pivot type (HIGH/LOW) makes sense for the context")


if __name__ == "__main__":
    main()
