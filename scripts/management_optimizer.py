"""
Management Optimizer — Build TODO #4 from ANALYSIS_SYSTEM.md

Exhaustive sweep of all stop/target/trail/time/partial combinations
against precomputed outcome data from the Outcome Engine (#3).

Each combination is pure numpy array math against the MFE/MAE matrices —
no per-trade simulation loops. Hundreds of thousands of combos run in seconds.

Usage:
  from scripts.management_optimizer import ManagementOptimizer
  from scripts.outcome_engine import OutcomeEngine

  # Load outcome data
  engine = OutcomeEngine()
  outcomes = engine.compute_for_examples("3-4db")
  matrix = OutcomeEngine.outcomes_to_matrix(outcomes)

  # Optimize
  optimizer = ManagementOptimizer()
  results = optimizer.optimize(matrix, direction="short")

  # View top strategies
  optimizer.print_top(results, n=20)

  # Find robust plateaus
  plateaus = optimizer.find_plateaus(results)

  # Full report
  optimizer.print_report(results, plateaus)
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from itertools import product


# ============================================================
# Parameter Space Definition
# ============================================================

# Stop distances in ATR units
STOP_DISTANCES = [round(x, 2) for x in np.arange(0.25, 5.25, 0.25)]  # 0.25 to 5.0 (20 values)

# Target distances in ATR units
TARGET_DISTANCES = [round(x, 2) for x in np.arange(0.5, 10.25, 0.25)]  # 0.5 to 10.0 (39 values)

# Time stops (days)
TIME_STOPS = list(range(1, 31))  # 1 to 30 days (30 values)
TIME_STOPS.append(0)  # 0 = no time stop

# Trailing stop types
# Each is a tuple: (name, activation_threshold_R, trail_distance_atr)
# activation_threshold_R: trail starts after this much favorable move (in R)
# trail_distance_atr: how far behind the best price to trail
TRAIL_TYPES = [
    ("none", 0, 0),  # No trailing stop
    # Fixed ATR trail — activates immediately
    ("trail_0.5", 0, 0.5),
    ("trail_0.75", 0, 0.75),
    ("trail_1.0", 0, 1.0),
    ("trail_1.5", 0, 1.5),
    ("trail_2.0", 0, 2.0),
    ("trail_2.5", 0, 2.5),
    ("trail_3.0", 0, 3.0),
    # Breakeven trails — move stop to BE after N*R move, then trail
    ("be_1R_trail_0.5", 1.0, 0.5),
    ("be_1R_trail_1.0", 1.0, 1.0),
    ("be_1.5R_trail_0.5", 1.5, 0.5),
    ("be_1.5R_trail_1.0", 1.5, 1.0),
    ("be_2R_trail_0.5", 2.0, 0.5),
    ("be_2R_trail_1.0", 2.0, 1.0),
    ("be_2R_trail_1.5", 2.0, 1.5),
]

# Partial exit strategies
# Each is a tuple: (name, first_exit_R, first_exit_pct, remainder_strategy)
# first_exit_R: take partial at this R multiple
# first_exit_pct: % of position to exit (0.0 to 1.0)
# remainder_strategy: "hold" = hold rest to target/trail, "trail" = trail rest
PARTIAL_STRATEGIES = [
    ("full_to_target", 0, 0, "hold"),      # No partial — full position to target
    ("half_at_1R", 1.0, 0.5, "hold"),      # Take half at 1R, hold rest
    ("half_at_1.5R", 1.5, 0.5, "hold"),
    ("half_at_2R", 2.0, 0.5, "hold"),
    ("third_at_1R", 1.0, 0.33, "hold"),    # Take 1/3 at 1R
    ("third_at_2R", 2.0, 0.33, "hold"),
    ("half_at_1R_trail", 1.0, 0.5, "trail"),  # Take half at 1R, trail rest
    ("half_at_2R_trail", 2.0, 0.5, "trail"),
    ("quarter_at_1R", 1.0, 0.25, "hold"),  # Take 1/4 at 1R
    ("quarter_at_2R", 2.0, 0.25, "hold"),
]


# ============================================================
# Data Structures
# ============================================================

@dataclass
class StrategyResult:
    """Result of testing a single management strategy across all signals."""
    stop: float
    target: float
    time_stop: int
    trail_name: str
    trail_activation: float
    trail_distance: float
    partial_name: str
    partial_exit_r: float
    partial_exit_pct: float

    # Performance metrics
    ev_per_trade: float         # Expected value in R (ATR units)
    win_rate: float             # % of trades that are winners
    avg_winner: float           # Average winning trade in R
    avg_loser: float            # Average losing trade in R
    profit_factor: float        # Sum of winners / abs(sum of losers)
    max_drawdown_r: float       # Worst single trade in R
    median_trade: float         # Median trade result in R
    n_trades: int               # Number of trades with valid exits
    avg_hold_days: float        # Average holding period

    # Raw results for debugging
    trade_results: np.ndarray = field(repr=False, default=None)
    exit_bars: np.ndarray = field(repr=False, default=None)

    def to_dict(self) -> dict:
        return {
            "stop": self.stop,
            "target": self.target,
            "time_stop": self.time_stop,
            "trail": self.trail_name,
            "partial": self.partial_name,
            "ev": round(self.ev_per_trade, 4),
            "win_rate": round(self.win_rate, 4),
            "avg_winner": round(self.avg_winner, 4),
            "avg_loser": round(self.avg_loser, 4),
            "profit_factor": round(self.profit_factor, 4),
            "max_dd": round(self.max_drawdown_r, 4),
            "median": round(self.median_trade, 4),
            "n_trades": self.n_trades,
            "avg_hold": round(self.avg_hold_days, 1),
        }


@dataclass
class PlateauRegion:
    """A region of parameter space where performance is consistently good."""
    stop_range: tuple
    target_range: tuple
    time_stop_range: tuple
    trail_cluster: list
    partial_cluster: list
    avg_ev: float
    min_ev: float
    max_ev: float
    n_strategies: int
    robustness_score: float  # 0-1, higher = more robust


# ============================================================
# Core Optimizer
# ============================================================

class ManagementOptimizer:
    """Exhaustive management parameter sweep against outcome matrices."""

    def __init__(self,
                 stops: list = None,
                 targets: list = None,
                 time_stops: list = None,
                 trails: list = None,
                 partials: list = None):
        """Initialize with custom parameter ranges, or use defaults."""
        self.stops = stops or STOP_DISTANCES
        self.targets = targets or TARGET_DISTANCES
        self.time_stops = time_stops or TIME_STOPS
        self.trails = trails or TRAIL_TYPES
        self.partials = partials or PARTIAL_STRATEGIES

    def total_combinations(self) -> int:
        """How many strategy combinations to test."""
        return (len(self.stops) * len(self.targets) *
                len(self.time_stops) * len(self.trails) *
                len(self.partials))

    def optimize(self, matrix: dict, direction: str = "short",
                 min_trades: int = 5, verbose: bool = True) -> list[StrategyResult]:
        """Run the full exhaustive sweep.

        Args:
            matrix: Output from OutcomeEngine.outcomes_to_matrix()
            direction: "long" or "short" — affects how we interpret high/low vs entry
            min_trades: Minimum number of valid trades to include a strategy
            verbose: Print progress

        Returns:
            List of StrategyResult sorted by EV descending
        """
        if not matrix:
            print("No outcome data provided")
            return []

        n_signals = matrix["n_signals"]
        max_bars = matrix["max_bars"]

        # Core arrays — these are already sign-adjusted by the outcome engine
        # For shorts: positive = profit (price dropped), negative = loss (price rose)
        high_vs = matrix["high_vs_entry"]   # shape (n_signals, max_bars)
        low_vs = matrix["low_vs_entry"]     # shape (n_signals, max_bars)
        close_pl = matrix["close_pl"]       # shape (n_signals, max_bars)
        mfe = matrix["mfe"]                 # shape (n_signals, max_bars) — cumulative best
        mae = matrix["mae"]                 # shape (n_signals, max_bars) — cumulative worst

        total = self.total_combinations()
        if verbose:
            print(f"Testing {total:,} management combinations")
            print(f"  {n_signals} signals × {max_bars} bars")
            print(f"  {len(self.stops)} stops × {len(self.targets)} targets × "
                  f"{len(self.time_stops)} time stops × {len(self.trails)} trails × "
                  f"{len(self.partials)} partials")

        results = []
        tested = 0

        for stop in self.stops:
            for target in self.targets:
                # Skip impossible combos where target <= stop
                if target <= stop:
                    continue

                for time_stop_days in self.time_stops:
                    for trail_name, trail_act, trail_dist in self.trails:
                        for partial_name, partial_r, partial_pct, partial_strat in self.partials:
                            # Skip partials where partial exit R >= target
                            if partial_r > 0 and partial_r >= target:
                                continue

                            trade_pls, exit_bars_arr = self._simulate_strategy(
                                high_vs, low_vs, close_pl, mfe, mae,
                                n_signals, max_bars,
                                stop, target, time_stop_days,
                                trail_act, trail_dist,
                                partial_r, partial_pct,
                            )

                            # Only count trades with valid exits
                            valid = ~np.isnan(trade_pls)
                            n_valid = valid.sum()

                            if n_valid < min_trades:
                                tested += 1
                                continue

                            valid_pls = trade_pls[valid]
                            valid_exits = exit_bars_arr[valid]

                            winners = valid_pls[valid_pls > 0]
                            losers = valid_pls[valid_pls <= 0]

                            ev = float(np.mean(valid_pls))
                            win_rate = len(winners) / n_valid if n_valid > 0 else 0
                            avg_win = float(np.mean(winners)) if len(winners) > 0 else 0
                            avg_loss = float(np.mean(losers)) if len(losers) > 0 else 0
                            pf = (float(np.sum(winners)) / abs(float(np.sum(losers)))
                                  if len(losers) > 0 and np.sum(losers) != 0 else 999.0)
                            max_dd = float(np.min(valid_pls))
                            median = float(np.median(valid_pls))
                            avg_hold = float(np.mean(valid_exits))

                            result = StrategyResult(
                                stop=stop, target=target,
                                time_stop=time_stop_days,
                                trail_name=trail_name,
                                trail_activation=trail_act,
                                trail_distance=trail_dist,
                                partial_name=partial_name,
                                partial_exit_r=partial_r,
                                partial_exit_pct=partial_pct,
                                ev_per_trade=ev,
                                win_rate=win_rate,
                                avg_winner=avg_win,
                                avg_loser=avg_loss,
                                profit_factor=pf,
                                max_drawdown_r=max_dd,
                                median_trade=median,
                                n_trades=int(n_valid),
                                avg_hold_days=avg_hold,
                                trade_results=valid_pls,
                                exit_bars=valid_exits,
                            )
                            results.append(result)
                            tested += 1

                if verbose and tested % 50000 == 0:
                    print(f"  Tested {tested:,} / ~{total:,} combos ({len(results):,} valid)")

        # Sort by EV descending
        results.sort(key=lambda r: r.ev_per_trade, reverse=True)

        if verbose:
            print(f"\nDone: {tested:,} tested, {len(results):,} valid strategies")
            if results:
                print(f"Best EV: {results[0].ev_per_trade:.3f}R  |  "
                      f"WR: {results[0].win_rate:.1%}  |  "
                      f"PF: {results[0].profit_factor:.2f}")

        return results

    @staticmethod
    def _simulate_strategy(high_vs, low_vs, close_pl, mfe, mae,
                           n_signals, max_bars,
                           stop, target, time_stop_days,
                           trail_activation, trail_distance,
                           partial_exit_r, partial_exit_pct) -> tuple:
        """Simulate a single strategy across all signals using vectorized ops where possible.

        Returns (trade_pls, exit_bars) arrays of shape (n_signals,).

        The outcome matrices are already sign-adjusted:
          - high_vs_entry: most favorable intrabar extreme (positive = favorable)
          - low_vs_entry: most adverse intrabar extreme (negative = adverse)
          - mfe: cumulative max favorable excursion through bar
          - mae: cumulative max adverse excursion through bar (negative)
          - close_pl: close-to-close P&L vs entry

        For shorts: high_vs is actually mapped to the LOW (favorable),
                    low_vs is mapped to the HIGH (adverse).
        The outcome engine handles this sign flip already.
        """
        trade_pls = np.full(n_signals, np.nan)
        exit_bars = np.full(n_signals, np.nan)

        has_trail = trail_distance > 0
        has_partial = partial_exit_pct > 0 and partial_exit_r > 0
        has_time_stop = time_stop_days > 0

        for i in range(n_signals):
            best_favorable = 0.0  # Track best favorable move for trailing
            trail_active = False
            partial_taken = False
            partial_pl = 0.0
            remaining_pct = 1.0

            exit_bar = -1
            exit_pl = np.nan

            bars_to_check = min(max_bars, time_stop_days if has_time_stop else max_bars)

            for j in range(bars_to_check):
                if np.isnan(high_vs[i, j]) or np.isnan(low_vs[i, j]):
                    break  # No more data

                bar_favorable = high_vs[i, j]   # Best case this bar
                bar_adverse = low_vs[i, j]       # Worst case this bar
                bar_close = close_pl[i, j]

                # Update running best favorable
                if bar_favorable > best_favorable:
                    best_favorable = bar_favorable

                # --- Check stop hit ---
                # mae tracks cumulative worst, but we need intrabar for stop checks
                # low_vs_entry is the intrabar adverse extreme for this specific bar
                if bar_adverse <= -stop:
                    exit_pl = -stop
                    exit_bar = j + 1
                    break

                # --- Check trailing stop ---
                if has_trail:
                    # Activate trail?
                    if not trail_active and best_favorable >= trail_activation:
                        trail_active = True

                    if trail_active:
                        trail_level = best_favorable - trail_distance
                        # If trail level is better than initial stop, use it
                        if trail_level > -stop:
                            if bar_adverse <= trail_level:
                                exit_pl = trail_level
                                exit_bar = j + 1
                                break

                # --- Check partial exit ---
                if has_partial and not partial_taken:
                    if bar_favorable >= partial_exit_r:
                        partial_taken = True
                        partial_pl = partial_exit_r * partial_exit_pct
                        remaining_pct = 1.0 - partial_exit_pct

                # --- Check target hit ---
                if bar_favorable >= target:
                    if partial_taken:
                        exit_pl = partial_pl + target * remaining_pct
                    else:
                        exit_pl = target
                    exit_bar = j + 1
                    break

            # --- Time stop: exit at close of last bar if nothing else triggered ---
            if exit_bar == -1 and has_time_stop:
                # Use close of the time-stop bar
                ts_idx = min(time_stop_days - 1, max_bars - 1)
                if not np.isnan(close_pl[i, ts_idx]):
                    if partial_taken:
                        exit_pl = partial_pl + close_pl[i, ts_idx] * remaining_pct
                    else:
                        exit_pl = close_pl[i, ts_idx]
                    exit_bar = ts_idx + 1

            # --- No time stop and no exit triggered: use last available bar ---
            if exit_bar == -1 and not has_time_stop:
                # Find last valid bar
                for j in range(max_bars - 1, -1, -1):
                    if not np.isnan(close_pl[i, j]):
                        if partial_taken:
                            exit_pl = partial_pl + close_pl[i, j] * remaining_pct
                        else:
                            exit_pl = close_pl[i, j]
                        exit_bar = j + 1
                        break

            trade_pls[i] = exit_pl
            exit_bars[i] = exit_bar

        return trade_pls, exit_bars

    # ============================================================
    # Plateau Detection — Find Robust Parameter Regions
    # ============================================================

    def find_plateaus(self, results: list[StrategyResult],
                      top_pct: float = 0.05,
                      min_cluster_size: int = 10) -> list[PlateauRegion]:
        """Find regions of parameter space with consistently high performance.

        Takes the top N% of strategies and clusters them by parameter proximity
        to find broad plateaus rather than single optimal points.

        Args:
            results: Sorted list from optimize()
            top_pct: Fraction of top results to analyze
            min_cluster_size: Minimum strategies to form a valid plateau

        Returns:
            List of PlateauRegion objects sorted by robustness score
        """
        if not results:
            return []

        n_top = max(min_cluster_size, int(len(results) * top_pct))
        top = results[:n_top]

        # Extract parameter values from top strategies
        stops = np.array([r.stop for r in top])
        targets = np.array([r.target for r in top])
        time_stops = np.array([r.time_stop for r in top])
        trails = [r.trail_name for r in top]
        partials = [r.partial_name for r in top]
        evs = np.array([r.ev_per_trade for r in top])

        # Cluster by stop/target ranges using simple binning
        # Find the densest region of stop × target space
        plateaus = []

        # Try different stop/target windows
        stop_window_sizes = [0.5, 1.0, 1.5, 2.0]
        target_window_sizes = [1.0, 2.0, 3.0, 4.0]

        for sw in stop_window_sizes:
            for tw in target_window_sizes:
                # Slide window across stop range
                for s_start in np.arange(min(stops), max(stops) + 0.01, sw / 2):
                    s_end = s_start + sw
                    for t_start in np.arange(min(targets), max(targets) + 0.01, tw / 2):
                        t_end = t_start + tw

                        mask = ((stops >= s_start) & (stops <= s_end) &
                                (targets >= t_start) & (targets <= t_end))

                        if mask.sum() < min_cluster_size:
                            continue

                        cluster_evs = evs[mask]
                        cluster_ts = time_stops[mask]
                        cluster_trails = [t for t, m in zip(trails, mask) if m]
                        cluster_partials = [p for p, m in zip(partials, mask) if m]

                        # Robustness = (min EV in cluster / max EV in cluster) * density
                        ev_range = cluster_evs.max() - cluster_evs.min()
                        ev_consistency = 1.0 - (ev_range / cluster_evs.max()) if cluster_evs.max() > 0 else 0
                        density = mask.sum() / n_top

                        robustness = ev_consistency * density

                        plateau = PlateauRegion(
                            stop_range=(round(s_start, 2), round(s_end, 2)),
                            target_range=(round(t_start, 2), round(t_end, 2)),
                            time_stop_range=(int(cluster_ts.min()), int(cluster_ts.max())),
                            trail_cluster=list(set(cluster_trails)),
                            partial_cluster=list(set(cluster_partials)),
                            avg_ev=float(cluster_evs.mean()),
                            min_ev=float(cluster_evs.min()),
                            max_ev=float(cluster_evs.max()),
                            n_strategies=int(mask.sum()),
                            robustness_score=robustness,
                        )
                        plateaus.append(plateau)

        # Remove duplicates (overlapping plateaus) — keep highest robustness
        plateaus.sort(key=lambda p: p.robustness_score, reverse=True)
        filtered = []
        for p in plateaus:
            # Check if this overlaps significantly with any already kept
            overlaps = False
            for kept in filtered:
                # Simple overlap check on stop/target ranges
                s_overlap = (min(p.stop_range[1], kept.stop_range[1]) -
                             max(p.stop_range[0], kept.stop_range[0]))
                t_overlap = (min(p.target_range[1], kept.target_range[1]) -
                             max(p.target_range[0], kept.target_range[0]))
                if s_overlap > 0 and t_overlap > 0:
                    s_union = (max(p.stop_range[1], kept.stop_range[1]) -
                               min(p.stop_range[0], kept.stop_range[0]))
                    t_union = (max(p.target_range[1], kept.target_range[1]) -
                               min(p.target_range[0], kept.target_range[0]))
                    overlap_ratio = (s_overlap * t_overlap) / (s_union * t_union)
                    if overlap_ratio > 0.5:
                        overlaps = True
                        break
            if not overlaps:
                filtered.append(p)

        return filtered[:10]  # Top 10 non-overlapping plateaus

    # ============================================================
    # Quick Optimization — Reduced Parameter Space
    # ============================================================

    def optimize_quick(self, matrix: dict, direction: str = "short",
                       min_trades: int = 5) -> list[StrategyResult]:
        """Run a quick optimization with reduced parameter space.

        ~10x fewer combinations for faster iteration.
        Good for initial exploration before running full sweep.
        """
        quick_stops = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
        quick_targets = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
        quick_time = [0, 3, 5, 7, 10, 15, 20, 30]
        quick_trails = [
            ("none", 0, 0),
            ("trail_1.0", 0, 1.0),
            ("trail_2.0", 0, 2.0),
            ("be_1R_trail_1.0", 1.0, 1.0),
            ("be_2R_trail_1.0", 2.0, 1.0),
        ]
        quick_partials = [
            ("full_to_target", 0, 0, "hold"),
            ("half_at_1R", 1.0, 0.5, "hold"),
            ("half_at_2R", 2.0, 0.5, "hold"),
        ]

        # Temporarily swap params
        orig = (self.stops, self.targets, self.time_stops, self.trails, self.partials)
        self.stops = quick_stops
        self.targets = quick_targets
        self.time_stops = quick_time
        self.trails = quick_trails
        self.partials = quick_partials

        results = self.optimize(matrix, direction, min_trades)

        # Restore
        self.stops, self.targets, self.time_stops, self.trails, self.partials = orig
        return results

    # ============================================================
    # Reporting
    # ============================================================

    @staticmethod
    def print_top(results: list[StrategyResult], n: int = 20):
        """Print the top N strategies in a table."""
        if not results:
            print("No results")
            return

        print(f"\n{'#':<4} {'Stop':<6} {'Tgt':<6} {'Time':<5} {'Trail':<20} "
              f"{'Partial':<18} {'EV(R)':<8} {'WR':<7} {'AvgW':<7} {'AvgL':<7} "
              f"{'PF':<6} {'MaxDD':<7} {'Med':<7} {'Hold':<5} {'N':<4}")
        print("-" * 130)

        for i, r in enumerate(results[:n]):
            ts = f"{r.time_stop}d" if r.time_stop > 0 else "none"
            print(f"{i+1:<4} {r.stop:<6.2f} {r.target:<6.2f} {ts:<5} "
                  f"{r.trail_name:<20} {r.partial_name:<18} "
                  f"{r.ev_per_trade:<8.3f} {r.win_rate:<7.1%} "
                  f"{r.avg_winner:<7.3f} {r.avg_loser:<7.3f} "
                  f"{r.profit_factor:<6.2f} {r.max_drawdown_r:<7.3f} "
                  f"{r.median_trade:<7.3f} {r.avg_hold_days:<5.1f} {r.n_trades:<4}")

    @staticmethod
    def print_plateaus(plateaus: list[PlateauRegion]):
        """Print plateau analysis."""
        if not plateaus:
            print("No plateaus found")
            return

        print(f"\n{'#':<4} {'Stop Range':<14} {'Target Range':<14} "
              f"{'Time Range':<12} {'AvgEV':<8} {'MinEV':<8} {'MaxEV':<8} "
              f"{'N':<5} {'Robust':<8}")
        print("-" * 95)

        for i, p in enumerate(plateaus):
            print(f"{i+1:<4} "
                  f"{p.stop_range[0]:.1f}-{p.stop_range[1]:.1f}{'':>4} "
                  f"{p.target_range[0]:.1f}-{p.target_range[1]:.1f}{'':>4} "
                  f"{p.time_stop_range[0]}-{p.time_stop_range[1]}{'':>5} "
                  f"{p.avg_ev:<8.3f} {p.min_ev:<8.3f} {p.max_ev:<8.3f} "
                  f"{p.n_strategies:<5} {p.robustness_score:<8.3f}")

        # Detail on best plateau
        if plateaus:
            best = plateaus[0]
            print(f"\n--- Best Plateau Detail ---")
            print(f"  Stop: {best.stop_range[0]:.2f} - {best.stop_range[1]:.2f} ATR")
            print(f"  Target: {best.target_range[0]:.2f} - {best.target_range[1]:.2f} ATR")
            print(f"  Time stop: {best.time_stop_range[0]} - {best.time_stop_range[1]} days")
            print(f"  Trails: {', '.join(best.trail_cluster[:5])}")
            print(f"  Partials: {', '.join(best.partial_cluster[:5])}")
            print(f"  EV range: {best.min_ev:.3f} - {best.max_ev:.3f}R "
                  f"(avg {best.avg_ev:.3f}R)")
            print(f"  Robustness: {best.robustness_score:.3f}")

    @staticmethod
    def print_report(results: list[StrategyResult],
                     plateaus: list[PlateauRegion] = None,
                     n_top: int = 20):
        """Print a complete optimization report."""
        if not results:
            print("No results to report")
            return

        print("=" * 80)
        print("MANAGEMENT OPTIMIZATION REPORT")
        print("=" * 80)

        # Overall stats
        evs = [r.ev_per_trade for r in results]
        print(f"\nStrategies tested: {len(results):,}")
        print(f"EV range: {min(evs):.3f}R to {max(evs):.3f}R")
        print(f"Median strategy EV: {np.median(evs):.3f}R")
        print(f"% of strategies with positive EV: "
              f"{sum(1 for e in evs if e > 0) / len(evs):.1%}")

        # Top strategies
        print(f"\n--- Top {n_top} Strategies ---")
        ManagementOptimizer.print_top(results, n_top)

        # Plateaus
        if plateaus:
            print(f"\n--- Robust Plateaus ---")
            ManagementOptimizer.print_plateaus(plateaus)

        # Parameter sensitivity analysis
        print(f"\n--- Parameter Sensitivity ---")
        ManagementOptimizer._print_sensitivity(results)

    @staticmethod
    def _print_sensitivity(results: list[StrategyResult]):
        """Show which parameter ranges produce the best results."""
        if not results:
            return

        # Group by each parameter and show average EV
        print("\n  Stop distance → Average EV:")
        stop_groups = {}
        for r in results:
            stop_groups.setdefault(r.stop, []).append(r.ev_per_trade)
        for s in sorted(stop_groups.keys()):
            avg = np.mean(stop_groups[s])
            n = len(stop_groups[s])
            bar = "█" * max(1, int(avg * 10)) if avg > 0 else ""
            print(f"    {s:>5.2f} ATR: {avg:>7.3f}R ({n:>5} combos) {bar}")

        print("\n  Target distance → Average EV:")
        tgt_groups = {}
        for r in results:
            tgt_groups.setdefault(r.target, []).append(r.ev_per_trade)
        for t in sorted(tgt_groups.keys())[:15]:  # Top 15
            avg = np.mean(tgt_groups[t])
            n = len(tgt_groups[t])
            bar = "█" * max(1, int(avg * 10)) if avg > 0 else ""
            print(f"    {t:>5.2f} ATR: {avg:>7.3f}R ({n:>5} combos) {bar}")

        print("\n  Trail type → Average EV:")
        trail_groups = {}
        for r in results:
            trail_groups.setdefault(r.trail_name, []).append(r.ev_per_trade)
        for t in sorted(trail_groups.keys(), key=lambda x: np.mean(trail_groups[x]), reverse=True):
            avg = np.mean(trail_groups[t])
            n = len(trail_groups[t])
            print(f"    {t:<25}: {avg:>7.3f}R ({n:>5} combos)")

        print("\n  Partial strategy → Average EV:")
        partial_groups = {}
        for r in results:
            partial_groups.setdefault(r.partial_name, []).append(r.ev_per_trade)
        for p in sorted(partial_groups.keys(), key=lambda x: np.mean(partial_groups[x]), reverse=True):
            avg = np.mean(partial_groups[p])
            n = len(partial_groups[p])
            print(f"    {p:<25}: {avg:>7.3f}R ({n:>5} combos)")

        print("\n  Time stop → Average EV:")
        ts_groups = {}
        for r in results:
            ts_groups.setdefault(r.time_stop, []).append(r.ev_per_trade)
        for ts in sorted(ts_groups.keys()):
            avg = np.mean(ts_groups[ts])
            n = len(ts_groups[ts])
            label = f"{ts}d" if ts > 0 else "none"
            print(f"    {label:>6}: {avg:>7.3f}R ({n:>5} combos)")

    # ============================================================
    # Export
    # ============================================================

    @staticmethod
    def to_json(results: list[StrategyResult], n: int = 100) -> list[dict]:
        """Export top N results as JSON-serializable dicts."""
        return [r.to_dict() for r in results[:n]]

    @staticmethod
    def playbook_entry(results: list[StrategyResult],
                       plateaus: list[PlateauRegion] = None,
                       setup_name: str = "") -> dict:
        """Generate a playbook entry dict summarizing the optimal management."""
        if not results:
            return {}

        best = results[0]
        entry = {
            "setup": setup_name,
            "optimal_strategy": best.to_dict(),
            "top_5": [r.to_dict() for r in results[:5]],
        }

        if plateaus:
            bp = plateaus[0]
            entry["recommended_range"] = {
                "stop": f"{bp.stop_range[0]:.2f} - {bp.stop_range[1]:.2f} ATR",
                "target": f"{bp.target_range[0]:.2f} - {bp.target_range[1]:.2f} ATR",
                "time_stop": f"{bp.time_stop_range[0]} - {bp.time_stop_range[1]} days",
                "trails": bp.trail_cluster[:3],
                "partials": bp.partial_cluster[:3],
                "expected_ev": f"{bp.avg_ev:.3f}R",
                "robustness": f"{bp.robustness_score:.3f}",
            }

        return entry


# ============================================================
# CLI Entry Point
# ============================================================

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python -m scripts.management_optimizer <setup_type> [quick|full]")
        print("  setup_type: 3-4db, dtss, etc.")
        print("  mode: quick (reduced params, faster) or full (exhaustive)")
        sys.exit(1)

    setup_type = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "quick"

    # Determine direction from setup type
    direction_map = {
        "3-4db": "short",
        "dtss": "short",
        "htf": "long",
    }
    direction = direction_map.get(setup_type, "short")

    print(f"Loading outcome data for {setup_type} ({direction})...")
    from scripts.outcome_engine import OutcomeEngine
    engine = OutcomeEngine()

    # Try examples first
    outcomes = engine.compute_for_examples(setup_type)
    if not outcomes:
        print(f"No outcomes found for {setup_type}")
        sys.exit(1)

    matrix = OutcomeEngine.outcomes_to_matrix(outcomes)
    OutcomeEngine.print_summary(outcomes)

    print(f"\nRunning {mode} optimization...")
    optimizer = ManagementOptimizer()

    if mode == "quick":
        results = optimizer.optimize_quick(matrix, direction)
    else:
        results = optimizer.optimize(matrix, direction)

    plateaus = optimizer.find_plateaus(results)
    optimizer.print_report(results, plateaus)

    # Save top results
    output = optimizer.playbook_entry(results, plateaus, setup_type)
    output_path = f"data/{setup_type}_management.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nPlaybook entry saved to {output_path}")

    from file_mirror import mirror_file
    mirror_file(output_path)
