"""
Brute Force Expression Generator — Every meaningful parameter combination.

This isn't the curated 385. This is EVERYTHING that could possibly matter,
letting pure compute find what works.

Usage:
    python local_runner/brute_expressions.py

Generates: local_runner/cache/brute_expressions.json
"""

import json
import os

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def generate_all():
    exprs = []

    # ═══════════════════════════════════════════════════════
    # NEAR RESISTANCE — Price distance to prior highs
    # Every MAXH period from 5-120 in steps of 5, plus key ones
    # ═══════════════════════════════════════════════════════
    maxh_periods = sorted(set(
        list(range(5, 125, 5)) +  # every 5 from 5-120
        [3, 7, 10, 15, 63, 65, 126]  # key extras
    ))
    for p in maxh_periods:
        for price in ["C", "H"]:
            for norm in ["atr14", "adr14", "pct"]:
                exprs.append({
                    "name": f"nr_{price.lower()}_maxh{p}_{norm}",
                    "category": "near_resistance",
                    "compute": {"op": "distance_to_maxh", "price_ref": price,
                                "maxh_period": p, "normalizer": norm}
                })
        # Ratio
        exprs.append({
            "name": f"nr_ratio_maxh{p}",
            "category": "near_resistance",
            "compute": {"op": "ratio_c_maxh", "maxh_period": p}
        })

    # ═══════════════════════════════════════════════════════
    # EXTENSION ABOVE MAs — The core TA concept
    # Every meaningful MA type and period
    # ═══════════════════════════════════════════════════════
    sma_periods = [5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200]
    ema_periods = [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200]

    all_mas = [f"avgc{p}" for p in sma_periods] + [f"xavgc{p}" for p in ema_periods]

    for ma in all_mas:
        for norm in ["atr14", "adr14", "pct"]:
            exprs.append({
                "name": f"ext_{ma}_{norm}",
                "category": "extension",
                "compute": {"op": "extension", "ma": ma, "normalizer": norm}
            })

    # ═══════════════════════════════════════════════════════
    # MA SLOPES — Trend direction and acceleration
    # ═══════════════════════════════════════════════════════
    slope_mas = ["xavgc8", "xavgc13", "xavgc21", "xavgc50", "xavgc100", "xavgc200",
                 "avgc10", "avgc20", "avgc50", "avgc100", "avgc200"]
    slope_offsets = [1, 2, 3, 5, 7, 10, 15, 20]

    for ma in slope_mas:
        for offset in slope_offsets:
            for norm in ["atr14", "adr14"]:
                exprs.append({
                    "name": f"slope_{ma}_off{offset}_{norm}",
                    "category": "ma_slope",
                    "compute": {"op": "ma_slope", "ma": ma, "offset": offset,
                                "normalizer": norm}
                })

    # ═══════════════════════════════════════════════════════
    # MA SPREADS — Stacking, convergence/divergence
    # ═══════════════════════════════════════════════════════
    spread_pairs = [
        ("xavgc8", "xavgc21"), ("xavgc8", "xavgc50"), ("xavgc8", "xavgc100"),
        ("xavgc8", "xavgc200"), ("xavgc13", "xavgc50"), ("xavgc21", "xavgc50"),
        ("xavgc21", "xavgc100"), ("xavgc50", "xavgc100"), ("xavgc50", "xavgc200"),
        ("xavgc100", "xavgc200"),
        ("avgc10", "avgc20"), ("avgc10", "avgc50"), ("avgc20", "avgc50"),
        ("avgc20", "avgc100"), ("avgc50", "avgc100"), ("avgc50", "avgc200"),
        ("avgc100", "avgc200"),
        ("xavgc8", "avgc50"), ("xavgc21", "avgc50"), ("xavgc50", "avgc200"),
    ]
    for fast, slow in spread_pairs:
        for norm in ["atr14", "adr14"]:
            exprs.append({
                "name": f"spread_{fast}_{slow}_{norm}",
                "category": "ma_spread",
                "compute": {"op": "ma_spread", "ma_fast": fast, "ma_slow": slow,
                            "normalizer": norm}
            })

    # ═══════════════════════════════════════════════════════
    # MOMENTUM — ROC, RSI, Stochastic, CCI, ADX, BOP, Volume
    # ═══════════════════════════════════════════════════════

    # Rate of Change — every period 1-50
    for p in list(range(1, 21)) + [25, 30, 40, 50, 65]:
        exprs.append({
            "name": f"roc_{p}",
            "category": "momentum",
            "compute": {"op": "roc", "period": p}
        })

    # ROC delta (is momentum accelerating or decelerating?)
    for p in [3, 5, 10, 15, 20, 30, 50]:
        for co in [3, 5, 10]:
            exprs.append({
                "name": f"roc_delta_{p}_vs{co}",
                "category": "momentum",
                "compute": {"op": "roc_delta", "period": p, "compare_offset": co}
            })

    # RSI — multiple periods
    for p in [5, 7, 9, 14, 21, 28]:
        exprs.append({
            "name": f"rsi_{p}",
            "category": "momentum",
            "compute": {"op": "rsi", "period": p}
        })
        # RSI slope
        for offset in [3, 5, 10]:
            exprs.append({
                "name": f"rsi_slope_{p}_off{offset}",
                "category": "momentum",
                "compute": {"op": "rsi_slope", "period": p, "offset": offset}
            })

    # Volume ratios
    for p in [5, 10, 15, 20, 30, 50]:
        exprs.append({
            "name": f"vol_ratio_{p}",
            "category": "momentum",
            "compute": {"op": "volume_ratio", "avg_period": p}
        })

    # ADX + slope
    for p in [7, 10, 14, 20]:
        exprs.append({
            "name": f"adx_{p}",
            "category": "momentum",
            "compute": {"op": "adx", "period": p}
        })
        for offset in [3, 5, 10]:
            exprs.append({
                "name": f"adx_slope_{p}_off{offset}",
                "category": "momentum",
                "compute": {"op": "adx_slope", "period": p, "offset": offset}
            })

    # DI spread
    for p in [7, 14, 20]:
        exprs.append({
            "name": f"di_spread_{p}",
            "category": "momentum",
            "compute": {"op": "di_spread", "period": p}
        })

    # Stochastic
    for p in [5, 9, 14, 21]:
        exprs.append({
            "name": f"stoch_{p}",
            "category": "momentum",
            "compute": {"op": "stochastic", "period": p}
        })

    # CCI
    for p in [10, 14, 20, 30]:
        exprs.append({
            "name": f"cci_{p}",
            "category": "momentum",
            "compute": {"op": "cci", "period": p}
        })

    # BOP
    for p in [5, 10, 14, 20]:
        exprs.append({
            "name": f"bop_{p}",
            "category": "momentum",
            "compute": {"op": "bop", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # RANGE & CHANNEL — Where price sits, squeeze detection
    # ═══════════════════════════════════════════════════════

    # Range position
    for p in [5, 10, 15, 20, 30, 50, 65, 90, 120]:
        exprs.append({
            "name": f"range_pos_{p}",
            "category": "range",
            "compute": {"op": "range_position", "period": p}
        })

    # Pullback from high
    for p in [5, 10, 15, 20, 30, 50, 65, 120]:
        for norm in ["atr14", "adr14", "pct"]:
            exprs.append({
                "name": f"pullback_{p}_{norm}",
                "category": "range",
                "compute": {"op": "pullback", "period": p, "normalizer": norm}
            })

    # Range width (volatility squeeze)
    for p in [5, 10, 15, 20, 30, 50, 65, 120]:
        exprs.append({
            "name": f"range_width_{p}",
            "category": "range",
            "compute": {"op": "range_width", "period": p, "normalizer": "atr14"}
        })

    # Channel slope of highs
    for p in [5, 10, 15, 20, 30]:
        exprs.append({
            "name": f"channel_slope_{p}",
            "category": "range",
            "compute": {"op": "channel_slope", "ref": "maxh", "period": p,
                        "normalizer": "atr14"}
        })

    # Candle anatomy
    exprs.append({"name": "candle_range_atr", "category": "range",
                  "compute": {"op": "candle_range_ratio"}})
    exprs.append({"name": "body_range_ratio", "category": "range",
                  "compute": {"op": "body_range_ratio"}})
    exprs.append({"name": "upper_wick_ratio", "category": "range",
                  "compute": {"op": "upper_wick_ratio"}})

    # ═══════════════════════════════════════════════════════
    # EXTENSION DYNAMICS — Is extension building or declining?
    # ═══════════════════════════════════════════════════════
    ext_dyn_mas = ["avgc50", "avgc200", "xavgc21", "xavgc50", "xavgc100"]
    ext_offsets = [1, 2, 3, 5, 7, 10, 15, 20]

    for ma in ext_dyn_mas:
        for offset in ext_offsets:
            exprs.append({
                "name": f"ext_slope_{ma}_off{offset}",
                "category": "extension_dynamics",
                "compute": {"op": "extension_slope", "ma": ma, "offset": offset,
                            "normalizer": "adr14"}
            })
        # Peak ratio — has extension peaked?
        for lb in [10, 15, 20, 30, 50]:
            exprs.append({
                "name": f"ext_peak_{ma}_lb{lb}",
                "category": "extension_dynamics",
                "compute": {"op": "extension_peak_ratio", "ma": ma, "lookback": lb}
            })

    # ═══════════════════════════════════════════════════════
    # EXTENSION CEILING — Statistical ceiling proximity
    # How close is current extension to its historical max?
    # From ta_knowledge: "knowing a stock's typical max extension helps gauge where it is in its cycle"
    # ═══════════════════════════════════════════════════════
    ceiling_mas = ["avgc50", "avgc200", "xavgc50", "xavgc100"]
    ceiling_lookbacks = [60, 120, 252, 504]  # 3mo, 6mo, 1yr, 2yr

    for ma in ceiling_mas:
        for lb in ceiling_lookbacks:
            for norm in ["adr14", "atr14"]:
                exprs.append({
                    "name": f"ext_ceil_{ma}_lb{lb}_{norm}",
                    "category": "extension_ceiling",
                    "compute": {"op": "extension_ceiling_ratio", "ma": ma,
                                "lookback": lb, "normalizer": norm}
                })

    # ═══════════════════════════════════════════════════════
    # EXTENSION ADR MULTIPLES — Core ta_knowledge metric
    # "Extension from 50 SMA in multiples of ADR is the universal normalized cycle indicator"
    # ═══════════════════════════════════════════════════════
    adr_mult_mas = ["avgc50", "avgc200", "xavgc21", "xavgc50", "xavgc100", "xavgc200"]
    for ma in adr_mult_mas:
        exprs.append({
            "name": f"ext_adr_{ma}",
            "category": "extension_adr",
            "compute": {"op": "ext_adr_multiples", "ma": ma}
        })

    # ═══════════════════════════════════════════════════════
    # MA CROSS DYNAMICS — Cross frequency and recency
    # "50 SMA cross frequency" = choppy/stage 3 detection
    # ═══════════════════════════════════════════════════════
    cross_mas = ["avgc50", "avgc200", "xavgc8", "xavgc21", "xavgc50"]
    cross_periods = [20, 30, 50, 65, 120]

    for ma in cross_mas:
        for p in cross_periods:
            exprs.append({
                "name": f"cross_count_{ma}_{p}",
                "category": "ma_cross",
                "compute": {"op": "ma_cross_count", "ma": ma, "period": p}
            })
        exprs.append({
            "name": f"bars_since_cross_{ma}",
            "category": "ma_cross",
            "compute": {"op": "bars_since_ma_cross", "ma": ma, "max_lookback": 120}
        })
        for p in [20, 50, 120]:
            for norm in ["atr14", "adr14"]:
                exprs.append({
                    "name": f"undercut_{ma}_{p}_{norm}",
                    "category": "ma_cross",
                    "compute": {"op": "ma_undercut_depth", "ma": ma, "period": p,
                                "normalizer": norm}
                })

    # ═══════════════════════════════════════════════════════
    # SWING STRUCTURE — Higher highs, lower lows, etc.
    # From ta_knowledge: "Higher lows, surfing a moving average"
    # ═══════════════════════════════════════════════════════
    swing_periods = [15, 20, 30, 50, 65, 90, 120]

    for p in swing_periods:
        for op_name in ["swing_high_count", "swing_low_count",
                        "higher_high_count", "higher_low_count",
                        "lower_high_count", "lower_low_count"]:
            exprs.append({
                "name": f"{op_name}_{p}",
                "category": "swing_structure",
                "compute": {"op": op_name, "period": p}
            })

    # ═══════════════════════════════════════════════════════
    # RETRACEMENT — Fib-style level of current price in N-bar range
    # From ta_knowledge: "Best-profit 3-4DB bounce ~50% of measured move"
    # ═══════════════════════════════════════════════════════
    retrace_periods = [5, 10, 15, 20, 30, 40, 50, 65, 90, 120]

    for p in retrace_periods:
        exprs.append({
            "name": f"retrace_{p}",
            "category": "retracement",
            "compute": {"op": "retracement_level", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # GAP ANALYSIS — Size, frequency, unfilled gaps
    # ═══════════════════════════════════════════════════════
    for norm in ["atr14", "adr14"]:
        exprs.append({
            "name": f"gap_today_{norm}",
            "category": "gap",
            "compute": {"op": "gap_size", "normalizer": norm}
        })

    for p in [5, 10, 20, 30, 50]:
        for thresh in [0.3, 0.5, 1.0]:
            exprs.append({
                "name": f"gap_count_{p}_t{thresh}",
                "category": "gap",
                "compute": {"op": "gap_count", "period": p, "threshold": thresh}
            })

    for p in [10, 20, 30, 50]:
        exprs.append({
            "name": f"unfilled_gapup_{p}",
            "category": "gap",
            "compute": {"op": "unfilled_gap_up_count", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # CONSECUTIVE MOVE — Up/down streaks with magnitude
    # ═══════════════════════════════════════════════════════
    for op_name in ["consecutive_up_roc", "consecutive_down_roc",
                    "consecutive_up_days", "consecutive_down_days"]:
        exprs.append({
            "name": op_name,
            "category": "consecutive",
            "compute": {"op": op_name}
        })

    # ═══════════════════════════════════════════════════════
    # CANDLE PATTERNS — Inside bars, outside bars, NR, compression
    # From ta_knowledge: "tight candle = AVWAP foothold confirmation"
    # ═══════════════════════════════════════════════════════
    for p in [5, 7, 10, 15, 20, 30]:
        exprs.append({
            "name": f"inside_bars_{p}",
            "category": "candle_pattern",
            "compute": {"op": "inside_bar_count", "period": p}
        })
        exprs.append({
            "name": f"outside_bars_{p}",
            "category": "candle_pattern",
            "compute": {"op": "outside_bar_count", "period": p}
        })

    for p in [3, 5, 7, 10, 14, 20]:
        exprs.append({
            "name": f"nr_ratio_{p}",
            "category": "candle_pattern",
            "compute": {"op": "nr_ratio", "period": p}
        })

    exprs.append({"name": "lower_wick_ratio", "category": "candle_pattern",
                  "compute": {"op": "lower_wick_ratio"}})

    for p in [5, 10, 15, 20]:
        exprs.append({
            "name": f"avg_body_ratio_{p}",
            "category": "candle_pattern",
            "compute": {"op": "avg_candle_body_ratio", "period": p}
        })

    for p in [10, 20, 30]:
        exprs.append({
            "name": f"close_vs_open_{p}",
            "category": "candle_pattern",
            "compute": {"op": "close_vs_open_ratio", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # VOLUME CHARACTER — OBV, accumulation/distribution, CMF
    # From ta_knowledge: "volume drying up on second approach (bearish confirmation)"
    # ═══════════════════════════════════════════════════════
    for offset in [3, 5, 10, 15, 20, 30]:
        exprs.append({
            "name": f"obv_slope_{offset}",
            "category": "volume_character",
            "compute": {"op": "obv_slope", "offset": offset, "vol_period": 20}
        })

    for p in [5, 10, 15, 20, 30]:
        exprs.append({
            "name": f"up_vol_ratio_{p}",
            "category": "volume_character",
            "compute": {"op": "up_volume_ratio", "period": p}
        })

    for p in [10, 14, 20, 30]:
        exprs.append({
            "name": f"cmf_{p}",
            "category": "volume_character",
            "compute": {"op": "cmf", "period": p}
        })
        for offset in [3, 5, 10]:
            exprs.append({
                "name": f"cmf_slope_{p}_off{offset}",
                "category": "volume_character",
                "compute": {"op": "cmf_slope", "period": p, "offset": offset}
            })

    for p in [10, 20, 30]:
        for mult in [1.5, 2.0, 3.0]:
            exprs.append({
                "name": f"hivol_pct_{p}_x{mult}",
                "category": "volume_character",
                "compute": {"op": "high_volume_bar_pct", "period": p,
                            "multiplier": mult, "avg_period": 50}
            })

    # ═══════════════════════════════════════════════════════
    # BOLLINGER — %B, bandwidth, squeeze detection
    # ═══════════════════════════════════════════════════════
    for p in [10, 15, 20, 30]:
        exprs.append({
            "name": f"bb_pctb_{p}",
            "category": "bollinger",
            "compute": {"op": "bollinger_pctb", "period": p}
        })
        exprs.append({
            "name": f"bb_bandwidth_{p}",
            "category": "bollinger",
            "compute": {"op": "bollinger_bandwidth", "period": p}
        })
        for lb in [60, 120, 252]:
            exprs.append({
                "name": f"bb_bw_rank_{p}_lb{lb}",
                "category": "bollinger",
                "compute": {"op": "bollinger_bandwidth_rank", "period": p, "lookback": lb}
            })

    # ═══════════════════════════════════════════════════════
    # MACD — Histogram, signal cross, line value
    # ═══════════════════════════════════════════════════════
    macd_configs = [(12, 26, 9), (8, 17, 9), (5, 13, 8)]

    for fast, slow, sig in macd_configs:
        exprs.append({
            "name": f"macd_hist_{fast}_{slow}_{sig}",
            "category": "macd",
            "compute": {"op": "macd_histogram", "fast": fast, "slow": slow, "signal": sig}
        })
        for offset in [1, 3, 5, 10]:
            exprs.append({
                "name": f"macd_hist_slope_{fast}_{slow}_{sig}_off{offset}",
                "category": "macd",
                "compute": {"op": "macd_histogram_slope", "fast": fast, "slow": slow,
                            "signal": sig, "offset": offset}
            })
        for norm in ["atr14", "adr14"]:
            exprs.append({
                "name": f"macd_line_{fast}_{slow}_{norm}",
                "category": "macd",
                "compute": {"op": "macd_line_norm", "fast": fast, "slow": slow,
                            "normalizer": norm}
            })

    # ═══════════════════════════════════════════════════════
    # AROON — Trend identification
    # ═══════════════════════════════════════════════════════
    for p in [14, 20, 25, 50]:
        exprs.append({
            "name": f"aroon_osc_{p}",
            "category": "aroon",
            "compute": {"op": "aroon_oscillator", "period": p}
        })
        exprs.append({
            "name": f"aroon_up_{p}",
            "category": "aroon",
            "compute": {"op": "aroon_up_val", "period": p}
        })
        exprs.append({
            "name": f"aroon_down_{p}",
            "category": "aroon",
            "compute": {"op": "aroon_down_val", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # EFFICIENCY — Kaufman Efficiency Ratio (trending vs choppy)
    # From ta_knowledge: "50 SMA is flat = no trend direction" (stage 3)
    # ═══════════════════════════════════════════════════════
    for p in [5, 10, 15, 20, 30, 50, 65]:
        exprs.append({
            "name": f"efficiency_{p}",
            "category": "efficiency",
            "compute": {"op": "kaufman_efficiency_ratio", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # MA STACK ORDER — Bullish/bearish stacking score
    # Full bull: 8ema > 21ema > 50sma > 200sma = 6
    # ═══════════════════════════════════════════════════════
    stack_combos = [
        ("stack_4ma", ["xavgc8", "xavgc21", "avgc50", "avgc200"]),
        ("stack_3ma_short", ["xavgc8", "xavgc21", "avgc50"]),
        ("stack_3ma_long", ["xavgc21", "avgc50", "avgc200"]),
        ("stack_2ma_fast", ["xavgc8", "xavgc21"]),
        ("stack_ema_sma", ["xavgc50", "avgc200"]),
    ]
    for name, mas in stack_combos:
        exprs.append({
            "name": name,
            "category": "ma_stack",
            "compute": {"op": "ma_stack_score", "mas": mas}
        })

    # ═══════════════════════════════════════════════════════
    # RANGE CONTRACTION / EXPANSION — Squeeze dynamics
    # From ta_knowledge: "Steeper channels more likely to snap"
    # ═══════════════════════════════════════════════════════
    for p in [5, 10, 15, 20, 30]:
        exprs.append({
            "name": f"range_contract_{p}",
            "category": "range_dynamics",
            "compute": {"op": "range_contraction_ratio", "period": p}
        })

    for p in [14]:
        for offset in [5, 10, 15, 20, 30, 50]:
            exprs.append({
                "name": f"atr_ratio_{p}_off{offset}",
                "category": "range_dynamics",
                "compute": {"op": "atr_ratio", "period": p, "offset": offset}
            })

    # ═══════════════════════════════════════════════════════
    # ROLLING VWAP — Distance to volume-weighted price
    # From ta_knowledge: AVWAP concepts
    # ═══════════════════════════════════════════════════════
    for p in [5, 10, 20, 30, 50, 65]:
        for norm in ["atr14", "adr14"]:
            exprs.append({
                "name": f"vwap_dist_{p}_{norm}",
                "category": "vwap",
                "compute": {"op": "vwap_distance", "period": p, "normalizer": norm}
            })

    # ═══════════════════════════════════════════════════════
    # NEAR SUPPORT — Price distance to prior lows (complement to near_resistance)
    # Critical for DTSS: price pulling back toward support levels
    # ═══════════════════════════════════════════════════════
    minl_periods = sorted(set(
        list(range(5, 65, 5)) + [3, 7, 65, 90, 120]
    ))
    for p in minl_periods:
        for price in ["C", "L"]:
            for norm in ["atr14", "adr14", "pct"]:
                exprs.append({
                    "name": f"ns_{price.lower()}_minl{p}_{norm}",
                    "category": "near_support",
                    "compute": {"op": "distance_to_minl", "price_ref": price,
                                "minl_period": p, "normalizer": norm}
                })
        exprs.append({
            "name": f"ns_ratio_minl{p}",
            "category": "near_support",
            "compute": {"op": "ratio_c_minl", "minl_period": p}
        })

    # ═══════════════════════════════════════════════════════
    # PERCENTILE RANK — Normalize any metric to 0-100 vs history
    # "Is this value high/low relative to recent history?"
    # ═══════════════════════════════════════════════════════
    for source in ["close", "volume", "range", "atr14", "rsi14"]:
        for period in [20, 50, 65, 120, 252]:
            exprs.append({
                "name": f"pctrank_{source}_{period}",
                "category": "percentile_rank",
                "compute": {"op": "percentile_rank", "source": source, "period": period}
            })

    # ═══════════════════════════════════════════════════════
    # SPREAD SLOPE — Is MA spread widening or narrowing?
    # Widening = trend strengthening, Narrowing = convergence/reversal
    # ═══════════════════════════════════════════════════════
    spread_slope_pairs = [
        ("xavgc8", "xavgc21"), ("xavgc8", "xavgc50"),
        ("xavgc21", "xavgc50"), ("xavgc21", "avgc50"),
        ("xavgc50", "xavgc200"), ("avgc50", "avgc200"),
    ]
    for fast, slow in spread_slope_pairs:
        for offset in [3, 5, 10, 20]:
            for norm in ["atr14", "adr14"]:
                exprs.append({
                    "name": f"spread_slope_{fast}_{slow}_off{offset}_{norm}",
                    "category": "spread_slope",
                    "compute": {"op": "spread_slope", "ma_fast": fast, "ma_slow": slow,
                                "offset": offset, "normalizer": norm}
                })

    # ═══════════════════════════════════════════════════════
    # SLOPE RATIOS — Fast MA slope / Slow MA slope
    # >1 = fast accelerating vs slow, <0 = diverging directions
    # ═══════════════════════════════════════════════════════
    slope_ratio_pairs = [
        ("xavgc8", "xavgc21"), ("xavgc8", "xavgc50"),
        ("xavgc21", "xavgc50"), ("xavgc50", "avgc200"),
    ]
    for fast, slow in slope_ratio_pairs:
        for offset in [3, 5, 10]:
            exprs.append({
                "name": f"slope_ratio_{fast}_{slow}_off{offset}",
                "category": "slope_ratio",
                "compute": {"op": "slope_ratio", "fast_ma": fast, "slow_ma": slow,
                            "offset": offset}
            })

    # ═══════════════════════════════════════════════════════
    # CONTINUOUS RVOL — Rolling average relative volume
    # Smooth measure of whether volume is building or fading
    # ═══════════════════════════════════════════════════════
    for period in [3, 5, 10, 15, 20]:
        for avg_period in [20, 50]:
            exprs.append({
                "name": f"rvol_cont_{period}d_avg{avg_period}",
                "category": "volume_continuous",
                "compute": {"op": "rvol_continuous", "period": period,
                            "avg_period": avg_period}
            })
            exprs.append({
                "name": f"rvol_cum_{period}d_avg{avg_period}",
                "category": "volume_continuous",
                "compute": {"op": "cumulative_rvol", "period": period,
                            "avg_period": avg_period}
            })

    # ═══════════════════════════════════════════════════════
    # SEPARATE HIGH/LOW RETRACEMENT — Where are H and L in the range?
    # Complements range_position (close-based) with high/low precision
    # ═══════════════════════════════════════════════════════
    for p in [10, 15, 20, 30, 50, 65, 120]:
        exprs.append({
            "name": f"retrace_high_{p}",
            "category": "retracement",
            "compute": {"op": "retrace_high", "period": p}
        })
        exprs.append({
            "name": f"retrace_low_{p}",
            "category": "retracement",
            "compute": {"op": "retrace_low", "period": p}
        })

    # ═══════════════════════════════════════════════════════
    # VWAP SLOPE — Direction of rolling VWAP
    # ═══════════════════════════════════════════════════════
    for p in [10, 20, 30, 50]:
        for offset in [3, 5, 10]:
            for norm in ["atr14", "adr14"]:
                exprs.append({
                    "name": f"vwap_slope_{p}_off{offset}_{norm}",
                    "category": "vwap",
                    "compute": {"op": "vwap_slope", "period": p, "offset": offset,
                                "normalizer": norm}
                })

    # ═══════════════════════════════════════════════════════
    # ADDITIONAL MOMENTUM — More stoch/CCI periods + extra RSI
    # ═══════════════════════════════════════════════════════
    for p in [3, 7, 10, 28, 50]:
        if f"stoch_{p}" not in [e["name"] for e in exprs]:
            exprs.append({
                "name": f"stoch_{p}",
                "category": "momentum",
                "compute": {"op": "stochastic", "period": p}
            })
    for p in [5, 7, 50]:
        if f"cci_{p}" not in [e["name"] for e in exprs]:
            exprs.append({
                "name": f"cci_{p}",
                "category": "momentum",
                "compute": {"op": "cci", "period": p}
            })

    # ═══════════════════════════════════════════════════════
    # EXPANDED BOOLEANS — New conditions for count_true/since_true
    # ═══════════════════════════════════════════════════════
    bool_conditions = [
        "c_gt_xavgc8", "c_gt_xavgc21", "c_gt_xavgc50", "c_gt_xavgc100",
        "c_gt_avgc50", "c_gt_avgc200",
        "c_lt_xavgc8", "c_lt_xavgc21", "c_lt_avgc50", "c_lt_avgc200",
        "c_gt_c1", "c_lt_c1",
        "h_gt_h1", "l_lt_l1",
        "v_gt_avgv20", "v_gt_2x_avgv20", "v_gt_avgv50",
        "v_lt_avgv20", "v_lt_half_avgv20",
        "c_gt_o",
        "xavgc8_gt_xavgc21", "xavgc50_gt_xavgc200", "avgc50_gt_avgc200",
        "xavgc21_gt_avgc50", "xavgc8_gt_avgc50",
        "avgc50_rising", "avgc50_falling",
        "avgc200_rising", "xavgc50_rising",
        "xavgc21_rising", "xavgc21_falling",
        "xavgc8_rising", "xavgc8_falling",
        "h_gt_maxh5_1", "h_gt_maxh10_1", "h_gt_maxh20_1",
        "l_lt_minl5_1", "l_lt_minl10_1", "l_lt_minl20_1",
        "c_gt_maxc10_1",
        "range_gt_atr", "body_gt_half_range",
        "c_upper_half", "c_lower_half",
        "inside_bar", "outside_bar",
        "gap_up", "gap_down", "big_gap_up", "big_gap_down",
        "diplus_gt_diminus",
        "rsi14_gt_50", "rsi14_gt_60", "rsi14_gt_70",
        "rsi14_lt_30", "rsi14_lt_40", "rsi14_lt_50",
        "adx14_gt_20", "adx14_gt_25", "adx14_gt_30", "adx14_lt_20",
        "c_gt_bbtop", "c_lt_bbbot",
        "cmf20_positive", "cmf20_negative",
    ]

    ct_periods = [3, 5, 7, 10, 15, 20, 30, 50]
    st_periods = [5, 10, 15, 20, 30, 50]
    tir_periods = [5, 10, 15, 20, 30]

    for cond in bool_conditions:
        for p in ct_periods:
            exprs.append({
                "name": f"ct_{cond}_{p}",
                "category": "boolean",
                "compute": {"op": "count_true", "condition": cond, "period": p}
            })
        for p in st_periods:
            exprs.append({
                "name": f"st_{cond}_{p}",
                "category": "boolean",
                "compute": {"op": "since_true", "condition": cond, "period": p}
            })
        for p in tir_periods:
            exprs.append({
                "name": f"tir_{cond}_{p}",
                "category": "boolean",
                "compute": {"op": "true_in_row", "condition": cond, "period": p}
            })

    return exprs




    os.makedirs(CACHE_DIR, exist_ok=True)
    exprs = generate_all()

    # Count by category
    cats = {}
    for e in exprs:
        cat = e["category"]
        cats[cat] = cats.get(cat, 0) + 1

    out_path = os.path.join(CACHE_DIR, "brute_expressions.json")
    with open(out_path, "w") as f:
        json.dump({"total": len(exprs), "by_category": cats, "expressions": exprs}, f)

    print(f"\n{'='*60}")
    print(f"  BRUTE FORCE EXPRESSION GENERATOR")
    print(f"{'='*60}")
    print(f"\n  Total: {len(exprs):,} expressions\n")
    for cat, n in sorted(cats.items()):
        print(f"    {cat:30s} {n:5,}")
    print(f"\n  Saved: {out_path}")

    # Estimate
    bool_count = cats.get("boolean", 0)
    arith_count = len(exprs) - bool_count
    # Base indicators: ~24ms/ticker on fast desktop
    # Arithmetic: ~0.02ms per expression per ticker
    # Booleans: ~1ms per expression per ticker (series computation)
    tickers = 4167
    base_s = tickers * 24 / 1000
    arith_s = tickers * arith_count * 0.02 / 1000
    bool_s = tickers * bool_count * 1 / 1000
    total_s = base_s + arith_s + bool_s
    print(f"\n  Estimated compute (4,167 tickers on desktop):")
    print(f"    Base indicators:    {base_s:6.0f}s ({base_s/60:.1f} min)")
    print(f"    Arithmetic ({arith_count:,}):  {arith_s:6.0f}s ({arith_s/60:.1f} min)")
    print(f"    Booleans ({bool_count:,}):    {bool_s:6.0f}s ({bool_s/60:.1f} min)")
    print(f"    TOTAL:              {total_s:6.0f}s ({total_s/60:.0f} min)")
    print()


if __name__ == "__main__":
    main()
