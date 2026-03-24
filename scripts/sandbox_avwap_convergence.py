"""
Sandbox: Test pivot-to-pivot contextual AVWAP logic on 10 tickers.

Compares OLD (hardcoded 25-bar window, closest-to-line selection)
vs NEW (pivot-to-pivot range, max/min selection based on pivot direction).

Run locally:
    python scripts/sandbox_avwap_convergence.py

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
    LSPDetectorV2, RawPivot, precompute_avwap_arrays, resample_ohlcv
)
from scripts.algo_line_detector import (
    detect_algo_lines, _find_shallowest_line, _get_active_lines_at_bar,
    _compute_atr14, _line_price_at_bar, AlgoLine
)


# ══════════════════════════════════════════════════════════════
# NEW LOGIC — pivot-to-pivot contextual AVWAP
# ══════════════════════════════════════════════════════════════

def find_contextual_avwap(bar_idx: int, pivots: list, cum_tpv: np.ndarray,
                          cum_v: np.ndarray, n_bars: int) -> tuple:
    """Find the contextual AVWAP at bar_idx using pivot-to-pivot range.

    1. Find the nearest D1 pivot BEFORE bar_idx
    2. Find the previous opposite pivot before that one (trend leg start)
    3. Sweep all anchors in that range for max (pivot high) or min (pivot low)
    4. Return (avwap_value, pivot_idx, opposite_pivot_idx, anchor_idx)

    Returns (np.nan, -1, -1, -1) if no valid pivots found.
    """
    if bar_idx >= len(cum_tpv):
        return (np.nan, -1, -1, -1)

    # Sort pivots by bar index for easy backward scan
    # Only consider pivots before bar_idx
    prior_pivots = [(p.idx, p.is_high, p.price, p.max_window)
                    for p in pivots if p.idx < bar_idx]

    if not prior_pivots:
        return (np.nan, -1, -1, -1)

    # Sort by idx descending (most recent first)
    prior_pivots.sort(key=lambda x: -x[0])

    # Find the most recent pivot (this is the one we're contextualizing)
    pivot_idx = prior_pivots[0][0]
    pivot_is_high = prior_pivots[0][1]

    # Find the previous OPPOSITE pivot before this one
    opposite_idx = -1
    for p_idx, p_is_high, p_price, p_window in prior_pivots[1:]:
        if p_is_high != pivot_is_high:
            opposite_idx = p_idx
            break

    # If no opposite pivot found, use bar 0 as start
    search_start = opposite_idx if opposite_idx >= 0 else 0
    search_end = pivot_idx  # exclusive — never to the right of the pivot

    if search_start >= search_end:
        return (np.nan, pivot_idx, opposite_idx, -1)

    # Vectorized AVWAP computation across all anchors in range
    anchors = np.arange(search_start, search_end)

    tpv_at_bar = cum_tpv[bar_idx]
    v_at_bar = cum_v[bar_idx]

    # Handle anchor=0 case
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
        return (np.nan, pivot_idx, opposite_idx, -1)

    # Pick max AVWAP for pivot highs, min AVWAP for pivot lows
    if pivot_is_high:
        avwaps = np.full(len(anchors), -np.inf)
        avwaps[valid] = total_tpv[valid] / total_v[valid]
        best_local_idx = avwaps.argmax()
        best_avwap = avwaps[best_local_idx]
        if best_avwap == -np.inf:
            return (np.nan, pivot_idx, opposite_idx, -1)
    else:
        avwaps = np.full(len(anchors), np.inf)
        avwaps[valid] = total_tpv[valid] / total_v[valid]
        best_local_idx = avwaps.argmin()
        best_avwap = avwaps[best_local_idx]
        if best_avwap == np.inf:
            return (np.nan, pivot_idx, opposite_idx, -1)

    best_anchor_idx = anchors[best_local_idx]
    return (best_avwap, pivot_idx, opposite_idx, best_anchor_idx)


def compute_avwap_convergence_new(line_price: float, bar_idx: int,
                                   pivots: list, cum_tpv: np.ndarray,
                                   cum_v: np.ndarray, atr_val: float,
                                   n_bars: int) -> float:
    """New convergence metric: distance between algo line price and
    pivot-to-pivot contextual AVWAP, normalized by ATR."""
    if atr_val <= 0:
        return np.nan

    avwap_val, _, _, _ = find_contextual_avwap(
        bar_idx, pivots, cum_tpv, cum_v, n_bars
    )
    if np.isnan(avwap_val):
        return np.nan

    return (line_price - avwap_val) / atr_val


# ══════════════════════════════════════════════════════════════
# OLD LOGIC — copied from current algo_line_detector.py
# ══════════════════════════════════════════════════════════════

def compute_avwap_convergence_old(line: AlgoLine, line_price: float,
                                   bar_idx: int, cum_tpv: np.ndarray,
                                   cum_v: np.ndarray, close: float,
                                   atr_val: float, search_range: int = 25) -> float:
    """Current (broken) logic — for comparison."""
    origin = line.origin_idx
    search_start = max(0, origin - search_range)
    search_end = min(origin + search_range, bar_idx)

    if search_start >= search_end or bar_idx >= len(cum_tpv):
        return np.nan

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
        return np.nan

    avwaps = np.full(len(anchors), np.nan)
    avwaps[valid] = total_tpv[valid] / total_v[valid]

    distances = np.abs(avwaps - line_price)
    best_idx = np.nanargmin(distances)
    best_avwap = avwaps[best_idx]

    if np.isnan(best_avwap) or atr_val <= 0:
        return np.nan

    return (line_price - best_avwap) / atr_val


# ══════════════════════════════════════════════════════════════
# TEST HARNESS
# ══════════════════════════════════════════════════════════════

def test_ticker(ticker: str, df: pd.DataFrame) -> dict:
    """Run old and new AVWAP convergence on one ticker, compare results."""

    if not pd.api.types.is_datetime64_any_dtype(df['date']):
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])

    n_bars = len(df)
    highs = df['high'].values.astype(np.float64)
    lows = df['low'].values.astype(np.float64)
    closes = df['close'].values.astype(np.float64)

    # Detect algo lines
    all_lines = detect_algo_lines(df)

    # Detect pivots via LSP detector
    weekly_df = resample_ohlcv(df, 'W') if n_bars >= 20 else None
    monthly_df = resample_ohlcv(df, 'ME') if n_bars >= 60 else None
    detector = LSPDetectorV2(df, weekly_df, monthly_df)
    pivots = detector.pivots

    # Precompute AVWAP arrays
    cum_tpv, cum_v = precompute_avwap_arrays(df)

    # ATR
    atr = _compute_atr14(highs, lows, closes)

    # Compare at a sample of bars (every 50th bar from 100 onward)
    sample_bars = list(range(100, n_bars, 50))
    if n_bars - 1 not in sample_bars:
        sample_bars.append(n_bars - 1)

    results = {
        'ticker': ticker,
        'n_bars': n_bars,
        'n_pivots': len(pivots),
        'n_hminus': len(all_lines['hminus']),
        'n_lplus': len(all_lines['lplus']),
        'comparisons': [],
    }

    for bar_idx in sample_bars:
        close = closes[bar_idx]
        atr_val = atr[bar_idx]
        if np.isnan(atr_val) or atr_val <= 0 or close <= 0:
            continue

        for dir_name, line_list, is_hminus in [
            ('hminus', all_lines['hminus'], True),
            ('lplus', all_lines['lplus'], False)
        ]:
            active = _get_active_lines_at_bar(line_list, bar_idx, is_hminus, close, atr_val)
            shallowest = _find_shallowest_line(active, is_hminus, close)

            if shallowest is None:
                continue

            s_line, s_distance, s_line_price = shallowest

            old_val = compute_avwap_convergence_old(
                s_line, s_line_price, bar_idx, cum_tpv, cum_v, close, atr_val
            )

            new_val = compute_avwap_convergence_new(
                s_line_price, bar_idx, pivots, cum_tpv, cum_v, atr_val, n_bars
            )

            # Get diagnostic info from new logic
            avwap_price, pivot_used, opp_pivot, anchor_used = find_contextual_avwap(
                bar_idx, pivots, cum_tpv, cum_v, n_bars
            )

            results['comparisons'].append({
                'bar': bar_idx,
                'date': str(df['date'].iloc[bar_idx].date()),
                'direction': dir_name,
                'line_price': round(s_line_price, 2),
                'close': round(close, 2),
                'old_convergence': round(old_val, 4) if not np.isnan(old_val) else 'NaN',
                'new_convergence': round(new_val, 4) if not np.isnan(new_val) else 'NaN',
                'avwap_price': round(avwap_price, 2) if not np.isnan(avwap_price) else 'NaN',
                'pivot_bar': pivot_used,
                'opp_pivot_bar': opp_pivot,
                'anchor_bar': anchor_used,
                'search_width': (pivot_used - opp_pivot) if opp_pivot >= 0 else 'N/A',
            })

    return results


def main():
    # Load cache
    cache_path = "local_runner/cache/universe_ohlcv_5yr.pkl"
    if not os.path.exists(cache_path):
        cache_path = "local_runner/cache/universe_ohlcv.pkl"
    if not os.path.exists(cache_path):
        print(f"ERROR: No OHLCV cache found at {cache_path}")
        sys.exit(1)

    print(f"Loading OHLCV cache from {cache_path}...")
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)

    # Pick 10 tickers — mix of well-known names for variety
    test_tickers = ['AAPL', 'TSLA', 'NVDA', 'META', 'POWL',
                    'SMCI', 'CELH', 'MSTR', 'COIN', 'AMD']

    # Fall back to first 10 if any missing
    available = [t for t in test_tickers if t in cache]
    if len(available) < 10:
        for t in list(cache.keys()):
            if t not in available:
                available.append(t)
            if len(available) >= 10:
                break

    print(f"\nTesting {len(available)} tickers: {', '.join(available)}")
    print("=" * 100)

    total_time_old = 0
    total_time_new = 0

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
        print(f"{ticker}: {results['n_bars']} bars, "
              f"{results['n_pivots']} pivots, "
              f"H-={results['n_hminus']} L+={results['n_lplus']} algo lines, "
              f"{elapsed:.2f}s")

        if not results['comparisons']:
            print("  No shallowest lines found at sample bars")
            continue

        # Show last 5 comparisons (most recent bars)
        recent = results['comparisons'][-5:]
        for c in recent:
            changed = ''
            if c['old_convergence'] != 'NaN' and c['new_convergence'] != 'NaN':
                diff = abs(float(c['new_convergence']) - float(c['old_convergence']))
                if diff > 0.01:
                    changed = ' ← CHANGED'

            lp = f"{c['line_price']:.2f}" if isinstance(c['line_price'], float) else str(c['line_price'])
            cl = f"{c['close']:.2f}" if isinstance(c['close'], float) else str(c['close'])
            ov = f"{c['old_convergence']:.4f}" if isinstance(c['old_convergence'], float) else str(c['old_convergence'])
            nv = f"{c['new_convergence']:.4f}" if isinstance(c['new_convergence'], float) else str(c['new_convergence'])
            av = f"{c['avwap_price']:.2f}" if isinstance(c['avwap_price'], float) else str(c['avwap_price'])
            print(f"  bar {c['bar']:4d} ({c['date']}) {c['direction']:6s} | "
                  f"line=${lp:>8s}  close=${cl:>8s} | "
                  f"OLD={ov:>8s}  NEW={nv:>8s}{changed} | "
                  f"AVWAP=${av:>8s}  "
                  f"pivot@{c['pivot_bar']}  opp@{c['opp_pivot_bar']}  "
                  f"anchor@{c['anchor_bar']}  width={c['search_width']}")

        # Summary stats for this ticker
        old_vals = [c['old_convergence'] for c in results['comparisons']
                    if c['old_convergence'] != 'NaN']
        new_vals = [c['new_convergence'] for c in results['comparisons']
                    if c['new_convergence'] != 'NaN']
        print(f"  Summary: {len(results['comparisons'])} comparisons, "
              f"{len(old_vals)} old valid, {len(new_vals)} new valid")

        if old_vals and new_vals:
            n_changed = sum(1 for c in results['comparisons']
                          if c['old_convergence'] != 'NaN'
                          and c['new_convergence'] != 'NaN'
                          and abs(float(c['new_convergence']) - float(c['old_convergence'])) > 0.01)
            print(f"  Values changed (>0.01 ATR): {n_changed}/{min(len(old_vals), len(new_vals))}")

    print(f"\n{'=' * 100}")
    print("Done. Review output above to verify:")
    print("  1. Pivot-to-pivot search ranges look reasonable (not too wide, not too narrow)")
    print("  2. Anchor bars fall within the trend leg (between opposite pivot and pivot)")
    print("  3. AVWAP prices make sense relative to line prices and close")
    print("  4. Values changed meaningfully from old logic")


if __name__ == "__main__":
    main()
