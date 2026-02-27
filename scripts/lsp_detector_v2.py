"""
LSP Detector V2 — DataFrame-based, precomputed, cache-builder optimized.

Refactored from lsp_detector.py to:
1. Accept DataFrames (no API calls — all data from local caches)
2. Detect BOTH pivot highs AND pivot lows across daily/weekly/monthly
3. Precompute all pivots + break arrays ONCE per ticker
4. get_levels_at_bar() is O(n_pivots) with fast numpy ops — millions of calls feasible
5. Cluster pivots within 1 ATR into unified levels with full metadata

The grinder never calls this directly. The expr_cache_builder calls it once per ticker,
then feeds levels into the expression engine bar-by-bar to produce series that get
cached as flat columns alongside all other expressions.

Design constraints (from EXPRESSION_ENGINE_V2.md):
- Single computation path: all expressions via ExpressionEngine → compute_series()
- Precomputed in expr_cache_builder: grinders never compute LSP live
- No network calls: all data from local 5yr OHLCV cache
- Parallel via ProcessPoolExecutor: same worker pattern as current cache builder
- 100% example pass rule: expressions either pass all examples or get auto-excluded
- Grinders unchanged: just more columns in the expression cache
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


# ══════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════

@dataclass
class RawPivot:
    """A single detected pivot point (high or low)."""
    idx: int              # Index in the daily DataFrame
    price: float          # High for pivot highs, Low for pivot lows
    is_high: bool         # True = pivot high, False = pivot low
    max_window: int       # Largest window size that detected this pivot
    timeframe: str        # 'd', 'w', 'm' — which timeframe detected it
    volume_ratio: float   # Pivot bar volume vs 20-bar avg volume
    first_break_bars: np.ndarray = field(repr=False, default=None)
    # first_break_bars[i] = 1 if bar i broke this pivot, 0 otherwise
    # cumsum gives break count at any bar in O(1)


@dataclass
class Level:
    """A clustered level — multiple pivots at similar prices merged together."""
    center_price: float         # ATR-weighted center of the cluster
    pivot_count: int            # How many individual pivots in this cluster
    timeframe_count: int        # How many distinct timeframes (d/w/m) have a pivot here
    break_count: int            # Total breaks across all pivots at current bar
    max_window: int             # Largest pivot window among clustered pivots
    bars_back_nearest: int      # Bars back to the most recent pivot in cluster
    volume_ratio: float         # Highest volume ratio among clustered pivots
    distance: float             # Distance from close to center_price, normalized by ATR
    pivot_indices: list = field(repr=False, default_factory=list)  # Raw pivot indices


# ══════════════════════════════════════════════════════════════
# PIVOT DETECTION — Vectorized
# ══════════════════════════════════════════════════════════════

def _find_pivot_highs_vectorized(highs: np.ndarray, window: int) -> np.ndarray:
    """Find pivot high indices using vectorized rolling max.

    A bar is a pivot high if its high >= max of N bars on each side.
    Returns array of indices.
    """
    n = len(highs)
    if n < window * 2 + 1:
        return np.array([], dtype=np.int64)

    # Rolling max for left side (exclusive of center)
    # For each bar i, compute max(highs[i-window:i])
    # Use a simple approach: for each window size, track the running max
    pivot_mask = np.zeros(n, dtype=bool)

    for i in range(window, n):
        left_start = max(0, i - window)
        right_end = min(n, i + window + 1)

        left_max = highs[left_start:i].max()
        # Right side: allow partial window near end (don't require full right confirmation)
        if i + 1 < n:
            right_max = highs[i + 1:right_end].max()
        else:
            right_max = -np.inf

        if highs[i] >= left_max and highs[i] >= right_max:
            pivot_mask[i] = True

    return np.where(pivot_mask)[0]


def _find_pivot_lows_vectorized(lows: np.ndarray, window: int) -> np.ndarray:
    """Find pivot low indices — bar's low <= min of N bars on each side."""
    n = len(lows)
    if n < window * 2 + 1:
        return np.array([], dtype=np.int64)

    pivot_mask = np.zeros(n, dtype=bool)

    for i in range(window, n):
        left_start = max(0, i - window)
        right_end = min(n, i + window + 1)

        left_min = lows[left_start:i].min()
        if i + 1 < n:
            right_min = lows[i + 1:right_end].min()
        else:
            right_min = np.inf

        if lows[i] <= left_min and lows[i] <= right_min:
            pivot_mask[i] = True

    return np.where(pivot_mask)[0]


