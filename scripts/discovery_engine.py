"""
Discovery Engine — Step 4 of ANALYSIS_SYSTEM.md

Takes profiling DataFrames (examples + universe sample) and scores every
numeric feature on two axes:

  1. Consistency — how tightly examples cluster relative to the universe spread.
  2. Selectivity — what % of the universe falls within the example range.
  3. Combined score — product of ranks on both axes.
  4. Threshold extraction — tightest filter that passes 100% of examples.

Usage:
  from scripts.discovery_engine import DiscoveryEngine

  engine = DiscoveryEngine()
  # From pre-computed DataFrames
  report = engine.discover(examples_df, universe_df)
  # Or run profiling + discovery end-to-end
  report = engine.discover_for_setup("dtss", universe_n=500)

  # Access results
  report.top_features(n=50)       # Ranked feature list
  report.grouped_by_concept()     # Features grouped by TA concept
  report.to_json("output.json")   # Export for storage
"""

import json
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

# Feature concept groupings — maps column name patterns to TA concepts.
# Order matters: first match wins.
CONCEPT_RULES = [
    # Setup-specific metadata (Layer 5)
    ("lsp_dist_", "lsp_distance"),
    ("lsp_valley_", "lsp_valley"),
    ("lsp_approach_", "lsp_approach"),
    ("lsp_maxh", "lsp_recent_highs"),
    ("lsp_sma", "lsp_ma_distance"),
    ("lsp_close_above_", "lsp_extension"),
    ("lsp_bars_", "lsp_timing"),
    ("lsp_pct_", "lsp_distance"),
    ("lsp_", "lsp_other"),

    # Market context (Layer 3)
    ("mkt_rel_", "market_relative"),
    ("spy_", "market_context"),
    ("qqq_", "market_context"),

    # Rate of change (Layer 4)
    ("roc_", "indicator_momentum"),
    ("accel_", "indicator_acceleration"),

    # Extension & distance from MA
    ("ext_maxh_sma", "extension_from_sma"),
    ("ext_maxh_ema", "extension_from_ema"),
    ("ext_", "extension"),
    ("dist_c_sma", "distance_from_sma"),
    ("dist_c_ema", "distance_from_ema"),
    ("dist_c_fwma", "distance_from_fwma"),
    ("dist_c_hma", "distance_from_hma"),
    ("dist_", "ma_distance"),

    # Pullback
    ("pullback_depth_", "pullback_depth"),
    ("pullback_pct_", "pullback_retracement"),
    ("pullback_", "pullback"),

    # MA structure
    ("ma_slope_sma", "sma_slope"),
    ("ma_slope_ema", "ema_slope"),
    ("ma_slope_", "ma_slope"),
    ("ma_spread_", "ma_spread"),
    ("ma_ratio_", "ma_ratio"),

    # Price position
    ("price_pos_", "price_position"),
    ("bb_pctb_", "bollinger_position"),

    # Range
    ("range_ratio_", "range_ratio"),
    ("candle_", "candle_shape"),

    # Volume
    ("vol_ratio_", "volume_ratio"),
    ("vol_", "volume"),
    ("obv_", "obv"),
    ("cmf_", "cmf"),
    ("pvo_", "pvo"),

    # Counting patterns
    ("count_up_", "consecutive_up"),
    ("count_dn_", "consecutive_down"),
    ("count_above_", "count_above"),
    ("count_below_", "count_below"),
    ("since_", "bars_since"),
    ("tir_", "true_in_row"),

    # Momentum indicators
    ("rsi_", "rsi"),
    ("wrsi_", "wrsi"),
    ("stoch_", "stochastic"),
    ("cci_", "cci"),
    ("adx_", "adx"),
    ("aroon_", "aroon"),
    ("macd_", "macd"),
    ("ppo_", "ppo"),
    ("willr_", "williams_r"),
    ("bop_", "bop"),
    ("kaufman_", "kaufman_er"),
    ("elder_", "elder"),

    # Bollinger bands
    ("bb_", "bollinger"),

    # ATR
    ("atr_", "atr"),
    ("hvol_", "historical_vol"),

    # Raw price / MA levels
    ("sma_c_", "sma"),
    ("ema_c_", "ema"),
    ("fwma_c_", "fwma"),
    ("hma_c_", "hma"),
    ("maxh_", "rolling_high"),
    ("maxc_", "rolling_high"),
    ("minl_", "rolling_low"),
    ("minc_", "rolling_low"),

    # Price offsets
    ("c_", "price_offset"),
    ("o_", "price_offset"),
    ("h_", "price_offset"),
    ("l_", "price_offset"),
    ("v_", "volume_offset"),

    # VWAP
    ("vwap_", "vwap"),
]


