"""
Post-Signal Exit Expression Library — comprehensive library for exit grinder.

Every expression is evaluated at each forward bar relative to the signal bar.
The grinder tests every bar as a candidate exit and finds which expression
states correlate with maximum captured move.

Design principles:
    1. Forward window expansion: base expressions × 7 windows = delay-insensitive
    2. All TA knowledge concepts that CAN be computed ARE included
    3. Boolean aggregations (count_true, pct_true, since_true, true_in_row) for all bool conditions
    4. Extension structure (per ta_knowledge.md) is primary — ceiling ratios, slopes, dynamics
    5. MA reclaim sequences track structural recovery
    6. Volume character tracks institutional participation

Architecture:
    - Base expressions: ~330 single-bar point-in-time measures
    - Boolean conditions: ~150 (native + threshold-based)
    - Boolean aggregations: ~150 bools × 4 aggs × 7 windows = ~4,200
    - TOTAL: ~4,500+

Each expression has:
    - name: unique identifier
    - category: grouping
    - compute: dict with 'op' and parameters (dispatched by ExitExprEngine)
"""


# ═══════════════════════════════════════════════════════════════════
# Forward window sizes used for boolean aggregation expansion
# ═══════════════════════════════════════════════════════════════════
WINDOWS = [5, 10, 15, 20, 30, 40, 60]

# MAs used throughout
MAS = ["xavgc8", "xavgc12", "xavgc21", "avgc50", "avgc200"]
FAST_MAS = ["xavgc8", "xavgc12", "xavgc21"]
SLOW_MAS = ["avgc50", "avgc200"]
NORMS = ["adr14", "atr14"]


