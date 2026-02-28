"""
Algo Line Detector — Trendline detection from high-volume D1 candles.

Detects H- (downsloping from highs) and L+ (upsloping from lows) algo lines
across full daily OHLCV history. Produces precomputed expression series for
the expression cache, same pattern as lsp_detector_v2.py.

Key rules (from ta_knowledge.md):
- H- lines: downsloping, originate from HIGHS of candles with V > 50-period MA(V)
- L+ lines: upsloping, originate from LOWS of candles with V > 50-period MA(V)
- Touch points: subsequent candle highs (H-) or lows (L+) that contact the line
- No violations: no wick can cross the line between origin and current bar
- Touch tolerance: ~0.3% of price (standard charting software snap)
- More touches = stronger; high-volume touches add extra significance
- Broken lines that get retested become potential support/resistance

Design:
- Accepts DataFrames only (no API calls) — designed for cache builder
- Daily timeframe only (algo lines don't need weekly/monthly)
- Precomputes all lines once per ticker
- compute_all_algo_series() is the single entry point for expr_cache_builder.py
- Returns dict of expression_name → float64 array of length n_bars

Performance:
- Vectorized violation checking with numpy broadcasting
- O(n²) origination pairs with O(n) validation per pair
- Parallel across tickers via cache builder's ProcessPoolExecutor
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════

@dataclass
class AlgoLine:
    """A detected algo trendline (H- or L+)."""
    origin_idx: int          # Bar index of origination point
    origin_price: float      # Price at origin (high for H-, low for L+)
    slope_per_bar: float     # Price change per bar (negative for H-, positive for L+)
    is_hminus: bool          # True = H- (downsloping from highs), False = L+ (upsloping from lows)
    touch_indices: list      # Bar indices of all touch points (including origin)
    touch_count: int         # Total touch points
    hivol_touch_count: int   # Touch points that are also high-volume candles
    last_valid_idx: int      # Last bar where line has no violations (line extends to here)
    broken_idx: int          # Bar index where line was first broken (-1 if unbroken)


# ══════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════

TOUCH_TOLERANCE_PCT = 0.003   # 0.3% snap tolerance for touch detection
VOLUME_MA_PERIOD = 50         # Period for volume moving average (origin qualification)
MIN_TOUCH_COUNT = 2           # Minimum touches to qualify (origin + 1)
MIN_LINE_BARS = 5             # Minimum bars between origin and second touch


# ══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS — Pure numpy, no pandas
# ══════════════════════════════════════════════════════════════

def _compute_volume_sma50(volumes: np.ndarray) -> np.ndarray:
    """50-period SMA of volume. Pure numpy."""
    n = len(volumes)
    result = np.full(n, np.nan, dtype=np.float64)
    if n < VOLUME_MA_PERIOD:
        return result
    cumsum = np.cumsum(volumes.astype(np.float64))
    result[VOLUME_MA_PERIOD - 1] = cumsum[VOLUME_MA_PERIOD - 1] / VOLUME_MA_PERIOD
    result[VOLUME_MA_PERIOD:] = (cumsum[VOLUME_MA_PERIOD:] - cumsum[:-VOLUME_MA_PERIOD]) / VOLUME_MA_PERIOD
    return result


def _compute_atr14(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """14-period SMA-based ATR. Pure numpy. Matches TC2000/expression engine."""
    period = 14
    prev_close = np.empty_like(closes)
    prev_close[0] = closes[0]
    prev_close[1:] = closes[:-1]

    tr = np.maximum(
        highs - lows,
        np.maximum(
            np.abs(highs - prev_close),
            np.abs(lows - prev_close)
        )
    )

    atr_arr = np.full_like(tr, np.nan)
    n = len(tr)
    if n >= period:
        cumsum = np.cumsum(tr)
        atr_arr[period - 1] = cumsum[period - 1] / period
        atr_arr[period:] = (cumsum[period:] - cumsum[:-period]) / period

    return atr_arr


def _line_price_at_bar(origin_idx: int, origin_price: float,
                       slope: float, bar_idx: int) -> float:
    """Get the price of a trendline at a specific bar index."""
    return origin_price + slope * (bar_idx - origin_idx)


# ══════════════════════════════════════════════════════════════
# ALGO LINE DETECTION
# ══════════════════════════════════════════════════════════════

def _find_high_volume_bars(volumes: np.ndarray) -> np.ndarray:
    """Find bar indices where volume > 50-period SMA of volume."""
    vol_sma = _compute_volume_sma50(volumes)
    # Only consider bars where SMA is valid (>= 50 bars in)
    valid = ~np.isnan(vol_sma) & (vol_sma > 0)
    return np.where(valid & (volumes > vol_sma))[0]


def _find_local_extremes(prices: np.ndarray, is_high: bool, window: int = 3) -> np.ndarray:
    """Find local maxima (for highs) or minima (for lows) as touch candidates.

    A bar is a local extreme if its price is >= (high) or <= (low) its neighbors
    within the window. This dramatically reduces candidate second touch points.
    """
    n = len(prices)
    if n < window * 2 + 1:
        return np.arange(n)

    mask = np.zeros(n, dtype=bool)
    for i in range(window, n - window):
        left = prices[max(0, i - window):i]
        right = prices[i + 1:min(n, i + window + 1)]
        if is_high:
            if prices[i] >= left.max() and prices[i] >= right.max():
                mask[i] = True
        else:
            if prices[i] <= left.min() and prices[i] <= right.min():
                mask[i] = True

    # Also include first and last few bars
    mask[:window] = True
    mask[-window:] = True
    return np.where(mask)[0]


def _detect_lines_from_origin(origin_idx: int, origin_price: float,
                              is_hminus: bool, prices: np.ndarray,
                              check_prices: np.ndarray,
                              volumes: np.ndarray, vol_sma: np.ndarray,
                              n_bars: int,
                              candidate_second_bars: np.ndarray = None) -> list:
    """Detect all valid algo lines from a single origination point.

    Optimized: only tests local extremes as second touch points,
    vectorized violation checking and forward extension.

    Args:
        candidate_second_bars: Pre-filtered local extremes to try as second touch.
                               If None, tries all bars (slow).

    Returns list of AlgoLine objects.
    """
    lines = []
    tol = TOUCH_TOLERANCE_PCT  # 0.3%

    start = origin_idx + MIN_LINE_BARS
    if start >= n_bars:
        return lines

    # Use pre-filtered candidates if provided
    if candidate_second_bars is not None:
        candidates = candidate_second_bars[candidate_second_bars >= start]
    else:
        candidates = np.arange(start, n_bars)

    if len(candidates) == 0:
        return lines

    candidate_prices = prices[candidates]
    bar_diffs = candidates - origin_idx
    slopes = (candidate_prices - origin_price) / bar_diffs

    # Filter by direction
    if is_hminus:
        valid_slope = slopes <= 0
    else:
        valid_slope = slopes >= 0

    valid_mask = np.where(valid_slope)[0]
    if len(valid_mask) == 0:
        return lines

    for vi in valid_mask:
        second_bar = candidates[vi]
        slope = slopes[vi]

        # Check violations between origin and second point
        if second_bar > origin_idx + 1:
            between = np.arange(origin_idx + 1, second_bar)
            line_at_between = origin_price + slope * (between - origin_idx)
            check_between = check_prices[between]
            tolerances = line_at_between * tol

            if is_hminus:
                if np.any(check_between > line_at_between + tolerances):
                    continue
            else:
                if np.any(check_between < line_at_between - tolerances):
                    continue

        # Valid line. Extend forward vectorized.
        forward_bars = np.arange(second_bar + 1, n_bars)
        if len(forward_bars) > 0:
            line_at_forward = origin_price + slope * (forward_bars - origin_idx)
            check_forward = check_prices[forward_bars]
            forward_tolerances = line_at_forward * tol

            if is_hminus:
                violations = check_forward > line_at_forward + forward_tolerances
            else:
                violations = check_forward < line_at_forward - forward_tolerances

            viol_positions = np.where(violations)[0]
            if len(viol_positions) > 0:
                broken_rel = viol_positions[0]
                broken_idx = forward_bars[broken_rel]
                valid_forward = forward_bars[:broken_rel]
                last_valid_idx = forward_bars[broken_rel - 1] if broken_rel > 0 else second_bar
            else:
                broken_idx = -1
                valid_forward = forward_bars
                last_valid_idx = forward_bars[-1]

            # Find touches in valid forward range
            if len(valid_forward) > 0:
                touch_prices = prices[valid_forward]
                line_at_touches = origin_price + slope * (valid_forward - origin_idx)
                touch_tol = line_at_touches * tol
                is_touch = np.abs(touch_prices - line_at_touches) <= touch_tol
                touch_bars = valid_forward[is_touch]
            else:
                touch_bars = np.array([], dtype=np.int64)
        else:
            broken_idx = -1
            last_valid_idx = second_bar
            touch_bars = np.array([], dtype=np.int64)

        touch_indices = [origin_idx, second_bar] + touch_bars.tolist()
        touch_count = len(touch_indices)

        if touch_count < MIN_TOUCH_COUNT:
            continue

        # Count high-volume touches
        hivol_touches = 1  # Origin always high-volume
        for t_idx in touch_indices[1:]:
            if t_idx < len(vol_sma) and not np.isnan(vol_sma[t_idx]):
                if volumes[t_idx] > vol_sma[t_idx]:
                    hivol_touches += 1

        lines.append(AlgoLine(
            origin_idx=origin_idx,
            origin_price=origin_price,
            slope_per_bar=slope,
            is_hminus=is_hminus,
            touch_indices=touch_indices,
            touch_count=touch_count,
            hivol_touch_count=hivol_touches,
            last_valid_idx=last_valid_idx,
            broken_idx=broken_idx,
        ))

    return lines


def _deduplicate_lines(lines: list, prices: np.ndarray, n_bars: int) -> list:
    """Remove near-duplicate algo lines that are very similar.

    Two lines are duplicates if their prices at the midpoint of the chart
    differ by less than 0.5% AND they have the same direction.
    Keep the one with more touches (or more hivol touches as tiebreak).
    """
    if len(lines) <= 1:
        return lines

    mid_bar = n_bars // 2
    dedup = []

    # Sort by touch_count descending (best lines first)
    sorted_lines = sorted(lines, key=lambda l: (-l.touch_count, -l.hivol_touch_count))

    for line in sorted_lines:
        line_mid_price = _line_price_at_bar(
            line.origin_idx, line.origin_price, line.slope_per_bar, mid_bar
        )
        is_dup = False
        for existing in dedup:
            if existing.is_hminus != line.is_hminus:
                continue
            existing_mid_price = _line_price_at_bar(
                existing.origin_idx, existing.origin_price,
                existing.slope_per_bar, mid_bar
            )
            if existing_mid_price > 0:
                pct_diff = abs(line_mid_price - existing_mid_price) / existing_mid_price
                if pct_diff < 0.005:  # 0.5% similarity threshold
                    is_dup = True
                    break
        if not is_dup:
            dedup.append(line)

    return dedup


def detect_algo_lines(daily_df: pd.DataFrame) -> dict:
    """Detect all H- and L+ algo lines in daily OHLCV data.

    Args:
        daily_df: DataFrame with date, open, high, low, close, volume

    Returns:
        dict with 'hminus': [AlgoLine, ...], 'lplus': [AlgoLine, ...]
        Each list sorted by touch_count descending.
    """
    n_bars = len(daily_df)
    if n_bars < VOLUME_MA_PERIOD + MIN_LINE_BARS:
        return {'hminus': [], 'lplus': []}

    highs = daily_df['high'].values.astype(np.float64)
    lows = daily_df['low'].values.astype(np.float64)
    closes = daily_df['close'].values.astype(np.float64)
    volumes = daily_df['volume'].values.astype(np.float64)

    vol_sma = _compute_volume_sma50(volumes)
    hivol_bars = _find_high_volume_bars(volumes)

    # Pre-compute local extremes as candidate second touch points
    # This reduces candidates from ~1200 to ~200-300
    high_extremes = _find_local_extremes(highs, is_high=True, window=3)
    low_extremes = _find_local_extremes(lows, is_high=False, window=3)

    all_hminus = []
    all_lplus = []

    for origin_idx in hivol_bars:
        # H- lines: originate from highs, downsloping
        h_lines = _detect_lines_from_origin(
            origin_idx=origin_idx,
            origin_price=highs[origin_idx],
            is_hminus=True,
            prices=highs,
            check_prices=highs,
            volumes=volumes,
            vol_sma=vol_sma,
            n_bars=n_bars,
            candidate_second_bars=high_extremes,
        )
        all_hminus.extend(h_lines)

        # L+ lines: originate from lows, upsloping
        l_lines = _detect_lines_from_origin(
            origin_idx=origin_idx,
            origin_price=lows[origin_idx],
            is_hminus=False,
            prices=lows,
            check_prices=lows,
            volumes=volumes,
            vol_sma=vol_sma,
            n_bars=n_bars,
            candidate_second_bars=low_extremes,
        )
        all_lplus.extend(l_lines)

    # Deduplicate
    all_hminus = _deduplicate_lines(all_hminus, highs, n_bars)
    all_lplus = _deduplicate_lines(all_lplus, lows, n_bars)

    # Sort by touch count (most significant first)
    all_hminus.sort(key=lambda l: (-l.touch_count, -l.hivol_touch_count))
    all_lplus.sort(key=lambda l: (-l.touch_count, -l.hivol_touch_count))

    return {'hminus': all_hminus, 'lplus': all_lplus}


# ══════════════════════════════════════════════════════════════
# EXPRESSION SERIES COMPUTATION
# ══════════════════════════════════════════════════════════════

def _get_active_lines_at_bar(lines: list, bar_idx: int,
                             is_hminus: bool, close: float,
                             atr_val: float) -> list:
    """Get algo lines that are relevant at a specific bar.

    A line is relevant if:
    - Its origin is before bar_idx
    - Its second touch point is before or at bar_idx
    - It either extends to bar_idx (unbroken) OR was broken (for retest tracking)

    Returns list of (line, distance, line_price, is_broken_at_bar) tuples,
    sorted by absolute distance (nearest first).
    """
    results = []

    for line in lines:
        # Line must have at least its second touch established
        if len(line.touch_indices) < 2:
            continue
        if line.touch_indices[1] > bar_idx:
            continue  # Second touch hasn't happened yet

        line_price = _line_price_at_bar(
            line.origin_idx, line.origin_price,
            line.slope_per_bar, bar_idx
        )

        # Skip lines with non-positive prices (extrapolated too far)
        if line_price <= 0:
            continue

        # Is the line broken as of this bar?
        is_broken = line.broken_idx != -1 and line.broken_idx <= bar_idx

        # Distance from close to line, normalized by ATR
        if atr_val > 0:
            distance = (line_price - close) / atr_val
        else:
            distance = 0.0

        results.append((line, distance, line_price, is_broken))

    # Sort by absolute distance (nearest first)
    results.sort(key=lambda x: abs(x[1]))
    return results


def _find_shallowest_line(active_lines: list, is_hminus: bool,
                          close: float) -> Optional[tuple]:
    """Find the shallowest line that is above (H-) or below (L+) price and unbroken.

    For H-: shallowest downsloping line currently ABOVE close (resistance overhead)
    For L+: shallowest upsloping line currently BELOW close (support underneath)

    Returns (line, distance, line_price) or None.
    """
    candidates = []
    for line, distance, line_price, is_broken in active_lines:
        if is_broken:
            continue  # Only unbroken lines
        if is_hminus and line_price > close:
            # H- line above price = resistance
            candidates.append((line, distance, line_price))
        elif not is_hminus and line_price < close:
            # L+ line below price = support
            candidates.append((line, distance, line_price))

    if not candidates:
        return None

    # Shallowest = smallest absolute slope
    candidates.sort(key=lambda x: abs(x[0].slope_per_bar))
    return candidates[0]


def compute_all_algo_series(daily_df: pd.DataFrame,
                            cum_tpv: np.ndarray = None,
                            cum_v: np.ndarray = None,
                            n_ranks: int = 3) -> dict:
    """Compute ALL algo line expression series for one ticker.

    This is the main entry point called by expr_cache_builder.py.

    Args:
        daily_df: Daily OHLCV DataFrame
        cum_tpv: Precomputed cumulative TP*V (for AVWAP convergence)
        cum_v: Precomputed cumulative V (for AVWAP convergence)
        n_ranks: Number of proximity-ranked lines per direction

    Returns:
        dict mapping expression name to float64 array of length n_bars
    """
    n_bars = len(daily_df)

    # Detect all lines
    all_lines = detect_algo_lines(daily_df)
    hminus_lines = all_lines['hminus']
    lplus_lines = all_lines['lplus']

    # Precompute ATR for normalization
    highs = daily_df['high'].values.astype(np.float64)
    lows = daily_df['low'].values.astype(np.float64)
    closes = daily_df['close'].values.astype(np.float64)
    atr = _compute_atr14(highs, lows, closes)

    # Pre-allocate all output arrays
    series = {}
    directions = [('hminus', hminus_lines, True), ('lplus', lplus_lines, False)]
    metrics = ['distance', 'touch_count', 'hivol_touch_count', 'slope', 'broken', 'retest_distance']
    shallowest_metrics = ['shallowest_distance', 'shallowest_slope',
                          'shallowest_touch_count', 'shallowest_avwap_convergence']

    for dir_name, _, _ in directions:
        for rank in range(1, n_ranks + 1):
            for metric in metrics:
                name = f"algo_{dir_name}{rank}_{metric}"
                series[name] = np.full(n_bars, np.nan, dtype=np.float64)
        for metric in shallowest_metrics:
            name = f"algo_{dir_name}_{metric}"
            series[name] = np.full(n_bars, np.nan, dtype=np.float64)

    # Compute AVWAP arrays if not provided (for convergence metric)
    compute_avwap = cum_tpv is not None and cum_v is not None
    if not compute_avwap:
        try:
            tp = (highs + lows + closes) / 3.0
            tpv = tp * daily_df['volume'].values.astype(np.float64)
            cum_tpv = np.cumsum(tpv)
            cum_v = np.cumsum(daily_df['volume'].values.astype(np.float64))
            compute_avwap = True
        except:
            pass

    # Bar-by-bar computation
    # Start from bar 60 (need 50 bars for volume SMA + some history for lines)
    for bar_idx in range(60, n_bars):
        close = closes[bar_idx]
        atr_val = atr[bar_idx]

        if np.isnan(atr_val) or atr_val <= 0 or close <= 0:
            continue

        for dir_name, line_list, is_hminus in directions:
            # Get active lines at this bar, sorted by proximity
            active = _get_active_lines_at_bar(
                line_list, bar_idx, is_hminus, close, atr_val
            )

            # Ranked lines (top N by proximity)
            for rank_idx in range(min(n_ranks, len(active))):
                rank = rank_idx + 1
                line, distance, line_price, is_broken = active[rank_idx]
                prefix = f"algo_{dir_name}{rank}_"

                series[prefix + "distance"][bar_idx] = distance
                series[prefix + "touch_count"][bar_idx] = line.touch_count
                series[prefix + "hivol_touch_count"][bar_idx] = line.hivol_touch_count
                series[prefix + "slope"][bar_idx] = line.slope_per_bar / atr_val if atr_val > 0 else 0.0
                series[prefix + "broken"][bar_idx] = 1.0 if is_broken else 0.0

                if is_broken:
                    series[prefix + "retest_distance"][bar_idx] = distance
                # else stays NaN (unbroken = no retest concept)

            # Shallowest line in the way
            shallowest = _find_shallowest_line(active, is_hminus, close)
            shal_prefix = f"algo_{dir_name}_shallowest_"

            if shallowest is not None:
                s_line, s_distance, s_line_price = shallowest
                series[shal_prefix + "distance"][bar_idx] = s_distance
                series[shal_prefix + "slope"][bar_idx] = s_line.slope_per_bar / atr_val if atr_val > 0 else 0.0
                series[shal_prefix + "touch_count"][bar_idx] = s_line.touch_count

                # AVWAP convergence: distance between shallowest line price
                # and the nearest contextual AVWAP
                if compute_avwap:
                    convergence = _compute_avwap_convergence(
                        s_line, s_line_price, bar_idx,
                        cum_tpv, cum_v, close, atr_val
                    )
                    series[shal_prefix + "avwap_convergence"][bar_idx] = convergence

    return series


def _compute_avwap_convergence(line: AlgoLine, line_price: float,
                               bar_idx: int, cum_tpv: np.ndarray,
                               cum_v: np.ndarray, close: float,
                               atr_val: float, search_range: int = 25) -> float:
    """Compute convergence between algo line price and nearest contextual AVWAP.

    Searches for the AVWAP anchored near the line's origin that produces
    the closest value to the line price at the current bar.

    Returns distance between line price and best AVWAP, normalized by ATR.
    Small values = convergence (line and AVWAP aligning).
    """
    origin = line.origin_idx
    search_start = max(0, origin - search_range)
    search_end = min(origin + search_range, bar_idx)

    if search_start >= search_end or bar_idx >= len(cum_tpv):
        return np.nan

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
        return np.nan

    avwaps = np.full(len(anchors), np.nan)
    avwaps[valid] = total_tpv[valid] / total_v[valid]

    # Find the AVWAP closest to the line price
    distances = np.abs(avwaps - line_price)
    best_idx = np.nanargmin(distances)
    best_avwap = avwaps[best_idx]

    if np.isnan(best_avwap) or atr_val <= 0:
        return np.nan

    # Return distance between line and AVWAP, normalized by ATR
    return (line_price - best_avwap) / atr_val


# ══════════════════════════════════════════════════════════════
# EXPRESSION NAME REGISTRY
# ══════════════════════════════════════════════════════════════

def get_algo_expression_names(n_ranks: int = 3) -> list:
    """Get the ordered list of algo line expression names.

    Used by brute_expressions.py and cache builder to know the column order.
    """
    names = []
    metrics = ['distance', 'touch_count', 'hivol_touch_count',
               'slope', 'broken', 'retest_distance']
    shallowest_metrics = ['shallowest_distance', 'shallowest_slope',
                          'shallowest_touch_count', 'shallowest_avwap_convergence']

    for dir_name in ['hminus', 'lplus']:
        for rank in range(1, n_ranks + 1):
            for metric in metrics:
                names.append(f"algo_{dir_name}{rank}_{metric}")

    for dir_name in ['hminus', 'lplus']:
        for metric in shallowest_metrics:
            names.append(f"algo_{dir_name}_{metric}")

    return names


# ══════════════════════════════════════════════════════════════
# CLI — For testing and benchmarking
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import os
    import pickle
    import time

    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if len(sys.argv) > 1 and sys.argv[1] == "bench":
        # Benchmark: detect algo lines + compute series for one ticker
        ticker = sys.argv[2] if len(sys.argv) > 2 else "AAPL"
        cache_path = "local_runner/cache/universe_ohlcv_5yr.pkl"
        if not os.path.exists(cache_path):
            cache_path = "local_runner/cache/universe_ohlcv.pkl"
        print(f"Loading cache...")
        with open(cache_path, "rb") as f:
            cache = pickle.load(f)
        data = cache.get(ticker)
        if data is None:
            print(f"Ticker {ticker} not in cache")
            sys.exit(1)
        df = pd.DataFrame(data) if isinstance(data, dict) else data
        if not pd.api.types.is_datetime64_any_dtype(df['date']):
            df['date'] = pd.to_datetime(df['date'])

        # Detection benchmark
        t0 = time.time()
        lines = detect_algo_lines(df)
        t_detect = time.time() - t0

        print(f"\n{ticker}: {len(df)} bars")
        print(f"  Detection: {t_detect:.3f}s")
        print(f"  H- lines: {len(lines['hminus'])}")
        print(f"  L+ lines: {len(lines['lplus'])}")

        # Show top lines
        for dir_name, line_list in lines.items():
            if line_list:
                print(f"\n  Top {dir_name} lines:")
                for i, line in enumerate(line_list[:5]):
                    print(f"    #{i+1}: origin bar {line.origin_idx} "
                          f"@ ${line.origin_price:.2f}, "
                          f"slope={line.slope_per_bar:.4f}/bar, "
                          f"touches={line.touch_count} "
                          f"(hivol={line.hivol_touch_count}), "
                          f"broken={'yes @'+str(line.broken_idx) if line.broken_idx >= 0 else 'no'}")

        # Full series benchmark
        t0 = time.time()
        series = compute_all_algo_series(df)
        t_series = time.time() - t0

        print(f"\n  Series computation: {t_series:.3f}s")
        print(f"  {len(series)} expressions")

        # Show sample values at last bar
        last_bar = len(df) - 1
        print(f"\n  Last bar ({df['date'].iloc[-1].strftime('%Y-%m-%d')}):")
        for name, arr in sorted(series.items()):
            val = arr[last_bar]
            if not np.isnan(val):
                print(f"    {name}: {val:.4f}")

    elif len(sys.argv) > 1 and sys.argv[1] == "count":
        # Count total lines across a sample of tickers
        cache_path = "local_runner/cache/universe_ohlcv_5yr.pkl"
        if not os.path.exists(cache_path):
            cache_path = "local_runner/cache/universe_ohlcv.pkl"
        print(f"Loading cache...")
        with open(cache_path, "rb") as f:
            cache = pickle.load(f)

        sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        tickers = list(cache.keys())[:sample_size]

        total_h = 0
        total_l = 0
        total_time = 0

        for ticker in tickers:
            data = cache[ticker]
            df = pd.DataFrame(data) if isinstance(data, dict) else data
            if not pd.api.types.is_datetime64_any_dtype(df['date']):
                df['date'] = pd.to_datetime(df['date'])
            if len(df) < 60:
                continue

            t0 = time.time()
            lines = detect_algo_lines(df)
            elapsed = time.time() - t0
            total_time += elapsed

            nh = len(lines['hminus'])
            nl = len(lines['lplus'])
            total_h += nh
            total_l += nl
            print(f"  {ticker:6s}: H-={nh:3d}  L+={nl:3d}  ({elapsed:.2f}s)")

        print(f"\n  Sample of {len(tickers)} tickers:")
        print(f"  Total H-: {total_h}, Total L+: {total_l}")
        print(f"  Avg H-: {total_h/len(tickers):.1f}, Avg L+: {total_l/len(tickers):.1f}")
        print(f"  Total time: {total_time:.1f}s, Avg: {total_time/len(tickers):.2f}s/ticker")

    else:
        print("Usage:")
        print("  python scripts/algo_line_detector.py bench AAPL  # Benchmark one ticker")
        print("  python scripts/algo_line_detector.py count 20    # Count lines across sample")
        print(f"\nExpression names ({len(get_algo_expression_names())}):")
        for name in get_algo_expression_names():
            print(f"  {name}")