def _compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                 period: int = 14) -> np.ndarray:
    """SMA-based ATR matching TC2000 PCF semantics. Pure numpy."""
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

    # SMA of TR
    atr = np.full_like(tr, np.nan)
    if len(tr) >= period:
        cumsum = np.cumsum(tr)
        atr[period - 1] = cumsum[period - 1] / period
        atr[period:] = (cumsum[period:] - cumsum[:-period]) / period

    return atr


def _compute_volume_ratios(volumes: np.ndarray, period: int = 20) -> np.ndarray:
    """Volume ratio: bar volume / rolling 20-bar average. Pure numpy."""
    n = len(volumes)
    ratios = np.ones(n, dtype=np.float64)

    if n < period:
        avg = volumes.mean() if volumes.mean() > 0 else 1.0
        ratios = volumes / avg
        return ratios

    cumsum = np.cumsum(volumes.astype(np.float64))
    # For bars >= period: avg = (cumsum[i] - cumsum[i-period]) / period
    avg_vol = np.full(n, np.nan)
    avg_vol[period:] = (cumsum[period:] - cumsum[:-period]) / period
    # For bars < period: use expanding average
    for i in range(1, min(period, n)):
        avg_vol[i] = cumsum[i] / (i + 1)
    avg_vol[0] = volumes[0] if volumes[0] > 0 else 1.0

    valid = avg_vol > 0
    ratios[valid] = volumes[valid] / avg_vol[valid]
    ratios[~valid] = 1.0

    return ratios


# ══════════════════════════════════════════════════════════════
# BREAK COUNT PRECOMPUTATION
# ══════════════════════════════════════════════════════════════

def _precompute_break_array(n_bars: int, pivot_idx: int, pivot_price: float,
                            is_high: bool, highs: np.ndarray, lows: np.ndarray) -> np.ndarray:
    """Precompute a binary break array for one pivot.

    break_arr[i] = 1 if bar i broke this pivot level, 0 otherwise.
    For pivot highs: broken when a subsequent bar's high > pivot_price
    For pivot lows: broken when a subsequent bar's low < pivot_price

    To get break count at bar B: np.sum(break_arr[pivot_idx+1:B+1])
    Or use cumsum for O(1) lookup at any bar.
    """
    break_arr = np.zeros(n_bars, dtype=np.int32)

    if is_high:
        # Bars after pivot where high exceeded pivot price
        mask = np.zeros(n_bars, dtype=bool)
        mask[pivot_idx + 1:] = highs[pivot_idx + 1:] > pivot_price
        break_arr[mask] = 1
    else:
        # Bars after pivot where low went below pivot price
        mask = np.zeros(n_bars, dtype=bool)
        mask[pivot_idx + 1:] = lows[pivot_idx + 1:] < pivot_price
        break_arr[mask] = 1

    return break_arr


# ══════════════════════════════════════════════════════════════
# HTF → DAILY INDEX MAPPING
# ══════════════════════════════════════════════════════════════

def _map_htf_pivot_to_daily(htf_pivot_idx: int, htf_dates: np.ndarray,
                            daily_dates: np.ndarray) -> int:
    """Map a weekly/monthly pivot index back to the nearest daily bar index.

    Uses the HTF bar's date to find the closest daily bar on or before that date.
    """
    htf_date = htf_dates[htf_pivot_idx]

    # Find the daily bar on or before the HTF date
    mask = daily_dates <= htf_date
    if not mask.any():
        return 0
    return int(np.where(mask)[0][-1])


# ══════════════════════════════════════════════════════════════
# RESAMPLING
# ══════════════════════════════════════════════════════════════

def resample_ohlcv(daily_df: pd.DataFrame, freq: str = 'W') -> pd.DataFrame:
    """Resample daily OHLCV to weekly/monthly.

    Args:
        daily_df: DataFrame with date, open, high, low, close, volume columns
        freq: 'W' for weekly, 'ME' for monthly

    Returns:
        Resampled DataFrame with same column structure
    """
    df = daily_df.set_index('date')
    resampled = df.resample(freq).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    return resampled.reset_index()


# ══════════════════════════════════════════════════════════════
# LSP DETECTOR V2
# ══════════════════════════════════════════════════════════════

PIVOT_WINDOWS = [5, 10, 15, 20, 30, 40]