def generate_exit_expressions():
    """Generate the full base exit expression library (~330 expressions).
    
    These are single-bar point-in-time expressions evaluated at each forward bar.
    The exit_grinder expands these with boolean aggregations.
    """
    exprs = []

    # ═══════════════════════════════════════════════════════════
    # 1. MOVE CAPTURED — distance from entry to current bar
    #    Core benchmark metrics for scoring
    # ═══════════════════════════════════════════════════════════
    for price_ref in ["close", "low"]:
        for norm in ["adr14", "atr14", "pct"]:
            exprs.append({
                "name": f"move_captured_{price_ref}_{norm}",
                "category": "move_captured",
                "compute": {"op": "move_captured", "price_ref": price_ref, "normalizer": norm},
            })
    # MFE (max favorable excursion so far)
    for norm in ["adr14", "atr14", "pct"]:
        exprs.append({
            "name": f"mfe_{norm}",
            "category": "move_captured",
            "compute": {"op": "mfe", "normalizer": norm},
        })
    # Capture efficiency: current captured / MFE
    exprs.append({
        "name": "capture_efficiency",
        "category": "move_captured",
        "compute": {"op": "capture_efficiency"},
    })
    # Raw % move from entry
    exprs.append({
        "name": "move_pct",
        "category": "move_captured",
        "compute": {"op": "move_pct"},
    })

    # ═══════════════════════════════════════════════════════════
    # 2. EXTENSION FROM MA — where is price relative to MAs now?
    #    Per ta_knowledge.md: "Extension from 50 SMA (in multiples of ADR)
    #    is the universal normalized cycle indicator"
    # ═══════════════════════════════════════════════════════════
    for ma in MAS:
        for norm in NORMS:
            exprs.append({
                "name": f"ext_{ma}_{norm}",
                "category": "extension_from_ma",
                "compute": {"op": "extension", "ma": ma, "normalizer": norm},
            })
            # Extension ceiling ratio — per ta_knowledge.md:
            # "knowing a stock's typical max extension helps gauge where it is in its cycle"
            # "Use historical extension peak/valley clustering to improve fade timing"
            for lookback in [126, 252, 504, 1260]:
                exprs.append({
                    "name": f"ext_ceil_{ma}_{norm}_lb{lookback}",
                    "category": "extension_from_ma",
                    "compute": {"op": "ext_ceiling_ratio", "ma": ma,
                                "normalizer": norm, "lookback": lookback},
                })

    # ═══════════════════════════════════════════════════════════
    # 3. EXTENSION DYNAMICS — how is extension changing?
    #    Per ta_knowledge.md: "Trendline breaks on the 50 extension
    #    structure itself can confirm the move is starting"
    # ═══════════════════════════════════════════════════════════
    for ma in MAS:
        for norm in NORMS:
            # Extension slope (rate of change of extension)
            for slope_lb in [1, 2, 3, 5, 10]:
                exprs.append({
                    "name": f"ext_slope_{ma}_{norm}_{slope_lb}b",
                    "category": "extension_dynamics",
                    "compute": {"op": "ext_slope", "ma": ma,
                                "normalizer": norm, "offset": slope_lb},
                })
            # Extension retrace from its post-signal peak
            exprs.append({
                "name": f"ext_retrace_peak_{ma}_{norm}",
                "category": "extension_dynamics",
                "compute": {"op": "ext_retrace_from_peak", "ma": ma, "normalizer": norm},
            })
            # Extension acceleration (slope of slope)
            exprs.append({
                "name": f"ext_accel_{ma}_{norm}",
                "category": "extension_dynamics",
                "compute": {"op": "ext_accel", "ma": ma, "normalizer": norm},
            })

    # ═══════════════════════════════════════════════════════════
    # 4. MA RECLAIM — price crossing back above MAs
    #    Per ta_knowledge.md: "baby/daddy" MAs (8/21 EMA) are key
    #    references. Sequential MA reclaim = structural recovery.
    # ═══════════════════════════════════════════════════════════
    for ma in MAS:
        # Close above MA (boolean)
        exprs.append({
            "name": f"close_above_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "close_above_ma", "ma": ma},
        })
        # Bars since first reclaim
        exprs.append({
            "name": f"bars_since_reclaim_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "bars_since_reclaim", "ma": ma},
        })
        # Bars since touch (price reached the MA level)
        exprs.append({
            "name": f"bars_since_touch_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "bars_since_touch_ma", "ma": ma},
        })
        # Reclaim then lost (reclaimed above but then fell back below)
        exprs.append({
            "name": f"reclaim_then_lost_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "reclaim_then_lost", "ma": ma},
        })
        # Distance from MA (signed, normalized)
        for norm in NORMS:
            exprs.append({
                "name": f"dist_from_{ma}_{norm}",
                "category": "ma_reclaim",
                "compute": {"op": "distance_from_ma", "ma": ma, "normalizer": norm},
            })

    # Sequential reclaim pairs — per ta_knowledge.md:
    # "baby and daddy to sequentially confirm above the breakout AVWAP"
    for i, fast_ma in enumerate(MAS[:-1]):
        for slow_ma in MAS[i + 1:]:
            exprs.append({
                "name": f"reclaimed_{fast_ma}_and_{slow_ma}",
                "category": "ma_reclaim",
                "compute": {"op": "sequential_reclaim", "ma_fast": fast_ma, "ma_slow": slow_ma},
            })

    # ═══════════════════════════════════════════════════════════
    # 5. MOMENTUM REVERSAL — momentum turning against the move
    # ═══════════════════════════════════════════════════════════
    # RSI at multiple periods
    for p in [5, 7, 9, 14, 21]:
        exprs.append({"name": f"rsi_{p}", "category": "momentum_reversal",
                       "compute": {"op": "rsi", "period": p}})
        # RSI slope
        for sl in [1, 3, 5]:
            exprs.append({"name": f"rsi_{p}_slope_{sl}", "category": "momentum_reversal",
                           "compute": {"op": "rsi_slope", "period": p, "offset": sl}})

    # Rate of change
    for roc_p in [1, 2, 3, 5, 10, 20]:
        exprs.append({"name": f"roc_{roc_p}", "category": "momentum_reversal",
                       "compute": {"op": "roc", "period": roc_p}})

    # MACD histogram + slope
    for fast, slow, sig in [(8, 17, 9), (12, 26, 9), (5, 13, 8)]:
        exprs.append({"name": f"macd_hist_{fast}_{slow}_{sig}", "category": "momentum_reversal",
                       "compute": {"op": "macd_histogram", "fast": fast, "slow": slow, "signal": sig}})
        for sl in [1, 3, 5]:
            exprs.append({"name": f"macd_hist_slope_{fast}_{slow}_{sig}_{sl}b", "category": "momentum_reversal",
                           "compute": {"op": "macd_histogram_slope", "fast": fast, "slow": slow,
                                       "signal": sig, "offset": sl}})

    # Stochastic
    for p in [5, 9, 14]:
        exprs.append({"name": f"stoch_{p}", "category": "momentum_reversal",
                       "compute": {"op": "stochastic", "period": p}})

    # ADX + DI spread
    for p in [7, 14, 21]:
        exprs.append({"name": f"adx_{p}", "category": "momentum_reversal",
                       "compute": {"op": "adx", "period": p}})
        for sl in [1, 3, 5]:
            exprs.append({"name": f"adx_{p}_slope_{sl}", "category": "momentum_reversal",
                           "compute": {"op": "adx_slope", "period": p, "offset": sl}})
        exprs.append({"name": f"di_spread_{p}", "category": "momentum_reversal",
                       "compute": {"op": "di_spread", "period": p}})

    # ═══════════════════════════════════════════════════════════
    # 6. CANDLE CHARACTER — bar quality and patterns
    #    Per ta_knowledge.md: "tight candle" behavior matters
    # ═══════════════════════════════════════════════════════════
    for norm in NORMS:
        exprs.append({"name": f"bar_range_{norm}", "category": "candle_character",
                       "compute": {"op": "bar_range", "normalizer": norm}})
    exprs.append({"name": "body_range_ratio", "category": "candle_character",
                   "compute": {"op": "body_range_ratio"}})
    exprs.append({"name": "upper_wick_ratio", "category": "candle_character",
                   "compute": {"op": "upper_wick_ratio"}})
    exprs.append({"name": "lower_wick_ratio", "category": "candle_character",
                   "compute": {"op": "lower_wick_ratio"}})
    exprs.append({"name": "is_green", "category": "candle_character",
                   "compute": {"op": "is_green"}})
    exprs.append({"name": "is_doji", "category": "candle_character",
                   "compute": {"op": "is_doji"}})

    # Gap from prior bar
    for norm in NORMS:
        exprs.append({"name": f"gap_from_prior_{norm}", "category": "candle_character",
                       "compute": {"op": "gap_from_prior", "normalizer": norm}})

    # Rolling candle stats — captures "tight candle" patterns from ta_knowledge.md
    for window in [3, 5, 10, 20]:
        exprs.append({"name": f"pct_green_{window}b", "category": "candle_character",
                       "compute": {"op": "pct_green_rolling", "period": window}})
        exprs.append({"name": f"avg_body_ratio_{window}b", "category": "candle_character",
                       "compute": {"op": "avg_body_ratio_rolling", "period": window}})
        exprs.append({"name": f"avg_range_adr_{window}b", "category": "candle_character",
                       "compute": {"op": "avg_bar_range_rolling", "period": window, "normalizer": "adr14"}})
        exprs.append({"name": f"avg_range_atr_{window}b", "category": "candle_character",
                       "compute": {"op": "avg_bar_range_rolling", "period": window, "normalizer": "atr14"}})
        # Consecutive green/red streaks
        exprs.append({"name": f"consec_green_{window}b", "category": "candle_character",
                       "compute": {"op": "consecutive_green", "period": window}})
        exprs.append({"name": f"consec_red_{window}b", "category": "candle_character",
                       "compute": {"op": "consecutive_red", "period": window}})

    # ═══════════════════════════════════════════════════════════
    # 7. VOLUME CHARACTER — institutional participation
    #    Per ta_knowledge.md: RVOL, volume trends, up/down volume
    # ═══════════════════════════════════════════════════════════
    for avg_p in [10, 20, 50]:
        exprs.append({"name": f"rvol_{avg_p}", "category": "volume_character",
                       "compute": {"op": "rvol", "avg_period": avg_p}})

    for window in [3, 5, 10, 20]:
        exprs.append({"name": f"avg_rvol_{window}b", "category": "volume_character",
                       "compute": {"op": "avg_rvol_rolling", "period": window, "avg_period": 20}})
        exprs.append({"name": f"up_vol_ratio_{window}b", "category": "volume_character",
                       "compute": {"op": "up_vol_ratio_rolling", "period": window}})
        exprs.append({"name": f"down_vol_ratio_{window}b", "category": "volume_character",
                       "compute": {"op": "down_vol_ratio_rolling", "window": window}})
        exprs.append({"name": f"vol_trend_{window}b", "category": "volume_character",
                       "compute": {"op": "vol_trend_rolling", "window": window}})
        exprs.append({"name": f"obv_slope_{window}b", "category": "volume_character",
                       "compute": {"op": "obv_slope", "period": window}})

    # Volume relative to signal bar
    exprs.append({"name": "vol_vs_signal_bar", "category": "volume_character",
                   "compute": {"op": "vol_vs_signal_bar"}})
    # Volume rank in post-signal window
    for window in [10, 20]:
        exprs.append({"name": f"vol_rank_post_{window}b", "category": "volume_character",
                       "compute": {"op": "vol_rank_post_signal", "period": window}})

    # ═══════════════════════════════════════════════════════════
    # 8. STRUCTURAL — key levels reached
    #    Per ta_knowledge.md: 50 SMA and 200 SMA are major structural
    #    levels. Baby/daddy (8/21 EMA) for short-term structure.
    # ═══════════════════════════════════════════════════════════
    for ma in MAS:
        exprs.append({"name": f"touched_{ma}", "category": "structural",
                       "compute": {"op": "touched_ma", "ma": ma}})
        exprs.append({"name": f"closed_below_{ma}", "category": "structural",
                       "compute": {"op": "closed_below_ma", "ma": ma}})
        exprs.append({"name": f"bars_since_touch_{ma}", "category": "structural",
                       "compute": {"op": "bars_since_touch_ma", "ma": ma}})

    # New lows / new highs (structural progression)
    for p in [5, 10, 20]:
        exprs.append({"name": f"new_low_count_{p}", "category": "structural",
                       "compute": {"op": "new_low_count", "period": p}})
        exprs.append({"name": f"new_high_count_{p}", "category": "structural",
                       "compute": {"op": "new_high_count", "period": p}})
        exprs.append({"name": f"higher_low_formed_{p}", "category": "structural",
                       "compute": {"op": "higher_low_formed", "period": p}})

    # Lower-low sequences (trend continuation for shorts)
    for p in [5, 10, 20]:
        exprs.append({"name": f"lower_low_seq_{p}", "category": "structural",
                       "compute": {"op": "lower_low_sequence", "period": p}})

    # Below signal bar low
    exprs.append({"name": "below_signal_bar_low", "category": "structural",
                   "compute": {"op": "below_signal_bar_low"}})

    # ═══════════════════════════════════════════════════════════
    # 9. RANGE COMPRESSION — move stalling, consolidation
    #    Per ta_knowledge.md: "tight candle = price sat and traded
    #    around" — range compression is how the move stalls
    # ═══════════════════════════════════════════════════════════
    for window in [3, 5, 10, 14, 20]:
        exprs.append({"name": f"atr_ratio_vs_entry_{window}b", "category": "range_compression",
                       "compute": {"op": "atr_ratio_vs_entry", "period": 14, "offset": window}})
        exprs.append({"name": f"range_contracting_{window}b", "category": "range_compression",
                       "compute": {"op": "range_contracting", "window": window}})
        exprs.append({"name": f"inside_bar_count_{window}b", "category": "range_compression",
                       "compute": {"op": "inside_bar_count", "period": window}})

    for p in [10, 20]:
        exprs.append({"name": f"bb_bandwidth_{p}", "category": "range_compression",
                       "compute": {"op": "bollinger_bandwidth", "period": p}})
        exprs.append({"name": f"bb_pctb_{p}", "category": "range_compression",
                       "compute": {"op": "bollinger_pctb", "period": p}})
        for lb in [50, 126, 252]:
            exprs.append({"name": f"bb_bw_rank_{p}_{lb}", "category": "range_compression",
                           "compute": {"op": "bollinger_bandwidth_rank", "period": p, "lookback": lb}})

    # ═══════════════════════════════════════════════════════════
    # 10. RETRACEMENT — giving back the move
    # ═══════════════════════════════════════════════════════════
    for norm in ["adr14", "atr14", "pct"]:
        exprs.append({"name": f"retrace_from_mfe_{norm}", "category": "retracement",
                       "compute": {"op": "retrace_from_mfe", "normalizer": norm}})
    exprs.append({"name": "retrace_from_mfe_pct_raw", "category": "retracement",
                   "compute": {"op": "retrace_from_mfe_pct"}})
    exprs.append({"name": "position_in_post_range", "category": "retracement",
                   "compute": {"op": "position_in_post_range"}})
    exprs.append({"name": "bars_since_mfe", "category": "retracement",
                   "compute": {"op": "bars_since_mfe"}})
    for n in [3, 5, 10, 20]:
        exprs.append({"name": f"mfe_expanding_{n}b", "category": "retracement",
                       "compute": {"op": "mfe_expanding", "period": n}})

    # ═══════════════════════════════════════════════════════════
    # 11. TIME — bars since signal, velocity, pace
    # ═══════════════════════════════════════════════════════════
    exprs.append({"name": "bars_since_signal", "category": "time",
                   "compute": {"op": "bars_since_signal"}})
    for norm in ["adr14", "atr14", "pct"]:
        exprs.append({"name": f"move_per_bar_{norm}", "category": "time",
                       "compute": {"op": "move_per_bar", "normalizer": norm}})
    # Velocity change (acceleration/deceleration of the move)
    exprs.append({"name": "velocity_accelerating", "category": "time",
                   "compute": {"op": "velocity_change", "direction": "increasing"}})
    exprs.append({"name": "velocity_decelerating", "category": "time",
                   "compute": {"op": "velocity_change", "direction": "decreasing"}})

    # ═══════════════════════════════════════════════════════════
    # 12. RELATIVE STRENGTH — stock vs SPY
    #     Per ta_knowledge.md: market regime affects everything
    # ═══════════════════════════════════════════════════════════
    for window in [5, 10, 20]:
        exprs.append({"name": f"rs_vs_spy_{window}", "category": "relative_strength",
                       "compute": {"op": "rs_vs_spy", "period": window}})
        exprs.append({"name": f"rs_vs_spy_slope_{window}", "category": "relative_strength",
                       "compute": {"op": "rs_vs_spy_slope", "period": window}})

    # ═══════════════════════════════════════════════════════════
    # 13. LSP STRUCTURE — Last Structural Pivot levels post-entry
    #     Per ta_knowledge.md: "LSP = the most prominent pivot
    #     high/low visible on the left side of the chart"
    #     "LSP + algo line convergence = high-probability reaction zone"
    #
    #     For exit: levels are FROZEN at entry bar, then we track
    #     how price relates to those frozen levels as the trade
    #     progresses. Key signals: approaching/touching/breaking
    #     the nearest overhead resistance or support.
    #
    #     LSP levels computed ONCE per example (expensive), then
    #     evaluated cheaply per forward bar.
    # ═══════════════════════════════════════════════════════════

    # Distance to nearest LSP above/below (ATR-normalized) at each forward bar
    # Positive = price hasn't reached the level yet; negative = price passed it
    for direction in ["above", "below"]:
        for rank in [1, 2, 3]:
            exprs.append({
                "name": f"lsp_{direction}{rank}_distance_atr",
                "category": "lsp_structure",
                "compute": {"op": "lsp_distance", "direction": direction,
                            "rank": rank, "normalizer": "atr"},
            })

    # LSP break detection — did price break through the level?
    # For shorts: breaking below support = continuation (good)
    # For longs: breaking above resistance = continuation (good)
    for direction in ["above", "below"]:
        for rank in [1, 2, 3]:
            exprs.append({
                "name": f"lsp_{direction}{rank}_broken",
                "category": "lsp_structure",
                "compute": {"op": "lsp_broken", "direction": direction, "rank": rank},
            })

    # LSP level count in proximity — congestion detection
    # How many clustered levels exist within N ATR above/below current price?
    for atr_range in [1.0, 2.0, 3.0]:
        exprs.append({
            "name": f"lsp_levels_within_{str(atr_range).replace('.','_')}atr",
            "category": "lsp_structure",
            "compute": {"op": "lsp_congestion", "atr_range": atr_range},
        })

    # Distance to nearest unbroken level (the real obstacle)
    for direction in ["above", "below"]:
        exprs.append({
            "name": f"lsp_{direction}_nearest_unbroken_dist",
            "category": "lsp_structure",
            "compute": {"op": "lsp_nearest_unbroken", "direction": direction},
        })

    return exprs