def classify_concept(feature_name: str) -> str:
    """Map a feature column name to its TA concept group."""
    name_lower = feature_name.lower()
    for prefix, concept in CONCEPT_RULES:
        if name_lower.startswith(prefix):
            return concept
    return "other"


# ============================================================
# Feature scoring
# ============================================================

@dataclass
class FeatureScore:
    """Score for a single feature."""
    name: str
    concept: str

    # Example statistics
    example_min: float
    example_max: float
    example_median: float
    example_std: float
    example_count: int          # How many examples had non-null values
    example_total: int          # Total examples

    # Universe statistics
    universe_p5: float          # 5th percentile
    universe_p25: float
    universe_median: float
    universe_p75: float
    universe_p95: float
    universe_std: float
    universe_count: int

    # Scoring
    consistency: float          # Lower = tighter clustering (example spread / universe IQR)
    selectivity: float          # Lower = more selective (% of universe in example range)
    combined_rank: int = 0      # Rank by consistency_rank × selectivity_rank

    # Threshold
    threshold_direction: str = ""  # ">" or "<"
    threshold_value: float = 0.0
    threshold_pass_pct: float = 0.0  # % of universe that passes this threshold


@dataclass
class DiscoveryReport:
    """Complete discovery output."""
    setup_type: str
    n_examples: int
    n_universe: int
    n_features_scored: int
    features: list[FeatureScore] = field(default_factory=list)

    def top_features(self, n: int = 50) -> list[FeatureScore]:
        """Get top N features by combined rank."""
        return sorted(self.features, key=lambda f: f.combined_rank)[:n]

    def grouped_by_concept(self, n: int = 100) -> dict[str, list[FeatureScore]]:
        """Group top features by TA concept."""
        top = self.top_features(n)
        groups: dict[str, list[FeatureScore]] = {}
        for f in top:
            groups.setdefault(f.concept, []).append(f)
        # Sort groups by best feature rank in each
        return dict(sorted(groups.items(),
                           key=lambda kv: kv[1][0].combined_rank))

    def to_dict(self) -> dict:
        """Export as serializable dict."""
        return {
            "setup_type": self.setup_type,
            "n_examples": self.n_examples,
            "n_universe": self.n_universe,
            "n_features_scored": self.n_features_scored,
            "features": [
                {
                    "rank": f.combined_rank,
                    "name": f.name,
                    "concept": f.concept,
                    "consistency": round(f.consistency, 4),
                    "selectivity": round(f.selectivity, 4),
                    "example_min": round(f.example_min, 6),
                    "example_max": round(f.example_max, 6),
                    "example_median": round(f.example_median, 6),
                    "example_count": f.example_count,
                    "universe_median": round(f.universe_median, 6),
                    "universe_p5": round(f.universe_p5, 6),
                    "universe_p95": round(f.universe_p95, 6),
                    "threshold_direction": f.threshold_direction,
                    "threshold_value": round(f.threshold_value, 6),
                    "threshold_pass_pct": round(f.threshold_pass_pct, 4),
                }
                for f in sorted(self.features, key=lambda x: x.combined_rank)
            ],
        }

    def to_json(self, path: str):
        """Write report to JSON file."""
        with open(path, "w") as fp:
            json.dump(self.to_dict(), fp, indent=2)

    def summary(self, n: int = 30) -> str:
        """Human-readable summary of top features."""
        lines = [
            f"Discovery Report: {self.setup_type}",
            f"Examples: {self.n_examples} | Universe sample: {self.n_universe} | Features scored: {self.n_features_scored}",
            "",
            f"{'Rank':>4}  {'Feature':<45}  {'Concept':<24}  {'Consist':>8}  {'Select':>8}  {'Ex Range':<24}  {'Thresh':<16}  {'Univ Pass':>9}",
            "-" * 160,
        ]
        for f in self.top_features(n):
            ex_range = f"{f.example_min:.3f} — {f.example_max:.3f}"
            thresh = f"{f.threshold_direction} {f.threshold_value:.4f}"
            lines.append(
                f"{f.combined_rank:>4}  {f.name:<45}  {f.concept:<24}  "
                f"{f.consistency:>8.4f}  {f.selectivity:>8.4f}  "
                f"{ex_range:<24}  {thresh:<16}  {f.threshold_pass_pct:>8.1%}"
            )
        return "\n".join(lines)


# ============================================================
# Discovery Engine
# ============================================================