class LSPDetectorV2:
    """Multi-timeframe pivot detector with precomputed break tracking.

    Designed for cache builder integration:
    1. Initialize with DataFrames (no API calls)
    2. All pivots detected + break arrays computed in __init__
    3. get_levels_at_bar() is fast — just filtering + clustering precomputed data

    Usage:
        detector = LSPDetectorV2(daily_df, weekly_df, monthly_df)
        for bar_idx in range(50, n_bars):
            levels = detector.get_levels_at_bar(bar_idx, n_above=5, n_below=5)
            # levels['above'] = [Level, Level, ...]  nearest to furthest
            # levels['below'] = [Level, Level, ...]  nearest to furthest
    """

    def __init__(self, daily_df: pd.DataFrame,
                 weekly_df: Optional[pd.DataFrame] = None,
                 monthly_df: Optional[pd.DataFrame] = None):
        """Initialize detector and precompute ALL pivots + break arrays.

        This is the expensive step — runs once per ticker in the cache builder.
        """
        # Extract numpy arrays for speed
        self.n_bars = len(daily_df)
        self.highs = daily_df['high'].values.astype(np.float64)
        self.lows = daily_df['low'].values.astype(np.float64)
        self.closes = daily_df['close'].values.astype(np.float64)
        self.volumes = daily_df['volume'].values.astype(np.float64)
        self.dates = daily_df['date'].values  # datetime64

        # Precompute ATR and volume ratios (used for clustering + expressions)
        self.atr = _compute_atr(self.highs, self.lows, self.closes, period=14)
        self.vol_ratios = _compute_volume_ratios(self.volumes, period=20)

        # Detect all pivots across all timeframes
        self.pivots: list[RawPivot] = []
        self._detect_all_pivots(daily_df, weekly_df, monthly_df)

        # Precompute cumulative break counts for O(1) lookup
        self._precompute_all_breaks()

    def _detect_all_pivots(self, daily_df, weekly_df, monthly_df):
        """Detect pivots on all timeframes, map HTF pivots to daily indices."""

        # Daily pivots
        self._detect_pivots_on_timeframe(
            self.highs, self.lows, self.volumes, self.vol_ratios,
            timeframe='d', daily_indices=None
        )

        # Weekly pivots (mapped back to daily bar indices)
        if weekly_df is not None and len(weekly_df) >= 20:
            self._detect_htf_pivots(weekly_df, daily_df, timeframe='w')

        # Monthly pivots (mapped back to daily bar indices)
        if monthly_df is not None and len(monthly_df) >= 10:
            self._detect_htf_pivots(monthly_df, daily_df, timeframe='m')

    def _detect_pivots_on_timeframe(self, highs, lows, volumes, vol_ratios,
                                     timeframe, daily_indices=None):
        """Detect pivot highs and lows at multiple window sizes.

        If daily_indices is provided, maps detected indices through it.
        """
        # Track best (largest) window per pivot index
        high_pivots = {}   # idx -> max_window
        low_pivots = {}    # idx -> max_window

        for window in PIVOT_WINDOWS:
            # Pivot highs
            ph_indices = _find_pivot_highs_vectorized(highs, window)
            for idx in ph_indices:
                mapped_idx = daily_indices[idx] if daily_indices is not None else idx
                if mapped_idx not in high_pivots or window > high_pivots[mapped_idx]:
                    high_pivots[mapped_idx] = window

            # Pivot lows
            pl_indices = _find_pivot_lows_vectorized(lows, window)
            for idx in pl_indices:
                mapped_idx = daily_indices[idx] if daily_indices is not None else idx
                if mapped_idx not in low_pivots or window > low_pivots[mapped_idx]:
                    low_pivots[mapped_idx] = window

        # Create RawPivot objects
        for idx, max_window in high_pivots.items():
            if 0 <= idx < self.n_bars:
                vr = self.vol_ratios[idx] if idx < len(self.vol_ratios) else 1.0
                self.pivots.append(RawPivot(
                    idx=idx,
                    price=float(self.highs[idx]),
                    is_high=True,
                    max_window=max_window,
                    timeframe=timeframe,
                    volume_ratio=float(vr),
                ))

        for idx, max_window in low_pivots.items():
            if 0 <= idx < self.n_bars:
                vr = self.vol_ratios[idx] if idx < len(self.vol_ratios) else 1.0
                self.pivots.append(RawPivot(
                    idx=idx,
                    price=float(self.lows[idx]),
                    is_high=False,
                    max_window=max_window,
                    timeframe=timeframe,
                    volume_ratio=float(vr),
                ))

    def _detect_htf_pivots(self, htf_df: pd.DataFrame, daily_df: pd.DataFrame,
                           timeframe: str):
        """Detect pivots on a higher timeframe and map back to daily indices."""
        htf_highs = htf_df['high'].values.astype(np.float64)
        htf_lows = htf_df['low'].values.astype(np.float64)
        htf_volumes = htf_df['volume'].values.astype(np.float64)
        htf_vol_ratios = _compute_volume_ratios(htf_volumes, period=20)
        htf_dates = htf_df['date'].values  # datetime64
        daily_dates = daily_df['date'].values

        # Build HTF index → daily index mapping
        # For each HTF bar, find the last daily bar on or before the HTF date
        daily_indices = np.zeros(len(htf_df), dtype=np.int64)
        for i, htf_date in enumerate(htf_dates):
            mask = daily_dates <= htf_date
            if mask.any():
                daily_indices[i] = int(np.where(mask)[0][-1])
            else:
                daily_indices[i] = 0

        self._detect_pivots_on_timeframe(
            htf_highs, htf_lows, htf_volumes, htf_vol_ratios,
            timeframe=timeframe, daily_indices=daily_indices
        )

    def _precompute_all_breaks(self):
        """Precompute cumulative break count arrays for every pivot.

        After this, getting break count at any bar is:
            break_count = self._break_cumsums[pivot_index][bar_idx]
        """
        self._break_cumsums = []  # parallel to self.pivots

        for pivot in self.pivots:
            break_arr = _precompute_break_array(
                self.n_bars, pivot.idx, pivot.price,
                pivot.is_high, self.highs, self.lows
            )
            # Cumulative sum for O(1) range queries
            cumsum = np.cumsum(break_arr)
            self._break_cumsums.append(cumsum)
            pivot.first_break_bars = break_arr  # keep for reference

    def get_break_count(self, pivot_index: int, bar_idx: int) -> int:
        """Get break count for a pivot as of bar_idx. O(1)."""
        if bar_idx < 0 or bar_idx >= self.n_bars:
            return 0
        return int(self._break_cumsums[pivot_index][bar_idx])

    def get_levels_at_bar(self, bar_idx: int, n_above: int = 5,
                          n_below: int = 5) -> dict:
        """Get proximity-ordered clustered levels as of a specific bar.

        Fast — uses precomputed pivot table, just clusters and sorts.

        Args:
            bar_idx: Current bar index in the daily DataFrame
            n_above: Number of levels above current close to return
            n_below: Number of levels below current close to return

        Returns:
            dict with 'above': [Level, ...] and 'below': [Level, ...]
            Sorted by proximity (nearest first).
        """
        if bar_idx < 14 or bar_idx >= self.n_bars:
            return {'above': [], 'below': []}

        current_close = self.closes[bar_idx]
        current_atr = self.atr[bar_idx]

        if np.isnan(current_atr) or current_atr <= 0:
            return {'above': [], 'below': []}

        # Filter pivots: only those BEFORE bar_idx
        # Collect pivot data for clustering
        above_pivots = []  # (price, pivot_list_index)
        below_pivots = []

        for pi, pivot in enumerate(self.pivots):
            if pivot.idx >= bar_idx:
                continue  # Pivot hasn't happened yet at this bar

            if pivot.price > current_close:
                above_pivots.append((pivot.price, pi))
            else:
                below_pivots.append((pivot.price, pi))

        # Cluster and return
        above_levels = self._cluster_pivots(
            above_pivots, bar_idx, current_close, current_atr, ascending=True
        )
        below_levels = self._cluster_pivots(
            below_pivots, bar_idx, current_close, current_atr, ascending=False
        )

        return {
            'above': above_levels[:n_above],
            'below': below_levels[:n_below],
        }

    def _cluster_pivots(self, pivot_tuples: list, bar_idx: int,
                        current_close: float, current_atr: float,
                        ascending: bool) -> list[Level]:
        """Cluster pivots within 1 ATR of each other into levels.

        Args:
            pivot_tuples: [(price, pivot_list_index), ...]
            bar_idx: Current bar
            current_close: Close price at bar_idx
            current_atr: ATR at bar_idx
            ascending: True for above (sort nearest-first = ascending distance),
                       False for below (sort nearest-first = descending price)

        Returns:
            List of Level objects, sorted by proximity to current_close (nearest first).
        """
        if not pivot_tuples:
            return []

        # Sort by price
        pivot_tuples.sort(key=lambda x: x[0])

        # Cluster: walk through sorted prices, merge within 1 ATR
        clusters = []  # each cluster: list of (price, pivot_list_index)
        current_cluster = [pivot_tuples[0]]
        cluster_max_price = pivot_tuples[0][0]

        for i in range(1, len(pivot_tuples)):
            price, pi = pivot_tuples[i]
            # If within 1 ATR of the cluster's highest price, merge
            if price - cluster_max_price <= current_atr:
                current_cluster.append((price, pi))
                cluster_max_price = max(cluster_max_price, price)
            else:
                clusters.append(current_cluster)
                current_cluster = [(price, pi)]
                cluster_max_price = price

        clusters.append(current_cluster)

        # Convert clusters to Level objects
        levels = []
        for cluster in clusters:
            prices = [p for p, _ in cluster]
            pis = [pi for _, pi in cluster]

            center_price = float(np.mean(prices))

            # Metadata aggregation
            timeframes_seen = set()
            total_breaks = 0
            max_window = 0
            min_bars_back = self.n_bars  # will find minimum
            max_vol_ratio = 0.0

            for pi in pis:
                pivot = self.pivots[pi]
                timeframes_seen.add(pivot.timeframe)
                total_breaks += self.get_break_count(pi, bar_idx)
                max_window = max(max_window, pivot.max_window)
                bars_back = bar_idx - pivot.idx
                min_bars_back = min(min_bars_back, bars_back)
                max_vol_ratio = max(max_vol_ratio, pivot.volume_ratio)

            distance = (center_price - current_close) / current_atr

            levels.append(Level(
                center_price=center_price,
                pivot_count=len(cluster),
                timeframe_count=len(timeframes_seen),
                break_count=total_breaks,
                max_window=max_window,
                bars_back_nearest=min_bars_back,
                volume_ratio=max_vol_ratio,
                distance=distance,
                pivot_indices=pis,
            ))

        # Sort by proximity (absolute distance, nearest first)
        levels.sort(key=lambda lv: abs(lv.distance))

        return levels

    def compute_all_series(self, cum_tpv: np.ndarray = None,
                           cum_v: np.ndarray = None,
                           n_above: int = 5, n_below: int = 5) -> dict:
        """Compute ALL level + contextual AVWAP series in a SINGLE pass.

        This avoids the redundant get_levels_at_bar() call that happens when
        compute_level_series() and compute_ctx_avwap_series() are called separately.
        ~2x faster than separate computation.

        Args:
            cum_tpv: Precomputed cumulative TP*V (optional, for ctx AVWAP)
            cum_v: Precomputed cumulative V (optional, for ctx AVWAP)
            n_above: Number of levels above to track
            n_below: Number of levels below to track

        Returns:
            dict mapping expression name to float64 array of length n_bars
        """
        n_bars = self.n_bars
        compute_avwap = cum_tpv is not None and cum_v is not None
        directions = ['above', 'below']
        metrics = [
            'distance', 'pivot_count', 'timeframe_count', 'break_count',
            'max_window', 'bars_back_nearest', 'volume_ratio'
        ]

        # Pre-allocate all output arrays
        series = {}
        for d in directions:
            n_ranks = n_above if d == 'above' else n_below
            for rank in range(1, n_ranks + 1):
                for metric in metrics:
                    name = f"level_{d}{rank}_{metric}"
                    series[name] = np.full(n_bars, np.nan, dtype=np.float64)
                if compute_avwap:
                    name = f"level_{d}{rank}_ctx_avwap_distance"
                    series[name] = np.full(n_bars, np.nan, dtype=np.float64)

        # Single pass: compute bar by bar
        for bar_idx in range(50, n_bars):
            levels = self.get_levels_at_bar(bar_idx, n_above=n_above, n_below=n_below)

            for d in directions:
                level_list = levels[d]
                for rank_idx, level in enumerate(level_list):
                    rank = rank_idx + 1
                    prefix = f"level_{d}{rank}_"
                    series[prefix + "distance"][bar_idx] = level.distance
                    series[prefix + "pivot_count"][bar_idx] = level.pivot_count
                    series[prefix + "timeframe_count"][bar_idx] = level.timeframe_count
                    series[prefix + "break_count"][bar_idx] = level.break_count
                    series[prefix + "max_window"][bar_idx] = level.max_window
                    series[prefix + "bars_back_nearest"][bar_idx] = level.bars_back_nearest
                    series[prefix + "volume_ratio"][bar_idx] = level.volume_ratio

                    if compute_avwap:
                        avwap_val = compute_contextual_avwap_for_level(
                            level, self, cum_tpv, cum_v, bar_idx
                        )
                        series[prefix + "ctx_avwap_distance"][bar_idx] = avwap_val

        return series