def generate_exit_boolean_conditions(base_exprs):
    """Generate boolean conditions from base expressions.
    
    Two types:
    1. Native booleans (ops that return 0/1 directly)
    2. Threshold booleans (continuous expressions above/below key levels)
    
    These get aggregated over forward windows for delay-insensitive detection.
    """
    # ─────────────────────────────────────────────
    # Native boolean ops (already return 0/1)
    # ─────────────────────────────────────────────
    native_bool_ops = {
        "close_above_ma", "sequential_reclaim", "is_green", "is_doji",
        "touched_ma", "closed_below_ma", "below_signal_bar_low",
        "higher_low_formed", "mfe_expanding", "reclaim_then_lost",
        "range_contracting", "lsp_broken",
    }

    native_bools = [e for e in base_exprs if e["compute"]["op"] in native_bool_ops]

    # ─────────────────────────────────────────────
    # Threshold-based boolean conditions
    # ─────────────────────────────────────────────
    threshold_bools = []

    # RSI above key levels
    for p in [5, 7, 14, 21]:
        for thresh in [30, 40, 50, 60, 70]:
            threshold_bools.append({
                "name": f"rsi_{p}_above_{thresh}",
                "condition": {"base_op": "rsi", "period": p, "threshold": thresh, "direction": "above"},
            })

    # ROC positive/negative
    for roc_p in [1, 3, 5, 10]:
        threshold_bools.append({
            "name": f"roc_{roc_p}_positive",
            "condition": {"base_op": "roc", "period": roc_p, "threshold": 0, "direction": "above"},
        })
        threshold_bools.append({
            "name": f"roc_{roc_p}_negative",
            "condition": {"base_op": "roc", "period": roc_p, "threshold": 0, "direction": "below"},
        })

    # Stochastic above key levels
    for p in [5, 14]:
        for thresh in [20, 50, 80]:
            threshold_bools.append({
                "name": f"stoch_{p}_above_{thresh}",
                "condition": {"base_op": "stochastic", "period": p,
                              "threshold": thresh, "direction": "above"},
            })

    # ADX declining / strong trend
    for p in [7, 14]:
        threshold_bools.append({
            "name": f"adx_{p}_declining",
            "condition": {"base_op": "adx_slope", "period": p, "offset": 3,
                          "threshold": 0, "direction": "below"},
        })
        for thresh in [20, 25, 30]:
            threshold_bools.append({
                "name": f"adx_{p}_above_{thresh}",
                "condition": {"base_op": "adx", "period": p,
                              "threshold": thresh, "direction": "above"},
            })

    # DI spread direction
    for p in [7, 14]:
        threshold_bools.append({
            "name": f"di_spread_{p}_positive",
            "condition": {"base_op": "di_spread", "period": p,
                          "threshold": 0, "direction": "above"},
        })

    # MACD histogram direction + slope direction
    for fast, slow, sig in [(8, 17, 9), (12, 26, 9)]:
        threshold_bools.append({
            "name": f"macd_hist_{fast}_{slow}_{sig}_positive",
            "condition": {"base_op": "macd_histogram", "fast": fast, "slow": slow,
                          "signal": sig, "threshold": 0, "direction": "above"},
        })
        threshold_bools.append({
            "name": f"macd_hist_slope_{fast}_{slow}_{sig}_positive",
            "condition": {"base_op": "macd_histogram_slope", "fast": fast, "slow": slow,
                          "signal": sig, "offset": 3, "threshold": 0, "direction": "above"},
        })

    # Capture efficiency above thresholds
    for thresh in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        threshold_bools.append({
            "name": f"capture_eff_above_{int(thresh * 100)}",
            "condition": {"base_op": "capture_efficiency",
                          "threshold": thresh, "direction": "above"},
        })

    # Extension slope positive (extension recovering = move stalling for shorts)
    for ma in MAS:
        for norm in NORMS:
            threshold_bools.append({
                "name": f"ext_slope_{ma}_{norm}_positive",
                "condition": {"base_op": "ext_slope", "ma": ma, "normalizer": norm,
                              "offset": 3, "threshold": 0, "direction": "above"},
            })

    # Extension ceiling ratio above levels (approaching historical limits)
    # Per ta_knowledge.md: "short at the statistical ceiling"
    for ma in ["xavgc21", "avgc50", "avgc200"]:
        for norm in ["adr14"]:
            for thresh in [0.5, 0.7, 0.8, 0.9]:
                threshold_bools.append({
                    "name": f"ext_ceil_{ma}_{norm}_above_{int(thresh*100)}",
                    "condition": {"base_op": "ext_ceiling_ratio", "ma": ma,
                                  "normalizer": norm, "lookback": 252,
                                  "threshold": thresh, "direction": "above"},
                })

    # Retrace from MFE above thresholds (giving back the move)
    for thresh in [0.1, 0.2, 0.3, 0.5]:
        threshold_bools.append({
            "name": f"retrace_mfe_pct_above_{int(thresh*100)}",
            "condition": {"base_op": "retrace_from_mfe_pct",
                          "threshold": thresh, "direction": "above"},
        })

    # Volume character thresholds
    for avg_p in [20]:
        for thresh in [0.5, 0.8, 1.5, 2.0]:
            threshold_bools.append({
                "name": f"rvol_{avg_p}_above_{str(thresh).replace('.','_')}",
                "condition": {"base_op": "rvol", "avg_period": avg_p,
                              "threshold": thresh, "direction": "above"},
            })

    # Bar range thresholds (wide/narrow bars)
    for thresh in [0.5, 1.0, 1.5, 2.0]:
        threshold_bools.append({
            "name": f"bar_range_adr_above_{str(thresh).replace('.','_')}",
            "condition": {"base_op": "bar_range", "normalizer": "adr14",
                          "threshold": thresh, "direction": "above"},
        })

    # Bollinger %B thresholds
    for p in [20]:
        for thresh in [0.0, 0.2, 0.5, 0.8, 1.0]:
            threshold_bools.append({
                "name": f"bb_pctb_{p}_above_{str(thresh).replace('.','_')}",
                "condition": {"base_op": "bollinger_pctb", "period": p,
                              "threshold": thresh, "direction": "above"},
            })

    # LSP level broken (price passed through the frozen level)
    for lsp_dir in ["above", "below"]:
        for rank in [1, 2, 3]:
            threshold_bools.append({
                "name": f"lsp_{lsp_dir}{rank}_is_broken",
                "condition": {"base_op": "lsp_broken", "lsp_direction": lsp_dir,
                              "rank": rank, "threshold": 0.5, "direction": "above"},
            })

    # LSP distance thresholds — approaching the level
    for lsp_dir in ["above", "below"]:
        for rank in [1]:  # Only nearest level for threshold bools
            for thresh in [0.5, 1.0, 2.0]:
                threshold_bools.append({
                    "name": f"lsp_{lsp_dir}{rank}_within_{str(thresh).replace('.','_')}atr",
                    "condition": {"base_op": "lsp_distance", "lsp_direction": lsp_dir,
                                  "rank": rank, "normalizer": "atr",
                                  "threshold": thresh, "direction": "below"},
                })

    # LSP congestion — many levels nearby (dense S/R zone)
    for atr_range in [2.0, 3.0]:
        for thresh in [2, 3, 5]:
            threshold_bools.append({
                "name": f"lsp_congestion_{str(atr_range).replace('.','_')}atr_above_{thresh}",
                "condition": {"base_op": "lsp_congestion", "atr_range": atr_range,
                              "threshold": thresh, "direction": "above"},
            })

    # RS vs SPY thresholds
    for window in [10, 20]:
        threshold_bools.append({
            "name": f"rs_spy_{window}_positive",
            "condition": {"base_op": "rs_vs_spy", "period": window,
                          "threshold": 0, "direction": "above"},
        })

    return native_bools, threshold_bools