class DiscoveryEngine:
    """Scores profiling features for consistency × selectivity."""

    # Columns to skip — metadata, not measurements
    SKIP_COLS = {
        "ticker", "date", "entry_date", "scan_date", "is_example",
        "C", "O", "H", "L", "V",  # Raw price/vol — not normalized
    }

    # Minimum fraction of examples that must have non-null values
    MIN_EXAMPLE_COVERAGE = 0.75

    # Buffer factor: expand example range by this fraction of range width
    # to avoid knife-edge thresholds
    BUFFER_FACTOR = 0.10

    def __init__(self, buffer_factor: float = 0.10, min_coverage: float = 0.75):
        self.buffer_factor = buffer_factor
        self.min_coverage = min_coverage

    def discover(
        self,
        examples_df: pd.DataFrame,
        universe_df: pd.DataFrame,
        setup_type: str = "unknown",
    ) -> DiscoveryReport:
        """Run discovery on pre-computed profiling DataFrames.

        Args:
            examples_df: DataFrame from profiling engine (is_example=True rows)
            universe_df: DataFrame from profiling engine (is_example=False rows)
            setup_type: Label for the report

        Returns:
            DiscoveryReport with all scored features
        """
        # Identify numeric columns present in both DataFrames
        ex_numeric = set(examples_df.select_dtypes(include=[np.number]).columns)
        uni_numeric = set(universe_df.select_dtypes(include=[np.number]).columns)
        shared_cols = sorted(ex_numeric & uni_numeric - self.SKIP_COLS)

        scores: list[FeatureScore] = []

        for col in shared_cols:
            score = self._score_feature(col, examples_df, universe_df)
            if score is not None:
                scores.append(score)

        # Rank by consistency (lower is better)
        scores_by_consist = sorted(scores, key=lambda s: s.consistency)
        for rank, s in enumerate(scores_by_consist, 1):
            s._consist_rank = rank

        # Rank by selectivity (lower is better)
        scores_by_select = sorted(scores, key=lambda s: s.selectivity)
        for rank, s in enumerate(scores_by_select, 1):
            s._select_rank = rank

        # Combined rank = product of individual ranks
        for s in scores:
            s.combined_rank = s._consist_rank * s._select_rank

        # Sort by combined rank and re-number
        scores.sort(key=lambda s: (s.combined_rank, s.consistency))
        for i, s in enumerate(scores, 1):
            s.combined_rank = i

        return DiscoveryReport(
            setup_type=setup_type,
            n_examples=len(examples_df),
            n_universe=len(universe_df),
            n_features_scored=len(scores),
            features=scores,
        )

    def discover_for_setup(
        self,
        setup_type: str,
        universe_n: int = 500,
        universe_date: str = None,
        api_base: str = "https://web-production-e3025.up.railway.app",
        progress_callback=None,
    ) -> DiscoveryReport:
        """Run profiling + discovery end-to-end for a setup type.

        Args:
            setup_type: e.g. "dtss", "3-4db"
            universe_n: Number of universe tickers to sample
            universe_date: Date for universe sampling (defaults to most recent)
            api_base: Railway API base URL
            progress_callback: Optional fn(current, total, msg)

        Returns:
            DiscoveryReport
        """
        from scripts.profiling_engine import ProfilingEngine

        engine = ProfilingEngine(api_base=api_base)

        if progress_callback:
            progress_callback(0, 0, "Profiling examples...")

        examples_df = engine.profile_examples(
            setup_type, include_market=True, progress_callback=progress_callback
        )

        if examples_df.empty:
            raise ValueError(f"No examples profiled for '{setup_type}'")

        if progress_callback:
            progress_callback(0, 0, f"Profiling {universe_n} universe tickers...")

        universe_df = engine.profile_universe_sample(
            date=universe_date or self._get_latest_date(engine),
            n=universe_n,
            include_market=True,
            progress_callback=progress_callback,
            detect_lsp=(setup_type == "dtss"),  # Enable LSP detection for DTSS
        )

        if universe_df.empty:
            raise ValueError("No universe tickers profiled")

        if progress_callback:
            progress_callback(0, 0, "Running discovery...")

        return self.discover(examples_df, universe_df, setup_type=setup_type)

    # ----------------------------------------------------------
    # Internal: score a single feature
    # ----------------------------------------------------------

    def _score_feature(
        self, col: str, examples_df: pd.DataFrame, universe_df: pd.DataFrame
    ) -> Optional[FeatureScore]:
        """Score one feature column for consistency and selectivity."""

        ex_vals = examples_df[col].dropna()
        uni_vals = universe_df[col].dropna()

        n_examples_total = len(examples_df)

        # Skip if too few valid values
        if len(ex_vals) < self.min_coverage * n_examples_total:
            return None
        if len(uni_vals) < 20:
            return None

        # Skip if zero variance in examples (constant across all — usually a
        # degenerate indicator, e.g. count=0 everywhere)
        ex_std = ex_vals.std()
        uni_std = uni_vals.std()
        if uni_std == 0 or np.isnan(uni_std):
            return None

        # Example stats
        ex_min = ex_vals.min()
        ex_max = ex_vals.max()
        ex_median = ex_vals.median()
        ex_spread = ex_max - ex_min

        # Universe stats
        uni_p5 = uni_vals.quantile(0.05)
        uni_p25 = uni_vals.quantile(0.25)
        uni_median = uni_vals.median()
        uni_p75 = uni_vals.quantile(0.75)
        uni_p95 = uni_vals.quantile(0.95)
        uni_iqr = uni_p75 - uni_p25

        # -------------------------------------------------------
        # Consistency: example spread / universe IQR
        # Lower = examples cluster tighter relative to universe
        # -------------------------------------------------------
        if uni_iqr == 0:
            # All universe values in a tight band — use full range instead
            uni_range = uni_vals.max() - uni_vals.min()
            consistency = ex_spread / uni_range if uni_range > 0 else 1.0
        else:
            consistency = ex_spread / uni_iqr

        # -------------------------------------------------------
        # Selectivity: % of universe within the buffered example range
        # Lower = more selective
        # -------------------------------------------------------
        buffer = ex_spread * self.buffer_factor
        range_lo = ex_min - buffer
        range_hi = ex_max + buffer

        in_range = ((uni_vals >= range_lo) & (uni_vals <= range_hi)).sum()
        selectivity = in_range / len(uni_vals)

        # -------------------------------------------------------
        # Threshold extraction
        # Determine whether examples sit HIGH or LOW relative to universe
        # -------------------------------------------------------
        threshold_dir, threshold_val, threshold_pass_pct = self._extract_threshold(
            ex_vals, uni_vals
        )

        concept = classify_concept(col)

        return FeatureScore(
            name=col,
            concept=concept,
            example_min=float(ex_min),
            example_max=float(ex_max),
            example_median=float(ex_median),
            example_std=float(ex_std) if not np.isnan(ex_std) else 0.0,
            example_count=len(ex_vals),
            example_total=n_examples_total,
            universe_p5=float(uni_p5),
            universe_p25=float(uni_p25),
            universe_median=float(uni_median),
            universe_p75=float(uni_p75),
            universe_p95=float(uni_p95),
            universe_std=float(uni_std),
            universe_count=len(uni_vals),
            consistency=float(consistency),
            selectivity=float(selectivity),
            threshold_direction=threshold_dir,
            threshold_value=float(threshold_val),
            threshold_pass_pct=float(threshold_pass_pct),
        )

    def _extract_threshold(
        self, ex_vals: pd.Series, uni_vals: pd.Series
    ) -> tuple[str, float, float]:
        """Determine the best single threshold that passes all examples
        and filters out the most universe.

        Returns:
            (direction, value, universe_pass_pct)
            direction is ">" or "<"
        """
        ex_min = ex_vals.min()
        ex_max = ex_vals.max()
        ex_median = ex_vals.median()
        uni_median = uni_vals.median()

        # Apply small buffer to avoid knife-edge thresholds
        ex_spread = ex_max - ex_min
        buffer = ex_spread * self.buffer_factor

        # Try ">" threshold (examples are higher than universe)
        # All examples must be >= threshold
        gt_threshold = ex_min - buffer
        gt_pass = (uni_vals >= gt_threshold).sum() / len(uni_vals)

        # Try "<" threshold (examples are lower than universe)
        # All examples must be <= threshold
        lt_threshold = ex_max + buffer
        lt_pass = (uni_vals <= lt_threshold).sum() / len(uni_vals)

        # Pick whichever direction filters out more of the universe
        if gt_pass <= lt_pass:
            # ">" filters more (lower pass % = more selective)
            return ">", gt_threshold, gt_pass
        else:
            return "<", lt_threshold, lt_pass

    def _get_latest_date(self, engine) -> str:
        """Get the most recent trading date from the universe."""
        rows = engine._query(
            "SELECT MAX(date) as d FROM universe_ohlcv WHERE ticker='SPY'"
        )
        if rows and rows[0].get('d'):
            return rows[0]['d']
        raise ValueError("Cannot determine latest trading date")


# ============================================================
# CLI for standalone testing
# ============================================================

if __name__ == "__main__":
    import sys
    import time

    setup_type = sys.argv[1] if len(sys.argv) > 1 else "dtss"
    universe_n = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    def progress(cur, total, msg):
        if total > 0:
            print(f"  [{cur}/{total}] {msg}")
        else:
            print(f"  {msg}")

    print(f"\n=== Discovery Engine: {setup_type} ===")
    print(f"Universe sample: {universe_n} tickers\n")

    engine = DiscoveryEngine()
    t0 = time.time()

    report = engine.discover_for_setup(
        setup_type,
        universe_n=universe_n,
        progress_callback=progress,
    )

    elapsed = time.time() - t0
    print(f"\nDiscovery complete in {elapsed:.1f}s")
    print(f"Features scored: {report.n_features_scored}")
    print()
    print(report.summary(40))

    # Save report
    out_path = f"discovery_{setup_type}.json"
    report.to_json(out_path)
    print(f"\nFull report saved to {out_path}")