# ══════════════════════════════════════════════════════════════
# CONTEXTUAL AVWAP
# ══════════════════════════════════════════════════════════════

def precompute_avwap_arrays(daily_df: pd.DataFrame) -> tuple:
    """Precompute cumulative TP×V and V arrays for O(1) AVWAP computation.

    AVWAP from anchor to bar = (cum_tpv[bar] - cum_tpv[anchor-1]) / (cum_v[bar] - cum_v[anchor-1])

    Returns:
        (cum_tpv, cum_v) — both float64 arrays of length n_bars
    """
    highs = daily_df['high'].values.astype(np.float64)
    lows = daily_df['low'].values.astype(np.float64)
    closes = daily_df['close'].values.astype(np.float64)
    volumes = daily_df['volume'].values.astype(np.float64)

    tp = (highs + lows + closes) / 3.0
    tpv = tp * volumes

    cum_tpv = np.cumsum(tpv)
    cum_v = np.cumsum(volumes)

    return cum_tpv, cum_v


def avwap_from_anchor(cum_tpv: np.ndarray, cum_v: np.ndarray,
                      anchor: int, bar: int) -> float:
    """Compute AVWAP from anchor bar to target bar. O(1).

    Args:
        cum_tpv: Cumulative TP*V array
        cum_v: Cumulative volume array
        anchor: Anchor bar index (inclusive)
        bar: Target bar index (inclusive)

    Returns:
        AVWAP value, or NaN if invalid
    """
    if anchor < 0 or bar < anchor or bar >= len(cum_tpv):
        return np.nan

    if anchor == 0:
        total_tpv = cum_tpv[bar]
        total_v = cum_v[bar]
    else:
        total_tpv = cum_tpv[bar] - cum_tpv[anchor - 1]
        total_v = cum_v[bar] - cum_v[anchor - 1]

    if total_v <= 0:
        return np.nan

    return total_tpv / total_v