def generate_all_exit_expressions():
    """Generate complete exit expression library with boolean aggregations.
    
    Returns dict with:
        base_expressions: list of base expression dicts
        native_booleans: list of native bool expressions from base
        threshold_booleans: list of threshold boolean condition defs
        boolean_aggregations: list of windowed aggregation expressions
        stats: summary counts
    """
    base = generate_exit_expressions()
    native_bools, threshold_bools = generate_exit_boolean_conditions(base)

    # ─────────────────────────────────────────────
    # Boolean aggregation expressions
    # Each boolean × 4 aggregation types × 7 windows
    # ─────────────────────────────────────────────
    all_bool_names = [e["name"] for e in native_bools] + [tb["name"] for tb in threshold_bools]

    agg_exprs = []
    agg_types = ["count_true", "pct_true", "since_true", "true_in_row"]

    for bool_name in all_bool_names:
        for agg in agg_types:
            for w in WINDOWS:
                agg_exprs.append({
                    "name": f"{bool_name}_{agg}_{w}b",
                    "category": "boolean",
                    "compute": {"op": f"bool_{agg}", "condition": bool_name, "period": w},
                })

    # ─────────────────────────────────────────────
    # Summary stats
    # ─────────────────────────────────────────────
    cats = {}
    for e in base:
        c = e["category"]
        cats[c] = cats.get(c, 0) + 1

    return {
        "base_expressions": base,
        "native_booleans": native_bools,
        "threshold_booleans": threshold_bools,
        "boolean_aggregations": agg_exprs,
        "stats": {
            "base_count": len(base),
            "native_bool_count": len(native_bools),
            "threshold_bool_count": len(threshold_bools),
            "total_bool_conditions": len(all_bool_names),
            "bool_agg_count": len(agg_exprs),
            "total": len(base) + len(agg_exprs),
            "categories": cats,
        },
    }


if __name__ == "__main__":
    lib = generate_all_exit_expressions()
    stats = lib["stats"]
    print("Post-Signal Exit Expression Library")
    print("=" * 60)
    print(f"\nBase expressions: {stats['base_count']}")
    for cat, n in sorted(stats["categories"].items(), key=lambda x: -x[1]):
        print(f"  {cat:25s}: {n}")
    print(f"\nNative boolean conditions: {stats['native_bool_count']}")
    print(f"Threshold boolean conditions: {stats['threshold_bool_count']}")
    print(f"Total boolean conditions: {stats['total_bool_conditions']}")
    print(f"Boolean aggregations: {stats['bool_agg_count']} "
          f"({stats['total_bool_conditions']} bools × 4 aggs × {len(WINDOWS)} windows)")
    print(f"\nTOTAL EXPRESSIONS: {stats['total']}")