def compute_contextual_avwap_for_level(
    level: Level, detector: 'LSPDetectorV2',
    cum_tpv: np.ndarray, cum_v: np.ndarray,
    bar_idx: int, search_range: int = 25
) -> float:
    """Compute contextual AVWAP distance for a level at a specific bar.

    Searches ~25 bars before the level's nearest pivot for the anchor that
    produces the highest AVWAP at bar_idx. Returns distance from close to
    that AVWAP, normalized by ATR.

    Vectorized: computes all anchor AVWAPs at once using numpy slicing.
    """
    if not level.pivot_indices:
        return np.nan

    # Find the most prominent pivot in the cluster (largest window)
    best_pi = max(level.pivot_indices,
                  key=lambda pi: detector.pivots[pi].max_window)
    pivot_idx = detector.pivots[best_pi].idx

    # Search range: bars before the pivot
    search_start = max(0, pivot_idx - search_range)
    search_end = pivot_idx  # exclusive

    if search_start >= search_end or bar_idx >= len(cum_tpv):
        return np.nan

    # Vectorized: compute AVWAP for all anchors at once
    # AVWAP(anchor, bar) = (cum_tpv[bar] - cum_tpv[anchor-1]) / (cum_v[bar] - cum_v[anchor-1])
    # For anchor=0: AVWAP = cum_tpv[bar] / cum_v[bar]
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

    # Compute AVWAPs, mask out zero-volume
    valid = total_v > 0
    if not valid.any():
        return np.nan

    avwaps = np.full(len(anchors), -np.inf)
    avwaps[valid] = total_tpv[valid] / total_v[valid]

    best_avwap = avwaps.max()
    if best_avwap == -np.inf:
        return np.nan

    current_close = detector.closes[bar_idx]
    current_atr = detector.atr[bar_idx]

    if np.isnan(current_atr) or current_atr <= 0:
        return np.nan

    return (best_avwap - current_close) / current_atr


def compute_ctx_avwap_series(detector: 'LSPDetectorV2',
                             cum_tpv: np.ndarray, cum_v: np.ndarray,
                             n_above: int = 5, n_below: int = 5) -> dict:
    """Compute contextual AVWAP distance series for all levels.

    Returns dict of expression_name → np.ndarray(n_bars).

    Expression naming: level_{dir}{rank}_ctx_avwap_distance
    """
    n_bars = detector.n_bars
    series = {}

    for d in ['above', 'below']:
        n_ranks = n_above if d == 'above' else n_below
        for rank in range(1, n_ranks + 1):
            name = f"level_{d}{rank}_ctx_avwap_distance"
            series[name] = np.full(n_bars, np.nan, dtype=np.float64)

    for bar_idx in range(50, n_bars):
        levels = detector.get_levels_at_bar(bar_idx, n_above=n_above, n_below=n_below)

        for d in ['above', 'below']:
            level_list = levels[d]
            for rank_idx, level in enumerate(level_list):
                rank = rank_idx + 1
                name = f"level_{d}{rank}_ctx_avwap_distance"
                val = compute_contextual_avwap_for_level(
                    level, detector, cum_tpv, cum_v, bar_idx
                )
                series[name][bar_idx] = val

    return series


# ══════════════════════════════════════════════════════════════
# COMBINED COMPUTATION — Single entry point for cache builder
# ══════════════════════════════════════════════════════════════

def compute_all_lsp_series(daily_df: pd.DataFrame,
                           weekly_df: pd.DataFrame = None,
                           monthly_df: pd.DataFrame = None,
                           n_above: int = 5, n_below: int = 5) -> dict:
    """Compute ALL LSP + contextual AVWAP expression series for one ticker.

    This is the main entry point called by expr_cache_builder.py.
    Returns a dict of expression_name → np.ndarray(n_bars, dtype=float64).

    Total expressions: 7 metrics × 5 ranks × 2 directions = 70 LSP expressions
                     + 1 ctx_avwap × 5 ranks × 2 directions = 10 AVWAP expressions
                     = 80 expressions total

    Args:
        daily_df: Daily OHLCV DataFrame (must have date, open, high, low, close, volume)
        weekly_df: Weekly resampled OHLCV (optional, computed if None)
        monthly_df: Monthly resampled OHLCV (optional, computed if None)
        n_above: Number of levels above to track
        n_below: Number of levels below to track

    Returns:
        dict mapping expression name to float64 array of length n_bars
    """
    # Resample if not provided
    if weekly_df is None and len(daily_df) >= 20:
        weekly_df = resample_ohlcv(daily_df, 'W')
    if monthly_df is None and len(daily_df) >= 60:
        monthly_df = resample_ohlcv(daily_df, 'ME')

    # Detect all pivots + precompute breaks
    detector = LSPDetectorV2(daily_df, weekly_df, monthly_df)

    # Precompute AVWAP lookup arrays
    cum_tpv, cum_v = precompute_avwap_arrays(daily_df)

    # Single-pass: level metrics + contextual AVWAP together
    return detector.compute_all_series(cum_tpv, cum_v, n_above=n_above, n_below=n_below)


def get_lsp_expression_names(n_above: int = 5, n_below: int = 5) -> list[str]:
    """Get the ordered list of LSP expression names.

    Used by brute_expressions.py and cache builder to know the column order.
    """
    names = []
    metrics = [
        'distance', 'pivot_count', 'timeframe_count', 'break_count',
        'max_window', 'bars_back_nearest', 'volume_ratio'
    ]

    for d in ['above', 'below']:
        n_ranks = n_above if d == 'above' else n_below
        for rank in range(1, n_ranks + 1):
            for metric in metrics:
                names.append(f"level_{d}{rank}_{metric}")

    # Contextual AVWAP
    for d in ['above', 'below']:
        n_ranks = n_above if d == 'above' else n_below
        for rank in range(1, n_ranks + 1):
            names.append(f"level_{d}{rank}_ctx_avwap_distance")

    return names


# ══════════════════════════════════════════════════════════════
# VALIDATION — Compare V2 output against labeled DTSS LSP data
# ══════════════════════════════════════════════════════════════

def validate_against_labeled(daily_cache: dict, labeled_path: str = "data/dtss_lsp_data.json"):
    """Validate V2 detector against labeled DTSS examples.

    The hand-labeled LSP should appear as the nearest level above (above1)
    with break_count=0 at the signal bar.

    Args:
        daily_cache: dict of ticker → DataFrame (from 5yr OHLCV cache)
        labeled_path: path to labeled LSP JSON
    """
    import json

    with open(labeled_path) as f:
        labeled = json.load(f)

    print(f"Validating {len(labeled)} labeled LSPs against V2 detector")
    print("=" * 90)

    exact = 0
    close = 0
    missed = 0

    for item in labeled:
        ticker = item['ticker']
        entry_date = item['entry_date']
        lsp_price = item['price']

        df = daily_cache.get(ticker)
        if df is None or df.empty:
            print(f"  {ticker}: No data in cache")
            missed += 1
            continue

        # Ensure date column is datetime
        if not pd.api.types.is_datetime64_any_dtype(df['date']):
            df = df.copy()
            df['date'] = pd.to_datetime(df['date'])

        # Find scan bar (day before entry)
        entry_dt = pd.Timestamp(entry_date)
        mask = df['date'] < entry_dt
        if not mask.any():
            print(f"  {ticker}: No bars before entry date")
            missed += 1
            continue
        scan_idx = df.loc[mask].index[-1]

        # Run detector
        weekly_df = resample_ohlcv(df, 'W')
        monthly_df = resample_ohlcv(df, 'ME')
        detector = LSPDetectorV2(df, weekly_df, monthly_df)

        levels = detector.get_levels_at_bar(scan_idx, n_above=5, n_below=5)
        above_levels = levels['above']

        if not above_levels:
            print(f"  {ticker} ({entry_date}): ❌ No levels above detected")
            missed += 1
            continue

        # Check if labeled LSP matches above1
        top = above_levels[0]
        price_diff_pct = abs(top.center_price - lsp_price) / lsp_price * 100

        if price_diff_pct < 3.0:
            print(f"  {ticker} ({entry_date}): ✅ above1 @ ${top.center_price:.2f} "
                  f"(labeled ${lsp_price:.2f}, {price_diff_pct:.1f}% off, "
                  f"pivots={top.pivot_count}, breaks={top.break_count})")
            exact += 1
        else:
            # Check other ranks
            found_rank = None
            for ri, lv in enumerate(above_levels[1:], 2):
                pd_pct = abs(lv.center_price - lsp_price) / lsp_price * 100
                if pd_pct < 3.0:
                    found_rank = ri
                    break

            if found_rank:
                print(f"  {ticker} ({entry_date}): ⚠️  Found at above{found_rank} "
                      f"(above1 @ ${top.center_price:.2f}, labeled ${lsp_price:.2f})")
                close += 1
            else:
                print(f"  {ticker} ({entry_date}): ❌ Not found in top 5 "
                      f"(above1 @ ${top.center_price:.2f}, labeled ${lsp_price:.2f}, "
                      f"{price_diff_pct:.1f}% off)")
                missed += 1

    total = len(labeled)
    print(f"\n{'=' * 90}")
    print(f"V2 VALIDATION: {total} labeled LSPs")
    print(f"  ✅ above1 match (<3%):  {exact}/{total} ({exact/total*100:.0f}%)")
    print(f"  ⚠️  Found in top 5:     {close}/{total} ({close/total*100:.0f}%)")
    print(f"  ❌ Missed:              {missed}/{total} ({missed/total*100:.0f}%)")


# ══════════════════════════════════════════════════════════════
# CLI — For testing and validation
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    import os
    import pickle

    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        # Load 5yr cache
        cache_path = "local_runner/cache/universe_ohlcv_5yr.pkl"
        if not os.path.exists(cache_path):
            cache_path = "local_runner/cache/universe_ohlcv.pkl"
        print(f"Loading OHLCV cache from {cache_path}...")
        with open(cache_path, "rb") as f:
            cache = pickle.load(f)
        # Convert cache entries to DataFrames if needed
        daily_cache = {}
        for ticker, data in cache.items():
            if isinstance(data, pd.DataFrame):
                daily_cache[ticker] = data
            elif isinstance(data, dict):
                daily_cache[ticker] = pd.DataFrame(data)
        validate_against_labeled(daily_cache)

    elif len(sys.argv) > 1 and sys.argv[1] == "bench":
        # Benchmark: compute LSP series for one ticker
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

        import time
        t0 = time.time()
        series = compute_all_lsp_series(df)
        elapsed = time.time() - t0
        print(f"\n{ticker}: {len(df)} bars, {len(series)} expressions, {elapsed:.2f}s")

        # Show sample values at last bar
        last_bar = len(df) - 1
        print(f"\nLast bar ({df['date'].iloc[-1].strftime('%Y-%m-%d')}):")
        for name, arr in sorted(series.items()):
            val = arr[last_bar]
            if not np.isnan(val):
                print(f"  {name}: {val:.4f}")

    else:
        print("Usage:")
        print("  python scripts/lsp_detector_v2.py validate    # Validate against labeled DTSS data")
        print("  python scripts/lsp_detector_v2.py bench AAPL  # Benchmark one ticker")
